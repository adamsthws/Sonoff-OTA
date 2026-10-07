#!/usr/bin/env python3
"""Watch a stock S60 for firmware-update activity, read-only, for hours at a time.

Two independent views, either of which can be turned off:

- network: tcpdump on the OPNsense router over SSH, filtered to the plug.  It
  logs every DNS lookup, every new connection, DHCP (a reboot or rejoin) and
  every plain-HTTP request.  Sonoff's firmware download is plain HTTP on port
  8088, so a genuine update cannot happen without appearing here.
- cloud: the owner's eWeLink account.  It holds the app WebSocket open and logs
  every message about the plug, and every --poll-minutes records the plug's
  firmware version, whether it is online, and what update Sonoff is offering.

Nothing is ever sent to the plug.  Everything is written under
captures/update-watch/<start time>/, which git ignores.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

try:
    from tools.autoflash.prompts import ask_secret
    from tools.get_device_key import API, APP_ID, EwelinkError, api_json, get_devices, login_session
    from tools.probe_ota_command import WS_DISPATCH, nonce
    from tools.query_ota import ota_identity, query_ota
    from tools.serve_tasmota_ota import S60_DEVICE_MODEL
    from tools.websocket_minimal import WebSocket, WebSocketError, encode_client_frame
except ModuleNotFoundError:  # Support direct execution as tools/watch_sonoff_updates.py.
    from autoflash.prompts import ask_secret
    from get_device_key import API, APP_ID, EwelinkError, api_json, get_devices, login_session
    from probe_ota_command import WS_DISPATCH, nonce
    from query_ota import ota_identity, query_ota
    from serve_tasmota_ota import S60_DEVICE_MODEL
    from websocket_minimal import WebSocket, WebSocketError, encode_client_frame


# Lookups, time sync and DHCP mark a boot or reconnect.  Every TCP packet is
# taken: SYNs show new connections, ports 80 and 8088 are plain HTTP and read
# whole, and the encrypted cloud connection is logged by timing and size.
CAPTURE_FILTER = "udp port 53 or udp port 123 or udp port 67 or udp port 68 or tcp"
# Cloud data arriving this long after the plug last sent on that connection was
# not a reply: the cloud spoke first.  Segments closer together than
# CONTINUATION are one message.
REPLY_WINDOW = 2.0
CONTINUATION = 1.0
FIRMWARE_PORT = 8088
# Fields in cloud messages that work as credentials; never written to disk.
SECRET_KEYS = {"apikey", "at", "rt", "devicekey", "password"}

HEADER = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+) "
    r"(?P<smac>[0-9a-f:]{17}) > (?P<dmac>[0-9a-f:]{17}), .*?length \d+: "
    r"(?P<src>\S+) > (?P<dst>\S+?):? (?P<rest>.*)$"
)
DNS_QUERY = re.compile(r"^(?P<id>\d+)\+?(?: \[\S+\])? (?:A|AAAA)\? (?P<name>\S+?)\.? \(")
DNS_REPLY = re.compile(r"^(?P<id>\d+)\*?-? .*?\d+/\d+/\d+ ")
DNS_ADDRESS = re.compile(r"\bA (\d+\.\d+\.\d+\.\d+)")
HTTP_REQUEST = re.compile(r"(GET|POST|HEAD|PUT) (\S+) HTTP/1\.[01]")
TCP_PACKET = re.compile(r"^Flags \[(?P<flags>[^\]]*)\].*?, length (?P<length>\d+)")


def split_endpoint(text: str) -> tuple[str, int | None]:
    """'192.0.2.1.443' -> ('192.0.2.1', 443).  tcpdump joins port with a dot."""
    address, _, port = text.rstrip(":").rpartition(".")
    if port.isdigit() and address:
        return address, int(port)
    return text.rstrip(":"), None


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: "<redacted>" if k in SECRET_KEYS else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class Recorder:
    """Thread-safe event log: JSON lines on disk, a short line on screen."""

    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        self.directory = directory
        self.lock = threading.Lock()
        self.counts: collections.Counter[str] = collections.Counter()
        self.notable: list[str] = []
        self.events = self._open("events.jsonl")
        self.raw = self._open("tcpdump.txt")

    def _open(self, name: str):
        path = self.directory / name
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(descriptor, "a", encoding="utf-8", buffering=1)

    def event(self, kind: str, screen: str | None = None, *, notable: bool = False, **fields: Any) -> None:
        now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        with self.lock:
            self.counts[kind] += 1
            self.events.write(json.dumps({"time": now, "kind": kind, **fields}, sort_keys=True) + "\n")
            if screen:
                line = f"{now[11:19]}  {'!! ' if notable else ''}{screen}"
                print(line, flush=True)
                if notable:
                    self.notable.append(f"{now}  {screen}")

    def raw_line(self, line: str) -> None:
        with self.lock:
            self.raw.write(line + "\n")

    def close(self) -> None:
        self.events.close()
        self.raw.close()


# ------------------------------------------------------------------ network


class PacketParser:
    """Turns tcpdump -e -nn -tttt -A text into events.  Pure, so it is testable."""

    def __init__(self, plug_ip: str | None, plug_mac: str | None):
        self.plug_ip = plug_ip
        self.plug_mac = plug_mac.lower() if plug_mac else None
        self.pending_dns: dict[tuple[int | None, str], str] = {}
        self.names: dict[str, str] = {}      # address -> name the plug looked up
        self.http_port: int | None = None     # payload lines belong to port 80/8088
        # Per connection: when the plug last sent data, when the cloud last did.
        self.last_out: dict[tuple[str, int | None, int | None], float] = {}
        self.last_in: dict[tuple[str, int | None, int | None], float] = {}

    def from_plug(self, smac: str, src: str) -> bool:
        if self.plug_mac:
            return smac == self.plug_mac
        return src == self.plug_ip

    def feed(self, line: str) -> list[dict[str, Any]]:
        match = HEADER.match(line)
        if not match:
            if self.http_port:
                request = HTTP_REQUEST.search(line)
                if request:
                    return [{"kind": "http_request", "method": request.group(1),
                             "path": request.group(2), "port": self.http_port}]
            return []
        src, sport = split_endpoint(match["src"])
        dst, dport = split_endpoint(match["dst"])
        outbound = self.from_plug(match["smac"], src)
        rest = match["rest"]
        self.http_port = next((p for p in (dport, sport) if p in (80, FIRMWARE_PORT)), None)

        if 53 in (sport, dport):
            if outbound:
                query = DNS_QUERY.match(rest)
                if query:
                    self.pending_dns[(sport, query["id"])] = query["name"]
                    return [{"kind": "dns_query", "name": query["name"], "server": dst}]
                return []
            reply = DNS_REPLY.match(rest)
            name = self.pending_dns.pop((dport, reply["id"]), None) if reply else None
            if name:
                addresses = DNS_ADDRESS.findall(rest)
                for address in addresses:
                    self.names[address] = name
                return [{"kind": "dns_answer", "name": name, "addresses": addresses}]
            return []
        if {sport, dport} & {67, 68}:
            return [{"kind": "dhcp"}] if outbound else []
        if 123 in (sport, dport):
            return [{"kind": "ntp", "server": dst}] if outbound else []
        if outbound and "Flags [S]," in rest:
            return [{"kind": "connect", "address": dst, "port": dport, "name": self.names.get(dst)}]
        packet = TCP_PACKET.match(rest)
        if packet and int(packet["length"]) and self.http_port is None:
            return self.data(match["ts"], outbound, src, sport, dst, dport, int(packet["length"]))
        return []

    def data(self, ts: str, outbound: bool, src: str, sport: int | None, dst: str, dport: int | None,
             length: int) -> list[dict[str, Any]]:
        """One encrypted segment: who sent it, how big, and whether the cloud spoke first."""
        at = datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S.%f").timestamp()
        remote, rport, lport = (dst, dport, sport) if outbound else (src, sport, dport)
        key = (remote, rport, lport)
        event = {"kind": "tcp_data", "at": at, "direction": "out" if outbound else "in",
                 "address": remote, "port": rport, "name": self.names.get(remote), "bytes": length}
        if outbound:
            self.last_out[key] = at
            return [event]
        previous_in, self.last_in[key] = self.last_in.get(key), at
        if previous_in is not None and at - previous_in < CONTINUATION:
            return [event]  # the rest of a message already counted
        if at - self.last_out.get(key, float("-inf")) <= REPLY_WINDOW:
            return [event]  # an answer to the plug
        return [event, dict(event, kind="cloud_push")]


def describe(event: dict[str, Any]) -> tuple[str | None, bool]:
    """Screen text for a network event, and whether it matters for updates."""
    kind = event["kind"]
    if kind == "connect":
        where = f"{event['name'] or event['address']}:{event['port']}"
        if event["port"] == FIRMWARE_PORT:
            return f"plug opened a connection to the firmware port: {where}", True
        return f"plug connected to {where}", False
    if kind == "http_request":
        path = event["path"].split("?")[0]  # the query holds the device id and signature
        short = path if len(path) <= 70 else path[:67] + "..."
        if "/ota/" in path or event.get("port") == FIRMWARE_PORT:
            return f"FIRMWARE DOWNLOAD: {event['method']} {short}", True
        return f"plain HTTP: {event['method']} {short}", True
    if kind == "dns_query":
        return f"plug looked up {event['name']}", False
    if kind == "dhcp":
        return "plug used DHCP (booted or rejoined Wi-Fi)", False
    if kind == "cloud_push":
        where = f"{event['name'] or event['address']}:{event['port']}"
        return f"cloud sent the plug {event['bytes']} bytes unprompted ({where})", False
    return None, False


def remote_capture(args: argparse.Namespace, seconds: int) -> str:
    """The command run on the router.

    -p keeps the interface out of promiscuous mode: the plug's traffic is sent
    to the router anyway, so nothing about the interface changes.  timeout ends
    the capture by itself if this computer vanishes without closing the session.
    Nothing is written on the router; the output streams back here.
    """
    selector = f"ether host {args.plug_mac}" if args.plug_mac else f"host {args.plug_ip}"
    return (
        f"timeout {seconds} tcpdump -p -l -e -nn -tttt -A -s 0 -i {shlex.quote(args.interface)} "
        + shlex.quote(f"{selector} and ({CAPTURE_FILTER})")
    )


def end_process(process: subprocess.Popen) -> None:
    """Stop ssh, and with it the capture on the router; never leave it running."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def watch_network(args: argparse.Namespace, recorder: Recorder, stop: threading.Event,
                  deadline: float, children: list[subprocess.Popen]) -> None:
    parser = PacketParser(args.plug_ip, args.plug_mac)
    while not stop.is_set():
        # Each capture may outlive the watch by two minutes at most.
        seconds = max(60, int(deadline - time.monotonic()) + 120)
        command = ["ssh", "-tt", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30",
                   "-o", "ServerAliveCountMax=4", args.opnsense, remote_capture(args, seconds)]
        recorder.event("network_start", f"capturing on {args.opnsense} {args.interface}", command=command[-1])
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, text=True, errors="replace")
        children.append(process)
        threading.Thread(target=lambda p=process: (stop.wait(), end_process(p)), daemon=True).start()
        try:
            assert process.stdout is not None
            for raw in process.stdout:
                line = raw.rstrip("\r\n")
                recorder.raw_line(line)
                for event in parser.feed(line):
                    screen, notable = describe(event)
                    recorder.event(event.pop("kind"), screen, notable=notable, **event)
        finally:
            end_process(process)
        # Ctrl-C reaches ssh and this script together; give the stop a moment
        # to arrive before treating the end as a lost connection.
        if stop.wait(2):
            break
        recorder.event("network_lost", f"capture ended (exit {process.returncode}); retrying in 30s",
                       notable=True, code=process.returncode)
        stop.wait(30)


