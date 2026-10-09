"""The run's log can be shared for support: nothing in it identifies anyone."""

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.autoflash import output
from tools.autoflash.state import RunState


class PrivacyTestCase(unittest.TestCase):
    def setUp(self):
        saved = dict(output._private)
        output._private.clear()
        self.addCleanup(lambda: (output._private.clear(), output._private.update(saved)))


class ScrubTests(PrivacyTestCase):
    def test_what_identifies_someone_is_replaced(self):
        cases = {
            "--email someone@gmail.com --listen-ip 192.168.0.10": "--email <email> --listen-ip 192.168.0.10",
            '"GET /user1.bin?deviceid=1001234abc&ts=4708736&sign=fafd2f9d3bcc HTTP/1.1"':
                '"GET /user1.bin?deviceid=<device-id>&ts=4708736&sign=<signature> HTTP/1.1"',
            "Tasmota is at 192.168.0.50 as tasmota-1A2B3C-4567.": "Tasmota is at 192.168.0.50 as <device-name>.",
            "the plug's address is AA:BB:CC:12:34:56": "the plug's address is <mac>",
            '{"apikey": "abc-123", "at": "token", "action": "update"}':
                '{"apikey": "<redacted>", "at": "<redacted>", "action": "update"}',
            "[sudo] password for someone: ": "[sudo] password for <user>: ",
            "this computer is 81.2.69.160 on the internet": "this computer is <public-ip> on the internet",
        }
        for line, expected in cases.items():
            self.assertEqual(output.scrub(line), expected)

    def test_what_is_needed_to_follow_a_run_stays(self):
        for line in ("The plug is at 192.168.0.50; this computer is 10.0.0.2 and 172.16.4.1",
                     "Tasmota 15.6.0(release-tasmota), template tasmota-settings-template-15.6.0.0.bin",
                     "Stage 3 of 9 - Discovering the plug - Complete (4s)",
                     "Sonoff S60 Plug model S60TPG"):
            self.assertEqual(output.scrub(line), line)

    def test_the_home_folder_is_shortened(self):
        path = str(Path.home() / "Git" / "repo" / "captures" / "run.log")
        expected = os.sep.join(["~", "Git", "repo", "captures", "run.log"])
        self.assertEqual(output.scrub(f"saved to {path}"), f"saved to {expected}")

    def test_values_the_run_learns_are_replaced(self):
        output.private("Home & Garden 2G", "wifi-name")
        output.private("hunter22", "wifi-password")
        output.private("Kitchen", "plug-name")
        self.assertEqual(output.scrub("joined 'Home & Garden 2G' with hunter22; Kitchen answered"),
                         "joined '<wifi-name>' with <wifi-password>; <plug-name> answered")

    def test_too_short_to_replace_safely_is_not_registered(self):
        output.private("ab", "wifi-name")
        output.private(None, "wifi-name")
        self.assertEqual(output.scrub("ab cd"), "ab cd")


class LogFileTests(PrivacyTestCase):
    def test_the_log_file_holds_the_scrubbed_text_and_the_screen_the_real_one(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict(os.environ, {"NO_COLOR": "1"}), \
             mock.patch("sys.stdout", io.StringIO()) as screen:
            log = Path(directory) / "run.log"
            output.private("Home-2G", "wifi-name")
            output.start(log, verbose=True)
            output.show("Network check: the plug is on 'Home-2G'; account someone@gmail.com")
            output.answered("someone@gmail.com")
            output.finish()
            written = log.read_text()
        self.assertIn("'Home-2G'", screen.getvalue())
        self.assertIn("Network check: the plug is on '<wifi-name>'; account <email>", written)
        self.assertIn("-> <email>", written)
        self.assertNotIn("Home-2G", written)
        self.assertNotIn("someone@gmail.com", written)

    def test_a_resumed_run_knows_its_private_inputs_again(self):
        with tempfile.TemporaryDirectory() as directory:
            state = RunState.open(Path(directory))
            state.set_input("device_name", "kitchen-plug")
            state.set_input("plug_ip", "192.168.0.50")
            output._private.clear()
            RunState.open(Path(directory))  # as a new run reads it back
        self.assertEqual(output.scrub("kitchen-plug at 192.168.0.50"), "<device-name> at 192.168.0.50")


if __name__ == "__main__":
    unittest.main()
