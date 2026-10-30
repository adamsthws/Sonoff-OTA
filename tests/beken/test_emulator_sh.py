"""bk-emu.sh hands the container its own process: a signal to the script - a
timeout's, a Ctrl-C - reaches the docker client and so the container, instead
of leaving both running behind a script that has gone."""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "beken" / "emulator" / "bk-emu.sh"


@unittest.skipIf(shutil.which("bash") is None, "no bash")
class ContainerIsExecedTests(unittest.TestCase):
    def test_docker_runs_as_the_script_itself(self):
        function = re.search(r"^container\(\) \{.*?^\}", SCRIPT.read_text(encoding="utf-8"), re.S | re.M).group(0)
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp, "docker")
            fake.write_text("#!/bin/sh\necho \"docker pid $$: $*\"\n", encoding="utf-8")
            fake.chmod(0o755)
            script = ("set -u\nROOT=%s FIRMWARES=%s/firmwares IMAGE=img S61_DUMP=dump.bin\n%s\n"
                      "echo \"shell pid $$\"\ncontainer python -c pass\necho \"still here\"\n"
                      % (tmp, tmp, function))
            done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                                  env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}"}, cwd=tmp)
        self.assertEqual(done.returncode, 0, done.stderr)
        shell, docker = re.search(r"shell pid (\d+)", done.stdout), re.search(r"docker pid (\d+)", done.stdout)
        self.assertEqual(shell.group(1), docker.group(1), done.stdout)
        self.assertNotIn("still here", done.stdout)


if __name__ == "__main__":
    unittest.main()
