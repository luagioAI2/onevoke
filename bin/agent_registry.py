#!/usr/bin/env python3

"""Agent 适配器注册表: 全部 Agent 相关配置的唯一数据源.

新增 Agent 只需在 AGENT_REGISTRY 增加一条记录, 并保证 `~/.local/bin` 中存在
对应的二进制 (官方 CLI 或本仓库的适配器脚本). kanban / onevoke / review
wrapper 一律从本注册表读取, 不硬编码 Agent 名单、模型名或启动参数.

每个条目的字段:
- label          展示名 (welcome / doctor / 输出)
- kind           "cli" = 官方 CLI; "api" = 通过适配器脚本驱动 API
- binary         二进制名 (PATH 中查找)
- launch_template 启动参数模板, 占位符 {binary} {model} {effort} {prompt}
- default_model  未指定时的默认模型
- models         {"large": ..., "small": ...} 按任务规模选模型
- effort         {"large": ..., "small": ...} 按任务规模选推理强度
- rules_path     该 Agent 的全局规则文件 (相对于 HOME)
- rules_mode     "merge" = 全文合并/软链; "import" = @导入 (Claude)
- memsearch      是否支持 MemSearch 记忆
- review         是否可作为审核 Reviewer
"""

from __future__ import annotations

from typing import Any


AGENT_REGISTRY: dict[str, dict[str, Any]] = {
    "codex": {
        "label": "Codex",
        "kind": "cli",
        "binary": "codex",
        "launch_template": [
            "--model", "{model}",
            "--config", 'model_reasoning_effort="{effort}"',
            "--dangerously-bypass-approvals-and-sandbox",
            "{prompt}",
        ],
        "default_model": "gpt-5.6-sol",
        "models": {"large": "gpt-5.6-sol", "small": "gpt-5.6-sol"},
        "effort": {"large": "high", "small": "medium"},
        "rules_path": "~/.codex/AGENTS.md",
        "rules_mode": "merge",
        "memsearch": True,
        "review": True,
    },
    "claude": {
        "label": "Claude",
        "kind": "cli",
        "binary": "claude",
        "launch_template": [
            "--model", "{model}",
            "--effort", "{effort}",
            "--dangerously-skip-permissions",
            "{prompt}",
        ],
        "default_model": "opus",
        "models": {"large": "opus", "small": "opus"},
        "effort": {"large": "high", "small": "medium"},
        "rules_path": "~/.claude/CLAUDE.md",
        "rules_mode": "import",
        "memsearch": True,
        "review": False,
    },
    "grok": {
        "label": "Grok",
        "kind": "cli",
        "binary": "grok",
        "launch_template": [
            "--effort", "{effort}",
            "--permission-mode", "bypassPermissions",
            "{prompt}",
        ],
        "default_model": "",
        "models": {"large": "", "small": ""},
        "effort": {"large": "xhigh", "small": "high"},
        "rules_path": "~/.grok/AGENTS.md",
        "rules_mode": "merge",
        "memsearch": False,
        "review": True,
    },
    "deepseek": {
        "label": "DeepSeek",
        "kind": "api",
        "binary": "deepseek",
        "launch_template": [
            "exec", "--provider", "deepseek",
            "--model", "{model}",
            "--effort", "{effort}",
            "{prompt}",
        ],
        "default_model": "deepseek-chat",
        "models": {"large": "deepseek-reasoner", "small": "deepseek-chat"},
        "effort": {"large": "high", "small": "medium"},
        "rules_path": "~/.deepseek/AGENTS.md",
        "rules_mode": "merge",
        "memsearch": False,
        "review": True,
    },
    "glm": {
        "label": "GLM",
        "kind": "api",
        "binary": "glm",
        "launch_template": [
            "exec", "--provider", "glm",
            "--model", "{model}",
            "--effort", "{effort}",
            "{prompt}",
        ],
        "default_model": "glm-4.5",
        "models": {"large": "glm-4.5", "small": "glm-4.5"},
        "effort": {"large": "high", "small": "medium"},
        "rules_path": "~/.glm/AGENTS.md",
        "rules_mode": "merge",
        "memsearch": False,
        "review": True,
    },
}

# 全部执行 Agent (welcome / kanban --agent 的选择来源)
EXECUTION_AGENTS: tuple[str, ...] = tuple(AGENT_REGISTRY)
# 可作审核 Reviewer 的 Agent
REVIEW_AGENTS: tuple[str, ...] = tuple(
    name for name, spec in AGENT_REGISTRY.items() if spec["review"]
)


class AgentRegistryError(Exception):
    """引用了注册表中不存在的 Agent."""


def agent_spec(name: str) -> dict[str, Any]:
    try:
        return AGENT_REGISTRY[name]
    except KeyError as error:
        raise AgentRegistryError(
            f"不支持的 Agent: {name}", f"Unsupported Agent: {name}"
        ) from error


def agent_label(name: str) -> str:
    return agent_spec(name)["label"]


def agent_launch_args(
    name: str, binary: str, model: str, effort: str, prompt: str
) -> list[str]:
    """按注册表模板拼启动参数, argv[0] 为二进制路径."""
    template = agent_spec(name)["launch_template"]
    rendered = [
        part
        .replace("{binary}", binary)
        .replace("{model}", model)
        .replace("{effort}", effort)
        .replace("{prompt}", prompt)
        for part in template
    ]
    return [binary, *rendered]


def task_model(name: str, kind: str) -> str:
    """按任务规模选模型; 无规模映射时回落到 default_model."""
    spec = agent_spec(name)
    return spec["models"].get(kind) or spec["default_model"]


def task_effort(name: str, kind: str) -> str:
    spec = agent_spec(name)
    return spec["effort"][kind]


def rules_path(name: str) -> str:
    return agent_spec(name)["rules_path"]


def rules_mode(name: str) -> str:
    return agent_spec(name)["rules_mode"]


def memsearch_supported(name: str) -> bool:
    return bool(agent_spec(name)["memsearch"])


def review_supported(name: str) -> bool:
    return bool(agent_spec(name)["review"])


def review_wrapper_name(name: str) -> str:
    return f"{name}-review.sh"
