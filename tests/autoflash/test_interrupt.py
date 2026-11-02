"""Ctrl-C asks before it stops a run part way; a second, soon after, stops it."""
import signal
import unittest
from unittest import mock

from tools.autoflash import interrupt, output, prompts, runner


class CtrlCTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(interrupt._state.update, dict(interrupt._state))
        interrupt.clear()
        interrupt._state.update(closed=False, deferring=0, deferred=False, asking=0)
        self.clock = [1000.0]
        patcher = mock.patch.object(interrupt.time, "monotonic", side_effect=lambda: self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)
        quiet = mock.patch.object(interrupt, "_say")
        quiet.start()
        self.addCleanup(quiet.stop)

    def press(self):
        interrupt._on_interrupt(signal.SIGINT, None)

    def test_one_press_only_asks_at_the_next_safe_point(self):
        self.press()  # no KeyboardInterrupt: the run goes on
        self.assertTrue(interrupt.pending())

    def test_a_second_press_soon_after_stops_at_once(self):
        self.press()
        self.clock[0] += interrupt.GRACE - 1
        with self.assertRaises(KeyboardInterrupt):
            self.press()

    def test_a_second_press_long_after_is_a_first_again(self):
        self.press()
        self.clock[0] += interrupt.GRACE + 1
        self.press()  # asks again, rather than stopping
        self.assertTrue(interrupt.pending())

    def test_at_a_question_ctrl_c_stops_at_once(self):
        with interrupt.asking():
            with self.assertRaises(KeyboardInterrupt):
                self.press()

    def test_the_window_closing_stops_at_once_and_is_remembered(self):
        with self.assertRaises(KeyboardInterrupt):
            interrupt._on_closed(getattr(signal, "SIGHUP", 1), None)
        self.assertTrue(interrupt.window_closed())

    def test_a_stop_inside_a_deferred_block_comes_at_its_end(self):
        reached = []
        with self.assertRaises(KeyboardInterrupt):
            with interrupt.deferred():
                self.press()
                self.press()  # would stop at once
                reached.append("rest of the block")
        self.assertEqual(reached, ["rest of the block"])

    def test_a_question_inside_a_deferred_block_still_stops_at_once(self):
        reached = []
        with self.assertRaises(KeyboardInterrupt):
            with interrupt.deferred():
                with interrupt.asking():
                    self.press()
                    reached.append("after the question")
        self.assertEqual(reached, [])
        self.assertFalse(interrupt._state["deferred"])

    def test_one_press_inside_a_deferred_block_only_asks(self):
        with interrupt.deferred():
            self.press()
        self.assertTrue(interrupt.pending())

    def test_the_notice_counts_down_and_goes_once_a_second_press_would_not_stop(self):
        self.assertEqual(interrupt.notice(), [])
        self.press()
        self.assertEqual(interrupt.notice(), ["! Are you sure?", "Press Ctrl-C again within 10 seconds to stop now."])
        self.clock[0] += 3.5
        self.assertEqual(interrupt.notice()[-1], "Press Ctrl-C again within 7 seconds to stop now.")
        self.clock[0] += 6
        self.assertEqual(interrupt.notice()[-1], "Press Ctrl-C again within 1 second to stop now.")
        self.clock[0] += interrupt.GRACE
        self.assertEqual(interrupt.notice(), [])
        self.assertTrue(interrupt.pending())   # still asked at the end of the stage

    def test_the_notice_is_written_out_only_where_the_screen_does_not_draw_it(self):
        self.addCleanup(interrupt.shown_by, None)
        interrupt.shown_by(lambda: None)
        self.press()
        interrupt._say.assert_not_called()
        interrupt.clear()
        interrupt.shown_by(lambda: "laid out\n")
        self.press()
        interrupt._say.assert_called_once_with("laid out\n")
        interrupt.clear()
        interrupt.shown_by(None)   # before a run's output starts: a blank line, then the notice
        self.press()
        interrupt._say.assert_called_with("\n\n ! Are you sure?\n     Press Ctrl-C again within 10 seconds to stop now.\n")

    def test_helpers_are_kept_out_of_the_terminals_ctrl_c(self):
        self.assertTrue(interrupt.child_group())


