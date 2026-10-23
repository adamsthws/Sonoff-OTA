import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.emulator import choose_plug
from tools.emulator.choose_plug import Choice, choose

A = Choice("58:cf:79:01:02:03", Path("a"), "58:cf:79:01:02:03  (Sonoff 1.2.0)")
B = Choice("58:cf:79:0a:0b:0c", Path("b"), "58:cf:79:0a:0b:0c  (Sonoff 1.3.0)")


class ChoosePlugTests(unittest.TestCase):
    """The emulator runs one of your backed-up plugs, chosen by its MAC."""

    def test_one_plug_is_chosen_without_asking(self):
        with mock.patch.object(choose_plug, "ask_choice") as menu:
            self.assertEqual(choose([A], None, None, interactive=True), A)
        menu.assert_not_called()

    def test_several_are_offered_by_mac_with_the_last_one_first(self):
        with mock.patch.object(choose_plug, "ask_choice", return_value=0) as menu:
            self.assertEqual(choose([A, B], None, B.mac, interactive=True), B)
        options = menu.call_args.args[1]
        self.assertTrue(options[0].startswith(B.mac))
        self.assertIn("MAC address", " ".join(menu.call_args.kwargs["help_lines"]))

    def test_with_nobody_to_ask_the_last_one_is_used(self):
        self.assertEqual(choose([A, B], None, B.mac, interactive=False), B)
        with self.assertRaises(SystemExit):
            choose([A, B], None, None, interactive=False)

    def test_a_mac_given_picks_that_plug_in_any_spelling(self):
        self.assertEqual(choose([A, B], "58-CF-79-0A-0B-0C", None, interactive=True), B)
        with self.assertRaises(SystemExit):
            choose([A, B], "00:11:22:33:44:55", None, interactive=True)

    def test_backups_and_an_old_style_dump_are_both_found(self):
        from tools.autoflash import plugs
        from tests.test_autoflash import efuse_with_mac
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "emulation"
            legacy.mkdir()
            (legacy / "flash.bin").write_bytes(b"F")
            (legacy / "efuse.bin").write_bytes(efuse_with_mac("58:cf:79:0a:0b:0c"))
            with mock.patch.object(plugs, "PLUGS", root / "plugs"), \
                 mock.patch.object(choose_plug, "LEGACY", legacy):
                plugs.save_backup(b"F", efuse_with_mac(A.mac), {}, "1.2.0")
                found = choose_plug.choices()
                self.assertEqual([c.mac for c in found], [A.mac, B.mac])
                # The same plug backed up properly is not offered twice.
                plugs.save_backup(b"F", efuse_with_mac(B.mac), {}, "1.2.0")
                self.assertEqual(len(choose_plug.choices()), 2)


if __name__ == "__main__":
    unittest.main()


class RestoreTests(unittest.TestCase):
    def test_with_no_backup_here_the_sealed_one_is_restored_and_a_test_copy_made(self):
        from unittest import mock
        from tools.emulator import choose_plug as cp
        with mock.patch("tools.private_backup.restore", return_value="94:a9:90:00:00:01") as restore, \
             mock.patch("tools.emulator.copy_plug.copy") as copy, \
             mock.patch.object(cp, "choices", return_value=[]), \
             mock.patch.object(cp.plugs, "PLUGS", cp.Path("/nonexistent")), mock.patch("builtins.print"):
            cp.restored("12:34:56:ab:cd:ef")
        restore.assert_called_once()
        copy.assert_called_once_with("94:a9:90:00:00:01", "12:34:56:ab:cd:ef")

    def test_a_restore_that_cannot_be_done_leaves_the_usual_message(self):
        from unittest import mock
        from tools import private_backup
        from tools.emulator import choose_plug as cp
        with mock.patch("tools.private_backup.restore", side_effect=private_backup.SealError("no key")), \
             mock.patch.object(cp, "choices", return_value=[]), mock.patch("sys.stderr"):
            self.assertEqual(cp.restored(None), [])

    def test_restoring_can_be_turned_off(self):
        import os, tempfile
        from unittest import mock
        from tools.emulator import choose_plug as cp
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"AUTOFLASH_NO_RESTORE": "1"}), \
             mock.patch.object(cp, "choices", return_value=[]), mock.patch.object(cp, "restored") as restored, \
             mock.patch("sys.stderr"):
            self.assertEqual(cp.main(["--out", f"{tmp}/plug"]), 1)
        restored.assert_not_called()

