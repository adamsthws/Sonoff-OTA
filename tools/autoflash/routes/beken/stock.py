"""From Sonoff's firmware: what the plug reports of itself (B1), and the new
firmware sent to it through eWeLink (B2) - the one write of the route, after
which the plug's stock bootloader applies the image and the new firmware
starts, reading the plug's Wi-Fi from the stock store it leaves untouched.
"""

from __future__ import annotations

import re
import time
from typing import Any, TYPE_CHECKING

from tools.autoflash import detect, firewall, kinds, output, plugs, prompts, runs
from tools.autoflash.errors import Abort, NothingWritten
from tools.autoflash.routes.beken import images
from tools.beken import fota_wrap
from tools.ewelink import ota_push
from tools.ewelink.get_device_key import EwelinkError
from tools.ewelink.query_ota import ota_identity

if TYPE_CHECKING:
    from tools.autoflash.context import Context

# The stock updater fetches the file in ranges, at its own pace: a few minutes.
DELIVERY_WAIT = 300.0
# How the plug turns an update down while one cut off part way still holds its
# updater (error 410, relayed by eWeLink): until it is switched off and on.
BUSY = re.compile(r"\berror 410\b")


def account_record(ctx: Context) -> dict[str, Any]:
    """The plug's record in the owner's eWeLink account, found by the device
    id A1 kept for it."""
    device_id = ""
    found = getattr(ctx, "account_plug", None)
    if found is not None:
        device_id = found.device_id
    if not device_id:
        mac = ctx.state.input("plug_mac")
        device_id = str(plugs.read_plug(mac).get("ewelink_id") or "") if mac else ""
    if not device_id:
        raise Abort("the device's eWeLink device id is not known: start again with --new, so the device is found "
                    "in the account first")
    devices = ctx.account_devices()
    if devices is None:
        raise Abort("eWeLink could not be reached, and the device's firmware can only be sent through it")
    for device in devices:
        if device.get("deviceid") == device_id:
            return device
    raise Abort(f"the device is no longer in the eWeLink account {ctx.email()}; add it again in the app")


def step_identify(ctx: Context) -> dict[str, Any]:
    """B1 - what the plug reports of itself, through eWeLink: its model and
    firmware, which must be the kind's."""
    device = account_record(ctx)
    try:
        identity = ota_identity(device)
    except EwelinkError as exc:
        raise Abort(str(exc)) from exc
    kind = kinds.for_stock_model(identity["model"])
    if kind is None:
        raise Abort(f"the device is a {identity['model']}, which this tool does not convert yet. Nothing was written")
    product = str(device.get("productModel") or "").strip().upper()
    ctx.state.set_input("plug_kind", kind.key)
    ctx.state.set_input("stock_version", identity["version"])
    ctx.state.set_input("product_model", product)
    prompts.section("What the device reported", quiet=True, body=[
        f"Model            {identity['model']}",
        f"Product          {product or 'not given'}",
        f"Firmware now     {identity['version']}",
        "",
        "The device fetches its new firmware straight from this computer, as its",
        "own updates come, and its bootloader installs it on the next start.",
    ])
    return {"stock_version": identity["version"], "model": identity["model"], "product": product}


def step_push(ctx: Context) -> dict[str, Any]:
    """B2 - the new firmware, sent to the plug through eWeLink.  The first and
    only write: once the plug has it, its bootloader replaces Sonoff's
    firmware on the next start, and there is no going back but the way this
    tool offers (Sonoff's firmware fetched again, from the public dump)."""
    kind = kinds.of_run(ctx.state)
    firmware = runs.run_firmware(ctx.state)
    plug_ip, stock_version = ctx.state.require("plug_ip", "stock_version")
    device = account_record(ctx)
    image, version = images.image(ctx, firmware)
    container = fota_wrap.wrap(image, name=kind.wrapper_model, version=images.FOTA_VERSION)
    listen_ip = ctx.listen_ip()
    port = firewall.choose_port(ctx, "delivery_port", firewall.DELIVERY_PORTS, "the firmware")
    firewall.ensure_firewall(ctx)
    output.transient_begin()
    prompts.warning_box("Before going on", [
        "From here the device's Sonoff firmware is replaced, so the eWeLink app",
        "will no longer control it. DO NOT UNPLUG IT during the process.",
        "The device's own settings - its Wi-Fi, its meter calibration, its eWeLink",
        "identity - stay on the device untouched, and the new firmware reads them.",
        "Only go ahead with a device you can afford to lose.",
    ])
    if not prompts.require_word(prompts.CONSENT_WORD):
        raise NothingWritten("not confirmed - nothing has been written to the device")
    output.transient_end()  # agreed: the note and the question have done their job
    if ctx.dry_run:
        output.say(f"[dry-run] would send {firmware} {version} ({len(container):,} bytes wrapped) to {plug_ip}")
        return {"dry_run": True, "firmware": firmware, "version": version}

    def check(found: dict[str, Any], model: str) -> None:
        # Only the plug A1 found, as it was at B1: another device on the
        # account, or a plug updated since, is never sent this.
        if model != kind.stock_model:
            raise EwelinkError(f"the device's model is {model}, not the {kind.name}'s {kind.stock_model}")
        reported = str((found.get("params") or {}).get("fwVersion"))
        if reported != stock_version:
            raise EwelinkError(f"the device reports firmware {reported}, not the {stock_version} it reported at B1")

    output.say(f"Sending {firmware} {version} ({len(image):,} bytes; {len(container):,} wrapped) through eWeLink...")
    started = time.monotonic()
    delivered = ota_push.deliver(
        email=ctx.email(), password=ctx.ewelink_env()["EWELINK_PASSWORD"], country_code="+44", region="eu",
        device_id=device["deviceid"], firmware=container, version=images.FOTA_VERSION,
        listen_ip=listen_ip, listen_port=port, device_ip=plug_ip, names=("user1.bin",),
        wait=DELIVERY_WAIT, check_device=check, say=output.say, live=False)
    if not delivered.sent:
        raise Abort(f"the firmware was not sent: {delivered.error or 'eWeLink did not take the command'}. "
                    f"{ota_push.NOT_SENT}")
    if BUSY.search(delivered.error or ""):
        raise Abort("the device turned the new firmware down: it is still busy with an update that was cut off "
                    "part way. Switch the device off and on - it keeps its Sonoff firmware - then run the same "
                    "command again")
    if not delivered.complete:
        # Sent, and some of it may be on the plug: the plug says what it runs
        # when the step is asked about (evidence_pushed).
        raise OSError(f"the device fetched {delivered.bytes_served:,} of {len(container):,} bytes before the "
                      f"delivery stopped ({delivered.error or 'it stopped asking'})")
    seconds = round(time.monotonic() - started, 1)
    output.say(f"    The device has the whole image after {seconds}s; its bootloader installs it as it restarts.")
    return {"seconds": seconds, "firmware": firmware, "version": version, "bytes": len(container)}


def evidence_pushed(ctx: Context) -> tuple[bool | None, str]:
    """Whether a push cut off got through: the plug answers as the firmware it
    was sent (it restarted into it), or as nothing this tool knows yet."""
    host = ctx.state.input("plug_ip")
    firmware = runs.run_firmware(ctx.state)
    running = detect.identify(host, timeout=3.0) if host else None
    if running == firmware:
        return True, f"The device answers as {detect.NAMES[firmware]}: the firmware was delivered and installed."
    if running == "sonoff":
        return False, "The device still answers as Sonoff's firmware: the delivery did not finish."
    return None, "The device does not answer as the new firmware yet - it may still be installing it."
