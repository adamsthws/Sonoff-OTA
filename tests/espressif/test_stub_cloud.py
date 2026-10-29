"""The cloud stub's helpers run on their own, as the emulator runs them: in
isolated mode (-I), where only their own path puts the repository on sys.path."""

import subprocess
import sys
import unittest
from pathlib import Path

STUB = Path(__file__).resolve().parents[2] / "tools" / "espressif" / "emulator" / "stub_cloud"


class StubHelpersRunIsolatedTests(unittest.TestCase):
    def run_isolated(self, script):
        return subprocess.run([sys.executable, "-I", str(STUB / script), "--help"],
                              capture_output=True, text=True, timeout=60)

    def test_device_identity_imports_the_repository(self):
        done = self.run_isolated("device_identity.py")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("usage:", done.stdout)

    def test_make_ca_imports_the_repository(self):
        done = self.run_isolated("make_ca.py")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("usage:", done.stdout)


if __name__ == "__main__":
    unittest.main()
