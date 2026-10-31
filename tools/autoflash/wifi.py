"""Where the Wi-Fi details come from: read off the plug - its Tasmota, its
ESPHome - or from its Sonoff backup, which keeps its meter calibration too.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tools.autoflash import calibration, errors, esphome, output, plugs, tasmota_settings
from tools.autoflash.device import DeviceError
from tools.autoflash.errors import Abort
from tools.espressif import inspect_stock_nvs
from tools.espressif.safeboot.prepare_imprinted_bundle import BundleError, bundle_ssid, recovery_image

if TYPE_CHECKING:
    from tools.autoflash.context import Context


def sonoff_backup_wifi(flash: bytes) -> tuple[str, str] | None:
    """The Wi-Fi Sonoff's firmware was set up with, from a backup of it: the
    name and password in its frame_params (an open network has none)."""
    names, values = inspect_stock_nvs.parse(flash[0x9000:0x19000])
    for (namespace, key), value in values.items():
        if names.get(namespace) == "frame_params" and key == "content":
            params = json.loads(value.data.split(b"\x00", 1)[0])
            ssid = params.get("ssid")
            if isinstance(ssid, str) and ssid:
                return ssid, str(params.get("passwd") or "")
    return None


def sonoff_backup_calibration(flash: bytes) -> list[float] | None:
    """The plug's factory meter calibration, from a backup of its Sonoff
    firmware: calib_coefficient0 in its user_unusual (calibration says how
    it is read)."""
    names, values = inspect_stock_nvs.parse(flash[0x9000:0x19000])
    for (namespace, key), value in values.items():
        if names.get(namespace) == "user_unusual" and key == "content":
            content = json.loads(value.data.split(b"\x00", 1)[0])
            text = content.get("calib_coefficient0") if isinstance(content, dict) else None
            return calibration.parse(text) if isinstance(text, str) else None
    return None


def backup_calibration(ctx: "Context") -> list[float] | None:
    """The factory calibration in the plug's Sonoff backup on this computer;
    None without a backup, or one that has none."""
    mac = ctx.state.input("plug_mac")
    backup = plugs.find_backup(mac) if mac else None
    if backup is None:
        return None
    try:
        return sonoff_backup_calibration((backup.directory / "flash.bin").read_bytes())
    except (OSError, ValueError, KeyError):
        return None


def wifi_from_esphome(ctx: "Context") -> tuple[str, str] | None:
    """The Wi-Fi the plug is on, name and password, from ESPHome itself: our
    ESPHome gives them from 1.1.0, through its native API - encrypted with
    its key, where it has one.  None from one that does not, or whose key is
    not known (noted: it is offered before the Wi-Fi is asked for)."""
    host = ctx.state.input("plug_ip")
    try:
        found = esphome.api_wifi_details(host, key=ctx.esphome_key) if host else None
    except esphome.ApiLocked:
        ctx.esphome_api_locked = True
        return None
    if found is None:
        return None
    output.private(found[0], "wifi-name")
    output.private(found[1], "wifi-password")
    output.say(f"    Read the Wi-Fi details off ESPHome on the device ('{found[0]}') - none to type.")
    return found


def wifi_from_backup(ctx: "Context") -> tuple[str, str] | None:
    """The Wi-Fi in the plug's Sonoff backup on this computer - the network
    its Sonoff firmware was set up on - when it is known to be the plug's
    still: the bundle set up for exactly these details, or the network
    ESPHome says it is on.  None otherwise, and the details are asked for.
    The password is read in memory only, as the backup holds it already."""
    mac = ctx.state.input("plug_mac")
    backup = plugs.find_backup(mac) if mac else None
    if backup is None:
        return None
    try:
        found = sonoff_backup_wifi((backup.directory / "flash.bin").read_bytes())
    except (OSError, ValueError, KeyError):
        return None
    if found is None:
        return None
    ssid, password = found
    output.private(ssid, "wifi-name")
    output.private(password, "wifi-password")
    try:
        pinned = bundle_ssid(ctx.manifest_path)
    except (BundleError, OSError, ValueError):
        pinned = None
    if pinned == ssid:
        # Its password rebuilds the bundle's own startup firmware, or it is not
        # the one the bundle was proven with.
        try:
            recovery_image(ctx.manifest_path, ssid, password)
        except BundleError:
            output.tell(f"    The device's Sonoff backup has an older password for '{ssid}' than the bundle's.")
            return None
        output.say(f"    The Wi-Fi in the device's Sonoff backup, '{ssid}', is the bundle's - none to type.")
        return found
    host = ctx.state.input("plug_ip")
    wifi = esphome.api_wifi(host, key=ctx.esphome_key) if host else None
    joined = wifi.get("ssid") if wifi else None
    if joined == ssid:
        output.say(f"    The device is on '{ssid}', the Wi-Fi in its Sonoff backup on this computer - "
            "none to type.")
        return found
    if joined:
        output.tell(f"    The device is on '{joined}' now, not on '{ssid}' as in its Sonoff backup.")
    return None


# How many times the Wi-Fi details are asked of a plug the network keeps from
# answering, before the person is asked for them.
WIFI_READ_TRIES = 3


def read_wifi_from_plug(ctx: "Context") -> tuple[str, str] | None:
    """The Wi-Fi details the plug is connected with, or None if they cannot be read.

    Tasmota never reports its password to a command, but its own configuration
    backup carries one.  It is decoded in memory, never written down, and only
    used when the name in it matches the network the plug says it is on - so
    the details are known to work at that moment.
    """
    def unreadable(reason: str) -> None:
        output.tell(f"    The Wi-Fi details could not be read off the device: {reason}.")

    for attempt in range(1, WIFI_READ_TRIES + 1):
        try:
            device = ctx.device
            status = device.command("Status 11")
            wifi = status.get("StatusSTS", {}).get("Wifi", {}) if isinstance(status, dict) else {}
            connected = wifi.get("SSId")
            if not connected:
                return unreadable("it did not say which network it is on")
            record = tasmota_settings.decode_backup(device.download_settings())
            if tasmota_settings.problems(record):
                return unreadable("its settings backup is not in the expected format")
            ssid = tasmota_settings.text(record, tasmota_settings.SET_STASSID1)
            password = tasmota_settings.text(record, tasmota_settings.SET_STAPWD1)
            output.private(connected, "wifi-name")
            output.private(ssid, "wifi-name")
            output.private(password, "wifi-password")
            break
        except errors.NETWORK_ERRORS as exc:
            # Seen in the fault suite: a plug not answering for a moment, a
            # Wi-Fi blip, and the person was asked to type what the plug
            # holds. Asked again once it answers, before they are.
            if attempt == WIFI_READ_TRIES:
                return unreadable(f"it did not answer as Tasmota ({exc})")
            try:
                ctx.device.wait_reachable(timeout=60.0)
            except DeviceError:
                pass
        except (Abort, DeviceError, ValueError, OSError) as exc:
            return unreadable(f"it did not answer as Tasmota ({exc})")
    if ssid != connected:
        return unreadable(f"it is on '{connected}', not the network in its first slot")
    if not password:
        # Tasmota joins with exactly its first slot's details, and it is on
        # that network now - so an empty password means an open network.
        output.say(f"    The device is on '{ssid}', an open network (no password) - none to type.")
        return ssid, ""
    output.say(f"    Read the Wi-Fi details the device is connected with ('{ssid}') - none to type.")
    return ssid, password
