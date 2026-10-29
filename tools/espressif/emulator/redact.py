#!/usr/bin/env python3
"""Mask a stock dump's secrets in emulator logs before anyone reads them.

The stock firmware may print its Wi-Fi details or eWeLink keys on the UART.
This reads those secrets from the dump's NVS in memory, never prints them, and
replaces each one (raw, and as hex for binary keys) with `<redacted:NAME>`.
Panic memory dumps print RAM as little-endian words, which these patterns
cannot see into, so their lines are dropped whole.

    python -I tools/espressif/emulator/redact.py captures/emulation/flash.bin < uart.log
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.inspect_stock_nvs import parse  # noqa: E402

NVS_OFFSET, NVS_SIZE = 0x9000, 0x10000
# Kept visible: already public in the handoff, or needed to read the log.
PUBLIC_JSON = {"init", "deviceid", "model"}
# ESP-IDF's own Wi-Fi blobs: u32 length then the bytes (ssid), or raw bytes.
NET80211 = {"sta.ssid": "wifi-ssid", "ap.ssid": "ap-ssid", "sta.pswd": "wifi-password",
            "ap.passwd": "ap-password", "sta.pmk": "wifi-pmk", "ap.pmk": "ap-pmk"}
MIN_LEN = 4
MEMORY_DUMP = re.compile(rb"^\s*(?:0x)?[0-9a-fA-F]{8}:\s+0x[0-9a-fA-F]{8}")


def secrets(flash: bytes) -> dict[bytes, str]:
    """Every secret byte string in the dump's NVS, mapped to a label."""
    names, values = parse(flash[NVS_OFFSET : NVS_OFFSET + NVS_SIZE])
    found: dict[bytes, str] = {}

    def add(raw: bytes, label: str) -> None:
        raw = raw.rstrip(b"\0")
        if len(raw) >= MIN_LEN and len(set(raw)) > 1:  # not erased/blank filler
            found.setdefault(raw, label)
            found.setdefault(raw.hex().encode(), label)
            found.setdefault(raw.hex().upper().encode(), label)

    for (ns, key), value in values.items():
        data, space = value.data, names.get(ns, "")
        if space == "nvs.net80211" and key in NET80211:
            label = NET80211[key]
            if key.endswith("ssid") and len(data) >= 36:
                length = int.from_bytes(data[:4], "little")
                add(data[4 : 4 + min(length, 32)], label)
            else:
                add(data, label)
        elif space == "bt_config.conf":
            for item in re.findall(rb"[0-9A-Fa-f]{16,}", data):
                found.setdefault(item, "bt-key")
        elif data[:1] == b"{":
            for name, text in re.findall(rb'"([A-Za-z0-9_]+)"\s*:\s*"([^"]*)"', data):
                if name.decode() not in PUBLIC_JSON and len(text) >= MIN_LEN:
                    found.setdefault(text, f"{space}.{name.decode()}")
    return found


def redactor(flash: bytes):
    table = secrets(flash)
    pattern = re.compile(b"|".join(re.escape(s) for s in sorted(table, key=len, reverse=True)))

    def apply(line: bytes) -> bytes:
        if MEMORY_DUMP.match(line):
            return b""
        return pattern.sub(lambda m: b"<redacted:" + table[m.group(0)].encode() + b">", line)

    return apply


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    apply = redactor(Path(args[0]).read_bytes())
    for line in sys.stdin.buffer:
        sys.stdout.buffer.write(apply(line))
        sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
