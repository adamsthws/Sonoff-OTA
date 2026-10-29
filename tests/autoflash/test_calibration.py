"""The meter calibrated as Sonoff's firmware calibrates it, on Tasmota and
ESPHome (tools/autoflash/calibration.py)."""

import unittest
from unittest import mock

from tools.autoflash import calibration

# Made up, in calib_coefficient0's form: set 1 scaling current, voltage and
# power by 1.01, 0.99 and 1.02; set 2 the identity.
FACTORY = "1,1,[0,1010000,0,0,990000,0,0,1020000,0],[0,1000000,0,0,1000000,0,0,1000000,0]"
# A chip's own numbers, as Tasmota takes them from it.
CHIP = {"VoltageCal": 1848, "CurrentCal": 15435, "PowerCal": 5047}


class FakeTasmota:
    """Tasmota's console, as far as calibrating its meter goes: each command
    comes with a reading from the meter (if it reports), at which a setting
    holding Tasmota's default is taken from the chip."""

    def __init__(self, pins=(1, 0, 1), chip=CHIP, meter=True, berry=True):
        self.pins = dict(zip(calibration.BOARD_PINS, pins))
        self.chip, self.meter, self.berry = chip, meter, berry
        self.settings = dict(chip)
        self.sent = []

    def __call__(self, text):
        self.sent.append(text)
        if self.meter:
            for name, default in calibration.TASMOTA_DEFAULTS.items():
                if self.settings[name] == default:
                    self.settings[name] = self.chip[name]
        if text.startswith("Br "):
            if not self.berry:
                return {"Command": "Unknown"}
            if text.startswith("Br gpio.digital_read("):
                return {"Br": str(self.pins[int(text[len("Br gpio.digital_read("):-1])])}
            return {"Br": "nil"}
        name, _, value = text.partition(" ")
        if name in self.settings:
            if value:
                self.settings[name] = int(value)
            return {name: self.settings[name]}
        return None


class ParseTests(unittest.TestCase):
    def test_the_two_sets_after_the_two_ones(self):
        numbers = calibration.parse(FACTORY)
        self.assertEqual(len(numbers), 18)
        self.assertEqual(numbers[:3], [0.0, 1010000.0, 0.0])

    def test_negative_and_fractional_terms_are_read(self):
        numbers = calibration.parse("1,1,[0,1e6,-3320904,0,1e6,354937521,-0.016045,1014763,-715345048],"
                                    "[0,1e6,0,0,1e6,0,0,1e6,0]")
        self.assertEqual((numbers[2], numbers[5], numbers[6]), (-3320904.0, 354937521.0, -0.016045))

    def test_anything_else_is_refused(self):
        self.assertIsNone(calibration.parse("1,1,[0,1000000,0]"))
        self.assertIsNone(calibration.parse(""))
        # A b term that would not scale a reading: a damaged entry.
        self.assertIsNone(calibration.parse(FACTORY.replace("990000", "0", 1)))


class TasmotaValueTests(unittest.TestCase):
    def test_the_boards_factor_scales_voltage_and_power(self):
        self.assertEqual(calibration.tasmota_values(CHIP, 1.02),
                         {"VoltageCal": 1885, "CurrentCal": 15435, "PowerCal": 5148})
        self.assertEqual(calibration.tasmota_values(CHIP, 1.0), CHIP)

    def test_the_factory_calibration_scales_each_by_its_set_1_b(self):
        self.assertEqual(calibration.tasmota_values(CHIP, 1.02, calibration.parse(FACTORY)),
                         {"VoltageCal": 1866, "CurrentCal": 15589, "PowerCal": 5251})

    def test_a_default_is_never_set(self):
        # 1912 * 1.02 rounds to 1950, Tasmota's default: it would take the chip's again.
        values = calibration.tasmota_values({**CHIP, "VoltageCal": 1912}, 1.02)
        self.assertEqual(values["VoltageCal"], 1951)

    def test_the_board_factor_is_1_02_unless_every_pin_reads_high(self):
        self.assertEqual(calibration.board_factor([1, 1, 1]), 1.0)
        for levels in ([0, 1, 1], [1, 0, 1], [1, 1, 0]):
            self.assertEqual(calibration.board_factor(levels), 1.02, levels)


