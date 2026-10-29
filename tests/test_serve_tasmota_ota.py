import http.client
import threading
import time
import unittest

from tools.espressif.serve_tasmota_ota import FirmwareServer, parse_single_range


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
        import tools.espressif.serve_tasmota_ota as sender
        looks = iter([{"online": False}, {"online": True}])
        with mock.patch("builtins.print"):
            device = sender.until_online({"online": False}, lambda: next(looks), sleep=lambda _: None)
        self.assertTrue(device["online"])

    def test_one_that_stays_away_is_given_up_on(self):
        from unittest import mock
        import tools.espressif.serve_tasmota_ota as sender
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
        import tools.autoflash_cli as autoflash
        import tools.espressif.serve_tasmota_ota as sender
        self.assertTrue(sender.NOT_SENT.startswith(autoflash.BRIDGE_NOT_SENT))


class DescribeCloudTests(unittest.TestCase):
    def test_offline_and_online(self):
        from tools.espressif.serve_tasmota_ota import describe_cloud
        self.assertIn("went offline", describe_cloud({"action": "sysmsg", "params": {"online": False}}, "1"))
        self.assertIn("back online", describe_cloud({"action": "sysmsg", "params": {"online": True}}, "1"))

    def test_upgrade_reply(self):
        from tools.espressif.serve_tasmota_ota import describe_cloud
        self.assertEqual(describe_cloud({"error": 0, "sequence": "7"}, "7"), "Cloud: upgrade command accepted")
        self.assertIn("error 406", describe_cloud({"error": 406, "sequence": "7"}, "7"))

    def test_update_shows_state_but_never_identifiers(self):
        from tools.espressif.serve_tasmota_ota import describe_cloud
        line = describe_cloud({"action": "update", "deviceid": "1001234abc", "apikey": "secret",
                               "params": {"fwVersion": "1.2.0", "ssid": "Home", "switch": "on"}}, "1")
        self.assertIn('"fwVersion": "1.2.0"', line)
        self.assertNotIn("1001234abc", line)
        self.assertNotIn("secret", line)
        self.assertNotIn("Home", line)

    def test_empty_messages_stay_quiet(self):
        from tools.espressif.serve_tasmota_ota import describe_cloud
        self.assertIsNone(describe_cloud({"action": "update", "params": {}}, "1"))
        self.assertIsNone(describe_cloud({"error": 0, "sequence": "other"}, "1"))
