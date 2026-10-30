"""The image formats a Beken conversion makes itself: the AES the rt_ota
packer uses, the RBL the stock bootloader applies, the UF2 a LibreTiny ESPHome
takes as an update, and the stock app lifted out of a dump."""
import struct
import unittest
import zlib

from tools.beken import aes, rbl, stock_app, uf2


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


if __name__ == "__main__":
    unittest.main()
