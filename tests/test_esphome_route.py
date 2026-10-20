import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tools.autoflash_cli as af
from tools.autoflash import prompts
from tools.autoflash.state import DONE, RunState


class RouteTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)
        # No plug's own network to be seen, nor joined, and an ESPHome that
        # wants nothing for an update: nothing goes on the network.
        for owner, name, value in ((af, "on_a_plug_network", False), (af, "find_ssid", None),
                                   (af.esphome_ota, "wants", af.esphome_ota.NOTHING)):
            patch = mock.patch.object(owner, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)

    def ctx(self, given=None, run="run", **inputs):
        state = RunState.open(Path(self.tmp.name) / run)
        for key, value in {"product_model": "S60TPF", "stock_version": "1.2.0",
                           "plug_ip": "192.168.0.50", **inputs}.items():
            state.set_input(key, value)
        return SimpleNamespace(state=state, firmware_given=given, esphome_firmware=None,
                               dry_run=False, finder=SimpleNamespace(poll=lambda: None),
                               esphome_password=None, esphome_key=None, web_password=None,
                               esphome_login=None)

    def setUp_found(self, running="sonoff"):
        # What the plug runs now, as the network would show it.
        patch = mock.patch.object(af, "find_current_firmware", return_value=running)
        patch.start()
        self.addCleanup(patch.stop)