# -------------------------------------------------------------------- cloud


def offers(response: dict[str, Any]) -> list[dict[str, Any]]:
    """What Sonoff is offering, without the download links (they act as tokens)."""
    return [
        {"version": ota.get("version"), "files": len(ota.get("binList", []))}
        for ota in response.get("data", {}).get("otaInfoList", [])
    ]


def find_plug(devices: list[dict[str, Any]], device_id: str | None) -> dict[str, Any]:
    if device_id:
        matches = [d for d in devices if d.get("deviceid") == device_id]
    else:
        matches = []
        for device in devices:
            try:
                if ota_identity(device)["model"] == S60_DEVICE_MODEL:
                    matches.append(device)
            except EwelinkError:
                continue
    if len(matches) != 1:
        listed = ", ".join(f"{d.get('name')} ({d.get('deviceid')})" for d in devices) or "none"
        raise EwelinkError(
            f"expected one S60 on the account, found {len(matches)}; pass --device-id. Devices: {listed}"
        )
    return matches[0]


class CloudWatch:
    def __init__(self, args: argparse.Namespace, password: str, recorder: Recorder, stop: threading.Event):
        self.args = args
        self.password = password
        self.recorder = recorder
        self.stop = stop
        self.region = args.region
        self.session: dict[str, Any] | None = None
        self.device_id: str | None = args.device_id
        self.last_state: dict[str, Any] | None = None
        self.last_offer: list[dict[str, Any]] | None = None

    def login(self) -> None:
        self.region, self.session = login_session(self.args.email, self.password, self.args.country_code,
                                                  self.args.region)

    def poll(self) -> None:
        assert self.session is not None
        device = find_plug(get_devices(self.region, self.session["at"]), self.device_id)
        self.device_id = device["deviceid"]
        state = {"online": bool(device.get("online")), "version": device.get("params", {}).get("fwVersion")}
        offer = offers(query_ota(self.region, self.session["at"], ota_identity(device)))
        self.recorder.event("cloud_poll", None, **state, offer=offer)
        if state != self.last_state:
            changed = self.last_state is not None and state["version"] != self.last_state["version"]
            self.recorder.event(
                "cloud_state",
                f"cloud: plug {'online' if state['online'] else 'OFFLINE'}, firmware {state['version']}",
                notable=changed, **state,
            )
            self.last_state = state
        if offer != self.last_offer:
            text = ", ".join(o["version"] or "?" for o in offer) or "nothing"
            self.recorder.event("ota_offer", f"cloud: Sonoff is offering {text}",
                                notable=self.last_offer is not None, offer=offer)
            self.last_offer = offer

    def connect(self) -> tuple[WebSocket, float]:
        assert self.session is not None
        dispatch = api_json(WS_DISPATCH[self.region], headers={"Authorization": "Bearer " + self.session["at"]})
        ws = WebSocket.connect(dispatch["domain"], int(dispatch["port"]))
        now = time.time()
        ws.send_json({
            "action": "userOnline", "at": self.session["at"], "apikey": self.session["user"]["apikey"],
            "appid": APP_ID, "nonce": nonce(), "ts": int(now), "userAgent": "app",
            "sequence": str(int(now * 1000)), "version": 8,
        })
        handshake = ws.receive_json(timeout=10)
        if handshake.get("error", 0) != 0:
            raise EwelinkError("cloud WebSocket authentication failed")
        interval = float(handshake.get("config", {}).get("hbInterval", 90))
        self.recorder.event("cloud_ws", "cloud: listening for messages about the plug")
        return ws, max(15.0, interval * 0.8)

    def handle(self, message: dict[str, Any]) -> None:
        if message.get("deviceid") not in (None, self.device_id):
            return
        action = message.get("action") or "reply"
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        keys = ", ".join(sorted(params)) or "-"
        upgrade = action == "upgrade" or any("ota" in k.lower() or "upgrade" in k.lower() for k in params)
        self.recorder.event("cloud_message", f"cloud message: {action} [{keys}]", notable=upgrade,
                            message=redact(message))

    def run(self) -> None:
        ws: WebSocket | None = None
        next_poll = next_ping = next_connect = 0.0
        ping_every = 60.0
        while not self.stop.is_set():
            now = time.monotonic()
            try:
                if self.session is None:
                    self.login()
                if now >= next_poll:
                    next_poll = now + self.args.poll_minutes * 60
                    self.poll()
                if ws is None and now >= next_connect:
                    ws, ping_every = self.connect()
                    next_ping = now + ping_every
                if ws is None:
                    self.stop.wait(1)
                    continue
                if now >= next_ping:
                    ws.sock.sendall(encode_client_frame(b"ping"))
                    next_ping = now + ping_every
                try:
                    self.handle(ws.receive_json(timeout=1))
                except socket.timeout:
                    pass
                except json.JSONDecodeError:
                    pass  # "pong"
            except (EwelinkError, WebSocketError, OSError, KeyError, ValueError) as exc:
                self.recorder.event("cloud_error", f"cloud: {exc}; retrying in 60s", error=str(exc))
                if ws is not None:
                    ws.close()
                ws = None
                if isinstance(exc, EwelinkError):
                    self.session = None  # the token may have expired
                next_connect = time.monotonic() + 60
                next_poll = min(next_poll, next_connect)
                self.stop.wait(60)
        if ws is not None:
            ws.close()


