import base64
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tools.autoflash import hostsys, redirect_test


class FirewallCommandTests(unittest.TestCase):
    """The add and remove commands must undo each other exactly."""

    def test_ufw_rule_admits_only_the_plug_on_both_ports(self):
        add, remove = hostsys.firewall_commands("ufw", "192.0.2.50", [8088, 8089])
        self.assertEqual(add, ["ufw", "allow", "from", "192.0.2.50", "to", "any", "port",
                               "8088,8089", "proto", "tcp", "comment", "Autoflash"])
        self.assertEqual(remove, ["ufw", "delete", "allow", "from", "192.0.2.50", "to", "any",
                                  "port", "8088,8089", "proto", "tcp"])

    def test_firewalld_rule_is_runtime_only(self):
        add, remove = hostsys.firewall_commands("firewalld", "192.0.2.50", [8088, 8089])
        self.assertNotIn("--permanent", add)
        self.assertEqual(add[1].replace("--add-", "--remove-"), remove[1])
        self.assertIn("source address=192.0.2.50", add[1])

    def test_windows_rule_is_named_per_plug_and_removed_by_name(self):
        add, remove = hostsys.firewall_commands("windows", "192.0.2.50", [8088, 8089])
        self.assertIn("name=Autoflash 192.0.2.50", add)
        self.assertIn("remoteip=192.0.2.50", add)
        self.assertIn("localport=8088,8089", add)
        self.assertEqual(remove[-1], "name=Autoflash 192.0.2.50")

    def test_a_windows_rule_is_removed_by_the_name_it_was_added_under(self):
        # One a run before 2.0.0 added has no name with it: it was called that.
        for detail, name in (({"name": "Autoflash"}, "Autoflash"), ({}, "S60 autoflash")):
            rule = hostsys.FirewallRule("windows", "192.0.2.50", [8088],
                                        {"program": r"C:\Python\python.exe", **detail})
            with mock.patch.object(hostsys, "IS_WINDOWS", True):
                self.assertIn(f'"name={name} 192.0.2.50"', hostsys.undo_commands(rule)[0])
            with mock.patch.object(hostsys, "_elevated_powershell", return_value=0) as ran:
                self.assertTrue(hostsys.close_firewall(rule))
            self.assertIn(f"'name={name} 192.0.2.50'", ran.call_args[0][0])

    def test_a_rule_that_was_already_there_is_left_alone(self):
        rule = hostsys.FirewallRule("macos", "192.0.2.50", [8088], {"preexisting": True})
        with mock.patch.object(hostsys, "elevated") as elevated:
            self.assertTrue(hostsys.close_firewall(rule))
        elevated.assert_not_called()

    def test_a_rule_survives_the_run_state_round_trip(self):
        rule = hostsys.FirewallRule("ufw", "192.0.2.50", [8088, 8089])
        self.assertEqual(hostsys.FirewallRule.from_json(rule.to_json()), rule)

    def test_no_firewall_means_no_rule(self):
        with mock.patch.object(hostsys, "active_firewall", return_value=None), \
             mock.patch.object(hostsys, "elevated") as elevated:
            self.assertIsNone(hostsys.open_firewall("192.0.2.50", [8088]))
        elevated.assert_not_called()

    def test_a_refused_elevation_is_an_error(self):
        with mock.patch.object(hostsys, "active_firewall", return_value="ufw"), \
             mock.patch.object(hostsys, "elevated_output", return_value=(1, "")):
            with self.assertRaises(OSError):
                hostsys.open_firewall("192.0.2.50", [8088])

    def test_a_rule_that_already_existed_is_not_removed_afterwards(self):
        for kind, reply in (("ufw", "Skipping adding existing rule\n"),
                            ("firewalld", "Warning: ALREADY_ENABLED: rule\nsuccess\n")):
            with mock.patch.object(hostsys, "active_firewall", return_value=kind), \
                 mock.patch.object(hostsys, "elevated_output", return_value=(0, reply)), \
                 mock.patch.object(hostsys, "elevated") as elevated:
                rule = hostsys.open_firewall("192.0.2.50", [8088])
                self.assertTrue(rule.detail.get("preexisting"))
                self.assertTrue(hostsys.close_firewall(rule))
            elevated.assert_not_called()

    def test_one_an_earlier_run_left_is_known_by_its_comment_and_removed(self):
        # Seen in the fault suite: a run stopped with Ctrl-C as ufw added the
        # rule, before it was recorded; the next took it for the computer's own.
        status = ("Status: active\n\nTo                         Action      From\n"
                  "8088/tcp                   ALLOW       192.0.2.50               # {}\n")
        # Its comment now, and before 2.0.0.
        for comment in ("Autoflash", "S60 autoflash"):
            with mock.patch.object(hostsys, "active_firewall", return_value="ufw"), \
                 mock.patch.object(hostsys, "elevated_output",
                                   side_effect=[(0, "Skipping adding existing rule\n"),
                                                (0, status.format(comment))]), \
                 mock.patch.object(hostsys, "elevated", return_value=0) as elevated:
                rule = hostsys.open_firewall("192.0.2.50", [8088])
                self.assertFalse(rule.detail.get("preexisting"), comment)
                self.assertTrue(rule.detail.get("left_by_earlier_run"), comment)
                self.assertTrue(hostsys.close_firewall(rule))
            elevated.assert_called_once()

    def test_one_without_the_comment_is_the_computers_own(self):
        status = "Status: active\n\n8088/tcp                   ALLOW       192.0.2.50\n"
        with mock.patch.object(hostsys, "active_firewall", return_value="ufw"), \
             mock.patch.object(hostsys, "elevated_output",
                               side_effect=[(0, "Skipping adding existing rule\n"), (0, status)]):
            rule = hostsys.open_firewall("192.0.2.50", [8088])
        self.assertTrue(rule.detail.get("preexisting"))

    def test_a_rule_this_run_added_is_removed_afterwards(self):
        with mock.patch.object(hostsys, "active_firewall", return_value="ufw"), \
             mock.patch.object(hostsys, "elevated_output", return_value=(0, "Rule added\n")), \
             mock.patch.object(hostsys, "elevated", return_value=0) as elevated:
            rule = hostsys.open_firewall("192.0.2.50", [8088])
            self.assertTrue(hostsys.close_firewall(rule))
        elevated.assert_called_once()


