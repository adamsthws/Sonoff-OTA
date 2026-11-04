"""The S61's fault suite: its scenarios, and how it tells a plug converted."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from tools.beken.emulator import fault_suite


class ScenarioTests(unittest.TestCase):
    def test_every_direction_and_id_once(self):
        found = fault_suite.scenarios()
        ids = [sc["id"] for sc in found]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual({sc["route"] for sc in found}, set(fault_suite.ROUTES))

    def test_the_steps_that_write_are_also_faulted_early_and_late(self):
        ids = {sc["id"] for sc in fault_suite.scenarios()}
        self.assertIn("sonoff>openbeken:B2@10:plugoff-1", ids)
        self.assertIn("openbeken>sonoff:O2@90:crash-1", ids)
        self.assertNotIn("sonoff>openbeken:K1@10:plugoff-1", ids)   # a check step: 40% only

    def test_each_step_faulted_is_one_its_route_takes(self):
        for route, steps in fault_suite.ROUTES.items():
            self.assertLessEqual(set(steps), set(fault_suite.route_steps(route)), route)


class BadImageScenarioTests(unittest.TestCase):
    def test_each_step_that_sends_an_image_the_bootloader_installs_has_one_it_refuses(self):
        found = {sc["id"] for sc in fault_suite.scenarios() if sc["fault"] == "badimage"}
        self.assertEqual(found, {"sonoff>openbeken:B2:badimage-once", "sonoff>esphome:B2:badimage-once",
                                 "sonoff>esphome:J2:badimage-once", "openbeken>esphome:O1:badimage-once",
                                 "interim>esphome:J2:badimage-once"})

    def test_it_hears_the_run_send_the_image_again(self):
        # Matched against what the run says as it goes back (arrived.until_back).
        from unittest import mock
        from tools.autoflash import errors
        from tools.autoflash.routes.beken import arrived

        def look(restarted):
            raise arrived.NotTaken
        ctx = SimpleNamespace(state=SimpleNamespace(input=lambda key: None, set_input=lambda key, value: None))
        with mock.patch.object(arrived.prompts, "UNATTENDED", True), self.assertRaises(errors.GoBack) as back:
            arrived.until_back(ctx, look, "OpenBeken", "advice", sent_by="B2")
        self.assertRegex(back.exception.reason, fault_suite.SENT_AGAIN)


class NewScenarioTests(unittest.TestCase):
    def test_every_route_that_writes_has_its_apply_cut_and_the_writing_steps_their_double_and_readdress(self):
        ids = {sc["id"] for sc in fault_suite.scenarios()}
        for route, steps in fault_suite.ROUTES.items():
            if not set(steps) & fault_suite.beken.DESTRUCTIVE_STEPS:
                continue
            for at in (10, 50, 90):
                self.assertIn(f"{route}:apply@{at}:plugoff-1", ids)
            for step in set(steps) & fault_suite.beken.DESTRUCTIVE_STEPS:
                self.assertIn(f"{route}:{step}:double-plugoff-1", ids)
                self.assertIn(f"{route}:{step}:readdress-new", ids)


class ApplyCutTests(unittest.TestCase):
    """The cut while the bootloader writes the image in, timed off its output."""

    def watch(self, lines, at, *, reads=None):
        """What the cut did as `lines` came out one at a time - or, with
        `reads`, in those groups of lines, each taken in by one read."""
        with tempfile.TemporaryDirectory() as tmp:
            switched = []
            serial = Path(tmp, "serial.log")
            serial.write_bytes(b"")

            def power(how):
                switched.append(how)
                if how == "off":
                    with serial.open("ab") as out:
                        out.write(fault_suite.SWITCHED_OFF + b"; its flash is kept\n")
            instance = SimpleNamespace(state=Path(tmp), power=power)
            cut = fault_suite.ApplyCut(instance, at)
            cut.start()
            for group in reads or [[line] for line in lines]:
                with serial.open("ab") as out:
                    out.write(b"".join(line + b"\n" for line in group))
                time.sleep(0.5 if reads else 0.05)
            deadline = time.monotonic() + 3
            while cut.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            cut.done.set()
            cut.join(2)
            return cut.fired, switched

    def write(self, *percents):
        return [b"[I/OTA] OTA Write: [" + b"=" * p + b">] %d%%" % p for p in percents]

    def test_cut_at_its_share_of_the_bootloaders_write(self):
        lines = [b"[I/OTA] OTA firmware(app) upgrade(1.0.2->1.0.0) startup."] + self.write(5, 30, 49, 52, 80)
        self.assertEqual(self.watch(lines, 50), (52, ["off", "on"]))

    def test_progress_before_an_apply_begins_is_not_it(self):
        # The firmware's own download prints no such line; a stray one is ignored.
        self.assertEqual(self.watch(self.write(60, 70), 50), (None, []))

    def test_an_apply_over_before_its_moment_waits_for_the_next(self):
        lines = ([b"OTA firmware(app) upgrade(a->b) startup."] + self.write(10, 20)
                 + [b"[E/OTA] (ota_erase_dl_rbl:32) ota_erase_dl_rbl erase:286000"]
                 + [b"OTA firmware(app) upgrade(a->b) startup."] + self.write(95))
        self.assertEqual(self.watch(lines, 90), (95, ["off", "on"]))
        # One read taking in the first's end and the next's start, as a busy computer's may.
        self.assertEqual(self.watch(lines, 90, reads=[lines[:-1], lines[-1:]]), (95, ["off", "on"]))


class FaultAppliedTests(unittest.TestCase):
    """A fault that never happened is not a pass (fault_suite.not_fired)."""

    def instance(self, tmp, says_off=True):
        serial = Path(tmp, "serial.log")
        serial.write_bytes(b"[plug] switched off; an earlier cut\n")

        def power(how):
            self.switched.append(how)
            if how == "off" and says_off:
                with serial.open("ab") as out:
                    out.write(fault_suite.SWITCHED_OFF + b"; its flash is kept\n")
        self.switched = []
        return SimpleNamespace(state=Path(tmp), power=power, network="a-network", computer="a-computer",
                               plug="a-plug", lease=lambda: "198.18.62.51")

    def test_a_plug_seen_to_stop_is_a_cut(self):
        with tempfile.TemporaryDirectory() as tmp:
            notes = {}
            fault_suite.inject(self.instance(tmp), "plugoff", 0, notes)
        self.assertEqual(self.switched, ["off", "on"])
        self.assertNotIn("not_applied", notes)

    def test_a_plug_that_never_says_it_stopped_is_not(self):
        # Only a line since the switch counts: an earlier cut's is in the log already.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(fault_suite.cut_power(self.instance(tmp, says_off=False), 0, wait=0.5))
        self.assertEqual(self.switched, ["off", "on"])

    def test_the_apply_cut_does_not_fire_on_a_plug_that_stayed_on(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            serial = Path(tmp, "serial.log")
            instance = self.instance(tmp)
            serial.write_bytes(b"OTA firmware(app) upgrade(a->b) startup.\nOTA Write: [=>] 60%\n")
            cut = fault_suite.ApplyCut(instance, 50)
            with mock.patch.object(fault_suite, "cut_power", return_value=False):
                cut.start()
                cut.join(3)
        self.assertIsNone(cut.fired)

    def docker(self, fault, arg, codes):
        from unittest import mock
        notes = {}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(fault_suite.time, "sleep"), \
                mock.patch.object(fault_suite.subprocess, "run",
                                  side_effect=[SimpleNamespace(returncode=c, stdout="198.18.62.2\n") for c in codes]):
            if fault == "readdress":
                fault_suite.readdress(self.instance(tmp), notes)
            else:
                fault_suite.inject(self.instance(tmp), fault, arg, notes)
        return notes

    def test_each_docker_command_that_fails_is_noted(self):
        for fault, arg, codes in (("outage", 0, [0, 1, 0]), ("outage", 0, [0, 0, 1]), ("crash", 1, [1]),
                                  ("ctrlc", "once", [1]), ("readdress", None, [1])):
            with self.subTest(fault=fault, codes=codes):
                self.assertTrue(self.docker(fault, arg, codes).get("not_applied"))

    def test_each_docker_command_that_works_is_not(self):
        for fault, arg, codes in (("outage", 0, [0, 0, 0]), ("crash", 1, [0]), ("ctrlc", "once", [0]),
                                  ("readdress", None, [0])):
            with self.subTest(fault=fault):
                self.assertNotIn("not_applied", self.docker(fault, arg, codes))

    def test_an_outage_with_no_address_to_come_back_to_is_not_started(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(fault_suite.subprocess, "run",
                                  return_value=SimpleNamespace(returncode=0, stdout="\n")) as run:
            notes = {}
            fault_suite.inject(self.instance(tmp), "outage", 0, notes)
        self.assertEqual(run.call_count, 1)
        self.assertTrue(notes["not_applied"])

    def test_what_never_happened_proves_nothing(self):
        self.assertFalse(fault_suite.not_fired("plugoff", {}))
        self.assertTrue(fault_suite.not_fired("plugoff", {"not_applied": ["x"]}))
        self.assertTrue(fault_suite.not_fired("double", {}))
        self.assertFalse(fault_suite.not_fired("double", {"second_cut": True}))
        self.assertTrue(fault_suite.not_fired("readdress", {"moved_from": "a", "moved_to": "a"}))
        self.assertTrue(fault_suite.not_fired("readdress", {"moved_from": "", "moved_to": "a"}))
        self.assertFalse(fault_suite.not_fired("readdress", {"moved_from": "a", "moved_to": "b"}))
        self.assertTrue(fault_suite.not_fired("ctrlc", {"ctrlc_seen": False}))
        self.assertFalse(fault_suite.not_fired("ctrlc", {"ctrlc_seen": True}))

    def test_a_plug_that_stayed_on_is_noted(self):
        from unittest import mock
        notes = {}
        with mock.patch.object(fault_suite, "cut_power", return_value=False):
            fault_suite.inject(None, "plugoff", 1, notes)
        self.assertTrue(notes["not_applied"])

    def step_result(self, *, is_converted):
        from unittest import mock
        sc = next(sc for sc in fault_suite.scenarios() if sc["fault"] == "ctrlc")
        instance = SimpleNamespace(tool=lambda *a, **k: None, last_state=lambda: {}, last_log=lambda: "",
                                   runs=lambda: [])
        with mock.patch.object(fault_suite, "wait_for_step", return_value=True), \
                mock.patch.object(fault_suite, "inject"), mock.patch.object(fault_suite, "finish"), \
                mock.patch.object(fault_suite, "recover"), mock.patch.object(fault_suite.time, "sleep"), \
                mock.patch.object(fault_suite, "converted", return_value=is_converted):
            return fault_suite.step_scenario(instance, sc, "esphome", Path("unused"), {})["result"]

    def test_converted_without_its_fault_is_not_fired_and_a_failure_stands(self):
        self.assertEqual(self.step_result(is_converted=True), "NOT-FIRED")
        self.assertEqual(self.step_result(is_converted=False), "FAIL")

    def test_only_a_failure_is_labelled_starved(self):
        starved = {"notes": {"memory": {"starved": True}}}
        self.assertIn("memory-starved", fault_suite.starved_label({**starved, "result": "FAIL"}))
        self.assertEqual(fault_suite.starved_label({**starved, "result": "PASS"}), "")
        self.assertEqual(fault_suite.starved_label({"result": "ERROR", "notes": {"error": "x"}}), "")
        self.assertEqual(fault_suite.starved_label({}), "")


class RecoverTests(unittest.TestCase):
    def test_the_plug_is_switched_off_and_on_only_where_the_run_said_to(self):
        for said, cycles in (("[..] FAILED: ... switch the device off and on, then run", 1),
                             ("[..] FAILED: eWeLink could not be reached", 0)):
            powered = []
            instance = SimpleNamespace(last_log=lambda said=said: said, power=powered.append,
                                       ready=lambda firmware, wait=0: True)
            notes = {}
            fault_suite.recover(instance, "esphome", notes)
            self.assertEqual((powered, notes.get("power_cycles", 0)), (["cycle"] * cycles, cycles))

    def test_a_step_is_seen_starting_from_the_runs_own_line(self):
        log = ["[05:36:49] [skip] O1 ESPHome, fetched by OpenBeken (recorded as done)\n"]
        instance = SimpleNamespace(last_log=lambda: "".join(log))
        run = SimpleNamespace(poll=lambda: None)
        threading.Timer(0.3, lambda: log.append("[05:36:50] O1  ESPHome, fetched by OpenBeken\n")).start()
        self.assertTrue(fault_suite.wait_for_step(instance, "O1", run, limit=5))
        self.assertFalse(fault_suite.wait_for_step(SimpleNamespace(last_log=lambda: log[0]), "O1",
                                                   SimpleNamespace(poll=lambda: None), limit=0.6))


class InterimTests(unittest.TestCase):
    CLEAN = bytes([0xFF]) * 0x400000

    def test_a_new_openbeken_is_checked_with_scenarios_the_suite_has(self):
        self.assertLessEqual(set(fault_suite.OPENBEKEN_CHECK), {sc["id"] for sc in fault_suite.scenarios()})

    def test_every_way_from_the_interim_firmware(self):
        self.assertLessEqual({"interim>esphome", "interim>openbeken", "interim>sonoff"}, set(fault_suite.ROUTES))

    def test_kept_as_openbeken_nothing_is_written_and_j3_waits_for_its_restart(self):
        # J3 sends no image, so there is no bootloader write to cut; and it
        # waits for the restart itself, so K1 finds the plug answering at once.
        ids = {sc["id"] for sc in fault_suite.scenarios() if sc["route"] == "interim>openbeken"}
        self.assertFalse({i for i in ids if ":apply@" in i or ":K1:" in i})

    def test_the_names_a_plug_asked_dhcp_for(self):
        log = ("Nov  3 10:00:01 dnsmasq-dhcp[7]: 3126 DHCPACK(br0) 198.18.0.50 12:34:56:ab:cd:ef\n"
               "Nov  3 10:01:02 dnsmasq-dhcp[7]: 3127 DHCPACK(br0) 198.18.0.50 12:34:56:ab:cd:ef "
               "autoflash-recovery-CDEF\n")
        self.assertEqual([m.group(1) for m in fault_suite.DHCP_NAME.finditer(log)], ["autoflash-recovery-CDEF"])

    def test_through_the_interim_firmware_by_its_own_name_and_left_clean(self):
        self.assertEqual(fault_suite.interim_problems(
            "sonoff>esphome", ["autoflash-recovery-CDEF", "esphome-abcdef"], self.CLEAN), [])

    def test_the_interim_firmware_by_openbekens_name(self):
        problems = fault_suite.interim_problems(
            "sonoff>esphome", ["openbeken-abcdef", "esphome-abcdef"], self.CLEAN)
        self.assertEqual(len(problems), 2)   # never its own name, and OpenBeken's

    def test_its_settings_left_behind(self):
        flash = bytearray(self.CLEAN)
        flash[0x1E5000 + 0x10] = 0
        problems = fault_suite.interim_problems(
            "interim>esphome", ["autoflash-recovery-CDEF", "esphome-abcdef"], bytes(flash))
        self.assertEqual(problems, ["the interim firmware's settings at 0x1e5000 were not erased"])

    def test_kept_as_openbeken_by_openbekens_name(self):
        self.assertEqual(fault_suite.interim_problems(
            "interim>openbeken", ["autoflash-recovery-CDEF", "openbeken-abcdef"], None), [])
        self.assertTrue(fault_suite.interim_problems("interim>openbeken", ["autoflash-recovery-CDEF"], None))

    def test_routes_that_never_run_it_ask_nothing_of_it(self):
        self.assertEqual(fault_suite.interim_problems("sonoff>openbeken", ["openbeken-abcdef"], None), [])
        self.assertEqual(fault_suite.interim_problems("openbeken>sonoff", ["openbeken-abcdef"], None), [])


class ConvertedTests(unittest.TestCase):
    def state(self, **steps):
        return {"steps": {step: {"status": status} for step, status in steps.items()}}

    def test_converted_when_every_step_of_the_route_is_done(self):
        self.assertTrue(fault_suite.converted(self.state(B1="done", B2="done", K1="done", B4="done"),
                                              "sonoff>openbeken"))

    def test_not_while_a_step_is_left(self):
        self.assertFalse(fault_suite.converted(self.state(B1="done", B2="done", K1="failed"),
                                               "sonoff>openbeken"))
        self.assertFalse(fault_suite.converted({}, "esphome>sonoff"))


class RecoveryTests(unittest.TestCase):
    def test_the_tools_every_ask_to_switch_the_plug_off_and_on_is_heard(self):
        from tools.autoflash.routes.beken import arrived, stock
        import inspect
        for module in (arrived, stock):
            for line in inspect.getsource(module).splitlines():
                if "off and on" in line and ("plug" in line or "device" in line):
                    self.assertRegex(line, fault_suite.POWER_CYCLE)
        self.assertRegex("Switch the device off and on - it keeps", fault_suite.POWER_CYCLE)


class SavingTests(unittest.TestCase):
    def test_a_saved_plug_keeps_its_flash_and_its_runs_captures_only(self):
        self.assertEqual(fault_suite.keep_for_saving("/x/work", ["captures", "tools", "README.md"]),
                         ["tools", "README.md"])
        self.assertEqual(fault_suite.keep_for_saving("/x/autoflash-s61-1",
                                                     ["flash.bin", "serial.log", "power", "ca.crt"]),
                         ["serial.log", "power", "ca.crt"])
        self.assertEqual(fault_suite.keep_for_saving("/x/plugs/12-34-56-ab-cd-ef", ["plug.json", "sonoff-backup"]),
                         ["sonoff-backup"])


if __name__ == "__main__":
    unittest.main()
