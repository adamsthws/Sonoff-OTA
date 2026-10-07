"""Tasmota's settings record, as the pinned release lays it out.

On ESP32, Tasmota loads its settings at boot from NVS (namespace "main", key
"Settings") and uses them whenever `cfg_holder` matches; otherwise it starts
from defaults with no Wi-Fi and raises its setup network.  A record carrying the
home Wi-Fi therefore lets a freshly installed Tasmota join it straight away.

The record here is a *template*: Tasmota's own defaults with the Wi-Fi fields
blanked, captured once from a plug running the same release.  The bridge fills
in the Wi-Fi it joined with, so the password never leaves the plug.

Layout: tasmota/include/tasmota_types.h and tasmota.h at commit db3ae7e.
Algorithms: settings.ino (GetCfgCrc32, SettingsUpdateText, SettingsBufferXor).
"""

from __future__ import annotations

import struct

SIZE = 4096
CFG_HOLDER = 4617                # my_user_config.h
CONFIG_FILE_XOR = 0x5A           # backups are XORed with this + offset
TEXT_POOL = 0x017                # char text_pool[...]
TEXT_POOL_SIZE = 699             # settings_text_size
SET_STASSID1, SET_STASSID2, SET_STAPWD1, SET_STAPWD2 = 4, 5, 6, 7
IPV4_ADDRESS = 0x544             # ip, gateway, mask, dns1, dns2; ip 0.0.0.0 = DHCP
# my_user_config.h defaults.  With DHCP Tasmota records the gateway and DNS it
# was given here, so a template gets these back rather than the source network's.
IPV4_DEFAULTS = bytes([0, 0, 0, 0, 192, 168, 1, 1, 255, 255, 255, 0, 192, 168, 1, 1, 0, 0, 0, 0])
WIFI_CHANNEL = 0xF09             # last channel, used for a quick reconnect
WIFI_BSSID = 0xF0A               # last access point's address (6 bytes)
CONFIG_VERSION = 0xF36           # chip family; Tasmota refuses a restore otherwise
ESP32C3 = 4
CRC32 = 0xFFC                    # over everything before it
SSID_MAX, PASSWORD_MAX = 32, 64

# A template is only used with the firmware its layout was checked against.
#
# It is captured from a converted plug running the official release the bundle
# pins (safeboot_migration.OFFICIAL_TASMOTA_COMMIT, Tasmota 15.6.0), and
# installed into the Berry Tasmota D1 uploads (safeboot_migration
# PINNED_ARTIFACTS["bluetooth"], 15.6.0.1 built from f5b34a2).  At those two
# commits the record layout, text pool order and size, cfg_holder, CRC and the
# load path are identical, and no SettingsDelta step lies between 15.6.0 and
# 15.6.0.1.  Change either pin and these must be checked again: until then D1
# gives Tasmota its Wi-Fi by hand, and tests/test_tasmota_settings.py fails
# saying so.  See archive/docs/reference/automatic-wifi.md.
TEMPLATE_SOURCE_COMMIT = "db3ae7e0276fdc38b7aeb241a2a8e33d8ffd6892"
TEMPLATE_VERSION = 0x0F060000
BERRY_FIRMWARE_SHA256 = "a92cfadaff5a3c824f790471ba31464acd2076a24ff1fcad2de28574c5641708"


def layout_query() -> dict[str, str]:
    """The layout, as the bridge's /tasmota-settings takes it."""
    return {"pool": str(TEXT_POOL), "pool_size": str(TEXT_POOL_SIZE),
            "ssid": str(SET_STASSID1), "password": str(SET_STAPWD1), "crc": str(CRC32)}


class SettingsError(ValueError):
    pass