class FirmwareChoiceTests(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.setUp_found()

    def test_it_is_asked_once_then_kept(self):
        ctx = self.ctx()
        with mock.patch.object(af, "ask_choice", return_value=1) as asked:
            self.assertEqual(af.settle_firmware(ctx), "esphome")
        self.assertEqual(asked.call_args.args[1], ["Tasmota", "ESPHome"])
        self.assertEqual(ctx.state.input("firmware"), "esphome")
        with mock.patch.object(af, "ask_choice", side_effect=AssertionError("asked again")):
            self.assertEqual(af.settle_firmware(ctx), "esphome")

    def test_given_on_the_command_line_it_is_not_asked(self):
        ctx = self.ctx(given="esphome")
        with mock.patch.object(af, "ask_choice", side_effect=AssertionError("asked")):
            self.assertEqual(af.settle_firmware(ctx), "esphome")

    def test_a_conversion_cannot_switch_part_way(self):
        ctx = self.ctx(given="tasmota", firmware="esphome")
        with self.assertRaisesRegex(af.Abort, "cannot switch"):
            af.settle_firmware(ctx)

    def test_a_run_begun_before_there_was_a_choice_installs_tasmota(self):
        ctx = self.ctx()
        ctx.state.mark("A1", DONE)
        with mock.patch.object(af, "ask_choice", side_effect=AssertionError("asked")):
            self.assertEqual(af.settle_firmware(ctx), "tasmota")

    def test_an_unattended_run_must_be_told(self):
        ctx = self.ctx()
        with mock.patch.object(prompts, "UNATTENDED", True):
            with self.assertRaisesRegex(af.Abort, "--firmware esphome"):
                af.settle_firmware(ctx)
        self.assertIsNone(ctx.state.input("firmware"))


class RouteTests(RouteTestCase):
    def test_each_firmware_has_its_own_steps(self):
        esphome = [step for step, _, _ in af.steps_for("esphome")]
        tasmota = [step for step, _, _ in af.steps_for("tasmota")]
        self.assertEqual(esphome[-4:], ["D4", "D5", "I1", "I2"])
        self.assertNotIn("E3", esphome)  # the partition table is left as Sonoff made it
        self.assertEqual(tasmota[-1], "H1")
        self.assertEqual(esphome[:len(af.COMMON_STEPS)], tasmota[:len(af.COMMON_STEPS)])

    def test_d4_is_named_for_what_it_does_on_each_route(self):
        self.addCleanup(af.apply_target, "tasmota")
        af.apply_target("esphome")
        self.assertEqual(af.stage_name("D4"), "Making room for ESPHome")
        af.apply_target("tasmota")
        self.assertEqual(af.stage_name("D4"), "Preparing memory layout")

    def test_an_install_cut_short_by_the_network_is_tried_again(self):
        self.assertIn("I1", af.RETRY_STEPS)
        said = ("esphome did not pass: the plug says: migration_refused: "
                "POST /capture/written/21 returned HTTP -11 after 6 attempts")
        self.assertRegex(said, af.TRANSIENT_RE)

    def test_a_phase_left_to_be_asked_about_is_still_tried_again(self):
        # I1 stays "attempted", with no error kept, when its phase ends with no
        # result; the reason it gave decides whether to try again in this run.
        ctx = self.ctx(firmware="esphome")
        ctx.last_failure = ("the plug stopped the esphome without reaching this computer; "
                            "its console says: POST /capture/written/22 returned HTTP -1")
        tries = []

        def execute(ctx, step):
            tries.append(step)
            return 1 if len(tries) == 1 else 0

        with mock.patch.object(af, "execute", side_effect=execute), \
                mock.patch.object(af, "wait_for_plug_back"), mock.patch.object(af.time, "sleep"):
            self.assertEqual(af.execute_with_retries(ctx, "I1"), 0)
        self.assertEqual(tries, ["I1", "I1"])

    def test_a_run_is_complete_by_its_own_firmware(self):
        ctx = self.ctx(firmware="esphome")
        ctx.state.mark("I2", DONE)
        self.assertTrue(af.run_is_complete(Path(self.tmp.name) / "run"))

    def test_d4_is_not_rechecked_once_esphome_runs(self):
        ctx = self.ctx(firmware="esphome")
        ctx.state.mark("I1", DONE)
        ctx.device = SimpleNamespace(current_ota=lambda: self.fail("asked the plug"))
        with mock.patch.object(af, "commit_report", return_value=None):
            self.assertEqual(af.verify_high_slot(ctx), (True, ""))


class EndingTests(RouteTestCase):
    def test_the_report_says_esphome(self):
        fields = dict(af.report_fields(self.ctx(firmware="esphome"), 300, False))
        self.assertEqual(fields["Firmware"], "ESPHome")
        self.assertEqual(fields["Name in ESPHome"], "Sonoff S60")
        self.assertNotIn("Name in Tasmota", fields)
        tasmota = dict(af.report_fields(self.ctx(run="tasmota-run"), 300, False))
        self.assertEqual(tasmota["Firmware"], "Tasmota")

    def test_the_hardware_check_switches_through_esphome(self):
        relay = {"on": True}

        def switch(host, on):
            relay["on"] = on
            return True

        def meter(host):
            return {"Voltage": 236.0, "Power": 0.0} if relay["on"] else {"Voltage": 0.0, "Power": 0.0}

        with mock.patch.object(af, "METER_SETTLE", 0), \
                mock.patch.object(af.esphome, "relay", side_effect=lambda host: relay["on"]), \
                mock.patch.object(af.esphome, "switch_relay", side_effect=switch), \
                mock.patch.object(af.esphome, "meter", side_effect=meter):
            result = af.check_esphome_hardware(self.ctx(firmware="esphome"))
        self.assertTrue(relay["on"], "left as it was found")
        self.assertIn("236 V", result["Power meter"])

    def test_a_plug_that_does_not_answer_is_reported_not_raised(self):
        with mock.patch.object(af.esphome, "relay", return_value=None):
            result = af.check_esphome_hardware(self.ctx(firmware="esphome"))
        self.assertEqual(result["Relay"], "could not be checked")


class CheckStepTests(RouteTestCase):
    def test_it_recognises_esphome_itself(self):
        with mock.patch.object(af.esphome, "is_esphome", return_value=True):
            self.assertIn("seconds", af.step_esphome_check(self.ctx(firmware="esphome")))

    def test_waiting_in_vain_points_at_the_wifi(self):
        with mock.patch.object(af.esphome, "is_esphome", return_value=False), \
                mock.patch.object(af.time, "sleep"):
            with self.assertRaisesRegex(af.Abort, "Sonoff-Setup-XXXX"):
                af.step_esphome_check(self.ctx(firmware="esphome"), wait=0.05)

    def test_on_its_own_network_it_is_set_up_through_that(self):
        with mock.patch.object(af.esphome, "is_esphome", return_value=False), \
                mock.patch.object(af.time, "sleep"), \
                mock.patch.object(af, "set_up_through_own_network", return_value=("Home", "pw")) as own:
            result = af.step_esphome_check(self.ctx(firmware="esphome"), wait=0.05)
        self.assertEqual(result["set_up_through"], "its own network")
        self.assertEqual(own.call_args.args[1], "ESPHome")

    def test_joined_already_it_stops_waiting_on_the_home_wifi(self):
        with mock.patch.object(af.esphome, "is_esphome", return_value=False), \
                mock.patch.object(af, "on_a_plug_network", return_value=True), \
                mock.patch.object(af.time, "sleep") as slept, \
                mock.patch.object(af, "set_up_through_own_network", return_value=("Home", "pw")):
            af.step_esphome_check(self.ctx(firmware="esphome"), wait=600)
        slept.assert_not_called()  # straight on, not three minutes first

    def test_a_dry_run_does_not_wait(self):
        ctx = self.ctx(firmware="esphome")
        ctx.dry_run = True
        with mock.patch.object(af.esphome, "is_esphome", side_effect=AssertionError("waited")):
            self.assertEqual(af.step_esphome_check(ctx), {"dry_run": True})


class ESPHomePageTests(unittest.TestCase):
    """ESPHome's page, as the plug serves it with web_server's local: true."""

    def serve(self, body, headers):
        import http.server
        import threading

        class Page(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"127.0.0.1:{server.server_address[1]}"

    def test_a_gzipped_page_is_recognised(self):
        import gzip
        page = b"<!doctype html><html><body><esp-app></esp-app></body></html>"
        host = self.serve(gzip.compress(page), {"Content-Type": "text/html", "Content-Encoding": "gzip"})
        self.assertTrue(af.esphome.is_esphome(host))

    def test_a_page_damaged_on_its_way_is_no_answer_not_a_crash(self):
        # Seen in the fault suite: one byte changed in the gzipped page, and
        # decompressing it raised zlib.error - "something unexpected went
        # wrong" - where a dropped connection is simply asked again.
        import gzip
        page = bytes(range(256)) * 64 + b"<esp-app></esp-app>"
        damaged = bytearray(gzip.compress(page))
        damaged[len(damaged) // 2] ^= 0x5A
        host = self.serve(bytes(damaged), {"Content-Type": "text/html", "Content-Encoding": "gzip"})
        self.assertFalse(af.esphome.is_esphome(host))

    def test_a_tasmota_page_is_not(self):
        host = self.serve(b"<html><body>Tasmota</body></html>", {"Content-Type": "text/html"})
        self.assertFalse(af.esphome.is_esphome(host))


class SignalSensorTests(unittest.TestCase):
    """An ESPHome built with the Wi-Fi signal sensor (ours from 1.1.0) gives the
    plug's signal; one without it gives nothing, and the run goes on."""

    def serve(self, pages):
        import http.server
        import threading
        asked = []

        class Page(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                asked.append(self.path)
                body = pages.get(self.path)
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Length", str(len(body or b"")))
                self.end_headers()
                self.wfile.write(body or b"")

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"127.0.0.1:{server.server_address[1]}", asked

    def test_the_sensor_gives_the_signal_in_whole_dbm(self):
        host, asked = self.serve({"/sensor/WiFi%20Signal":
                                  b'{"id":"sensor-wifi_signal","value":-58.4,"state":"-58 dBm"}'})
        self.assertEqual(af.esphome.sensor_signal(host), -58)
        self.assertEqual(asked, ["/sensor/WiFi%20Signal"])

    def test_no_sensor_or_no_reading_yet_gives_nothing(self):
        for pages in ({}, {"/sensor/WiFi%20Signal": b'{"value":NaN,"state":"NA"}'},
                      {"/sensor/WiFi%20Signal": b'{"state":"NA"}'}):
            host, _ = self.serve(pages)
            self.assertIsNone(af.esphome.sensor_signal(host), pages)

    def test_without_the_sensor_the_api_is_asked(self):
        with mock.patch.object(af.esphome, "sensor_signal", return_value=None), \
                mock.patch.object(af.esphome, "api_signal", return_value=-66) as api:
            self.assertEqual(af.esphome.signal("192.168.0.50"), -66)
        api.assert_called_once_with("192.168.0.50", key=None)
        with mock.patch.object(af.esphome, "sensor_signal", return_value=-58), \
                mock.patch.object(af.esphome, "api_signal") as api:
            self.assertEqual(af.esphome.signal("192.168.0.50"), -58)
        api.assert_not_called()


class ApiSignalTests(unittest.TestCase):
    """ESPHome's native API, spoken plainly: its configuration dump gives the
    signal, as an ESPHome without the signal sensor (ours, 1.0.0) prints it."""

    def serve(self, answer):
        """A fake ESPHome API that reads the client's request, then sends `answer`."""
        import socket
        import threading
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)

        def run():
            connection, _ = server.accept()
            with connection:
                connection.recv(4096)
                connection.sendall(answer)
                connection.recv(4096)  # its Disconnect, or the close

        threading.Thread(target=run, daemon=True).start()
        return server.getsockname()[1]

    @staticmethod
    def log(text):
        from tools.autoflash.esphome import _frame, _varint
        return _frame(29, b"\x08\x04" + b"\x1a" + _varint(len(text)) + text)

    def test_the_configuration_dump_gives_the_signal(self):
        from tools.autoflash.esphome import _frame
        hello = _frame(2, b"\x08\x01\x10\x10\x1a\x082026.9.1")
        dump = (self.log(b"\x1b[0;35m[C][logger:250]: Logger:\x1b[0m")
                + self.log("[C][wifi:1297]:   SSID: 'Home'\n  Signal strength: -61 dB ▂▄▆_".encode()))
        port = self.serve(hello + _frame(7) + dump)
        self.assertEqual(af.esphome.api_signal("127.0.0.1", port), -61)

    def test_the_dump_names_the_network_with_the_signal(self):
        dump = self.log("[C][wifi:1297]:   SSID: 'Home-2G'\n  BSSID: 10:01:00:C4:0A:56\n"
                        "  Signal strength: -61 dB ▂▄▆_".encode())
        port = self.serve(dump)
        self.assertEqual(af.esphome.api_wifi("127.0.0.1", port), {"signal": -61, "ssid": "Home-2G"})

    def test_an_encrypted_api_gives_nothing(self):
        port = self.serve(b"\x01\x00\x08Bad indicator")
        self.assertIsNone(af.esphome.api_signal("127.0.0.1", port))

    def test_an_api_that_never_says_gives_nothing_in_time(self):
        port = self.serve(self.log(b"[C][logger:250]: Logger:"))
        self.assertIsNone(af.esphome.api_signal("127.0.0.1", port, timeout=1.0))

    def test_no_api_at_all_gives_nothing(self):
        import socket
        unused = socket.socket()
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
        unused.close()
        self.assertIsNone(af.esphome.api_signal("127.0.0.1", port, timeout=2.0))


class WifiActionTests(unittest.TestCase):
    """Our ESPHome gives the Wi-Fi it is on through its s60_wifi action: listed,
    then called, its answer the details as JSON."""

    def serve(self, *steps):
        """A fake ESPHome API: for each (expected, answer), it waits until what it
        has been sent holds `expected`, then sends `answer`."""
        import socket
        import threading
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)

        def run():
            connection, _ = server.accept()
            received = b""
            with connection:
                for expected, answer in steps:
                    while expected not in received:
                        chunk = connection.recv(4096)
                        if not chunk:
                            return
                        received += chunk
                    connection.sendall(answer)
                connection.recv(4096)

        threading.Thread(target=run, daemon=True).start()
        return server.getsockname()[1]

    @staticmethod
    def listed(*actions):
        import struct
        from tools.autoflash.esphome import _frame, _varint
        out = b""
        for name, key in actions:
            out += _frame(41, b"\x0a" + _varint(len(name)) + name + b"\x15" + struct.pack("<I", key))
        return out + _frame(19)

    @staticmethod
    def answer(success, data=b""):
        from tools.autoflash.esphome import _frame, _varint
        return _frame(131, b"\x08\x01" + (b"\x10\x01" if success else b"")
                      + (b"\x22" + _varint(len(data)) + data if data else b""))

    LIST = b"\x00\x00\x0b"  # the request to list entities

    @staticmethod
    def call(key):
        import struct
        return b"\x0d" + struct.pack("<I", key)  # the action named by its key

    def test_the_action_gives_the_name_and_password(self):
        # Answered only once it is called by the key it was listed with.
        port = self.serve((self.LIST, self.listed((b"other", 7), (b"s60_wifi", 0xCAFEF00D))),
                          (self.call(0xCAFEF00D), self.answer(True, b'{"ssid":"Home-2G","password":"pw"}')))
        self.assertEqual(af.esphome.api_wifi_details("127.0.0.1", port, timeout=3.0), ("Home-2G", "pw"))

    def test_an_esphome_without_the_action_gives_nothing(self):
        port = self.serve((self.LIST, self.listed((b"other", 7))))
        self.assertIsNone(af.esphome.api_wifi_details("127.0.0.1", port, timeout=2.0))

    def test_a_failed_call_gives_nothing(self):
        port = self.serve((self.LIST, self.listed((b"s60_wifi", 1))), (self.call(1), self.answer(False)))
        self.assertIsNone(af.esphome.api_wifi_details("127.0.0.1", port, timeout=2.0))

    def test_an_answer_not_of_json_gives_nothing(self):
        from tools.autoflash.esphome import _frame
        for data in (b"not json", b'["Home-2G"]', b'{"ssid": 7}'):
            port = self.serve((self.LIST, self.listed((b"s60_wifi", 1))), (self.call(1), self.answer(True, data)))
            self.assertIsNone(af.esphome.api_wifi_details("127.0.0.1", port, timeout=2.0), data)
        port = self.serve((self.LIST, self.listed((b"s60_wifi", 1))),
                          (self.call(1), _frame(131, b"\x08\x01\x10\x01\x20\x07")))  # its data a number
        self.assertIsNone(af.esphome.api_wifi_details("127.0.0.1", port, timeout=2.0))

    def test_an_encrypted_api_says_so(self):
        port = self.serve((b"", b"\x01\x00\x08Bad indicator"))
        with self.assertRaises(af.esphome.ApiLocked) as locked:
            af.esphome.api_wifi_details("127.0.0.1", port, timeout=2.0)
        self.assertFalse(locked.exception.wrong)  # no key given: none to be wrong


class EncryptedApiTests(unittest.TestCase):
    """An ESPHome whose API has an encryption key, spoken to as ESPHome's
    api_frame_helper_noise.cpp answers: the hello, the handshake, then each
    message sealed in a frame of its own.  Without its key, or with another,
    it says so; given a key, an ESPHome whose API is plain is spoken to
    plainly."""

    KEY = bytes(range(32))
    WIFI = b'{"ssid":"Home-2G","password":"pw"}'
    DUMP = "[C][wifi:1297]:   SSID: 'Home-2G'\n  Signal strength: -61 dB".encode()

    def serve(self, *, plain_first=False):
        """The fake, for two connections at most: encrypted with KEY - or, with
        `plain_first`, a plain API, which answers the encrypted hello "Bad
        indicator byte" and closes, and then speaks plainly."""
        import socket
        import threading
        from tools.autoflash import noise
        from tools.autoflash.esphome import _frame, _read_varint, _varint
        import struct
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(2)
        self.addCleanup(server.close)
        listed = (b"\x0a" + _varint(8) + b"s60_wifi" + b"\x15" + struct.pack("<I", 0xCAFEF00D))
        answered = b"\x08\x01\x10\x01\x22" + _varint(len(self.WIFI)) + self.WIFI
        log = b"\x08\x04\x1a" + _varint(len(self.DUMP)) + self.DUMP
        # What each request is answered with: its kind, then (kind, payload)s.
        answers = {1: [(2, b"\x08\x01\x10\x10")], 11: [(41, listed), (19, b"")],
                   42: [(131, answered)], 28: [(29, log)]}

        def exactly(connection, size):
            data = b""
            while len(data) < size:
                piece = connection.recv(size - len(data))
                if not piece:
                    raise ConnectionError
                data += piece
            return data

        def plainly(connection):
            read = lambda size: exactly(connection, size)  # noqa: E731
            while True:
                assert read(1) == b"\x00"
                size, kind = _read_varint(read), _read_varint(read)
                read(size)
                for answer, payload in answers.get(kind, []):
                    connection.sendall(_frame(answer, payload))

        def sealed(connection):
            read = lambda size: exactly(connection, size)  # noqa: E731
            try:
                hello = noise.read_frame(read)
            except noise.NoiseError:  # a plain client
                connection.sendall(noise.frame(b"\x01Bad indicator byte"))
                return
            assert hello == b""
            first = noise.read_frame(read)
            connection.sendall(noise.frame(b"\x01sonoff-s60\x0012:34:56:AB:CD:EF\x00"))
            plug = noise.Handshake(self.KEY, b"NoiseAPIInit\x00\x00", initiator=False)
            try:
                plug.read_message(first[1:])
            except noise.NoiseError:
                connection.sendall(noise.frame(b"\x01" + noise.WRONG_KEY_REASON.encode()))
                return
            connection.sendall(noise.frame(b"\x00" + plug.write_message()))
            sending, receiving = plug.split()
            while True:
                message = receiving.decrypt(noise.read_frame(read))
                for answer, payload in answers.get(int.from_bytes(message[:2], "big"), []):
                    connection.sendall(noise.frame(sending.encrypt(
                        answer.to_bytes(2, "big") + len(payload).to_bytes(2, "big") + payload)))

        def run():
            for turn in range(2):
                try:
                    connection, _ = server.accept()
                except OSError:
                    return
                with connection:
                    try:
                        if plain_first and turn == 0:
                            connection.recv(4096)
                            connection.sendall(b"\x00Bad indicator byte")
                        elif plain_first:
                            plainly(connection)
                        else:
                            sealed(connection)
                    except (ConnectionError, OSError, AssertionError):
                        pass

        threading.Thread(target=run, daemon=True).start()
        return server.getsockname()[1]

    def test_with_its_key_the_action_gives_the_details(self):
        port = self.serve()
        self.assertEqual(af.esphome.api_wifi_details("127.0.0.1", port, timeout=3.0, key=self.KEY),
                         ("Home-2G", "pw"))

    def test_with_its_key_the_dump_gives_the_signal_and_network(self):
        port = self.serve()
        self.assertEqual(af.esphome.api_wifi("127.0.0.1", port, timeout=3.0, key=self.KEY),
                         {"signal": -61, "ssid": "Home-2G"})

    def test_without_its_key_or_with_another_it_says_so(self):
        port = self.serve()
        with self.assertRaises(af.esphome.ApiLocked) as locked:
            af.esphome.api_wifi_details("127.0.0.1", port, timeout=3.0)
        self.assertFalse(locked.exception.wrong)
        port = self.serve()
        with self.assertRaises(af.esphome.ApiLocked) as locked:
            af.esphome.api_wifi_details("127.0.0.1", port, timeout=3.0, key=bytes(32))
        self.assertTrue(locked.exception.wrong)
        self.assertIsNone(af.esphome.api_signal("127.0.0.1", self.serve(), timeout=3.0))  # just none

    def test_a_key_given_for_a_plain_api_is_not_used(self):
        port = self.serve(plain_first=True)
        self.assertEqual(af.esphome.api_wifi_details("127.0.0.1", port, timeout=3.0, key=self.KEY),
                         ("Home-2G", "pw"))

    def test_access_says_what_it_takes(self):
        self.assertEqual(af.esphome.api_access("127.0.0.1", self.KEY, self.serve()), af.esphome.API_OPEN)
        self.assertEqual(af.esphome.api_access("127.0.0.1", None, self.serve()), af.esphome.API_KEY)
        self.assertEqual(af.esphome.api_access("127.0.0.1", bytes(32), self.serve()), af.esphome.API_KEY)
        self.assertEqual(af.esphome.api_access("127.0.0.1", self.KEY, self.serve(plain_first=True)),
                         af.esphome.API_PLAIN)


class SignalBeforeLeavingESPHomeTests(RouteTestCase):
    """X1 reads the signal before anything is written, where ESPHome gives it."""

    def x1(self, signal, carry_on=True):
        ctx = self.ctx(source="esphome")
        with mock.patch.object(af.esphome, "signal", return_value=signal), \
                mock.patch.object(af, "consent_to_leave") as consent, \
                mock.patch.object(af.detect, "identify", return_value="startup"), \
                mock.patch.object(af, "ask_yes_no", return_value=carry_on) as asked, \
                mock.patch.object(af, "section"):
            try:
                af.step_startup_from_esphome(ctx)
            except af.Abort as exc:
                return ctx, consent, asked, exc
        return ctx, consent, asked, None

    def test_a_weak_signal_is_asked_about_before_the_go_ahead(self):
        _, consent, asked, stopped = self.x1(-82, carry_on=False)
        self.assertRegex(str(stopped), "weak Wi-Fi signal")
        asked.assert_called_once()
        consent.assert_not_called()

    def test_a_good_signal_is_recorded(self):
        ctx, consent, asked, stopped = self.x1(-58)
        self.assertIsNone(stopped)
        self.assertEqual(ctx.state.data["captures"]["X1.signal_dbm"], -58)
        asked.assert_not_called()
        consent.assert_called_once()

    def test_an_esphome_without_the_sensor_goes_on_unasked(self):
        ctx, consent, asked, stopped = self.x1(None)
        self.assertIsNone(stopped)
        self.assertIsNone(ctx.state.data["captures"]["X1.signal_dbm"])
        asked.assert_not_called()
        consent.assert_called_once()


class FromAConvertedPlugTests(RouteTestCase):
    """A plug this tool converted, found on the network, offered the other two."""

    def settle(self, running, *, backup=True, given=None, answer=0):
        ctx = self.ctx(given=given, run=f"{running}-{given}-{backup}")
        with mock.patch.object(af, "find_current_firmware", return_value=running), \
                mock.patch.object(af, "sonoff_backup_exists", return_value=backup), \
                mock.patch.object(af, "ask_choice", return_value=answer) as asked:
            route = af.settle_route(ctx)
        return route, (asked.call_args.args[1] if asked.called else None)

    def test_from_tasmota_the_other_two_are_offered(self):
        (firmware, source), offered = self.settle("tasmota", answer=1)
        self.assertEqual(offered, ["ESPHome", "Sonoff's own firmware (back to eWeLink, from its backup)"])
        self.assertEqual((firmware, source), ("sonoff", "tasmota"))

    def test_without_its_backup_sonoff_is_not_offered(self):
        (firmware, _), offered = self.settle("esphome", backup=False)
        self.assertEqual(offered, ["Tasmota"])
        self.assertEqual(firmware, "tasmota")

    def test_the_firmware_it_already_runs_cannot_be_asked_for(self):
        with self.assertRaisesRegex(af.NothingToDo, "already runs ESPHome"):
            self.settle("esphome", given="esphome")
        with self.assertRaisesRegex(af.Abort, "no backup"):
            self.settle("tasmota", backup=False, given="sonoff")

    def test_a_plug_left_part_way_can_go_on_to_any_of_the_three(self):
        _, offered = self.settle("midway")
        self.assertEqual(len(offered), 3)

    def test_every_route_ends_by_checking_its_firmware(self):
        last = {"tasmota": "H1", "esphome": "I2", "sonoff": "R2"}
        for source in af.SOURCES:
            for firmware in af.FIRMWARES:
                if firmware == source or (source == "sonoff" and firmware == "sonoff"):
                    continue
                steps = [step for step, _, _ in af.steps_for(firmware, source)]
                self.assertEqual(steps[-1], last[firmware], (source, firmware))
                self.assertEqual(len(steps), len(set(steps)), (source, firmware))
                # The bootloader is written on every route, before any firmware goes
                # in - past the commit, by the run that got there (D5 comes before E3).
                self.assertTrue("D5" in steps or "L1" in steps or source == "committed",
                                (source, firmware))

    def test_past_the_commit_official_tasmota_goes_on_first(self):
        # Never the hub's steps: they need Sonoff's two slots, and Tasmota's
        # layout has one - the slot swap waits for a restart that never comes.
        for firmware in af.FIRMWARES:
            steps = [step for step, _, _ in af.steps_for(firmware, "committed")]
            self.assertEqual(steps[:4], ["A0", "A5", "F2", "F4"], firmware)
            if firmware != "tasmota":
                self.assertIn("H1", steps)
                self.assertEqual(steps[steps.index("H1") + 1], "L1", firmware)  # then on from Tasmota
            self.assertFalse({"X2", "D4", "E3"} & set(steps[:steps.index("H1")]), firmware)


class FromTheInstallerTests(RouteTestCase):
    def test_on_from_the_installer_it_needs_no_ewelink(self):
        # Left part way from Sonoff's firmware: the backup and D1 need only the
        # installer; the eWeLink steps, which a plug on it cannot answer, are not run.
        for firmware in af.FIRMWARES:
            steps = [step for step, _, _ in af.steps_for(firmware, "installer")]
            self.assertEqual(steps[:4], ["A0", "A5", "C3", "D1"], firmware)
            self.assertFalse({"A1", "A2", "C2"} & set(steps), firmware)

    def test_an_upload_to_the_installer_is_tried_again(self):
        # The installer selects itself before an upload writes its peer slot.
        self.assertIn("D1", af.RETRY_STEPS)


class AlreadyThereTests(RouteTestCase):
    def test_asking_for_what_it_runs_ends_the_run_done(self):
        ctx = self.ctx(given="esphome")
        with mock.patch.object(af, "find_current_firmware", return_value="esphome"):
            with self.assertRaisesRegex(af.NothingToDo, "already runs ESPHome"):
                af.settle_route(ctx)
        # Never offered back as an unfinished run.
        self.assertTrue(af.run_is_complete(Path(self.tmp.name) / "run"))


class ConfirmedOnlyAfterAMinuteTests(RouteTestCase):
    """ESPHome sent to the mover is confirmed only after a minute running."""

    def check(self, source, answers, probes=()):
        ctx = self.ctx(source=source, firmware="esphome")
        clock = iter(range(0, 100_000, 3))
        with mock.patch.object(af.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch.object(af.time, "sleep"), \
                mock.patch.object(af.esphome, "is_esphome", side_effect=list(answers)), \
                mock.patch.object(af.esphome_ota, "probe", side_effect=list(probes) or None), \
                mock.patch.object(af, "step_esphome_from_mover") as resent:
            return af.step_esphome_check(ctx), resent

    def test_from_tasmota_it_is_only_done_once_esphome_has_confirmed_itself(self):
        result, _ = self.check("tasmota", [True] * 40)
        self.assertEqual(result["resent"], 0)
        # Asked again until 70 s after it first answered: 3 s a step.
        self.assertGreaterEqual(result["seconds"], 0)

    def test_back_on_the_mover_it_is_sent_esphome_again(self):
        result, resent = self.check("tasmota", [False] + [True] * 40, probes=[object()])
        resent.assert_called_once()
        self.assertEqual(result["resent"], 1)

    def test_from_stock_nothing_is_waited_for(self):
        with mock.patch.object(af.esphome, "is_esphome", return_value=True) as asked:
            ctx = self.ctx(firmware="esphome")
            af.step_esphome_check(ctx)
        self.assertEqual(asked.call_count, 1)
