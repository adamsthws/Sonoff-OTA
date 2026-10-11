import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.autoflash import output, prompts


class FakeTerminal(io.StringIO):
    def isatty(self):
        return True


class OutputTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "run.log"
        patcher = mock.patch.dict(os.environ, {"NO_COLOR": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        output.finish()
        self.tmp.cleanup()

    def run_with(self, stdout, verbose, body):
        with mock.patch("sys.stdout", stdout):
            output.start(self.log, verbose=verbose)
            body()
            output.finish()
        return stdout.getvalue(), self.log.read_text()


class ConciseTests(OutputTestCase):
    """By default the screen gets stage lines and what matters; the log gets everything."""

    def stages(self):
        output.stage_begin(1, 2, "Checking the files")
        output.detail("bundle and pinned artifacts: OK")
        output.stage_end("complete")
        output.stage_begin(2, 2, "Installing Tasmota")
        output.show("Join the bridge's Wi-Fi network")
        output.stage_end("complete")

    def test_piped_output_gets_one_line_per_change(self):
        screen, log = self.run_with(io.StringIO(), False, self.stages)
        self.assertIn("Stage 1 of 2 - Checking the files - Underway", screen)
        self.assertIn("Stage 1 of 2 - Checking the files - Complete (0s)", screen)
        self.assertIn("Join the bridge's Wi-Fi network", screen)
        self.assertNotIn("bundle and pinned artifacts", screen)
        self.assertIn("bundle and pinned artifacts: OK", log)
        self.assertRegex(log, r"\[\d\d:\d\d:\d\d\] Stage 2 of 2 - Installing Tasmota - complete")

    def test_on_a_terminal_underway_becomes_complete_on_the_same_line(self):
        def body():
            output.stage_begin(1, 1, "Checking the files")
            output.stage_end("complete")
        screen, _ = self.run_with(FakeTerminal(), False, body)
        drawn = screen.split("\n")[0]
        self.assertIn("Checking the files - Underway", drawn)
        self.assertTrue(drawn.split("\r")[-1].startswith(" Stage 1 of 1 - Checking the files - Complete"))

    def test_stage_lines_line_up_in_columns(self):
        def body():
            output.set_stage_width(len("Installing official Tasmota"))
            for number, name in ((1, "Checking you have everything"[:20]), (10, "Installing official Tasmota")):
                output.stage_begin(number, 14, name)
                output.stage_end("complete")
        screen, log = self.run_with(io.StringIO(), False, body)
        done = [line for line in screen.splitlines() if line.endswith("Complete (0s)")]
        self.assertEqual(len(done), 2)
        self.assertEqual(done[0].index(" - Complete"), done[1].index(" - Complete"))
        self.assertEqual(done[0].index(" - Checking"), done[1].index(" - Installing"))
        self.assertTrue(done[0].startswith(" Stage  1 of 14 - "))
        self.assertIn("Stage 1 of 14 - Checking you have ev - complete", log)  # the log is not padded

    def test_an_answered_question_is_taken_off_the_screen(self):
        def body():
            with mock.patch.object(output, "colour_ok", return_value=True), \
                 mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 24))):
                output.stage_begin(5, 14, "Installing the installer firmware")
                output.transient_begin()          # the underway line: 1 row
                prompts.warning_box("Before going on", ["One line."])  # 3 rows
                output.before_input("Type PROCEED to go ahead: ")
                output.answered("proceed")        # prompt and answer: 1 row
                output.transient_end()
                output.stage_end("complete")
        screen, log = self.run_with(FakeTerminal(), False, body)
        self.assertIn("\x1b[1F\x1b[2K" * 5, screen)
        self.assertNotIn("\x1b[1F\x1b[2K" * 6, screen)
        self.assertNotIn("\x1b[J", screen)   # never an erase that could clear the whole screen
        self.assertIn("Before going on", log)
        self.assertIn("-> proceed", log)

    def test_a_question_taller_than_the_window_stays_whole(self):
        def body():
            with mock.patch.object(output, "colour_ok", return_value=True), \
                 mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 5))):
                output.stage_begin(5, 14, "Installing the installer firmware")
                output.transient_begin()          # the underway line: 1 row
                prompts.warning_box("Before going on", ["One line."])  # 3 rows
                output.before_input("Type PROCEED to go ahead: ")
                output.answered("proceed")        # prompt and answer: 1 row - 5 in all
                output.transient_end()
                output.stage_end("complete")
        screen, _ = self.run_with(FakeTerminal(), False, body)
        # The cursor cannot reach the block's first row: erasing would strand it.
        self.assertNotIn("\x1b[1F", screen)
        self.assertIn("Before going on", screen)
        self.assertIn("Installing the installer firmware - Complete", output.ANSI_RE.sub("", screen))

    def ask_address(self, rows):
        """Two addresses that do not answer, then one that does, in a window `rows` tall."""
        def body():
            with mock.patch.object(output, "colour_ok", return_value=True), \
                 mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, rows))), \
                 mock.patch.object(prompts, "ping_ok", side_effect=[False, False, True]), \
                 mock.patch("builtins.input", side_effect=["10.0.0.1", "n", "10.0.0.2", "n", "10.0.0.3"]):
                output.stage_begin(3, 14, "Discovering the plug")
                output.transient_begin()
                self.assertEqual(prompts.ask_ipv4("The plug's address", help_lines=["Where to look."],
                                                  must_respond=True), "10.0.0.3")
                output.transient_end()
                output.stage_end("complete")
        return self.run_with(FakeTerminal(), False, body)

    def test_an_address_asked_again_takes_the_last_try_away(self):
        # A short window, as a terminal panel often is: three tries piled up
        # would be taller than it, and stay on the screen for good.
        screen, log = self.ask_address(12)
        self.assertEqual(screen.count("Where to look."), 3)  # explained again each time
        tail = screen.rsplit("Where to look.", 1)[1]
        self.assertIn("\x1b[1F\x1b[2K", tail)                # the block went at the end
        self.assertEqual(log.count("answered a ping"), 2)    # the log keeps every try

    def test_no_clock_drawn_in_an_open_block(self):
        def body():
            with mock.patch.object(output, "colour_ok", return_value=True), \
                 mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 24))):
                output.stage_begin(3, 14, "Discovering the plug")
                output.transient_begin()
                before = sys.stdout.getvalue()
                output._tick()  # while the block's note is still being worked out
                self.assertEqual(sys.stdout.getvalue(), before)
                output.transient_end()
                output._tick()  # the block gone, the clock goes on
                self.assertIn("Underway", sys.stdout.getvalue()[len(before):])
                output.stage_end("complete")
        self.run_with(FakeTerminal(), False, body)

    def test_a_short_window_is_noticed(self):
        found = {}
        def body():
            with mock.patch.object(output, "colour_ok", return_value=True):
                for rows in (12, output.MIN_ROWS):
                    with mock.patch("shutil.get_terminal_size",
                                    return_value=os.terminal_size((80, rows))):
                        found[rows] = output.short_window()
        self.run_with(FakeTerminal(), False, body)
        self.assertEqual(found, {12: 12, output.MIN_ROWS: None})

    def test_a_short_window_does_not_matter_when_nothing_is_cleared(self):
        found = []
        def body():
            with mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 12))):
                found.append(output.short_window())
        self.run_with(FakeTerminal(), True, body)   # verbose: everything stays anyway
        self.run_with(io.StringIO(), False, body)  # not a terminal
        self.assertEqual(found, [None, None])

    def test_a_short_window_is_asked_to_grow_and_waited_for(self):
        height = [12]
        size = lambda *a: os.terminal_size((80, height[0]))
        waited = []
        def wait(prompt, *, until):
            for rows in (16, 24):  # made taller: still short, then tall enough
                self.assertFalse(until())
                height[0] = rows
            self.assertTrue(until())
            waited.append(prompt)
        stdout = FakeTerminal()
        with mock.patch("sys.stdout", stdout), \
             mock.patch.object(output, "colour_ok", return_value=True), \
             mock.patch("shutil.get_terminal_size", side_effect=size), \
             mock.patch.object(prompts, "wait_for_key", side_effect=wait):
            prompts.wait_for_taller_window()
        self.assertEqual(len(waited), 1)
        self.assertIn("It is 12 rows tall; make it at least 20", stdout.getvalue())

    def test_a_tall_enough_window_is_not_mentioned(self):
        stdout = FakeTerminal()
        with mock.patch("sys.stdout", stdout), \
             mock.patch.object(output, "colour_ok", return_value=True), \
             mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 30))), \
             mock.patch.object(prompts, "wait_for_key") as wait:
            prompts.wait_for_taller_window()
        wait.assert_not_called()
        self.assertEqual(stdout.getvalue(), "")

    def test_no_clock_under_an_address_while_it_is_pinged(self):
        # Its line and the gap after it made a try two rows taller: in a short
        # window the try's first row scrolled away, where it could not be erased.
        held = []
        def ping(address, *args):
            held.append(output._s.held)
            return address == "10.0.0.2"
        with mock.patch.object(prompts, "ping_ok", side_effect=ping), \
             mock.patch("builtins.input", side_effect=["10.0.0.1", "n", "10.0.0.2"]):
            self.assertEqual(prompts.ask_ipv4("The plug's address", must_respond=True), "10.0.0.2")
        self.assertEqual(held, [True, True])
        self.assertFalse(output._s.held)

    def test_wrong_answers_replace_each_other(self):
        def body():
            with mock.patch.object(output, "colour_ok", return_value=True), \
                 mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 6))), \
                 mock.patch("builtins.input", side_effect=["maybe", "192.168.0.50", "n"]):
                output.stage_begin(3, 14, "Discovering the plug")
                output.transient_begin()
                self.assertFalse(prompts.ask_yes_no("Use it anyway?", default=False))
                output.transient_end()
                output.stage_end("complete")
        screen, log = self.run_with(FakeTerminal(), False, body)
        # Piled up, three tries and two complaints would outgrow the window and stay.
        tail = screen.rsplit("Please answer y or n", 1)[1]
        self.assertIn("\x1b[1F\x1b[2K", tail)
        self.assertEqual(log.count("Please answer y or n"), 2)

    def test_a_try_taller_than_the_window_stays(self):
        screen, _ = self.ask_address(4)
        self.assertEqual(screen.count("Where to look."), 1)  # still there: not repeated
        self.assertNotIn("\x1b[1F", screen)

    def test_success_is_green_and_underway_is_bold(self):
        def body():
            with mock.patch.object(output, "colour_ok", return_value=True):
                output.stage_begin(1, 1, "Checking the files")
                output.stage_end("complete")
        screen, log = self.run_with(FakeTerminal(), False, body)
        self.assertIn(f"{output.BOLD}Underway{output.RESET}", screen)
        self.assertIn(f"{output.GREEN}Stage 1 of 1{output.RESET} - Checking the files - "
                      f"{output.GREEN}Complete{output.RESET} (0s)", screen)
        self.assertIn("Stage 1 of 1 - Checking the files - complete (0s)", log)

    def test_a_stage_done_on_an_earlier_run_is_green_too(self):
        def body():
            with mock.patch.object(output, "colour_ok", return_value=True):
                output.stage_begin(1, 1, "Checking the files")
                output.stage_end("already done")
        screen, _ = self.run_with(FakeTerminal(), False, body)
        self.assertIn(f"{output.GREEN}Stage 1 of 1{output.RESET} - Checking the files - "
                      f"{output.GREEN}Already done{output.RESET}", screen)

    def test_without_cursor_control_the_question_stays(self):
        def body():
            output.stage_begin(5, 14, "Installing the installer firmware")
            output.transient_begin()
            prompts.warning_box("Before going on", ["One line."])
            output.transient_end()
            output.stage_end("complete")
        screen, _ = self.run_with(FakeTerminal(), False, body)   # NO_COLOR is set
        self.assertNotIn("\x1b[", screen)
        self.assertIn("Before going on", screen)

    def test_a_message_mid_stage_starts_on_its_own_line(self):
        def body():
            output.stage_begin(1, 1, "Installing Tasmota")
            output.show("Join the bridge's Wi-Fi network")
            output.stage_end("complete")
        screen, _ = self.run_with(FakeTerminal(), False, body)
        lines = [line.split("\r")[-1] for line in screen.split("\n")]
        self.assertIn(" Join the bridge's Wi-Fi network", lines)
        self.assertTrue(any(line.startswith(" Stage 1 of 1 - Installing Tasmota - Complete") for line in lines))

    def test_progress_stays_off_the_concise_screen_and_the_log(self):
        def body():
            output.stage_begin(1, 1, "Installing Tasmota")
            output.progress("looking for the installer on your network... 12s")
            output.progress_clear()
            output.stage_end("complete")
        screen, log = self.run_with(FakeTerminal(), False, body)
        self.assertNotIn("looking for the installer", screen)
        self.assertNotIn("looking for the installer", log)
        self.assertIn("Installing Tasmota - Underway 0s", screen)

    def test_a_stage_begun_twice_keeps_one_line(self):
        def body():
            output.stage_begin(1, 1, "Installing Tasmota")
            output.stage_begin(1, 1, "Installing Tasmota")
            output.stage_end("already done")
        screen, _ = self.run_with(io.StringIO(), False, body)
        self.assertEqual(screen.count("Underway"), 1)
        self.assertIn("Installing Tasmota - Already done", screen)

    def test_quiet_sections_are_logged_only(self):
        def body():
            prompts.section("What the plug reported", ["Firmware now 1.2.0"], quiet=True)
            prompts.section("Join the plug's setup network", ["Connect to it now."])
        screen, log = self.run_with(io.StringIO(), False, body)
        self.assertNotIn("What the plug reported", screen)
        self.assertIn("What the plug reported", log)
        self.assertIn("Join the plug's setup network", screen)

    def test_the_warning_is_a_short_note(self):
        screen, _ = self.run_with(io.StringIO(), False,
                                  lambda: prompts.warning_box("Before going on", ["One line."]))
        self.assertEqual(screen.strip().splitlines(), ["! Before going on", "     One line."])
        self.assertNotIn("!!", screen)

    def test_block_counters_stay_off_the_concise_screen(self):
        counter = "\r    Copying the startup firmware: block 3/198"
        screen, _ = self.run_with(io.StringIO(), False, lambda: output.raw_progress(counter))
        self.assertNotIn("block 3", screen)


