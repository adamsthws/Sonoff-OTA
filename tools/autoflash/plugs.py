"""Where each plug's records live: one directory a plug, named by its MAC.

    captures/plugs/aa-bb-cc-dd-ee-ff/
        plug.json          what the plug is: its eWeLink name and id, its model
        sonoff-backup/     its Sonoff firmware, whole, as it came - never rewritten
        runs/20261013-031500/   one conversion: its state, its log, what it read

A run starts in plugs/unidentified/runs/ and moves under the plug's MAC as soon
as the plug is found (A1), before anything is written to it.  A plug's MAC
never changes, so a later run - or the emulator - finds its backup by it.

Everything here is private: the backup holds the home Wi-Fi password and the
plug's eWeLink keys, and a MAC identifies the plug.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CAPTURES = ROOT / "captures"
PLUGS = CAPTURES / "plugs"
# Runs whose plug is not found yet.
UNIDENTIFIED = "unidentified"
BACKUP = "sonoff-backup"
RUNS = "runs"
PLUG_FILE = "plug.json"
MAC_RE = re.compile(r"^(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}$", re.IGNORECASE)
# The eFuse dump's base MAC: words 6 and 7 of EFUSE_RD_MAC_SPI_SYS, bytes reversed.
EFUSE_MAC = slice(0x18, 0x1E)


def folder_name(mac: str) -> str:
    """A MAC as a directory name: lower case, with dashes (Windows allows no colons)."""
    if not MAC_RE.match(mac.strip()):
        raise ValueError(f"not a MAC address: {mac!r}")
    return mac.strip().lower().replace(":", "-")


def plug_dir(mac: str) -> Path:
    return PLUGS / folder_name(mac)


def backup_dir(mac: str) -> Path:
    return plug_dir(mac) / BACKUP


def runs_dir(mac: str | None) -> Path:
    return (plug_dir(mac) if mac else PLUGS / UNIDENTIFIED) / RUNS


def new_run_dir(mac: str | None) -> Path:
    """A directory for a new run, named by when it started; not yet made."""
    base = runs_dir(mac) / time.strftime("%Y%m%d-%H%M%S")
    path, suffix = base, 2
    while path.exists():
        path, suffix = base.with_name(f"{base.name}-{suffix}"), suffix + 1
    return path


def mac_of_run(directory: Path) -> str | None:
    """The plug a run belongs to, from where it is kept; None if not found yet."""
    try:
        relative = directory.resolve().relative_to(PLUGS.resolve())
    except ValueError:
        return None
    name = relative.parts[0] if relative.parts else ""
    return name.replace("-", ":") if MAC_RE.match(name) else None


def mac_from_efuse(efuse: bytes) -> str:
    """The plug's MAC from its eFuse registers, as the chip itself reports it."""
    return ":".join(f"{b:02x}" for b in efuse[EFUSE_MAC][::-1])


def read_plug(mac: str) -> dict[str, Any]:
    try:
        return json.loads((plug_dir(mac) / PLUG_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def remember_plug(mac: str, **details: Any) -> None:
    """Add what is known about a plug to its plug.json; empty values are left out."""
    record = read_plug(mac)
    record.update({key: value for key, value in details.items() if value not in (None, "")})
    record["mac"] = folder_name(mac).replace("-", ":")
    path = plug_dir(mac) / PLUG_FILE
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class Backup:
    """One plug's Sonoff backup, as the emulator and later runs see it."""
    mac: str
    directory: Path
    version: str
    taken: str
    name: str

    def describe(self) -> str:
        """For a menu: the MAC first, since that is what tells plugs apart."""
        extra = [f"Sonoff {self.version}"] + ([f"backed up {self.taken}"] if self.taken else [])
        named = f'"{self.name}", ' if self.name and self.name != "Sonoff S60" else ""
        return f"{self.mac}  ({named}{', '.join(extra)})"


def find_backup(mac: str) -> Backup | None:
    directory = backup_dir(mac)
    if not ((directory / "flash.bin").is_file() and (directory / "efuse.bin").is_file()):
        return None
    record = read_plug(mac)
    return Backup(mac=folder_name(mac).replace("-", ":"), directory=directory,
                  version=str(record.get("stock_version") or "unknown version"),
                  taken=str(record.get("backed_up") or ""), name=str(record.get("ewelink_name") or ""))


def backups() -> list[Backup]:
    """Every plug with a Sonoff backup, most recently backed up first."""
    if not PLUGS.is_dir():
        return []
    found = [find_backup(child.name) for child in PLUGS.iterdir()
             if child.is_dir() and MAC_RE.match(child.name)]
    return sorted((b for b in found if b), key=lambda b: b.taken, reverse=True)


README = """\
The Sonoff firmware of the Sonoff S60 with MAC address {mac}, backed up on
{date}, just before Tasmota was written over it. It was running Sonoff
{version}.

  flash.bin    the plug's whole flash chip: bootloader, partition table, the
               Sonoff firmware, its settings, its eWeLink device id and keys
  efuse.bin    the chip's eFuse registers; only the emulator needs them
  report.json  what the backup holds, as checked when it was taken

This is the original: nothing rewrites it, and the files are read-only. Keep
it private. flash.bin holds your Wi-Fi password and the plug's eWeLink keys in
plain text. It belongs to this plug only - the MAC above is on no other.

Going back to the Sonoff firmware
  Write flash.bin back over USB-serial. That means opening the plug (its case
  is welded shut) and wiring a 3.3 V USB-serial adapter to the ESP32-C3 - never
  with the plug in a mains socket. Then, with the chip held in its download
  mode:

      esptool.py --chip esp32c3 write_flash 0x0 flash.bin

  It boots the Sonoff {version} it had, with its settings and eWeLink identity.
  One of its two app slots holds the conversion's small installer instead of
  an older Sonoff version; the plug boots the other.

Running it in the emulator
  tools/emulator/emu.sh offers every backed-up plug by its MAC address and
  runs a copy; this backup is never changed (docs/emulator.md).
"""


def _write_read_only(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def save_backup(flash: bytes, efuse: bytes, report: dict[str, Any],
                version: str | None, date: str | None = None) -> tuple[Path, bool]:
    """Keep a plug's Sonoff firmware as its original backup.

    Returns where it is, and whether it was saved now: a plug that has one
    already keeps it, whatever was read this time.  The MAC comes from the
    chip's own eFuses, so it is filed under the plug it came from.
    """
    mac = mac_from_efuse(efuse)
    directory = backup_dir(mac)
    if (directory / "flash.bin").exists():
        return directory, False
    directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=f".{BACKUP}.", dir=directory.parent))
    _write_read_only(staging / "flash.bin", flash)
    _write_read_only(staging / "efuse.bin", efuse)
    _write_read_only(staging / "report.json", (json.dumps(report, indent=2) + "\n").encode())
    date = date or time.strftime("%Y-%m-%d")
    readme = README.format(mac=mac, date=date, version=version or "(unknown version)")
    _write_read_only(staging / "README.txt", readme.encode())
    # Whole or not at all: a backup is only ever seen complete.
    staging.rename(directory)
    remember_plug(mac, stock_version=version, backed_up=date)
    return directory, True
