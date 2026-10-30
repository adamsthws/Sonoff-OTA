"""A proxy in front of the emulated plug's web page - OpenBeken's or ESPHome's -
so a browser can use it (bk-emu.sh ui).

Why it is needed: the emulated plug runs the real firmware on lwIP, but under
Unicorn, far slower than a chip. OpenBeken answers every request with
`Connection: close`, so each one leaves a TCP connection in TIME_WAIT on the
plug, and lwIP has room for only a few; on the emulator's slow clock they take
a long while to clear. A browser opens several connections at once and the page
asks for its state every second, so within seconds every slot is taken and the
plug stops answering new connections - its firmware still running, but
unreachable until they clear. Opened straight, the page locks the plug up.

So the browser talks to this proxy, which keeps the plug almost idle:

  * one request at a time reaches the plug, a clean Connection: close one,
    and never sooner than MIN_GAP after the last, so its slots have time to clear;
  * reads are kept for CACHE_TTL seconds, so the page's polling, and a
    browser's repeated or parallel fetches, are answered here;
  * a request that looks like an action (a toggle, a command, a restart) is
    always passed through, never kept;
  * when the plug does not answer, the last good copy of a page is shown
    rather than an error;
  * the favicon is answered here, and on OpenBeken `/` too (its redirect to /index).

ESPHome's page keeps one connection open for its live values (/events, a
server-sent event stream): that one is passed straight through as it comes,
on a connection of its own, one at a time - a second tab is told to try
again, as the page does by itself. Everything else goes as above. Sonoff's
firmware has no page.

    python3 tools/beken/emulator/ui_proxy.py --device ADDRESS[:80] [--firmware esphome]
                                             [--listen 127.0.0.1:8088]
"""
from __future__ import annotations

import argparse
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEVICE_TIMEOUT = 25.0   # seconds: a slow answer from the plug still completes
RETRIES = 4             # a busy plug usually answers within a few tries
CACHE_TTL = 3.0         # seconds a read is answered from the cache
MIN_GAP = 0.8           # seconds between two connections to the plug
STALE_MAX = 120.0       # how old a page may be, shown when the plug does not answer

# The page polls index?state=1 every second: a read, so it is kept like any
# page. Queries that look like actions are passed through every time, so a
# click still reaches the plug.
ACTIONS = ("tgl", "toggle", "set", "pwm", "rgb", "cmd", "restart", "reboot",
           "clear", "ota", "flash", "save", "on=", "off=", "rel")
# Not passed on to the plug: each request to it is one of its own, closed.
HOP_BY_HOP = {"connection", "keep-alive", "proxy-connection", "accept-encoding", "te",
              "upgrade", "transfer-encoding", "host"}
# Not passed back: this proxy sets them.
OWN_HEADERS = {b"connection", b"keep-alive", b"transfer-encoding", b"content-length", b"server", b"date"}


def cacheable(method: str, path: str) -> bool:
    """Whether an answer may be kept: a GET that is not an action."""
    if method != "GET":
        return False
    query = path.split("?", 1)[1].lower() if "?" in path else ""
    return not any(action in query for action in ACTIONS)


def status_code(status_line: bytes) -> int:
    try:
        return int(status_line.split(b" ")[1])
    except (IndexError, ValueError):
        return 502


class UiProxy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, listen: tuple[str, int], device: tuple[str, int], *, firmware: str = "openbeken",
                 min_gap: float = MIN_GAP, cache_ttl: float = CACHE_TTL):
        super().__init__(listen, Handler)
        self.device, self.firmware = device, firmware
        self.stream_lock = threading.Lock()   # ESPHome's event stream: one at a time
        self.min_gap, self.cache_ttl = min_gap, cache_ttl
        self.device_lock = threading.Lock()   # one request at the plug at a time
        self.cache: dict[tuple[str, str], tuple[float, bytes, bytes, bytes]] = {}
        self.cache_lock = threading.Lock()
        self.last_connection = 0.0

    def cached(self, key: tuple[str, str], max_age: float) -> tuple[bytes, bytes, bytes] | None:
        with self.cache_lock:
            hit = self.cache.get(key)
        if hit and time.monotonic() - hit[0] <= max_age:
            return hit[1:]
        return None

    def keep(self, key: tuple[str, str], answer: tuple[bytes, bytes, bytes]) -> None:
        with self.cache_lock:
            self.cache[key] = (time.monotonic(), *answer)

    def ask_device(self, method: str, path: str, headers: list[tuple[str, str]],
                   body: bytes) -> tuple[bytes, bytes, bytes] | None:
        """One Connection: close request to the plug, under device_lock: its
        (status line, headers, body), or None if no whole answer came. The plug
        ends its answer by closing, so it is read to the end."""
        lines = [f"{method} {path} HTTP/1.1", "Host: %s:%d" % self.device, "Connection: close"]
        lines += [f"{name}: {value}" for name, value in headers if name.lower() not in HOP_BY_HOP]
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace") + body
        wait = self.min_gap - (time.monotonic() - self.last_connection)
        if wait > 0:
            time.sleep(wait)
        answer = bytearray()
        with socket.create_connection(self.device, timeout=DEVICE_TIMEOUT) as plug:
            self.last_connection = time.monotonic()
            plug.sendall(request)
            try:
                while chunk := plug.recv(65536):
                    answer += chunk
            except socket.timeout:
                pass
        head, found, rest = bytes(answer).partition(b"\r\n\r\n")
        if not found:
            return None
        status, _, header_lines = head.partition(b"\r\n")
        return status, header_lines, rest


