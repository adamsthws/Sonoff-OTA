import os
import subprocess
import tempfile
import unittest
from pathlib import Path

PLUG = Path(__file__).resolve().parents[2] / "tools/espressif/emulator/container/bin/plug"


class PlugRouterTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.run_dir = self.dir / "run"
        self.run_dir.mkdir()
        (self.run_dir / "qemu.pid").write_text(str(os.getpid()), encoding="utf-8")
        self.plug = self.dir / "plug"
        self.plug.write_text(PLUG.read_text(encoding="utf-8").replace("/run/autoflash-emu", str(self.run_dir)),
                             encoding="utf-8")
        self.plug.chmod(0o755)
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        # QEMU's monitor, as a client sees it: what it was sent is kept.
        socat = bin_dir / "socat"
        socat.write_text(f'#!/bin/bash\ncat > {self.dir}/sent\nprintf "%b" "$ANSWER"\n', encoding="utf-8")
        socat.chmod(0o755)
        self.env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

    def router(self, state, answer):
        return subprocess.run([str(self.plug), "router", state], env={**self.env, "ANSWER": answer},
                              capture_output=True, text=True, check=False)

    def test_off_taken(self):
        result = self.router("off", "(qemu) qom-get wifi access-points-off\r\ntrue\r\n(qemu) ")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("qom-set wifi access-points-off on\n", (self.dir / "sent").read_text(encoding="utf-8"))
        self.assertTrue((self.run_dir / "router-off").exists())

    def test_on_taken(self):
        (self.run_dir / "router-off").touch()
        result = self.router("on", "false\r\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("qom-set wifi access-points-off off\n", (self.dir / "sent").read_text(encoding="utf-8"))
        self.assertFalse((self.run_dir / "router-off").exists())

    def test_a_monitor_that_does_not_answer_fails(self):
        result = self.router("off", "")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not take the switch", result.stderr)

    def test_the_old_value_read_back_fails(self):
        self.assertNotEqual(self.router("off", "false\r\n").returncode, 0)

    def test_a_command_echoed_is_not_an_answer(self):
        self.assertNotEqual(self.router("off", "qom-get wifi access-points-off true\r\n").returncode, 0)

    def test_with_the_plug_switched_off_the_next_start_is_left_to_take_it(self):
        (self.run_dir / "qemu.pid").unlink()
        result = self.router("off", "")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.dir / "sent").exists())
        self.assertTrue((self.run_dir / "router-off").exists())

    def test_a_restart_with_the_router_off_names_the_fork_s_device(self):
        # QEMU ignores a -global for a type that does not exist. esp32c3_wifi is
        # only the NIC model: the device is esp32_wifi on both chips.
        supervisor = PLUG.with_name("plug-supervisor").read_text(encoding="utf-8")
        self.assertIn("driver=esp32_wifi,property=access-points-off,value=on", supervisor)


if __name__ == "__main__":
    unittest.main()
