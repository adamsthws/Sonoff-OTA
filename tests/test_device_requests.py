import base64
import http.server
import json
import select
import socket
import threading
import time
import unittest
import urllib.error
import urllib.parse

from tools.autoflash import device
from tools.autoflash.device import Device, DeviceError


class BusyTasmota(http.server.ThreadingHTTPServer):
    """Answers as a Tasmota plug does - /slow only after `delay` seconds, as
    while Berry runs - and notes whether the client closed a request first."""
    daemon_threads = True

    def __init__(self, delay: float = 0.0):
        super().__init__(("127.0.0.1", 0), BusyTasmotaHandler)
        self.delay = delay
        self.slow_page = False
        self.closed_unanswered: list[bool] = []
        self.answered = threading.Event()


class BusyTasmotaHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status: int, body: bytes = b"ok", headers: dict[str, str] | None = None):
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        server: BusyTasmota = self.server
        if self.path.startswith("/slow") or (server.slow_page and self.path == "/"):
            time.sleep(server.delay)
            # A client that gave up and closed has sent its FIN by now.
            readable, _, _ = select.select([self.connection], [], [], 0)
            closed = bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
            server.closed_unanswered.append(closed)
            self.reply(200, b"late")
            server.answered.set()
        elif self.path.startswith("/missing"):
            self.reply(404, b"not found")
        elif self.path.startswith("/moved"):
            self.reply(302, b"", {"Location": "/cm?cmnd=Status"})
        elif self.path.startswith("/cm"):
            self.reply(200, b'{"Status":{}}')
        else:
            self.reply(200, b"<title>Tasmota</title>")


class HeldRequestTests(unittest.TestCase):
    def start(self, delay: float = 0.0) -> Device:
        server = BusyTasmota(delay)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        self.server = server
        return Device(f"127.0.0.1:{server.server_address[1]}")

    def test_a_request_the_plug_has_not_answered_is_held_open_not_closed(self):
        # Closing it is what leaves a busy plug's web server stuck for good.
        plug = self.start(delay=1.5)
        with self.assertRaises(TimeoutError):
            plug._get("/slow", {}, timeout=0.3)
        self.assertEqual(device.held_requests(), 1)
        self.assertTrue(self.server.answered.wait(5))
        time.sleep(0.2)
        self.assertEqual(self.server.closed_unanswered, [False])
        self.assertEqual(device.held_requests(), 0)

    def test_a_plug_with_requests_held_is_not_sent_more_than_a_few(self):
        plug = self.start(delay=1.5)
        for _ in range(device.MOST_HELD + 2):
            with self.assertRaises(TimeoutError):
                plug._get("/slow", {}, timeout=0.2)
        self.assertEqual(device.held_requests(plug.host), device.MOST_HELD)
        deadline = time.monotonic() + 10
        while device.held_requests() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(self.server.closed_unanswered, [False] * device.MOST_HELD)
        self.assertEqual(plug._get("/slow", {}, timeout=5), "late")  # answered: sent again

    def test_a_held_request_is_kept_alive_and_ends_when_the_plug_restarts(self):
        # The commit restarts the plug, cutting off the reply to the request that
        # started it: that hold must end, or it counts against the plug for ever.
        from unittest import mock
        import struct
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        accepted = []
        threading.Thread(target=lambda: accepted.append(listener.accept()[0]), daemon=True).start()
        plug = Device(f"127.0.0.1:{listener.getsockname()[1]}")
        with mock.patch.object(device, "_keep_alive", wraps=device._keep_alive) as kept:
            with self.assertRaises(TimeoutError):
                plug._get("/cm", {"cmnd": "Br autoflash_busy(600000)"}, timeout=0.3)
        kept.assert_called_once()
        self.assertTrue(kept.call_args.args[0].getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE))
        self.assertEqual(device.held_requests(plug.host), 1)
        # A restarted plug refuses the old connection: here, reset at once.
        connection = accepted[0]
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        connection.close()
        deadline = time.monotonic() + 5
        while device.held_requests(plug.host) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(device.held_requests(plug.host), 0)

    def test_the_web_page_check_still_says_no_at_once(self):
        plug = self.start(delay=1.5)
        self.server.slow_page = True
        started = time.monotonic()
        self.assertFalse(plug.answers(timeout=0.3))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(self.server.answered.wait(5))

    def test_errors_reach_the_caller_as_before(self):
        plug = self.start()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            plug._get("/missing", {}, timeout=5)
        self.assertEqual(caught.exception.code, 404)
        self.assertEqual(caught.exception.read(), b"not found")
        self.assertTrue(plug.answers(timeout=5))  # any answer is an answer
        self.assertEqual(plug.command("Status"), {"Status": {}})

    def test_a_redirect_is_followed(self):
        plug = self.start()
        self.assertEqual(plug._get("/moved", {}, timeout=5), '{"Status":{}}')

    def test_nobody_there_is_a_url_error_and_nothing_is_held(self):
        with socket.socket() as spare:
            spare.bind(("127.0.0.1", 0))
            port = spare.getsockname()[1]
        with self.assertRaises(urllib.error.URLError):
            Device(f"127.0.0.1:{port}")._get("/", {}, timeout=2)
        self.assertEqual(device.held_requests(), 0)

    def test_a_web_password_is_still_reported(self):
        class Refusing(BusyTasmotaHandler):
            def do_GET(self):
                self.reply(401, b"")
        server = BusyTasmota()
        server.RequestHandlerClass = Refusing
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        with self.assertRaisesRegex(DeviceError, "web UI has a password"):
            Device(f"127.0.0.1:{server.server_address[1]}")._get("/", {}, timeout=5)


