import tempfile
import unittest
from pathlib import Path

from tools.autoflash.phases import ARM_RE, LOADER_RE, PhaseRunner, PhaseError, short_loader
from tools.autoflash.state import DONE, FAILED, RunState


class RunStateTests(unittest.TestCase):
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
        import tools.s60_autoflash as autoflash

        ids = [step for step, _, _ in autoflash.STEPS]
        self.assertEqual(ids[ids.index("E1"):ids.index("E1") + 3], ["E1", "E2", "E3"])
        state = RunState.open(self.root)
        state.mark("E1", DONE)
        state.mark("E2", DONE)
        reopened = RunState.open(self.root)
        self.assertTrue(reopened.is_done("E2"))
        self.assertFalse(reopened.is_done("E3"))

    def test_every_destructive_step_records_an_attempt_first(self):
        import tools.s60_autoflash as autoflash

        known = {step for step, _, _ in autoflash.STEPS}
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
        import tools.s60_autoflash as autoflash

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
        return SimpleNamespace(device=device, manifest_path=Path("unused"))

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

        def drain(seconds):
            nonlocal output
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                import select
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        output += os.read(master, 4096)
                    except OSError:
                        return

        drain(1.0)
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
        import tools.s60_autoflash as autoflash

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
        import tools.s60_autoflash as autoflash

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
        import tools.s60_autoflash as autoflash
        with self.assertRaisesRegex(autoflash.Abort, "not a Sonoff S60"):
            self.discover_with_model("SN-ESP32-MINIR4")


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


class PhaseDisplayTests(unittest.TestCase):
    """Hundreds of server request lines become a few readable ones."""

    def run_lines(self, phase, blocks, lines):
        from tools.autoflash.phases import PhaseDisplay
        written, logged = [], []
        display = PhaseDisplay(phase, blocks, write=written.append, log=logged.append)
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

    def test_refusals_are_always_shown(self):
        _, logged = self.run_lines("commit", None, ["  REFUSED /chunk/safeboot/9: outside the artifact",
                                                    "REFUSING: could not bind server"])
        self.assertEqual(len(logged), 2)


