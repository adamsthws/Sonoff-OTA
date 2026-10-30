"""Extract the stock S61 app from a flash dump, verified, ready to repackage.

The app partition holds the firmware as the bootloader left it: decompressed and
CRC-striped (32 data bytes, 2 CRC bytes). Its rt_ota RBL header survives just
before the download partition (logical 0x25ffa0) and carries the decompressed
size and the FNV-1a hash of the decompressed image, so the extraction can be
checked byte-for-byte. The compressed OTA body itself is not kept anywhere, so to
put a converted plug back to stock the app is packed again as an RBL (rbl.py,
byte for byte what the Beken SDK's packer makes with -c gzip -s aes and
RT-Thread's public default key), which the stock bootloader applies from the
download slot: `extract()` for the app, `stock_rbl()` for the RBL, which the
conversion tool makes from the public S61 dump when a plug goes back.

  python3 stock_app.py dump.bin [-o stock_app_1.0.2.bin]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.beken import rbl  # noqa: E402

STRIPE_DATA, STRIPE_CRC = 32, 2
APP_LOGICAL = 0x10000
APP_RBL_LOGICAL = 0x25FFA0


class DumpError(ValueError):
    """Not an S61 dump with a stock app in it."""


def extract(dump: bytes) -> tuple[bytes, rbl.Header]:
    """The stock app in a raw S61 flash dump, checked against the RBL header
    the bootloader left beside it, and that header."""
    logical = unstripe(dump)
    try:
        header = rbl.header(logical[APP_RBL_LOGICAL:APP_RBL_LOGICAL + rbl.HEADER])
    except rbl.RblError as exc:
        raise DumpError(f"no app RBL header at logical 0x{APP_RBL_LOGICAL:x}: {exc}") from None
    if header.name != "app":
        raise DumpError(f"no app RBL header at logical 0x{APP_RBL_LOGICAL:x}: the one there is {header.name!r}")
    app = logical[APP_LOGICAL:APP_LOGICAL + header.size_raw]
    if len(app) != header.size_raw or fnv1a32(app) != header.raw_hash:
        raise DumpError("the app does not match its header's size and raw hash (FNV-1a)")
    return app, header


def stock_rbl(dump: bytes) -> tuple[bytes, str]:
    """The stock app of `dump` packed as the rt_ota RBL the stock bootloader
    applies - version, serial and time as the dump's own header has them - and
    its version."""
    app, header = extract(dump)
    return rbl.pack(app, name="app", version=header.version, timestamp=header.timestamp, sn=header.sn), header.version


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
    try:
        app, header = extract(open(a.dump, "rb").read())
    except DumpError as exc:
        print(exc, file=sys.stderr)
        return 1
    out = a.out or "stock_app_%s.bin" % header.version
    open(out, "wb").write(app)
    print("stock app %s: %d bytes, FNV-1a 0x%08x verified -> %s" % (header.version, len(app), header.raw_hash, out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
