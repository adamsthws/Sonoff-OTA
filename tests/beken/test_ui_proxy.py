"""The proxy in front of the emulated plug's OpenBeken page keeps the plug
almost idle: one request at a time, reads kept, actions passed through."""

import http.client
import socket
import threading
import time
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tools.beken.emulator import ui_proxy


class FakePlug(ThreadingHTTPServer):
    """OpenBeken as the proxy sees it: every answer closes its connection."""
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakePlugHandler)
        self.requests: list[str] = []
        self.open = 0
        self.most_open = 0
        self.lock = threading.Lock()
        self.release = threading.Event()   # ends ESPHome's event stream


class FakePlugHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/events":
            # ESPHome's live values: a stream that stays open.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'event: ping\ndata: {"title":"Sonoff S61 abcdef"}\n\n')
            self.wfile.flush()
            self.server.release.wait(10)
            return
        with self.server.lock:
            self.server.requests.append(self.path)
            self.server.open += 1
            self.server.most_open = max(self.server.most_open, self.server.open)
        time.sleep(0.05)   # the emulated plug is slow
        body = f"page {self.path}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        with self.server.lock:
            self.server.open -= 1


class UiProxyTests(unittest.TestCase):
    def setUp(self):
        self.plug = FakePlug()
        threading.Thread(target=self.plug.serve_forever, daemon=True).start()
        self.addCleanup(self.plug.server_close)
        self.addCleanup(self.plug.shutdown)
        self.proxy = ui_proxy.UiProxy(("127.0.0.1", 0), self.plug.server_address[:2], min_gap=0.0)
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        self.addCleanup(self.proxy.server_close)
        self.addCleanup(self.proxy.shutdown)

    def get(self, path):
        connection = http.client.HTTPConnection(*self.proxy.server_address[:2], timeout=10)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.getheader("X-S61-Proxy"), response.read()
        finally:
            connection.close()

    def test_a_browsers_parallel_requests_reach_the_plug_one_at_a_time(self):
        threads = [threading.Thread(target=self.get, args=(f"/page{n}",)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.plug.requests), 8)
        self.assertEqual(self.plug.most_open, 1)

    def test_the_pages_polling_is_answered_from_the_cache(self):
        for _ in range(5):
            status, how, body = self.get("/index?state=1")
            self.assertEqual((status, body), (200, b"page /index?state=1"))
        self.assertEqual(self.plug.requests, ["/index?state=1"])
        self.assertEqual(how, "cache")

    def test_an_action_always_reaches_the_plug(self):
        for _ in range(3):
            self.assertEqual(self.get("/index?tgl=1")[1], "live")
        self.assertEqual(self.plug.requests, ["/index?tgl=1"] * 3)

    def test_the_last_good_page_is_shown_when_the_plug_does_not_answer(self):
        self.get("/index")
        self.proxy.cache_ttl = 0.0
        # Nothing listening there now: as a plug whose connections are all taken.
        with socket.socket() as spare:
            spare.bind(("127.0.0.1", 0))
            self.proxy.device = spare.getsockname()
        with unittest.mock.patch.object(ui_proxy, "RETRIES", 1), unittest.mock.patch.object(ui_proxy.time, "sleep"):
            status, how, body = self.get("/index")
        self.assertEqual((status, how, body), (200, "stale", b"page /index"))

    def test_the_root_is_answered_here(self):
        status, _, _ = self.get("/")
        self.assertEqual(status, 302)
        self.assertEqual(self.plug.requests, [])


class EsphomeTests(unittest.TestCase):
    """ESPHome's page: its event stream passed straight through, one at a time."""

    def setUp(self):
        self.plug = FakePlug()
        threading.Thread(target=self.plug.serve_forever, daemon=True).start()
        self.addCleanup(self.plug.server_close)
        self.addCleanup(self.plug.shutdown)
        self.addCleanup(self.plug.release.set)
        self.proxy = ui_proxy.UiProxy(("127.0.0.1", 0), self.plug.server_address[:2], firmware="esphome",
                                      min_gap=0.0)
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        self.addCleanup(self.proxy.server_close)
        self.addCleanup(self.proxy.shutdown)

    def open_events(self):
        stream = socket.create_connection(self.proxy.server_address[:2], timeout=5)
        stream.sendall(b"GET /events HTTP/1.1\r\nHost: x\r\nAccept: text/event-stream\r\n\r\n")
        return stream

    def get(self, path):
        connection = http.client.HTTPConnection(*self.proxy.server_address[:2], timeout=10)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_the_stream_comes_through_while_it_stays_open(self):
        stream = self.open_events()
        got = b""
        while b"title" not in got:
            got += stream.recv(4096)
        self.assertIn(b"text/event-stream", got)
        self.assertIn(b'"title":"Sonoff S61 abcdef"', got)
        # The page's other requests are still answered meanwhile.
        self.assertEqual(self.get("/switch/Relay"), (200, b"page /switch/Relay"))
        stream.close()

    def test_a_second_stream_is_told_to_try_again(self):
        first = self.open_events()
        first.recv(4096)
        self.assertEqual(self.get("/events")[0], 503)
        first.close()

    def test_esphomes_root_is_its_page_not_a_redirect(self):
        self.assertEqual(self.get("/"), (200, b"page /"))


if __name__ == "__main__":
    unittest.main()
