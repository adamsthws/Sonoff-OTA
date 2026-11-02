"""The kinds of plug the tool converts, and what is each one's own."""
import json
import tempfile
import unittest
from pathlib import Path

from tools.autoflash import kinds
from tools.autoflash.state import RunState

S60 = kinds.SONOFF_S60


class KnownByTests(unittest.TestCase):
    def test_an_account_device_is_known_by_its_product_model_or_its_firmware(self):
        self.assertIs(kinds.for_account("S60TPG", ""), S60)
        self.assertIs(kinds.for_account(" s60tpf ", None), S60)
        self.assertIs(kinds.for_account("", "SN-ESP32C3-S60-01"), S60)
        self.assertIsNone(kinds.for_account("MINIR4", "SN-ESP32-MINIR4"))
        self.assertIsNone(kinds.for_account("S60ZBTPF", ""))  # the Zigbee S60

    def test_a_stock_firmware_by_what_it_calls_itself(self):
        self.assertIs(kinds.for_stock_model("SN-ESP32C3-S60-01"), S60)
        self.assertIsNone(kinds.for_stock_model("SN-ESP32-MINIR4"))
        self.assertIsNone(kinds.for_stock_model(None))

    def test_a_converted_tasmota_by_its_template(self):
        self.assertIs(kinds.for_tasmota("Sonoff-S60"), S60)
        self.assertIs(kinds.for_tasmota(" sonoff-s60 "), S60)
        # As earlier releases named it.
        self.assertIs(kinds.for_tasmota("Sonoff S60TPG"), S60)
        self.assertIs(kinds.for_tasmota("Sonoff S60"), S60)
        self.assertIsNone(kinds.for_tasmota("Generic"))
        self.assertIsNone(kinds.for_tasmota(None))

    def test_a_tasmota_template_only_naming_the_kind_in_passing_is_not_its(self):
        # A name holding S60 is not enough: another plug's, or the user's own.
        for name in ("Sonoff S600", "Sonoff S60ZBTPF", "Sonoff-S60 v2", "My S60 lamp", "Sonoff-S61"):
            with self.subTest(name=name):
                self.assertIsNone(kinds.for_tasmota(name))

    def test_by_its_key(self):
        self.assertIs(kinds.by_key("sonoff-s60"), S60)
        self.assertIsNone(kinds.by_key("sonoff-minir4"))
        self.assertIsNone(kinds.by_key(None))


class NamesTests(unittest.TestCase):
    def test_every_firmware_says_what_plug_it_is_alike(self):
        # One for every model of the kind: one image serves them all.
        self.assertEqual((S60.display_name, kinds.SONOFF_S61.display_name), ("Sonoff-S60", "Sonoff-S61"))
        self.assertEqual(json.loads(S60.tasmota_template())["NAME"], "Sonoff-S60")

    def test_an_esphome_is_known_by_its_comment_whatever_its_name(self):
        S61 = kinds.SONOFF_S61
        self.assertIs(kinds.for_esphome("esphome abcdef", "Sonoff-S61"), S61)
        self.assertIs(kinds.for_esphome("Kitchen", "Sonoff-S60"), S60)   # renamed
        self.assertIsNone(kinds.for_esphome("esphome abcdef", ""))
        self.assertIsNone(kinds.for_esphome(None, None))

    def test_an_s60_esphome_from_before_the_comment_is_known_by_its_friendly_name(self):
        # Released S60 images (1.4.0 and before) gave no comment.
        for title in ("Sonoff S60", "sonoff s60 abcdef"):
            self.assertIs(kinds.for_esphome(title, ""), S60, title)
        self.assertIsNone(kinds.for_esphome("Sonoff S61 abcdef", ""))   # never released so
        self.assertIsNone(kinds.for_esphome("Sonoff S60 kitchen", ""))

    def test_an_openbeken_is_asked_on_our_chips_alone(self):
        self.assertEqual(kinds.on_chip("BK7238"), (kinds.SONOFF_S61,))
        self.assertEqual(kinds.on_chip("BK7231N"), ())
        self.assertEqual(kinds.on_chip(None), ())

    def test_before_the_plug_is_known_every_kind_is_named(self):
        self.assertEqual(kinds.names(), "Sonoff S60 or Sonoff S61")
        self.assertEqual(kinds.names("short"), "S60 or S61")
        self.assertEqual(kinds.name_words(), {"sonoff", "s60", "s61"})


class RunTests(unittest.TestCase):
    def state(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return RunState.open(Path(directory.name))

    def test_a_run_converts_the_kind_it_recorded(self):
        state = self.state()
        state.set_input("plug_kind", "sonoff-s60")
        self.assertIs(kinds.of_run(state), S60)

    def test_one_that_recorded_none_the_first_the_tool_knows(self):
        # A run from before kinds were recorded: the S60's, the only kind then.
        self.assertIs(kinds.of_run(self.state()), kinds.ALL[0])


if __name__ == "__main__":
    unittest.main()