class NoticeOnScreenTests(unittest.TestCase):
    """The notice is drawn under the stage line, not after a copy of it, and
    taken off again once its seconds are up."""

    def test_the_notice_sits_under_the_stage_line_and_goes(self):
        import io
        import os
        import tempfile
        from pathlib import Path

        class Terminal(io.StringIO):
            def isatty(self):
                return True

        interrupt.clear()
        interrupt._state.update(asking=0)
        clock = [1000.0]
        screen = Terminal()
        with tempfile.TemporaryDirectory() as tmp, mock.patch("sys.stdout", screen), \
                mock.patch("sys.stdin", io.StringIO()), mock.patch.dict(os.environ, {"NO_COLOR": ""}), \
                mock.patch.object(interrupt.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(interrupt, "_say") as said, \
                mock.patch.object(output, "_tick_forever"):    # drawn here, not by the ticker
            os.environ.pop("NO_COLOR")
            output.start(Path(tmp) / "run.log", verbose=False)
            try:
                output.stage_begin(2, "Discovering the device")
                interrupt._on_interrupt(signal.SIGINT, None)
                output.tick()
                output.tick()
                drawn = screen.getvalue()
                clock[0] += interrupt.GRACE + 1
                output.tick()
                cleared = screen.getvalue()[len(drawn):]
            finally:
                output.finish()
                interrupt.clear()
        said.assert_not_called()
        self.assertEqual(drawn.count("Stage 2 - Discovering the device"), 5)   # each draw on its own row
        self.assertNotIn("\x1b[2K", drawn)                        # written over, never erased: no flicker
        self.assertNotIn("\n Stage 2", drawn)                     # never a second copy below
        # A blank row, then laid out as a warning: its title highlighted.
        self.assertIn(f"\n\x1b[K\n{output.BOLD}{output.YELLOW} ! Are you sure?{output.RESET}\x1b[K"
                      "\n     Press Ctrl-C again within 10 seconds to stop now.", drawn)
        self.assertIn("\x1b[3A\r", drawn)                         # back up to the stage line
        self.assertTrue(cleared.startswith("\x1b[1E\x1b[2K" * 3 + "\x1b[3F"))
        self.assertNotIn("Ctrl-C", cleared)
        self.assertNotIn("\x1b[J", drawn + cleared)


class NoticeWrittenTests(unittest.TestCase):
    def test_where_the_ticker_draws_nothing_the_notice_is_written_with_a_blank_line_above(self):
        # In an open block (a note and its question) the stage line is not
        # drawn, so the notice is written out, laid out as the drawn one.
        with mock.patch.object(output, "colour_ok", return_value=True), \
                mock.patch.object(interrupt.time, "monotonic", return_value=1000.0), \
                mock.patch.multiple(output._s, draws_notice=True, concise=True, ticker=object(), paused=False,
                                    held=False, stage="Stage 1 - Confirming prerequisites", transient_rows=0,
                                    live=0):
            interrupt._state["first"] = 1000.0
            try:
                self.assertEqual(output._notice_text(),
                                 f"\n{output.BOLD}{output.YELLOW} ! Are you sure?{output.RESET}\n"
                                 "     Press Ctrl-C again within 10 seconds to stop now.\n")
                output._s.live = 20   # an unfinished line is ended first
                self.assertTrue(output._notice_text().startswith("\n\n"))
                output._s.transient_rows = None
                output._s.live = 0
                self.assertIsNone(output._notice_text())   # the ticker draws it
            finally:
                interrupt.clear()


class StageEndTests(unittest.TestCase):
    """At the end of a stage, a Ctrl-C pressed during it is asked about, with a
    countdown that carries on by itself."""

    def ask(self, pending, answer):
        interrupt.clear()
        if pending:
            interrupt._state["first"] = 1.0
        with mock.patch.object(prompts, "countdown_choice", return_value=answer) as asked, \
             mock.patch.object(prompts, "section"), mock.patch.object(output, "say"):
            stop = runner.stop_here_after_ctrl_c("D1")
        return stop, asked.called

    def test_nothing_pressed_nothing_asked(self):
        self.assertEqual(self.ask(False, 1), (False, False))

    def test_carrying_on_is_the_default(self):
        self.assertEqual(self.ask(True, 0), (False, True))
        self.assertFalse(interrupt.pending())

    def test_stopping_when_said(self):
        self.assertEqual(self.ask(True, 1), (True, True))

    def test_unattended_the_countdown_takes_its_default(self):
        from tools.autoflash import prompts
        with mock.patch.object(prompts, "UNATTENDED", True), \
             mock.patch.object(prompts, "answered_unattended"):
            self.assertEqual(prompts.countdown_choice("Stop the run here", ["Carry on", "Stop here"]), 0)

    def test_the_emulators_countdown_waits_as_asking(self):
        from tools.autoflash import prompts
        seen = []
        with mock.patch.object(prompts, "UNATTENDED", True), \
             mock.patch.object(prompts, "answered_unattended"), \
             mock.patch.dict("os.environ", {"AUTOFLASH_EMULATOR_COUNTDOWN": "30"}), \
             mock.patch.object(prompts.time, "sleep", side_effect=lambda s: seen.append((s, interrupt._state["asking"]))):
            self.assertEqual(prompts.countdown_choice("Stop the run here", ["Carry on", "Stop here"]), 0)
        self.assertEqual(seen, [(30.0, 1)])  # a Ctrl-C during it stops at once


if __name__ == "__main__":
    unittest.main()