class WindowsFirewallTests(unittest.TestCase):
    """Windows' firewall read as objects, whatever language Windows is in."""

    STATE = {
        "profiles": [
            {"name": "Domain", "enabled": True, "local_rules": True},
            {"name": "Private", "enabled": True, "local_rules": True},
            {"name": "Public", "enabled": True, "local_rules": False},
        ],
        "networks": [{"interface": "Wi-Fi", "category": "Private"}],
        "others": ["Windows Defender Firewall", "Norton Security"],
        "domain": True,
    }

    def read(self, state, interface="Wi-Fi"):
        hostsys._firewall_cache.clear()
        reply = mock.Mock(returncode=0, stdout=json.dumps(state))
        with mock.patch.object(hostsys, "powershell", return_value=reply), \
                mock.patch.object(hostsys, "interface_towards", return_value=interface):
            return hostsys.windows_firewall("192.0.2.50")

    def test_the_plugs_network_decides_which_profile_counts(self):
        state = self.read(self.STATE)
        self.assertTrue(state["enabled"])
        self.assertTrue(state["local_rules"])          # Private takes local rules...
        self.assertEqual(state["others"], ["Norton Security"])  # ...and Windows' own is not "other"
        public = dict(self.STATE, networks=[{"interface": "Wi-Fi", "category": "Public"}])
        self.assertFalse(self.read(public)["local_rules"])     # Public is set to ignore them

    def test_an_unreadable_state_counts_as_on(self):
        hostsys._firewall_cache.clear()
        with mock.patch.object(hostsys, "powershell", return_value=None):
            self.assertIsNone(hostsys.windows_firewall()["enabled"])
            with mock.patch.object(hostsys, "IS_WINDOWS", True):
                self.assertEqual(hostsys.active_firewall(), "windows")

    def test_advice_names_what_the_tool_cannot_change(self):
        public = dict(self.STATE, networks=[{"interface": "Wi-Fi", "category": "Public"}])
        self.read(public)
        with mock.patch.object(hostsys, "IS_WINDOWS", True), \
                mock.patch.object(hostsys, "interface_towards", return_value="Wi-Fi"):
            text = " ".join(hostsys.firewall_advice("192.0.2.50", [8088, 8089]))
        self.assertIn("Norton Security", text)
        self.assertIn("8088,8089", text)
        self.assertIn("Group Policy", text)

    def test_being_on_a_domain_is_only_said_after_a_failure(self):
        self.read(self.STATE)
        with mock.patch.object(hostsys, "IS_WINDOWS", True), \
                mock.patch.object(hostsys, "interface_towards", return_value="Wi-Fi"):
            before = " ".join(hostsys.firewall_advice("192.0.2.50", [8088]))
            after = " ".join(hostsys.firewall_advice("192.0.2.50", [8088], after_failure=True))
        self.assertNotIn("domain", before)
        self.assertIn("domain", after)

    def test_the_rule_names_python_so_windows_security_does_not_ask(self):
        add, remove = hostsys.firewall_commands("windows", "192.0.2.50", [8088], r"C:\Program Files\Python\python.exe")
        self.assertIn(r"program=C:\Program Files\Python\python.exe", add)
        self.assertNotIn("program", " ".join(remove))  # removed by name

    def test_paused_block_rules_are_put_back(self):
        rule = hostsys.FirewallRule("windows", "192.0.2.50", [8088],
                                    {"program": r"C:\Python\python.exe", "paused": ["{abc}"]})
        with mock.patch.object(hostsys, "IS_WINDOWS", True):
            undo = hostsys.undo_commands(rule)
        self.assertEqual(undo[1], "Enable-NetFirewallRule -Name '{abc}'")
        with mock.patch.object(hostsys, "_elevated_powershell", return_value=0) as ran:
            self.assertTrue(hostsys.close_firewall(rule))
        script = ran.call_args[0][0]
        self.assertIn("'name=S60 autoflash 192.0.2.50'", script)  # added before 2.0.0
        self.assertIn("@('{abc}')", script)

    def test_windows_commands_are_shown_with_double_quotes(self):
        with mock.patch.object(hostsys, "IS_WINDOWS", True):
            shown = hostsys.display(["netsh", "name=Autoflash 1.2.3.4", r"program=C:\Python\python.exe"])
        self.assertEqual(shown, r'netsh "name=Autoflash 1.2.3.4" program=C:\Python\python.exe')


