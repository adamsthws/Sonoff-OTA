#!/usr/bin/env python3
"""Make a copy of the stock 1.2.0 flash dump that boots under QEMU.

Each patch names the instruction it replaces and checks the original bytes
first, so it can only apply to the Sonoff 1.2.0 image it was written for. The
image is then re-sealed (checksum and SHA-256) so the bootloader accepts it.
The source dump is never modified.

    python -I tools/espressif/emulator/patch_stock.py captures/emulation/flash.bin captures/emulation/run-flash.bin
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.espressif.emulator import appimage  # noqa: E402

OTA_0 = 0x20000
OTA_SIZE = 0x1F0000
RETURN_0 = bytes.fromhex("01458280")  # c.li a0,0 ; c.jr ra
PROLOGUE = bytes.fromhex("5d7186c6")  # c.addi16sp sp,-80 ; c.swsp ra,76(sp)

# The stock app pins a single self-signed coolkit root CA, embedded twice,
# byte-identical, as a null-terminated PEM C-string. Each copy sits in a fixed
# 1040-byte slot (1036-byte PEM + "\r\n\x00\x00"); the next byte is unrelated
# data ("GET %s?deviceid=..."), so the slot length must be preserved exactly.
# ota_0-relative offsets (flash 0x38d4c / 0x39ed8 minus OTA_0).
CA_SLOTS = (0x18D4C, 0x19ED8)
CA_SLOT_BYTES = 1040
PEM_BEGIN = b"-----BEGIN CERTIFICATE-----"
PEM_END = b"-----END CERTIFICATE-----"

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


def _is_coolkit_ca(pem: bytes) -> bool:
    """True if `pem` is the stock coolkit root CA (by issuer org/CN)."""
    from cryptography import x509  # local import: only needed for --trust-ca
    try:
        cert = x509.load_pem_x509_certificate(pem)
    except ValueError:
        return False
    org = cert.subject.get_attributes_for_oid(x509.oid.NameOID.ORGANIZATION_NAME)
    cn = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
    return (bool(org) and org[0].value == "coolkit"
            and bool(cn) and cn[0].value == "coolkit.cn")


def swap_trust_anchor(app: bytearray, ca_pem: bytes) -> None:
    """Replace both coolkit CA slots in ota_0 with our own CA PEM.

    Each slot is checked to currently hold the coolkit CA, then overwritten with
    `ca_pem` null-padded to the original 1040-byte slot length, so every later
    offset (and the unrelated data right after each slot) is left untouched. The
    firmware parses the slot as a null-terminated string, so trailing nulls and
    the PEM's own `END CERTIFICATE` marker bound what mbedTLS reads.
    """
    if len(ca_pem) >= CA_SLOT_BYTES:
        raise SystemExit(f"replacement CA PEM is {len(ca_pem)} bytes; must be < "
                         f"{CA_SLOT_BYTES} to fit the trust slot (use an EC CA)")
    if _is_coolkit_ca(ca_pem):  # must be our own CA, never coolkit's
        raise SystemExit("replacement CA is the coolkit CA; expected our own CA")
    padded = ca_pem + b"\x00" * (CA_SLOT_BYTES - len(ca_pem))
    for at in CA_SLOTS:
        slot = bytes(app[at : at + CA_SLOT_BYTES])
        begin = slot.find(PEM_BEGIN)
        end = slot.find(PEM_END)
        if begin != 0 or end < 0:
            raise SystemExit(f"ota_0 0x{at:05x}: no coolkit CA PEM at the expected slot")
        existing = slot[: end + len(PEM_END)]
        if not _is_coolkit_ca(existing):
            raise SystemExit(f"ota_0 0x{at:05x}: slot is not the coolkit CA; refusing to patch")
        app[at : at + CA_SLOT_BYTES] = padded


def patch(flash: bytes, ca_pem: bytes | None = None) -> bytes:
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
    if ca_pem is not None:
        swap_trust_anchor(app, ca_pem)
    appimage.reseal(app, image)
    out[OTA_0 : OTA_0 + OTA_SIZE] = app
    return bytes(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--trust-ca", type=Path, default=None,
                        help="PEM of our own CA to swap in for the coolkit root "
                             "(cloud-stub mode); omit for a plain boot image")
    args = parser.parse_args(argv)
    if args.source.resolve() == args.output.resolve():
        raise SystemExit("refusing to overwrite the source dump")
    ca_pem = args.trust_ca.read_bytes() if args.trust_ca else None
    args.output.write_bytes(patch(args.source.read_bytes(), ca_pem))
    args.output.chmod(0o600)
    extra = " + trust-anchor swap" if ca_pem else ""
    print(f"Wrote {args.output} with {len(PATCHES)} patches{extra}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
