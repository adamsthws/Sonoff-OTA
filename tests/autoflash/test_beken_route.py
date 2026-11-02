"""The Beken routes: every direction one move, the plug's own store never
written, the backup after the first conversion, through OpenBeken."""
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.autoflash.test_openbeken import FakeOpenBeken
from tools.autoflash import detect, errors, handover, kinds, openbeken, plugs, routes, state
from tools.autoflash.routes import beken
from tools.autoflash.routes.beken import arrived, images, onward, stock
from tools.beken import fota_wrap, rbl, uf2
from tools.ewelink import ota_push
from tools.ewelink.get_device_key import EwelinkError

S61 = kinds.SONOFF_S61
MAC = "12:34:56:ab:cd:ef"


class RouteTests(unittest.TestCase):
    def test_every_direction_is_one_move_and_a_check(self):
        for firmware, source, expected in (
            ("openbeken", "sonoff", ["A0", "A1", "A3", "B1", "B2", "K1", "B4"]),
            ("esphome", "sonoff", ["A0", "A1", "A3", "B1", "B2", "K2", "B4"]),
            ("esphome", "openbeken", ["A0", "O1", "K2", "B4"]),
            ("sonoff", "openbeken", ["A0", "B4", "O2", "K3"]),
            ("openbeken", "esphome", ["A0", "E1", "K1", "B4"]),
            ("sonoff", "esphome", ["A0", "E2", "K3"]),
        ):
            with self.subTest(firmware=firmware, source=source):
                self.assertEqual([s for s, _, _ in routes.steps_for(firmware, source, beken)], expected)
        self.assertEqual(beken.steps_for("esphome", "esphome"), [])
        with self.assertRaises(ValueError):
            beken.steps_for("esphome", "midway")

    def test_the_point_of_no_return_is_the_push(self):
        self.assertEqual(beken.POINT_OF_NO_RETURN, "B2")
        self.assertIn("B2", beken.ATTEMPTED_EVIDENCE)
        for step in beken.DESTRUCTIVE_STEPS:
            self.assertIn(step, beken.ATTEMPTED_EVIDENCE, step)
        self.assertFalse(beken.DESTRUCTIVE_STEPS & beken.RETRY_STEPS)

    def test_an_s61_run_takes_these_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = state.RunState.open(Path(tmp) / "run")
            run.set_input("plug_kind", "sonoff-s61")
            self.assertIs(routes.family_of(run), beken)
            run.set_input("firmware", "openbeken")
            self.assertEqual([s for s, _, _ in routes.steps_of(run)][-2:], ["K1", "B4"])


def a_context(tmp, **inputs):
    run = state.RunState.open(Path(tmp) / "run")
    for key, value in {"plug_kind": "sonoff-s61", "plug_ip": "127.0.0.1", "plug_mac": MAC, **inputs}.items():
        run.set_input(key, value)
    return SimpleNamespace(state=run, dry_run=False, finder=SimpleNamespace(poll=lambda: None),
                           esphome_password=None, esphome_key=None, esphome_firmware=None,
                           args=SimpleNamespace(no_backup=False), consented=True,
                           listen_ip=lambda: "192.168.0.2", email=lambda: "owner@example.com",
                           ewelink_env=lambda: {"EWELINK_PASSWORD": "pw"}, account_plug=None,
                           account_devices=lambda: None,
                           # OpenBeken answering Tasmota's Status 11, its Wi-Fi signal in it.
                           device=SimpleNamespace(command=lambda cmd: {"StatusSTS": {"Wifi": {"Signal": -60}}}))


