#!/usr/bin/env python3
"""Make a saved plug public: nothing of Sonoff's, or of the real plug's, in it.

    python -I tools/espressif/emulator/sanitise_plug.py SAVED OUT --stock DUMP_DIR

SAVED is a plug kept with unattended-test.sh --save, on the made-up MAC of a
test copy (copy_plug.py), carried at least as far as D5: past it the plug runs
none of Sonoff's code - the bridge and the interim Tasmota have taken its app
slots, D5 its bootloader - so from there on the final firmware can be tested
with nothing of Sonoff's at all (docs/updating-firmware.md). DUMP_DIR is the
real plug's Sonoff backup (flash.bin, efuse.bin), only read, to prove the
result holds nothing of it.

The flash is rebuilt from blank, and only what is shown to be public is
copied in: the bootloader, the partition table and app images byte for byte
the published files in artifacts/, the startup firmware the published one
with the emulator's open network written in, the boot selection, and the
settings of the interim Tasmota alone, naming only that network. Everything
else - Sonoff's settings (its eWeLink keys, the Wi-Fi it was set up with, its
calibration and energy history), the copy of them the repartition keeps, and
any leftover - is not copied. Of the eFuses only the MAC (the test copy's)
and what every such chip shares are kept, nothing measured on the one chip
(EFUSE_KEPT_BITS). Of what the computer keeps, only the
run's state and its steps' reports, with the account and the eWeLink device
named by placeholders: not the Sonoff backup, logs or the firmware bundle,
which the run builds again.

Then every file written is searched: for any 256-byte block of the dump,
every value of Sonoff's settings in it, the real MAC in any form, and the
eWeLink account and device on this computer. Anything found and nothing is
written. OUT gets flash.bin, efuse.bin, flash-mode ("public"), captures/ and
SANITISED.json, what each region is and how it was shown.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools.autoflash import esphome, imprint, plugs, tasmota_settings as ts  # noqa: E402
from tools.espressif.emulator.copy_plug import MAC_AT  # noqa: E402
from tools.inspect_stock_nvs import parse as parse_nvs  # noqa: E402
from tools.sonoff_secrets import PUBLIC_FIELDS, sonoff_values  # noqa: E402,F401
from tools.safeboot_migration import (  # noqa: E402
    PUBLISHED_BLUETOOTH_FILE,
    PUBLISHED_RECOVERY_FILE,
    SOURCE_TABLE_SHA256,
    TARGET_TABLE_SHA256,
    parse_partition_sector,
    sha256_bytes,
)

FLASH_SIZE = 0x400000
TABLE_AT, TABLE_SIZE = 0x8000, 0x1000
SECTOR = 0x1000
BLOCK = 256
EMULATOR_WIFI = "PICSimLabWifi"  # the emulator's open network (rewrite_nvs.py)
PLACEHOLDER_EMAIL = "autoflash-test@example.invalid"
PLACEHOLDER_DEVICE = ("1000abcdef", "Sonoff S60")
# eFuse file (QEMU's: the read registers from 0x2C on, little-endian): BLOCK0's
# settings in its first 0x18 bytes, then BLOCK1 (0x18) and BLOCK2 (0x30), each
# bit as ESP-IDF's esp_efuse_table.csv for the ESP32-C3 numbers it. Of those,
# only what every chip of its kind shares is kept - the MAC (the test copy's),
# the chip's revision and package, which the bootloader checks, and the flash's
# type - not what was measured on the one chip: its calibration, and its
# factory unique ID. The rest (user data, keys) is blank.
EFUSE_BLOCK0 = (0x00, 0x18)
EFUSE_KEPT_BITS = {
    0x18: ((0, 48),      # MAC
           (114, 3),     # WAFER_VERSION_MINOR_LO
           (117, 3),     # PKG_VERSION
           (120, 3),     # BLK_VERSION_MINOR
           (123, 3),     # FLASH_CAP
           (126, 2),     # FLASH_TEMP
           (128, 3),     # FLASH_VENDOR
           (183, 1),     # WAFER_VERSION_MINOR_HI
           (184, 2)),    # WAFER_VERSION_MAJOR
    0x30: ((128, 2),),   # BLK_VERSION_MAJOR
}
EFUSE_SIZE = 1024
# What the computer keeps that the next steps read: the run's state, the
# report of each step done so far (G1 holds the commit to its record), and
# which run is current. Nothing else.
KEPT_REPORTS = re.compile(r"^(live/[a-z0-9-]+-report\.json|live/live-partition-table\.bin|"
                          r"live/live-old-otadata\.bin|autoflash-state\.json)$")


class Refused(RuntimeError):
    pass


# -- the flash ---------------------------------------------------------------

def nvs_rows_of_interim_tasmota(nvs: bytes) -> tuple[list[tuple[str, str, str, str]], str]:
    """The interim Tasmota's settings record alone, as nvs_partition_gen rows,
    shown to name no network but the emulator's."""
    names, values = parse_nvs(nvs)
    record = next((value.data for (ns, key), value in values.items()
                   if names.get(ns) == "main" and key == "Settings"), None)
    if record is None:
        raise Refused("the plug's settings hold no Tasmota record (main/Settings)")
    settings = record[:ts.SIZE]
    wifi = [ts.text(settings, index) for index in (ts.SET_STASSID1, ts.SET_STAPWD1,
                                                   ts.SET_STASSID2, ts.SET_STAPWD2)]
    if wifi != [EMULATOR_WIFI, "", "", ""]:
        raise Refused("the Tasmota record names another network than the emulator's")
    return [("main", "namespace", "", ""), ("Settings", "data", "hex2bin", record.hex())], \
        f"Tasmota's record alone ({len(record)} bytes), Wi-Fi '{EMULATOR_WIFI}' with no password"


