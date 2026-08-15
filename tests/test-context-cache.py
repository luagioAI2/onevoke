#!/usr/bin/env python3

"""compile-rules / index 与 start prompt 注入测试.

compile-rules 从规则目录生成 SUMMARY.md; index 生成 .onevoke/context.md;
kanban start 的 prompt 引导 Agent 先读摘要和上下文缓存 (token 节省).
"""

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ONEVOKE = PROJECT_ROOT / "bin" / "onevoke"
KANBAN = PROJECT_ROOT / "bin" / "kanban"


class CompileRulesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = os.environ.copy()
        self.env["ONEVOKE_LANG"] = "zh"
        self.env["ONEVOKE_CONFIG"] = str(self.root / "config.json")
        self.env["HOME"] = str(self.root / "home")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_compile_rules_generates_summary_with_bullets(self) -> None:
        rules = self.root / "rules"
        rules.mkdir()
        (rules / "BASE-RULES.md").write_text(
            "# 基础规则\n\n## 交流与格式\n\n- 对话默认中文\n- 使用 ASCII 标点\n\n"
            "## 安全\n\n- 凭据只经环境变量注入\n",
            encoding="utf-8",
        )
        (rules / "GIT-RULES.md").write_text(
            "# Git 规则\n\n## 分支\n\n- 分支模型固定为 main + develop\n",
            encoding="utf-8",
        )

        result = subprocess.run(
            [sys.executable, str(ONEVOKE), "compile-rules", "--source", str(rules)],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        summary = rules / "SUMMARY.md"
        self.assertTrue(summary.is_file())
        text = summary.read_text(encoding="utf-8")
        self.assertIn("## BASE-RULES.md", text)
        self.assertIn("- 对话默认中文", text)
        self.assertIn("## GIT-RULES.md", text)
        self.assertIn("- 分支模型固定为 main + develop", text)
        # SUMMARY.md 自身不能出现在摘要里.
        self.assertNotIn("## SUMMARY.md", text)

    def test_compile_rules_missing_dir_is_rejected(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ONEVOKE), "compile-rules", "--source", str(self.root / "absent")],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(1, result.returncode)
        self.assertIn("规则目录不存在", result.stderr)


class IndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        (self.project / "src").mkdir(parents=True)
        (self.project / "README.md").write_text(
            "# 演示项目\n\n这是一个用于测试的项目。\n", encoding="utf-8"
        )
        (self.project / "pyproject.toml").write_text(
            "[project]\nname = \"demo\"\n", encoding="utf-8"
        )
        (self.project / "src" / "main.py").write_text("print(1)\n", encoding="utf-8")
        (self.project / ".git").mkdir()
        (self.project / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        self.env = os.environ.copy()
        self.env["ONEVOKE_LANG"] = "zh"
        self.env["ONEVOKE_CONFIG"] = str(self.root / "config.json")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_index_generates_context_md(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ONEVOKE), "index", str(self.project)],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        context = self.project / ".onevoke" / "context.md"
        self.assertTrue(context.is_file())
        text = context.read_text(encoding="utf-8")
        self.assertIn("README", text)
        self.assertIn("演示项目", text)
        self.assertIn("src/", text)
        self.assertIn("main.py", text)
        self.assertIn("pyproject.toml", text)
        # .git 必须被排除.
        self.assertNotIn(".git/", text)


class StartPromptInjectionTest(unittest.TestCase):
    STATES = ("backlog", "todo", "working", "done", "archived", "trash")

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for state in self.STATES:
            (self.root / state).mkdir()
        self.home = self.root / "home"
        (self.home / ".agents").mkdir(parents=True)
        (self.home / ".agents" / "KANBAN-RULES.md").write_text("# 规则\n", encoding="utf-8")
        self.fake_bin = self.root / "fake-bin"
        self.fake_bin.mkdir()
        self.env = os.environ.copy()
        self.env["HOME"] = str(self.home)
        self.env["KANBAN_DIR"] = str(self.root)
        self.env["PATH"] = str(self.fake_bin) + os.pathsep + self.env.get("PATH", "")
        self.env["ONEVOKE_LANG"] = "zh"
        self.env["TMUX"] = "/tmp/fake-tmux,1,0"
        self.env["TMUX_PANE"] = "%1"
        self.env["KANBAN_TMUX_LOG"] = str(self.root / "tmux.log")
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
        agent = self.fake_bin / "codex"
        agent.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        agent.chmod(0o755)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_todo(self, slug: str, group: bool = False) -> str:
        task_id = f"{datetime.now().strftime('%Y%m%d')}-{slug}-task"
        subprocess.run(
            [sys.executable, str(KANBAN), "new", "feature", slug, f"任务 {slug}"],
            env=self.env, text=True, capture_output=True, check=True,
        )
        task = self.root / "backlog" / f"{task_id}.md"
        text = task.read_text(encoding="utf-8")
        for replacement in ("实现目标", "产生可验证结果", "满足验收", "无额外范围"):
            text = text.replace("<填写>", replacement, 1)
        if group:
            text = text.replace(
                "## 讨论与决策\n",
                "## 讨论与决策\n\n任务组: 20260815-demo-group\n前置任务: N/A\n",
                1,
            )
        task.write_text(text, encoding="utf-8")
        subprocess.run(
            [sys.executable, str(KANBAN), "move", task_id, "todo"],
            env=self.env, text=True, capture_output=True, check=True,
        )
        return task_id

    def test_prompt_mentions_summary_and_context_cache(self) -> None:
        task_id = self.make_todo("cache-hints")
        subprocess.run(
            [sys.executable, str(KANBAN), "start", task_id],
            env=self.env, text=True, capture_output=True, check=True,
        )
        command = (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()[-1]
        self.assertIn("SUMMARY.md", command)
        self.assertIn(".onevoke/context.md", command)
        self.assertIn(task_id, command)

    def test_group_card_prompt_mentions_handoff(self) -> None:
        task_id = self.make_todo("group-card", group=True)
        subprocess.run(
            [sys.executable, str(KANBAN), "start", task_id],
            env=self.env, text=True, capture_output=True, check=True,
        )
        command = (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()[-1]
        self.assertIn("任务组", command)
        self.assertIn("完成总结", command)

    def test_plain_card_prompt_has_no_handoff_hint(self) -> None:
        task_id = self.make_todo("plain-card")
        subprocess.run(
            [sys.executable, str(KANBAN), "start", task_id],
            env=self.env, text=True, capture_output=True, check=True,
        )
        command = (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()[-1]
        self.assertNotIn("本卡属于任务组", command)


if __name__ == "__main__":
    unittest.main()
