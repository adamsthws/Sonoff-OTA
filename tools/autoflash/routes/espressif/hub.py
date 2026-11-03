"""The hub every route but Tasmota to ESPHome passes through: the Berry
Tasmota in the high slot on Sonoff's layout, ESPHome's bootloader in place
(D4, D5), and the plans the plug carries out from there.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

from tools.autoflash import device, errors, esphome, join, names, output, phases, plans, prompts
from tools.autoflash.device import DeviceError, UploadRefused
from tools.autoflash.errors import Abort
from tools.autoflash.runs import ROOT
from tools.autoflash.state import FAILED, PENDING

if TYPE_CHECKING:
    from tools.autoflash.context import Context


def upload_or_prompt(ctx: "Context", path: Path, what: str) -> str:
    """Upload over HTTP, falling back to the operator's browser.

    The multipart path has not been exercised on this hardware.  A failure here
    must not strand a run: the same file uploaded through the web UI is
    identical, so offer that and carry on.
    """
    device = ctx.device
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        booted = boot_of(device)
        try:
            started = time.monotonic()
            device.upload_firmware(path)
            seconds = round(time.monotonic() - started, 1)
            output.say(f"{what} uploaded in {seconds}s")
            return f"http:{seconds}s"
        except UploadRefused as exc:
            # Refused whole, nothing written: most likely damaged on the way.
            if attempt == UPLOAD_ATTEMPTS:
                return upload_or_prompt_by_hand(ctx, path, what, exc)
            output.tell(f"    The device refused the upload ({exc.reason}); sending it again "
                 f"(attempt {attempt + 1} of {UPLOAD_ATTEMPTS}).")
            time.sleep(5)
        except errors.NETWORK_ERRORS as exc:
            # Cut off on the way - the Wi-Fi going, say.  Sent again only once
            # the plug shows it did not take it: an upload taken is booted, and
            # another would write the other slot and boot that instead.
            took = upload_took(device, booted)
            if took:
                output.say(f"{what} was taken, though its answer was lost ({exc}): the device restarted.")
                return "http:answer lost"
            if took is None or attempt == UPLOAD_ATTEMPTS:
                return upload_or_prompt_by_hand(ctx, path, what, exc)
            output.tell(f"    The upload was cut off ({exc}); sending it again "
                        f"(attempt {attempt + 1} of {UPLOAD_ATTEMPTS}).")
        except Exception as exc:  # noqa: BLE001 - any failure falls back to the operator
            return upload_or_prompt_by_hand(ctx, path, what, exc)
    raise AssertionError("unreachable")


def boot_of(device: device.Device) -> str | None:
    """Which boot the plug is on (device.started_at), or None if it cannot say."""
    try:
        return device.started_at()
    except (DeviceError, OSError):
        return None


def upload_took(device: device.Device, booted: str | None) -> bool | None:
    """After an upload cut off: whether the plug took it - restarted, on
    another boot than `booted` - once it answers again; None where that cannot
    be told (it does not answer, or could not say which boot it was on)."""
    if booted is None:
        return None
    try:
        device.wait_reachable(timeout=120)
        now = boot_of(device)
    except DeviceError:
        return None
    return None if now is None else now != booted


def upload_or_prompt_by_hand(ctx: "Context", path: Path, what: str, exc: Exception) -> str:
    """The upload left to the operator's browser, after it failed with `exc`."""
    device = ctx.device
    output.transient_begin()
    output.tell(f"    Automatic upload failed: {exc}")
    output.say("")
    output.tell("    Upload it by hand instead - the file is identical:")
    output.tell(f"        open http://{device.host}/  ->  Firmware Upgrade  ->  choose file")
    output.tell(f"        {path}")
    if not prompts.confirm(f"{what} uploaded through the web UI?"):
        raise Abort(f"{what} was not uploaded")
    output.transient_end()
    return "manual"


