"""PhaseRunner starts the plan server (serve_safeboot_migration.py) where it is
in the repository: a script moved (the 2026-10 reorganisation took the safeboot
tooling to tools/espressif/safeboot/) stops every conversion at D5 otherwise."""

import types
import unittest
from pathlib import Path

from tools.autoflash.phases import PhaseRunner

ROOT = Path(__file__).resolve().parents[2]


class PlanServerCommandTests(unittest.TestCase):
    def test_the_plan_server_script_is_where_the_command_says(self):
        runner = PhaseRunner(repo_root=ROOT, manifest=ROOT / "m.json", evidence_dir=ROOT / "live",
                             listen_ip="192.0.2.141", device=types.SimpleNamespace(host="192.0.2.50"),
                             log=lambda _: None)
        for phase in ("stage", "commit", "restore"):
            with self.subTest(phase=phase):
                command = runner._command(phase, [])
                script = Path(command[command.index("-c") + 2])   # python -c RUN_SCRIPT <script> ...
                self.assertTrue(script.is_file(), script)
                self.assertEqual(script.name, "serve_safeboot_migration.py")


if __name__ == "__main__":
    unittest.main()
