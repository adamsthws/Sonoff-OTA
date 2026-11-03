"""The command line's own handling, before a run starts."""
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.autoflash import cli, credentials, hostsys, interrupt, output, prompts, runs
from tools.autoflash.routes.beken import images as beken_images
from tools.autoflash.routes.espressif import installer
from tools.autoflash.state import RunState


class BeforeTheRunTests(unittest.TestCase):
    def main(self, *, choosing):
        """cli.main() for `run`, the question of which run answered by `choosing`.
        What main() sets for the whole process is put back afterwards."""
        screen = io.StringIO()
        with mock.patch("sys.argv", ["cli.py", "run"]), mock.patch("sys.stdout", screen), \
                mock.patch.object(hostsys, "password_asker"), mock.patch.object(prompts, "UNATTENDED"), \
                mock.patch.object(installer, "SIMULATED_BACKUP_FAILURES"), \
                mock.patch.object(beken_images, "SPOIL_STEP"), \
                mock.patch.object(cli.signal, "signal"), mock.patch.object(interrupt, "install"), \
                mock.patch.object(credentials, "load", return_value={}), \
                mock.patch.object(cli, "adopt_older_runs"), mock.patch.object(output, "start_at_top"), \
                mock.patch.object(prompts, "wait_for_taller_window"), \
                mock.patch.object(cli, "resolve_run_name", side_effect=choosing):
            return cli.main(), screen.getvalue()

    def test_ctrl_c_at_the_first_question_stops_without_a_traceback(self):
        # Seen: Ctrl-C at "A conversion did not finish last time. What now"
        # ended in a KeyboardInterrupt traceback.
        code, screen = self.main(choosing=KeyboardInterrupt)
        self.assertEqual(code, 130)
        self.assertIn("Stopped. Nothing was changed.", screen)

    def test_q_there_stops_the_same_way(self):
        code, screen = self.main(choosing=prompts.QuitRun("stopped at an operator prompt"))
        self.assertEqual(code, 130)
        self.assertIn("Stopped. Nothing was changed.", screen)


class RunToLookAtTests(unittest.TestCase):
    """`status` and `cleanup` report on a run without starting one."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.current = None
        for patch in (
            mock.patch.object(runs, "run_directories",
                              lambda: sorted(p for p in self.root.iterdir() if p.is_dir())),
            mock.patch.object(runs, "read_current", lambda: self.current),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def run_at(self, name, plug_ip):
        RunState.open(self.root / name).set_input("plug_ip", plug_ip)
        return runs.run_name(self.root / name)

    def test_given_an_address_it_is_that_devices_latest_run(self):
        # Seen: status --plug-ip told of the current run, another device's.
        self.run_at("1-first", "10.0.0.51")
        latest = self.run_at("2-again", "10.0.0.51")
        self.current = self.run_at("3-other", "10.0.0.60")
        self.assertEqual(cli.run_to_look_at("10.0.0.51"), latest)

    def test_an_address_with_no_run_has_none(self):
        self.current = self.run_at("1-other", "10.0.0.60")
        self.assertIsNone(cli.run_to_look_at("10.0.0.51"))

    def test_without_an_address_it_is_the_current_run_else_the_latest(self):
        first = self.run_at("1-first", "10.0.0.51")
        latest = self.run_at("2-other", "10.0.0.60")
        self.assertEqual(cli.run_to_look_at(None), latest)
        self.current = first
        self.assertEqual(cli.run_to_look_at(None), first)


if __name__ == "__main__":
    unittest.main()
