#!/usr/bin/env python3

"""llm-agent 适配器测试.

用本地假 OpenAI 兼容服务器驱动适配器, 不发真实网络请求. 覆盖:
--version 探测; exec 工具循环 (read_file -> finished); 工具真实生效;
review 只读模式拒绝写工具; 429 指数退避重试; provider 选择.
"""

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = PROJECT_ROOT / "bin"
DEEPSEEK = BIN_DIR / "deepseek"
GLM = BIN_DIR / "glm"
LLM_AGENT = BIN_DIR / "llm_agent.py"


def tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def completion_with_tool_calls(calls: list[dict], content: str = "") -> dict:
    return {
        "choices": [{
            "message": {"role": "assistant", "content": content, "tool_calls": calls},
        }]
    }


def completion_plain(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class FakeServer:
    """按请求次序返回脚本化响应; None 表示返回 429."""

    def __init__(self, responses: list) -> None:
        self.responses = responses
        self.requests: list[dict] = []
        self.httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), self._handler_factory()
        )
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def _handler_factory(self):
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                server.requests.append(body)
                index = len(server.requests) - 1
                response = server.responses[
                    min(index, len(server.responses) - 1)
                ]
                if response is None:
                    self.send_response(429)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = json.dumps(response).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        return Handler

    def __enter__(self) -> "FakeServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class LlmAgentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = os.environ.copy()
        self.env["DEEPSEEK_API_KEY"] = "test-key"
        self.env["GLM_API_KEY"] = "test-key"
        self.env["LLM_AGENT_RETRY_ATTEMPTS"] = "5"
        self.env["LLM_AGENT_RETRY_BASE_SECONDS"] = "0.01"
        # 默认跳过对话确认, 让既有 exec 测试不被 stdin 阻塞.
        self.env["LLM_AGENT_NO_CONFIRM"] = "1"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_adapter(self, *args: str, succeeds: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [sys.executable, str(DEEPSEEK), *args],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        if succeeds and result.returncode != 0:
            self.fail(result.stderr)
        if not succeeds and result.returncode == 0:
            self.fail(f"command unexpectedly succeeded: {args}")
        return result

    def test_version_probe(self) -> None:
        result = self.run_adapter("--version")
        self.assertIn("llm-agent", result.stdout)

    def test_unsupported_provider_is_rejected(self) -> None:
        result = subprocess.run(
            [sys.executable, str(LLM_AGENT), "exec", "--provider", "nope", "做任务"],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(1, result.returncode)
        self.assertIn("不支持的 provider", result.stderr)

    def test_exec_runs_tool_loop_and_finishes(self) -> None:
        work = self.root / "work"
        work.mkdir()
        (work / "a.txt").write_text("line1\nline2\n", encoding="utf-8")
        responses = [
            completion_with_tool_calls([
                tool_call("call_1", "read_file", {"path": str(work / "a.txt")}),
            ], content="先读文件"),
            completion_with_tool_calls([
                tool_call("call_2", "finished", {"message": "任务完成"}),
            ], content=""),
        ]
        with FakeServer(responses) as server:
            self.env["LLM_AGENT_BASE_URL"] = f"http://127.0.0.1:{server.port}"
            result = self.run_adapter(
                "exec", "--provider", "deepseek",
                "--model", "deepseek-chat", "--effort", "medium",
                f"读取 {work / 'a.txt'} 并总结",
            )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("任务完成", result.stderr)
        # 第二次请求应包含 read_file 的工具结果 (role=tool).
        tool_messages = [
            message for message in server.requests[1]["messages"]
            if message.get("role") == "tool"
        ]
        self.assertEqual(1, len(tool_messages))
        self.assertIn("line1", tool_messages[0]["content"])
        self.assertEqual("deepseek-chat", server.requests[0]["model"])
        self.assertTrue(any(
            tool["function"]["name"] == "finished"
            for tool in server.requests[0]["tools"]
        ))

    def test_exec_tools_actually_modify_files(self) -> None:
        work = self.root / "work"
        work.mkdir()
        responses = [
            completion_with_tool_calls([
                tool_call("c1", "write_file", {
                    "path": str(work / "out.txt"), "content": "hello adapter",
                }),
            ]),
            completion_with_tool_calls([
                tool_call("c2", "run_command", {"command": "pwd"}),
            ]),
            completion_with_tool_calls([
                tool_call("c3", "finished", {"message": "写入完成"}),
            ]),
        ]
        with FakeServer(responses) as server:
            self.env["LLM_AGENT_BASE_URL"] = f"http://127.0.0.1:{server.port}"
            self.run_adapter("exec", "--provider", "deepseek", f"写入文件到 {work}")

        self.assertEqual("hello adapter", (work / "out.txt").read_text(encoding="utf-8"))

    def test_review_mode_rejects_write_and_run_command(self) -> None:
        work = self.root / "work"
        work.mkdir()
        responses = [
            completion_with_tool_calls([
                tool_call("r1", "run_command", {"command": "rm -rf /" }),
                tool_call("r2", "write_file", {"path": str(work / "x"), "content": "x"}),
            ]),
            completion_plain("QA-1 审核通过, 无问题"),
        ]
        prompt_file = self.root / "prompt.txt"
        prompt_file.write_text("审核当前改动", encoding="utf-8")
        with FakeServer(responses) as server:
            self.env["LLM_AGENT_BASE_URL"] = f"http://127.0.0.1:{server.port}"
            result = self.run_adapter(
                "review", "--provider", "deepseek", "--cwd", str(work),
                "--prompt-file", str(prompt_file),
            )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("QA-1 审核通过", result.stdout)
        self.assertFalse((work / "x").exists())
        tool_messages = [
            message for message in server.requests[1]["messages"]
            if message.get("role") == "tool"
        ]
        self.assertTrue(any("禁止" in m["content"] for m in tool_messages))

    def test_429_is_retried_with_backoff(self) -> None:
        responses = [None, None, completion_plain("重试后成功")]
        with FakeServer(responses) as server:
            self.env["LLM_AGENT_BASE_URL"] = f"http://127.0.0.1:{server.port}"
            result = self.run_adapter(
                "exec", "--provider", "glm", "--model", "glm-4.5", "任务"
            )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("重试后成功", result.stdout)
        self.assertEqual(3, len(server.requests))
        self.assertIn("HTTP 429", result.stderr)

    def test_persistent_429_fails_with_clear_error(self) -> None:
        responses = [None] * 20
        with FakeServer(responses) as server:
            self.env["LLM_AGENT_BASE_URL"] = f"http://127.0.0.1:{server.port}"
            result = self.run_adapter(
                "exec", "--provider", "deepseek", "任务", succeeds=False
            )

        self.assertIn("持续返回 HTTP 429", result.stderr)
        self.assertEqual(5, len(server.requests))

    # ---- 对话确认 (原始交互式确认) ----

    def _make_working_card(self, work: Path, task_id: str) -> Path:
        board = work / "kanban"
        for state in ("backlog", "todo", "working", "done", "archived", "trash"):
            (board / state).mkdir(parents=True)
        card = board / "working" / f"{task_id}.md"
        card.write_text(
            "# 确认测试\n\n- 类型: Feature\n- 创建时间: 2026-08-16 00:00\n"
            "- 负责人: deepseek\n- 开始时间: 2026-08-16 00:01\n- 完成时间:\n"
            "- 任务分支:\n- 结果:\n\n## 实施与验证\n\n已完成实现, 验证通过。\n\n"
            "## 完成总结\n\n<填写>\n",
            encoding="utf-8",
        )
        return card

    def test_exec_confirm_accept_finishes_card(self) -> None:
        work = self.root / "work"
        work.mkdir()
        task_id = "20260816-confirm-task"
        self._make_working_card(work, task_id)
        responses = [
            completion_with_tool_calls([
                tool_call("f1", "finished", {"message": "任务完成"}),
            ]),
        ]
        self.env.pop("LLM_AGENT_NO_CONFIRM", None)
        self.env["KANBAN_BIN"] = str(PROJECT_ROOT / "bin" / "kanban")
        with FakeServer(responses) as server:
            self.env["LLM_AGENT_BASE_URL"] = f"http://127.0.0.1:{server.port}"
            result = subprocess.run(
                [sys.executable, str(DEEPSEEK), "exec", "--provider", "deepseek",
                 "--cwd", str(work), "--effort", "medium",
                 f"执行 Kanban 任务 {task_id}. 完成工作"],
                env=self.env,
                text=True,
                input="验收通过\n",
                capture_output=True,
                check=False,
            )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("请验收", result.stdout)
        done = work / "kanban" / "done" / f"{task_id}.md"
        self.assertTrue(done.exists(), "卡片应被自动 move done")
        text = done.read_text(encoding="utf-8")
        self.assertIn("- 结果: completed", text)
        self.assertIn("用户验收通过", text)

    def test_exec_confirm_reject_keeps_working(self) -> None:
        work = self.root / "work"
        work.mkdir()
        task_id = "20260816-reject-task"
        self._make_working_card(work, task_id)
        responses = [
            completion_with_tool_calls([
                tool_call("f1", "finished", {"message": "完成"}),
            ]),
        ]
        self.env.pop("LLM_AGENT_NO_CONFIRM", None)
        self.env["KANBAN_BIN"] = str(PROJECT_ROOT / "bin" / "kanban")
        with FakeServer(responses) as server:
            self.env["LLM_AGENT_BASE_URL"] = f"http://127.0.0.1:{server.port}"
            result = subprocess.run(
                [sys.executable, str(DEEPSEEK), "exec", "--provider", "deepseek",
                 "--cwd", str(work), "--effort", "medium",
                 f"执行 Kanban 任务 {task_id}. 完成工作"],
                env=self.env,
                text=True,
                input="否\n",
                capture_output=True,
                check=False,
            )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue((work / "kanban" / "working" / f"{task_id}.md").exists())
        self.assertFalse((work / "kanban" / "done" / f"{task_id}.md").exists())


if __name__ == "__main__":
    unittest.main()
