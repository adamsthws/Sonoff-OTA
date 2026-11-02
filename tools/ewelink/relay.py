"""A plug's relay switched through eWeLink, as the app switches it: the only
way to reach it on Sonoff's firmware without its LAN key.  check_relay()
switches it to the other state and back, each confirmed by the plug's
answer to it, so a run back to Sonoff's firmware ends with the
relay proved as a run to another firmware does (autoflash's report).
"""

from __future__ import annotations

import socket
import time
from typing import Any, Callable

from tools.ewelink.get_device_key import APP_ID, EwelinkError, api_json, get_devices, login_session, select_device
from tools.ewelink.probe_ota_command import WS_DISPATCH, nonce
from tools.ewelink.websocket_minimal import WebSocket, WebSocketError

# How long the plug may take to report a switch it was sent (seconds).
SWITCH_WAIT = 15.0


def app_socket(region: str, session: dict[str, Any]) -> WebSocket:
    """The cloud's WebSocket, signed in as the app: what carries a command to
    the plug and the plug's reports back."""
    dispatch = api_json(WS_DISPATCH[region], headers={"Authorization": "Bearer " + session["at"]})
    ws = WebSocket.connect(dispatch["domain"], int(dispatch["port"]))
    now = time.time()
    ws.send_json({
        "action": "userOnline", "at": session["at"], "apikey": session["user"]["apikey"],
        "appid": APP_ID, "nonce": nonce(), "ts": int(now), "userAgent": "app",
        "sequence": str(int(now * 1000)), "version": 8,
    })
    handshake = ws.receive_json(timeout=10)
    if handshake.get("error", 0) != 0:
        raise EwelinkError("Cloud WebSocket authentication failed")
    return ws


def relay_state(params: dict[str, Any]) -> bool | None:
    """Whether the relay is on, from a plug's params: a single switch
    (switch), or outlet 0 of a list of them (switches, as the S61 has)."""
    switches = params.get("switches")
    if isinstance(switches, list):
        for outlet in switches:
            if isinstance(outlet, dict) and outlet.get("outlet", 0) == 0 and outlet.get("switch") in ("on", "off"):
                return outlet["switch"] == "on"
        return None
    value = params.get("switch")
    return value == "on" if value in ("on", "off") else None


def switch_params(params: dict[str, Any], on: bool) -> dict[str, Any]:
    """The params that switch the relay, in the shape the plug reports it."""
    word = "on" if on else "off"
    if isinstance(params.get("switches"), list):
        return {"switches": [{"switch": word, "outlet": 0}]}
    return {"switch": word}


def switch(ws: WebSocket, device: dict[str, Any], on: bool, *, wait: float = SWITCH_WAIT) -> bool:
    """Switch the relay on or off; True once the plug has taken it: its
    answer to the command (error 0), which the S61 gives without reporting
    its new state, or a report of that state."""
    seq = str(int(time.time() * 1000))
    ws.send_json({"action": "update", "deviceid": device["deviceid"], "apikey": device["apikey"],
                  "userAgent": "app", "sequence": seq,
                  "params": switch_params(device.get("params") or {}, on)})
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            msg = ws.receive_json(timeout=max(0.1, deadline - time.monotonic()))
        except socket.timeout:
            break
        if msg.get("sequence") == seq:
            return msg.get("error", 0) == 0
        if msg.get("deviceid") == device["deviceid"] and relay_state(msg.get("params") or {}) is on:
            return True
    return False


def check_relay(*, email: str, password: str, country_code: str, region: str, device_id: str,
                say: Callable[[str], None] = print) -> tuple[bool, bool] | None:
    """Switch the plug's relay to the other state and back: whether each
    switch was confirmed, or None when it could not be tried (eWeLink not
    reached, the plug offline, or no relay state in its record)."""
    try:
        region, session = login_session(email, password, country_code, region)
        device = select_device(get_devices(region, session["at"]), device_id)
        was_on = relay_state(device.get("params") or {})
        if was_on is None or not device.get("online"):
            say(f"    The device's relay cannot be switched through eWeLink "
                f"({'it is offline' if was_on is not None else 'its record gives no relay state'}).")
            return None
        ws = app_socket(region, session)
        try:
            switched = switch(ws, device, not was_on)
            restored = switch(ws, device, was_on)
        finally:
            ws.close()
    except (EwelinkError, WebSocketError, OSError, KeyError, ValueError) as exc:
        say(f"    The relay could not be switched through eWeLink: {exc}")
        return None
    return switched, restored
