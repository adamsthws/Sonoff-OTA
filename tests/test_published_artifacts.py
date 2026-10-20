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

    def test_the_run_installs_the_pinned_bridge(self):
        from tools.autoflash_cli import BRIDGE_OTA, ROOT
        self.assertEqual(BRIDGE_OTA.name, "s60-ota-bridge-4.4.3.ota")
        line = f"{sha256_bytes((ROOT / BRIDGE_OTA).read_bytes())}  {BRIDGE_OTA.as_posix()}"
        self.assertIn(line, (ROOT / "SHA256SUMS").read_text().splitlines())

    def test_the_bridge_carries_one_version_everywhere(self):
        import re
        from tools.analyze_vendor_ota import analyze
        from tools.build_vendor_ota import app_version
        from tools.autoflash_cli import BRIDGE_OTA, ROOT
        source = (ROOT / "archive/bridge/main/main.c").read_text()
        project = (ROOT / "archive/bridge/CMakeLists.txt").read_text()
        version = re.search(r'#define BRIDGE_VERSION "([^"]+)"', source).group(1)
        self.assertEqual(re.search(r'set\(PROJECT_VER "([^"]+)"\)', project).group(1), version)
        self.assertIn(f"-{version}.ota", BRIDGE_OTA.name)
        wrapped = (ROOT / BRIDGE_OTA).read_bytes()
        report = analyze(wrapped)
        self.assertEqual(report["wrapper"]["header_version"], version)
        self.assertEqual(report["wrapper"]["record_version"], version)
        self.assertEqual(app_version(wrapped[100:]), version)
        self.assertIn(f"bridge {version}\n".encode(), wrapped)

    def test_the_bridge_can_back_up_the_stock_flash(self):
        from tools.autoflash_cli import BRIDGE_OTA, ROOT
        wrapped = (ROOT / BRIDGE_OTA).read_bytes()
        self.assertIn(b"/flash\0", wrapped)
        self.assertIn(b"/efuse\0", wrapped)
        self.assertIn(b"/update\0", wrapped)

    def test_the_published_startup_firmware_saves_the_wifi(self):
        from tools.safeboot_migration import PUBLISHED_RECOVERY_FILE, PUBLISHED_RECOVERY_SHA256
        image = PUBLISHED_RECOVERY_FILE.read_bytes()
        self.assertEqual(sha256_bytes(image), PUBLISHED_RECOVERY_SHA256)
        self.assertIn(b"Autoflash recovery Wi-Fi defaults active", image)
        self.assertIn(b"Autoflash recovery: Wi-Fi settings saved for the next firmware", image)
        # The version that wipes NVS and reads the record back before saying so.
        self.assertIn(b"Autoflash recovery: settings not saved, read-back failed", image)
        # Named as the tool, not one device, on the network and in its log.
        self.assertIn(b"autoflash-recovery-%04d", image)
        self.assertNotIn(b"S60 recovery", image)
        self.assertNotIn(b"s60-recovery", image)


if __name__ == "__main__":
    unittest.main()


class PublishedESPHomeTests(unittest.TestCase):
    def test_the_esphome_image_is_pinned(self):
        from tools.autoflash import esphome
        from tools.autoflash_cli import ROOT
        image = esphome.published_image()  # refuses a changed file
        line = f"{sha256_bytes(image)}  {esphome.IMAGE.relative_to(ROOT).as_posix()}"
        self.assertIn(line, (ROOT / "SHA256SUMS").read_text().splitlines())

    def test_its_bootloader_is_pinned_and_comes_from_the_same_build(self):
        from tools.autoflash import esphome
        from tools.autoflash_cli import ROOT
        bootloader = esphome.published_bootloader()  # refuses a changed or broken file
        line = f"{sha256_bytes(bootloader)}  {esphome.BOOTLOADER.relative_to(ROOT).as_posix()}"
        self.assertIn(line, (ROOT / "SHA256SUMS").read_text().splitlines())
        version = esphome.IMAGE.name.rsplit("-", 1)[1]
        self.assertTrue(esphome.BOOTLOADER.name.endswith("-" + version), "built with the image")

    def test_the_esphome_image_can_have_the_wifi_written_in(self):
        from tools.autoflash import esphome, imprint
        image = esphome.published_image()
        validate_native_image(image, esphome.SLOT_SIZE, "published ESPHome")
        self.assertEqual(imprint.find_build_paths(image), [])
        written = esphome.image_for("Home", "a password")
        imprint.require_imprinted(written)
        self.assertEqual(len(written), len(image))

    def test_the_esphome_config_has_no_passwords_and_can_be_adopted(self):
        from tools.autoflash_cli import ROOT
        config = (ROOT / "ESPHome" / "sonoff_s60.yaml").read_text()
        for key in ("password:", "encryption:", "auth:", "key:"):
            self.assertNotIn(key, config.replace("password: !lambda", ""), key)
        self.assertIn("package_import_url: github://adamsthws/Sonoff-OTA/ESPHome/sonoff_s60.yaml@main", config)
        self.assertNotIn("includes:", config)  # adoption takes this one file


class ESPHomeImageCheckTests(unittest.TestCase):
    def test_a_factory_image_or_another_chip_is_refused(self):
        from tools.autoflash import esphome
        image = bytearray(esphome.published_image())
        factory = bytearray(image)
        factory[32:36] = b"\x50\x00\x00\x00"  # a bootloader's description, not an app's
        with self.assertRaisesRegex(esphome.ImageError, "factory"):
            esphome.check_app(bytes(factory), "firmware.factory.bin")
        other = bytearray(image)
        other[12] = 0  # ESP32
        with self.assertRaisesRegex(esphome.ImageError, "ESP32-C3"):
            esphome.check_app(bytes(other), "firmware.ota.bin")
        with self.assertRaisesRegex(esphome.ImageError, "app slot"):
            esphome.check_app(bytes(image) + b"\0" * esphome.SLOT_SIZE, "big.bin")


class PublishedFilesAreInGitTests(unittest.TestCase):
    def test_every_pinned_file_is_tracked(self):
        # .gitignore leaves out *.bin and *.ota but for each published one by
        # name; one not named passes every test here and is missing from a clone.
        import shutil
        import subprocess
        from tools.autoflash_cli import ROOT
        if shutil.which("git") is None or not (ROOT / ".git").exists():
            self.skipTest("not a Git checkout")
        pinned = [line.split()[1] for line in (ROOT / "SHA256SUMS").read_text().splitlines() if line.strip()]
        tracked = set(subprocess.run(["git", "ls-files", "--", *pinned], cwd=ROOT, check=True,
                                     capture_output=True, text=True).stdout.split())
        self.assertEqual([path for path in pinned if path not in tracked], [])
