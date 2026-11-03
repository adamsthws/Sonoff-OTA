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
            instance = SimpleNamespace(state=Path(tmp), power=switched.append)
            serial = Path(tmp, "serial.log")
            serial.write_bytes(b"")
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
