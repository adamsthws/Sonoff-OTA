"""Ctrl-C asks before it stops a run part way; a second, soon after, stops it."""
import signal
import unittest
from unittest import mock

from tools.autoflash import interrupt


class CtrlCTests(unittest.TestCase):
    def setUp(self):
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

    def test_helpers_are_kept_out_of_the_terminals_ctrl_c(self):
        self.assertTrue(interrupt.child_group())


class StageEndTests(unittest.TestCase):
    """At the end of a stage, a Ctrl-C pressed during it is asked about, with a
    countdown that carries on by itself."""

    def ask(self, pending, answer):
        import tools.autoflash.cli as autoflash
        interrupt.clear()
        if pending:
            interrupt._state["first"] = 1.0
        with mock.patch.object(autoflash.prompts, "countdown_choice", return_value=answer) as asked, \
             mock.patch.object(autoflash, "section"), mock.patch.object(autoflash, "say"):
            stop = autoflash.stop_here_after_ctrl_c("D1")
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
