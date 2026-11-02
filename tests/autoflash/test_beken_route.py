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
from tools.autoflash import detect, errors, handover, kinds, openbeken, plugs, report, routes, state
from tools.autoflash.routes import beken
from tools.autoflash.routes.beken import arrived, images, onward, stock
from tools.beken import fota_wrap, interim_marker, rbl, uf2
from tools.ewelink import ota_push
from tools.ewelink.get_device_key import EwelinkError

S61 = kinds.SONOFF_S61
MAC = "12:34:56:ab:cd:ef"


class RouteTests(unittest.TestCase):
    def test_every_direction_is_one_move_and_a_check(self):
        for firmware, source, expected in (
            ("openbeken", "sonoff", ["A0", "A1", "A3", "B1", "B2", "K1", "B4"]),
            # Through the interim firmware, the backup taken on it.
            ("esphome", "sonoff", ["A0", "A1", "A3", "B1", "B2", "J1", "B4", "J2", "K2"]),
            ("esphome", "openbeken", ["A0", "B4", "O1", "K2"]),
            ("sonoff", "openbeken", ["A0", "B4", "O2", "K3"]),
            ("openbeken", "esphome", ["A0", "E1", "K1", "B4"]),
            ("sonoff", "esphome", ["A0", "E2", "K3"]),
            # Left on the interim firmware by a run whose record is gone: on
            # as that run would have gone, kept as OpenBeken, or as from OpenBeken.
            ("esphome", "interim", ["A0", "B4", "J2", "K2"]),
            ("openbeken", "interim", ["A0", "J3", "K1", "B4"]),
            ("sonoff", "interim", ["A0", "B4", "J4", "K3"]),
        ):
            with self.subTest(firmware=firmware, source=source):
                self.assertEqual([s for s, _, _ in routes.steps_for(firmware, source, beken)], expected)
        self.assertEqual(beken.steps_for("esphome", "esphome"), [])
        with self.assertRaises(ValueError):
            beken.steps_for("esphome", "midway")
        self.assertIn("interim", beken.SOURCES)

    def test_a_run_to_esphome_shows_these_stages(self):
        names = [routes._stage_name(step, title, "esphome", beken)
                 for step, title, _ in routes.steps_for("esphome", "sonoff", beken)][-5:]
        self.assertEqual(names, ["Sending interim firmware", "Checking interim firmware", "Backing up the device",
                                 "Sending ESPHome", "Checking ESPHome"])

    def test_the_interim_firmware_is_never_called_openbeken(self):
        # Shown as OpenBeken, the person would think they chose it by mistake.
        steps = beken.INTERIM_STEPS + beken.FROM_INTERIM["esphome"] + beken.FROM_INTERIM["sonoff"]
        for name, title, _ in steps:
            with self.subTest(step=name):
                for text in (title, beken.STAGE_NAMES[name], beken.PLAIN_DESCRIPTION[name]):
                    self.assertNotIn("openbeken", text.lower())
        for text in (images.INTERIM_NAME, detect.NAMES["interim"], report.CONVERTED_FROM["interim"],
                     onward.INTERIM_FIRMWARE):
            self.assertNotIn("openbeken", text.lower())
        # A run to OpenBeken from it names OpenBeken as what the device is to
        # have, and the interim firmware as what it had.
        (name, title, _), = beken.FROM_INTERIM["openbeken"]
        self.assertEqual(title, "OpenBeken, in place of the interim firmware")

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
            # The image alone: a run to OpenBeken keeps it, so sends no marker.
            self.assertEqual(sent["firmware"], fota_wrap.wrap(image, name=S61.wrapper_model, version=images.FOTA_VERSION))
            self.assertEqual(result["bytes"], len(sent["firmware"]))
            self.assertEqual((sent["device_id"], sent["device_ip"], sent["listen_ip"], sent["listen_port"]),
                             ("1001234abc", "127.0.0.1", "192.168.0.2", 8088))
            self.assertEqual(sent["names"], ("user1.bin",))
            self.assertIs(sent["live"], False)   # what it fetches is logged, never drawn over the stage line
            # The plug must be as it was at B1: another model, or firmware, is refused before sending.
            with self.assertRaisesRegex(EwelinkError, "1.0.3"):
                sent["check_device"](dict(self.DEVICE, params={"fwVersion": "1.0.3"}), "SN-BK7238-S61-01")

    def test_b2_on_a_run_to_esphome_sends_the_interim_firmware(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, stock_version="1.0.2", firmware="esphome")
            ctx.account_plug = SimpleNamespace(device_id="1001234abc")
            ctx.account_devices = lambda: [self.DEVICE]
            sent = {}

            def deliver(**kwargs):
                sent.update(kwargs)
                return ota_push.Delivery(sent=True, complete=True, bytes_served=len(kwargs["firmware"]), seconds=9.5)

            with mock.patch.object(stock.firewall, "choose_port", return_value=8088), \
                    mock.patch.object(stock.firewall, "ensure_firewall"), \
                    mock.patch.object(stock.prompts, "warning_box"), \
                    mock.patch.object(stock.prompts, "require_word", return_value=True), \
                    mock.patch.object(stock.output, "say") as said, \
                    mock.patch.object(ota_push, "deliver", side_effect=deliver):
                result = stock.step_push(ctx)
            image, _ = images.image(ctx, "openbeken")
            self.assertEqual(result["firmware"], "openbeken")
            # The same image and the marker after it, by which it knows itself the
            # interim firmware - all of it in the container, its CRC over all of it.
            sent_payload = interim_marker.with_marker(image)
            self.assertEqual(sent["firmware"], fota_wrap.wrap(sent_payload, name=S61.wrapper_model,
                                                              version=images.FOTA_VERSION))
            self.assertEqual(sent["firmware"][116:], sent_payload)
            self.assertEqual(interim_marker.find(sent_payload), interim_marker.offset_after(len(image)))
            # And it is for the app that image installs, and only that.
            marker = interim_marker.read(sent_payload, interim_marker.offset_after(len(image)))
            self.assertTrue(interim_marker.for_app(marker, rbl.unpack(image)))
            self.assertFalse(interim_marker.for_app(marker, rbl.unpack(images.image(ctx, "esphome")[0])))
            shown = " ".join(str(call.args[0]) for call in said.call_args_list)
            self.assertIn("Sending the interim firmware", shown)
            self.assertNotIn("openbeken", shown.lower())
            with mock.patch.object(detect, "identify", return_value="interim"):
                self.assertEqual(stock.evidence_pushed(ctx),
                                 (True, "The device answers as the interim firmware: the firmware was delivered "
                                        "and installed."))
            # The plug's own OpenBeken would be another firmware than the one sent.
            with mock.patch.object(detect, "identify", return_value="openbeken"):
                self.assertIsNone(stock.evidence_pushed(ctx)[0])

    def test_a_spoilt_image_fails_only_the_bootloaders_check_and_only_once(self):
        # --simulate-bad-image: fetched whole (the update's CRC is made over it
        # as sent), but the RBL's own CRC of its body no longer holds.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="openbeken")
            image, _ = images.image(ctx, "openbeken")
            with mock.patch.object(images, "SPOIL_STEP", "B2"), mock.patch.object(images.output, "say"):
                spoilt = stock.payload(ctx, "openbeken", image)
                again = stock.payload(ctx, "openbeken", image)
                self.assertEqual(images.as_sent(ctx, "J2", image), image)   # another step's: as it is
            self.assertEqual((len(spoilt), spoilt[:rbl.HEADER]), (len(image), image[:rbl.HEADER]))
            with self.assertRaises(rbl.RblError):
                rbl.unpack(spoilt)
            # Once a run, by its record: the image sent again - by this run, or
            # one run again after it, asked the same - is the good one.
            self.assertEqual(again, image)
            self.assertTrue(ctx.state.data["captures"]["B2.spoilt"])

    def test_b2_not_confirmed_writes_nothing_and_a_cut_off_delivery_stays_attempted(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, stock_version="1.0.2", firmware="esphome")
            ctx.consented = False   # the go-ahead not given yet in this run
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
            with mock.patch.object(stock.prompts, "require_word", return_value=True) as asked, \
                    mock.patch.object(ota_push, "deliver", return_value=ota_push.Delivery(sent=False, error="login failed")):
                with self.assertRaisesRegex(errors.Abort, "was not sent: login failed"):
                    stock.step_push(ctx)
                # Given once a run: B2 sent again (errors.GoBack) does not ask again.
                with self.assertRaisesRegex(errors.Abort, "was not sent: login failed"):
                    stock.step_push(ctx)
            asked.assert_called_once()
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
            for running, verdict in (("openbeken", True), ("sonoff", False), (None, None), ("esphome", None),
                                     ("interim", None)):
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
            self.assertEqual(self.server.commands, ["AutoflashRevertToStock http://192.168.0.2:8088/firmware.rbl"])
            self.assertIn("firmware.rbl", fetched["names"])
            # An OpenBeken without the module cannot check where the image would go: refused.
            self.server.unknown.add("AutoflashRevertToStock")
            with mock.patch.object(images, "stock_rbl", return_value=(b"RBL stock", "1.0.2")):
                with self.assertRaisesRegex(errors.Abort, "no AutoflashRevertToStock"):
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


    def test_the_interim_firmware_fetches_esphome_without_asking_again(self):
        # The go-ahead was given at B2, before Sonoff's firmware was replaced.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, plug_ip=self.server.host, firmware="esphome", source="sonoff")
            ctx.consented = False
            fetched = {}
            with mock.patch.object(onward.handover, "consent_to_leave") as asked, \
                    mock.patch.object(onward.join, "note_signal") as signal:
                result = self.served(ctx, onward.step_interim_to_esphome, fetched)
            self.assertFalse(asked.called or signal.called)
            image, version = images.image(ctx, "esphome")
            self.assertEqual((fetched["image"], result["version"]), (image, version))
            self.assertEqual(self.server.commands, ["ota_http http://192.168.0.2:8088/firmware.rbl"])

    def test_a_plug_still_on_the_interim_firmware_did_not_take_esphome(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="esphome", source="sonoff")
            for running, expected in (
                    ("interim", (False, "The device still answers as the interim firmware: the image was not "
                                        "applied.")),
                    ("esphome", (True, "The device answers as ESPHome."))):
                with mock.patch.object(detect, "identify", return_value=running):
                    self.assertEqual(onward.evidence_installed(ctx), expected)


