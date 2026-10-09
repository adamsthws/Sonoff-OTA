import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from tools.get_device_key import EwelinkError
from tools.watch_sonoff_updates import (PacketParser, Recorder, describe, end_process, find_plug, offers,
                                        redact, remote_capture, summarise)

# Real tcpdump 4.99 output (-l -e -nn -tttt -A -s 0) for a plug that boots,
# looks up its server, opens the firmware port and asks for a download.
CAPTURE = """\
2026-10-03 05:00:00.000000 aa:bb:cc:11:22:33 > ff:ff:ff:ff:ff:ff, ethertype IPv4 (0x0800), length 282: 0.0.0.0.68 > 255.255.255.255.67: BOOTP/DHCP, Request, length 240
E.......@.y..........D.C...........
2026-10-03 05:00:01.000000 aa:bb:cc:11:22:33 > 00:11:22:33:44:55, ethertype IPv4 (0x0800), length 78: 192.168.1.96.40000 > 192.168.1.1.53: 4660+ A? eu-disp.coolkit.cc. (36)
E..@....@......`.....@.5.,...4...........eu-disp.coolkit.cc.....
2026-10-03 05:00:02.000000 00:11:22:33:44:55 > aa:bb:cc:11:22:33, ethertype IPv4 (0x0800), length 94: 192.168.1.1.53 > 192.168.1.96.40000: 4660 1/0/0 A 52.57.99.135 (52)
2026-10-03 05:00:03.000000 aa:bb:cc:11:22:33 > 00:11:22:33:44:55, ethertype IPv4 (0x0800), length 54: 192.168.1.96.50000 > 52.57.99.135.8088: Flags [S], seq 1, win 64240, length 0
2026-10-03 05:00:04.000000 00:11:22:33:44:55 > aa:bb:cc:11:22:33, ethertype IPv4 (0x0800), length 54: 52.57.99.135.8088 > 192.168.1.96.50000: Flags [S.], seq 1, ack 0, win 64240, length 0
2026-10-03 05:00:05.000000 aa:bb:cc:11:22:33 > 00:11:22:33:44:55, ethertype IPv4 (0x0800), length 172: 192.168.1.96.50000 > 52.57.99.135.8088: Flags [P.], seq 2:120, ack 4294967295, win 64240, length 118
E.......@. ....`49c..P..........P.......GET /ota/rom/abc/user1.1024.new.2.bin?deviceid=x&ts=1&sign=y HTTP/1.1
Range: bytes=0-23
2026-10-03 05:00:06.000000 aa:bb:cc:11:22:33 > 00:11:22:33:44:55, ethertype IPv4 (0x0800), length 54: 192.168.1.96.50001 > 3.120.0.1.443: Flags [S], seq 1, win 64240, length 0
2026-10-03 05:00:07.000000 aa:bb:cc:11:22:33 > 00:11:22:33:44:55, ethertype IPv4 (0x0800), length 90: 192.168.1.96.40001 > 192.168.1.1.123: NTPv4, Client, length 48
"""


def parse(parser):
    return [event for line in CAPTURE.splitlines() for event in parser.feed(line)]


class PacketParserTests(unittest.TestCase):
    def test_reads_a_boot_and_a_firmware_download(self):
        events = parse(PacketParser(None, "AA:BB:CC:11:22:33"))
        self.assertEqual([e["kind"] for e in events],
                         ["dhcp", "dns_query", "dns_answer", "connect", "http_request", "connect", "ntp"])
        self.assertEqual(events[1]["name"], "eu-disp.coolkit.cc")
        self.assertEqual(events[2]["addresses"], ["52.57.99.135"])
        # The connection is named from the plug's own lookup.
        self.assertEqual(events[3], {"kind": "connect", "address": "52.57.99.135", "port": 8088,
                                     "name": "eu-disp.coolkit.cc"})
        self.assertEqual(events[4]["port"], 8088)
        self.assertTrue(events[4]["path"].startswith("/ota/rom/abc/user1"))
        self.assertEqual(events[5]["port"], 443)

    def test_matching_by_address_sees_the_same_traffic_once_it_has_one(self):
        kinds = [e["kind"] for e in parse(PacketParser("192.168.1.96", None))]
        self.assertNotIn("dhcp", kinds)    # it had no address yet
        self.assertIn("http_request", kinds)

    def test_replies_and_syn_acks_are_not_new_connections(self):
        connects = [e for e in parse(PacketParser("192.168.1.96", None)) if e["kind"] == "connect"]
        self.assertEqual([c["port"] for c in connects], [8088, 443])

    def test_firmware_traffic_is_flagged(self):
        events = parse(PacketParser("192.168.1.96", None))
        flagged = [e["kind"] for e in events if describe(e)[1]]
        self.assertEqual(flagged, ["connect", "http_request"])
        self.assertIn("FIRMWARE DOWNLOAD", describe(events[3])[0])
        self.assertNotIn("sign=", describe(events[3])[0])