# ------------------------------------------------------------------ summary


def summarise(recorder: Recorder, started: float) -> str:
    connects: collections.Counter[str] = collections.Counter()
    lookups: collections.Counter[str] = collections.Counter()
    versions: list[str] = []
    offered: list[str] = []
    actions: collections.Counter[str] = collections.Counter()
    downloads = 0
    sent: dict[str, list[float]] = collections.defaultdict(list)       # plug -> cloud, per endpoint
    received: dict[str, list[float]] = collections.defaultdict(list)   # cloud -> plug
    pushes: list[dict[str, Any]] = []
    with open(recorder.directory / "events.jsonl", encoding="utf-8") as stream:
        for line in stream:
            event = json.loads(line)
            kind = event["kind"]
            if kind == "connect":
                connects[f"{event.get('name') or event['address']}:{event['port']}"] += 1
            elif kind == "dns_query":
                lookups[event["name"]] += 1
            elif kind == "http_request" and "/ota/" in event.get("path", ""):
                downloads += 1
            elif kind == "cloud_state" and event.get("version") not in versions:
                versions.append(event.get("version"))
            elif kind == "ota_offer":
                offered.append(", ".join(o["version"] or "?" for o in event["offer"]) or "nothing")
            elif kind == "tcp_data":
                where = f"{event.get('name') or event['address']}:{event['port']}"
                (sent if event["direction"] == "out" else received)[where].append(event["at"])
            elif kind == "cloud_push":
                pushes.append(event)
            elif kind == "cloud_message":
                actions[event["message"].get("action") or "reply"] += 1
    hours = (time.monotonic() - started) / 3600
    lines = [f"Watched for {hours:.1f} hours. Logs: {recorder.directory}", ""]
    lines.append(f"Firmware downloads seen: {downloads}"
                 + ("" if downloads else " - the plug never fetched firmware"))
    lines.append(f"Firmware versions reported: {', '.join(map(str, versions)) or 'none (no cloud view)'}")
    lines.append(f"Update offers, in order: {' -> '.join(offered) or 'none recorded'}")
    lines.append(f"Boots/rejoins (DHCP packets): {recorder.counts['dhcp']}")
    lines.append("")
    lines.append("DNS lookups:")
    lines += [f"  {count:5}  {name}" for name, count in lookups.most_common()] or ["  none"]
    lines.append("Connections opened:")
    lines += [f"  {count:5}  {where}" for where, count in connects.most_common()] or ["  none"]
    lines += cloud_connection(sent, received, pushes)
    lines.append("Cloud messages about the plug:")
    lines += [f"  {count:5}  {action}" for action, count in actions.most_common()] or ["  none"]
    lines.append("Things worth a look:")
    lines += [f"  {note}" for note in recorder.notable] or ["  none"]
    return "\n".join(lines)