class ImagesTests(unittest.TestCase):
    def test_the_published_images_are_the_ones_pinned(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp)
            for firmware in ("openbeken", "esphome"):
                data, version = images.image(ctx, firmware)
                self.assertEqual(rbl.header(data).name, "app", firmware)
                self.assertEqual(version, images.PINS["sonoff-s61"][firmware]["version"])
            with self.assertRaisesRegex(errors.Abort, "no tasmota image"):
                images.image(ctx, "tasmota")

    def test_a_changed_file_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp)
            pinned = dict(images.PINS["sonoff-s61"]["openbeken"], sha256="0" * 64)
            with mock.patch.dict(images.PINS["sonoff-s61"], {"openbeken": pinned}):
                with self.assertRaisesRegex(errors.Abort, "not the file firmware.json pins"):
                    images.image(ctx, "openbeken")

    def test_an_esphome_built_by_hand_is_checked_as_an_rt_ota_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "own.rbl"
            own.write_bytes(rbl.pack(b"an esphome", version="26.10.30-bk7238"))
            ctx = a_context(tmp)
            ctx.esphome_firmware = own
            with mock.patch.object(images.output, "say"):
                self.assertEqual(images.image(ctx, "esphome"), (own.read_bytes(), "26.10.30-bk7238"))
            own.write_bytes(b"not an image")
            with self.assertRaisesRegex(errors.Abort, "not an rt_ota image"):
                images.image(ctx, "esphome")

    def test_the_stock_firmware_comes_from_the_public_dump_fetched_once(self):
        app = b"stock app" * 2000
        header = rbl.Header(algo=0x402, timestamp=5, name="app", version="1.0.2", sn=b"sn", body_crc32=0,
                            raw_hash=rbl.fnv1a32(app), size_raw=len(app), size_package=0).pack()
        logical = bytearray(b"\xff" * (0x25FFA0 + 200))
        logical[0x10000:0x10000 + len(app)] = app
        logical[0x25FFA0:0x25FFA0 + 96] = header
        dump = b"".join(bytes(logical[i:i + 32]).ljust(32, b"\xff") + b"\0\0" for i in range(0, len(logical), 32))
        fetched = []

        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                fetched.append(1)
                return dump

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(images, "DUMPS", Path(tmp)), \
                mock.patch.dict(images.PUBLIC_DUMPS, {"sonoff-s61": ("http://example.test/dump.bin",
                                                                      hashlib.sha256(dump).hexdigest())}), \
                mock.patch.object(images.urllib.request, "urlopen", return_value=Reply()), \
                mock.patch.object(images.output, "say"):
            packed, version = images.stock_rbl(S61)
            self.assertEqual((rbl.unpack(packed), version), (app, "1.0.2"))
            packed_again, _ = images.stock_rbl(S61)  # from the file kept, not fetched again
            self.assertEqual(packed_again, packed)
            self.assertEqual(fetched, [1])
            # A dump that is not the one known is refused, fetched or kept.
            (Path(tmp) / "dump.bin").write_bytes(b"something else")
            with mock.patch.dict(images.PUBLIC_DUMPS, {"sonoff-s61": ("http://example.test/dump.bin", "0" * 64)}):
                with self.assertRaisesRegex(errors.Abort, "hash differs"):
                    images.stock_rbl(S61)


