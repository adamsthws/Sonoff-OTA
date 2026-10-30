"""The UF2 files an ESPHome built on LibreTiny takes as an update, as
ltchiptool writes them (`ltchiptool uf2 write ... --legacy "IMAGE=device:download"`).

A plug on ESPHome takes updates through ESPHome's OTA; on LibreTiny each is a
UF2, and LibreTiny writes the file's "download" part to the download slot -
where the S61's stock bootloader applies whatever rt_ota image it finds on the
next start.  So a UF2 carrying an RBL in its download part is how a plug on
ESPHome moves to OpenBeken, or back to Sonoff's firmware
(devices/sonoff-s61/esphome.md).

The file is 512-byte blocks: a first block of tags alone (the board, the
LibreTiny version, the firmware's name and version, the partition the data is
for), then the image in 256-byte pieces, each block saying where in the
partition its piece goes.  Byte for byte what ltchiptool 4.x writes for the
S61's board, given the same image, name, version and build time.
"""

from __future__ import annotations

import struct
import time

MAGIC_START0 = 0x0A324655
MAGIC_START1 = 0x9E5D5157
MAGIC_END = 0x0AB16F30
FLAG_NOT_MAIN_FLASH = 0x0001
FLAG_FAMILY_ID = 0x2000
FLAG_EXTENSION_TAGS = 0x8000
FAMILY_BK7238 = 0x159AC324  # LibreTiny's UF2 family for the BK7238
BLOCK = 512
PAYLOAD = 256

# ltchiptool's tags: a 24-bit type, each.
TAG_BOARD = 0xCA25C8          # the board the firmware is for
TAG_BOARD_ID = 0xC8A729       # its four-byte hash, as ltchiptool computes it
TAG_LT_VERSION = 0x59563D     # the LibreTiny version
TAG_FIRMWARE = 0x00DE43       # the firmware's name
TAG_VERSION = 0x9FC7BC        # and version
TAG_BUILD_DATE = 0x822F30     # when, seconds since the epoch
TAG_PARTITION_TABLE = 0x8288ED  # the partition the data is for: "01PE", name, flash, offset and size in 256s
TAG_OTA_VERSION = 0x6C8492
TAG_DEVICE = 0x650D9D         # "LibreTiny"
TAG_HAS_OTA1 = 0xBBD965
TAG_HAS_OTA2 = 0x92280E
TAG_LEGACY = 0x5D57D0         # --legacy: an image for the OTA scheme LibreTiny 1.x understands
TAG_LEGACY_SIZE = 0x6EC68A
TAG_PART_LEGACY = 0x805946    # on a data block: the piece is for the partition named next
TAG_PART_NAME = 0xA1E4D7
TAG_PART_LEGACY_NAME = 0xC0EE0C

BOARD = "generic-bk7238"
BOARD_ID = bytes.fromhex("7fbbfcc3")
LT_VERSION = "1.13.0"
DEVICE = "LibreTiny"
# The S61's download slot, as the board the image was built for lays it out:
# at 0x286000, 0x15a000 long, in flash0.
# What ltchiptool writes after the first block's tags, where it writes it.
NOTE = b"Hi! Please visit https://docs.libretiny.eu/ to read specifications of this file format."
NOTE_AT = 304
DOWNLOAD = "download"
DOWNLOAD_OFFSET = 0x286000
DOWNLOAD_SIZE = 0x15A000


def _tag(kind: int, data: bytes) -> bytes:
    total = 4 + len(data)
    if total > 0xFF:
        raise ValueError("a UF2 tag holds at most 251 bytes")
    tag = bytes([total]) + kind.to_bytes(3, "little") + data
    return tag + b"\0" * (-len(tag) % 4)


def _block(flags: int, address: int, payload: bytes, number: int, count: int, tags: bytes) -> bytes:
    head = struct.pack("<8I", MAGIC_START0, MAGIC_START1, flags, address, len(payload), number, count,
                       FAMILY_BK7238)
    body = payload + tags
    if len(body) > BLOCK - 36:
        raise ValueError("a UF2 block's data and tags are at most 476 bytes")
    return head + body + b"\0" * (BLOCK - 36 - len(body)) + struct.pack("<I", MAGIC_END)


