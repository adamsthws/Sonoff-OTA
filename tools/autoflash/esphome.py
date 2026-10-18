"""ESPHome instead of the final Tasmota: the image, and the plug once it runs it.

The run installs a pre-built ESPHome (artifacts/esphome-sonoff-s60-imprintable-*.bin,
from ESPHome/sonoff_s60.yaml) rather than asking anyone to build one: building it
takes gigabytes of tools and minutes of compiling. Like the recovery Safeboot,
it holds placeholders instead of a Wi-Fi name and password, and the plug's own
Wi-Fi is written into them in memory (imprint.py) - so the image on disk never
holds it, and the plug joins the home network by itself when it starts.
"""

from __future__ import annotations

import gzip
import hashlib
import http.client
import json
import struct
import urllib.error
import urllib.request
from pathlib import Path

from . import imprint

ROOT = Path(__file__).resolve().parents[2]
# The published image, pinned: a changed file is refused, never used unseen.
IMAGE = ROOT / "artifacts" / "esphome-sonoff-s60-imprintable-1.0.0.bin"
IMAGE_SHA256 = "5b69737350aeffb140e915c6764213dca2ac30a2b4daa6754ce1454fb66b6c6b"
IMAGE_SIZE = 950_144
# ESPHome's bootloader, from the same build, put in place of Sonoff's: Sonoff's
# (ESP-IDF 4.4) starts ESPHome but cannot roll back an update that fails to
# start ("Bootloader too old for OTA rollback").  It is all that sits before
# the partition table at 0x8000, and the one part the plug cannot start without.
BOOTLOADER = ROOT / "artifacts" / "esphome-sonoff-s60-bootloader-1.0.0.bin"
BOOTLOADER_SHA256 = "3792bb665916d69c410c9fd10f69d1a6968e61f2a9e4969829a1c49f9ea6f433"
BOOTLOADER_SIZE = 21_088
BOOTLOADER_ROOM = 0x8000
# Sonoff's app slots, which the run leaves as they are: ESPHome goes into ota_0.
SLOT_ADDRESS = 0x20000
SLOT_SIZE = 0x1F0000
# What makes a file an app rather than, say, a factory image (which starts with
# the bootloader): the app description ESP-IDF puts first in every app.
APP_DESC_MAGIC = 0xABCD5432
ESP32_C3_CHIP_ID = 5


class ImageError(ValueError):
    pass


def check_app(image: bytes, label: str) -> None:
    """Refuse anything but an ESP32-C3 app that fits Sonoff's slot."""
    if len(image) > SLOT_SIZE:
        raise ImageError(f"{label} is {len(image):,} bytes; the plug's app slot holds {SLOT_SIZE:,}")
    if len(image) < 64 or image[0] != 0xE9:
        raise ImageError(f"{label} is not an ESP32 firmware image")
    if struct.unpack_from("<H", image, 12)[0] != ESP32_C3_CHIP_ID:
        raise ImageError(f"{label} is not built for the ESP32-C3 in the S60")
    if struct.unpack_from("<I", image, 32)[0] != APP_DESC_MAGIC:
        raise ImageError(
            f"{label} is not an app image: use firmware.ota.bin, not firmware.factory.bin"
        )
    try:
        report = imprint.parse_esp_image(image)
    except ValueError as exc:
        raise ImageError(f"{label} is not a valid firmware image: {exc}") from exc
    if not report["checksum_valid"] or report["sha256_valid"] is False:
        raise ImageError(f"{label} fails its own integrity check")


def published_image() -> bytes:
    """The pinned image, its placeholders untouched."""
    image = IMAGE.read_bytes()
    if len(image) != IMAGE_SIZE or hashlib.sha256(image).hexdigest() != IMAGE_SHA256:
        raise ImageError(f"{IMAGE.name} differs from the pinned file")
    check_app(image, IMAGE.name)
    if not imprint.has_placeholders(image):
        raise ImageError(f"{IMAGE.name} has no untouched Wi-Fi placeholders")
    return image


