#!/usr/bin/env python3
"""Point a copy of the stock NVS at the emulator's open access point.

The stock firmware keeps the home Wi-Fi in two places: ESP-IDF's own
`nvs.net80211` (sta.ssid / sta.pswd) and Sonoff's `frame_params` JSON
(`ssid` / `passwd`). Both are rewritten to `--ssid` with an empty password,
and the saved BSSID / AP info is dropped so the firmware scans afresh.
Everything else (eWeLink keys, calibration, energy history) is kept as is.

The partition is decoded and rebuilt with ESP-IDF's `nvs_partition_gen`, so
entry CRCs and multi-page blobs are written the way ESP-IDF expects. The CSV
it needs holds secrets, so it lives only in a private temporary directory.

    python -I tools/espressif/emulator/rewrite_nvs.py captures/emulation/flash.bin out.bin --ssid PICSimLabWifi
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.inspect_stock_nvs import ENTRIES, PAGE, parse  # noqa: E402

NVS_OFFSET, NVS_SIZE = 0x9000, 0x10000
INTS = {0x01: "u8", 0x02: "u16", 0x04: "u32", 0x08: "u64",
        0x11: "i8", 0x12: "i16", 0x14: "i32", 0x18: "i64"}
DROPPED = {("nvs.net80211", "sta.bssid")}
# nvs_partition_gen writes these itself next to sta.ssid/sta.pswd (apinfo all
# 0xFF, pmk zeroed, apsw 0), which clears the saved AP; ours would duplicate them.
GENERATED = {("nvs.net80211", key) for key in ("sta.apinfo", "sta.pmk", "sta.apsw")}


def entries_of(page: bytes):
    """Written, non-chunk entries of one NVS page (enough to spot duplicates)."""
    index = 0
    while index < ENTRIES:
        entry = page[64 + index * 32 : 96 + index * 32]
        written = (page[32 + index // 4] >> ((index % 4) * 2)) & 3 == 2
        span = entry[2]
        if written and entry[1] != 0x42 and 1 <= span:
            yield entry
        index += span if written and 1 <= span <= ENTRIES - index else 1


def ssid_blob(ssid: bytes) -> bytes:
    """ESP-IDF's 36-byte SSID record: u32 length, then 32 bytes."""
    return len(ssid).to_bytes(4, "little") + ssid.ljust(32, b"\0")


def set_json_string(content: bytes, name: str, value: str) -> bytes:
    pattern = re.compile(rb'("' + name.encode() + rb'"\s*:\s*)"[^"]*"')
    new, count = pattern.subn(lambda m: m.group(1) + json.dumps(value).encode(), content)
    if count != 1:
        raise SystemExit(f"frame_params: expected one `{name}` field, found {count}")
    return new


def set_json_number(content: bytes, name: str, value: int) -> bytes:
    pattern = re.compile(rb'("' + name.encode() + rb'"\s*:\s*)\d+')
    new, count = pattern.subn(lambda m: m.group(1) + str(value).encode(), content)
    if count != 1:
        raise SystemExit(f"frame_params: expected one `{name}` number field, found {count}")
    return new


def rewritten(nvs: bytes, ssid: str, dis_server: str | None = None,
              dis_port: int | None = None) -> list[tuple[str, str, str, str]]:
    """CSV rows (key, type, encoding, value) for the whole changed partition."""
    names, values = parse(nvs)
    rows: list[tuple[str, str, str, str]] = []
    for ns in sorted({n for n, _ in values}):
        space = names[ns]
        rows.append((space, "namespace", "", ""))
        for (_, key), value in sorted((k, v) for k, v in values.items() if k[0] == ns):
            if (space, key) in DROPPED | GENERATED:
                continue
            data = value.data
            if space == "nvs.net80211":
                if key == "sta.ssid":
                    data = ssid_blob(ssid.encode())
                elif key == "sta.pswd":
                    data = bytes(len(data))
                elif key in ("sta.authmode", "bssid.set"):
                    data = bytes(len(data))
            elif space == "frame_params" and key == "content":
                data = set_json_string(set_json_string(data, "ssid", ssid), "passwd", "")
                if dis_server is not None:
                    data = set_json_string(data, "dis_server", dis_server)
                if dis_port is not None:
                    data = set_json_number(data, "dis_port", dis_port)
            if value.kind in INTS:
                signed = value.kind & 0x10 != 0
                rows.append((key, "data", INTS[value.kind], str(int.from_bytes(data, "little", signed=signed))))
            elif value.kind == 0x21:
                rows.append((key, "data", "string", data.rstrip(b"\0").decode()))
            else:
                rows.append((key, "data", "hex2bin", data.hex()))
    return rows


def rebuild(rows: list[tuple[str, str, str, str]]) -> bytes:
    with tempfile.TemporaryDirectory(prefix="autoflash-nvs-") as tmp:
        csv, out = Path(tmp) / "nvs.csv", Path(tmp) / "nvs.bin"
        csv.write_text("key,type,encoding,value\n" + "".join(",".join(r) + "\n" for r in rows))
        subprocess.run([sys.executable, "-m", "esp_idf_nvs_partition_gen", "generate",
                        "--outdir", tmp, str(csv), str(out), hex(NVS_SIZE)],
                       check=True, stdout=subprocess.DEVNULL)
        return out.read_bytes()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="whole-flash dump")
    parser.add_argument("output", type=Path, help="whole-flash copy to write")
    parser.add_argument("--ssid", default="PICSimLabWifi", help="open network to join")
    parser.add_argument("--dis-server", default=None,
                        help="redirect the plug's dispatch host (cloud-stub mode)")
    parser.add_argument("--dis-port", type=int, default=None,
                        help="dispatch port to go with --dis-server (default unchanged)")
    args = parser.parse_args(argv)
    if args.source.resolve() == args.output.resolve():
        raise SystemExit("refusing to overwrite the source dump")
    flash = bytearray(args.source.read_bytes())
    old = bytes(flash[NVS_OFFSET : NVS_OFFSET + NVS_SIZE])
    new = rebuild(rewritten(old, args.ssid, args.dis_server, args.dis_port))
    # Check the rebuilt partition holds the same keys, minus the dropped one,
    # and each key exactly once.
    old_names, old_values = parse(old)
    kept = {(old_names[ns], key) for ns, key in old_values} - DROPPED
    names, values = parse(new)
    if {(names[ns], key) for ns, key in values} != kept:
        raise SystemExit("rebuilt NVS does not hold the same keys")
    entries = [(entry[0], entry[1], entry[8:24]) for page in range(0, NVS_SIZE, PAGE)
               for entry in entries_of(new[page : page + PAGE])]
    if len(entries) != len(set(entries)):
        raise SystemExit("rebuilt NVS holds a key twice")
    flash[NVS_OFFSET : NVS_OFFSET + NVS_SIZE] = new
    args.output.write_bytes(flash)
    args.output.chmod(0o600)
    print(f"Wrote {args.output}: Wi-Fi set to open network '{args.ssid}', {len(values)} values kept.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