class VerboseTests(OutputTestCase):
    def test_everything_is_shown_and_logged(self):
        def body():
            output.stage_begin(1, 1, "Checking the files")
            output.detail("bundle and pinned artifacts: OK")
            output.stage_end("complete")
        screen, log = self.run_with(io.StringIO(), True, body)
        self.assertIn("== Stage 1 of 1 - Checking the files", screen)
        self.assertIn("bundle and pinned artifacts: OK", screen)
        self.assertIn("bundle and pinned artifacts: OK", log)
        self.assertIn("(verbose)", log)

    def test_outside_a_run_everything_is_shown(self):
        screen = io.StringIO()
        with mock.patch("sys.stdout", screen):
            output.detail("No conversions recorded yet.")
        self.assertIn("No conversions recorded yet.", screen.getvalue())


class StreamTests(OutputTestCase):
    """A helper tool's output goes to the log, and to the screen only when verbose."""

    CHILD = ("import sys, time\n"
             "print('Serving the firmware', flush=True)\n"
             "sys.stdout.write('\\r10%'); sys.stdout.flush()\n"
             "sys.stdout.write('\\r100%\\n'); sys.stdout.flush()\n"
             "print('Done')\n")

    def child(self):
        return subprocess.Popen([sys.executable, "-c", self.CHILD],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def test_concise_keeps_it_off_the_screen(self):
        codes = []
        screen, log = self.run_with(io.StringIO(), False, lambda: codes.append(output.stream(self.child())))
        self.assertEqual(codes, [0])
        self.assertNotIn("Serving the firmware", screen)
        self.assertIn("| Serving the firmware", log)
        self.assertIn("| 100%", log)          # where the redrawn line ended up
        self.assertNotIn("10%\r", log)

    def test_verbose_passes_it_through(self):
        screen, log = self.run_with(io.StringIO(), True, lambda: output.stream(self.child()))
        self.assertIn("Serving the firmware", screen)
        self.assertIn("| Done", log)

    def test_windows_line_endings_keep_the_line(self):
        # A child on Windows ends its lines "\r\n": the "\r" is not a redraw.
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'Served 10 bytes\\r\\nDone\\r\\n')"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        _, log = self.run_with(io.StringIO(), False, lambda: output.stream(child))
        self.assertIn("| Served 10 bytes", log)
        self.assertIn("| Done", log)
        self.assertEqual(output.tool_problems(), ["Served 10 bytes", "Done"])


