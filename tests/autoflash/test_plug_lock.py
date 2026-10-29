"""One run on a plug at a time - and a run that dies lets go of it."""
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from tools.autoflash import plugs

MAC = "12:34:56:ab:cd:ef"


class PlugLockTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        patcher = mock.patch.object(plugs, "PLUGS", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        held = mock.patch.dict(plugs._HELD, {}, clear=True)
        held.start()
        self.addCleanup(held.stop)

    def other_run(self, script):
        """A second process holding the plug, as another run would."""
        code = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})
            from tools.autoflash import plugs
            from pathlib import Path
            plugs.PLUGS = Path({str(self.root)!r})
            plugs.hold({MAC!r}, "plugs/the-other-run")
            print("held", flush=True)
            {script}
        """)
        process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.assertEqual(process.stdout.readline().strip(), "held")
        return process

    def test_a_second_run_is_refused_and_told_which_holds_it(self):
        other = self.other_run("time.sleep(30)")
        try:
            with self.assertRaises(plugs.PlugBusy) as busy:
                plugs.hold(MAC, "plugs/this-run")
            self.assertIn("the-other-run", str(busy.exception))
        finally:
            other.kill()
            other.wait()

    def test_a_run_that_died_leaves_no_lock_behind(self):
        other = self.other_run("import os; os._exit(9)")  # gone without a word, as in a crash
        other.wait()
        plugs.hold(MAC, "plugs/this-run")  # not refused
        self.assertIn(MAC, plugs._HELD)

    def test_the_run_refused_is_told_plainly(self):
        from types import SimpleNamespace
        import tools.autoflash_cli as autoflash
        with mock.patch.object(plugs, "hold", side_effect=plugs.PlugBusy("run plugs/x, process 7")):
            with self.assertRaisesRegex(autoflash.Abort, "another run is converting this plug"):
                autoflash.hold_plug(SimpleNamespace(), MAC)

    def test_busy_names_the_plugs_others_hold(self):
        self.assertEqual(plugs.busy(), [])
        other = self.other_run("time.sleep(30)")
        try:
            busy = plugs.busy()
            self.assertEqual(len(busy), 1)
            self.assertIn(MAC, busy[0])
            self.assertIn("the-other-run", busy[0])
        finally:
            other.kill()
            other.wait()
        self.assertEqual(plugs.busy(), [])  # let go with the process

    def ctx(self, mac=MAC):
        from types import SimpleNamespace
        return SimpleNamespace(state=SimpleNamespace(input=lambda key: mac if key == "plug_mac" else None),
                               dir=self.root / "runs" / "this-run")

    def test_a_carried_on_run_is_refused_before_it_touches_anything(self):
        # Its log, report, firewall rule and safety locks are the other run's
        # too: the run's main stops before any of them (seen in the fault
        # suite: a refused run took the other's firewall rule away).
        import tools.autoflash_cli as autoflash
        other = self.other_run("time.sleep(30)")
        try:
            with mock.patch.object(autoflash, "tell") as told:
                self.assertTrue(autoflash.refused_for_another_run("run", self.ctx()))
            self.assertIn("another run is converting this plug", told.call_args.args[0])
        finally:
            other.kill()
            other.wait()
        self.assertFalse(autoflash.refused_for_another_run("run", self.ctx()))  # then it is this run's

    def test_cleanup_waits_for_every_run_to_finish(self):
        import tools.autoflash_cli as autoflash
        other = self.other_run("time.sleep(30)")
        try:
            with mock.patch.object(autoflash, "tell") as told:
                self.assertTrue(autoflash.refused_for_another_run("cleanup", self.ctx(mac=None)))
            self.assertIn("Not cleaning up", told.call_args.args[0])
        finally:
            other.kill()
            other.wait()
        self.assertFalse(autoflash.refused_for_another_run("cleanup", self.ctx(mac=None)))

    def test_looking_is_never_refused(self):
        import tools.autoflash_cli as autoflash
        other = self.other_run("time.sleep(30)")
        try:
            for command in ("status", "list"):
                self.assertFalse(autoflash.refused_for_another_run(command, self.ctx()))
        finally:
            other.kill()
            other.wait()


if __name__ == "__main__":
    unittest.main()