class WindowsRootCertificateTests(unittest.TestCase):
    """A root Windows has not fetched yet: Windows Update's list of the roots
    Windows trusts is read, and the request tried once more."""

    URL = "https://eu-apia.coolkit.cc/v2/user/login"

    def setUp(self):
        hostsys._windows_update_tried = False
        hostsys._roots_context = None
        self.addCleanup(setattr, hostsys, "_roots_context", None)
        self.addCleanup(setattr, hostsys, "_windows_update_tried", False)
        self.untrusted = hostsys.urllib.error.URLError(
            hostsys.ssl.SSLCertVerificationError("certificate verify failed"))

    def open(self, outcomes, windows=True, roots=b"root one", listed=True):
        request = hostsys.urllib.request.Request(self.URL)
        stdout = (base64.b64encode(roots).decode() + "\r\n") if listed else ""
        listing = subprocess.CompletedProcess([], 0 if listed else 1, stdout, "")
        with mock.patch.object(hostsys, "IS_WINDOWS", windows), \
                mock.patch.object(hostsys, "powershell", return_value=listing) as asked, \
                mock.patch.object(hostsys.ssl, "create_default_context") as contexts, \
                mock.patch.object(hostsys.urllib.request, "urlopen", side_effect=outcomes) as opened:
            try:
                return hostsys.urlopen(request, timeout=5), asked, opened, contexts
            except hostsys.urllib.error.URLError as exc:
                return exc, asked, opened, contexts

    def test_windows_update_s_roots_are_read_and_the_request_tried_again(self):
        result, asked, opened, contexts = self.open([self.untrusted, "response"], roots=b"\x30\x82 der")
        self.assertEqual(result, "response")
        self.assertEqual(opened.call_count, 2)
        self.assertIn("certutil -generateSSTFromWU", asked.call_args[0][0])
        contexts.return_value.load_verify_locations.assert_called_once_with(cadata=b"\x30\x82 der")
        self.assertIs(opened.call_args.kwargs.get("context"), contexts.return_value)
        _, _, opened, _ = self.open(["next file"])
        self.assertIs(opened.call_args.kwargs.get("context"), contexts.return_value)  # and for what follows

    def test_still_untrusted_after_that_is_reported(self):
        result, _, opened, _ = self.open([self.untrusted, self.untrusted])
        self.assertIs(result, self.untrusted)
        self.assertEqual(opened.call_count, 2)
        result, asked, opened, _ = self.open([self.untrusted])  # read once a run: not again
        self.assertIs(result, self.untrusted)
        asked.assert_not_called()

    def test_without_the_list_the_failure_stands(self):
        result, _, opened, _ = self.open([self.untrusted], listed=False)
        self.assertIs(result, self.untrusted)
        self.assertEqual(opened.call_count, 1)

    def test_a_connection_of_its_own_gets_the_same_second_try(self):
        # The eWeLink websocket wraps its own socket: no URLError around the failure.
        tried = []

        def connect(context):
            tried.append(context)
            if len(tried) == 1:
                raise hostsys.ssl.SSLCertVerificationError("certificate verify failed")
            return "socket"
        listing = subprocess.CompletedProcess([], 0, base64.b64encode(b"root").decode(), "")
        with mock.patch.object(hostsys, "IS_WINDOWS", True), \
                mock.patch.object(hostsys, "powershell", return_value=listing) as asked, \
                mock.patch.object(hostsys.ssl, "create_default_context"):
            self.assertEqual(hostsys.with_trusted_roots(connect), "socket")
        self.assertEqual(len(tried), 2)
        asked.assert_called_once()

    def test_only_windows_is_asked(self):
        result, asked, opened, _ = self.open([self.untrusted], windows=False)
        self.assertIs(result, self.untrusted)
        asked.assert_not_called()
        self.assertEqual(opened.call_count, 1)

    def test_other_failures_are_not_retried(self):
        offline = hostsys.urllib.error.URLError(OSError("Network is unreachable"))
        result, asked, _, _ = self.open([offline])
        self.assertIs(result, offline)
        asked.assert_not_called()


