#!/usr/bin/env python3
"""The local cloud stub: one HTTPS listener that stands in for eWeLink.

This is self-hosting (see docs/cloud-stub.md): our own device, redirected to our
own domain, served our own cert under the CA we made it trust. It speaks just
enough of eWeLink for the plug to come online and for the conversion tool to
drive it - nothing reaches, forges or impersonates the real cloud.

One TLS socket on `:443` wears every hat, routed by request:

  POST /dispatch/device   the plug's dispatch: the IoT server to connect to -
                          ourselves, as a numeric IP (the plug does no DNS here).
  GET  /api/ws            the IoT channel, a minimal RFC 6455 server. The plug
                          registers, reports state and asks the date; we answer
                          error:0 (+ a heartbeat config on register) so it is
                          online. The tool's app side connects here too, and its
                          commands for the plug (an OTA upgrade) are relayed.
  GET  /dispatch/app      the tool's dispatch: the same IoT server.
  /v2/user/login, /v2/device/thing, /v2/device/ota/query
                          the account API the tool reads the plug's LAN key,
                          model and version from (the key is the one in the dump
                          we booted, from --device-json).

Stdlib only, so it runs under the emulator image's plain python3. Every message
is logged with secret values redacted, so one run is also the capture that pins
the protocol. Run inside the instance container, bound to the router address
the plug uses:

    python3 stub_server.py --bind 192.168.60.1 --cert server.crt --key server.key \\
        --device-json stub-device.json

With `--plain` it serves plaintext HTTP/WS instead of TLS and needs no cert. The
emulated S61's dispatch is plaintext (its factory dispatch URL is
`http://...:80`); its IoT WebSocket is still `wss://` and verifies the server
cert, so a run serves dispatch plaintext and the IoT channel over TLS and swaps
the firmware's trust anchor. See tools/beken/cloud_online.py.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import socket
import socketserver
import ssl
import struct
import sys
import threading
from pathlib import Path

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455 magic
# Never shown in cleartext in the log: they identify the account or device, or
# are the LAN-control key itself (devicekey/encrypt_key/apikey let one command
# the plug locally).
SECRET_KEYS = {"apikey", "deviceid", "devicekey", "encrypt_key", "at", "rt",
               "selfApikey", "finalUserApikey", "flashCipher", "ssid", "bssid",
               "staMac", "password", "token"}
# A client that connects and then says nothing must not hold a thread for ever.
REQUEST_TIMEOUT = 30.0


def log(*parts: object) -> None:
    ts = dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(ts, *parts, flush=True)


def redacted(value: object) -> object:
    """A copy of a decoded message safe to print (secret values masked)."""
    if isinstance(value, dict):
        return {k: ("<redacted>" if k in SECRET_KEYS else redacted(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redacted(v) for v in value]
    return value


# -- WebSocket framing (server side: read masked, write unmasked) --------------

def encode_frame(payload: bytes, opcode: int = 1) -> bytes:
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n <= 0xFFFF:
        header.append(126)
        header += struct.pack("!H", n)
    else:
        header.append(127)
        header += struct.pack("!Q", n)
    return bytes(header) + payload


def recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("peer closed")
        data += chunk
    return bytes(data)


def read_frame(sock: socket.socket) -> tuple[int, bytes]:
    first, second = recv_exact(sock, 2)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", recv_exact(sock, 8))[0]
    mask = recv_exact(sock, 4) if masked else b""
    payload = bytearray(recv_exact(sock, length))
    for i in range(len(payload)):
        payload[i] ^= mask[i % 4] if mask else 0
    return opcode, bytes(payload)


# -- app <-> device relay ------------------------------------------------------
# The real cloud passes an app's command (e.g. an OTA "upgrade") to the device
# and the device's replies back. Device and app each hold their own WebSocket to
# us, so we route between them by deviceid. This is what lets the conversion's
# serve_tasmota_ota drive the online plug (verification test 4c).

class Conn:
    """A WebSocket peer with a lock, so either thread can send to it safely."""
    def __init__(self, sock: socket.socket, peer: str):
        self.sock = sock
        self.peer = peer
        self.lock = threading.Lock()

    def send(self, payload: bytes, opcode: int = 1) -> None:
        with self.lock:
            self.sock.sendall(encode_frame(payload, opcode=opcode))


_relay_lock = threading.Lock()
_devices: dict[str, Conn] = {}          # deviceid -> the device's WebSocket
_subscribers: dict[str, set[Conn]] = {}  # deviceid -> app WebSockets watching it


def _register_device(deviceid: str, conn: Conn) -> None:
    with _relay_lock:
        _devices[deviceid] = conn
        _subscribers.setdefault(deviceid, set())


def _drop_device(deviceid: str, conn: Conn) -> set[Conn]:
    with _relay_lock:
        if _devices.get(deviceid) is conn:
            _devices.pop(deviceid, None)
        return set(_subscribers.get(deviceid, ()))


def _subscribe(deviceid: str, app: Conn) -> Conn | None:
    with _relay_lock:
        _subscribers.setdefault(deviceid, set()).add(app)
        return _devices.get(deviceid)


def _subscribers_of(deviceid: str) -> set[Conn]:
    with _relay_lock:
        return set(_subscribers.get(deviceid, ()))


def _unsubscribe(app: Conn) -> None:
    with _relay_lock:
        for watchers in _subscribers.values():
            watchers.discard(app)


def _online(deviceid: str) -> bool:
    """Whether the device holds its IoT WebSocket now: what eWeLink calls online."""
    with _relay_lock:
        return deviceid in _devices


# -- the server ----------------------------------------------------------------

class Stub:
    def __init__(self, iot_host: str, iot_port: int, hb_interval: int, device: dict | None = None):
        self.iot_host = iot_host
        self.iot_port = iot_port
        self.hb_interval = hb_interval
        self.device = device  # the account-API view of this plug, or None
        # What the plug says of itself (firmware version, model, the params of
        # its updates) in its own dispatch, register and update messages: the
        # account API reports that, as the real cloud does, rather than
        # anything assumed about the dump.
        self.reported: dict[str, dict] = {}

    def heard(self, msg: dict) -> None:
        deviceid = msg.get("deviceid")
        if not deviceid:
            return
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        version = msg.get("fwVersion") or msg.get("romVersion") or params.get("fwVersion")
        seen = self.reported.setdefault(deviceid, {})
        if msg.get("action") == "update":
            # Its state as it reports it (its network, its version): eWeLink's
            # record of a device carries the params it last sent.
            seen.setdefault("params", {}).update(params)
        if version:
            seen["fwVersion"] = version
        if msg.get("model"):
            seen["model"] = msg["model"]

    def dispatch_response(self) -> dict:
        # The plug connects to this host by numeric IP without DNS (it
        # inet_aton()s the field), so hand back an address, not a hostname.
        # The S60 reads `IP`; the S61 reads `domain` (same numeric address) -
        # include both so either firmware finds the field it parses.
        return {"error": 0, "reason": "", "IP": self.iot_host, "domain": self.iot_host,
                "port": self.iot_port}

    def app_dispatch_response(self) -> dict:
        # The tool's app-side dispatch (serve_tasmota_ota). It connects by TLS
        # with server_hostname set to this value, which the leaf's IP SAN covers.
        return {"error": 0, "reason": "", "domain": self.iot_host, "IP": self.iot_host,
                "port": self.iot_port}

    # -- tool-facing account API (touchpoint A) ---------------------------------
    # The tool already has this plug's key because it is in the flash dump we
    # booted; the stub just hands back that same local value. Nothing is fetched
    # from or sent to the real cloud.

    def account_login(self) -> dict:
        apikey = (self.device or {}).get("apikey", "stub-user-apikey")
        return {"error": 0, "data": {"at": "stub-access-token", "rt": "stub-refresh-token",
                                     "region": "eu", "user": {"apikey": apikey}}}

    def device_thing(self) -> dict:
        d = self.device
        if not d:
            return {"error": 404, "msg": "no device configured in the stub"}
        seen = self.reported.get(d["deviceid"], {})
        model = seen.get("model") or d.get("model")
        params = {**seen.get("params", {}), "model": model}
        if seen.get("fwVersion"):  # unknown until the plug has said, as at eWeLink
            params["fwVersion"] = seen["fwVersion"]
        if d.get("staMac"):  # eWeLink has it from the plug; this one never says it here
            params.setdefault("staMac", d["staMac"])
        item = {"itemData": {
            "deviceid": d["deviceid"], "devicekey": d["devicekey"],
            "apikey": d.get("apikey", d["devicekey"]),
            "name": d.get("name", "S60 (stub)"),
            "online": _online(d["deviceid"]),
            "extra": {"uiid": d.get("uiid", 190), "model": model},
            "params": params,
        }}
        # The product model (S60TPG/S60TPF) only when known: eWeLink names it,
        # the dump does not, so it is what eWeLink gave this plug before (see
        # emu.sh). Without it the tool leaves the report's plug model blank and
        # names the Tasmota template plainly "Sonoff S60".
        if d.get("productModel"):
            item["itemData"]["productModel"] = d["productModel"]
        return {"error": 0, "data": {"thingList": [item]}}

    def ota_query(self) -> dict:
        # The conversion only reads this metadata; it delivers firmware over LAN.
        return {"error": 0, "data": {"otaInfoList": []}}

    def ws_reply(self, msg: dict) -> dict | None:
        """The IoT-channel answer to one device message, or None to stay quiet."""
        action = msg.get("action")
        base = {"error": 0}
        # Echo back the identifiers and whichever sequence id the device used to
        # correlate the reply: the S60 register uses `d_seq` (a number), other
        # messages may use `sequence`.
        for key in ("deviceid", "apikey", "sequence", "d_seq"):
            if key in msg:
                base[key] = msg[key]
        if action in ("register", "userOnline"):
            # The heartbeat config the device waits for before it is "online".
            base["config"] = {"hb": 1, "hbInterval": self.hb_interval}
            return base
        if action == "date":
            base["date"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            return base
        if action in ("update", "query", "upgrade", "sysmsg"):
            return base
        if action is None:
            return None  # our own keep-alive being acked
        return base  # anything else: a bare success


class Handler(socketserver.BaseRequestHandler):
    stub: Stub  # set on the server

    def handle(self) -> None:
        sock = self.request
        peer = "%s:%s" % self.client_address
        # The handshake is made here, in this connection's own thread, and not
        # while accepting: one client that stalls in it must not stop the rest.
        sock.settimeout(REQUEST_TIMEOUT)
        try:
            if isinstance(sock, ssl.SSLSocket):
                sock.do_handshake()
            header = self._read_headers(sock)
        except (ConnectionError, ssl.SSLError, OSError) as exc:
            log(peer, "connection ended before a request:", exc)
            return
        if header is None:
            return
        request_line, headers, body = header
        log(peer, "->", request_line)
        parts = request_line.split(" ")
        path = parts[1].split("?", 1)[0] if len(parts) > 1 else ""
        if request_line.startswith("GET /api/ws"):
            self._serve_ws(sock, headers, peer)
        elif path == "/dispatch/device":
            self._serve_dispatch(sock, body, peer)
        elif path == "/dispatch/app":
            self._serve_json(sock, self.stub.app_dispatch_response(), peer, "app dispatch")
        elif path == "/v2/user/login":
            self._serve_json(sock, self.stub.account_login(), peer, "account login")
        elif path == "/v2/device/thing":
            self._serve_json(sock, self.stub.device_thing(), peer, "device list")
        elif path == "/v2/device/ota/query":
            self._serve_json(sock, self.stub.ota_query(), peer, "OTA query")
        else:
            self._send_http(sock, 404, b"", "text/plain")

    def _read_headers(self, sock):
        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                return None
            buf += chunk
            if len(buf) > 64 * 1024:
                return None
        head, _, rest = bytes(buf).partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        request_line = lines[0].decode("latin1")
        headers = {}
        for line in lines[1:]:
            if b":" in line:
                name, value = line.split(b":", 1)
                headers[name.strip().lower().decode("latin1")] = value.strip().decode("latin1")
        body = bytearray(rest)
        want = int(headers.get("content-length", 0) or 0)
        while len(body) < want:
            chunk = sock.recv(min(4096, want - len(body)))
            if not chunk:
                break
            body += chunk
        return request_line, headers, bytes(body)

    def _send_http(self, sock, status: int, body: bytes, ctype: str) -> None:
        reason = {200: "OK", 404: "Not Found"}.get(status, "OK")
        head = (f"HTTP/1.1 {status} {reason}\r\n"
                f"Content-Type: {ctype}\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n").encode("latin1")
        sock.sendall(head + body)

    def _serve_json(self, sock, response: dict, peer: str, what: str) -> None:
        log(peer, f"{what} ->", json.dumps(redacted(response)))
        self._send_http(sock, 200, json.dumps(response).encode(), "application/json")

    def _serve_dispatch(self, sock, body: bytes, peer: str) -> None:
        try:
            request = json.loads(body) if body else {}
            log(peer, "dispatch request:", json.dumps(redacted(request)))
            if isinstance(request, dict):
                self.stub.heard(request)
        except ValueError:
            log(peer, "dispatch request (non-JSON):", body[:200])
        response = self.stub.dispatch_response()
        log(peer, "dispatch ->", json.dumps(response))
        self._send_http(sock, 200, json.dumps(response).encode(), "application/json")

    def _serve_ws(self, sock, headers, peer: str) -> None:
        key = headers.get("sec-websocket-key", "")
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        sock.sendall((
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("latin1"))
        conn = Conn(sock, peer)
        role: str | None = None   # "device" or "app", from the first message
        deviceid: str | None = None
        sock.settimeout(None)  # long-lived: the plug speaks every heartbeat interval
        log(peer, "websocket open")
        try:
            while True:
                try:
                    opcode, payload = read_frame(sock)
                except (ConnectionError, ssl.SSLError, OSError) as exc:
                    log(peer, "websocket closed:", exc)
                    return
                if opcode == 0x8:
                    log(peer, "websocket close frame")
                    return
                if opcode == 0x9:  # ping -> pong
                    conn.send(payload, opcode=0xA)
                    continue
                if opcode == 0xA:
                    continue
                try:
                    msg = json.loads(payload)
                except ValueError:
                    log(peer, "ws <- (non-JSON)", payload[:200])
                    continue
                log(peer, "ws <-", json.dumps(redacted(msg)))
                if role is None:
                    role = "device" if msg.get("userAgent") == "device" or msg.get("action") == "register" else "app"
                    log(peer, f"role: {role}")
                if role == "device":
                    deviceid = self._device_message(conn, msg, deviceid)
                else:
                    self._app_message(conn, msg)
        finally:
            if role == "app":
                _unsubscribe(conn)
            if role == "device" and deviceid:
                for app in _drop_device(deviceid, conn):  # tell watchers it went offline
                    try:
                        app.send(json.dumps({"action": "sysmsg", "deviceid": deviceid,
                                             "params": {"online": False}}).encode())
                    except OSError:
                        pass

    def _device_message(self, conn: "Conn", msg: dict, deviceid: str | None) -> str | None:
        if msg.get("action") == "register" and msg.get("deviceid"):
            deviceid = msg["deviceid"]
            _register_device(deviceid, conn)
        self.stub.heard(msg)
        reply = self.stub.ws_reply(msg)   # keep the device online (error:0 + config)
        if reply is not None:
            log(conn.peer, "ws ->", json.dumps(redacted(reply)))
            conn.send(json.dumps(reply).encode())
        # Pass the device's own reports (incl. its reply to a relayed command) to
        # any app watching it.
        if deviceid:
            for app in _subscribers_of(deviceid):
                try:
                    app.send(json.dumps(msg).encode())
                except OSError:
                    pass
        return deviceid

    def _app_message(self, conn: "Conn", msg: dict) -> None:
        target_id = msg.get("deviceid")
        if target_id and msg.get("action") not in (None, "userOnline"):
            # A command for a device: hand it to that device and watch for its
            # replies. The device's answer (relayed back) is the app's ack.
            device = _subscribe(target_id, conn)
            if device is not None:
                log(conn.peer, f"relay app->device ({msg.get('action')})")
                device.send(json.dumps(msg).encode())
            else:
                log(conn.peer, "relay: no such device online")
                conn.send(json.dumps({"error": 404, "deviceid": target_id,
                                      "sequence": msg.get("sequence")}).encode())
            return
        reply = self.stub.ws_reply(msg)   # userOnline handshake, keep-alives
        if reply is not None:
            log(conn.peer, "ws ->", json.dumps(redacted(reply)))
            conn.send(json.dumps(reply).encode())


class TLSServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler, context: ssl.SSLContext):
        super().__init__(address, handler)
        self.context = context

    def get_request(self):
        raw, addr = super().get_request()
        try:
            # No I/O here: the handshake happens in the connection's thread.
            return self.context.wrap_socket(raw, server_side=True,
                                            do_handshake_on_connect=False), addr
        except (ssl.SSLError, OSError):
            raw.close()
            raise


class PlainServer(socketserver.ThreadingTCPServer):
    """Plaintext variant for the emulated S61, which dials http://...:80."""
    allow_reuse_address = True
    daemon_threads = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--cert")
    parser.add_argument("--key")
    parser.add_argument("--plain", action="store_true",
                        help="serve plaintext HTTP/WS, no TLS (the emulated S61 dials "
                             "http://...:80 with the factory dispatch URL)")
    parser.add_argument("--iot-host", default="192.168.60.1",
                        help="IoT server handed back by dispatch: a numeric IP (the plug "
                             "does no DNS for it) that the cert's IP SAN covers")
    parser.add_argument("--iot-port", type=int, default=443)
    parser.add_argument("--hb-interval", type=int, default=145)
    parser.add_argument("--device-json", type=Path, default=None,
                        help="JSON of this plug's account-API view (deviceid, "
                             "devicekey, model, ...) for the tool-facing API")
    args = parser.parse_args(argv)

    device = json.loads(args.device_json.read_text()) if args.device_json else None
    Handler.stub = Stub(args.iot_host, args.iot_port, args.hb_interval, device)
    if args.plain:
        server = PlainServer((args.bind, args.port), Handler)
        scheme = "plaintext"
    else:
        if not args.cert or not args.key:
            parser.error("--cert and --key are required unless --plain is given")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.cert, args.key)
        server = TLSServer((args.bind, args.port), Handler, context)
        scheme = "TLS"
    log(f"stub listening on {args.bind}:{args.port} ({scheme}); "
        f"dispatch -> {args.iot_host}:{args.iot_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
