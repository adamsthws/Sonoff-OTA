"""What a plug on the network is running, from what it answers."""
import http.server
import threading
import unittest
from unittest import mock

from tests.autoflash.test_device_requests import LockedTasmota
from tools.autoflash import detect

TASMOTA_TABLE = "1" * 64


class FakeTasmota:
    def __init__(self, *, version, table, module="Sonoff S60TPG", safeboot=False, hostname="tasmota-ABCDEF-1234"):
        self.version, self.table, self.module = version, table, module
        self.safeboot, self.hostname = safeboot, hostname

    def __call__(self, host, timeout=None, password=None):
        return self

    def wants_password(self, timeout=None):
        return False

    def is_safeboot(self):
        return self.safeboot

    def command(self, command):
        if command == "Status 0":
            return {"Status": {"Module": self.module}, "StatusNET": {"Hostname": self.hostname},
                    "StatusFWR": {"Version": self.version}}
        if command == "Status 5":
            return {"StatusNET": {"Hostname": self.hostname}}
        return {"NAME": self.module}

    def sha_region(self, address, length, timeout=None):
        return self.table


class TasmotaTests(unittest.TestCase):

    def setUp(self):
        # No OpenBeken at these made-up addresses: asked, it is whatever is there on this network.
        patch = mock.patch.object(detect, "openbeken_plug", return_value=None)
        patch.start()
        self.addCleanup(patch.stop)
    def kind(self, fake):
        with mock.patch.object(detect, "Device", fake):
            found = detect._tasmota("192.168.0.137", 2.0)
        return found and found[0]

    def test_the_routes_own_tasmota_on_sonoffs_layout_is_part_way(self):
        fake = FakeTasmota(version="15.6.0.1(f5b34a2-bluetooth)", table=detect.SONOFF_TABLE_SHA256,
                           module="Generic")
        self.assertEqual(self.kind(fake), "hub")

    def test_the_routes_own_tasmota_on_the_interim_layout_is_part_way_too(self):
        # A commit stopped part way: the next step puts Sonoff's layout back.
        fake = FakeTasmota(version="15.6.0.1(f5b34a2-bluetooth)", table=detect.INTERIM_TABLE_SHA256,
                           module="Generic")
        self.assertEqual(self.kind(fake), "hub")

    def found_busy(self, fake):
        """What the search and identify make of `fake`, a plug this computer
        knows that answers as a Tasmota once its Berry is free."""
        neighbours = {"192.168.0.137": "12:34:56:ab:cd:ef"}
        with mock.patch.object(detect, "Device", fake), \
                mock.patch.object(detect.hostsys, "sweep"), \
                mock.patch.object(detect.hostsys, "neighbour_table", return_value=neighbours), \
                mock.patch.object(detect, "_port_open", return_value=True), \
                mock.patch.object(detect, "_is_installer", return_value=False), \
                mock.patch.object(detect, "answers_as_tasmota", return_value=True), \
                mock.patch.object(detect.esphome, "page", return_value="other"), \
                mock.patch.object(detect.esphome_ota, "probe", return_value=None):
            return (detect.converted_plugs("192.168.0.1", known_macs={"12:34:56:ab:cd:ef"}),
                    detect.identify("192.168.0.137"))

    def test_a_tasmota_too_busy_to_answer_is_asked_again(self):
        # Seen in the fault suite: its Berry held for 18 s by a phase whose
        # server Ctrl-C had stopped, the hub did not answer Status 0 in time,
        # was taken for official Tasmota without its template once it did,
        # and sent the way off Tasmota's layout.
        import http.client
        import urllib.error
        # Or its connection dropped as its Berry came free (seen at E3).
        for failure in (TimeoutError("timed out"), ConnectionResetError("reset"),
                        http.client.RemoteDisconnected("closed"), urllib.error.URLError(OSError("refused"))):
            fake = FakeTasmota(version="15.6.0.1(f5b34a2-bluetooth)", table=detect.SONOFF_TABLE_SHA256,
                               module="Generic")
            answers = fake.command

            def busy(command, failure=failure, answers=answers):
                if command == "Status 0":
                    raise failure
                return answers(command)
            fake.command = busy
            self.assertEqual(self.found_busy(fake), ([], None), failure)

    def test_a_page_that_answers_no_to_the_command_is_no_tasmota(self):
        import io
        import urllib.error
        fake = FakeTasmota(version="", table=None)

        def not_found(command):
            raise urllib.error.HTTPError("http://192.168.0.137/cm", 404, "Not Found", {}, io.BytesIO())
        fake.command = not_found
        self.assertIsNone(self.kind(fake))

    def test_the_routes_own_tasmota_too_busy_to_say_its_layout_is_asked_again(self):
        fake = FakeTasmota(version="15.6.0.1(f5b34a2-bluetooth)", table=None, module="Generic")

        def busy(address, length, timeout=None):
            raise TimeoutError("timed out")
        fake.sha_region = busy
        self.assertEqual(self.found_busy(fake), ([], None))
        # Official Tasmota, which may have no Berry to ask, is found as before.
        fake.version, fake.module = "15.6.0(release-tasmota32)", "Sonoff S60TPG"
        self.assertEqual(self.kind(fake), "tasmota")

    def test_official_tasmota_with_sonoffs_table_is_still_leaving_tasmota(self):
        # The table written, the restart into the mover not yet: leaving Tasmota
        # is done again, and finds its writes done - never taken as the hub.
        fake = FakeTasmota(version="15.6.0(release-tasmota32)", table=detect.SONOFF_TABLE_SHA256)
        self.assertEqual(self.kind(fake), "tasmota")

    def test_official_tasmota_counts_only_with_the_s60s_template(self):
        self.assertEqual(self.kind(FakeTasmota(version="15.6.0(release-tasmota32)", table=TASMOTA_TABLE)),
                         "tasmota")
        self.assertIsNone(self.kind(FakeTasmota(version="15.6.0(release-tasmota)", table=TASMOTA_TABLE,
                                                module="Jomarto SH0617")))

    def test_a_template_slow_to_answer_is_asked_again(self):
        fake = FakeTasmota(version="15.6.0(release-tasmota32)", table=TASMOTA_TABLE, module=0)
        answers = fake.command
        tries = []

        def busy_at_first(command):
            if command in ("Template", "Module"):
                tries.append(command)
                if len(tries) < 3:
                    raise TimeoutError("timed out")
                return {"Module": {"0": "Sonoff S60TPG"}}
            return answers(command)
        fake.command = busy_at_first
        self.assertEqual(self.kind(fake), "tasmota")
        self.assertEqual(tries, ["Template", "Template", "Module"])

    def test_a_set_up_s60_that_will_not_say_its_template_is_not_taken_for_one_not_set_up(self):
        # Seen once in the emulator: Tasmota busy answering the network sweep
        # did not say its template in time, the plug was taken for a Tasmota
        # not yet set up as an S60, and the run sent it the wrong way.
        fake = FakeTasmota(version="15.6.0(release-tasmota32)", table=TASMOTA_TABLE, module=0)
        answers = fake.command

        def no_template(command):
            if command in ("Template", "Module"):
                raise TimeoutError("timed out")
            return answers(command)
        fake.command = no_template
        neighbours = {"192.168.0.137": "12:34:56:ab:cd:ef"}
        with mock.patch.object(detect, "Device", fake), \
                mock.patch.object(detect.hostsys, "sweep"), \
                mock.patch.object(detect.hostsys, "neighbour_table", return_value=neighbours), \
                mock.patch.object(detect, "_port_open", return_value=True), \
                mock.patch.object(detect, "_is_installer", return_value=False), \
                mock.patch.object(detect.esphome, "page", return_value="other"):
            self.assertEqual(detect.converted_plugs("192.168.0.1", known_macs={"12:34:56:ab:cd:ef"}), [])
            self.assertIsNone(detect.identify("192.168.0.137"))

    def test_a_safeboot_is_taken_only_with_a_mac_this_computer_knows(self):
        # It answers no commands: nothing else tells this tool's startup
        # firmware from any Tasmota part way through an update.
        self.assertEqual(self.kind(FakeTasmota(version="", table="", safeboot=True)), "startup")
        fake = FakeTasmota(version="", table="", safeboot=True)
        neighbours = {"192.168.0.137": "12:34:56:ab:cd:ef", "192.168.0.99": "aa:bb:cc:dd:ee:ff"}
        with mock.patch.object(detect, "Device", fake), \
                mock.patch.object(detect.hostsys, "sweep"), \
                mock.patch.object(detect.hostsys, "neighbour_table", return_value=neighbours), \
                mock.patch.object(detect, "_port_open", return_value=True), \
                mock.patch.object(detect.esphome, "page", return_value="other"), \
                mock.patch.object(detect, "_is_installer", return_value=False):  # nothing real asked
            found = detect.converted_plugs("192.168.0.1", known_macs={"12:34:56:ab:cd:ef"})
        self.assertEqual([(f.host, f.firmware) for f in found], [("192.168.0.137", "midway")])

    def test_the_mover_is_taken_only_with_a_mac_this_computer_knows(self):
        # No page, only ESPHome's update service: as any ESPHome built without
        # a web server looks - seen on a network with four of them.
        neighbours = {"192.168.0.137": "12:34:56:ab:cd:ef", "192.168.0.99": "aa:bb:cc:dd:ee:ff"}
        with mock.patch.object(detect.hostsys, "sweep"), \
                mock.patch.object(detect.hostsys, "neighbour_table", return_value=neighbours), \
                mock.patch.object(detect, "_port_open", return_value=False), \
                mock.patch.object(detect.esphome_ota, "probe", return_value=object()):
            found = detect.converted_plugs("192.168.0.1", known_macs={"12:34:56:ab:cd:ef"})
        self.assertEqual([(f.host, f.name) for f in found], [("192.168.0.137", "the mover")])

    def test_a_converted_tasmota_is_found_with_its_kind(self):
        fake = FakeTasmota(version="15.6.0(release-tasmota32)", table=TASMOTA_TABLE)
        with mock.patch.object(detect, "Device", fake), \
                mock.patch.object(detect.hostsys, "sweep"), \
                mock.patch.object(detect.hostsys, "neighbour_table",
                                  return_value={"192.168.0.137": "12:34:56:ab:cd:ef"}), \
                mock.patch.object(detect, "_port_open", return_value=True), \
                mock.patch.object(detect.esphome, "page", return_value="other"), \
                mock.patch.object(detect, "_is_installer", return_value=False):  # nothing real asked
            found = detect.converted_plugs("192.168.0.1")
        self.assertEqual([(f.host, f.firmware, f.kind) for f in found],
                         [("192.168.0.137", "tasmota", "sonoff-s60")])
        self.assertTrue(found[0].describe().startswith("the device at 192.168.0.137"))

    def test_a_tasmota_not_yet_set_up_is_taken_only_with_a_mac_this_computer_knows(self):
        # Official Tasmota, put on by a run stopped before it gave it the S60's
        # template: any Tasmota could look like it.
        fake = FakeTasmota(version="15.6.0(release-tasmota32)", table=TASMOTA_TABLE, module="Generic")
        neighbours = {"192.168.0.137": "12:34:56:ab:cd:ef", "192.168.0.99": "aa:bb:cc:dd:ee:ff"}
        with mock.patch.object(detect, "Device", fake), \
                mock.patch.object(detect.hostsys, "sweep"), \
                mock.patch.object(detect.hostsys, "neighbour_table", return_value=neighbours), \
                mock.patch.object(detect, "_port_open", return_value=True), \
                mock.patch.object(detect.esphome, "page", return_value="other"), \
                mock.patch.object(detect, "_is_installer", return_value=False):  # nothing real asked
            found = detect.converted_plugs("192.168.0.1", known_macs={"12:34:56:ab:cd:ef"})
        self.assertEqual([(f.host, f.firmware) for f in found], [("192.168.0.137", "untemplated")])


