"""From one converted firmware to another, or back to Sonoff's: each firmware
takes the next as an update of its own kind.  OpenBeken fetches an rt_ota
image by URL into the download slot (O1, O2); an ESPHome on LibreTiny takes
a UF2 carrying one through ESPHome's OTA (E1, E2).  The stock bootloader
applies whichever is there on the restart that follows, and the new firmware
reads the plug's Wi-Fi from the stock store.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, TYPE_CHECKING

from tools.autoflash import detect, firewall, handover, kinds, openbeken, output, runs
from tools.autoflash.errors import Abort
from tools.autoflash.routes.beken import images
from tools.beken import uf2
from tools.ewelink import ota_push

if TYPE_CHECKING:
    from tools.autoflash.context import Context

# ESPHome's OTA listens here on LibreTiny (3232 on an Espressif plug).
ESPHOME_OTA_PORT = 8892
# OpenBeken fetches an image in a minute or two; it then restarts by itself.
FETCH_WAIT = 240.0
# A plug that has asked for nothing this long after being told to fetch is
# stuck - OpenBeken whose last download was cut off (this computer stopped
# mid-download) refuses another until it restarts: restarted, and asked again.
FIRST_BYTE_WAIT = 30.0
IMAGE_NAME = "firmware.rbl"


def serve_to_plug(ctx: Context, image: bytes, what: str, fetch: Callable[[str], None],
                  unstick: Callable[[], bool] | None = None) -> float:
    """Serve `image` to the plug's address alone and have the plug fetch it
    (`fetch(url)`), until it has every byte.  How long that took.  If it asks
    for nothing in FIRST_BYTE_WAIT, `unstick()` - a restart - and ask once more."""
    plug_ip = ctx.state.input("plug_ip")
    listen_ip = ctx.listen_ip()
    port = firewall.choose_port(ctx, "delivery_port", firewall.DELIVERY_PORTS, what)
    firewall.ensure_firewall(ctx)
    # Each range it fetches is logged, not counted on screen: the stage line is the run's.
    server = ota_push.FirmwareServer((listen_ip, port), image, plug_ip, say=output.say, names=(IMAGE_NAME,),
                                     live=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        url = f"http://{listen_ip}:{port}/{IMAGE_NAME}"
        output.say(f"Serving {what} to {plug_ip} at {url}")
        fetch(url)
        asked = time.monotonic()
        while time.monotonic() - started < FETCH_WAIT:
            served = server.unique_bytes_served()
            if served == len(image):
                break
            if not served and unstick and time.monotonic() - asked > FIRST_BYTE_WAIT:
                output.progress_clear()
                output.say(f"    The device has not asked for {what} yet - restarting it, then asking again.")
                if not unstick():
                    raise OSError(f"the device asked for nothing of {what}, and did not answer again after "
                                  "being restarted")
                unstick = None
                fetch(url)
                started = asked = time.monotonic()
            output.progress(f"the device is fetching {what}: {100 * served // len(image)}%")
            time.sleep(2)
        else:
            raise OSError(f"the device fetched {server.unique_bytes_served():,} of {len(image):,} bytes of {what} "
                          f"in {FETCH_WAIT:.0f}s")
    finally:
        output.progress_clear()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    seconds = round(time.monotonic() - started, 1)
    output.say(f"    The device has all of {what} after {seconds}s; it restarts to install it.")
    return seconds


def step_openbeken_to_esphome(ctx: Context) -> dict[str, Any]:
    """O1 - ESPHome, fetched by OpenBeken into the download slot."""
    handover.consent_to_leave(ctx, "OpenBeken")
    image, version = images.image(ctx, "esphome")
    if ctx.dry_run:
        return {"dry_run": True, "version": version}
    host = ctx.state.input("plug_ip")
    seconds = serve_to_plug(ctx, image, f"ESPHome {version}", lambda url: openbeken.ota_http(host, url),
                            unstick=lambda: openbeken.restart(host))
    return {"seconds": seconds, "version": version}


def step_openbeken_to_sonoff(ctx: Context) -> dict[str, Any]:
    """O2 - Sonoff's firmware, fetched by OpenBeken into the download slot:
    through the plug's import module, which checks the slot is the stock
    bootloader's before it fetches."""
    handover.consent_to_leave(ctx, "OpenBeken")
    kind = kinds.of_run(ctx.state)
    stock, version = images.stock_rbl(kind)
    if ctx.dry_run:
        return {"dry_run": True, "version": version}
    host = ctx.state.input("plug_ip")
    command = kind.openbeken_revert or "ota_http"

    def fetch(url: str) -> None:
        try:
            openbeken.command(host, f"{command} {url}")
        except openbeken.CommandError as exc:
            if "not a command" in str(exc):
                raise Abort(f"this OpenBeken has no {command}: it is not the one this tool installs on a "
                            f"{kind.name}, so where it would write Sonoff's firmware cannot be checked") from exc
            raise Abort(f"OpenBeken refused to fetch Sonoff's firmware: {exc}") from exc

    seconds = serve_to_plug(ctx, stock, f"Sonoff's firmware {version}", fetch,
                            unstick=lambda: openbeken.restart(host))
    return {"seconds": seconds, "version": version}


