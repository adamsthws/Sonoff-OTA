"""Stage 3: the S60s in the owner's eWeLink account, matched on the network."""

import types
import unittest
from unittest import mock

import tools.s60_autoflash as af

S60 = {"deviceid": "1001234abc", "name": "Kitchen", "productModel": "S60TPG", "online": True,
       "devicekey": "secret", "extra": {"model": "SN-ESP32C3-S60-01"},
       "params": {"staMac": "AA:BB:CC:12:34:56", "ssid": "Home-2G"}}
SWITCH = {"deviceid": "1000aaaaaa", "name": "Hall light", "productModel": "MINIR4", "online": True,
          "extra": {"model": "SN-ESP32-MINIR4"}, "params": {}}


class AccountPlugTests(unittest.TestCase):
    def test_an_s60_is_kept_without_its_key(self):
        plug = af.account_plug(S60)
        self.assertEqual((plug.device_id, plug.name, plug.model, plug.online, plug.mac, plug.ssid),
                         ("1001234abc", "Kitchen", "S60TPG", True, "aa:bb:cc:12:34:56", "Home-2G"))
        self.assertNotIn("secret", repr(plug))

    def test_other_sonoff_devices_are_not_offered(self):
        self.assertIsNone(af.account_plug(SWITCH))

    def test_the_firmware_family_alone_is_enough(self):
        self.assertIsNotNone(af.account_plug({**S60, "productModel": ""}))


class MatchTests(unittest.TestCase):
    plug = af.account_plug(S60)

    def test_matched_by_its_ewelink_id(self):
        [seen] = af.match_plugs([self.plug], {"1001234abc": "192.168.0.50"}, {})
        self.assertEqual((seen.ip, seen.answering), ("192.168.0.50", True))

    def test_another_device_answering_is_not_mistaken_for_it(self):
        [seen] = af.match_plugs([self.plug], {"1000aaaaaa": "192.168.0.60"}, {})
        self.assertEqual((seen.ip, seen.answering), (None, False))

    def test_seen_by_hardware_address_only_means_lan_control_is_off(self):
        [seen] = af.match_plugs([self.plug], {}, {"192.168.0.50": "aa:bb:cc:12:34:56"})
        self.assertEqual((seen.ip, seen.answering), ("192.168.0.50", False))
        self.assertIn("turn on LAN Control", af.why_not_found(seen))

    def test_offline_in_ewelink_says_so(self):
        [seen] = af.match_plugs([af.account_plug({**S60, "online": False})], {}, {})
        self.assertIn("offline", af.why_not_found(seen))

    def test_online_but_unseen_names_its_network(self):
        [seen] = af.match_plugs([self.plug], {}, {})
        self.assertIn("(it is on 'Home-2G')", af.why_not_found(seen))


class DescribeTests(unittest.TestCase):
    def test_a_name_that_is_the_model_is_not_repeated(self):
        self.assertEqual(af.describe_plug(af.account_plug({**S60, "name": "S60TPG"})), "'S60TPG'")
        self.assertEqual(af.describe_plug(af.account_plug(S60)), "'Kitchen' (S60TPG)")


class FindAccountPlugTests(unittest.TestCase):
    def ctx(self):
        return types.SimpleNamespace(email=lambda: "owner@example.com", forget_account=mock.Mock(),
                                     account_devices=mock.Mock(return_value=[S60]))

    def test_the_one_s60_answering_is_chosen_without_asking(self):
        sighting = af.Sighting(af.account_plug(S60), "192.168.0.50", True)
        with mock.patch.object(af, "sight_plugs", return_value=[sighting]), \
             mock.patch.object(af, "ask_choice") as asked, \
             mock.patch.object(af, "tell") as shown, mock.patch.object(af, "say"):
            self.assertEqual(af.find_account_plug(self.ctx(), [S60, SWITCH]), "192.168.0.50")
        asked.assert_not_called()
        shown.assert_not_called()  # the stage line completing is all the screen needs

    def test_looking_again_finds_it_once_it_answers(self):
        plug = af.account_plug(S60)
        with mock.patch.object(af, "sight_plugs", side_effect=[
                [af.Sighting(plug, "192.168.0.50", False)],
                [af.Sighting(plug, "192.168.0.50", True)]]), \
             mock.patch.object(af, "ask_choice", return_value=0), \
             mock.patch.object(af, "section") as shown, \
             mock.patch.object(af, "tell"), mock.patch.object(af, "say"):
            self.assertEqual(af.find_account_plug(self.ctx(), [S60]), "192.168.0.50")
        self.assertIn("turn on LAN Control", shown.call_args.args[1][0])

    def test_an_account_without_an_s60_says_what_it_has(self):
        ctx = self.ctx()
        with mock.patch.object(af, "ask_choice", return_value=2), \
             mock.patch.object(af, "ask_ipv4", return_value="192.168.0.50"), \
             mock.patch.object(af, "section") as shown:
            self.assertEqual(af.find_account_plug(ctx, [SWITCH]), "192.168.0.50")
        self.assertIn("No Sonoff S60 in the eWeLink account owner@example.com", shown.call_args.args[0])
        self.assertIn("- 'Hall light' (MINIR4)", shown.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