if __name__ == "__main__":
    unittest.main()


# The page Tasmota 15.6.0 sent back for an image it would not take (the
# emulator, an upload too big for its slot), and the end of one it took.
REFUSED_PAGE = ("<div style='text-align:center;color:var(--c_ttl);'><h3>Sonoff S60TPG</h3><h2>Tasmota</h2></div>"
                "<div style='text-align:center;'><b>Upload <font color='#ff5661'>Failed</font></b><br><br>"
                "Not enough space</div><br><div></div><p></p><form id=but2 style=\"display:block;\" "
                "action='.' method='get'><button>Main Menu</button></form>")
TAKEN_PAGE = ("<div style='text-align:center;'><b>Upload <font color='#47c266'>Successful</font></b><br>"
              "<br>Device will restart in a few seconds</div>")


class UploadRefusalTests(unittest.TestCase):
    """A refused upload comes back as an ordinary page, which must be read: an
    image damaged on the way was once taken for a good one, and the run waited
    five minutes on a restart that never came."""

    def test_the_reason_is_read_off_the_page(self):
        self.assertEqual(device.upload_refusal(REFUSED_PAGE), "Not enough space")
        self.assertIsNone(device.upload_refusal(TAKEN_PAGE))
        self.assertIsNone(device.upload_refusal(""))

    def test_a_refused_upload_raises(self):
        from unittest import mock
        plug = Device("192.0.2.1")
        with mock.patch.object(plug, "_get"), \
             mock.patch.object(plug, "_request", return_value=REFUSED_PAGE.encode()):
            with self.assertRaises(device.UploadRefused) as raised:
                plug._upload("/up", "tasmota32c3.bin", b"\xe9" * 16, 10)
        self.assertEqual(raised.exception.reason, "Not enough space")
        self.assertIsInstance(raised.exception, DeviceError)

    def test_a_taken_upload_returns_its_page(self):
        from unittest import mock
        plug = Device("192.0.2.1")
        with mock.patch.object(plug, "_get"), \
             mock.patch.object(plug, "_request", return_value=TAKEN_PAGE.encode()):
            self.assertIn("Successful", plug._upload("/up", "tasmota32c3.bin", b"\xe9" * 16, 10))


