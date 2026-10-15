"""ESPHome instead of the final Tasmota: the image, and the plug once it runs it.

The run installs a pre-built ESPHome (artifacts/esphome-s60-imprintable-*.bin,
from ESPHome/s60.yaml) rather than asking anyone to build one: building it
takes gigabytes of tools and minutes of compiling. Like the recovery Safeboot,
it holds placeholders instead of a Wi-Fi name and password, and the plug's own
Wi-Fi is written into them in memory (imprint.py) - so the image on disk never
holds it, and the plug joins the home network by itself when it starts.
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path

from . import imprint

ROOT = Path(__file__).resolve().parents[2]
# The published image, pinned: a changed file is refused, never used unseen.
IMAGE = ROOT / "artifacts" / "esphome-s60-imprintable-1.0.0.bin"
IMAGE_SHA256 = "ea054c7da18321f10f717ae6e4734552e442bd394d4749194ddff7fa603a0e0a"
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