def tls(second, outbound, length):
    src, dst = ("192.168.0.50.49200", "3.120.0.1.8081") if outbound else ("3.120.0.1.8081", "192.168.0.50.49200")
    smac, dmac = ("aa:bb:cc:12:34:56", "02:00:00:00:00:01") if outbound else ("02:00:00:00:00:01", "aa:bb:cc:12:34:56")
    import datetime
    stamp = (datetime.datetime(2026, 10, 7, 13) + datetime.timedelta(seconds=second)).strftime("%Y-%m-%d %H:%M:%S.%f")
    return (f"{stamp} {smac} > {dmac}, ethertype IPv4 (0x0800), length {length + 54}: "
            f"{src} > {dst}: Flags [P.], seq 1:{length + 1}, ack 1, win 5000, length {length}")


class CloudConnectionTests(unittest.TestCase):
    """Encrypted traffic is judged by timing: replies follow the plug, pushes do not."""

    def events(self, packets):
        parser = PacketParser(None, "aa:bb:cc:12:34:56")
        return [e for second, out, length in packets for e in parser.feed(tls(second, out, length))]

    def test_a_reply_to_the_plug_is_not_a_push(self):
        events = self.events([(1, True, 40), (1.3, False, 40)])
        self.assertEqual([e["kind"] for e in events], ["tcp_data", "tcp_data"])

    def test_data_the_plug_did_not_ask_for_is_a_push_counted_once(self):
        events = self.events([(1, True, 40), (1.3, False, 40), (20, False, 1400), (20.01, False, 300)])
        pushes = [e for e in events if e["kind"] == "cloud_push"]
        self.assertEqual(len(pushes), 1)          # the second segment is the same message
        self.assertEqual((pushes[0]["bytes"], pushes[0]["port"]), (1400, 8081))
        self.assertIn("1400 bytes unprompted", describe(pushes[0])[0])

    def test_zero_length_acks_are_ignored(self):
        line = tls(5, False, 0)
        self.assertEqual(PacketParser(None, "aa:bb:cc:12:34:56").feed(line), [])

    def test_summary_gives_check_in_interval_and_pushes(self):
        packets = [(s, True, 40) for s in (0, 60, 120, 180)] + [(s + 0.2, False, 40) for s in (0, 60, 120, 180)]
        packets += [(150, False, 500)]
        packets.sort()
        with tempfile.TemporaryDirectory() as tmp:
            recorder = Recorder(Path(tmp) / "run")
            for event in self.events(packets):
                recorder.event(event.pop("kind"), None, **event)
            report = summarise(recorder, time.monotonic())
            recorder.close()
        self.assertIn("3.120.0.1:8081: plug checked in 4 times, typically every 1.0 min", report)
        self.assertIn("Cloud spoke first: 1 time(s)", report)
        self.assertIn("1 x 500 bytes", report)

    def test_the_plug_answering_the_cloud_is_not_a_check_in(self):
        # The real pattern: the cloud speaks, the plug answers, later a heartbeat.
        packets = [(17.8, False, 194), (18.0, True, 232), (18.07, True, 168), (18.1, False, 128),
                   (163.2, True, 35), (163.3, False, 31)]
        with tempfile.TemporaryDirectory() as tmp:
            recorder = Recorder(Path(tmp) / "run")
            for event in self.events(packets):
                recorder.event(event.pop("kind"), None, **event)
            report = summarise(recorder, time.monotonic())
            recorder.close()
        self.assertIn("plug sent data 1 time(s)", report)
        self.assertIn("Cloud spoke first: 1 time(s)", report)


