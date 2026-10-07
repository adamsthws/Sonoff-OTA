import http.server
import threading
import unittest
import urllib.parse

from tools.autoflash.device import Device


class FakeTasmota(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakeTasmotaHandler)
        self.visits: list[str] = []
        self.armed_for_settings = False
        self.restored: bytes | None = None


class FakeTasmotaHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        server: FakeTasmota = self.server
        path = urllib.parse.urlsplit(self.path).path
        server.visits.append(path)
        if path == "/dl":
            self.reply(b"BACKUP")
        elif path == "/cs":
            server.referer = self.headers.get("Referer")
            self.reply(b"47}1}1\x1e00:00:00.255 S60 recovery Wi-Fi defaults active\n"
                       b"00:00:00.316 S60 recovery: Wi-Fi settings saved for the next firmware\n")
        else:
            server.armed_for_settings = path == "/rs"  # Tasmota: HandleRestoreConfiguration
            self.reply(b"<title>Tasmota</title>")

    def do_POST(self):
        server: FakeTasmota = self.server
        server.visits.append(urllib.parse.urlsplit(self.path).path)
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if server.armed_for_settings:
            start = body.index(b"\r\n\r\n") + 4
            server.restored = body[start : body.rindex(b"\r\n--")]
        self.reply(b"Upload successful")


class SettingsBackupTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeTasmota()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))
        self.device = Device(f"127.0.0.1:{self.server.server_address[1]}")

    def test_a_backup_is_downloaded(self):
        self.assertEqual(self.device.download_settings(), b"BACKUP")

    def test_the_console_log_is_read_with_a_referer(self):
        lines = self.device.console_log()
        self.assertEqual(lines[-1], "00:00:00.316 S60 recovery: Wi-Fi settings saved for the next firmware")
        self.assertTrue(self.server.referer)  # without one the page closes the connection

    def test_a_restore_arms_the_restore_page_first(self):
        # Without visiting /rs first, Tasmota would take the file as firmware.
        self.device.restore_settings(b"\x09\x12" + bytes(4094))
        self.assertEqual(self.server.visits, ["/rs", "/u2"])
        self.assertEqual(self.server.restored, b"\x09\x12" + bytes(4094))


if __name__ == "__main__":
    unittest.main()
