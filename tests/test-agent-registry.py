#!/usr/bin/env python3

"""agent_registry 注册表与 kanban 集成测试.

覆盖: 注册表结构完整性; 各家启动参数模板与旧硬编码一致; 任务规模到
模型/推理强度的映射; 规则接入路径; review wrapper 命名; 以及 kanban
对 deepseek/glm 的端到端启动、--model 覆盖和并发配额.
"""

import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
KANBAN = PROJECT_ROOT / "bin" / "kanban"


def load_registry():
    loader = importlib.machinery.SourceFileLoader(
        "agent_registry_under_test", str(PROJECT_ROOT / "bin" / "agent_registry.py")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("无法加载 agent_registry 测试模块")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(PROJECT_ROOT / "bin"))
    try:
        loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


class RegistryStructureTest(unittest.TestCase):
    REQUIRED_KEYS = (
        "label", "kind", "binary", "launch_template", "default_model",
        "models", "effort", "rules_path", "rules_mode", "memsearch", "review",
    )

    def setUp(self) -> None:
        self.registry = load_registry()

    def test_all_agents_have_required_fields(self) -> None:
        for name, spec in self.registry.AGENT_REGISTRY.items():
            with self.subTest(agent=name):
                for key in self.REQUIRED_KEYS:
                    self.assertIn(key, spec, f"{name} 缺少 {key}")
                self.assertIn("large", spec["models"])
                self.assertIn("small", spec["models"])
                self.assertIn("large", spec["effort"])
                self.assertIn("small", spec["effort"])
                self.assertIn(spec["rules_mode"], ("merge", "import"))

    def test_supported_agent_set(self) -> None:
        self.assertEqual(
            ("codex", "claude", "grok", "deepseek", "glm"),
            self.registry.EXECUTION_AGENTS,
        )
        self.assertIn("deepseek", self.registry.REVIEW_AGENTS)
        self.assertIn("glm", self.registry.REVIEW_AGENTS)
        self.assertNotIn("claude", self.registry.REVIEW_AGENTS)

    def test_unsupported_agent_raises(self) -> None:
        with self.assertRaises(self.registry.AgentRegistryError):
            self.registry.agent_spec("nope")

    def test_launch_args_match_original_hardcoded_shapes(self) -> None:
        r = self.registry
        prompt = "执行任务 1"
        self.assertEqual(
            ["/bin/codex", "--model", "gpt-5.6-sol", "--config",
             'model_reasoning_effort="medium"',
             "--dangerously-bypass-approvals-and-sandbox", prompt],
            r.agent_launch_args("codex", "/bin/codex", "gpt-5.6-sol", "medium", prompt),
        )
        self.assertEqual(
            ["/bin/claude", "--model", "opus", "--effort", "high",
             "--dangerously-skip-permissions", prompt],
            r.agent_launch_args("claude", "/bin/claude", "opus", "high", prompt),
        )
        self.assertEqual(
            ["/bin/grok", "--effort", "xhigh", "--permission-mode",
             "bypassPermissions", prompt],
            r.agent_launch_args("grok", "/bin/grok", "", "xhigh", prompt),
        )

    def test_api_agents_receive_exec_subcommand_and_provider(self) -> None:
        r = self.registry
        args = r.agent_launch_args(
            "deepseek", "/bin/deepseek", "deepseek-chat", "medium", "做任务"
        )
        self.assertEqual("/bin/deepseek", args[0])
        self.assertIn("exec", args)
        self.assertIn("--provider", args)
        self.assertEqual("deepseek", args[args.index("--provider") + 1])
        self.assertEqual("deepseek-chat", args[args.index("--model") + 1])
        self.assertEqual("medium", args[args.index("--effort") + 1])

        glm_args = r.agent_launch_args(
            "glm", "/bin/glm", "glm-4.5", "high", "做任务"
        )
        self.assertEqual("glm", glm_args[glm_args.index("--provider") + 1])

    def test_task_model_and_effort_mapping(self) -> None:
        r = self.registry
        self.assertEqual("gpt-5.6-sol", r.task_model("codex", "large"))
        self.assertEqual("opus", r.task_model("claude", "small"))
        self.assertEqual("", r.task_model("grok", "large"))
        self.assertEqual("deepseek-reasoner", r.task_model("deepseek", "large"))
        self.assertEqual("deepseek-chat", r.task_model("deepseek", "small"))
        self.assertEqual("xhigh", r.task_effort("grok", "large"))
        self.assertEqual("high", r.task_effort("grok", "small"))
        self.assertEqual("high", r.task_effort("deepseek", "large"))
        self.assertEqual("medium", r.task_effort("claude", "small"))

    def test_rules_path_and_review_wrapper(self) -> None:
        r = self.registry
        self.assertEqual("~/.codex/AGENTS.md", r.rules_path("codex"))
        self.assertEqual("~/.claude/CLAUDE.md", r.rules_path("claude"))
        self.assertEqual("~/.deepseek/AGENTS.md", r.rules_path("deepseek"))
        self.assertEqual("~/.glm/AGENTS.md", r.rules_path("glm"))
        self.assertEqual("import", r.rules_mode("claude"))
        self.assertEqual("merge", r.rules_mode("deepseek"))
        self.assertEqual("deepseek-review.sh", r.review_wrapper_name("deepseek"))
        self.assertFalse(r.memsearch_supported("deepseek"))
        self.assertTrue(r.memsearch_supported("claude"))


