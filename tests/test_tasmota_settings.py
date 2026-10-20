import ctypes
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.autoflash import tasmota_settings as ts

ROOT = Path(__file__).resolve().parents[1]
C_SOURCE = ROOT / "archive/bridge/main/tasmota_settings.c"

# Tasmota's defaults start the pool like this (SET_OTAURL, three MQTT prefixes,
# two SSIDs, two passwords, hostname, ...); the rest of the pool is unused.
DEFAULT_TEXTS = [b"http://ota.tasmota.com/tasmota32/release/tasmota32c3.bin", b"cmnd", b"stat",
                 b"tele", b"", b"", b"", b"", b"%s-%04d", b"", b"", b"", b"", b"DVES_%06X"]


def default_record(texts=DEFAULT_TEXTS) -> bytes:
    record = bytearray(ts.SIZE)
    struct.pack_into("<HHII", record, 0, ts.CFG_HOLDER, ts.SIZE, 1, 0x0F010000)
    pool = b"\0".join(texts) + b"\0"
    record[ts.TEXT_POOL : ts.TEXT_POOL + len(pool)] = pool
    record[ts.CONFIG_VERSION] = ts.ESP32C3
    record[ts.WIFI_CHANNEL] = 6
    record[ts.WIFI_BSSID : ts.WIFI_BSSID + 6] = bytes.fromhex("a1b2c3d4e5f6")
    record[ts.IPV4_ADDRESS : ts.IPV4_ADDRESS + 8] = bytes([192, 168, 0, 52, 192, 168, 0, 1])
    return ts.with_crc(bytes(record))


class SettingsRecordTests(unittest.TestCase):
    def test_a_backup_decodes_to_the_record_and_back(self):
        record = default_record()
        self.assertEqual(ts.decode_backup(ts.encode_backup(record)), record)
        self.assertNotEqual(ts.encode_backup(record)[2:40], record[2:40])  # it is obscured

    def test_a_backup_with_driver_files_has_a_header_to_skip(self):
        record = default_record()
        body = bytearray(b"/.settings".ljust(14, b"\0") + struct.pack("<H", 4096 + 32) + record + bytes(16))
        for index in range(18, len(body)):
            body[index] ^= (ts.CONFIG_FILE_XOR + index) & 0xFF
        self.assertEqual(ts.decode_backup(bytes(body)), record)

    def test_a_template_has_no_network_in_it(self):
        configured = ts.fill(ts.make_template(default_record()), "Home", "secret password")
        template = ts.make_template(configured)
        for index in (ts.SET_STASSID1, ts.SET_STAPWD1):
            self.assertEqual(ts.text(template, index), "")
        self.assertNotIn(b"secret", template)
        self.assertEqual(template[ts.WIFI_CHANNEL : ts.WIFI_CHANNEL + 7], bytes(7))
        self.assertEqual(template[ts.IPV4_ADDRESS : ts.IPV4_ADDRESS + 20], ts.IPV4_DEFAULTS)
        self.assertEqual(ts.problems(template), [])

    def test_filling_puts_the_network_in_and_keeps_every_other_setting(self):
        template = ts.make_template(default_record())
        record = ts.fill(template, "Home & Garden", "p@ss word")
        self.assertEqual(ts.text(record, ts.SET_STASSID1), "Home & Garden")
        self.assertEqual(ts.text(record, ts.SET_STAPWD1), "p@ss word")
        self.assertEqual(ts.text(record, 8), "%s-%04d")      # hostname, after both
        self.assertEqual(ts.text(record, 13), "DVES_%06X")
        self.assertEqual(ts.problems(record), [])
        self.assertEqual(record[ts.TEXT_POOL + ts.TEXT_POOL_SIZE :ts.CRC32],
                         template[ts.TEXT_POOL + ts.TEXT_POOL_SIZE :ts.CRC32])

    def test_a_name_is_written_out_and_the_rest_kept(self):
        template = ts.make_template(default_record())
        named = ts.named(template, "autoflash-recovery-CDEF")
        self.assertEqual(ts.text(named, ts.SET_HOSTNAME), "autoflash-recovery-CDEF")
        self.assertEqual(ts.text(named, 13), "DVES_%06X")
        self.assertEqual(ts.problems(named), [])  # the bridge checks the checksum first
        record = ts.fill(named, "Home", "pw")
        self.assertEqual(ts.text(record, ts.SET_HOSTNAME), "autoflash-recovery-CDEF")

    def test_a_name_with_a_format_in_it_is_refused(self):
        # Tasmota would take it for its own default instead.
        template = ts.make_template(default_record())
        for name in ("autoflash-recovery-%4X", "", "x" * 33):
            with self.assertRaises(ts.SettingsError, msg=name):
                ts.named(template, name)

    def test_a_full_pool_is_refused_not_truncated(self):
        crowded = default_record(DEFAULT_TEXTS + [b"x" * 560])
        with self.assertRaises(ts.SettingsError):
            ts.fill(ts.make_template(crowded), "A" * 32, "B" * 64)

    def test_problems_are_named(self):
        record = bytearray(default_record())
        record[ts.CONFIG_VERSION] = 1
        self.assertTrue(any("ESP32-C3" in p for p in ts.problems(bytes(record))))
        self.assertTrue(any("checksum" in p for p in ts.problems(bytes(record))))

    def test_the_version_reads_as_tasmota_writes_it(self):
        self.assertEqual(ts.version_text(ts.version(default_record())), "15.1.0.0")


