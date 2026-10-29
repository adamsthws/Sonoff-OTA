"""windows-test.sh tells a Windows layer to be set up again from the hash of the
files it was built from: every one of them must be where the script looks, or
the hash is of nothing and every run installs Windows afresh."""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "espressif" / "emulator" / "windows-test.sh"


@unittest.skipIf(shutil.which("bash") is None or shutil.which("sha256sum") is None, "no bash")
class LayerHashTests(unittest.TestCase):
    def built_from(self, layer):
        text = SCRIPT.read_text(encoding="utf-8")
        function = re.search(r"^built_from\(\) \{.*?^\}", text, re.S | re.M).group(0)
        script = "set -u\nROOT=%s ISO_SHA256=iso GUEST_TOOLS_SHA256=tools\n%s\nbuilt_from %s\n" % (
            ROOT, function, layer)
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)

    def test_every_layers_files_are_found(self):
        for layer in ("installed", "rig", "tool"):
            with self.subTest(layer=layer):
                done = self.built_from(layer)
                self.assertEqual(done.stderr, "")
                self.assertRegex(done.stdout.strip(), r"^[0-9a-f]{16}$")


if __name__ == "__main__":
    unittest.main()
