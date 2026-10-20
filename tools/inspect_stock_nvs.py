#!/usr/bin/env python3
"""Find where the stock S60 firmware keeps its Wi-Fi details (research only).

Reads the whole stock NVS partition from the research bridge's read-only
`GET /nvs` (build env `autoflash-bridge-nvs-probe`) into memory, then lists each
namespace's keys - names, types and sizes, never values - and says which
entries contain the Wi-Fi name and password you type. The dump is never
written to disk and the password is never printed.

Join the bridge's network first, then run:

    python tools/inspect_stock_nvs.py
"""

from __future__ import annotations

import argparse
import struct
import sys
import urllib.request
import zlib
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.autoflash.prompts import ask_secret, ask_text  # noqa: E402

PAGE = 4096
ENTRIES = 126
TYPE_NAMES = {
    0x01: "u8", 0x02: "u16", 0x04: "u32", 0x08: "u64",
    0x11: "i8", 0x12: "i16", 0x14: "i32", 0x18: "i64",
    0x21: "str", 0x41: "blob(v1)", 0x42: "blob-chunk", 0x48: "blob-index",
}
VARIABLE = {0x21, 0x41, 0x42}


@dataclass
class Value:
    namespace: int
    key: str
    kind: int
    chunks: dict[int, bytes] = field(default_factory=dict)
    size: int = 0

    @property
    def data(self) -> bytes:
        return b"".join(self.chunks[i] for i in sorted(self.chunks))


def parse(dump: bytes) -> tuple[dict[int, str], dict[tuple[int, str], Value]]:
    """Namespace names by index, and every written value keyed by (namespace, key)."""
    namespaces: dict[int, str] = {}
    values: dict[tuple[int, str], Value] = {}
    for base in range(0, len(dump) - PAGE + 1, PAGE):
        page = dump[base : base + PAGE]
        state = struct.unpack_from("<I", page, 0)[0]
        if state in (0xFFFFFFFF, 0xFFFFFFF0):  # empty or corrupt
            continue
        bitmap = page[32:64]
        index = 0
        while index < ENTRIES:
            written = (bitmap[index // 4] >> ((index % 4) * 2)) & 3 == 2
            entry = page[64 + index * 32 : 96 + index * 32]
            ns, kind, span, chunk = struct.unpack_from("<BBBB", entry, 0)
            if not written or kind not in TYPE_NAMES or not 1 <= span <= ENTRIES - index:
                index += 1
                continue
            crc = zlib.crc32(entry[:4] + entry[8:32], 0xFFFFFFFF) & 0xFFFFFFFF
            key = entry[8:24].split(b"\0", 1)[0].decode("ascii", "replace")
            if crc == struct.unpack_from("<I", entry, 4)[0]:
                if ns == 0 and kind == 0x01:
                    namespaces[entry[24]] = key
                else:
                    value = values.setdefault((ns, key), Value(ns, key, kind))
                    if kind in VARIABLE:
                        length = struct.unpack_from("<H", entry, 24)[0]
                        payload = page[96 + index * 32 : 64 + (index + span) * 32][:length]
                        value.chunks[chunk if kind == 0x42 else 0] = payload
                        if kind != 0x42:
                            value.kind, value.size = kind, length
                    elif kind == 0x48:
                        value.kind = 0x48
                        value.size = struct.unpack_from("<I", entry, 24)[0]
                    else:
                        value.chunks[0] = entry[24 : 24 + (kind & 0x0F)]
                        value.kind, value.size = kind, kind & 0x0F
            index += span
    return namespaces, values


def station_ssid(values: dict[tuple[int, str], Value], namespace: int) -> str | None:
    """The SSID ESP-IDF saved for station mode, if it saved one."""
    for key in ("sta.ssid", "sta.apinfo"):
        value = values.get((namespace, key))
        if value and len(value.data) >= 36:
            length = struct.unpack_from("<I", value.data, 0)[0]
            if 0 < length <= 32:
                return value.data[4 : 4 + length].decode("utf-8", "replace")
    return None


def fetch(host: str) -> bytes:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://{host}/nvs", timeout=30) as response:
        return response.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="192.168.4.1")
    args = parser.parse_args(argv)

    dump = fetch(args.host)
    print(f"Read {len(dump):,} bytes of NVS ({len(dump) // PAGE} pages), kept in memory only.\n")
    namespaces, values = parse(dump)

    ssid = ask_text("Your Wi-Fi name (as the plug was set up with)").encode()
    password = ask_secret("Its password (only compared, never shown)").encode()

    for ns in sorted({key[0] for key in values}):
        print(f"[{namespaces.get(ns, f'namespace {ns}')}]")
        for (_, key), value in sorted((k, v) for k, v in values.items() if k[0] == ns):
            data = value.data
            marks = [label for label, needle in (("Wi-Fi name", ssid), ("password", password))
                     if needle and needle in data]
            note = f"  <-- contains the {' and '.join(marks)}" if marks else ""
            size = value.size or len(data)
            print(f"  {key:<16} {TYPE_NAMES.get(value.kind, hex(value.kind)):<11} {size:>6} bytes{note}")
    print()

    net80211 = next((i for i, name in namespaces.items() if name == "nvs.net80211"), None)
    saved = station_ssid(values, net80211) if net80211 is not None else None
    if saved:
        print(f"ESP-IDF's own station record is present: SSID '{saved}'.")
    elif net80211 is not None:
        print("ESP-IDF's nvs.net80211 namespace exists but holds no station SSID.")
    else:
        print("There is no nvs.net80211 namespace: the stock firmware keeps Wi-Fi elsewhere.")
    found = any(password and password in v.data for v in values.values())
    whole = password and password in dump
    print(f"Password found in a readable entry: {'yes' if found else 'no'}"
          f"{'' if found or not whole else ' (but present elsewhere in the raw dump)'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
