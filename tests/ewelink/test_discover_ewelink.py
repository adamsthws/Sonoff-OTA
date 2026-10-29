import socket
import struct
import unittest

from unittest import mock

from tools.ewelink import discover_ewelink
from tools.ewelink.discover_ewelink import _keep, answered_ids, encode_name, sweep_targets, is_response, make_query, parse_packet, read_name


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


def addresses(route, others=()):
    """This computer: the address the mDNS route uses, and its others."""
    return mock.patch.multiple(
        discover_ewelink, local_ipv4=mock.Mock(return_value=route),
        hostsys=mock.Mock(local_addresses=mock.Mock(return_value=set(others))))


class SweepTests(unittest.TestCase):
    def test_every_other_address_on_this_computers_slash_24(self):
        with addresses("192.168.0.110"):
            targets = sweep_targets()
        self.assertEqual(len(targets), 253)
        self.assertNotIn("192.168.0.110", targets)
        self.assertIn("192.168.0.50", targets)
        self.assertNotIn("192.168.0.255", targets)

    def test_no_network_means_no_sweep(self):
        with addresses(None):
            self.assertEqual(sweep_targets(), [])

    def test_every_network_is_swept_not_just_the_routes(self):
        # Windows' multicast route was QEMU's NAT network; the plug was on the other.
        with addresses("10.0.2.15", {"10.0.2.15", "192.168.60.10"}):
            targets = sweep_targets()
        self.assertIn("10.0.2.2", targets)
        self.assertIn("192.168.60.50", targets)
        self.assertNotIn("192.168.60.10", targets)
        self.assertEqual(len(targets), 2 * 253)

    def test_an_interface_given_is_the_only_one_swept(self):
        with addresses("10.0.2.15", {"10.0.2.15", "192.168.60.10"}):
            targets = sweep_targets("192.168.60.10")
        self.assertNotIn("10.0.2.2", targets)
        self.assertEqual(len(targets), 253)


class NetworkTests(unittest.TestCase):
    def test_the_routes_address_comes_first_then_the_other_home_networks(self):
        others = {"192.168.60.10", "10.0.2.15", "127.0.0.1", "169.254.3.4", "8.8.8.8"}
        with addresses("10.0.2.15", others):
            self.assertEqual(discover_ewelink.local_ipv4s(), ["10.0.2.15", "192.168.60.10"])

    def test_the_question_goes_out_of_each_network_it_names(self):
        sock = mock.Mock()
        discover_ewelink._send_group(sock, b"q", "192.168.60.10")
        sock.setsockopt.assert_called_once_with(
            socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("192.168.60.10"))
        sock.sendto.assert_called_once_with(b"q", (discover_ewelink.MDNS_GROUP, discover_ewelink.MDNS_PORT))

    def test_without_a_network_it_goes_where_the_system_sends_it(self):
        sock = mock.Mock()
        discover_ewelink._send_group(sock, b"q", None)
        sock.setsockopt.assert_not_called()
        sock.sendto.assert_called_once()

    def test_the_ids_that_answered(self):
        packets = [
            {"source": "192.168.0.50", "records": [{"type": "TXT", "value": ["type=plug", "id=10000abcde"]}]},
            {"source": "192.168.0.51", "records": [{"type": "PTR", "value": "x"}]},
            {"source": "192.168.0.52", "parse_error": "truncated"},
        ]
        self.assertEqual(answered_ids(packets), {"10000abcde"})


if __name__ == "__main__":
    unittest.main()