class MacFirewallTests(unittest.TestCase):
    """The macOS firewall allows programs: the one that really listens."""

    APP = "/Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python"

    def test_the_rule_names_the_running_program_and_removes_that_one(self):
        with mock.patch.object(hostsys, "active_firewall", return_value="macos"), \
                mock.patch.object(hostsys, "python_program", return_value=self.APP), \
                mock.patch.object(hostsys, "mac_app_already_allowed", return_value=False), \
                mock.patch.object(hostsys, "elevated_output", return_value=(0, "")) as added:
            rule = hostsys.open_firewall("192.0.2.50", [8088])
        self.assertEqual(added.call_args[0][0][1:], ["--add", self.APP, "--unblockapp", self.APP])
        self.assertEqual(rule.detail["program"], self.APP)
        # Removed by what was added, even if this Python would now say otherwise.
        with mock.patch.object(hostsys, "python_program", return_value="/somewhere/else"), \
                mock.patch.object(hostsys, "elevated", return_value=0) as removed:
            self.assertTrue(hostsys.close_firewall(hostsys.FirewallRule.from_json(rule.to_json())))
        self.assertEqual(removed.call_args[0][0][1:], ["--remove", self.APP])

    def test_an_already_allowed_program_is_left_alone(self):
        with mock.patch.object(hostsys, "active_firewall", return_value="macos"), \
                mock.patch.object(hostsys, "python_program", return_value=self.APP), \
                mock.patch.object(hostsys, "mac_app_already_allowed", return_value=True) as checked, \
                mock.patch.object(hostsys, "elevated_output") as added:
            rule = hostsys.open_firewall("192.0.2.50", [8088])
        checked.assert_called_once_with(self.APP)
        added.assert_not_called()
        self.assertTrue(rule.detail["preexisting"])


class CommandOutputTests(unittest.TestCase):
    def test_output_that_does_not_decode_is_not_fatal(self):
        # A German netsh's umlaut, say, in a code page Python did not expect.
        completed = hostsys._run([sys.executable, "-c",
                                  "import sys; sys.stdout.buffer.write(bytes(range(128, 256)))"])
        self.assertIsNotNone(completed)
        self.assertEqual(completed.returncode, 0)


