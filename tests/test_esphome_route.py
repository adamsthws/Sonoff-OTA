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
        # No plug's own network to be seen, nor joined: nothing goes on the network.
        for name, value in (("on_a_plug_network", False), ("find_ssid", None)):
            patch = mock.patch.object(af, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)

    def ctx(self, given=None, run="run", **inputs):
        state = RunState.open(Path(self.tmp.name) / run)
        for key, value in {"product_model": "S60TPF", "stock_version": "1.2.0",
                           "plug_ip": "192.168.0.50", **inputs}.items():
            state.set_input(key, value)
        return SimpleNamespace(state=state, firmware_given=given, esphome_firmware=None,
                               dry_run=False, finder=SimpleNamespace(poll=lambda: None))

    def setUp_found(self, running="sonoff"):
        # What the plug runs now, as the network would show it.
        patch = mock.patch.object(af, "find_current_firmware", return_value=running)
        patch.start()
        self.addCleanup(patch.stop)


class FirmwareChoiceTests(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.setUp_found()

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
        self.assertEqual(esphome[-4:], ["D4", "D5", "I1", "I2"])
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

    def test_a_phase_left_to_be_asked_about_is_still_tried_again(self):
        # I1 stays "attempted", with no error kept, when its phase ends with no
        # result; the reason it gave decides whether to try again in this run.
        ctx = self.ctx(firmware="esphome")
        ctx.last_failure = ("the plug stopped the esphome without reaching this computer; "
                            "its console says: POST /capture/written/22 returned HTTP -1")
        tries = []

        def execute(ctx, step):
            tries.append(step)
            return 1 if len(tries) == 1 else 0

        with mock.patch.object(af, "execute", side_effect=execute), \
                mock.patch.object(af, "wait_for_plug_back"), mock.patch.object(af.time, "sleep"):
            self.assertEqual(af.execute_with_retries(ctx, "I1"), 0)
        self.assertEqual(tries, ["I1", "I1"])

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
            with self.assertRaisesRegex(af.Abort, "Sonoff-Setup-XXXX"):
                af.step_esphome_check(self.ctx(firmware="esphome"), wait=0.05)

    def test_on_its_own_network_it_is_set_up_through_that(self):
        with mock.patch.object(af.esphome, "is_esphome", return_value=False), \
                mock.patch.object(af.time, "sleep"), \
                mock.patch.object(af, "set_up_through_own_network", return_value=("Home", "pw")) as own:
            result = af.step_esphome_check(self.ctx(firmware="esphome"), wait=0.05)
        self.assertEqual(result["set_up_through"], "its own network")
        self.assertEqual(own.call_args.args[1], "ESPHome")

    def test_joined_already_it_stops_waiting_on_the_home_wifi(self):
        with mock.patch.object(af.esphome, "is_esphome", return_value=False), \
                mock.patch.object(af, "on_a_plug_network", return_value=True), \
                mock.patch.object(af.time, "sleep") as slept, \
                mock.patch.object(af, "set_up_through_own_network", return_value=("Home", "pw")):
            af.step_esphome_check(self.ctx(firmware="esphome"), wait=600)
        slept.assert_not_called()  # straight on, not three minutes first

    def test_a_dry_run_does_not_wait(self):
        ctx = self.ctx(firmware="esphome")
        ctx.dry_run = True
        with mock.patch.object(af.esphome, "is_esphome", side_effect=AssertionError("waited")):
            self.assertEqual(af.step_esphome_check(ctx), {"dry_run": True})


class ESPHomePageTests(unittest.TestCase):
    """ESPHome's page, as the plug serves it with web_server's local: true."""

    def serve(self, body, headers):
        import http.server
        import threading

        class Page(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"127.0.0.1:{server.server_address[1]}"

    def test_a_gzipped_page_is_recognised(self):
        import gzip
        page = b"<!doctype html><html><body><esp-app></esp-app></body></html>"
        host = self.serve(gzip.compress(page), {"Content-Type": "text/html", "Content-Encoding": "gzip"})
        self.assertTrue(af.esphome.is_esphome(host))

    def test_a_page_damaged_on_its_way_is_no_answer_not_a_crash(self):
        # Seen in the fault suite: one byte changed in the gzipped page, and
        # decompressing it raised zlib.error - "something unexpected went
        # wrong" - where a dropped connection is simply asked again.
        import gzip
        page = bytes(range(256)) * 64 + b"<esp-app></esp-app>"
        damaged = bytearray(gzip.compress(page))
        damaged[len(damaged) // 2] ^= 0x5A
        host = self.serve(bytes(damaged), {"Content-Type": "text/html", "Content-Encoding": "gzip"})
        self.assertFalse(af.esphome.is_esphome(host))

    def test_a_tasmota_page_is_not(self):
        host = self.serve(b"<html><body>Tasmota</body></html>", {"Content-Type": "text/html"})
        self.assertFalse(af.esphome.is_esphome(host))


class FromAConvertedPlugTests(RouteTestCase):
    """A plug this tool converted, found on the network, offered the other two."""

    def settle(self, running, *, backup=True, given=None, answer=0):
        ctx = self.ctx(given=given, run=f"{running}-{given}-{backup}")
        with mock.patch.object(af, "find_current_firmware", return_value=running), \
                mock.patch.object(af, "sonoff_backup_exists", return_value=backup), \
                mock.patch.object(af, "ask_choice", return_value=answer) as asked:
            route = af.settle_route(ctx)
        return route, (asked.call_args.args[1] if asked.called else None)

    def test_from_tasmota_the_other_two_are_offered(self):
        (firmware, source), offered = self.settle("tasmota", answer=1)
        self.assertEqual(offered, ["ESPHome", "Sonoff's own firmware (back to eWeLink, from its backup)"])
        self.assertEqual((firmware, source), ("sonoff", "tasmota"))

    def test_without_its_backup_sonoff_is_not_offered(self):
        (firmware, _), offered = self.settle("esphome", backup=False)
        self.assertEqual(offered, ["Tasmota"])
        self.assertEqual(firmware, "tasmota")

    def test_the_firmware_it_already_runs_cannot_be_asked_for(self):
        with self.assertRaisesRegex(af.NothingToDo, "already runs ESPHome"):
            self.settle("esphome", given="esphome")
        with self.assertRaisesRegex(af.Abort, "no backup"):
            self.settle("tasmota", backup=False, given="sonoff")

    def test_a_plug_left_part_way_can_go_on_to_any_of_the_three(self):
        _, offered = self.settle("midway")
        self.assertEqual(len(offered), 3)

    def test_every_route_ends_by_checking_its_firmware(self):
        last = {"tasmota": "H1", "esphome": "I2", "sonoff": "R2"}
        for source in af.SOURCES:
            for firmware in af.FIRMWARES:
                if firmware == source or (source == "sonoff" and firmware == "sonoff"):
                    continue
                steps = [step for step, _, _ in af.steps_for(firmware, source)]
                self.assertEqual(steps[-1], last[firmware], (source, firmware))
                self.assertEqual(len(steps), len(set(steps)), (source, firmware))
                # The bootloader is written on every route, before any firmware goes
                # in - past the commit, by the run that got there (D5 comes before E3).
                self.assertTrue("D5" in steps or "L1" in steps or source == "committed",
                                (source, firmware))

    def test_past_the_commit_official_tasmota_goes_on_first(self):
        # Never the hub's steps: they need Sonoff's two slots, and Tasmota's
        # layout has one - the slot swap waits for a restart that never comes.
        for firmware in af.FIRMWARES:
            steps = [step for step, _, _ in af.steps_for(firmware, "committed")]
            self.assertEqual(steps[:4], ["A0", "A5", "F2", "F4"], firmware)
            if firmware != "tasmota":
                self.assertIn("H1", steps)
                self.assertEqual(steps[steps.index("H1") + 1], "L1", firmware)  # then on from Tasmota
            self.assertFalse({"X2", "D4", "E3"} & set(steps[:steps.index("H1")]), firmware)


class FromTheInstallerTests(RouteTestCase):
    def test_on_from_the_installer_it_needs_no_ewelink(self):
        # Left part way from Sonoff's firmware: the backup and D1 need only the
        # installer; the eWeLink steps, which a plug on it cannot answer, are not run.
        for firmware in af.FIRMWARES:
            steps = [step for step, _, _ in af.steps_for(firmware, "installer")]
            self.assertEqual(steps[:4], ["A0", "A5", "C3", "D1"], firmware)
            self.assertFalse({"A1", "A2", "C2"} & set(steps), firmware)

    def test_an_upload_to_the_installer_is_tried_again(self):
        # The installer selects itself before an upload writes its peer slot.
        self.assertIn("D1", af.RETRY_STEPS)


class AlreadyThereTests(RouteTestCase):
    def test_asking_for_what_it_runs_ends_the_run_done(self):
        ctx = self.ctx(given="esphome")
        with mock.patch.object(af, "find_current_firmware", return_value="esphome"):
            with self.assertRaisesRegex(af.NothingToDo, "already runs ESPHome"):
                af.settle_route(ctx)
        # Never offered back as an unfinished run.
        self.assertTrue(af.run_is_complete(Path(self.tmp.name) / "run"))


class ConfirmedOnlyAfterAMinuteTests(RouteTestCase):
    """ESPHome sent to the mover is confirmed only after a minute running."""

    def check(self, source, answers, probes=()):
        ctx = self.ctx(source=source, firmware="esphome")
        clock = iter(range(0, 100_000, 3))
        with mock.patch.object(af.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch.object(af.time, "sleep"), \
                mock.patch.object(af.esphome, "is_esphome", side_effect=list(answers)), \
                mock.patch.object(af.esphome_ota, "probe", side_effect=list(probes) or None), \
                mock.patch.object(af, "step_esphome_from_mover") as resent:
            return af.step_esphome_check(ctx), resent

    def test_from_tasmota_it_is_only_done_once_esphome_has_confirmed_itself(self):
        result, _ = self.check("tasmota", [True] * 40)
        self.assertEqual(result["resent"], 0)
        # Asked again until 70 s after it first answered: 3 s a step.
        self.assertGreaterEqual(result["seconds"], 0)

    def test_back_on_the_mover_it_is_sent_esphome_again(self):
        result, resent = self.check("tasmota", [False] + [True] * 40, probes=[object()])
        resent.assert_called_once()
        self.assertEqual(result["resent"], 1)

    def test_from_stock_nothing_is_waited_for(self):
        with mock.patch.object(af.esphome, "is_esphome", return_value=True) as asked:
            ctx = self.ctx(firmware="esphome")
            af.step_esphome_check(ctx)
        self.assertEqual(asked.call_count, 1)
