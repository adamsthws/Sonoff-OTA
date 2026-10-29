"""The Beken conversion tooling in tools/beken: the pieces a conversion's data
passes through - the OpenBeken config and its CRC, the EasyFlash KV40 store, the
Wi-Fi migration, the FOTA container and its server, the stock-app extraction
and the per-device backup. The emulator drivers beside them need the
BekenEmulator fork and a dump, and are tested in the emulator, not here."""

import contextlib
import hashlib
import http.client
import io
import json
import socket
import struct
import sys
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[2] / "tools" / "beken"
if str(TOOLS) not in sys.path:
    sys.path.append(str(TOOLS))   # the tools import each other by bare name

import ef40_dump  # noqa: E402
import obk_config  # noqa: E402
import backup  # noqa: E402
import ef40  # noqa: E402
import fota_wrap as fota  # noqa: E402
import stock_app  # noqa: E402
import wifi_migrate  # noqa: E402

MAC = "12:34:56:ab:cd:ef"   # made up, like every identifier here


def kv40_store(nodes, size=0x1000):
    """An erased store holding these (name, value) nodes in order, 4-byte aligned."""
    store = bytearray(b"\xff" * size)
    pos = 0
    for name, value in nodes:
        node = ef40.encode_node(name, value)
        store[pos:pos + len(node)] = node
        pos += (len(node) + 3) & ~3
    return store


def rbl_header(name=b"app", version=b"1.0.2", raw_hash=0, size_raw=0):
    """A 96-byte rt_ota RBL header as stock_app and fota_wrap read it."""
    h = bytearray(96)
    h[0:4] = b"RBL\x00"
    h[12:12 + len(name)] = name
    h[28:28 + len(version)] = version
    struct.pack_into("<II", h, 80, raw_hash, size_raw)
    struct.pack_into("<I", h, 92, zlib.crc32(bytes(h[:92])) & 0xFFFFFFFF)
    return bytes(h)


class TinyCrc8Tests(unittest.TestCase):
    """OpenBeken's Tiny_CRC8 as the BK7238 build runs it: a signed `char crc`, so
    the shift sign-extends. A plain CRC-8/MAXIM gives other values."""

    def test_the_values_worked_by_hand_from_the_c(self):
        # One byte 0x01: bit 0 sets 0x8c; from then on bit 7 is set, so each
        # shift brings in a 1 - 0xc6, 0xe3, 0x7d, 0xb2, 0xd9, 0x60, 0x30.
        self.assertEqual(obk_config.tiny_crc8(b"\x01"), 0x30)
        # An unsigned CRC-8/MAXIM would give 0x5e here and 0xa1 for the check string.
        self.assertNotEqual(obk_config.tiny_crc8(b"\x01"), 0x5E)
        self.assertEqual(obk_config.tiny_crc8(b"123456789"), 0x63)

    def test_zeros_leave_it_at_zero(self):
        self.assertEqual(obk_config.tiny_crc8(b""), 0)
        self.assertEqual(obk_config.tiny_crc8(bytes(100)), 0)

    def test_every_byte_counts(self):
        data = bytes(range(256))
        crc = obk_config.tiny_crc8(data)
        changed = bytearray(data)
        changed[200] ^= 0x01
        self.assertNotEqual(obk_config.tiny_crc8(bytes(changed)), crc)
        self.assertNotEqual(obk_config.tiny_crc8(data[:-1]), crc)


