# Onevoke

一个人用看板调度多个 AI Agent.

## 1. 支持的 Agent

Codex、Claude、Grok 使用官方 CLI; DeepSeek、GLM 通过 `bin/llm-agent` 适配器
直接调用 OpenAI 兼容 API. 全部 Agent 的名单、模型和启动参数由
`bin/agent_registry.py` 注册表统一管理, 新增 Agent 只需在注册表登记并准备
对应二进制与 `bin/<agent>-review.sh`.

DeepSeek / GLM 需要设置 API Key 环境变量:

```sh
export DEEPSEEK_API_KEY=sk-...
export GLM_API_KEY=...
```

## 2. 安装

需要 Python 3, Git, POSIX shell, 以及上述 Agent 中至少一个.

```sh
./install.sh
```

安装过程会引导选择默认 Agent, Reviewer 和启动方式.

如果 `~/.agents/AGENTS.md` 不存在, 安装器会将其链接到 `ONEVOKE-AGENTS.md`; 已有文件不会修改.

如果 welcome 提示 Agent 尚未接入规则:

- Claude: 在 `~/.claude/CLAUDE.md` 加 `@~/.agents/ONEVOKE-AGENTS.md`.
- Codex: 将 `~/.codex/AGENTS.md` 软链接到该入口, 或把入口内容合入现有文件.
- Grok: 将 `~/.grok/AGENTS.md` 软链接到该入口, 或把入口内容合入现有文件.
- DeepSeek / GLM: 将 `~/.deepseek/AGENTS.md` / `~/.glm/AGENTS.md` 软链接到该入口, 或把入口内容合入现有文件.

## 3. 使用

在项目目录首次使用时初始化看板:

```sh
kanban init
```

先在 Agent 中讨论需求, 明确目标, 验收条件和不做的范围.

讨论完成后, 让 Agent 创建并启动任务卡:

```text
需求已确认. 请用 kanban new & start 创建任务卡并启动.
```

Agent 会填完整任务卡, 再执行:

```sh
kanban new feature login-retry 登录重试
kanban pick 20260813-login-retry-task
kanban start 20260813-login-retry-task
```

指定 Agent / 模型 / 启动方式:

```sh
kanban start --agent deepseek 20260813-login-retry-task
kanban start --agent glm --model glm-4.5 20260813-login-retry-task
kanban start --launcher foreground 20260813-login-retry-task
```

大型任务由 Agent 拆成多张可并行执行的任务卡, 再按依赖启动. 并发上限由配置
`max_concurrent_tasks` 控制 (默认 3, 0 关闭).

查看看板状态:

```sh
kanban list
```

只看某个状态:

```sh
kanban list working
kanban list done
```

完整规则:

```sh
kanban rules
```

## 4. 降低 token 消耗

- `onevoke compile-rules` 把 `~/.agents/` 规则分册压成 `SUMMARY.md` 摘要,
  `kanban start` 的 prompt 会引导执行 Agent 先读摘要再按需读分册.
- `onevoke index` 在项目根生成 `.onevoke/context.md` (目录树 + README + 入口
  文件), 新任务 session 先读它, 免去冷启动探索.
- 任务组卡片启动时会提示复用已完成前置卡的「完成总结」.
- 审核 wrapper 共用 `bin/review-lib.sh` 框架, 各家只写启动与输出解析薄壳.
