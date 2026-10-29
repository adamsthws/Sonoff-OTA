"""The Wi-Fi password and other secrets are never left on disk."""

import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.espressif.safeboot import prepare_imprinted_bundle as bundle
from tools.espressif.safeboot import safeboot_migration
from tools.espressif.safeboot.safeboot_migration import RECOVERY_ARTIFACT

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / "captures" / "safeboot-recovery-migration"
OFFICIAL = {name: LOCAL / name for name in
            ("tasmota32c3.factory.bin", "tasmota32c3-safeboot.bin", "tasmota32c3.bin")}


def _is_pinned(path: Path, name: str) -> bool:
    import hashlib
    from tools.espressif.safeboot.safeboot_migration import PINNED_ARTIFACTS
    return path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == PINNED_ARTIFACTS[name]["sha256"]


# The files of the release pinned now: a bundle left by a run before the
# firmware the tool installs changed holds the earlier one's, which a bundle
# built now refuses - not a failure of the code under test.
HAVE_OFFICIAL = all(_is_pinned(OFFICIAL[file], name) for name, file in
                    (("factory", "tasmota32c3.factory.bin"), ("safeboot", "tasmota32c3-safeboot.bin"),
                     ("app", "tasmota32c3.bin")))


def serve_through_pipe(arguments: list[str], data: bytes, until: bytes) -> bytes:
    """What serve_safeboot_migration.py says, given `data` on stdin, until it
    says `until` (or 15 s). On a port free when picked - which something else,
    another test running at the same time, can take before the server binds
    it: a server that could not bind is started again on another."""
    for _ in range(5):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, "-u", str(ROOT / "tools" / "serve_safeboot_migration.py"), *arguments,
             "--listen-ip", "127.0.0.1", "--listen-port", str(port)],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        process.stdin.write(data)
        process.stdin.close()
        output, deadline = b"", time.monotonic() + 15
        while time.monotonic() < deadline and until not in output:
            chunk = process.stdout.read1(4096) if hasattr(process.stdout, "read1") else b""
            if not chunk:
                break
            output += chunk
        process.terminate()
        process.wait(timeout=10)
        if b"could not bind server" not in output:
            break
    return output


def keep_memory_artifacts_to_the_test(test: unittest.TestCase) -> None:
    """The recovery image handed over in memory goes with the test: left
    behind, it would be read for the next test's bundle in this process."""
    patcher = mock.patch.dict(safeboot_migration._MEMORY_ARTIFACTS, clear=True)
    patcher.start()
    test.addCleanup(patcher.stop)


@unittest.skipUnless(HAVE_OFFICIAL, "needs the official release files from a real bundle")
class MemoryOnlyBundleTests(unittest.TestCase):
    def setUp(self):
        keep_memory_artifacts_to_the_test(self)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.out = Path(directory.name)
        with mock.patch("builtins.print"):
            self.manifest, self.image = bundle.build_bundle(
                "TestNet-2G", "correct-horse-battery", self.out,
                OFFICIAL["tasmota32c3.factory.bin"], OFFICIAL["tasmota32c3-safeboot.bin"],
                OFFICIAL["tasmota32c3.bin"])

    def test_the_password_is_nowhere_in_the_bundle(self):
        for path in self.out.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"correct-horse-battery", path.read_bytes(), path.name)
        self.assertFalse((self.out / bundle.RECOVERY_FILE).exists())
        self.assertEqual(bundle.bundle_ssid(self.manifest), "TestNet-2G")

    def test_the_right_password_rebuilds_the_pinned_image(self):
        self.assertEqual(bundle.recovery_image(self.manifest, "TestNet-2G", "correct-horse-battery"),
                         self.image)

    def test_a_wrong_password_is_caught(self):
        with self.assertRaisesRegex(bundle.BundleError, "does not match"):
            bundle.recovery_image(self.manifest, "TestNet-2G", "correct-horse-batterz")

    def test_the_bundle_checks_out_before_the_password_is_known(self):
        bundle.validate_bundle(self.manifest)

    def test_the_server_takes_the_image_through_a_pipe(self):
        output = serve_through_pipe(
            ["preflight", "--manifest", str(self.manifest), "--evidence-dir", str(self.out / "live"),
             "--device-ip", "127.0.0.1", "--recovery-from-stdin", "--no-sensitive-captures", "--wait", "5"],
            self.image, b"/berry/preflight.be")
        self.assertIn(b"/berry/preflight.be", output, output.decode(errors="replace"))
        self.assertFalse((self.out / bundle.RECOVERY_FILE).exists())

    def test_the_plan_server_takes_its_plan_through_a_pipe_alone(self):
        # Not the recovery Safeboot: only the plan, ESPHome with the Wi-Fi written in.
        from tools.autoflash import esphome, plans
        image = plans.esphome(esphome.image_for("TestNet-2G", "correct-horse-battery")).bundle()
        output = serve_through_pipe(
            ["plan", "--manifest", str(self.manifest), "--evidence-dir", str(self.out / "live"),
             "--device-ip", "127.0.0.1", "--plan-from-stdin", "--no-sensitive-captures", "--wait", "5"],
            image, b"/berry/plan.be")
        self.assertIn(b"/berry/plan.be", output, output.decode(errors="replace"))
        for path in self.out.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"correct-horse-battery", path.read_bytes(), path.name)

    def test_an_old_bundle_is_converted_and_its_image_deleted(self):
        manifest = json.loads(self.manifest.read_text())
        del manifest["artifacts"][RECOVERY_ARTIFACT]["memory_only"]
        del manifest["imprinted_safeboot"]["ssid"]
        self.manifest.write_text(json.dumps(manifest))
        (self.out / bundle.RECOVERY_FILE).write_bytes(self.image)
        self.assertTrue(bundle.forget_stored_password(self.manifest))
        self.assertFalse((self.out / bundle.RECOVERY_FILE).exists())
        self.assertEqual(bundle.bundle_ssid(self.manifest), "TestNet-2G")
        bundle.recovery_image(self.manifest, "TestNet-2G", "correct-horse-battery")


