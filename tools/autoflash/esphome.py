"""ESPHome instead of the final Tasmota: the image, and the plug once it runs it.

The run installs a pre-built ESPHome (artifacts/esphome-sonoff-s60-imprintable-*.bin,
from ESPHome/sonoff_s60.yaml) rather than asking anyone to build one: building it
takes gigabytes of tools and minutes of compiling. Like the recovery Safeboot,
it holds placeholders instead of a Wi-Fi name and password, and the plug's own
Wi-Fi is written into them in memory (imprint.py) - so the image on disk never
holds it, and the plug joins the home network by itself when it starts.
"""

from __future__ import annotations

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
IMAGE_SHA256 = "549dd12171535bacb5cba5dbd5c4db36411fcf52af8fe68719956de0600e8aeb"
IMAGE_SIZE = 923_504
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
            return response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, http.client.HTTPException):
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
