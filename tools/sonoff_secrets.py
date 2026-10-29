"""The values in a Sonoff backup's settings that say which plug, account or
network it is: what must never leave the computer the backup is on.

Used by tools/espressif/emulator/sanitise_plug.py, to show a public plug holds none of
them, and tools/leak_scan.py, to keep them out of Git. Only the standard
library and tools/inspect_stock_nvs.py: a Git hook imports it.
"""

from __future__ import annotations

import json
from pathlib import Path

try:
    from tools.inspect_stock_nvs import parse as parse_nvs
except ModuleNotFoundError:  # run from inside tools/
    from inspect_stock_nvs import parse as parse_nvs

ROOT = Path(__file__).resolve().parents[1]


# Fields of Sonoff's JSON settings that say what the product is, not which
# plug, account or network: the same on every S60 of a model, or Sonoff's own
# servers and switches. Every other field is sought.
PUBLIC_FIELDS = {"model", "uiid", "init", "dis_port", "dis_server", "is_encrypt",
                 "factory_uart_flag", "sledOnline", "timeZone", "upgradeResult",
                 "upgradeSequence", "chlLockFlg", "volChgFlg"}


def sonoff_values(stock: bytes) -> dict[bytes, str]:
    """Each value of Sonoff's settings in the dump worth finding again, and
    where it came from (named, never shown): each field of the JSON ones but
    the public, the Wi-Fi's name, password and keys, the Bluetooth key, the
    event log. Not one found in a published file as well: the same bytes there
    are a coincidence, not the plug's."""
    names, values = parse_nvs(stock[0x9000:0x19000])
    found: dict[bytes, str] = {}
    for (ns, key), value in values.items():
        space, data = names.get(ns), value.data
        where = f"{space}/{key}"
        if space in ("main", "phy", "histEnergy", "evtLogName") and key != "evtLogLog":
            continue  # an earlier Tasmota's record, the chip's calibration, counters
        try:
            fields = json.loads(data.split(b"\0", 1)[0])
        except ValueError:
            fields = None
        if isinstance(fields, dict):
            for field, item in fields.items():
                if field not in PUBLIC_FIELDS:
                    found.setdefault(str(item).encode(), f"{where} .{field}")
            continue
        if space == "nvs.net80211" and key.endswith(".ssid") and len(data) >= 4:
            data = data[4:4 + int.from_bytes(data[:4], "little")]
        elif space == "nvs.net80211" and not key.endswith((".pswd", ".passwd", ".pmk", ".apinfo", ".bssid")):
            continue  # the driver's settings: channels, modes, flags
        found.setdefault(data.rstrip(b"\0")[:64], where)
    published = b"".join(path.read_bytes() for path in sorted((ROOT / "artifacts").iterdir()) if path.is_file())
    return {value: where for value, where in found.items()
            if len(value) >= 6 and value.count(value[:1]) != len(value) and value not in published}
