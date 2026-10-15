import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tools.s60_autoflash as af
from tools.autoflash import prompts
from tools.autoflash.state import DONE, RunState


class RouteTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)

    def ctx(self, given=None, run="run", **inputs):
        state = RunState.open(Path(self.tmp.name) / run)
        for key, value in {"product_model": "S60TPF", "stock_version": "1.2.0",
                           "plug_ip": "192.168.0.50", **inputs}.items():
            state.set_input(key, value)
        return SimpleNamespace(state=state, firmware_given=given, esphome_firmware=None,
                               dry_run=False)


class FirmwareChoiceTests(RouteTestCase):
    def test_it_is_asked_once_then_kept(self):
        ctx = self.ctx()
        with mock.patch.object(af, "ask_choice", return_value=1) as asked:
            self.assertEqual(af.settle_firmware(ctx), "esphome")
        self.assertEqual(asked.call_args.args[1], ["Tasmota", "ESPHome"])
        self.assertEqual(ctx.state.input("firmware"), "esphome")
        with mock.patch.object(af, "ask_choice", side_effect=AssertionError("asked again")):
            self.assertEqual(af.settle_firmware(ctx), "esphome")

    def test_given_on_the_command_line_it_is_not_asked(self):
        ctx = self.ctx(given="esphome")
        with mock.patch.object(af, "ask_choice", side_effect=AssertionError("asked")):
            self.assertEqual(af.settle_firmware(ctx), "esphome")

    def test_a_conversion_cannot_switch_part_way(self):
        ctx = self.ctx(given="tasmota", firmware="esphome")
        with self.assertRaisesRegex(af.Abort, "cannot switch"):
            af.settle_firmware(ctx)

    def test_a_run_begun_before_there_was_a_choice_installs_tasmota(self):
        ctx = self.ctx()
        ctx.state.mark("A1", DONE)
        with mock.patch.object(af, "ask_choice", side_effect=AssertionError("asked")):
            self.assertEqual(af.settle_firmware(ctx), "tasmota")

    def test_an_unattended_run_must_be_told(self):
        ctx = self.ctx()
        with mock.patch.object(prompts, "UNATTENDED", True):
            with self.assertRaisesRegex(af.Abort, "--firmware esphome"):
                af.settle_firmware(ctx)
        self.assertIsNone(ctx.state.input("firmware"))


class RouteTests(RouteTestCase):
    def test_each_firmware_has_its_own_steps(self):
        esphome = [step for step, _, _ in af.steps_for("esphome")]
        tasmota = [step for step, _, _ in af.steps_for("tasmota")]
        self.assertEqual(esphome[-3:], ["D4", "I1", "I2"])
        self.assertNotIn("E3", esphome)  # the partition table is left as Sonoff made it
        self.assertEqual(tasmota[-1], "H1")
        self.assertEqual(esphome[:len(af.COMMON_STEPS)], tasmota[:len(af.COMMON_STEPS)])

    def test_d4_is_named_for_what_it_does_on_each_route(self):
        self.addCleanup(af.apply_target, "tasmota")
        af.apply_target("esphome")
        self.assertEqual(af.stage_name("D4"), "Making room for ESPHome")
        af.apply_target("tasmota")
        self.assertEqual(af.stage_name("D4"), "Preparing memory layout")

    def test_an_install_cut_short_by_the_network_is_tried_again(self):
        self.assertIn("I1", af.RETRY_STEPS)
        said = ("esphome did not pass: the plug says: migration_refused: "
                "POST /capture/written/21 returned HTTP -11 after 6 attempts")
        self.assertRegex(said, af.TRANSIENT_RE)

    def test_a_run_is_complete_by_its_own_firmware(self):
        ctx = self.ctx(firmware="esphome")
        ctx.state.mark("I2", DONE)
        self.assertTrue(af.run_is_complete(Path(self.tmp.name) / "run"))

    def test_d4_is_not_rechecked_once_esphome_runs(self):
        ctx = self.ctx(firmware="esphome")
        ctx.state.mark("I1", DONE)
        ctx.device = SimpleNamespace(current_ota=lambda: self.fail("asked the plug"))
        with mock.patch.object(af, "commit_report", return_value=None):
            self.assertEqual(af.verify_high_slot(ctx), (True, ""))


class EndingTests(RouteTestCase):
    def test_the_report_says_esphome(self):
        fields = dict(af.report_fields(self.ctx(firmware="esphome"), 300, False))
        self.assertEqual(fields["Firmware"], "ESPHome")
        self.assertEqual(fields["Name in ESPHome"], "Sonoff S60")
        self.assertNotIn("Name in Tasmota", fields)
        tasmota = dict(af.report_fields(self.ctx(run="tasmota-run"), 300, False))
        self.assertEqual(tasmota["Firmware"], "Tasmota")

    def test_the_hardware_check_switches_through_esphome(self):
        relay = {"on": True}

        def switch(host, on):
            relay["on"] = on
            return True

        def meter(host):
            return {"Voltage": 236.0, "Power": 0.0} if relay["on"] else {"Voltage": 0.0, "Power": 0.0}

        with mock.patch.object(af, "METER_SETTLE", 0), \
                mock.patch.object(af.esphome, "relay", side_effect=lambda host: relay["on"]), \
                mock.patch.object(af.esphome, "switch_relay", side_effect=switch), \
                mock.patch.object(af.esphome, "meter", side_effect=meter):
            result = af.check_esphome_hardware(self.ctx(firmware="esphome"))
        self.assertTrue(relay["on"], "left as it was found")
        self.assertIn("236 V", result["Power meter"])

    def test_a_plug_that_does_not_answer_is_reported_not_raised(self):
        with mock.patch.object(af.esphome, "relay", return_value=None):
            result = af.check_esphome_hardware(self.ctx(firmware="esphome"))
        self.assertEqual(result["Relay"], "could not be checked")


class CheckStepTests(RouteTestCase):
    def test_it_recognises_esphome_itself(self):
        with mock.patch.object(af.esphome, "is_esphome", return_value=True):
            self.assertIn("seconds", af.step_esphome_check(self.ctx(firmware="esphome")))

    def test_waiting_in_vain_points_at_the_wifi(self):
        with mock.patch.object(af.esphome, "is_esphome", return_value=False), \
                mock.patch.object(af.time, "sleep"):
            with self.assertRaisesRegex(af.Abort, "sonoff-s60-xxxxxx"):
                af.step_esphome_check(self.ctx(firmware="esphome"), wait=0.05)

    def test_a_dry_run_does_not_wait(self):
        ctx = self.ctx(firmware="esphome")
        ctx.dry_run = True
        with mock.patch.object(af.esphome, "is_esphome", side_effect=AssertionError("waited")):
            self.assertEqual(af.step_esphome_check(ctx), {"dry_run": True})
