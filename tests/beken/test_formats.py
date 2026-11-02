"""The image formats a Beken conversion makes itself: the AES the rt_ota
packer uses, the RBL the stock bootloader applies, the UF2 a LibreTiny ESPHome
takes as an update, the stock app lifted out of a dump, and the marker sent
after the interim firmware."""
import re
import struct
import unittest
import zlib
from pathlib import Path

from tools.beken import aes, interim_marker, rbl, stock_app, uf2

ROOT = Path(__file__).resolve().parents[2]


class AesTests(unittest.TestCase):
    """FIPS-197's own vectors (appendix C), then CBC around them."""

    def test_the_fips_197_vectors(self):
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        for key_length, expected in ((16, "69c4e0d86a7b0430d8cdb78070b4c55a"),
                                     (24, "dda97ca4864cdfe06eaf70a0ec0d7191"),
                                     (32, "8ea2b7ca516745bfeafc49904b496089")):
            with self.subTest(bits=key_length * 8):
                keys = aes.expand_key(bytes(range(key_length)))
                cipher = aes.encrypt_block(keys, plain)
                self.assertEqual(cipher.hex(), expected)
                self.assertEqual(aes.decrypt_block(keys, cipher), plain)

    def test_cbc_round_trip_pads_as_pkcs7(self):
        key, iv = b"k" * 32, b"i" * 16
        for length in (0, 1, 15, 16, 17, 100):
            data = bytes(range(256))[:length] * (1 if length else 0)
            cipher = aes.cbc_encrypt(key, iv, data)
            self.assertEqual(len(cipher), (length // 16 + 1) * 16)  # always some padding
            self.assertEqual(aes.cbc_decrypt(key, iv, cipher), data)
        # The padding bytes say how many they are: a whole block for an aligned length.
        self.assertEqual(aes.pad(b"x" * 16)[-16:], bytes([16]) * 16)
        with self.assertRaises(ValueError):
            aes.unpad(b"x" * 15 + b"\x00")

    def test_a_wrong_key_or_iv_is_refused(self):
        with self.assertRaises(ValueError):
            aes.expand_key(b"short")
        with self.assertRaises(ValueError):
            aes.cbc_encrypt(b"k" * 32, b"iv", b"data")
        with self.assertRaises(ValueError):
            aes.cbc_decrypt(b"k" * 32, b"i" * 16, b"not a block")


class RblTests(unittest.TestCase):
    def test_pack_and_unpack_round_trip(self):
        firmware = bytes(range(256)) * 40 + b"tail"
        packed = rbl.pack(firmware, version="1.2.3", timestamp=1234, sn=b"serial")
        header = rbl.header(packed)
        self.assertEqual((header.name, header.version, header.timestamp, header.algo),
                         ("app", "1.2.3", 1234, rbl.ALGO_AES_GZIP))
        self.assertEqual(header.sn, b"serial".ljust(24, b"\0"))
        self.assertEqual((header.size_raw, header.raw_hash), (len(firmware), rbl.fnv1a32(firmware)))
        self.assertEqual(header.size_package, len(packed) - rbl.HEADER)
        self.assertEqual(zlib.crc32(packed[rbl.HEADER:]) & 0xFFFFFFFF, header.body_crc32)
        self.assertEqual(rbl.unpack(packed), firmware)
        # The body is gzip under AES-CBC with the public key, as the SDK's packer makes it.
        plain = aes.cbc_decrypt(rbl.KEY, rbl.IV, packed[rbl.HEADER:])
        self.assertEqual(plain[:10], b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\x03")

    def test_fnv1a32_is_the_standard_one(self):
        self.assertEqual(rbl.fnv1a32(b""), 0x811C9DC5)
        self.assertEqual(rbl.fnv1a32(b"a"), 0xE40C292C)

    def test_a_damaged_rbl_is_refused(self):
        packed = bytearray(rbl.pack(b"firmware", version="1"))
        for where, message in ((0, "magic"), (20, "header's CRC"), (rbl.HEADER + 3, "body's CRC")):
            damaged = bytearray(packed)
            damaged[where] ^= 0xFF
            with self.assertRaisesRegex(rbl.RblError, message):
                rbl.unpack(bytes(damaged))
        with self.assertRaisesRegex(rbl.RblError, "shorter"):
            rbl.unpack(bytes(packed[:-1]))
        other = rbl.Header(algo=0x402, timestamp=0, name="app", version="1", sn=b"", body_crc32=0,
                           raw_hash=0, size_raw=0, size_package=0).pack()
        with self.assertRaisesRegex(rbl.RblError, "0x402"):
            rbl.unpack(other)


class Uf2Tests(unittest.TestCase):
    def test_the_image_goes_into_the_download_partition_in_order(self):
        image = bytes(range(256)) * 3 + b"end"
        data = uf2.write(image, name="openbeken-s61", version="1.0.0", build_date=1700000000)
        self.assertEqual(len(data) % uf2.BLOCK, 0)
        blocks = [data[i:i + uf2.BLOCK] for i in range(0, len(data), uf2.BLOCK)]
        self.assertEqual(len(blocks), 1 + 4)  # the tags, then 256-byte pieces
        for number, block in enumerate(blocks):
            start0, start1, flags, address, size, index, count, family = struct.unpack_from("<8I", block, 0)
            self.assertEqual((start0, start1, index, count, family),
                             (uf2.MAGIC_START0, uf2.MAGIC_START1, number, 5, uf2.FAMILY_BK7238))
            self.assertEqual(struct.unpack_from("<I", block, 508)[0], uf2.MAGIC_END)
            if number:
                self.assertEqual(address, (number - 1) * 256)
                self.assertTrue(flags & uf2.FLAG_FAMILY_ID)
        self.assertEqual(struct.unpack_from("<I", blocks[-1], 16)[0], 3)  # the last piece's size
        self.assertEqual(uf2.image_in(data), ("download", image))
        # The first block says what the file is, for the plug to check.
        self.assertIn(b"generic-bk7238", blocks[0])
        self.assertIn(b"openbeken-s61", blocks[0])
        self.assertIn(b"1.0.0", blocks[0])
        self.assertIn(uf2.NOTE, blocks[0])
        self.assertIn(b"01PEdownload", blocks[0])

    def test_the_partition_entry_counts_in_256s(self):
        entry = uf2.partition_entry()
        self.assertEqual(len(entry), 48)
        self.assertEqual(struct.unpack_from("<II", entry, 37), (0x286000 // 256, 0x15A000 // 256))

    def test_an_image_too_big_for_the_slot_is_refused(self):
        with self.assertRaises(ValueError):
            uf2.write(b"x" * (uf2.DOWNLOAD_SIZE + 1), name="n", version="v")
        with self.assertRaises(ValueError):
            uf2.write(b"", name="n", version="v")

    def test_something_else_is_not_read_as_a_uf2(self):
        with self.assertRaises(ValueError):
            uf2.image_in(b"\0" * 1024)
        with self.assertRaises(ValueError):
            uf2.image_in(b"short")


def striped(logical: bytes) -> bytes:
    """A raw dump: a CRC after every 32 bytes, as the flash holds it."""
    out = bytearray()
    for i in range(0, len(logical), 32):
        out += logical[i:i + 32].ljust(32, b"\xff") + b"\0\0"
    return bytes(out)


class StockAppTests(unittest.TestCase):
    def dump(self, app=b"stock app" * 1000, version="1.0.2", hash_offset=0):
        header = rbl.Header(algo=0x402, timestamp=7, name="app", version=version, sn=b"sn", body_crc32=0,
                            raw_hash=(rbl.fnv1a32(app) + hash_offset) & 0xFFFFFFFF, size_raw=len(app),
                            size_package=0).pack()
        logical = bytearray(b"\xff" * (stock_app.APP_RBL_LOGICAL + rbl.HEADER + 64))
        logical[stock_app.APP_LOGICAL:stock_app.APP_LOGICAL + len(app)] = app
        logical[stock_app.APP_RBL_LOGICAL:stock_app.APP_RBL_LOGICAL + rbl.HEADER] = header
        return striped(bytes(logical))

    def test_the_app_is_lifted_out_and_checked_against_its_header(self):
        app = b"stock app" * 1000
        found, header = stock_app.extract(self.dump(app))
        self.assertEqual(found, app)
        self.assertEqual((header.version, header.timestamp, header.sn), ("1.0.2", 7, b"sn".ljust(24, b"\0")))
        packed, version = stock_app.stock_rbl(self.dump(app))
        self.assertEqual(version, "1.0.2")
        self.assertEqual(rbl.unpack(packed), app)
        self.assertEqual((rbl.header(packed).timestamp, rbl.header(packed).sn), (7, header.sn))

    def test_a_dump_whose_app_does_not_match_is_refused(self):
        with self.assertRaisesRegex(stock_app.DumpError, "raw hash"):
            stock_app.extract(self.dump(hash_offset=1))
        with self.assertRaisesRegex(stock_app.DumpError, "no app RBL header"):
            stock_app.extract(striped(b"\xff" * (stock_app.APP_RBL_LOGICAL + 200)))

    def test_unstripe_drops_the_crc_after_every_32_bytes(self):
        self.assertEqual(stock_app.unstripe(striped(bytes(range(64)))), bytes(range(64)))


APP = bytes(range(256)) * 40   # an app, as an image installs it


def an_image(size: int, app: bytes = APP) -> bytes:
    """An rt_ota image of `size` bytes installing `app`: what follows an RBL's
    body is no part of it, so filler makes it any size."""
    image = rbl.pack(app, version="1.1.0")
    return image + b"i" * (size - len(image))


class InterimMarkerTests(unittest.TestCase):
    """The sector B2 sends after the interim firmware, which the S61 import
    module looks for in the download slot on its first start, and acts on
    only for the app running."""

    def test_it_follows_the_image_at_the_next_4_kb_boundary(self):
        for size, at in ((0x1000, 0x1000), (0x1001, 0x2000), (508736, 0x7D000)):
            with self.subTest(size=size):
                image = an_image(size)
                sent = interim_marker.with_marker(image)
                self.assertEqual(sent[:size], image)
                self.assertEqual(sent[size:at], b"\xff" * (at - size))   # as erased flash
                self.assertEqual(len(sent), at + interim_marker.SECTOR)
                self.assertTrue(interim_marker.valid_at(sent, at))
                self.assertEqual(interim_marker.find(sent), at)

    def test_the_sector_is_its_fields_their_crc_then_ff(self):
        image = an_image(508736)
        header = rbl.header(image)
        sector = interim_marker.sector(image)
        self.assertEqual(len(sector), interim_marker.SECTOR)
        self.assertEqual(sector[:8], b"AFINTERM")
        self.assertEqual(struct.unpack_from("<IIII", sector, 8), (2, 508736, len(APP), rbl.fnv1a32(APP)))
        self.assertEqual((header.size_raw, header.raw_hash), (len(APP), rbl.fnv1a32(APP)))
        self.assertEqual(struct.unpack_from("<I", sector, 24)[0], zlib.crc32(sector[:24]))
        self.assertEqual(sector[28:], b"\xff" * (interim_marker.SECTOR - 28))
        self.assertEqual(interim_marker.read(sector.rjust(0x7D000 + interim_marker.SECTOR, b"\xff"), 0x7D000),
                         interim_marker.Marker(508736, len(APP), rbl.fnv1a32(APP)))

    def test_only_an_rt_ota_image_has_one(self):
        # The app it names comes from the image's own header.
        with self.assertRaises(rbl.RblError):
            interim_marker.with_marker(b"RBL\0" + bytes(5000))

    def test_it_is_for_the_app_its_image_installs_and_no_other(self):
        image = an_image(5000)
        marker = interim_marker.read(interim_marker.with_marker(image), 0x2000)
        self.assertTrue(interim_marker.for_app(marker, APP))
        # The app partition holds more than the app: erased flash, or an older app's tail.
        self.assertTrue(interim_marker.for_app(marker, APP + b"\xff" * 0x1000))
        other = APP[:-1] + b"\x00"
        self.assertFalse(interim_marker.for_app(marker, other))
        self.assertFalse(interim_marker.for_app(marker, APP[:-1]))
        # A marker sent with another image, valid in itself, is not this app's.
        stale = interim_marker.read(interim_marker.with_marker(an_image(5000, other)), 0x2000)
        self.assertIsNotNone(stale)
        self.assertFalse(interim_marker.for_app(stale, APP))
        self.assertTrue(interim_marker.for_app(stale, other))

    def test_a_marker_changed_anywhere_is_refused(self):
        sent = interim_marker.with_marker(an_image(5004))
        at = interim_marker.offset_after(5004)
        for index in range(interim_marker.SIZE):
            with self.subTest(byte=index):
                broken = bytearray(sent)
                broken[at + index] ^= 0x01
                self.assertFalse(interim_marker.valid_at(bytes(broken), at))
                self.assertIsNone(interim_marker.find(bytes(broken)))

    def test_one_of_the_first_version_is_not_one(self):
        fields = struct.pack("<8sII", b"AFINTERM", 1, 0x1000)
        slot = b"\xff" * 0x1000 + (fields + struct.pack("<I", zlib.crc32(fields))).ljust(0x1000, b"\xff")
        self.assertIsNone(interim_marker.find(slot))

    def test_one_elsewhere_than_after_the_image_it_names_is_refused(self):
        # A copy of a marker in other data - a file in OpenBeken's own
        # filesystem at the slot's top, say - is not after an image of its size.
        slot = bytearray(b"\xff" * 0x10000)
        slot[0x3000:0x4000] = interim_marker.sector(an_image(0x1000))
        self.assertFalse(interim_marker.valid_at(bytes(slot), 0x3000))
        self.assertIsNone(interim_marker.find(bytes(slot)))
        self.assertFalse(interim_marker.valid_at(bytes(slot), 0x3001))   # off a sector's start

    def test_an_erased_slot_or_one_without_a_marker_has_none(self):
        self.assertIsNone(interim_marker.find(b"\xff" * interim_marker.SLOT_SIZE))
        self.assertIsNone(interim_marker.find(b"RBL\0" + bytes(0x20000)))

    def test_the_slots_first_sector_is_never_a_marker(self):
        # The image's header is there - erased by the bootloader once applied.
        self.assertIsNone(interim_marker.find(interim_marker.sector(an_image(0x1000))))

    def test_it_must_fit_the_download_slot(self):
        largest = interim_marker.SLOT_SIZE - interim_marker.SECTOR
        self.assertEqual(len(interim_marker.with_marker(an_image(largest))), interim_marker.SLOT_SIZE)
        with self.assertRaisesRegex(ValueError, "no room for the marker"):
            interim_marker.with_marker(an_image(largest + 1))
        self.assertEqual(interim_marker.SLOT_SIZE, uf2.DOWNLOAD_SIZE)

    def test_the_openbeken_driver_reads_the_same_format(self):
        # tools/beken/openbeken/drv_autoflash.c, which a build of OpenBeken
        # carries: its magic, version and size are these, its fields where
        # these are, and it hashes the app as the RBL header does.
        driver = (ROOT / "tools/beken/openbeken/drv_autoflash.c").read_text(encoding="utf-8")
        defined = dict(re.findall(r"^#define (AUTOFLASH_\w+)\s+(\S+)", driver, re.MULTILINE))
        self.assertEqual(defined["AUTOFLASH_MARKER_MAGIC"], f'"{interim_marker.MAGIC.decode()}"')
        self.assertEqual(int(defined["AUTOFLASH_MARKER_VERSION"]), interim_marker.VERSION)
        self.assertEqual(int(defined["AUTOFLASH_MARKER_SIZE"]), interim_marker.SIZE)
        self.assertEqual(int(defined["AUTOFLASH_SECTOR"], 16), interim_marker.SECTOR)
        self.assertEqual(int(defined["AUTOFLASH_STRIPE"]), 34)   # as stock_app.unstripe takes the app partition
        marker_at = driver[driver.index("static int autoflash_marker_at("):
                           driver.index("static int autoflash_marker_for_this_app(")]
        offsets = {name: int(at) for name, at in re.findall(r"\*(\w+) = autoflash_u32\(head \+ (\d+)\)", marker_at)}
        self.assertEqual(offsets, {"appSize": 16, "appHash": 20})
        self.assertIn("autoflash_u32(head + 12)", marker_at)   # the image size
        self.assertIn(f"autoflash_u32(head + {interim_marker.FIELDS.size}) != "
                      f"ef40_crc32(head, {interim_marker.FIELDS.size})", marker_at)
        self.assertIn("0x811c9dc5", driver)
        self.assertIn("* 0x01000193", driver)
        self.assertEqual(rbl.fnv1a32(b"a"), 0xE40C292C)   # FNV-1a's own vector


if __name__ == "__main__":
    unittest.main()
