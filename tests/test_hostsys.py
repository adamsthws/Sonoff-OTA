import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.autoflash import hostsys, redirect_test


class FirewallCommandTests(unittest.TestCase):
    """The add and remove commands must undo each other exactly."""

    def test_ufw_rule_admits_only_the_plug_on_both_ports(self):
        add, remove = hostsys.firewall_commands("ufw", "192.168.1.50", [8088, 8089])
        self.assertEqual(add, ["ufw", "allow", "from", "192.168.1.50", "to", "any", "port",
                               "8088,8089", "proto", "tcp", "comment", "S60 autoflash"])
        self.assertEqual(remove, ["ufw", "delete", "allow", "from", "192.168.1.50", "to", "any",
                                  "port", "8088,8089", "proto", "tcp"])

    def test_firewalld_rule_is_runtime_only(self):
        add, remove = hostsys.firewall_commands("firewalld", "192.168.1.50", [8088, 8089])
        self.assertNotIn("--permanent", add)
        self.assertEqual(add[1].replace("--add-", "--remove-"), remove[1])
        self.assertIn("source address=192.168.1.50", add[1])

    def test_windows_rule_is_named_per_plug_and_removed_by_name(self):
        add, remove = hostsys.firewall_commands("windows", "192.168.1.50", [8088, 8089])
        self.assertIn("name=S60 autoflash 192.168.1.50", add)
        self.assertIn("remoteip=192.168.1.50", add)
        self.assertIn("localport=8088,8089", add)
        self.assertEqual(remove[-1], "name=S60 autoflash 192.168.1.50")

    def test_a_rule_that_was_already_there_is_left_alone(self):
        rule = hostsys.FirewallRule("macos", "192.168.1.50", [8088], {"preexisting": True})
        with mock.patch.object(hostsys, "elevated") as elevated:
            self.assertTrue(hostsys.close_firewall(rule))
        elevated.assert_not_called()

    def test_a_rule_survives_the_run_state_round_trip(self):
        rule = hostsys.FirewallRule("ufw", "192.168.1.50", [8088, 8089])
        self.assertEqual(hostsys.FirewallRule.from_json(rule.to_json()), rule)

    def test_no_firewall_means_no_rule(self):
        with mock.patch.object(hostsys, "active_firewall", return_value=None), \
             mock.patch.object(hostsys, "elevated") as elevated:
            self.assertIsNone(hostsys.open_firewall("192.168.1.50", [8088]))
        elevated.assert_not_called()

    def test_a_refused_elevation_is_an_error(self):
        with mock.patch.object(hostsys, "active_firewall", return_value="ufw"), \
             mock.patch.object(hostsys, "elevated_output", return_value=(1, "")):
            with self.assertRaises(OSError):
                hostsys.open_firewall("192.168.1.50", [8088])

    def test_a_rule_that_already_existed_is_not_removed_afterwards(self):
        for kind, reply in (("ufw", "Skipping adding existing rule\n"),
                            ("firewalld", "Warning: ALREADY_ENABLED: rule\nsuccess\n")):
            with mock.patch.object(hostsys, "active_firewall", return_value=kind), \
                 mock.patch.object(hostsys, "elevated_output", return_value=(0, reply)), \
                 mock.patch.object(hostsys, "elevated") as elevated:
                rule = hostsys.open_firewall("192.168.1.50", [8088])
                self.assertTrue(rule.detail.get("preexisting"))
                self.assertTrue(hostsys.close_firewall(rule))
            elevated.assert_not_called()

    def test_a_rule_this_run_added_is_removed_afterwards(self):
        with mock.patch.object(hostsys, "active_firewall", return_value="ufw"), \
             mock.patch.object(hostsys, "elevated_output", return_value=(0, "Rule added\n")), \
             mock.patch.object(hostsys, "elevated", return_value=0) as elevated:
            rule = hostsys.open_firewall("192.168.1.50", [8088])
            self.assertTrue(hostsys.close_firewall(rule))
        elevated.assert_called_once()