class PinnedReleaseTests(unittest.TestCase):
    """The committed template only fits the firmware it was checked against."""

    HOW = (" The Tasmota settings template was checked against the previous pin. "
           "Before updating the constants in tools/autoflash/tasmota_settings.py: compare "
           "tasmota_types.h (TSettings offsets), tasmota.h (SettingsTextIndex), "
           "settings_text_size, CFG_HOLDER and SettingsDelta between the template's "
           "release and the new firmware; then capture a new template with "
           "tools/capture_tasmota_template.py from a plug running the new release, test "
           "it with --verify, and update artifacts/ and SHA256SUMS. Until then D1 gives "
           "Tasmota its Wi-Fi by hand. See archive/docs/reference/automatic-wifi.md.")

    def test_the_berry_firmware_is_the_one_the_template_was_checked_against(self):
        from tools.safeboot_migration import PINNED_ARTIFACTS
        self.assertEqual(PINNED_ARTIFACTS["bluetooth"]["sha256"], ts.BERRY_FIRMWARE_SHA256,
                         "The Berry Tasmota pin changed." + self.HOW)

    def test_the_official_release_is_the_one_the_template_came_from(self):
        from tools.safeboot_migration import OFFICIAL_TASMOTA_COMMIT
        self.assertEqual(OFFICIAL_TASMOTA_COMMIT, ts.TEMPLATE_SOURCE_COMMIT,
                         "The official Tasmota pin changed." + self.HOW)

    def test_the_committed_template_loads_and_is_pinned(self):
        from tools.safeboot_migration import PINNED_ARTIFACTS
        template, reason = ts.load_template(ROOT, PINNED_ARTIFACTS["bluetooth"]["sha256"])
        self.assertIsNotNone(template, reason)
        name = ts.template_path(ROOT).relative_to(ROOT).as_posix()
        self.assertIn(name, (ROOT / "SHA256SUMS").read_text())


@unittest.skipUnless(shutil.which("cc"), "needs a C compiler")
class BridgeCodeMatchesTests(unittest.TestCase):
    """The bridge's C must produce exactly what the Python produces."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        library = Path(cls.directory.name) / "libtasmota.so"
        subprocess.run(["cc", "-shared", "-fPIC", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-o", str(library), str(C_SOURCE)], check=True)
        cls.lib = ctypes.CDLL(str(library))

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def c_fill(self, template: bytes, ssid: str, password: str) -> tuple[int, bytes]:
        class Layout(ctypes.Structure):
            _fields_ = [("pool", ctypes.c_size_t), ("pool_size", ctypes.c_size_t),
                        ("ssid", ctypes.c_uint), ("password", ctypes.c_uint), ("crc", ctypes.c_size_t)]
        layout = Layout(ts.TEXT_POOL, ts.TEXT_POOL_SIZE, ts.SET_STASSID1, ts.SET_STAPWD1, ts.CRC32)
        buffer = ctypes.create_string_buffer(template, len(template))
        s, p = ssid.encode(), password.encode()
        code = self.lib.tasmota_fill(buffer, ctypes.c_size_t(len(template)), ctypes.byref(layout),
                                     s, ctypes.c_size_t(len(s)), p, ctypes.c_size_t(len(p)))
        return code, buffer.raw

    def test_same_record_for_short_long_and_unicode_networks(self):
        template = ts.make_template(default_record())
        for ssid, password in (("A", ""), ("Home & Garden", "p@ss word"),
                               ("x" * 32, "y" * 64), ("Café-Wi-Fi", "ünïcode pässword")):
            code, record = self.c_fill(template, ssid, password)
            self.assertEqual(code, 0)
            self.assertEqual(record, ts.fill(template, ssid, password), ssid)

    def test_a_named_template_is_filled_the_same(self):
        template = ts.named(ts.make_template(default_record()), "autoflash-recovery-CDEF")
        code, record = self.c_fill(template, "Home & Garden", "p@ss word")
        self.assertEqual(code, 0)
        self.assertEqual(record, ts.fill(template, "Home & Garden", "p@ss word"))

    def test_on_its_own_network_the_named_template_is_saved_without_wifi(self):
        # Joined to no network, the bridge fills in none: the template as sent,
        # its name kept, its checksum still Tasmota's.
        template = ts.named(ts.make_template(default_record()), "autoflash-recovery-CDEF")
        code, record = self.c_fill(template, "", "")
        self.assertEqual(code, 0)
        self.assertEqual(record, template)
        self.assertEqual(ts.text(record, ts.SET_HOSTNAME), "autoflash-recovery-CDEF")
        self.assertEqual(ts.text(record, ts.SET_STASSID1), "")
        self.assertEqual(ts.problems(record), [])

    def test_a_damaged_template_is_refused(self):
        template = bytearray(ts.make_template(default_record()))
        template[100] ^= 1
        self.assertEqual(self.c_fill(bytes(template), "Home", "pw")[0], 2)

    def test_no_room_is_refused_and_says_so(self):
        crowded = ts.make_template(default_record(DEFAULT_TEXTS + [b"x" * 560]))
        self.assertEqual(self.c_fill(crowded, "A" * 32, "B" * 64)[0], 4)

    def test_the_checksum_agrees(self):
        data = bytes(range(256)) * 16
        self.lib.tasmota_crc32.restype = ctypes.c_uint32
        self.assertEqual(self.lib.tasmota_crc32(data, ctypes.c_size_t(len(data))), ts.crc32(data))


if __name__ == "__main__":
    unittest.main()
