"""A converted plug handed its next firmware: the go-ahead, asked once before
the first write of a run from Tasmota, ESPHome or OpenBeken, and an image sent
to an ESPHome as an ordinary update, whatever the plug.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from tools.autoflash import esphome_ota, output, prompts, unlock
from tools.autoflash.errors import Abort, NothingWritten

if TYPE_CHECKING:
    from tools.autoflash.context import Context


def consent_to_leave(ctx: Context, what: str) -> None:
    """The go-ahead, once, before the first write of a run from a converted plug.
    Not asked again as a step that stopped is tried again by itself: seen, a
    retry asked for it, and Enter stopped the run."""
    if getattr(ctx, "consented", False):
        return
    output.transient_begin()
    prompts.warning_box("Before going on", [
        f"From here the device's {what} is replaced. DO NOT UNPLUG IT during the",
        "process: a power cut while its memory is rewritten could leave a device",
        "that has to be opened to repair.",
        "Only go ahead with a device you can afford to lose.",
    ])
    if not prompts.require_word(prompts.CONSENT_WORD):
        raise NothingWritten("not confirmed - nothing has been written to the device")
    output.transient_end()
    ctx.consented = True


def send_as_esphome_update(ctx: Context, image: bytes, what: str, port: int = esphome_ota.PORT) -> float:
    """`image` as an ordinary ESPHome update, to the ESPHome (or the mover) at
    the plug's address, on `port` - ESPHome's own on an Espressif plug,
    LibreTiny's on a Beken one.  How long the sending took."""
    host = ctx.state.input("plug_ip")
    started = time.monotonic()

    def progress(sent: int, total: int) -> None:
        output.progress(f"sending {what}: {100 * sent // total}%")

    # Asked before the go-ahead (unlock_esphome); here again only for a plug
    # that changed since, or a run sending it once more.
    while True:
        try:
            esphome_ota.upload(host, image, port=port, progress=progress, password=ctx.esphome_password,
                               key=ctx.esphome_key)
            break
        except esphome_ota.NeedsPassword:
            output.progress_clear()
            unlock.ask_esphome_password(ctx, host)
        except (esphome_ota.NeedsEncryption, esphome_ota.WrongKey):
            output.progress_clear()
            unlock.ask_esphome_key(ctx, host)
        except (esphome_ota.OTAError, OSError) as exc:
            raise Abort(f"sending {what} to the device failed: {exc}") from exc
        finally:
            output.progress_clear()
        started = time.monotonic()  # the time asked is not the time sending
    return round(time.monotonic() - started, 1)
