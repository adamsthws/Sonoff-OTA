"""Back to Sonoff's own firmware, from the plug's backup (R1, R2).
"""

from __future__ import annotations

import time
from typing import Any, TYPE_CHECKING

from tools.autoflash import detect, join, output, plans, plugs
from tools.autoflash.errors import Abort
from tools.autoflash.routes.espressif import hub

if TYPE_CHECKING:
    from tools.autoflash.context import Context


def sonoff_backup(ctx: Context) -> bytes:
    """The plug's own Sonoff backup, checked to be this plug's."""
    mac = ctx.state.input("plug_mac")
    backup = plugs.find_backup(mac) if mac else None
    if backup is None:
        raise Abort("there is no backup of this device's Sonoff firmware on this computer")
    try:
        return (backup.directory / "flash.bin").read_bytes()
    except OSError as exc:
        raise Abort(f"the backup could not be read: {exc}") from exc


def step_sonoff_restore(ctx: Context) -> dict[str, Any]:
    """R1 - the plug's own Sonoff firmware and settings, from its backup, beside
    the Tasmota it runs; the boot selection set to it last, then a restart.
    Two plans, as Sonoff's firmware is more than one may write."""
    try:
        first, second = plans.back_to_sonoff(sonoff_backup(ctx), running=1)
    except plans.PlanError as exc:
        raise Abort(f"the backup cannot be written back: {exc}") from exc
    hub.run_plan(ctx, first)
    return hub.run_plan(ctx, second)


def sonoff_answers(address: str) -> bool:
    return detect.identify(address, timeout=3.0) == "sonoff"


def step_sonoff_check(ctx: Context, wait: float = 240.0) -> dict[str, Any]:
    """R2 - Sonoff's firmware started and back on the Wi-Fi it had."""
    if ctx.dry_run:
        return {"dry_run": True}
    started = time.monotonic()
    try:
        join.wait_for(ctx, sonoff_answers, "Sonoff's firmware on your Wi-Fi", timeout=wait)
    except Abort as exc:
        raise Abort(
            "Sonoff's firmware was written and checked, but has not answered on your Wi-Fi. "
            "If the network dropped while it first started, it does not try again: switch "
            "the device off and on, then run the same command again to check it. It rejoins the "
            "network it was on when the backup was taken; if that has changed, add it again "
            "in the eWeLink app"
        ) from exc
    seconds = round(time.monotonic() - started, 1)
    output.say(f"    Sonoff's firmware answered after {seconds}s.")
    return {"seconds": seconds}
