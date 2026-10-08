import socket
import struct
import unittest

from unittest import mock

from tools import discover_ewelink
from tools.discover_ewelink import _keep, answered_ids, encode_name, sweep_targets, is_response, make_query, parse_packet, read_name


class MdnsTests(unittest.TestCase):
    def test_name_round_trip(self):
        packet = encode_name("_ewelink._tcp.local")
        self.assertEqual(read_name(packet, 0), ("_ewelink._tcp.local", len(packet)))

    def test_query_requests_ptr(self):
        packet = make_query()
        name, cursor = read_name(packet, 12)
        rtype, rclass = struct.unpack_from("!HH", packet, cursor)
        self.assertEqual(name, "_ewelink._tcp.local")
        self.assertEqual(rtype, 12)
        self.assertEqual(rclass, 0x8001)

    def test_query_can_request_multicast_reply(self):
        packet = make_query(unicast=False)
        _, cursor = read_name(packet, 12)
        self.assertEqual(struct.unpack_from("!HH", packet, cursor), (12, 0x0001))

    def test_parses_ptr_srv_and_a(self):
        service = encode_name("_ewelink._tcp.local")
        instance = encode_name("eWeLink_test._ewelink._tcp.local")
        host = encode_name("eWeLink_test.local")
        ptr = service + struct.pack("!HHIH", 12, 1, 120, len(instance)) + instance
        srv_data = struct.pack("!HHH", 0, 0, 8081) + host
        srv = instance + struct.pack("!HHIH", 33, 1, 120, len(srv_data)) + srv_data
        address = socket.inet_aton("192.168.1.96")
        arecord = host + struct.pack("!HHIH", 1, 1, 120, len(address)) + address
        packet = struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 2) + ptr + srv + arecord

        records = parse_packet(packet)

        self.assertEqual(records[0]["value"], "eWeLink_test._ewelink._tcp.local")
        self.assertEqual(records[1]["value"]["port"], 8081)
        self.assertEqual(records[2]["value"], "192.168.1.96")

    def test_only_answers_count_as_devices(self):
        # The eWeLink app on a phone asks for devices, listing the plug it
        # already knows as a known answer: a question that mentions eWeLink.
        service = encode_name("_ewelink._tcp.local")
        instance = encode_name("eWeLink_10000abcde._ewelink._tcp.local")
        known = service + struct.pack("!HHIH", 12, 1, 4500, len(instance)) + instance
        question = service + struct.pack("!HH", 12, 1)
        from_the_app = struct.pack("!HHHHHH", 0, 0x0000, 1, 1, 0, 0) + question + known
        from_a_plug = struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0) + known
        self.assertFalse(is_response(from_the_app))
        self.assertFalse(is_response(make_query()))
        self.assertTrue(is_response(from_a_plug))
        self.assertFalse(is_response(b"\x00"))


    def test_a_question_mentioning_ewelink_is_not_kept(self):
        service = encode_name("_ewelink._tcp.local")
        instance = encode_name("eWeLink_10000abcde._ewelink._tcp.local")
        known = service + struct.pack("!HHIH", 12, 1, 4500, len(instance)) + instance
        question = struct.pack("!HHHHHH", 0, 0x0000, 0, 1, 0, 0) + known
        answer = struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0) + known
        self.assertIsNone(_keep(question, "192.168.0.194", None))
        self.assertEqual(_keep(answer, "192.168.0.50", None)["source"], "192.168.0.50")
        self.assertIsNone(_keep(answer, "192.168.0.50", "192.168.0.51"))  # not the target


class SweepTests(unittest.TestCase):
    def test_every_other_address_on_this_computers_slash_24(self):
        with mock.patch.object(discover_ewelink, "local_ipv4", return_value="192.168.0.10"):
            addresses = sweep_targets()
        self.assertEqual(len(addresses), 253)
        self.assertNotIn("192.168.0.10", addresses)
        self.assertIn("192.168.0.50", addresses)
        self.assertNotIn("192.168.0.255", addresses)

    def test_no_network_means_no_sweep(self):
        with mock.patch.object(discover_ewelink, "local_ipv4", return_value=None):
            self.assertEqual(sweep_targets(), [])

    def test_the_ids_that_answered(self):
        packets = [
            {"source": "192.168.0.50", "records": [{"type": "TXT", "value": ["type=plug", "id=10000abcde"]}]},
            {"source": "192.168.0.51", "records": [{"type": "PTR", "value": "x"}]},
            {"source": "192.168.0.52", "parse_error": "truncated"},
        ]
        self.assertEqual(answered_ids(packets), {"10000abcde"})


if __name__ == "__main__":
    unittest.main()