class BuildConfigTests(unittest.TestCase):
    def test_the_layout_openbeken_reads(self):
        blob = obk_config.build_config("HomeNet", "secret-pass")
        self.assertEqual(len(blob), 3584)
        self.assertEqual(blob[0:3], b"CFG")
        self.assertEqual(struct.unpack_from("<i", blob, 4)[0], 5)
        self.assertEqual(struct.unpack_from("<H", blob, 0x10)[0], 1)   # changeCounter: written once
        self.assertEqual(blob[0x14:0x14 + 64], b"HomeNet".ljust(64, b"\x00"))
        self.assertEqual(blob[0x54:0x54 + 64], b"secret-pass".ljust(64, b"\x00"))
        self.assertEqual(blob[3], obk_config.tiny_crc8(blob[4:]))
        self.assertEqual(blob[0x94:], bytes(3584 - 0x94))   # nothing else set

    def test_bytes_and_str_credentials_are_the_same_blob(self):
        self.assertEqual(obk_config.build_config("HomeNet", "secret-pass"),
                         obk_config.build_config(b"HomeNet", b"secret-pass"))

    def test_a_credential_needs_its_terminator(self):
        obk_config.build_config("s" * 63, "p" * 63)
        with self.assertRaises(ValueError):
            obk_config.build_config("s" * 64, "p")
        with self.assertRaises(ValueError):
            obk_config.build_config("s", "p" * 64)

    def test_a_template_keeps_every_other_field(self):
        template = bytes(range(256)) * 14   # 3584 bytes of anything
        blob = obk_config.build_config("HomeNet", "pw", template=template)
        self.assertEqual(len(blob), 3584)
        self.assertEqual(blob[0:3], b"CFG")
        self.assertEqual(struct.unpack_from("<i", blob, 4)[0], 5)
        self.assertEqual(blob[8:0x14], template[8:0x14])       # flags and counters as they were
        self.assertEqual(blob[0x94:], template[0x94:])
        self.assertEqual(blob[0x14:0x14 + 7], b"HomeNet")
        self.assertEqual(blob[3], obk_config.tiny_crc8(blob[4:]))

    def test_a_short_template_is_padded_to_the_size(self):
        blob = obk_config.build_config("HomeNet", "pw", template=b"CFG" + bytes(100))
        self.assertEqual(len(blob), 3584)
        self.assertEqual(blob[3], obk_config.tiny_crc8(blob[4:]))


