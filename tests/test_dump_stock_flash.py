import hashlib
import struct
import unittest
import zlib

from tools import dump_stock_flash

FLASH_SIZE = 0x400000
# The stock S60 layout (archive/docs/reference/s60-actual-partition-map.md).
LAYOUT = [
    ("nvs", 1, 0x02, 0x009000, 0x010000),
    ("reserve", 1, 0x02, 0x019000, 0x004000),
    ("otadata", 1, 0x00, 0x01D000, 0x002000),
    ("phy_init", 1, 0x01, 0x01F000, 0x001000),
    ("ota_0", 0, 0x10, 0x020000, 0x1F0000),
    ("ota_1", 0, 0x11, 0x210000, 0x1F0000),
]


def esp_image(version: str = "", project: str = "") -> bytes:
    """A minimal valid ESP32-C3 image, with an app description if named."""
    data = bytearray(256)
    if version:
        struct.pack_into("<I", data, 0, dump_stock_flash.APP_DESC_MAGIC)
        data[16 : 16 + len(version)] = version.encode()
        data[48 : 48 + len(project)] = project.encode()
        data[96:107] = b"Jan  1 2025"
    header = bytearray(24)
    header[0], header[1] = 0xE9, 1
    struct.pack_into("<H", header, 12, 5)  # ESP32-C3
    header[23] = 1  # SHA-256 appended
    body = bytes(header) + struct.pack("<II", 0x3C000020, len(data)) + bytes(data)
    checksum = 0xEF
    for value in data:
        checksum ^= value
    padded = body + bytes(((len(body) // 16) + 1) * 16 - 1 - len(body)) + bytes([checksum])
    return padded + hashlib.sha256(padded).digest()


def otadata_record(seq: int, state: int = 0xFFFFFFFF) -> bytes:
    crc = zlib.crc32(struct.pack("<I", seq), 0xFFFFFFFF)
    return struct.pack("<I", seq) + b"\xff" * 20 + struct.pack("<II", state, crc)


def flash(*, seqs=(5, 6), states=(0xFFFFFFFF, 0xFFFFFFFF), table_md5=True) -> bytearray:
    image = bytearray(b"\xff" * FLASH_SIZE)
    boot = esp_image()
    image[: len(boot)] = boot
    table = b"".join(
        struct.pack("<HBBII16sI", 0x50AA, ptype, subtype, offset, size, label.encode(), 0)
        for label, ptype, subtype, offset, size in LAYOUT
    )
    digest = hashlib.md5(table).digest() if table_md5 else bytes(16)
    table += struct.pack("<H", 0xEBEB) + b"\xff" * 14 + digest
    image[0x8000 : 0x8000 + len(table)] = table
    for base, seq, state in zip((0x1D000, 0x1E000), seqs, states):
        if seq is not None:
            image[base : base + 32] = otadata_record(seq, state)
    for offset, version in ((0x20000, "1.2.0"), (0x210000, "1.1.1")):
        app = esp_image(version, "esp32_fw")
        image[offset : offset + len(app)] = app
    return image


class DumpStockFlashTests(unittest.TestCase):
    def test_reports_both_stock_versions_and_the_booted_slot(self):
        report = dump_stock_flash.check(bytes(flash()))
        self.assertTrue(report["partition_table_md5_valid"])
        self.assertTrue(report["bootloader"]["checksum_valid"])
        slots = {p["label"]: p for p in report["partitions"]}
        self.assertEqual(slots["ota_0"]["app"]["version"], "1.2.0")
        self.assertEqual(slots["ota_1"]["app"]["version"], "1.1.1")
        self.assertTrue(slots["ota_0"]["image_valid"])
        # Sequence 6 is the newer record: (6 - 1) % 2 selects ota_1.
        self.assertEqual(report["otadata_selects"], "ota_1")

    def test_a_damaged_partition_table_is_reported(self):
        report = dump_stock_flash.check(bytes(flash(table_md5=False)))
        self.assertFalse(report["partition_table_md5_valid"])

    def test_an_erased_slot_is_not_a_valid_image(self):
        image = flash()
        image[0x210000:0x400000] = b"\xff" * 0x1F0000
        slots = {p["label"]: p for p in dump_stock_flash.check(bytes(image))["partitions"]}
        self.assertFalse(slots["ota_1"]["image_valid"])
        self.assertTrue(slots["ota_0"]["image_valid"])

    def test_otadata_ignores_bad_crc_and_rejected_states(self):
        image = flash(seqs=(7, 8), states=(0xFFFFFFFF, 3))  # 8 is ESP_OTA_IMG_INVALID
        self.assertEqual(dump_stock_flash.check(bytes(image))["otadata_selects"], "ota_0")
        image[0x1D000 + 28] ^= 0xFF  # break record 7's CRC too
        self.assertIsNone(dump_stock_flash.check(bytes(image))["otadata_selects"])

    def test_one_record_is_enough(self):
        self.assertEqual(dump_stock_flash.check(bytes(flash(seqs=(1, None))))["otadata_selects"], "ota_0")

    def test_real_otadata_crc(self):
        # A record read from a plug: sequence 20 with its stored CRC.
        record = struct.pack("<I", 20) + b"\xff" * 24 + struct.pack("<I", 0x20843F37)
        self.assertEqual(dump_stock_flash.selected_ota_slot(record + b"\xff" * 0x1FE0, 2), 1)


if __name__ == "__main__":
    unittest.main()


class PlugHashTests(unittest.TestCase):
    """A backup is checked byte for byte against the plug's own hash of its flash."""

    def read(self, on_plug):
        import hashlib
        import urllib.error
        from unittest import mock
        from tools import dump_stock_flash
        image = bytes(flash())
        answers = {
            "/flash": (image, {"X-Flash-Size": str(len(image))}),
            "/efuse": (b"E" * 16, {}),
        }

        def fetch(host, path, timeout=60.0):
            if path == "/flash-sha256":
                if on_plug is None:
                    raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
                return (on_plug(hashlib.sha256(image).hexdigest()) + "\n").encode(), {}
            return answers[path]

        with mock.patch.object(dump_stock_flash, "fetch", side_effect=fetch):
            return dump_stock_flash.read_dump("bridge")

    def test_a_backup_that_matches_the_plug_says_so(self):
        _, _, report = self.read(lambda digest: digest.upper())
        self.assertIs(report["matches_plug"], True)

    def test_a_backup_that_differs_from_the_plug_is_refused(self):
        from tools.dump_stock_flash import IncompleteDump
        with self.assertRaises(IncompleteDump):
            self.read(lambda digest: "0" * 64)

    def test_an_older_bridge_leaves_it_unchecked(self):
        _, _, report = self.read(None)
        self.assertIsNone(report["matches_plug"])
