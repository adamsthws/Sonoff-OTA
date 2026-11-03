"""ESPHome, and the way to and from it: ESPHome written from the hub (I1,
I2), Tasmota left for the mover (L1, M1), the startup firmware sent to an
ESPHome to bring the plug back to the hub (X1, X2), and an earlier release
of ESPHome updated (U1, U2).
"""

from __future__ import annotations

import time
from typing import Any, TYPE_CHECKING

from tools.autoflash import (
    device,
    esphome,
    esphome_ota,
    handover,
    imprint,
    join,
    names,
    output,
    plans,
    prompts,
    runs,
    unlock,
    wifi,
)
from tools.autoflash.device import DeviceError
from tools.autoflash.errors import Abort, NothingWritten
from tools.autoflash.routes import update
from tools.autoflash.routes.espressif import hub
from tools.autoflash.runs import ROOT
from tools.espressif.safeboot.prepare_imprinted_bundle import BundleError, startup_image

if TYPE_CHECKING:
    from tools.autoflash.context import Context


def step_esphome_install(ctx: Context) -> dict[str, Any]:
    """I1 - ESPHome into ota_0 from the temporary Tasmota, every block read back.

    Like staging Safeboot, it writes only the slot Tasmota does not run from,
    and the partition table is left as Sonoff made it.  Only once the whole
    image has been read back and matched does the plug erase its boot
    selection and restart, which starts ESPHome (the esphome phase).
    """
    host = ctx.state.input("plug_ip")
    if host and esphome.is_esphome(host):
        output.say("The device already runs ESPHome; there is nothing to install again.")
        return {"already_installed": True}
    ssid, password = ctx.wifi()
    # Read off the temporary Tasmota, as E1 reads it on the way to Tasmota:
    # once ESPHome is written, it has to join the Wi-Fi on its own.
    signal = join.note_signal(ctx, join.plug_signal(ctx.device), [
        "Once ESPHome is written, it has to join",
        "your Wi-Fi on its own, and a weak signal makes that less certain."])
    try:
        image = esphome.image_for(ssid, password, own=ctx.esphome_firmware)
    except (esphome.ImageError, imprint.ImprintError, OSError) as exc:
        raise Abort(f"the ESPHome image cannot be used: {exc}") from exc
    name = ctx.esphome_firmware.name if ctx.esphome_firmware else esphome.IMAGE.name
    prompts.section("Installing ESPHome", quiet=True, body=[
        f"{name}, with your Wi-Fi written in (in memory only), goes into the half",
        "of the device's memory Tasmota is not running from. Every block is read back",
        "and checked against the image; only then does the device switch to it and",
        "restart. Its memory layout stays as Sonoff made it.",
    ])
    if ctx.dry_run:
        output.say(f"[dry-run] would install {name}")
        return {"dry_run": True, "image": name}
    return hub.run_plan(ctx, plans.esphome(image)) | {"image": name, "size": len(image),
                                                   "signal_dbm": signal}


def step_leave_tasmota(ctx: Context) -> dict[str, Any]:
    """L1 - from Tasmota's layout to Sonoff's, the mover started there.

    Official Tasmota runs across both of Sonoff's app slots, so it cannot write
    a firmware into either; the mover is small enough to go in front of it.
    The plan writes ESPHome's bootloader, the mover, clears where Sonoff's
    layout keeps its settings and boot selection, and writes Sonoff's
    partition table last - then the plug restarts into the mover.
    """
    host = ctx.state.input("plug_ip")
    if host and esphome_ota.probe(host) is not None and not esphome.is_esphome(host):
        output.say("The device already runs the mover.")
        return {"already": True}
    ssid, password = ctx.wifi()
    # The earliest reading of a run from Tasmota, before it makes way: the
    # mover, and each firmware after it, has to join the Wi-Fi on its own.
    signal = join.note_signal(ctx, join.plug_signal(ctx.device), [
        "After this stage the mover, and each firmware after it, has",
        "to join your Wi-Fi on its own, and a weak signal makes that less certain."])
    handover.consent_to_leave(ctx, "Tasmota")
    try:
        plan = plans.leave_tasmota(esphome.mover_for(ssid, password), esphome.sonoff_table(),
                                   esphome.published_bootloader())
    except (esphome.ImageError, imprint.ImprintError, plans.PlanError, OSError) as exc:
        raise Abort(f"the mover cannot be used: {exc}") from exc
    try:
        result = hub.run_plan(ctx, plan)
    except Abort:
        # Sonoff's table is the plan's last write, there only if every write
        # before it read back right: with it in place, all that is missing is
        # the restart into the mover.  Not left waiting for one.  A plan
        # refused on a plug that already had it - the interim Tasmota, on
        # slot 1 - wrote nothing, and has no mover to restart into.
        if not table_written(ctx) or ctx.device.current_ota() != plan.running:
            raise
        output.say("    Everything was written before the plan stopped; restarting the device into the mover.")
        ctx.device.command("Restart 99")
        result = {"plan": plan.name, "restarted_after": "a stopped report"}
    if not ctx.dry_run:
        wait_for_mover(ctx, "the device to start the mover")
        output.say("    The device is running the mover, on Sonoff's memory layout.")
    return result | {"signal_dbm": signal}


