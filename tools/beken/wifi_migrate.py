"""Migrate a stock S61's Wi-Fi credentials into an OpenBeken config so the
converted plug joins the user's network on first boot (no typing).

Chain: read `wifi-ssid` / `wifi-pass` from the stock EF40 store -> build an
OpenBeken mainConfig_t (ident CFG, version 5, signed-char Tiny_CRC8, creds at
0x14/0x54) -> write it at the OpenBeken config partition (BK_PARTITION_NET_PARAM).

A template (OpenBeken's own default config blob) keeps every non-credential
field valid; without one a zeroed config still loads and joins but carries no
pin/relay mapping.

  python wifi_migrate.py --stock stock.bin --obk OpenBK7238_QIO.bin \
         --out converted.bin [--cfg-offset 0x1e1000] [--template default_cfg.bin]
"""
import argparse
import sys

import ef40
from obk_config import build_config, CFG_SIZE


def read_stock_creds(stock_path):
    data = open(stock_path, "rb").read()
    kv = ef40.parse_store(data)
    ssid = kv.get("wifi-ssid")
    pw = kv.get("wifi-pass")
    if ssid is None or pw is None:
        return None, None
    return bytes(ssid).rstrip(b"\x00").decode("latin1"), bytes(pw).rstrip(b"\x00").decode("latin1")


def migrate(stock_path, obk_path, out_path, cfg_offset, template_path=None):
    ssid, pw = read_stock_creds(stock_path)
    if ssid is None:
        print("No wifi-ssid/wifi-pass in the stock EF40 store (unprovisioned plug).",
              file=sys.stderr)
        return None
    template = open(template_path, "rb").read() if template_path else None
    blob = build_config(ssid, pw, size=CFG_SIZE, template=template)
    img = bytearray(open(obk_path, "rb").read())
    if cfg_offset + len(blob) > len(img):
        img += b"\xff" * (cfg_offset + len(blob) - len(img))
    img[cfg_offset:cfg_offset + len(blob)] = blob
    open(out_path, "wb").write(img)
    return ssid, pw, blob[3]


def main():
    ap = argparse.ArgumentParser(description="Migrate stock S61 Wi-Fi creds into an OpenBeken image.")
    ap.add_argument("--stock", required=True, help="stock S61 flash dump (reads EF40 wifi-ssid/wifi-pass)")
    ap.add_argument("--obk", required=True, help="OpenBeken image to seed")
    ap.add_argument("--out", required=True, help="output (credentialed) OpenBeken image")
    ap.add_argument("--cfg-offset", default="0x1e1000", help="NET_PARAM config offset (default 0x1e1000)")
    ap.add_argument("--template", default=None, help="OpenBeken default config blob to preserve non-cred fields")
    a = ap.parse_args()
    r = migrate(a.stock, a.obk, a.out, int(a.cfg_offset, 0), a.template)
    if r:
        ssid, pw, crc = r
        print("migrated: ssid=%r pass=%r crc=0x%02x -> %s" % (ssid, pw, crc, a.out))


if __name__ == "__main__":
    main()
