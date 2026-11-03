"""The plug on its new firmware: started, on the Wi-Fi, and answering as what
was installed (K1 OpenBeken, J1 OpenBeken as the interim firmware, K2
ESPHome, K3 Sonoff's own firmware) - and, on OpenBeken, the plug's own data
backed up (B4), which only OpenBeken can read out.

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
from tools.autoflash.errors import Abort, GoBack
from tools.autoflash.routes.beken import images, onward
from tools.beken import backup
from tools.ewelink import relay as ewelink_relay

if TYPE_CHECKING:
    from tools.autoflash.context import Context

# The bootloader applies the image and the new firmware joins the Wi-Fi within
# a minute or two: after three, whatever was under way has finished, and the
# person is asked to switch the device off and on (until_back).
ARRIVAL_WAIT = 180.0
# How long eWeLink takes to show a plug back on Sonoff's firmware online,
# before the same question.
CLOUD_WAIT = 180.0
# The emulated plug's power switch, where the run has one: a file holding off or
# on (tools/beken/emulator/bk-emu.sh computer), offered as a choice of its own.
EMULATED_POWER = "AUTOFLASH_EMULATOR_POWER"
EMULATED_OFF = 3.0   # seconds off: long enough for the emulator to stop the plug
# An OpenBeken's uptime set against the time since it fetched an image: this
# much shorter, and it started after the fetch, not before (step_esphome_check).
RESTART_SLACK = 10

T = TypeVar("T")


class NotBack(Exception):
    """The plug did not answer as its new firmware in the time waited."""


class NotTaken(Exception):
    """Restarted, the plug started the firmware it was leaving: the image it
    was sent was not installed, and will not be by waiting."""


def until_back(ctx: Context, look: Callable[[bool], T], what: str, advice: str,
               sent_by: str | None = None) -> T:
    """look(restarted), and again each time the person at the keyboard says
    the plug is back on after a restart - `restarted` then True - until it
    finds the plug or they stop (q).  An unattended run stops at the first
    NotBack, with `advice` as the reason.  Each switch off and on is counted,
    by whom (power_cycles), for the report: normally none is needed, as
    every firmware restarts by itself once it has its update.

    `sent_by`: the step that sent the image.  A look after a restart that
    finds the plug on the firmware it was leaving (NotTaken) sends the run
    back to that step, unasked: the bootloader installs a whole image on the
    first start after it arrived, so one not installed then never will be.
    Before a restart that says nothing: the plug may not have restarted.
    A switch off and on asked for is remembered in the run's record
    (restart_asked), so a run that stopped to ask - an unattended one, or a
    person who quit there - takes it as done when it is run again."""
    restarted = bool(ctx.state.input("restart_asked"))
    while True:
        try:
            found = look(restarted)
        except NotTaken as exc:
            ctx.state.set_input("restart_asked", None)
            if sent_by is None:
                raise Abort(advice) from exc
            raise GoBack(sent_by, f"{what} was not installed: the device restarted on the firmware it had",
                         "not installed") from exc
        except NotBack as exc:
            ctx.state.set_input("restart_asked", True)
            if prompts.UNATTENDED:
                raise Abort(advice) from exc
        else:
            ctx.state.set_input("restart_asked", None)
            return found
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
        counted = dict(ctx.state.input("power_cycles") or {})
        who = "by the tool" if chosen == 1 else "by hand"
        counted[who] = counted.get(who, 0) + 1
        ctx.state.set_input("power_cycles", counted)
        if chosen == 1:
            power_cycle_emulated(Path(switch))
        restarted = True


def power_cycle_emulated(switch: Path) -> None:
    """The emulated plug switched off, then on, as at the socket."""
    switch.write_text("off\n", encoding="utf-8")
    time.sleep(EMULATED_OFF)
    switch.write_text("on\n", encoding="utf-8")
    output.say("    Power-cycled the emulated device.")


def step_openbeken_check(ctx: Context, wait: float = ARRIVAL_WAIT, *, interim: bool = False) -> dict[str, Any]:
    """K1 - OpenBeken started from the image, and on the home Wi-Fi: it
    answers at the plug's address (or wherever its hardware address is now)
    as the OpenBeken the tool installs on this kind of plug - and, as its
    driver says, not as the interim firmware; J1 (`interim`) the
    same, as the interim firmware, which it is named to the person as.  One
    that says otherwise is told what it is: the plug's own OpenBeken, then
    restarted, so the name it keeps follows; the interim firmware (the
    marker B2 sends after it cut off, say) not restarted, so it is never
    named wrongly on the network, only not renamed for its few minutes."""
    if ctx.dry_run:
        return {"dry_run": True}
    shown = images.INTERIM_NAME if interim else "OpenBeken"
    kind = kinds.of_run(ctx.state)
    started = time.monotonic()

    # Sent by Sonoff's firmware (B2): one still answering after a restart
    # did not take the image.
    sent_by = "B2" if ctx.state.is_done("B2") else None

    def check(host: str, restarted: bool) -> dict[str, Any] | None:
        found = openbeken.info(host, timeout=3.0)
        if found and openbeken.kind(host, found, timeout=3.0) is kind:
            answer = openbeken.is_interim(host, timeout=3.0)
            if answer is not None:
                return {**found, "interim": answer}
        if restarted and sent_by and detect.identify(host, timeout=3.0) == "sonoff":
            raise NotTaken
        return None

    def look(restarted: bool) -> dict[str, Any]:
        nonlocal started
        started = time.monotonic()
        try:
            return join.wait_for(ctx, lambda host: check(host, restarted), f"{shown} on your Wi-Fi", timeout=wait)
        except Abort as exc:
            raise NotBack from exc

    title = shown[0].upper() + shown[1:]
    found = until_back(ctx, look, title, (
        f"{title} has not answered on your Wi-Fi. The device installs it as it restarts, then reads its "
        "Wi-Fi from the device's own store and joins; if it has not, switch the device off and on, then run "
        "the same command again to check it"), sent_by=sent_by)
    seconds = round(time.monotonic() - started, 1)
    version = openbeken.version(found)
    settled = found["interim"] == interim
    if interim:
        # Not the plug's name for good: the firmware chosen names it.
        output.say(f"    {title} {version} answered after {seconds}s.")
    if not settled:
        onward.set_interim(ctx, interim, (
            "It started without the marker sent after it (its update cut off at the very end): it is the "
            "interim firmware now, under the name it started with." if interim else
            "It had the interim firmware's name: given its own."), restart=not interim)
    if interim:
        return {"seconds": seconds, "version": version, **({} if settled else {"told": "interim"})}
    if not settled:
        found = openbeken.info(ctx.state.input("plug_ip")) or {}
    name = str(found.get("shortName") or "")
    ctx.state.set_input("device_name", name)
    output.say(f"    OpenBeken {version} answered after {seconds}s, named {name!r}.")
    return {"seconds": seconds, "version": version, "name": name, **({} if settled else {"told": "not interim"})}


def step_interim_check(ctx: Context, wait: float = ARRIVAL_WAIT) -> dict[str, Any]:
    """J1 - the interim firmware (OpenBeken, images.INTERIM) started and on
    the home Wi-Fi, as K1 - there only to back the plug up, then fetch the
    firmware chosen - and saying it is the interim firmware (AutoflashInterim)."""
    return step_openbeken_check(ctx, wait, interim=True)


def step_backup(ctx: Context) -> dict[str, Any]:
    """B4 - the plug's own data, read out of it through OpenBeken's
    /api/flash - the firmware chosen, or the interim one: the factory record
    with its MAC and radio calibration, the stock store with its Wi-Fi, meter
    calibration and eWeLink identity, and the system store - and, with them,
    Sonoff's firmware as the image that puts it back (from the public dump).
    Kept on this computer, never overwritten; the plug keeps its own.
    """
    mac = ctx.state.input("plug_mac")
    if ctx.dry_run or not mac:
        return {"dry_run": True} if ctx.dry_run else {"outcome": "not taken", "stage_outcome": "skipped"}
    outcome = _back_up(ctx, mac)
    ctx.state.set_input("backup_outcome", outcome)
    return {"outcome": outcome, "stage_outcome": plugs.BACKUP_OUTCOMES[outcome][0]}


def _read_region(host: str) -> bytes:
    """The plug's own data, read through OpenBeken."""
    start, size = plugs.BEKEN_REGION

    def progress(done: int, total: int) -> None:
        output.progress(f"reading the device's own data: {100 * done // total}%")
    return openbeken.read_flash(host, start, size, progress=progress)


