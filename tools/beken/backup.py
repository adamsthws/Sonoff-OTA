"""Back up a converted S61's stock per-device data from OpenBeken.

The conversion leaves everything that makes the plug *this* plug untouched: the
factory TLV store (MAC, radio calibration) at 0x3e0000, the EasyFlash config
store (Wi-Fi creds, energy-meter calibration, eWeLink identity) and the system
ENV, all in 0x3e0000-0x400000. OpenBeken can read flash over its REST API, so
right after the plug joins we copy that region out and file it the way the S60
conversion files its Sonoff backup:

    captures/plugs/<mac-with-dashes>/sonoff-backup/
        config-0x3e0000.bin   the 128 KiB per-device region, as read
        stock-app-1.0.2.rbl   the generic stock app, repackaged for the stock
                              bootloader's download slot (when --stock-rbl is given)
        backup.json           what this is: model, offsets, hashes, when

With these a plug can be put back to stock later whatever happens: stage the
stock RBL (S61RevertToStock), and if the config region were ever lost, rewrite
it from the copy. The backup is never overwritten once made.

  python3 backup.py --plug http://192.168.1.100 [--stock-rbl stock_app_1.0.2.rbl]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.autoflash import plugs  # noqa: E402  (shares the S60's captures layout)

REGION_START = 0x3E0000
REGION_END = 0x400000
# One request per 16 KiB: the device's HTTP server is single-threaded and its
# lwIP keeps each closed connection in TIME_WAIT for a while, so few, larger,
# paced requests are what it copes with.
CHUNK = 0x4000
GAP_S = 1.0
BACKUP_BIN = "config-0x3e0000.bin"
MANIFEST = "backup.json"


def read_flash(base: str, start: int, length: int, timeout: float = 30.0) -> bytes:
    """GET /api/flash/<start>-<len> in sector chunks (the device is slow and single-threaded)."""
    out = bytearray()
    for a in range(start, start + length, CHUNK):
        n = min(CHUNK, start + length - a)
        url = "%s/api/flash/%x-%x" % (base.rstrip("/"), a, n)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(url, timeout=timeout) as r:
                    data = r.read()
                if len(data) == n:
                    break
            except Exception:
                data = b""
            time.sleep(1.0 + attempt)
        if len(data) != n:
            raise RuntimeError("short read at 0x%x: got %d of %d bytes" % (a, len(data), n))
        out += data
        time.sleep(GAP_S)
    return bytes(out)


def factory_mac(region: bytes) -> str:
    """The MAC from the factory TLV at the start of the region: section 0x111111, tag 0x02."""
    if region[:4] != b"TLV\x00":
        raise ValueError("no factory TLV at 0x3e0000")
    pos, end = 8, 8 + struct.unpack_from("<I", region, 4)[0]
    while pos + 8 <= end:
        tag, marker = region[pos], region[pos + 1:pos + 4]
        ln = struct.unpack_from("<I", region, pos + 4)[0]
        if tag == 0x00:
            pos += 8
            continue
        if marker == b"\x11\x11\x11" and tag == 0x02 and ln == 6:
            return ":".join("%02x" % b for b in region[pos + 8:pos + 14])
        pos += 8 + ln
    raise ValueError("no MAC in the factory TLV")


def main() -> int:
    ap = argparse.ArgumentParser(description="Back up a converted S61's stock config region from OpenBeken.")
    ap.add_argument("--plug", required=True, help="OpenBeken base URL, e.g. http://192.168.1.100")
    ap.add_argument("--stock-rbl", default=None, help="repackaged stock app RBL to file beside it")
    ap.add_argument("--captures", default=None, help="captures directory (default: the repository's)")
    a = ap.parse_args()

    region = read_flash(a.plug, REGION_START, REGION_END - REGION_START)
    mac = factory_mac(region)
    plugs_dir = (Path(a.captures) / "plugs") if a.captures else plugs.PLUGS
    backup = plugs_dir / plugs.folder_name(mac) / plugs.BACKUP
    if (backup / BACKUP_BIN).exists():
        print("backup already exists, never overwritten: %s" % (backup / BACKUP_BIN))
        return 0
    backup.mkdir(parents=True, exist_ok=True)
    (backup / BACKUP_BIN).write_bytes(region)
    manifest = {
        "device": "Sonoff S61 (BK7238)",
        "mac": mac,
        "taken": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": "OpenBeken GET /api/flash after conversion",
        "regions": {
            BACKUP_BIN: {
                "flash_offset": "0x%x" % REGION_START,
                "length": REGION_END - REGION_START,
                "sha256": hashlib.sha256(region).hexdigest(),
                "contents": "factory TLV (MAC, RF cal) @0x3e0000; EasyFlash app store (wifi-ssid/wifi-pass, "
                            "elecCalibCoef, eWeLink keys) @0x3e2000; EasyFlash system ENV @0x3f6000",
            }
        },
        "restore": "stage stock-app-*.rbl in the download slot (S61RevertToStock); "
                   "rewrite config-0x3e0000.bin at 0x3e0000 only if that region was lost",
    }
    if a.stock_rbl:
        rbl = Path(a.stock_rbl)
        dest = backup / ("stock-app-%s" % rbl.name.split("_", 2)[-1] if "_" in rbl.name else rbl.name)
        shutil.copyfile(rbl, dest)
        manifest["regions"][dest.name] = {
            "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
            "contents": "generic stock app, repackaged as an rt_ota RBL for the stock bootloader",
        }
    (backup / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print("backed up %d bytes of 0x%x for %s -> %s" % (len(region), REGION_START, mac, backup))
    return 0


if __name__ == "__main__":
    sys.exit(main())
