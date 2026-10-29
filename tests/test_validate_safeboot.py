"""The checks a recovery Safeboot passes before it is published or used:
each passes the image as published, and refuses one it should not."""

import contextlib
import io
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.espressif.safeboot import validate_imprintable_safeboot, validate_recovery_safeboot
from tools.espressif.safeboot.configure_recovery_safeboot import render_header
from tools.autoflash.imprint import imprint, repair_image
from tools.espressif.safeboot.safeboot_migration import RECOVERY_MARKER

ROOT = Path(__file__).resolve().parents[1]
PUBLISHED = (ROOT / "artifacts/autoflash-recovery-safeboot-imprintable.bin").read_bytes()


def with_marker_replaced(image, replacement):
    """The image with the recovery marker's bytes overwritten, NUL-padded, and
    its checksum and hash made good, as a build would have them."""
    assert len(replacement) <= len(RECOVERY_MARKER)
    return repair_image(image.replace(RECOVERY_MARKER, replacement.ljust(len(RECOVERY_MARKER), b"\0"), 1))


class ImprintableSafebootTests(unittest.TestCase):
    """validate_imprintable_safeboot.py: publishable, and writable into."""

    def validate(self, image):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "safeboot.bin"
            path.write_bytes(image)
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(sys, "argv", ["validate_imprintable_safeboot.py", str(path)]), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = validate_imprintable_safeboot.main()
        failed = [line.strip()[len("FAIL  "):] for line in out.getvalue().splitlines()
                  if line.strip().startswith("FAIL")]
        return code, failed, out.getvalue() + err.getvalue()

    def test_the_published_image_is_publishable(self):
        code, failed, said = self.validate(PUBLISHED)
        self.assertEqual((code, failed), (0, []), said)
        self.assertIn("\nPUBLISHABLE", said)

    def test_an_image_with_credentials_written_in_is_not(self):
        code, failed, said = self.validate(imprint(PUBLISHED, "Home-2G", "a-password"))
        self.assertEqual(code, 1)
        self.assertTrue(any(check.startswith("network name placeholder") for check in failed), failed)
        self.assertIn("NOT PUBLISHABLE", said)

    def test_an_image_holding_a_build_path_is_not(self):
        code, failed, _ = self.validate(with_marker_replaced(PUBLISHED, b"/home/builder/tasmota/src/x.c"))
        self.assertEqual(code, 1)
        self.assertEqual(len(failed), 1, failed)
        self.assertTrue(failed[0].startswith("no build-machine paths baked in"), failed)

    def test_an_image_already_holding_what_it_is_tested_with_is_not(self):
        # Else reading back what was written could find it where it already was.
        code, failed, _ = self.validate(with_marker_replaced(PUBLISHED, b"S" * 32))
        self.assertEqual(code, 1)
        self.assertEqual(failed, ["no credential survives into the shipped image"])

    def test_a_corrupted_image_is_not(self):
        changed = bytearray(PUBLISHED)
        changed[len(changed) // 2] ^= 0xFF
        code, failed, _ = self.validate(bytes(changed))
        self.assertEqual(code, 1)
        self.assertTrue({"checksum valid", "appended SHA-256 valid"} & set(failed), failed)

    def test_an_image_too_big_for_the_partition_is_not(self):
        code, failed, _ = self.validate(PUBLISHED + bytes(validate_imprintable_safeboot.SAFEBOOT_PARTITION_BYTES))
        self.assertEqual(code, 1)
        self.assertTrue(any(check.startswith("fits the safeboot partition") for check in failed), failed)

    def test_a_missing_image_is_said_so(self):
        with mock.patch.object(sys, "argv", ["validate_imprintable_safeboot.py", "/nonexistent/safeboot.bin"]), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(validate_imprintable_safeboot.main(), 2)
        self.assertIn("FAIL", err.getvalue())


def header(ssid, password):
    """The private header configure_recovery_safeboot.py writes for that Wi-Fi."""
    return render_header(ssid.decode(), password.decode())


class RecoverySafebootTests(unittest.TestCase):
    """validate_recovery_safeboot.py: a private build holds the Wi-Fi it was
    built with, and its report never shows it."""

    SSID, PASSWORD = b"Home-2G", b"a-password"
    BUILT = imprint(PUBLISHED, SSID.decode(), PASSWORD.decode())

    def test_a_build_holding_its_wifi_passes_and_its_report_holds_none_of_it(self):
        report = validate_recovery_safeboot.validate(self.BUILT, header(self.SSID, self.PASSWORD))
        self.assertEqual((report["ssid_bytes"], report["password_present"]), (len(self.SSID), True))
        self.assertEqual(report["kind"], validate_recovery_safeboot.RECOVERY_KIND)
        self.assertEqual(report["headroom"], validate_recovery_safeboot.SAFEBOOT_SIZE - len(self.BUILT))
        written = json.dumps(report)
        self.assertNotIn(self.SSID.decode(), written)
        self.assertNotIn(self.PASSWORD.decode(), written)

    def test_an_open_network_needs_no_password(self):
        # configure_recovery_safeboot.py takes an empty password, as STA_PASS1 "".
        built = imprint(PUBLISHED, self.SSID.decode(), "")
        self.assertFalse(validate_recovery_safeboot.validate(built, header(self.SSID, b""))["password_present"])

    def test_a_build_without_what_it_should_hold_is_refused(self):
        for case, image, given, said in (
                ("another network", self.BUILT, header(b"Other-5G", self.PASSWORD), "SSID was not linked"),
                ("another password", self.BUILT, header(self.SSID, b"other-password"), "password was not linked"),
                ("no recovery marker", with_marker_replaced(self.BUILT, b"x"), header(self.SSID, self.PASSWORD),
                 "recovery marker is absent"),
                ("a header without the network", self.BUILT, b'#define STA_PASS1 "\\x61"\n', "no encoded STA_SSID1"),
                ("an empty network name", self.BUILT, b'#define STA_SSID1 ""\n#define STA_PASS1 ""\n',
                 "empty STA_SSID1"),
                ("not an image", b"\0" * 1024, header(self.SSID, self.PASSWORD), "")):
            with self.assertRaisesRegex(ValueError, said, msg=case):
                validate_recovery_safeboot.validate(image, given)

    def test_the_official_safeboot_is_refused(self):
        from tools.espressif.safeboot.safeboot_migration import sha256_bytes
        pinned = {"safeboot": {"sha256": sha256_bytes(self.BUILT)}}
        with mock.patch.object(validate_recovery_safeboot, "PINNED_ARTIFACTS", pinned), \
             self.assertRaisesRegex(ValueError, "equals official Safeboot"):
            validate_recovery_safeboot.validate(self.BUILT, header(self.SSID, self.PASSWORD))

    def test_the_report_is_written_for_its_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "image.bin").write_bytes(self.BUILT)
            (root / "private.h").write_bytes(header(self.SSID, self.PASSWORD))
            output = root / "private" / "report.json"
            argv = ["validate_recovery_safeboot.py", "--image", str(root / "image.bin"),
                    "--private-header", str(root / "private.h"), "--output", str(output)]
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()) as said:
                self.assertEqual(validate_recovery_safeboot.main(), 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["ssid_bytes"], len(self.SSID))
            self.assertEqual([p.name for p in output.parent.iterdir()], ["report.json"])  # no temporary left
            if sys.platform != "win32":
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(output.parent.stat().st_mode), 0o700)
        for secret in (self.SSID, self.PASSWORD):
            self.assertNotIn(secret.decode(), said.getvalue())

    def test_a_refused_build_writes_no_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "image.bin").write_bytes(self.BUILT)
            (root / "private.h").write_bytes(header(b"Other-5G", self.PASSWORD))
            output = root / "report.json"
            argv = ["validate_recovery_safeboot.py", "--image", str(root / "image.bin"),
                    "--private-header", str(root / "private.h"), "--output", str(output)]
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stderr(io.StringIO()), \
                 self.assertRaises(SystemExit):
                validate_recovery_safeboot.main()
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
