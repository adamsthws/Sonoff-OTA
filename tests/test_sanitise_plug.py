"""The public plug: only what is shown to be public goes in, and the proof finds
anything of Sonoff's or the real plug's that would have."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from tools.autoflash import esphome, plugs, tasmota_settings as ts
from tools.emulator import public_plugs, sanitise_plug as sp

HAS_NVS_GEN = importlib.util.find_spec("esp_idf_nvs_partition_gen") is not None
TEST_MAC, REAL_MAC = "12:34:56:ab:cd:ef", "12:34:56:00:00:01"


def efuse_for(mac: str) -> bytes:
    efuse = bytearray(sp.EFUSE_SIZE)
    efuse[0x00:0x18] = b"\x11" * 0x18             # BLOCK0's settings
    efuse[0x18:0x1E] = bytes.fromhex(mac.replace(":", ""))[::-1]
    efuse[0x1E:0x30] = b"\x22" * 0x12             # the chip's calibration
    efuse[0x30:0x40] = b"\x33" * 0x10             # its factory unique ID
    efuse[0x40:0x50] = b"\x44" * 0x10             # more calibration
    efuse[0x50:0x70] = b"\x55" * 0x20             # the vendor's user data
    return bytes(efuse)


class EfuseTests(unittest.TestCase):
    def test_only_what_every_such_chip_shares_is_kept(self):
        efuse = bytearray(b"\xff" * sp.EFUSE_SIZE)
        efuse[0x18:0x1E] = bytes.fromhex(TEST_MAC.replace(":", ""))[::-1]
        kept = sp.sanitise_efuse(bytes(efuse), TEST_MAC)
        self.assertEqual(plugs.mac_from_efuse(kept), TEST_MAC)
        self.assertEqual(kept[:0x18], b"\xff" * 0x18)                 # BLOCK0's settings
        bit = lambda block, n: kept[block + n // 8] >> (n % 8) & 1  # noqa: E731
        for n in (114, 117, 120, 128, 183, 184, 185):                # revision, package, flash
            self.assertEqual(bit(0x18, n), 1, n)
        for n in (135, 142, 149, 157, 165, 170, 179, 50):            # BLOCK1 calibration, pads
            self.assertEqual(bit(0x18, n), 0, n)
        self.assertEqual(kept[0x30:0x40], bytes(16))                 # the factory unique ID
        self.assertEqual(bit(0x30, 128), 1)                          # BLK_VERSION_MAJOR
        for n in (131, 140, 148, 227):                               # temperature, ADC calibration
            self.assertEqual(bit(0x30, n), 0, n)
        self.assertEqual(kept[0x50:], bytes(sp.EFUSE_SIZE - 0x50))

    def test_another_plugs_efuses_are_refused(self):
        with self.assertRaises(sp.Refused):
            sp.sanitise_efuse(efuse_for(REAL_MAC), TEST_MAC)


class FlashTests(unittest.TestCase):
    def test_a_plug_before_d5_is_refused(self):
        # Sonoff's bootloader is still there: nothing is taken from it.
        flash = bytearray(b"\xff" * sp.FLASH_SIZE)
        flash[:4] = b"\xe9\x03\x02\x20"
        with self.assertRaisesRegex(sp.Refused, "past D5"):
            sp.sanitise_flash(bytes(flash))

    def test_an_unknown_partition_table_is_refused(self):
        flash = bytearray(b"\xff" * sp.FLASH_SIZE)
        bootloader = esphome.published_bootloader()
        flash[:len(bootloader)] = bootloader
        with self.assertRaisesRegex(sp.Refused, "partition table"):
            sp.sanitise_flash(bytes(flash))

    def test_the_mac_is_sought_in_every_form(self):
        forms = sp.mac_forms("12:34:56:9a:bc:de")
        for form in (b"12:34:56:9a:bc:de", b"12-34-56-9A-BC-DE", b"1234569abcde", b"9ABCDE",
                     bytes.fromhex("1234569abcde"), bytes.fromhex("debc9a563412")):
            self.assertIn(form, forms)


@unittest.skipUnless(HAS_NVS_GEN, "needs esp-idf-nvs-partition-gen (the emulator's)")
class SettingsTests(unittest.TestCase):
    def tasmota_nvs(self, ssid: str, password: str = "") -> bytes:
        template, _ = ts.load_template(sp.ROOT, ts.BERRY_FIRMWARE_SHA256)
        record = ts.with_text(ts.with_text(template, ts.SET_STASSID1, ssid), ts.SET_STAPWD1, password)
        return sp.rebuilt_nvs([("main", "namespace", "", ""), ("Settings", "data", "hex2bin", record.hex()),
                               ("frame_params", "namespace", "", ""),
                               ("content", "data", "hex2bin",
                                b'{"deviceid":"1000fedcba","apikey":"secret-key-1"}\0'.hex())],
                              0x5000)

    def test_only_the_interim_tasmotas_record_is_kept(self):
        rows, _ = sp.nvs_rows_of_interim_tasmota(self.tasmota_nvs(sp.EMULATOR_WIFI))
        self.assertEqual([row[0] for row in rows], ["main", "Settings"])

    def test_a_record_naming_a_real_network_is_refused(self):
        with self.assertRaisesRegex(sp.Refused, "another network"):
            sp.nvs_rows_of_interim_tasmota(self.tasmota_nvs("Home-2G", "a password"))

    def test_the_proof_finds_a_value_of_sonoffs_settings(self):
        stock = bytearray(b"\xff" * sp.FLASH_SIZE)
        nvs = self.tasmota_nvs(sp.EMULATOR_WIFI)
        stock[0x9000:0x9000 + len(nvs)] = nvs
        values = sp.sonoff_values(bytes(stock))
        self.assertIn(b"1000fedcba", values)
        self.assertEqual(values[b"secret-key-1"], "frame_params/content .apikey")
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            (out / "clean.json").write_text(json.dumps({"deviceid": "1000abcdef"}))
            sp.prove(out, bytes(stock), REAL_MAC)
            (out / "leaked.json").write_text(json.dumps({"id": "1000fedcba"}))
            with self.assertRaisesRegex(sp.Refused, r"leaked.json: a value of Sonoff's settings \(frame_params"):
                sp.prove(out, bytes(stock), REAL_MAC, explain=True)

    def test_the_proof_finds_the_real_mac_and_blocks_of_the_dump(self):
        stock = bytearray(b"\xff" * sp.FLASH_SIZE)
        stock[0x20000:0x20100] = bytes(range(256))
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            (out / "flash.bin").write_bytes(b"\xff" * 512 + bytes(range(256)))
            with self.assertRaisesRegex(sp.Refused, "1 block"):
                sp.prove(out, bytes(stock), REAL_MAC)
            (out / "flash.bin").write_bytes(b"MAC " + REAL_MAC.upper().encode())
            with self.assertRaisesRegex(sp.Refused, "the real MAC"):
                sp.prove(out, bytes(stock), REAL_MAC)


class PublicPlugsTests(unittest.TestCase):
    def test_a_kept_plug_counts_as_stopped_only_after_its_step(self):
        with tempfile.TemporaryDirectory() as directory:
            saved = Path(directory)
            run = saved / "captures/plugs/12-34-56-ab-cd-ef/runs/1"
            run.mkdir(parents=True)
            (saved / "captures/.autoflash-current").write_text("plugs/12-34-56-ab-cd-ef/runs/1\n")
            (saved / "flash.bin").write_bytes(b"")
            (run / "autoflash-state.json").write_text(json.dumps({"steps": {"E3": {"status": "attempted"}}}))
            self.assertFalse(public_plugs.step_done(saved, "E3"))
            (run / "autoflash-state.json").write_text(json.dumps({"steps": {"E3": {"status": "done"}}}))
            self.assertTrue(public_plugs.step_done(saved, "E3"))

    def test_each_final_firmware_is_installed_then_left(self):
        self.assertEqual({way[0][1] for way in public_plugs.WAYS}, {"tasmota", "esphome"})
        for (_, first, kept), (source, second, _) in public_plugs.WAYS:
            self.assertEqual(source, kept)   # from the plug its own first run made
            self.assertNotEqual(second, first)

    def test_each_way_goes_on_without_waiting_for_the_other(self):
        import argparse
        import io
        import threading
        import time
        from contextlib import redirect_stdout
        from unittest import mock
        # The way to Tasmota's first run takes long and fails; the way to
        # ESPHome's is quick, and its second starts while the first still runs.
        events, lock = [], threading.Lock()

        def fake_run(instance, source, firmware, save, log):
            with lock:
                events.append(("start", source))
            time.sleep(0.3 if source == "public-pre-final-tasmota" else 0.05)
            with lock:
                events.append(("end", source))
            ok = source != "public-pre-final-tasmota"
            return ok, "Converted" if ok else "exit 1"

        with tempfile.TemporaryDirectory() as directory:
            saved = Path(directory)
            for start in ("public-pre-final-tasmota", "public-pre-final-esphome"):
                (saved / start).mkdir()
                (saved / start / "flash-mode").write_text("public\n")
            out = io.StringIO()
            with mock.patch.object(public_plugs, "SAVED", saved), \
                    mock.patch.object(public_plugs, "LOGS", saved / "logs"), \
                    mock.patch.object(public_plugs, "run", side_effect=fake_run), redirect_stdout(out):
                code = public_plugs.command_test(argparse.Namespace(instances=[1, 2]))
        self.assertEqual(code, 1)
        self.assertLess(events.index(("start", "public-on-esphome")),
                        events.index(("end", "public-pre-final-tasmota")))
        self.assertNotIn(("start", "public-on-tasmota"), events)
        said = out.getvalue()
        self.assertIn("FAIL  public-pre-final-tasmota to tasmota: exit 1", said)
        self.assertIn("SKIP  public-on-tasmota to esphome: the run before it failed", said)
        self.assertIn("PASS  public-on-esphome to tasmota: Converted", said)
        self.assertIn("1 failed", said)


if __name__ == "__main__":
    unittest.main()