class InputTests(OutputTestCase):
    def test_questions_and_answers_are_logged(self):
        def body():
            with mock.patch("builtins.input", return_value="y"):
                self.assertTrue(prompts.ask_yes_no("Have you joined it"))
        _, log = self.run_with(io.StringIO(), False, body)
        self.assertIn("? Have you joined it", log)
        self.assertIn("-> y", log)

    def test_a_password_is_never_logged(self):
        def body():
            with mock.patch.object(prompts, "_read_secret", return_value="Secret-9x"):
                self.assertEqual(prompts.ask_secret("eWeLink password"), "Secret-9x")
        _, log = self.run_with(io.StringIO(), False, body)
        self.assertIn("eWeLink password", log)
        self.assertNotIn("Secret-9x", log)



class NarrowTerminalTests(OutputTestCase):
    def test_a_stage_line_too_long_for_the_terminal_is_cut_short(self):
        def body():
            with mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((40, 24))):
                output.stage_begin(6, 14, "Installing the official startup firmware")
                output.stage_end("complete")
        screen, _ = self.run_with(FakeTerminal(), False, body)
        drawn = screen.split("\r")[1]
        self.assertTrue(drawn.endswith("..."))
        self.assertLessEqual(len(drawn), 39)


class FallbackScreenTests(OutputTestCase):
    """The Wi-Fi fallbacks still say everything the person has to read or do."""

    def test_typing_the_wifi_shows_why_and_every_question(self):
        import tools.s60_autoflash as af
        from tools.autoflash.device import DeviceError

        class Ctx:
            args = mock.Mock(wifi=False)
            _wifi = None
            manifest_path = Path("m.json")
            state = mock.Mock()

            @property
            def device(self):
                raise DeviceError("no answer")

            def adopt_wifi(self, ssid, password):
                self._wifi = (ssid, password)

        secrets = iter(["password-one", "password-oops", "password-one", "password-one"])

        def body():
            with mock.patch.object(af, "bundle_ssid", return_value=None), \
                 mock.patch("builtins.input", return_value="Home-2G"), \
                 mock.patch.object(prompts, "_read_secret", side_effect=lambda q, r: next(secrets)):
                self.assertEqual(af.Context.wifi(Ctx()), ("Home-2G", "password-one"))
        screen, log = self.run_with(io.StringIO(), False, body)
        self.assertIn("could not be read off the plug: it did not answer as Tasmota", screen)
        self.assertIn("Your Wi-Fi details are needed", screen)
        self.assertIn("The two passwords did not match", screen)
        self.assertNotIn("password-one", log)

    def test_the_setup_network_steps_are_all_on_screen(self):
        import tools.s60_autoflash as af
        from tools.autoflash import setup_network
        polls = iter([None, "192.168.0.77"])
        ctx = mock.Mock(wifi=lambda why=None: ("Home-2G", "password-one"))
        ctx.finder.poll = lambda: next(polls)

        def body():
            with mock.patch.object(af.hostsys, "wifi_menu_hint", return_value="the Wi-Fi menu"), \
                 mock.patch.object(setup_network, "wait_for_port", return_value=True), \
                 mock.patch.object(setup_network, "send_wifi", return_value=("saved", "192.168.0.77")), \
                 mock.patch.object(af.time, "sleep"):
                af.configure_plug_wifi(ctx, "tasmota-5A3C1F-7340")
        screen, _ = self.run_with(io.StringIO(), False, body)
        for words in ("Join the plug's setup network", "tasmota-5A3C1F-7340", "(open - no password)",
                      "stay on its network for about 20 seconds",
                      "The plug connected to 'Home-2G' and is restarting onto it",
                      "Back to your usual Wi-Fi"):
            self.assertIn(words, screen)



