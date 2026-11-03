"""Stage 3: the plugs in the owner's eWeLink account, matched on the network."""

import types
import unittest
import time
from unittest import mock
from tools.autoflash import account, output, prompts
from tools.ewelink import discover_ewelink

S60 = {"deviceid": "1001234abc", "name": "Kitchen", "productModel": "S60TPG", "online": True,
       "devicekey": "secret", "extra": {"model": "SN-ESP32C3-S60-01"},
       "params": {"staMac": "AA:BB:CC:12:34:56", "ssid": "Home-2G"}}
S61 = {"deviceid": "1001234abd", "name": "Desk", "productModel": "S61STPF", "online": True,
       "extra": {"model": "SN-BK7238-S61-01"}, "params": {"staMac": "12:34:56:AB:CD:EF"}}
SWITCH = {"deviceid": "1000aaaaaa", "name": "Hall light", "productModel": "MINIR4", "online": True,
          "extra": {"model": "SN-ESP32-MINIR4"}, "params": {}}


class AccountPlugTests(unittest.TestCase):
    def test_an_s60_is_kept_without_its_key(self):
        plug = account.account_plug(S60)
        self.assertEqual((plug.device_id, plug.name, plug.model, plug.online, plug.mac, plug.ssid, plug.kind),
                         ("1001234abc", "Kitchen", "S60TPG", True, "aa:bb:cc:12:34:56", "Home-2G", "sonoff-s60"))
        self.assertNotIn("secret", repr(plug))

    def test_other_sonoff_devices_are_not_offered(self):
        self.assertIsNone(account.account_plug(SWITCH))

    def test_the_firmware_family_alone_is_enough(self):
        self.assertIsNotNone(account.account_plug({**S60, "productModel": ""}))


class MatchTests(unittest.TestCase):
    plug = account.account_plug(S60)

    def test_matched_by_its_ewelink_id(self):
        [seen] = account.match_plugs([self.plug], {"1001234abc": "192.168.0.50"}, {})
        self.assertEqual((seen.ip, seen.answering), ("192.168.0.50", True))

    def test_another_device_answering_is_not_mistaken_for_it(self):
        [seen] = account.match_plugs([self.plug], {"1000aaaaaa": "192.168.0.60"}, {})
        self.assertEqual((seen.ip, seen.answering), (None, False))

    def test_seen_by_hardware_address_only_means_lan_control_is_off(self):
        [seen] = account.match_plugs([self.plug], {}, {"192.168.0.50": "aa:bb:cc:12:34:56"})
        self.assertEqual((seen.ip, seen.answering), ("192.168.0.50", False))
        self.assertIn("turn on LAN Control", account.why_not_found(seen))

    def test_an_s60_seen_by_hardware_address_alone_is_not_ready(self):
        [seen] = account.match_plugs([self.plug], {}, {"192.168.0.50": "aa:bb:cc:12:34:56"})
        self.assertFalse(account.ready(seen))

    def test_an_s61_needs_no_lan_control(self):
        # Its conversion goes through eWeLink alone: online there, and at the
        # hardware address the account gives it, it is found.
        [seen] = account.match_plugs([account.account_plug(S61)], {}, {"192.0.2.51": "12:34:56:ab:cd:ef"})
        self.assertTrue(account.ready(seen))

    def test_an_s61_offline_in_ewelink_is_not_ready(self):
        [seen] = account.match_plugs([account.account_plug({**S61, "online": False})], {},
                                     {"192.0.2.51": "12:34:56:ab:cd:ef"})
        self.assertFalse(account.ready(seen))

    def test_offline_in_ewelink_says_so(self):
        [seen] = account.match_plugs([account.account_plug({**S60, "online": False})], {}, {})
        self.assertIn("offline", account.why_not_found(seen))

    def test_answering_but_offline_in_ewelink_means_no_internet(self):
        offline = account.account_plug({**S60, "online": False})
        [seen] = account.match_plugs([offline], {"1001234abc": "192.168.0.50"}, {})
        self.assertTrue(account.cut_off(seen))
        self.assertIn("allow it internet access", account.why_not_found(seen))

    def test_online_but_unseen_names_its_network(self):
        [seen] = account.match_plugs([self.plug], {}, {})
        self.assertIn("(it is on 'Home-2G')", account.why_not_found(seen))


