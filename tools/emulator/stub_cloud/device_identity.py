#!/usr/bin/env python3
"""Derive the stub's account-API view of a plug from its own flash dump.

The LAN-control key the tool would normally fetch from the cloud is already in
the flash we booted (self-hosting; see docs/cloud-stub.md), so the stub
reads it straight from there. Emits the JSON `stub_server.py --device-json`
expects. Run inside the instance container against the read-only stock image,
writing to the container's own /run (not /state, which is on your disk), so the
key never leaves that network:

    python3 -I -B device_identity.py /secrets/stock-flash.bin --out /run/s60emu/stub-device.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.inspect_stock_nvs import parse  # noqa: E402

NVS_OFFSET, NVS_SIZE = 0x9000, 0x10000
# eWeLink's names for the S60 (S60TPG UK, S60TPF Europe), as the tool accepts them.
PRODUCT_MODEL = re.compile(r"S60TP[A-Z]")
MAC = re.compile(r"[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}")


def frame_params(flash: bytes) -> dict:
    names, vals = parse(flash[NVS_OFFSET:NVS_OFFSET + NVS_SIZE])
    for (ns, key), value in vals.items():
        if names[ns] == "frame_params" and key == "content":
            return json.loads(value.data.split(b"\x00", 1)[0])
    raise SystemExit("frame_params not found in NVS")


def identity(flash: bytes, lan_key_field: str, product_model: str = "", sta_mac: str = "") -> dict:
    fp = frame_params(flash)
    lan_key = fp.get(lan_key_field)
    if not lan_key:
        raise SystemExit(f"frame_params has no `{lan_key_field}` for the LAN key")
    device = {
        "deviceid": fp["deviceid"],
        "apikey": fp["apikey"],
        # The LAN key: the plug encrypts its LAN advert with it, which the tool
        # opens for the plug's Wi-Fi signal. In this plug's dump apikey and
        # encrypt_key hold the same value, and either opens it; --lan-key-field
        # chooses for a plug where they differ.
        "devicekey": lan_key,
        "model": fp.get("model", "SN-ESP32C3-S60-01"),
        "uiid": fp.get("uiid", 190),
        "name": "S60 (stub)",
        # Not here: the firmware version, which the stub reports as the plug
        # gives it.
    }
    # The product model is eWeLink's name for the plug, given when it was
    # paired, and is not in the dump: only passed in, from what eWeLink said.
    if product_model:
        if not PRODUCT_MODEL.fullmatch(product_model):
            raise SystemExit(f"{product_model!r} is not an S60 product model (S60TPG or S60TPF)")
        device["productModel"] = product_model
    # Its Wi-Fi MAC, which eWeLink's record of it carries (params.staMac) and
    # the tool first looks for it on the network by. The plug never says it to
    # the stub, so it is the MAC of the plug being run, passed in.
    if sta_mac:
        if not MAC.fullmatch(sta_mac):
            raise SystemExit(f"{sta_mac!r} is not a MAC address")
        device["staMac"] = sta_mac.replace("-", ":").upper()
    return device


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("flash", type=Path, help="whole-flash dump to read")
    parser.add_argument("--lan-key-field", choices=("apikey", "encrypt_key"),
                        default="apikey", help="NVS field holding the LAN key")
    parser.add_argument("--product-model", default="",
                        help="eWeLink's product model for this plug (S60TPG or S60TPF), "
                             "which the dump does not record")
    parser.add_argument("--sta-mac", default="",
                        help="the plug's Wi-Fi MAC, which eWeLink's record of it carries")
    parser.add_argument("--out", type=Path, default=None, help="write here (0600) instead of stdout")
    args = parser.parse_args(argv)
    data = identity(args.flash.read_bytes(), args.lan_key_field, args.product_model.strip().upper(),
                    args.sta_mac.strip())
    text = json.dumps(data, indent=2)
    if args.out:
        args.out.write_text(text)
        args.out.chmod(0o600)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
