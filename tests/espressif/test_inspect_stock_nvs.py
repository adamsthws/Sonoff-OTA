import struct
import unittest
import zlib

from tools.espressif import inspect_stock_nvs


def entry(ns: int, kind: int, key: str, span: int = 1, chunk: int = 0xFF, data: bytes = b"") -> bytes:
    body = struct.pack("<BBBB", ns, kind, span, chunk)
    tail = key.encode().ljust(16, b"\0") + data.ljust(8, b"\xff")
    crc = zlib.crc32(body + tail, 0xFFFFFFFF) & 0xFFFFFFFF
    return body + struct.pack("<I", crc) + tail


def variable(ns: int, kind: int, key: str, payload: bytes, chunk: int = 0xFF) -> list[bytes]:
    span = 1 + (len(payload) + 31) // 32
    meta = struct.pack("<HHI", len(payload), 0xFFFF, zlib.crc32(payload, 0xFFFFFFFF) & 0xFFFFFFFF)
    padded = payload.ljust((span - 1) * 32, b"\xff")
    return [entry(ns, kind, key, span, chunk, meta)] + [padded[i : i + 32] for i in range(0, len(padded), 32)]


def page(entries: list[bytes]) -> bytes:
    bitmap = bytearray(b"\xff" * 32)
    for index in range(len(entries)):
        bitmap[index // 4] &= ~(1 << ((index % 4) * 2)) & 0xFF  # 0b10: written
    body = b"".join(entries).ljust(126 * 32, b"\xff")
    header = struct.pack("<II", 0xFFFFFFFE, 0).ljust(32, b"\xff")
    return header + bytes(bitmap) + body


class InspectStockNvsTests(unittest.TestCase):
    def setUp(self):
        ssid_blob = struct.pack("<I", 8) + b"HomeWiFi".ljust(32, b"\0")
        entries = [
            entry(0, 0x01, "nvs.net80211", data=b"\x01"),
            entry(0, 0x01, "user_usual", data=b"\x02"),
            *variable(1, 0x42, "sta.ssid", ssid_blob, chunk=0),
            entry(1, 0x48, "sta.ssid", data=struct.pack("<IBB", len(ssid_blob), 1, 0)),
            entry(1, 0x01, "opmode", data=b"\x01"),
            *variable(2, 0x21, "content", b'{"pw":"hunter22"}\0'),
        ]
        self.dump = page(entries) + b"\xff" * 4096
        self.namespaces, self.values = inspect_stock_nvs.parse(self.dump)

    def test_namespaces_and_keys_are_found(self):
        self.assertEqual(self.namespaces, {1: "nvs.net80211", 2: "user_usual"})
        self.assertEqual(set(self.values), {(1, "sta.ssid"), (1, "opmode"), (2, "content")})
        self.assertEqual(self.values[(1, "sta.ssid")].size, 36)

    def test_the_station_ssid_is_decoded(self):
        self.assertEqual(inspect_stock_nvs.station_ssid(self.values, 1), "HomeWiFi")
        self.assertIsNone(inspect_stock_nvs.station_ssid(self.values, 2))

    def test_values_are_searchable_for_the_password(self):
        self.assertIn(b"hunter22", self.values[(2, "content")].data)

    def test_an_entry_with_a_bad_crc_is_ignored(self):
        damaged = bytearray(self.dump)
        damaged[64 + 32 + 10] ^= 0xFF  # inside user_usual's namespace entry
        namespaces, _ = inspect_stock_nvs.parse(bytes(damaged))
        self.assertNotIn(2, namespaces)


if __name__ == "__main__":
    unittest.main()
