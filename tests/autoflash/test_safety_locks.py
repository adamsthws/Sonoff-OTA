"""The safety locks, lifted by more than one run at once (two plugs) - put
back only when the last is done, and never left lifted by a run that died."""
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tools.autoflash import safety_locks

NAME = "RECOVERY_LOCK"


class SafetyLockTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / NAME).write_text("shipped lock\n")

    def lock_in_place(self):
        return (self.root / NAME).exists()

    def other_run(self, then):
        """Another run, in a process of its own, with the lock lifted."""
        code = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})
            from pathlib import Path
            from tools.autoflash import safety_locks
            with safety_locks.lifted(Path({str(self.root)!r}), {NAME!r}):
                print("lifted", flush=True)
                {then}
        """)
        process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                   stdin=subprocess.PIPE, text=True)
        self.assertEqual(process.stdout.readline().strip(), "lifted")
        self.addCleanup(lambda: (process.kill(), process.wait()))
        return process

    def test_lifted_for_the_block_and_put_back_after(self):
        with safety_locks.lifted(self.root, NAME):
            self.assertFalse(self.lock_in_place())
        self.assertTrue(self.lock_in_place())
        self.assertEqual((self.root / NAME).read_text(), "shipped lock\n")

    def test_put_back_even_when_the_block_fails(self):
        with self.assertRaises(RuntimeError):
            with safety_locks.lifted(self.root, NAME):
                raise RuntimeError("the helper failed")
        self.assertTrue(self.lock_in_place())

    def test_kept_lifted_while_another_run_still_needs_it(self):
        other = self.other_run("sys.stdin.readline()")
        with safety_locks.lifted(self.root, NAME):
            self.assertFalse(self.lock_in_place())
        self.assertFalse(self.lock_in_place())  # the other run's helper still runs
        self.assertTrue(safety_locks.in_place(self.root, NAME))  # but it is accounted for
        self.assertEqual(safety_locks.restore_left(self.root), [])  # and not put back under it
        other.stdin.write("done\n")
        other.stdin.flush()
        other.wait(timeout=30)
        self.assertTrue(self.lock_in_place())  # the last one out put it back

    def test_one_left_lifted_by_a_run_that_died_is_put_back(self):
        other = self.other_run("import os; os._exit(9)")  # gone without a word, as in a crash
        other.wait(timeout=30)
        self.assertFalse(self.lock_in_place())
        self.assertFalse(safety_locks.in_place(self.root, NAME))
        self.assertEqual(safety_locks.restore_left(self.root), [NAME])
        self.assertTrue(self.lock_in_place())

    def test_a_lift_from_before_this_record_is_put_back(self):
        # A run of an earlier version moved it aside and stopped there.
        (self.root / NAME).rename(self.root / f"{NAME}.owner-authorized")
        self.assertEqual(safety_locks.restore_left(self.root), [NAME])
        self.assertTrue(self.lock_in_place())


if __name__ == "__main__":
    unittest.main()
