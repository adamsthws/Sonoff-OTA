"""The plug on its new firmware: started, on the Wi-Fi, and answering as what
was installed (K1 OpenBeken, K2 ESPHome, K3 Sonoff's own firmware) - and,
on OpenBeken, the plug's own data backed up (B4), which only OpenBeken can
read out.

A plug that has not come back on its new firmware is the person's to switch
off and on, then looked for again (until_back); an unattended run stops there,
saying to.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING, TypeVar

from tools.autoflash import detect, esphome, join, kinds, openbeken, output, plugs, prompts
from tools.autoflash.errors import Abort
from tools.autoflash.routes.beken import images
from tools.beken import backup

if TYPE_CHECKING:
    from tools.autoflash.context import Context

# The bootloader applies the image and the new firmware joins the Wi-Fi within
# a minute or two; a router slow to hand out an address takes longer.
ARRIVAL_WAIT = 300.0
# How long eWeLink takes to show a plug back on Sonoff's firmware online.
CLOUD_WAIT = 300.0
# The emulated plug's power switch, where the run has one: a file holding off or
# on (tools/beken/emulator/bk-emu.sh computer), offered as a choice of its own.
EMULATED_POWER = "AUTOFLASH_EMULATOR_POWER"
EMULATED_OFF = 3.0   # seconds off: long enough for the emulator to stop the plug

T = TypeVar("T")


class NotBack(Exception):
    """The plug did not answer as its new firmware in the time waited."""


def until_back(look: Callable[[], T], what: str, advice: str) -> T:
    """look(), and again each time the person at the keyboard says the plug is
    back on after a restart, until it finds the plug or they stop (q).  An
    unattended run stops at the first NotBack, with `advice` as the reason."""
    while True:
        try:
            return look()
        except NotBack as exc:
            if prompts.UNATTENDED:
                raise Abort(advice) from exc
        output.transient_begin()
        prompts.section(f"{what} has not answered yet", [
            "The device installs its new firmware as it restarts, and has not come",
            "back yet. Switch the device off and on - at the socket, off for a few",
            "seconds - and it starts on its new firmware.",
        ])
        options = ["Look for it again - the device is back on"]
        switch = os.environ.get(EMULATED_POWER)
        if switch:
            options.append("Power-cycle the emulated device, then look for it again")
        chosen = prompts.ask_choice("Once it is back on, choose", options)
        output.transient_end()
        if chosen == 1:
            power_cycle_emulated(Path(switch))


def power_cycle_emulated(switch: Path) -> None:
    """The emulated plug switched off, then on, as at the socket."""
    switch.write_text("off\n", encoding="utf-8")
    time.sleep(EMULATED_OFF)
    switch.write_text("on\n", encoding="utf-8")
    output.say("    Power-cycled the emulated device.")


def step_openbeken_check(ctx: Context, wait: float = ARRIVAL_WAIT) -> dict[str, Any]:
    """K1 - OpenBeken started from the image, and on the home Wi-Fi: it
    answers at the plug's address (or wherever its hardware address is now)
    as the OpenBeken the tool installs on this kind of plug."""
    if ctx.dry_run:
        return {"dry_run": True}
    kind = kinds.of_run(ctx.state)
    started = time.monotonic()

    def check(host: str) -> dict[str, Any] | None:
        found = openbeken.info(host, timeout=3.0)
        if found and kinds.for_openbeken(found.get("shortName")) is kind:
            return found
        return None

    def look() -> dict[str, Any]:
        nonlocal started
        started = time.monotonic()
        try:
            return join.wait_for(ctx, check, "OpenBeken on your Wi-Fi", timeout=wait)
        except Abort as exc:
            raise NotBack from exc

    found = until_back(look, "OpenBeken", (
        "OpenBeken has not answered on your Wi-Fi. The device installs it as it restarts, then reads "
        "its Wi-Fi from the device's own store and joins; if it has not, switch the device off and on, "
        "then run the same command again to check it"))
    seconds = round(time.monotonic() - started, 1)
    version = openbeken.version(found)
    ctx.state.set_input("device_name", str(found.get("shortName") or ""))
    output.say(f"    OpenBeken {version} answered after {seconds}s, named {found.get('shortName')!r}.")
    return {"seconds": seconds, "version": version, "name": found.get("shortName")}


def step_backup(ctx: Context) -> dict[str, Any]:
    """B4 - the plug's own data, read out of it through OpenBeken: the factory
    record with its MAC and radio calibration, the stock store with its Wi-Fi,
    meter calibration and eWeLink identity, and the system store - and, with
    them, Sonoff's firmware as the image that puts it back (from the public
    dump).  Kept on this computer, never overwritten; the plug keeps its own.
    """
    mac = ctx.state.input("plug_mac")
    if ctx.dry_run or not mac:
        return {"dry_run": True} if ctx.dry_run else {"outcome": "not taken", "stage_outcome": "skipped"}
    outcome = _back_up(ctx, mac)
    ctx.state.set_input("backup_outcome", outcome)
    return {"outcome": outcome, "stage_outcome": plugs.BACKUP_OUTCOMES[outcome][0]}


def _back_up(ctx: Context, mac: str) -> str:
    if plugs.find_backup(mac):
        output.say("    The device's own data is backed up already.")
        return "existing"
    if getattr(getattr(ctx, "args", None), "no_backup", False):
        return "not wanted"
    kind = kinds.of_run(ctx.state)
    host = ctx.state.input("plug_ip")
    start, size = plugs.BEKEN_REGION
    try:
        region = openbeken.read_flash(
            host, start, size,
            progress=lambda done, total: output.progress(f"reading the device's own data: {100 * done // total}%"))
    except OSError as exc:
        output.progress_clear()
        output.tell(f"    The device's own data could not be read ({exc}); carrying on without a backup. "
                    "The device keeps it, and the data read is checked next time.")
        return "failed"
    finally:
        output.progress_clear()
    try:
        found = backup.factory_mac(region)
    except ValueError as exc:
        output.tell(f"    What was read is not the device's factory record ({exc}); no backup kept.")
        return "unreadable"
    if found.lower() != mac.lower():
        output.tell(f"    The data read names another device ({found}); no backup kept.")
        return "unreadable"
    try:
        stock, stock_version = images.stock_rbl(kind)
    except Abort as exc:
        output.tell(f"    {exc}: the device's data is kept, without Sonoff's firmware beside it.")
        stock, stock_version = None, str(ctx.state.input("stock_version") or "")
    directory, saved = plugs.save_beken_backup(mac, region, stock, stock_version, kind=kind.name)
    if not saved:
        return "existing"
    output.say(f"    Backed up the device's own data ({len(region):,} bytes) and Sonoff's firmware to "
               f"{directory.relative_to(plugs.CAPTURES.parent)}.")
    return "saved"


def step_esphome_check(ctx: Context, wait: float = ARRIVAL_WAIT) -> dict[str, Any]:
    """K2 - ESPHome started from the image, and on the home Wi-Fi, reading its
    calibration from the plug's own store."""
    if ctx.dry_run:
        return {"dry_run": True}
    started = time.monotonic()

    def check(host: str) -> bool:
        return esphome.is_esphome(host, timeout=3.0) and esphome.relay(host) is not None

    def look() -> bool:
        nonlocal started
        started = time.monotonic()
        try:
            return join.wait_for(ctx, check, "ESPHome on your Wi-Fi", timeout=wait)
        except Abort as exc:
            raise NotBack from exc

    until_back(look, "ESPHome", (
        "ESPHome has not answered on your Wi-Fi. The device installs it as it restarts, then reads its "
        "Wi-Fi from the device's own store and joins; if it has not, switch the device off and on, then "
        "run the same command again to check it"))
    seconds = round(time.monotonic() - started, 1)
    host = ctx.state.input("plug_ip")
    calibration = esphome.calibration_state(host)
    output.say(f"    ESPHome answered after {seconds}s; its meter calibration: {calibration or 'not said'}.")
    return {"seconds": seconds, "calibration": calibration}