class CloudHelperTests(unittest.TestCase):
    def test_offers_keep_versions_and_drop_links(self):
        response = {"data": {"otaInfoList": [{"version": "1.2.0", "binList": [
            {"downloadUrl": "http://192.0.2.1:8088/ota/rom/secret/user1.bin", "digest": "x"}, {}]}]}}
        self.assertEqual(offers(response), [{"version": "1.2.0", "files": 2}])
        self.assertNotIn("secret", json.dumps(offers(response)))

    def test_credentials_are_redacted_at_any_depth(self):
        message = {"action": "update", "apikey": "k", "params": {"switch": "on"},
                   "nested": [{"at": "token", "devicekey": "d"}]}
        self.assertEqual(redact(message), {"action": "update", "apikey": "<redacted>",
                                           "params": {"switch": "on"},
                                           "nested": [{"at": "<redacted>", "devicekey": "<redacted>"}]})

    def test_finds_the_only_s60(self):
        s60 = {"deviceid": "1", "extra": {"model": "SN-ESP32C3-S60-01"}, "params": {"fwVersion": "1.2.0"}}
        other = {"deviceid": "2", "extra": {"model": "OTHER"}, "params": {"fwVersion": "3.0"}}
        self.assertIs(find_plug([other, {"deviceid": "3"}, s60], None), s60)
        with self.assertRaises(EwelinkError):
            find_plug([s60, dict(s60, deviceid="4")], None)
        self.assertIs(find_plug([other, s60], "2"), other)


class SummaryTests(unittest.TestCase):
    def test_summary_counts_a_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = Recorder(Path(tmp) / "run")
            posix = os.name == "posix"  # Windows has no permission bits to check
            if posix:
                self.assertEqual(os.stat(recorder.directory).st_mode & 0o777, 0o700)
            with contextlib.redirect_stdout(io.StringIO()):
                for event in parse(PacketParser("192.168.1.96", None)):
                    screen, notable = describe(event)
                    recorder.event(event.pop("kind"), screen, notable=notable, **event)
                recorder.event("cloud_state", "cloud", online=True, version="1.2.0")
                recorder.event("ota_offer", "offer", offer=[{"version": "1.2.1", "files": 2}])
            report = summarise(recorder, time.monotonic())
            recorder.close()
            if posix:
                self.assertEqual(os.stat(recorder.directory / "events.jsonl").st_mode & 0o777, 0o600)
        self.assertIn("Firmware downloads seen: 1", report)
        self.assertIn("Firmware versions reported: 1.2.0", report)
        self.assertIn("Update offers, in order: 1.2.1", report)
        self.assertIn("eu-disp.coolkit.cc:8088", report)



class RouterSafetyTests(unittest.TestCase):
    """The router is only read from, and nothing is left running there."""

    def command(self, **kwargs):
        from types import SimpleNamespace
        args = SimpleNamespace(**{"plug_ip": None, "plug_mac": "aa:bb:cc:11:22:33", "interface": "em0", **kwargs})
        return remote_capture(args, 3720)

    def test_capture_never_changes_the_interface_and_ends_by_itself(self):
        import shlex
        words = shlex.split(self.command())
        self.assertEqual(words[:2], ["timeout", "3720"])
        self.assertEqual(words[2], "tcpdump")
        self.assertIn("-p", words)                       # no promiscuous mode
        self.assertNotIn("-w", words)                    # nothing written on the router
        self.assertTrue(words[-1].startswith("ether host aa:bb:cc:11:22:33 and ("))

    def test_an_address_filter_is_used_without_a_mac(self):
        import shlex
        self.assertTrue(shlex.split(self.command(plug_ip="192.168.0.50", plug_mac=None))[-1]
                        .startswith("host 192.168.0.50 and ("))

    def test_a_process_that_ignores_terminate_is_killed(self):
        import subprocess, sys
        stubborn = subprocess.Popen([sys.executable, "-c",
                                     "import signal, time\n"
                                     "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                                     "print('ready', flush=True)\n"
                                     "time.sleep(60)"], stdout=subprocess.PIPE, text=True)
        stubborn.stdout.readline()
        started = time.monotonic()
        end_process(stubborn)
        self.assertIsNotNone(stubborn.poll())
        self.assertLess(time.monotonic() - started, 15)
        stubborn.stdout.close()


if __name__ == "__main__":
    unittest.main()
