#!/usr/bin/env python3
"""Image a stock S60's whole flash through the readout bridge (research only).

Reads the flash chip from the research bridge's read-only `GET /flash` (build
env `s60-ota-bridge-flash-readout`) and its eFuse read registers from
`GET /efuse`, checks the dump is complete and well formed, and reports what
each application slot holds and which one otadata selects.

The dump holds the plug's Wi-Fi password and eWeLink keys in cleartext. It is
written only under captures/ (never committed), readable by you alone.

tools/s60_readout.py does the whole readout and uses this; run this alone
only to read again from a readout bridge that is already on a plug:

    python3 tools/dump_stock_flash.py --host <bridge address>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import time
import urllib.request
import zlib
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.analyze_vendor_ota import parse_esp_image  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PARTITION_TABLE = 0x8000
PARTITION_MAGIC = 0x50AA
MD5_MAGIC = 0xEBEB
APP_DESC_MAGIC = 0xABCD5432
# esp_app_desc_t follows the image header (24) and first segment header (8).
APP_DESC_OFFSET = 32
OTA_SECTOR = 0x1000
OTA_STATES_REJECTED = {3, 4}  # ESP_OTA_IMG_INVALID, ESP_OTA_IMG_ABORTED
# "bridge v5 flash readout" or "bridge 4.4.1 flash readout".
READOUT_STATUS = "flash readout"


def parse_partition_table(flash: bytes) -> tuple[list[dict[str, Any]], bool]:
    """Partition entries, and whether the table's MD5 record matches them."""
    entries: list[dict[str, Any]] = []
    cursor = PARTITION_TABLE
    while cursor + 32 <= PARTITION_TABLE + 0xC00:
        magic = struct.unpack_from("<H", flash, cursor)[0]
        if magic != PARTITION_MAGIC:
            break
        _, ptype, subtype, offset, size, label, flags = struct.unpack_from("<HBBII16sI", flash, cursor)
        entries.append({
            "label": label.split(b"\0", 1)[0].decode("ascii", "replace"),
            "type": ptype, "subtype": subtype, "offset": offset, "size": size, "flags": flags,
        })
        cursor += 32
    md5_ok = (
        struct.unpack_from("<H", flash, cursor)[0] == MD5_MAGIC
        and flash[cursor + 16 : cursor + 32] == hashlib.md5(flash[PARTITION_TABLE:cursor]).digest()
    )
    return entries, md5_ok


def app_description(image: bytes) -> dict[str, str] | None:
    """The esp_app_desc_t fields that say what an application is."""
    if len(image) < APP_DESC_OFFSET + 256:
        return None
    if struct.unpack_from("<I", image, APP_DESC_OFFSET)[0] != APP_DESC_MAGIC:
        return None

    def text(start: int, size: int) -> str:
        field = image[APP_DESC_OFFSET + start : APP_DESC_OFFSET + start + size]
        return field.split(b"\0", 1)[0].decode("ascii", "replace")

    return {
        "version": text(16, 32), "project": text(48, 32),
        "time": text(80, 16), "date": text(96, 16), "idf": text(112, 32),
    }


def selected_ota_slot(otadata: bytes, slot_count: int) -> int | None:
    """The OTA slot otadata boots: the valid record with the highest sequence."""
    best = None
    for base in (0, OTA_SECTOR):
        seq, state, crc = struct.unpack_from("<I", otadata, base)[0], *struct.unpack_from("<II", otadata, base + 24)
        if seq in (0, 0xFFFFFFFF) or state in OTA_STATES_REJECTED:
            continue
        # ESP-IDF: esp_rom_crc32_le(UINT32_MAX, &ota_seq, 4).
        if zlib.crc32(struct.pack("<I", seq), 0xFFFFFFFF) != crc:
            continue
        best = seq if best is None else max(best, seq)
    return None if best is None or slot_count == 0 else (best - 1) % slot_count