class PythonScriptTests(unittest.TestCase):
    """A helper script imports the tools beside it, even on a Python that does
    not put the script's folder on sys.path - python.org's portable one, which
    start.cmd fetches.  -I leaves it off the same way."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        (self.folder / "beside.py").write_text("WORD = 'found'\n")
        self.script = self.folder / "helper.py"
        self.script.write_text(
            "import sys\n"
            "from beside import WORD\n"
            "if __name__ == '__main__':\n"
            "    print(WORD, *sys.argv[1:])\n"
            "    sys.exit(3)\n")

    def run_isolated(self, command):
        return subprocess.run([command[0], "-I", *command[1:]], capture_output=True, text=True,
                              cwd=self.folder.parent)

    def test_without_it_the_script_cannot_import_its_neighbours(self):
        completed = self.run_isolated([sys.executable, str(self.script)])
        self.assertIn("No module named 'beside'", completed.stderr)

    def test_the_script_runs_as_main_with_its_arguments_and_exit_code(self):
        completed = self.run_isolated(hostsys.python_script(self.script, "--email", "a b", unbuffered=True))
        self.assertEqual(completed.stdout, "found --email a b\n", completed.stderr)
        self.assertEqual(completed.returncode, 3)


class WindowsElevationTests(unittest.TestCase):
    def test_an_argument_with_spaces_stays_one_argument(self):
        add, _ = hostsys.firewall_commands("windows", "192.0.2.50", [8088, 8089])
        with mock.patch.object(hostsys, "IS_WINDOWS", True), \
                mock.patch.object(hostsys.subprocess, "run",
                                  return_value=mock.Mock(returncode=0, stdout="")) as run:
            self.assertEqual(hostsys.elevated(add), 0)
        script = run.call_args[0][0][-1]
        # Start-Process given a list joins it unquoted; one quoted line keeps the name whole.
        self.assertIn("-ArgumentList 'advfirewall firewall add rule \"name=Autoflash 192.0.2.50\" "
                      "dir=in action=allow", script)

    @unittest.skipUnless(hostsys.IS_WINDOWS, "runs PowerShell")
    def test_a_failed_start_is_not_success(self):
        # What a cancelled elevation prompt does: Start-Process fails, no process.
        real = hostsys.subprocess.run

        def without_elevation(command, **kwargs):
            return real([*command[:-1], command[-1].replace("-Verb RunAs ", "")], **kwargs)

        with mock.patch.object(hostsys.subprocess, "run", side_effect=without_elevation):
            self.assertNotEqual(hostsys.elevated(["no-such-program-autoflash.exe"]), 0)


class SystemCommandTests(unittest.TestCase):
    def test_ping_flags_follow_the_system(self):
        for flag, system in (("IS_WINDOWS", ["-n", "1", "-w", "2000"]),
                             ("IS_MAC", ["-c", "1", "-t", "2"])):
            with mock.patch.object(hostsys, "IS_WINDOWS", flag == "IS_WINDOWS"), \
                    mock.patch.object(hostsys, "IS_MAC", flag == "IS_MAC"):
                self.assertEqual(hostsys.ping_command("10.0.0.1", 2)[1:-1], system)
        with mock.patch.object(hostsys, "IS_WINDOWS", False), mock.patch.object(hostsys, "IS_MAC", False):
            self.assertEqual(hostsys.ping_command("10.0.0.1", 2)[1:-1], ["-c", "1", "-W", "2"])

    def test_alias_commands_undo_each_other(self):
        for flag in ("IS_WINDOWS", "IS_MAC", None):
            patches = [mock.patch.object(hostsys, name, name == flag) for name in ("IS_WINDOWS", "IS_MAC")]
            for patch in patches:
                patch.start()
            try:
                add, remove = hostsys.alias_commands("eth0", "192.0.2.50")
            finally:
                for patch in patches:
                    patch.stop()
            self.assertIn("192.0.2.50", " ".join(add))
            self.assertIn("192.0.2.50", " ".join(remove))
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

        with mock.patch.object(hostsys, "local_addresses", return_value={"192.0.2.20"}), \
             mock.patch.object(hostsys, "ping_ok", return_value=False):
            try:
                redirect_test.run(interface="eth0", plug_ip="192.0.2.50", own_ip="192.0.2.20",
                                  vendor_ip="203.0.113.10", vendor_port=8088, log=lambda _: None,
                                  elevated=elevated, connect=lambda src, *_: reaches[src])
                result = None
            except redirect_test.RedirectTestError as exc:
                result = exc
        return result, calls

    def test_a_working_rule_passes_and_the_address_is_returned(self):
        error, calls = self.run_test({"192.0.2.50": False, "192.0.2.20": True})
        self.assertIsNone(error)
        self.assertEqual(len(calls), 2, "borrow then give back")

    def test_a_missing_rule_fails_and_still_returns_the_address(self):
        error, calls = self.run_test({"192.0.2.50": True, "192.0.2.20": True})
        self.assertIn("not catching it", str(error))
        self.assertEqual(len(calls), 2)

    def test_no_internet_is_inconclusive_not_a_pass(self):
        error, _ = self.run_test({"192.0.2.50": False, "192.0.2.20": False})
        self.assertIn("cannot tell", str(error))

    def test_a_plug_still_powered_stops_the_test(self):
        with mock.patch.object(hostsys, "local_addresses", return_value=set()), \
             mock.patch.object(hostsys, "ping_ok", return_value=True):
            with self.assertRaisesRegex(redirect_test.RedirectTestError, "unplug"):
                redirect_test.run(interface="eth0", plug_ip="192.0.2.50", own_ip="192.0.2.20",
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


class SudoPromptTests(unittest.TestCase):
    """sudo's own question is counted on screen only when it is really asked."""

    def run_elevated(self, will_ask):
        from tools.autoflash import hostsys
        completed = mock.Mock(returncode=0, stdout="")
        with mock.patch.object(hostsys, "IS_WINDOWS", False), \
             mock.patch.object(hostsys, "sudo_will_ask", return_value=will_ask), \
             mock.patch.object(hostsys.subprocess, "run", return_value=completed) as run, \
             mock.patch.object(hostsys.getpass, "getuser", return_value="someone"), \
             mock.patch.object(hostsys.output, "before_input") as before, \
             mock.patch.object(hostsys.output, "after_input") as after:
            hostsys.elevated_output(["ufw", "status"])
        return run.call_args.args[0], before, after

    def test_a_remembered_password_puts_nothing_on_screen(self):
        command, before, after = self.run_elevated(False)
        before.assert_not_called()
        after.assert_not_called()
        self.assertEqual(command[:3], ["sudo", "-p", " [sudo] password for %u: "])  # at the margin

    def test_the_question_is_the_one_sudo_is_told_to_ask(self):
        _, before, after = self.run_elevated(True)
        before.assert_called_once_with(" [sudo] password for someone: ")
        after.assert_called_once()


