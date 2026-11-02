"""The marker that tells OpenBeken it is the interim firmware: one flash
sector sent after its image, through the same update.

A run to ESPHome from Sonoff's firmware installs OpenBeken first, to back the
plug up through it, then has it fetch ESPHome. The stock updater writes the
whole file it is sent into the download slot, and the stock bootloader applies
the rt_ota image at its start and erases only the slot's first sector after
(the image's header): so whatever follows the image stays in the slot. B2
sends the OpenBeken image, 0xff to the next 4 KB boundary (from the image's
start, the slot's), and this sector. On the first boot after the install
the tool's OpenBeken driver (tools/beken/openbeken/drv_autoflash.c)
finds it, names the plug as an interim firmware - autoflash-recovery-CDEF -
and erases it, so it is used once. A run to OpenBeken sends the image alone.

The marker names the app its image installs - the size and FNV-1a hash of
the firmware unpacked, from the image's RBL header, which the bootloader
checks the app against once it has written it - and the module acts on one
only when that is the app running: a marker left in the slot by anything
else, or for another image, never names a plug the interim firmware. One
that is not for the app running is erased.

The sector: 28 bytes, then 0xff. All little-endian.

    [0:8]    magic       b"AFINTERM"
    [8:12]   version     2
    [12:16]  image size  the bytes of image before the padding: the marker is
                         at that, rounded up to 4 KB, from the slot's start
    [16:20]  app size    the RBL header's size_raw: the app's bytes
    [20:24]  app hash    the RBL header's raw_hash: FNV-1a (32-bit) of them
    [24:28]  CRC-32      zlib's, over [0:24]
"""

from __future__ import annotations

import struct
import zlib
from typing import NamedTuple

from tools.beken import rbl

MAGIC = b"AFINTERM"
VERSION = 2
SECTOR = 0x1000
FIELDS = struct.Struct("<8sIIII")
SIZE = FIELDS.size + 4
# The stock download slot, where the updater writes what it is sent.
SLOT_SIZE = 0x15A000


class Marker(NamedTuple):
    image_size: int
    app_size: int
    app_hash: int


def offset_after(image_size: int) -> int:
    """Where the marker goes after an image of `image_size` bytes: the next
    4 KB boundary from the image's start."""
    return -(-image_size // SECTOR) * SECTOR


def sector(image: bytes) -> bytes:
    """The marker sector for `image`, an rt_ota RBL."""
    header = rbl.header(image)
    fields = FIELDS.pack(MAGIC, VERSION, len(image), header.size_raw, header.raw_hash)
    return (fields + struct.pack("<I", zlib.crc32(fields) & 0xFFFFFFFF)).ljust(SECTOR, b"\xff")


def with_marker(image: bytes) -> bytes:
    """`image`, 0xff to the next 4 KB boundary, and the marker sector: what B2
    sends for the interim firmware.  It must all fit the download slot."""
    at = offset_after(len(image))
    if at + SECTOR > SLOT_SIZE:
        raise ValueError(f"an image of {len(image):,} bytes leaves no room for the marker in the download slot")
    return image + b"\xff" * (at - len(image)) + sector(image)


def read(data: bytes, offset: int) -> Marker | None:
    """The marker at `offset` of `data` (the slot, from its start), if the
    sector there is one: its magic, version and CRC, and at the offset its
    image size gives."""
    head = data[offset:offset + SIZE]
    if len(head) != SIZE or offset % SECTOR:
        return None
    magic, version, image_size, app_size, app_hash = FIELDS.unpack_from(head)
    (crc,) = struct.unpack_from("<I", head, FIELDS.size)
    if (magic != MAGIC or version != VERSION or crc != zlib.crc32(head[:FIELDS.size]) & 0xFFFFFFFF
            or image_size <= 0 or offset_after(image_size) != offset):
        return None
    return Marker(image_size, app_size, app_hash)


def valid_at(data: bytes, offset: int) -> bool:
    """Whether the sector at `offset` of `data` is a marker (read)."""
    return read(data, offset) is not None


def for_app(marker: Marker, app: bytes) -> bool:
    """Whether `marker` is for `app`, the app installed (from its start, as
    the CPU sees it): its first app-size bytes hash as the marker says."""
    return 0 < marker.app_size <= len(app) and rbl.fnv1a32(app[:marker.app_size]) == marker.app_hash


def find(slot: bytes) -> int | None:
    """The offset of the marker in the download slot `slot`, as the module
    looks for it: at each sector's start past the first (the image's header,
    which the bootloader erases), to the slot's end."""
    for offset in range(SECTOR, min(len(slot), SLOT_SIZE), SECTOR):
        if valid_at(slot, offset):
            return offset
    return None
