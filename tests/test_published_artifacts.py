import unittest

from tools.safeboot_migration import (
    PINNED_ARTIFACTS,
    PUBLISHED_BLUETOOTH_FILE,
    PUBLISHED_BLUETOOTH_SOURCE_COMMIT,
    sha256_bytes,
    validate_native_image,
)


class PublishedArtifactTests(unittest.TestCase):
    def test_bluetooth_image_is_the_reviewed_upstream_build(self):
        image = PUBLISHED_BLUETOOTH_FILE.read_bytes()
        pinned = PINNED_ARTIFACTS["bluetooth"]

        self.assertEqual(len(image), pinned["size"])
        self.assertEqual(sha256_bytes(image), pinned["sha256"])
        report = validate_native_image(image, 0x1F0000, "published Bluetooth")
        self.assertTrue(report["chip_is_esp32_c3"])
        self.assertIn(b"bluetooth", image)
        self.assertIn(
            PUBLISHED_BLUETOOTH_SOURCE_COMMIT[:7].encode("ascii"),
            image,
        )

    def test_the_run_installs_the_pinned_bridge_v5(self):
        from tools.s60_autoflash import BRIDGE_OTA, ROOT
        self.assertIn("-v5-", BRIDGE_OTA.name)
        line = f"{sha256_bytes((ROOT / BRIDGE_OTA).read_bytes())}  {BRIDGE_OTA.as_posix()}"
        self.assertIn(line, (ROOT / "SHA256SUMS").read_text().splitlines())

    def test_the_published_startup_firmware_saves_the_wifi(self):
        from tools.safeboot_migration import PUBLISHED_RECOVERY_FILE, PUBLISHED_RECOVERY_SHA256
        image = PUBLISHED_RECOVERY_FILE.read_bytes()
        self.assertEqual(sha256_bytes(image), PUBLISHED_RECOVERY_SHA256)
        self.assertIn(b"S60 recovery Wi-Fi defaults active", image)
        self.assertIn(b"S60 recovery: Wi-Fi settings saved for the next firmware", image)
        # The version that wipes NVS and reads the record back before saying so.
        self.assertIn(b"S60 recovery: settings not saved, read-back failed", image)


if __name__ == "__main__":
    unittest.main()
