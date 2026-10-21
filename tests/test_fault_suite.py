"""The fault suite's own logic: the names a plug may go by, the saved plugs
each plug has, and the scenarios it lists."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools/emulator"))
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
                self.assertTrue(sc["step"] == "start" or sc["step"] in fs.LEGACY_STEPS[sc["arg"].partition("+")[0]], sc["id"])
                continue
            if sc["fault"] == "lostapp":
                self.assertEqual(sc["step"], "start", sc["id"])
                continue
            if sc["step"] == "C2":
                continue
            steps = fs.ROUTES[sc["route"]][2]
            self.assertTrue(sc["step"] in steps or sc["step"] in fs.STARTS, sc["id"])


if __name__ == "__main__":
    unittest.main()
