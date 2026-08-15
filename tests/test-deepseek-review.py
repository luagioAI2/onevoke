#!/usr/bin/env python3

"""deepseek-review.sh 的门禁测试.

假 deepseek 二进制模拟 llm-agent 适配器的 review 契约: 把 prompt 文件内容
记录到日志, 报告写到 stdout. 验证 wrapper 的拒绝路径与隔离参数传递.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
REVIEWER = PROJECT_ROOT / "bin" / "deepseek-review.sh"

FAKE_DEEPSEEK = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_DEEPSEEK_ARGV"

prompt=""
while [ "$#" -gt 0 ]; do
    if [ "$1" = "--prompt-file" ]; then
        prompt="$2"
    fi
    shift
done
[ -n "$prompt" ] && cp "$prompt" "$FAKE_DEEPSEEK_PROMPT"

if [ -n "${FAKE_DEEPSEEK_TAMPER:-}" ]; then
    printf '%s\\n' 'tampered' > "$FAKE_DEEPSEEK_TAMPER"
fi
if [ -n "${FAKE_DEEPSEEK_FAIL:-}" ]; then
    printf '%s\\n' 'fake deepseek failure' >&2
    exit 3
fi
printf '%s\\n' "${FAKE_DEEPSEEK_REPORT:-REPORT BODY}"
exit 0
"""


class DeepSeekReviewGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.tmp = self.root / "tmp"
        self.deepseek_home = self.root / "deepseek"
        for path in (self.repo, self.tmp, self.deepseek_home):
            path.mkdir()

        self.fake_deepseek = self.root / "fake-deepseek"
        self.fake_deepseek.write_text(FAKE_DEEPSEEK, encoding="utf-8")
        os.chmod(self.fake_deepseek, 0o755)
        self.argv_log = self.root / "argv.log"
        self.prompt_log = self.root / "prompt.log"

        self.git("init", "-q", "-b", "main")
        self.base = self.commit("a.txt", "base\n", "基线")
        self.head = self.commit("b.txt", "head\n", "改动")

        self.repo_real = Path(os.path.realpath(self.repo))

        self.env = os.environ.copy()
        self.env.update(
            GIT_CEILING_DIRECTORIES=str(self.root),
            TMPDIR=str(self.tmp),
            DEEPSEEK_HOME=str(self.deepseek_home),
            DEEPSEEK_REVIEW_BIN=str(self.fake_deepseek),
            DEEPSEEK_REVIEW_CHECK_INTERVAL_SECONDS="1",
            DEEPSEEK_REVIEW_MAX_RUNTIME_SECONDS="30",
            FAKE_DEEPSEEK_ARGV=str(self.argv_log),
            FAKE_DEEPSEEK_PROMPT=str(self.prompt_log),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repo),
             "-c", "user.name=test", "-c", "user.email=test@example.com",
             "-c", "commit.gpgsign=false", *args],
            text=True, capture_output=True, check=True,
        )
        return result.stdout.strip()

    def commit(self, name: str, body: str, message: str) -> str:
        (self.repo / name).write_text(body, encoding="utf-8")
        self.git("add", name)
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def review(self, *args: str, **overrides: str) -> subprocess.CompletedProcess:
        env = {**self.env, **overrides}
        return subprocess.run(
            [str(REVIEWER), *args], env=env, text=True, capture_output=True, check=False
        )

    def default_review(self, **overrides: str) -> subprocess.CompletedProcess:
        return self.review(
            str(self.repo), self.base, self.head, "QA", "确认改动正确", **overrides
        )

    def test_missing_arguments_report_usage(self) -> None:
        result = self.review()
        self.assertEqual(2, result.returncode)
        self.assertIn("Usage: deepseek-review.sh", result.stderr)

    def test_unsupported_role_is_rejected(self) -> None:
        result = self.review(str(self.repo), self.base, self.head, "Architect", "目标")
        self.assertEqual(2, result.returncode)
        self.assertIn("unsupported role", result.stderr)

    def test_path_outside_git_worktree_is_rejected(self) -> None:
        outside = self.root / "plain"
        outside.mkdir()
        result = self.review(str(outside), self.base, self.head, "QA", "目标")
        self.assertEqual(2, result.returncode)
        self.assertIn("not inside a Git worktree", result.stderr)

    def test_abbreviated_sha_is_rejected(self) -> None:
        result = self.review(str(self.repo), self.base[:8], self.head, "QA", "目标")
        self.assertEqual(2, result.returncode)
        self.assertIn("must be a full commit SHA", result.stderr)

    def test_head_not_matching_commit_is_rejected(self) -> None:
        result = self.review(str(self.repo), self.base, self.base, "QA", "目标")
        self.assertEqual(2, result.returncode)
        self.assertIn("HEAD does not match commit", result.stderr)

    def test_untracked_file_blocks_the_review(self) -> None:
        (self.repo / "scratch.txt").write_text("未提交\n", encoding="utf-8")
        result = self.default_review()
        self.assertEqual(2, result.returncode)
        self.assertIn("uncommitted or untracked changes", result.stderr)

    def test_worktree_inside_deepseek_home_is_rejected(self) -> None:
        result = self.default_review(DEEPSEEK_HOME=str(self.root))
        self.assertEqual(2, result.returncode)
        self.assertIn("overlaps a DeepSeek-writable directory", result.stderr)

    def test_missing_binary_reports_127(self) -> None:
        result = self.default_review(DEEPSEEK_REVIEW_BIN=str(self.root / "absent"))
        self.assertEqual(127, result.returncode)
        self.assertIn("DeepSeek CLI is unavailable", result.stderr)

    def test_failure_is_propagated(self) -> None:
        result = self.default_review(FAKE_DEEPSEEK_FAIL="1")
        self.assertEqual(3, result.returncode)
        self.assertIn("fake deepseek failure", result.stderr)

    def test_worktree_tampering_is_detected(self) -> None:
        result = self.default_review(FAKE_DEEPSEEK_TAMPER=str(self.repo / "injected.txt"))
        self.assertEqual(2, result.returncode)
        self.assertIn("modified the target worktree", result.stderr)

    def test_clean_review_returns_the_report(self) -> None:
        result = self.default_review(FAKE_DEEPSEEK_REPORT="QA-1 没有发现问题")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("QA-1 没有发现问题", result.stdout)

    def test_adapter_is_invoked_with_review_contract(self) -> None:
        self.assertEqual(0, self.default_review().returncode)

        argv = self.argv_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual("review", argv[0])
        self.assertEqual(str(self.repo_real), argv[argv.index("--cwd") + 1])
        self.assertEqual("high", argv[argv.index("--effort") + 1])
        self.assertIn("--prompt-file", argv)
        self.assertNotIn("--model", argv)

    def test_model_override_is_forwarded(self) -> None:
        self.assertEqual(0, self.default_review(DEEPSEEK_REVIEW_MODEL="deepseek-chat").returncode)
        argv = self.argv_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual("deepseek-chat", argv[argv.index("--model") + 1])

    def test_prompt_carries_role_task_and_scope(self) -> None:
        self.assertEqual(0, self.default_review().returncode)
        prompt = self.prompt_log.read_text(encoding="utf-8")
        self.assertIn("You are the QA review agent", prompt)
        self.assertIn("Authoritative task goal: 确认改动正确", prompt)
        self.assertIn(f"{self.base}..{self.head}", prompt)
        self.assertIn("Do not modify files", prompt)


if __name__ == "__main__":
    unittest.main()