class FailureReasonTests(OutputTestCase):
    """When something fails, the reason reaches the screen, not only the log."""

    def test_a_helper_tools_own_reason_is_kept(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "print('Logging in...'); print('Error: login failed'); raise SystemExit(1)"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        codes = []
        self.run_with(io.StringIO(), False, lambda: codes.append(output.stream(child)))
        self.assertEqual(codes, [1])
        self.assertEqual(output.tool_problems(), ["Error: login failed"])

    def test_without_an_error_line_the_last_lines_are_given(self):
        child = subprocess.Popen([sys.executable, "-c", "print('one'); print('two'); raise SystemExit(2)"],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.run_with(io.StringIO(), False, lambda: output.stream(child))
        self.assertEqual(output.tool_problems(), ["one", "two"])

    def test_a_phase_refusal_is_shown_and_its_progress_is_not(self):
        import tools.s60_autoflash as af

        def body():
            af.phase_log("    The plug fetched its instructions.")
            af.phase_log("    REFUSED: preflight evidence has the wrong source table")
            af.phase_log("    The server's full output is in captures/run/preflight-server.log")
        screen, log = self.run_with(io.StringIO(), False, body)
        self.assertNotIn("fetched its instructions", screen)
        self.assertIn("fetched its instructions", log)
        self.assertIn("REFUSED: preflight evidence has the wrong source table", screen)
        self.assertIn("preflight-server.log", screen)

    def test_the_bundle_download_is_logged_not_drawn_over_the_stage(self):
        from tools.prepare_safeboot_migration import obtain
        source = Path(self.tmp.name) / "source.bin"
        source.write_bytes(b"x" * 10)

        def body():
            output.stage_begin(2, 14, "Checking the files on this computer")
            obtain(source, "http://unused.example/", Path(self.tmp.name) / "pinned.bin")
            output.stage_end("complete")
        screen, log = self.run_with(FakeTerminal(), False, body)
        self.assertNotIn("Pinned", screen)
        self.assertIn("Pinned pinned.bin: 10 bytes", log)



class ReportTests(OutputTestCase):
    """The end of a run asks for a report, filled in from the run and nothing private."""

    def ctx(self, **inputs):
        from types import SimpleNamespace
        from tools.autoflash.state import RunState
        state = RunState.open(Path(self.tmp.name) / "run")
        for key, value in {"product_model": "S60TPG", "stock_version": "1.2.0",
                           "email": "me@example.com", "plug_ip": "192.168.0.50",
                           "device_name": "tasmota-1A2B3C-4567", **inputs}.items():
            state.set_input(key, value)
        state.capture("D1.upload_route", "home network")
        state.capture("D1.wifi", "preset")
        state.capture("F4.wifi_route", "joined by itself")
        return SimpleNamespace(state=state)

    def test_the_report_is_filled_in_from_the_run(self):
        import tools.s60_autoflash as af
        fields = dict(af.report_fields(self.ctx(), 441, False))
        self.assertEqual(fields["Result"], "Converted")
        self.assertEqual(fields["Plug model"], "S60TPG")
        self.assertEqual(fields["Plug type / region"], "UK (Type G)")
        self.assertEqual(fields["Stock firmware"], "1.2.0")
        self.assertIn("Python ", fields["Computer"])
        self.assertEqual(fields["Time taken"], "7m 21s")
        self.assertEqual(fields["Typed the Wi-Fi"], "No")
        self.assertEqual(fields["Joined setup network"], "No")
        self.assertEqual(fields["Name in Tasmota"], "Sonoff S60TPG")

    def test_hardware_that_was_not_checked_says_so(self):
        import tools.s60_autoflash as af
        fields = dict(af.report_fields(self.ctx(), 441, False))
        self.assertEqual(fields["Relay"], "Not checked")
        self.assertNotIn("Mains voltage", fields)
        self.assertEqual(fields["Power meter"], "Not checked")
        self.assertNotIn("System prompts", fields)
        self.assertNotIn("__", fields.values())

    def test_every_answer_starts_with_a_capital_and_only_that_changes(self):
        import tools.s60_autoflash as af
        fields = dict(af.report_fields(self.ctx(), 441, False,
                                       {"Relay": "switches (seen on the meter)",
                                        "Power meter": "on: 239 V, 0 W; off: 0 V, 0 W"}))
        self.assertEqual(fields["Relay"], "Switches (seen on the meter)")
        self.assertEqual(fields["Power meter"], "On: 239 V, 0 W; off: 0 V, 0 W")
        self.assertEqual(fields["Plug model"], "S60TPG")

    def test_the_plug_page_is_the_plug_or_the_emulators_forward(self):
        import tools.s60_autoflash as af
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("S60_EMULATOR_URL", None)
            self.assertEqual(af.plug_page_url("192.168.0.50"), "http://192.168.0.50/")
            self.assertIsNone(af.plug_page_url(None))
        with mock.patch.dict(os.environ, {"S60_EMULATOR_URL": "http://localhost:8080"}):
            self.assertEqual(af.plug_page_url("192.168.60.50"), "http://localhost:8080")

    def test_the_report_holds_nothing_private(self):
        import tools.s60_autoflash as af
        report = af.report_markdown(af.report_fields(self.ctx(), 441, False))
        for private in ("me@example.com", "192.168.0.50", "tasmota-1A2B3C-4567", "1001234abc"):
            self.assertNotIn(private, report)

    def test_fallbacks_and_unknown_models_are_reported_as_such(self):
        import tools.s60_autoflash as af
        ctx = self.ctx(product_model="", wifi_typed=True)
        ctx.state.capture("F4.wifi_route", "setup network")
        fields = dict(af.report_fields(ctx, 60, True))
        self.assertEqual(fields["Plug model"], "__")
        self.assertEqual(fields["Typed the Wi-Fi"], "Yes")
        self.assertEqual(fields["Joined setup network"], "Yes")
        self.assertEqual(fields["Name in Tasmota"], "Sonoff S60")
        self.assertIn("(this session; the run was resumed)", fields["Time taken"])

    def test_the_invitation_links_the_thread_and_copies_the_report(self):
        import tools.s60_autoflash as af
        copied = []

        def body():
            with mock.patch.object(af.hostsys, "copy_to_clipboard", side_effect=lambda text: copied.append(text) or True), \
                 mock.patch.object(af, "check_hardware", return_value={"Relay": "switches (seen on the meter)"}):
                af.invite_report(self.ctx(), 441, False)
        screen, _ = self.run_with(io.StringIO(), False, body)
        self.assertIn(af.REPORTS_URL, screen)
        self.assertIn("adamsthws/Sonoff-OTA/discussions", af.REPORTS_URL)
        self.assertIn("Please share how it went", screen)
        self.assertIn("already on your clipboard", screen)
        self.assertIn(f"Test reports: {af.REPORTS_URL}", screen)
        self.assertIn("Throw your friendly neighbourhood dev a biscuit:", screen)
        self.assertGreater(screen.index(af.DONATE_URL), screen.index("Plug model"))  # after the report
        self.assertGreater(screen.index(f"PayPal: {af.DONATE_URL}"), screen.index(f"Ko-fi: {af.KOFI_URL}"))
        self.assertNotIn("**", screen)                       # the terminal shows no Markdown
        self.assertRegex(screen, r"Plug model: +S60TPG")
        self.assertIn("**Plug model:** S60TPG", copied[0])   # the thread gets bold labels
        self.assertIn("**Relay:** Switches (seen on the meter)", copied[0])



class LinkTests(OutputTestCase):
    URL = "https://github.com/adamsthws/Sonoff-OTA/discussions/1"

    def test_a_terminal_gets_a_clickable_link_and_the_log_the_plain_url(self):
        def body():
            with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}), \
                 mock.patch.object(output, "IS_WINDOWS", False):
                os.environ.pop("NO_COLOR", None)
                output.show_link(self.URL)
        screen, log = self.run_with(FakeTerminal(), False, body)
        self.assertIn(f"\x1b]8;;{self.URL}\x1b\\{self.URL}\x1b]8;;\x1b\\", screen)
        self.assertIn(f"  {self.URL}\n", log)
        self.assertNotIn("\x1b", log)

    def test_without_colour_or_on_the_classic_windows_console_it_is_plain(self):
        screen, _ = self.run_with(FakeTerminal(), False, lambda: output.show_link(self.URL))  # NO_COLOR set
        self.assertEqual(screen.strip(), self.URL)
        with mock.patch.object(output, "colour_ok", return_value=True), \
             mock.patch.object(output, "IS_WINDOWS", True), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WT_SESSION", None)
            self.assertEqual(output.hyperlink(self.URL), self.URL)

    def links_on(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.dict(os.environ, {"TERM": "xterm-256color"}))
        stack.enter_context(mock.patch.object(output, "IS_WINDOWS", False))
        stack.enter_context(mock.patch.object(output.platform, "system", return_value="Linux"))
        os.environ.pop("NO_COLOR", None)
        os.environ.pop("TERM_PROGRAM", None)
        return stack

    def test_a_titled_link_shows_the_title_and_the_url_both(self):
        def body():
            with self.links_on():
                output.show_link(self.URL, "Test reports")
        screen, log = self.run_with(FakeTerminal(), False, body)
        self.assertIn(f"\x1b]8;;{self.URL}\x1b\\{self.URL}\x1b]8;;\x1b\\", screen)  # only the URL is the link
        self.assertLess(screen.index(f"{output.PURPLE}Test reports:"), screen.index("\x1b]8;;"))
        self.assertEqual(output.ANSI_RE.sub("", screen).strip(), f"Test reports: {self.URL}")  # no hint
        self.assertIn(f"  Test reports: {self.URL}\n", log)

    def test_a_group_of_links_has_the_hint_once_underneath(self):
        def body():
            with self.links_on():
                output.show_links([(self.URL, "Ko-fi"), (self.URL, "PayPal")], hint=True)
        screen, log = self.run_with(FakeTerminal(), False, body)
        lines = output.ANSI_RE.sub("", screen).strip().splitlines()
        self.assertEqual([line.split(":")[0].strip() for line in lines], ["Ko-fi", "PayPal", "(Ctrl+click to open)"])
        self.assertNotIn("click", log)

    def test_a_titled_link_is_plain_text_where_links_do_not_work(self):
        screen, _ = self.run_with(FakeTerminal(), False, lambda: output.show_link(self.URL, "Ko-fi"))  # NO_COLOR set
        self.assertEqual(screen.strip(), f"Ko-fi: {self.URL}")  # no hint: clicking may not work
        with mock.patch.object(output, "colour_ok", return_value=True), \
             mock.patch.object(output, "IS_WINDOWS", False), \
             mock.patch.dict(os.environ, {"TERM_PROGRAM": "Apple_Terminal"}):
            text = output.hyperlink(self.URL, "Ko-fi")
        self.assertNotIn("\x1b]8;;", text)
        self.assertEqual(output.ANSI_RE.sub("", text), f"Ko-fi: {self.URL}")

    def test_with_only_basic_colours_the_title_is_magenta_not_rounded_to_blue(self):
        with mock.patch.object(output, "colour_ok", return_value=True), \
             mock.patch.object(output, "IS_WINDOWS", False), \
             mock.patch.dict(os.environ, {"TERM": "screen"}):
            os.environ.pop("COLORTERM", None)
            self.assertIn(f"{output.MAGENTA}Ko-fi:", output.hyperlink(self.URL, "Ko-fi"))
            os.environ["TERM"] = "tmux-256color"
            self.assertIn(f"{output.PURPLE}Ko-fi:", output.hyperlink(self.URL, "Ko-fi"))

    def test_the_title_is_purple_on_the_classic_windows_console_too(self):
        with mock.patch.object(output, "colour_ok", return_value=True), \
             mock.patch.object(output, "IS_WINDOWS", True), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WT_SESSION", None)
            text = output.hyperlink(self.URL, "Ko-fi")
        self.assertIn(f"{output.PURPLE}Ko-fi:", text)
        self.assertNotIn("\x1b]8;;", text)                    # coloured, not a link
        self.assertEqual(output.ANSI_RE.sub("", text), f"Ko-fi: {self.URL}")