class AdminPasswordTests(unittest.TestCase):
    """With a password_asker, sudo never writes to the terminal itself."""

    def run_with_asker(self, will_ask, password="hunter2"):
        from tools.autoflash import hostsys
        completed = mock.Mock(returncode=0, stdout="Rule added\n")
        asker = mock.Mock(return_value=password)
        with mock.patch.object(hostsys, "IS_WINDOWS", False), \
             mock.patch.object(hostsys, "password_asker", asker), \
             mock.patch.object(hostsys, "sudo_will_ask", return_value=will_ask), \
             mock.patch.object(hostsys.subprocess, "run", return_value=completed) as run, \
             mock.patch.object(hostsys.output, "before_input") as before:
            self.assertEqual(hostsys.elevated_output(["ufw", "status"]), (0, "Rule added\n"))
        before.assert_not_called()  # sudo's own question is never shown
        return asker, run.call_args

    def test_a_needed_password_goes_to_sudo_through_a_pipe(self):
        asker, call = self.run_with_asker(True)
        asker.assert_called_once()
        self.assertEqual(call.args[0], ["sudo", "-S", "-p", "", "ufw", "status"])
        self.assertEqual(call.kwargs["input"], "hunter2\n")
        self.assertNotIn("hunter2", " ".join(call.args[0]))  # never on a command line

    def test_a_remembered_password_is_not_asked_for(self):
        asker, call = self.run_with_asker(False)
        asker.assert_not_called()
        self.assertEqual(call.args[0][:2], ["sudo", "-n"])

    def test_sudo_says_whether_the_password_was_right(self):
        from tools.autoflash import hostsys
        with mock.patch.object(hostsys.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stderr="")):
            self.assertIsNone(hostsys.check_sudo_password("hunter2"))
        wrong = mock.Mock(returncode=1, stderr="Sorry, try again.\nsudo: 1 incorrect password attempt\n")
        with mock.patch.object(hostsys.subprocess, "run", return_value=wrong):
            self.assertEqual(hostsys.check_sudo_password("nope"), "wrong")
        refused = mock.Mock(returncode=1, stderr="someone is not in the sudoers file.\n")
        with mock.patch.object(hostsys.subprocess, "run", return_value=refused), \
             self.assertRaisesRegex(OSError, "not in the sudoers file"):
            hostsys.check_sudo_password("hunter2")

    def test_three_wrong_passwords_give_up(self):
        from tools.autoflash import hostsys, prompts
        with mock.patch.object(prompts, "ask_secret", return_value="nope"), \
             mock.patch.object(hostsys, "check_sudo_password", return_value="wrong") as check, \
             mock.patch.object(prompts.output, "show") as shown, \
             self.assertRaisesRegex(OSError, "not accepted 3 times"):
            prompts.ask_admin_password()
        self.assertEqual(check.call_count, 3)
        self.assertEqual(shown.call_count, 2)  # "try again" twice, not after the last


if __name__ == "__main__":
    unittest.main()


class LanAddressesTests(unittest.TestCase):
    """The networks discovery may sweep: Ethernet and Wi-Fi, a TAP adapter among
    them - the emulator's link is one - and never a VPN's tunnel."""

    def test_linux_keeps_ether_links_and_drops_tunnels_and_container_bridges(self):
        links = (
            "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN mode DEFAULT\\    link/loopback\n"
            "2: wlp1s0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue state UP\\    link/ether 12:34:56:ab:cd:ef brd ff:ff:ff:ff:ff:ff\n"
            "3: tap0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc fq_codel state UP\\    link/ether 12:34:56:ab:cd:01 brd ff:ff:ff:ff:ff:ff\n"
            "4: tailscale0: <POINTOPOINT,MULTICAST,NOARP,UP,LOWER_UP> mtu 1280 qdisc fq_codel state UNKNOWN\\    link/none \n"
            "5: wg0: <POINTOPOINT,NOARP,UP,LOWER_UP> mtu 1420 qdisc noqueue state UNKNOWN\\    link/none \n"
            "6: ppp0: <POINTOPOINT,MULTICAST,NOARP,UP,LOWER_UP> mtu 1492 qdisc fq_codel state UNKNOWN\\    link/ppp \n"
            "7: veth1a2b@if6: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue state UP\\    link/ether 12:34:56:ab:cd:02 brd ff:ff:ff:ff:ff:ff\n"
            "8: docker0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue state UP\\    link/ether 12:34:56:ab:cd:03 brd ff:ff:ff:ff:ff:ff\n"
            "9: br-26e078a930f7: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue state UP\\    link/ether 12:34:56:ab:cd:04 brd ff:ff:ff:ff:ff:ff\n"
            "10: virbr0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue state UP\\    link/ether 12:34:56:ab:cd:05 brd ff:ff:ff:ff:ff:ff\n"
            "11: br0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue state UP\\    link/ether 12:34:56:ab:cd:06 brd ff:ff:ff:ff:ff:ff\n")
        addresses = (
            "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever\n"
            "2: wlp1s0    inet 192.0.2.10/24 brd 192.0.2.255 scope global dynamic wlp1s0\\       valid_lft 80000sec\n"
            "3: tap0    inet 192.168.60.10/24 brd 192.168.60.255 scope global tap0\\       valid_lft forever\n"
            "4: tailscale0    inet 100.64.0.7/32 scope global tailscale0\\       valid_lft forever\n"
            "5: wg0    inet 10.8.0.2/24 scope global wg0\\       valid_lft forever\n"
            "6: ppp0    inet 10.64.64.1 peer 10.64.64.64/32 scope global ppp0\\       valid_lft forever\n"
            "8: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\\       valid_lft forever\n"
            "9: br-26e078a930f7    inet 172.19.0.1/16 brd 172.19.255.255 scope global br-26e078a930f7\\       valid_lft forever\n"
            "10: virbr0    inet 192.168.122.1/24 brd 192.168.122.255 scope global virbr0\\       valid_lft forever\n"
            "11: br0    inet 10.20.5.9/22 brd 10.20.7.255 scope global br0\\       valid_lft forever\n")
        # Docker's and libvirt's bridges are left out; a bridge of the person's own, br0, is kept.
        self.assertEqual(hostsys.lan_networks_linux(addresses, links),
                         {"192.0.2.10": 24, "192.168.60.10": 24, "10.20.5.9": 22})

    def test_mac_keeps_interfaces_with_a_hardware_address_not_point_to_point(self):
        ifconfig = (
            "lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384\n"
            "\tinet 127.0.0.1 netmask 0xff000000\n"
            "en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500\n"
            "\tether 12:34:56:ab:cd:ef\n"
            "\tinet 192.0.2.10 netmask 0xffffff00 broadcast 192.0.2.255\n"
            "bridge100: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500\n"
            "\tether 12:34:56:ab:cd:01\n"
            "\tinet 192.0.2.1 netmask 0xffffff00 broadcast 192.0.2.255\n"
            "utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280\n"
            "\tinet 100.64.0.7 --> 100.64.0.7 netmask 0xffffffff\n"
            "en5: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500\n"
            "\tether 12:34:56:ab:cd:02\n"
            "\tinet 10.0.0.5 netmask 0xffffffff broadcast 10.0.0.5\n"
            "en6: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500\n"
            "\tether 12:34:56:ab:cd:03\n"
            "\tinet 10.20.5.9 netmask 0xfffffc00 broadcast 10.20.7.255\n")
        self.assertEqual(hostsys.lan_networks_mac(ifconfig), {"192.0.2.10": 24, "192.0.2.1": 24, "10.20.5.9": 22})

    def test_windows_keeps_adapters_carrying_ethernet_the_tap_adapter_among_them(self):
        up, down = hostsys.IF_OPER_STATUS_UP, 2
        # As GetAdaptersAddresses gave them on the emulator's Windows, and the tunnels it may meet.
        rows = [("192.168.60.10", 24, 53, up, 6),    # the emulator's TAP-Windows: "virtual", with a MAC
                ("192.0.2.10", 24, 71, up, 6),     # Wi-Fi
                ("10.0.2.15", 24, 6, up, 6),         # Ethernet
                ("10.8.0.2", 24, 53, up, 0),         # Wintun (WireGuard): "virtual", no MAC
                ("100.64.0.7", 32, 6, up, 6),        # one address: a tunnel, whatever it says
                ("10.9.0.2", 24, 23, up, 6),         # PPP
                ("10.10.0.2", 24, 131, up, 6),       # a tunnel
                ("169.254.148.5", 16, 53, down, 6),  # an unused TAP adapter
                ("127.0.0.1", 8, 24, up, 0)]         # the loopback
        self.assertEqual(hostsys.lan_networks_windows(rows),
                         {"192.168.60.10": 24, "192.0.2.10": 24, "10.0.2.15": 24})

    def test_it_reads_this_systems_own_tool(self):
        for flag, expected in (("IS_LINUX", ["ip", "-o", "-4", "address"]), ("IS_MAC", ["ifconfig"])):
            with self.subTest(system=flag), \
                    mock.patch.object(hostsys, "IS_LINUX", flag == "IS_LINUX"), \
                    mock.patch.object(hostsys, "IS_MAC", flag == "IS_MAC"), \
                    mock.patch.object(hostsys, "IS_WINDOWS", False), \
                    mock.patch.object(hostsys, "_run", return_value=mock.Mock(returncode=1, stdout="")) as run:
                self.assertEqual(hostsys.lan_networks(), {})   # a tool that fails: nothing beyond the default
            self.assertEqual(run.call_args_list[0].args[0], expected)


