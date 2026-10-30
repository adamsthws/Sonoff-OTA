"""Which family's steps a run takes: its kind's."""
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.autoflash import kinds, routes
from tools.autoflash.routes import espressif
from tools.autoflash.state import RunState


def a_kind(key, family):
    """A made-up kind of plug of `family`, for a run to record."""
    return kinds.Kind(key=key, family=family, chip="made-up", ecosystem="ewelink", name="Made Up",
                      maker="Nobody", short="MU", product=re.compile("MU"), regions=(),
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
                                 POINT_OF_NO_RETURN="Z1")
        other = a_kind("made-up", "madeup")
        with mock.patch.object(kinds, "ALL", (kinds.SONOFF_S60, other)), \
                mock.patch.dict(routes.FAMILIES, {"madeup": family}):
            state = self.state(plug_kind="made-up", firmware="esphome", source="sonoff")
            self.assertIs(routes.family_of(state), family)
            self.assertEqual([s for s, _, _ in routes.steps_of(state)], ["Z1"])
            self.assertFalse(routes.past_the_point_of_no_return(state))
            state.mark("Z1", "done")
            self.assertTrue(routes.past_the_point_of_no_return(state))
            routes.apply_target("esphome", "sonoff", family)
            self.assertIs(routes.FAMILY, family)
            self.assertEqual(routes.stage_name("Z1"), "Doing its own thing")
        # An S60's run, and one from before kinds were recorded, take the Espressif steps.
        for state in (self.state(plug_kind="sonoff-s60"), self.state()):
            self.assertIs(routes.family_of(state), espressif)
            self.assertEqual(routes.steps_of(state), espressif.steps_for("tasmota", "sonoff"))

    def test_every_family_gives_the_runner_the_same_names(self):
        for name, family in routes.FAMILIES.items():
            with self.subTest(family=name):
                for attribute in ("FIRMWARES", "SOURCES", "steps_for", "POINT_OF_NO_RETURN",
                                  "DESTRUCTIVE_STEPS", "RETRY_STEPS", "ERASED_CHECK_STEPS", "STAGE_NAMES",
                                  "TARGET_STAGE_NAMES", "PLAIN_DESCRIPTION", "ATTEMPTED_EVIDENCE",
                                  "STEP_VERIFIERS"):
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
        family = SimpleNamespace(FIRMWARES={"openbeken": "OpenBeken", "sonoff": "Sonoff"})
        state = self.state(plug_kind="made-up")
        ctx = SimpleNamespace(state=state, firmware_given="tasmota", esphome_firmware=None)
        with mock.patch.object(kinds, "ALL", (kinds.SONOFF_S60, other)), \
                mock.patch.dict(routes.FAMILIES, {"madeup": family}), \
                mock.patch.object(routes.source, "find_current_firmware", return_value="sonoff"):
            with self.assertRaisesRegex(routes.Abort, "takes OpenBeken or Sonoff"):
                routes.settle_route(ctx)


class KindTests(unittest.TestCase):
    def test_every_kind_names_a_family_the_routes_know(self):
        for kind in kinds.ALL:
            self.assertIn(kind.family, routes.FAMILIES, kind.key)
            self.assertEqual(kind.ecosystem, "ewelink")
