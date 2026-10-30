"""Stage an rt_ota .rbl in the S61's download slot and boot the stock bootloader
over it in the emulator - the second half of a conversion, the one the plug does
by itself after the cloud-pushed download, and the whole of a reversion's apply.

A copy of a dump is made with the image at 0x286000 (the slot erased first), and
test Wi-Fi credentials in the EasyFlash store unless SEED=0, so the firmware the
bootloader installs has a network to join. The emulator models the flash writes
the bootloader makes (flash_writethrough: the RDID and the XIP mirror, patch
0003), and the flash as the run leaves it is saved to OUT: a second boot of OUT,
`bk-emu.sh run OUT --wifi --wifi-peer ap --wifi-ssid SSID --wifi-pass PASS`,
shows the installed firmware start afresh and join.

    bk-emu.sh python tools/beken/stage_apply.py DUMP IMAGE.rbl OUT
    (env: SECS=120, SSID, PASS, SEED=0 for no credentials; IMAGE "-" boots DUMP as it is)
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # the tools beside this one
import ef40  # noqa: E402

DOWNLOAD_SLOT = (0x286000, 0x3E0000)   # the stock download partition, up to the per-device region


def stage(flash: bytearray, image: bytes | None, credentials: dict[str, bytes] | None = None) -> bytearray:
    """`flash` with `image` in the erased download slot and `credentials` added to
    the EasyFlash store; either may be None. Changed in place and returned."""
    if credentials:
        ef40.provision(flash, credentials)
    if image is not None:
        start, end = DOWNLOAD_SLOT
        if image[:4] != b"RBL\0":
            raise ValueError("the image is not an rt_ota RBL (no 'RBL\\0' magic)")
        if len(image) > end - start:
            raise ValueError("the image (%d bytes) does not fit the download slot (%d bytes)"
                             % (len(image), end - start))
        flash[start:end] = b"\xff" * (end - start)
        flash[start:start + len(image)] = image
    return flash


def staged(flash: bytes, image: bytes) -> bool:
    """Whether `image` is in the download slot, byte for byte, where the stock
    bootloader applies it from - as the plug stages a download, verbatim."""
    start, end = DOWNLOAD_SLOT
    return len(image) <= end - start and flash[start:start + len(image)] == image


def boot(path: Path, secs: float) -> bytes:
    """Boot the flash at `path` from its bootloader for `secs` seconds, with the
    emulator modelling the flash writes, and save the flash as it ends to `path`."""
    root = Path(os.environ.get("BK_EMU_ROOT", "/opt/beken-emulator"))
    sys.path.insert(0, str(root))
    from src.main import build_emulator  # noqa: E402  (the emulator, in its container)
    emu = build_emulator(str(path), chip="BK7238", with_boot=True, only_uart=True, save_decrypted=False)
    emu.flash_writethrough = True
    emu.setup()
    started = time.time()
    threading.Thread(target=emu.run, daemon=True).start()
    time.sleep(secs)
    try:
        emu.stop()
    except Exception:   # the emulator may already have stopped by itself
        pass
    time.sleep(1)
    flash = bytes(emu.physical_flash if emu.physical_flash is not None else emu.raw_flash)
    path.write_bytes(flash)
    sys.stdout.flush()
    print("\n[stage] ran %.0f s; the flash as it ends is %s" % (time.time() - started, path), flush=True)
    return flash


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: stage_apply.py DUMP IMAGE.rbl OUT   (env: SECS, SSID, PASS, SEED=0; IMAGE - boots DUMP as it is)",
              file=sys.stderr)
        return 2
    dump, image, out = (Path(argv[0]), argv[1], Path(argv[2]))
    secs = float(os.environ.get("SECS", "120"))
    ssid, password = os.environ.get("SSID", "AutoflashNet"), os.environ.get("PASS", "autoflash1234")
    flash = bytearray(dump.read_bytes())
    print("[stage] EasyFlash keys:", " ".join(sorted(ef40.parse_store(flash))), flush=True)
    credentials = None if os.environ.get("SEED", "1") == "0" else {
        "wifi-ssid": ssid.encode(), "wifi-pass": password.encode()}
    rbl = None if image == "-" else Path(image).read_bytes()
    stage(flash, rbl, credentials)
    if rbl is not None:
        print("[stage] %d bytes of %s at 0x%x" % (len(rbl), Path(image).name, DOWNLOAD_SLOT[0]), flush=True)
    out.write_bytes(flash)

    boot(out, secs)
    os._exit(0)   # the emulator's threads do not all stop; the flash is saved


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