def check(flash: bytes) -> dict[str, Any]:
    """Everything a dump has to show before it is worth keeping."""
    report: dict[str, Any] = {"size": len(flash), "sha256": hashlib.sha256(flash).hexdigest()}
    try:
        boot = parse_esp_image(flash[:PARTITION_TABLE])
        report["bootloader"] = {"checksum_valid": boot["checksum_valid"], "sha256_valid": boot["sha256_valid"]}
    except ValueError as exc:
        report["bootloader"] = {"error": str(exc)}

    entries, md5_ok = parse_partition_table(flash)
    report["partition_table_md5_valid"] = md5_ok
    report["partitions"] = []
    ota_slots = sorted((e for e in entries if e["type"] == 0 and 0x10 <= e["subtype"] <= 0x1F),
                       key=lambda e: e["subtype"])
    otadata = next((e for e in entries if e["type"] == 1 and e["subtype"] == 0), None)
    for entry in entries:
        item = {"label": entry["label"], "offset": f"0x{entry['offset']:06x}",
                "size": f"0x{entry['size']:06x}", "within_dump": entry["offset"] + entry["size"] <= len(flash)}
        if entry["type"] == 0 and item["within_dump"]:
            image = flash[entry["offset"] : entry["offset"] + entry["size"]]
            try:
                parsed = parse_esp_image(image)
                item["image_valid"] = parsed["checksum_valid"] and parsed["sha256_valid"] is not False
                item["app"] = app_description(image)
            except ValueError as exc:
                item["image_valid"] = False
                item["error"] = str(exc)
        report["partitions"].append(item)

    selected = None
    if otadata is not None and ota_slots:
        data = flash[otadata["offset"] : otadata["offset"] + otadata["size"]]
        index = selected_ota_slot(data, len(ota_slots))
        selected = ota_slots[index]["label"] if index is not None else None
    report["otadata_selects"] = selected
    return report


def fetch(host: str, path: str, timeout: float = 60.0) -> tuple[bytes, dict[str, str]]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://{host}{path}", timeout=timeout) as response:
        return response.read(), dict(response.headers)


def write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def is_readout_bridge(host: str) -> bool:
    """True when `host` answers as the flash-readout bridge."""
    try:
        status, _ = fetch(host, "/status", timeout=5)
    except OSError:
        return False
    return READOUT_STATUS in status.decode("ascii", "replace")


def save_dump(host: str, label: str) -> int:
    """Read, check and save one dump from the readout bridge at `host`.

    0 when the dump is complete and well formed; nothing is kept from a short
    transfer.
    """
    print("Reading the whole flash (about a minute)...")
    flash, headers = fetch(host, "/flash", timeout=300)
    declared = int(headers.get("X-Flash-Size", "0"))
    if declared == 0 or len(flash) != declared:
        print(f"Incomplete: received {len(flash):,} of {declared:,} bytes. Nothing kept.", file=sys.stderr)
        return 1
    efuse, _ = fetch(host, "/efuse", timeout=10)

    report = check(flash)
    report["efuse_sha256"] = hashlib.sha256(efuse).hexdigest()
    out = ROOT / "captures" / "flash-readout" / f"{label}-{time.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True, mode=0o700)
    write_private(out / "flash.bin", flash)
    write_private(out / "efuse.bin", efuse)
    write_private(out / "report.json", (json.dumps(report, indent=2) + "\n").encode())

    print(f"Saved to {out.relative_to(ROOT)} (private: it holds the Wi-Fi password).")
    print(f"  {len(flash):,} bytes, SHA-256 {report['sha256']}")
    print(f"  partition table MD5: {'valid' if report['partition_table_md5_valid'] else 'INVALID'}")
    for item in report["partitions"]:
        app = item.get("app")
        what = f"{app['project']} {app['version']} ({app['date']})" if app else item.get("error", "")
        valid = "" if "image_valid" not in item else (" valid" if item["image_valid"] else " INVALID")
        print(f"  {item['label']:<10} {item['offset']}{valid} {what}")
    print(f"  otadata boots: {report['otadata_selects']}")
    good = report["partition_table_md5_valid"] and report["otadata_selects"] is not None
    return 0 if good else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="192.168.4.1")
    parser.add_argument("--label", default="stock", help="name for this dump, e.g. the stock version")
    args = parser.parse_args(argv)

    if not is_readout_bridge(args.host):
        print(f"{args.host} is not the flash-readout bridge.", file=sys.stderr)
        return 2
    code = save_dump(args.host, args.label)
    if code == 0:
        print("\nSwitch the plug off and on: it boots its Sonoff firmware again.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