def step_sonoff_check(ctx: Context, wait: float = CLOUD_WAIT) -> dict[str, Any]:
    """K3 - Sonoff's firmware started, and the plug back in eWeLink: the
    account shows it online again, with its identity as before."""
    if ctx.dry_run:
        return {"dry_run": True}
    device_id = str(plugs.read_plug(ctx.state.input("plug_mac") or "").get("ewelink_id") or "")
    host = ctx.state.input("plug_ip")

    def look() -> dict[str, Any]:
        started = time.monotonic()
        while time.monotonic() - started < wait:
            if host and detect.identify(host, timeout=3.0) in ("openbeken", "esphome"):
                output.progress(f"the device still runs the firmware it is leaving... {time.monotonic() - started:.0f}s")
            else:
                devices = ctx.account_devices() if device_id else None
                if devices is None and not device_id:
                    break
                for device in devices or []:
                    if device.get("deviceid") == device_id and device.get("online"):
                        output.progress_clear()
                        seconds = round(time.monotonic() - started, 1)
                        output.say(f"    eWeLink shows the device online again after {seconds}s.")
                        return {"seconds": seconds, "confirmed_by": "eWeLink"}
                output.progress(f"waiting for eWeLink to show the device online... {time.monotonic() - started:.0f}s")
            time.sleep(10)
        output.progress_clear()
        if host and detect.identify(host, timeout=3.0) in ("openbeken", "esphome"):
            raise Abort("the device still answers as the firmware it was to leave: Sonoff's firmware was not "
                        "installed. Run the same command again to send it again")
        if not device_id:
            output.tell("    The device no longer answers as OpenBeken or ESPHome. Its eWeLink device id is not "
                        "known here, so whether it is back in the app is for the app to show.")
            return {"seconds": round(time.monotonic() - started, 1), "confirmed_by": "nothing answering"}
        raise NotBack

    return until_back(look, "Sonoff's firmware", (
        "eWeLink has not shown the device online again. Sonoff's firmware rejoins the Wi-Fi the device was "
        "on and reports to eWeLink by itself; if the app still shows it offline, switch the device off "
        "and on, then run the same command again to check it"))
