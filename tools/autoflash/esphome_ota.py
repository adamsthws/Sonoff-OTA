"""ESPHome's own update protocol (port 3232), as its espota2.py speaks it.

Only what the run needs: an update, with the plug's update password if it
has one, of an app (written by ESPHome into its other app slot, which it then
starts) or, from a firmware built with allow_partition_access (the mover), a
partition table - which ESPHome checks, moving the running app into a slot of
the new table first when it has to.

The wire protocol: five magic bytes; the plug answers OK and its protocol
version; feature flags both ways; its password challenge, if it has one (a
nonce, answered with ours and the hash of the password with both); the
update type; the size, the MD5 of what follows, then the data in 8 KiB
blocks, each acknowledged; then "received" and "update ended", after which
the plug starts what it was sent.  One set to take only encrypted updates
(`ota: encryption:`, keyed by its API key) is not spoken to.
"""

from __future__ import annotations

import gzip
import hashlib
import secrets
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

AUTH_INVALID = 0x82
ENCRYPTION_REQUIRED = 0x94

CLIENT_COMPRESSION = 0x01
CLIENT_SHA256_AUTH = 0x02
CLIENT_EXTENDED = 0x04
SERVER_COMPRESSION = 0x01
SERVER_PARTITION_ACCESS = 0x02

# The plug's password challenge, by the answer that makes it: the hash, and
# the length of its nonce in hex.  SHA256 in current ESPHome, which takes no
# other; MD5 in older ones.
CHALLENGES = {REQUEST_SHA256_AUTH: (hashlib.sha256, 64), REQUEST_AUTH: (hashlib.md5, 32)}

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
    """ESPHome on the plug has an update password (set in Home Assistant, say),
    and none was given, or not the right one."""

    def __init__(self, given: bool) -> None:
        super().__init__("ESPHome on the plug has an update password"
                         + (", and the one given is not it" if given else ""))
        self.given = given


class NeedsEncryption(OTAError):
    """ESPHome on the plug takes only encrypted updates (`ota: encryption:`),
    made with its API key."""

    def __init__(self) -> None:
        super().__init__("ESPHome on the plug takes only encrypted updates (its ota config has "
                         "encryption:), which this tool cannot make")


@dataclass
class Offer:
    version: int
    extended: bool  # it takes an update type: the extended protocol
    compression: bool
    partition_access: bool
    encrypted_only: bool = False  # it refused to go on unencrypted


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
    # SHA256 offered for the password challenge: current ESPHome, if it has a
    # password, refuses a client that does not offer it.
    sock.sendall(bytes([CLIENT_COMPRESSION | CLIENT_SHA256_AUTH | CLIENT_EXTENDED]))
    answer = _recv(sock, 1, "features")[0]
    if answer == ENCRYPTION_REQUIRED:
        return Offer(version=version, extended=False, compression=False, partition_access=False,
                     encrypted_only=True)
    if answer in ERRORS:
        raise OTAError(f"features: {ERRORS[answer]}")
    if answer == FEATURE_FLAGS:
        flags = _recv(sock, 1, "feature flags")[0]
    else:
        flags = SERVER_COMPRESSION if answer == SUPPORTS_COMPRESSION else 0
    extended = answer == FEATURE_FLAGS
    return Offer(version=version, extended=extended, compression=bool(flags & SERVER_COMPRESSION),
                 partition_access=extended and bool(flags & SERVER_PARTITION_ACCESS))


def _authenticate(sock: socket.socket, password: str | None) -> None:
    """Through the plug's password challenge, if it makes one."""
    asked = _recv(sock, 1, "authentication")[0]
    if asked == AUTH_OK:
        return
    if asked == AUTH_INVALID or (asked in CHALLENGES and not password):
        raise NeedsPassword(given=bool(password))
    if asked not in CHALLENGES:
        raise OTAError(f"authentication: {ERRORS.get(asked, f'unexpected answer 0x{asked:02X}')}")
    hash_function, size = CHALLENGES[asked]
    nonce = _recv(sock, size, "the password challenge")
    ours = secrets.token_hex(size // 2).encode("ascii")
    proof = hash_function(password.encode("utf-8") + nonce + ours).hexdigest().encode("ascii")
    sock.sendall(ours + proof)
    answer = _recv(sock, 1, "the password")[0]
    if answer == AUTH_INVALID:
        raise NeedsPassword(given=True)
    if answer != AUTH_OK:
        raise OTAError(f"the password: {ERRORS.get(answer, f'unexpected answer 0x{answer:02X}')}")


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


def wants_password(host: str, password: str | None = None, *, port: int = PORT,
                   timeout: float = 5.0) -> bool | None:
    """Whether ESPHome's update service at `host` wants a password that
    `password` is not, none or another: True when it says so, False when it
    takes it (or wants none), None when it does not answer.  Raises
    NeedsEncryption for one that takes only encrypted updates.

    Nothing is updated: the session ends at the password, before the plug
    starts an update - which it does only once told its type and size, so
    that a scan cannot start one.  The plug notes the session failed, as it
    does for the one probe() leaves.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            if _handshake(sock).encrypted_only:
                raise NeedsEncryption()
            _authenticate(sock, password)
            return False
    except NeedsPassword:
        return True
    except NeedsEncryption:
        raise
    except (OSError, OTAError):
        return None


def upload(host: str, data: bytes, *, kind: int = APP, port: int = PORT,
           timeout: float = 10.0, progress=None, password: str | None = None) -> None:
    """Send `data` as an update of `kind`, with the plug's update `password` if
    it has one; returns once the plug has it all and says the update ended (it
    then restarts into it by itself)."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        offer = _handshake(sock)
        if offer.encrypted_only:
            raise NeedsEncryption()
        if kind != APP and not offer.partition_access:
            raise OTAError("this ESPHome was built without allow_partition_access")
        _authenticate(sock, password)
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