def wait_for_mover(ctx: Context, what: str) -> bool:
    """The mover on the home network - or, when it cannot join that, set up
    through its own (set_up_through_own_network). True when it was given other
    details than the run had, which ESPHome then needs too."""
    def mover(address: str) -> Any:
        return esphome_ota.probe(address)

    if not join.on_a_plug_network():
        try:
            join.wait_for(ctx, mover, what)
            return False
        except Abort:
            pass
    before = ctx._wifi
    joined = join.set_up_through_own_network(ctx, what, mover, names.mover_networks(ctx))
    if joined is None:
        raise Abort(
            f"{what} did not answer on your Wi-Fi, and its own network "
            f"({names.mover_network_hint(ctx)}) is not to be seen either. If your Wi-Fi is "
            "on, switch the device off and on: two minutes after it cannot join your "
            "Wi-Fi, it raises its own network, and running this again sets it up "
            "through that")
    if joined != before:
        ctx.adopt_wifi(*joined)
        return True
    return False


def table_written(ctx: Context) -> bool:
    try:
        return ctx.device.sha_region(plans.TABLE_ADDRESS, plans.SECTOR, timeout=30) == plans.SONOFF_TABLE_SHA256
    except (DeviceError, OSError):
        return False


def step_esphome_from_mover(ctx: Context) -> dict[str, Any]:
    """M1 - ESPHome, sent to the mover as an ESPHome update: it writes it into
    the other slot, checks it, and starts it."""
    host = ctx.state.input("plug_ip")
    if host and esphome.is_esphome(host):
        output.say("The device already runs ESPHome.")
        return {"already": True}
    ssid, password = ctx.wifi()
    try:
        image = esphome.image_for(ssid, password, own=ctx.esphome_firmware)
    except (esphome.ImageError, imprint.ImprintError, OSError) as exc:
        raise Abort(f"the ESPHome image cannot be used: {exc}") from exc
    if ctx.dry_run:
        return {"dry_run": True}
    if wait_for_mover(ctx, "the mover"):
        # Given new Wi-Fi details on its own network: ESPHome gets those too.
        ssid, password = ctx.wifi()
        try:
            image = esphome.image_for(ssid, password, own=ctx.esphome_firmware)
        except (esphome.ImageError, imprint.ImprintError, OSError) as exc:
            raise Abort(f"the ESPHome image cannot be used: {exc}") from exc
    seconds = handover.send_as_esphome_update(ctx, image, "ESPHome")
    output.say(f"    ESPHome sent in {seconds}s; the device restarts into it.")
    return {"seconds": seconds, "size": len(image)}


def step_esphome_update(ctx: Context) -> dict[str, Any]:
    """U1 - this release's ESPHome, the plug's Wi-Fi written in, sent to the
    earlier release as an ordinary ESPHome update: it writes it into its
    other slot and starts it, its settings kept, and goes back to the one it
    had should the new one not start."""
    unlock.unlock_esphome(ctx)
    _, new = update.versions(ctx)
    ssid, password = ctx.wifi(why=[
        "ESPHome cannot say what its Wi-Fi password is, and the update needs it",
        "written in, as every ESPHome image this tool installs does.",
    ])
    try:
        image = esphome.image_for(ssid, password)
    except (esphome.ImageError, imprint.ImprintError, OSError) as exc:
        raise Abort(f"the ESPHome image cannot be used: {exc}") from exc
    if ctx.dry_run:
        return {"dry_run": True}
    seconds = handover.send_as_esphome_update(ctx, image, f"ESPHome {new}")
    output.say(f"    ESPHome {new} sent in {seconds}s; the device restarts into it.")
    return {"seconds": seconds, "size": len(image)}


