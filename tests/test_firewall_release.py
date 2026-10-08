"""The temporary firewall rule is removed however the tool exits."""

import types
import unittest
from unittest import mock

import tools.s60_autoflash as af
from tools.autoflash import hostsys


class FakeState:
    def __init__(self, rules):
        self.inputs = {"firewall_rules": [rule.to_json() for rule in rules]}

    def input(self, key):
        return self.inputs.get(key)

    def set_input(self, key, value):
        self.inputs[key] = value


class ReleaseFirewallTests(unittest.TestCase):
    RULE = hostsys.FirewallRule("ufw", "192.168.1.50", [8088, 8089])

    def ctx(self, rules=(RULE,)):
        return types.SimpleNamespace(state=FakeState(list(rules)))

    def test_a_recorded_rule_is_removed(self):
        ctx = self.ctx()
        with mock.patch.object(hostsys, "close_firewall", return_value=True) as close, \
             mock.patch.object(af, "tell"), mock.patch.object(af, "say"):
            af.release_firewall(ctx)
        close.assert_called_once_with(self.RULE)
        self.assertEqual(af.recorded_rules(ctx), [])

    def test_nothing_is_asked_when_no_rule_was_added(self):
        with mock.patch.object(hostsys, "close_firewall") as close, mock.patch.object(af, "tell") as told:
            af.release_firewall(self.ctx(rules=()))
        close.assert_not_called()
        told.assert_not_called()

    def test_ctrl_c_at_the_password_prompt_says_how_to_remove_it(self):
        ctx = self.ctx()
        lines = []
        with mock.patch.object(hostsys, "close_firewall", side_effect=KeyboardInterrupt), \
             mock.patch.object(af, "tell", side_effect=lines.append):
            af.release_firewall(ctx)  # does not raise
        screen = "\n".join(lines)
        self.assertIn("was not removed (stopped with Ctrl-C)", screen)
        self.assertIn("192.168.1.50", screen)
        self.assertEqual(af.recorded_rules(ctx), [self.RULE])  # still recorded for cleanup


class QuitTests(unittest.TestCase):
    RULE = ReleaseFirewallTests.RULE

    def quit_screen(self, rules, removed=True):
        ctx = types.SimpleNamespace(state=FakeState(rules))
        lines = []
        with mock.patch.object(hostsys, "close_firewall", return_value=removed), \
             mock.patch.object(af, "tell", side_effect=lines.append), mock.patch.object(af, "say"), \
             mock.patch.object(af, "section"):
            af.say_quit(ctx)
        return lines

    def test_quitting_ends_with_the_cleaned_up_line(self):
        for rules in ([], [self.RULE]):
            self.assertEqual(self.quit_screen(rules)[-1], "Cleaned up. Nothing was left half done. Exiting.")

    def test_a_rule_left_behind_is_not_called_cleaned_up(self):
        self.assertNotIn("Cleaned up. Nothing was left half done. Exiting.",
                         self.quit_screen([self.RULE], removed=False))


class EmulatorPasswordTests(unittest.TestCase):
    def test_the_emulator_password_is_given_only_inside_the_emulator(self):
        import os
        with mock.patch.dict(os.environ, {"S60_EMULATOR_SUDO_PASSWORD": "s60"}), \
             mock.patch.object(hostsys, "IS_WINDOWS", False):
            self.assertEqual(af.emulator_password_hint(),
                             ["In the emulator, the password is: s60"])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("S60_EMULATOR_SUDO_PASSWORD", None)
            self.assertEqual(af.emulator_password_hint(), [])

    def test_the_removal_note_gives_it_too(self):
        import os
        ctx = types.SimpleNamespace(state=FakeState([ReleaseFirewallTests.RULE]))
        lines = []
        with mock.patch.dict(os.environ, {"S60_EMULATOR_SUDO_PASSWORD": "s60"}), \
             mock.patch.object(hostsys, "IS_WINDOWS", False), \
             mock.patch.object(hostsys, "close_firewall", return_value=True), \
             mock.patch.object(af, "tell", side_effect=lines.append), mock.patch.object(af, "say"):
            af.close_firewall_rules(ctx)
        self.assertIn("In the emulator, the password is: s60", lines)


if __name__ == "__main__":
    unittest.main()
