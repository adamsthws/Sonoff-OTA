"""The sealed Sonoff backup: it opens to exactly what was sealed, with the key
alone, and nothing about it writes over anything or goes anywhere unsealed."""

import argparse
import base64
import os
import secrets
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import private_backup as pb
from tools.autoflash import credentials


def a_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


class SealTests(unittest.TestCase):
    FILES = {"flash.bin": bytes(range(256)) * 64 + b"\xff" * 4096, "efuse.bin": b"\x01" * 1024,
             "report.json": b'{"ok": true}\n'}

    def setUp(self):
        self.secret = pb.base64.urlsafe_b64decode(a_key() + "=")

    def test_it_opens_to_exactly_what_was_sealed(self):
        sealed = pb.seal(self.FILES, self.secret)
        self.assertTrue(sealed.startswith(pb.MAGIC))
        self.assertEqual(pb.opened(sealed, self.secret), self.FILES)

    def test_nothing_of_the_backup_shows_in_the_sealed_file(self):
        sealed = pb.seal(self.FILES, self.secret)
        for data in self.FILES.values():
            self.assertNotIn(data[:64], sealed)
        self.assertNotIn(b"flash.bin", sealed)

    def test_another_key_does_not_open_it(self):
        sealed = pb.seal(self.FILES, self.secret)
        with self.assertRaisesRegex(pb.SealError, "does not open"):
            pb.opened(sealed, secrets.token_bytes(32))

    def test_a_changed_byte_is_found(self):
        sealed = bytearray(pb.seal(self.FILES, self.secret))
        sealed[len(pb.MAGIC) + 40] ^= 1
        with self.assertRaisesRegex(pb.SealError, "changed"):
            pb.opened(bytes(sealed), self.secret)

    def test_its_contents_are_the_same_bytes_every_time(self):
        self.assertEqual(pb.packed(self.FILES), pb.packed(dict(reversed(list(self.FILES.items())))))

    def test_a_file_that_is_not_its_manifests_is_refused(self):
        packed = pb.packed(self.FILES).replace(b'{"ok": true}', b'{"ok": nope}')
        with self.assertRaisesRegex(pb.SealError, "not what was sealed"):
            pb.unpacked(packed)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.backup = self.tmp / "sonoff-backup"
        self.backup.mkdir()
        for name, data in SealTests.FILES.items():
            (self.backup / name).write_bytes(data)
        self.env = mock.patch.dict(os.environ, {pb.KEY_NAME: a_key()})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_command(self, function, **paths):
        with mock.patch("builtins.print"):
            return function(argparse.Namespace(**paths))

    def test_seal_verify_and_unseal_give_the_backup_back(self):
        sealed, out = self.tmp / "b.sealed", self.tmp / "out"
        self.assertEqual(self.run_command(pb.command_seal, backup=self.backup, sealed=sealed), 0)
        self.assertEqual(self.run_command(pb.command_verify, sealed=sealed, backup=self.backup), 0)
        self.assertEqual(self.run_command(pb.command_unseal, sealed=sealed, out=out), 0)
        for name, data in SealTests.FILES.items():
            self.assertEqual((out / name).read_bytes(), data)
            self.assertFalse((out / name).stat().st_mode & stat.S_IWUSR)

    def test_nothing_is_written_over(self):
        sealed = self.tmp / "b.sealed"
        sealed.write_bytes(b"there")
        with self.assertRaisesRegex(pb.SealError, "there already"):
            self.run_command(pb.command_seal, backup=self.backup, sealed=sealed)
        with self.assertRaisesRegex(pb.SealError, "there already"):
            self.run_command(pb.command_unseal, sealed=sealed, out=self.backup)
        self.assertEqual(sealed.read_bytes(), b"there")

    def test_verify_says_when_it_differs(self):
        sealed = self.tmp / "b.sealed"
        self.run_command(pb.command_seal, backup=self.backup, sealed=sealed)
        (self.backup / "report.json").write_bytes(b"{}")
        with self.assertRaisesRegex(pb.SealError, "report.json"):
            self.run_command(pb.command_verify, sealed=sealed, backup=self.backup)

    def test_only_a_sealed_file_is_ever_uploaded(self):
        with mock.patch.object(pb, "oras") as oras:
            with self.assertRaisesRegex(pb.SealError, "only a sealed one"):
                self.run_command(pb.command_upload, sealed=self.backup / "flash.bin")
        oras.assert_not_called()

    def test_a_key_that_is_not_one_is_refused(self):
        with mock.patch.dict(os.environ, {pb.KEY_NAME: "short"}):
            with self.assertRaisesRegex(pb.SealError, "not a key"):
                pb.key()

    def test_a_new_key_is_never_made_over_an_old_one(self):
        env = self.tmp / ".env"
        env.write_text("EWELINK_EMAIL=someone@example.com")
        with mock.patch.object(credentials, "ENV_FILE", env), mock.patch.dict(os.environ, clear=True), \
             mock.patch("builtins.print"):
            self.assertEqual(pb.command_key(argparse.Namespace()), 0)
            made = credentials.parse(env.read_text())
            self.assertEqual(made["EWELINK_EMAIL"], "someone@example.com")
            self.assertEqual(len(base64.urlsafe_b64decode(made[pb.KEY_NAME] + "=")), 32)
            self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600)
            with self.assertRaisesRegex(pb.SealError, "set already"):
                pb.command_key(argparse.Namespace())
            self.assertEqual(credentials.parse(env.read_text())[pb.KEY_NAME], made[pb.KEY_NAME])

    def test_a_run_still_loads_a_env_with_the_key_and_keeps_it_from_its_log(self):
        env = self.tmp / ".env"
        env.write_text(f"{pb.KEY_NAME}={a_key()}\n")
        with mock.patch.object(credentials.output, "private") as private:
            self.assertIn(pb.KEY_NAME, credentials.load(env))
        self.assertIn("backup-key", [call.args[1] for call in private.call_args_list])

    def restore_into(self, captures):
        sealed = pb.seal(pb.backup_files(self.backup), pb.key())
        return mock.patch.multiple(pb, fetched=mock.Mock(return_value=sealed)), \
            mock.patch.object(pb.plugs, "PLUGS", captures / "plugs")

    def test_restore_files_the_backup_where_the_emulator_looks(self):
        (self.tmp / "plug.json").write_text('{"model": "S60TPG"}')
        efuse = bytearray(1024)
        efuse[0x18:0x1E] = bytes.fromhex("123456abcdef")[::-1]
        (self.backup / "efuse.bin").write_bytes(bytes(efuse))
        fetch, where = self.restore_into(self.tmp / "captures")
        with fetch, where:
            self.assertEqual(pb.restore(), "12:34:56:ab:cd:ef")
            filed = self.tmp / "captures/plugs/12-34-56-ab-cd-ef"
            self.assertEqual((filed / "sonoff-backup/flash.bin").read_bytes(), SealTests.FILES["flash.bin"])
            self.assertEqual((filed / "plug.json").read_text(), '{"model": "S60TPG"}')
            self.assertFalse((filed / "sonoff-backup/flash.bin").stat().st_mode & stat.S_IWUSR)
            self.assertEqual(pb.restore(), "12:34:56:ab:cd:ef")  # there already, as sealed: left alone

    def test_restore_never_writes_over_a_backup_that_differs(self):
        efuse = bytearray(1024)
        efuse[0x18:0x1E] = bytes.fromhex("123456abcdef")[::-1]
        (self.backup / "efuse.bin").write_bytes(bytes(efuse))
        fetch, where = self.restore_into(self.tmp / "captures")
        other = self.tmp / "captures/plugs/12-34-56-ab-cd-ef/sonoff-backup"
        other.mkdir(parents=True)
        (other / "flash.bin").write_bytes(b"another")
        (other / "efuse.bin").write_bytes(bytes(efuse))
        with fetch, where:
            with self.assertRaisesRegex(pb.SealError, "not the sealed backup"):
                pb.restore()
        self.assertEqual((other / "flash.bin").read_bytes(), b"another")

    def test_the_emulator_never_hands_the_key_to_a_run(self):
        script = (pb.ROOT / "tools/emulator/unattended-test.sh").read_text()
        self.assertIn("BACKUP_KEY\\)' \"$ROOT/.env\"", script)


if __name__ == "__main__":
    unittest.main()
