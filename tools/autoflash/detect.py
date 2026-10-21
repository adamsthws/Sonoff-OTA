"""What a plug on the network is running, and finding the converted ones.

A plug still on Sonoff's firmware is found through eWeLink, as ever; one this
tool converted is found here, by what answers: Tasmota's command interface
naming the template of a kind of plug this tool converts (kinds), or
ESPHome's page with the relay the ESPHome made for it names - each, behind
its web login, once given that.  Midway through a conversion a plug can also be running the mover (an
ESPHome with no page, only its update service) or the startup firmware (a
Tasmota safeboot), which a run carrying on recognises.
"""

from __future__ import annotations

import concurrent.futures
import socket
from dataclasses import dataclass

from . import esphome, esphome_ota, hostsys, kinds, setup_network
from .plans import INTERIM_TABLE_SHA256
from .device import Device

NAMES = {
    "sonoff": "Sonoff's own firmware",
    "tasmota": "Tasmota",
    "esphome": "ESPHome",
    "midway": "a firmware part way through a conversion",
    "installer": "this tool's installer, part way from Sonoff's firmware",
    "safeboot": "Tasmota's safeboot, its Tasmota missing",
    "locked": "Tasmota with a web password",
    "esphome-login": "ESPHome with a web login",
}


def _is_installer(host: str, timeout: float) -> bool:
    """This tool's installer (the bridge) answers with its own page."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{host}/", timeout=timeout) as reply:
            return setup_network.is_bridge_page(reply.read(4096).decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - anything but the installer answering
        return False
SONOFF_TABLE_SHA256 = "f63f66bbf23b9e291c7eb5dcf24be820190dacf4bf52af515d9664526a4f4daf"
SONOFF_LAN_PORT = 8081


@dataclass
class Found:
    host: str
    firmware: str          # sonoff, tasmota, esphome; mover, startup midway
    mac: str | None = None
    name: str | None = None
    kind: str | None = None  # its kinds.Kind's key, where what it runs says

    def describe(self) -> str:
        if self.firmware == "locked":
            # Which plug it is at all, it says only once given its password.
            return f"the Tasmota at {self.host}, which has a web password (asked for next)"
        if self.firmware == "esphome-login":
            return f"the ESPHome at {self.host}, which has a web login (asked for next)"
        what = NAMES.get(self.firmware, self.firmware)
        return f"the plug at {self.host}{f' ({self.name})' if self.name else ''}, running {what}"


def _port_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _tasmota(host: str, timeout: float,
             password: str | None = None) -> tuple[str, str | None, str | None] | None:
    """("tasmota", "startup" or "hub", its name, its kind's key) if a Tasmota
    of this tool's answers.

    A safeboot is the startup firmware: it answers no commands, so says
    nothing more (converted_plugs takes only one with a MAC this computer
    knows).  A Tasmota on Sonoff's partition table is one part way through a
    conversion (the Berry Tasmota the routes pass through), whatever its
    template; official Tasmota on its own layout counts only with the
    template of a kind of plug this tool converts, which names the kind.

    A Tasmota with a web password is given `password` - only once it has
    said it is a Tasmota that wants one, so the password goes to nothing else
    on the network - and raises Locked without it, or when it is not its own.
    """
    device = Device(host, timeout=timeout)
    if device.wants_password(timeout):
        device = Device(host, timeout=timeout, password=password)
        if not password or device.wants_password(timeout):
            raise Locked(host)
    try:
        if device.is_safeboot():
            return "startup", None, None
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
        return "hub", name, None
    template = _template(device)
    if template is None:
        # Not a Tasmota without the plug's template: one too busy to say.
        raise Unreadable(host)
    kind = kinds.for_tasmota(f"{status.get('Status', {}).get('Module')} {template}")
    if kind is None:
        return None  # a Tasmota, but not on a plug this tool converts
    return "tasmota", name, kind.key


class Unreadable(RuntimeError):
    """A Tasmota that answered, but not what its template is."""


class Locked(RuntimeError):
    """A Tasmota that wants a web password: none was given, or not its own."""


def _template(device: Device) -> str | None:
    """The template Tasmota runs, or None if it would not say.  Asked again,
    then as its module, which names the template too: a plug just started, or
    answering other questions, can take longer than one ask allows."""
    for command in ("Template", "Template", "Module", "Module"):
        try:
            return str(device.command(command))
        except Exception:  # noqa: BLE001
            continue
    return None


def _esphome(host: str, timeout: float, login: tuple[str, str] | None) -> str | None:
    """"esphome" if this tool's ESPHome answers at `host` - one with the relay
    the ESPHome made for each kind of plug names; "esphome-login" for an
    ESPHome behind a web login that `login` (user name, password) is not;
    "other" for any other ESPHome; None for no ESPHome page.

    Its login goes only to a page that asked for one, and only once the
    update service there has answered as ESPHome's - Tasmota's page asks the
    same way, and the mover has no page at all - so it goes to nothing else.
    """
    found = esphome.page(host, timeout)
    if found == "login":
        if esphome_ota.probe(host, timeout=timeout) is None:
            return None
        if login is None:
            return "esphome-login"
        esphome.use_login(host, *login)
        found = esphome.page(host, timeout)
        if found == "login":
            return "esphome-login"
    if found != "esphome":
        return None
    return "esphome" if esphome.relay(host) is not None else "other"


def _answers_as_tasmota(host: str, timeout: float) -> bool:
    try:
        status = Device(host, timeout=timeout).command("Status 0")
    except Exception:  # noqa: BLE001 - anything but Tasmota answering
        return False
    return isinstance(status, dict) and "Status" in status


def tasmota(host: str, *, timeout: float = 3.0,
            password: str | None = None) -> tuple[str, str | None, str | None] | None:
    """What a Tasmota at `host` is to this tool: (what it runs, its name, its
    kind's key), as identify sees it - or None. Unreadable and Locked as there."""
    return _tasmota(host, timeout, password)


def identify(host: str, *, timeout: float = 3.0, password: str | None = None,
             esphome_login: tuple[str, str] | None = None) -> str | None:
    """sonoff, tasmota, esphome; mover, startup or hub midway; locked, a
    Tasmota whose web password `password` is not; esphome-login, an ESPHome
    whose web login `esphome_login` is not - or None if nothing known
    answers."""
    found = _esphome(host, timeout, esphome_login)
    if found:
        return None if found == "other" else found
    if _port_open(host, 80, timeout):
        if _is_installer(host, timeout):
            return "installer"
        try:
            found = _tasmota(host, timeout, password)
        except Unreadable:
            return None  # asked again later, not taken for something else
        except Locked:
            return "locked"
        if found:
            return found[0]
    if esphome_ota.probe(host, timeout=timeout) is not None:
        return "mover"
    if _port_open(host, SONOFF_LAN_PORT, timeout):
        return "sonoff"
    return None


def converted_plugs(near: str, *, known_macs: set[str] = frozenset(), timeout: float = 2.0,
                    password: str | None = None,
                    esphome_login: tuple[str, str] | None = None) -> list[Found]:
    """The plugs on the network running Tasmota or ESPHome, found by asking every
    device on the subnet of `near` (an address on it) what it is.  A Tasmota
    safeboot counts only with one of `known_macs`: a plug this computer was
    converting, left on the startup firmware - any Tasmota mid-update looks
    the same.  A Tasmota whose web password `password` is not is "locked",
    and an ESPHome whose web login `esphome_login` is not "esphome-login":
    whether it is a plug this tool converts each says only once given its own."""
    hostsys.sweep(near)
    neighbours = hostsys.neighbour_table()

    def ask(item: tuple[str, str]) -> Found | None:
        host, mac = item
        if not _port_open(host, 80, timeout):
            # The mover has no web page, only ESPHome's update service.
            if esphome_ota.probe(host, timeout=timeout) is not None:
                return Found(host, "midway", mac, "the mover")
            return None
        found = _esphome(host, timeout, esphome_login)
        if found:
            return None if found == "other" else Found(host, found, mac)
        if _is_installer(host, timeout):
            return Found(host, "installer", mac, "the installer")
        try:
            found = _tasmota(host, timeout, password)
        except Unreadable:
            # Left out this time - it is looked for again - rather than taken
            # for a Tasmota without the plug's template yet, and sent the wrong way.
            return None
        except Locked:
            return Found(host, "locked", mac)
        known = hostsys.normalise_mac(mac) in known_macs
        if not found:
            # Official Tasmota put on by a run stopped before it gave it the
            # plug's template: any Tasmota, so only a plug this computer knows.
            if known and _answers_as_tasmota(host, timeout):
                return Found(host, "midway", mac, "Tasmota, without the plug's template yet")
            return None
        if found[0] == "startup":
            if hostsys.normalise_mac(mac) not in known_macs:
                return None
            return Found(host, "midway", mac, "the startup firmware")
        return Found(host, "tasmota" if found[0] == "tasmota" else "midway", mac, found[1], found[2])

    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(ask, neighbours.items()))
    return sorted((found for found in results if found), key=lambda f: tuple(
        int(part) for part in f.host.split(".")))
