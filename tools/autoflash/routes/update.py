"""This tool's firmware updated on a plug it converted: from an earlier release
of the image published for the plug's kind to this one, sent as the firmware
takes any update of its own, the plug's settings kept.  The way a fix
reaches a plug that nothing else builds for: an ESPHome no dashboard has
adopted, or an OpenBeken carrying this tool's driver and flash layout, which
upstream's own builds do not.  Official Tasmota updates itself, from its own
page; it is offered the release this tool installs too, so that running the
tool again updates any plug it converted.

Each family sends the image its own way and checks it arrived; what is
shared is here: which plug is offered the update, and what it says once it
has it.  One is offered it only when its firmware says it is this tool's
for its kind at an earlier version (RELEASE).  An ESPHome is also passed
over where its API is encrypted: ours never is, and one adopted into an
ESPHome dashboard, which gives it a key, is that dashboard's to update - the
published image would take away its changes and its key.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, TYPE_CHECKING

from tools.autoflash import detect, esphome, join, kinds, openbeken, output, runs
from tools.autoflash.errors import Abort

if TYPE_CHECKING:
    from tools.autoflash.context import Context


def older(version: str, than: str) -> bool:
    """Whether `version` (1.4.0) is an earlier release than `than`; False
    where either is not one of ours, all numbers."""
    try:
        return tuple(map(int, version.split("."))) < tuple(map(int, than.split(".")))
    except ValueError:
        return False


def esphome_release(ctx: Context) -> str | None:
    """The release of this tool's ESPHome for the run's kind the plug runs
    (esphome.api_device_info), or None: another project's, or an encrypted one."""
    host = ctx.state.input("plug_ip")
    try:
        info = esphome.api_device_info(host, key=ctx.esphome_key) if host else None
    except esphome.ApiLocked:
        return None
    if not info or info["encrypted"] or info["project_name"] != kinds.of_run(ctx.state).esphome_project:
        return None
    return str(info["project_version"])


def openbeken_release(ctx: Context) -> str | None:
    """The release of this tool's OpenBeken the plug runs: the version its
    build string gives (each is built at its release's), where its driver
    says it is on the run's kind of plug - or None."""
    host = ctx.state.input("plug_ip")
    found = openbeken.info(host) if host else None
    if not found or openbeken.kind(host, found) is not kinds.of_run(ctx.state):
        return None
    return openbeken.version(found)


def tasmota_release(ctx: Context) -> str | None:
    """The release of official Tasmota the plug runs, as its version says
    (16.0.0(release-tasmota32)) - or None: anyone's own build, its
    safeboot, or nothing answering."""
    found = re.fullmatch(r"(\d+(?:\.\d+)+)\s*\(release-tasmota32\)", ctx.device.firmware_version())
    return found.group(1) if found else None


# How each firmware this tool installs says which release of it the plug runs.
RELEASE: dict[str, Callable[["Context"], str | None]] = {"esphome": esphome_release,
                                                          "openbeken": openbeken_release,
                                                          "tasmota": tasmota_release}


def outdated(ctx: Context, firmware: str, published: str | None) -> bool:
    """Whether the plug runs this tool's `firmware` for its kind, older than
    the `published` one: recorded with both versions (update_from, update_to)."""
    if firmware not in RELEASE or not published or (firmware == "esphome" and ctx.esphome_firmware):
        return False
    release = RELEASE[firmware](ctx)
    if release is None or not older(release, published):
        return False
    ctx.state.set_input("update_from", release)
    ctx.state.set_input("update_to", published)
    return True


def versions(ctx: Context) -> tuple[str, str]:
    """The release the plug runs and the one it is updated to."""
    return ctx.state.input("update_from") or "", ctx.state.input("update_to") or ""


def release(ctx: Context) -> str | None:
    """The release of the run's firmware the plug says it runs now."""
    return RELEASE[runs.run_firmware(ctx.state)](ctx)


def evidence_updated(ctx: Context) -> tuple[bool | None, str]:
    """Whether the update got onto the plug, by the version it answers with."""
    old, new = versions(ctx)
    name = detect.NAMES[runs.run_firmware(ctx.state)]
    version = release(ctx)
    if version == new:
        return True, f"The device runs {name} {new}, so the update did finish."
    if version == old:
        return False, f"The device still runs {name} {old}: it is safe to send the update again."
    return None, f"The device does not answer as {name}."


def still_updated(ctx: Context) -> tuple[bool, str]:
    """An update recorded done is doubted where the plug still runs the release
    it had (STEP_VERIFIERS): its new one never started, and is sent again."""
    updated, message = evidence_updated(ctx)
    return updated is not False, message[0].lower() + message[1:].rstrip(".")


def wait_for_update(ctx: Context, wait: float = 180.0, confirm: float = 0.0) -> dict[str, Any]:
    """The plug started the new release and joined the Wi-Fi; then, for
    `confirm` seconds, still runs it: an ESPHome that takes an update back if
    it restarts before then (the S60's) has kept it."""
    if ctx.dry_run:
        output.say("[dry-run] would wait for the updated firmware to answer")
        return {"dry_run": True}
    old, new = versions(ctx)
    name = detect.NAMES[runs.run_firmware(ctx.state)]
    output.say(f"Waiting for the device to start {name} {new} and join your Wi-Fi...")
    started = time.monotonic()
    join.wait_for(ctx, lambda _host: release(ctx) == new, f"{name} {new}", timeout=wait)
    seconds = round(time.monotonic() - started, 1)
    if confirm:
        output.say("    Waiting a minute while it confirms itself, so a power cut now")
        output.say("    cannot take it back...")
        time.sleep(confirm)
        if release(ctx) == old:
            raise Abort(f"the device went back to {name} {old}: the update did not hold. Running the same "
                        "command again sends it again")
    output.say(f"    {name} {new} answered after {seconds:.0f}s.")
    return {"seconds": seconds, "from": old, "to": new}
