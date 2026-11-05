"""requirements/ci.txt pins everything, so its installs read it alone: pip 23,
which Python 3.10 ships, otherwise looks up esp-pylib[cli], the extra the file
strips, and refuses it for having no pin."""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALL = re.compile(r"--require-hashes(?P<flags>[^\n]*?)\s*\\?\s*-r [^\n]*requirements/ci\.txt")


def without_no_deps(text: str) -> list[str]:
    return [found.group() for found in INSTALL.finditer(text) if "--no-deps" not in found["flags"]]


class LockfileInstallTests(unittest.TestCase):
    def test_every_install_of_the_lockfile_takes_no_dependencies(self):
        # Not this file, whose examples below are installs without it.
        this_file = Path(__file__).resolve().relative_to(ROOT).as_posix()
        holding = subprocess.run(["git", "grep", "-lI", "requirements/ci.txt", "--", ".", f":!{this_file}"], cwd=ROOT,
                                 capture_output=True, text=True, encoding="utf-8", check=True).stdout.splitlines()
        installs = 0
        for path in holding:
            text = (ROOT / path).read_text(encoding="utf-8")
            installs += len(INSTALL.findall(text))
            with self.subTest(path=path):
                self.assertEqual(without_no_deps(text), [])
        self.assertGreater(installs, 5)

    def test_it_sees_an_install_on_one_line_or_two(self):
        self.assertTrue(without_no_deps("pip install --require-hashes -r requirements/ci.txt"))
        self.assertTrue(without_no_deps('pip install -q --require-hashes \\\n    -r "$ROOT/requirements/ci.txt"'))
        self.assertFalse(without_no_deps("pip install --require-hashes --no-deps -r requirements/ci.txt"))
        self.assertFalse(without_no_deps("pip install --require-hashes -r requirements/lint.txt"))
