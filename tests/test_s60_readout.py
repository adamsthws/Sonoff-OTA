import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import s60_readout


class FindBridgeTests(unittest.TestCase):
    def run_find(self, answers, by_mac=None, mac="aa:bb:cc:12:34:56"):
        ticks = iter(range(0, 1000, 3))
        return s60_readout.find_bridge(
            "192.168.0.50", mac, timeout=30,
            probe=lambda host: host in answers,
            by_mac=by_mac or (lambda m, s: None),
            clock=lambda: next(ticks), sleep=lambda s: None,
        )

    def test_found_where_the_plug_was(self):
        self.assertEqual(self.run_find({"192.168.0.50"}), "192.168.0.50")

    def test_found_by_hardware_address_when_moved(self):
        self.assertEqual(self.run_find({"192.168.0.77"}, by_mac=lambda m, s: "192.168.0.77"), "192.168.0.77")

    def test_another_device_at_the_moved_address_is_not_taken(self):
        self.assertIsNone(self.run_find(set(), by_mac=lambda m, s: "192.168.0.77"))

    def test_gives_up_without_a_mac(self):
        self.assertIsNone(self.run_find(set(), mac=None))


class FakeSender:
    def __init__(self, root, seen, lines=("Firmware: ok\n",), code=0):
        self.root, self.seen, self.lines, self.code = root, seen, lines, code

    def __call__(self, command, **kwargs):
        self.seen["lock_present"] = (self.root / "RECOVERY_LOCK").exists()
        self.seen["command"] = command
        self.stdout = iter(self.lines)
        return self

    def wait(self):
        return self.code


class DeliverTests(unittest.TestCase):
    def test_recovery_lock_is_restored_when_the_sender_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "RECOVERY_LOCK").write_text("lock")
            with mock.patch.object(s60_readout, "ROOT", root), \
                 mock.patch.object(s60_readout.subprocess, "Popen", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    s60_readout.deliver("192.168.0.50", "192.168.0.10", 8088, "a@b", "1.2.0", root, {})
            self.assertTrue((root / "RECOVERY_LOCK").exists())
            self.assertFalse((root / "RECOVERY_LOCK.owner-authorized").exists())

    def test_sender_runs_with_the_lock_lifted_and_its_output_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "RECOVERY_LOCK").write_text("lock")
            seen = {}
            sender = FakeSender(root, seen, lines=("  Cloud: the plug went offline\n",))
            with mock.patch.object(s60_readout, "ROOT", root), \
                 mock.patch.object(s60_readout.subprocess, "Popen", side_effect=sender), \
                 mock.patch("builtins.print"):
                self.assertEqual(s60_readout.deliver("192.168.0.50", "192.168.0.10", 8088, "a@b", "1.2.0", root, {}), 0)
            self.assertFalse(seen["lock_present"])
            self.assertIn(str(s60_readout.READOUT_OTA), seen["command"])
            self.assertIn("1.2.0", seen["command"])
            self.assertTrue((root / "RECOVERY_LOCK").exists())
            self.assertIn("went offline", (root / "sender.log").read_text())

    def test_the_readout_file_is_the_conversion_bridge_version(self):
        from tools import s60_autoflash
        self.assertEqual(s60_readout.READOUT_OTA.name.rsplit("-", 1)[1],
                         s60_autoflash.BRIDGE_OTA.name.rsplit("-", 1)[1])


class CloudChatterTests(unittest.TestCase):
    def test_routine_cloud_lines_are_hidden_but_problems_are_not(self):
        self.assertTrue(s60_readout.is_cloud_chatter("  Cloud: the plug went offline - expected while it restarts\n"))
        self.assertTrue(s60_readout.is_cloud_chatter("  Cloud: upgrade command accepted\n"))
        self.assertFalse(s60_readout.is_cloud_chatter("  Cloud: upgrade command rejected (error 406)\n"))
        self.assertFalse(s60_readout.is_cloud_chatter("  Complete firmware byte coverage observed\n"))

    def test_hidden_lines_still_reach_the_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "RECOVERY_LOCK").write_text("lock")
            sender = FakeSender(root, {}, lines=("  Cloud: the plug is back online\n", "Serving\n"))
            with mock.patch.object(s60_readout, "ROOT", root), \
                 mock.patch.object(s60_readout.subprocess, "Popen", side_effect=sender), \
                 mock.patch("builtins.print") as printed:
                s60_readout.deliver("192.168.0.50", "192.168.0.10", 8088, "a@b", "1.2.0", root, {})
            shown = "".join(call.args[0] for call in printed.call_args_list)
            self.assertNotIn("Cloud", shown)
            self.assertIn("Serving", shown)
            self.assertIn("back online", (root / "sender.log").read_text())


