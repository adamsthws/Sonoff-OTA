"""The firmware a Beken plug is given: the OpenBeken and ESPHome images
published in artifacts/ and pinned in firmware.json under "beken", by plug,
and Sonoff's own firmware for a plug going back, made from the public dump
of its stock flash when it is needed - never published here (AGENTS.md).

Each is an rt_ota image the plug's stock bootloader applies from its download
slot (tools/beken/rbl.py); the stock firmware's updater takes one wrapped in a
FOTA container (tools/beken/fota_wrap.py), OpenBeken fetches one by URL, and
an ESPHome takes one inside a UF2 (tools/beken/uf2.py).
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from tools.autoflash import kinds, output
from tools.autoflash.errors import Abort
from tools.autoflash.runs import ROOT
from tools.beken import fota_wrap
from tools.beken import rbl, stock_app

if TYPE_CHECKING:
    from tools.autoflash.context import Context

PINS = json.loads((ROOT / "artifacts" / "firmware.json").read_text(encoding="utf-8")).get("beken", {})
# The version the FOTA container says it is (the S60's bridge is sent the same way).
FOTA_VERSION = fota_wrap.ANNOUNCED_VERSION
# Each kind's stock flash, as another project publishes it: where, and what it
# is, by hash.  Fetched once into the directory the emulator keeps dumps in.
PUBLIC_DUMPS = {
    "sonoff-s61": (
        "https://raw.githubusercontent.com/openshwprojects/BekenEmulator/d9a3772a26fa92158be72faf46855726ef8e21d7"
        "/firmwares/Sonoff_S61s_EUPlug_WBBK_01P_V1.3.bin",
        "0f858068b7b6c4107119e6afdf7a6eb05349234e726028602dbcf63b92ad6df3",
    ),
}
DUMPS = ROOT / "captures" / "emulation" / "beken-firmwares"


def pin(kind: kinds.Kind, firmware: str) -> dict:
    try:
        return PINS[kind.key][firmware]
    except KeyError:
        raise Abort(f"no {firmware} image is published for the {kind.name} (artifacts/firmware.json)") from None


def image(ctx: Context, firmware: str) -> tuple[bytes, str]:
    """The published image of `firmware` for the run's kind of plug, checked
    against its pin - or, for ESPHome, one built by hand (--esphome), checked
    as an rt_ota app.  The image and its version."""
    kind = kinds.of_run(ctx.state)
    own = getattr(ctx, "esphome_firmware", None) if firmware == "esphome" else None
    if own:
        data = Path(own).read_bytes()
        try:
            header = rbl.header(data)
            rbl.unpack(data)
        except rbl.RblError as exc:
            raise Abort(f"{own} is not an rt_ota image for the plug: {exc}") from exc
        if header.name != "app":
            raise Abort(f"{own} is an rt_ota {header.name!r} image, not an app")
        output.say(f"Using the ESPHome built by hand at {own} ({header.version}).")
        return data, header.version
    pinned = pin(kind, firmware)
    path = ROOT / pinned["file"]
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise Abort(f"the published {firmware} image is missing: {exc}") from exc
    if len(data) != pinned["size"] or hashlib.sha256(data).hexdigest() != pinned["sha256"]:
        raise Abort(f"{pinned['file']} is not the file firmware.json pins: refusing to send it")
    return data, str(pinned["version"])


def stock_rbl(kind: kinds.Kind) -> tuple[bytes, str]:
    """Sonoff's own firmware for a plug of `kind`, as the rt_ota image its
    bootloader applies: made from the public dump of a stock plug, fetched
    once (DUMPS) and checked against its pinned hash.  The image and Sonoff's
    version of it."""
    try:
        url, digest = PUBLIC_DUMPS[kind.key]
    except KeyError:
        raise Abort(f"no public dump of a {kind.name}'s stock firmware is known: it cannot go back") from None
    path = DUMPS / url.rsplit("/", 1)[1]
    data = path.read_bytes() if path.is_file() else b""
    if hashlib.sha256(data).hexdigest() != digest:
        output.say(f"Fetching the public {kind.name} stock dump ({url.rsplit('/', 1)[1]}, 4 MB)...")
        try:
            with urllib.request.urlopen(url, timeout=120) as reply:
                data = reply.read()
        except OSError as exc:
            raise Abort(f"the public dump of the {kind.name}'s stock firmware could not be fetched: {exc}") from exc
        if hashlib.sha256(data).hexdigest() != digest:
            raise Abort("the public dump fetched is not the one this tool knows (its hash differs): refusing it")
        DUMPS.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    try:
        return stock_app.stock_rbl(data)
    except stock_app.DumpError as exc:
        raise Abort(f"the public dump holds no stock app this tool can use: {exc}") from exc
