import unittest

from tests.espressif.test_inspect_stock_nvs import entry, page, variable
from tools.espressif.emulator import redact


def flash(nvs: bytes) -> bytes:
    return (b"\xff" * redact.NVS_OFFSET + nvs).ljust(redact.NVS_OFFSET + redact.NVS_SIZE, b"\xff")


class RedactTests(unittest.TestCase):
    def test_masks_a_dumps_secret(self):
        nvs = page([entry(0, 0x01, "user_usual", data=b"\x01"), *variable(1, 0x21, "content", b'{"pw":"hunter22"}\0')])
        apply = redact.redactor(flash(nvs))
        self.assertEqual(apply(b"key hunter22\n"), b"key <redacted:user_usual.pw>\n")

    def test_flash_with_no_secrets_passes_lines_through(self):
        # A public plug's flash has no Sonoff NVS: the log is kept, not lost.
        apply = redact.redactor(b"\xff" * 0x400000)
        self.assertEqual(apply(b"boot line\n"), b"boot line\n")
        self.assertEqual(apply(b"0x3fc8a000: 0x00000000 0x12345678\n"), b"")


if __name__ == "__main__":
    unittest.main()
