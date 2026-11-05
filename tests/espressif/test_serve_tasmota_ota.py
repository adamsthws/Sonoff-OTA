import http.client
import threading
import time
import unittest

from tools.espressif.serve_tasmota_ota import FirmwareServer, parse_single_range
from tools.autoflash.routes.espressif import installer


class FirmwareServerTests(unittest.TestCase):
    def setUp(self):
        self.firmware = bytes(range(100))
        self.server = FirmwareServer(("127.0.0.1", 0), self.firmware, "127.0.0.1")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path="/user1.bin", byte_range=None):
        headers = {} if byte_range is None else {"Range": byte_range}
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=2
        )
        try:
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            return response.status, response.getheaders(), response.read()
        finally:
            connection.close()

    def test_shutdown_is_not_held_up_by_an_open_keep_alive_connection(self):
        # The plug leaves its connection open after the last range. Shutdown
        # used to wait on it forever, after every byte had been delivered.
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=2
        )
        connection.request("GET", "/user1.bin", headers={"Range": "bytes=0-99"})
        response = connection.getresponse()
        self.assertEqual(response.read(), self.firmware)
        try:
            stopper = threading.Thread(target=self.server.shutdown, daemon=True)
            stopper.start()
            stopper.join(timeout=3)
            self.assertFalse(stopper.is_alive(), "shutdown blocked on an idle connection")
        finally:
            connection.close()

    def test_serves_only_exact_firmware_paths(self):
        status, _, body = self.request("/wrong.bin")
        self.assertEqual((status, body), (404, b""))
        status, _, body = self.request("/user1.bin?ignored=query")
        self.assertEqual((status, body), (200, self.firmware))

    def test_serves_the_names_it_is_given(self):
        # OpenBeken fetches by the URL it is given, firmware.rbl (routes/beken/onward.py).
        named = FirmwareServer(("127.0.0.1", 0), self.firmware, "127.0.0.1", names=("firmware.rbl",))
        thread = threading.Thread(target=named.serve_forever, daemon=True)
        thread.start()
        try:
            for path, status in (("/firmware.rbl", 200), ("/user1.bin", 404)):
                connection = http.client.HTTPConnection("127.0.0.1", named.server_address[1], timeout=2)
                connection.request("GET", path)
                self.assertEqual(connection.getresponse().status, status, path)
                connection.close()
        finally:
            named.shutdown()
            named.server_close()
            thread.join(timeout=2)

    def test_a_download_cut_off_says_how_far_it_got(self):
        import socket
        big = FirmwareServer(("127.0.0.1", 0), bytes(16 * 1024 * 1024), "127.0.0.1")
        thread = threading.Thread(target=big.serve_forever, daemon=True)
        thread.start()
        try:
            client = socket.create_connection(("127.0.0.1", big.server_address[1]), timeout=2)
            client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            client.sendall(b"GET /user1.bin HTTP/1.1\r\nHost: x\r\n\r\n")
            got = 0
            while got < 200 * 1024:
                got += len(client.recv(65536))
            client.close()
            deadline = time.monotonic() + 5
            while big.unique_bytes_served() == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertGreater(big.unique_bytes_served(), 0)
            self.assertLess(big.unique_bytes_served(), 16 * 1024 * 1024)
        finally:
            big.shutdown()
            big.server_close()
            thread.join(timeout=2)

    def test_rejects_wrong_source(self):
        self.server.device_ip = "192.0.2.55"
        status, _, body = self.request()
        self.assertEqual((status, body), (403, b""))
        self.assertEqual(self.server.bytes_served, 0)

    def test_valid_range_and_unique_coverage(self):
        status, headers, body = self.request(byte_range="bytes=0-23")
        self.assertEqual(status, 206)
        self.assertIn(("Content-Range", "bytes 0-23/100"), headers)
        self.assertEqual(body, self.firmware[:24])
        self.request(byte_range="bytes=20-99")
        # The handler counts after writing, so its thread may finish after the client has read.
        deadline = time.monotonic() + 2
        while self.server.bytes_served < 104 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.server.bytes_served, 104)
        self.assertEqual(self.server.unique_bytes_served(), 100)

    def test_invalid_ranges_return_416_without_bytes(self):
        for value in ("items=0-1", "bytes=-10", "bytes=10-9", "bytes=0-100", "bytes=0-1,3-4"):
            with self.subTest(value=value):
                status, headers, body = self.request(byte_range=value)
                self.assertEqual((status, body), (416, b""))
                self.assertIn(("Content-Range", "bytes */100"), headers)
        self.assertEqual(self.server.bytes_served, 0)

    def test_range_parser_accepts_open_end(self):
        self.assertEqual(parse_single_range("bytes=24-", 100), (24, 99))


if __name__ == "__main__":
    unittest.main()


