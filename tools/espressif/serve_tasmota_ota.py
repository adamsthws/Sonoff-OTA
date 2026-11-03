#!/usr/bin/env python3
"""Serve a validated wrapped ESP32-C3 app to one owned plug.

This is the live, state-changing end of the research workflow.  It remains
blocked by ``RECOVERY_LOCK`` and also requires an explicit no-rollback
acknowledgement.  Building or validating an image never invokes this tool.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import os
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # run as a script: the repository root, for `tools.`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.autoflash import kinds  # noqa: E402
from tools.autoflash.prompts import ask_secret
from tools.espressif.analyze_vendor_ota import analyze
from tools.espressif.build_vendor_ota import ANNOUNCED_VERSION, DEFAULT_MAX_PAYLOAD_SIZE, announce
from tools.ewelink.get_device_key import EwelinkError, target_id_from_mdns
from tools.ewelink.ota_push import NOT_SENT, FirmwareServer, deliver, parse_single_range  # noqa: F401  (the server, for its tests)


SAFETY_FLAG = "--i-understand-stock-has-no-automatic-rollback"


def report_failure(exc: BaseException, sent: bool) -> None:
    print(f"ERROR: {exc}", file=sys.stderr)
    if not sent:
        print(NOT_SENT, file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-ip", required=True)
    parser.add_argument("--listen-port", type=int, default=8088)
    parser.add_argument("--device-ip", required=True)
    parser.add_argument("--firmware", type=Path, required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--country-code", default="+44")
    parser.add_argument("--region", default="eu")
    parser.add_argument("--expected-current-version", required=True)
    parser.add_argument("--mdns-capture", type=Path, required=True)
    parser.add_argument("--wait", type=float, default=120.0)
    parser.add_argument(SAFETY_FLAG, action="store_true")
    args = parser.parse_args()

    recovery_lock = Path(__file__).resolve().parent / "RECOVERY_LOCK"
    if recovery_lock.exists():
        print(f"Refusing: recovery lock is active at {recovery_lock}", file=sys.stderr)
        return 2
    if not args.i_understand_stock_has_no_automatic_rollback:
        print(f"Refusing to run without {SAFETY_FLAG}", file=sys.stderr)
        return 2
    listen_address = ipaddress.ip_address(args.listen_ip)
    if listen_address.version != 4 or not listen_address.is_private:
        print("Refusing: --listen-ip must be a private IPv4 LAN address", file=sys.stderr)
        return 2
    device_address = ipaddress.ip_address(args.device_ip)
    if device_address.version != 4 or not device_address.is_private:
        print("Refusing: --device-ip must be a private IPv4 LAN address", file=sys.stderr)
        return 2

    try:
        firmware = args.firmware.read_bytes()
    except OSError as exc:
        print(f"Refusing: could not read firmware: {exc}", file=sys.stderr)
        return 2
    try:
        report = analyze(firmware)
    except ValueError as exc:
        print(f"Refusing invalid OTA file: {exc}", file=sys.stderr)
        return 2
    wrapper = report["wrapper"]
    esp = report["esp_image"]
    valid = (
        wrapper["magic_valid"] and wrapper["header_reserved_zero"]
        and wrapper["header_crc32_valid"] and wrapper["payload_offset_valid"]
        and wrapper["payload_size_valid"] and wrapper["payload_crc32_valid"]
        and wrapper["record_crc32_valid"] and wrapper["record_reserved_zero"]
        and wrapper["model"] in {kind.wrapper_model for kind in kinds.ALL}
        and wrapper["actual_payload_size"] <= DEFAULT_MAX_PAYLOAD_SIZE
        and esp["chip_is_esp32_c3"] and esp["checksum_valid"]
        and esp["sha256_valid"] is not False and esp["trailing_bytes"] == 0
    )
    if not valid:
        print("Refusing: firmware failed the complete wrapper/native-image preflight", file=sys.stderr)
        return 2
    firmware = announce(firmware)
    print(f"Firmware: {args.firmware} ({len(firmware):,} bytes), sent as {ANNOUNCED_VERSION}, "
          f"SHA-256 {hashlib.sha256(firmware).hexdigest()}")

    password = os.environ.get("EWELINK_PASSWORD") or ask_secret("eWeLink password: ")

    def check(device: dict[str, Any], model: str) -> None:
        # Only a plug of a kind this tool converts, and only a file wrapped
        # for it: another Sonoff on the same account must never be sent it.
        kind = kinds.for_stock_model(model)
        if kind is None:
            raise EwelinkError(
                f"Device model is {model}, not one this tool converts; refusing"
            )
        if wrapper["model"] != kind.wrapper_model:
            raise EwelinkError(
                f"The file is wrapped for {wrapper['model']}, not the {kind.name}'s "
                f"{kind.wrapper_model}; refusing"
            )
        version = str((device.get("params") or {}).get("fwVersion"))
        if version != args.expected_current_version:
            raise EwelinkError(
                f"Device reports {version}, expected "
                f"{args.expected_current_version}; refusing"
            )

    delivered = deliver(email=args.email, password=password, country_code=args.country_code, region=args.region,
                        device_id=target_id_from_mdns(args.mdns_capture), firmware=firmware,
                        version=ANNOUNCED_VERSION, listen_ip=args.listen_ip, listen_port=args.listen_port,
                        device_ip=args.device_ip, wait=args.wait, check_device=check)
    password = ""
    if delivered.error and not delivered.interrupted:
        report_failure(EwelinkError(delivered.error), delivered.sent)
    elif delivered.interrupted:
        print("\n  Interrupted before the firmware was fully delivered.", file=sys.stderr)
    if not delivered.sent and delivered.error:
        return 1
    print(f"\nUnique firmware bytes served: {delivered.bytes_served:,}/{len(firmware):,}")
    if not delivered.sent or delivered.bytes_served == 0:
        print("Device did not connect; no firmware bytes were transferred.")
    elif not delivered.complete:
        print("WARNING: the device did not request every firmware byte.")
    return 0 if delivered.error is None and delivered.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