class CalibrateTasmotaTests(unittest.TestCase):
    def test_the_chips_numbers_are_scaled_and_kept(self):
        tasmota = FakeTasmota()
        outcome = calibration.calibrate_tasmota(tasmota, sleep=lambda _: None)
        self.assertEqual(tasmota.settings, {"VoltageCal": 1885, "CurrentCal": 15435, "PowerCal": 5148})
        self.assertEqual((outcome["factor"], outcome["factory"], outcome["chip"]), (1.02, False, CHIP))

    def test_done_again_it_comes_out_the_same(self):
        # The chip's own numbers taken afresh: the factor is never applied twice.
        tasmota = FakeTasmota()
        calibration.calibrate_tasmota(tasmota, calibration.parse(FACTORY), sleep=lambda _: None)
        first = dict(tasmota.settings)
        calibration.calibrate_tasmota(tasmota, calibration.parse(FACTORY), sleep=lambda _: None)
        self.assertEqual(tasmota.settings, first)

    def test_the_pins_are_read_with_pull_ups_and_left_without(self):
        tasmota = FakeTasmota(pins=(1, 1, 1))
        outcome = calibration.calibrate_tasmota(tasmota, sleep=lambda _: None)
        self.assertEqual(outcome["factor"], 1.0)
        for pin in calibration.BOARD_PINS:
            pulled = tasmota.sent.index(f"Br gpio.pin_mode({pin}, gpio.INPUT_PULLUP)")
            read = tasmota.sent.index(f"Br gpio.digital_read({pin})")
            released = tasmota.sent.index(f"Br gpio.pin_mode({pin}, gpio.INPUT)")
            self.assertLess(pulled, read)
            self.assertLess(read, released)

    def test_a_meter_that_does_not_report_is_left_as_it_was(self):
        # The emulator's plug has no meter: the chip's numbers never come.
        tasmota = FakeTasmota(meter=False, chip={"VoltageCal": 1900, "CurrentCal": 15000, "PowerCal": 5000})
        before = dict(tasmota.settings)
        outcome = calibration.calibrate_tasmota(tasmota, wait=0, sleep=lambda _: None)
        self.assertEqual(outcome["skipped"], "the meter did not report")
        self.assertEqual(tasmota.settings, before)

    def test_one_calibrated_by_hand_is_left_as_it_was(self):
        # PowerSet against a meter, say: neither the chip's numbers nor ours.
        tasmota = FakeTasmota()
        tasmota.settings = {"VoltageCal": 1870, "CurrentCal": 15500, "PowerCal": 5100}
        outcome = calibration.calibrate_tasmota(tasmota, sleep=lambda _: None)
        self.assertEqual(outcome["kept"], {"VoltageCal": 1870, "CurrentCal": 15500, "PowerCal": 5100})
        self.assertIn("calibrated another way", outcome["skipped"])
        self.assertEqual(tasmota.settings, outcome["kept"])

    def test_it_says_whether_it_changed_anything(self):
        tasmota = FakeTasmota()
        self.assertTrue(calibration.calibrate_tasmota(tasmota, sleep=lambda _: None)["changed"])
        self.assertFalse(calibration.calibrate_tasmota(tasmota, sleep=lambda _: None)["changed"])

    def test_a_tasmota_without_berry_is_not_touched(self):
        tasmota = FakeTasmota(berry=False)
        outcome = calibration.calibrate_tasmota(tasmota, sleep=lambda _: None)
        self.assertEqual(outcome, {"skipped": "the plug's board could not be read"})
        self.assertFalse([text for text in tasmota.sent if not text.startswith("Br ")])


class BackupCalibrationTests(unittest.TestCase):
    """The factory calibration, as a plug's Sonoff backup holds it."""

    def read(self, content):
        import tools.autoflash.cli as autoflash
        from tools.espressif.inspect_stock_nvs import Value
        value = Value(namespace=5, key="content", kind=0x42, chunks={0: content})
        with mock.patch.object(autoflash.inspect_stock_nvs, "parse",
                               return_value=({5: "user_unusual"}, {(5, "content"): value})):
            return autoflash.sonoff_backup_calibration(b"")

    def test_it_is_calib_coefficient0_in_user_unusual(self):
        content = ('{"volChgFlg":false,"calib_coefficient0":"' + FACTORY + '"}').encode() + b"\x00\xff"
        self.assertEqual(self.read(content), calibration.parse(FACTORY))

    def test_a_backup_without_it_gives_nothing(self):
        self.assertIsNone(self.read(b'{"volChgFlg":false}\x00'))


