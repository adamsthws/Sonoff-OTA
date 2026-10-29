"""The end-of-run relay and power meter check, as it reads in the report."""

import unittest
from unittest import mock

import tools.autoflash_cli as af


class DescribeHardwareTests(unittest.TestCase):
    def test_voltage_dropping_with_the_relay_off_proves_it_switches(self):
        result = af.describe_hardware(True, True, {"Voltage": 239, "Power": 0}, {"Voltage": 0, "Power": 0})
        self.assertEqual(result["Relay"], "switches (meter confirms)")
        self.assertEqual(result["Power meter"], "on: 239 V, 0 W; off: 0 V, 0 W")
        self.assertNotIn("Mains voltage", result)  # the power meter line shows it

    def test_a_load_losing_power_proves_it_switches(self):
        result = af.describe_hardware(True, True, {"Voltage": 238, "Power": 41.5}, {"Voltage": 238, "Power": 0})
        self.assertEqual(result["Relay"], "switches (meter confirms)")

    def test_no_change_on_the_meter_is_not_claimed_as_proof(self):
        result = af.describe_hardware(True, True, {"Voltage": 238, "Power": 0}, {"Voltage": 238, "Power": 0})
        self.assertEqual(result["Relay"], "switches (meter unchanged)")

    def test_a_relay_that_does_not_follow_the_command_is_reported(self):
        result = af.describe_hardware(True, False, None, None)
        self.assertEqual(result["Relay"], "did not switch")
        self.assertEqual(result["Power meter"], "no readings")
        self.assertNotIn("Mains voltage", result)  # the power meter line shows it


class CheckHardwareTests(unittest.TestCase):
    def ctx(self, readings, *, start="ON"):
        state = {"power": start}
        sent = []

        def command(text, timeout=None):
            sent.append(text)
            if text == "Power":
                return {"POWER": state["power"]}
            if text.startswith("Power "):
                state["power"] = text.split()[1]
                return {"POWER": state["power"]}
            if text == "Status 10":
                return {"StatusSNS": {"ENERGY": readings[state["power"]]}}
            raise AssertionError(text)

        from types import SimpleNamespace
        return SimpleNamespace(device=SimpleNamespace(command=command)), sent, state

    def run_check(self, ctx):
        with mock.patch.object(af.time, "sleep"), mock.patch.object(af, "say"):
            return af.check_hardware(ctx)

    def test_a_plug_that_was_off_goes_on_then_back_off(self):
        ctx, sent, state = self.ctx({"ON": {"Voltage": 240, "Power": 3}, "OFF": {"Voltage": 0, "Power": 0}},
                                    start="OFF")
        result = self.run_check(ctx)
        self.assertEqual([c for c in sent if c.startswith("Power ")], ["Power ON", "Power OFF"])
        self.assertEqual(state["power"], "OFF")
        self.assertEqual(result["Relay"], "switches (meter confirms)")
        self.assertNotIn("Mains voltage", result)  # the power meter line shows it

    def test_a_plug_that_was_on_goes_off_then_back_on(self):
        ctx, sent, state = self.ctx({"ON": {"Voltage": 231.5, "Power": 40}, "OFF": {"Voltage": 231.5, "Power": 0}})
        result = self.run_check(ctx)
        self.assertEqual([c for c in sent if c.startswith("Power ")], ["Power OFF", "Power ON"])
        self.assertEqual(state["power"], "ON")
        self.assertNotIn("Mains voltage", result)  # the power meter line shows it
        self.assertEqual(result["Power meter"], "on: 231.5 V, 40 W; off: 231.5 V, 0 W")

    def test_the_screen_starts_each_answer_with_a_capital_as_the_report_does(self):
        ctx, _, _ = self.ctx({"ON": {"Voltage": 240, "Power": 3}, "OFF": {"Voltage": 0, "Power": 0}})
        with mock.patch.object(af.time, "sleep"), mock.patch.object(af, "say") as said:
            af.check_hardware(ctx)
        lines = [c.args[0] for c in said.call_args_list]
        self.assertIn("    Relay: Switches (meter confirms)", lines)
        self.assertIn("    Power meter: On: 240 V, 3 W; off: 0 V, 0 W", lines)

    def test_nothing_is_asked(self):
        ctx, _, _ = self.ctx({"ON": {}, "OFF": {}})
        with mock.patch.object(af, "ask_yes_no", side_effect=AssertionError("asked")):
            self.run_check(ctx)

    def test_a_plug_that_stops_answering_is_reported_not_raised(self):
        from types import SimpleNamespace
        ctx = SimpleNamespace(device=SimpleNamespace(command=mock.Mock(side_effect=OSError("refused"))))
        result = self.run_check(ctx)
        self.assertEqual(result["Relay"], "could not be checked")
        self.assertNotIn("Mains voltage", result)  # the power meter line shows it


if __name__ == "__main__":
    unittest.main()
