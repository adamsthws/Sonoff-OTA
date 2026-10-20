"""Windows' permission prompt (User Account Control), answered every way it can be.

The test Windows (Server Core) cannot show the prompt, so these stand in for
it: Yes, No, a command that fails once allowed, and PowerShell missing - from
the prompt itself (hostsys.elevated), through the firewall rule it guards, to
what the tool then says.
"""

import base64
import re
import types
import unittest
from pathlib import Path
from unittest import mock

import tools.autoflash_cli as af
from tools.autoflash import hostsys

PYTHON = r"C:\Users\O'Brien\S60\.python\python.exe"  # an apostrophe, as some names have
CANCELLED = "This operation was canceled by the user."


def powershell(returncode=0, stdout=""):
    """subprocess.run as PowerShell answers once the prompt is dealt with."""
    return mock.patch.object(hostsys.subprocess, "run",
                             return_value=mock.Mock(returncode=returncode, stdout=stdout))


def script_of(run):
    return run.call_args[0][0][-1]


def decoded(command):
    """The script an elevated PowerShell is given with -EncodedCommand."""
    return base64.b64decode(command[command.index("-EncodedCommand") + 1]).decode("utf-16-le")


class PromptTests(unittest.TestCase):
    """hostsys.elevated: what each answer to the prompt returns."""

    def setUp(self):
        patcher = mock.patch.object(hostsys, "IS_WINDOWS", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_yes_and_the_command_succeeds(self):
        with powershell(0):
            self.assertEqual(hostsys.elevated(["netsh", "advfirewall", "show"]), 0)

    def test_yes_and_the_command_fails_gives_its_exit_code(self):
        with powershell(5):
            self.assertEqual(hostsys.elevated(["netsh", "advfirewall", "show"]), 5)

    def test_no_is_a_failure_and_says_why(self):
        with powershell(1223, CANCELLED + "\n"), mock.patch.object(hostsys.output, "detail") as detail:
            self.assertEqual(hostsys.elevated(["netsh", "advfirewall", "show"]), 1223)
        detail.assert_called_once_with(CANCELLED)

    def test_without_powershell_it_is_a_failure(self):
        with mock.patch.object(hostsys.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(hostsys.elevated(["netsh", "advfirewall", "show"]), 127)

    def test_a_prompt_that_is_never_answered_yes_can_never_read_as_success(self):
        # No process is started then, and a missing process's exit code would read as 0.
        with powershell() as run:
            hostsys.elevated(["netsh", "advfirewall", "show"])
        script = script_of(run)
        for part in ("-Verb RunAs", "-Wait", "-PassThru", "-ErrorAction Stop", "exit 1223", "exit $p.ExitCode"):
            self.assertIn(part, script)
        self.assertLess(script.index("catch"), script.index("exit 1223"))

    def test_apostrophes_are_quoted_in_the_program_and_its_arguments(self):
        with powershell() as run:
            hostsys.elevated([PYTHON, f"program={PYTHON}"])
        script = script_of(run)
        self.assertIn("-FilePath 'C:\\Users\\O''Brien\\S60\\.python\\python.exe'", script)
        self.assertIn("-ArgumentList 'program=C:\\Users\\O''Brien", script)

    def test_a_program_alone_has_no_argument_list(self):
        with powershell() as run:
            hostsys.elevated(["wf.msc"])
        self.assertNotIn("-ArgumentList", script_of(run))

    def test_nothing_is_captured_on_windows(self):
        with powershell(1223, CANCELLED), mock.patch.object(hostsys.output, "detail"):
            self.assertEqual(hostsys.elevated_output(["netsh"]), (1223, ""))


class FirewallRuleTests(unittest.TestCase):
    """The rule the prompt guards: added on Yes, nothing left over on No."""

    def setUp(self):
        for patcher in (mock.patch.object(hostsys, "IS_WINDOWS", True),
                        mock.patch.object(hostsys, "python_program", return_value=PYTHON),
                        mock.patch.object(hostsys, "active_firewall", return_value="windows")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def open(self, answer):
        """open_firewall, with `answer(script) -> exit code` standing in for the prompt."""
        with mock.patch.object(hostsys, "elevated", side_effect=lambda command: answer(decoded(command))):
            return hostsys.open_firewall("192.168.1.50", [8088, 8089])

    @staticmethod
    def result_path(script):
        return Path(re.search(r"Set-Content -Encoding UTF8 -Path '([^']+)'", script).group(1))

    def test_no_raises_and_adds_nothing(self):
        with self.assertRaises(OSError) as caught:
            self.open(lambda script: 1223)
        self.assertIn("could not add the firewall rule", str(caught.exception))

    def test_no_leaves_no_scratch_directory_behind(self):
        seen = []
        with self.assertRaises(OSError):
            self.open(lambda script: seen.append(self.result_path(script)) or 1223)
        self.assertFalse(seen[0].parent.exists())

    def test_yes_gives_the_rule_and_the_block_rules_it_paused(self):
        def allowed(script):
            self.result_path(script).write_text('{"paused": ["{block-1}"]}', encoding="utf-8")
            return 0

        rule = self.open(allowed)
        self.assertEqual((rule.kind, rule.plug_ip, rule.ports), ("windows", "192.168.1.50", [8088, 8089]))
        self.assertEqual(rule.detail, {"program": PYTHON, "paused": ["{block-1}"]})

    def test_yes_without_a_result_still_gives_the_rule(self):
        self.assertEqual(self.open(lambda script: 0).detail["paused"], [])

    def test_the_elevated_script_admits_only_the_plug_to_this_python(self):
        scripts = []
        self.open(lambda script: scripts.append(script) or 0)
        self.assertIn("$program = 'C:\\Users\\O''Brien\\S60\\.python\\python.exe'", scripts[0])
        self.assertIn("'remoteip=192.168.1.50'", scripts[0])
        self.assertIn("'localport=8088,8089'", scripts[0])

    def test_removing_it_on_no_reports_it_left(self):
        rule = hostsys.FirewallRule("windows", "192.168.1.50", [8088, 8089],
                                    {"program": PYTHON, "paused": ["{block-1}"]})
        with mock.patch.object(hostsys, "elevated", return_value=1223):
            self.assertFalse(hostsys.close_firewall(rule))
        scripts = []
        with mock.patch.object(hostsys, "elevated", side_effect=lambda c: scripts.append(decoded(c)) or 0):
            self.assertTrue(hostsys.close_firewall(rule))
        self.assertIn("'name=S60 autoflash 192.168.1.50'", scripts[0])
        self.assertIn("@('{block-1}')", scripts[0])  # the paused block rule, back on

    def test_a_rule_that_was_already_there_is_left_without_asking(self):
        rule = hostsys.FirewallRule("windows", "192.168.1.50", [8088], {"preexisting": True})
        with mock.patch.object(hostsys, "elevated") as asked:
            self.assertTrue(hostsys.close_firewall(rule))
        asked.assert_not_called()

    def test_by_hand_the_paused_rules_are_quoted(self):
        rule = hostsys.FirewallRule("windows", "192.168.1.50", [8088], {"program": PYTHON, "paused": ["it's"]})
        self.assertEqual(hostsys.undo_commands(rule)[-1], "Enable-NetFirewallRule -Name 'it''s'")


class FakeState:
    def __init__(self, **inputs):
        self.inputs = inputs

    def input(self, key):
        return self.inputs.get(key)

    def set_input(self, key, value):
        self.inputs[key] = value


class ToolTests(unittest.TestCase):
    """What the person sees when they answer the prompt No."""

    RULE = hostsys.FirewallRule("windows", "192.168.1.50", [8088, 8089], {"program": PYTHON, "paused": []})

    def setUp(self):
        self.screen = []
        for patcher in (
                mock.patch.object(hostsys, "IS_WINDOWS", True),
                mock.patch.object(hostsys, "python_program", return_value=PYTHON),
                mock.patch.object(hostsys, "active_firewall", return_value="windows"),
                mock.patch.object(hostsys, "firewall_advice", return_value=[]),
                mock.patch.object(hostsys, "local_rules_ignored", return_value=False),
                mock.patch.object(af.output, "transient_begin"),
                mock.patch.object(af.output, "transient_end"),
                mock.patch.object(af, "section", side_effect=lambda title, lines: self.screen.extend([title, *lines])),
                mock.patch.object(af, "say", side_effect=self.screen.append),
                mock.patch.object(af, "tell", side_effect=self.screen.append)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def ctx(self, rules=()):
        return types.SimpleNamespace(state=FakeState(
            plug_ip="192.168.1.50", firewall_rules=[rule.to_json() for rule in rules]))

    def test_it_says_windows_will_ask_before_asking(self):
        def prompt(plug_ip, ports):
            self.assertIn("Windows will ask whether to allow the change - choose Yes. This window",
                          self.screen)
            return self.RULE

        with mock.patch.object(hostsys, "open_firewall", side_effect=prompt):
            af.ensure_firewall(self.ctx())
        self.assertIn("Firewall rule added.", self.screen)

    def test_no_says_how_to_add_it_and_carries_on(self):
        ctx = self.ctx()
        with mock.patch.object(hostsys, "open_firewall", side_effect=OSError("could not add")):
            af.ensure_firewall(ctx)  # does not raise
        screen = "\n".join(self.screen)
        self.assertIn("The firewall rule could not be added", screen)
        self.assertIn('netsh advfirewall firewall add rule "name=S60 autoflash 192.168.1.50"', screen)
        self.assertIn("Adding it needs administrator rights", screen)
        self.assertEqual(af.recorded_rules(ctx), [])

    def test_no_on_the_way_out_says_how_to_remove_it(self):
        ctx = self.ctx([self.RULE])
        with mock.patch.object(hostsys, "close_firewall", return_value=False):
            af.close_firewall_rules(ctx)
        screen = "\n".join(self.screen)
        self.assertIn("Windows will ask for permission again", screen)
        self.assertIn("Remove the temporary firewall rules yourself", screen)
        self.assertIn('netsh advfirewall firewall delete rule "name=S60 autoflash 192.168.1.50"', screen)
        self.assertEqual(af.recorded_rules(ctx), [self.RULE])  # kept, to try again next time

    def test_yes_on_the_way_out_forgets_the_rule(self):
        ctx = self.ctx([self.RULE])
        with mock.patch.object(hostsys, "close_firewall", return_value=True):
            af.close_firewall_rules(ctx)
        self.assertIn("The temporary firewall rules have been removed.", self.screen)
        self.assertEqual(af.recorded_rules(ctx), [])


if __name__ == "__main__":
    unittest.main()