class LoginPage(http.server.ThreadingHTTPServer):
    """Something else on the network with a login - a router's page, say:
    it wants a password for everything, and notes what it was sent."""
    daemon_threads = True

    def __init__(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.server.seen.append((self.path, self.headers.get("Authorization")))
                body = b"<html>Log in</html>"
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Router"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        super().__init__(("127.0.0.1", 0), Handler)
        self.seen: list[tuple[str, str | None]] = []
        threading.Thread(target=self.serve_forever, daemon=True).start()
        self.host = f"127.0.0.1:{self.server_address[1]}"

    def stop(self):
        self.shutdown()
        self.server_close()


class WebPasswordTests(unittest.TestCase):
    """A Tasmota with a web password says whether it is an S60 only once given
    it - and the password goes to nothing that has not asked for it."""

    def setUp(self):
        # No OpenBeken at these made-up addresses: asked, it is whatever is there on this network.
        patch = mock.patch.object(detect, "openbeken_plug", return_value=None)
        patch.start()
        self.addCleanup(patch.stop)

    def started(self, server):
        self.addCleanup(server.stop)
        return server

    def test_without_its_password_it_is_locked(self):
        plug = self.started(LockedTasmota())
        for password in (None, "not-it-9"):
            with self.assertRaises(detect.Locked):
                detect._tasmota(plug.host, 2.0, password)

    def test_given_it_an_s60s_tasmota_is_found(self):
        plug = self.started(LockedTasmota())
        self.assertEqual(detect._tasmota(plug.host, 2.0, LockedTasmota.PASSWORD),
                         ("tasmota", "tasmota-ABCDEF-1234", "sonoff-s60"))
        # Asked without it first: it went only once the plug had said it wanted one.
        self.assertIsNone(plug.seen[0][1])
        self.assertNotIn(LockedTasmota.PASSWORD, plug.seen[0][0])

    def test_nothing_else_on_the_network_is_sent_it(self):
        router = self.started(LoginPage())
        self.assertIsNone(detect._tasmota(router.host, 2.0, LockedTasmota.PASSWORD))
        self.assertTrue(router.seen)
        self.assertEqual([(path, authorization) for path, authorization in router.seen
                          if authorization or LockedTasmota.PASSWORD in path], [])

    def test_found_by_address_or_on_the_network_it_is_named_locked(self):
        neighbours = {"192.168.0.42": "12:34:56:ab:cd:ef"}
        with mock.patch.object(detect, "_tasmota", side_effect=detect.Locked("192.168.0.42")), \
                mock.patch.object(detect.esphome, "page", return_value="other"), \
                mock.patch.object(detect, "_port_open", return_value=True), \
                mock.patch.object(detect, "_is_installer", return_value=False), \
                mock.patch.object(detect.hostsys, "sweep"), \
                mock.patch.object(detect.hostsys, "neighbour_table", return_value=neighbours):
            self.assertEqual(detect.identify("192.168.0.42"), "locked")
            found = detect.converted_plugs("192.168.0.1")
        self.assertEqual([(f.host, f.firmware) for f in found], [("192.168.0.42", "locked")])
        self.assertIn("web password", found[0].describe())


class LockedESPHome(http.server.ThreadingHTTPServer):
    """ESPHome's page behind a web login (web_server's auth:), as
    web_server_idf.cpp asks for one: Basic - or Digest, with `digest` -
    realm "Login Required", the same words Tasmota's asks in.  Notes what
    it was sent."""
    daemon_threads = True
    USER, PASSWORD = "admin", "w3b-pass"
    PAGE = b'<!DOCTYPE html><html><body><esp-app></esp-app></body></html>'

    def __init__(self, *, digest=False):
        import base64
        import hashlib
        import secrets

        def md5(text):
            return hashlib.md5(text.encode()).hexdigest()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def allowed(self):
                given = self.headers.get("Authorization") or ""
                if not digest:
                    expected = base64.b64encode(f"{LockedESPHome.USER}:{LockedESPHome.PASSWORD}".encode())
                    return given == "Basic " + expected.decode()
                if not given.startswith("Digest "):
                    return False
                fields = dict(part.strip().split("=", 1) for part in given[7:].split(","))
                fields = {key: value.strip('"') for key, value in fields.items()}
                ha1 = md5(f"{fields.get('username')}:Login Required:{LockedESPHome.PASSWORD}")
                ha2 = md5(f"GET:{fields.get('uri')}")
                expected = md5(f"{ha1}:{fields.get('nonce')}:{fields.get('nc')}:{fields.get('cnonce')}:"
                               f"{fields.get('qop')}:{ha2}")
                return fields.get("username") == LockedESPHome.USER and fields.get("response") == expected

            def do_GET(self):
                self.server.seen.append((self.path, self.headers.get("Authorization")))
                if self.allowed():
                    body = LockedESPHome.PAGE if self.path == "/" else b'{"id":"switch-relay","value":true}'
                    self.send_response(200)
                else:
                    body = b"401 Unauthorized"
                    self.send_response(401)
                    self.send_header("WWW-Authenticate",
                                     f'Digest realm="Login Required", qop="auth", nonce="{secrets.token_hex(16)}", '
                                     f'opaque="{secrets.token_hex(16)}"' if digest
                                     else 'Basic realm="Login Required"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        super().__init__(("127.0.0.1", 0), Handler)
        self.seen: list[tuple[str, str | None]] = []
        threading.Thread(target=self.serve_forever, daemon=True).start()
        self.host = f"127.0.0.1:{self.server_address[1]}"

    def stop(self):
        self.shutdown()
        self.server_close()


class EsphomeLoginTests(unittest.TestCase):
    """An ESPHome with a web login says whether it is an S60 only once given
    it - and the login goes only to a page that asked for one, where ESPHome's
    update service answers too: Tasmota's page asks in the same words."""

    LOGIN = (LockedESPHome.USER, LockedESPHome.PASSWORD)

    def started(self, server):
        self.addCleanup(server.stop)
        return server

    def esphome(self, host, login, *, update_service=True):
        offer = object() if update_service else None
        with mock.patch.object(detect.esphome_ota, "probe", return_value=offer):
            return detect._esphome(host, 2.0, login)

    def test_without_its_login_it_says_it_has_one(self):
        for digest in (False, True):
            plug = self.started(LockedESPHome(digest=digest))
            self.assertEqual(self.esphome(plug.host, None), "esphome-login")
            self.assertEqual(self.esphome(plug.host, ("admin", "not-it-9")), "esphome-login")

    def test_given_it_an_s60s_esphome_is_found_basic_or_digest(self):
        for digest in (False, True):
            plug = self.started(LockedESPHome(digest=digest))
            self.assertEqual(self.esphome(plug.host, self.LOGIN), "esphome")
            # Asked without it first: it went only once the page had asked for it.
            self.assertIsNone(plug.seen[0][1])
            self.assertTrue(any(authorization for _, authorization in plug.seen))

    def test_a_page_asking_where_no_update_service_answers_is_not_sent_it(self):
        # Tasmota with a web password, or a router: not ESPHome's.
        router = self.started(LoginPage())
        self.assertIsNone(self.esphome(router.host, self.LOGIN, update_service=False))
        self.assertEqual([authorization for _, authorization in router.seen if authorization], [])

    def test_found_by_address_or_on_the_network_it_is_named(self):
        neighbours = {"192.168.0.42": "12:34:56:ab:cd:ef"}
        with mock.patch.object(detect.esphome, "page", return_value="login"), \
                mock.patch.object(detect.esphome_ota, "probe", return_value=object()), \
                mock.patch.object(detect, "_port_open", return_value=True), \
                mock.patch.object(detect.hostsys, "sweep"), \
                mock.patch.object(detect.hostsys, "neighbour_table", return_value=neighbours):
            self.assertEqual(detect.identify("192.168.0.42"), "esphome-login")
            found = detect.converted_plugs("192.168.0.1")
        self.assertEqual([(f.host, f.firmware) for f in found], [("192.168.0.42", "esphome-login")])
        self.assertIn("web login", found[0].describe())

    def test_before_this_it_was_taken_for_the_mover(self):
        # Its page shut, its update service open: what the mover looks like but
        # for the page - which the mover has none of.
        with mock.patch.object(detect.esphome, "page", return_value="login"), \
                mock.patch.object(detect.esphome_ota, "probe", return_value=object()), \
                mock.patch.object(detect, "_port_open", return_value=True):
            self.assertNotEqual(detect.identify("192.168.0.42"), "mover")


class InstallerTests(unittest.TestCase):
    """This tool's installer, left by a run part way from Sonoff's firmware,
    answers with its own page: a run carries on from it, not from eWeLink."""

    def setUp(self):
        # No OpenBeken at these made-up addresses: asked, it is whatever is there on this network.
        patch = mock.patch.object(detect, "openbeken_plug", return_value=None)
        patch.start()
        self.addCleanup(patch.stop)

    def test_its_page_names_it(self):
        with mock.patch.object(detect.esphome, "page", return_value="other"), \
             mock.patch.object(detect, "_port_open", return_value=True), \
             mock.patch.object(detect, "_is_installer", return_value=True):
            self.assertEqual(detect.identify("192.168.0.137"), "installer")

    def test_found_on_the_network_it_is_offered(self):
        neighbours = {"192.168.0.137": "12:34:56:ab:cd:ef"}
        with mock.patch.object(detect.hostsys, "sweep"), \
             mock.patch.object(detect.hostsys, "neighbour_table", return_value=neighbours), \
             mock.patch.object(detect, "_port_open", return_value=True), \
             mock.patch.object(detect.esphome, "page", return_value="other"), \
             mock.patch.object(detect, "_is_installer", return_value=True):
            found = detect.converted_plugs("192.168.0.1")
        self.assertEqual([(f.host, f.firmware) for f in found], [("192.168.0.137", "installer")])



class RenamedRelayTests(unittest.TestCase):
    """An ESPHome knows its kind by its comment as well as its relay's name: the
    adoption guide's example renames the relay, and it was taken for Sonoff's."""

    def found(self, relay, comment):
        with mock.patch.object(detect.esphome, "page", return_value="esphome"), \
                mock.patch.object(detect.esphome, "relay", return_value=relay), \
                mock.patch.object(detect.esphome, "title", return_value=("my plug", comment)):
            return detect._esphome("192.0.2.1", 1.0, None)

    def test_its_relay_renamed_it_is_known_by_its_comment(self):
        self.assertEqual(self.found(None, "Sonoff-S60"), "esphome")
        self.assertEqual(self.found(None, "Sonoff-S61"), "esphome")

    def test_with_its_relay_named_its_comment_is_not_asked_for(self):
        self.assertEqual(self.found(True, None), "esphome")

    def test_any_other_esphome_is_another_projects(self):
        self.assertEqual(self.found(None, None), "other")
        self.assertEqual(self.found(None, "a lamp of my own"), "other")


if __name__ == "__main__":
    unittest.main()


class OtherNetworksTests(unittest.TestCase):
    """The network this computer's internet goes through is searched first; the
    others only if no plug is there, then side by side with it again."""

    def search(self, plugs_after_sweeps, others=(("192.168.60.10", 24), ("10.0.0.5", 24), ("10.0.0.7", 24),
                                                 ("127.0.0.1", 8))):
        sweeps = []
        found = detect.Found("192.168.60.50", "tasmota", "12:34:56:ab:cd:ef")

        def sweep(subnet_of):
            sweeps.append(subnet_of)
            return {}

        def ask(*_args):
            return [found] if len(sweeps) >= plugs_after_sweeps else []

        lan = mock.Mock(return_value=dict(others))
        with mock.patch.object(detect.hostsys, "sweep", side_effect=sweep), \
                mock.patch.object(detect, "_ask_neighbours", side_effect=ask):
            result = detect.converted_plugs("10.0.0.2", others=lan)
        return result, sweeps, lan

    def test_a_plug_on_the_default_network_needs_no_other(self):
        result, sweeps, lan = self.search(plugs_after_sweeps=1)
        self.assertEqual([plug.host for plug in result], ["192.168.60.50"])
        self.assertEqual(sweeps, ["10.0.0.2"])
        lan.assert_not_called()

    def test_otherwise_every_network_is_swept_at_once_the_default_again(self):
        result, sweeps, lan = self.search(plugs_after_sweeps=2)
        self.assertEqual([plug.host for plug in result], ["192.168.60.50"])
        # One sweep of every network side by side, each network once; not loopback.
        self.assertEqual(sweeps, ["10.0.0.2", ["10.0.0.2", "192.168.60.10"]])

    def test_with_no_other_network_there_is_no_second_sweep(self):
        result, sweeps, _ = self.search(plugs_after_sweeps=9, others=(("10.0.0.9", 24), ("169.254.1.2", 16)))
        self.assertEqual((result, sweeps), ([], ["10.0.0.2"]))

    def test_a_wider_network_holding_the_default_one_is_no_other(self):
        # The default address on the computer's /22: one network, swept already.
        result, sweeps, _ = self.search(plugs_after_sweeps=9, others=(("10.0.1.7", 22),))
        self.assertEqual((result, sweeps), ([], ["10.0.0.2"]))

    def test_a_plug_the_sweep_saw_is_asked_though_the_table_dropped_it(self):
        asked = []
        with mock.patch.object(detect.hostsys, "sweep", return_value={"10.0.0.37": "12:34:56:ab:cd:ef"}), \
                mock.patch.object(detect.hostsys, "neighbour_table", return_value={}), \
                mock.patch.object(detect, "_port_open", side_effect=lambda host, *_: asked.append(host) and False):
            detect.converted_plugs("10.0.0.2")
        self.assertEqual(asked, ["10.0.0.37"])