class NetworkOfTests(unittest.TestCase):
    """A sweep covers the network as this computer has it, no wider than a /22."""

    def width(self, address, lan):
        network = hostsys.network_of(address, lan)
        return str(network), network.num_addresses - 2

    def test_a_network_is_swept_at_its_real_size(self):
        self.assertEqual(self.width("10.20.5.9", {"10.20.5.9": 22}), ("10.20.4.0/22", 1022))
        self.assertEqual(self.width("10.20.5.9", {"10.20.5.9": 23}), ("10.20.4.0/23", 510))
        self.assertEqual(self.width("192.0.2.70", {"192.0.2.70": 26}), ("192.0.2.64/26", 62))

    def test_a_wider_one_only_across_the_widest_around_the_address(self):
        self.assertEqual(self.width("10.20.5.9", {"10.20.5.9": 16}), ("10.20.4.0/22", 1022))

    def test_an_address_on_another_of_its_networks_takes_that_ones_size(self):
        # The plug's address, on the computer's /23: the plug's half of it, not a /24.
        self.assertEqual(self.width("10.20.4.200", {"10.20.5.9": 23, "192.0.2.10": 24}), ("10.20.4.0/23", 510))

    def test_a_network_this_computer_does_not_know_is_a_24(self):
        self.assertEqual(self.width("10.0.0.2", {}), ("10.0.0.0/24", 254))
        self.assertEqual(self.width("10.0.0.2", {"192.0.2.10": 24}), ("10.0.0.0/24", 254))