class FromTheInterimFirmwareTests(unittest.TestCase):
    """A plug left on the interim firmware, found by a run with no record of
    the one that left it there."""

    def setUp(self):
        self.server = FakeOpenBeken()
        self.server.interim, self.server.short_name = True, "autoflash-recovery-CDEF"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        for patch in (mock.patch.object(onward.firewall, "choose_port", return_value=8088),
                      mock.patch.object(onward.firewall, "ensure_firewall"),
                      mock.patch.object(onward.output, "say"), mock.patch.object(onward.output, "progress"),
                      mock.patch.object(onward.output, "progress_clear"), mock.patch.object(openbeken.time, "sleep"),
                      mock.patch.object(handover.output, "transient_begin"),
                      mock.patch.object(handover.output, "transient_end")):
            patch.start()
            self.addCleanup(patch.stop)

    def going_ahead(self, ctx, step, image=b"RBL image"):
        """`step` run with the firmware server stood in for, the go-ahead given:
        what the go-ahead said was replaced."""
        boxes = []

        class Server:
            def __init__(self, address, image, device_ip, say=print, names=(), live=None):
                self.image = image

            def serve_forever(self):
                pass

            def unique_bytes_served(self):
                return len(self.image)

            def shutdown(self):
                pass

            def server_close(self):
                pass

        with mock.patch.object(ota_push, "FirmwareServer", Server), \
                mock.patch.object(images, "stock_rbl", return_value=(image, "1.0.2")), \
                mock.patch.object(handover.prompts, "warning_box", side_effect=lambda title, lines: boxes.append(lines)), \
                mock.patch.object(handover.prompts, "require_word", return_value=True):
            step(ctx)
        return " ".join(boxes[0]) if boxes else None

    def test_to_esphome_it_fetches_esphome_once_the_go_ahead_is_given(self):
        # This run gave none before (no B2 of its own): asked, of the interim firmware.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, plug_ip=self.server.host, firmware="esphome", source="interim")
            ctx.consented = False
            said = self.going_ahead(ctx, onward.step_interim_to_esphome)
            self.assertIn("the device's interim firmware is replaced", said)
            self.assertNotIn("openbeken", said.lower())
            self.assertEqual(self.server.commands[-1], "ota_http http://192.168.0.2:8088/firmware.rbl")

    def test_to_sonoffs_firmware_it_fetches_it_as_openbeken_would(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, plug_ip=self.server.host, firmware="sonoff", source="interim")
            ctx.consented = False
            said = self.going_ahead(ctx, onward.step_interim_to_sonoff)
            self.assertIn("the device's interim firmware is replaced", said)
            self.assertEqual(self.server.commands[-1], "AutoflashRevertToStock http://192.168.0.2:8088/firmware.rbl")
            # Still on it afterwards: the image was not applied, and is sent again.
            with mock.patch.object(detect, "identify", return_value="interim"):
                self.assertEqual(onward.evidence_installed(ctx),
                                 (False, "The device still answers as the interim firmware: the image was not "
                                         "applied."))

    def test_to_openbeken_it_is_kept_as_the_plugs_own_and_restarted(self):
        # Nothing sent: it is the OpenBeken the run would install.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, plug_ip=self.server.host, firmware="openbeken", source="interim")
            with mock.patch.object(onward.output, "say") as said:
                self.assertEqual(onward.step_interim_to_openbeken(ctx), {"restarted": True})
            self.assertEqual(self.server.commands, ["AutoflashInterim off", "reboot"])
            self.assertFalse(self.server.interim)
            self.assertIn("so your network knows it by its new name", said.call_args_list[-1].args[0])
            ctx.dry_run = True
            self.assertEqual(onward.step_interim_to_openbeken(ctx), {"dry_run": True})
            self.assertEqual(len(self.server.commands), 2)

    def test_one_that_will_not_be_renamed_stops_the_run(self):
        self.server.kind = None   # a module without the command
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, plug_ip=self.server.host, firmware="openbeken", source="interim")
            with self.assertRaisesRegex(errors.Abort, "would not take its new name"):
                onward.step_interim_to_openbeken(ctx)

    def test_the_person_is_told_it_was_left_part_way(self):
        with mock.patch.object(routes.prompts, "section") as section, \
                mock.patch.object(routes.prompts, "ask_choice", return_value=1), \
                mock.patch.object(routes.output, "transient_begin"), mock.patch.object(routes.output, "transient_end"):
            self.assertEqual(routes.ask_firmware("interim", ["openbeken", "esphome", "sonoff"]), "esphome")
        self.assertIn("left part way through a conversion", " ".join(section.call_args.args[1]))

    def test_with_plug_ip_it_is_found_with_its_kind(self):
        # As the search finds it: the plug's kind is its import module's to say
        # (an OpenBeken at --plug-ip was taken for Sonoff's firmware before).
        from tools.autoflash.routes.espressif import source
        for interim, running in ((True, "interim"), (False, "openbeken")):
            with self.subTest(running=running), tempfile.TemporaryDirectory() as tmp:
                self.server.interim = interim
                run = state.RunState.open(Path(tmp) / "run")
                run.set_input("plug_ip", self.server.host)
                ctx = SimpleNamespace(state=run, web_password=None, esphome_login=None,
                                      set_plug_ip=lambda ip: run.set_input("plug_ip", ip))
                with mock.patch.object(source.unlock, "identify", return_value=running), \
                        mock.patch.object(source.hostsys, "mac_of", return_value=None), \
                        mock.patch.object(source.output, "say"):
                    self.assertEqual(source.find_current_firmware(ctx), running)
                self.assertEqual(run.input("plug_kind"), "sonoff-s61")
        # Answering otherwise the second time: never guessed at.
        with tempfile.TemporaryDirectory() as tmp:
            run = state.RunState.open(Path(tmp) / "run")
            run.set_input("plug_ip", self.server.host)
            ctx = SimpleNamespace(state=run, web_password=None, esphome_login=None, set_plug_ip=lambda ip: None)
            with mock.patch.object(source.unlock, "identify", return_value="interim"), \
                    mock.patch.object(source.hostsys, "mac_of", return_value=None):
                with self.assertRaisesRegex(errors.Abort, "did not say twice alike"):
                    source.find_current_firmware(ctx)


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

    def test_the_onward_steps_are_checked_on_resume(self):
        self.assertEqual(set(beken.STEP_VERIFIERS), {"J2", "J4", "O1", "O2", "E1", "E2"})


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
            # Its module was asked, and said it is not the interim firmware: nothing changed.
            self.assertIn("AutoflashInterim", self.server.commands)
            self.assertNotIn("told", result)
            self.assertFalse([c for c in self.server.commands if c.startswith("AutoflashInterim ")])
            with mock.patch.object(openbeken, "info", return_value=None), \
                    mock.patch.object(arrived.join.time, "sleep"), \
                    mock.patch.object(arrived.prompts, "UNATTENDED", True), \
                    mock.patch.object(arrived.join.time, "monotonic", side_effect=iter(range(0, 10000, 100))):
                with self.assertRaisesRegex(errors.Abort, "has not answered.*switch the device off and on"):
                    arrived.step_openbeken_check(ctx, wait=10)

    def test_k1_on_the_interim_firmware_makes_it_the_plugs_own_and_restarts_it(self):
        # Not sent as one on a run to OpenBeken; were it so, it is told it is
        # not, and restarted so its network's name for it follows.
        self.server.interim, self.server.short_name = True, "autoflash-recovery-CDEF"
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, plug_ip=self.server.host)
            with mock.patch.object(onward.output, "say") as said:
                result = arrived.step_openbeken_check(ctx)
            self.assertEqual((result["name"], result["told"]), ("openbeken-abcdef", "not interim"))
            self.assertEqual(ctx.state.input("device_name"), "openbeken-abcdef")
            self.assertEqual(self.server.commands[-2:], ["AutoflashInterim off", "reboot"])
            self.assertIn("Restarting it, so your network knows it by its new name",
                          " ".join(str(call.args[0]) for call in said.call_args_list))
            self.assertFalse(self.server.interim)

    def waiting_on_sonoff(self, ctx, answers):
        """J1 with the plug answering as Sonoff's firmware throughout, and the
        question after each wait answered with `answers` in turn."""
        with mock.patch.object(openbeken, "info", return_value=None), \
                mock.patch.object(detect, "identify", return_value="sonoff"), \
                mock.patch.object(arrived.join.time, "sleep"), \
                mock.patch.object(arrived.join.time, "monotonic", side_effect=iter(range(0, 100000, 100))), \
                mock.patch.object(arrived.prompts, "section"), mock.patch.object(arrived.output, "transient_begin"), \
                mock.patch.object(arrived.output, "transient_end"), \
                mock.patch.object(arrived.prompts, "ask_choice", side_effect=answers) as asked:
            try:
                arrived.step_interim_check(ctx, wait=1000)
            finally:
                self.asked = asked.call_count

    def test_j1_after_b2_sends_the_run_back_to_it_when_a_restart_leaves_sonoffs_firmware(self):
        # The image was sent whole, but the plug, switched off and on, starts
        # Sonoff's firmware again: it was not installed, and never will be.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="esphome")
            ctx.state.mark("B2", state.DONE)
            with self.assertRaises(errors.GoBack) as back:
                self.waiting_on_sonoff(ctx, [0])
            self.assertEqual((back.exception.step, back.exception.outcome), ("B2", "not installed"))
            self.assertIn("The interim firmware was not installed", back.exception.reason)
            # Before the restart, Sonoff's firmware answering said nothing: asked once, to switch it off and on.
            self.assertEqual(self.asked, 1)

    def test_a_switch_off_and_on_asked_for_is_taken_as_done_by_the_next_run(self):
        # An unattended run stops to ask; run again after it, Sonoff's firmware
        # answering means the image was not taken - at once, not after a wait.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="esphome")
            ctx.state.mark("B2", state.DONE)
            with mock.patch.object(arrived.prompts, "UNATTENDED", True), self.assertRaises(errors.Abort):
                self.waiting_on_sonoff(ctx, [])
            self.assertTrue(ctx.state.input("restart_asked"))
            with self.assertRaises(errors.GoBack):
                self.waiting_on_sonoff(ctx, [])
            self.assertEqual(self.asked, 0)
            self.assertIsNone(ctx.state.input("restart_asked"))   # the image sent again starts afresh

    def k2_with_openbeken_up_for(self, ctx, uptime):
        """K2 with the plug answering as OpenBeken, up `uptime` seconds."""
        with mock.patch.object(arrived.esphome, "is_esphome", return_value=False), \
                mock.patch.object(openbeken, "info", return_value={"uptime_s": uptime}), \
                mock.patch.object(arrived.join.time, "sleep"), \
                mock.patch.object(arrived.join.time, "monotonic", side_effect=iter(range(0, 100000, 100))), \
                mock.patch.object(arrived.prompts, "UNATTENDED", True):
            arrived.step_esphome_check(ctx, wait=1000)

    def test_k2_after_j2_sends_the_run_back_when_openbeken_restarted_since_the_fetch(self):
        # OpenBeken restarts by itself once it has ESPHome: back on OpenBeken
        # since, its bootloader did not take the image - no restart to ask for.
        import time
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="esphome")
            ctx.state.mark("J2", state.DONE)
            ctx.state.capture("J2.fetched_at", round(time.time()) - 120)
            with self.assertRaises(errors.GoBack) as back:
                self.k2_with_openbeken_up_for(ctx, 30)
            self.assertEqual(back.exception.step, "J2")
            self.assertIn("ESPHome was not installed", back.exception.reason)
            # Up since before the fetch: not restarted yet, only waited for.
            with self.assertRaisesRegex(errors.Abort, "switch the device off and on"):
                self.k2_with_openbeken_up_for(ctx, 600)

    def test_k2_sees_the_restart_by_the_uptime_before_the_fetch_whatever_its_clock(self):
        # The emulated plug's clock runs fast: its uptime soon outgrows the time
        # since the fetch, but restarted it is still below what it was before.
        import time
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="esphome")
            ctx.state.mark("J2", state.DONE)
            ctx.state.capture("J2.fetched_at", round(time.time()) - 20)
            ctx.state.capture("J2.uptime_before", 900)
            with self.assertRaises(errors.GoBack):
                self.k2_with_openbeken_up_for(ctx, 300)
            with self.assertRaisesRegex(errors.Abort, "switch the device off and on"):
                self.k2_with_openbeken_up_for(ctx, 950)

    def test_without_b2_a_plug_on_sonoffs_firmware_is_only_waited_for(self):
        # Nothing this run sent to go back to: asked again, as before.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="esphome")
            with self.assertRaises(errors.Abort):
                self.waiting_on_sonoff(ctx, [0, errors.Abort("stopped")])
            self.assertEqual(self.asked, 2)

    def test_j1_finds_the_interim_firmware_and_names_it_so(self):
        self.server.interim, self.server.short_name = True, "autoflash-recovery-CDEF"
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, plug_ip=self.server.host, firmware="esphome")
            with mock.patch.object(arrived.output, "say") as said:
                result = arrived.step_interim_check(ctx)
            self.assertEqual(result, {"seconds": result["seconds"], "version": "1.0.0"})
            self.assertNotIn("reboot", self.server.commands)
            self.assertEqual(said.call_args.args[0][:46], "    The interim firmware 1.0.0 answered after ")
            # Its name is not the plug's: ESPHome, next, names it.
            self.assertIsNone(ctx.state.input("device_name"))
            with mock.patch.object(openbeken, "info", return_value=None), \
                    mock.patch.object(arrived.join.time, "sleep"), \
                    mock.patch.object(arrived.prompts, "UNATTENDED", True), \
                    mock.patch.object(arrived.join.time, "monotonic", side_effect=iter(range(0, 10000, 100))):
                with self.assertRaisesRegex(errors.Abort, "^The interim firmware has not answered"):
                    arrived.step_interim_check(ctx, wait=10)

    def test_j1_on_an_openbeken_that_does_not_know_itself_the_interim_firmware_tells_it(self):
        # Its update cut off in its last few kilobytes: the image whole, so
        # installed, but not the marker after it. Told, so the hook wipes it
        # after J2 - but not restarted: never named wrongly on the network,
        # only not renamed for its few minutes.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, plug_ip=self.server.host, firmware="esphome")
            with mock.patch.object(onward.output, "say") as said:
                result = arrived.step_interim_check(ctx)
            self.assertEqual(result["told"], "interim")
            self.assertEqual(self.server.commands[-1], "AutoflashInterim on")
            self.assertNotIn("reboot", self.server.commands)
            self.assertTrue(self.server.interim)
            shown = " ".join(str(call.args[0]) for call in said.call_args_list)
            self.assertIn("it is the interim firmware now, under the name it started with", shown)
            self.assertNotIn("Restarting", shown)
            self.assertNotIn("openbeken", shown.lower())

    def test_j1_and_k1_wait_for_an_openbeken_that_will_not_say_which_it_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, plug_ip=self.server.host, firmware="esphome")
            with mock.patch.object(openbeken, "is_interim", return_value=None), \
                    mock.patch.object(arrived.join.time, "sleep"), \
                    mock.patch.object(arrived.prompts, "UNATTENDED", True), \
                    mock.patch.object(arrived.join.time, "monotonic", side_effect=iter(range(0, 10000, 100))):
                with self.assertRaisesRegex(errors.Abort, "has not answered"):
                    arrived.step_interim_check(ctx, wait=10)
            self.assertFalse([c for c in self.server.commands if c.startswith("AutoflashInterim ")])

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

    def test_b4_on_a_run_to_esphome_reads_through_the_interim_firmware(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, plug_ip=self.server.host, firmware="esphome", stock_version="1.0.2")
            with mock.patch.object(plugs, "PLUGS", Path(tmp) / "plugs"), mock.patch.object(plugs, "CAPTURES", Path(tmp)), \
                    mock.patch.object(plugs, "BEKEN_REGION", (0x3E0000, 0x100)), \
                    mock.patch.object(arrived.backup, "factory_mac", return_value=MAC), \
                    mock.patch.object(images, "stock_rbl", return_value=(b"RBL stock", "1.0.2")):
                self.assertEqual(arrived.step_backup(ctx)["outcome"], "saved")
                manifest = json.loads((plugs.find_backup(MAC).directory / plugs.BEKEN_MANIFEST).read_text())
                self.assertEqual(manifest["source"], "OpenBeken GET /api/flash after conversion")

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
        found = detect.openbeken_plug(server.host, 2.0)
        self.assertEqual((found.firmware, found.name, found.kind), ("openbeken", "openbeken-abcdef", "sonoff-s61"))
        # The interim firmware, as its module says: told apart, its name shown.
        server.interim, server.short_name = True, "autoflash-recovery-CDEF"
        found = detect.openbeken_plug(server.host, 2.0)
        self.assertEqual((found.firmware, found.name, found.kind), ("interim", "autoflash-recovery-CDEF", "sonoff-s61"))
        self.assertEqual(found.describe(), f"the device at {server.host} (autoflash-recovery-CDEF), running the "
                                           "interim firmware")
        server.kind = None   # an OpenBeken the tool did not install: not a plug of ours
        self.assertIsNone(detect.openbeken_plug(server.host, 2.0))

    def test_one_that_will_not_say_whether_it_is_the_interim_firmware_is_asked_again_later(self):
        # Never taken for the plug's own OpenBeken, and sent the wrong way.
        server = FakeOpenBeken()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with mock.patch.object(openbeken, "is_interim", return_value=None):
            self.assertIsNone(detect.openbeken_plug(server.host, 2.0))


if __name__ == "__main__":
    unittest.main()