class StepTests(unittest.TestCase):
    """What F4 and I2 do with it: never a failed step."""

    def ctx(self, command=None):
        from types import SimpleNamespace
        inputs = {"plug_ip": "192.168.1.51", "plug_mac": "12:34:56:ab:cd:ef"}
        return SimpleNamespace(device=SimpleNamespace(command=command), esphome_key=None,
                               state=SimpleNamespace(input=inputs.get))

    def test_tasmota_is_calibrated_at_f4_with_the_backups_numbers(self):
        import tools.autoflash.cli as autoflash
        tasmota = FakeTasmota()
        with mock.patch.object(autoflash, "backup_calibration", return_value=calibration.parse(FACTORY)), \
             mock.patch.object(autoflash, "say") as said, mock.patch.object(calibration.time, "sleep"):
            outcome = autoflash.calibrate_tasmota_meter(self.ctx(lambda text, timeout=None: tasmota(text)))
        self.assertEqual(tasmota.settings, {"VoltageCal": 1866, "CurrentCal": 15589, "PowerCal": 5251})
        self.assertTrue(outcome["factory"])
        self.assertIn("factory calibration from its backup", said.call_args.args[0])

    def test_a_plug_that_does_not_answer_is_left_as_tasmota_calibrates_it(self):
        import tools.autoflash.cli as autoflash

        def command(text, timeout=None):
            raise autoflash.DeviceError("no answer")
        with mock.patch.object(autoflash, "backup_calibration", return_value=None), \
             mock.patch.object(autoflash, "tell") as told:
            outcome = autoflash.calibrate_tasmota_meter(self.ctx(command))
        self.assertEqual(outcome, {"skipped": "no answer"})
        self.assertIn("left as it is", told.call_args.args[0])

    def test_a_rerun_on_tasmota_checks_its_meter(self):
        import tools.autoflash.cli as autoflash
        with mock.patch.object(autoflash, "unlock_tasmota") as unlocked, \
             mock.patch.object(autoflash, "calibrate_tasmota_meter") as calibrated, \
             mock.patch.object(autoflash, "say"):
            self.assertEqual(autoflash.rerun_on_tasmota(self.ctx()), 0)
        unlocked.assert_called_once()
        calibrated.assert_called_once()

    def test_a_rerun_needing_a_password_not_given_stops_untouched(self):
        import tools.autoflash.cli as autoflash
        with mock.patch.object(autoflash, "unlock_tasmota", side_effect=autoflash.Abort("no password")), \
             mock.patch.object(autoflash, "calibrate_tasmota_meter") as calibrated, \
             mock.patch.object(autoflash, "tell"):
            self.assertEqual(autoflash.rerun_on_tasmota(self.ctx()), 2)
        calibrated.assert_not_called()

    def test_esphome_is_given_the_backups_numbers(self):
        import tools.autoflash.cli as autoflash
        numbers = calibration.parse(FACTORY)
        with mock.patch.object(autoflash, "backup_calibration", return_value=numbers), \
             mock.patch.object(autoflash.esphome, "api_give_calibration", return_value=True) as give, \
             mock.patch.object(autoflash.esphome, "calibration_state", return_value="from backup, factor 1.02"), \
             mock.patch.object(autoflash, "say") as said:
            outcome = autoflash.calibrate_esphome_meter(self.ctx())
        give.assert_called_once_with("192.168.1.51", numbers, key=None)
        self.assertEqual(outcome, {"calibration": "from backup, factor 1.02"})
        self.assertIn("from its Sonoff backup", said.call_args.args[0])

    def test_without_a_backup_esphome_is_not_asked(self):
        import tools.autoflash.cli as autoflash
        with mock.patch.object(autoflash, "backup_calibration", return_value=None), \
             mock.patch.object(autoflash.esphome, "api_give_calibration") as give:
            outcome = autoflash.calibrate_esphome_meter(self.ctx())
        give.assert_not_called()
        self.assertIn("skipped", outcome)

    def test_an_esphome_without_the_action_is_told_of_not_failed(self):
        import tools.autoflash.cli as autoflash
        with mock.patch.object(autoflash, "backup_calibration", return_value=calibration.parse(FACTORY)), \
             mock.patch.object(autoflash.esphome, "api_give_calibration", return_value=False), \
             mock.patch.object(autoflash, "tell"):
            outcome = autoflash.calibrate_esphome_meter(self.ctx())
        self.assertEqual(outcome, {"skipped": "ESPHome did not take it"})


if __name__ == "__main__":
    unittest.main()
