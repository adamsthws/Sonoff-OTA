"""Finding the plug's own setup network, and naming it for a person to find."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tools.autoflash_cli as af
from tools.autoflash import prompts
from tools.autoflash.state import RunState


class SetupNetworkTestCase(unittest.TestCase):
    def ctx(self, mac="12:34:56:ab:cd:ef"):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        if mac:
            state.set_input("plug_mac", mac)
        return SimpleNamespace(state=state)


class NamesTests(SetupNetworkTestCase):
    def test_this_plugs_names_are_known_from_its_mac(self):
        # Tasmota's own name, and ESP-IDF's default - the hotspot's MAC, one more.
        self.assertEqual(af.setup_networks(self.ctx()), ("tasmota-ABCDEF-", "ESP_ABCDF0"))
        self.assertEqual(af.setup_network_hint(self.ctx()), "tasmota-ABCDEF-NNNN, or ESP_ABCDF0")
        # Every setup network of this project's firmware: one name, from the
        # last two bytes of the plug's own MAC.
        self.assertEqual(af.esphome_network_hint(self.ctx()), "Sonoff-Setup-CDEF")
        self.assertEqual(af.mover_network_hint(self.ctx()), "Sonoff-Setup-CDEF")
        self.assertEqual(af.our_setup_networks(self.ctx(), af.LEGACY_BRIDGE_PREFIX),
                         ("Sonoff-Setup-CDEF", "S60-OTA-Bridge"))

    def test_one_more_carries_into_the_next_byte(self):
        self.assertEqual(af.setup_networks(self.ctx("12-34-56-AB-FF-FF"))[1], "ESP_AC0000")

    def test_without_the_mac_any_tasmota_will_do(self):
        self.assertEqual(af.setup_networks(self.ctx(mac=None)), ("tasmota-",))
        self.assertEqual(af.setup_network_hint(self.ctx(mac=None)), "tasmota-XXXXXX-NNNN")
        self.assertEqual(af.esphome_network_hint(self.ctx(mac=None)), "Sonoff-Setup-XXXX")
        self.assertEqual(af.our_setup_networks(self.ctx(mac=None), af.LEGACY_BRIDGE_PREFIX),
                         ("Sonoff-Setup-", "S60-OTA-Bridge"))


class SearchTests(SetupNetworkTestCase):
    def find(self, visible, ctx):
        with mock.patch.object(prompts, "scan_wifi_ssids", return_value=visible):
            return prompts.find_ssid(af.setup_networks(ctx))

    def test_the_plugs_network_is_found_by_either_name(self):
        self.assertEqual(self.find(["Home", "tasmota-ABCDEF-1234"], self.ctx()), "tasmota-ABCDEF-1234")
        self.assertEqual(self.find(["Home", "ESP_ABCDF0"], self.ctx()), "ESP_ABCDF0")

    def test_a_neighbours_tasmota_or_esp_is_not(self):
        self.assertIsNone(self.find(["tasmota-112233-0001", "ESP_112234", "ESP_ABCDEF"], self.ctx()))

    def test_only_tasmotas_own_name_is_kept_as_the_plugs(self):
        ctx = self.ctx()
        af.note_setup_network(ctx, "ESP_ABCDF0")  # not what its router shows
        self.assertIsNone(ctx.state.input("device_name"))
        af.note_setup_network(ctx, "tasmota-ABCDEF-1234")
        self.assertEqual(ctx.state.input("device_name"), "tasmota-ABCDEF-1234")


class LocateTests(SetupNetworkTestCase):
    def test_a_plug_not_found_can_be_set_up_on_its_own_network_instead(self):
        ctx = self.ctx()
        ctx.state.set_input("plug_ip", "192.168.0.50")
        ctx.finder = SimpleNamespace(poll=lambda: None)
        ctx.set_plug_ip = lambda ip: ctx.state.set_input("plug_ip", ip)
        offered = []

        def choose(question, options, help_lines=()):
            offered.extend(options)
            return 2

        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(af.time, "sleep"), \
                mock.patch.object(af.time, "monotonic", side_effect=iter(range(0, 10_000, 100))), \
                mock.patch.object(prompts, "wait_for_host", return_value=False), \
                mock.patch.object(prompts, "ask_choice", side_effect=choose), \
                mock.patch.object(af, "configure_plug_wifi", return_value="192.168.0.77") as setup:
            self.assertEqual(af.locate_plug(ctx, setup=True), "192.168.0.77")
        setup.assert_called_once_with(ctx, None)
        self.assertIn("tasmota-ABCDEF-NNNN, or ESP_ABCDF0", offered[2])

    def test_without_it_only_waiting_or_an_address_is_offered(self):
        offered = []
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(prompts, "wait_for_host", return_value=False), \
                mock.patch.object(prompts, "ask_choice",
                                  side_effect=lambda q, options, help_lines=(): offered.extend(options) or 1), \
                mock.patch.object(prompts, "ask_ipv4", return_value="192.168.0.9"):
            self.assertEqual(prompts.wait_or_ask("192.168.0.50"), "192.168.0.9")
        self.assertEqual(len(offered), 2)


if __name__ == "__main__":
    unittest.main()