def partition_entry(name: str = DOWNLOAD, offset: int = DOWNLOAD_OFFSET, size: int = DOWNLOAD_SIZE,
                    flash: str = "flash0") -> bytes:
    """The partition-table tag's data: the one partition the image is for."""
    return (b"01PE" + name.encode("ascii").ljust(16, b"\0") + flash.encode("ascii").ljust(17, b"\0")
            + struct.pack("<II", offset // PAYLOAD, size // PAYLOAD) + b"\0\0\0")


def write(image: bytes, *, name: str, version: str, build_date: int | None = None,
          partition: str = DOWNLOAD, partition_offset: int = DOWNLOAD_OFFSET,
          partition_size: int = DOWNLOAD_SIZE) -> bytes:
    """A UF2 that puts `image` at the start of `partition` - the S61's download
    slot, so the stock bootloader applies an rt_ota image on the next start."""
    if not image:
        raise ValueError("nothing to write")
    if len(image) > partition_size:
        raise ValueError(f"the image ({len(image):,} bytes) does not fit the {partition} partition "
                         f"({partition_size:,} bytes)")
    stamp = int(time.time()) if build_date is None else build_date
    pieces = [image[i:i + PAYLOAD] for i in range(0, len(image), PAYLOAD)]
    count = len(pieces) + 1
    header_tags = b"".join([
        _tag(TAG_BOARD, BOARD.encode("ascii")),
        _tag(TAG_BOARD_ID, BOARD_ID),
        _tag(TAG_LT_VERSION, LT_VERSION.encode("ascii")),
        _tag(TAG_FIRMWARE, name.encode("ascii")),
        _tag(TAG_VERSION, version.encode("ascii")),
        _tag(TAG_BUILD_DATE, struct.pack("<I", stamp)),
        _tag(TAG_PARTITION_TABLE, partition_entry(partition, partition_offset, partition_size)),
        _tag(TAG_OTA_VERSION, b"\x02"),
        _tag(TAG_DEVICE, DEVICE.encode("ascii")),
        _tag(TAG_HAS_OTA1, b"\x00"),
        _tag(TAG_HAS_OTA2, b"\x01"),
        _tag(TAG_LEGACY, b"\x01"),
        _tag(TAG_LEGACY_SIZE, b"\x10\x00\x00"),
    ])
    note = header_tags.ljust(NOTE_AT - 32, b"\0") + NOTE
    blocks = [_block(FLAG_EXTENSION_TAGS | FLAG_FAMILY_ID | FLAG_NOT_MAIN_FLASH, 0, b"", 0, count, note)]
    first_tags = b"".join([
        _tag(TAG_PART_LEGACY, b""),
        _tag(TAG_PART_NAME, partition.encode("ascii")),
        _tag(TAG_PART_LEGACY_NAME, b"\x10\x00\x00" + partition.encode("ascii") + b"\0"),
    ])
    for index, piece in enumerate(pieces):
        tags = first_tags if index == 0 else b""
        flags = FLAG_FAMILY_ID | (FLAG_EXTENSION_TAGS if tags else 0)
        blocks.append(_block(flags, index * PAYLOAD, piece, index + 1, count, tags))
    return b"".join(blocks)


def image_in(uf2: bytes) -> tuple[str, bytes]:
    """The partition a UF2 of this shape is for, and the image it carries."""
    if len(uf2) % BLOCK or len(uf2) < 2 * BLOCK:
        raise ValueError("not a UF2 file")
    partition, image = None, bytearray()
    for i in range(0, len(uf2), BLOCK):
        block = uf2[i:i + BLOCK]
        start0, start1, flags, address, size, number, count, family = struct.unpack_from("<8I", block, 0)
        if (start0, start1) != (MAGIC_START0, MAGIC_START1) or struct.unpack_from("<I", block, 508)[0] != MAGIC_END:
            raise ValueError("not a UF2 file")
        if number == 0:
            continue
        if flags & FLAG_EXTENSION_TAGS:
            at = 32 + size
            while at < 508 and block[at]:
                total, kind = block[at], int.from_bytes(block[at + 1:at + 4], "little")
                if kind == TAG_PART_NAME:
                    partition = block[at + 4:at + total].decode("ascii")
                at += (total + 3) & ~3
        if address != len(image):
            raise ValueError("the UF2's pieces are not in order")
        image += block[32:32 + size]
    if partition is None:
        raise ValueError("the UF2 names no partition")
    return partition, bytes(image)
