"""rt_ota RBL images, as the Beken SDK's packer makes them and the S61's stock
bootloader applies them (docs/beken-s61-emulation.md, "Update (RBL) format").

An RBL is a 96-byte header and a body: the firmware gzipped (level 9, no
name, no time), padded (PKCS#7) and encrypted with AES-256-CBC under
RT-Thread's public default key.  The header carries the body's CRC-32, the
FNV-1a hash and size of the firmware before packing, the packed size, and
its own CRC-32.  Nothing in it is secret, so a tool can make one: the
conversion makes the stock app's this way, from the public S61 dump, when a
plug is to go back to Sonoff's firmware (stock_app.py).

    pack(firmware, name="app", version="1.0.2")  -> the RBL
    header(rbl)                                   -> its fields
    unpack(rbl)                                   -> the firmware it carries, checked
"""

from __future__ import annotations

import gzip
import struct
import zlib
from dataclasses import dataclass

from tools.beken import aes

MAGIC = b"RBL\0"
HEADER = 96
# algo: the crypt nibbles (0x100: AES) and the compression (0x2: gzip), as the
# packer's "-s aes -c gzip" writes them and the S61's own images carry them.
ALGO_AES_GZIP = 0x102
# RT-Thread's public default key and IV: the packer's, and every S61 image's.
KEY = b"0123456789ABCDEF0123456789ABCDEF"
IV = b"0123456789ABCDEF"


class RblError(ValueError):
    pass


def fnv1a32(data: bytes) -> int:
    h = 0x811C9DC5
    for b in data:
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


@dataclass(frozen=True)
class Header:
    algo: int
    timestamp: int
    name: str
    version: str
    sn: bytes
    body_crc32: int
    raw_hash: int
    size_raw: int
    size_package: int

    def pack(self) -> bytes:
        h = bytearray(HEADER)
        h[0:4] = MAGIC
        struct.pack_into("<II", h, 4, self.algo, self.timestamp)
        h[12:28] = self.name.encode("ascii")[:16].ljust(16, b"\0")
        h[28:52] = self.version.encode("ascii")[:24].ljust(24, b"\0")
        h[52:76] = self.sn[:24].ljust(24, b"\0")
        struct.pack_into("<IIII", h, 76, self.body_crc32, self.raw_hash, self.size_raw, self.size_package)
        struct.pack_into("<I", h, 92, zlib.crc32(bytes(h[:92])) & 0xFFFFFFFF)
        return bytes(h)


def header(rbl: bytes) -> Header:
    """The header of `rbl`, checked against its own CRC-32."""
    if len(rbl) < HEADER or rbl[:4] != MAGIC:
        raise RblError("not an rt_ota RBL (no 'RBL' magic)")
    if zlib.crc32(rbl[:92]) & 0xFFFFFFFF != struct.unpack_from("<I", rbl, 92)[0]:
        raise RblError("the RBL header's CRC-32 is wrong")
    algo, timestamp = struct.unpack_from("<II", rbl, 4)
    body_crc32, raw_hash, size_raw, size_package = struct.unpack_from("<IIII", rbl, 76)
    return Header(algo=algo, timestamp=timestamp,
                  name=rbl[12:28].split(b"\0", 1)[0].decode("ascii", "replace"),
                  version=rbl[28:52].split(b"\0", 1)[0].decode("ascii", "replace"),
                  sn=rbl[52:76], body_crc32=body_crc32, raw_hash=raw_hash,
                  size_raw=size_raw, size_package=size_package)


def _gzip(data: bytes) -> bytes:
    """gzip as the packer writes it: level 9, no name or time, and "Unix" for
    the system, where Python's module writes "unknown"."""
    deflate = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    body = deflate.compress(data) + deflate.flush()
    header = b"\x1f\x8b\x08\x00" + b"\x00\x00\x00\x00" + b"\x02\x03"
    return header + body + struct.pack("<II", zlib.crc32(data) & 0xFFFFFFFF, len(data) & 0xFFFFFFFF)


def pack(firmware: bytes, *, name: str = "app", version: str, timestamp: int = 0,
         sn: bytes = b"") -> bytes:
    """The RBL carrying `firmware`, as the packer would make it with -c gzip -s aes."""
    body = aes.cbc_encrypt(KEY, IV, _gzip(firmware))
    fields = Header(algo=ALGO_AES_GZIP, timestamp=timestamp, name=name, version=version, sn=sn,
                    body_crc32=zlib.crc32(body) & 0xFFFFFFFF, raw_hash=fnv1a32(firmware),
                    size_raw=len(firmware), size_package=len(body))
    return fields.pack() + body


def unpack(rbl: bytes) -> bytes:
    """The firmware an RBL of ALGO_AES_GZIP carries, checked against its header."""
    fields = header(rbl)
    body = rbl[HEADER:HEADER + fields.size_package]
    if len(body) != fields.size_package:
        raise RblError("the RBL is shorter than its header says")
    if zlib.crc32(body) & 0xFFFFFFFF != fields.body_crc32:
        raise RblError("the RBL body's CRC-32 is wrong")
    if fields.algo != ALGO_AES_GZIP:
        raise RblError(f"an RBL of algorithm 0x{fields.algo:x} is not one this unpacks (AES, gzip)")
    firmware = gzip.decompress(aes.cbc_decrypt(KEY, IV, body))
    if len(firmware) != fields.size_raw or fnv1a32(firmware) != fields.raw_hash:
        raise RblError("the firmware in the RBL does not match its header's size and hash")
    return firmware