class Kv40NodeTests(unittest.TestCase):
    def test_a_node_byte_for_byte(self):
        body = b"\x01\xff\xff\xff" + struct.pack("<I", 1) + b"k" + b"v"
        expected = (b"KV40" + struct.pack("<I", 30) + struct.pack("<I", zlib.crc32(body))
                    + body + b"\x00\x00" + b"\xff" * 6)
        self.assertEqual(ef40.encode_node("k", "v"), expected)
        self.assertEqual(ef40.encode_node(b"k", b"v"), expected)

    def test_a_store_reads_back_with_the_last_write_winning(self):
        nodes = [("wifi-ssid", b"OldNet\x00"), ("elecCalibCoef", bytes(12)), ("wifi-ssid", b"HomeNet\x00"),
                 ("k", b"v"), ("n" * 64, b"the longest name"), ("blob", bytes(0x2000))]   # the largest value
        store = kv40_store(nodes, size=0x3000)
        kv = ef40.parse_store(store, region=(0, len(store)))
        self.assertEqual(kv, {**dict(nodes[1:]), "wifi-ssid": b"HomeNet\x00"})

    def test_only_the_region_is_read(self):
        store = kv40_store([("wifi-ssid", b"HomeNet")])
        flash = bytes(store) + bytes(store)
        self.assertEqual(ef40.parse_store(flash, region=(len(store), 2 * len(store))),
                         {"wifi-ssid": b"HomeNet"})
        self.assertEqual(ef40.parse_store(flash, region=(0, 8)), {})

    def test_an_erased_store_has_no_keys(self):
        self.assertEqual(ef40.parse_store(b"\xff" * 0x100, region=(0, 0x100)), {})

    def test_a_corrupt_node_is_skipped(self):
        store = kv40_store([("wifi-ssid", b"HomeNet"), ("wifi-pass", b"secret")])
        store[8] ^= 0x01                 # a bit of the first node's stored CRC flipped
        self.assertEqual(ef40.parse_store(store, region=(0, len(store))), {"wifi-pass": b"secret"})

    def test_nonsense_lengths_are_skipped(self):
        for name_len, value_len in ((0, 1), (65, 1), (1, 0x2001)):
            node = bytearray(ef40.encode_node("k", "v"))
            node[12] = name_len
            struct.pack_into("<I", node, 16, value_len)
            store = bytes(node) + ef40.encode_node("ok", "yes")
            self.assertEqual(ef40.parse_store(store, region=(0, len(store))), {"ok": b"yes"},
                             (name_len, value_len))

    def test_a_node_cut_off_by_the_end_of_the_store_is_skipped(self):
        node = ef40.encode_node("wifi-ssid", b"HomeNet")
        for cut in (len(node) - 10, 19, 4):
            self.assertEqual(ef40.parse_store(node[:cut], region=(0, cut)), {}, cut)

    def test_the_dump_tool_reads_the_same_store(self):
        store = kv40_store([("wifi-ssid", b"HomeNet"), ("wifi-pass", b"secret"), ("wifi-ssid", b"NewNet")])
        store[8] ^= 0x01                 # the first wifi-ssid's CRC fails: stale
        garbage = bytearray(ef40.encode_node("k", "v"))
        garbage[12] = 0                  # a node with no name, before them all
        store = garbage + store
        nodes = ef40_dump.parse(bytes(store))
        self.assertEqual({k: v for k, (v, ok, _n) in nodes.items() if ok},
                         ef40.parse_store(store, region=(0, len(store))))
        self.assertEqual(nodes["wifi-pass"][1:], (True, 1))
        self.assertEqual(nodes["wifi-ssid"][0], b"NewNet")

    def test_the_dump_tool_lists_valid_keys_unless_asked_for_all(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        flash = bytearray(b"\xff" * 0x3FD000)
        flash[0x3E0000:0x3E0000 + 0x1000] = kv40_store([("wifi-ssid", b"HomeNet"), ("elecCalibCoef", bytes(12)),
                                                        ("blob", bytes(range(40)))])
        flash[0x3E0000 + 8] ^= 0x01      # wifi-ssid stale
        flash[0x3F6000:0x3F6000 + 0x100] = kv40_store([("sys-key", b"on")], size=0x100)
        (tmp / "flash.bin").write_bytes(flash)

        def run(*args):
            with mock.patch.object(sys, "argv", ["ef40_dump.py", str(tmp / "flash.bin"), *args]), \
                 contextlib.redirect_stdout(io.StringIO()) as log:
                ef40_dump.main()
            return log.getvalue()

        log = run()
        self.assertNotIn("wifi-ssid", log)
        self.assertIn("elecCalibCoef", log)
        self.assertIn("hex  " + bytes(12).hex(), log)                    # a short value, whole
        self.assertIn("hex  %s... (40 bytes)" % bytes(range(16)).hex(), log)   # a long one, cut
        self.assertIn("str  'on'", log)
        self.assertIn("app  (0x3e0000-0x3ebfff) : 3 distinct keys", log)
        log = run("--all")
        self.assertIn("wifi-ssid", log)
        self.assertIn("[CRC MISMATCH/stale]", log)

    def test_the_dump_tool_flags_a_stale_node(self):
        store = kv40_store([("wifi-pass", b"secret")])
        store[8] ^= 0x01
        value, ok, _n = ef40_dump.parse(bytes(store))["wifi-pass"]
        self.assertEqual(value, b"secret")
        self.assertFalse(ok)


class ProvisionTests(unittest.TestCase):
    REGION = (0x100, 0x100 + 0x400)

    def flash(self, nodes):
        data = bytearray(b"\xff" * 0x600)
        store = kv40_store(nodes, size=0x400)
        data[self.REGION[0]:self.REGION[1]] = store
        return data

    def test_the_credentials_follow_the_last_node_without_a_gap(self):
        # Node lengths of 56 and 30 bytes: one already 4-byte aligned, one not;
        # the second has a one-character name. The firmware walks the store
        # node by node, so the new ones must follow on, aligned, with no gap.
        existing = [("elecCalibCoef", bytes(15)), ("k", b"v")]
        data = self.flash(existing)
        before = bytes(data)
        ef40.provision(data, {"wifi-ssid": "HomeNet", "wifi-pass": "secret"}, region=self.REGION)
        kv = ef40.parse_store(data, region=self.REGION)
        self.assertEqual(kv, {**dict(existing), "wifi-ssid": b"HomeNet", "wifi-pass": b"secret"})
        end = self.REGION[0] + sum((len(ef40.encode_node(*node)) + 3) & ~3 for node in existing)   # 56 + 32
        self.assertEqual(data[:end], before[:end])                                 # nothing before touched
        added = ef40.encode_node("wifi-ssid", "HomeNet")                        # 44 bytes: aligned already
        added += ef40.encode_node("wifi-pass", "secret")                        # 43 bytes, padded to 44
        self.assertEqual(data[end:end + len(added)], added)
        self.assertEqual(data[end + len(added):end + len(added) + 4], b"\xff" * 4)  # erased after them
        self.assertEqual(data[self.REGION[1]:], before[self.REGION[1]:])          # nothing past the store

    def test_an_erased_store_starts_at_its_beginning(self):
        data = self.flash([])
        ef40.provision(data, {"wifi-ssid": "HomeNet"}, region=self.REGION)
        self.assertEqual(data[self.REGION[0]:self.REGION[0] + 4], b"KV40")
        self.assertEqual(ef40.parse_store(data, region=self.REGION), {"wifi-ssid": b"HomeNet"})

    def test_kv40_inside_a_value_does_not_move_the_end(self):
        # A value holding the magic, followed by what reads as a huge length:
        # taken as a node, the "end" would land far past the store and the
        # credentials would be written over whatever is there.
        for value_len in (0x7FFFFFF0, 0x1000):   # one absurd, one plausible but past the store's end
            decoy = b"KV40" + b"\xff" * 8 + b"\x08\xff\xff\xff" + struct.pack("<I", value_len) + b"x" * 8
            data = self.flash([("note", decoy)])
            before = bytes(data)
            ef40.provision(data, {"wifi-ssid": "HomeNet"}, region=self.REGION)
            self.assertEqual(data[self.REGION[1]:], before[self.REGION[1]:], hex(value_len))
            self.assertEqual(ef40.parse_store(data, region=self.REGION),
                             {"note": decoy, "wifi-ssid": b"HomeNet"}, hex(value_len))

    # A "big" node's length is 31 + its value's; the new wifi-ssid node is 44 bytes.
    def test_a_full_store_refuses_rather_than_writing_past_it(self):
        data = self.flash([("big", bytes(0x400 - 31 - 40))])   # 984 bytes: 40 left, 44 needed
        before = bytes(data)
        with self.assertRaises(ValueError):
            ef40.provision(data, {"wifi-ssid": "HomeNet"}, region=self.REGION)
        self.assertEqual(data, before)

    def test_what_exactly_fits_is_written(self):
        data = self.flash([("big", bytes(0x400 - 31 - 44))])   # 980 bytes: 44 left, 44 needed
        ef40.provision(data, {"wifi-ssid": "HomeNet"}, region=self.REGION)
        self.assertEqual(ef40.parse_store(data, region=self.REGION)["wifi-ssid"], b"HomeNet")
        self.assertEqual(data[self.REGION[1] - 44:self.REGION[1]], ef40.encode_node("wifi-ssid", "HomeNet"))


class WifiMigrateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def stock(self, creds):
        data = bytearray(b"\xff" * ef40.APP_STORE[1])
        if creds:
            ef40.provision(data, creds)
        path = self.tmp / "stock.bin"
        path.write_bytes(data)
        return path

    def test_the_credentials_are_read_without_their_terminators(self):
        path = self.stock({"wifi-ssid": b"HomeNet\x00", "wifi-pass": b"secret\x00"})
        self.assertEqual(wifi_migrate.read_stock_creds(path), (b"HomeNet", b"secret"))

    def test_an_unprovisioned_plug_has_none(self):
        self.assertEqual(wifi_migrate.read_stock_creds(self.stock({})), (None, None))
        self.assertEqual(wifi_migrate.read_stock_creds(self.stock({"wifi-ssid": b"HomeNet"})),
                         (None, None))

    def test_the_config_lands_at_the_offset_and_the_rest_is_kept(self):
        stock = self.stock({"wifi-ssid": b"HomeNet", "wifi-pass": b"secret"})
        obk = self.tmp / "obk.bin"
        image = bytes(range(256)) * 32      # 8 KiB of an image
        obk.write_bytes(image)
        out = self.tmp / "out.bin"
        result = wifi_migrate.migrate(stock, obk, out, 0x1000)
        blob = obk_config.build_config("HomeNet", "secret")
        self.assertEqual(result, (b"HomeNet", b"secret", blob[3]))
        got = out.read_bytes()
        self.assertEqual(got[:0x1000], image[:0x1000])
        self.assertEqual(got[0x1000:0x1000 + 3584], blob)
        self.assertEqual(got[0x1000 + 3584:], image[0x1000 + 3584:])

    def test_an_image_shorter_than_the_offset_is_padded_erased(self):
        stock = self.stock({"wifi-ssid": b"HomeNet", "wifi-pass": b"secret"})
        obk = self.tmp / "obk.bin"
        obk.write_bytes(bytes(100))
        out = self.tmp / "out.bin"
        wifi_migrate.migrate(stock, obk, out, 0x200)
        got = out.read_bytes()
        self.assertEqual(len(got), 0x200 + 3584)
        self.assertEqual(got[100:0x200], b"\xff" * (0x200 - 100))
        self.assertEqual(got[0x200:], obk_config.build_config("HomeNet", "secret"))

    def test_a_template_reaches_the_config(self):
        stock = self.stock({"wifi-ssid": b"HomeNet", "wifi-pass": b"secret"})
        obk = self.tmp / "obk.bin"
        obk.write_bytes(bytes(0x100))
        template = self.tmp / "template.bin"
        template.write_bytes(bytes([0xAB]) * 3584)
        out = self.tmp / "out.bin"
        wifi_migrate.migrate(stock, obk, out, 0, template)
        self.assertEqual(out.read_bytes()[:3584],
                         obk_config.build_config("HomeNet", "secret", template=bytes([0xAB]) * 3584))

    def test_nothing_is_written_for_an_unprovisioned_plug(self):
        stock = self.stock({})
        obk = self.tmp / "obk.bin"
        obk.write_bytes(bytes(0x100))
        out = self.tmp / "out.bin"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(wifi_migrate.migrate(stock, obk, out, 0))
        self.assertFalse(out.exists())
        self.assertIn("unprovisioned", err.getvalue())

    def test_the_command_line_takes_a_hex_offset(self):
        stock = self.stock({"wifi-ssid": b"HomeNet", "wifi-pass": b"secret"})
        obk = self.tmp / "obk.bin"
        obk.write_bytes(bytes(0x100))
        out = self.tmp / "out.bin"
        argv = ["wifi_migrate.py", "--stock", str(stock), "--obk", str(obk), "--out", str(out),
                "--cfg-offset", "0x800"]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()) as log:
            wifi_migrate.main()
        self.assertEqual(out.read_bytes()[0x800:0x800 + 3], b"CFG")
        self.assertIn("ssid=b'HomeNet'", log.getvalue())