def step_esphome_to_openbeken(ctx: Context) -> dict[str, Any]:
    """E1 - OpenBeken, sent to ESPHome as an update: a UF2 carrying the image,
    which LibreTiny writes to the download slot."""
    handover.consent_to_leave(ctx, "ESPHome")
    kind = kinds.of_run(ctx.state)
    image, version = images.image(ctx, "openbeken")
    if ctx.dry_run:
        return {"dry_run": True, "version": version}
    data = uf2.write(image, name=f"openbeken-{kind.key}", version=version)
    seconds = handover.send_as_esphome_update(ctx, data, f"OpenBeken {version}", port=ESPHOME_OTA_PORT)
    return {"seconds": seconds, "version": version}


def step_esphome_to_sonoff(ctx: Context) -> dict[str, Any]:
    """E2 - Sonoff's firmware, sent to ESPHome as an update the same way."""
    handover.consent_to_leave(ctx, "ESPHome")
    kind = kinds.of_run(ctx.state)
    stock, version = images.stock_rbl(kind)
    if ctx.dry_run:
        return {"dry_run": True, "version": version}
    data = uf2.write(stock, name="sonoff-stock", version=version)
    seconds = handover.send_as_esphome_update(ctx, data, f"Sonoff's firmware {version}", port=ESPHOME_OTA_PORT)
    return {"seconds": seconds, "version": version}


# How long a plug just switched on may take to answer, before its record is judged.
ANSWER_WAIT = 60.0


def still_installed(ctx: Context, wait: float = ANSWER_WAIT) -> tuple[bool, str]:
    """Whether an onward step recorded done still holds: not where the plug
    answers as the firmware it was leaving. A plug not answering yet, or
    answering as the new one, leaves the record be."""
    deadline = time.monotonic() + wait
    while True:
        installed, message = evidence_installed(ctx)
        if installed is not None or time.monotonic() >= deadline:
            return installed is not False, message[0].lower() + message[1:].rstrip(".")
        time.sleep(5)


def evidence_installed(ctx: Context) -> tuple[bool | None, str]:
    """Whether a step cut off got its image onto the plug: the plug answers as
    the firmware the run installs, or still as the one it left."""
    host = ctx.state.input("plug_ip")
    firmware, source = runs.run_firmware(ctx.state), runs.run_source(ctx.state)
    running = detect.identify(host, timeout=3.0) if host else None
    if running == firmware or (firmware == "sonoff" and running is None and not detect.answers_as_tasmota(host)):
        return (True, f"The device answers as {detect.NAMES[firmware]}." if running == firmware
                else "The device answers as neither OpenBeken nor ESPHome: Sonoff's firmware is back.")
    if running == source:
        return False, f"The device still answers as {detect.NAMES[source]}: the image was not applied."
    return None, "The device does not answer as the new firmware yet - it may still be installing it."
