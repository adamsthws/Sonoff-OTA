"""Secrets and real identifiers are kept out of Git (tools/leak_scan.py)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import leak_scan as ls


def scanner(own=None, allow=()):
    with mock.patch.object(ls, "own_values", return_value=dict(own or {})), \
         mock.patch.object(ls, "allowed", return_value=set(allow)):
        return ls.Scanner()


class ShapeTests(unittest.TestCase):
    def found(self, text, **kwargs):
        s = scanner(**kwargs)
        s.text("f:1", text)
        return [f.what for f in s.findings]

    def test_a_mac_that_could_be_a_real_devices_is_found(self):
        self.assertTrue(self.found("router 58:cf:79:12:34:56 here"))
        self.assertTrue(self.found("58-CF-79-12-34-56"))

    def test_made_up_and_listed_macs_are_not(self):
        for text in ("12:34:56:ab:cd:ef", "aa:bb:cc:dd:ee:ff", "02:00:00:00:00:01",   # locally administered
                     "ff:ff:ff:ff:ff:ff", "01:00:5e:00:00:fb"):                        # broadcast, multicast
            self.assertEqual(self.found(text), [], text)
        self.assertEqual(self.found("58:cf:79:01:02:03", allow={"58:cf:79:01:02:03"}), [])

    def test_hex_that_is_not_a_mac_is_not(self):
        self.assertEqual(self.found("sha256 5c4181c756fb0a98c107a4d53389536f878b69fedbbce47168b07458122f53e8"), [])
        self.assertEqual(self.found("2026-10-23T12:34:56Z"), [])

    def test_a_uuid_that_could_be_a_device_apikey_or_id_is_found(self):
        uuid = "deadbeef-0000-1111-2222-" + "333344445555"   # split so the tree scan ignores this file
        self.assertTrue(self.found("apikey " + uuid))
        self.assertEqual(self.found("apikey " + uuid, allow={uuid}), [])   # a listed example

    def test_a_uuid_run_on_into_more_hex_is_not(self):
        uuid = "deadbeef-0000-1111-2222-" + "333344445555"
        self.assertEqual(self.found("x" + uuid + "0"), [])

    def test_disassembly_and_decompiler_output_are_found(self):
        what = "disassembly or decompiler output (commit the tool that makes it, not its dump)"
        disasm = "0x04203c:  " + "4ff00008  mov.w\tr0, #8"   # the capstone line shape
        banner = "### FUNCTION 0x" + "04203c ###"            # the angr-decompiler banner
        self.assertIn(what, self.found(disasm))
        self.assertIn(what, self.found(banner))

    def test_an_inline_address_in_prose_is_not_disassembly(self):
        self.assertEqual(self.found("the start-cloud handler 0x2c204 has three callers"), [])
        self.assertEqual(self.found("0x2c204  if (*0x403e31 != 0)   // a note, not a dump"), [])

    def test_tokens_and_keys_are_found(self):
        key = "PRIVATE " + "KEY-----"   # not written whole: the scan would find this file
        for text, what in (("-----BEGIN " + key, "a private key"),
                           ("-----BEGIN OPENSSH " + key, "a private key"),
                           ("ghp_" + "a" * 36, "a GitHub token"),
                           ("AKIA" + "ABCDEFGHIJKLMNOP", "an AWS access key")):
            self.assertIn(what, self.found(text), text)


class OwnValueTests(unittest.TestCase):
    def test_a_value_is_found_whole_not_inside_a_longer_one(self):
        s = scanner(own={"998877": "a plug's MAC", "Fake-Home-Wifi": "a Wi-Fi name"})
        s.text("a:1", "the plug tasmota-998877")                       # whole: found
        s.text("b:1", "hash 1f9988779e")                               # inside hex: not
        s.text("c:1", "FAKE-HOME-WIFI")                                # any case
        self.assertEqual(sorted(f.where for f in s.findings), ["a:1", "c:1"])

    def test_values_are_learnt_from_this_computer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plug = root / "captures/plugs/58-cf-79-99-88-77"
            (plug / "runs/1").mkdir(parents=True)
            (plug / "plug.json").write_text(json.dumps({"mac": "58:cf:79:99:88:77", "ewelink_id": "1000fake99"}))
            (plug / "runs/1/autoflash-state.json").write_text(json.dumps(
                {"inputs": {"plug_ip": "10.9.8.7", "listen_ip": "192.168.60.10"}}))
            (root / "captures/settings.json").write_text(json.dumps({"ewelink_email": "someone@example.org"}))
            (root / ".env").write_text("EWELINK_PASSWORD=Fake-Passw0rd\n")
            (root / ".leak-scan.local").write_text("# mine\nmy-router-name\n")
            with mock.patch.object(ls, "CAPTURES", root / "captures"), \
                 mock.patch.object(ls, "LOCAL_FILE", root / ".leak-scan.local"), \
                 mock.patch.object(ls, "ALLOW_FILE", root / "none"), \
                 mock.patch("tools.autoflash.credentials.ENV_FILE", root / ".env"):
                values = ls.own_values()
        self.assertEqual(values["58:cf:79:99:88:77"], "a plug's MAC")
        self.assertEqual(values["998877"], "a plug's MAC")
        self.assertIn("1000fake99", values)
        self.assertIn("someone@example.org", values)
        self.assertIn("Fake-Passw0rd", values)
        self.assertIn("my-router-name", values)
        self.assertIn("10.9.8.7", values)
        self.assertNotIn("192.168.60.10", values)   # the emulator's own network

    def test_a_mac_in_a_file_as_six_bytes_either_way_round(self):
        s = scanner(own={"58cf79998877": "a plug's MAC"})
        s.raw("a.bin", b"x" + bytes.fromhex("58cf79998877") + b"y")
        s.raw("b.bin", b"x" + bytes.fromhex("778899" "79cf58") + b"y")
        s.raw("c.bin", b"nothing here")
        self.assertEqual(sorted(f.where for f in s.findings), ["a.bin", "b.bin"])


class FileNameTests(unittest.TestCase):
    def test_a_packet_capture_is_refused_by_its_name(self):
        s = scanner()
        s.filename("airkiss.pcap", "airkiss.pcap")
        s.filename("tools/beken/grab.PCAPNG", "tools/beken/grab.PCAPNG")
        s.filename("airkiss_replay.py", "airkiss_replay.py")   # a tool, not a capture
        self.assertEqual(sorted(f.where for f in s.findings),
                         ["airkiss.pcap", "tools/beken/grab.PCAPNG"])


class PatchTests(unittest.TestCase):
    def test_only_added_lines_are_looked_at_by_file_and_line(self):
        s = scanner(own={"Fake-Passw0rd": "a value in .env"})
        ls.scan_patch(s, "diff --git a/x.txt b/x.txt\n--- a/x.txt\n+++ b/x.txt\n@@ -1,2 +10,2 @@\n"
                         "-Fake-Passw0rd was here\n+fine\n+still Fake-Passw0rd\n", "")
        self.assertEqual([(f.where, f.what) for f in s.findings], [("x.txt:11", "a value in .env")])

    def test_a_finding_stops_it_and_says_where_never_what(self):
        s = scanner(own={"Fake-Passw0rd": "a value in .env"})
        s.text("x.txt:1", "Fake-Passw0rd")
        with mock.patch("sys.stderr") as err:
            self.assertEqual(s.report(), 1)
        said = "".join(call.args[0] for call in err.write.call_args_list)
        self.assertIn("x.txt:1", said)
        self.assertNotIn("Fake-Passw0rd", said)

    def test_the_repository_as_it_stands_holds_nothing(self):
        # Shapes only, here: this computer's own values are the hooks' to find.
        s = scanner()
        s.allow = ls.allowed()
        ls.command_tree(s)
        self.assertEqual([(f.where, f.what) for f in s.findings], [])


if __name__ == "__main__":
    unittest.main()