class FotaWrapTests(unittest.TestCase):
    RBL = rbl_header(version=b"1.18.319") + bytes(range(256)) * 4

    def test_the_version_comes_from_the_rbl_header(self):
        self.assertEqual(fota._rbl_version(self.RBL), "1.18.319")
        self.assertEqual(fota._rbl_version(self.RBL[:96]), "1.18.319")   # a header alone is enough

    def test_only_an_rbl_is_wrapped(self):
        with self.assertRaises(ValueError):
            fota._rbl_version(b"ELF\x00" + bytes(200))
        with self.assertRaises(ValueError):
            fota._rbl_version(b"RBL\x00" + bytes(50))     # too short for a header
        with self.assertRaises(ValueError):
            fota.wrap(bytes(200))

    def test_the_container_the_firmware_decodes(self):
        c = fota.wrap(self.RBL)
        self.assertEqual(len(c), 116 + len(self.RBL))
        self.assertEqual(struct.unpack_from("<I", c, 0)[0], 0x1BEEF11E)
        self.assertEqual(struct.unpack_from("<H", c, 12)[0], 0)
        trailer, desc = c[16:40], c[40:116]
        self.assertEqual(trailer[1], 1)                                   # one descriptor
        self.assertEqual(trailer[2:20], b"1.18.319".ljust(18, b"\x00"))
        self.assertEqual(struct.unpack_from(">I", trailer, 20)[0], zlib.crc32(trailer[:20]))
        self.assertEqual(desc[:18], b"FWSW-01-S61-BK7238")
        self.assertEqual(desc[0x20:0x30], b"1.18.319".ljust(16, b"\x00"))
        self.assertEqual(struct.unpack_from(">I", desc, 0x30)[0], 116 - 16)   # payload at 116, base 16
        self.assertEqual(struct.unpack_from(">I", desc, 0x34)[0], len(self.RBL))
        self.assertEqual(struct.unpack_from(">I", desc, 0x3C)[0], zlib.crc32(desc[:0x3C]))
        self.assertEqual(c[116:], self.RBL)

    def test_a_given_name_and_version_are_used(self):
        c = fota.wrap(self.RBL, name="OTHER", version="26.10.29-bk7238")
        self.assertEqual(c[40:45], b"OTHER")
        self.assertEqual(c[45:58], bytes(13))
        self.assertEqual(c[18:33], b"26.10.29-bk7238")
        self.assertEqual(c[40 + 0x20:40 + 0x2F], b"26.10.29-bk7238")
        self.assertEqual(c[40 + 0x2F], 0)

    def test_a_version_that_does_not_fit_is_refused(self):
        c = fota.wrap(self.RBL, version="v" * 15)
        self.assertEqual(len(c), 116 + len(self.RBL))
        for version in ("v" * 16, "", "1.0.2é"):
            with self.assertRaises(ValueError, msg=repr(version)):
                fota.wrap(self.RBL, version=version)

    def test_the_server_answers_each_range_as_the_plug_fetches(self):
        container = fota.wrap(self.RBL)
        srv = fota._server(container, "127.0.0.1", 0)
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        self.addCleanup(conn.close)
        with contextlib.redirect_stderr(io.StringIO()):
            got = []
            for rng, want in (("bytes=0-115", (0, 115)), ("bytes=116-215", (116, 215)),
                              ("bytes=1000-", (1000, len(container) - 1)),
                              ("bytes=0-99999", (0, len(container) - 1)), (None, (0, len(container) - 1))):
                headers = {"Range": rng} if rng else {}
                conn.request("GET", "/user1.bin", headers=headers)
                r = conn.getresponse()
                body = r.read()
                got.append((r.status, r.getheader("Content-Range"), r.will_close, body))
                self.assertEqual(body, container[want[0]:want[1] + 1], rng)
        for status, content_range, will_close, body in got:
            self.assertEqual(status, 206)
            self.assertFalse(will_close)       # keep-alive: the plug reads every range on one connection
            self.assertEqual(content_range, "bytes %d-%d/%d" % (
                int(content_range.split()[1].split("-")[0]),
                int(content_range.split()[1].split("-")[0]) + len(body) - 1, len(container)))

    def test_the_response_head_is_what_the_plugs_parser_reads(self):
        container = fota.wrap(self.RBL)
        srv = fota._server(container, "127.0.0.1", 0)
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        with contextlib.redirect_stderr(io.StringIO()), socket.create_connection(srv.server_address, timeout=10) as s:
            s.sendall(b"GET /user1.bin HTTP/1.1\r\nHost: plug\r\nRange: bytes=16-39\r\n\r\n")
            got = b""
            while b"\r\n\r\n" not in got or len(got) < got.index(b"\r\n\r\n") + 4 + 24:
                got += s.recv(4096)
        head, body = got.split(b"\r\n\r\n", 1)
        self.assertEqual(body, container[16:40])
        lines = head.split(b"\r\n")
        self.assertEqual(lines[0], b"HTTP/1.1 206 Partial Content")
        for line in (b"Content-Type: application/octet-stream", b"Content-Range: bytes 16-39/%d" % len(container),
                     b"Content-Length: 24", b"Connection: keep-alive"):
            self.assertIn(line, lines)

    def test_the_command_writes_the_container_beside_the_input(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        rbl = tmp / "OpenBK7238_1.18.319.rbl"
        rbl.write_bytes(self.RBL)
        with contextlib.redirect_stdout(io.StringIO()) as log:
            self.assertEqual(fota.main([str(rbl)]), 0)
        container = (tmp / "OpenBK7238_1.18.319.fota").read_bytes()
        self.assertEqual(container, fota.wrap(self.RBL))
        self.assertIn(hashlib.sha256(container).hexdigest(), log.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            fota.main([str(rbl), "-o", str(tmp / "out.fota"), "--version", "2.0.0", "--name", "OTHER"])
        self.assertEqual((tmp / "out.fota").read_bytes(), fota.wrap(self.RBL, version="2.0.0", name="OTHER"))

    def test_the_command_refuses_what_is_not_an_rbl(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        (tmp / "x.bin").write_bytes(bytes(300))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            fota.main([str(tmp / "x.bin")])
        self.assertFalse((tmp / "x.fota").exists())


class StockAppTests(unittest.TestCase):
    def test_unstripe_drops_each_blocks_crc(self):
        physical = b"".join(bytes([i]) * 32 + b"\xcc\xcc" for i in range(3)) + b"\x07" * 20
        self.assertEqual(stock_app.unstripe(physical), b"".join(bytes([i]) * 32 for i in range(3)))

    def test_fnv1a32_known_values(self):
        self.assertEqual(stock_app.fnv1a32(b""), 0x811C9DC5)
        self.assertEqual(stock_app.fnv1a32(b"a"), 0xE40C292C)
        self.assertEqual(stock_app.fnv1a32(b"foobar"), 0xBF9CF968)

    def dump(self, app, raw_hash=None, name=b"app"):
        logical = bytearray(b"\xff" * 0x260000)
        logical[0x10000:0x10000 + len(app)] = app
        header = rbl_header(name=name, version=b"1.0.2", size_raw=len(app),
                            raw_hash=stock_app.fnv1a32(app) if raw_hash is None else raw_hash)
        logical[0x25FFA0:0x25FFA0 + 96] = header
        return b"".join(bytes(logical[i:i + 32]) + b"\xcc\xcc" for i in range(0, len(logical), 32))

    def extract(self, dump):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        (tmp / "dump.bin").write_bytes(dump)
        out = tmp / "app.bin"
        with mock.patch.object(sys, "argv", ["stock_app.py", str(tmp / "dump.bin"), "-o", str(out)]), \
             contextlib.redirect_stdout(io.StringIO()) as log, contextlib.redirect_stderr(io.StringIO()) as err:
            code = stock_app.main()
        return code, out, log.getvalue() + err.getvalue()

    def test_the_app_is_extracted_and_verified(self):
        app = bytes(range(256)) * 10 + b"end"
        code, out, log = self.extract(self.dump(app))
        self.assertEqual(code, 0)
        self.assertEqual(out.read_bytes(), app)
        self.assertIn("stock app 1.0.2: %d bytes" % len(app), log)

    def test_an_app_that_does_not_match_its_header_is_not_written(self):
        app = bytes(range(256)) * 10
        for dump, what in ((self.dump(app, raw_hash=0x12345678), "raw hash"),
                           (self.dump(app, name=b"bootloader"), "no app RBL header")):
            code, out, log = self.extract(dump)
            self.assertEqual(code, 1, what)
            self.assertFalse(out.exists(), what)
            self.assertIn(what, log)


def factory_tlv(entries, pad=0):
    """The factory TLV at 0x3e0000: 'TLV\\0', total length, then tag/marker/len/data entries."""
    body = b""
    for tag, marker, data in entries:
        body += bytes([tag]) + marker + struct.pack("<I", len(data)) + data
    body = b"\x00" * pad + body
    return b"TLV\x00" + struct.pack("<I", len(body)) + body


class BackupTests(unittest.TestCase):
    MAC_BYTES = bytes.fromhex(MAC.replace(":", ""))

    def test_the_mac_is_read_from_the_factory_tlv(self):
        region = factory_tlv([(0x01, b"\x11\x11\x11", b"S61"), (0x02, b"\x11\x11\x11", self.MAC_BYTES)])
        self.assertEqual(backup.factory_mac(region + b"\xff" * 64), MAC)

    def test_padding_entries_are_stepped_over(self):
        region = factory_tlv([(0x02, b"\x11\x11\x11", self.MAC_BYTES)], pad=16)
        self.assertEqual(backup.factory_mac(region), MAC)

    def test_tag_2_of_another_section_is_not_the_mac(self):
        other = factory_tlv([(0x02, b"\x22\x22\x22", bytes(6))])
        with self.assertRaises(ValueError):
            backup.factory_mac(other)
        both = factory_tlv([(0x02, b"\x22\x22\x22", bytes(6)), (0x02, b"\x11\x11\x11", self.MAC_BYTES)])
        self.assertEqual(backup.factory_mac(both), MAC)

    def test_a_region_without_the_tlv_is_refused(self):
        with self.assertRaises(ValueError):
            backup.factory_mac(b"\xff" * 64)
        with self.assertRaises(ValueError):
            backup.factory_mac(factory_tlv([(0x02, b"\x11\x11\x11", bytes(4))]))   # not 6 bytes

    def quick(self):
        for name in ("GAP_S", "RETRY_S"):
            patch = mock.patch.object(backup, name, 0)
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def response(data):
        r = mock.MagicMock()
        r.__enter__.return_value.read.return_value = data
        return r

    def test_the_region_is_read_in_paced_chunks(self):
        self.quick()
        flash = bytes(range(256)) * 96      # 24 KiB: a whole chunk and a half
        urls = []

        def urlopen(url, timeout):
            urls.append(url)
            a, n = (int(x, 16) for x in url.rsplit("/", 1)[1].split("-"))
            return self.response(flash[a - 0x3E0000:a - 0x3E0000 + n])

        with mock.patch.object(backup.urllib.request, "urlopen", urlopen):
            got = backup.read_flash("http://plug/", 0x3E0000, len(flash))
        self.assertEqual(got, flash)
        self.assertEqual(urls, ["http://plug/api/flash/3e0000-4000", "http://plug/api/flash/3e4000-2000"])

    def test_a_failed_or_short_read_is_tried_again(self):
        self.quick()
        answers = [OSError("timed out"), self.response(b"\x01" * 10), self.response(b"\x01" * 0x4000)]
        with mock.patch.object(backup.urllib.request, "urlopen", side_effect=answers) as urlopen:
            self.assertEqual(backup.read_flash("http://plug", 0x3E0000, 0x4000), b"\x01" * 0x4000)
        self.assertEqual(urlopen.call_count, 3)

    def test_a_read_that_keeps_failing_names_where(self):
        self.quick()
        with mock.patch.object(backup.urllib.request, "urlopen", side_effect=OSError("down")) as urlopen:
            with self.assertRaises(RuntimeError) as caught:
                backup.read_flash("http://plug", 0x3E0000, 0x8000)
        self.assertIn("0x3e0000", str(caught.exception))
        self.assertEqual(urlopen.call_count, 4)      # four tries, then given up: no later chunk asked for

    def run_main(self, tmp, region, stock_rbl=None):
        argv = ["backup.py", "--plug", "http://plug", "--captures", str(tmp)]
        if stock_rbl:
            argv += ["--stock-rbl", str(stock_rbl)]
        with mock.patch.object(backup, "read_flash", return_value=region) as read, \
             mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()) as log:
            code = backup.main()
        return code, read, log.getvalue()

    def test_the_backup_is_filed_under_the_plugs_mac_with_its_manifest(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        region = factory_tlv([(0x02, b"\x11\x11\x11", self.MAC_BYTES)]).ljust(0x20000, b"\xff")
        rbl = tmp / "stock_app_1.0.2.rbl"
        rbl.write_bytes(b"RBL\x00" + bytes(200))
        backup = tmp / "plugs" / "12-34-56-ab-cd-ef" / "sonoff-backup"
        backup.mkdir(parents=True)       # the plug's directory may be there already, with no backup in it
        code, read, _log = self.run_main(tmp, region, rbl)
        self.assertEqual(code, 0)
        read.assert_called_once_with("http://plug", 0x3E0000, 0x20000)
        self.assertEqual((backup / "config-0x3e0000.bin").read_bytes(), region)
        self.assertEqual((backup / "stock-app-1.0.2.rbl").read_bytes(), rbl.read_bytes())
        manifest = json.loads((backup / "backup.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["mac"], MAC)
        self.assertEqual(manifest["regions"]["config-0x3e0000.bin"]["flash_offset"], "0x3e0000")
        self.assertEqual(manifest["regions"]["config-0x3e0000.bin"]["length"], 0x20000)
        self.assertEqual(manifest["regions"]["config-0x3e0000.bin"]["sha256"], hashlib.sha256(region).hexdigest())
        self.assertEqual(manifest["regions"]["stock-app-1.0.2.rbl"]["sha256"],
                         hashlib.sha256(rbl.read_bytes()).hexdigest())

    def test_a_backup_once_made_is_never_overwritten(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        region = factory_tlv([(0x02, b"\x11\x11\x11", self.MAC_BYTES)]).ljust(0x20000, b"\xff")
        backup = tmp / "plugs" / "12-34-56-ab-cd-ef" / "sonoff-backup"
        backup.mkdir(parents=True)
        (backup / "config-0x3e0000.bin").write_bytes(b"the first backup")
        code, _read, log = self.run_main(tmp, region)
        self.assertEqual(code, 0)
        self.assertEqual((backup / "config-0x3e0000.bin").read_bytes(), b"the first backup")
        self.assertFalse((backup / "backup.json").exists())
        self.assertIn("never overwritten", log)


class ConversionChainTests(unittest.TestCase):
    """A stock dump's credentials end up in the OpenBeken config, whole."""

    def test_stock_store_to_openbeken_config(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        stock = bytearray(b"\xff" * 0x400000)
        ef40.provision(stock, {"wifi-ssid": "Café Net".encode("latin1"), "wifi-pass": b"p@ss w0rd!"})
        (tmp / "stock.bin").write_bytes(stock)
        (tmp / "obk.bin").write_bytes(b"\x00" * 0x200000)
        wifi_migrate.migrate(tmp / "stock.bin", tmp / "obk.bin", tmp / "out.bin", 0x1E1000)
        config = (tmp / "out.bin").read_bytes()[0x1E1000:0x1E1000 + 3584]
        self.assertEqual(config[:3], b"CFG")
        self.assertEqual(config[3], obk_config.tiny_crc8(config[4:]))
        ssid = "Café Net".encode("latin1")
        self.assertEqual(config[0x14:0x14 + len(ssid) + 1], ssid + b"\x00")
        self.assertEqual(config[0x54:0x54 + 11], b"p@ss w0rd!\x00")


if __name__ == "__main__":
    unittest.main()