def step_esphome_updated(ctx: Context) -> dict[str, Any]:
    """U2 - the update started, joined the Wi-Fi and kept; then given the
    plug's factory calibration, as I2 does, which a release before 1.4.0 did
    not take."""
    result = update.wait_for_update(ctx, confirm=ESPHOME_CONFIRMS_AFTER)
    if not result.get("dry_run"):
        result["meter"] = calibrate_esphome_meter(ctx)
    return result


def step_startup_from_esphome(ctx: Context, *, again: bool = False) -> dict[str, Any]:
    """X1 - the startup firmware, with the Wi-Fi written in, sent to ESPHome (or
    the mover) as an ESPHome update.  It joins the Wi-Fi by itself, and saves
    the details where Tasmota reads them.  `again`: sent once more by X2, the
    go-ahead given already."""
    host = ctx.state.input("plug_ip")
    if runs.run_source(ctx.state) in ("esphome", "midway") and not again:
        unlock.unlock_esphome(ctx)
        # The earliest reading of a run from ESPHome, before anything is
        # written: from its Wi-Fi signal sensor (ours from 1.1.0), else from
        # its native API - encrypted, given its key (ours, 1.0.0 too, has
        # none). From an ESPHome that gives neither, the first is at D4.
        ctx.state.capture("X1.signal_dbm", join.note_signal(
            ctx, esphome.signal(host, ctx.esphome_key) if host else None, [
                "Nothing has been written yet. From here the device restarts several",
                "times, and each time it has to rejoin your Wi-Fi on its own; a weak",
                "signal makes that less certain."]))
        handover.consent_to_leave(ctx, "ESPHome" if runs.run_source(ctx.state) == "esphome" else "firmware")
    running = unlock.identify(ctx, host) if host else None
    if running in ("startup", "hub"):
        output.say("The device is already past this step.")
        return {"already": running}
    ssid, password = ctx.wifi(why=[
        "ESPHome cannot say what its Wi-Fi password is, and the firmware that",
        "goes on next needs it written in, to join your Wi-Fi by itself.",
    ])
    try:
        image = startup_image(ssid, password)
    except BundleError as exc:
        raise Abort(f"the startup firmware cannot be prepared: {exc}") from exc
    if ctx.dry_run:
        return {"dry_run": True}
    try:
        join.wait_for(ctx, lambda address: esphome_ota.probe(address), "ESPHome's update service")
    except Abort as exc:
        raise NothingWritten(str(exc)) from exc
    seconds = handover.send_as_esphome_update(ctx, image, "the startup firmware")
    join.wait_for(ctx, lambda address: device.Device(address, timeout=4).is_safeboot(),
             "the startup firmware on your Wi-Fi")
    output.say(f"    The startup firmware is on your Wi-Fi (sent in {seconds}s).")
    return {"seconds": seconds}


def step_berry_from_startup(ctx: Context) -> dict[str, Any]:
    """X2 - the Berry Tasmota, uploaded to the startup firmware, which writes it
    into the other slot; it starts with the Wi-Fi the startup firmware saved."""
    image = ROOT / "captures/safeboot-recovery-migration/tasmota32c3-bluetooth.bin"
    if ctx.dry_run:
        return {"dry_run": True}
    running = unlock.identify(ctx, ctx.device.host)
    if running == "hub":
        output.say("The device already runs Tasmota.")
        return {"already": True}
    if running in ("mover", "esphome", "esphome-login"):
        # The startup firmware confirms itself only once it has been used: a
        # plug switched off and on before then starts the firmware it ran
        # before (seen in the fault suite, a power cut early in X2).
        output.tell("    The device is back on the firmware it ran before - switched off and on")
        output.tell("    before the startup firmware was settled in - so that is sent again.")
        step_startup_from_esphome(ctx, again=True)
    how = hub.upload_or_prompt(ctx, image, "Berry Tasmota")

    def berry(address: str) -> bool:
        # Tasmota only once it says so: the page not answering, as while the
        # plug restarts, is no sign of it.
        try:
            version = device.Device(address, timeout=4).firmware_version()
        except (DeviceError, OSError):
            return False
        return bool(version) and "safeboot" not in version.lower()

    join.wait_for(ctx, berry, "Tasmota to start and join your Wi-Fi")
    output.say("    Tasmota is running and on your Wi-Fi.")
    return {"upload": how}


# ESPHome installed as an update (to the mover) confirms itself only after a
# minute running (safe_mode's boot_is_good_after); restarted before then, the
# bootloader takes the plug back to what it ran before.
ESPHOME_CONFIRMS_AFTER = 70.0