class OfflineTests(unittest.TestCase):
    """A plug offline in eWeLink - after a send cut off part way, say - is
    waited for, not refused at once."""

    def test_one_that_comes_back_is_sent_to(self):
        from unittest import mock
        from tools.ewelink import ota_push as sender
        looks = iter([{"online": False}, {"online": True}])
        with mock.patch("builtins.print"):
            device = sender.until_online({"online": False}, lambda: next(looks), sleep=lambda _: None)
        self.assertTrue(device["online"])

    def test_one_that_stays_away_is_given_up_on(self):
        from unittest import mock
        from tools.ewelink import ota_push as sender
        looks = []

        def look():
            looks.append(1)
            return {"online": False}

        with mock.patch("builtins.print"):
            device = sender.until_online({"online": False}, look, sleep=lambda _: None)
        self.assertFalse(device["online"])
        self.assertEqual(len(looks), sender.OFFLINE_WAIT // 10)


class FailureTests(unittest.TestCase):
    """A failed send says whether the upgrade command went out."""

    def said(self, sent):
        import io
        from unittest import mock
        import tools.espressif.serve_tasmota_ota as sender
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            sender.report_failure(OSError("EOF occurred in violation of protocol"), sent)
        return err.getvalue()

    def test_before_the_command_it_says_nothing_reached_the_plug(self):
        import tools.espressif.serve_tasmota_ota as sender
        said = self.said(sent=False)
        self.assertIn("ERROR: EOF occurred", said)
        self.assertIn(sender.NOT_SENT, said)

    def test_after_it_only_the_error(self):
        import tools.espressif.serve_tasmota_ota as sender
        self.assertNotIn(sender.NOT_SENT, self.said(sent=True))

    def test_the_run_looks_for_what_it_says(self):
        import tools.espressif.serve_tasmota_ota as sender
        self.assertTrue(sender.NOT_SENT.startswith(installer.BRIDGE_NOT_SENT))


class AnnouncedVersionTests(unittest.TestCase):
    def test_the_bridge_is_sent_as_the_announced_version(self):
        # Its own version (4.4.4) stays in the published file; the plug's
        # updater is told the fixed one, in the command and the wrapper.
        import sys
        from pathlib import Path
        from unittest import mock
        import tools.espressif.serve_tasmota_ota as sender
        from tools.autoflash.runs import ROOT
        from tools.espressif.analyze_vendor_ota import analyze
        from tools.espressif.build_vendor_ota import ANNOUNCED_VERSION
        from tools.ewelink.ota_push import Delivery
        bridge = ROOT / installer.BRIDGE_OTA
        exists = Path.exists
        argv = ["serve_tasmota_ota.py", "--listen-ip", "192.0.2.2", "--device-ip", "192.0.2.3",
                "--firmware", str(bridge), "--email", "someone@example.com",
                "--expected-current-version", "1.2.0", "--mdns-capture", "mdns.json", sender.SAFETY_FLAG]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(Path, "exists", autospec=True,
                               side_effect=lambda path: path.name != "RECOVERY_LOCK" and exists(path)), \
             mock.patch.dict("os.environ", {"EWELINK_PASSWORD": "pw"}), \
             mock.patch.object(sender, "target_id_from_mdns", return_value="1000000000"), \
             mock.patch.object(sender, "deliver", return_value=Delivery(sent=True, complete=True)) as deliver, \
             mock.patch("builtins.print"):
            self.assertEqual(sender.main(), 0)
        sent = deliver.call_args.kwargs
        self.assertEqual(sent["version"], ANNOUNCED_VERSION)
        self.assertEqual(analyze(sent["firmware"])["wrapper"]["header_version"], ANNOUNCED_VERSION)
        self.assertEqual(sent["firmware"][100:], bridge.read_bytes()[100:])


class DescribeCloudTests(unittest.TestCase):
    def test_offline_and_online(self):
        from tools.ewelink.ota_push import describe_cloud
        self.assertIn("went offline", describe_cloud({"action": "sysmsg", "params": {"online": False}}, "1"))
        self.assertIn("back online", describe_cloud({"action": "sysmsg", "params": {"online": True}}, "1"))

    def test_upgrade_reply(self):
        from tools.ewelink.ota_push import describe_cloud
        self.assertEqual(describe_cloud({"error": 0, "sequence": "7"}, "7"), "Cloud: upgrade command accepted")
        self.assertIn("error 406", describe_cloud({"error": 406, "sequence": "7"}, "7"))

    def test_update_shows_state_but_never_identifiers(self):
        from tools.ewelink.ota_push import describe_cloud
        line = describe_cloud({"action": "update", "deviceid": "1001234abc", "apikey": "secret",
                               "params": {"fwVersion": "1.2.0", "ssid": "Home", "switch": "on"}}, "1")
        self.assertIn('"fwVersion": "1.2.0"', line)
        self.assertNotIn("1001234abc", line)
        self.assertNotIn("secret", line)
        self.assertNotIn("Home", line)

    def test_empty_messages_stay_quiet(self):
        from tools.ewelink.ota_push import describe_cloud
        self.assertIsNone(describe_cloud({"action": "update", "params": {}}, "1"))
        self.assertIsNone(describe_cloud({"error": 0, "sequence": "other"}, "1"))
