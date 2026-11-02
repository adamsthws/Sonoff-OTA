"""Sending an eWeLink plug new firmware: the one way its stock firmware takes
any, and the first write of every conversion.

The cloud passes the plug an `upgrade` command naming where to fetch the file
and its SHA-256; the plug fetches it from this computer in ranges and checks
it, then installs it and restarts.  deliver() does the whole exchange for one
plug: signs in to the owner's eWeLink account, finds the plug, waits for it to
be online, opens the cloud's WebSocket as the app does, serves the file to the
plug's address alone, sends the command and watches the delivery.  The file
is whatever the plug's updater takes - an ESP32-C3 app wrapped for the S60
(tools/espressif/serve_tasmota_ota.py checks one), a FOTA container for the
S61 (tools/beken/fota_wrap.py) - and is checked before it comes here.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import socket
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

if __package__ in (None, ""):  # run as a script: the repository root, for `tools.`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.ewelink.get_device_key import EwelinkError, get_devices, login_session, select_device  # noqa: E402
from tools.ewelink.relay import app_socket  # noqa: E402
from tools.ewelink.websocket_minimal import WebSocket, WebSocketError  # noqa: E402


# The body is sent in pieces this size, and a plug may take this long to take
# the next one (seconds).
PIECE = 16 * 1024
SEND_STALL = 60.0


class FirmwareHandler(http.server.BaseHTTPRequestHandler):
    """Serve the wrapped Tasmota binary, including Range request support."""
    server_version = "TasmotaOTA/1.0"
    protocol_version = "HTTP/1.1"  # keep-alive by default
    # The plug holds its keep-alive connection open after the last range. With
    # no idle limit the handler waits on it forever, and shutdown() - which
    # waits for the handler - never returns.
    timeout = 10

    def log_message(self, fmt, *args):
        # Successful range requests become one progress line on a terminal;
        # anything else - refusals, errors - is always printed in full.
        if self.server.live and len(args) > 1 and str(args[1]).startswith("2"):
            return
        self.server.end_progress()
        self.server.say(f"  [{self.client_address[0]}] {fmt % args}")

    def _empty_response(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_GET(self):
        if self.client_address[0] != self.server.device_ip:
            self._empty_response(403)
            return
        path = urllib.parse.urlsplit(self.path).path
        if path.lstrip("/") not in self.server.names:
            self._empty_response(404)
            return

        fw: bytes = self.server.firmware
        total = len(fw)

        range_hdr = self.headers.get("Range", "")
        if range_hdr:
            try:
                lo, hi = parse_single_range(range_hdr, total)
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            chunk = fw[lo:hi + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {lo}-{hi}/{total}")
        else:
            lo, hi = 0, total - 1
            chunk = fw
            self.send_response(200)

        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        # Sent a piece at a time, each counted once it is out, so a download
        # cut off part way says how far it got. A plug may pause between
        # pieces to write its flash: longer than the idle limit (timeout).
        self.connection.settimeout(SEND_STALL)
        for start in range(0, len(chunk), PIECE):
            piece = chunk[start:start + PIECE]
            self.wfile.write(piece)
            self.server.bytes_served += len(piece)
            self.server.served_ranges.append((lo + start, lo + start + len(piece) - 1))
        self.connection.settimeout(self.timeout)
        if self.server.live:
            unique = self.server.unique_bytes_served()
            sys.stdout.write(f"\r  Delivered {unique:,} of {total:,} bytes "
                             f"({100 * unique // total}%)   ")
            sys.stdout.flush()
            self.server.progress_shown = True
        else:
            self.server.say(f"  Served {path} bytes {lo:,}-{hi:,} ({len(chunk):,} bytes)")


class FirmwareServer(http.server.ThreadingHTTPServer):
    # Connections are handled on their own threads, so the accept loop - and
    # with it shutdown() - is never stuck behind an open connection.
    daemon_threads = True
    block_on_close = False

    def __init__(self, addr, firmware: bytes, device_ip: str, say: Callable[[str], None] = print,
                 names: tuple[str, ...] = ("user1.bin", "user2.bin"), live: bool | None = None):
        super().__init__(addr, FirmwareHandler)
        self.firmware = firmware
        # The file names it is served under: the stock updater's by default.
        self.names = frozenset(names)
        self.device_ip = device_ip
        self.say = say
        self.bytes_served = 0
        self.served_ranges: list[tuple[int, int]] = []
        # One updating progress line on a terminal; full per-request lines in
        # logs and tests, and to `say` whenever live is False (a caller that
        # keeps the terminal to itself, as Autoflash does).
        self.live = sys.stdout.isatty() if live is None else live
        self.progress_shown = False

    def end_progress(self) -> None:
        if self.progress_shown:
            sys.stdout.write("\n")
            self.progress_shown = False

    def unique_bytes_served(self) -> int:
        if not self.served_ranges:
            return 0
        merged = 0
        lo, hi = sorted(self.served_ranges)[0]
        for next_lo, next_hi in sorted(self.served_ranges)[1:]:
            if next_lo <= hi + 1:
                hi = max(hi, next_hi)
            else:
                merged += hi - lo + 1
                lo, hi = next_lo, next_hi
        return merged + hi - lo + 1


def parse_single_range(value: str, total: int) -> tuple[int, int]:
    """Parse the one explicit byte range supported by the stock updater."""
    if not value.startswith("bytes=") or "," in value:
        raise ValueError("unsupported Range header")
    bounds = value[6:].split("-")
    if len(bounds) != 2 or not bounds[0]:
        raise ValueError("range start is required")
    try:
        lo = int(bounds[0])
        hi = int(bounds[1]) if bounds[1] else total - 1
    except ValueError as exc:
        raise ValueError("non-numeric Range header") from exc
    if lo < 0 or hi < lo or lo >= total or hi >= total:
        raise ValueError("range is outside the firmware body")
    return lo, hi


# Never printed: they identify the account, the device or the network.
CLOUD_PRIVATE_KEYS = {"apikey", "deviceid", "at", "ssid", "bssid", "ip", "mac", "staMac", "userAgent"}


def describe_cloud(message: dict[str, Any], seq: str) -> str | None:
    """One plain line for a cloud message during delivery, or None to stay quiet.

    The upgrade reply, the plug going offline or coming back, and what the plug
    reports about itself are what matter here; account and device identifiers
    are never printed.
    """
    action = message.get("action")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if message.get("sequence") == seq and action is None:
        error = message.get("error", 0)
        return "Cloud: upgrade command accepted" if error == 0 else f"Cloud: upgrade command rejected (error {error})"
    if action == "sysmsg" and "online" in params:
        return ("Cloud: the device is back online" if params["online"]
                else "Cloud: the device went offline - expected while it restarts to install")
    shown = {key: value for key, value in params.items()
             if key not in CLOUD_PRIVATE_KEYS and isinstance(value, (str, int, float, bool))}
    if action == "update":
        return f"Cloud: the device reported {json.dumps(shown, sort_keys=True)}" if shown else None
    if action is None and message.get("error", 0) == 0:
        return None  # replies to the cloud's own keep-alives
    detail = f" {json.dumps(shown, sort_keys=True)}" if shown else ""
    error = f" error {message['error']}" if message.get("error") else ""
    return f"Cloud: {action or 'message'}{error}{detail}"


# How long a plug offline in eWeLink is waited for before the send is refused.
OFFLINE_WAIT = 180.0


def until_online(device: dict, look_again, *, sleep=time.sleep, say=print) -> dict:
    """`device` as eWeLink has it once it is online, or after OFFLINE_WAIT."""
    waited = 0.0
    while not device.get("online") and waited < OFFLINE_WAIT:
        if waited == 0:
            say(f"Device is offline in eWeLink; waiting up to {OFFLINE_WAIT:.0f}s for it to come back...")
        sleep(10)
        waited += 10
        device = look_again()
    return device


# Said when a send failed before the upgrade command went out: the plug was
# never asked to fetch the installer, so it never came - which the run must
# not blame on this computer's firewall (tools.autoflash.routes.espressif.installer.bridge_failure_advice).
NOT_SENT = "The upgrade command was not sent: nothing reached the device."



@dataclass
class Delivery:
    """How a send went: whether the command went out, whether every byte of the
    file reached the plug, how long it took, and what went wrong if it did."""
    sent: bool = False
    complete: bool = False
    seconds: float = 0.0
    bytes_served: int = 0
    error: str | None = None
    interrupted: bool = False


def deliver(*, email: str, password: str, country_code: str, region: str, device_id: str,
            firmware: bytes, version: str, listen_ip: str, listen_port: int, device_ip: str,
            names: tuple[str, ...] = ("user1.bin", "user2.bin"), wait: float = 120.0,
            check_device: Callable[[dict[str, Any], str], None] | None = None,
            say: Callable[[str], None] = print, live: bool | None = None) -> Delivery:
    """Send `firmware` to the plug `device_id` through eWeLink, as `version` of
    its model.  `check_device(device, model)` may refuse the plug (EwelinkError)
    once eWeLink says what it is; nothing is sent before it has passed.
    `live`: the progress line on the terminal (FirmwareServer).
    Failures before the command goes out leave `sent` False: the plug was
    never asked for anything.  Ctrl-C after every byte was served is a
    delivery completed; before, one interrupted."""
    result = Delivery()
    started = time.monotonic()
    server: FirmwareServer | None = None
    thread: threading.Thread | None = None
    ws: WebSocket | None = None
    try:
        region, session = login_session(email, password, country_code, region)
        password = ""
        devices = get_devices(region, session["at"])
        device = select_device(devices, device_id)
        model = ((device.get("extra") or {}).get("model") or device.get("model") or device.get("productModel") or "")
        say(f"Device: {device.get('name')} ({model}, stock {(device.get('params') or {}).get('fwVersion')})")
        if check_device is not None:
            check_device(device, str(model))
        # Offline a while after a send cut off part way (this computer stopped
        # mid-download, say): the plug keeps its firmware and comes back to
        # eWeLink in its own time - or, seen in the emulator, only once it is
        # switched off and on.  Waited for, not refused at once.
        device = until_online(device, lambda: select_device(get_devices(region, session["at"]), device_id),
                              say=say)
        if not device.get("online"):
            raise EwelinkError(
                "Device is offline in eWeLink; refusing to send - the upgrade command "
                "reaches the device only through eWeLink. If it has internet access, "
                "switch the device off and on (after a send cut off part way it can "
                "stay away from eWeLink until it restarts), then run this again"
            )
        ws = app_socket(region, session)

        digest = hashlib.sha256(firmware).hexdigest()
        server = FirmwareServer((listen_ip, listen_port), firmware, device_ip, say, names=names, live=live)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://{listen_ip}:{listen_port}"
        say(f"Serving only {device_ip} at {' and '.join(f'{base_url}/{name}' for name in names)}")
        say(f"SHA-256 (complete file): {digest}")
        entries = [{"name": name, "downloadUrl": f"{base_url}/{name}", "digest": digest} for name in names]
        seq = str(int(time.time() * 1000))
        ws.send_json({
            "action": "upgrade", "deviceid": device["deviceid"],
            "apikey": device["apikey"], "userAgent": "app", "sequence": seq,
            "params": {"model": model, "version": version, "binList": entries},
        })
        result.sent = True
        say("\nUpgrade command sent with owner-controlled private-LAN URLs.")
        say(f"Waiting up to {wait:.0f}s for device to connect...")

        deadline = time.monotonic() + wait
        complete_at: float | None = None
        while time.monotonic() < deadline:
            # Delivery is checked first.  A quiet cloud socket must never delay
            # the exit: every byte may already be on the device.
            if server.unique_bytes_served() == len(firmware):
                if complete_at is None:
                    complete_at = time.monotonic()
                    server.end_progress()
                    say("  Complete firmware byte coverage observed; allowing verification time.")
                elif time.monotonic() - complete_at >= 8:
                    break
            if ws is not None:
                try:
                    msg = ws.receive_json(timeout=0.5)
                    server.end_progress()
                    line = describe_cloud(msg, seq)
                    if line:
                        say(f"  {line}")
                    if msg.get("sequence") == seq and msg.get("error", 0) != 0:
                        raise EwelinkError(f"Cloud rejected upgrade command: error {msg['error']}")
                except socket.timeout:
                    pass
                except WebSocketError as exc:
                    # The device going offline for its reboot commonly closes
                    # the cloud stream. HTTP delivery must continue regardless.
                    server.end_progress()
                    say(f"  Cloud monitoring ended ({exc}); HTTP server remains active.")
                    try:
                        ws.close()
                    except OSError:
                        pass
                    ws = None
            else:
                time.sleep(0.25)
    except KeyboardInterrupt:
        # Ctrl-C after the whole image has been delivered is not a failure: the
        # device already has it and is rebooting.  Interrupting before that is.
        if server is not None and server.unique_bytes_served() == len(firmware):
            say("\n  Interrupted after every byte was delivered; treating as complete.")
        else:
            result.interrupted = True
            result.error = "interrupted before the firmware was fully delivered"
    except (EwelinkError, WebSocketError, OSError, KeyError, json.JSONDecodeError) as exc:
        result.error = str(exc)
    finally:
        password = ""
        if ws is not None:
            ws.close()
        if server is not None:
            result.bytes_served = server.unique_bytes_served()
            server.end_progress()
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=3)
    result.seconds = round(time.monotonic() - started, 1)
    result.complete = result.bytes_served == len(firmware) and len(firmware) > 0
    return result