def rendered(text):
    """The rows a terminal shows once `text` is written to it: carriage
    returns, erasing a row or its end, and moving up a row applied."""
    import re
    rows, row, col = [""], 0, 0
    for token in re.findall(r"\x1b\[1F|\x1b\[2K|\x1b\[K|\x1b\[[0-9;?]*[A-Za-z]|\x1b\]8;;[^\x1b]*\x1b\\|[\s\S]", text):
        if token == "\x1b[1F":
            row, col = max(0, row - 1), 0
        elif token == "\x1b[2K":
            rows[row] = ""
        elif token == "\x1b[K":
            rows[row] = rows[row][:col]
        elif token.startswith("\x1b"):
            continue
        elif token == "\r":
            col = 0
        elif token == "\n":
            row, col = row + 1, 0
            if row == len(rows):
                rows.append("")
        else:
            line = rows[row].ljust(col)
            rows[row] = line[:col] + token + line[col + 1:]
            col += 1
    rows = [" ".join(line.split()) for line in rows]  # columns padded or not
    while rows and not rows[-1]:
        rows.pop()
    return rows


class StageGapTests(OutputTestCase):
    """Whatever is shown under a stage's line - a message, a question, a
    helper's output - has one blank line between it and the stage line."""

    def screen(self, body, verbose=False, colour=False):
        def wrapped():
            with mock.patch.object(output, "colour_ok", return_value=colour), \
                 mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((100, 40))):
                body()
        screen, _ = self.run_with(FakeTerminal(), verbose, wrapped)
        return rendered(screen)

    def test_a_message_under_the_stage_line_has_a_blank_line_above_it(self):
        def body():
            output.stage_begin(3, 14, "Discovering the plug")
            output.show("Looking for it on your network...")
            output.stage_end("complete")
        rows = self.screen(body)
        self.assertTrue(rows[0].startswith("Stage 3 of 14 - Discovering the plug - Underway"))
        self.assertEqual(rows[1:3], ["", "Looking for it on your network..."])
        self.assertTrue(rows[3].startswith("Stage 3 of 14 - Discovering the plug - Complete"))

    def test_a_section_s_own_blank_line_is_the_gap(self):
        def body():
            output.stage_begin(4, 14, "Querying Sonoff firmware")
            prompts.section("What the plug reported", ["Firmware now 1.2.0"])
        rows = self.screen(body)
        self.assertEqual(rows[1:3], ["", "-- What the plug reported"])

    def test_a_question_has_the_gap_too(self):
        def body():
            output.stage_begin(3, 14, "Discovering the plug")
            output.before_input("eWeLink account email: ")
            sys.stdout.write("eWeLink account email: ")
            output.answered("someone@example.com")
        rows = self.screen(body)
        self.assertEqual(rows[1], "")
        self.assertEqual(rows[2], "eWeLink account email:")

    def test_the_gap_leaves_the_screen_with_an_answered_question(self):
        def body():
            output.stage_begin(3, 14, "Discovering the plug")
            output.transient_begin()
            prompts.warning_box("Before going on", ["One line."])
            output.before_input("Type PROCEED to go ahead: ")
            sys.stdout.write("Type PROCEED to go ahead: proceed\n")
            output.answered("proceed")
            output.transient_end()
            output.show("Sending the bridge...")
            output.stage_end("complete")
        rows = self.screen(body, colour=True)
        self.assertTrue(rows[0].startswith("Stage 3 of 14 - Discovering the plug - Underway"))
        self.assertEqual(rows[1:3], ["", "Sending the bridge..."])
        self.assertNotIn("Before going on", "\n".join(rows))

    def test_stage_lines_follow_one_another_without_a_gap(self):
        def body():
            for number in (1, 2):
                output.stage_begin(number, 14, f"Stage number {number}")
                output.stage_end("already done")
        rows = self.screen(body)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[1].startswith("Stage 2 of 14"))

    def test_a_failure_under_a_stopped_stage_has_the_gap(self):
        def body():
            output.stage_begin(5, 14, "Installing bridge firmware")
            output.stage_end("stopped")
            output.show("FAILED: bridge delivery failed")
        rows = self.screen(body)
        self.assertTrue(rows[0].endswith("Stopped"))
        self.assertEqual(rows[1:], ["", "FAILED: bridge delivery failed"])

    def test_verbose_output_under_a_stage_heading_has_the_gap(self):
        def body():
            output.stage_begin(4, 14, "Querying Sonoff firmware")
            output.detail("$ python tools/query_ota.py")
        rows = self.screen(body, verbose=True)
        heading = rows.index("== Stage 4 of 14 - Querying Sonoff firmware")
        self.assertEqual(rows[heading + 1:heading + 3], ["", "$ python tools/query_ota.py"])

    def test_piped_output_gets_the_gap_too(self):
        def body():
            output.stage_begin(3, 14, "Discovering the plug")
            output.show("Found it.")
        with mock.patch("sys.stdout", io.StringIO()) as piped:
            output.start(self.log, verbose=False)
            body()
            output.finish()
        rows = piped.getvalue().splitlines()
        self.assertEqual(rows[1:3], ["", " Found it."])