def running_slot(device: device.Device, timeout: float = 60.0) -> int:
    """Which slot Tasmota runs from, once it can say: just after a restart its
    Berry is not up, and the slot reads as unknown (seen in the fault suite,
    taken for "not slot 1", and another upload sent into a plug starting)."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            device.wait_reachable(timeout=30)
            slot = device.current_ota()
        except (DeviceError, OSError):
            slot = None
        if slot is not None:
            return slot
        if time.monotonic() > deadline:
            raise Abort(f"the device at {device.host} did not say which slot it runs from "
                        f"within {timeout:.0f}s")
        time.sleep(3)


def step_promote(ctx: Context) -> dict[str, Any]:
    """D4/D5 - move Tasmota to the high slot and freeze NVS."""
    device = ctx.device
    image = ROOT / "captures/safeboot-recovery-migration/tasmota32c3-bluetooth.bin"
    device.wait_reachable(timeout=120)
    # The first reading of a run from an ESPHome that gives none (no signal
    # sensor, an encrypted API; the startup firmware gives none either), and
    # another of every other run through here.
    ctx.state.capture("D4.signal_dbm", join.note_signal(ctx, join.plug_signal(device), [
        "From here the device restarts several times, and each time it has to",
        "rejoin your Wi-Fi on its own; a weak signal makes that less certain."]))
    slot = running_slot(device)
    output.say(f"The device is running from slot {slot}. The next steps need slot 1.")
    if slot == 1:
        device.command("SaveData 0")
        output.say("Already there. SaveData 0 - settings frozen so the migration's checks stay stable")
        return {"current_ota": slot, "uploads": 0}
    if ctx.dry_run:
        output.say("[dry-run] would upload the same image through Firmware Upgrade")
        return {"current_ota": slot, "uploads": 0}

    # Tried again after an upload that was cut off (the Wi-Fi going part way),
    # Tasmota still holds that upload, and the next one hangs without a word
    # until it gives up minutes later: a restart clears it.  It starts the same
    # slot - an upload switches only once it is whole.
    if ctx.state.status("D4") == FAILED:
        output.say("Restarting the device first, to clear the upload that was cut off...")
        booted, uptime = device.started_at(), device.uptime_seconds()
        device.command("Restart 1")
        device.wait_for_reboot(timeout=180, started_at=booted, before=uptime)
        device.wait_reachable(timeout=120)

    # Every upload writes the other slot and boots it, so this alternates.
    # One swap is expected; a second is allowed in case the run started from an
    # odd position.  Anything beyond that is a real fault, not bad luck.
    uploads = 0
    for attempt in range(2):
        booted, uptime = device.started_at(), device.uptime_seconds()
        upload_or_prompt(ctx, image, "Berry Tasmota")
        uploads += 1
        output.say("Waiting for the device to restart into the other slot...")
        device.wait_for_reboot(timeout=300, started_at=booted, before=uptime)
        slot = running_slot(device)
        output.say(f"Now running from slot {slot}.")
        if slot == 1:
            break
        if attempt == 0:
            output.say("Each upload writes the other slot and boots it, so that swap put")
            output.say("it back where it started. Doing it once more to land on slot 1.")
    after = slot
    if after != 1:
        raise Abort(
            f"the device is still running from slot {after} after {uploads} uploads; "
            "it should alternate on every upload, so something is stopping the new "
            "image being selected"
        )
    device.command("SaveData 0")
    output.say("SaveData 0 - settings frozen so the migration's checks stay stable")
    return {"current_ota": after, "uploads": uploads}


def plug_back_on_wifi(ctx: Context) -> None:
    """Before E1 or E3 asks the plug anything: find it, or bring it back.

    A power cut while the commit writes can cost Tasmota its settings: it comes
    back on its old layout, asking to be set up on its own network.  Found on
    the network is not yet answering: a plug just switched back on answers
    ping a few seconds before its web server, so that is waited for too.
    """
    if not ctx.finder.poll():
        setup = prompts.find_ssid(names.setup_networks(ctx), timeout=15)
        if setup:
            output.tell(f"    The device is not on your Wi-Fi: it is asking to be set up on {setup},")
            output.tell("    as Tasmota does when it has lost its settings - after a power cut, say.")
            join.configure_plug_wifi(ctx, setup)
        else:
            join.locate_plug(ctx, setup=True)
    ctx.device.wait_reachable(timeout=120)


def sonoff_layout_back(ctx: Context) -> None:
    """A commit stopped part way - by a power cut, say - leaves the plug on the
    interim layout, its settings moved aside (safeboot_migration says why).
    Every check and plan expects Sonoff's: put back first, then a restart."""
    if ctx.dry_run:
        return
    device = ctx.device
    try:
        table = device.sha_region(plans.TABLE_ADDRESS, plans.SECTOR, timeout=30)
    except (DeviceError, OSError):
        return  # no Berry to ask: the step's own checks say what is wrong
    if table != plans.INTERIM_TABLE_SHA256:
        return
    output.tell("    The last commit stopped part way, with the device's settings moved aside;")
    output.tell("    putting them back first.")
    booted, uptime = device.started_at(), device.uptime_seconds()
    run_plan(ctx, plans.sonoff_layout_back(esphome.sonoff_table()))
    device.wait_for_reboot(timeout=300, started_at=booted, before=uptime)
    output.say("    The device is back on Sonoff's layout, with its settings.")


# Tries at the upload, as the commit and the installer's sends are tried.
UPLOAD_ATTEMPTS = 3


def run_plan(ctx: Context, plan: plans.Plan) -> dict[str, Any]:
    """Have the plug carry out `plan` (tools/autoflash/plans.py)."""
    if ctx.dry_run:
        output.say(f"[dry-run] would write {plan.title}: {plan.summary()}")
        return {"dry_run": True, "plan": plan.name}
    code, captured = ctx.phase_runner().run("plan", plan=plan)
    if code != 0:
        raise phases.phase_failed(f"plan {plan.name}", code, captured)
    return {"plan": plan.name, "steps": len(plan.steps)}


def step_bootloader(ctx: Context) -> dict[str, Any]:
    """D5 - ESPHome's bootloader, written once for every firmware: it starts
    Tasmota, ESPHome and Sonoff's firmware alike, and rolls back an update
    that fails to start.  Skipped when it is already in place."""
    if ctx.state.status("E3") != PENDING:
        # A run begun before this step was (1.2.0's), carried on by this
        # version once its commit had begun: the plug may no longer have the
        # layout and the Tasmota this needs, and never will on this route.
        # Its bootloader stays Sonoff's, as runs of that version left every
        # plug; the next change of firmware puts ESPHome's on (L1).
        output.say("Left as it is: this run was begun by an earlier version of this tool, which")
        output.say("kept Sonoff's bootloader, and is past the commit that step needs to come before.")
        ctx.state.set_input("bootloader_kept", "sonoff")
        # And the commit put that version's startup firmware on, which this
        # checkout cannot rebuild to prove at G1 (step_restore).
        ctx.state.set_input("startup_unproven", True)
        return {"skipped": "begun before this step, and past the commit"}
    sonoff_layout_back(ctx)
    return run_plan(ctx, plans.bootloader(esphome.published_bootloader()))
