"""Read and re-seal ESP32-C3 app images inside a flash dump (emulation only).

Patching code in an image means fixing what the bootloader checks: the XOR
checksum after the last segment and, when the header asks for it, the
SHA-256 of everything before it.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

MAGIC = 0xE9
HEADER = 24  # 8-byte image header + 16-byte extended header
CHECKSUM_SEED = 0xEF


@dataclass
class Segment:
    vaddr: int
    offset: int  # of the segment's data, from the start of the image
    size: int

    def contains(self, address: int) -> bool:
        return self.vaddr <= address < self.vaddr + self.size


@dataclass
class Image:
    segments: list[Segment]
    checksum_at: int  # offset of the checksum byte
    hash_appended: bool

    @property
    def length(self) -> int:
        return self.checksum_at + 1 + (32 if self.hash_appended else 0)

    def offset_of(self, address: int) -> int:
        """Image offset holding the byte loaded at `address`."""
        for seg in self.segments:
            if seg.contains(address):
                return seg.offset + address - seg.vaddr
        raise ValueError(f"0x{address:08x} is in no segment")


def parse(data: bytes) -> Image:
    if data[0] != MAGIC:
        raise ValueError("not an ESP app image")
    count = data[1]
    hash_appended = data[23] == 1
    pos, segments = HEADER, []
    for _ in range(count):
        vaddr, size = struct.unpack_from("<II", data, pos)
        segments.append(Segment(vaddr, pos + 8, size))
        pos += 8 + size
    checksum_at = pos + 15 - pos % 16  # padded so the checksum ends a 16-byte block
    return Image(segments, checksum_at, hash_appended)


def checksum(data: bytes, image: Image) -> int:
    value = CHECKSUM_SEED
    for seg in image.segments:
        for byte in data[seg.offset : seg.offset + seg.size]:
            value ^= byte
    return value


def reseal(data: bytearray, image: Image) -> None:
    """Rewrite the checksum and appended SHA-256 after changing segment bytes."""
    data[image.checksum_at] = checksum(data, image)
    if image.hash_appended:
        end = image.checksum_at + 1
        data[end : end + 32] = hashlib.sha256(bytes(data[:end])).digest()


def verify(data: bytes, image: Image) -> bool:
    ok = data[image.checksum_at] == checksum(data, image)
    if image.hash_appended:
        end = image.checksum_at + 1
        ok = ok and data[end : end + 32] == hashlib.sha256(data[:end]).digest()
    return ok
