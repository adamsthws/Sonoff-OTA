"""The OpenBeken HTTP client, against a stand-in for OpenBeken's own server."""
import http.server
import json
import threading
import unittest
from unittest import mock

from tools.autoflash import openbeken

FLASH = bytes(range(256)) * 128  # 32 KiB of "flash", from 0x3e0000


class FakeOpenBeken(http.server.ThreadingHTTPServer):
    """OpenBeken's REST as the S61 build answers it: /api/info, /api/cmnd,
    /api/channels, /api/flash/<hex>-<hex>."""

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.commands: list[str] = []
        self.unknown = {"NoSuchCommand"}
        self.flash_failures = 0   # the first this many flash reads come back short
        self.uptime = 42
        self.restarts_on_reboot = True
        self.kind = "sonoff-s61"   # what its import module answers AutoflashKind for; None: no module

    @property
    def host(self):
        return f"127.0.0.1:{self.server_address[1]}"


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, status, body, kind="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/info":
            self._send(200, json.dumps({
                "uptime_s": self.server.uptime, "build": "Built on Oct 30 2026 12:00:00 version 1.0.0", "ip": "192.168.0.50",
                "mac": "12:34:56:AB:CD:EF", "flags": "0", "mqtthost": ":1883", "mqtttopic": "obk",
                "chipset": "BK7238", "webapp": "", "shortName": "openbeken-abcdef", "startcmd": "",
                "supportsSSDP": 0, "supportsClientDeviceDB": True}).encode())
        elif self.path == "/api/channels":
            self._send(200, b'{"1":1,"2":0}')
        elif self.path.startswith("/api/flash/"):
            start, length = (int(part, 16) for part in self.path[len("/api/flash/"):].split("-"))
            data = FLASH[start - 0x3E0000:start - 0x3E0000 + length]
            if self.server.flash_failures:
                self.server.flash_failures -= 1
                data = data[:10]
            self._send(200, data, "application/octet-stream")
        else:
            self._send(404, b"<html>not here</html>", "text/html")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        if self.path != "/api/cmnd":
            self._send(404, b"{}")
            return
        self.server.commands.append(body)
        name = body.split()[0]
        if name == "reboot" and self.server.restarts_on_reboot:
            self.server.uptime = 1
        if name in self.server.unknown or (name == "AutoflashKind" and self.server.kind is None):
            self._send(200, json.dumps({"error": 501, "msg": "command not found", "res": ""}).encode())
        elif name == "AutoflashKind" and body.split()[1:] != [self.server.kind]:
            self._send(400, json.dumps({"error": 400, "msg": "bad argument", "res": ""}).encode())
        else:
            self._send(200, json.dumps({"success": 200, "msg": f"{name} done", "res": ""}).encode())


class OpenBekenTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeOpenBeken()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        patch = mock.patch.object(openbeken.time, "sleep")
        patch.start()
        self.addCleanup(patch.stop)

    def test_info_says_what_it_is(self):
        found = openbeken.info(self.server.host)
        self.assertEqual((found["chipset"], found["shortName"]), ("BK7238", "openbeken-abcdef"))
        self.assertEqual(openbeken.version(found), "1.0.0")
        self.assertTrue(openbeken.is_openbeken(self.server.host))

    def test_its_kind_is_what_its_import_module_says_whatever_its_name(self):
        from tools.autoflash import kinds
        found = openbeken.info(self.server.host)
        self.assertIs(openbeken.kind(self.server.host, found), kinds.SONOFF_S61)
        self.assertIn("AutoflashKind sonoff-s61", self.server.commands)
        # Renamed by its user: the same kind.
        self.assertIs(openbeken.kind(self.server.host, {**found, "shortName": "kitchen"}), kinds.SONOFF_S61)

    def test_an_openbeken_the_tool_did_not_install_is_no_kind(self):
        self.server.kind = None   # no import module: AutoflashKind unknown
        self.assertIsNone(openbeken.kind(self.server.host, openbeken.info(self.server.host)))
        self.server.kind = "another-plug"
        self.assertIsNone(openbeken.kind(self.server.host, openbeken.info(self.server.host)))

    def test_only_an_openbeken_on_a_chip_of_ours_is_asked(self):
        found = {**openbeken.info(self.server.host), "chipset": "BK7231N"}
        self.assertIsNone(openbeken.kind(self.server.host, found))
        self.assertEqual(self.server.commands, [])

    def test_nothing_else_is_taken_for_openbeken(self):
        class Other(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                body = b'{"a page": "with json but not OpenBeken"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Other)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        self.assertIsNone(openbeken.info(f"127.0.0.1:{other.server_address[1]}"))
        self.assertIsNone(openbeken.info("127.0.0.1:1", timeout=0.5))  # nothing listening

    def test_a_command_answers_through_the_envelope(self):
        self.assertEqual(openbeken.command(self.server.host, "SetStartValue 1 -1"), "SetStartValue done")
        self.assertEqual(self.server.commands, ["SetStartValue 1 -1"])
        with self.assertRaisesRegex(openbeken.CommandError, "NoSuchCommand.*not a command this OpenBeken knows"):
            openbeken.command(self.server.host, "NoSuchCommand 1")

    def test_ota_http_sends_the_url_as_one_argument(self):
        openbeken.ota_http(self.server.host, "http://192.168.0.2:8088/esphome.rbl")
        self.assertEqual(self.server.commands, ["ota_http http://192.168.0.2:8088/esphome.rbl"])

    def test_a_restart_is_seen_by_its_uptime(self):
        self.assertTrue(openbeken.restart(self.server.host, wait=10))
        self.assertEqual(self.server.commands, ["reboot"])

    def test_one_that_does_not_restart_is_said_so(self):
        self.server.restarts_on_reboot = False
        with mock.patch.object(openbeken.time, "monotonic", side_effect=[0, 0, 5, 11]):
            self.assertFalse(openbeken.restart(self.server.host, wait=10))

    def test_channels_are_read(self):
        self.assertEqual(openbeken.channels(self.server.host), {"1": 1, "2": 0})

    def test_flash_is_read_in_paced_chunks_each_tried_again(self):
        self.server.flash_failures = 2
        seen = []
        data = openbeken.read_flash(self.server.host, 0x3E0000, len(FLASH), progress=lambda done, total: seen.append(done))
        self.assertEqual(data, FLASH)
        self.assertEqual(seen, [0x4000, 0x8000])
        with mock.patch.object(openbeken, "FLASH_TRIES", 1):
            self.server.flash_failures = 1
            with self.assertRaisesRegex(OSError, "short read"):
                openbeken.read_flash(self.server.host, 0x3E0000, 0x100)


if __name__ == "__main__":
    unittest.main()
