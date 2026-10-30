"""The cloud stub's helpers run on their own, as the emulator runs them: in
isolated mode (-I), where only their own path puts the repository on sys.path."""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tools.espressif.emulator.stub_cloud import stub_server

STUB = Path(__file__).resolve().parents[2] / "tools" / "espressif" / "emulator" / "stub_cloud"


class StubHelpersRunIsolatedTests(unittest.TestCase):
    def run_isolated(self, script):
        return subprocess.run([sys.executable, "-I", str(STUB / script), "--help"],
                              capture_output=True, text=True, timeout=60)

    def test_device_identity_imports_the_repository(self):
        done = self.run_isolated("device_identity.py")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("usage:", done.stdout)

    def test_make_ca_imports_the_repository(self):
        done = self.run_isolated("make_ca.py")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("usage:", done.stdout)


class SendToDeviceTests(unittest.TestCase):
    def test_a_command_reaches_the_device_online_and_no_other(self):
        conn = mock.Mock()
        with mock.patch.dict(stub_server._devices, {"1000abcdef": conn}, clear=True):
            self.assertTrue(stub_server.send_to_device("1000abcdef", {"action": "upgrade"}))
            self.assertFalse(stub_server.send_to_device("1000fedcba", {"action": "upgrade"}))
        self.assertEqual(json.loads(conn.send.call_args.args[0]), {"action": "upgrade"})


class AccountDeviceTests(unittest.TestCase):
    """A plug whose identity the stub is not given - the S61's, sealed in its
    store - is listed as it registers, as eWeLink lists it."""

    def stub(self, device=None):
        return stub_server.Stub("192.0.2.1", 443, 10, device)

    def test_nothing_listed_before_the_plug_registers(self):
        stub = self.stub({"staMac": "12:34:56:ab:cd:ef", "model": "SN-BK7238-S61-01"})
        self.assertEqual(stub.device_thing()["error"], 404)

    def test_the_registered_identity_completes_the_record(self):
        stub = self.stub({"staMac": "12:34:56:ab:cd:ef", "model": "SN-BK7238-S61-01"})
        stub.heard({"action": "register", "deviceid": "1000abcdef", "apikey": "made-up-apikey",
                    "romVersion": "1.0.2"})
        item = stub.device_thing()["data"]["thingList"][0]["itemData"]
        self.assertEqual((item["deviceid"], item["apikey"]), ("1000abcdef", "made-up-apikey"))
        self.assertEqual(item["extra"]["model"], "SN-BK7238-S61-01")
        self.assertEqual(item["params"]["staMac"], "12:34:56:ab:cd:ef")
        self.assertEqual(item["params"]["fwVersion"], "1.0.2")
        self.assertEqual(stub.account_login()["data"]["user"]["apikey"], "made-up-apikey")

    def test_a_given_identity_is_kept(self):
        stub = self.stub({"deviceid": "1000fedcba", "devicekey": "k", "apikey": "a"})
        stub.heard({"action": "register", "deviceid": "1000abcdef", "apikey": "other"})
        item = stub.device_thing()["data"]["thingList"][0]["itemData"]
        self.assertEqual(item["deviceid"], "1000fedcba")


class TimeZoneTests(unittest.TestCase):
    def test_a_time_zone_query_is_answered_with_one(self):
        stub = stub_server.Stub("192.0.2.1", 443, 10)
        reply = stub.ws_reply({"action": "query", "deviceid": "1000abcdef", "params": ["TZ"]})
        tz = reply["params"]["TZ"]
        self.assertEqual((tz["dst"], tz["offset"], tz["nextOffset"]), (0, 0, 0))
        self.assertRegex(tz["utc"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertGreater(tz["nextDst"], tz["utc"])

    def test_another_query_is_answered_plainly(self):
        stub = stub_server.Stub("192.0.2.1", 443, 10)
        self.assertNotIn("params", stub.ws_reply({"action": "query", "params": ["switch"]}))


class SilentDeviceTests(unittest.TestCase):
    """A device switched off sends no close: missing its heartbeats, it goes
    offline, as at eWeLink."""

    def test_a_silent_device_is_dropped(self):
        import socket
        import threading
        stub = stub_server.Stub("192.0.2.1", 443, 10)
        stub.silence_limit = lambda: 0.5
        ours, theirs = socket.socketpair()
        handler = stub_server.Handler.__new__(stub_server.Handler)
        handler.stub = stub
        register = json.dumps({"action": "register", "deviceid": "1000abcdef", "apikey": "made-up"}).encode()
        theirs.sendall(bytes([0x81, len(register)]) + register)
        with mock.patch.dict(stub_server._devices, {}, clear=True), mock.patch.object(stub_server, "log"):
            serving = threading.Thread(target=handler._serve_ws, args=(ours, {}, "test"))
            serving.start()
            serving.join(10)
            self.assertFalse(serving.is_alive())
            self.assertFalse(stub_server._online("1000abcdef"))
        ours.close()
        theirs.close()


if __name__ == "__main__":
    unittest.main()
