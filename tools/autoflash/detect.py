"""What an S60 on the network is running, and finding the converted ones.

A plug still on Sonoff's firmware is found through eWeLink, as ever; one this
tool converted is found here, by what answers: Tasmota's command interface
naming the S60's template, or ESPHome's page with the relay its S60 config
names.  Midway through a conversion a plug can also be running the mover (an
ESPHome with no page, only its update service) or the startup firmware (a
Tasmota safeboot), which a run carrying on recognises.
"""

from __future__ import annotations

import concurrent.futures
import socket
from dataclasses import dataclass

from . import esphome, esphome_ota, hostsys
from .plans import INTERIM_TABLE_SHA256
from .device import Device

NAMES = {
    "sonoff": "Sonoff's own firmware",
    "tasmota": "Tasmota",
    "esphome": "ESPHome",
    "midway": "a firmware part way through a conversion",
}
SONOFF_TABLE_SHA256 = "f63f66bbf23b9e291c7eb5dcf24be820190dacf4bf52af515d9664526a4f4daf"
SONOFF_LAN_PORT = 8081


@dataclass
class Found:
    host: str
    firmware: str          # sonoff, tasmota, esphome; mover, startup midway
    mac: str | None = None
    name: str | None = None

    def describe(self) -> str:
        what = NAMES.get(self.firmware, self.firmware)
        return f"the S60 at {self.host}{f' ({self.name})' if self.name else ''}, running {what}"


def _port_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _tasmota(host: str, timeout: float) -> tuple[str, str | None] | None:
    """("tasmota", "startup" or "hub", its name) if an S60's Tasmota answers.

    A safeboot is the startup firmware: it answers no commands, so says
    nothing more (converted_s60s takes only one with a MAC this computer
    knows).  A Tasmota on Sonoff's partition table is one part way through a
    conversion (the Berry Tasmota the routes pass through), whatever its
    template; official Tasmota on its own layout counts only with the S60's.
    """
    device = Device(host, timeout=timeout)
    try:
        if device.is_safeboot():
            return "startup", None
        status = device.command("Status 0")
        if isinstance(status, dict) and "StatusFWR" not in status:
            status["StatusFWR"] = device.command("Status 2").get("StatusFWR", {})
    except Exception:  # noqa: BLE001 - anything but Tasmota answering
        return None
    if not isinstance(status, dict) or "Status" not in status:
        return None
    name = status.get("StatusNET", {}).get("Hostname")
    version = str(status.get("StatusFWR", {}).get("Version", ""))
    try:
        # Or the interim layout a commit stopped part way leaves: the same
        # Tasmota, its settings moved aside, which the next step puts back.
        sonoff_layout = device.sha_region(0x8000, 0x1000, timeout=20) in (SONOFF_TABLE_SHA256,
                                                                           INTERIM_TABLE_SHA256)
    except Exception:  # noqa: BLE001 - no Berry: not one of this tool's
        sonoff_layout = False
    # The routes' own Tasmota is the bluetooth build.  Official Tasmota with
    # Sonoff's table is one leaving Tasmota that has not restarted yet: still
    # Tasmota, so leaving it carries on (the plan finds its writes done).
    if sonoff_layout and "bluetooth" in version:
        return "hub", name
    try:
        template = device.command("Template")
    except Exception:  # noqa: BLE001
        template = {}
    named = f"{status.get('Status', {}).get('Module')} {template}"
    if "S60" not in named:
        return None  # a Tasmota, but not an S60's
    return "tasmota", name


def _answers_as_tasmota(host: str, timeout: float) -> bool:
    try:
        status = Device(host, timeout=timeout).command("Status 0")
    except Exception:  # noqa: BLE001 - anything but Tasmota answering
        return False
    return isinstance(status, dict) and "Status" in status


def identify(host: str, *, timeout: float = 3.0) -> str | None:
    """sonoff, tasmota, esphome; mover, startup or hub midway - or None if
    nothing known answers."""
    if esphome.is_esphome(host, timeout=timeout):
        return "esphome" if esphome.relay(host) is not None else None
    if _port_open(host, 80, timeout):
        found = _tasmota(host, timeout)
        if found:
            return found[0]
    if esphome_ota.probe(host, timeout=timeout) is not None:
        return "mover"
    if _port_open(host, SONOFF_LAN_PORT, timeout):
        return "sonoff"
    return None


def converted_s60s(near: str, *, known_macs: set[str] = frozenset(), timeout: float = 2.0) -> list[Found]:
    """The S60s on the network running Tasmota or ESPHome, found by asking every
    device on the subnet of `near` (an address on it) what it is.  A Tasmota
    safeboot counts only with one of `known_macs`: a plug this computer was
    converting, left on the startup firmware - any Tasmota mid-update looks
    the same."""
    hostsys.sweep(near)
    neighbours = hostsys.neighbour_table()

    def ask(item: tuple[str, str]) -> Found | None:
        host, mac = item
        if not _port_open(host, 80, timeout):
            # The mover has no web page, only ESPHome's update service.
            if esphome_ota.probe(host, timeout=timeout) is not None:
                return Found(host, "midway", mac, "the mover")
            return None
        if esphome.is_esphome(host, timeout=timeout):
            if esphome.relay(host) is not None:
                return Found(host, "esphome", mac)
            return None
        found = _tasmota(host, timeout)
        known = hostsys.normalise_mac(mac) in known_macs
        if not found:
            # Official Tasmota put on by a run stopped before it gave it the
            # S60's settings: any Tasmota, so only a plug this computer knows.
            if known and _answers_as_tasmota(host, timeout):
                return Found(host, "midway", mac, "Tasmota, not yet set up as an S60")
            return None
        if found[0] == "startup":
            if hostsys.normalise_mac(mac) not in known_macs:
                return None
            return Found(host, "midway", mac, "the startup firmware")
        return Found(host, "tasmota" if found[0] == "tasmota" else "midway", mac, found[1])

    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(ask, neighbours.items()))
    return sorted((found for found in results if found), key=lambda f: tuple(
        int(part) for part in f.host.split(".")))
