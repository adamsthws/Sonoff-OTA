import re
import subprocess
import sys
import unittest
from pathlib import Path

from tools import release_notes
from tools.autoflash import __version__

ROOT = Path(__file__).resolve().parents[1]


class VersionTests(unittest.TestCase):
    def test_the_version_is_x_y_z(self):
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")

    def test_the_current_version_has_release_notes(self):
        # A release takes its notes from here, so a version cannot reach main without them.
        text = release_notes.notes(__version__, release_notes.CHANGELOG.read_text(encoding="utf-8"))
        self.assertTrue(text.strip())

    def test_notes_stop_at_the_next_version(self):
        changelog = "# Changelog\n\n## 1.1.0\n\n- New.\n\n## 1.0.0\n\n- Old.\n"
        self.assertEqual(release_notes.notes("1.1.0", changelog), "- New.\n")
        self.assertEqual(release_notes.notes("1.0.0", changelog), "- Old.\n")

    def test_a_version_without_notes_is_refused(self):
        with self.assertRaises(ValueError):
            release_notes.notes("9.9.9", "# Changelog\n\n## 1.0.0\n\n- Old.\n")
        with self.assertRaises(ValueError):
            release_notes.notes("1.1.0", "# Changelog\n\n## 1.1.0\n\n## 1.0.0\n\n- Old.\n")

    def test_the_release_workflow_reads_the_same_version(self):
        out = subprocess.run([sys.executable, str(ROOT / "tools/release_notes.py"), "--version"],
                             capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, __version__)

    def test_the_tool_reports_its_version(self):
        out = subprocess.run([sys.executable, str(ROOT / "tools/autoflash_cli.py"), "--version"],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.strip(), f"Autoflash {__version__}")
        self.assertTrue(re.search(r"\d+\.\d+\.\d+", out))


class PortablePythonTests(unittest.TestCase):
    def test_the_unit_tests_run_on_the_python_the_start_scripts_install(self):
        def versions(path, pattern):
            return set(re.findall(pattern, (ROOT / path).read_text(encoding="utf-8")))
        installed = versions("start.sh", r"portable Python (3\.\d+\.\d+)") | \
            versions("tools/windows-python.ps1", r"portable Python (3\.\d+\.\d+)")
        tested = versions(".github/workflows/emulator-check.yml", r'python-version: "(3\.\d+\.\d+)"')
        self.assertEqual(len(installed), 1, installed)
        self.assertEqual(tested, installed)
