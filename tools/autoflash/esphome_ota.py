"""ESPHome's own update protocol (port 3232), as its espota2.py speaks it.

Only what the run needs: an update to a plug with no update password, of an
app (written by ESPHome into its other app slot, which it then starts) or,
from a firmware built with allow_partition_access (the mover), a partition
table - which ESPHome checks, moving the running app into a slot of the new
table first when it has to.

The wire protocol: five magic bytes; the plug answers OK and its protocol
version; feature flags both ways; its auth (none here); the update type; the
size, the MD5 of what follows, then the data in 8 KiB blocks, each
acknowledged; then "received" and "update ended", after which the plug starts
what it was sent.
"""

from __future__ import annotations

import gzip
import hashlib
import socket
from dataclasses import dataclass

PORT = 3232

APP = 0x00
PARTITION_TABLE = 0x01
BOOTLOADER = 0x02

MAGIC = bytes([0x6C, 0x26, 0xF7, 0x5C, 0x45])
OK = 0x00
REQUEST_AUTH = 0x01
REQUEST_SHA256_AUTH = 0x02
AUTH_OK = 0x41
UPDATE_PREPARE_OK = 0x42
BIN_MD5_OK = 0x43
RECEIVE_OK = 0x44
UPDATE_END_OK = 0x45
SUPPORTS_COMPRESSION = 0x46
CHUNK_OK = 0x47
FEATURE_FLAGS = 0x48

CLIENT_COMPRESSION = 0x01
CLIENT_SHA256_AUTH = 0x02
CLIENT_EXTENDED = 0x04
SERVER_COMPRESSION = 0x01
SERVER_PARTITION_ACCESS = 0x02

BLOCK = 8192
# Longer than the plug's own data timeout (105 s), as espota2 has it.
DATA_TIMEOUT = 160.0

# What the plug says when it refuses, in words for the run's log.
ERRORS = {
    0x80: "invalid magic bytes",
    0x81: "could not prepare the flash for the update",
    0x82: "update password refused",
    0x83: "writing the flash failed",
    0x84: "finishing the update failed",
    0x89: "the image does not fit the slot",
    0x8A: "no slot to update",
    0x8B: "the image was damaged on the way (MD5 differs)",
    0x8E: "this firmware cannot take that kind of update",
    0x8F: "the new partition table was refused",
    0x90: "writing the partition table failed",
    0x91: "the new bootloader was refused",
    0x92: "writing the bootloader failed",
    0x93: "it refuses an older version",
    0x94: "it requires an encrypted update",
}


class OTAError(RuntimeError):
    """The plug refused, or the update could not be completed."""


class NeedsPassword(OTAError):
    """ESPHome on the plug has an update password (set in Home Assistant, say)."""


@dataclass
class Offer:
    version: int
    extended: bool  # it takes an update type: the extended protocol
    compression: bool
    partition_access: bool


def _recv(sock: socket.socket, count: int, what: str) -> bytes:
    data = b""
    while len(data) < count:
        piece = sock.recv(count - len(data))
        if not piece:
            raise OTAError(f"the plug closed the connection at {what}")
        data += piece
    return data


def _expect(sock: socket.socket, wanted: tuple[int, ...], what: str) -> int:
    code = _recv(sock, 1, what)[0]
    if code in ERRORS:
        raise OTAError(f"{what}: {ERRORS[code]}")
    if code not in wanted:
        raise OTAError(f"{what}: unexpected answer 0x{code:02X}")
    return code


def _handshake(sock: socket.socket) -> Offer:
    sock.sendall(MAGIC)
    _expect(sock, (OK,), "greeting")
    version = _recv(sock, 1, "version")[0]
    if version not in (1, 2):
        raise OTAError(f"unsupported ESPHome update protocol {version}")
    sock.sendall(bytes([CLIENT_COMPRESSION | CLIENT_EXTENDED]))
    answer = _recv(sock, 1, "features")[0]
    if answer == FEATURE_FLAGS:
        flags = _recv(sock, 1, "feature flags")[0]
    else:
        flags = SERVER_COMPRESSION if answer == SUPPORTS_COMPRESSION else 0
    extended = answer == FEATURE_FLAGS
    return Offer(version=version, extended=extended, compression=bool(flags & SERVER_COMPRESSION),
                 partition_access=extended and bool(flags & SERVER_PARTITION_ACCESS))


def probe(host: str, *, port: int = PORT, timeout: float = 4.0) -> Offer | None:
    """What ESPHome's update service at `host` offers, or None if none answers.

    The plug drops a session left at this point by itself.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            return _handshake(sock)
    except (OSError, OTAError):
        return None


def upload(host: str, data: bytes, *, kind: int = APP, port: int = PORT,
           timeout: float = 10.0, progress=None) -> None:
    """Send `data` as an update of `kind`; returns once the plug has it all and
    says the update ended (it then restarts into it by itself)."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        offer = _handshake(sock)
        if kind != APP and not offer.partition_access:
            raise OTAError("this ESPHome was built without allow_partition_access")
        auth = _expect(sock, (AUTH_OK, REQUEST_AUTH, REQUEST_SHA256_AUTH), "authentication")
        if auth != AUTH_OK:
            raise NeedsPassword("ESPHome on the plug has an update password")
        sock.settimeout(DATA_TIMEOUT)
        if offer.extended:
            sock.sendall(bytes([kind]))
        payload = gzip.compress(data, compresslevel=9) if offer.compression else data
        sock.sendall(len(payload).to_bytes(4, "big"))
        _expect(sock, (UPDATE_PREPARE_OK,), "preparing")
        sock.sendall(hashlib.md5(payload).hexdigest().encode("ascii"))
        _expect(sock, (BIN_MD5_OK,), "checksum")
        sent = 0
        while sent < len(payload):
            block = payload[sent:sent + BLOCK]
            sock.sendall(block)
            sent += len(block)
            if offer.version >= 2:
                _expect(sock, (CHUNK_OK,), f"block at {sent}")
            if progress:
                progress(sent, len(payload))
        _expect(sock, (RECEIVE_OK,), "receiving")
        _expect(sock, (UPDATE_END_OK,), "finishing")
        try:
            sock.sendall(bytes([OK]))
        except OSError:
            pass  # already restarting into it, which is what was asked