class MarginTests(OutputTestCase):
    """Every row starts one space in, and nothing is wider than the terminal."""

    def narrow(self, body, columns=40):
        def run():
            with mock.patch.dict(os.environ, {"COLUMNS": str(columns), "LINES": "24"}):
                body()
        screen, _ = self.run_with(FakeTerminal(), True, run)
        return [row for row in output.ANSI_RE.sub("", screen).split("\n") if row.strip()]

    def test_a_long_message_wraps_between_words_inside_the_margin(self):
        text = "Fix the cause, then carry on with the same command again, and it picks up where it stopped."
        rows = self.narrow(lambda: output.show(text))
        self.assertGreater(len(rows), 1)
        for row in rows:
            self.assertTrue(row.startswith(" ") and not row.startswith("  "), row)
            self.assertLessEqual(len(row), 39)
        self.assertEqual(" ".join(row.strip() for row in rows), text)

    def test_lined_up_labels_carry_on_under_their_value(self):
        rows = self.narrow(lambda: output.show("    Relay:      switches, seen on the meter with a lamp plugged in"))
        self.assertEqual(rows[0].index("switches"), rows[1].index(rows[1].strip()))

    def test_a_url_is_never_broken(self):
        url = "https://github.com/adamsthws/Sonoff-OTA/discussions/1"
        rows = self.narrow(lambda: output.show_link(url, "Test reports"))
        self.assertIn(url, [row.strip() for row in rows])

    def test_a_long_question_wraps_and_leaves_its_last_row_for_the_answer(self):
        shown = []
        rows = self.narrow(lambda: shown.append(output.question(
            "Type YES to go ahead - anything else stops, changing nothing", ": ")))
        self.assertTrue(shown[0].endswith(": "))
        self.assertTrue(shown[0].startswith(" "))
        self.assertTrue(all(len(row) <= 39 for row in rows + shown))

    def test_piped_output_keeps_the_margin_but_is_not_wrapped(self):
        text = "word " * 40
        screen, _ = self.run_with(io.StringIO(), True, lambda: output.show(text.strip()))
        self.assertEqual(screen.strip("\n"), " " + text.strip())


if __name__ == "__main__":
    unittest.main()
