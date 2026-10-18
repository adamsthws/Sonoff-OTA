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

    def test_the_routes_own_tasmota_on_the_interim_layout_is_part_way_too(self):
        # A commit stopped part way: the next step puts Sonoff's layout back.
        fake = FakeTasmota(version="15.6.0.1(f5b34a2-bluetooth)", table=detect.INTERIM_TABLE_SHA256,
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

    def test_a_template_slow_to_answer_is_asked_again(self):
        fake = FakeTasmota(version="15.6.0(release-tasmota32)", table=TASMOTA_TABLE, module=0)
        answers = fake.command
        tries = []

        def busy_at_first(command):
            if command in ("Template", "Module"):
                tries.append(command)
                if len(tries) < 3:
                    raise TimeoutError("timed out")
                return {"Module": {"0": "Sonoff S60TPG"}}
            return answers(command)
        fake.command = busy_at_first
        self.assertEqual(self.kind(fake), "tasmota")
        self.assertEqual(tries, ["Template", "Template", "Module"])

    def test_a_set_up_s60_that_will_not_say_its_template_is_not_taken_for_one_not_set_up(self):
        # Seen once in the emulator: Tasmota busy answering the network sweep
        # did not say its template in time, the plug was taken for a Tasmota
        # not yet set up as an S60, and the run sent it the wrong way.
        fake = FakeTasmota(version="15.6.0(release-tasmota32)", table=TASMOTA_TABLE, module=0)
        answers = fake.command

        def no_template(command):
            if command in ("Template", "Module"):
                raise TimeoutError("timed out")
            return answers(command)
        fake.command = no_template
        neighbours = {"192.168.0.37": "12:34:56:ab:cd:ef"}
        with mock.patch.object(detect, "Device", fake), \
                mock.patch.object(detect.hostsys, "sweep"), \
                mock.patch.object(detect.hostsys, "neighbour_table", return_value=neighbours), \
                mock.patch.object(detect, "_port_open", return_value=True), \
                mock.patch.object(detect, "_is_installer", return_value=False), \
                mock.patch.object(detect.esphome, "is_esphome", return_value=False):
            self.assertEqual(detect.converted_s60s("192.168.0.1", known_macs={"12:34:56:ab:cd:ef"}), [])
            self.assertIsNone(detect.identify("192.168.0.37"))

    def test_a_safeboot_is_taken_only_with_a_mac_this_computer_knows(self):
        # It answers no commands: nothing else tells this tool's startup
        # firmware from any Tasmota part way through an update.
        self.assertEqual(self.kind(FakeTasmota(version="", table="", safeboot=True)), "startup")
        fake = FakeTasmota(version="", table="", safeboot=True)
        neighbours = {"192.168.0.37": "12:34:56:ab:cd:ef", "192.168.0.99": "aa:bb:cc:dd:ee:ff"}
        with mock.patch.object(detect, "Device", fake), \
                mock.patch.object(detect.hostsys, "sweep"), \
                mock.patch.object(detect.hostsys, "neighbour_table", return_value=neighbours), \
                mock.patch.object(detect, "_port_open", return_value=True), \
                mock.patch.object(detect.esphome, "is_esphome", return_value=False):
            found = detect.converted_s60s("192.168.0.1", known_macs={"12:34:56:ab:cd:ef"})
        self.assertEqual([(f.host, f.firmware) for f in found], [("192.168.0.37", "midway")])

    def test_a_tasmota_not_yet_set_up_is_taken_only_with_a_mac_this_computer_knows(self):
        # Official Tasmota, put on by a run stopped before it gave it the S60's
        # template: any Tasmota could look like it.
        fake = FakeTasmota(version="15.6.0(release-tasmota32)", table=TASMOTA_TABLE, module="Generic")
        neighbours = {"192.168.0.37": "12:34:56:ab:cd:ef", "192.168.0.99": "aa:bb:cc:dd:ee:ff"}
        with mock.patch.object(detect, "Device", fake), \
                mock.patch.object(detect.hostsys, "sweep"), \
                mock.patch.object(detect.hostsys, "neighbour_table", return_value=neighbours), \
                mock.patch.object(detect, "_port_open", return_value=True), \
                mock.patch.object(detect.esphome, "is_esphome", return_value=False):
            found = detect.converted_s60s("192.168.0.1", known_macs={"12:34:56:ab:cd:ef"})
        self.assertEqual([(f.host, f.firmware) for f in found], [("192.168.0.37", "midway")])


class InstallerTests(unittest.TestCase):
    """This tool's installer, left by a run part way from Sonoff's firmware,
    answers with its own page: a run carries on from it, not from eWeLink."""

    def test_its_page_names_it(self):
        with mock.patch.object(detect.esphome, "is_esphome", return_value=False), \
             mock.patch.object(detect, "_port_open", return_value=True), \
             mock.patch.object(detect, "_is_installer", return_value=True):
            self.assertEqual(detect.identify("192.168.0.37"), "installer")

    def test_found_on_the_network_it_is_offered(self):
        neighbours = {"192.168.0.37": "12:34:56:ab:cd:ef"}
        with mock.patch.object(detect.hostsys, "sweep"), \
             mock.patch.object(detect.hostsys, "neighbour_table", return_value=neighbours), \
             mock.patch.object(detect, "_port_open", return_value=True), \
             mock.patch.object(detect.esphome, "is_esphome", return_value=False), \
             mock.patch.object(detect, "_is_installer", return_value=True):
            found = detect.converted_s60s("192.168.0.1")
        self.assertEqual([(f.host, f.firmware) for f in found], [("192.168.0.37", "installer")])


if __name__ == "__main__":
    unittest.main()
