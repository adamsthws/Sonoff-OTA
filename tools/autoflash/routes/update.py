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
over where its API is encrypted, which ours never is, or where it is a build
this project did not publish (esphome.published_builds): one adopted into an
ESPHome dashboard, with a key or without, is that dashboard's to update - the
published image would take away its changes and any key.  Its owner can ask
for that (--replace-own-build, replace_own_build).
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, TYPE_CHECKING

from tools.autoflash import detect, esphome, join, kinds, openbeken, output, prompts, runs, unlock
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


# What a plug whose own build is being replaced runs, in place of a release:
# the same version as the published one, often, so the version cannot tell them.
OWN_BUILD = "your own build"


def esphome_release(ctx: Context) -> str | None:
    """The release of this tool's ESPHome for the run's kind the plug runs
    (esphome.api_device_info), or None: another project's, an encrypted one, or
    a build this project did not publish."""
    host = ctx.state.input("plug_ip")
    try:
        info = esphome.api_device_info(host, key=ctx.esphome_key) if host else None
    except esphome.ApiLocked:
        return None
    kind = kinds.of_run(ctx.state)
    if not info or info["project_name"] != kind.esphome_project:
        return None
    if info["built"] == ctx.state.input("replaced_build"):
        return OWN_BUILD
    if info["encrypted"] or info["built"] not in esphome.published_builds(kind):
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


def earlier_release(ctx: Context, firmware: str, published: str | None) -> str | None:
    """The release the plug runs, where that is this tool's `firmware` for its
    kind, older than the `published` one; else None."""
    if firmware not in RELEASE or not published or (firmware == "esphome" and ctx.esphome_firmware):
        return None
    release = RELEASE[firmware](ctx)
    return release if release is not None and older(release, published) else None


def outdated(ctx: Context, firmware: str, published: str | None) -> bool:
    """Whether the plug runs an earlier release of `firmware` than the
    `published` one: recorded with both versions (update_from, update_to)."""
    release = earlier_release(ctx, firmware, published)
    if release is None:
        return False
    ctx.state.set_input("update_from", release)
    ctx.state.set_input("update_to", published)
    return True


def is_own_build(ctx: Context) -> bool:
    """Whether the plug's ESPHome is this project's for its kind but not a build
    it published, or has an API that will not say without a key: adopted into a
    dashboard, most likely."""
    host = ctx.state.input("plug_ip")
    kind = kinds.of_run(ctx.state)
    try:
        info = esphome.api_device_info(host, key=ctx.esphome_key) if host else None
    except esphome.ApiLocked:
        return True
    return bool(info and info["project_name"] == kind.esphome_project
                and (info["encrypted"] or info["built"] not in esphome.published_builds(kind)))


def replace_own_build(ctx: Context, firmware: str, published: str | None) -> bool:
    """--replace-own-build: whether to send the published ESPHome over the plug's
    own build, its owner having agreed to what that loses; recorded as an update
    is, with the build it replaces.  False from a build this project published,
    which needs nothing of the kind.  Refused for all but this project's ESPHome
    for the plug's kind: another's may have other pins or another flash layout."""
    host = ctx.state.input("plug_ip")
    kind = kinds.of_run(ctx.state)
    if firmware != "esphome" or not published or not host:
        raise Abort(f"--replace-own-build is for this project's ESPHome, and the device does not answer as that"
                    f"{'' if firmware == 'sonoff' else f': it runs {detect.NAMES[firmware]}'}")
    try:
        info = esphome.api_device_info(host, key=ctx.esphome_key)
    except esphome.ApiLocked as locked:
        if not unlock.ask_esphome_key(ctx, host, wifi=True):
            raise Abort(f"--replace-own-build needs the device's API encryption key, "
                        f"{'and the one given is not it' if locked.wrong else 'and none was given'}: "
                        f"{unlock.ESPHOME_KEY_HINT}") from None
        info = esphome.api_device_info(host, key=ctx.esphome_key)
    if not info or info["project_name"] != kind.esphome_project:
        raise Abort(f"--replace-own-build only replaces this project's ESPHome for a {kind.name}, and the "
                    f"device says it is {(info or {}).get('project_name') or 'no project'}")
    if not info["encrypted"] and info["built"] in esphome.published_builds(kind):
        return False
    prompts.warning_box("Replacing your own build", [
        f"This installs this project's ESPHome {published} over the build on the device.",
        "You lose any changes in its config, and any API encryption key. Home Assistant",
        "stops reaching it until you remove the device there and add it again, and the",
        "ESPHome builder's config for it no longer matches what it runs. If you renamed",
        "its relay, the relay goes back to how it was before that, which may switch",
        "whatever is plugged in.",
    ])
    if not prompts.require_word(prompts.CONSENT_WORD):
        raise Abort("not confirmed")
    ctx.state.set_input("replaced_build", info["built"])
    ctx.state.set_input("update_from", OWN_BUILD)
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
