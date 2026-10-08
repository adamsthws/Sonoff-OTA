#!/usr/bin/env python3
"""Make a copy of the stock 1.2.0 flash dump that boots under QEMU.

Each patch names the instruction it replaces and checks the original bytes
first, so it can only apply to the Sonoff 1.2.0 image it was written for. The
image is then re-sealed (checksum and SHA-256) so the bootloader accepts it.
The source dump is never modified.

    python -I tools/emulator/patch_stock.py captures/emulation/flash.bin captures/emulation/run-flash.bin
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.emulator import appimage  # noqa: E402

OTA_0 = 0x20000
OTA_SIZE = 0x1F0000
RETURN_0 = bytes.fromhex("01458280")  # c.li a0,0 ; c.jr ra
PROLOGUE = bytes.fromhex("5d7186c6")  # c.addi16sp sp,-80 ; c.swsp ra,76(sp)

# (load address, original bytes, replacement, why)
# QEMU (Espressif's and the lcgamboa fork) emulates no Bluetooth: the
# controller's clock setup fails and its ROM code faults. Bluetooth is only
# for first-time pairing in the eWeLink app, which a plug being converted has
# done already, so its start-up is skipped. The "BLE ready" flag
# (0x3FC9DBAC) then stays clear, as if Bluetooth were off.
PATCHES = [
    (0x4200A66A, PROLOGUE, RETURN_0,
     "Sonoff BLE setup (esp_bt_controller_init/enable, bluedroid, GATT "
     "callbacks; -1..-11 on failure): return 0 without starting Bluetooth"),
    (0x4201BB60, PROLOGUE, RETURN_0,
     "second BT controller start-up (random 21-digit name, no error "
     "checks; likely a test mode): return 0"),
]


def patch(flash: bytes) -> bytes:
    out = bytearray(flash)
    app = bytearray(out[OTA_0 : OTA_0 + OTA_SIZE])
    image = appimage.parse(app)
    if not appimage.verify(app, image):
        raise SystemExit("ota_0 does not verify before patching; is this the 1.2.0 dump?")
    for address, original, replacement, why in PATCHES:
        at = image.offset_of(address)
        found = bytes(app[at : at + len(original)])
        if found not in (original, replacement):
            raise SystemExit(f"0x{address:08x}: expected {original.hex()}, found {found.hex()} ({why})")
        app[at : at + len(replacement)] = replacement
    appimage.reseal(app, image)
    out[OTA_0 : OTA_0 + OTA_SIZE] = app
    return bytes(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    if args.source.resolve() == args.output.resolve():
        raise SystemExit("refusing to overwrite the source dump")
    args.output.write_bytes(patch(args.source.read_bytes()))
    args.output.chmod(0o600)
    print(f"Wrote {args.output} with {len(PATCHES)} patches.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