class LockedTasmota(http.server.ThreadingHTTPServer):
    """Answers as Tasmota with a web password does (Tasmota 15.6.0's
    xdrv_01_9_webserver.ino): its command interface takes the password as
    HTTP Basic or in the query, and refuses without it in its own words;
    every other page takes it as HTTP Basic only.  Notes each request, and
    the Authorization it came with."""
    daemon_threads = True
    PASSWORD = "s3cret-71"
    STATUS = {"Status": {"Module": "Sonoff S60TPG"}, "StatusNET": {"Hostname": "tasmota-ABCDEF-1234"},
              "StatusFWR": {"Version": "15.6.0(release-tasmota32)"}}

    def __init__(self):
        super().__init__(("127.0.0.1", 0), LockedTasmotaHandler)
        self.seen: list[tuple[str, str | None]] = []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.server_address[1]}"

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


class LockedTasmotaHandler(BusyTasmotaHandler):
    def do_GET(self):
        server: LockedTasmota = self.server
        authorization = self.headers.get("Authorization")
        server.seen.append((self.path, authorization))
        path, _, query = self.path.partition("?")
        params = urllib.parse.parse_qs(query)
        basic = authorization == "Basic " + base64.b64encode(f"admin:{server.PASSWORD}".encode()).decode()
        if path == "/cm":
            commands = {"Status 0": server.STATUS, "Status 2": {"StatusFWR": server.STATUS["StatusFWR"]},
                        "Template": {"NAME": "Sonoff S60TPG"}}
            if not (basic or (params.get("user") == ["admin"] and params.get("password") == [server.PASSWORD])):
                self.reply(401, b'{"WARNING":"Need user=<username>&password=<password>"}')
            elif "cmnd" not in params:
                self.reply(200, b'{"WARNING":"Enter command cmnd="}')
            else:
                self.reply(200, json.dumps(commands.get(params["cmnd"][0], {"Command": "Unknown"})).encode())
        elif basic:
            self.reply(200, b"<title>Tasmota</title>")
        else:
            self.reply(401, b"401 Unauthorized", {"WWW-Authenticate": 'Basic realm="Login Required"'})


class WebPasswordTests(unittest.TestCase):
    """A plug on Tasmota with a web password: given it, every page is
    reached; without it, the plug says so, in words saying what to do."""

    def setUp(self):
        self.plug = LockedTasmota()
        self.addCleanup(self.plug.stop)

    def test_it_says_when_it_wants_one_and_runs_nothing_to_say_it(self):
        self.assertTrue(Device(self.plug.host).wants_password())
        self.assertTrue(Device(self.plug.host, password="not-it-9").wants_password())
        self.assertFalse(Device(self.plug.host, password=LockedTasmota.PASSWORD).wants_password())
        self.assertEqual({path.partition("?")[0] for path, _ in self.plug.seen}, {"/cm"})
        self.assertFalse([path for path, _ in self.plug.seen if "cmnd=" in path])

    def test_nobody_answering_is_neither(self):
        with socket.socket() as spare:
            spare.bind(("127.0.0.1", 0))
            port = spare.getsockname()[1]
        self.assertIsNone(Device(f"127.0.0.1:{port}").wants_password(timeout=2))

    def test_a_command_refused_for_it_says_how_to_give_it(self):
        with self.assertRaisesRegex(device.NeedsPassword, "not given: give it with --web-password"):
            Device(self.plug.host).command("Status 0")
        with self.assertRaisesRegex(device.NeedsPassword, "the one given is not it"):
            Device(self.plug.host, password="not-it-9").command("Status 0")

    def test_given_it_every_page_is_reached_its_query_holding_it_only_for_commands(self):
        plug = Device(self.plug.host, password=LockedTasmota.PASSWORD)  # no user: Tasmota's admin
        self.assertEqual(plug.command("Status 0"), LockedTasmota.STATUS)
        self.assertIn("Tasmota", plug._get("/", {}, 5))
        basic = "Basic " + base64.b64encode(f"admin:{LockedTasmota.PASSWORD}".encode()).decode()
        self.assertEqual({authorization for _, authorization in self.plug.seen}, {basic})
        commands = [path for path, _ in self.plug.seen if path.startswith("/cm?")]
        pages = [path for path, _ in self.plug.seen if not path.startswith("/cm?")]
        self.assertTrue(commands and all(f"password={LockedTasmota.PASSWORD}" in path for path in commands))
        self.assertTrue(pages and all(LockedTasmota.PASSWORD not in path for path in pages))
