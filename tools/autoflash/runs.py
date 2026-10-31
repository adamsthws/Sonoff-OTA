"""A run's record on disk: where it is, which run is current, under which plug
it is filed, and the settings kept between runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TYPE_CHECKING

from tools.autoflash import output, plugs
from tools.autoflash.errors import Abort
from tools.autoflash.state import RunState

if TYPE_CHECKING:
    from tools.autoflash.account import AccountPlug
    from tools.autoflash.context import Context

ROOT = Path(__file__).resolve().parents[2]
SETTINGS_PATH = ROOT / "captures" / "settings.json"


def run_name(directory: Path) -> str:
    """How a run is named: its place under captures/, such as
    plugs/aa-bb-cc-dd-ee-ff/runs/20261013-031500 (tools/autoflash/plugs.py)."""
    try:
        return directory.relative_to(plugs.CAPTURES).as_posix()
    except ValueError:
        return directory.name


def run_directories() -> list[Path]:
    captures = plugs.CAPTURES
    if not captures.is_dir():
        return []
    # Runs from before plugs/ were kept straight under captures/.
    candidates = [*captures.glob(f"plugs/*/{plugs.RUNS}/*"), *captures.iterdir()]
    return sorted(
        (child for child in candidates if (child / "autoflash-state.json").is_file()),
        key=lambda child: (child / "autoflash-state.json").stat().st_mtime,
    )


CURRENT_POINTER = ROOT / "captures" / ".autoflash-current"


def remember_current(name: str) -> None:
    try:
        CURRENT_POINTER.parent.mkdir(parents=True, exist_ok=True)
        CURRENT_POINTER.write_text(name + "\n", encoding="utf-8")
    except OSError:
        pass  # the pointer is a convenience, never a requirement


def read_current() -> str | None:
    try:
        name = CURRENT_POINTER.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return name or None


def new_run_name(plug_ip: str | None) -> str:
    """A name the operator never has to type: when it started, filed under
    plugs/unidentified/ until A1 finds the plug and moves it under its MAC."""
    return run_name(plugs.new_run_dir(None))


def load_settings() -> dict[str, Any]:
    """Preferences kept between runs on this computer - never secrets."""
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict[str, Any]) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")


def hold_plug(ctx: Context, mac: str) -> None:
    """One run on a plug at a time: two at once would each write over what the
    other is doing."""
    try:
        plugs.hold(mac, run_name(ctx.dir) if hasattr(ctx, "dir") else "")
    except plugs.PlugBusy as busy:
        raise Abort(
            f"another run is converting this device ({mac}) right now"
            + (f" - {busy}" if str(busy) else "")
            + ". Let it finish, or stop it first, then run this again. Nothing was done"
        ) from None


def file_plug(ctx: Context, mac: str, account: AccountPlug | None) -> None:
    """Keep this run, and what is known of the plug, under its MAC; say if its
    Sonoff firmware is backed up from an earlier run."""
    hold_plug(ctx, mac)
    if hasattr(ctx, "file_under_plug"):
        ctx.file_under_plug(mac)
    if account:
        plugs.remember_plug(mac, ewelink_name=account.name, ewelink_id=account.device_id,
                            model=account.model)
    backup = plugs.find_backup(mac)
    if backup:
        output.say(f"    Its Sonoff firmware is backed up already{f' ({backup.taken})' if backup.taken else ''},"
            f" in {display_path(backup.directory)}.")


def plug_runs_tasmota(state: RunState) -> bool:
    """Whether the plug runs a Tasmota at this point of its run: everywhere but
    on a route from ESPHome before X2, and after the plug has left Tasmota."""
    source = run_source(state)
    if source == "safeboot":
        # Its safeboot says nothing of the Wi-Fi; from T1 it is a Tasmota's run.
        if not state.is_done("T1"):
            return False
        source = "tasmota"
    if source == "untemplated":
        source = "tasmota"
    if source not in ("tasmota", "esphome"):
        return True
    if source == "tasmota" and not state.is_done("L1"):
        return True
    return state.is_done("X2") and not state.is_done("I1") and not state.is_done("R1")


def run_firmware(state: RunState) -> str:
    """A run's firmware; one from before there was a choice installs Tasmota."""
    return state.input("firmware") or "tasmota"


def run_source(state: RunState) -> str:
    """What the plug ran at a run's start; before there was a choice, Sonoff's."""
    return state.input("source") or "sonoff"


def display_path(path: Path) -> str:
    """A path as short as it can be: relative to the repository when it is inside."""
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


# Files a run can leave that hold secrets: the plug's eWeLink LAN key (no
# longer fetched), update links that may act as tokens, the plug's settings
# memory, and the staged copy of the recovery image with the Wi-Fi password in
# it.  Their reports keep only hashes, which is all later steps use.
SECRET_FILES = (
    "device-key.json",
    "ota-metadata.json",
    "live/live-canonical-nvs.bin",
    "live/staged-safeboot-readback.bin",
)


def forget_run_secrets(directory: Path) -> list[str]:
    """Delete one run's secret files.  Returns what was removed."""
    removed = []
    for name in SECRET_FILES:
        path = directory / name
        if path.exists():
            path.unlink()
            removed.append(str(path.relative_to(ROOT)))
    return removed
