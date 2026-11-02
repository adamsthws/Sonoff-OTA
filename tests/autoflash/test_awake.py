"""This computer is kept awake for a run, with nothing installed, and let go after."""
import shutil
import subprocess
import sys
import textwrap
import time
import unittest
from unittest import mock

from tools.autoflash import awake


# The run's own hold, as awake.keep_awake takes it: not an emulator's
# ("Autoflash emulator"), which may be held at the same time.
RUN_HOLD = "A device is being converted"


def inhibitors(pid: int) -> str:
    """This run's hold in systemd's list - the one that systemd-inhibit process
    holds - not another test's, run beside this one."""
    listed = subprocess.run(["systemd-inhibit", "--list", "--no-pager"], capture_output=True, text=True).stdout
    return "\n".join(line for line in listed.splitlines() if RUN_HOLD in line and f" {pid} " in f" {line} ")


def comes_to(check, want: bool, within: float = 15) -> bool:
    """Whether `check()` comes to `want` within `within` seconds: systemd
    lists a hold, or drops it, a moment after the process that takes it."""
    deadline = time.monotonic() + within
    while bool(check()) != want and time.monotonic() < deadline:
        time.sleep(0.1)
    return bool(check()) == want


def systemd_holds() -> bool:
    """Whether this system lets this user hold off sleep: a runner with no
    login session (GitHub Actions) has systemd-inhibit, but takes no hold."""
    if not (shutil.which("systemd-inhibit") and sys.platform.startswith("linux")):
        return False
    probe = subprocess.Popen(["systemd-inhibit", "--what=sleep", "--who=autoflash-test-probe",
                              "--why=probe", "--mode=block", "sleep", "15"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        return comes_to(lambda: "autoflash-test-probe" in subprocess.run(
            ["systemd-inhibit", "--list", "--no-pager"], capture_output=True, text=True).stdout,
            True, within=10)
    finally:
        probe.kill()
        probe.wait()


HOLDS = systemd_holds()


class AwakeTests(unittest.TestCase):
    def tearDown(self):
        awake.let_sleep()

    def test_with_nothing_to_do_it_the_run_goes_on(self):
        with mock.patch.object(awake.shutil, "which", return_value=None), \
             mock.patch.object(awake.os, "name", "posix"), mock.patch.object(awake.sys, "platform", "linux"):
            self.assertIn("not kept awake", awake.keep_awake())

    @unittest.skipUnless(HOLDS, "systemd does not let this user hold off sleep here")
    def test_systemd_holds_off_sleep_and_lets_go(self):
        self.assertEqual(awake.keep_awake(), "kept awake with systemd-inhibit")
        pid = awake._held["process"].pid
        self.assertTrue(comes_to(lambda: inhibitors(pid), True), "no hold taken")
        awake.let_sleep()
        self.assertTrue(comes_to(lambda: inhibitors(pid), False), "the hold was not let go")

    @unittest.skipUnless(HOLDS, "systemd does not let this user hold off sleep here")
    def test_a_run_killed_outright_leaves_no_hold_behind(self):
        code = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(__import__("pathlib").Path(__file__).resolve().parents[2])!r})
            from tools.autoflash import awake
            print(awake.keep_awake(), awake._held["process"].pid, flush=True)
            time.sleep(60)
        """)
        run = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        line = run.stdout.readline()
        self.assertIn("kept awake", line)
        pid = int(line.split()[-1])
        self.assertTrue(comes_to(lambda: inhibitors(pid), True), "no hold taken")
        run.kill()  # no clean-up of its own
        run.wait()
        self.assertTrue(comes_to(lambda: inhibitors(pid), False), "the hold outlived the run")


if __name__ == "__main__":
    unittest.main()
