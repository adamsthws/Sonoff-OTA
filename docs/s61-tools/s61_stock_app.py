"""Extract the stock S61 app from a flash dump, verified, ready to repackage.

The app partition holds the firmware as the bootloader left it: decompressed and
CRC-striped (32 data bytes, 2 CRC bytes). Its rt_ota RBL header survives just
before the download partition (logical 0x25ffa0) and carries the decompressed
size and the FNV-1a hash of the decompressed image, so the extraction can be
checked byte-for-byte. The compressed OTA body itself is not kept anywhere, so to
put a converted plug back to stock the app is repackaged with the Beken SDK's
own packer - the recipe OpenBeken's build uses, which the stock bootloader
accepts:

    rt_ota_packaging_tool_cli -f stock_app_1.0.2.bin -o stock_app_1.0.2.rbl \
        -p app -c gzip -s aes -k 0123456789ABCDEF0123456789ABCDEF \
        -i 0123456789ABCDEF -v 1.0.2

(the key is RT-Thread's public default; see docs/beken-s61-emulation.md).

  python3 s61_stock_app.py dump.bin [-o stock_app_1.0.2.bin]
"""
from __future__ import annotations

import argparse
import struct
import sys
import zlib

STRIPE_DATA, STRIPE_CRC = 32, 2
APP_LOGICAL = 0x10000
APP_RBL_LOGICAL = 0x25FFA0


def unstripe(physical: bytes) -> bytes:
    """Drop the 2-byte CRC after every 32 data bytes."""
    out = bytearray()
    step = STRIPE_DATA + STRIPE_CRC
    for i in range(0, len(physical) - step + 1, step):
        out += physical[i:i + STRIPE_DATA]
    return bytes(out)


def fnv1a32(data: bytes) -> int:
    h = 0x811C9DC5
    for b in data:
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract the verified stock app from an S61 flash dump.")
    ap.add_argument("dump")
    ap.add_argument("-o", "--out", default=None)
    a = ap.parse_args()
    logical = unstripe(open(a.dump, "rb").read())
    hdr = logical[APP_RBL_LOGICAL:APP_RBL_LOGICAL + 96]
    if hdr[:4] != b"RBL\x00" or hdr[12:28].split(b"\0")[0] != b"app":
        print("no app RBL header at logical 0x%x" % APP_RBL_LOGICAL, file=sys.stderr)
        return 1
    if zlib.crc32(hdr[:92]) & 0xFFFFFFFF != struct.unpack_from("<I", hdr, 92)[0]:
        print("app RBL header CRC mismatch", file=sys.stderr)
        return 1
    version = hdr[28:52].split(b"\0")[0].decode("latin1")
    raw_hash, size_raw = struct.unpack_from("<II", hdr, 80)
    app = logical[APP_LOGICAL:APP_LOGICAL + size_raw]
    got = fnv1a32(app)
    if got != raw_hash:
        print("extracted app does not match the header's raw hash (0x%08x vs 0x%08x)" % (got, raw_hash), file=sys.stderr)
        return 1
    out = a.out or "stock_app_%s.bin" % version
    open(out, "wb").write(app)
    print("stock app %s: %d bytes, FNV-1a 0x%08x verified -> %s" % (version, len(app), got, out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
