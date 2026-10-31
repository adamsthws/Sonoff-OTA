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


class NewScenarioTests(unittest.TestCase):
    def test_every_route_has_its_apply_cut_and_the_writing_steps_their_double_and_readdress(self):
        ids = {sc["id"] for sc in fault_suite.scenarios()}
        for route, steps in fault_suite.ROUTES.items():
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
        for said, cycles in (("[..] FAILED: ... switch the plug off and on, then run", 1),
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
                if "off and on" in line and "plug" in line:
                    self.assertRegex(line, fault_suite.POWER_CYCLE)
        self.assertRegex("Switch the plug off and on - it keeps", fault_suite.POWER_CYCLE)


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
