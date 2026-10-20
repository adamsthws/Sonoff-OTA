import hashlib
import struct
import unittest
from pathlib import Path

from tools.analyze_vendor_ota import parse_esp_image
from tools.autoflash.imprint import (
    FILLER,
    PASSWORD_FIELD_BYTES,
    PASSWORD_MARKER,
    SSID_FIELD_BYTES,
    SSID_MARKER,
    ImprintError,
    has_placeholders,
    require_imprinted,
    find_field,
    derived_from,
    imprint,
    imprinted_ssid,
    read_c_string,
    repair_image,
)

ESP32_C3_CHIP_ID = 5


def build_image(payload: bytes, hash_appended: bool = True) -> bytes:
    """A minimal but structurally real ESP32-C3 application image."""
    header = bytearray(24)
    header[0] = 0xE9
    header[1] = 1                                   # one segment
    struct.pack_into("<I", header, 4, 0x40380000)   # entry address
    struct.pack_into("<H", header, 12, ESP32_C3_CHIP_ID)
    header[23] = 1 if hash_appended else 0

    segment = struct.pack("<II", 0x3C000000, len(payload)) + payload
    body = bytes(header) + segment

    checksum = 0xEF
    for value in payload:
        checksum ^= value
    checksum_offset = ((len(body) // 16) + 1) * 16 - 1
    image = bytearray(body.ljust(checksum_offset, b"\x00"))
    image.append(checksum)
    if hash_appended:
        image += hashlib.sha256(bytes(image)).digest()
    return bytes(image)


def placeholder_payload() -> bytes:
    """Exactly what tools/configure_imprintable_safeboot.py makes the compiler emit."""
    return (
        b"some other rodata\x00"
        + SSID_MARKER + FILLER * SSID_FIELD_BYTES + b"\x00"
        + b"more rodata\x00"
        + PASSWORD_MARKER + FILLER * PASSWORD_FIELD_BYTES + b"\x00"
        + b"trailing\x00"
    )


class ImprintTests(unittest.TestCase):
    def setUp(self):
        self.image = build_image(placeholder_payload())
        report = parse_esp_image(self.image)
        self.assertTrue(report["checksum_valid"], "fixture image must start valid")
        self.assertTrue(report["sha256_valid"], "fixture image must start valid")

    def test_credentials_are_written_and_the_image_stays_valid(self):
        result = imprint(self.image, "Cloud Machine", "hunter2-hunter2")
        report = parse_esp_image(result)
        self.assertTrue(report["checksum_valid"])
        self.assertTrue(report["sha256_valid"])
        self.assertTrue(report["chip_is_esp32_c3"])
        self.assertEqual(len(result), len(self.image))
        self.assertIn(b"Cloud Machine\x00", result)
        self.assertIn(b"hunter2-hunter2\x00", result)

    def test_patching_actually_changes_both_integrity_fields(self):
        result = imprint(self.image, "Cloud Machine", "hunter2-hunter2")
        before, after = parse_esp_image(self.image), parse_esp_image(result)
        self.assertNotEqual(before["stored_checksum"], after["stored_checksum"])
        self.assertNotEqual(before["stored_sha256"], after["stored_sha256"])

    def test_a_naive_patch_without_repair_would_be_rejected(self):
        # Guards the reason this module exists: editing the bytes is not enough.
        naive = bytearray(self.image)
        offset = find_field(self.image, SSID_MARKER, SSID_FIELD_BYTES)
        naive[offset : offset + 5] = b"WIFI\x00"
        report = parse_esp_image(bytes(naive))
        self.assertFalse(report["checksum_valid"] and report["sha256_valid"])

    def test_the_longest_allowed_credentials_fit(self):
        result = imprint(self.image, "S" * 32, "P" * 63)
        self.assertTrue(parse_esp_image(result)["sha256_valid"])

    def test_values_too_long_for_the_reserved_space_are_refused(self):
        with self.assertRaisesRegex(ImprintError, "too long"):
            imprint(self.image, "S" * 33, "fine")
        with self.assertRaisesRegex(ImprintError, "too long"):
            imprint(self.image, "fine", "P" * 64)

    def test_an_image_without_placeholders_is_refused(self):
        plain = build_image(b"no placeholders here\x00")
        self.assertFalse(has_placeholders(plain))
        with self.assertRaisesRegex(ImprintError, "not built to have credentials"):
            imprint(plain, "net", "pass")

    def test_a_duplicated_placeholder_is_refused_rather_than_guessed(self):
        doubled = build_image(placeholder_payload() + SSID_MARKER + FILLER * SSID_FIELD_BYTES)
        with self.assertRaisesRegex(ImprintError, "more than once"):
            imprint(doubled, "net", "pass")

    def test_an_unwritten_image_is_refused_before_it_can_be_installed(self):
        # The dangerous case: placeholders left in place would boot looking for
        # a network that does not exist and never raise an access point.
        self.assertTrue(has_placeholders(self.image))
        with self.assertRaisesRegex(ImprintError, "still has its credential placeholders"):
            require_imprinted(self.image)
        require_imprinted(imprint(self.image, "Net", "Pass"))

    def test_an_empty_network_name_is_refused(self):
        with self.assertRaisesRegex(ImprintError, "network name is required"):
            imprint(self.image, "", "pass")

    def test_an_open_network_with_no_password_is_allowed(self):
        result = imprint(self.image, "OpenNet", "")
        self.assertTrue(parse_esp_image(result)["sha256_valid"])

    def test_imprinting_is_deterministic_from_the_template(self):
        once = imprint(self.image, "Net", "Pass")
        self.assertEqual(once, imprint(self.image, "Net", "Pass"))
        different = imprint(self.image, "Other", "Pass")
        self.assertNotEqual(once, different)
        self.assertTrue(parse_esp_image(different)["sha256_valid"])

    def test_an_imprinted_copy_cannot_be_imprinted_again(self):
        once = imprint(self.image, "Net", "Pass")
        with self.assertRaisesRegex(ImprintError, "not built to have credentials"):
            imprint(once, "Other", "Pass")

    def test_the_firmware_reads_exactly_the_credentials_given(self):
        # The compiled string literal starts at the marker, so that is where the
        # firmware starts reading. Regression: values written after the marker
        # left the plug looking for "S60-IMPRINT-SSID>Net".
        ssid_at = find_field(self.image, SSID_MARKER, SSID_FIELD_BYTES)
        pass_at = find_field(self.image, PASSWORD_MARKER, PASSWORD_FIELD_BYTES)
        result = imprint(self.image, "Wonky-2G", "correct horse")
        self.assertEqual(read_c_string(result, ssid_at), b"Wonky-2G")
        self.assertEqual(read_c_string(result, pass_at), b"correct horse")
        self.assertNotIn(SSID_MARKER, result)
        self.assertNotIn(PASSWORD_MARKER, result)

    def test_the_network_name_can_be_read_back_from_an_imprinted_copy(self):
        result = imprint(self.image, "Wonky-2G", "correct horse")
        self.assertEqual(imprinted_ssid(result, self.image), "Wonky-2G")

    def test_an_image_written_after_the_marker_is_refused(self):
        # What the faulty tool produced: marker kept, value after it.
        payload = (
            b"some other rodata\x00"
            + SSID_MARKER + b"Net".ljust(SSID_FIELD_BYTES, b"\x00") + b"\x00"
            + b"more rodata\x00"
            + PASSWORD_MARKER + b"Pass".ljust(PASSWORD_FIELD_BYTES, b"\x00") + b"\x00"
        )
        with self.assertRaisesRegex(ImprintError, "still in this image"):
            require_imprinted(build_image(payload))


if __name__ == "__main__":
    unittest.main()


class RealImageTests(unittest.TestCase):
    """The synthetic fixture has one segment. Real Tasmota images have several,
    a different padding position and a much larger appended hash input, so the
    repair is also exercised against a genuine binary when one is available."""

    REAL = Path("captures/safeboot-recovery-migration/tasmota32c3-safeboot.bin")

    @unittest.skipUnless(REAL.exists(), "no local Tasmota image to test against")
    def test_repair_reproduces_a_real_image_byte_for_byte(self):
        image = self.REAL.read_bytes()
        before = parse_esp_image(image)
        self.assertGreater(before["segment_count"], 1)
        self.assertTrue(before["checksum_valid"] and before["sha256_valid"])
        # Recomputing the integrity fields of an untouched image must be a no-op.
        self.assertEqual(repair_image(image), image)

    @unittest.skipUnless(REAL.exists(), "no local Tasmota image to test against")
    def test_a_changed_byte_is_detected_then_repaired(self):
        image = bytearray(self.REAL.read_bytes())
        report = parse_esp_image(bytes(image))
        target = report["segments"][-1]["data_offset"]
        image[target] ^= 0xFF

        broken = parse_esp_image(bytes(image))
        self.assertFalse(broken["checksum_valid"] and broken["sha256_valid"])

        repaired = parse_esp_image(repair_image(bytes(image)))
        self.assertTrue(repaired["checksum_valid"])
        self.assertTrue(repaired["sha256_valid"])


class DerivationTests(unittest.TestCase):
    """Provenance without secrets: an imprinted image must be the published one
    plus credentials, and nothing else."""

    def setUp(self):
        self.published = build_image(placeholder_payload())

    def test_an_imprinted_copy_is_recognised_as_derived(self):
        for ssid, password in (("Home", "pw"), ("Другая сеть"[:32], ""), ("S" * 32, "P" * 63)):
            with self.subTest(ssid=ssid):
                self.assertTrue(derived_from(imprint(self.published, ssid, password), self.published))

    def test_two_different_imprints_are_both_derived_from_the_same_original(self):
        one = imprint(self.published, "NetOne", "passwordone")
        two = imprint(self.published, "NetTwo", "passwordtwo")
        self.assertNotEqual(one, two)
        self.assertTrue(derived_from(one, self.published))
        self.assertTrue(derived_from(two, self.published))

    def test_code_smuggled_in_alongside_credentials_is_caught(self):
        tampered = bytearray(imprint(self.published, "Home", "pw"))
        report = parse_esp_image(bytes(tampered))
        target = report["segments"][0]["data_offset"]
        tampered[target] ^= 0xFF                      # change a byte of the program
        from tools.autoflash.imprint import repair_image
        repaired = repair_image(bytes(tampered))      # and fix the hashes to hide it
        self.assertTrue(parse_esp_image(repaired)["sha256_valid"])
        self.assertFalse(derived_from(repaired, self.published),
                         "a repaired but modified image must not pass as derived")

    def test_a_different_firmware_is_not_derived(self):
        other = build_image(placeholder_payload() + b"different build\x00")
        self.assertFalse(derived_from(other, self.published))


class RecoverySafebootHeaderTests(unittest.TestCase):
    """The published build is unchanged unless settings-saving is asked for."""

    def header(self, persist: bool) -> str:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        from configure_imprintable_safeboot import render_header
        return render_header(persist).decode()

    def test_the_default_build_is_the_published_one(self):
        self.assertNotIn("AUTOFLASH_RECOVERY_PERSIST_SETTINGS", self.header(False))
        self.assertIn("AUTOFLASH_RECOVERY_WIFI_DEFAULTS", self.header(False))

    def test_settings_saving_is_asked_for_explicitly(self):
        self.assertIn("AUTOFLASH_RECOVERY_PERSIST_SETTINGS", self.header(True))

    def test_the_patch_guards_against_being_used_alone(self):
        patch = (Path(__file__).resolve().parents[1]
                 / "repartition/recovery-safeboot/persist-settings.patch").read_text()
        # It must refuse to build without the Wi-Fi defaults it saves, and must
        # never overwrite settings a configured device already has.
        self.assertIn("#error AUTOFLASH_RECOVERY_PERSIST_SETTINGS requires", patch)
        self.assertIn("CFG_HOLDER == stored->cfg_holder", patch)
        self.assertIn("left alone", patch)
