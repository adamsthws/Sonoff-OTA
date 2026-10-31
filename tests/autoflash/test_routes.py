"""Which family's steps a run takes: its kind's."""
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.autoflash import kinds, routes
from tools.autoflash.routes import beken, espressif
from tools.autoflash.state import RunState


def a_kind(key, family):
    """A made-up kind of plug of `family`, for a run to record."""
    return kinds.Kind(key=key, family=family, chip="made-up", ecosystem="ewelink", name="Made Up",
                      maker="Nobody", short="MU", product=re.compile("MU"),
                      stock_model="MU-01", wrapper_model="MU", tasmota_gpio=(), esphome_name="Made Up",
                      esphome_legacy_setup_prefix="", esphome_legacy_prefix="")


class FamilyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # The route tables apply_target leaves behind are the test's to put back.
        self.addCleanup(routes.apply_target, "tasmota", "sonoff")

    def state(self, **inputs):
        self.runs = getattr(self, "runs", 0) + 1
        state = RunState.open(Path(self.tmp.name) / f"run{self.runs}")
        for key, value in inputs.items():
            state.set_input(key, value)
        return state

    def test_a_run_takes_its_kinds_familys_steps(self):
        # A made-up family: its own steps, its own names for them.
        step = ("Z1", "Something of its own", lambda ctx: {})
        family = SimpleNamespace(FIRMWARES={"esphome": "ESPHome"}, SOURCES=("sonoff",),
                                 steps_for=lambda firmware, source="sonoff": [step],
                                 STAGE_NAMES={"Z1": "Doing its own thing"}, TARGET_STAGE_NAMES={},
                                 POINT_OF_NO_RETURN="Z1", RESUME_CHECK=None)
        other = a_kind("made-up", "madeup")
        with mock.patch.object(kinds, "ALL", (kinds.SONOFF_S60, other)), \
                mock.patch.dict(routes.FAMILIES, {"madeup": family}):
            state = self.state(plug_kind="made-up", firmware="esphome", source="sonoff")
            self.assertIs(routes.family_of(state), family)
            self.assertEqual([s for s, _, _ in routes.steps_of(state)], ["A0", "A1", "A3", "Z1"])
            self.assertFalse(routes.past_the_point_of_no_return(state))
            state.mark("Z1", "done")
            self.assertTrue(routes.past_the_point_of_no_return(state))
            routes.apply_target("esphome", "sonoff", family)
            self.assertIs(routes.FAMILY, family)
            self.assertEqual(routes.stage_name("Z1"), "Doing its own thing")
        # An S60's run, and one from before kinds were recorded, take the Espressif steps:
        # the shared start until the firmware is chosen, then the route.
        for state in (self.state(plug_kind="sonoff-s60"), self.state()):
            self.assertIs(routes.family_of(state), espressif)
            self.assertEqual([s for s, _, _ in routes.steps_of(state)], ["A0", "A1", "A3"])
            state.set_input("firmware", "tasmota")
            self.assertEqual(routes.steps_of(state), routes.steps_for("tasmota", "sonoff", espressif))
        # A run of 1.x recorded its steps and no choice: it was for Tasmota.
        legacy = self.state()
        legacy.mark("C2", "done")
        self.assertEqual([s for s, _, _ in routes.steps_of(legacy)][-1], "H1")

    def test_a_run_from_sonoff_lays_its_stages_out_the_same_before_and_after_its_route_is_known(self):
        # The kind of plug, and with it the rest of the route, is known only
        # after A1 and A3: the same columns before and after, and no total.
        begun, layouts = [], []
        with mock.patch.object(routes.output, "stage_begin", side_effect=lambda *a: begun.append(a)), \
                mock.patch.object(routes.output, "set_stage_width", side_effect=lambda *a: layouts.append(a)):
            routes.apply_target(None, "sonoff", beken)
            routes.begin_stage("A3")
            routes.apply_target("openbeken", "sonoff", beken)
            routes.begin_stage("B1")
            routes.apply_target("esphome", "sonoff", espressif)
            routes.begin_stage("A3")
        self.assertEqual(begun, [(3, "Choosing the firmware"), (4, "Querying Sonoff firmware"),
                                 (3, "Choosing the firmware")])
        self.assertEqual(len(set(layouts)), 1)
        longest = max(len(routes.steps_for(firmware, "sonoff", family)) for family in (espressif, beken)
                      for firmware in family.FIRMWARES if firmware != "sonoff")
        self.assertEqual(layouts[0][1], longest)

    def test_before_you_start_lists_what_is_needed_for_any_device(self):
        for source, needed in (("sonoff", "- LAN Control turned on in the device's settings in the app."),
                               ("tasmota", "- Your device connected to Wi-Fi, as it is now.")):
            shown = []
            ctx = SimpleNamespace(state=self.state(source=source))
            with mock.patch.object(routes.prompts, "section", side_effect=lambda title, body: shown.append((title, body))), \
                    mock.patch.object(routes.prompts, "wait_for_key"), \
                    mock.patch.object(routes.output, "transient_begin"), mock.patch.object(routes.output, "transient_end"):
                routes.step_prerequisites(ctx)
            title, body = shown[0]
            self.assertEqual(title, "Before you start, you'll need:")
            self.assertIn(needed, body)
            self.assertEqual(body[-1], "- This computer on the same network.")
            self.assertFalse(any("plug" in line for line in body))

    def test_every_family_gives_the_runner_the_same_names(self):
        for name, family in routes.FAMILIES.items():
            with self.subTest(family=name):
                for attribute in ("FIRMWARES", "SOURCES", "steps_for", "POINT_OF_NO_RETURN",
                                  "DESTRUCTIVE_STEPS", "RETRY_STEPS", "ERASED_CHECK_STEPS", "STAGE_NAMES",
                                  "TARGET_STAGE_NAMES", "PLAIN_DESCRIPTION", "ATTEMPTED_EVIDENCE",
                                  "STEP_VERIFIERS", "REDO_STEPS", "SONOFF_FROM_BACKUP", "RESUME_CHECK"):
                    self.assertTrue(hasattr(family, attribute), attribute)
                # Every step of every route has a name for its stage line and a plain description.
                steps = {step for firmware in family.FIRMWARES for source in family.SOURCES
                         for step, _, _ in family.steps_for(firmware, source)}
                self.assertEqual(steps - set(family.STAGE_NAMES), set())
                self.assertEqual(steps - set(family.PLAIN_DESCRIPTION), set())
                self.assertIn(family.POINT_OF_NO_RETURN, family.DESTRUCTIVE_STEPS)

    def test_the_command_line_offers_every_familys_firmware(self):
        for family in routes.FAMILIES.values():
            self.assertLessEqual(set(family.FIRMWARES), set(routes.FIRMWARES))

    def test_a_firmware_another_family_takes_is_refused_for_this_plug(self):
        other = a_kind("made-up", "madeup")
        family = SimpleNamespace(FIRMWARES={"openbeken": "OpenBeken", "sonoff": "Sonoff"}, SONOFF_FROM_BACKUP=True)
        state = self.state(plug_kind="made-up")
        ctx = SimpleNamespace(state=state, firmware_given="tasmota", esphome_firmware=None)
        with mock.patch.object(kinds, "ALL", (kinds.SONOFF_S60, other)), \
                mock.patch.dict(routes.FAMILIES, {"madeup": family}), \
                mock.patch.object(routes.source, "find_current_firmware", return_value="sonoff"):
            # From Sonoff's firmware the kind is known only at A1; A3 refuses.
            self.assertEqual(routes.settle_route(ctx), (None, "sonoff"))
            with self.assertRaisesRegex(routes.Abort, "a Made Up takes OpenBeken"):
                routes.step_choose(ctx)
            # A converted plug's kind is known when the route is settled.
            family.FIRMWARES["esphome"] = "ESPHome"
            converted = SimpleNamespace(state=self.state(plug_kind="made-up"), firmware_given="tasmota",
                                        esphome_firmware=None)
            with mock.patch.object(routes.source, "find_current_firmware", return_value="esphome"):
                with self.assertRaisesRegex(routes.Abort, "takes OpenBeken or Sonoff or ESPHome"):
                    routes.settle_route(converted)