def step_esphome_check(ctx: Context, wait: float = 180.0) -> dict[str, Any]:
    """I2 - ESPHome started from what was written, and joined the home Wi-Fi;
    then given the plug's factory calibration, where its backup has it."""
    result = wait_for_esphome(ctx, wait)
    if not result.get("dry_run"):
        result["meter"] = calibrate_esphome_meter(ctx)
    return result


def calibrate_esphome_meter(ctx: Context) -> dict[str, Any]:
    """The plug's factory calibration, from its Sonoff backup, given to
    ESPHome, which uses it when Sonoff's NVS has none: on a plug converted
    through Tasmota.  ESPHome reads the board's factor itself."""
    coefficients = wifi.backup_calibration(ctx)
    if coefficients is None:
        return {"skipped": "no factory calibration in a backup of the device"}
    host = ctx.state.input("plug_ip")
    try:
        given = esphome.api_give_calibration(host, coefficients, key=ctx.esphome_key)
    except esphome.ApiLocked:
        given = False
    if not given:
        output.tell("    ESPHome did not take the device's factory calibration from its backup")
        output.tell("    (one older than 1.4.0, or whose API key is not known).")
        return {"skipped": "ESPHome did not take it"}
    # Its sensor says what it made of it once the action has run.
    state = None
    for _ in range(10):
        state = esphome.calibration_state(host)
        if state and not state.startswith("missing"):
            break
        time.sleep(0.5)
    if state and state.startswith("from backup"):
        output.say("Gave ESPHome the device's factory calibration, from its Sonoff backup")
    return {"calibration": state}


def wait_for_esphome(ctx: Context, wait: float) -> dict[str, Any]:
    """ESPHome started from what was written, and joined the home Wi-Fi
    (step_esphome_check).

    ESPHome sent to the mover as an update (from Tasmota) is not confirmed
    until it has run a minute: a power cut before then takes the plug back to
    the mover, which is then sent ESPHome again.  So it is waited out, and the
    plug is only done once ESPHome is there for good.
    """
    host = ctx.state.input("plug_ip")
    if not host:
        raise Abort("the device's address is not known")
    if ctx.dry_run:
        output.say("[dry-run] would wait for ESPHome to answer")
        return {"dry_run": True}
    as_update = runs.run_source(ctx.state) in ("tasmota", "safeboot")  # through the mover (M1)
    output.say("Waiting for the device to start ESPHome and join your Wi-Fi...")
    started = time.monotonic()
    first_seen = None
    resent = 0
    while time.monotonic() - started < wait + ESPHOME_CONFIRMS_AFTER * as_update:
        host = ctx.state.input("plug_ip")
        if esphome.is_esphome(host):
            if first_seen is None:
                first_seen = time.monotonic()
                output.say(f"    ESPHome answered at {host} after {first_seen - started:.0f}s.")
                if as_update:
                    output.say("    Waiting a minute while it confirms itself, so a power cut now")
                    output.say("    cannot take it back...")
            if not as_update or time.monotonic() - first_seen >= ESPHOME_CONFIRMS_AFTER:
                return {"seconds": round(first_seen - started, 1), "resent": resent}
        elif as_update and esphome_ota.probe(host) is not None:
            # Back on the mover: ESPHome restarted before it had confirmed itself.
            first_seen = None
            resent += 1
            if resent > 2:
                raise Abort("the device keeps going back to the mover instead of starting ESPHome")
            output.say("    The device went back to the mover: ESPHome restarted before it had")
            output.say("    confirmed itself. Sending ESPHome again...")
            step_esphome_from_mover(ctx)
            started = time.monotonic()
            continue
        else:
            moved = ctx.finder.poll()
            if moved and moved != host:
                continue
        if join.on_a_plug_network():
            break  # this computer joined its own network already: on through that
        output.progress(f"waiting for ESPHome... {time.monotonic() - started:.0f}s")
        time.sleep(3)
    output.progress_clear()
    if join.set_up_through_own_network(ctx, "ESPHome", esphome.is_esphome, names.esphome_networks(ctx)):
        return {"seconds": round(time.monotonic() - started, 1), "resent": resent,
                "set_up_through": "its own network"}
    raise Abort(
        "ESPHome was written and checked, but has not answered on your Wi-Fi within "
        "three minutes - most likely it cannot join it, and its own network "
        f"({names.esphome_network_hint(ctx)}) is not to be seen either. Switch the device off and "
        "on, and run the same command again"
    )
