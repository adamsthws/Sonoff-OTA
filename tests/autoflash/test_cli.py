"""The command line's own handling, before a run starts."""
import io
import unittest
from unittest import mock

from tools.autoflash import cli, credentials, hostsys, interrupt, output, prompts
from tools.autoflash.routes.beken import images as beken_images
from tools.autoflash.routes.espressif import installer


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


if __name__ == "__main__":
    unittest.main()
