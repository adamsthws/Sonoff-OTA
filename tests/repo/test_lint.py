"""A linked worktree lints with the main checkout's .venv (tools/repo/lint.py, .githooks/)."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.repo import lint

HOOKS = Path(__file__).resolve().parents[2] / ".githooks"
# Git as it would be on a computer of its own: none of this one's settings
# (signing, hooks) and none of a hook's GIT_DIR, if the tests run inside one.
GIT_ENV = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")} | {
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


@unittest.skipIf(shutil.which("git") is None, "no git")
class WorktreeVenvTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.main = Path(tmp.name, "main").resolve()
        self.worktree = Path(tmp.name, "worktree").resolve()
        self.main.mkdir()
        self.git(self.main, "init", "-q")
        self.git(self.main, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                 "commit", "-q", "--allow-empty", "-m", "init")
        self.git(self.main, "worktree", "add", "-q", "--detach", str(self.worktree))
        self.env = mock.patch.dict(os.environ, GIT_ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def git(self, cwd, *args):
        subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, check=True, capture_output=True)

    def fake_python(self, path):
        """A python that says which it is instead of running the script."""
        path.parent.mkdir(parents=True)
        path.write_text(f'#!/bin/sh\necho "{path}"\n', encoding="utf-8")
        path.chmod(0o755)

    def hook(self, name, cwd):
        return subprocess.run(["sh", str(HOOKS / name)], cwd=cwd, env=GIT_ENV, check=True,
                              capture_output=True, text=True).stdout.splitlines()

    def test_lint_finds_the_main_checkouts_venv_from_a_worktree(self):
        for root in (self.worktree, self.main):
            with mock.patch.object(lint, "ROOT", root):
                self.assertEqual(lint.shared_venv_dirs(),
                                 [self.main / ".venv" / "bin", self.main / ".venv" / "Scripts"])

    def test_lint_outside_git_finds_none(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(lint, "ROOT", Path(tmp)):
            self.assertEqual(lint.shared_venv_dirs(), [])

    def test_the_hooks_in_a_worktree_use_the_main_checkouts_venv(self):
        python = self.main / ".venv" / "bin" / "python"
        self.fake_python(python)
        self.assertEqual(self.hook("pre-commit", self.worktree), [str(python)] * 2)   # leak scan, lint
        self.assertEqual(self.hook("pre-push", self.worktree), [str(python)])

    def test_the_hooks_prefer_the_worktrees_own_venv(self):
        self.fake_python(self.main / ".venv" / "bin" / "python")
        own = self.worktree / ".venv" / "bin" / "python"
        self.fake_python(own)
        self.assertEqual(self.hook("pre-push", self.worktree), [str(own)])

    def test_the_hooks_find_a_windows_venv(self):
        python = self.main / ".venv" / "Scripts" / "python.exe"
        self.fake_python(python)
        self.assertEqual(self.hook("pre-push", self.worktree), [str(python)])
