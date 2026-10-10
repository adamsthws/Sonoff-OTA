#!/usr/bin/env python3
"""Discover eWeLink DNS-SD services using an unprivileged mDNS socket.

Background
----------
eWeLink (Sonoff) devices in "DIY mode" advertise themselves on the local
network with multicast DNS / DNS-SD: they publish a ``_ewelink._tcp.local``
service whose SRV record gives the device's port and whose TXT record carries
the device id, encryption flag and JSON state blob.

Rather than depending on a library such as ``zeroconf``, this script speaks
just enough of the DNS wire format (RFC 1035, plus the mDNS extensions in
RFC 6762) to send a PTR query and decode the replies. "Unprivileged" in the
docstring refers to the fallback in :func:`discover`: binding UDP port 5353 is
often impossible (another responder such as avahi/Bonjour already owns it, or
the process lacks the privilege), so the script falls back to an ephemeral
port and asks for unicast replies instead.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import select
import socket
import struct
import sys
import time
from pathlib import Path
from typing import Any

try:
    from tools.autoflash import hostsys
except ModuleNotFoundError:  # Support direct execution as tools/discover_ewelink.py.
    from autoflash import hostsys

# The link-local multicast group and port reserved for mDNS (RFC 6762 §3).
MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
# DNS-SD service type published by eWeLink/Sonoff devices in DIY mode.
SERVICE = "_ewelink._tcp.local"


def encode_name(name: str) -> bytes:
    """Encode a dotted DNS name into wire format.

    Each label is prefixed with its length byte and the whole name is
    terminated by a zero-length label, so ``"_ewelink._tcp.local"`` becomes
    ``b"\\x08_ewelink\\x04_tcp\\x05local\\x00"``. A trailing dot on the input is
    tolerated (stripped) so both ``"x.local"`` and ``"x.local."`` work.
    """
    return b"".join(bytes([len(part)]) + part.encode("utf-8") for part in name.rstrip(".").split(".")) + b"\0"


def read_name(packet: bytes, offset: int, seen: set[int] | None = None) -> tuple[str, int]:
    """Decode a (possibly compressed) DNS name starting at ``offset``.

    Returns ``(name, next_offset)`` where ``next_offset`` is the position in
    ``packet`` immediately after the encoded name - that is, where the caller
    should continue parsing, not where the name's data ended up.

    DNS name compression (RFC 1035 §4.1.4) lets a label sequence end with a
    two-byte pointer (top two bits set) to an earlier name in the same packet.
    When that happens the name continues elsewhere but the *caller's* cursor
    advances only past the two pointer bytes, which is what ``consumed``
    tracks. ``seen`` records pointer targets already followed so a malicious or
    corrupt packet pointing at itself raises instead of recursing forever.

    Raises ``ValueError`` on any truncated or malformed name.
    """
    labels: list[str] = []
    cursor = offset
    # Set only when the name terminated via a compression pointer; it is the
    # offset just past the two pointer bytes.
    consumed: int | None = None
    seen = set() if seen is None else seen
    while True:
        if cursor >= len(packet):
            raise ValueError("DNS name extends beyond packet")
        length = packet[cursor]
        if length == 0:
            # Root label: end of this name.
            cursor += 1
            break
        if length & 0xC0 == 0xC0:
            # Compression pointer: 14-bit offset spread over these two bytes.
            if cursor + 1 >= len(packet):
                raise ValueError("truncated DNS compression pointer")
            pointer = ((length & 0x3F) << 8) | packet[cursor + 1]
            if pointer in seen:
                raise ValueError("DNS compression pointer loop")
            seen.add(pointer)
            # Recurse to read the remainder of the name from the target, then
            # stop: a pointer is always the last thing in a name.
            pointed, _ = read_name(packet, pointer, seen)
            labels.append(pointed)
            consumed = cursor + 2
            cursor = consumed
            break
        # Ordinary label: `length` bytes of text follow the length byte.
        cursor += 1
        end = cursor + length
        if end > len(packet):
            raise ValueError("truncated DNS label")
        labels.append(packet[cursor:end].decode("utf-8", errors="replace"))
        cursor = end
    # Drop empty labels so a bare root name decodes to "" rather than ".".
    return ".".join(label for label in labels if label), consumed or cursor


def _decode_rdata(packet: bytes, rtype: int, offset: int, length: int) -> Any:
    """Decode one record's RDATA into a JSON-friendly Python value.

    ``packet`` (not just the RDATA slice) is passed in because PTR and SRV
    targets may use compression pointers into earlier parts of the packet.
    Record types this script does not care about fall through to a hex dump so
    nothing is silently lost.
    """
    raw = packet[offset : offset + length]
    if rtype in (12,):  # PTR - service instance name, e.g. "eWeLink_1000.…"
        return read_name(packet, offset)[0]
    if rtype == 33 and length >= 6:  # SRV - where the service actually lives
        priority, weight, port = struct.unpack_from("!HHH", packet, offset)
        target = read_name(packet, offset + 6)[0]
        return {"priority": priority, "weight": weight, "port": port, "target": target}
    if rtype == 16:  # TXT - one or more length-prefixed "key=value" strings
        values: list[str] = []
        cursor = 0
        while cursor < len(raw):
            size = raw[cursor]
            cursor += 1
            values.append(raw[cursor : cursor + size].decode("utf-8", errors="replace"))
            cursor += size
        return values
    if rtype == 1 and length == 4:  # A - IPv4 address of the device
        return socket.inet_ntop(socket.AF_INET, raw)
    if rtype == 28 and length == 16:  # AAAA - IPv6 address
        return socket.inet_ntop(socket.AF_INET6, raw)
    return raw.hex()


def parse_packet(packet: bytes) -> list[dict[str, Any]]:
    """Parse a DNS/mDNS message into a flat list of resource records.

    The 12-byte header gives the record counts for the four sections; the
    question section is skipped (its names are re-stated in the answers) and
    the answer, authority and additional sections are decoded. Each returned
    dict is JSON-serialisable so the caller can dump it straight to disk.

    Raises ``ValueError`` if the message is truncated at any point.
    """
    if len(packet) < 12:
        raise ValueError("truncated DNS header")
    # Header: id, flags, then the four section counts.
    _, _, questions, answers, authorities, additionals = struct.unpack_from("!HHHHHH", packet)
    cursor = 12
    for _ in range(questions):
        # Skip each question: its name, then QTYPE + QCLASS (4 bytes).
        _, cursor = read_name(packet, cursor)
        cursor += 4
        if cursor > len(packet):
            raise ValueError("truncated DNS question")
    records: list[dict[str, Any]] = []
    for section, count in (("answer", answers), ("authority", authorities), ("additional", additionals)):
        for _ in range(count):
            name, cursor = read_name(packet, cursor)
            # Fixed part of a resource record: TYPE, CLASS, TTL, RDLENGTH.
            if cursor + 10 > len(packet):
                raise ValueError("truncated DNS record")
            rtype, rclass, ttl, rdlength = struct.unpack_from("!HHIH", packet, cursor)
            cursor += 10
            if cursor + rdlength > len(packet):
                raise ValueError("truncated DNS record data")
            value = _decode_rdata(packet, rtype, cursor, rdlength)
            cursor += rdlength
            records.append(
                {
                    "section": section,
                    "name": name,
                    # Map the numeric type to a readable label, falling back to
                    # the number itself for anything unrecognised.
                    "type": {1: "A", 12: "PTR", 16: "TXT", 28: "AAAA", 33: "SRV"}.get(rtype, str(rtype)),
                    # In mDNS the top CLASS bit is the cache-flush flag, so mask
                    # it off to recover the real class (1 = IN) and expose the
                    # flag separately.
                    "class": rclass & 0x7FFF,
                    "cache_flush": bool(rclass & 0x8000),
                    "ttl": ttl,
                    "value": value,
                }
            )
    return records


def is_response(packet: bytes) -> bool:
    """Whether the message is an answer (the header's QR bit), not a question.

    The eWeLink app asks the same question this tool does, and lists the
    devices it already knows among its known answers, so a phone running it
    mentions eWeLink too. Only a device answering is one.
    """
    return len(packet) >= 4 and bool(struct.unpack_from("!H", packet, 2)[0] & 0x8000)


def make_query(service: str = SERVICE, unicast: bool = True) -> bytes:
    """Build the single-question mDNS query packet asking who offers ``service``."""
    # ID=0, standard mDNS query, one PTR question. With ``unicast`` set, the
    # unicast-response bit lets discovery work when binding multicast port 5353
    # is unavailable.
    #
    # Header fields: id=0, flags=0 (standard query), 1 question, no answers or
    # authority/additional records. The question is QTYPE=12 (PTR) and
    # QCLASS=0x8001 - class IN with the high "QU" bit set, which asks
    # responders to reply directly to our source port instead of multicasting.
    qclass = 0x8001 if unicast else 0x0001
    return struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0) + encode_name(service) + struct.pack("!HH", 12, qclass)


# How often the questions are asked again while listening.
MULTICAST_EVERY = 1.0
DIRECT_EVERY = 3.0
# When nothing wanted has answered by then, every address is asked, once.
SWEEP_AFTER = 2.0


def local_ipv4(interface: str | None = None) -> str | None:
    """This computer's address on the home network: ``interface`` if given,
    else the one the mDNS group is reached through - where the question goes,
    so where the devices answering it are.  Not the default route's: that can
    be a VPN, or Docker's network inside the emulator.  Sends nothing."""
    if interface:
        return interface
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((MDNS_GROUP, MDNS_PORT))  # only picks the route
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def local_ipv4s(interface: str | None = None) -> list[str]:
    """Every address of this computer's a plug could be reached from:
    ``interface`` if given, else the one local_ipv4() finds, then the other
    private ones.  Windows sends multicast out of the adapter that has the
    default route, whatever the metrics - a VPN, a virtual machine's network -
    so the question goes out on each network, not just that one."""
    if interface:
        return [interface]
    found: list[str] = []
    for address in [local_ipv4(), *sorted(hostsys.local_addresses())]:
        if address and address not in found and _could_be_home(address):
            found.append(address)
    return found


def _could_be_home(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.version == 4 and ip.is_private and not ip.is_loopback and not ip.is_link_local


def sweep_targets(interface: str | None = None) -> list[str]:
    """Every other address in each of this computer's /24s, the range
    ``hostsys.sweep`` pings too."""
    mine = local_ipv4s(interface)
    targets: list[str] = []
    for local in mine:
        network = ipaddress.ip_network(f"{local}/24", strict=False)
        targets += [str(host) for host in network.hosts()
                    if str(host) not in mine and str(host) not in targets]
    return targets


def answered_ids(packets: list[dict[str, Any]]) -> set[str]:
    """The eWeLink device ids in the answers so far (their TXT ``id=``)."""
    return {
        str(value)[3:]
        for packet in packets
        for record in packet.get("records", [])
        if record.get("type") == "TXT"
        for value in record.get("value") or []
        if str(value).startswith("id=")
    }


def discover(timeout: float, interface: str | None = None, target: str | None = None, *,
             hints: list[str] | tuple[str, ...] = (),
             wanted: set[str] | None = None) -> dict[str, Any]:
    """Ask who offers the eWeLink service, and collect answers for ``timeout`` seconds.

    A device in Wi-Fi power save can miss most multicast - a stock S60 has
    been seen answering 2 of 30 questions sent to the mDNS group and 20 of 20
    sent to its own address.  So the question goes to the group every second
    and, from a second socket on an ordinary port, straight to devices too; a
    device answers that one to the port it came from (RFC 6762 §6.7):

    - ``target``, or the ``hints`` (addresses this computer already knows),
      every few seconds;
    - without a ``target``, every address in this computer's /24, once, if
      after a couple of seconds the ``wanted`` device ids (or, without them,
      any device at all) have not answered.

    ``interface`` is the local IPv4 address to send multicast from; without
    it, the question goes out from every one in local_ipv4s().
    ``target`` restricts the results to a single device IP.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    bind_port = MDNS_PORT
    try:
        # Preferred path: bind 5353 so we can receive multicast replies too.
        sock.bind(("", MDNS_PORT))
    except OSError:
        # Port already owned by a system responder (avahi/Bonjour) or not
        # permitted - fall back to an ephemeral port. Only unicast replies
        # arrive in this mode, which is why make_query() sets the QU bit.
        bind_port = 0
        sock.bind(("", 0))
    # Each local network the question goes out on; with none known, wherever
    # the system sends multicast.
    outgoing = local_ipv4s(interface) or [None]
    if bind_port == MDNS_PORT:
        # Joining the group is only meaningful (and only permitted) when we
        # actually hold the mDNS port.
        for local in outgoing:
            membership = socket.inet_aton(MDNS_GROUP) + socket.inet_aton(local or "0.0.0.0")
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            except OSError:
                pass  # an adapter that has just gone: the others still count
    # When 5353 is shared with avahi/Bonjour (SO_REUSEADDR), the kernel hands a
    # unicast reply to only one of the sockets on that port - usually the system
    # responder's - so ask for a multicast reply, which every socket receives.
    group_query = make_query(unicast=bind_port != MDNS_PORT)
    # Direct questions come from a port of their own, so their answers come
    # back to it alone, whoever else shares 5353.
    direct = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    direct.bind((interface or "", 0))
    direct_query = make_query(unicast=True)
    addresses = [target] if target else list(dict.fromkeys(hints))

    packets: list[dict[str, Any]] = []
    started = time.monotonic()
    deadline = started + timeout
    next_group = next_direct = started
    sweep_at = None if target else started + SWEEP_AFTER
    try:
        while (now := time.monotonic()) < deadline:
            if now >= next_group:
                for local in outgoing:
                    _send_group(sock, group_query, local)
                next_group = now + MULTICAST_EVERY
            if now >= next_direct:
                for address in addresses:
                    _send(direct, direct_query, address)
                next_direct = now + DIRECT_EVERY
            if sweep_at is not None and now >= sweep_at:
                sweep_at = None
                found = answered_ids(packets)
                if (wanted is not None and not wanted <= found) or (wanted is None and not found):
                    for address in sweep_targets(interface):
                        _send(direct, direct_query, address)
            # Wake for whichever comes first: a packet, a question due or the end.
            due = [next_group, next_direct, deadline] + ([sweep_at] if sweep_at is not None else [])
            wait = max(0.0, min(due) - time.monotonic())
            ready, _, _ = select.select([sock, direct], [], [], wait)
            for receiver in ready:
                try:
                    payload, source = receiver.recvfrom(64 * 1024)
                except OSError:
                    continue
                packet = _keep(payload, source[0], target)
                if packet is not None:
                    packets.append(packet)
    finally:
        sock.close()
        direct.close()
    return {
        "service": SERVICE,
        "target_filter": target,
        "interface": interface,
        "packets": packets,
    }


def _send_group(sock: socket.socket, query: bytes, local: str | None) -> None:
    """The question to the mDNS group, out of the network `local` is on."""
    if local is not None:
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local))
        except OSError:
            return
    _send(sock, query, MDNS_GROUP)


def _send(sock: socket.socket, query: bytes, address: str) -> None:
    try:
        sock.sendto(query, (address, MDNS_PORT))
    except OSError:
        pass  # an unreachable address, or no route right now: the others still count


def _keep(payload: bytes, source: str, target: str | None) -> dict[str, Any] | None:
    """The packet as reported, if it is a device answering about eWeLink."""
    if target and source != target:
        return None
    if not is_response(payload):
        # A question - ours, or the eWeLink app's on a phone - not a device.
        return None
    try:
        records = parse_packet(payload)
    except ValueError as exc:
        # Keep a note of unparseable traffic rather than dropping it, so a
        # failed discovery run is still diagnosable.
        return {"source": source, "parse_error": str(exc)}
    # The socket also sees unrelated mDNS chatter (printers, AirPlay, …);
    # keep only packets that mention eWeLink anywhere in their decoded form.
    if any("ewelink" in json.dumps(record).lower() for record in records):
        return {"source": source, "records": records}
    return None


def main() -> int:
    """CLI entry point: run discovery and print or save the JSON result."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--interface", help="local IPv4 address used for multicast")
    parser.add_argument("--target", help="only retain replies from this device IP")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(
        f"Listening for eWeLink devices for {args.timeout:g} seconds...",
        file=sys.stderr,
        flush=True,
    )
    result = discover(args.timeout, args.interface, args.target)
    # sort_keys keeps runs diffable against one another.
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        sources = sorted(
            {packet.get("source") for packet in result["packets"] if packet.get("source")}
        )
        if sources:
            print(f"Found eWeLink device(s): {', '.join(sources)}")
        else:
            print("No eWeLink devices replied.")
        print(f"Saved discovery report to {args.output}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