class SenderOutputTests(unittest.TestCase):
    SERVED = "  Served /user1.bin bytes 389,220-399,459 (10,240 bytes)\n"
    REQUEST = '  [192.168.0.50] "GET /user1.bin?deviceid=1&ts=2&sign=3 HTTP/1.1" 206 -\n'

    def test_routine_http_lines_are_not_shown(self):
        self.assertEqual(s60_readout.sender_line_kind(self.SERVED), "served")
        self.assertEqual(s60_readout.sender_line_kind(self.REQUEST), "hide")
        self.assertEqual(s60_readout.sender_line_kind(self.REQUEST.replace("206", "403")), "show")
        self.assertEqual(s60_readout.sender_line_kind("  Complete firmware byte coverage observed\n"), "show")

    def test_progress_line_reports_how_far_the_plug_has_got(self):
        progress = s60_readout.SenderProgress(869332)
        with mock.patch("sys.stdout") as out:
            progress.update(self.SERVED)
        self.assertIn("45%", out.write.call_args_list[0].args[0])

    def test_non_verbose_screen_has_progress_not_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "RECOVERY_LOCK").write_text("lock")
            sender = FakeSender(root, {}, lines=(self.REQUEST, self.SERVED, "  Complete firmware byte coverage observed\n"))
            with mock.patch.object(s60_readout, "ROOT", root), \
                 mock.patch.object(s60_readout.subprocess, "Popen", side_effect=sender), \
                 mock.patch("builtins.print") as printed, mock.patch("sys.stdout"):
                s60_readout.deliver("192.168.0.50", "192.168.0.10", 8088, "a@b", "1.2.0", root, {})
            shown = "".join(call.args[0] for call in printed.call_args_list)
            self.assertNotIn("GET", shown)
            self.assertNotIn("Served", shown)
            self.assertIn("Complete firmware", shown)
            self.assertIn("GET", (root / "sender.log").read_text())


class ControlTests(unittest.TestCase):
    def test_controls_are_the_pinned_published_bridges(self):
        sums = (s60_readout.ROOT / "SHA256SUMS").read_text()
        for path, digest in s60_readout.CONTROL_FILES.values():
            s60_readout.check_control_file(s60_readout.ROOT / path, digest)
            self.assertIn(f"{digest}  {Path(path).as_posix()}", sums)

    def test_the_default_control_is_what_the_conversion_sends(self):
        from tools import s60_autoflash
        self.assertEqual(s60_readout.CONTROL_FILES["4.4.1"][0], s60_autoflash.BRIDGE_OTA)

    def test_a_changed_file_is_refused(self):
        with tempfile.NamedTemporaryFile() as handle:
            handle.write(b"not the bridge")
            handle.flush()
            with self.assertRaises(s60_readout.Stop):
                s60_readout.check_control_file(Path(handle.name), "0" * 64)


class BridgeIdTests(unittest.TestCase):
    def test_old_and_new_names_are_both_bridges(self):
        import re
        for status in (b"bridge v5\nhome network: joined", b"bridge 4.4.1\nhome network: joined",
                       b"bridge 4.4.12 flash readout\n"):
            self.assertIsNotNone(re.match(s60_readout.BRIDGE_ID, status), status)
        self.assertIsNone(re.match(s60_readout.BRIDGE_ID, b"tasmota"))


class ConfirmTests(unittest.TestCase):
    def test_only_the_word_goes_ahead(self):
        for answer, expected in (("readout", True), (" READOUT ", True), ("proceed", False), ("", False)):
            with mock.patch("builtins.input", return_value=answer), mock.patch("builtins.print"):
                self.assertEqual(s60_readout.confirmed("1.2.0"), expected, answer)


if __name__ == "__main__":
    unittest.main()
