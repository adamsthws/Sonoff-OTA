"""This computer is kept awake for a run, with nothing installed, and let go after."""
import shutil
import subprocess
import sys
import textwrap
import time
import unittest
from unittest import mock

from tools.autoflash import awake


def inhibitors() -> str:
    return subprocess.run(["systemd-inhibit", "--list", "--no-pager"], capture_output=True, text=True).stdout


class AwakeTests(unittest.TestCase):
    def tearDown(self):
        awake.let_sleep()

    def test_with_nothing_to_do_it_the_run_goes_on(self):
        with mock.patch.object(awake.shutil, "which", return_value=None), \
             mock.patch.object(awake.os, "name", "posix"), mock.patch.object(awake.sys, "platform", "linux"):
            self.assertIn("not kept awake", awake.keep_awake())

    @unittest.skipUnless(shutil.which("systemd-inhibit") and sys.platform.startswith("linux"),
                         "systemd-inhibit is not on this system")
    def test_systemd_holds_off_sleep_and_lets_go(self):
        self.assertEqual(awake.keep_awake(), "kept awake with systemd-inhibit")
        time.sleep(0.5)
        self.assertIn("Autoflash", inhibitors())
        awake.let_sleep()
        time.sleep(0.5)
        self.assertNotIn("Autoflash", inhibitors())

    @unittest.skipUnless(shutil.which("systemd-inhibit") and sys.platform.startswith("linux"),
                         "systemd-inhibit is not on this system")
    def test_a_run_killed_outright_leaves_no_hold_behind(self):
        code = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(__import__("pathlib").Path(__file__).resolve().parents[1])!r})
            from tools.autoflash import awake
            print(awake.keep_awake(), flush=True)
            time.sleep(60)
        """)
        run = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.assertIn("kept awake", run.stdout.readline())
        time.sleep(0.5)
        self.assertIn("Autoflash", inhibitors())
        run.kill()  # no clean-up of its own
        run.wait()
        deadline = time.monotonic() + 15
        while "Autoflash" in inhibitors() and time.monotonic() < deadline:
            time.sleep(1)
        self.assertNotIn("Autoflash", inhibitors())


if __name__ == "__main__":
    unittest.main()
