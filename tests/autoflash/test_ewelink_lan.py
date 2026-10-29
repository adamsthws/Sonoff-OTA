import base64
import unittest

from tools.autoflash import ewelink_lan

# An advert encrypted as a stock S60 encrypts its own, under a made-up key.
KEY = "00000000-1111-2222-3333-444444444444"
ADVERT = {
    "id": "1000abcdef", "encrypt": "true", "iv": "EBESExQVFhcYGRobHB0eHw==",
    "data1": "huKLUV5mXw+AqhcspGTc2CJXcZXxGdVjTRuzbySprRzOSOiwyrRhKbWznoMlzPr7K7blPSFkD3AefRlZ",
    "data2": "9l79HkmJ9F7U5HLzMwDS4MGbTyjl5AuyagTI/TgYJzjg/fL2L9hPuC06KSo6vRBQ3kqr8eyXHaFG/Q/t8ujPCMP8QEWkzqT/ebM2Hjh9TwJ44ueB",
}


class AesTests(unittest.TestCase):
    """The plain-Python AES the tool opens the advert with."""

    def test_the_fips_197_example_decrypts(self):
        keys = ewelink_lan._round_keys(bytes(range(16)))
        block = ewelink_lan._decrypt_block(bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a"), keys)
        self.assertEqual(block, bytes.fromhex("00112233445566778899aabbccddeeff"))

    def test_data_that_is_not_aes_is_refused(self):
        with self.assertRaises(ValueError):
            ewelink_lan.decrypt_cbc(bytes(16), bytes(16), b"short")


class AdvertTests(unittest.TestCase):
    """Sonoff's firmware gives its Wi-Fi signal in its encrypted advert."""

    def test_the_advert_opens_with_the_plugs_key(self):
        state = ewelink_lan.advert_state(ADVERT, KEY)
        self.assertEqual(state["fwVersion"], "1.2.0")
        self.assertEqual(ewelink_lan.signal(state), -61)

    def test_another_key_opens_nothing(self):
        self.assertIsNone(ewelink_lan.advert_state(ADVERT, KEY[:-1] + "5"))

    def test_an_advert_sent_in_the_clear_is_read_as_it_is(self):
        data = base64.b64encode(b'{"rssi": -70}').decode()
        self.assertEqual(ewelink_lan.signal(ewelink_lan.advert_state({"data1": data}, KEY)), -70)

    def test_the_advert_is_found_by_its_device_id(self):
        report = {"packets": [{"source": "192.168.0.7", "records": [
            {"type": "TXT", "value": ["id=1000ffffff", "data1=x"]}]},
            {"source": "192.168.0.50", "records": [
                {"type": "A", "value": "192.168.0.50"},
                {"type": "TXT", "value": [f"{k}={v}" for k, v in ADVERT.items()]}]}]}
        self.assertEqual(ewelink_lan.advert_fields(report, "1000abcdef"), ADVERT)
        self.assertIsNone(ewelink_lan.advert_fields(report, "1000000000"))

    def test_only_a_number_is_a_signal(self):
        for state in (None, {}, {"rssi": "-50"}, {"rssi": True}):
            self.assertIsNone(ewelink_lan.signal(state), state)


if __name__ == "__main__":
    unittest.main()
