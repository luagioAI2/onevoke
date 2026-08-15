#!/usr/bin/env python3

"""llm-agent — 把无官方 CLI 的模型 (DeepSeek / GLM) 变成 Onevoke 任务进程.

Onevoke 的 Agent 抽象是「PATH 上的一个 CLI」: kanban start 拉起它执行任务,
审核 wrapper 只读调用它做审核. 本适配器让 DeepSeek / GLM 这两个只有
OpenAI 兼容 API 的模型满足该契约:

  llm-agent --version                      能力探测 (doctor / welcome)
  llm-agent exec --provider X <prompt>     执行任务: 交互式工具循环 (YOLO)
  llm-agent review --provider X --cwd DIR  只读审核: 只读工具集, 不改文件

二进制薄壳 (bin/deepseek, bin/glm) 把 provider 固定后转发到本模块.

工具循环: 调 chat/completions (function calling), 执行工具调用, 把结果作为
role=tool 消息继续, 直到模型不再请求工具. 429 / 5xx 用指数退避重试.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


VERSION = "llm-agent 0.1.0 (onevoke api adapter)"

PROVIDERS: dict[str, dict[str, str]] = {
    "deepseek": {
        "base_url": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "api_key_env": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-chat",
    },
    "glm": {
        "base_url": os.environ.get(
            "GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"
        ),
        "api_key_env": "GLM_API_KEY",
        "default_model": "glm-4.5",
    },
}

MAX_TURNS = 100
MAX_TOOL_OUTPUT_CHARS = 20000
MAX_READ_CHARS = 200_000
RETRY_STATUSES = (429, 500, 502, 503, 504)
RETRY_ATTEMPTS = 5
RETRY_BASE_SECONDS = 1.0


class AdapterError(Exception):
    pass


def die(message: str) -> None:
    print(f"llm-agent: {message}", file=sys.stderr)
    raise SystemExit(1)


def provider_config(provider: str) -> dict[str, str]:
    try:
        return PROVIDERS[provider]
    except KeyError:
        die(f"不支持的 provider: {provider} (可选: {', '.join(PROVIDERS)})")


def api_base_url(provider: str) -> str:
    """测试可用 LLM_AGENT_BASE_URL 覆盖."""
    return os.environ.get("LLM_AGENT_BASE_URL", provider_config(provider)["base_url"])


# ---------------------------------------------------------------------------
# OpenAI 兼容 chat/completions 客户端
# ---------------------------------------------------------------------------

def chat_completion(
    provider: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """带 429/5xx 指数退避的 chat/completions 调用."""
    base = api_base_url(provider).rstrip("/")
    url = base + "/chat/completions"
    api_key = os.environ.get(provider_config(provider)["api_key_env"], "")
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    body = json.dumps(payload).encode("utf-8")

    attempts = int(os.environ.get("LLM_AGENT_RETRY_ATTEMPTS", str(RETRY_ATTEMPTS)))
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as error:
            if error.code not in RETRY_STATUSES:
                raise AdapterError(f"API 返回 HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:500]}")
            if attempt >= attempts:
                raise AdapterError(f"API 持续返回 HTTP {error.code}, 已重试 {attempts} 次")
            delay = RETRY_BASE_SECONDS * (2 ** (attempt - 1)) * (0.5 + ((time.time_ns() % 1000) / 1000))
            print(f"llm-agent: HTTP {error.code}, {delay:.1f}s 后重试 ({attempt}/{attempts})", file=sys.stderr)
            time.sleep(delay)
        except urllib.error.URLError as error:
            raise AdapterError(f"无法连接 API: {error.reason}")
    raise AdapterError("重试耗尽")  # pragma: no cover


# ---------------------------------------------------------------------------
# 工具集
# ---------------------------------------------------------------------------

def _tool(name: str, description: str, properties: dict[str, Any],
          required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


READ_TOOLS: list[dict[str, Any]] = [
    _tool("read_file", "读取文本文件内容. path 必须是绝对路径; start/end 为可选行号 (1 起).",
          {"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}},
          ["path"]),
    _tool("list_dir", "列出目录下的直接子项 (文件/目录名).",
          {"path": {"type": "string"}}, ["path"]),
    _tool("grep", "在文件或目录中用正则搜索, 返回匹配行.",
          {"pattern": {"type": "string"}, "path": {"type": "string"}},
          ["pattern", "path"]),
]

WRITE_TOOLS: list[dict[str, Any]] = [
    _tool("write_file", "创建或整体覆盖文本文件 (自动创建父目录).",
          {"path": {"type": "string"}, "content": {"type": "string"}},
          ["path", "content"]),
    _tool("edit_file", "把文件中第一处 old 文本替换为 new 文本.",
          {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
          ["path", "old", "new"]),
    _tool("run_command", "在 shell 中执行命令 (cwd 为当前项目). 输出截断到 20K 字符.",
          {"command": {"type": "string"}}, ["command"]),
]

FINISH_TOOL: list[dict[str, Any]] = [
    _tool("finished", "声明任务完成, 附最终总结. 调用后结束本次会话.",
          {"message": {"type": "string"}}, ["message"]),
]


def _tool_result_text(name: str, arguments: dict[str, Any], cwd: Path) -> str:
    if name == "read_file":
        path = Path(arguments["path"])
        if not path.is_absolute():
            path = cwd / path
        try:
            data = path.read_bytes()[:MAX_READ_CHARS]
        except OSError as error:
            return f"错误: {error}"
        lines = data.decode("utf-8", "replace").splitlines()
        start = int(arguments.get("start", 1))
        end = int(arguments.get("end", len(lines)))
        selected = lines[max(0, start - 1): end]
        numbered = "\n".join(
            f"{i + start:5d} | {line}" for i, line in enumerate(selected)
        )
        return f"文件 {path} 共 {len(lines)} 行:\n{numbered}"[:MAX_TOOL_OUTPUT_CHARS]
    if name == "list_dir":
        path = Path(arguments["path"])
        if not path.is_absolute():
            path = cwd / path
        try:
            children = sorted(
                (entry.name + ("/" if entry.is_dir() else "") for entry in path.iterdir())
            )
        except OSError as error:
            return f"错误: {error}"
        return "\n".join(children) or "(空目录)"
    if name == "grep":
        path = Path(arguments["path"])
        if not path.is_absolute():
            path = cwd / path
        pattern = arguments["pattern"]
        matches: list[str] = []
        try:
            targets = [path] if path.is_file() else sorted(path.rglob("*"))
            for target in targets:
                if not target.is_file():
                    continue
                try:
                    for lineno, line in enumerate(
                        target.read_text(encoding="utf-8", errors="replace").splitlines(), 1
                    ):
                        if re.search(pattern, line):
                            matches.append(f"{target}:{lineno}: {line}")
                except OSError:
                    continue
        except OSError as error:
            return f"错误: {error}"
        return "\n".join(matches[:200]) or "(无匹配)"[:MAX_TOOL_OUTPUT_CHARS]
    if name == "write_file":
        path = Path(arguments["path"])
        if not path.is_absolute():
            path = cwd / path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(arguments["content"], encoding="utf-8")
        except OSError as error:
            return f"错误: {error}"
        return f"已写入 {path} ({len(arguments['content'])} 字符)"
    if name == "edit_file":
        path = Path(arguments["path"])
        if not path.is_absolute():
            path = cwd / path
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            return f"错误: {error}"
        old = arguments["old"]
        new = arguments.get("new", "")
        if old not in text:
            return f"错误: 未找到要替换的文本 (旧文本 {len(old)} 字符)"
        text = text.replace(old, new, 1)
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as error:
            return f"错误: {error}"
        return f"已替换 {path} 中的 1 处文本"
    if name == "run_command":
        result = subprocess.run(
            arguments["command"],
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=600,
        )
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            return f"退出码 {result.returncode}:\n{output}"[:MAX_TOOL_OUTPUT_CHARS]
        return output[:MAX_TOOL_OUTPUT_CHARS] or "(命令无输出)"
    if name == "finished":
        return "OK"
    return f"未知工具: {name}"


def available_tools(mode: str) -> list[dict[str, Any]]:
    tools = list(READ_TOOLS)
    if mode == "exec":
        tools += WRITE_TOOLS
    tools += FINISH_TOOL
    return tools


def system_prompt(mode: str, cwd: Path, model: str, effort: str) -> str:
    if mode == "exec":
        return (
            "你是 Onevoke 的任务执行 Agent, 通过工具修改当前项目完成用户给出的任务.\n"
            f"工作目录: {cwd}\n"
            f"模型: {model}; 推理强度: {effort}\n"
            "可用工具: read_file / list_dir / grep / write_file / edit_file / "
            "run_command / finished.\n"
            "规则:\n"
            "- 先读任务相关的文件, 再动手修改; 不要臆测不存在的内容.\n"
            "- 修改后用 run_command 运行最小验证; 失败要如实报告.\n"
            "- 完成任务后必须调用 finished(message) 并附最终总结, 否则会话不会结束.\n"
            "- 不要输出 JSON; 用自然语言解释你的进展.\n"
        )
    return (
        "你是 Onevoke 的只读审核 Agent, 只能检查代码, 严禁修改任何文件.\n"
        f"工作目录: {cwd}\n"
        f"模型: {model}; 推理强度: {effort}\n"
        "可用工具: read_file / list_dir / grep (只读).\n"
        "规则:\n"
        "- 只读操作; 任何写操作都被拒绝.\n"
        "- 根据用户提供的审核要求完成审查, 最后用自然语言输出审核报告.\n"
    )


# ---------------------------------------------------------------------------
# 循环
# ---------------------------------------------------------------------------

def run_loop(
    provider: str,
    model: str,
    effort: str,
    cwd: Path,
    prompt: str,
    mode: str,
) -> str:
    tools = available_tools(mode)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt(mode, cwd, model, effort)},
        {"role": "user", "content": prompt},
    ]
    for _turn in range(1, MAX_TURNS + 1):
        response = chat_completion(provider, model, messages, tools)
        try:
            message = response["choices"][0]["message"]
        except (KeyError, IndexError) as error:
            raise AdapterError(f"API 响应缺少 choices/message: {response}") from error
        content = message.get("content") or ""
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            if mode == "review":
                return content
            # exec 模式: 模型没调 finished 就结束视为未完成.
            messages.append({"role": "assistant", "content": content})
            return content

        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
        for call in tool_calls:
            name = call.get("function", {}).get("name", "")
            try:
                arguments = json.loads(call.get("function", {}).get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            if name == "finished" and mode == "exec":
                print(content or "", end="", flush=True)
                final = str(arguments.get("message", content or ""))
                print("\n" + final if content else final, file=sys.stderr)
                return final
            if mode == "review" and name in ("write_file", "edit_file", "run_command", "finished"):
                result_text = "错误: review 模式禁止写操作和命令执行, 只允许只读工具 (read_file/list_dir/grep)"
            else:
                try:
                    result_text = _tool_result_text(name, arguments, cwd)
                except (OSError, subprocess.TimeoutExpired) as error:
                    result_text = f"错误: {error}"
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "content": result_text,
            })
        if mode == "review":
            print(content or "", end="", flush=True)
    raise AdapterError(f"超过最大轮数 {MAX_TURNS}, 任务未完成")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-agent",
        description="Onevoke OpenAI-compatible API 适配器 (deepseek / glm)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    exec_parser = sub.add_parser("exec", help="执行任务 (工具循环)")
    exec_parser.add_argument("--provider", required=True)
    exec_parser.add_argument("--model")
    exec_parser.add_argument("--effort", default="medium")
    exec_parser.add_argument("--cwd")
    exec_parser.add_argument("prompt", nargs=argparse.REMAINDER)

    review_parser = sub.add_parser("review", help="只读审核")
    review_parser.add_argument("--provider", required=True)
    review_parser.add_argument("--model")
    review_parser.add_argument("--effort", default="high")
    review_parser.add_argument("--cwd", required=True)
    review_parser.add_argument("--prompt-file", required=True)
    return parser


def run_provider_shim(provider: str, argv: list[str] | None = None) -> int:
    """deepseek / glm 薄壳入口: provider 固定, 其余参数透传."""
    arguments = argv if argv is not None else sys.argv[1:]
    if arguments == ["--version"]:
        print(VERSION)
        return 0
    # 薄壳只处理 exec/review; 显式 --provider 时也接受.
    if arguments and arguments[0] == "--provider":
        provider = arguments[1]
        arguments = arguments[2:]
    parser = build_parser()
    args = parser.parse_args(arguments)
    return main_with_args(args, provider)


def main() -> int:
    if "--version" in sys.argv[1:] or sys.argv[1:] == ["--version"]:
        print(VERSION)
        return 0
    parser = build_parser()
    args = parser.parse_args()
    return main_with_args(args, getattr(args, "provider", None))


def main_with_args(args: argparse.Namespace, provider: str | None) -> int:
    try:
        provider = provider or getattr(args, "provider", None)
        if not provider:
            die("缺少 --provider")
        cfg = provider_config(provider)
        model = getattr(args, "model", None) or cfg["default_model"]
        effort = getattr(args, "effort", "medium")
        cwd = Path(getattr(args, "cwd", None) or os.getcwd()).resolve()
        if args.command == "exec":
            prompt = " ".join(getattr(args, "prompt", [])).strip()
            if not prompt:
                die("exec 需要任务 prompt")
            final = run_loop(provider, model, effort, cwd, prompt, "exec")
            print(final)
            return 0
        if args.command == "review":
            prompt_file = Path(args.prompt_file)
            if not prompt_file.is_file():
                die(f"prompt 文件不存在: {prompt_file}")
            prompt = prompt_file.read_text(encoding="utf-8")
            final = run_loop(provider, model, effort, cwd, prompt, "review")
            print(final)
            return 0
        die(f"未知命令: {args.command}")
    except AdapterError as error:
        die(str(error))
    except KeyboardInterrupt:
        die("用户取消")


if __name__ == "__main__":
    sys.exit(main())