def _back_up(ctx: Context, mac: str) -> str:
    if plugs.find_backup(mac):
        output.say("    The device's own data is backed up already.")
        return "existing"
    if getattr(getattr(ctx, "args", None), "no_backup", False):
        return "not wanted"
    kind = kinds.of_run(ctx.state)
    host = ctx.state.input("plug_ip")
    try:
        region = _read_region(host)
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
    calibration from the plug's own store.

    OpenBeken fetches ESPHome (J2, or O1) and restarts by itself to install
    it: one answering that has started since - its uptime below what it was
    before the fetch, or than the time since - its bootloader did not take
    the image, and the run goes back to send it again (until_back), no
    restart to ask for.  Both, as the emulated plug's clock runs fast: there
    its uptime outgrows the time since, but not on a real plug."""
    if ctx.dry_run:
        return {"dry_run": True}
    started = time.monotonic()
    captures = ctx.state.data.get("captures", {})
    sent_by = next((step for step in ("J2", "O1")
                    if ctx.state.is_done(step) and isinstance(captures.get(f"{step}.fetched_at"), int)), None)

    def restarted_since_sent(uptime: int) -> bool:
        before = captures.get(f"{sent_by}.uptime_before")
        return ((isinstance(before, int) and uptime < before)
                or uptime < time.time() - captures[f"{sent_by}.fetched_at"] - RESTART_SLACK)

    def check(host: str) -> bool:
        if esphome.is_esphome(host, timeout=3.0) and esphome.relay(host) is not None:
            return True
        if sent_by:
            uptime = (openbeken.info(host, timeout=3.0) or {}).get("uptime_s")
            if isinstance(uptime, int) and restarted_since_sent(uptime):
                raise NotTaken
        return False

    def look(_restarted: bool) -> bool:
        nonlocal started
        started = time.monotonic()
        try:
            return join.wait_for(ctx, check, "ESPHome on your Wi-Fi", timeout=wait)
        except Abort as exc:
            raise NotBack from exc

    until_back(ctx, look, "ESPHome", (
        "ESPHome has not answered on your Wi-Fi. The device installs it as it restarts, then reads its "
        "Wi-Fi from the device's own store and joins; if it has not, switch the device off and on, then "
        "run the same command again to check it"), sent_by=sent_by)
    seconds = round(time.monotonic() - started, 1)
    host = ctx.state.input("plug_ip")
    calibration = esphome.calibration_state(host)
    output.say(f"    ESPHome answered after {seconds}s; its meter calibration: {calibration or 'not said'}.")
    return {"seconds": seconds, "calibration": calibration}


def step_sonoff_check(ctx: Context, wait: float = CLOUD_WAIT) -> dict[str, Any]:
    """K3 - Sonoff's firmware started, and the plug back in eWeLink: the
    account shows it online again, with its identity as before, and it
    answers through eWeLink - so not online still from before it left."""
    if ctx.dry_run:
        return {"dry_run": True}
    device_id = str(plugs.read_plug(ctx.state.input("plug_mac") or "").get("ewelink_id") or "")
    host = ctx.state.input("plug_ip")

    def look(_restarted: bool) -> dict[str, Any]:
        started = time.monotonic()
        while time.monotonic() - started < wait:
            if host and detect.identify(host, timeout=3.0) in ("openbeken", "interim", "esphome"):
                output.progress(f"the device still runs the firmware it is leaving... {time.monotonic() - started:.0f}s")
            else:
                devices = ctx.account_devices() if device_id else None
                if devices is None and not device_id:
                    break
                online = any(device.get("deviceid") == device_id and device.get("online") for device in devices or [])
                if online and ewelink_relay.answers(
                        email=ctx.email(), password=ctx.ewelink_env()["EWELINK_PASSWORD"], country_code="+44",
                        region="eu", device_id=device_id):
                    output.progress_clear()
                    seconds = round(time.monotonic() - started, 1)
                    output.say(f"    eWeLink shows the device online again, and it answers, after {seconds}s.")
                    return {"seconds": seconds, "confirmed_by": "eWeLink"}
                output.progress(f"waiting for eWeLink to show the device online"
                                f"{', and for it to answer' if online else ''}... {time.monotonic() - started:.0f}s")
            time.sleep(10)
        output.progress_clear()
        if host and detect.identify(host, timeout=3.0) in ("openbeken", "interim", "esphome"):
            raise Abort("the device still answers as the firmware it was to leave: Sonoff's firmware was not "
                        "installed. Run the same command again to send it again")
        if not device_id:
            output.tell("    The device no longer answers as the firmware it left. Its eWeLink device id is not "
                        "known here, so whether it is back in the app is for the app to show.")
            return {"seconds": round(time.monotonic() - started, 1), "confirmed_by": "nothing answering"}
        raise NotBack

    return until_back(ctx, look, "Sonoff's firmware", (
        "eWeLink has not shown the device online again, answering there. Sonoff's firmware rejoins the Wi-Fi the device was "
        "on and reports to eWeLink by itself; if the app still shows it offline, switch the device off "
        "and on, then run the same command again to check it"))