class RunChoiceTests(unittest.TestCase):
    """An unfinished conversion is offered back, never resumed or dropped silently."""

    def setUp(self):
        from unittest import mock
        import tools.s60_autoflash as autoflash

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
        self.assertIn("stopped at step D1", options[0])
        self.assertIn("past the point of no return", options[0])

    def test_starting_a_new_plug_leaves_the_unfinished_run_alone(self):
        self.run_dir("half", ["A0", "A5"])
        name, menu = self.choose(1)
        self.assertNotEqual(name, "half")
        self.assertNotIn("past the point of no return", menu.call_args.args[1][0])
        self.assertTrue((self.root / "half").is_dir())

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
        import tools.s60_autoflash as autoflash
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

    def ctx(self):
        from types import SimpleNamespace
        import tools.s60_autoflash as autoflash
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        return SimpleNamespace(state=RunState.open(root), evidence=root / "live",
                               manifest_path=(autoflash.ROOT / autoflash.DEFAULT_MANIFEST).resolve(),
                               wifi=lambda: ("Home-2G", "password"))

    def test_a5_asks_nothing_about_wifi(self):
        # The details are read off the plug at E1, so A5 has nothing to ask.
        from unittest import mock
        import tools.s60_autoflash as autoflash
        with mock.patch.object(autoflash, "validate_bundle", return_value={}), \
             mock.patch.object(autoflash.hostsys, "check_sha256sums", return_value=[]), \
             mock.patch.object(autoflash, "ask_choice") as menu, \
             mock.patch.object(autoflash, "make_bundle") as rebuild, \
             mock.patch.object(autoflash, "say"):
            autoflash.step_check(self.ctx())
        menu.assert_not_called()
        rebuild.assert_not_called()

    def test_a5_builds_a_missing_bundle_without_asking_for_wifi(self):
        # A first run: the bundle is built with no Wi-Fi; E1 adds it, read off the plug.
        from unittest import mock
        import tools.s60_autoflash as autoflash
        with mock.patch.object(autoflash, "validate_bundle",
                               side_effect=[ValueError("no manifest"), {}]), \
             mock.patch.object(autoflash, "build_bundle") as build, \
             mock.patch.object(autoflash.hostsys, "check_sha256sums", return_value=[]), \
             mock.patch.object(autoflash, "ask_credentials") as credentials, \
             mock.patch.object(autoflash, "ask_secret") as secret, \
             mock.patch.object(autoflash, "section"), mock.patch.object(autoflash, "say"):
            autoflash.step_check(self.ctx())
        build.assert_called_once()
        self.assertNotIn("ssid", build.call_args.kwargs)
        self.assertEqual(build.call_args.args, ())
        credentials.assert_not_called()
        secret.assert_not_called()

    def test_no_plug_found_offers_to_look_again(self):
        from unittest import mock
        import tools.s60_autoflash as autoflash
        ctx = self.ctx()
        with mock.patch.object(autoflash, "find_ewelink_plugs",
                               side_effect=[[], [("10.0.0.5", "eWeLink device abc")]]), \
             mock.patch.object(autoflash, "ask_choice", return_value=0), \
             mock.patch.object(autoflash.hostsys, "mac_of", return_value="aa:bb:cc:dd:ee:ff"), \
             mock.patch.object(autoflash, "section"), mock.patch.object(autoflash, "say"):
            result = autoflash.step_pair(ctx)
        self.assertEqual(result["plug_ip"], "10.0.0.5")


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
        from tools import s60_autoflash
        from tools.autoflash import setup_network

        polls = iter(addresses)
        ctx = SimpleNamespace(finder=SimpleNamespace(poll=lambda: next(polls, None)))
        clock = iter(range(0, 10_000, 2))
        with mock.patch.object(setup_network, "page", side_effect=lambda host, **_: pages.get(host)), \
             mock.patch.object(s60_autoflash.time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(s60_autoflash.time, "sleep"):
            return s60_autoflash.bridge_on_home_network(ctx, timeout=timeout)

    def test_the_bridge_is_found_where_the_plug_was(self):
        bridge = "<title>S60 OTA Bridge</title>"
        self.assertEqual(self.find([None, "192.168.1.51"], {"192.168.1.51": bridge}), "192.168.1.51")

    def test_stock_firmware_at_that_address_is_not_the_bridge(self):
        # Before the bridge starts, the plug answers there with no web page.
        self.assertIsNone(self.find(["192.168.1.51"] * 20, {}))

    def test_no_plug_on_the_network_falls_back_to_joining(self):
        self.assertIsNone(self.find([None] * 20, {}))


class TasmotaWifiPresetTests(unittest.TestCase):
    """D1 gives Tasmota the Wi-Fi through the bridge when it safely can."""

    def setUp(self):
        from types import SimpleNamespace
        from tools.autoflash import tasmota_settings as ts
        self.ts = ts
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ctx = SimpleNamespace()

    def run_preset(self, template, send_error=None):
        from unittest import mock
        from tools import s60_autoflash
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
             mock.patch.object(s60_autoflash, "say"):
            return s60_autoflash.give_tasmota_the_wifi(self.ctx, "192.168.1.51"), sent

    def test_with_a_template_the_bridge_gets_it(self):
        self.assertEqual(self.run_preset(b"T" * 4096), (True, [b"T" * 4096]))

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
        from tools import s60_autoflash
        from tools.autoflash import setup_network
        pages = iter(["<title>S60 OTA Bridge</title>", None, "<title>Tasmota</title> tasmota"])
        state = SimpleNamespace(set_input=lambda *a: None, input=lambda *a: None)
        ctx = SimpleNamespace(finder=SimpleNamespace(poll=lambda: "192.168.1.51"),
                              set_plug_ip=lambda ip: None, state=state)
        clock = iter(range(0, 10_000, 3))
        with mock.patch.object(setup_network, "page", side_effect=lambda *a, **k: next(pages)), \
             mock.patch.object(s60_autoflash.time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(s60_autoflash.time, "sleep"), \
             mock.patch.object(s60_autoflash, "Device", side_effect=OSError), \
             mock.patch.object(s60_autoflash, "say"):
            self.assertEqual(s60_autoflash.tasmota_on_home_network(ctx), "192.168.1.51")


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
        import tools.s60_autoflash as autoflash
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
        import tools.s60_autoflash as autoflash
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
        import tools.s60_autoflash as autoflash
        for kwargs, words in (({"connected": None}, "did not say which network"),
                              ({"fail": "command"}, "did not answer as Tasmota"),
                              ({"connected": "Guest"}, "not the network in its first slot")):
            with mock.patch.object(autoflash, "say") as said:
                self.assertIsNone(autoflash.read_wifi_from_plug(self.ctx(**kwargs)))
            self.assertIn(words, " ".join(str(c.args[0]) for c in said.call_args_list), kwargs)


class WifiFallbackTests(unittest.TestCase):
    """Typing the Wi-Fi is the fallback: explained, asked once, and needs no rebuild."""

    def ctx(self, *, found=None, bundle_for=None):
        from types import SimpleNamespace
        from unittest import mock
        import tools.s60_autoflash as autoflash
        ctx = SimpleNamespace(args=SimpleNamespace(wifi=False), manifest_path=Path("unused"),
                              state=SimpleNamespace(set_input=lambda *_: None),
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
        import tools.s60_autoflash as autoflash
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
        import tools.s60_autoflash as autoflash
        ctx = self.ctx(found=("Home-2G", ""))
        recorded = {}
        ctx.state.set_input = lambda key, value: recorded.__setitem__(key, value)
        with mock.patch.object(autoflash, "recovery_image", return_value=b"image"):
            self.assertEqual(autoflash.Context.wifi(ctx), ("Home-2G", ""))
        self.assertIs(recorded.get("wifi_typed"), False)

    def test_when_they_cannot_be_read_the_reason_is_shown_then_both_are_asked(self):
        from unittest import mock
        import tools.s60_autoflash as autoflash
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
        import tools.s60_autoflash as autoflash
        ctx = self.ctx(found=("Home-2G", "pw"))
        with mock.patch.object(autoflash, "recovery_image", side_effect=autoflash.BundleError("x")), \
             mock.patch.object(autoflash, "pin_wifi", side_effect=autoflash.BundleError("old")), \
             mock.patch.object(autoflash, "build_bundle", return_value=(Path("m"), b"image")) as rebuild:
            autoflash.Context.wifi(ctx)
        rebuild.assert_called_once()


class EwelinkAccountTests(unittest.TestCase):
    """The last eWeLink account is offered, not assumed."""

    def email(self, *, remembered=None, chosen=None, choice=0, typed="new@example.com"):
        from types import SimpleNamespace
        from unittest import mock
        import tools.s60_autoflash as autoflash
        inputs = {"email": chosen} if chosen else {}
        ctx = SimpleNamespace(state=SimpleNamespace(input=inputs.get,
                                                    set_input=inputs.__setitem__),
                              remember_email=lambda e: inputs.__setitem__("email", e))
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
        import tools.s60_autoflash as autoflash
        sent = []
        device = SimpleNamespace(wait_reachable=lambda **_: None, started_at=lambda: "t",
                                 uptime_seconds=lambda: 10, wait_for_reboot=lambda **_: 1.0,
                                 command=lambda c: sent.append(c))
        state = SimpleNamespace(input=lambda key: {"product_model": "S60TPF"}.get(key))
        ctx = SimpleNamespace(finder=SimpleNamespace(poll=lambda: "192.168.1.51"), device=device, state=state)
        with mock.patch.object(autoflash, "say") as said:
            result = autoflash.step_configure(ctx)
        self.assertEqual(sent[0], "Reset 4")
        self.assertEqual(sent[1:3], ["SaveData 1", f"Template {autoflash.s60_template('S60TPF')}"])
        self.assertIn("Sonoff S60TPF", " ".join(str(c.args[0]) for c in said.call_args_list))
        self.assertEqual(result["wifi_route"], "joined by itself")
        self.assertIn("joined your Wi-Fi by itself", " ".join(str(c.args[0]) for c in said.call_args_list))


class StartupSettingsReportTests(unittest.TestCase):
    """F2 keeps what the startup firmware said about saving the Wi-Fi."""

    def report(self, lines=None, error=None):
        from types import SimpleNamespace
        from unittest import mock
        import tools.s60_autoflash as autoflash

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
        import tools.s60_autoflash as autoflash
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
        import tools.s60_autoflash as autoflash
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
        import tools.s60_autoflash as autoflash
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
        import tools.s60_autoflash as autoflash
        self.assertEqual(
            autoflash.s60_template("S60TPG"),
            '{"NAME":"Sonoff S60TPG","GPIO":[1,1,1,1,224,544,1,3104,1,32,1,0,0,0,0,0,0,0,'
            '1,1,1,1],"FLAG":0,"BASE":1}',
        )


class BridgeEvidenceTests(unittest.TestCase):
    """Bridge v6 raises its own network only when the home one fails."""

    def test_bridge_on_the_home_network_counts(self):
        from unittest import mock
        from tools import s60_autoflash
        with mock.patch.object(s60_autoflash, "bridge_on_home_network", return_value="192.168.0.50"), \
             mock.patch.object(s60_autoflash, "find_ssid", return_value=None):
            installed, reason = s60_autoflash.evidence_bridge_installed(mock.Mock())
        self.assertTrue(installed)
        self.assertIn("192.168.0.50", reason)

    def test_its_own_network_still_counts(self):
        from unittest import mock
        from tools import s60_autoflash
        with mock.patch.object(s60_autoflash, "bridge_on_home_network", return_value=None), \
             mock.patch.object(s60_autoflash, "find_ssid", return_value="S60-OTA-Bridge-A04D"):
            installed, _ = s60_autoflash.evidence_bridge_installed(mock.Mock())
        self.assertTrue(installed)

    def test_neither_is_not_proof_of_failure(self):
        from unittest import mock
        from tools import s60_autoflash
        with mock.patch.object(s60_autoflash, "bridge_on_home_network", return_value=None), \
             mock.patch.object(s60_autoflash, "find_ssid", return_value=None):
            installed, _ = s60_autoflash.evidence_bridge_installed(mock.Mock())
        self.assertIsNone(installed)