class FromStockTests(unittest.TestCase):
    DEVICE = {"deviceid": "1001234abc", "apikey": "k", "online": True, "productModel": "S61STPF",
              "extra": {"model": "SN-BK7238-S61-01"}, "params": {"fwVersion": "1.0.2"}}

    def test_b1_takes_the_plugs_identity_from_its_account_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp)
            ctx.account_devices = lambda: [{"deviceid": "other"}, self.DEVICE]
            plugs_dir = Path(tmp) / "plugs"
            with mock.patch.object(plugs, "PLUGS", plugs_dir), mock.patch.object(plugs, "CAPTURES", Path(tmp)):
                plugs.remember_plug(MAC, ewelink_id="1001234abc")
                with mock.patch.object(stock.prompts, "section"):
                    result = stock.step_identify(ctx)
            self.assertEqual(result, {"stock_version": "1.0.2", "model": "SN-BK7238-S61-01", "product": "S61STPF",
                                      "signal_dbm": None})
            self.assertEqual(ctx.state.input("stock_version"), "1.0.2")
            # Another model on the account is never the plug.
            ctx.account_devices = lambda: [dict(self.DEVICE, extra={"model": "SN-ESP32-MINIR4"})]
            with mock.patch.object(plugs, "PLUGS", plugs_dir), mock.patch.object(plugs, "CAPTURES", Path(tmp)):
                with self.assertRaisesRegex(errors.Abort, "does not convert"):
                    stock.step_identify(ctx)

    def test_b1_reads_the_wifi_signal_the_device_gave_ewelink_and_asks_about_a_weak_one(self):
        for rssi, asked in ((-61, False), (-85, True)):
            with tempfile.TemporaryDirectory() as tmp:
                ctx = a_context(tmp)
                device = dict(self.DEVICE, params={"fwVersion": "1.0.2", "rssi": rssi})
                ctx.account_devices = lambda device=device: [device]
                with mock.patch.object(plugs, "PLUGS", Path(tmp) / "plugs"), mock.patch.object(plugs, "CAPTURES", Path(tmp)):
                    plugs.remember_plug(MAC, ewelink_id="1001234abc")
                    with mock.patch.object(stock.prompts, "section"), \
                            mock.patch.object(stock.join.prompts, "section"), \
                            mock.patch.object(stock.join.output, "transient_begin"), \
                            mock.patch.object(stock.join.output, "transient_end"), \
                            mock.patch.object(stock.join.prompts, "ask_yes_no", return_value=False) as question:
                        if asked:
                            with self.assertRaisesRegex(errors.Abort, "weak Wi-Fi signal; nothing was written"):
                                stock.step_identify(ctx)
                        else:
                            self.assertEqual(stock.step_identify(ctx)["signal_dbm"], rssi)
                self.assertEqual(question.called, asked)
        self.assertIsNone(stock.account_signal({"params": {"rssi": "strong"}}))

    def test_b2_wraps_the_image_and_sends_it_through_ewelink(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, stock_version="1.0.2", firmware="openbeken")
            ctx.account_plug = SimpleNamespace(device_id="1001234abc")
            ctx.account_devices = lambda: [self.DEVICE]
            sent = {}

            def deliver(**kwargs):
                sent.update(kwargs)
                kwargs["check_device"](self.DEVICE, "SN-BK7238-S61-01")
                return ota_push.Delivery(sent=True, complete=True, bytes_served=len(kwargs["firmware"]), seconds=9.5)

            with mock.patch.object(stock.firewall, "choose_port", return_value=8088), \
                    mock.patch.object(stock.firewall, "ensure_firewall"), \
                    mock.patch.object(stock.prompts, "warning_box"), \
                    mock.patch.object(stock.prompts, "require_word", return_value=True), \
                    mock.patch.object(stock.output, "say"), \
                    mock.patch.object(ota_push, "deliver", side_effect=deliver):
                result = stock.step_push(ctx)
            image, version = images.image(ctx, "openbeken")
            self.assertEqual(result["firmware"], "openbeken")
            self.assertEqual(result["version"], version)
            self.assertEqual(sent["firmware"], fota_wrap.wrap(image, name=S61.wrapper_model, version=images.FOTA_VERSION))
            self.assertEqual((sent["device_id"], sent["device_ip"], sent["listen_ip"], sent["listen_port"]),
                             ("1001234abc", "127.0.0.1", "192.168.0.2", 8088))
            self.assertEqual(sent["names"], ("user1.bin",))
            self.assertIs(sent["live"], False)   # what it fetches is logged, never drawn over the stage line
            # The plug must be as it was at B1: another model, or firmware, is refused before sending.
            with self.assertRaisesRegex(EwelinkError, "1.0.3"):
                sent["check_device"](dict(self.DEVICE, params={"fwVersion": "1.0.3"}), "SN-BK7238-S61-01")

    def test_b2_not_confirmed_writes_nothing_and_a_cut_off_delivery_stays_attempted(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, stock_version="1.0.2", firmware="esphome")
            ctx.account_plug = SimpleNamespace(device_id="1001234abc")
            ctx.account_devices = lambda: [self.DEVICE]
            common = [mock.patch.object(stock.firewall, "choose_port", return_value=8088),
                      mock.patch.object(stock.firewall, "ensure_firewall"),
                      mock.patch.object(stock.prompts, "warning_box"), mock.patch.object(stock.output, "say")]
            for patch in common:
                patch.start()
                self.addCleanup(patch.stop)
            with mock.patch.object(stock.prompts, "require_word", return_value=False):
                with self.assertRaises(errors.NothingWritten):
                    stock.step_push(ctx)
            with mock.patch.object(stock.prompts, "require_word", return_value=True), \
                    mock.patch.object(ota_push, "deliver", return_value=ota_push.Delivery(sent=False, error="login failed")):
                with self.assertRaisesRegex(errors.Abort, "was not sent: login failed"):
                    stock.step_push(ctx)
            with mock.patch.object(stock.prompts, "require_word", return_value=True), \
                    mock.patch.object(ota_push, "deliver",
                                      return_value=ota_push.Delivery(sent=True, complete=False, bytes_served=100)):
                with self.assertRaisesRegex(OSError, "fetched 100 of"):
                    stock.step_push(ctx)
            # Turned down while an update cut off part way holds the plug's
            # updater (seen in the fault suite): switching it off and on frees it.
            refused = ota_push.Delivery(sent=True, complete=False,
                                        error="Cloud rejected upgrade command: error 410")
            with mock.patch.object(stock.prompts, "require_word", return_value=True), \
                    mock.patch.object(ota_push, "deliver", return_value=refused):
                with self.assertRaisesRegex(errors.Abort, "Switch the device off and on"):
                    stock.step_push(ctx)

    def test_the_evidence_of_a_push_is_what_the_plug_answers_as(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="openbeken")
            for running, verdict in (("openbeken", True), ("sonoff", False), (None, None), ("esphome", None)):
                with mock.patch.object(detect, "identify", return_value=running):
                    self.assertEqual(stock.evidence_pushed(ctx)[0], verdict, running)


class OnwardTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeOpenBeken()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        for patch in (mock.patch.object(onward.firewall, "choose_port", return_value=8088),
                      mock.patch.object(onward.firewall, "ensure_firewall"),
                      mock.patch.object(onward.output, "say"), mock.patch.object(onward.output, "progress"),
                      mock.patch.object(onward.output, "progress_clear")):
            patch.start()
            self.addCleanup(patch.stop)

    def served(self, ctx, step, fetched):
        """Run `step` with the firmware server stood in for: what the plug was told to fetch."""
        class Server:
            def __init__(self, address, image, device_ip, say=print, names=("user1.bin", "user2.bin"), live=None):
                self.image = image
                fetched["image"] = image
                fetched["names"] = names
                fetched["live"] = live

            def serve_forever(self):
                pass

            def unique_bytes_served(self):
                return len(self.image)

            def shutdown(self):
                pass

            def server_close(self):
                pass

        with mock.patch.object(ota_push, "FirmwareServer", Server):
            return step(ctx)

    def test_every_direction_checks_the_wifi_signal_before_the_go_ahead(self):
        # OpenBeken's as Tasmota gives it, ESPHome's from its sensor: a weak one asked about first.
        order = []
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, plug_ip="192.0.2.50", firmware="esphome", source="openbeken")
            ctx.device = SimpleNamespace(command=lambda cmd: {"StatusSTS": {"Wifi": {"Signal": -85}}})
            with mock.patch.object(onward.join, "warn_if_weak_signal",
                                   side_effect=lambda ctx, signal, then: order.append(("weak?", signal))), \
                    mock.patch.object(onward.handover, "consent_to_leave",
                                      side_effect=lambda ctx, what: order.append(("go-ahead", what))), \
                    mock.patch.object(onward, "serve_to_plug", return_value=1.0), \
                    mock.patch.object(onward.join.output, "say"):
                self.assertEqual(onward.step_openbeken_to_esphome(ctx)["signal_dbm"], -85)
                with mock.patch.object(onward.esphome, "signal", return_value=-70), \
                        mock.patch.object(onward.handover, "send_as_esphome_update", return_value=1.0):
                    self.assertEqual(onward.step_esphome_to_openbeken(ctx)["signal_dbm"], -70)
        self.assertEqual(order, [("weak?", -85), ("go-ahead", "OpenBeken"), ("weak?", -70), ("go-ahead", "ESPHome")])

    def test_openbeken_fetches_esphome_by_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, plug_ip=self.server.host, firmware="esphome", source="openbeken")
            fetched = {}
            result = self.served(ctx, onward.step_openbeken_to_esphome, fetched)
            image, version = images.image(ctx, "esphome")
            self.assertEqual(fetched["image"], image)
            self.assertEqual(result["version"], version)
            self.assertEqual(self.server.commands, ["ota_http http://192.168.0.2:8088/firmware.rbl"])
            # Served under the name the plug is sent to fetch (it was once 404).
            self.assertIn("firmware.rbl", fetched["names"])
            # Its progress logged, never drawn over the stage line.
            self.assertIs(fetched["live"], False)

    def test_an_openbeken_that_asks_for_nothing_is_restarted_and_asked_again(self):
        # Its last download cut off - this computer stopped mid-way - OpenBeken
        # refuses another until it restarts (seen in the fault suite).
        asked = []

        class Server:
            def __init__(self, address, image, device_ip, say=print, names=(), live=None):
                self.image = image

            def serve_forever(self):
                pass

            def unique_bytes_served(self):
                return len(self.image) if len(asked) > 1 else 0

            def shutdown(self):
                pass

            def server_close(self):
                pass

        restarts = []
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ota_push, "FirmwareServer", Server), \
                mock.patch.object(onward, "FIRST_BYTE_WAIT", 0.0), mock.patch.object(onward.time, "sleep"):
            ctx = a_context(tmp, plug_ip=self.server.host, firmware="esphome", source="openbeken")
            onward.serve_to_plug(ctx, b"image", "ESPHome", asked.append,
                                 unstick=lambda: restarts.append(1) or True)
        self.assertEqual((len(asked), len(restarts)), (2, 1))

    def test_openbeken_fetches_sonoffs_firmware_through_the_modules_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, plug_ip=self.server.host, firmware="sonoff", source="openbeken")
            fetched = {}
            with mock.patch.object(images, "stock_rbl", return_value=(b"RBL stock", "1.0.2")):
                result = self.served(ctx, onward.step_openbeken_to_sonoff, fetched)
            self.assertEqual((fetched["image"], result["version"]), (b"RBL stock", "1.0.2"))
            self.assertEqual(self.server.commands, ["S61RevertToStock http://192.168.0.2:8088/firmware.rbl"])
            self.assertIn("firmware.rbl", fetched["names"])
            # An OpenBeken without the module cannot check where the image would go: refused.
            self.server.unknown.add("S61RevertToStock")
            with mock.patch.object(images, "stock_rbl", return_value=(b"RBL stock", "1.0.2")):
                with self.assertRaisesRegex(errors.Abort, "no S61RevertToStock"):
                    self.served(ctx, onward.step_openbeken_to_sonoff, fetched)

    def test_esphome_is_sent_a_uf2_on_libretinys_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="openbeken", source="esphome")
            with mock.patch.object(handover, "send_as_esphome_update", return_value=4.0) as sent:
                result = onward.step_esphome_to_openbeken(ctx)
            image, version = images.image(ctx, "openbeken")
            data, what = sent.call_args.args[1], sent.call_args.args[2]
            self.assertEqual(uf2.image_in(data), ("download", image))
            self.assertEqual(sent.call_args.kwargs["port"], 8892)
            self.assertEqual((result["seconds"], result["version"]), (4.0, version))
            self.assertIn(version, what)
            with mock.patch.object(images, "stock_rbl", return_value=(b"RBL stock", "1.0.2")), \
                    mock.patch.object(handover, "send_as_esphome_update", return_value=4.0) as sent:
                onward.step_esphome_to_sonoff(ctx)
            self.assertEqual(uf2.image_in(sent.call_args.args[1]), ("download", b"RBL stock"))

    def test_the_go_ahead_comes_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="esphome", source="openbeken")
            ctx.consented = False
            with mock.patch.object(handover.prompts, "warning_box"), \
                    mock.patch.object(handover.prompts, "require_word", return_value=False):
                with self.assertRaises(errors.NothingWritten):
                    onward.step_openbeken_to_esphome(ctx)
            self.assertEqual(self.server.commands, [])


