"""What an S60 on the network is running, from what it answers."""
import unittest
from unittest import mock

from tools.autoflash import detect

TASMOTA_TABLE = "1" * 64


class FakeTasmota:
    def __init__(self, *, version, table, module="Sonoff S60TPG", safeboot=False, hostname="tasmota-ABCDEF-1234"):
        self.version, self.table, self.module = version, table, module
        self.safeboot, self.hostname = safeboot, hostname

    def __call__(self, host, timeout=None):
        return self

    def is_safeboot(self):
        return self.safeboot

    def command(self, command):
        if command == "Status 0":
            return {"Status": {"Module": self.module}, "StatusNET": {"Hostname": self.hostname},
                    "StatusFWR": {"Version": self.version}}
        if command == "Status 5":
            return {"StatusNET": {"Hostname": self.hostname}}
        return {"NAME": self.module}

    def sha_region(self, address, length, timeout=None):
        return self.table


class TasmotaTests(unittest.TestCase):
    def kind(self, fake):
        with mock.patch.object(detect, "Device", fake):
            found = detect._tasmota("192.168.0.37", 2.0)
        return found and found[0]

    def test_the_routes_own_tasmota_on_sonoffs_layout_is_part_way(self):
        fake = FakeTasmota(version="15.6.0.1(f5b34a2-bluetooth)", table=detect.SONOFF_TABLE_SHA256,
                           module="Generic")
        self.assertEqual(self.kind(fake), "hub")

    def test_official_tasmota_with_sonoffs_table_is_still_leaving_tasmota(self):
        # The table written, the restart into the mover not yet: leaving Tasmota
        # is done again, and finds its writes done - never taken as the hub.
        fake = FakeTasmota(version="15.6.0(release-tasmota32)", table=detect.SONOFF_TABLE_SHA256)
        self.assertEqual(self.kind(fake), "tasmota")

    def test_official_tasmota_counts_only_with_the_s60s_template(self):
        self.assertEqual(self.kind(FakeTasmota(version="15.6.0(release-tasmota32)", table=TASMOTA_TABLE)),
                         "tasmota")
        self.assertIsNone(self.kind(FakeTasmota(version="15.6.0(release-tasmota)", table=TASMOTA_TABLE,
                                                module="Jomarto SH0617")))

    def test_only_this_tools_startup_firmware_counts(self):
        self.assertEqual(self.kind(FakeTasmota(version="", table="", safeboot=True,
                                               hostname="s60-recovery-0076")), "startup")
        self.assertIsNone(self.kind(FakeTasmota(version="", table="", safeboot=True)))


if __name__ == "__main__":
    unittest.main()