class KanbanIntegrationTest(unittest.TestCase):
    STATES = ("backlog", "todo", "working", "done", "archived", "trash")

    def setUp(self) -> None:
        self.language = os.environ.copy()
        self.language["ONEVOKE_LANG"] = "zh"
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for state in self.STATES:
            (self.root / state).mkdir()
        self.home = self.root / "home"
        rules_dir = self.home / ".agents"
        rules_dir.mkdir(parents=True)
        (rules_dir / "KANBAN-RULES.md").write_text("# 规则\n", encoding="utf-8")
        self.fake_bin = self.root / "fake-bin"
        self.fake_bin.mkdir()
        self.env = os.environ.copy()
        self.env["HOME"] = str(self.home)
        self.env["KANBAN_DIR"] = str(self.root)
        self.env["PATH"] = str(self.fake_bin) + os.pathsep + self.env.get("PATH", "")
        self.env["ONEVOKE_LANG"] = "zh"
        self.env["TMUX"] = "/tmp/fake-tmux,1,0"
        self.env["TMUX_PANE"] = "%1"
        self.install_tmux_fake()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def install_tmux_fake(self) -> None:
        tmux = self.fake_bin / "tmux"
        tmux.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"display-message\" ]; then printf '%s\\n' '$42'; exit 0; fi\n"
            "printf '%s\\n' \"$*\" >> \"$KANBAN_TMUX_LOG\"\n"
            "printf '%s\\n' '@7'\n"
            "exit 0\n",
            encoding="utf-8",
        )
        tmux.chmod(0o755)
        self.env["KANBAN_TMUX_LOG"] = str(self.root / "tmux.log")

    def fake_agent(self, name: str) -> Path:
        command = self.fake_bin / name
        command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        command.chmod(0o755)
        return command

    def run_command(self, *args: str, succeeds: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [sys.executable, str(KANBAN), *args],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        if succeeds and result.returncode != 0:
            self.fail(result.stderr)
        if not succeeds and result.returncode == 0:
            self.fail(f"command unexpectedly succeeded: {' '.join(args)}")
        return result

    def make_todo(self, slug: str) -> tuple[str, Path]:
        task_id = f"{datetime.now().strftime('%Y%m%d')}-{slug}-task"
        self.run_command("new", "feature", slug, f"任务 {slug}")
        task = self.root / "backlog" / f"{task_id}.md"
        text = task.read_text(encoding="utf-8")
        for replacement in ("实现目标", "产生可验证结果", "满足验收", "无额外范围"):
            text = text.replace("<填写>", replacement, 1)
        task.write_text(text, encoding="utf-8")
        self.run_command("move", task_id, "todo")
        return task_id, self.root / "todo" / f"{task_id}.md"

    def test_start_accepts_deepseek_and_builds_adapter_argv(self) -> None:
        self.fake_agent("deepseek")
        task_id, _ = self.make_todo("deepseek-start")

        result = self.run_command("start", "--agent", "deepseek", task_id)

        self.assertIn("Agent=deepseek", result.stdout)
        command = (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()[-1]
        self.assertIn(str(self.fake_bin / "deepseek"), command)
        self.assertIn("exec", command)
        self.assertIn("--provider deepseek", command)
        self.assertIn("--model deepseek-chat", command)
        self.assertIn("--effort medium", command)
        self.assertIn(task_id, command)

    def test_start_model_flag_overrides_registry_default(self) -> None:
        self.fake_agent("deepseek")
        task_id, _ = self.make_todo("deepseek-model")

        self.run_command("start", "--agent", "deepseek", "--model", "custom-x1", task_id)

        command = (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()[-1]
        self.assertIn("--model custom-x1", command)
        self.assertNotIn("--model deepseek-chat", command)

    def test_glm_start_builds_adapter_argv(self) -> None:
        self.fake_agent("glm")
        task_id, _ = self.make_todo("glm-start")

        self.run_command("start", "--agent", "glm", task_id)

        command = (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()[-1]
        self.assertIn(str(self.fake_bin / "glm"), command)
        self.assertIn("--provider glm", command)

    def test_concurrency_limit_blocks_extra_starts(self) -> None:
        self.fake_agent("codex")
        self.env["KANBAN_MAX_CONCURRENT_TASKS"] = "1"
        first_id, _ = self.make_todo("quota-a")
        second_id, _ = self.make_todo("quota-b")

        self.run_command("start", first_id)
        result = self.run_command("start", second_id, succeeds=False)

        self.assertIn("并发上限", result.stderr)
        self.assertTrue((self.root / "todo" / f"{second_id}.md").exists())
        self.assertFalse((self.root / "working" / f"{second_id}.md").exists())

    def test_concurrency_limit_zero_disables(self) -> None:
        self.fake_agent("codex")
        self.env["KANBAN_MAX_CONCURRENT_TASKS"] = "0"
        first_id, _ = self.make_todo("quota-off-a")
        second_id, _ = self.make_todo("quota-off-b")

        self.run_command("start", first_id)
        self.run_command("start", second_id)

    # ---- 批量启动: start --all / --limit ----

    def test_start_all_launches_every_todo_card(self) -> None:
        self.fake_agent("codex")
        self.env["KANBAN_MAX_CONCURRENT_TASKS"] = "0"
        ids = [self.make_todo(f"batch-{n}")[0] for n in range(3)]

        result = self.run_command("start", "--all")

        self.assertIn("成功 3/3", result.stdout)
        for task_id in ids:
            self.assertTrue((self.root / "working" / f"{task_id}.md").exists())
            self.assertFalse((self.root / "todo" / f"{task_id}.md").exists())

    def test_start_all_with_limit_starts_only_n(self) -> None:
        self.fake_agent("codex")
        self.env["KANBAN_MAX_CONCURRENT_TASKS"] = "0"
        ids = [self.make_todo(f"limit-{n}")[0] for n in range(4)]

        result = self.run_command("start", "--all", "--limit", "2")

        self.assertIn("成功 2/2", result.stdout)
        self.assertTrue((self.root / "working" / f"{ids[0]}.md").exists())
        self.assertTrue((self.root / "working" / f"{ids[1]}.md").exists())
        self.assertTrue((self.root / "todo" / f"{ids[2]}.md").exists())
        self.assertTrue((self.root / "todo" / f"{ids[3]}.md").exists())

    def test_start_all_respects_concurrency_cap(self) -> None:
        self.fake_agent("codex")
        self.env["KANBAN_MAX_CONCURRENT_TASKS"] = "2"
        ids = [self.make_todo(f"cap-{n}")[0] for n in range(3)]

        self.run_command("start", "--all")

        self.assertTrue((self.root / "working" / f"{ids[0]}.md").exists())
        self.assertTrue((self.root / "working" / f"{ids[1]}.md").exists())
        self.assertTrue((self.root / "todo" / f"{ids[2]}.md").exists())

    def test_start_all_with_no_capacity_reports_error(self) -> None:
        self.fake_agent("codex")
        self.env["KANBAN_MAX_CONCURRENT_TASKS"] = "1"
        first_id, _ = self.make_todo("full-a")
        second_id, _ = self.make_todo("full-b")
        self.run_command("start", first_id)

        result = self.run_command("start", "--all", succeeds=False)

        self.assertIn("无可用并发名额", result.stderr)
        self.assertTrue((self.root / "todo" / f"{second_id}.md").exists())

    def test_start_all_rejects_foreground_launcher(self) -> None:
        self.fake_agent("codex")
        self.env["KANBAN_MAX_CONCURRENT_TASKS"] = "0"
        self.make_todo("fg-all")

        result = self.run_command("start", "--all", "--launcher", "foreground", succeeds=False)

        self.assertIn("只支持 tmux launcher", result.stderr)

    def test_start_all_with_empty_todo_reports_error(self) -> None:
        self.fake_agent("codex")
        self.env["KANBAN_MAX_CONCURRENT_TASKS"] = "0"

        result = self.run_command("start", "--all", succeeds=False)

        self.assertIn("todo 中没有可启动的任务", result.stderr)

    # ---- 用户确认收尾: finish ----

    def make_working_with_impl(self, slug: str) -> str:
        """建卡 -> start 进 working -> 填「实施与验证」."""
        self.fake_agent("codex")
        self.env["KANBAN_MAX_CONCURRENT_TASKS"] = "0"
        task_id, _ = self.make_todo(slug)
        self.run_command("start", task_id)
        card = self.root / "working" / f"{task_id}.md"
        text = card.read_text(encoding="utf-8")
        text = text.replace(
            "## 实施与验证\n\n<填写>\n",
            "## 实施与验证\n\n已完成实现, 验证通过。\n",
            1,
        )
        card.write_text(text, encoding="utf-8")
        return task_id

    def test_finish_moves_working_task_to_done(self) -> None:
        task_id = self.make_working_with_impl("finish-one")

        result = self.run_command("finish", task_id)

        self.assertIn(f"已收尾: {task_id}", result.stdout)
        card = self.root / "done" / f"{task_id}.md"
        self.assertTrue(card.exists())
        text = card.read_text(encoding="utf-8")
        self.assertIn("- 结果: completed", text)
        self.assertIn("用户验收通过", text)

    def test_finish_rejects_non_working_task(self) -> None:
        task_id, _ = self.make_todo("finish-todo")
        result = self.run_command("finish", task_id, succeeds=False)
        self.assertIn("只能收尾 working 任务", result.stderr)

    def test_finish_requires_implementation_log(self) -> None:
        self.fake_agent("codex")
        self.env["KANBAN_MAX_CONCURRENT_TASKS"] = "0"
        task_id, _ = self.make_todo("finish-noimpl")
        self.run_command("start", task_id)  # working, 但实施与验证仍是 <填写>

        result = self.run_command("finish", task_id, succeeds=False)

        self.assertIn("尚未填写", result.stderr)
        self.assertTrue((self.root / "working" / f"{task_id}.md").exists())

    def test_finish_all_finishes_all_working_cards(self) -> None:
        first = self.make_working_with_impl("finish-batch-a")
        second = self.make_working_with_impl("finish-batch-b")

        result = self.run_command("finish", "--all")

        self.assertIn("批量收尾完成: 2/2", result.stdout)
        for task_id in (first, second):
            self.assertTrue((self.root / "done" / f"{task_id}.md").exists())

    def test_finish_all_skips_cards_without_impl(self) -> None:
        first = self.make_working_with_impl("finish-skip-a")
        self.fake_agent("codex")
        self.env["KANBAN_MAX_CONCURRENT_TASKS"] = "0"
        second, _ = self.make_todo("finish-skip-b")
        self.run_command("start", second)  # working, 但无实施与验证

        result = self.run_command("finish", "--all")

        self.assertIn("批量收尾完成: 1/2", result.stdout)
        self.assertTrue((self.root / "done" / f"{first}.md").exists())
        self.assertTrue((self.root / "working" / f"{second}.md").exists())


if __name__ == "__main__":
    unittest.main()