def minutes(seconds: float) -> str:
    return f"{seconds / 60:.1f} min" if seconds >= 60 else f"{seconds:.0f} s"


def cloud_connection(sent: dict[str, list[float]], received: dict[str, list[float]],
                     pushes: list[dict[str, Any]]) -> list[str]:
    """How often the plug checks in, and how often the cloud speaks first."""
    lines = ["Encrypted connections (timing and size only):"]
    if not sent and not pushes:
        return lines + ["  none seen"]
    for where, times in sorted(sent.items(), key=lambda item: -len(item[1])):
        # A check-in is the plug speaking first: sending after five quiet
        # seconds, and not answering something the cloud just sent.
        heard = received.get(where, [])
        starts = [t for i, t in enumerate(times)
                  if (i == 0 or t - times[i - 1] > 5)
                  and not any(0 <= t - r <= REPLY_WINDOW for r in heard)]
        gaps = sorted(b - a for a, b in zip(starts, starts[1:]))
        if gaps:
            typical = gaps[len(gaps) // 2]
            lines.append(f"  {where}: plug checked in {len(starts)} times, typically every "
                         f"{minutes(typical)} (range {minutes(gaps[0])} - {minutes(gaps[-1])})")
        else:
            lines.append(f"  {where}: plug sent data {len(starts)} time(s)")
    lines.append(f"  Cloud spoke first: {len(pushes)} time(s)")
    sizes = collections.Counter(p["bytes"] for p in pushes)
    lines += [f"    {count:5} x {size} bytes" for size, count in sorted(sizes.items())]
    if 0 < len(pushes) <= 20:
        lines += [f"    at {datetime.datetime.fromtimestamp(p['at']).strftime('%H:%M:%S')}, "
                  f"{p['bytes']} bytes" for p in pushes]
    return lines


def main(argv: list[str] | None = None, *, secret: Callable[[str], str] = ask_secret) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--plug-ip", help="the plug's LAN address")
    parser.add_argument("--plug-mac", help="filter on the plug's MAC instead; survives an address change")
    parser.add_argument("--opnsense", help="SSH target for the router, e.g. root@192.168.1.1")
    parser.add_argument("--interface", help="the router's LAN interface, e.g. igc1")
    parser.add_argument("--no-cloud", action="store_true", help="network view only")
    parser.add_argument("--email", help="eWeLink account; prompted when omitted")
    parser.add_argument("--country-code", default="+44")
    parser.add_argument("--region", choices=sorted(API), default="eu")
    parser.add_argument("--device-id", help="needed only if the account has more than one S60")
    parser.add_argument("--poll-minutes", type=float, default=15.0)
    args = parser.parse_args(argv)

    if args.opnsense and not args.interface:
        parser.error("--opnsense needs --interface")
    if args.opnsense and not (args.plug_ip or args.plug_mac):
        parser.error("--opnsense needs --plug-ip or --plug-mac")
    if not args.opnsense and args.no_cloud:
        parser.error("nothing to watch: give --opnsense, or drop --no-cloud")

    password = ""
    if not args.no_cloud:
        args.email = args.email or input("eWeLink email: ").strip()
        password = os.environ.get("EWELINK_PASSWORD") or secret("eWeLink password (kept in memory only): ")

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    recorder = Recorder(Path("captures/update-watch") / stamp)
    stop = threading.Event()
    children: list[subprocess.Popen] = []
    started = time.monotonic()
    deadline = started + args.hours * 3600
    workers = []
    if args.opnsense:
        workers.append(threading.Thread(target=watch_network, daemon=True,
                                        args=(args, recorder, stop, deadline, children)))
    if not args.no_cloud:
        workers.append(threading.Thread(target=CloudWatch(args, password, recorder, stop).run, daemon=True))

    def interrupted(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt
    # Closing the terminal or a plain `kill` stops it the same way as Ctrl-C.
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), interrupted)

    print(f"Watching for {args.hours:g} hours; Ctrl-C stops early. Nothing is sent to the plug.")
    try:
        for worker in workers:
            worker.start()
        stop.wait(max(0.0, deadline - time.monotonic()))
    except KeyboardInterrupt:
        print("\nStopping early.")
    finally:
        # A second Ctrl-C must not cut the clean-up short.
        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), signal.SIG_IGN)
        stop.set()
        for worker in workers:
            worker.join(timeout=15)
        for process in children:
            end_process(process)
        leftover = [p.pid for p in children if p.poll() is None]
        recorder.event("stop", None, leftover_ssh=leftover)
        report = summarise(recorder, started)
        report += "\n\nRouter: " + ("capture stopped; nothing was changed or left running there."
                                     if not leftover else f"ssh {leftover} did not stop - check the router.")
        with recorder._open("summary.txt") as stream:
            stream.write(report + "\n")
        recorder.close()
        print("\n" + report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