def crc32(data: bytes) -> int:
    """Tasmota's GetCfgCrc32: reflected 0xEDB88320, initial 0, inverted at the end."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xEDB88320 if crc & 1 else 0)
    return ~crc & 0xFFFFFFFF


def holder(settings: bytes) -> int:
    return struct.unpack_from("<H", settings, 0)[0]


def version(settings: bytes) -> int:
    """TASMOTA_VERSION of the firmware that wrote the record, e.g. 0x0F010000."""
    return struct.unpack_from("<I", settings, 8)[0]


def version_text(value: int) -> str:
    return ".".join(str((value >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def decode_backup(data: bytes) -> bytes:
    """The settings record from a Tasmota backup (`/dl`, a .dmp file)."""
    if len(data) < SIZE:
        raise SettingsError(f"a backup is at least {SIZE} bytes, this is {len(data)}")
    start = 18 if len(data) > SIZE else 2  # larger backups have a 16-byte file header
    plain = bytearray(data)
    for index in range(start, len(plain)):
        plain[index] ^= (CONFIG_FILE_XOR + index) & 0xFF
    record = bytes(plain[16 : 16 + SIZE]) if len(data) > SIZE else bytes(plain)
    return record


def encode_backup(settings: bytes) -> bytes:
    """A record as a restorable backup file (no extra driver files)."""
    _require_size(settings)
    data = bytearray(settings)
    for index in range(2, len(data)):
        data[index] ^= (CONFIG_FILE_XOR + index) & 0xFF
    return bytes(data)


def text(settings: bytes, index: int) -> str:
    """Entry `index` of the text pool (Tasmota's SettingsText)."""
    pool = settings[TEXT_POOL : TEXT_POOL + TEXT_POOL_SIZE]
    start = 0
    for _ in range(index):
        start = pool.index(b"\0", start) + 1
    return pool[start : pool.index(b"\0", start)].decode("utf-8", errors="replace")


def with_text(settings: bytes, index: int, value: str) -> bytes:
    """Replace entry `index` as Tasmota's SettingsUpdateText does: shift, never overflow."""
    encoded = value.encode("utf-8")
    if b"\0" in encoded:
        raise SettingsError("a setting cannot contain a NUL character")
    pool = settings[TEXT_POOL : TEXT_POOL + TEXT_POOL_SIZE]
    start = 0
    for _ in range(index):
        start = pool.index(b"\0", start) + 1
    end = pool.index(b"\0", start)
    rebuilt = pool[:start] + encoded + pool[end:]
    if any(rebuilt[TEXT_POOL_SIZE:]):
        raise SettingsError(f"no room in Tasmota's text settings for {len(encoded)} more bytes")
    rebuilt = rebuilt[:TEXT_POOL_SIZE].ljust(TEXT_POOL_SIZE, b"\0")
    return settings[:TEXT_POOL] + rebuilt + settings[TEXT_POOL + TEXT_POOL_SIZE :]


def with_crc(settings: bytes) -> bytes:
    _require_size(settings)
    return settings[:CRC32] + struct.pack("<I", crc32(settings[:CRC32]))


def problems(settings: bytes) -> list[str]:
    """Why Tasmota (ESP32-C3) would not take this record, if it would not."""
    if len(settings) != SIZE:
        return [f"the record is {len(settings)} bytes, not {SIZE}"]
    found = []
    if holder(settings) != CFG_HOLDER:
        found.append(f"cfg_holder is {holder(settings)}, not {CFG_HOLDER}")
    if settings[CONFIG_VERSION] != ESP32C3:
        found.append(f"it is for chip family {settings[CONFIG_VERSION]}, not ESP32-C3")
    if struct.unpack_from("<I", settings, CRC32)[0] != crc32(settings[:CRC32]):
        found.append("its checksum does not match")
    try:
        for index in (SET_STASSID1, SET_STASSID2, SET_STAPWD1, SET_STAPWD2):
            text(settings, index)
    except ValueError:
        found.append("its text settings are not in Tasmota's format")
    return found


def make_template(settings: bytes) -> bytes:
    """Strip everything that identifies a network: Wi-Fi, last AP, addresses (DHCP on)."""
    record = settings
    for index in (SET_STASSID1, SET_STASSID2, SET_STAPWD1, SET_STAPWD2):
        record = with_text(record, index, "")
    record = (record[:WIFI_CHANNEL] + bytes(7) + record[WIFI_CHANNEL + 7 :])  # channel + BSSID
    record = (record[:IPV4_ADDRESS] + IPV4_DEFAULTS
              + record[IPV4_ADDRESS + len(IPV4_DEFAULTS) :])
    return with_crc(record)


def fill(template: bytes, ssid: str, password: str) -> bytes:
    """The template with one Wi-Fi network in it, ready for Tasmota."""
    if not 0 < len(ssid.encode()) <= SSID_MAX:
        raise SettingsError(f"a Wi-Fi name is 1 to {SSID_MAX} bytes")
    if len(password.encode()) > PASSWORD_MAX:
        raise SettingsError(f"a Wi-Fi password is at most {PASSWORD_MAX} bytes")
    record = with_text(template, SET_STASSID1, ssid)
    record = with_text(record, SET_STAPWD1, password)
    return with_crc(record)


def _require_size(settings: bytes) -> None:
    if len(settings) != SIZE:
        raise SettingsError(f"the record is {len(settings)} bytes, not {SIZE}")


def template_path(root) -> "Path":
    from pathlib import Path
    return Path(root) / "artifacts" / f"tasmota-settings-template-{version_text(TEMPLATE_VERSION)}.bin"


def load_template(root, berry_sha256: str | None) -> tuple[bytes | None, str]:
    """The saved template if it fits the Berry Tasmota D1 installs, else None and why not."""
    if berry_sha256 != BERRY_FIRMWARE_SHA256:
        return None, "the bundle's Berry Tasmota is not the one the settings template was checked against"
    path = template_path(root)
    if not path.exists():
        return None, "no Tasmota settings template has been captured yet"
    template = path.read_bytes()
    found = problems(template)
    if version(template) != TEMPLATE_VERSION:
        found.append(f"it is from Tasmota {version_text(version(template))}")
    if not found and (text(template, SET_STASSID1) or text(template, SET_STAPWD1)):
        found.append("it still has Wi-Fi details in it")
    if found:
        return None, "the saved template is not usable: " + "; ".join(found)
    return template, ""
