"""A whole S61 conversion in the emulator, the way the tool would drive one: the
plug comes online against the local eWeLink stub (cloud_online.CloudSession),
the stub pushes it an `upgrade` for a firmware image, and the plug downloads it
and stages it in its download slot - then, with --apply, the stock bootloader
applies it on the next boot and the new firmware starts.

    bk-emu.sh python tools/beken/cloud_push.py IMAGE.rbl OUT.bin [--apply]
    (env: SECS=400, the most the push may take; APPLY_SECS=120 for the apply; SSID, PASS)

IMAGE is an rt_ota .rbl: OpenBeken's 4 MB-table build, the ESPHome (LibreTiny)
build's image_bk7238_app.ota.rbl, or OpenBeken's release. It is wrapped in
CoolKit's FOTA container (fota_wrap.py) and served from a placeholder public
address the stub's relay sends to this host, as the cloud's download is: the
plug fetches it in ranges, checks the `upgrade` command's digest, and writes it
to 0x286000 verbatim. OUT is the flash it leaves - checked to hold the image,
byte for byte, where the bootloader looks - and with --apply the flash after
the apply.

The plug's Wi-Fi credentials are written into a copy of the dump's EasyFlash
store first (stage_apply.stage), so the firmware it ends on finds them there
as on a provisioned plug; the dump itself is never written.

Proven with it (2026-10-28, as scratch drivers; the notes in
docs/beken-s61-emulation.md "Firmware update and conversion"): the descriptor
name FWSW-01-S61-BK7238 selects the staging handler, the plug's HTTP client
wants 206 answers, and nothing in the chain checks a signature.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # the tools beside this one
import cloud_online  # noqa: E402  (the emulator, in its container)
import fota_wrap  # noqa: E402
import stage_apply  # noqa: E402

OTA_PORT = 8080   # the firmware server's port at the placeholder address


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Push an rt_ota image to the emulated S61 through the eWeLink stub.")
    ap.add_argument("image", type=Path, help="the .rbl to convert to")
    ap.add_argument("out", type=Path, help="where the flash is written as the plug leaves it")
    ap.add_argument("--apply", action="store_true", help="then boot it, for the stock bootloader to apply it")
    ap.add_argument("--dump", type=Path, default=Path(cloud_online.DUMP), help="the stock dump (default: S61_DUMP)")
    args = ap.parse_args(argv)
    secs = float(os.environ.get("SECS", "400"))

    rbl = args.image.read_bytes()
    version = fota_wrap.rbl_version(rbl)
    container = fota_wrap.wrap(rbl)
    digest = hashlib.sha256(container).hexdigest()
    print("[push] %s: RBL %d bytes, version %s; FOTA container %d bytes, sha256 %s"
          % (args.image.name, len(rbl), version, len(container), digest[:16]), flush=True)

    flash = bytearray(args.dump.read_bytes())
    stage_apply.stage(flash, None, {"wifi-ssid": cloud_online.SSID, "wifi-pass": cloud_online.PW})
    args.out.write_bytes(flash)

    server = fota_wrap.server(container, "127.0.0.1", 0, quiet=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    session = cloud_online.CloudSession(str(args.out), flash_writethrough=True,
                                        routes={(cloud_online.DISPATCH_IP, OTA_PORT): server.server_address[1]})
    url = "http://%s:%d/user1.bin" % (cloud_online.DISPATCH_IP, OTA_PORT)

    # Once the plug is online and reports its state, push the upgrade, as the
    # cloud does when the app asks for one: through the plug's own connection.
    registered: dict = {}
    pushed: list[str] = []
    heard = session.stub.heard

    def heard_and_push(message):
        answer = heard(message)
        if isinstance(message, dict) and message.get("action") == "register":
            registered.update(message)
        elif (not pushed and isinstance(message, dict) and message.get("action") in ("query", "update")
                and registered.get("deviceid")):
            command = fota_wrap.upgrade_command(registered, url, digest, version)
            if cloud_online.ss.send_to_device(registered["deviceid"], command):
                pushed.append(url)
                session.stublog.append("pusher -> pushed upgrade %s" % url)
        return answer
    session.stub.heard = heard_and_push

    # Until the image is staged: the download is the plug's, in 2 KB ranges, and
    # takes minutes in the emulator.
    session.run(secs, until=lambda: stage_apply.staged(session.flash(), rbl))
    out = session.flash()
    args.out.write_bytes(out)
    _, _, _, online = session.online()
    ok = stage_apply.staged(out, rbl)
    print("=== S61 CLOUD PUSH ===", flush=True)
    for line in session.stublog:
        if re.search(r"register|websocket|pushed|upgrade|error", line):
            print("    " + re.sub(r"^\S+\s", "", line)[:150], flush=True)
    print("  online: %s | upgrade pushed: %s | ranges fetched: %d | staged at 0x%x byte for byte: %s"
          % (online, bool(pushed), server.requests, stage_apply.DOWNLOAD_SLOT[0], ok), flush=True)
    if not ok:
        print("  FAILED: the image is not in the download slot; the flash is in %s" % args.out, flush=True)
        os._exit(1)
    if args.apply:
        print("=== the next boot: the stock bootloader applies it ===", flush=True)
        stage_apply.boot(args.out, float(os.environ.get("APPLY_SECS", "120")))
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