class StillInstalledTests(unittest.TestCase):
    """A power cut while the image is written leaves the plug on the firmware
    it was leaving, its step recorded done (seen in the fault suite)."""

    def judge(self, *answers):
        with mock.patch.object(onward, "evidence_installed", side_effect=list(answers)), \
                mock.patch.object(onward.time, "sleep"), \
                mock.patch.object(onward.time, "monotonic", side_effect=[0, 0, 10, 100, 200]):
            return onward.still_installed(SimpleNamespace(), wait=60)

    def test_still_on_the_firmware_it_left_is_doubted(self):
        ok, message = self.judge((False, "The plug still answers as OpenBeken: the image was not applied."))
        self.assertFalse(ok)
        self.assertEqual(message, "the plug still answers as OpenBeken: the image was not applied")

    def test_on_the_new_firmware_it_stands(self):
        self.assertTrue(self.judge((True, "The plug answers as ESPHome."))[0])

    def test_a_plug_not_answering_yet_is_waited_for_then_left_be(self):
        waiting = (None, "The plug does not answer as the new firmware yet.")
        self.assertTrue(self.judge(waiting, waiting, waiting)[0])

    def test_the_onward_steps_are_run_again_unasked(self):
        self.assertEqual(set(beken.STEP_VERIFIERS), {"O1", "O2", "E1", "E2"})
        self.assertEqual(beken.REDO_STEPS, set(beken.STEP_VERIFIERS))
        self.assertEqual(routes.espressif.REDO_STEPS, set())


class ArrivedTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeOpenBeken()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        for patch in (mock.patch.object(arrived.output, "say"), mock.patch.object(arrived.output, "progress"),
                      mock.patch.object(arrived.output, "progress_clear"), mock.patch.object(arrived.output, "tell"),
                      mock.patch.object(openbeken.time, "sleep")):
            patch.start()
            self.addCleanup(patch.stop)

    def test_k1_finds_the_openbeken_that_says_it_is_on_an_s61(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, plug_ip=self.server.host)
            result = arrived.step_openbeken_check(ctx)
            self.assertEqual((result["version"], result["name"]), ("1.0.0", "openbeken-abcdef"))
            self.assertEqual(ctx.state.input("device_name"), "openbeken-abcdef")
            with mock.patch.object(openbeken, "info", return_value=None), \
                    mock.patch.object(arrived.join.time, "sleep"), \
                    mock.patch.object(arrived.prompts, "UNATTENDED", True), \
                    mock.patch.object(arrived.join.time, "monotonic", side_effect=iter(range(0, 10000, 100))):
                with self.assertRaisesRegex(errors.Abort, "has not answered.*switch the device off and on"):
                    arrived.step_openbeken_check(ctx, wait=10)

    def not_back_then_back(self, ctx, choices, *, switch=None):
        """K2 on a plug that does not answer until it has been switched off and
        on - by the person, or by the tool where the emulator says how - with
        `choices` the answers given at the question; what it offered."""
        offered = []
        looks = []

        def look(*args, **kwargs):
            looks.append(1)
            if len(looks) == 1:
                raise errors.Abort("did not answer")
            return True

        def choose(question, options):
            offered.append(list(options))
            answer = choices.pop(0)
            if answer == "q":
                raise arrived.prompts.QuitRun("stopped at an operator prompt")
            return answer

        env = {arrived.EMULATED_POWER: str(switch)} if switch else {}
        with mock.patch.object(arrived.join, "wait_for", side_effect=look), \
                mock.patch.object(arrived.esphome, "calibration_state", return_value=None), \
                mock.patch.object(arrived.prompts, "ask_choice", side_effect=choose), \
                mock.patch.object(arrived.prompts, "section"), \
                mock.patch.object(arrived.output, "transient_begin"), mock.patch.object(arrived.output, "transient_end"), \
                mock.patch.object(arrived.time, "sleep"), mock.patch.dict(arrived.os.environ, env, clear=False), \
                mock.patch.object(plugs, "PLUGS", ctx.state.path.parent / "plugs"):
            if not switch:
                arrived.os.environ.pop(arrived.EMULATED_POWER, None)
            arrived.step_esphome_check(ctx)
        return offered, len(looks)

    def test_an_attended_run_waits_for_the_plug_switched_off_and_on_then_looks_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, plug_ip="192.0.2.50")
            offered, looks = self.not_back_then_back(ctx, [0])
            self.assertEqual((len(offered[0]), looks), (1, 2))   # no emulator: one choice, and q
            # Counted for the report, by whom.
            self.assertEqual(ctx.state.input("power_cycles"), {"by hand": 1})

    def test_in_the_emulator_the_tool_can_switch_the_plug_off_and_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, plug_ip="192.0.2.50")
            switch = Path(tmp, "power")
            switch.write_text("on\n", encoding="utf-8")
            written = []
            real_write = Path.write_text

            def write_text(path, text, *args, **kwargs):
                if path == switch:
                    written.append(text.strip())
                return real_write(path, text, *args, **kwargs)

            with mock.patch.object(Path, "write_text", write_text):
                offered, looks = self.not_back_then_back(ctx, [1], switch=switch)
            self.assertEqual(len(offered[0]), 2)
            self.assertEqual((written, looks), (["off", "on"], 2))
            self.assertEqual(switch.read_text(encoding="utf-8"), "on\n")

    def test_q_at_the_question_stops_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, plug_ip="192.0.2.50")
            with self.assertRaises(arrived.prompts.QuitRun):
                self.not_back_then_back(ctx, ["q"])

    def test_b4_backs_up_the_plugs_own_data_with_sonoffs_firmware_beside_it(self):
        from tests.autoflash.test_openbeken import FLASH
        region = FLASH[:0x20000] if len(FLASH) >= 0x20000 else FLASH
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, plug_ip=self.server.host, stock_version="1.0.2")
            with mock.patch.object(plugs, "PLUGS", Path(tmp) / "plugs"), mock.patch.object(plugs, "CAPTURES", Path(tmp)), \
                    mock.patch.object(plugs, "BEKEN_REGION", (0x3E0000, len(region))), \
                    mock.patch.object(arrived.backup, "factory_mac", return_value=MAC), \
                    mock.patch.object(images, "stock_rbl", return_value=(b"RBL stock", "1.0.2")):
                result = arrived.step_backup(ctx)
                self.assertEqual(result, {"outcome": "saved", "stage_outcome": "complete"})
                found = plugs.find_backup(MAC)
                self.assertEqual((found.family, found.version), ("beken", "1.0.2"))
                self.assertEqual((found.directory / plugs.BEKEN_BACKUP_BIN).read_bytes(), region)
                self.assertEqual((found.directory / "stock-app-1.0.2.rbl").read_bytes(), b"RBL stock")
                manifest = json.loads((found.directory / plugs.BEKEN_MANIFEST).read_text())
                self.assertEqual(manifest["regions"][plugs.BEKEN_BACKUP_BIN]["sha256"], hashlib.sha256(region).hexdigest())
                # Taken once: a second run keeps the first.
                self.assertEqual(arrived.step_backup(ctx)["outcome"], "existing")
                self.assertEqual(ctx.state.input("backup_outcome"), "existing")

    def test_b4_keeps_nothing_that_is_not_this_plugs(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, plug_ip=self.server.host)
            with mock.patch.object(plugs, "PLUGS", Path(tmp) / "plugs"), mock.patch.object(plugs, "CAPTURES", Path(tmp)), \
                    mock.patch.object(plugs, "BEKEN_REGION", (0x3E0000, 0x100)), \
                    mock.patch.object(arrived.backup, "factory_mac", return_value="aa:bb:cc:dd:ee:ff"):
                self.assertEqual(arrived.step_backup(ctx)["outcome"], "unreadable")
                self.assertIsNone(plugs.find_backup(MAC))
            ctx.args.no_backup = True
            with mock.patch.object(plugs, "PLUGS", Path(tmp) / "plugs"), mock.patch.object(plugs, "CAPTURES", Path(tmp)):
                self.assertEqual(arrived.step_backup(ctx)["outcome"], "not wanted")

    def esphome_backup(self, tmp, read):
        """B4 on ESPHome, its reader giving what `read` does: the outcome, and
        whether the reader was shut after."""
        ctx = a_context(tmp, firmware="esphome", stock_version="1.0.2")
        with mock.patch.object(plugs, "PLUGS", Path(tmp) / "plugs"), mock.patch.object(plugs, "CAPTURES", Path(tmp)), \
                mock.patch.object(arrived.esphome, "api_read_backup", side_effect=read), \
                mock.patch.object(arrived.esphome, "api_close_backup", return_value=True) as shut, \
                mock.patch.object(arrived.backup, "factory_mac", return_value=MAC), \
                mock.patch.object(images, "stock_rbl", return_value=(b"RBL stock", "1.0.2")):
            outcome = arrived.step_backup(ctx)["outcome"]
            found = plugs.find_backup(MAC)
            source = json.loads((found.directory / plugs.BEKEN_MANIFEST).read_text())["source"] if found else None
        return outcome, shut.called, source

    def test_b4_reads_esphome_s_reader_then_shuts_it_for_good(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome, shut, source = self.esphome_backup(tmp, lambda host, size, progress: b"\x01" * size)
            self.assertEqual((outcome, shut), ("saved", True))
            self.assertIn("ESPHome", source)

    def test_b4_on_an_esphome_whose_reader_is_shut_or_absent_takes_none(self):
        def shut(host, size, progress):
            raise arrived.esphome.BackupShut()
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.esphome_backup(tmp, shut), ("reader shut", True, None))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.esphome_backup(tmp, lambda host, size, progress: None), ("openbeken only", True, None))

    def test_b4_leaves_a_reader_whose_read_failed_to_shut_by_itself(self):
        # Tried again, it can still be read; untried, its time runs out.
        def cut(host, size, progress):
            raise OSError("the device stopped giving its data")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.esphome_backup(tmp, cut), ("failed", False, None))

    def test_k3_waits_for_ewelink_to_show_the_plug_online(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, plug_ip="192.0.2.50")
            answers = iter([[{"deviceid": "1001234abc", "online": False}], [{"deviceid": "1001234abc", "online": True}]])
            ctx.account_devices = lambda: next(answers)
            with mock.patch.object(plugs, "PLUGS", Path(tmp) / "plugs"), mock.patch.object(plugs, "CAPTURES", Path(tmp)), \
                    mock.patch.object(detect, "identify", return_value=None), \
                    mock.patch.object(arrived.time, "sleep"), \
                    mock.patch.object(arrived.time, "monotonic", side_effect=iter(range(0, 10000, 5))):
                plugs.remember_plug(MAC, ewelink_id="1001234abc")
                self.assertEqual(arrived.step_sonoff_check(ctx)["confirmed_by"], "eWeLink")
            # Still answering as the firmware it was to leave: not back.
            with mock.patch.object(plugs, "PLUGS", Path(tmp) / "plugs"), mock.patch.object(plugs, "CAPTURES", Path(tmp)), \
                    mock.patch.object(detect, "identify", return_value="openbeken"), \
                    mock.patch.object(arrived.time, "sleep"), \
                    mock.patch.object(arrived.time, "monotonic", side_effect=iter(range(0, 10000, 100))):
                with self.assertRaisesRegex(errors.Abort, "still answers"):
                    arrived.step_sonoff_check(ctx, wait=50)
            # Never shown online: an unattended run stops, saying to switch it off and on.
            ctx.account_devices = lambda: [{"deviceid": "1001234abc", "online": False}]
            with mock.patch.object(plugs, "PLUGS", Path(tmp) / "plugs"), mock.patch.object(plugs, "CAPTURES", Path(tmp)), \
                    mock.patch.object(detect, "identify", return_value=None), \
                    mock.patch.object(arrived.time, "sleep"), mock.patch.object(arrived.prompts, "UNATTENDED", True), \
                    mock.patch.object(arrived.time, "monotonic", side_effect=iter(range(0, 10000, 100))):
                with self.assertRaisesRegex(errors.Abort, "has not shown.*switch the device off and on"):
                    arrived.step_sonoff_check(ctx, wait=50)


class DetectTests(unittest.TestCase):
    def test_openbeken_named_as_the_s61s_is_found_with_its_kind(self):
        server = FakeOpenBeken()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        found = detect._openbeken(server.host, 2.0)
        self.assertEqual((found.firmware, found.name, found.kind), ("openbeken", "openbeken-abcdef", "sonoff-s61"))
        server.kind = None   # an OpenBeken the tool did not install: not a plug of ours
        self.assertIsNone(detect._openbeken(server.host, 2.0))


if __name__ == "__main__":
    unittest.main()
