"""Waiting for the recovery firmware: "Keep waiting" waits again."""

import types
import unittest
import time
from unittest import mock
from tools.autoflash import output
from tools.autoflash.routes.espressif import tasmota


class RecoveryWaitTests(unittest.TestCase):
    def ctx(self, versions):
        device = types.SimpleNamespace(host="192.168.0.50", firmware_version=lambda: next(versions),
                                       is_safeboot=lambda: False)
        return types.SimpleNamespace(finder=types.SimpleNamespace(poll=lambda: "192.168.0.50"), device=device)

    def test_it_is_seen_once_the_version_says_safeboot(self):
        ctx = self.ctx(iter(["15.6.0(tasmota)", "15.6.0(safeboot)"]))
        with mock.patch.object(time, "sleep"), mock.patch.object(output, "say"):
            self.assertTrue(tasmota.recovery_firmware_up(ctx, 0.0, within=300))

    def test_it_gives_up_on_this_window_without_stopping_the_run(self):
        clock = iter(range(0, 1000, 100))
        ctx = self.ctx(iter(lambda: "15.6.0(tasmota)", None))
        with mock.patch.object(time, "sleep"), mock.patch.object(time, "monotonic", lambda: next(clock)):
            self.assertFalse(tasmota.recovery_firmware_up(ctx, 0.0, within=300))


class StepCanonicalWaitTests(unittest.TestCase):
    """F2 asks what now once it has waited five minutes: ten with nothing to
    see would be too long."""

    def wait(self, seen):
        ctx = types.SimpleNamespace(state=types.SimpleNamespace(data={}, input=lambda key, default=None: None),
                                    device=types.SimpleNamespace(), dry_run=True)
        with mock.patch.object(tasmota, "recovery_firmware_up", side_effect=seen), \
                mock.patch.object(tasmota.runs, "run_source", return_value="sonoff"), \
                mock.patch.object(tasmota, "startup_settings_report", return_value=None), \
                mock.patch.object(tasmota.prompts, "section"), mock.patch.object(time, "sleep"), \
                mock.patch.object(tasmota.prompts, "ask_choice", return_value=0) as asked, \
                mock.patch.object(output, "say"), mock.patch.object(output, "transient_begin"), \
                mock.patch.object(output, "transient_end"):
            tasmota.step_canonical(ctx)
        return asked.call_count

    def test_the_person_is_asked_after_the_first_wait(self):
        self.assertEqual(self.wait([True]), 0)
        self.assertEqual(self.wait([False, True]), 1)


if __name__ == "__main__":
    unittest.main()