class SystemCommandTests(unittest.TestCase):
    def test_ping_flags_follow_the_system(self):
        for flag, system in (("IS_WINDOWS", ["-n", "1", "-w", "2000"]),
                             ("IS_MAC", ["-c", "1", "-t", "2"])):
            with mock.patch.object(hostsys, flag, True):
                self.assertEqual(hostsys.ping_command("10.0.0.1", 2)[1:-1], system)
        with mock.patch.object(hostsys, "IS_WINDOWS", False), mock.patch.object(hostsys, "IS_MAC", False):
            self.assertEqual(hostsys.ping_command("10.0.0.1", 2)[1:-1], ["-c", "1", "-W", "2"])

    def test_alias_commands_undo_each_other(self):
        for flag in ("IS_WINDOWS", "IS_MAC", None):
            patches = [mock.patch.object(hostsys, name, name == flag) for name in ("IS_WINDOWS", "IS_MAC")]
            for patch in patches:
                patch.start()
            try:
                add, remove = hostsys.alias_commands("eth0", "192.168.1.50")
            finally:
                for patch in patches:
                    patch.stop()
            self.assertIn("192.168.1.50", " ".join(add))
            self.assertIn("192.168.1.50", " ".join(remove))
            self.assertNotEqual(add, remove)

    def test_mac_addresses_are_normalised(self):
        self.assertEqual(hostsys.normalise_mac("AA-BB-CC-7E-81-9F"), "aa:bb:cc:7e:81:9f")
        self.assertEqual(hostsys.normalise_mac("2:b1:c2:d:3e:4f"), "02:b1:c2:0d:3e:4f")

    def test_checksum_listing_is_verified_without_sha256sum(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.bin").write_bytes(b"abc")
            listing = root / "SUMS"
            listing.write_text(
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad  a.bin\n")
            self.assertEqual(hostsys.check_sha256sums(listing), [])
            (root / "a.bin").write_bytes(b"abd")
            self.assertEqual(hostsys.check_sha256sums(listing), ["a.bin: checksum differs"])


class RedirectTestTests(unittest.TestCase):
    """The router-rule test: borrow the plug's address, try both sources, give it back."""

    def run_test(self, reaches):
        calls = []

        def elevated(command):
            calls.append(command)
            return 0

        with mock.patch.object(hostsys, "local_addresses", return_value={"192.168.1.20"}), \
             mock.patch.object(hostsys, "ping_ok", return_value=False):
            try:
                redirect_test.run(interface="eth0", plug_ip="192.168.1.50", own_ip="192.168.1.20",
                                  vendor_ip="203.0.113.10", vendor_port=8088, log=lambda _: None,
                                  elevated=elevated, connect=lambda src, *_: reaches[src])
                result = None
            except redirect_test.RedirectTestError as exc:
                result = exc
        return result, calls

    def test_a_working_rule_passes_and_the_address_is_returned(self):
        error, calls = self.run_test({"192.168.1.50": False, "192.168.1.20": True})
        self.assertIsNone(error)
        self.assertEqual(len(calls), 2, "borrow then give back")

    def test_a_missing_rule_fails_and_still_returns_the_address(self):
        error, calls = self.run_test({"192.168.1.50": True, "192.168.1.20": True})
        self.assertIn("not catching it", str(error))
        self.assertEqual(len(calls), 2)

    def test_no_internet_is_inconclusive_not_a_pass(self):
        error, _ = self.run_test({"192.168.1.50": False, "192.168.1.20": False})
        self.assertIn("cannot tell", str(error))

    def test_a_plug_still_powered_stops_the_test(self):
        with mock.patch.object(hostsys, "local_addresses", return_value=set()), \
             mock.patch.object(hostsys, "ping_ok", return_value=True):
            with self.assertRaisesRegex(redirect_test.RedirectTestError, "unplug"):
                redirect_test.run(interface="eth0", plug_ip="192.168.1.50", own_ip="192.168.1.20",
                                  vendor_ip="203.0.113.10", vendor_port=8088,
                                  elevated=lambda _: self.fail("must not borrow the address"))



class PortTests(unittest.TestCase):
    """A port another program is listening on is skipped; the next free one is used."""

    def test_a_listening_port_is_in_use_and_the_next_one_is_chosen(self):
        import socket
        with socket.socket() as taken, socket.socket() as spare:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            spare.bind(("127.0.0.1", 0))
            busy, free = taken.getsockname()[1], spare.getsockname()[1]
            spare.close()
            self.assertFalse(hostsys.port_is_free("127.0.0.1", busy))
            self.assertTrue(hostsys.port_is_free("127.0.0.1", free))
            self.assertEqual(hostsys.first_free_port("127.0.0.1", [busy, free]), free)
            self.assertIsNone(hostsys.first_free_port("127.0.0.1", [busy]))


if __name__ == "__main__":
    unittest.main()
