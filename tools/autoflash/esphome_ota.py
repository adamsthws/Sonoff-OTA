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
the plug starts what it was sent.

From ESPHome 2026.9, one with an API encryption key also takes the update
encrypted (tools/autoflash/noise.py), keyed by it - and with `ota:
encryption:`, only that way.  Given the key, the client says so in its
feature flags; once the plug has said it can too, a Noise handshake follows,
sealed to every byte both ends sent before it, and the rest of the session is
the same, in encrypted frames.  ESPHome asks for no password then: the key is
its proof.
"""

from __future__ import annotations

import gzip
import hashlib
import secrets
import socket
from dataclasses import dataclass

from . import noise

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
CLIENT_NOISE = 0x08
SERVER_COMPRESSION = 0x01
SERVER_PARTITION_ACCESS = 0x02
SERVER_NOISE = 0x04

# What starts the handshake's prologue, ahead of every byte of the session so
# far; and the most an encrypted frame holds, less its tag (espota2's).
NOISE_PROLOGUE = b"NoiseOTAInit"
NOISE_MAX_PLAINTEXT = 1024

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
    made with its API encryption key, and none was given."""

    def __init__(self) -> None:
        super().__init__("ESPHome on the plug takes only encrypted updates (encryption: under ota: "
                         "in its ESPHome YAML config), made with its API encryption key")


class WrongKey(OTAError):
    """The API encryption key given is not the one ESPHome on the plug has."""

    def __init__(self) -> None:
        super().__init__("the API encryption key given is not the one ESPHome on the plug has")


@dataclass
class Offer:
    version: int
    extended: bool  # it takes an update type: the extended protocol
    compression: bool
    partition_access: bool
    encrypted_only: bool = False  # it refused to go on unencrypted
    encryption: bool = False  # it takes encrypted updates: it has an API encryption key
    sent: int = 0  # the feature flags the client sent, and the plug's: both go
    flags: int = 0  # into an encrypted session's prologue


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


def _handshake(sock: socket.socket, key: bytes | None = None) -> Offer:
    sock.sendall(MAGIC)
    _expect(sock, (OK,), "greeting")
    version = _recv(sock, 1, "version")[0]
    if version not in (1, 2):
        raise OTAError(f"unsupported ESPHome update protocol {version}")
    # SHA256 offered for the password challenge: current ESPHome, if it has a
    # password, refuses a client that does not offer it.  Encryption, only
    # with a key to make it.
    sent = CLIENT_COMPRESSION | CLIENT_SHA256_AUTH | CLIENT_EXTENDED | (CLIENT_NOISE if key else 0)
    sock.sendall(bytes([sent]))
    answer = _recv(sock, 1, "features")[0]
    if answer == ENCRYPTION_REQUIRED:
        return Offer(version=version, extended=False, compression=False, partition_access=False,
                     encrypted_only=True, sent=sent)
    if answer in ERRORS:
        raise OTAError(f"features: {ERRORS[answer]}")
    if answer == FEATURE_FLAGS:
        flags = _recv(sock, 1, "feature flags")[0]
    else:
        flags = SERVER_COMPRESSION if answer == SUPPORTS_COMPRESSION else 0
    extended = answer == FEATURE_FLAGS
    return Offer(version=version, extended=extended, compression=bool(flags & SERVER_COMPRESSION),
                 partition_access=extended and bool(flags & SERVER_PARTITION_ACCESS),
                 encryption=extended and bool(flags & SERVER_NOISE), sent=sent, flags=flags)


