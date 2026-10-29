#!/usr/bin/env python3
"""Imprint Wi-Fi details into a built prototype image, as the tool would.

    python3 ESPHome/prototype/imprint_image.py TEMPLATE OUT SSID [PASSWORD]

TEMPLATE is the built firmware.ota.bin, its placeholders untouched. Uses the
tool's own tools/autoflash/imprint.py, which also recalculates the image's
checksum and SHA-256, and reports what it found on the way.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.analyze_vendor_ota import parse_esp_image  # noqa: E402
from tools.autoflash import imprint  # noqa: E402

template_path, out_path, ssid = sys.argv[1:4]
password = sys.argv[4] if len(sys.argv) > 4 else ""
template = Path(template_path).read_bytes()
print("placeholders untouched:", imprint.has_placeholders(template))
print("placeholders at (offset, length):", imprint.credential_regions(template))
print("build-machine paths in the image:", len(imprint.find_build_paths(template)))
imprinted = imprint.imprint(template, ssid, password)
imprint.require_imprinted(imprinted)
report = parse_esp_image(imprinted)
print("imprinted: checksum valid", report["checksum_valid"], "- SHA-256 valid", report["sha256_valid"],
      "- same length", len(imprinted) == len(template))
Path(out_path).write_bytes(imprinted)