@unittest.skipUnless(HAVE_OFFICIAL, "needs the official release files from a real bundle")
class BundleWithoutWifiTests(unittest.TestCase):
    """A5 builds the bundle with no Wi-Fi; E1 writes in what it reads off the plug."""

    def setUp(self):
        keep_memory_artifacts_to_the_test(self)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.out = Path(directory.name)
        with mock.patch("builtins.print"):
            self.manifest, image = bundle.build_bundle(
                None, None, self.out,
                OFFICIAL["tasmota32c3.factory.bin"], OFFICIAL["tasmota32c3-safeboot.bin"],
                OFFICIAL["tasmota32c3.bin"])
        self.assertIsNone(image)

    def test_it_checks_out_and_has_no_network(self):
        bundle.validate_bundle(self.manifest)
        self.assertIsNone(bundle.bundle_ssid(self.manifest))
        with self.assertRaisesRegex(bundle.BundleError, "no Wi-Fi details yet"):
            bundle.recovery_image(self.manifest, "TestNet-2G", "correct-horse-battery")

    def test_pinning_the_wifi_needs_no_download_and_keeps_no_password(self):
        with mock.patch.object(bundle, "obtain") as download:
            image = bundle.pin_wifi(self.manifest, "TestNet-2G", "correct-horse-battery")
        download.assert_not_called()
        self.assertEqual(bundle.bundle_ssid(self.manifest), "TestNet-2G")
        self.assertEqual(bundle.recovery_image(self.manifest, "TestNet-2G", "correct-horse-battery"),
                         image)
        bundle.validate_bundle(self.manifest, image)
        for path in self.out.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"correct-horse-battery", path.read_bytes(), path.name)

    def test_it_can_be_pinned_again_for_another_network(self):
        bundle.pin_wifi(self.manifest, "TestNet-2G", "correct-horse-battery")
        image = bundle.pin_wifi(self.manifest, "Other-2G", "another password")
        self.assertEqual(bundle.bundle_ssid(self.manifest), "Other-2G")
        self.assertEqual(bundle.recovery_image(self.manifest, "Other-2G", "another password"), image)

    def test_unusable_details_leave_the_bundle_as_it_was(self):
        before = self.manifest.read_bytes()
        with self.assertRaises(bundle.BundleError):
            bundle.pin_wifi(self.manifest, "x" * 40, "too long a name")
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_a_bundle_from_another_startup_firmware_is_refused(self):
        manifest = json.loads(self.manifest.read_text())
        manifest["imprinted_safeboot"]["published_sha256"] = "0" * 64
        self.manifest.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "different startup firmware"):
            bundle.validate_bundle(self.manifest)


class RunSecretsTests(unittest.TestCase):
    def test_only_the_secret_files_are_removed(self):
        import tools.autoflash.cli as autoflash
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            run = Path(directory)
            (run / "live").mkdir()
            for name in autoflash.SECRET_FILES:
                (run / name).write_bytes(b"secret")
            kept = [run / "autoflash-state.json", run / "live" / "stage-report.json"]
            for path in kept:
                path.write_text("{}")
            removed = autoflash.forget_run_secrets(run)
            self.assertEqual(len(removed), len(autoflash.SECRET_FILES))
            for name in autoflash.SECRET_FILES:
                self.assertFalse((run / name).exists(), name)
            for path in kept:
                self.assertTrue(path.exists(), path.name)


if __name__ == "__main__":
    unittest.main()