class _Encrypted:
    """The rest of a session, sealed.  Each sendall() goes in frames of its
    own - the plug reads each answer, the size and the checksum as one frame -
    with data split at NOISE_MAX_PLAINTEXT; recv() hands out what each frame
    decrypts to."""

    def __init__(self, sock: socket.socket, ciphers: tuple[noise.CipherState, noise.CipherState]) -> None:
        self.sock = sock
        self.sending, self.receiving = ciphers
        self.pending = b""

    def settimeout(self, timeout: float) -> None:
        self.sock.settimeout(timeout)

    def sendall(self, data: bytes) -> None:
        self.sock.sendall(b"".join(
            noise.frame(self.sending.encrypt(data[at:at + NOISE_MAX_PLAINTEXT]))
            for at in range(0, len(data), NOISE_MAX_PLAINTEXT)))

    def recv(self, count: int) -> bytes:
        if not self.pending:
            try:
                sealed = noise.read_frame(lambda size: _recv(self.sock, size, "an encrypted answer"))
                self.pending = self.receiving.decrypt(sealed)
            except noise.NoiseError as exc:
                raise OTAError(f"an encrypted answer: {exc}") from None
            if not self.pending:
                raise OTAError("the plug sent an empty encrypted answer")
        data, self.pending = self.pending[:count], self.pending[count:]
        return data


def _encrypt(sock: socket.socket, offer: Offer, key: bytes) -> _Encrypted:
    """The session encrypted from here, after the handshake, which binds the
    greeting and both ends' feature flags (any of them changed on the way,
    and it fails).  WrongKey when the plug has another key."""
    prologue = NOISE_PROLOGUE + MAGIC + bytes([OK, offer.version, offer.sent, FEATURE_FLAGS, offer.flags])
    handshake = noise.Handshake(key, prologue)
    sock.sendall(noise.frame(bytes([noise.HANDSHAKE_OK]) + handshake.write_message()))
    try:
        noise.handshake_reply(handshake, noise.read_frame(
            lambda size: _recv(sock, size, "the encryption handshake")))
    except noise.NoiseError as exc:
        if str(exc) == noise.WRONG_KEY_REASON:
            raise WrongKey() from None
        raise OTAError(f"the encryption handshake: {exc}") from None
    return _Encrypted(sock, handshake.split())


def _open(sock: socket.socket, password: str | None, key: bytes | None) -> tuple[Offer, socket.socket]:
    """The session through to where the plug is ready for an update: the
    greeting, encrypted from there if both ends can, and its password."""
    offer = _handshake(sock, key)
    if offer.encrypted_only:
        raise NeedsEncryption()
    if key and offer.encryption:
        sock = _encrypt(sock, offer, key)  # type: ignore[assignment]
    _authenticate(sock, password)
    return offer, sock


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


# What wants() says the plug wants, besides what it was given.
NOTHING, PASSWORD, KEY = "", "password", "key"


def wants(host: str, password: str | None = None, key: bytes | None = None, *, port: int = PORT,
          timeout: float = 5.0) -> str | None:
    """What ESPHome's update service at `host` wants for an update that
    `password` and `key` (its API encryption key, 32 bytes) are not: NOTHING,
    PASSWORD (it has an update password: none was given, or not it), or KEY
    (it takes only encrypted updates and no key was given, or the key given
    is not its own); None when it does not answer.

    Nothing is updated: the session ends where the plug is ready for one,
    before it starts an update - which it does only once told its type and
    size, so that a scan cannot start one.  The plug notes the session
    failed, as it does for the one probe() leaves.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            _open(sock, password, key)
            return NOTHING
    except NeedsPassword:
        return PASSWORD
    except (NeedsEncryption, WrongKey):
        return KEY
    except (OSError, OTAError):
        return None


def upload(host: str, data: bytes, *, kind: int = APP, port: int = PORT,
           timeout: float = 10.0, progress=None, password: str | None = None,
           key: bytes | None = None) -> None:
    """Send `data` as an update of `kind` - encrypted with `key`, the plug's
    API encryption key, where it takes that; else with its update `password`
    if it has one.  Returns once the plug has it all and says the update ended
    (it then restarts into it by itself)."""
    with socket.create_connection((host, port), timeout=timeout) as raw:
        raw.settimeout(timeout)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        offer = _handshake(raw, key)
        if offer.encrypted_only:
            raise NeedsEncryption()
        if kind != APP and not offer.partition_access:
            raise OTAError("this ESPHome was built without allow_partition_access")
        sock = _encrypt(raw, offer, key) if key and offer.encryption else raw
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
