import http.server
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

from tools.autoflash import setup_network


class FakePlug(http.server.ThreadingHTTPServer):
    """Answers like the bridge or like Tasmota's setup mode, as configured."""

    daemon_threads = True

    def __init__(self, behaviour: str):
        super().__init__(("127.0.0.1", 0), FakePlugHandler)
        self.behaviour = behaviour
        self.uploaded = b""
        self.wifi: dict[str, str] = {}
        self.page_views = 0  # each one is a Wi-Fi scan on a real plug
        self.settings: tuple[dict, bytes] | None = None

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.server_address[1]}"


class FakePlugHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, body: str, status: int = 200) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        plug: FakePlug = self.server
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/":
            self.reply("<title>S60 OTA Bridge</title>" if plug.behaviour == "bridge"
                       else "<title>Tasmota</title>")
            return
        if url.path == "/wi":
            query = urllib.parse.parse_qs(url.query, keep_blank_values=True)
            if "save" in query:
                plug.wifi = {"ssid": query["s1"][0], "password": query["p1"][0]}
                self.reply("<h3>Trying to connect device to network</h3>")
                return
            plug.page_views += 1
            if plug.page_views == 1 or plug.behaviour == "tasmota-silent":
                self.reply("<h3>Trying to connect device to network<br>Home</h3>")
            elif plug.behaviour == "tasmota-rejects":
                self.reply("<h3>Connect failed to Home<br>Please, check your credentials</h3>")
            else:
                self.reply("Successful WiFi Connection<br>Redirecting to new device's IP "
                           "address<br><a href='http://192.168.1.77'>192.168.1.77</a>")
            return
        self.reply("not found", 404)

    def do_POST(self):
        plug: FakePlug = self.server
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/tasmota-settings":
            if plug.behaviour == "bridge-v4":
                self.reply("Nothing matches the given URI", 404)
                return
            body = self.rfile.read(int(self.headers["Content-Length"]))
            plug.settings = (dict(urllib.parse.parse_qsl(url.query)), body)
            if plug.behaviour == "bridge-not-joined":
                self.reply("The bridge has not joined a network. Nothing was saved.", 409)
            else:
                self.reply("Tasmota settings saved with the home network.")
            return
        if self.headers.get("Expect"):
            self.reply("Expect is not supported", 417)
            return
        length = int(self.headers["Content-Length"])
        plug.uploaded = self.rfile.read(length)
        self.reply("Upload verified. Rebooting into the new application...\n")


class SetupNetworkTests(unittest.TestCase):
    def start(self, behaviour: str) -> FakePlug:
        plug = FakePlug(behaviour)
        thread = threading.Thread(target=plug.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (plug.shutdown(), plug.server_close()))
        return plug

    def test_joining_is_noticed_by_the_page_marker(self):
        plug = self.start("bridge")
        self.assertTrue(setup_network.wait_for_page(
            setup_network.BRIDGE_PAGE_MARKER, host=plug.host, timeout=5, poll=0.1))
        self.assertFalse(setup_network.wait_for_page(
            setup_network.BRIDGE_PAGE_MARKER, host="127.0.0.1:9", timeout=0.3, poll=0.1))

    def test_the_bridge_receives_the_exact_image(self):
        plug = self.start("bridge")
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "tasmota.bin"
            image.write_bytes(bytes(range(256)) * 300)
            reply = setup_network.upload_to_bridge(image, host=plug.host, timeout=10)
        self.assertIn("Upload verified", reply)
        self.assertEqual(plug.uploaded, bytes(range(256)) * 300)

    def test_an_upload_the_bridge_stops_taking_is_given_up(self):
        # The network gone part way: noticed after the stall limit, not left
        # hanging, so the run can try again (through the bridge's own network).
        import socket
        import threading
        import time
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)
        held = []
        threading.Thread(target=lambda: held.append(server.accept()), daemon=True).start()
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "tasmota.bin"
            image.write_bytes(b"\xe9" * 8_000_000)
            started = time.monotonic()
            with self.assertRaisesRegex(setup_network.SetupError, "did not complete"):
                setup_network.upload_to_bridge(image, host=f"127.0.0.1:{server.getsockname()[1]}",
                                               timeout=0.5)
        self.assertLess(time.monotonic() - started, 10)
        for connection, _ in held:
            connection.close()
        self.assertLessEqual(setup_network.UPLOAD_STALL, 120)

    def test_wifi_details_are_sent_and_the_new_address_is_read(self):
        plug = self.start("tasmota")
        outcome = setup_network.send_wifi(
            "Home & Garden", "p@ss word", host=plug.host, settle=0.05, poll=0.05)
        self.assertEqual(outcome, ("saved", "192.168.1.77"))
        self.assertEqual(plug.wifi, {"ssid": "Home & Garden", "password": "p@ss word"})
        # A look while still testing, then the verdict - no rapid polling,
        # which on a real plug scans and knocks the computer off its network.
        self.assertEqual(plug.page_views, 2)

    def test_a_failed_connection_is_reported_not_saved(self):
        plug = self.start("tasmota-rejects")
        outcome = setup_network.send_wifi(
            "Home", "wrong", host=plug.host, settle=0.05, poll=0.05)
        self.assertEqual(outcome, ("rejected", None))

    def test_no_verdict_within_the_time_is_unknown(self):
        plug = self.start("tasmota-silent")  # keeps answering "trying to connect"
        outcome = setup_network.send_wifi(
            "Home", "pw", host=plug.host, settle=0.0, poll=0.05, timeout=0.3)
        self.assertEqual(outcome, ("unknown", None))

    def test_no_setup_page_is_an_error(self):
        with self.assertRaises(setup_network.SetupError):
            setup_network.send_wifi("Home", "pw", host="127.0.0.1:9", settle=0.0, poll=0.1)

    def test_the_template_and_its_layout_reach_the_bridge(self):
        plug = self.start("bridge")
        layout = {"pool": "23", "pool_size": "699", "ssid": "4", "password": "6", "crc": "4092"}
        reply = setup_network.send_tasmota_settings(b"T" * 4096, host=plug.host, layout=layout)
        self.assertIn("saved", reply)
        self.assertEqual(plug.settings, (layout, b"T" * 4096))

    def test_a_bridge_that_cannot_save_says_why(self):
        for behaviour, words in (("bridge-v4", "older than v5"), ("bridge-not-joined", "not joined")):
            plug = self.start(behaviour)
            with self.assertRaisesRegex(setup_network.SetupError, words):
                setup_network.send_tasmota_settings(b"T" * 4096, host=plug.host, layout={})

    def test_joining_is_detected_without_requesting_a_page(self):
        plug = self.start("tasmota")
        hostname, port = plug.host.split(":")
        self.assertTrue(setup_network.port_open(hostname, int(port)))
        self.assertEqual(plug.page_views, 0)
        self.assertFalse(setup_network.port_open("127.0.0.1", 9, timeout=0.3))


if __name__ == "__main__":
    unittest.main()