class Handler(BaseHTTPRequestHandler):
    server: UiProxy
    protocol_version = "HTTP/1.1"
    server_version = "s61-ui-proxy"

    def log_message(self, *args) -> None:
        pass

    def answer(self, code: int, headers: bytes, body: bytes, how: str = "") -> None:
        self.send_response_only(code)
        for line in headers.split(b"\r\n"):
            name, colon, value = line.partition(b":")
            if colon and name.strip().lower() not in OWN_HEADERS:
                self.send_header(name.strip().decode("latin-1"), value.strip().decode("latin-1"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        if how:
            self.send_header("X-S61-Proxy", how)   # cache, live, stale or busy
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def handle_one(self, method: str) -> None:
        if self.path in ("/favicon.ico", "/favicon.png"):   # never worth a request to the plug
            self.answer(404, b"", b"")
            return
        if self.path == "/" and self.server.firmware == "openbeken":
            # OpenBeken's own / only redirects to /index, and is the least
            # reliable of its answers under the emulator.
            self.answer(302, b"Location: /index", b"")
            return
        if method == "GET" and self.path.split("?", 1)[0] == "/events":
            self.stream()
            return
        keepable = cacheable(method, self.path)
        key = (method, self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        result = None
        with self.server.device_lock:
            # Answered from the cache, perhaps filled while this one waited.
            fresh = self.server.cached(key, self.server.cache_ttl) if keepable else None
            if fresh:
                status, headers, page = fresh
                self.answer(status_code(status), headers, page, "cache")
                return
            for attempt in range(RETRIES):
                try:
                    result = self.server.ask_device(method, self.path, list(self.headers.items()), body)
                except OSError:
                    result = None
                if result is not None:
                    break
                time.sleep(0.5 + attempt)
        if result is None:
            stale = self.server.cached(key, STALE_MAX) if keepable else None
            if stale:
                status, headers, page = stale
                self.answer(status_code(status), headers, page, "stale")
                return
            self.answer(503, b"Content-Type: text/html",
                        b"<html><head><meta http-equiv='refresh' content='3'></head><body>"
                        b"The emulated plug is busy; trying again...</body></html>", "busy")
            return
        if keepable:
            self.server.keep(key, result)
        status, headers, page = result
        self.answer(status_code(status), headers, page, "live")

    def stream(self) -> None:
        """ESPHome's event stream, relayed as it comes until either end closes."""
        if not self.server.stream_lock.acquire(blocking=False):
            self.answer(503, b"Retry-After: 5", b"", "busy")
            return
        try:
            with self.server.device_lock:   # opened as any request is: paced
                wait = self.server.min_gap - (time.monotonic() - self.server.last_connection)
                if wait > 0:
                    time.sleep(wait)
                plug = socket.create_connection(self.server.device, timeout=DEVICE_TIMEOUT)
                self.server.last_connection = time.monotonic()
            with plug:
                plug.sendall(("GET %s HTTP/1.1\r\nHost: %s:%d\r\nAccept: text/event-stream\r\n\r\n"
                              % (self.path, *self.server.device)).encode("latin-1"))
                plug.settimeout(None)
                while chunk := plug.recv(4096):
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except OSError:
            pass   # the browser or the plug went: the page opens another
        finally:
            self.close_connection = True
            self.server.stream_lock.release()

    def do_GET(self) -> None:
        self.handle_one("GET")

    def do_HEAD(self) -> None:
        self.handle_one("HEAD")

    def do_POST(self) -> None:
        self.handle_one("POST")


def address(text: str, port: int) -> tuple[str, int]:
    host, _, given = text.rpartition(":") if ":" in text else (text, "", "")
    return host, int(given) if given else port


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--device", required=True, help="the plug's address, with :PORT if not 80")
    parser.add_argument("--firmware", choices=("openbeken", "esphome"), default="openbeken",
                        help="whose page it is")
    parser.add_argument("--listen", default="127.0.0.1:8088", help="where the browser opens it")
    args = parser.parse_args(argv)
    proxy = UiProxy(address(args.listen, 8088), address(args.device, 80), firmware=args.firmware)
    print("%s's page: http://%s:%d/ (the plug at %s:%d); Ctrl-C ends it."
          % ("ESPHome" if args.firmware == "esphome" else "OpenBeken", *proxy.server_address[:2],
             *proxy.device), flush=True)
    try:
        proxy.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
