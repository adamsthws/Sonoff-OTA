#!/usr/bin/env python3
"""A test copy of a backed-up plug under another MAC, for the emulator: the
same flash, the new MAC in its eFuses - so every name made from the MAC
(autoflash-recovery-XXXX, tasmota-xxxxxx, ...) can be seen to follow it.

    python -I tools/emulator/copy_plug.py --from MAC --to MAC
    python -I tools/emulator/copy_plug.py --remove MAC

The original is only read. The copy goes in captures/plugs/<new mac>/, marked
as a test copy (plug.json's test_copy_of); --remove deletes only such a copy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.autoflash import plugs  # noqa: E402

# Where the base MAC sits in the eFuse read registers, last byte first.
MAC_AT = slice(0x18, 0x1E)


def with_mac(efuse: bytes, mac: str) -> bytes:
    data = bytearray(efuse)
    data[MAC_AT] = bytes.fromhex(mac.replace(":", ""))[::-1]
    return bytes(data)


def copy(source_mac: str, mac: str) -> Path:
    source = plugs.PLUGS / plugs.folder_name(source_mac) / "sonoff-backup"
    target = plugs.PLUGS / plugs.folder_name(mac)
    if not (source / "flash.bin").is_file():
        raise SystemExit(f"No backed-up plug {source_mac} ({source})")
    if target.exists():
        raise SystemExit(f"{target} is there already")
    efuse = with_mac((source / "efuse.bin").read_bytes(), mac)
    if plugs.mac_from_efuse(efuse) != mac:
        raise SystemExit(f"{mac} did not read back from the new eFuses")
    backup = target / "sonoff-backup"
    backup.mkdir(parents=True)
    shutil.copyfile(source / "flash.bin", backup / "flash.bin")
    (backup / "efuse.bin").write_bytes(efuse)
    report = json.loads((source / "report.json").read_text())
    report["efuse_sha256"] = hashlib.sha256(efuse).hexdigest()
    report["note"] = f"Emulator test copy of {source_mac}: the same flash, another MAC in its eFuses."
    (backup / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    (backup / "README.txt").write_text((source / "README.txt").read_text().replace(source_mac, mac))
    (target / "plug.json").write_text(json.dumps({
        "backed_up": time.strftime("%Y-%m-%d"), "ewelink_name": "Emulator test copy",
        "mac": mac, "stock_version": report.get("stock_version", ""), "test_copy_of": source_mac,
    }, indent=2) + "\n")
    for path in backup.iterdir():
        path.chmod(0o400)
    return target


def remove(mac: str) -> None:
    target = plugs.PLUGS / plugs.folder_name(mac)
    try:
        record = json.loads((target / "plug.json").read_text())
    except (OSError, ValueError):
        raise SystemExit(f"{target} is not a test copy made by this tool") from None
    if not record.get("test_copy_of"):
        raise SystemExit(f"{target} is a real plug's backup, not a test copy: left alone")
    for path in (target / "sonoff-backup").glob("*"):
        path.chmod(0o600)
    shutil.rmtree(target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="source", help="the backed-up plug to copy")
    parser.add_argument("--to", help="the copy's MAC")
    parser.add_argument("--remove", help="a test copy to delete")
    args = parser.parse_args()
    if args.remove:
        remove(args.remove.lower().replace("-", ":"))
        print(f"Removed the test copy {args.remove}.")
        return 0
    if not (args.source and args.to):
        parser.error("--from and --to, or --remove")
    target = copy(args.source.lower().replace("-", ":"), args.to.lower().replace("-", ":"))
    print(f"Test copy made: {target} (emu.sh --plug {args.to})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