class TypedAddressTests(unittest.TestCase):
    """An address typed in is used only once the plug answers eWeLink's discovery there."""

    def found(self, ids):
        packets = [{"source": "192.168.0.50", "records": [{"type": "TXT", "value": [f"id={i}"]}]}
                   for i in ids]
        return mock.patch.object(discover_ewelink, "discover", return_value={"packets": packets})

    def test_the_accounts_s60_answering_will_do(self):
        with self.found(["1001234abc"]), mock.patch.object(output, "tell"):
            self.assertEqual(account.plug_at({"1001234abc"})("192.168.0.50"), [])

    def test_something_that_only_answers_a_ping_is_probably_the_router(self):
        with self.found([]), mock.patch.object(output, "tell"):
            problem = account.plug_at({"1001234abc"})("192.168.0.1")
        self.assertIn("not as an eWeLink device", problem[0])
        self.assertIn("such as your router", " ".join(problem))

    def test_another_ewelink_device_is_not_taken_for_the_plug(self):
        with self.found(["1000aaaaaa"]), mock.patch.object(output, "tell"):
            problem = account.plug_at({"1001234abc"})("192.168.0.60")
        self.assertEqual(problem, ["192.168.0.60 is a different eWeLink device, not one of this "
                                   "account's supported devices."])

    def test_without_an_account_any_ewelink_device_will_do(self):
        with self.found(["1000aaaaaa"]), mock.patch.object(output, "tell"):
            self.assertEqual(account.plug_at(None)("192.168.0.60"), [])


class DescribeTests(unittest.TestCase):
    def test_a_name_that_is_the_model_is_not_repeated(self):
        self.assertEqual(account.describe_plug(account.account_plug({**S60, "name": "S60TPG"})), "'S60TPG'")
        self.assertEqual(account.describe_plug(account.account_plug(S60)), "'Kitchen' (S60TPG)")