def rebuilt_nvs(rows: list[tuple[str, str, str, str]], size: int) -> bytes:
    with tempfile.TemporaryDirectory(prefix="autoflash-public-nvs-") as tmp:
        csv, out = Path(tmp) / "nvs.csv", Path(tmp) / "nvs.bin"
        csv.write_text("key,type,encoding,value\n" + "".join(",".join(r) + "\n" for r in rows))
        made = subprocess.run([sys.executable, "-m", "esp_idf_nvs_partition_gen", "generate",
                               "--outdir", tmp, str(csv), str(out), hex(size)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if made.returncode:
            raise Refused(f"the settings could not be rebuilt: {made.stderr.strip()}")
        return out.read_bytes()


def public_image(slot: bytes, image: bytes, label: str) -> bytes | None:
    """`image` if the slot starts with it, else None."""
    return image if slot[:len(image)] == image else None


def sanitise_flash(flash: bytes) -> tuple[bytes, dict[str, str]]:
    out = bytearray(b"\xff" * FLASH_SIZE)
    proof: dict[str, str] = {}

    bootloader = esphome.published_bootloader()
    if flash[:len(bootloader)] != bootloader:
        raise Refused("its bootloader is not the published one: carry the plug past D5 first, "
                      "which writes it in place of Sonoff's")
    out[:len(bootloader)] = bootloader
    proof["bootloader"] = f"{esphome.BOOTLOADER.relative_to(ROOT)}, byte for byte"

    table = flash[TABLE_AT:TABLE_AT + TABLE_SIZE]
    layout = {SOURCE_TABLE_SHA256: "sonoff", TARGET_TABLE_SHA256: "tasmota"}.get(sha256_bytes(table))
    if layout is None:
        raise Refused("its partition table is neither Sonoff's nor Tasmota's")
    out[TABLE_AT:TABLE_AT + TABLE_SIZE] = table
    proof["table"] = ("Sonoff's, as artifacts/sonoff-s60-partition-table.bin" if layout == "sonoff"
                      else "Tasmota's, as its factory image has it")
    partitions = {entry["label"]: entry for entry in parse_partition_sector(table)["entries"]}

    def place(label: str, data: bytes) -> None:
        entry = partitions[label]
        out[entry["offset"]:entry["offset"] + len(data)] = data

    def region(label: str) -> bytes:
        entry = partitions[label]
        return flash[entry["offset"]:entry["end"]]

    rows, said = nvs_rows_of_interim_tasmota(region("nvs"))
    place("nvs", rebuilt_nvs(rows, partitions["nvs"]["size"]))
    proof["nvs"] = said

    otadata = region("otadata")
    for at in range(0, len(otadata), SECTOR):
        if otadata[at + 32:at + SECTOR] != b"\xff" * (SECTOR - 32):
            raise Refused("its boot selection holds more than a boot selection")
    place("otadata", otadata)
    proof["otadata"] = "the boot selection, as it stood"

    bluetooth = PUBLISHED_BLUETOOTH_FILE.read_bytes()
    if layout == "sonoff":
        for label in ("ota_0", "ota_1"):
            image = public_image(region(label), bluetooth, label)
            if image is not None:
                place(label, image)
                proof[label] = f"{PUBLISHED_BLUETOOTH_FILE.relative_to(ROOT)}, byte for byte"
            else:
                proof[label] = "left out (erased): not a published image"
        for label in ("reserve", "phy_init"):
            proof[label] = "left out (erased): Sonoff's"
    else:
        startup = imprint.imprint(PUBLISHED_RECOVERY_FILE.read_bytes(), EMULATOR_WIFI, "")
        if region("safeboot")[:len(startup)] != startup:
            raise Refused("its Safeboot partition is not the published startup firmware "
                          "with the emulator's network written in")
        place("safeboot", startup)
        proof["safeboot"] = (f"{PUBLISHED_RECOVERY_FILE.relative_to(ROOT)} with '{EMULATOR_WIFI}' "
                             "written in, byte for byte")
        # Before F2: what is left of Sonoff's slots, and the copy of its
        # settings the repartition keeps, until the official app is written.
        proof["app0"] = "left out (erased): leftovers, written over at F2"
        proof["spiffs"] = "left out (erased): leftovers"
    return bytes(out), proof


def sanitise_efuse(efuse: bytes, mac: str) -> bytes:
    if plugs.mac_from_efuse(efuse) != mac:
        raise Refused("the eFuses are not the test copy's")
    out = bytearray(EFUSE_SIZE)
    out[EFUSE_BLOCK0[0]:EFUSE_BLOCK0[1]] = efuse[EFUSE_BLOCK0[0]:EFUSE_BLOCK0[1]]
    for block, fields in EFUSE_KEPT_BITS.items():
        for first, count in fields:
            for bit in range(first, first + count):
                at, mask = block + bit // 8, 1 << (bit % 8)
                out[at] |= efuse[at] & mask
    if plugs.mac_from_efuse(bytes(out)) != mac or out[MAC_AT] != efuse[MAC_AT]:
        raise Refused("the MAC did not survive")
    return bytes(out)


# -- what the computer keeps -------------------------------------------------

def sanitise_captures(source: Path, target: Path, mac: str) -> list[str]:
    """The current run's state and reports, the plug's record, the locks."""
    folder = plugs.folder_name(mac)
    current = (source / ".autoflash-current").read_text(encoding="utf-8").strip()
    run = source / current
    if not current.startswith(f"plugs/{folder}/runs/") or not (run / "autoflash-state.json").is_file():
        raise Refused("the saved captures have no current run of this plug")
    kept = []
    for path in sorted(run.rglob("*")):
        relative = path.relative_to(run).as_posix()
        if path.is_file() and KEPT_REPORTS.match(relative):
            destination = target / current / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
            kept.append(f"{current}/{relative}")
    state_path = target / current / "autoflash-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["inputs"]["email"] = PLACEHOLDER_EMAIL
    state["inputs"]["stock_backup"] = ""
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    record = json.loads((source / f"plugs/{folder}/plug.json").read_text(encoding="utf-8"))
    record["ewelink_id"], record["ewelink_name"] = PLACEHOLDER_DEVICE
    (target / f"plugs/{folder}/plug.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    kept.append(f"plugs/{folder}/plug.json")
    (target / ".autoflash-current").write_text(current + "\n", encoding="utf-8")
    kept.append(".autoflash-current")
    locks = source / ".safety-locks"
    if locks.is_dir():
        shutil.copytree(locks, target / ".safety-locks")
        kept += [f".safety-locks/{p.relative_to(locks).as_posix()}" for p in sorted(locks.rglob("*"))]
    return kept


# -- the proof ---------------------------------------------------------------

def mac_forms(mac: str) -> set[bytes]:
    raw = bytes.fromhex(mac.replace(":", ""))
    hexes = mac.replace(":", "")
    forms = {raw, raw[::-1]}
    for text in (mac, mac.replace(":", "-"), hexes, hexes[6:]):
        forms |= {text.encode(), text.upper().encode()}
    return forms


def local_identities() -> set[bytes]:
    """The eWeLink account and the real plugs' eWeLink devices, where this
    computer has them."""
    found: set[bytes] = set()
    try:
        email = json.loads((ROOT / "captures/settings.json").read_text())["ewelink_email"]
        found.add(email.encode())
    except (OSError, ValueError, KeyError):
        pass
    from tools.autoflash import credentials
    try:
        email = credentials.parse(credentials.ENV_FILE.read_text(encoding="utf-8")).get("EWELINK_EMAIL")
    except (OSError, ValueError):
        email = None
    if email:
        found.add(email.encode())
    for record in (ROOT / "captures/plugs").glob("*/plug.json"):
        try:
            data = json.loads(record.read_text())
        except ValueError:
            continue
        if data.get("ewelink_id") and data["ewelink_id"] != PLACEHOLDER_DEVICE[0]:
            found.add(data["ewelink_id"].encode())
    return {value for value in found if len(value) >= 6}


def prove(out: Path, stock: bytes, real_mac: str, explain: bool = False) -> dict[str, int]:
    table = (ROOT / "artifacts/sonoff-s60-partition-table.bin").read_bytes()
    published = {table[i:i + BLOCK] for i in range(0, len(table), BLOCK)}
    blocks = {stock[i:i + BLOCK] for i in range(0, len(stock), BLOCK)
              if stock[i:i + BLOCK].count(stock[i:i + 1]) != BLOCK} - published
    sonoff = sonoff_values(stock)
    needles = {"a value of Sonoff's settings": set(sonoff),
               "the real MAC": mac_forms(real_mac),
               "the eWeLink account or device": local_identities()}
    problems = []
    files = sorted(p for p in out.rglob("*") if p.is_file())
    for path in files:
        data = path.read_bytes()
        name = path.relative_to(out).as_posix()
        hits = sum(1 for i in range(0, len(data) - BLOCK + 1, BLOCK) if data[i:i + BLOCK] in blocks)
        if hits:
            problems.append(f"{name}: {hits} block(s) of the Sonoff dump")
        for what, values in needles.items():
            hits = [value for value in values if value in data]
            if hits:
                said = ""
                if explain and what.startswith("a value"):
                    said = " (" + ", ".join(sorted(f"{sonoff[v]}, {len(v)} bytes" for v in hits)) + ")"
                problems.append(f"{name}: {what}{said}")
    if problems:
        raise Refused("not public:\n  " + "\n  ".join(problems))
    return {"files": len(files), "dump_blocks_sought": len(blocks),
            "values_sought": sum(len(values) for values in needles.values())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("saved", type=Path, help="a plug kept with unattended-test.sh --save")
    parser.add_argument("out", type=Path, help="where the public plug goes (must not exist)")
    parser.add_argument("--stock", type=Path, required=True,
                        help="the real plug's Sonoff backup directory (flash.bin, efuse.bin), only read")
    parser.add_argument("--explain", action="store_true",
                        help="refused: say which of Sonoff's settings each value found came from (never the value)")
    args = parser.parse_args(argv)
    if args.out.exists():
        raise SystemExit(f"{args.out} is there already")
    stock = (args.stock / "flash.bin").read_bytes()
    real_mac = plugs.mac_from_efuse((args.stock / "efuse.bin").read_bytes())
    captures = args.saved / "captures"
    backups = [p for p in (captures / "plugs").glob("*/sonoff-backup/efuse.bin")]
    if len(backups) != 1:
        raise SystemExit("the saved plug should hold one plug's captures")
    efuse = backups[0].read_bytes()
    mac = plugs.mac_from_efuse(efuse)
    if mac == real_mac:
        raise SystemExit("the saved plug is the real one: save a test copy's (copy_plug.py)")

    tmp = Path(tempfile.mkdtemp(prefix="autoflash-public-", dir=args.out.parent))
    try:
        flash, proof = sanitise_flash((args.saved / "flash.bin").read_bytes())
        (tmp / "flash.bin").write_bytes(flash)
        (tmp / "efuse.bin").write_bytes(sanitise_efuse(efuse, mac))
        (tmp / "flash-mode").write_text("public\n")
        kept = sanitise_captures(captures, tmp / "captures", mac)
        checked = prove(tmp, stock, real_mac, args.explain)
        report = {
            "schema": 1,
            "mac": mac,
            "flash_sha256": hashlib.sha256(flash).hexdigest(),
            "efuse_sha256": hashlib.sha256((tmp / "efuse.bin").read_bytes()).hexdigest(),
            "flash": proof,
            "efuse": "the test copy's MAC, the chip's revision and package, and the flash's type; "
                     "nothing measured on the chip (calibration, unique ID), no user data or keys",
            "captures": kept,
            "checked": checked,
        }
        (tmp / "SANITISED.json").write_text(json.dumps(report, indent=2) + "\n")
        prove(tmp, stock, real_mac)  # the report too
        os.replace(tmp, args.out)
    except Refused as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    print(f"Public plug written to {args.out}:")
    for label, said in proof.items():
        print(f"  {label:10} {said}")
    print(f"  checked {checked['files']} files for {checked['dump_blocks_sought']:,} blocks of the dump "
          f"and {checked['values_sought']} values: none found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