class SweepTests(unittest.TestCase):
    def sweep(self, subnet_of, linux, lan=None):
        pinged, slept = [], []
        record = lambda host, _timeout: pinged.append(host)
        with mock.patch.object(hostsys, "IS_LINUX", linux), \
                mock.patch.object(hostsys, "lan_networks", return_value=dict(lan or {})), \
                mock.patch.object(hostsys, "neighbour_table", return_value={}), \
                mock.patch.object(hostsys, "_ping_quietly", side_effect=record), \
                mock.patch.object(hostsys, "ping_ok", side_effect=record), \
                mock.patch.object(hostsys.time, "sleep", side_effect=slept.append):
            seen = hostsys.sweep(subnet_of)
        return pinged, slept, seen

    def test_several_networks_are_pinged_once_each_address(self):
        for linux in (True, False):
            pinged, _, _ = self.sweep(["10.0.0.2", "10.0.0.9", "192.168.60.10"], linux)
            self.assertEqual(len(pinged), 2 * 254)
            self.assertEqual(len(set(pinged)), len(pinged))
            self.assertIn("192.168.60.50", pinged)

    def test_one_address_sweeps_its_network(self):
        pinged, _, _ = self.sweep("10.0.0.2", linux=True)
        self.assertEqual(len(pinged), 254)

    def test_a_wider_network_is_swept_at_its_size(self):
        pinged, _, _ = self.sweep("10.20.5.9", linux=True, lan={"10.20.5.9": 22})
        self.assertEqual(len(set(pinged)), 1022)
        self.assertIn("10.20.4.1", pinged)
        self.assertIn("10.20.7.254", pinged)

    def test_on_linux_it_waits_out_arps_three_asks(self):
        # Asks at 0, 1 and 2 s: the pings end at 1 s, the wait takes it past 2.
        _, slept, _ = self.sweep("10.0.0.2", linux=True)
        self.assertEqual(slept, [hostsys.ARP_TRIES_END - 1])
        self.assertGreater(hostsys.ARP_TRIES_END, 2)

    def test_what_was_seen_counts_though_the_table_dropped_it_since(self):
        # The kernel drops entries not renewed for 5 s once its table is full:
        # a plug seen early in a wide sweep is kept from the reads along the way.
        tables = [{"10.20.4.37": "12:34:56:ab:cd:ef"}, {"10.20.6.8": "12:34:56:ab:cd:01"}]
        reads = threading.Event()
        calls = []

        def table():
            calls.append(1)
            if len(calls) >= 2:
                reads.set()
            return dict(tables[min(len(calls), len(tables)) - 1])

        def ping(_host, _timeout):
            reads.wait(10)   # the sweep lasts until the watcher has read twice
        with mock.patch.object(hostsys, "IS_LINUX", True), \
                mock.patch.object(hostsys, "SWEEP_READ_EVERY", 0.001), \
                mock.patch.object(hostsys, "lan_networks", return_value={}), \
                mock.patch.object(hostsys, "neighbour_table", side_effect=table), \
                mock.patch.object(hostsys, "_ping_quietly", side_effect=ping), \
                mock.patch.object(hostsys.time, "sleep"):
            seen = hostsys.sweep("10.20.5.9")
        self.assertEqual(seen, {"10.20.4.37": "12:34:56:ab:cd:ef", "10.20.6.8": "12:34:56:ab:cd:01"})

    def test_no_more_pings_at_once_than_files_allow(self):
        resource = __import__("resource")
        for soft, workers in ((1024, 240), (1048576, 256), (256, 48), (80, 16), (resource.RLIM_INFINITY, 256)):
            with self.subTest(soft=soft), \
                    mock.patch.object(resource, "getrlimit", return_value=(soft, resource.RLIM_INFINITY)):
                self.assertEqual(hostsys._sweep_workers(), workers)


class FindByMacTests(unittest.TestCase):
    def test_a_plug_the_sweep_saw_is_found_though_the_table_dropped_it(self):
        with mock.patch.object(hostsys, "neighbour_table", return_value={}), \
                mock.patch.object(hostsys, "sweep", return_value={"10.20.4.37": "12:34:56:ab:cd:ef"}):
            self.assertEqual(hostsys.find_by_mac("12:34:56:ab:cd:ef", "10.20.5.9"), "10.20.4.37")

class NeighbourTableTests(unittest.TestCase):
    def test_windows_broadcast_and_multicast_entries_are_not_neighbours(self):
        arp = ("\r\nInterface: 192.168.60.10 --- 0x3\r\n"
               "  Internet Address      Physical Address      Type\r\n"
               "  192.168.60.50         12-34-56-ab-cd-ef     dynamic   \r\n"
               "  192.168.60.255        ff-ff-ff-ff-ff-ff     static    \r\n"
               "  224.0.0.22            01-00-5e-00-00-16     static    \r\n"
               "  239.255.255.250       01-00-5e-7f-ff-fa     static    \r\n"
               "  255.255.255.255       ff-ff-ff-ff-ff-ff     static    \r\n")
        with mock.patch.object(hostsys, "IS_LINUX", False), mock.patch.object(hostsys, "IS_WINDOWS", True), \
                mock.patch.object(hostsys, "_run", return_value=mock.Mock(returncode=0, stdout=arp)):
            self.assertEqual(hostsys.neighbour_table(), {"192.168.60.50": "12:34:56:ab:cd:ef"})