class FindAccountPlugTests(unittest.TestCase):
    def ctx(self):
        return types.SimpleNamespace(email=lambda: "owner@example.com", forget_account=mock.Mock(),
                                     account_devices=mock.Mock(return_value=[S60]))

    def test_the_one_s60_answering_is_chosen_without_asking(self):
        sighting = account.Sighting(account.account_plug(S60), "192.168.0.50", True)
        with mock.patch.object(account, "sight_plugs", return_value=[sighting]), \
             mock.patch.object(prompts, "ask_choice") as asked, \
             mock.patch.object(output, "tell") as shown, mock.patch.object(output, "say"):
            self.assertEqual(account.find_account_plug(self.ctx(), [S60, SWITCH]), "192.168.0.50")
        asked.assert_not_called()
        shown.assert_not_called()  # the stage line completing is all the screen needs

    def test_it_is_looked_for_again_by_itself_before_anyone_is_asked(self):
        # Just plugged in, or a busy Wi-Fi: it answers a moment later.
        plug = account.account_plug(S60)
        with mock.patch.object(account, "sight_plugs", side_effect=[
                [account.Sighting(plug, "192.168.0.50", False)],
                [account.Sighting(plug, "192.168.0.50", True)]]), \
             mock.patch.object(prompts, "ask_choice") as asked, mock.patch.object(time, "sleep"), \
             mock.patch.object(output, "tell"), mock.patch.object(output, "say"):
            self.assertEqual(account.find_account_plug(self.ctx(), [S60]), "192.168.0.50")
        asked.assert_not_called()

    def test_looking_again_finds_it_once_it_answers(self):
        plug = account.account_plug(S60)
        unseen = [account.Sighting(plug, "192.168.0.50", False)]
        with mock.patch.object(account, "sight_plugs", side_effect=[unseen] * (account.LOOK_AGAIN + 1) + [
                [account.Sighting(plug, "192.168.0.50", True)]]), \
             mock.patch.object(prompts, "ask_choice", return_value=0) as asked, mock.patch.object(time, "sleep"), \
             mock.patch.object(prompts, "section") as shown, \
             mock.patch.object(output, "tell"), mock.patch.object(output, "say"):
            self.assertEqual(account.find_account_plug(self.ctx(), [S60]), "192.168.0.50")
        asked.assert_called_once()   # only once it had been looked for again by itself
        self.assertIn("turn on LAN Control", shown.call_args.args[1][0])

    def clock(self):
        """A clock that moves on 10 s for every sleep, and no real waiting."""
        now = [0.0]
        return (mock.patch.object(time, "monotonic", side_effect=lambda: now[0]),
                mock.patch.object(time, "sleep", side_effect=lambda s: now.__setitem__(0, now[0] + s)))

    def test_a_plug_just_started_is_waited_for_until_ewelink_sees_it(self):
        # It answers here a few seconds before eWeLink shows it online.
        offline = {**S60, "online": False}
        ctx = self.ctx()
        ctx.account_devices = mock.Mock(side_effect=[[offline], [S60]])
        monotonic, sleep = self.clock()
        with mock.patch.object(account, "sight_plugs",
                               side_effect=lambda plugs: [account.Sighting(p, "192.168.0.50", True) for p in plugs]), \
             mock.patch.object(prompts, "ask_choice") as asked, monotonic, sleep, \
             mock.patch.object(output, "tell"), mock.patch.object(output, "say"):
            self.assertEqual(account.find_account_plug(ctx, [offline]), "192.168.0.50")
        asked.assert_not_called()

    def test_a_plug_just_switched_on_is_waited_for_before_asking(self):
        # Plugged in as the run began: neither answering here nor online yet.
        offline = {**S60, "online": False}
        ctx = self.ctx()
        ctx.account_devices = mock.Mock(side_effect=[[offline], [S60]])
        plug = account.account_plug(offline)
        monotonic, sleep = self.clock()
        with mock.patch.object(account, "sight_plugs", side_effect=[
                [account.Sighting(plug, None, False)],
                [account.Sighting(account.account_plug(S60), "192.168.0.50", True)]]), \
             mock.patch.object(prompts, "ask_choice") as asked, monotonic, sleep, \
             mock.patch.object(output, "tell"), mock.patch.object(output, "say"):
            self.assertEqual(account.find_account_plug(ctx, [offline]), "192.168.0.50")
        asked.assert_not_called()

    def test_a_plug_offline_for_a_minute_is_asked_about(self):
        offline = {**S60, "online": False}
        ctx = self.ctx()
        ctx.account_devices = mock.Mock(return_value=[offline])
        monotonic, sleep = self.clock()
        with mock.patch.object(account, "sight_plugs",
                               side_effect=lambda plugs: [account.Sighting(p, None, False) for p in plugs]), \
             mock.patch.object(prompts, "ask_choice", return_value=2), \
             mock.patch.object(prompts, "ask_ipv4", return_value="192.168.0.99"), \
             mock.patch.object(prompts, "section") as shown, monotonic, sleep, \
             mock.patch.object(output, "tell"), mock.patch.object(output, "say"):
            self.assertEqual(account.find_account_plug(ctx, [offline]), "192.168.0.99")
        self.assertIn("eWeLink shows it offline", shown.call_args.args[1][0])
        # Looked again every 10 s for the minute, then LOOK_AGAIN times more, before asking.
        self.assertEqual(ctx.account_devices.call_count, int(account.CLOUD_CATCH_UP // 10) + account.LOOK_AGAIN)

    def test_without_ewelink_a_plug_not_answering_is_looked_for_again_before_asking(self):
        with mock.patch.object(account, "find_ewelink_plugs", side_effect=[[], [], [("192.168.0.50", "S60")]]), \
             mock.patch.object(prompts, "ask_choice") as asked, mock.patch.object(time, "sleep"), \
             mock.patch.object(output, "say"):
            self.assertEqual(account.find_any_plug(), "192.168.0.50")
        asked.assert_not_called()
        with mock.patch.object(account, "find_ewelink_plugs", side_effect=[[]] * (account.LOOK_AGAIN + 1)), \
             mock.patch.object(prompts, "ask_choice", return_value=1) as asked, mock.patch.object(time, "sleep"), \
             mock.patch.object(prompts, "ask_ipv4", return_value="192.168.0.99"), \
             mock.patch.object(prompts, "section"), mock.patch.object(output, "say"):
            self.assertEqual(account.find_any_plug(), "192.168.0.99")
        asked.assert_called_once()

    def test_a_plug_without_internet_is_not_chosen(self):
        sighting = account.Sighting(account.account_plug({**S60, "online": False}), "192.168.0.50", True)
        monotonic, sleep = self.clock()
        with mock.patch.object(account, "sight_plugs", return_value=[sighting]), monotonic, sleep, \
             mock.patch.object(prompts, "ask_choice", return_value=2), \
             mock.patch.object(prompts, "ask_ipv4", return_value="192.168.0.99"), \
             mock.patch.object(prompts, "section") as shown, \
             mock.patch.object(output, "tell"), mock.patch.object(output, "say"):
            self.assertEqual(account.find_account_plug(self.ctx(), [S60]), "192.168.0.99")
        self.assertEqual("Your device is on this network but cannot reach the internet",
                         shown.call_args.args[0])
        self.assertIn("cannot reach the internet", shown.call_args.args[1][0])

    def test_an_account_without_an_s60_says_what_it_has(self):
        ctx = self.ctx()
        with mock.patch.object(prompts, "ask_choice", return_value=2), \
             mock.patch.object(prompts, "ask_ipv4", return_value="192.168.0.50"), \
             mock.patch.object(prompts, "section") as shown:
            self.assertEqual(account.find_account_plug(ctx, [SWITCH]), "192.168.0.50")
        self.assertIn("No supported device in the eWeLink account owner@example.com", shown.call_args.args[0])
        self.assertIn("- 'Hall light' (MINIR4)", shown.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
