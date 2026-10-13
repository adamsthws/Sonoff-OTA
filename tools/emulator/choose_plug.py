#!/usr/bin/env python3
"""Choose which of your plugs the emulator runs, from their Sonoff backups.

Every conversion keeps the plug's Sonoff firmware in
captures/plugs/<mac>/sonoff-backup/ (tools/autoflash/plugs.py). This lists
them, by MAC address, and writes the chosen one's directory and MAC to --out,
a line each, for emu.sh. A dump in captures/emulation/ (flash.bin and
efuse.bin), the way the emulator used to be set up, is offered too.

The backups are only read: emu.sh boots a working copy in its state directory.

    python -I tools/emulator/choose_plug.py --out FILE [--plug MAC]

With one plug it is chosen without asking; with several, a menu asks - or,
with nobody at the keyboard, the plug chosen last time is used.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.autoflash import plugs  # noqa: E402
from tools.autoflash.prompts import QuitRun, ask_choice  # noqa: E402

LEGACY = ROOT / "captures" / "emulation"
HINT = [
    "Each plug is named by its MAC address",
]


@dataclass(frozen=True)
class Choice:
    mac: str
    directory: Path
    label: str


def choices() -> list[Choice]:
    found = [Choice(b.mac, b.directory, b.describe()) for b in plugs.backups()]
    flash, efuse = LEGACY / "flash.bin", LEGACY / "efuse.bin"
    if flash.is_file() and efuse.is_file():
        mac = plugs.mac_from_efuse(efuse.read_bytes())
        if all(c.mac != mac for c in found):
            found.append(Choice(mac, LEGACY, f"{mac}  (the dump in captures/emulation/)"))
    return found


def previous(out: Path) -> str | None:
    try:
        lines = out.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    return lines[1] if len(lines) > 1 else None


def choose(found: list[Choice], wanted: str | None, last: str | None, interactive: bool) -> Choice:
    if wanted:
        try:
            key = plugs.folder_name(wanted)
        except ValueError as exc:
            raise SystemExit(f"--plug: {exc}") from exc
        match = [c for c in found if plugs.folder_name(c.mac) == key]
        if not match:
            raise SystemExit(f"No Sonoff backup of a plug with MAC {wanted}. Backed up: "
                             + (", ".join(c.mac for c in found) or "none"))
        return match[0]
    if len(found) == 1:
        return found[0]
    # The one used last time first, so pressing 1 carries on with it.
    found = sorted(found, key=lambda c: c.mac != last)
    if interactive:
        return found[ask_choice("Which plug should the emulator run", [c.label for c in found],
                                help_lines=HINT)]
    if last and found[0].mac == last:
        return found[0]
    raise SystemExit("Several plugs are backed up; say which with --plug <MAC>: "
                     + ", ".join(c.mac for c in found))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--plug", help="the MAC address of the plug to emulate")
    args = parser.parse_args(argv)

    found = choices()
    if not found:
        print("No Sonoff backup to emulate yet. A conversion keeps one for each plug, in\n"
              "captures/plugs/<MAC>/sonoff-backup/; tools/s60_readout.py takes one without\n"
              "converting. See docs/emulator.md.", file=sys.stderr)
        return 1
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    try:
        chosen = choose(found, args.plug, previous(args.out), interactive)
    except QuitRun:
        return 130
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(f"{chosen.directory}\n{chosen.mac}\n", encoding="utf-8")
    print(f"Emulating {chosen.label}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
