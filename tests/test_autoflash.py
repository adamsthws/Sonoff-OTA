import contextlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.autoflash.phases import ARM_RE, LOADER_RE, PhaseRunner, PhaseError, short_loader
from tools.autoflash.state import ATTEMPTED, DONE, FAILED, RunState


def no_lock_records(root, name):
    """A lock lifted with nothing written where the runs that lift locks say
    so: this checkout's captures - the main checkout's, through a worktree's
    link - where a test's record would count as a real run's."""
    return contextlib.nullcontext()


class RunStateTests(unittest.TestCase):
    def test_a_moment_of_windows_refusal_is_waited_out(self):
        from tools.autoflash import hostsys
        real = hostsys.os.replace
        with tempfile.TemporaryDirectory() as directory:
            run = RunState.open(Path(directory))
            refusals = [PermissionError(5, "Access is denied")] * 2

            def flaky(source, target):
                if refusals:
                    raise refusals.pop()
                real(source, target)

            with mock.patch.object(hostsys.os, "replace", side_effect=flaky), \
                    mock.patch.object(hostsys, "IS_WINDOWS", True), \
                    mock.patch.object(hostsys.time, "sleep"):
                run.mark("E3", DONE)
            self.assertTrue(RunState.open(Path(directory)).is_done("E3"))
            # Elsewhere a refusal means what it says, at once.
            with mock.patch.object(hostsys.os, "replace", side_effect=PermissionError(13, "denied")), \
                    mock.patch.object(hostsys, "IS_WINDOWS", False), \
                    self.assertRaises(PermissionError):
                run.mark("E2", DONE)

    def test_state_survives_reload_and_records_steps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = RunState.open(root)
            state.set_input("plug_ip", "192.168.1.99")
            state.mark("A1", DONE, note="paired")
            state.capture("D1.upload_seconds", 41)

            reopened = RunState.open(root)
            self.assertEqual(reopened.input("plug_ip"), "192.168.1.99")
            self.assertTrue(reopened.is_done("A1"))
            self.assertEqual(reopened.data["captures"]["D1.upload_seconds"], 41)

    def test_require_reports_every_missing_input(self):
        with tempfile.TemporaryDirectory() as directory:
            state = RunState.open(Path(directory))
            state.set_input("plug_ip", "")
            with self.assertRaisesRegex(ValueError, "plug_ip.*listen_ip|listen_ip"):
                state.require("plug_ip", "listen_ip")

    def test_failed_step_is_not_skipped_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = RunState.open(root)
            state.mark("E1", FAILED, error="stage refused")
            self.assertFalse(RunState.open(root).is_done("E1"))


class LoaderTests(unittest.TestCase):
    URL = "http://192.168.1.141:8089/berry/commit.be"

    def test_short_loader_is_equivalent_and_much_shorter(self):
        printed = (
            "def s60_urlbeload(url) var wc=webclient() wc.begin(url) var st=wc.GET() "
            "if st!=200 wc.close() raise 'connection_error',format('status:%i',st) end "
            f"var code=wc.get_string() return compile(code)() end s60_commit=s60_urlbeload('{self.URL}')"
        )
        compact = short_loader(self.URL, assign_to="s60_commit")
        self.assertLess(len(compact), len(printed) / 2)
        for fragment in ("webclient()", self.URL, "compile(", "s60_commit="):
            self.assertIn(fragment, compact)

    def test_loader_url_and_arm_token_are_parsed_from_server_output(self):
        printed = f"return s60_urlbeload('{self.URL}')"
        self.assertEqual(LOADER_RE.search(printed).group("url"), self.URL)
        found = ARM_RE.search('s60_commit("d2_fjQTqh4KzfHDFSefx")')
        self.assertEqual(found.group("name"), "s60_commit")
        self.assertEqual(found.group("token"), "d2_fjQTqh4KzfHDFSefx")

    def test_armed_phase_refuses_to_run_without_a_confirmation_callback(self):
        runner = PhaseRunner(
            repo_root=Path("."),
            manifest=Path("m.json"),
            evidence_dir=Path("live"),
            listen_ip="192.168.1.141",
            device=None,  # never reached: the guard fires first
            log=lambda _: None,
        )
        for phase in ("commit", "restore"):
            with self.subTest(phase=phase):
                with self.assertRaisesRegex(PhaseError, "explicit confirmation"):
                    runner.run(phase)


if __name__ == "__main__":
    unittest.main()


class ResumeTests(unittest.TestCase):
    """The checklist is only useful if it behaves under interruption."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)

    def test_a_step_that_never_reported_is_re_run(self):
        state = RunState.open(self.root)
        state.mark("C2", "attempted")
        reopened = RunState.open(self.root)
        self.assertFalse(reopened.is_done("C2"))
        self.assertEqual(reopened.status("C2"), "attempted")

    def test_a_crash_before_any_mark_leaves_the_step_pending(self):
        state = RunState.open(self.root)
        state.mark("A1", DONE)
        # C2 was never reached at all
        self.assertEqual(RunState.open(self.root).status("C2"), "pending")

    def test_completed_and_failed_steps_are_distinguishable(self):
        state = RunState.open(self.root)
        state.mark("E1", DONE)
        state.mark("E2", FAILED, error="stage refused")
        reopened = RunState.open(self.root)
        self.assertTrue(reopened.is_done("E1"))
        self.assertFalse(reopened.is_done("E2"))
        self.assertEqual(reopened.data["steps"]["E2"]["error"], "stage refused")

    def test_migration_phases_resume_independently(self):
        import tools.autoflash_cli as autoflash

        ids = [step for step, _, _ in autoflash.STEPS]
        self.assertEqual(ids[ids.index("E1"):ids.index("E1") + 3], ["E1", "E2", "E3"])
        state = RunState.open(self.root)
        state.mark("E1", DONE)
        state.mark("E2", DONE)
        reopened = RunState.open(self.root)
        self.assertTrue(reopened.is_done("E2"))
        self.assertFalse(reopened.is_done("E3"))

    def test_every_destructive_step_records_an_attempt_first(self):
        import tools.autoflash_cli as autoflash

        # Every route's steps: STEPS holds only the route chosen for the run.
        known = {step for firmware in autoflash.FIRMWARES for source in autoflash.SOURCES
                 if firmware != source
                 for step, _, _ in autoflash.steps_for(firmware, source)}
        self.assertTrue(autoflash.DESTRUCTIVE_STEPS <= known)
        self.assertIn("E3", autoflash.DESTRUCTIVE_STEPS)
        self.assertIn("C2", autoflash.DESTRUCTIVE_STEPS)

    def test_state_file_is_replaced_atomically(self):
        state = RunState.open(self.root)
        state.mark("A1", DONE)
        leftovers = [f for f in self.root.iterdir() if f.name.startswith(".")]
        self.assertEqual(leftovers, [], "temporary files must not survive a write")


class NetworkCheckTests(unittest.TestCase):
    """The plug's live network must be the one the bundle's firmware will join."""

    def setUp(self):
        from unittest import mock
        import tools.autoflash_cli as autoflash

        self.autoflash = autoflash
        self.patches = [
            mock.patch.object(autoflash, "bundle_ssid", return_value="Home-2G"),
            mock.patch.object(autoflash, "say"),
            mock.patch.object(autoflash, "section"),
        ]
        for patch in self.patches:
            patch.start()
        self.addCleanup(lambda: [patch.stop() for patch in self.patches])

    def ctx(self, ssid, signal=-50):
        from types import SimpleNamespace
        wifi = {"SSId": ssid, "Signal": signal} if ssid else {}
        device = SimpleNamespace(command=lambda _: {"StatusSTS": {"Wifi": wifi}})
        state = SimpleNamespace(input=lambda key: None, set_input=lambda key, value: None)
        return SimpleNamespace(device=device, manifest_path=Path("unused"), state=state)

    def test_a_matching_network_passes(self):
        result = self.autoflash.check_plug_network(self.ctx("Home-2G"))
        self.assertEqual(result, {"network": "Home-2G", "signal_dbm": -50})

    def test_a_different_network_stops_when_not_rebuilt(self):
        from unittest import mock
        with mock.patch.object(self.autoflash, "ask_yes_no", return_value=False):
            with self.assertRaisesRegex(self.autoflash.Abort, "'Guest' but the bundle is set up for 'Home-2G'"):
                self.autoflash.check_plug_network(self.ctx("Guest"))

    def test_a_different_network_can_be_fixed_with_its_password(self):
        from unittest import mock
        ctx = self.ctx("Guest")
        ctx.adopt_wifi = mock.Mock()
        with mock.patch.object(self.autoflash, "ask_yes_no", return_value=True), \
             mock.patch.object(self.autoflash, "ask_password", return_value="guest pass") as asked, \
             mock.patch.object(self.autoflash, "bundle_ssid", side_effect=["Home-2G", "Guest"]):
            result = self.autoflash.check_plug_network(ctx)
        asked.assert_called_once_with("Guest")  # the name is known: only the password is asked
        ctx.adopt_wifi.assert_called_once_with("Guest", "guest pass")
        self.assertEqual(result["network"], "Guest")

    def test_names_differing_only_in_case_do_not_match(self):
        from unittest import mock
        with mock.patch.object(self.autoflash, "ask_yes_no", return_value=False):
            with self.assertRaises(self.autoflash.Abort):
                self.autoflash.check_plug_network(self.ctx("home-2g"))

    def test_no_reported_network_stops_rather_than_guessing(self):
        with self.assertRaisesRegex(self.autoflash.Abort, "did not say which Wi-Fi network"):
            self.autoflash.check_plug_network(self.ctx(None))

    def test_a_weak_signal_needs_a_deliberate_yes(self):
        from unittest import mock
        with mock.patch.object(self.autoflash, "ask_yes_no", return_value=False):
            with self.assertRaisesRegex(self.autoflash.Abort, "weak Wi-Fi signal"):
                self.autoflash.check_plug_network(self.ctx("Home-2G", signal=-82))
        with mock.patch.object(self.autoflash, "ask_yes_no", return_value=True):
            self.assertEqual(
                self.autoflash.check_plug_network(self.ctx("Home-2G", signal=-82))["signal_dbm"], -82
            )


class EsphomeSignalTests(unittest.TestCase):
    """On the way to ESPHome the signal is read too, off the Tasmota the plug
    still runs, before what then has to join the Wi-Fi on its own is written."""

    def setUp(self):
        from unittest import mock
        import tools.autoflash_cli as autoflash

        self.autoflash = autoflash
        self.run_plan = mock.Mock(return_value={"plan": "written"})
        self.consent = mock.Mock()
        for patch in (mock.patch.object(autoflash, "say"),
                      mock.patch.object(autoflash, "section"),
                      mock.patch.object(autoflash, "run_plan", self.run_plan),
                      mock.patch.object(autoflash, "consent_to_leave", self.consent),
                      mock.patch.object(autoflash, "wait_for_mover"),
                      mock.patch.object(autoflash.esphome, "is_esphome", return_value=False),
                      mock.patch.object(autoflash.esphome, "image_for", return_value=b"\0" * 64),
                      mock.patch.object(autoflash.esphome, "mover_for", return_value=b"\0" * 64),
                      mock.patch.object(autoflash.esphome, "sonoff_table", return_value=b""),
                      mock.patch.object(autoflash.esphome, "published_bootloader", return_value=b""),
                      mock.patch.object(autoflash.esphome_ota, "probe", return_value=None),
                      mock.patch.object(autoflash.plans, "esphome", return_value="plan"),
                      mock.patch.object(autoflash.plans, "leave_tasmota", return_value="plan")):
            patch.start()
            self.addCleanup(patch.stop)

    def ctx(self, status):
        from types import SimpleNamespace

        def command(_):
            if isinstance(status, Exception):
                raise status
            return status

        inputs = {"plug_ip": "192.168.60.50"}
        state = SimpleNamespace(input=inputs.get, set_input=inputs.__setitem__)
        return SimpleNamespace(state=state, wifi=lambda: ("Home-2G", "pass"), esphome_firmware=None,
                               dry_run=False, device=SimpleNamespace(command=command))

    def signal(self, dbm):
        return {"StatusSTS": {"Wifi": {"SSId": "Home-2G", "Signal": dbm}}}

    def test_the_signal_is_recorded_with_the_install(self):
        for step in (self.autoflash.step_esphome_install, self.autoflash.step_leave_tasmota):
            self.run_plan.reset_mock()
            self.assertEqual(step(self.ctx(self.signal(-58)))["signal_dbm"], -58, step.__name__)
            self.run_plan.assert_called_once()

    def test_a_weak_signal_stops_before_anything_is_written(self):
        from unittest import mock
        for step in (self.autoflash.step_esphome_install, self.autoflash.step_leave_tasmota):
            with mock.patch.object(self.autoflash, "ask_yes_no", return_value=False):
                with self.assertRaisesRegex(self.autoflash.Abort, "weak Wi-Fi signal"):
                    step(self.ctx(self.signal(-82)))
        self.run_plan.assert_not_called()
        self.consent.assert_not_called()  # asked first, before the go-ahead to leave Tasmota

    def test_a_plug_that_does_not_say_is_installed_all_the_same(self):
        from tools.autoflash.device import DeviceError
        for status in ({"StatusSTS": {}}, DeviceError("no answer")):
            self.run_plan.reset_mock()
            self.assertIsNone(self.autoflash.step_esphome_install(self.ctx(status))["signal_dbm"])
            self.run_plan.assert_called_once()

    def test_a_weak_signal_agreed_to_is_not_asked_about_again(self):
        from unittest import mock
        ctx = self.ctx(self.signal(-82))
        with mock.patch.object(self.autoflash, "ask_yes_no", return_value=True) as asked:
            self.autoflash.step_leave_tasmota(ctx)        # the earliest reading: asked
            self.autoflash.step_esphome_install(ctx)      # a later one: told, not asked
        asked.assert_called_once()
        self.assertEqual(self.run_plan.call_count, 2)


class AdvertSignalTests(unittest.TestCase):
    """From Sonoff's firmware the signal is read at A2, before anything is
    written, from the plug's own eWeLink advert, opened with its LAN key."""

    def report(self, source="192.168.0.50"):
        from tests.test_ewelink_lan import ADVERT
        return {"packets": [{"source": source, "records": [
            {"type": "TXT", "value": [f"{k}={v}" for k, v in ADVERT.items()]}]}]}

    def ctx(self, devices):
        from types import SimpleNamespace
        return SimpleNamespace(account_devices=lambda: devices)

    def test_the_plugs_own_advert_gives_its_signal(self):
        import tools.autoflash_cli as af
        from tests.test_ewelink_lan import ADVERT, KEY
        ctx = self.ctx([{"deviceid": "1000ffffff", "devicekey": "other"},
                        {"deviceid": ADVERT["id"], "devicekey": KEY}])
        self.assertEqual(af.advert_signal(ctx, self.report(), "192.168.0.50"), -61)

    def test_another_devices_advert_or_no_key_gives_nothing(self):
        import tools.autoflash_cli as af
        from tests.test_ewelink_lan import ADVERT, KEY
        key = [{"deviceid": ADVERT["id"], "devicekey": KEY}]
        self.assertIsNone(af.advert_signal(self.ctx(key), self.report("192.168.0.7"), "192.168.0.50"))
        self.assertIsNone(af.advert_signal(self.ctx([]), self.report(), "192.168.0.50"))
        self.assertIsNone(af.advert_signal(self.ctx(None), self.report(), "192.168.0.50"))


class SecretPromptTests(unittest.TestCase):
    """Only the character just typed is ever readable, and never after Enter."""

    def test_only_the_last_character_is_shown(self):
        from tools.autoflash.prompts import SecretLine
        line = SecretLine()
        for char in "pass":
            self.assertTrue(line.feed(char))
        self.assertEqual(line.display(reveal_last=True), "***s")
        self.assertEqual(line.display(reveal_last=False), "****")
        self.assertEqual(line.value, "pass")

    def test_backspace_and_ctrl_u_edit_the_value(self):
        from tools.autoflash.prompts import SecretLine
        line = SecretLine()
        for char in "pasz\x7fs":
            line.feed(char)
        self.assertEqual(line.value, "pass")
        line.feed("\x15")
        self.assertEqual((line.value, line.display(True)), ("", ""))

    def test_control_characters_are_not_stored(self):
        from tools.autoflash.prompts import SecretLine
        line = SecretLine()
        for char in "a\tb\x01c":
            line.feed(char)
        self.assertEqual(line.value, "abc")

    @unittest.skipIf(sys.platform == "win32", "needs a POSIX pseudo-terminal")
    def test_a_real_terminal_never_keeps_a_readable_character(self):
        import os
        import pty
        import subprocess
        import sys
        import time

        master, slave = pty.openpty()
        code = (
            "from tools.autoflash.prompts import ask_secret\n"
            "value = ask_secret('Password: ')\n"
            "print('LEN', len(value), value == 'Secret-9x')\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code], stdin=slave, stdout=slave, stderr=slave,
            cwd=Path(__file__).resolve().parents[1],
        )
        os.close(slave)
        output = b""

        def drain(seconds, until=None):
            nonlocal output
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline and not (until and until in output):
                import select
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        output += os.read(master, 4096)
                    except OSError:
                        return

        # Typed only once the prompt is up: a busy machine can take more than
        # a second to start the child, and keys typed before then are lost.
        drain(15.0, until=b"> ")
        for char in "Secret-9x":
            os.write(master, char.encode())
            drain(0.05)
        os.write(master, b"\r")
        drain(1.0)
        process.wait(timeout=5)
        os.close(master)

        text = output.decode(errors="replace")
        self.assertIn("LEN 9 True", text)
        # The prompt, with its quit hint, is too long to share the input row in
        # an 80-column terminal, so it sits on its own line above "> ".
        self.assertIn("Password (q to quit):", text)
        # What is left on the input row after its final redraw: asterisks only.
        input_rows = [row for row in text.split("\n") if "> " in row]
        final = input_rows[-1].rstrip("\r").split("\r")[-1].replace("\x1b[K", "")
        self.assertEqual(final.strip(), "> *********")
        # While typing, each character appeared alone at the end, never with the rest.
        self.assertIn("> ********x", text)
        self.assertNotIn("Secret", text)


class PlugFinderTests(unittest.TestCase):
    """The plug is found at its address, or by its hardware address if it moved."""

    def finder(self, inputs):
        from types import SimpleNamespace
        import tools.autoflash_cli as autoflash

        moved = []
        state = SimpleNamespace(input=lambda key: inputs.get(key))
        ctx = SimpleNamespace(state=state, set_plug_ip=moved.append)
        return autoflash.PlugFinder(ctx), moved

    def test_the_plug_at_its_usual_address_is_found_without_a_sweep(self):
        from unittest import mock
        from tools.autoflash import hostsys
        finder, moved = self.finder({"plug_ip": "10.0.0.5", "plug_mac": "aa:bb:cc:dd:ee:ff"})
        with mock.patch.object(hostsys, "ping_ok", return_value=True), \
             mock.patch.object(hostsys, "neighbour_table", return_value={"10.0.0.5": "aa:bb:cc:dd:ee:ff"}), \
             mock.patch.object(hostsys, "find_by_mac") as sweep:
            self.assertEqual(finder.poll(), "10.0.0.5")
        sweep.assert_not_called()
        self.assertEqual(moved, [])

    def test_a_different_device_at_the_old_address_is_not_mistaken_for_the_plug(self):
        from unittest import mock
        from tools.autoflash import hostsys
        finder, moved = self.finder({"plug_ip": "10.0.0.5", "plug_mac": "aa:bb:cc:dd:ee:ff"})
        with mock.patch.object(hostsys, "ping_ok", return_value=True), \
             mock.patch.object(hostsys, "neighbour_table", return_value={"10.0.0.5": "11:22:33:44:55:66"}), \
             mock.patch.object(hostsys, "find_by_mac", return_value="10.0.0.9"):
            self.assertEqual(finder.poll(), "10.0.0.9")
        self.assertEqual(moved, ["10.0.0.9"])

    def test_sweeps_are_rate_limited(self):
        from unittest import mock
        from tools.autoflash import hostsys
        finder, _ = self.finder({"plug_ip": "10.0.0.5", "plug_mac": "aa:bb:cc:dd:ee:ff"})
        with mock.patch.object(hostsys, "ping_ok", return_value=False), \
             mock.patch.object(hostsys, "find_by_mac", return_value=None) as sweep:
            self.assertIsNone(finder.poll())
            self.assertIsNone(finder.poll())
        self.assertEqual(sweep.call_count, 1)


class FetchCheckTests(unittest.TestCase):
    """A phase stops within seconds when the plug never fetches its program."""

    class Process:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    class Reader:
        def join(self, _):
            pass

    def test_a_fetch_lets_the_phase_continue(self):
        from tools.autoflash.phases import PhaseRunner
        process = self.Process()
        PhaseRunner._require_fetch(None, process, ['  [10.0.0.5] "GET /berry/preflight.be HTTP/1.1" 200 -'],
                                   self.Reader(), "preflight")
        self.assertFalse(process.terminated)

    def test_no_fetch_stops_the_server_and_says_so(self):
        from unittest import mock
        from tools.autoflash import phases
        process = self.Process()
        with mock.patch.object(phases, "FETCH_DEADLINE", 1.0):
            with self.assertRaises(phases.PlugNeverConnected):
                phases.PhaseRunner._require_fetch(None, process, [], self.Reader(), "preflight")
        self.assertTrue(process.terminated)