class BekenTargetTests(unittest.TestCase):
    """An S61 goes back to Sonoff's firmware from the public dump, not a
    backup; and a run of one that stopped before its firmware was chosen is
    resumed as that, not as an S60's of 1.x (both seen in the fault suite)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def state(self, **inputs):
        state = RunState.open(Path(self.tmp.name) / "run")
        for key, value in inputs.items():
            state.set_input(key, value)
        return state

    def test_an_s61_on_esphome_can_go_back_to_sonoff_without_a_backup(self):
        ctx = SimpleNamespace(state=self.state(plug_kind="sonoff-s61", plug_mac="12:34:56:ab:cd:ef"),
                              firmware_given="sonoff", esphome_firmware=None)
        with mock.patch.object(routes.source, "find_current_firmware", return_value="esphome"), \
                mock.patch.object(routes.plugs, "find_backup", return_value=None), \
                mock.patch.object(routes.output, "say"):
            self.assertEqual(routes.settle_route(ctx), ("sonoff", "esphome"))

    def test_an_s60_still_needs_its_backup(self):
        ctx = SimpleNamespace(state=self.state(plug_kind="sonoff-s60", plug_mac="12:34:56:ab:cd:ef"),
                              firmware_given="sonoff", esphome_firmware=None)
        with mock.patch.object(routes.source, "find_current_firmware", return_value="esphome"), \
                mock.patch.object(routes.plugs, "find_backup", return_value=None):
            with self.assertRaisesRegex(routes.Abort, "no backup"):
                routes.settle_route(ctx)

    def test_an_s61_run_with_no_firmware_chosen_has_the_start_alone(self):
        state = self.state(plug_kind="sonoff-s61", source="esphome")
        self.assertIsNone(routes.recorded_firmware(state))
        self.assertEqual(routes.steps_of(state), [])
        self.assertEqual(routes.recorded_firmware(self.state(plug_kind="sonoff-s60", source="esphome")), "tasmota")


class KindTests(unittest.TestCase):
    def test_every_kind_names_a_family_the_routes_know(self):
        for kind in kinds.ALL:
            self.assertIn(kind.family, routes.FAMILIES, kind.key)
            self.assertEqual(kind.ecosystem, "ewelink")
