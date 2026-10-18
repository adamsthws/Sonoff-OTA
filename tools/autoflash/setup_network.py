"""Talking to the plug while this computer is on the plug's own Wi-Fi network.

Twice in a run the plug has no Wi-Fi details and makes a network of its own:
the recovery bridge after the first install, and Tasmota after each fresh
install. The operator joins it; everything after that happens here, so nobody
has to open a second terminal or type into a setup page.

Both the bridge and Tasmota answer at 192.168.4.1 on their own networks.
"""

from __future__ import annotations

import http.client
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

SETUP_HOST = "192.168.4.1"
BRIDGE_PAGE_MARKER = "S60 OTA Bridge"
BRIDGE_SUCCESS = "Upload verified"
# Tasmota's own wording (language/en_GB.h at the pinned release).
TASMOTA_REJECTED = "check your credentials"
TASMOTA_SUCCESS = "successful wifi connection"
TASMOTA_SAVED = "configuration saved"
# The success page links to the plug's address on the home network.
NEW_ADDRESS_RE = re.compile(r"http://(\d{1,3}(?:\.\d{1,3}){3})")

# The plug's network is not the internet: never let a configured proxy take
# these requests, or the failure looks like the plug being absent.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class SetupError(RuntimeError):
    pass


def page(host: str = SETUP_HOST, path: str = "/", timeout: float = 3.0) -> str | None:
    """The page at `path`, or None when nothing answers."""
    request = urllib.request.Request(
        f"http://{host}{path}",
        headers={"User-Agent": "S60Autoflash/1", "Referer": f"http://{host}/"},
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        return None


def wait_for_page(
    marker: str,
    *,
    host: str = SETUP_HOST,
    timeout: float = 900.0,
    poll: float = 2.0,
    tick: Callable[[float], None] | None = None,
) -> bool:
    """Wait until the page at `host` mentions `marker` - i.e. the operator has joined."""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        body = page(host, timeout=poll)
        if body is not None and marker.lower() in body.lower():
            return True
        if tick:
            tick(time.monotonic() - started)
        time.sleep(poll)
    return False


# How long the upload may go without the bridge taking a byte or answering:
# its first wait, the slot erased, is about 20 s. A network gone part way is
# noticed after this, and the upload tried again - through the bridge's own
# network, if it has raised that meanwhile.
UPLOAD_STALL = 120.0


def upload_to_bridge(image: Path, *, host: str = SETUP_HOST, timeout: float = UPLOAD_STALL) -> str:
    """POST the image to the bridge exactly as the documented curl command does.

    One raw body, HTTP/1.1, an explicit length and no `Expect: 100-continue` -
    the bridge's server does not answer that header.
    """
    payload = image.read_bytes()
    hostname, _, port = host.partition(":")
    connection = http.client.HTTPConnection(hostname, int(port or 80), timeout=timeout)
    try:
        connection.putrequest("POST", "/update", skip_accept_encoding=True)
        connection.putheader("Content-Type", "application/octet-stream")
        connection.putheader("Content-Length", str(len(payload)))
        connection.endheaders()
        view = memoryview(payload)
        for offset in range(0, len(payload), 16384):
            connection.send(view[offset : offset + 16384])
        response = connection.getresponse()
        body = response.read().decode("utf-8", errors="replace").strip()
    except (OSError, http.client.HTTPException) as exc:
        raise SetupError(f"the upload to the bridge did not complete: {exc}") from exc
    finally:
        connection.close()
    if response.status != 200 or BRIDGE_SUCCESS not in body:
        raise SetupError(f"the bridge refused the image: {body or response.status}")
    return body


def send_tasmota_settings(template: bytes, *, host: str, layout: dict[str, str],
                          timeout: float = 30.0) -> str:
    """Give bridge v5 a Tasmota settings template to fill with its Wi-Fi and save."""
    hostname, _, port = host.partition(":")
    connection = http.client.HTTPConnection(hostname, int(port or 80), timeout=timeout)
    try:
        connection.putrequest("POST", "/tasmota-settings?" + urllib.parse.urlencode(layout),
                              skip_accept_encoding=True)
        connection.putheader("Content-Type", "application/octet-stream")
        connection.putheader("Content-Length", str(len(template)))
        connection.endheaders()
        connection.send(template)
        response = connection.getresponse()
        body = response.read().decode("utf-8", errors="replace").strip()
    except (OSError, http.client.HTTPException) as exc:
        raise SetupError(f"the bridge did not take Tasmota's settings: {exc}") from exc
    finally:
        connection.close()
    if response.status == 404:
        raise SetupError("this bridge cannot give Tasmota its Wi-Fi (it is older than v5)")
    if response.status != 200:
        raise SetupError(body or f"the bridge answered {response.status}")
    return body


def port_open(host: str = SETUP_HOST, port: int = 80, timeout: float = 2.0) -> bool:
    """True when something accepts a connection - without asking it for a page.

    In setup mode every request for Tasmota's pages runs a Wi-Fi scan, and its
    single radio leaves its own network to do it. Polling a page every couple of
    seconds therefore knocks the computer off the very network it is waiting on.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(
    *,
    host: str = SETUP_HOST,
    timeout: float = 900.0,
    poll: float = 2.0,
    tick: Callable[[float], None] | None = None,
) -> bool:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if port_open(host, timeout=poll):
            return True
        if tick:
            tick(time.monotonic() - started)
        time.sleep(poll)
    return False


def send_esphome_wifi(ssid: str, password: str, *, host: str = SETUP_HOST) -> None:
    """Give ESPHome's captive portal (the mover's own network) the Wi-Fi
    details, as its own page would: it saves them and joins at once."""
    query = urllib.parse.urlencode({"ssid": ssid, "psk": password})
    if page(host, f"/wifisave?{query}", timeout=10.0) is None:
        raise SetupError(f"the plug's page at {host} did not answer")


def send_wifi(
    ssid: str,
    password: str,
    *,
    host: str = SETUP_HOST,
    settle: float = 12.0,
    timeout: float = 45.0,
    poll: float = 4.0,
) -> tuple[str, str | None]:
    """Give Tasmota's setup page the Wi-Fi details, as its own form would.

    Works from the setup network alone, so the computer needs no other
    connection.  Tasmota tests the details for about nine seconds and saves
    them only if it connects; the page is left alone meanwhile, because every
    request for it runs a Wi-Fi scan that would interrupt the test.  Then one
    request for the Wi-Fi page gives the verdict - and on success makes the
    plug restart onto the home network, naming its address there.

    Returns ("saved", address or None), ("rejected", None) when Tasmota could
    not connect - nothing is saved then - or ("unknown", None).
    """
    query = urllib.parse.urlencode({"s1": ssid, "p1": password, "save": ""})
    if page(host, f"/wi?{query}", timeout=10.0) is None:
        raise SetupError(f"the plug's setup page at {host} did not answer")
    time.sleep(settle)
    started = time.monotonic()
    while True:
        body = page(host, "/wi", timeout=20.0)
        if body is not None:
            text = body.lower()
            if TASMOTA_REJECTED in text:
                return "rejected", None
            if TASMOTA_SUCCESS in text:
                found = NEW_ADDRESS_RE.search(body)
                return "saved", found.group(1) if found else None
            if TASMOTA_SAVED in text:
                return "saved", None
        if time.monotonic() - started > timeout:
            return "unknown", None
        time.sleep(poll)  # still testing, or the scan dropped the request