class ModelGuardTests(unittest.TestCase):
    """Only an S60 may continue past A2 - another Sonoff on the account must not."""

    def discover_with_model(self, model):
        import json
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        state = RunState.open(root)
        state.set_input("plug_ip", "10.0.0.63")
        ctx = SimpleNamespace(dir=root, state=state, email=lambda: "a@b.c",
                              ewelink_env=lambda: {})

        def fake_tool(args, **_):
            if "tools/query_ota.py" in args:
                (root / "ota-metadata.json").write_text(json.dumps({
                    "query": {"version": "1.2.0", "model": model},
                    "response": {"data": {"otaInfoList": []}},
                }))
            return 0

        answered = {"packets": [{"source": "10.0.0.63", "records": []}]}
        with mock.patch.object(autoflash, "run_tool", side_effect=fake_tool), \
             mock.patch.object(autoflash, "discover", return_value=answered), \
             mock.patch.object(autoflash, "section"), mock.patch.object(autoflash, "say"):
            return autoflash.step_discover(ctx)

    def test_an_s60_continues(self):
        self.assertEqual(self.discover_with_model("SN-ESP32C3-S60-01")["model"], "SN-ESP32C3-S60-01")

    def test_any_other_model_is_refused_before_anything_is_sent(self):
        import tools.autoflash_cli as autoflash
        with self.assertRaisesRegex(autoflash.Abort, "not a Sonoff S60"):
            self.discover_with_model("SN-ESP32-MINIR4")


class AlreadyConvertingTests(unittest.TestCase):
    """A plug part way through a conversion is not a LAN Control problem."""

    def silent_discover(self, page):
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash
        from tools.autoflash import setup_network

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        state.set_input("plug_ip", "10.0.0.63")
        ctx = SimpleNamespace(dir=Path(directory.name), state=state)
        with mock.patch.object(autoflash, "discover", return_value={"packets": []}), \
             mock.patch.object(setup_network, "page", return_value=page), \
             mock.patch.object(autoflash.hostsys, "security_software_note", return_value=None), \
             mock.patch.object(autoflash, "section"):
            return autoflash.step_discover(ctx)

    def test_the_bridge_is_named(self):
        import tools.autoflash_cli as autoflash
        with self.assertRaisesRegex(autoflash.Abort, "already running our installer firmware"):
            self.silent_discover("<title>S60 OTA Bridge</title>")

    def test_tasmota_is_named(self):
        import tools.autoflash_cli as autoflash
        with self.assertRaisesRegex(autoflash.Abort, "running Tasmota, not Sonoff's firmware"):
            self.silent_discover("<title>Tasmota</title>")

    def test_no_page_keeps_the_lan_control_advice(self):
        import tools.autoflash_cli as autoflash
        with self.assertRaisesRegex(autoflash.Abort, "LAN Control"):
            self.silent_discover(None)


