"""Waiting for the recovery firmware: "Keep waiting" waits again."""

import types
import unittest
from unittest import mock

import tools.autoflash_cli as af


class RecoveryWaitTests(unittest.TestCase):
    def ctx(self, versions):
        device = types.SimpleNamespace(host="192.168.0.50", firmware_version=lambda: next(versions),
                                       is_safeboot=lambda: False)
        return types.SimpleNamespace(finder=types.SimpleNamespace(poll=lambda: "192.168.0.50"), device=device)

    def test_it_is_seen_once_the_version_says_safeboot(self):
        ctx = self.ctx(iter(["15.6.0(tasmota)", "15.6.0(safeboot)"]))
        with mock.patch.object(af.time, "sleep"), mock.patch.object(af, "say"):
            self.assertTrue(af.recovery_firmware_up(ctx, 0.0, within=300))

    def test_it_gives_up_on_this_window_without_stopping_the_run(self):
        clock = iter(range(0, 1000, 100))
        ctx = self.ctx(iter(lambda: "15.6.0(tasmota)", None))
        with mock.patch.object(af.time, "sleep"), mock.patch.object(af.time, "monotonic", lambda: next(clock)):
            self.assertFalse(af.recovery_firmware_up(ctx, 0.0, within=300))


if __name__ == "__main__":
    unittest.main()