def published_bootloader() -> bytes:
    """The pinned bootloader, checked to be one for this plug."""
    bootloader = BOOTLOADER.read_bytes()
    if (len(bootloader) != BOOTLOADER_SIZE
            or hashlib.sha256(bootloader).hexdigest() != BOOTLOADER_SHA256):
        raise ImageError(f"{BOOTLOADER.name} differs from the pinned file")
    if len(bootloader) > BOOTLOADER_ROOM or bootloader[0] != 0xE9:
        raise ImageError(f"{BOOTLOADER.name} is not an ESP32 bootloader that fits before 0x8000")
    if struct.unpack_from("<H", bootloader, 12)[0] != ESP32_C3_CHIP_ID:
        raise ImageError(f"{BOOTLOADER.name} is not built for the ESP32-C3 in the S60")
    report = imprint.parse_esp_image(bootloader)
    if not report["checksum_valid"] or report["sha256_valid"] is not True:
        raise ImageError(f"{BOOTLOADER.name} fails its own integrity check")
    return bootloader


# The mover (ESPHome/sonoff_s60_mover.yaml): a small ESPHome, run only while a
# plug leaves Tasmota, that takes the next firmware as an ESPHome update.
MOVER = ROOT / "artifacts" / "esphome-sonoff-s60-mover-1.0.0.bin"
MOVER_SHA256 = "14c3ce4426508ec697c4f87544f1830332ae8dbd59f3271197debd12e7f709bf"
MOVER_SIZE = 759_232
# Sonoff's partition table, written back when a plug leaves Tasmota's layout.
SONOFF_TABLE = ROOT / "artifacts" / "sonoff-s60-partition-table.bin"


def mover_for(ssid: str, password: str) -> bytes:
    """The pinned mover with this Wi-Fi written in."""
    image = MOVER.read_bytes()
    if len(image) != MOVER_SIZE or hashlib.sha256(image).hexdigest() != MOVER_SHA256:
        raise ImageError(f"{MOVER.name} differs from the pinned file")
    check_app(image, MOVER.name)
    return imprint.imprint(image, ssid, password)


def sonoff_table() -> bytes:
    from .plans import SONOFF_TABLE_SHA256
    table = SONOFF_TABLE.read_bytes()
    if hashlib.sha256(table).hexdigest() != SONOFF_TABLE_SHA256:
        raise ImageError(f"{SONOFF_TABLE.name} is not Sonoff's partition table")
    return table


def image_for(ssid: str, password: str, own: Path | None = None) -> bytes:
    """The image to install: the published one with this Wi-Fi written in, or
    one built by hand (--esphome PATH), imprinted too if it has placeholders."""
    if own is None:
        return imprint.imprint(published_image(), ssid, password)
    image = own.read_bytes()
    check_app(image, own.name)
    if imprint.has_placeholders(image):
        return imprint.imprint(image, ssid, password)
    return image  # built with its own Wi-Fi


# ------------------------------------------------------------ the plug, running it
# Its web page (web_server version 3) and REST paths, named after each entity's
# name in ESPHome/sonoff_s60.yaml: /switch/Relay, /sensor/Voltage, /sensor/Power.

# Straight to the plug: a proxy set for the internet has no route to it.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _request(host: str, path: str, *, post: bool = False, timeout: float = 4.0) -> str | None:
    request = urllib.request.Request(f"http://{host}{path}", data=b"" if post else None,
                                     method="POST" if post else "GET")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            body = response.read()
            # The page itself (web_server's local: true) comes gzipped, asked or not.
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                body = gzip.decompress(body)
            return body.decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, http.client.HTTPException, EOFError, gzip.BadGzipFile):
        return None


def is_esphome(host: str, timeout: float = 4.0) -> bool:
    """Whether ESPHome's own web page answers at `host` (not merely "not Tasmota")."""
    page = _request(host, "/", timeout=timeout)
    return page is not None and "<esp-app" in page


def _state(host: str, path: str) -> object:
    body = _request(host, path)
    try:
        return json.loads(body)["value"] if body else None
    except (ValueError, KeyError, TypeError):
        return None


def relay(host: str) -> bool | None:
    value = _state(host, "/switch/Relay")
    return value if isinstance(value, bool) else None


def switch_relay(host: str, on: bool) -> bool:
    """Switch the relay, and say whether it then reads as asked."""
    _request(host, f"/switch/Relay/{'turn_on' if on else 'turn_off'}", post=True)
    return relay(host) is on


def meter(host: str) -> dict[str, float]:
    """The meter's voltage and power, as check_hardware's readings: each only
    if the plug has a reading (none, until its meter has reported)."""
    readings = {}
    for key in ("Voltage", "Power"):
        value = _state(host, f"/sensor/{key}")
        if isinstance(value, (int, float)):
            readings[key] = float(value)
    return readings