class ConversionWifiTests(unittest.TestCase):
    """G1 needs the Wi-Fi the plug was converted with, proven against the
    commit's record of the startup firmware - never the bundle's say-so."""

    def setUp(self):
        import hashlib
        import json
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash

        self.autoflash = autoflash
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        evidence = Path(directory.name)
        image = lambda ssid, password: f"{ssid}/{password}".encode()  # noqa: E731
        (evidence / "commit-report.json").write_text(json.dumps({
            "phase": "commit", "status": "PASS",
            "safeboot_sha256": hashlib.sha256(image("Home", "right")).hexdigest(),
        }))
        self.ctx = SimpleNamespace(evidence=evidence, manifest_path=Path("unused"),
                                   args=SimpleNamespace(wifi=False),
                                   remember_wifi=mock.Mock())
        self.pinned = ("Home", "right")
        self.pin = mock.Mock(side_effect=lambda _, ssid, pw: setattr(self, "pinned", (ssid, pw)))

        def recovery(_, ssid, password):
            if (ssid, password) != self.pinned:
                raise autoflash.BundleError("not the pinned one")
            return image(ssid, password)

        for patch in (
            mock.patch.object(autoflash, "startup_image", side_effect=image),
            mock.patch.object(autoflash, "recovery_image", side_effect=recovery),
            mock.patch.object(autoflash, "pin_wifi", self.pin),
            mock.patch.object(autoflash, "section"),
            mock.patch.object(autoflash, "say"),
            mock.patch.object(autoflash, "tell"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def run_with(self, on_plug, typed=()):
        from unittest import mock
        answers = iter(typed)
        with mock.patch.object(self.autoflash, "read_wifi_from_plug", return_value=on_plug), \
             mock.patch.object(self.autoflash, "ask_text", side_effect=lambda *_, **__: next(answers)), \
             mock.patch.object(self.autoflash, "ask_secret", side_effect=lambda *_: next(answers)) as asked:
            self.autoflash.conversion_wifi(self.ctx)
        return asked

    def test_the_plugs_own_details_are_used_when_they_match(self):
        asked = self.run_with(("Home", "right"))
        asked.assert_not_called()
        self.pin.assert_not_called()
        self.ctx.remember_wifi.assert_called_once_with("Home", "right", b"Home/right")

    def test_changed_wifi_on_the_plug_asks_for_the_conversions(self):
        asked = self.run_with(("NewNet", "new"), typed=["Home", "wrong", "Home", "right"])
        self.assertEqual(asked.call_count, 2)  # a wrong password is asked again
        self.ctx.remember_wifi.assert_called_once_with("Home", "right", b"Home/right")
        self.pin.assert_not_called()  # the bundle was never moved off them

    def test_a_bundle_set_up_for_other_wifi_is_put_back(self):
        self.pinned = ("NewNet", "new")
        self.run_with(("Home", "right"))
        self.pin.assert_called_once_with(Path("unused"), "Home", "right")

    def test_the_plugs_new_details_are_never_pinned(self):
        self.run_with(("NewNet", "new"), typed=["Home", "right"])
        for call in self.pin.call_args_list:
            self.assertNotEqual(call.args[1:], ("NewNet", "new"))

    def test_a_lost_commit_report_falls_back_to_the_staging_record(self):
        import hashlib
        import json
        (self.ctx.evidence / "commit-report.json").unlink()
        (self.ctx.evidence / "stage-report.json").write_text(json.dumps({
            "phase": "stage", "status": "PASS",
            "staged_sha256": hashlib.sha256(b"Home/right").hexdigest()}))
        self.run_with(("Home", "right"))
        self.ctx.remember_wifi.assert_called_once_with("Home", "right", b"Home/right")

    def test_no_commit_record_stops_before_asking(self):
        (self.ctx.evidence / "commit-report.json").unlink()
        with self.assertRaisesRegex(self.autoflash.Abort, "no record of the startup firmware"):
            self.run_with(("Home", "right"))


class PhaseFailureTests(unittest.TestCase):
    """A phase that does not pass says why: the plug's words, or the timeout's."""

    def test_the_plugs_reason_is_given(self):
        from tools.autoflash.phases import failure_reason
        captured = "STAGE RESULT: FAIL\nTHE PLUG SAYS: migration_refused: GET /chunk/safeboot/2 returned HTTP -1\n"
        self.assertEqual(failure_reason(captured),
                         "the plug says: migration_refused: GET /chunk/safeboot/2 returned HTTP -1")

    def test_a_timeout_is_given(self):
        from tools.autoflash.phases import failure_reason
        self.assertIn("sent nothing for 6 minutes",
                      failure_reason("TIMEOUT: the plug has sent nothing for 6 minutes, and gave no reason."))

    def test_nothing_known_is_none(self):
        from tools.autoflash.phases import failure_reason
        self.assertIsNone(failure_reason("  [10.0.0.63] \"GET /berry/stage.be HTTP/1.1\" 200 -"))

    def test_the_step_abort_carries_the_reason(self):
        import tools.autoflash_cli as autoflash
        error = autoflash.phase_failed("stage", 4, "THE PLUG SAYS: flash is not exactly 4 MiB")
        self.assertEqual(str(error), "stage did not pass: the plug says: flash is not exactly 4 MiB")
        self.assertEqual(str(autoflash.phase_failed("stage", 3, "")), "stage did not pass (exit 3)")

    def console(self, lines):
        from unittest import mock
        from tools.autoflash.phases import PhaseRunner
        runner = PhaseRunner.__new__(PhaseRunner)
        shown = []
        runner.log = shown.append
        runner.device = mock.Mock(console_log=mock.Mock(return_value=lines))
        return runner._show_plug_console(), shown

    def test_the_plugs_console_is_read_for_its_own_words(self):
        # As the emulated plug logged it, cut off from the computer mid-stage.
        said, shown = self.console([
            "13:56:01.000 WIF: Connected",
            "13:56:03.251 S60 STAGE: 65536/811824 bytes",
            "13:57:11.702 HTP: Connection timeout after -1 ms",
            '13:57:11.702 RSL: RESULT = {"Br":"[migration_refused] POST /capture/staged/22 '
            'returned HTTP -1 after 21 attempts"}',
        ])
        self.assertEqual(said, "POST /capture/staged/22 returned HTTP -1 after 21 attempts")
        self.assertEqual(len(shown), 2)
        self.assertTrue(all(line.startswith("    The plug's console: ") for line in shown))

    def test_another_berry_error_keeps_its_kind(self):
        said, _ = self.console(['RSL: RESULT = {"Br":"[index_error] list index out of range"}'])
        self.assertEqual(said, "index_error: list index out of range")

    def test_an_earlier_attempts_error_is_not_this_ones_reason(self):
        said, shown = self.console([
            '13:29:34.062 RSL: RESULT = {"Br":"[migration_refused] POST /capture/staged/2 '
            'returned HTTP -1 after 107 attempts"}',
            "13:31:03.251 S60 STAGE: 65536/811824 bytes",
        ])
        self.assertIsNone(said)
        self.assertEqual(len(shown), 2)  # still shown, for whoever reads the log

    def test_a_plug_stuck_after_a_phase_is_to_be_switched_off_and_on(self):
        # Tasmota waits for ever on a download cut off part way, answering
        # nothing; switching it off and on is the way out, and safe here.
        from unittest import mock
        from tools.autoflash.phases import PhaseRunner
        for answers, advised in ((False, True), (True, False)):
            runner = PhaseRunner.__new__(PhaseRunner)
            shown = []
            runner.log = shown.append
            runner._plug_answers = mock.Mock(return_value=answers)
            runner._advise_if_stuck()
            self.assertEqual(any("switch it off" in line for line in shown), advised, answers)

    def test_a_plug_that_cannot_answer_adds_nothing(self):
        from unittest import mock
        from tools.autoflash.phases import PhaseRunner
        runner = PhaseRunner.__new__(PhaseRunner)
        shown = []
        runner.log = shown.append
        runner.device = mock.Mock(console_log=mock.Mock(side_effect=OSError("unreachable")))
        runner._show_plug_console()
        self.assertEqual(shown, [])


class PhaseWaitTests(unittest.TestCase):
    """A phase is waited on only while the plug may still be working on it."""

    class Server:
        def __init__(self, exits_after=None):
            self.calls = 0
            self.exits_after = exits_after

        def wait(self, timeout):
            import subprocess
            self.calls += 1
            if self.exits_after is not None and self.calls >= self.exits_after:
                return 0
            raise subprocess.TimeoutExpired("server", timeout)

        def poll(self):
            return None

    def wait(self, server, answers, heard_ago=60.0, exec_timeout=540.0):
        from unittest import mock
        from tools.autoflash import phases
        runner = phases.PhaseRunner.__new__(phases.PhaseRunner)
        runner._plug_answers = mock.Mock(side_effect=answers)
        clock = iter(range(1000, 100_000, 2))
        with mock.patch.object(phases.time, "monotonic", side_effect=lambda: next(clock)):
            return runner._wait("stage", server, exec_timeout, lambda: 1000 - heard_ago), runner

    def test_a_plug_that_answers_after_going_quiet_has_stopped(self):
        from tools.autoflash.phases import PhaseError
        with self.assertRaisesRegex(PhaseError, "stopped the stage without reaching this computer"):
            self.wait(self.Server(), answers=[True])

    def test_a_busy_plug_is_waited_on(self):
        # Not answering: still at it (or offline, riding out a router restart).
        code, runner = self.wait(self.Server(exits_after=40), answers=[False] * 100)
        self.assertEqual(code, 0)
        self.assertGreater(runner._plug_answers.call_count, 1)

    def test_a_plug_still_sending_is_not_disturbed(self):
        code, runner = self.wait(self.Server(exits_after=20), answers=[True] * 100, heard_ago=-10_000)
        self.assertEqual(code, 0)
        runner._plug_answers.assert_not_called()

    def test_the_backstop_still_ends_it(self):
        from tools.autoflash.phases import PhaseError
        with self.assertRaisesRegex(PhaseError, "did not complete within 1 minutes"):
            self.wait(self.Server(), answers=[False] * 1000, exec_timeout=60.0)

    def test_starting_does_not_wait_for_a_reply(self):
        # The commit restarts the plug: its reply never comes, and the result
        # is already in - this used to cost a minute per conversion.
        import threading
        from unittest import mock
        from tools.autoflash.phases import PhaseRunner
        runner = PhaseRunner.__new__(PhaseRunner)
        release = threading.Event()
        runner.device = mock.Mock(berry_fire=mock.Mock(side_effect=lambda *a, **k: release.wait(5)))
        runner.log = mock.Mock()
        runner._start('s60_commit("x")')
        release.set()
        runner.device.berry_fire.assert_called_once()


class RetryTests(unittest.TestCase):
    """Safe steps that failed for a reason that may pass are run again."""

    def run_with(self, step, errors):
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        ctx = SimpleNamespace(state=state)
        outcomes = iter(errors)

        def fake_execute(_, name):
            error = next(outcomes)
            if error is None:
                state.mark(name, DONE)
                return 0
            state.mark(name, FAILED, error=error)
            return 1
        with mock.patch.object(autoflash, "execute", side_effect=fake_execute) as ran, \
             mock.patch.object(autoflash.time, "sleep"), mock.patch.object(autoflash, "tell"):
            code = autoflash.execute_with_retries(ctx, step)
        return code, ran.call_count

    def test_a_network_failure_is_tried_again(self):
        code, runs = self.run_with("E2", ["stage did not pass: the plug says: POST /capture/staged/22 "
                                          "returned HTTP -1 after 21 attempts", None])
        self.assertEqual((code, runs), (0, 2))

    def test_a_wifi_drop_is_tried_again(self):
        # The plug now gives up after 20s, and this computer waits out the rest.
        code, runs = self.run_with("E2", ["stage did not pass: the plug says: migration_refused: "
                                          "the plug's Wi-Fi went down and was not back within "
                                          "20 seconds", None])
        self.assertEqual((code, runs), (0, 2))

    def test_three_attempts_at_most(self):
        code, runs = self.run_with("E1", ["timed out"] * 5)
        self.assertEqual((code, runs), (1, 3))

    def test_a_refusal_is_not_tried_again(self):
        code, runs = self.run_with("E2", ["stage did not pass: the plug says: "
                                          "migration_refused: NVS changed since preflight"])
        self.assertEqual((code, runs), (1, 1))

    def test_the_commit_is_never_tried_again(self):
        code, runs = self.run_with("E3", ["timed out", None])
        self.assertEqual((code, runs), (1, 1))

    def test_a_phase_with_no_result_keeps_the_commit_attempted(self):
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash
        from tools.autoflash.phases import PhaseError

        for step, expected in (("E3", "attempted"), ("E2", FAILED)):
            directory = tempfile.TemporaryDirectory()
            self.addCleanup(directory.cleanup)
            state = RunState.open(Path(directory.name))
            title, _ = autoflash.STEP_INDEX[step]
            failing = mock.Mock(side_effect=PhaseError("the plug stopped the commit without reaching this computer"))
            with mock.patch.dict(autoflash.STEP_INDEX, {step: (title, failing)}), \
                 mock.patch.object(autoflash, "begin_stage"), \
                 mock.patch.object(autoflash.output, "stage_end"), \
                 mock.patch.object(autoflash, "tell"), mock.patch.object(autoflash, "say"):
                autoflash.execute(SimpleNamespace(state=state), step)
            self.assertEqual(state.status(step), expected, step)


class StageTimingTests(unittest.TestCase):
    """Every stage's time is kept for the report, however it ends."""

    def test_a_stage_that_fails_has_its_time_and_the_plugs_busy_time_kept(self):
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash
        from tools.autoflash.phases import PhaseError

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        ctx = SimpleNamespace(state=state)

        def stage(context):
            context.last_runner = SimpleNamespace(busy_seconds=41.5)
            raise PhaseError("the plug stopped the stage without reaching this computer")
        title, _ = autoflash.STEP_INDEX["E2"]
        with mock.patch.dict(autoflash.STEP_INDEX, {"E2": (title, stage)}), \
             mock.patch.object(autoflash, "begin_stage"), \
             mock.patch.object(autoflash.output, "stage_end"), \
             mock.patch.object(autoflash, "tell"), mock.patch.object(autoflash, "say"):
            self.assertEqual(autoflash.execute(ctx, "E2"), 1)
        captures = state.data["captures"]
        self.assertIsInstance(captures["E2.seconds"], int)
        self.assertEqual(captures["E2.plug_busy_seconds"], 41.5)
        self.assertIn("without reaching", ctx.last_failure)

        # A quicker try after it keeps the longest: how near the plug came to stopping.
        def quicker(context):
            context.last_runner = SimpleNamespace(busy_seconds=3.0)
            raise PhaseError("the plug did not fetch the stage instructions")
        with mock.patch.dict(autoflash.STEP_INDEX, {"E2": (title, quicker)}), \
             mock.patch.object(autoflash, "begin_stage"), \
             mock.patch.object(autoflash.output, "stage_end"), \
             mock.patch.object(autoflash, "tell"), mock.patch.object(autoflash, "say"):
            autoflash.execute(ctx, "E2")
        self.assertEqual(state.data["captures"]["E2.plug_busy_seconds"], 41.5)


class PlugBackTests(unittest.TestCase):
    """Before a step is tried again, a plug that is not answering is waited for."""

    def wait(self, answers, d1_done=True, step=None):
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        state.set_input("plug_ip", "10.0.0.63")
        if d1_done:
            state.mark("D1", DONE)
        device = mock.Mock(answers=mock.Mock(side_effect=list(answers)))
        clock = iter(range(0, 100_000, 3))
        with mock.patch.object(autoflash.time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(autoflash.time, "sleep"), \
             mock.patch.object(autoflash, "tell") as told, \
             mock.patch.object(autoflash.output, "progress"), \
             mock.patch.object(autoflash.output, "progress_clear"):
            autoflash.wait_for_plug_back(SimpleNamespace(state=state, device=device), step)
        return device, told

    def test_an_answering_plug_is_tried_again_at_once(self):
        device, told = self.wait([True])
        told.assert_not_called()

    def test_a_plug_back_after_a_router_restart_is_tried_again_then(self):
        device, told = self.wait([False] * 30 + [True])
        self.assertEqual(device.answers.call_count, 31)
        self.assertIn("answering again", told.call_args.args[0])

    def test_a_long_wait_says_the_plug_has_no_network_of_its_own_now(self):
        said = lambda told: " ".join(call.args[0] for call in told.call_args_list)
        device, told = self.wait([False] * 30 + [True], step="E2")
        self.assertIn("no setup network of its own", said(told))
        device, told = self.wait([False] * 5 + [True], step="E2")
        self.assertNotIn("no setup network", said(told))  # back before it was worth saying
        # F4's official Tasmota may not have the Wi-Fi yet, so may have one.
        device, told = self.wait([False] * 30 + [True], step="F4")
        self.assertNotIn("no setup network", said(told))

    def test_one_that_never_answers_is_tried_anyway_after_five_minutes(self):
        device, told = self.wait([False] * 1000)
        self.assertIn("trying anyway", told.call_args.args[0])
        self.assertLess(device.answers.call_count, 1000)

    def test_before_tasmota_nothing_is_waited_for(self):
        device, _ = self.wait([], d1_done=False)
        device.answers.assert_not_called()


class BusyPlugTests(unittest.TestCase):
    """A plug still finishing a stopped step is waited for, not misread."""

    def ctx(self, *, d1_done=True, answers=(), pings=True):
        from types import SimpleNamespace
        from unittest import mock
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        state.set_input("plug_ip", "10.0.0.63")
        if d1_done:
            state.mark("D1", DONE)
        device = mock.Mock(answers=mock.Mock(side_effect=list(answers)))
        return SimpleNamespace(state=state, device=device), pings

    def wait(self, ctx, pings):
        from unittest import mock
        import tools.autoflash_cli as autoflash
        clock = iter(range(0, 100_000, 3))
        with mock.patch.object(autoflash.hostsys, "ping_ok", return_value=pings), \
             mock.patch.object(autoflash.time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(autoflash.time, "sleep"), \
             mock.patch.object(autoflash, "tell") as told, \
             mock.patch.object(autoflash.output, "progress"), \
             mock.patch.object(autoflash.output, "progress_clear"):
            autoflash.wait_while_plug_busy(ctx)
        return told

    def test_a_plug_that_answers_is_not_waited_for(self):
        ctx, pings = self.ctx(answers=[True])
        self.wait(ctx, pings).assert_not_called()

    def test_a_busy_plug_is_waited_for_until_it_answers(self):
        ctx, pings = self.ctx(answers=[False, False, False, True])
        told = self.wait(ctx, pings)
        self.assertEqual(ctx.device.answers.call_count, 4)
        self.assertIn("answering again", told.call_args.args[0])

    def test_a_plug_off_the_network_is_left_to_the_steps(self):
        ctx, _ = self.ctx(answers=[False])
        self.wait(ctx, False).assert_not_called()

    def test_before_tasmota_nothing_is_checked(self):
        ctx, pings = self.ctx(d1_done=False)
        self.wait(ctx, pings)
        ctx.device.answers.assert_not_called()


class UserActionWaitTests(unittest.TestCase):
    """Waiting on a person has no time limit - unless nobody is coming."""

    def test_a_person_is_waited_for_as_long_as_it_takes(self):
        from unittest import mock
        import tools.autoflash_cli as autoflash
        with mock.patch.object(autoflash.prompts, "UNATTENDED", False):
            self.assertEqual(autoflash.user_action_limit(), float("inf"))
        with mock.patch.object(autoflash.prompts, "UNATTENDED", True):
            self.assertEqual(autoflash.user_action_limit(), 900.0)


class UnexpectedErrorTests(unittest.TestCase):
    """An error nothing anticipated is recorded and explained, not a traceback."""

    def run_step(self, step, error):
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        ctx = SimpleNamespace(state=state)
        title, _ = autoflash.STEP_INDEX[step]
        failing = mock.Mock(side_effect=error)
        with mock.patch.dict(autoflash.STEP_INDEX, {step: (title, failing)}), \
             mock.patch.object(autoflash, "begin_stage"), \
             mock.patch.object(autoflash.output, "stage_end") as ended, \
             mock.patch.object(autoflash.output, "log_only") as logged, \
             mock.patch.object(autoflash, "tell") as told, \
             mock.patch.object(autoflash, "say"):
            code = autoflash.execute(ctx, step)
        return code, state, ended, logged, told

    def test_it_stops_the_step_with_a_message_and_the_traceback_in_the_log(self):
        import http.client
        code, state, ended, logged, told = self.run_step("E1", http.client.IncompleteRead(b""))
        self.assertEqual(code, 1)
        self.assertEqual(state.status("E1"), FAILED)
        ended.assert_called_once_with("stopped")
        self.assertIn("Traceback", logged.call_args.args[0])
        self.assertIn("something unexpected went wrong: IncompleteRead", told.call_args.args[0])

    def test_a_step_that_writes_stays_attempted(self):
        # Whether the commit got as far as writing is for the next run to find
        # out from the plug - so it must not be recorded as failed.
        code, state, *_ = self.run_step("E3", RuntimeError("boom"))
        self.assertEqual(code, 1)
        self.assertEqual(state.status("E3"), "attempted")

    def test_ctrl_c_is_not_swallowed(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_step("E1", KeyboardInterrupt())


class BerryFireTests(unittest.TestCase):
    """A phase that restarts the plug cuts its own reply off: that is no reply,
    not a crash (the E3 traceback on macOS)."""

    def test_a_reply_cut_off_is_no_reply(self):
        import http.client
        from unittest import mock
        from tools.autoflash.device import Device

        device = Device("10.0.0.63")
        for error in (http.client.IncompleteRead(b""), ConnectionResetError(54, "reset")):
            with mock.patch.object(device, "berry", side_effect=error):
                self.assertFalse(device.berry_fire('s60_commit("x")'))

    def test_other_errors_still_surface(self):
        from unittest import mock
        from tools.autoflash.device import Device, DeviceError

        device = Device("10.0.0.63")
        with mock.patch.object(device, "berry", side_effect=DeviceError("password")):
            with self.assertRaises(DeviceError):
                device.berry_fire("x")


class CommitEvidenceTests(unittest.TestCase):
    """E3 interrupted after the plug reported is finished, not a question."""

    def ctx(self, report):
        import json
        from types import SimpleNamespace

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        evidence = Path(directory.name)
        if report is not None:
            (evidence / "commit-report.json").write_text(json.dumps(report))
        return SimpleNamespace(evidence=evidence)

    def test_a_pass_report_means_finished(self):
        import tools.autoflash_cli as autoflash
        finished, _ = autoflash.evidence_commit_finished(
            self.ctx({"phase": "commit", "status": "PASS"}))
        self.assertTrue(finished)

    def test_a_fail_report_means_not_finished(self):
        import tools.autoflash_cli as autoflash
        finished, _ = autoflash.evidence_commit_finished(
            self.ctx({"phase": "commit", "status": "FAIL"}))
        self.assertIs(finished, False)

    def no_report(self, *, fetched, safeboot=False, slot=None):
        # The emulated plug's PASS report lost on its way (the commit with the
        # network cut as it finished): it then restarts into the startup firmware.
        from unittest import mock
        ctx = self.ctx(None)
        if fetched:
            (ctx.evidence / "commit-server.log").write_text(
                '  [10.0.0.63] "GET /berry/commit.be HTTP/1.1" 200 -\n'
                '  [10.0.0.63] "GET /target-table HTTP/1.1" 200 -\n')
        ctx.device = mock.Mock(is_safeboot=mock.Mock(return_value=safeboot),
                               current_ota=mock.Mock(return_value=slot))
        import tools.autoflash_cli as autoflash
        return autoflash.evidence_commit_finished(ctx)

    def test_no_report_and_no_table_fetched_means_nothing_written(self):
        finished, _ = self.no_report(fetched=False)
        self.assertIs(finished, False)

    def test_no_report_but_on_the_startup_firmware_means_finished(self):
        finished, reason = self.no_report(fetched=True, safeboot=True)
        self.assertTrue(finished)
        self.assertIn("temporary startup", reason)

    def test_no_report_and_still_on_the_old_layout_means_not_finished(self):
        finished, _ = self.no_report(fetched=True, slot=1)
        self.assertIs(finished, False)

    def test_no_report_and_no_telling_is_left_to_the_operator(self):
        finished, _ = self.no_report(fetched=True, slot=None)
        self.assertIsNone(finished)

    def test_another_phase_report_is_not_taken(self):
        import tools.autoflash_cli as autoflash
        finished, _ = autoflash.evidence_commit_finished(
            self.ctx({"phase": "stage", "status": "PASS"}))
        self.assertIsNot(finished, True)  # never taken as the commit's

    def test_a_passed_commit_supersedes_the_high_slot_check(self):
        # After the commit the plug runs from slot 0 of the new layout: asking
        # to redo D4 then would put the interim Tasmota back over it.
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash

        ctx = self.ctx({"phase": "commit", "status": "PASS"})
        ctx.state = SimpleNamespace(is_done=lambda step: False)
        ctx.device = mock.Mock()
        self.assertEqual(autoflash.verify_high_slot(ctx), (True, ""))
        ctx.device.current_ota.assert_not_called()

    def test_without_a_report_the_high_slot_is_still_checked(self):
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash

        ctx = self.ctx(None)
        ctx.state = SimpleNamespace(is_done=lambda step: False)
        ctx.device = mock.Mock(current_ota=mock.Mock(return_value=0))
        ok, message = autoflash.verify_high_slot(ctx)
        self.assertFalse(ok)
        self.assertIn("high slot", message)

    def test_redoing_a_passed_commit_touches_nothing(self):
        # The startup firmware cannot answer the network check: running it
        # would fail and send the run back to E1 with no way forward.
        from unittest import mock
        import tools.autoflash_cli as autoflash

        ctx = self.ctx({"phase": "commit", "status": "PASS"})
        with mock.patch.object(autoflash, "check_plug_network") as network, \
             mock.patch.object(autoflash, "say"):
            result = autoflash.step_commit(ctx)
        network.assert_not_called()
        self.assertTrue(result["already_written"])

    def test_a_commit_refused_as_changed_sends_the_run_back_to_its_checks(self):
        # After a power cut mid-commit the plug's settings and staged copy have
        # changed: the commit, run again as it was, is refused every time.
        import json
        from unittest import mock
        import tools.autoflash_cli as autoflash

        ctx = self.ctx(None)
        manifest = ctx.evidence / "manifest.json"
        manifest.write_text("{}")
        (ctx.evidence / "stage-report.json").write_text(json.dumps(
            {"manifest_sha256": autoflash.sha256_bytes(manifest.read_bytes())}))
        ctx.manifest_path = manifest
        ctx.state = RunState.open(ctx.evidence)
        for step in ("E1", "E2"):
            ctx.state.mark(step, "done")
        refused = 'THE PLUG SAYS: migration_refused: NVS changed since preflight'
        ctx.phase_runner = lambda: mock.Mock(run=mock.Mock(return_value=(1, refused)))
        with mock.patch.object(autoflash, "check_plug_network"), \
             mock.patch.object(autoflash, "plug_back_on_wifi"), \
             mock.patch.object(autoflash, "sonoff_layout_back"), \
             mock.patch.object(autoflash.hostsys, "replace_file"), \
             mock.patch.object(autoflash.safety_locks, "lifted", no_lock_records), \
             mock.patch.object(autoflash, "section"), mock.patch.object(autoflash, "say"):
            with self.assertRaises(autoflash.Abort) as caught:
                autoflash.commit_once(ctx)
        self.assertIn("run the same command", str(caught.exception))
        self.assertEqual([ctx.state.status(s) for s in ("E1", "E2")], [FAILED, FAILED])

    def commit_tries(self, outcomes, recovered=False):
        """step_commit with each try's outcome given: an exception, or a result."""
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        ctx = SimpleNamespace(evidence=Path(directory.name), state=RunState.open(Path(directory.name)))
        tries = iter(outcomes)

        def once(_):
            outcome = next(tries)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with mock.patch.object(autoflash, "commit_once", side_effect=once) as commit, \
             mock.patch.object(autoflash, "commit_recovered", return_value=recovered) as recover, \
             mock.patch.object(autoflash, "tell"), mock.patch.object(autoflash, "say"):
            try:
                result = autoflash.step_commit(ctx)
            except Exception as exc:  # noqa: BLE001 - what the test looks at
                result = exc
        return result, commit.call_count, recover.call_count, ctx.state

    def test_a_power_cut_in_the_commit_is_recovered_from_and_tried_again(self):
        import tools.autoflash_cli as autoflash
        from tools.autoflash.phases import PhaseError
        result, tries, recoveries, state = self.commit_tries(
            [PhaseError("the plug stopped the commit without reaching this computer"), {"exit": 0}])
        self.assertEqual((result, tries, recoveries), ({"exit": 0}, 2, 1))
        self.assertEqual(state.data["captures"]["E3.attempts"], 2)
        self.assertEqual(autoflash.COMMIT_ATTEMPTS, 3)

    def test_a_read_back_that_differed_is_tried_again_and_so_is_a_refusal_as_changed(self):
        import tools.autoflash_cli as autoflash
        result, tries, _, _ = self.commit_tries([
            autoflash.CommitFailed("commit did not pass: the plug says: migration_refused: "
                                   "canonical Safeboot read-back differs at 0x31000",
                                   "S60 ROLLBACK PASS: interim layout kept"),
            autoflash.CommitFailed("commit refused before writing anything - the plug says: "
                                   "migration_refused: staged Safeboot SHA-256 differs", ""),
            {"exit": 0}])
        self.assertEqual((result, tries), ({"exit": 0}, 3))

    def test_the_plug_refusing_connections_while_it_comes_back_is_another_try(self):
        import urllib.error
        result, tries, _, _ = self.commit_tries(
            [urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")), {"exit": 0}])
        self.assertEqual((result, tries), ({"exit": 0}, 2))

    def test_three_tries_at_most_then_it_stops(self):
        from tools.autoflash.phases import PhaseError
        result, tries, recoveries, state = self.commit_tries([PhaseError("timed out")] * 5)
        self.assertIsInstance(result, PhaseError)
        self.assertEqual((tries, recoveries, state.data["captures"]["E3.attempts"]), (3, 2, 3))

    def test_a_rollback_that_failed_or_another_refusal_is_never_tried_again(self):
        import tools.autoflash_cli as autoflash
        for failure in (
                autoflash.CommitFailed("commit did not pass: the plug says: migration_refused: "
                                       "canonical Safeboot read-back differs at 0x31000",
                                       "S60 ROLLBACK FAILED; DO NOT REBOOT OR REMOVE POWER"),
                autoflash.CommitFailed("commit did not pass: the plug says: migration_refused: "
                                       "commit requires old ota_1 to be running", "")):
            result, tries, recoveries, _ = self.commit_tries([failure, {"exit": 0}])
            self.assertIs(result, failure)
            self.assertEqual((tries, recoveries), (1, 0))

    def test_a_commit_the_plug_finished_after_its_window_closed_is_not_tried(self):
        # Seen in the fault suite: the window closed as the commit ran; the plug
        # finished it, and the run carried on at E3 ran its network check on
        # the startup firmware, which cannot say its network - and went back
        # to E1 with no way forward.
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        evidence = Path(directory.name)
        (evidence / "commit-server.log").write_text('[192.168.1.51] "GET /target-table HTTP/1.1" 200 -\n')
        ctx = SimpleNamespace(evidence=evidence, state=RunState.open(evidence),
                              device=mock.Mock(is_safeboot=mock.Mock(return_value=True)))
        with mock.patch.object(autoflash, "commit_once") as commit, \
             mock.patch.object(autoflash, "plug_back_on_wifi"), \
             mock.patch.object(autoflash, "say"):
            result = autoflash.step_commit(ctx)
        commit.assert_not_called()
        self.assertEqual(result, {"exit": 0, "finished_unseen": True})
        # Still on Tasmota, the table fetched but the commit not finished:
        # tried as ever.
        ctx.device.is_safeboot.return_value = False
        with mock.patch.object(autoflash, "commit_once", return_value={"exit": 0}) as commit, \
             mock.patch.object(autoflash, "plug_back_on_wifi"), \
             mock.patch.object(autoflash, "say"):
            self.assertEqual(autoflash.step_commit(ctx), {"exit": 0})
        commit.assert_called_once()

    def test_a_commit_that_finished_unseen_is_not_done_again(self):
        # The cut came after the final table: the plug runs the startup firmware.
        from tools.autoflash.phases import PhaseError
        result, tries, recoveries, _ = self.commit_tries([PhaseError("timed out")], recovered=True)
        self.assertEqual((result, tries, recoveries), ({"exit": 0, "finished_unseen": True}, 1, 1))

    def test_the_interim_layout_is_put_back_before_anything_else(self):
        # A commit stopped part way leaves the settings moved aside, which
        # every check and plan after it does not expect.
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash
        from tools.autoflash import plans as plan_module
        from tools.autoflash.device import DeviceError

        for table, runs in ((plan_module.INTERIM_TABLE_SHA256, True),
                            (plan_module.SONOFF_TABLE_SHA256, False),
                            (DeviceError("no Berry"), False)):
            device = mock.Mock(sha_region=mock.Mock(side_effect=[table]))
            ctx = SimpleNamespace(dry_run=False, device=device)
            with mock.patch.object(autoflash, "run_plan") as run_plan, \
                 mock.patch.object(autoflash, "tell"), mock.patch.object(autoflash, "say"):
                autoflash.sonoff_layout_back(ctx)
            self.assertEqual(run_plan.called, runs, table)
            self.assertEqual(device.wait_for_reboot.called, runs, table)
            if runs:
                self.assertEqual(run_plan.call_args.args[1].name, "sonoff_layout")

    def test_a_plug_asking_to_be_set_up_is_set_up_before_the_commit(self):
        # After a power cut mid-commit, Tasmota may have lost its settings and be
        # waiting on its own network: the run used to stop on "No route to host".
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash

        for on_wifi, setup, expected in ((True, None, None), (False, "tasmota-ABCDEF-1234", "configure"),
                                         (False, None, "locate")):
            ctx = SimpleNamespace(finder=mock.Mock(poll=mock.Mock(return_value="192.0.2.5" if on_wifi else None)),
                                  device=mock.Mock())
            with mock.patch.object(autoflash, "find_ssid", return_value=setup), \
                 mock.patch.object(autoflash, "setup_networks", return_value=("tasmota-ABCDEF-",)), \
                 mock.patch.object(autoflash, "configure_plug_wifi") as configure, \
                 mock.patch.object(autoflash, "locate_plug") as locate, \
                 mock.patch.object(autoflash, "tell"):
                autoflash.plug_back_on_wifi(ctx)
            self.assertEqual(configure.called, expected == "configure", (on_wifi, setup))
            self.assertEqual(locate.called, expected == "locate", (on_wifi, setup))
            if expected == "configure":
                configure.assert_called_once_with(ctx, setup)
            # On the network is not answering yet: switched back on, the plug
            # answers ping before its web server is up.
            ctx.device.wait_reachable.assert_called_once()


class RebootDetectionTests(unittest.TestCase):
    """A restart soon after another must still be seen - the D4 hang."""

    class Plug:
        """Answers like a plug that reboots once, after `polls_until_reboot`."""

        def __init__(self, uptime_before, uptime_after, reboots=True):
            from tools.autoflash.device import Device

            outer = self

            class Fake(Device):
                def reachable(self, timeout=3.0):
                    return True

                def started_at(self):
                    return outer.marker()

                def uptime_seconds(self):
                    return outer.uptime()

            self.device = Fake("10.0.0.5")
            self.polls = 0
            self.reboots = reboots
            self.uptime_before = uptime_before
            self.uptime_after = uptime_after

        def rebooted(self):
            self.polls += 1
            return self.reboots and self.polls > 4

        def marker(self):
            return "2026-10-06T13:26:18#3" if self.rebooted() else "2026-10-06T13:25:45#2"

        def uptime(self):
            return self.uptime_after if self.polls > 4 and self.reboots else self.uptime_before + self.polls

    def wait(self, plug, **kwargs):
        from unittest import mock
        import tools.autoflash.device as device_module
        with mock.patch.object(device_module.time, "sleep"):
            return plug.device.wait_for_reboot(timeout=0.5, settle=0, **kwargs)

    def test_a_restart_with_higher_uptime_than_before_is_seen(self):
        # D1 had just restarted the plug: uptime 15s before the upload, and by
        # the time it was back on Wi-Fi after the next restart, 40s.
        plug = self.Plug(uptime_before=15, uptime_after=40)
        self.wait(plug, started_at="2026-10-06T13:25:45#2", before=15)

    def test_no_restart_is_not_mistaken_for_one_just_because_uptime_is_low(self):
        from tools.autoflash.device import DeviceError
        plug = self.Plug(uptime_before=15, uptime_after=0, reboots=False)
        with self.assertRaises(DeviceError):
            self.wait(plug, started_at="2026-10-06T13:25:45#2")

    class Clock:
        """Time that passes only as the code under test sleeps."""

        def __init__(self):
            self.now = 1000.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    def wait_on_clock(self, device, clock, **kwargs):
        from unittest import mock
        import tools.autoflash.device as device_module
        with mock.patch.object(device_module, "time", clock):
            return device.wait_for_reboot(timeout=60, settle=0, **kwargs)

    def test_a_restart_answered_late_on_a_plug_with_no_clock_is_seen(self):
        # F4's Reset 4 at 10s of uptime, just as Tasmota counted the boot: with
        # no clock both boots read "#1", and the request the restart cut off
        # held the first answer back until the new boot was 14s up.
        from tools.autoflash.device import Device
        clock = self.Clock()
        start, restart = clock.now, clock.now + 3.2

        class Fake(Device):
            def reachable(self, timeout=3.0):
                return not restart <= clock.now < restart + 12

            def started_at(self):
                return "#1"

            def uptime_seconds(self):
                if clock.now < restart:
                    return 10 + int(clock.now - start)
                return int(clock.now - restart)

        self.wait_on_clock(Fake("10.0.0.5"), clock, started_at="#1", before=10)

    def test_a_plug_with_no_clock_that_does_not_restart_is_still_waited_for(self):
        from tools.autoflash.device import Device, DeviceError
        clock = self.Clock()
        start = clock.now

        class Fake(Device):
            def reachable(self, timeout=3.0):
                return True

            def started_at(self):
                return "#1"

            def uptime_seconds(self):
                return 10 + int(clock.now - start)

        with self.assertRaises(DeviceError):
            self.wait_on_clock(Fake("10.0.0.5"), clock, started_at="#1", before=10)

    def test_an_uptime_question_the_restart_cuts_off_is_asked_again(self):
        import urllib.error
        from tools.autoflash.device import Device
        clock = self.Clock()
        answers = iter([urllib.error.URLError("connection reset"), 3])

        class Fake(Device):
            def reachable(self, timeout=3.0):
                return True

            def uptime_seconds(self):
                answer = next(answers)
                if isinstance(answer, Exception):
                    raise answer
                return answer

        self.wait_on_clock(Fake("10.0.0.5"), clock, before=10)


class PhaseDisplayTests(unittest.TestCase):
    """Hundreds of server request lines become a few readable ones."""

    def run_lines(self, phase, blocks, lines, title=None):
        from tools.autoflash.phases import PhaseDisplay
        written, logged = [], []
        display = PhaseDisplay(phase, blocks, write=written.append, log=logged.append, title=title)
        for line in lines:
            display.feed(line)
        display.close()
        return "".join(written), logged

    def test_staging_is_one_progress_line_and_the_result(self):
        lines = ['  server| Phase: stage', '  [10.0.0.5] "GET /berry/stage.be HTTP/1.1" 200 -']
        for n in range(198):
            lines += [f'  [10.0.0.5] "GET /chunk/safeboot/{n} HTTP/1.1" 200 -',
                      f'  [10.0.0.5] "POST /capture/staged/{n} HTTP/1.1" 200 -']
        lines += ['  [10.0.0.5] "POST /report/stage HTTP/1.1" 200 -', "", "STAGE RESULT: PASS",
                  "Evidence: /tmp/x/stage-report.json"]
        written, logged = self.run_lines("stage", 198, lines)
        self.assertEqual(logged, ["    The plug fetched its instructions.", "    STAGE RESULT: PASS"])
        self.assertIn("block 198/198", written)
        self.assertNotIn("\n", written.rstrip("\n"), "progress stays on one line")

    def test_installing_esphome_counts_its_blocks(self):
        lines = ['  [10.0.0.5] "GET /berry/plan.be HTTP/1.1" 200 -']
        for n in range(226):
            lines += [f'  [10.0.0.5] "GET /chunk/esphome/{n} HTTP/1.1" 200 -',
                      f'  [10.0.0.5] "POST /capture/esphome/{n} HTTP/1.1" 200 -']
        written, _ = self.run_lines("plan", 226, lines, title="ESPHome")
        self.assertIn("Copying ESPHome: block 226/226", written)
        self.assertNotIn("startup firmware", written)

    def test_refusals_are_always_shown(self):
        _, logged = self.run_lines("commit", None, ["  REFUSED /chunk/safeboot/9: outside the artifact",
                                                    "REFUSING: could not bind server"])
        self.assertEqual(len(logged), 2)


class RunChoiceTests(unittest.TestCase):
    """An unfinished conversion is offered back, never resumed or dropped silently."""

    def setUp(self):
        from unittest import mock
        import tools.autoflash_cli as autoflash

        self.autoflash = autoflash
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.remembered = []
        for patch in (
            mock.patch.object(autoflash, "run_directories",
                              lambda: sorted(p for p in self.root.iterdir() if p.is_dir())),
            mock.patch.object(autoflash, "read_current", lambda: None),
            mock.patch.object(autoflash, "remember_current", self.remembered.append),
            mock.patch.object(autoflash, "say"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def run_dir(self, name, done):
        state = RunState.open(self.root / name)
        state.set_input("plug_ip", "10.0.0.54")
        for step in done:
            state.mark(step, DONE)
        return name

    def choose(self, answer):
        from unittest import mock
        with mock.patch.object(self.autoflash, "ask_choice", return_value=answer) as menu:
            name = self.autoflash.resolve_run_name(None, False, None, ask=True)
        return name, menu

    def test_with_nothing_unfinished_a_new_run_starts_without_asking(self):
        self.run_dir("done", [step for step, _, _ in self.autoflash.STEPS])
        name, menu = self.choose(0)
        menu.assert_not_called()
        self.assertNotEqual(name, "done")

    def test_continuing_picks_up_the_unfinished_run(self):
        self.run_dir("half", ["A0", "A5", "A1", "A2", "C2"])
        name, menu = self.choose(0)
        self.assertEqual(name, "half")
        options = menu.call_args.args[1]
        self.assertIn("stopped at step C3", options[0])
        self.assertIn("past the point of no return", options[0])

    def test_starting_a_new_plug_leaves_the_unfinished_run_alone(self):
        self.run_dir("half", ["A0", "A5"])
        name, menu = self.choose(1)
        self.assertNotEqual(name, "half")
        self.assertNotIn("past the point of no return", menu.call_args.args[1][0])
        self.assertTrue((self.root / "half").is_dir())

    def test_the_answer_reaches_the_new_runs_log(self):
        # The menu is gone from the screen once answered and comes before the
        # log opens: a part-converted plug left behind must still show in it.
        from unittest import mock
        self.run_dir("past", ["A0", "A5", "A1", "A2", "C2"])
        self.autoflash.RUN_CHOICE.clear()
        self.choose(1)
        with mock.patch.object(self.autoflash, "say") as say:
            self.autoflash.log_run_choice()
        logged = "\n".join(call.args[0] for call in say.call_args_list)
        self.assertIn("a new plug", logged)
        self.assertIn("stopped at step C3", logged)
        self.assertIn("past the point of no return", logged)
        self.assertEqual(self.autoflash.RUN_CHOICE, [])

    def test_other_commands_continue_without_asking(self):
        from unittest import mock
        self.run_dir("half", ["A0"])
        with mock.patch.object(self.autoflash, "ask_choice") as menu:
            self.assertEqual(self.autoflash.resolve_run_name(None, False, None), "half")
        menu.assert_not_called()


class QuitOptionTests(unittest.TestCase):
    """Every kind of prompt offers a way to quit, and quitting stops cleanly."""

    def answer(self, function, *args, reply, **kwargs):
        from unittest import mock
        import tools.autoflash.prompts as prompts
        with mock.patch("builtins.input", return_value=reply), \
             mock.patch.object(prompts, "_emit"):
            return function(*args, **kwargs)

    def test_menus_end_with_a_quit_option(self):
        from unittest import mock
        import tools.autoflash.prompts as prompts
        shown = []
        with mock.patch("builtins.input", return_value="3"), \
             mock.patch.object(prompts, "_emit", side_effect=shown.extend):
            with self.assertRaises(prompts.QuitRun):
                prompts.ask_choice("Which", ["One", "Two"])
        self.assertEqual(shown[-1], "3. Quit - stop here")

    def test_a_menu_choice_still_works(self):
        import tools.autoflash.prompts as prompts
        self.assertEqual(self.answer(prompts.ask_choice, "Which", ["One", "Two"], reply="2"), 1)

    def test_q_quits_yes_no_and_typed_questions(self):
        import tools.autoflash.prompts as prompts
        for function, args in ((prompts.ask_yes_no, ("Go on",)), (prompts.ask_text, ("Name",))):
            with self.subTest(function=function.__name__):
                with self.assertRaises(prompts.QuitRun):
                    self.answer(function, *args, reply="q")

    def test_a_lone_q_at_a_password_prompt_quits(self):
        from unittest import mock
        import tools.autoflash.prompts as prompts
        with mock.patch.object(prompts, "_read_secret", return_value="q"):
            with self.assertRaises(prompts.QuitRun):
                prompts.ask_secret("Password: ")
        with mock.patch.object(prompts, "_read_secret", return_value="quietly-long-password"):
            self.assertEqual(prompts.ask_secret("Password: "), "quietly-long-password")

    def test_quitting_a_standalone_tool_is_a_clean_exit(self):
        import tools.autoflash.prompts as prompts
        self.assertTrue(issubclass(prompts.QuitRun, SystemExit))


class ConsentTests(unittest.TestCase):
    """Only the word itself goes ahead; anything else stops."""

    def consent(self, reply):
        from unittest import mock
        import tools.autoflash_cli as autoflash
        import tools.autoflash.prompts as prompts
        with mock.patch("builtins.input", return_value=reply), \
             mock.patch.object(prompts, "_emit"), mock.patch.object(autoflash, "say"):
            return autoflash.require_word(autoflash.CONSENT_WORD)

    def test_proceed_in_any_case_goes_ahead(self):
        for reply in ("PROCEED", "proceed", " Proceed "):
            with self.subTest(reply=reply):
                self.assertTrue(self.consent(reply))

    def test_anything_else_stops(self):
        for reply in ("y", "yes", "proceed now", "procede", ""):
            with self.subTest(reply=reply):
                self.assertFalse(self.consent(reply))


class StartChoiceTests(unittest.TestCase):
    """The first steps ask only what matters: which Wi-Fi, and look again if no plug."""

    @staticmethod
    def locks_in_place():
        """A5 finding both safety locks in place, as this checkout's are - without
        looking in its captures, where the runs that lift them say so."""
        import contextlib
        import tools.autoflash_cli as autoflash
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(autoflash.safety_locks, "restore_left", return_value=[]))
        stack.enter_context(mock.patch.object(autoflash.safety_locks, "in_place", return_value=True))
        return stack

    def ctx(self):
        from types import SimpleNamespace
        import tools.autoflash_cli as autoflash
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        return SimpleNamespace(state=RunState.open(root), evidence=root / "live",
                               manifest_path=(autoflash.ROOT / autoflash.DEFAULT_MANIFEST).resolve(),
                               wifi=lambda: ("Home-2G", "password"))

    def test_a5_asks_nothing_about_wifi(self):
        # The details are read off the plug at E1, so A5 has nothing to ask.
        from unittest import mock
        import tools.autoflash_cli as autoflash
        with mock.patch.object(autoflash, "validate_bundle", return_value={}), \
             mock.patch.object(autoflash.hostsys, "check_sha256sums", return_value=[]), \
             mock.patch.object(autoflash, "ask_choice") as menu, \
             mock.patch.object(autoflash, "make_bundle") as rebuild, \
             self.locks_in_place(), mock.patch.object(autoflash, "say"):
            autoflash.step_check(self.ctx())
        menu.assert_not_called()
        rebuild.assert_not_called()

    def test_a5_builds_a_missing_bundle_without_asking_for_wifi(self):
        # A first run: the bundle is built with no Wi-Fi; E1 adds it, read off the plug.
        from unittest import mock
        import tools.autoflash_cli as autoflash
        with mock.patch.object(autoflash, "validate_bundle",
                               side_effect=[ValueError("no manifest"), {}]), \
             mock.patch.object(autoflash, "build_bundle") as build, \
             mock.patch.object(autoflash.hostsys, "check_sha256sums", return_value=[]), \
             mock.patch.object(autoflash, "ask_credentials") as credentials, \
             mock.patch.object(autoflash, "ask_secret") as secret, \
             self.locks_in_place(), \
             mock.patch.object(autoflash, "section"), mock.patch.object(autoflash, "say"):
            autoflash.step_check(self.ctx())
        build.assert_called_once()
        self.assertNotIn("ssid", build.call_args.kwargs)
        self.assertEqual(build.call_args.args, ())
        credentials.assert_not_called()
        secret.assert_not_called()

    def test_without_ewelink_no_plug_found_offers_to_look_again(self):
        from unittest import mock
        import tools.autoflash_cli as autoflash
        ctx = self.ctx()
        ctx.account_devices = lambda: None  # eWeLink could not be reached
        # Filed under its MAC in a scratch folder, not this checkout's captures.
        with mock.patch.object(autoflash.plugs, "PLUGS", ctx.evidence.parent / "plugs"), \
             mock.patch.object(autoflash.plugs, "hold"), \
             mock.patch.object(autoflash, "find_ewelink_plugs",
                               side_effect=[[], [("10.0.0.5", "eWeLink device abc")]]), \
             mock.patch.object(autoflash, "ask_choice", return_value=0), \
             mock.patch.object(autoflash.hostsys, "mac_of", return_value="aa:bb:cc:dd:ee:ff"), \
             mock.patch.object(autoflash, "section"), mock.patch.object(autoflash, "say"):
            result = autoflash.step_pair(ctx)
        self.assertEqual(result["plug_ip"], "10.0.0.5")

    def test_a_typed_address_leaves_the_screen_with_its_question(self):
        # Asked after the "did not answer" menu, it is a block of its own, taken
        # off once answered - not left under the stage line.
        from unittest import mock
        import tools.autoflash_cli as autoflash
        ctx = self.ctx()
        ctx.account_devices = lambda: [{"deviceid": "1000000001", "productModel": "S60TPG",
                                        "name": "Sonoff S60", "online": True, "params": {}}]
        screen = mock.Mock()
        with mock.patch.object(autoflash, "sight_plugs",
                               side_effect=lambda plugs: [autoflash.Sighting(p, None, False) for p in plugs]), \
             mock.patch.object(autoflash, "ask_choice", return_value=2), \
             mock.patch.object(autoflash, "ask_ipv4", screen.ask_ipv4), \
             mock.patch.object(autoflash.output, "transient_begin", screen.begin), \
             mock.patch.object(autoflash.output, "transient_end", screen.end), \
             mock.patch.object(autoflash.hostsys, "security_software_note", return_value=[]), \
             mock.patch.object(autoflash.hostsys, "mac_of", return_value=None), \
             mock.patch.object(autoflash, "section"), mock.patch.object(autoflash, "say"):
            screen.ask_ipv4.return_value = "192.168.60.50"
            result = autoflash.step_pair(ctx)
        self.assertEqual(result["plug_ip"], "192.168.60.50")
        calls = [name for name, _, _ in screen.mock_calls]
        self.assertEqual(calls[-3:], ["begin", "ask_ipv4", "end"])


class RemoveRunTests(RunChoiceTests):
    """Unfinished runs can be removed from the menu, with care for half-converted plugs."""

    def menu(self, answers, confirm=True):
        from unittest import mock
        with mock.patch.object(self.autoflash, "ask_choice", side_effect=answers) as menu, \
             mock.patch.object(self.autoflash, "ask_yes_no", return_value=confirm), \
             mock.patch.object(self.autoflash, "section") as section:
            name = self.autoflash.resolve_run_name(None, False, None, ask=True)
        return name, menu, section

    def test_removing_the_only_unfinished_run_then_starts_a_new_one(self):
        self.run_dir("half", ["A0", "A5"])
        name, _, section = self.menu([2])          # 1 continue, 2 new, 3 remove
        self.assertFalse((self.root / "half").exists())
        self.assertNotEqual(name, "half")
        section.assert_not_called()                # not past the point of no return

    def test_a_half_converted_plug_is_warned_about_before_removal(self):
        self.run_dir("past", ["A0", "A5", "A1", "A2", "C2"])
        _, _, section = self.menu([2])
        self.assertIn("part-converted", section.call_args.args[0])

    def test_declining_keeps_the_run_and_shows_the_menu_again(self):
        self.run_dir("half", ["A0"])
        name, menu, _ = self.menu([2, 0], confirm=False)
        self.assertTrue((self.root / "half").exists())
        self.assertEqual(name, "half")
        self.assertEqual(menu.call_count, 2)

    def test_with_several_runs_it_asks_which_to_remove(self):
        self.run_dir("first", ["A0"])
        self.run_dir("second", ["A0", "A5"])
        # 1-2 continue, 3 new, 4 remove; then pick the second; then start new
        name, menu, _ = self.menu([3, 1, 1])
        remaining = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(len(remaining), 1)
        self.assertEqual(menu.call_args_list[1].args[0], "Which run should be removed")
        self.assertNotIn(name, ("first", "second"))


class BridgeOnHomeNetworkTests(unittest.TestCase):
    """Bridge v4 joins the home network; D1 uploads there instead of asking."""

    def find(self, addresses, pages, timeout=10.0):
        from types import SimpleNamespace
        from unittest import mock
        from tools import autoflash_cli
        from tools.autoflash import setup_network

        polls = iter(addresses)
        ctx = SimpleNamespace(finder=SimpleNamespace(poll=lambda: next(polls, None)))
        clock = iter(range(0, 10_000, 2))
        with mock.patch.object(setup_network, "page", side_effect=lambda host, **_: pages.get(host)), \
             mock.patch.object(autoflash_cli.time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(autoflash_cli.time, "sleep"):
            return autoflash_cli.bridge_on_home_network(ctx, timeout=timeout)

    def test_the_bridge_is_found_where_the_plug_was(self):
        bridge = "<title>S60 OTA Bridge</title>"
        self.assertEqual(self.find([None, "192.168.1.51"], {"192.168.1.51": bridge}), "192.168.1.51")

    def test_stock_firmware_at_that_address_is_not_the_bridge(self):
        # Before the bridge starts, the plug answers there with no web page.
        self.assertIsNone(self.find(["192.168.1.51"] * 20, {}))

    def test_no_plug_on_the_network_falls_back_to_joining(self):
        self.assertIsNone(self.find([None] * 20, {}))


class BackOnSonoffTests(unittest.TestCase):
    """A power cut before the installer has done its work puts the plug back on
    Sonoff's firmware, by design: the installer is sent again, not waited for."""

    def reach(self, running, sends=(None,), found=(None, "192.168.1.51"), waits=(None,)):
        from types import SimpleNamespace
        from unittest import mock
        from tools import autoflash_cli
        from tools.autoflash import setup_network

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        state.set_input("plug_ip", "192.168.1.51")
        state.set_input("stock_version", "1.2.0")  # as eWeLink gave it at A2
        ctx = SimpleNamespace(state=state, finder=SimpleNamespace(poll=lambda: "192.168.1.51"))
        found = iter(found)
        outcomes = iter(sends)

        def send(_):
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return {"seconds": 20}

        with mock.patch.object(setup_network, "page", return_value=None), \
             mock.patch.object(autoflash_cli, "bridge_on_home_network", side_effect=lambda *a, **k: next(found, None)), \
             mock.patch.object(autoflash_cli.detect, "identify",
                               **({"side_effect": running} if isinstance(running, list) else {"return_value": running})), \
             mock.patch.object(autoflash_cli, "step_bridge", side_effect=send) as bridge, \
             mock.patch.object(autoflash_cli, "wait_for_bridge", side_effect=list(waits)) as wait, \
             mock.patch.object(autoflash_cli, "find_ssid", return_value=None), \
             mock.patch.object(autoflash_cli.hostsys, "copy_to_clipboard", return_value=False), \
             mock.patch.object(autoflash_cli, "section"), mock.patch.object(autoflash_cli, "tell"), \
             mock.patch.object(autoflash_cli, "say"):
            try:
                address = autoflash_cli.reach_bridge(ctx)
            except autoflash_cli.Abort as exc:
                address = exc
        return address, bridge.call_count, wait.call_count, state

    def test_the_installer_is_sent_again_and_found(self):
        address, sent, waited, state = self.reach("sonoff")
        self.assertEqual((address, sent, waited), ("192.168.1.51", 1, 0))
        self.assertEqual(state.status("C2"), "done")

    def test_a_send_cut_off_is_tried_again_three_times_in_all(self):
        from tools import autoflash_cli
        failed = autoflash_cli.Abort("bridge delivery stopped part way")
        # Two cut off, the third gets there.
        address, sent, _, _ = self.reach("sonoff", sends=(failed, failed, None),
                                         found=(None, "192.168.1.51"))
        self.assertEqual((address, sent), ("192.168.1.51", 3))
        # Three cut off: it stops, with the reason, rather than go on for ever.
        address, sent, _, _ = self.reach("sonoff", sends=(failed,) * 5)
        self.assertIs(address, failed)
        self.assertEqual(sent, autoflash_cli.BRIDGE_SENDS)

    def test_a_failed_send_is_followed_by_the_next_though_the_plug_is_quiet(self):
        # Seen in the fault suite: a send refused (the plug offline in eWeLink),
        # then the plug not answering here for a moment - and the run asked the
        # person to join an installer network that was never raised.
        from tools import autoflash_cli
        failed = autoflash_cli.Abort("eWeLink does not see the plug")
        address, sent, waited, _ = self.reach(["sonoff"] + [None] * 10, sends=(failed, None),
                                              found=(None, "192.168.1.51"))
        self.assertEqual((address, sent, waited), ("192.168.1.51", 2, 0))

    def test_back_on_sonoff_after_every_send_stops_asking_after_three(self):
        # Sent, but the plug went back each time (its power keeps going): after
        # three it falls through to the usual wait, which says what is wrong.
        address, sent, waited, _ = self.reach("sonoff", sends=(None,) * 5, found=(None,) * 10)
        self.assertEqual((sent, waited), (3, 1))

    def test_back_on_sonoff_while_waiting_sends_it_again(self):
        # A power cut during the backup, once the run was waiting for the
        # installer's own network: the installer is gone, so it is sent again.
        from tools import autoflash_cli
        address, sent, waited, state = self.reach([None, "sonoff"], waits=(autoflash_cli.BACK_ON_SONOFF,),
                                                  found=(None, "192.168.1.51"))
        self.assertEqual((address, sent, waited), ("192.168.1.51", 1, 1))
        self.assertEqual(state.status("C2"), "done")

    def test_a_run_started_on_the_installer_says_to_start_again(self):
        # It has none of eWeLink's details, so cannot send the installer again.
        from unittest import mock
        import tools.autoflash_cli as autoflash
        with mock.patch.object(autoflash.RunState, "input",
                               side_effect=lambda self, key: {"plug_ip": "192.168.1.51"}.get(key),
                               autospec=True):
            address, sent, _, _ = self.reach("sonoff")
        self.assertIsInstance(address, autoflash.Abort)
        self.assertIn("--new", str(address))
        self.assertEqual(sent, 0)

    def test_anything_else_is_waited_for_as_before(self):
        address, sent, waited, _ = self.reach(None)
        self.assertEqual((sent, waited), (0, 1))


class PastTheCommitTests(unittest.TestCase):
    """A plug left on the startup firmware is on Tasmota's layout if this
    computer's run of it got past the commit - Sonoff's otherwise."""

    def source(self, earlier, safeboot=False):
        import json
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        runs = []
        for index, (done, report, *stopped) in enumerate(earlier):
            run = root / f"run{index}"
            (run / "live").mkdir(parents=True)
            state = RunState.open(run)
            for step in done:
                state.mark(step, "done")
            if stopped:  # stopped as E3 ran: what its server saw of the plug
                state.mark("E3", ATTEMPTED)
                (run / "live" / "commit-server.log").write_text(stopped[0])
            if report:
                (run / "live" / "commit-report.json").write_text(json.dumps({"phase": "commit", "status": report}))
            runs.append(run)
        now = root / "now"
        now.mkdir()
        ctx = SimpleNamespace(dir=now, state=RunState.open(now), evidence=now / "live",
                              device=SimpleNamespace(is_safeboot=lambda: safeboot))
        self.evidence = ctx.evidence
        ctx.state.set_input("plug_mac", "12:34:56:ab:cd:ef")
        with mock.patch.object(autoflash, "run_directories", return_value=runs), \
             mock.patch.object(autoflash.plugs, "mac_of_run", return_value="12:34:56:ab:cd:ef"), \
             mock.patch.object(autoflash, "say"):
            return autoflash.past_the_commit(ctx, "midway")

    def test_a_run_that_committed_and_did_not_finish(self):
        self.assertEqual(self.source([(["E1", "E2", "E3"], None)]), "committed")
        # Stopped as the plug restarted: the report says so, the record not yet.
        self.assertEqual(self.source([(["E1", "E2"], "PASS")]), "committed")
        # And G1 has that run's record of the startup firmware it put on.
        self.assertTrue((self.evidence / "commit-report.json").is_file())

    def test_otherwise_part_way_as_before(self):
        self.assertEqual(self.source([(["X1", "X2"], None)]), "midway")       # the ESPHome route
        self.assertEqual(self.source([(["E3", "F2", "F4", "G1", "H1"], None)]), "midway")  # finished
        self.assertEqual(self.source([]), "midway")                           # no record of it
        # The latest run decides: one past the commit, then one that started over.
        self.assertEqual(self.source([(["E3"], None), (["X1"], None)]), "midway")


    def test_a_commit_the_plug_finished_after_its_run_was_stopped(self):
        # Seen in the fault suite: Ctrl-C a second after the plug fetched the
        # new table; it finished alone, and a --new run took it for the
        # ESPHome route's, whose steps need Sonoff's layout.
        fetched = "GET /target-table HTTP/1.1 200\n"
        self.assertEqual(self.source([(["E1", "E2"], None, fetched)], safeboot=True), "committed")
        self.assertEqual(self.source([(["E1", "E2"], None, "")], safeboot=True), "midway")      # never fetched
        self.assertEqual(self.source([(["E1", "E2"], None, fetched)], safeboot=False), "midway")  # not finished


class WaitForBridgeTests(unittest.TestCase):
    """While waiting for the bridge's own network, a late join of the home one
    must still be noticed: the bridge then never raises its own."""

    def wait(self, addresses, pages, timeout=60.0, gone_back=None, answering=True, told=None):
        from types import SimpleNamespace
        from unittest import mock
        from tools import autoflash_cli
        from tools.autoflash import setup_network

        polls = iter(addresses)
        ctx = SimpleNamespace(finder=SimpleNamespace(poll=lambda: next(polls, None)),
                              state=SimpleNamespace(input=lambda key: "192.168.1.51"))
        clock = iter(range(0, 100_000, 2))
        with mock.patch.object(setup_network, "page", side_effect=lambda host, **_: pages.get(host)), \
             mock.patch.object(autoflash_cli.time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(autoflash_cli.time, "sleep"), \
             mock.patch.object(autoflash_cli.hostsys, "ping_ok", return_value=answering), \
             mock.patch.object(autoflash_cli, "tell", side_effect=lambda line: (told if told is not None else []).append(line)), \
             mock.patch.object(autoflash_cli, "say"):
            return autoflash_cli.wait_for_bridge(ctx, "S60-OTA-Bridge-A04D", timeout=timeout,
                                                 gone_back=gone_back)

    def test_a_late_join_of_the_home_network_is_taken(self):
        bridge = "<title>S60 OTA Bridge</title>"
        self.assertEqual(
            self.wait([None, None, "192.168.1.51"], {"192.168.1.51": bridge}), "192.168.1.51"
        )

    def test_joining_the_bridge_network_still_works(self):
        from tools.autoflash import setup_network
        bridge = "<title>S60 OTA Bridge</title>"
        self.assertIsNone(self.wait([None] * 50, {setup_network.SETUP_HOST: bridge}))

    def test_a_plug_gone_back_to_sonoffs_firmware_ends_the_wait(self):
        from tools import autoflash_cli
        self.assertEqual(self.wait([None] * 100, {}, timeout=600.0, gone_back=lambda: True),
                         autoflash_cli.BACK_ON_SONOFF)

    def test_nothing_at_the_plugs_address_is_told_to_switch_it_off_and_on(self):
        # Sonoff's firmware, its Wi-Fi gone while it started, stays off it.
        from tools import autoflash_cli
        told = []
        with self.assertRaises(autoflash_cli.Abort):
            self.wait([None] * 500, {}, timeout=400.0, answering=False, told=told)
        self.assertIn("switch it off and on", " ".join(told))
        told = []
        with self.assertRaises(autoflash_cli.Abort):
            self.wait([None] * 500, {}, timeout=400.0, answering=True, told=told)
        self.assertNotIn("switch it off and on", " ".join(told))  # something is there

    def test_neither_in_time_stops_with_a_reason(self):
        from tools import autoflash_cli
        with self.assertRaisesRegex(autoflash_cli.Abort, "did not appear on your network"):
            self.wait([None] * 50, {}, timeout=20.0)


class TasmotaWifiPresetTests(unittest.TestCase):
    """D1 gives Tasmota the Wi-Fi through the bridge when it safely can."""

    def setUp(self):
        from types import SimpleNamespace
        from tools.autoflash import tasmota_settings as ts
        self.ts = ts
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.inputs = {}
        self.ctx = SimpleNamespace(state=SimpleNamespace(set_input=self.inputs.__setitem__,
                                                         input=self.inputs.get))

    def run_preset(self, template, send_error=None):
        from unittest import mock
        from tools import autoflash_cli
        from tools.autoflash import setup_network
        sent = []

        def send(data, **kwargs):
            if send_error:
                raise setup_network.SetupError(send_error)
            sent.append(data)
            return "saved"

        with mock.patch.object(self.ts, "load_template",
                               return_value=(template, "" if template else "none captured")), \
             mock.patch.object(setup_network, "send_tasmota_settings", side_effect=send), \
             mock.patch.object(autoflash_cli, "say"):
            return autoflash_cli.give_tasmota_the_wifi(self.ctx, "192.168.1.51"), sent

    def test_with_a_template_the_bridge_gets_it(self):
        self.assertEqual(self.run_preset(b"T" * 4096), (True, [b"T" * 4096]))
        self.assertTrue(self.inputs.get("tasmota_wifi_preset"))  # outlasts a cut-off upload

    def through_the_bridges_network(self, *, preset, at_home):
        """D1 sent through the bridge's own network: what follows."""
        from types import SimpleNamespace
        from unittest import mock
        from tools import autoflash_cli
        from tools.autoflash import setup_network
        self.inputs.update(tasmota_wifi_preset=preset)
        ctx = SimpleNamespace(state=self.ctx.state)
        clock = iter(range(0, 10_000, 5))
        with mock.patch.object(autoflash_cli, "find_ssid", return_value=None), \
             mock.patch.object(autoflash_cli, "our_setup_networks", return_value=("Sonoff-Setup-",)), \
             mock.patch.object(autoflash_cli, "setup_networks", return_value=("tasmota-",)), \
             mock.patch.object(autoflash_cli, "reach_bridge", return_value=setup_network.SETUP_HOST), \
             mock.patch.object(autoflash_cli, "upload_tasmota"), \
             mock.patch.object(autoflash_cli, "tasmota_at_home",
                               return_value="192.168.1.51" if at_home else None), \
             mock.patch.object(autoflash_cli, "configure_plug_wifi", return_value="192.168.1.52") as by_hand, \
             mock.patch.object(autoflash_cli, "section") as shown, \
             mock.patch.object(autoflash_cli.time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(autoflash_cli.time, "sleep"), \
             mock.patch.object(autoflash_cli.output, "progress"), \
             mock.patch.object(autoflash_cli, "say"):
            result = autoflash_cli.step_berry_tasmota(ctx)
        return result, by_hand.called, [call.args[0] for call in shown.call_args_list]

    def test_tasmota_given_the_wifi_before_a_cut_off_joins_by_itself(self):
        # Seen in the fault suite: the bridge saved Tasmota's settings, the
        # upload was cut off, and the next went through the bridge's own
        # network - Tasmota then joined the home Wi-Fi with them, and the run
        # waited on a setup network it never raised.
        result, by_hand, shown = self.through_the_bridges_network(preset=True, at_home=True)
        self.assertEqual(result["plug_ip"], "192.168.1.51")
        self.assertFalse(by_hand)
        self.assertIn("Back to your usual Wi-Fi", shown)

    def test_tasmota_found_at_home_is_not_asked_for_its_setup_network(self):
        # Without the record - settings saved by an earlier run - it is seen anyway.
        result, by_hand, _ = self.through_the_bridges_network(preset=False, at_home=True)
        self.assertEqual(result["plug_ip"], "192.168.1.51")
        self.assertFalse(by_hand)

    def test_tasmota_on_neither_is_set_up_through_its_network(self):
        result, by_hand, _ = self.through_the_bridges_network(preset=False, at_home=False)
        self.assertTrue(by_hand)
        self.assertEqual(result["plug_ip"], "192.168.1.52")

    def test_without_a_template_tasmota_is_set_up_by_hand(self):
        self.assertEqual(self.run_preset(None), (False, []))

    def test_a_bridge_that_refuses_means_by_hand(self):
        self.assertEqual(self.run_preset(b"T" * 4096, send_error="older than v5")[0], False)

    def test_a_template_is_not_used_with_other_berry_firmware(self):
        template, reason = self.ts.load_template(self.dir.name, "0" * 64)
        self.assertIsNone(template)
        self.assertIn("Berry Tasmota", reason)

    def test_the_bridge_page_is_not_mistaken_for_tasmota(self):
        from types import SimpleNamespace
        from unittest import mock
        from tools import autoflash_cli
        from tools.autoflash import setup_network
        pages = iter(["<title>S60 OTA Bridge</title>", None, "<title>Tasmota</title> tasmota"])
        state = SimpleNamespace(set_input=lambda *a: None, input=lambda *a: None)
        ctx = SimpleNamespace(finder=SimpleNamespace(poll=lambda: "192.168.1.51"),
                              set_plug_ip=lambda ip: None, state=state)
        clock = iter(range(0, 10_000, 3))
        with mock.patch.object(setup_network, "page", side_effect=lambda *a, **k: next(pages)), \
             mock.patch.object(autoflash_cli.time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(autoflash_cli.time, "sleep"), \
             mock.patch.object(autoflash_cli, "Device", side_effect=OSError), \
             mock.patch.object(autoflash_cli, "say"):
            self.assertEqual(autoflash_cli.tasmota_on_home_network(ctx), "192.168.1.51")


def efuse_with_mac(mac):
    """eFuse registers whose base MAC is `mac`, as the chip stores it."""
    data = bytearray(1024)
    data[0x18:0x1E] = bytes.fromhex(mac.replace(":", ""))[::-1]
    return bytes(data)


class PlugLayoutTests(unittest.TestCase):
    """Each plug's records live under its MAC (tools/autoflash/plugs.py)."""

    def setUp(self):
        from tools.autoflash import plugs
        self.plugs = plugs
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patch = mock.patch.object(plugs, "PLUGS", Path(directory.name) / "plugs")
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_mac_names_a_folder_windows_can_hold(self):
        self.assertEqual(self.plugs.folder_name("AA:BB:CC:DD:EE:0F"), "aa-bb-cc-dd-ee-0f")
        self.assertEqual(self.plugs.folder_name("aa-bb-cc-dd-ee-0f"), "aa-bb-cc-dd-ee-0f")
        with self.assertRaises(ValueError):
            self.plugs.folder_name("../etc")

    def test_the_mac_is_read_from_the_chips_efuses(self):
        self.assertEqual(self.plugs.mac_from_efuse(efuse_with_mac("58:cf:79:01:02:03")),
                         "58:cf:79:01:02:03")

    def test_the_first_backup_is_kept_read_only_and_never_replaced(self):
        efuse = efuse_with_mac("58:cf:79:01:02:03")
        first, saved = self.plugs.save_backup(b"first", efuse, {}, "1.2.0")
        self.assertTrue(saved)
        self.assertEqual(first, self.plugs.backup_dir("58:cf:79:01:02:03"))
        again, saved = self.plugs.save_backup(b"second", efuse, {}, "1.3.0")
        self.assertFalse(saved)
        self.assertEqual((again / "flash.bin").read_bytes(), b"first")
        if sys.platform != "win32":
            self.assertEqual((first / "flash.bin").stat().st_mode & 0o777, 0o400)
        self.assertIn("58:cf:79:01:02:03", (first / "README.txt").read_text())

    def test_backed_up_plugs_are_listed_by_mac(self):
        self.plugs.save_backup(b"F", efuse_with_mac("58:cf:79:01:02:03"), {}, "1.2.0")
        self.plugs.remember_plug("58:cf:79:01:02:03", ewelink_name="Kettle")
        (found,) = self.plugs.backups()
        self.assertEqual(found.mac, "58:cf:79:01:02:03")
        self.assertTrue(found.describe().startswith("58:cf:79:01:02:03"))
        self.assertIn("Kettle", found.describe())
        self.assertIn("1.2.0", found.describe())

    def test_a_run_knows_its_plug_from_where_it_is_kept(self):
        run = self.plugs.runs_dir("58:cf:79:01:02:03") / "20261013-031500"
        self.assertEqual(self.plugs.mac_of_run(run), "58:cf:79:01:02:03")
        self.assertIsNone(self.plugs.mac_of_run(self.plugs.runs_dir(None) / "x"))


class RunFilingTests(unittest.TestCase):
    """A run moves under its plug's MAC once found; older runs are adopted."""

    def setUp(self):
        from tools.autoflash import plugs
        import tools.autoflash_cli as autoflash
        self.plugs, self.autoflash = plugs, autoflash
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.captures = Path(directory.name)
        for patch in (mock.patch.object(plugs, "CAPTURES", self.captures),
                      mock.patch.object(plugs, "PLUGS", self.captures / "plugs"),
                      mock.patch.object(autoflash, "CURRENT_POINTER", self.captures / ".current"),
                      mock.patch.object(autoflash, "say")):
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_new_run_waits_unidentified_then_moves_under_the_mac(self):
        from types import SimpleNamespace
        name = self.autoflash.new_run_name(None)
        self.assertTrue(name.startswith("plugs/unidentified/runs/"))
        ctx = self.autoflash.Context(SimpleNamespace(
            run=name, manifest=self.autoflash.DEFAULT_MANIFEST, dry_run=False, plug_ip=None,
            email=None, web_user=None, web_password=None, esphome_ota_password=None))
        ctx.state.set_input("plug_ip", "10.0.0.5")
        ctx.file_under_plug("58:CF:79:01:02:03")
        self.assertEqual(ctx.dir.parent, self.captures / "plugs/58-cf-79-01-02-03/runs")
        self.assertEqual(ctx.state.input("plug_ip"), "10.0.0.5")
        ctx.state.set_input("plug_mac", "58:cf:79:01:02:03")  # saved where it now is
        self.assertTrue((ctx.dir / "autoflash-state.json").is_file())
        self.assertFalse((self.captures / name).exists())
        self.assertEqual(self.autoflash.read_current(), ctx.plug)

    def test_firmware_passwords_come_from_the_flags_then_the_environment(self):
        from types import SimpleNamespace
        name = self.autoflash.new_run_name(None)

        def args(**over):
            base = dict(run=name, manifest=self.autoflash.DEFAULT_MANIFEST, dry_run=False,
                        plug_ip=None, email=None, web_user=None, web_password=None,
                        esphome_ota_password=None)
            base.update(over)
            return SimpleNamespace(**base)
        # The flags win, each kept out of the log under its label.
        private = mock.Mock()
        with mock.patch.object(self.autoflash.output, "private", private):
            ctx = self.autoflash.Context(args(web_password="tas-pw", esphome_ota_password="esp-pw"))
        self.assertEqual((ctx.web_password, ctx.esphome_password), ("tas-pw", "esp-pw"))
        private.assert_any_call("tas-pw", "tasmota-password")
        private.assert_any_call("esp-pw", "esphome-password")
        # With no flag, the environment (credentials) is the fallback.
        with mock.patch.object(self.autoflash.credentials, "get",
                               side_effect={"AUTOFLASH_TASMOTA_PASSWORD": "env-tas",
                                            "AUTOFLASH_ESPHOME_OTA_PASSWORD": "env-esp"}.get):
            ctx = self.autoflash.Context(args())
        self.assertEqual((ctx.web_password, ctx.esphome_password), ("env-tas", "env-esp"))

    def test_older_runs_with_a_mac_are_adopted(self):
        old = RunState.open(self.captures / "conversion-20261011-1431")
        old.set_input("plug_mac", "58:cf:79:01:02:03")
        RunState.open(self.captures / "conversion-unknown").save()
        self.autoflash.remember_current("conversion-20261011-1431")
        self.autoflash.adopt_older_runs()
        moved = self.captures / "plugs/58-cf-79-01-02-03/runs/conversion-20261011-1431"
        self.assertTrue((moved / "autoflash-state.json").is_file())
        self.assertTrue((self.captures / "conversion-unknown").is_dir())
        self.assertEqual(self.autoflash.read_current(), self.autoflash.run_name(moved))
        self.assertEqual(len(self.autoflash.run_directories()), 2)


class StockBackupTests(unittest.TestCase):
    """D1 keeps the plug's whole flash through the bridge before writing Tasmota."""

    REPORT = {"size": 8, "sha256": "0" * 64, "partition_table_md5_valid": True,
              "partitions": [], "otadata_selects": "ota_0"}
    MAC = "58:cf:79:01:02:03"

    def setUp(self):
        from types import SimpleNamespace
        from tools.autoflash import plugs
        self.plugs = plugs
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patch = mock.patch.object(plugs, "PLUGS", Path(self.dir.name))
        patch.start()
        self.addCleanup(patch.stop)
        self.inputs = {"stock_version": "1.2.0", "plug_mac": self.MAC}
        state = SimpleNamespace(input=lambda key, default=None: self.inputs.get(key, default),
                                set_input=self.inputs.__setitem__)
        self.ctx = SimpleNamespace(state=state, args=SimpleNamespace(no_backup=False))
        self.dump = (b"F" * 8, efuse_with_mac(self.MAC), dict(self.REPORT))

    def back_up(self, reads, answers=(1,)):
        """Run the backup with each read in turn returning or raising `reads`,
        and the failure menu answered with `answers` (0 again, 1 without)."""
        from tools import autoflash_cli
        from tools.autoflash.prompts import QuitRun
        outcomes, replies = iter(reads), iter(answers)
        asked = []

        def read(host):
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        def ask(question, options, help_lines=()):
            asked.append(options)
            reply = next(replies)
            if reply == "quit":
                raise QuitRun(130)
            return reply

        with mock.patch.object(autoflash_cli, "read_dump", side_effect=read), \
             mock.patch.object(autoflash_cli, "ask_choice", side_effect=ask), \
             mock.patch.object(autoflash_cli, "section"), \
             mock.patch.object(autoflash_cli, "say"):
            autoflash_cli.back_up_stock(self.ctx, "192.168.1.51")
        return asked

    def test_the_backup_is_kept_under_the_plugs_mac(self):
        self.assertEqual(self.back_up([self.dump]), [])
        saved = self.plugs.backup_dir(self.MAC)
        self.assertEqual((saved / "flash.bin").read_bytes(), b"F" * 8)
        self.assertIn("write_flash 0x0 flash.bin", (saved / "README.txt").read_text())
        self.assertIn("sonoff-backup", self.inputs["stock_backup"])
        self.assertEqual(self.plugs.read_plug(self.MAC)["stock_version"], "1.2.0")

    def test_a_plug_backed_up_before_is_not_read_again(self):
        self.plugs.save_backup(b"original", efuse_with_mac(self.MAC), {}, "1.2.0")
        self.assertEqual(self.back_up([]), [])
        self.assertIn("sonoff-backup", self.inputs["stock_backup"])

    def test_a_resumed_run_does_not_read_again(self):
        self.inputs["stock_backup"] = "captures/plugs/x/sonoff-backup"
        self.assertEqual(self.back_up([]), [])

    def test_no_backup_when_asked_for_none(self):
        self.ctx.args.no_backup = True
        self.back_up([])
        self.assertNotIn("stock_backup", self.inputs)

    def test_one_failed_read_is_retried(self):
        self.back_up([OSError("timed out"), self.dump])
        self.assertIn("stock_backup", self.inputs)

    def test_a_bridge_that_cannot_read_flash_means_no_backup(self):
        import urllib.error
        missing = urllib.error.HTTPError("http://x/flash", 404, "Not Found", {}, None)
        self.assertEqual(self.back_up([missing]), [])
        self.assertNotIn("stock_backup", self.inputs)

    def test_after_two_failures_the_person_may_carry_on_without(self):
        (options,) = self.back_up([OSError("a"), OSError("b")])
        self.assertEqual(options, ["Try again", "Carry on without a backup"])
        self.assertNotIn("stock_backup", self.inputs)

    def test_or_try_again(self):
        self.back_up([OSError("a"), OSError("b"), self.dump], answers=(0,))
        self.assertIn("stock_backup", self.inputs)

    def test_an_unattended_run_carries_on_without(self):
        from tools.autoflash import prompts
        with mock.patch.object(prompts, "UNATTENDED", True), \
             mock.patch.object(prompts, "answered_unattended"):
            self.assertEqual(self.back_up([OSError("a"), OSError("b")], answers=()), [])
        self.assertNotIn("stock_backup", self.inputs)

    def test_a_simulated_failure_fails_one_round_only(self):
        from tools import autoflash_cli
        with mock.patch.object(autoflash_cli, "SIMULATED_BACKUP_FAILURES", 2):
            asked = self.back_up([self.dump], answers=(0,))
        self.assertEqual(len(asked), 1)
        self.assertIn("stock_backup", self.inputs)

    def stage(self, done=()):
        """Run the backup stage, C3, with the bridge reached at 192.168.1.51."""
        from tools import autoflash_cli
        self.ctx.state.status = lambda step: DONE if step in done else "pending"
        with mock.patch.object(autoflash_cli, "reach_bridge", return_value="192.168.1.51") as reach, \
             mock.patch.object(autoflash_cli, "read_dump", return_value=self.dump) as read, \
             mock.patch.object(autoflash_cli, "say"):
            result = autoflash_cli.step_backup(self.ctx)
        return result, reach, read

    def test_the_backup_stage_reads_through_the_bridge(self):
        result, reach, read = self.stage()
        read.assert_called_once_with("192.168.1.51")
        self.assertEqual(result, {"outcome": "saved", "stage_outcome": "complete"})
        self.assertIn("sonoff-backup", self.inputs["stock_backup"])

    def test_carrying_on_without_a_backup_skips_the_stage(self):
        from tools import autoflash_cli
        self.ctx.state.status = lambda step: "pending"
        with mock.patch.object(autoflash_cli, "reach_bridge", return_value="192.168.1.51"), \
             mock.patch.object(autoflash_cli, "read_dump", side_effect=OSError("timed out")), \
             mock.patch.object(autoflash_cli, "ask_choice", return_value=1), \
             mock.patch.object(autoflash_cli, "section"), mock.patch.object(autoflash_cli, "say"):
            result = autoflash_cli.step_backup(self.ctx)
        self.assertEqual(result, {"outcome": "failed", "stage_outcome": "skipped"})
        self.assertEqual(self.inputs["backup_outcome"], "failed")

    def test_a_plug_backed_up_before_needs_no_bridge(self):
        self.plugs.save_backup(b"original", efuse_with_mac(self.MAC), {}, "1.2.0")
        result, reach, read = self.stage()
        reach.assert_not_called()
        read.assert_not_called()
        self.assertEqual(result, {"outcome": "existing", "stage_outcome": "already done"})

    def test_a_run_past_the_installer_does_not_look_for_it(self):
        # A run from before this stage existed, resumed after D1.
        result, reach, _ = self.stage(done=("D1",))
        reach.assert_not_called()
        self.assertEqual(result, {"outcome": "not taken", "stage_outcome": "skipped"})

    def test_or_quit_before_tasmota_is_written(self):
        from tools.autoflash.prompts import QuitRun
        with self.assertRaises(QuitRun):
            self.back_up([OSError("a"), OSError("b")], answers=("quit",))
        self.assertNotIn("stock_backup", self.inputs)


class WifiFromPlugTests(unittest.TestCase):
    """The Wi-Fi details come off the plug, so nobody types them."""

    def setUp(self):
        from tools.autoflash import tasmota_settings as ts
        from tests.test_tasmota_settings import default_record
        self.ts = ts
        self.record = ts.fill(ts.make_template(default_record()), "Home-2G", "a good password")

    def ctx(self, *, connected="Home-2G", backup=None, fail=None, forced=False):
        from types import SimpleNamespace
        from tools.autoflash.device import DeviceError
        from tools.autoflash import tasmota_settings as ts

        def command(name, **kwargs):
            if fail == "command":
                raise DeviceError("no answer")
            return {"StatusSTS": {"Wifi": {"SSId": connected} if connected else {}}}

        def download_settings(**kwargs):
            if fail == "download":
                raise OSError("refused")
            return ts.encode_backup(self.record if backup is None else backup)

        device = SimpleNamespace(command=command, download_settings=download_settings)
        return SimpleNamespace(device=device, args=SimpleNamespace(wifi=forced))

    def read(self, **kwargs):
        from unittest import mock
        import tools.autoflash_cli as autoflash
        with mock.patch.object(autoflash, "say"):
            return autoflash.read_wifi_from_plug(self.ctx(**kwargs))

    def test_the_connected_network_and_its_password_are_read(self):
        self.assertEqual(self.read(), ("Home-2G", "a good password"))

    def test_a_plug_on_another_network_than_its_first_slot_is_not_used(self):
        # Tasmota holds two networks; only the one it is actually on is proven.
        self.assertIsNone(self.read(connected="Guest"))

    def test_a_plug_that_will_not_say_or_will_not_give_its_backup_falls_back(self):
        for kwargs in ({"connected": None}, {"fail": "command"}, {"fail": "download"},
                       {"backup": b"not a settings record"}):
            self.assertIsNone(self.read(**kwargs), kwargs)

    def test_an_open_network_is_read_with_an_empty_password(self):
        # Connected with its first slot's details and that slot has no
        # password: the network is open, and there is nothing to type.
        self.record = self.ts.fill(self.ts.make_template(self.record), "Home-2G", "")
        self.assertEqual(self.read(), ("Home-2G", ""))

    def test_an_open_network_on_another_slot_is_still_not_used(self):
        self.record = self.ts.fill(self.ts.make_template(self.record), "Home-2G", "")
        self.assertIsNone(self.read(connected="Guest"))

    def test_the_flag_forces_typing_them_instead(self):
        from unittest import mock
        import tools.autoflash_cli as autoflash
        ctx = self.ctx(forced=True)
        ctx._wifi = None
        with mock.patch.object(autoflash, "read_wifi_from_plug") as read, \
             mock.patch.object(autoflash, "section"), mock.patch.object(autoflash, "say"):
            try:
                autoflash.Context.wifi(ctx)
            except Exception:
                pass  # it goes on to the asking path, which needs a real bundle
        read.assert_not_called()

    def test_each_reason_for_not_reading_them_is_said(self):
        from unittest import mock
        import tools.autoflash_cli as autoflash
        for kwargs, words in (({"connected": None}, "did not say which network"),
                              ({"fail": "command"}, "did not answer as Tasmota"),
                              ({"connected": "Guest"}, "not the network in its first slot")):
            with mock.patch.object(autoflash, "say") as said:
                self.assertIsNone(autoflash.read_wifi_from_plug(self.ctx(**kwargs)))
            self.assertIn(words, " ".join(str(c.args[0]) for c in said.call_args_list), kwargs)


class BackupWifiTests(unittest.TestCase):
    """From a plug whose firmware cannot say its Wi-Fi password (ESPHome, the
    mover), the details come from its Sonoff backup on this computer - when
    the bundle, or the network ESPHome is on, shows they are the plug's still."""

    def setUp(self):
        from unittest import mock
        import tools.autoflash_cli as autoflash
        self.af = autoflash
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        (Path(self.tmp.name) / "flash.bin").write_bytes(b"\xff" * 0x20000)
        backup = mock.Mock(directory=Path(self.tmp.name))
        for patch in (mock.patch.object(autoflash.plugs, "find_backup", return_value=backup),
                      mock.patch.object(autoflash, "sonoff_backup_wifi", return_value=("Home-2G", "pw")),
                      mock.patch.object(autoflash, "say"), mock.patch.object(autoflash, "tell")):
            patch.start()
            self.addCleanup(patch.stop)

    def ctx(self, **inputs):
        from types import SimpleNamespace
        inputs = {"plug_mac": "12:34:56:ab:cd:ef", "plug_ip": "192.168.0.50", **inputs}
        state = SimpleNamespace(input=inputs.get, set_input=inputs.__setitem__, is_done=lambda step: False)
        return SimpleNamespace(state=state, manifest_path=Path("unused"), args=SimpleNamespace(wifi=False),
                               _wifi=None, _recovery=None)

    def found(self, *, pinned=None, rebuilds=True, joined=None):
        from unittest import mock
        recovery = mock.Mock(return_value=b"image") if rebuilds else mock.Mock(
            side_effect=self.af.BundleError("not the pinned password"))
        with mock.patch.object(self.af, "bundle_ssid", return_value=pinned), \
                mock.patch.object(self.af, "recovery_image", recovery), \
                mock.patch.object(self.af.esphome, "api_wifi",
                                  return_value={"signal": -50, "ssid": joined} if joined else None) as api:
            return self.af.wifi_from_backup(self.ctx()), api

    def test_the_bundle_set_up_for_them_proves_them(self):
        details, api = self.found(pinned="Home-2G")
        self.assertEqual(details, ("Home-2G", "pw"))
        api.assert_not_called()

    def test_an_older_password_than_the_bundles_is_not_used(self):
        self.assertIsNone(self.found(pinned="Home-2G", rebuilds=False)[0])

    def test_the_network_esphome_is_on_proves_them(self):
        self.assertEqual(self.found(joined="Home-2G")[0], ("Home-2G", "pw"))

    def test_another_network_or_no_proof_is_not_used(self):
        self.assertIsNone(self.found(joined="Guest")[0])
        self.assertIsNone(self.found()[0])

    def test_no_backup_gives_nothing(self):
        from unittest import mock
        with mock.patch.object(self.af.plugs, "find_backup", return_value=None):
            self.assertIsNone(self.af.wifi_from_backup(self.ctx()))

    def test_a_plug_on_esphome_has_them_read_before_dotenv_or_asking(self):
        from unittest import mock
        ctx = self.ctx(source="esphome")
        ctx.adopt_wifi = mock.Mock(side_effect=lambda ssid, password: setattr(ctx, "_wifi", (ssid, password)))
        # .env names another network: taken first, it would be the answer.
        with mock.patch.object(self.af, "wifi_from_esphome", return_value=None), \
                mock.patch.object(self.af, "wifi_from_backup", return_value=("Home-2G", "pw")), \
                mock.patch.object(self.af.credentials, "get", return_value="Elsewhere"), \
                mock.patch.object(self.af, "read_wifi_from_plug") as off_tasmota:
            self.assertEqual(self.af.Context.wifi(ctx), ("Home-2G", "pw"))
        off_tasmota.assert_not_called()  # a Tasmota is not what runs
        ctx.adopt_wifi.assert_called_once_with("Home-2G", "pw")
        self.assertIs(ctx.state.input("wifi_typed"), False)

    def test_esphomes_own_details_come_before_the_backups(self):
        from unittest import mock
        ctx = self.ctx(source="esphome")
        ctx.adopt_wifi = mock.Mock(side_effect=lambda ssid, password: setattr(ctx, "_wifi", (ssid, password)))
        with mock.patch.object(self.af.esphome, "api_wifi_details", return_value=("Home-5", "pw5")), \
                mock.patch.object(self.af, "wifi_from_backup") as backup:
            self.assertEqual(self.af.Context.wifi(ctx), ("Home-5", "pw5"))
        backup.assert_not_called()


class SonoffBackupWifiTests(unittest.TestCase):
    """The Wi-Fi Sonoff's firmware was set up with, as its backup holds it."""

    def read(self, content):
        from unittest import mock
        import tools.autoflash_cli as autoflash
        from tools.inspect_stock_nvs import Value
        value = Value(namespace=3, key="content", kind=0x21, chunks={0: content})
        with mock.patch.object(autoflash.inspect_stock_nvs, "parse",
                               return_value=({3: "frame_params"}, {(3, "content"): value})):
            return autoflash.sonoff_backup_wifi(b"")

    def test_the_name_and_password_come_from_frame_params(self):
        self.assertEqual(self.read(b'{"ssid":"Home-2G","passwd":"pw","model":"SN-ESP32C3-S60-01"}\x00'),
                         ("Home-2G", "pw"))

    def test_an_open_network_has_no_password(self):
        self.assertEqual(self.read(b'{"ssid":"Home-2G"}\x00\xff\xff'), ("Home-2G", ""))

    def test_no_network_gives_nothing(self):
        self.assertIsNone(self.read(b'{"ssid":"","passwd":""}\x00'))


class WifiFallbackTests(unittest.TestCase):
    """Typing the Wi-Fi is the fallback: explained, asked once, and needs no rebuild."""

    def ctx(self, *, found=None, bundle_for=None):
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash
        ctx = SimpleNamespace(args=SimpleNamespace(wifi=False), manifest_path=Path("unused"),
                              state=SimpleNamespace(set_input=lambda *_: None, input=lambda *_: None),
                              _wifi=None, _recovery=None)
        ctx.remember_wifi = lambda ssid, password, image: autoflash.Context.remember_wifi(
            ctx, ssid, password, image)
        ctx.adopt_wifi = lambda ssid, password: autoflash.Context.adopt_wifi(ctx, ssid, password)
        self.patches = [
            mock.patch.object(autoflash, "read_wifi_from_plug", return_value=found),
            mock.patch.object(autoflash, "bundle_ssid", return_value=bundle_for),
            mock.patch.object(autoflash, "say"),
        ]
        for patch in self.patches:
            patch.start()
        self.addCleanup(lambda: [patch.stop() for patch in self.patches])
        return ctx

    def test_details_read_off_the_plug_are_pinned_without_asking_or_downloading(self):
        from unittest import mock
        import tools.autoflash_cli as autoflash
        ctx = self.ctx(found=("Home-2G", "pw"))
        with mock.patch.object(autoflash, "recovery_image",
                               side_effect=autoflash.BundleError("no Wi-Fi details yet")), \
             mock.patch.object(autoflash, "pin_wifi", return_value=b"image") as pin, \
             mock.patch.object(autoflash, "build_bundle") as rebuild, \
             mock.patch.object(autoflash, "section") as shown, \
             mock.patch.object(autoflash, "ask_credentials") as asked:
            self.assertEqual(autoflash.Context.wifi(ctx), ("Home-2G", "pw"))
        pin.assert_called_once_with(Path("unused"), "Home-2G", "pw")
        rebuild.assert_not_called()
        asked.assert_not_called()
        shown.assert_not_called()

    def test_details_read_off_the_plug_are_not_reported_as_typed(self):
        # A run stopped at the question, then resumed and read off the plug,
        # must not keep the earlier "typed" mark.
        from unittest import mock
        import tools.autoflash_cli as autoflash
        ctx = self.ctx(found=("Home-2G", ""))
        recorded = {}
        ctx.state.set_input = lambda key, value: recorded.__setitem__(key, value)
        with mock.patch.object(autoflash, "recovery_image", return_value=b"image"):
            self.assertEqual(autoflash.Context.wifi(ctx), ("Home-2G", ""))
        self.assertIs(recorded.get("wifi_typed"), False)

    def test_when_they_cannot_be_read_the_reason_is_shown_then_both_are_asked(self):
        from unittest import mock
        import tools.autoflash_cli as autoflash
        ctx = self.ctx(found=None, bundle_for=None)
        with mock.patch.object(autoflash, "recovery_image",
                               side_effect=autoflash.BundleError("no Wi-Fi details yet")), \
             mock.patch.object(autoflash, "pin_wifi", return_value=b"image"), \
             mock.patch.object(autoflash, "section") as shown, \
             mock.patch.object(autoflash, "ask_credentials", return_value=("Home-2G", "pw")) as asked:
            details = autoflash.Context.wifi(ctx, why=["Tasmota is waiting on its setup network."])
        self.assertEqual(details, ("Home-2G", "pw"))
        asked.assert_called_once()
        self.assertIn("Tasmota is waiting on its setup network.", shown.call_args.args[1])

    def test_a_bundle_the_checkout_cannot_use_is_built_again(self):
        from unittest import mock
        import tools.autoflash_cli as autoflash
        ctx = self.ctx(found=("Home-2G", "pw"))
        with mock.patch.object(autoflash, "recovery_image", side_effect=autoflash.BundleError("x")), \
             mock.patch.object(autoflash, "pin_wifi", side_effect=autoflash.BundleError("old")), \
             mock.patch.object(autoflash, "build_bundle", return_value=(Path("m"), b"image")) as rebuild:
            autoflash.Context.wifi(ctx)
        rebuild.assert_called_once()


class EwelinkAccountTests(unittest.TestCase):
    """The last eWeLink account is offered, not assumed."""

    def email(self, *, remembered=None, chosen=None, choice=0, typed="new@example.com", given=None):
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash
        inputs = {"email": chosen} if chosen else {}
        ctx = SimpleNamespace(state=SimpleNamespace(input=inputs.get,
                                                    set_input=inputs.__setitem__),
                              remember_email=lambda e: inputs.__setitem__("email", e),
                              _given_email=given)
        with mock.patch.object(autoflash, "load_settings",
                               return_value={"ewelink_email": remembered} if remembered else {}), \
             mock.patch.object(autoflash, "ask_choice", return_value=choice) as menu, \
             mock.patch.object(autoflash, "ask_text", return_value=typed) as asked:
            return autoflash.Context.email(ctx), menu, asked

    def test_last_time_s_account_is_offered_and_can_be_kept(self):
        email, menu, asked = self.email(remembered="old@example.com", choice=0)
        self.assertEqual(email, "old@example.com")
        self.assertIn("old@example.com", menu.call_args.args[1][0])
        asked.assert_not_called()

    def test_a_different_account_can_be_entered(self):
        email, _, asked = self.email(remembered="old@example.com", choice=1)
        self.assertEqual(email, "new@example.com")
        asked.assert_called_once()

    def test_one_given_in_advance_is_used_without_asking(self):
        email, menu, asked = self.email(remembered="old@example.com", given="env@example.com")
        self.assertEqual(email, "env@example.com")
        menu.assert_not_called()
        asked.assert_not_called()

    def test_with_none_remembered_it_is_simply_asked(self):
        email, menu, _ = self.email()
        self.assertEqual(email, "new@example.com")
        menu.assert_not_called()

    def test_once_chosen_in_a_run_it_is_not_asked_again(self):
        email, menu, asked = self.email(remembered="old@example.com", chosen="run@example.com")
        self.assertEqual(email, "run@example.com")
        menu.assert_not_called()
        asked.assert_not_called()


class ConfigureStepTests(unittest.TestCase):
    """F4 rebuilds Tasmota's settings from its own defaults before anything else."""

    def test_reset_4_comes_first_and_keeps_the_wifi(self):
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash
        sent = []
        device = SimpleNamespace(wait_reachable=lambda **_: None, started_at=lambda: "t",
                                 uptime_seconds=lambda: 10, wait_for_reboot=lambda **_: 1.0,
                                 command=lambda c, timeout=None: sent.append(c), is_safeboot=lambda: False)
        state = SimpleNamespace(input=lambda key: {"product_model": "S60TPF"}.get(key))
        ctx = SimpleNamespace(finder=SimpleNamespace(poll=lambda: "192.168.1.51"), device=device, state=state)
        with mock.patch.object(autoflash, "say") as said:
            result = autoflash.step_configure(ctx)
        writes = [command for command in sent if " " in command]  # not the questions first
        self.assertEqual(writes[0], "Reset 4")
        self.assertEqual(writes[1:3], ["SaveData 1", f"Template {autoflash.s60_template('S60TPF')}"])
        self.assertIn("Sonoff S60TPF", " ".join(str(c.args[0]) for c in said.call_args_list))
        self.assertEqual(result["wifi_route"], "joined by itself")
        self.assertIn("joined your Wi-Fi by itself", " ".join(str(c.args[0]) for c in said.call_args_list))


    def test_tried_again_once_the_settings_are_in_place_it_resets_nothing(self):
        # A busy plug took the template but answered too slowly: resetting
        # again would restart it again, every try.
        import json
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash
        template = autoflash.s60_template("S60TPF")
        answers = {"Template": json.loads(template), "Module": {"Module": {"0": "Sonoff S60TPF"}},
                   "SaveData": {"SaveData": "ON"}}
        sent = []

        def command(c, timeout=None):
            sent.append(c)
            return answers.get(c)

        device = SimpleNamespace(wait_reachable=lambda **_: None, command=command, is_safeboot=lambda: False)
        state = SimpleNamespace(input=lambda key: {"product_model": "S60TPF"}.get(key))
        ctx = SimpleNamespace(finder=SimpleNamespace(poll=lambda: "192.168.1.51"), device=device, state=state)
        with mock.patch.object(autoflash, "say"):
            result = autoflash.step_configure(ctx)
        self.assertNotIn("Reset 4", sent)
        self.assertEqual(result["template"], "applied")


class PromoteTests(unittest.TestCase):
    """D4 - tried again after an upload cut off, Tasmota is restarted first:
    it still holds that upload, and the next hangs."""

    def promote(self, failed_before, slots=(0, 1)):
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        if failed_before:
            state.mark("D4", FAILED, error="Berry Tasmota was not uploaded")
        events = []
        slots = iter(slots)
        device = SimpleNamespace(host="192.168.60.50",
            wait_reachable=lambda **_: None, started_at=lambda: "t", uptime_seconds=lambda: 10,
            current_ota=lambda: next(slots), wait_for_reboot=lambda **_: events.append("rebooted"),
            # What the plug is told, in order; a status read changes nothing.
            command=lambda c, timeout=None: {} if c.startswith("Status") else events.append(c))
        ctx = SimpleNamespace(device=device, state=state, dry_run=False)
        with mock.patch.object(autoflash, "upload_or_prompt", side_effect=lambda *a: events.append("upload")), \
             mock.patch.object(autoflash.time, "sleep"), mock.patch.object(autoflash, "say"):
            autoflash.step_promote(ctx)
        return events

    def test_a_slot_not_known_yet_after_the_restart_is_waited_for(self):
        # Seen in the fault suite: just restarted, Berry not up, the slot read
        # as unknown - taken for "not slot 1", and a second upload sent.
        events = self.promote(False, slots=(0, None, None, 1))
        self.assertEqual(events.count("upload"), 1)

    def test_a_first_try_uploads_at_once(self):
        self.assertEqual(self.promote(False)[0], "upload")

    def test_tried_again_it_restarts_first(self):
        events = self.promote(True)
        self.assertEqual(events[:3], ["Restart 1", "rebooted", "upload"])


class BerryFromStartupTests(unittest.TestCase):
    """X2 - the startup firmware confirms itself only once used: switched off
    and on before then, the plug starts the firmware it ran before (seen in the
    fault suite, a power cut early in X2), which once passed for Tasmota."""

    def x2(self, running, versions=("15.6.0.1(f5b34a2-bluetooth)",)):
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash
        ctx = SimpleNamespace(dry_run=False, device=SimpleNamespace(host="192.168.60.50"))
        answers = iter(versions)
        seen = []

        def wait_for(ctx_, check, what, timeout=240.0):
            seen.append(check("192.168.60.50"))

        with mock.patch.object(autoflash.detect, "identify", return_value=running), \
             mock.patch.object(autoflash, "step_startup_from_esphome") as x1, \
             mock.patch.object(autoflash, "upload_or_prompt", return_value="http:12s") as upload, \
             mock.patch.object(autoflash, "wait_for", side_effect=wait_for), \
             mock.patch.object(autoflash, "Device", lambda address, timeout=None: SimpleNamespace(
                 firmware_version=lambda: next(answers))), \
             mock.patch.object(autoflash, "tell"), mock.patch.object(autoflash, "say"):
            result = autoflash.step_berry_from_startup(ctx)
        return result, x1, upload.called, seen

    def test_already_on_tasmota_does_nothing(self):
        result, x1, uploaded, _ = self.x2("hub")
        self.assertEqual(result, {"already": True})
        self.assertFalse(uploaded or x1.called)

    def test_back_on_the_mover_the_startup_firmware_goes_on_again_first(self):
        result, x1, uploaded, _ = self.x2("mover")
        x1.assert_called_once()
        self.assertEqual(x1.call_args.kwargs, {"again": True})  # no second go-ahead
        self.assertTrue(uploaded)

    def test_on_the_startup_firmware_it_is_uploaded(self):
        result, x1, uploaded, seen = self.x2("startup")
        self.assertFalse(x1.called)
        self.assertTrue(uploaded)
        self.assertEqual(seen, [True])

    def test_a_page_not_answering_is_not_tasmota(self):
        _, _, _, seen = self.x2("startup", versions=("",))
        self.assertEqual(seen, [False])


class OfficialTasmotaSeenTests(unittest.TestCase):
    """F2 - official Tasmota is seen running only from what it says it is: a
    plug restarting does not answer its page for a moment (seen in the fault
    suite taken for Tasmota, while it came back on the startup firmware)."""

    def wait(self, versions, setup=None):
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash
        answers = iter(versions)
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds

        device = SimpleNamespace(host="192.168.60.50", firmware_version=lambda: next(answers))
        ctx = SimpleNamespace(finder=SimpleNamespace(poll=lambda: "192.168.60.50"), device=device)
        with mock.patch.object(autoflash.time, "sleep", side_effect=sleep), \
             mock.patch.object(autoflash.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(autoflash, "find_ssid", return_value=setup), \
             mock.patch.object(autoflash, "say"):
            return autoflash.wait_for_official(ctx)

    def test_silence_is_not_tasmota(self):
        outcome = self.wait(["", "", "15.6.0(release-tasmota32)"])
        self.assertEqual(outcome, "rejoined the network")

    def test_still_on_the_startup_firmware_a_minute_on_did_not_take(self):
        self.assertEqual(self.wait(["15.6.0(safeboot)"] * 20), "startup firmware")

    def test_one_that_did_not_take_is_sent_again(self):
        from unittest import mock
        import tools.autoflash_cli as autoflash
        with mock.patch.object(autoflash, "upload_to_startup_firmware", return_value="http:20s") as up, \
             mock.patch.object(autoflash, "wait_for_official",
                               side_effect=["startup firmware", "rejoined the network"]), \
             mock.patch.object(autoflash, "tell") as told:
            how, outcome = autoflash.install_official(object(), Path("tasmota32c3.bin"))
        self.assertEqual((up.call_count, outcome), (2, "rejoined the network"))
        self.assertIn("did not take", " ".join(str(c.args[0]) for c in told.call_args_list))

    def test_three_that_do_not_take_stop_with_the_reason(self):
        from unittest import mock
        import tools.autoflash_cli as autoflash
        with mock.patch.object(autoflash, "upload_to_startup_firmware", return_value="http:20s"), \
             mock.patch.object(autoflash, "wait_for_official", return_value="startup firmware"), \
             mock.patch.object(autoflash, "tell"):
            with self.assertRaisesRegex(autoflash.Abort, "did not take"):
                autoflash.install_official(object(), Path("tasmota32c3.bin"))

    def test_f4_finding_the_startup_firmware_sends_official_tasmota_again(self):
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash
        on_startup = [True]
        device = SimpleNamespace(wait_reachable=lambda **_: None, is_safeboot=lambda: on_startup[0],
                                 command=lambda c, timeout=None: None)
        state = SimpleNamespace(input=lambda key: {"product_model": "S60TPF"}.get(key))
        ctx = SimpleNamespace(finder=SimpleNamespace(poll=lambda: "192.168.60.50"), device=device, state=state)

        def installed(ctx_, app):
            on_startup[0] = False
            return "http:20s", "rejoined the network"

        with mock.patch.object(autoflash, "install_official", side_effect=installed) as install, \
             mock.patch.object(autoflash, "s60_settings_in_place", return_value=True), \
             mock.patch.object(autoflash, "tell"), mock.patch.object(autoflash, "say"):
            autoflash.step_configure(ctx)
        install.assert_called_once()


class BackAfterResetTests(unittest.TestCase):
    """F4 - Tasmota keeps its Wi-Fi through Reset 4 only once it has saved its
    defaults: cut off in the second before, it comes back on its own setup
    network, and is set up through that rather than the run failing."""

    def back(self, reboot_ok, setup):
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash
        from tools.autoflash.device import DeviceError

        def wait_for_reboot(**kwargs):
            if not reboot_ok:
                raise DeviceError("192.168.60.50 did not restart within 180s")
            return 5.0

        device = SimpleNamespace(wait_for_reboot=wait_for_reboot, wait_reachable=mock.Mock())
        ctx = SimpleNamespace(device=device, state=SimpleNamespace(input=lambda key: None))
        with mock.patch.object(autoflash, "find_ssid", return_value=setup) as looked, \
             mock.patch.object(autoflash, "setup_networks", return_value=("tasmota-ABCDEF-1234",)), \
             mock.patch.object(autoflash, "configure_plug_wifi") as configured, \
             mock.patch.object(autoflash, "tell"), mock.patch.object(autoflash, "say"):
            try:
                autoflash.back_after_reset(ctx, "boot", 12)
                outcome = "back"
            except autoflash.Abort as exc:
                outcome = str(exc)
        return outcome, looked.called, configured.call_args

    def test_back_as_usual_looks_no_further(self):
        outcome, looked, configured = self.back(True, None)
        self.assertEqual((outcome, looked, configured), ("back", False, None))

    def test_back_on_its_setup_network_is_set_up_through_it(self):
        outcome, looked, configured = self.back(False, "tasmota-ABCDEF-1234")
        self.assertEqual(outcome, "back")
        self.assertEqual(configured.args[1], "tasmota-ABCDEF-1234")

    def test_nowhere_to_be_seen_says_to_switch_it_off_and_on(self):
        outcome, _, configured = self.back(False, None)
        self.assertIn("Switch it off and on", outcome)
        self.assertIsNone(configured)


class RefusedUploadTests(unittest.TestCase):
    """An upload Tasmota took whole and refused (damaged on the way, say) is
    sent again, three tries in all, before it is left to the person."""

    def upload(self, outcomes):
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash
        tries = iter(outcomes)
        sent = []

        def upload_firmware(path, timeout=None):
            sent.append(path)
            outcome = next(tries)
            if isinstance(outcome, Exception):
                raise outcome

        ctx = SimpleNamespace(device=SimpleNamespace(upload_firmware=upload_firmware))
        with mock.patch.object(autoflash, "upload_or_prompt_by_hand", return_value="manual") as by_hand, \
             mock.patch.object(autoflash.time, "sleep"), \
             mock.patch.object(autoflash, "tell") as told, mock.patch.object(autoflash, "say"):
            result = autoflash.upload_or_prompt(ctx, Path("berry.bin"), "Berry Tasmota")
        return result, len(sent), by_hand.called, " ".join(str(c.args[0]) for c in told.call_args_list)

    def test_one_refusal_is_sent_again(self):
        from tools.autoflash.device import UploadRefused
        result, sent, by_hand, told = self.upload([UploadRefused("Bad magic byte"), None])
        self.assertTrue(result.startswith("http:"))
        self.assertEqual(sent, 2)
        self.assertIn("refused the upload (Bad magic byte)", told)
        self.assertFalse(by_hand)

    def test_three_refusals_leave_it_to_the_person(self):
        from tools.autoflash.device import UploadRefused
        result, sent, by_hand, _ = self.upload([UploadRefused("Not enough space")] * 3)
        self.assertEqual((result, sent, by_hand), ("manual", 3, True))

    def test_any_other_failure_goes_to_the_person_at_once(self):
        result, sent, by_hand, _ = self.upload([OSError("no route")])
        self.assertEqual((result, sent, by_hand), ("manual", 1, True))


class StalledUploadTests(unittest.TestCase):
    """F2 - an upload to the startup firmware that stalls is tried again from
    a restart, three tries in all, before it is left to the person."""

    def upload(self, uploads, back=True, restart_answers=True):
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash
        events = []
        tries = iter(uploads)

        def upload_firmware(path, timeout=None):
            events.append(("upload", timeout))
            outcome = next(tries)
            if isinstance(outcome, Exception):
                raise outcome

        def command(c, timeout=None):
            events.append(c)
            if not restart_answers:
                raise OSError("not answering")

        answering = iter([True, True, False] * 10)  # gone a few seconds after "Restart 1"

        def reachable(timeout=None):
            events.append("reachable")
            return next(answering)

        ctx = SimpleNamespace(device=SimpleNamespace(upload_firmware=upload_firmware, command=command,
                                                     reachable=reachable))
        with mock.patch.object(autoflash, "recovery_firmware_up", return_value=back), \
             mock.patch.object(autoflash, "upload_or_prompt_by_hand", return_value="manual") as by_hand, \
             mock.patch.object(autoflash.time, "sleep"), \
             mock.patch.object(autoflash, "tell") as told, mock.patch.object(autoflash, "say"):
            try:
                result = autoflash.upload_to_startup_firmware(ctx, Path("tasmota32c3.bin"))
            except autoflash.Abort as exc:
                result = exc
        return result, events, by_hand.called, " ".join(str(c.args[0]) for c in told.call_args_list)

    def test_an_upload_that_goes_through_is_done_once(self):
        result, events, by_hand, _ = self.upload([None])
        self.assertTrue(result.startswith("http:"))
        self.assertEqual(events, [("upload", 120.0)])  # a stall noticed in two minutes, not ten
        self.assertFalse(by_hand)

    def test_a_stalled_upload_is_tried_again_after_a_restart(self):
        result, events, by_hand, told = self.upload([TimeoutError("stalled"), TimeoutError("stalled"), None])
        self.assertEqual([e for e in events if e == "Restart 1"], ["Restart 1"] * 2)
        self.assertEqual(len([e for e in events if isinstance(e, tuple)]), 3)
        # sent again only once the plug was seen to go for its restart
        restart = events.index("Restart 1")
        self.assertEqual(events[restart + 1:restart + 4], ["reachable"] * 3)
        self.assertTrue(result.startswith("http:"))
        self.assertIn("attempt 3 of 3", told)
        self.assertFalse(by_hand)

    def test_three_stalls_leave_it_to_the_person(self):
        result, events, by_hand, _ = self.upload([TimeoutError("stalled")] * 5)
        self.assertEqual(len([e for e in events if isinstance(e, tuple)]), 3)
        self.assertTrue(by_hand)
        self.assertEqual(result, "manual")

    def test_a_plug_too_stuck_to_restart_is_switched_off_and_on(self):
        _, _, _, told = self.upload([TimeoutError("stalled"), None], restart_answers=False)
        self.assertIn("switch it off and on", told)

    def test_a_refused_upload_says_so_and_is_tried_again(self):
        from tools.autoflash.device import UploadRefused
        result, events, by_hand, told = self.upload([UploadRefused("Not enough space"), None])
        self.assertTrue(result.startswith("http:"))
        self.assertIn("refused the upload (Not enough space)", told)
        self.assertFalse(by_hand)

    def test_one_that_never_comes_back_stops_with_a_reason(self):
        from tools import autoflash_cli
        result, _, by_hand, _ = self.upload([TimeoutError("stalled")], back=False)
        self.assertIsInstance(result, autoflash_cli.Abort)
        self.assertFalse(by_hand)


class MoverOwnNetworkTests(unittest.TestCase):
    """A mover that cannot join the home Wi-Fi raises its own network, and is
    set up through it - with the details the run has first, new ones only if
    those fail - not left as a plug only a serial cable reaches."""

    def wait(self, answers, network=None, sends=(None,), joined_already=False, unattended=False):
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash
        tries = iter(answers)
        outcomes = iter(sends)

        def wait_for(ctx, check, what, timeout=240.0):
            if not next(tries):
                raise autoflash.Abort(f"{what} did not answer within 4 minutes")

        def send(ssid, password):
            outcome = next(outcomes, None)
            if outcome:
                raise autoflash.setup_network.SetupError(outcome)

        adopted = []
        ctx = SimpleNamespace(state=SimpleNamespace(input=lambda key: "12:34:56:ab:cd:ef"),
                              _wifi=("Home", "pw0"), wifi=lambda why=None: ("Home", "pw0"),
                              adopt_wifi=lambda ssid, pw: adopted.append((ssid, pw)))
        self.home_waits = 0
        with mock.patch.object(autoflash, "wait_for", side_effect=wait_for) as waited, \
             mock.patch.object(autoflash, "find_ssid", return_value=network), \
             mock.patch.object(autoflash, "on_a_plug_network", return_value=joined_already), \
             mock.patch.object(autoflash, "ask_credentials", return_value=("NewHome", "pw")), \
             mock.patch.object(autoflash.prompts, "UNATTENDED", unattended), \
             mock.patch.object(autoflash, "wait_until_joined"), \
             mock.patch.object(autoflash.time, "sleep"), \
             mock.patch.object(autoflash.setup_network, "send_esphome_wifi", side_effect=send) as sent, \
             mock.patch.object(autoflash, "section"), mock.patch.object(autoflash, "tell"):
            try:
                result = autoflash.wait_for_mover(ctx, "the mover")
            except autoflash.Abort as exc:
                result = exc
            self.home_waits = waited.call_count
        return result, sent, adopted

    def test_a_mover_on_the_home_wifi_is_used_as_ever(self):
        result, sent, _ = self.wait([True])
        self.assertIs(result, False)
        sent.assert_not_called()

    def test_after_a_router_outage_the_runs_own_details_do(self):
        result, sent, adopted = self.wait([False, True], network="Sonoff-Setup-CDEF")
        self.assertIs(result, False)  # the same details: ESPHome has them already
        sent.assert_called_once_with("Home", "pw0")
        self.assertEqual(adopted, [])

    def test_new_details_when_those_fail_which_esphome_gets_too(self):
        result, sent, adopted = self.wait([False, False, True], network="Sonoff-Setup-CDEF")
        self.assertIs(result, True)
        self.assertEqual([c.args for c in sent.call_args_list], [("Home", "pw0"), ("NewHome", "pw")])
        self.assertEqual(adopted, [("NewHome", "pw")])

    def test_unattended_it_stops_rather_than_asking(self):
        result, _, _ = self.wait([False, False], network="Sonoff-Setup-CDEF", unattended=True)
        from tools import autoflash_cli
        self.assertIsInstance(result, autoflash_cli.Abort)
        self.assertIn("did not join 'Home'", str(result))

    def test_a_send_that_gets_no_answer_is_tried_again(self):
        result, sent, _ = self.wait([False, True], network="Sonoff-Setup-CDEF",
                                    sends=("no answer", "no answer", None))
        self.assertIs(result, False)
        self.assertEqual(sent.call_count, 3)

    def test_joined_already_it_carries_on_through_that(self):
        result, sent, _ = self.wait([True], network="Sonoff-Setup-CDEF", joined_already=True)
        self.assertIs(result, False)
        sent.assert_called_once()
        self.assertEqual(self.home_waits, 1)  # only after sending: no wait first

    def test_neither_says_to_switch_it_off_and_on(self):
        result, sent, _ = self.wait([False], network=None)
        from tools import autoflash_cli
        self.assertIsInstance(result, autoflash_cli.Abort)
        self.assertIn("switch the plug off and on", str(result))
        self.assertIn("Sonoff-Setup-CDEF", str(result))

    def test_the_details_go_to_esphomes_own_page(self):
        from unittest import mock
        from tools.autoflash import setup_network
        with mock.patch.object(setup_network, "page", return_value="ok") as page:
            setup_network.send_esphome_wifi("My Wi-Fi", "p&ss")
        self.assertEqual(page.call_args.args[1], "/wifisave?ssid=My+Wi-Fi&psk=p%26ss")


class StartupSettingsReportTests(unittest.TestCase):
    """F2 keeps what the startup firmware said about saving the Wi-Fi."""

    def report(self, lines=None, error=None):
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash

        def console_log(**_):
            if error:
                raise error
            return lines
        with mock.patch.object(autoflash, "say") as said:
            note = autoflash.startup_settings_report(SimpleNamespace(console_log=console_log))
        return note, " ".join(str(c.args[0]) for c in said.call_args_list)

    def test_a_saved_record_is_reported(self):
        note, text = self.report(["00:00:00.255 S60 recovery Wi-Fi defaults active",
                                  "00:00:00.316 S60 recovery: Wi-Fi settings saved for the next firmware"])
        self.assertEqual(note, "S60 recovery: Wi-Fi settings saved for the next firmware")
        self.assertIn("should join your network by itself", text)

    def test_a_failure_is_explained_and_the_fallback_named(self):
        note, text = self.report(["00:00:00.300 S60 recovery: settings not saved, read-back failed (4354)"])
        self.assertIn("read-back failed", note)
        self.assertIn("its own network instead", text)

    def test_an_unreadable_log_does_not_stop_the_run(self):
        from tools.autoflash.device import DeviceError
        self.assertIsNone(self.report(error=DeviceError("no answer"))[0])
        self.assertIsNone(self.report(lines=["00:00:01 WIF: Connected"])[0])


class DeliveryPortTests(unittest.TestCase):
    """The bridge is served on 8088 unless something else has it."""

    def run_bridge(self, busy):
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "RECOVERY_LOCK").write_text("locked")
            state = RunState.open(root)
            state.set_input("plug_ip", "192.168.1.96")
            state.set_input("stock_version", "1.2.0")
            ctx = SimpleNamespace(state=state, bridge=Path("bridge.ota"), dir=root,
                                  listen_ip=lambda: "192.168.1.141", email=lambda: "me@example.com",
                                  ewelink_env=lambda: {})
            opened = []
            with mock.patch.object(autoflash, "ROOT", root), \
                 mock.patch.object(autoflash.hostsys, "port_is_free", lambda ip, port: port not in busy), \
                 mock.patch.object(autoflash, "ensure_firewall",
                                   lambda c: opened.append(autoflash.firewall_ports(c))), \
                 mock.patch.object(autoflash, "warning_box"), \
                 mock.patch.object(autoflash, "require_word", return_value=True), \
                 mock.patch.object(autoflash, "run_tool", return_value=0) as run_tool, \
                 mock.patch.object(autoflash, "say"):
                try:
                    autoflash.step_bridge(ctx)
                    error = None
                except autoflash.Abort as exc:
                    error = str(exc)
            command = run_tool.call_args.args[0] if run_tool.called else None
            port = command[command.index("--listen-port") + 1] if command else None
            return port, opened, state.input("delivery_port"), error

    def test_8088_is_used_when_free(self):
        port, opened, recorded, _ = self.run_bridge(busy=set())
        self.assertEqual((port, recorded), ("8088", "8088"))
        self.assertEqual(opened, [[8088, 8089]])

    def test_a_busy_8088_moves_to_8090_and_the_firewall_follows(self):
        # 8089 is the phase port, so it is never offered for the bridge.
        port, opened, recorded, _ = self.run_bridge(busy={8088})
        self.assertEqual((port, recorded), ("8090", "8090"))
        self.assertEqual(opened, [[8089, 8090]])

    def test_every_port_busy_stops_before_anything_is_sent(self):
        port, opened, _, error = self.run_bridge(busy={8088, *range(8090, 8100)})
        self.assertIsNone(port)
        self.assertEqual(opened, [])
        self.assertIn("all in use", error)


class PhasePortTests(unittest.TestCase):
    """The migration steps are served on 8089 unless something else has it."""

    def runner(self, busy, delivered="8088"):
        from types import SimpleNamespace
        from unittest import mock
        import tools.autoflash_cli as autoflash
        with tempfile.TemporaryDirectory() as directory:
            state = RunState.open(Path(directory))
            state.set_input("delivery_port", delivered)
            ctx = SimpleNamespace(state=state, manifest_path=Path("m.json"), evidence=Path("e"),
                                  listen_ip=lambda: "192.168.1.141", device=object(),
                                  recovery_bytes=lambda: None)
            opened = []
            with mock.patch.object(autoflash.hostsys, "port_is_free", lambda ip, port: port not in busy), \
                 mock.patch.object(autoflash, "ensure_firewall",
                                   lambda c: opened.append(autoflash.firewall_ports(c))), \
                 mock.patch.object(autoflash, "PhaseRunner") as runner, \
                 mock.patch.object(autoflash, "say"):
                try:
                    autoflash.Context.phase_runner(ctx)
                    error = None
                except autoflash.Abort as exc:
                    error = str(exc)
            port = runner.call_args.kwargs["listen_port"] if runner.called else None
            return port, opened, state.input("phase_port"), error

    def test_8089_is_used_when_free(self):
        self.assertEqual(self.runner(busy=set())[:3], (8089, [[8088, 8089]], "8089"))

    def test_a_busy_8089_moves_to_8100_and_the_firewall_follows(self):
        # The bridge may have been served on 8090-8099; the phases stay clear of those.
        self.assertEqual(self.runner(busy={8089}, delivered="8090")[:3], (8100, [[8090, 8100]], "8100"))

    def test_every_port_busy_stops_before_anything_is_sent(self):
        port, opened, _, error = self.runner(busy={8089, *range(8100, 8110)})
        self.assertIsNone(port)
        self.assertEqual(opened, [])
        self.assertIn("all in use", error)


class TemplateNameTests(unittest.TestCase):
    """Tasmota names the plug after its own model, and never trusts the field blindly."""

    def template(self, product):
        import json
        import tools.autoflash_cli as autoflash
        return json.loads(autoflash.s60_template(product))

    def test_each_region_gets_its_own_name(self):
        self.assertEqual(self.template("S60TPG")["NAME"], "Sonoff S60TPG")
        self.assertEqual(self.template("S60TPF")["NAME"], "Sonoff S60TPF")
        self.assertEqual(self.template(" s60tpf ")["NAME"], "Sonoff S60TPF")

    def test_anything_else_gets_the_series_name(self):
        for product in (None, "", "S60", "S60ZBTPF", "S60TPGX", 'S60TPG","GPIO":[0]'):
            self.assertEqual(self.template(product)["NAME"], "Sonoff S60", product)

    def test_the_wiring_is_the_tested_s60tpg_template(self):
        # Byte for byte the template proven on the UK plugs, apart from the name.
        import tools.autoflash_cli as autoflash
        self.assertEqual(
            autoflash.s60_template("S60TPG"),
            '{"NAME":"Sonoff S60TPG","GPIO":[1,1,1,1,224,544,1,3104,1,32,1,0,0,0,0,0,0,0,'
            '1,1,1,1],"FLAG":0,"BASE":1}',
        )


class BridgeEvidenceTests(unittest.TestCase):
    """The installer raises its own network only when the home one fails."""

    def test_bridge_on_the_home_network_counts(self):
        from unittest import mock
        from tools import autoflash_cli
        with mock.patch.object(autoflash_cli, "bridge_on_home_network", return_value="192.168.0.50"), \
             mock.patch.object(autoflash_cli, "find_ssid", return_value=None):
            installed, reason = autoflash_cli.evidence_bridge_installed(mock.Mock())
        self.assertTrue(installed)
        self.assertIn("192.168.0.50", reason)

    def test_its_own_network_still_counts(self):
        from unittest import mock
        from tools import autoflash_cli
        with mock.patch.object(autoflash_cli, "bridge_on_home_network", return_value=None), \
             mock.patch.object(autoflash_cli, "find_ssid", return_value="Sonoff-Setup-CDEF"):
            installed, _ = autoflash_cli.evidence_bridge_installed(self.ctx())
        self.assertTrue(installed)

    def test_neither_is_not_proof_of_failure(self):
        from unittest import mock
        from tools import autoflash_cli
        with mock.patch.object(autoflash_cli, "bridge_on_home_network", return_value=None), \
             mock.patch.object(autoflash_cli, "find_ssid", return_value=None):
            installed, _ = autoflash_cli.evidence_bridge_installed(self.ctx())
        self.assertIsNone(installed)

    def ctx(self):
        from types import SimpleNamespace
        return SimpleNamespace(state=SimpleNamespace(input=lambda key: "12:34:56:ab:cd:ef" if key == "plug_mac" else None))


class JoinBridgeTests(unittest.TestCase):
    """The installer's network is open, like every setup network of this
    project's firmware; only an earlier release's has a password."""

    def shown(self, network):
        from unittest import mock
        from tools import autoflash_cli
        with mock.patch.object(autoflash_cli, "section") as section, \
             mock.patch.object(autoflash_cli.hostsys, "copy_to_clipboard", return_value=False) as copied:
            autoflash_cli.ask_to_join_bridge(network)
        return " ".join(section.call_args.args[1]), copied.called

    def test_open_no_password(self):
        text, copied = self.shown("Sonoff-Setup-CDEF")
        self.assertIn("Sonoff-Setup-CDEF      (open - no password)", text)
        self.assertNotIn("password  ", text)
        self.assertFalse(copied)

    def test_an_earlier_installers_network_has_its_password(self):
        text, copied = self.shown("S60-OTA-Bridge-A04D")
        self.assertIn("password  s60-ota-bridge", text)
        self.assertTrue(copied)


class LeftLockTests(unittest.TestCase):
    def test_a_lock_left_moved_aside_by_a_stopped_run_is_put_back(self):
        from unittest import mock
        import tools.autoflash_cli as autoflash
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "RECOVERY_LOCK.owner-authorized").write_text("locked\n")
            (root / "REPARTITION_LOCK").write_text("locked\n")
            with mock.patch.object(autoflash, "ROOT", root), mock.patch.object(autoflash, "say"):
                autoflash.restore_left_locks()
            self.assertTrue((root / "RECOVERY_LOCK").exists())
            self.assertFalse((root / "RECOVERY_LOCK.owner-authorized").exists())
            self.assertTrue((root / "REPARTITION_LOCK").exists())


class ComputerDescriptionTests(unittest.TestCase):
    """Windows 11 is named so, on a Python before 3.12 that calls it "10"."""

    def describe(self, release, build, product_type=1):
        from types import SimpleNamespace
        import tools.autoflash_cli as autoflash
        from tools.autoflash import hostsys
        winver = SimpleNamespace(product_type=product_type)
        with mock.patch.object(hostsys, "IS_WINDOWS", True), \
                mock.patch.object(autoflash.platform, "release", return_value=release), \
                mock.patch.object(autoflash.platform, "version", return_value=build), \
                mock.patch.object(autoflash.sys, "getwindowsversion",
                                  return_value=winver, create=True):
            return autoflash.computer_description()

    def test_windows_10_stays_10(self):
        self.assertEqual(self.describe("10", "10.0.19045"), "Windows 10 (build 10.0.19045)")

    def test_windows_11_called_10_by_an_older_python(self):
        self.assertEqual(self.describe("10", "10.0.22631"), "Windows 11 (build 10.0.22631)")

    def test_windows_11_named_by_python_3_12(self):
        self.assertEqual(self.describe("11", "10.0.26100"), "Windows 11 (build 10.0.26100)")

    def test_a_server_is_not_called_11(self):
        self.assertEqual(self.describe("10", "10.0.26100", product_type=3),
                         "Windows 10 (build 10.0.26100)")


class TasmotaWebPasswordTests(unittest.TestCase):
    """A plug on Tasmota with a web password: asked for it, before anything
    needs it, where someone can type it; stopped, saying what to give, where
    nobody can.  Each try is checked on the plug."""

    def setUp(self):
        import tools.autoflash_cli as autoflash
        self.af = autoflash
        self.private = mock.Mock()
        for patch in (mock.patch.object(autoflash, "say"), mock.patch.object(autoflash, "tell"),
                      mock.patch.object(autoflash, "section"),
                      mock.patch.object(autoflash.output, "transient_begin"),
                      mock.patch.object(autoflash.output, "transient_end"),
                      mock.patch.object(autoflash.output, "private", self.private),
                      mock.patch.object(autoflash.prompts, "UNATTENDED", False),
                      mock.patch.object(autoflash.prompts, "answered_unattended"),
                      mock.patch.object(autoflash, "adopt_plug"),
                      mock.patch.object(autoflash, "converted_here", return_value=False),
                      mock.patch.object(autoflash.plugs, "PLUGS", Path("/nonexistent/plugs")),
                      mock.patch.object(autoflash.hostsys, "local_ip_towards", return_value="192.168.0.10"),
                      mock.patch.object(autoflash.hostsys, "mac_of", return_value="12:34:56:ab:cd:ef"),
                      # A question no test expects fails it, rather than waiting on a keyboard.
                      mock.patch.object(autoflash, "ask_choice", side_effect=AssertionError("asked")),
                      mock.patch.object(autoflash, "ask_secret", side_effect=AssertionError("asked"))):
            patch.start()
            self.addCleanup(patch.stop)
        # The plug's Tasmota wants this password, and none other - until it is
        # cleared on the plug (None).
        self.wanted = "s3cret-71"

        def plug(host, user=None, password=None, timeout=10.0):
            return mock.Mock(host=host, wants_password=mock.Mock(
                side_effect=lambda *_: self.wanted is not None and password != self.wanted))
        patch = mock.patch.object(autoflash, "Device", side_effect=plug)
        patch.start()
        self.addCleanup(patch.stop)

    def ctx(self, **inputs):
        from types import SimpleNamespace
        done = inputs.pop("done", ())
        state = SimpleNamespace(input=inputs.get, set_input=inputs.__setitem__, is_done=lambda step: step in done)
        return SimpleNamespace(state=state, web_password=None)

    def test_a_plug_given_by_address_is_asked_for_it_then_found(self):
        ctx = self.ctx(plug_ip="192.168.0.37")
        answers = {None: "locked", "s3cret-71": "tasmota"}
        with mock.patch.object(self.af.detect, "identify",
                               side_effect=lambda host, password=None, **_: answers[password]), \
                mock.patch.object(self.af, "ask_choice", return_value=0) as menu, \
                mock.patch.object(self.af, "ask_secret", side_effect=["not-it-9", "s3cret-71"]) as asked:
            self.assertEqual(self.af.find_current_firmware(ctx), "tasmota")
        self.assertEqual(asked.call_count, 2)  # the wrong one refused on the plug
        self.assertEqual(menu.call_args.args[1][0], "Type its web password")
        self.assertIn("once you have removed it yourself", menu.call_args.args[1][1])
        self.assertEqual(ctx.web_password, "s3cret-71")
        self.private.assert_any_call("s3cret-71", "tasmota-password")  # kept out of the log

    def test_unattended_it_stops_saying_what_to_give(self):
        with mock.patch.object(self.af.prompts, "UNATTENDED", True), \
                mock.patch.object(self.af.detect, "identify", return_value="locked"), \
                mock.patch.object(self.af, "ask_secret") as asked:
            with self.assertRaisesRegex(self.af.Abort, "web password.*--web-password.*AUTOFLASH_TASMOTA_PASSWORD"):
                self.af.find_current_firmware(self.ctx(plug_ip="192.168.0.37"))
        asked.assert_not_called()

    def test_asked_again_until_it_takes_one_or_the_run_is_stopped(self):
        with mock.patch.object(self.af.detect, "identify", return_value="locked"), \
                mock.patch.object(self.af, "ask_choice", side_effect=[0, 0, self.af.QuitRun("quit")]), \
                mock.patch.object(self.af, "ask_secret", return_value="not-it-9") as asked:
            with self.assertRaises(self.af.QuitRun):
                self.af.find_current_firmware(self.ctx(plug_ip="192.168.0.37"))
        self.assertEqual(asked.call_count, 2)

    def test_trying_again_once_it_is_cleared_goes_on_without_one(self):
        ctx = self.ctx(plug_ip="192.168.0.37")

        choices = []

        def try_again(*_):
            choices.append(1)
            if len(choices) == 2:
                self.wanted = None  # cleared on the plug before choosing again
            return 1
        answers = {None: "locked", "s3cret-71": "tasmota"}
        with mock.patch.object(self.af.detect, "identify",
                               side_effect=lambda host, password=None, **_: answers[password]
                               if self.wanted else "tasmota"), \
                mock.patch.object(self.af, "ask_choice", side_effect=try_again), \
                mock.patch.object(self.af, "ask_secret") as asked:
            self.assertEqual(self.af.find_current_firmware(ctx), "tasmota")
        asked.assert_not_called()
        self.assertIsNone(ctx.web_password)
        self.af.tell.assert_any_call("    Tasmota there still has a web password.")  # tried too soon

    def test_found_on_the_network_it_is_offered_and_asked_for_once_chosen(self):
        locked = self.af.detect.Found("192.168.0.42", "locked", "12:34:56:ab:cd:ef")
        with mock.patch.object(self.af.detect, "converted_s60s", return_value=[locked]), \
                mock.patch.object(self.af.detect, "identify", return_value="tasmota") as identify, \
                mock.patch.object(self.af, "ask_choice", side_effect=[0, 0]) as menu, \
                mock.patch.object(self.af, "ask_secret", return_value="s3cret-71"):
            self.assertEqual(self.af.find_current_firmware(self.ctx()), "tasmota")
        self.assertIn("web password", menu.call_args_list[0].args[1][0])  # the plug, then its password
        identify.assert_called_with("192.168.0.42", password="s3cret-71")
        chosen = self.af.adopt_plug.call_args.args[1]
        self.assertEqual((chosen.host, chosen.firmware), ("192.168.0.42", "tasmota"))

    def test_unattended_one_found_locked_is_left_out_and_named(self):
        found = [self.af.detect.Found("192.168.0.37", "tasmota", "12:34:56:ab:cd:ef", "tasmota-ABCDEF"),
                 self.af.detect.Found("192.168.0.42", "locked", "aa:bb:cc:dd:ee:ff")]
        with mock.patch.object(self.af.prompts, "UNATTENDED", True), \
                mock.patch.object(self.af.detect, "converted_s60s", return_value=found), \
                mock.patch.object(self.af, "ask_choice") as menu:
            self.assertEqual(self.af.find_current_firmware(self.ctx()), "tasmota")
        menu.assert_not_called()  # the one S60 that answered is the plug, as before
        self.assertEqual(self.af.adopt_plug.call_args.args[1].host, "192.168.0.37")
        told = " ".join(str(call.args[0]) for call in self.af.tell.call_args_list)
        self.assertIn("192.168.0.42", told)
        self.assertIn("--web-password", told)

    def test_unattended_a_plug_this_computer_converted_found_locked_stops_the_run(self):
        # Gone on to Sonoff's route instead, the run would fail later, and less clearly.
        locked = [self.af.detect.Found("192.168.0.42", "locked", "12:34:56:ab:cd:ef")]
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "12-34-56-ab-cd-ef").mkdir()
            with mock.patch.object(self.af.prompts, "UNATTENDED", True), \
                    mock.patch.object(self.af.plugs, "PLUGS", Path(directory)), \
                    mock.patch.object(self.af.detect, "converted_s60s", return_value=locked):
                with self.assertRaisesRegex(self.af.Abort, "192.168.0.42 has a web password.*--web-password"):
                    self.af.find_current_firmware(self.ctx())
            # One it does not know is only named: the plug may be on Sonoff's firmware still.
            with mock.patch.object(self.af.prompts, "UNATTENDED", True), \
                    mock.patch.object(self.af.plugs, "PLUGS", Path(directory) / "none"), \
                    mock.patch.object(self.af.detect, "converted_s60s", return_value=locked):
                self.assertEqual(self.af.find_current_firmware(self.ctx()), "sonoff")

    def test_a_run_carried_on_from_tasmota_is_asked_before_any_step(self):
        ctx = self.ctx(plug_ip="192.168.0.37", source="tasmota")
        ctx.device = mock.Mock(wants_password=mock.Mock(return_value=True))
        with mock.patch.object(self.af, "ask_choice", return_value=0), \
                mock.patch.object(self.af, "ask_secret", return_value="s3cret-71"):
            self.af.unlock_tasmota(ctx)
        self.assertEqual(ctx.web_password, "s3cret-71")
        # Past L1 the plug runs only firmware this tool put on; from ESPHome, none of the plug's Tasmota.
        for inputs in ({"source": "tasmota", "done": ("L1",)}, {"source": "esphome"}, {}):
            ctx = self.ctx(plug_ip="192.168.0.37", **inputs)
            ctx.device = mock.Mock()
            self.af.unlock_tasmota(ctx)
            ctx.device.wants_password.assert_not_called()


class EsphomeUpdatePasswordTests(unittest.TestCase):
    """A plug on ESPHome with an update password: asked for it before the
    go-ahead where someone can type it - or tried again without one, once
    its owner has removed it - and stopped, saying what to give, where nobody
    can.  The password goes with the update."""

    def setUp(self):
        import tools.autoflash_cli as autoflash
        self.af = autoflash
        self.private = mock.Mock()
        for patch in (mock.patch.object(autoflash, "say"), mock.patch.object(autoflash, "tell"),
                      mock.patch.object(autoflash, "section"),
                      mock.patch.object(autoflash.output, "transient_begin"),
                      mock.patch.object(autoflash.output, "transient_end"),
                      mock.patch.object(autoflash.output, "progress"),
                      mock.patch.object(autoflash.output, "progress_clear"),
                      mock.patch.object(autoflash.output, "private", self.private),
                      mock.patch.object(autoflash.prompts, "UNATTENDED", False),
                      # A question no test expects fails it, rather than waiting on a keyboard.
                      mock.patch.object(autoflash, "ask_choice", side_effect=AssertionError("asked")),
                      mock.patch.object(autoflash, "ask_secret", side_effect=AssertionError("asked"))):
            patch.start()
            self.addCleanup(patch.stop)
        # ESPHome on the plug wants this update password, until its owner
        # removes it (None) - or takes only encrypted updates, while encrypted.
        self.wanted, self.encrypted = "s3cret-71", False
        ota = autoflash.esphome_ota

        def wants_password(host, password=None, **_):
            if self.encrypted:
                raise ota.NeedsEncryption()
            return self.wanted is not None and password != self.wanted

        def probe(host, **_):
            return ota.Offer(2, True, True, False, encrypted_only=self.encrypted)
        for patch in (mock.patch.object(ota, "wants_password", side_effect=wants_password),
                      mock.patch.object(ota, "probe", side_effect=probe)):
            patch.start()
            self.addCleanup(patch.stop)

    def ctx(self, password=None):
        from types import SimpleNamespace
        inputs = {"plug_ip": "192.168.0.50"}
        state = SimpleNamespace(input=inputs.get, set_input=inputs.__setitem__, is_done=lambda step: False)
        return SimpleNamespace(state=state, esphome_password=password)

    def test_asked_for_before_the_go_ahead_and_sent_with_the_update(self):
        ctx = self.ctx()
        with mock.patch.object(self.af, "ask_choice", return_value=0) as menu, \
                mock.patch.object(self.af, "ask_secret", side_effect=["not-it-9", "s3cret-71"]) as asked:
            self.af.unlock_esphome(ctx)
        self.assertEqual(asked.call_count, 2)  # the wrong one refused on the plug
        self.assertEqual(ctx.esphome_password, "s3cret-71")
        self.assertEqual(menu.call_args.args[1][0], "Type its update password")
        self.assertIn("once you have removed it yourself", menu.call_args.args[1][1])
        self.private.assert_any_call("s3cret-71", "esphome-password")  # kept out of the log
        with mock.patch.object(self.af.esphome_ota, "upload") as upload:
            self.af.send_as_esphome_update(ctx, b"image", "the startup firmware")
        self.assertEqual(upload.call_args.kwargs["password"], "s3cret-71")

    def test_given_in_advance_it_is_not_asked(self):
        ctx = self.ctx("s3cret-71")  # AUTOFLASH_ESPHOME_OTA_PASSWORD, in .env or the environment
        self.af.unlock_esphome(ctx)
        self.assertEqual(ctx.esphome_password, "s3cret-71")
        self.wanted = None
        self.af.unlock_esphome(self.ctx())  # and a plug with none asks nothing

    def test_unattended_without_it_the_run_stops_saying_what_to_give(self):
        with mock.patch.object(self.af.prompts, "UNATTENDED", True):
            with self.assertRaisesRegex(self.af.Abort,
                                        "update password: give it with --esphome-ota-password"):
                self.af.unlock_esphome(self.ctx())
            with self.assertRaisesRegex(self.af.Abort, "the one given is not it"):
                self.af.unlock_esphome(self.ctx("not-it-9"))

    def test_trying_again_once_it_is_removed_goes_on_without_one(self):
        ctx = self.ctx()
        choices = []

        def try_again(*_):
            choices.append(1)
            if len(choices) == 2:
                self.wanted = None  # removed from its config, and that installed, before choosing again
            return 1
        with mock.patch.object(self.af, "ask_choice", side_effect=try_again):
            self.af.unlock_esphome(ctx)
        self.assertIsNone(ctx.esphome_password)
        self.af.tell.assert_any_call("    ESPHome there still has an update password.")  # tried too soon

    def test_an_update_refused_for_it_later_is_asked_for_and_sent_again(self):
        ctx = self.ctx()  # none at the go-ahead: an X2 sending the startup firmware once more, say
        refused = self.af.esphome_ota.NeedsPassword(given=False)
        with mock.patch.object(self.af.esphome_ota, "upload", side_effect=[refused, None]) as upload, \
                mock.patch.object(self.af, "ask_choice", return_value=0), \
                mock.patch.object(self.af, "ask_secret", return_value="s3cret-71"):
            self.af.send_as_esphome_update(ctx, b"image", "the startup firmware")
        self.assertEqual([call.kwargs["password"] for call in upload.call_args_list], [None, "s3cret-71"])

    def test_one_taking_only_encrypted_updates_is_waited_on_until_changed(self):
        self.encrypted = True
        ctx = self.ctx()
        choices = []

        def choose(*_):
            choices.append(1)
            if len(choices) == 2:
                self.encrypted = False  # encryption: removed, an update password put instead
            return 0
        with mock.patch.object(self.af, "ask_choice", side_effect=choose), \
                mock.patch.object(self.af, "ask_secret", return_value="s3cret-71"):
            self.af.unlock_esphome(ctx)
        self.assertEqual(ctx.esphome_password, "s3cret-71")  # then asked for that
        self.af.tell.assert_any_call("    ESPHome there still takes only encrypted updates.")
        self.encrypted = True
        with mock.patch.object(self.af.prompts, "UNATTENDED", True):
            with self.assertRaisesRegex(self.af.Abort,
                                        "only encrypted updates.*ESPHome YAML.*remove that encryption: line"):
                self.af.unlock_esphome(self.ctx())
