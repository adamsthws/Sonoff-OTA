"""The fault suite's own logic: the names a plug may go by, the saved plugs
each plug has, and the scenarios it lists."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/espressif/emulator"))
import fault_suite as fs  # noqa: E402

MAC = "12:34:56:ab:cd:ef"


class NameProblemTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(fs, "MAC", MAC)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_conversion_under_the_names_it_should_have(self):
        names = ["espressif", "autoflash-recovery-CDEF", "tasmota-ABCDEF-1234", "tasmota-abcdef"]
        self.assertEqual(fs.name_problems(names, ["autoflash-recovery-CDEF", "ESP_ABCDF0"], "tasmota"), ([], None))
        self.assertEqual(fs.name_problems(["tasmota-abcdef", "autoflash-recovery-CDEF", "esphome-abcdef"],
                                          [], "esphome"), ([], None))
        self.assertEqual(fs.name_problems(["esphome-abcdef", "autoflash-recovery-CDEF", "espressif"],
                                          [], "sonoff"), ([], None))

    def test_a_name_this_plug_should_never_have(self):
        unknown, _ = fs.name_problems(["autoflash-recovery-1234", "tasmota-abcdef"], ["tasmota"], "tasmota")
        self.assertEqual(unknown, ["autoflash-recovery-1234", "tasmota"])

    def test_not_ending_under_its_converted_name(self):
        _, final = fs.name_problems(["autoflash-recovery-CDEF", "tasmota-ABCDEF-1234"], [], "tasmota")
        self.assertEqual(final, "tasmota-ABCDEF-1234")

    def test_names_of_earlier_releases_are_allowed(self):
        names = ["sonoff-s60-abcdef", "s60-ota-bridge", "s60-recovery-0042", "x0042", "sonoff-s60-mover-abcdef",
                 "tasmota-abcdef"]
        self.assertEqual(fs.name_problems(names, ["Sonoff-Setup-CDEF", "S60-OTA-Bridge-CDEF"], "tasmota"),
                         ([], None))


class SavedPlugTests(unittest.TestCase):
    def saved_as(self, owner, mac):
        with tempfile.TemporaryDirectory() as emu:
            if owner:
                (Path(emu) / "saved/stub-suite-tasmota/captures/plugs" / owner).mkdir(parents=True)
            with mock.patch.object(fs, "EMU", Path(emu)), mock.patch.object(fs, "STUB_CLOUD", True), \
                 mock.patch.object(fs, "MAC", mac):
                return fs.saved("suite-tasmota")

    def test_the_plug_that_saved_it_has_it(self):
        self.assertEqual(self.saved_as("12-34-56-ab-cd-ef", MAC), "stub-suite-tasmota")

    def test_another_plug_has_its_own_beside_it(self):
        self.assertEqual(self.saved_as("12-34-56-ab-cd-ef", "12:34:56:00:11:22"), "stub-suite-tasmota-1122")

    def test_one_not_saved_yet_is_the_first_plugs(self):
        self.assertEqual(self.saved_as(None, MAC), "stub-suite-tasmota")


class ScaledBatchTests(unittest.TestCase):
    def allowed_with(self, text):
        with tempfile.TemporaryDirectory() as out:
            if text is not None:
                (Path(out) / "workers").write_text(text)
            with mock.patch.object(fs, "OUT", Path(out)):
                return fs.allowed(4)

    def test_the_number_in_the_file_while_it_is_there(self):
        self.assertEqual(self.allowed_with("6\n"), 6)
        self.assertEqual(self.allowed_with("0"), 0)

    def test_how_many_it_began_with_otherwise(self):
        self.assertEqual(self.allowed_with(None), 4)
        self.assertEqual(self.allowed_with("lots"), 4)

    def test_a_worker_held_back_stops_once_nothing_is_left(self):
        todo = fs.queue.Queue()
        with mock.patch.object(fs.time, "sleep", side_effect=AssertionError("waited")):
            fs.worker(5, todo, None, room=lambda n: False)


class ScenarioTests(unittest.TestCase):
    def test_every_scenario_has_its_own_id(self):
        ids = [sc["id"] for sc in fs.scenarios()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_the_composite_faults_are_listed(self):
        ids = {sc["id"] for sc in fs.scenarios()}
        for wanted in ("sonoff>tasmota:C3:ownnet-cut", "sonoff>esphome:D1:lostanswer-once",
                       "sonoff>tasmota:D1:seven-advised", "esphome>tasmota:G1:seven-times",
                       "sonoff>tasmota:F4:nohook-run", "esphome>tasmota:F4rs@90:plugoff-1"):
            self.assertIn(wanted, ids)

    def test_each_length_only_where_it_leads_somewhere_of_its_own(self):
        ids = {sc["id"] for sc in fs.scenarios()}
        self.assertFalse([i for i in ids if i.endswith(":outage-1")])
        self.assertIn("tasmota>sonoff:D4:outage-15", ids)
        # A rerun 60 s after a crash: only where a fallback network may come up.
        self.assertIn("tasmota>esphome:L1:crash-60", ids)
        self.assertIn("sonoff>tasmota:C3fb:crash-60", ids)
        self.assertIn("sonoff>esphome:C2:crash-60", ids)
        self.assertNotIn("tasmota>sonoff:D4:crash-60", ids)

    def test_a_packet_is_changed_only_where_a_step_sends_one(self):
        # R2 only sees Sonoff's LAN port take a connection: no packet there
        # carries data, and corrupt-once was always NO-TRAFFIC.
        ids = {sc["id"] for sc in fs.scenarios()}
        for route in ("tasmota>sonoff", "esphome>sonoff"):
            self.assertNotIn(f"{route}:R2:corrupt-once", ids)
            self.assertIn(f"{route}:R2:outage-5", ids)
            self.assertIn(f"{route}:R1:corrupt-once", ids)
        self.assertIn("sonoff>tasmota:H1:corrupt-once", ids)

    def test_the_tools_own_handling_at_the_riskiest_steps(self):
        ids = {sc["id"] for sc in fs.scenarios()}
        for fault in ("second-run", "ctrlc-countdown", "ctrlc-apart", "sleep-120"):
            self.assertIn(f"esphome>tasmota:F4:{fault}", ids)
            self.assertIn(f"sonoff>tasmota:C2:{fault}", ids)
            self.assertNotIn(f"esphome>tasmota:E1:{fault}", ids)
        self.assertIn("tasmota>esphome:M1:sleep-120", ids)
        self.assertNotIn("tasmota>esphome:M1:second-run", ids)
        self.assertIn("esphome>tasmota:E1:hangup-continue", ids)

    def test_runs_left_by_1_2_0_are_carried_on(self):
        ids = {sc["id"] for sc in fs.scenarios()}
        for step in ("C2", "C3", "D1", "D4", "E1", "E2", "E3", "F2", "F4", "G1", "H1"):
            self.assertIn(f"sonoff>tasmota:{step}:legacy-v1.2.0", ids)
        self.assertIn("tasmota>esphome:start:legacy-v1.2.0", ids)
        self.assertIn("tasmota>sonoff:start:legacy-v1.2.0", ids)
        # G1 over that release's startup firmware, unproven, cut off part way.
        self.assertIn("sonoff>tasmota:F4:legacy-v1.2.0+G1off", ids)
        self.assertTrue(fs.needs_cloud({"route": "tasmota>esphome", "step": "start", "fault": "legacy"}))

    def test_runs_left_by_the_release_before_a_firmware_update_are_carried_on(self):
        # Its bundle pinned to the firmware the tool installed then
        # (docs/updating-firmware.md): every step from Sonoff, both ways, and
        # each kind of plug it converted.
        ids = {sc["id"] for sc in fs.scenarios()}
        for route, steps in (("sonoff>tasmota", fs.ROUTES["sonoff>tasmota"][2]),
                             ("sonoff>esphome", fs.ROUTES["sonoff>esphome"][2])):
            for step in set(steps) - {"C3fb"} | {"C2"}:
                self.assertIn(f"{route}:{step}:legacy-v2.0.2", ids)
        for route in ("tasmota>esphome", "tasmota>sonoff", "esphome>tasmota", "esphome>sonoff"):
            self.assertIn(f"{route}:start:legacy-v2.0.2", ids)
        self.assertNotIn("sonoff>tasmota:F4:legacy-v2.0.2+G1off", ids)

    def test_phases_must_ride_out_blips_by_themselves(self):
        # PR #5: one early blip used up a phase's 20 s of retrying, and a later
        # one failed on its first try - the step then tried again from the start.
        ids = {sc["id"] for sc in fs.scenarios()}
        for route, step in fs.BLIP_STEPS:
            self.assertIn(f"{route}:{step}@10:blips-3", ids)
            self.assertIn(f"{route}:{step}@10:lossy-phase", ids)
        # Two blips 80% of the step apart: more than a phase's 20 s of retrying
        # in the longer ones, which PR #5's window, counted from the first
        # failure, ran out of; E2, G1 and L1 are shorter than that from 2.0.2.
        apart = {step: 0.8 * fs.ROUTES[route][2][step] for route, step in fs.BLIP_STEPS}
        self.assertGreaterEqual(sum(gap > 20 for gap in apart.values()), 2, apart)

    def test_how_busy_each_phase_kept_the_plug(self):
        import json
        import os
        import time
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            for name, captures, age in (("new", {"E2.plug_busy_seconds": 31.2, "G1.plug_busy_seconds": 12,
                                                 "E2.seconds": 40}, 0),
                                        ("again", {"E2.plug_busy_seconds": 44.0}, 0),
                                        ("old", {"E2.plug_busy_seconds": 99.0}, 3600)):
                run = state / "work/captures/plugs/12-34-56-ab-cd-ef/runs" / name
                run.mkdir(parents=True)
                path = run / "autoflash-state.json"
                path.write_text(json.dumps({"captures": captures}))
                os.utime(path, (time.time() - age, time.time() - age))
            self.assertEqual(fs.plug_busy(state, time.time() - 60), {"E2": 44.0, "G1": 12})

    def test_an_esphome_factory_image_as_its_builder_makes_one(self):
        image = fs.esphome_factory_image()
        self.assertEqual(image[0], 0xE9)  # all Tasmota's page checks of it, with the size
        self.assertEqual(image[0x8000:0x8002], b"\xaa\x50")  # a partition table
        self.assertEqual(image[0x20000], 0xE9)  # the app, in the first slot
        self.assertIn("tasmota>esphome:start:lostapp-factory", {sc["id"] for sc in fs.scenarios()})

    def test_a_legacy_run_goes_without_firmware_and_the_next_with_it(self):
        with tempfile.TemporaryDirectory() as tree:
            (Path(tree) / "tools").mkdir()
            (Path(tree) / fs.LEGACY_TOOL).touch()
            sc = fs.Scenario(24, {"id": "x", "route": "sonoff>tasmota", "step": "D4", "fault": "legacy",
                                  "arg": "v1.2.0", "at": 40}, "/dev/null")
            with mock.patch.object(fs.subprocess, "call", return_value=0) as call:
                sc.run_tool("tasmota", code=tree)
                sc.run_tool("tasmota", code=fs.REPO)
            old, new = (c.args[0] for c in call.call_args_list)
            self.assertIn(f"--code={tree}", old)
            self.assertNotIn("--firmware", old)
            self.assertEqual(new[-2:], ["--firmware", "tasmota"])

    def test_the_cut_in_the_g1_after_a_legacy_run_lands_in_this_checkouts_g1(self):
        # Timed by 1.2.0's G1 (33 s), it came after this one's (21 s from
        # 2.0.2) had finished: MISTIMED.
        sc = fs.Scenario(24, {"id": "x", "route": "sonoff>tasmota", "step": "F4", "fault": "legacy",
                              "arg": "v1.2.0+G1off", "at": 40}, "/dev/null")
        sc.fired = "legacy"
        with mock.patch.object(fs, "legacy_tree"), mock.patch.object(sc, "run_tool"), \
             mock.patch.object(sc, "screen_result", return_value="Stopped"), \
             mock.patch.object(fs.threading, "Thread"), mock.patch.object(sc, "again") as again:
            sc.go_legacy()
        self.assertEqual(again.call_args.kwargs["later"],
                         ("G1", "plugoff", 1, 0.6 * fs.ROUTES["sonoff>tasmota"][2]["G1"]))

    def test_an_earlier_releases_own_error_is_not_this_checkouts(self):
        # 1.2.0, killed as G1 began, had stopped on a TimeoutError of its own:
        # this checkout's run carried on in the same run log and converted.
        sc = fs.Scenario(24, {"id": "x", "route": "sonoff>tasmota", "step": "G1", "fault": "legacy",
                              "arg": "v1.2.0", "at": 40}, "/dev/null")
        old = f"=== run started, tool version 1.2.0 ===\nFAILED: {fs.UNEXPECTED}: TimeoutError\n"
        log = Path("run.log")
        sc.earlier_release_wrote = {log: len(old)}
        self.assertNotIn(fs.UNEXPECTED, sc.ours(log, old + "=== run started ===\nConverted\n"))
        self.assertIn(fs.UNEXPECTED, sc.ours(log, old + f"FAILED: {fs.UNEXPECTED}: KeyError\n"))
        self.assertIn(fs.UNEXPECTED, sc.ours(Path("another/run.log"), old))

    def test_a_step_with_a_start_of_its_own_needs_no_length(self):
        # F4rs is in STARTS only: its length comes from there.
        sc = fs.Scenario(24, {"id": "x", "route": "sonoff>tasmota", "step": "F4rs", "fault": "plugoff",
                              "arg": 1, "at": 50}, "/dev/null")
        with mock.patch.object(sc, "run_tool", side_effect=RuntimeError("stop here")), \
             mock.patch.object(fs.threading, "Thread"):
            with self.assertRaisesRegex(RuntimeError, "stop here"):
                sc.go_watched()
        self.assertEqual(sc.step_length, fs.STARTS["F4rs"][1])

    def test_every_step_has_a_length_or_a_start_of_its_own(self):
        for sc in fs.scenarios():
            if sc["fault"] == "legacy":
                steps = fs.LEGACY_STEPS[sc["arg"].partition("+")[0]].get(sc["route"], {})
                self.assertTrue(sc["step"] == "start" or sc["step"] in steps, sc["id"])
                continue
            if sc["fault"] in ("lostapp", "notemplate", "bootloop", "safemode"):
                self.assertEqual(sc["step"], "start", sc["id"])
                continue
            if sc["step"] == "C2":
                continue
            steps = fs.ROUTES[sc["route"]][2]
            self.assertTrue(sc["step"] in steps or sc["step"] in fs.STARTS, sc["id"])


if __name__ == "__main__":
    unittest.main()
