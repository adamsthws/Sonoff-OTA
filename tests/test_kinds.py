"""The kinds of plug the tool converts, and what is each one's own."""
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
        self.assertIs(kinds.for_tasmota('Sonoff S60TPG {"NAME":"Sonoff S60TPG"}'), S60)
        self.assertIs(kinds.for_tasmota('Sonoff S60 {"NAME":"Sonoff S60"}'), S60)
        self.assertIsNone(kinds.for_tasmota('Generic {"NAME":"Generic"}'))

    def test_by_its_key(self):
        self.assertIs(kinds.by_key("sonoff-s60"), S60)
        self.assertIsNone(kinds.by_key("sonoff-minir4"))
        self.assertIsNone(kinds.by_key(None))


class NamesTests(unittest.TestCase):
    def test_tasmota_is_named_after_its_own_model_where_known(self):
        self.assertEqual(S60.template_name("S60TPG"), "Sonoff S60TPG")
        self.assertEqual(S60.template_name("s60tpf"), "Sonoff S60TPF")
        # Anything else is never passed on to a Tasmota command.
        for product in (None, "", "S60", "S60TPGX", 'S60TPG","GPIO":[0]'):
            self.assertEqual(S60.template_name(product), "Sonoff S60", product)

    def test_its_region_from_the_model_letter(self):
        self.assertEqual(S60.region("S60TPG"), "UK (Type G)")
        self.assertEqual(S60.region("S60TPF"), "Europe (Type E/F)")
        self.assertIsNone(S60.region("S60TPX"))
        self.assertIsNone(S60.region(None))

    def test_before_the_plug_is_known_every_kind_is_named(self):
        self.assertEqual(kinds.names(), "Sonoff S60")
        self.assertEqual(kinds.names("short"), "S60")
        self.assertEqual(kinds.stock_models(), "Sonoff S60: SN-ESP32C3-S60-01")
        self.assertEqual(kinds.name_words(), {"sonoff", "s60"})


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
