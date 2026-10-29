"""Noise_NNpsk0_25519_ChaChaPoly_SHA256, as ESPHome speaks it, in plain Python.

An ESPHome with an API encryption key (`api: encryption: key:`, 32 bytes in
base64) encrypts its native API with the Noise protocol, keyed by it - and,
from ESPHome 2026.9, its updates too: offered whenever it has the key, and
the only kind it takes with `ota: encryption:`.  The key is shared, so there
are no certificates: each end proves it has the key, and every message after
is sealed with keys made for that connection.

All it takes is X25519 (RFC 7748), ChaCha20-Poly1305 (RFC 8439) and SHA-256,
so the tool needs no package for it: the first two are here, written from
their RFCs and checked against their test vectors, and the handshake against
ESPHome's own client (tests/autoflash/test_noise.py).  Plain Python is slower than a
compiled library - about two seconds a megabyte - and not constant-time;
its keys are ephemeral, made for one connection on the home network, and the
shared key is only ever hashed.

Both of ESPHome's protocols frame each message the same way: 0x01, the
length (two bytes, big-endian), then the message.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import struct
from collections.abc import Callable

PROTOCOL_NAME = b"Noise_NNpsk0_25519_ChaChaPoly_SHA256"
KEY_SIZE = 32
TAG_SIZE = 16
FRAME = 0x01
HANDSHAKE_OK = 0x00
# What ESPHome says, in the frame that refuses a handshake, when the other end
# has another key: the first message's tag did not check out.
WRONG_KEY_REASON = "Handshake MAC failure"


class NoiseError(ValueError):
    """A message that does not check out: the other end has another key, or
    it was damaged on the way."""


def decode_key(text: str) -> bytes:
    """An ESPHome API encryption key as its YAML config has it: 32 bytes in
    base64.  Raises ValueError for anything else."""
    text = text.strip().strip("'\"").strip()
    try:
        key = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("it is not base64") from None
    if len(key) != KEY_SIZE:
        raise ValueError(f"it is {len(key)} bytes, not 32")
    return key


# ------------------------------------------------------------------ X25519
_P = 2**255 - 19
_A24 = 121665
_BASE = (9).to_bytes(32, "little")


def x25519(scalar: bytes, point: bytes) -> bytes:
    """RFC 7748's X25519: the u-coordinate of `scalar` times `point`."""
    k = int.from_bytes(scalar, "little")
    k = (k & ~7 & ((1 << 255) - 1)) | (1 << 254)
    u = int.from_bytes(point, "little") & ((1 << 255) - 1)
    x1, x2, z2, x3, z3, swap = u, 1, 0, u, 1, 0
    for bit in range(254, -1, -1):
        k_t = (k >> bit) & 1
        swap ^= k_t
        if swap:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = k_t
        a = x2 + z2
        aa = a * a % _P
        b = x2 - z2
        bb = b * b % _P
        e = aa - bb
        c = x3 + z3
        d = x3 - z3
        da = d * a % _P
        cb = c * b % _P
        x3 = (da + cb) * (da + cb) % _P
        z3 = x1 * (da - cb) * (da - cb) % _P
        x2 = aa * bb % _P
        z2 = e * (aa + _A24 * e) % _P
    if swap:
        x2, z2 = x3, z3
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


def public_key(private: bytes) -> bytes:
    return x25519(private, _BASE)


# ------------------------------------------------------- ChaCha20-Poly1305
_M = 0xFFFFFFFF
_SIGMA = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)


def _quarter(x: list[int], a: int, b: int, c: int, d: int) -> None:
    """RFC 8439 2.1's quarter round, on four of the state's words."""
    x[a] = (x[a] + x[b]) & _M
    x[d] ^= x[a]
    x[d] = (x[d] << 16 | x[d] >> 16) & _M
    x[c] = (x[c] + x[d]) & _M
    x[b] ^= x[c]
    x[b] = (x[b] << 12 | x[b] >> 20) & _M
    x[a] = (x[a] + x[b]) & _M
    x[d] ^= x[a]
    x[d] = (x[d] << 8 | x[d] >> 24) & _M
    x[c] = (x[c] + x[d]) & _M
    x[b] ^= x[c]
    x[b] = (x[b] << 7 | x[b] >> 25) & _M


def _block(key: tuple[int, ...], counter: int, nonce: tuple[int, ...]) -> bytes:
    """One 64-byte ChaCha20 block (RFC 8439 2.3)."""
    state = [*_SIGMA, *key, counter & _M, *nonce]
    x = list(state)
    for _ in range(10):
        _quarter(x, 0, 4, 8, 12)
        _quarter(x, 1, 5, 9, 13)
        _quarter(x, 2, 6, 10, 14)
        _quarter(x, 3, 7, 11, 15)
        _quarter(x, 0, 5, 10, 15)
        _quarter(x, 1, 6, 11, 12)
        _quarter(x, 2, 7, 8, 13)
        _quarter(x, 3, 4, 9, 14)
    return struct.pack("<16I", *((word + start) & _M for word, start in zip(x, state)))


def chacha20(key: bytes, counter: int, nonce: bytes, data: bytes) -> bytes:
    """`data` encrypted, or decrypted, with ChaCha20 from block `counter`."""
    words, nonce_words = struct.unpack("<8I", key), struct.unpack("<3I", nonce)
    out = bytearray()
    for offset in range(0, len(data), 64):
        chunk = data[offset:offset + 64]
        stream = _block(words, counter + offset // 64, nonce_words)[:len(chunk)]
        out += (int.from_bytes(chunk, "little") ^ int.from_bytes(stream, "little")).to_bytes(
            len(chunk), "little")
    return bytes(out)


_P1305 = (1 << 130) - 5
_CLAMP = 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF


def poly1305(key: bytes, message: bytes) -> bytes:
    """RFC 8439 2.5's Poly1305 tag of `message` with the one-time `key`."""
    r = int.from_bytes(key[:16], "little") & _CLAMP
    s = int.from_bytes(key[16:32], "little")
    acc = 0
    for offset in range(0, len(message), 16):
        acc = (acc + int.from_bytes(message[offset:offset + 16] + b"\x01", "little")) * r % _P1305
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, "little")


def _tag(key: bytes, nonce: bytes, ad: bytes, ciphertext: bytes) -> bytes:
    one_time = _block(struct.unpack("<8I", key), 0, struct.unpack("<3I", nonce))[:32]
    return poly1305(one_time, ad + bytes(-len(ad) % 16) + ciphertext + bytes(-len(ciphertext) % 16)
                    + struct.pack("<QQ", len(ad), len(ciphertext)))


def seal(key: bytes, nonce: bytes, ad: bytes, plaintext: bytes) -> bytes:
    """RFC 8439 2.8's AEAD: the ciphertext, then its 16-byte tag."""
    ciphertext = chacha20(key, 1, nonce, plaintext)
    return ciphertext + _tag(key, nonce, ad, ciphertext)


def open_sealed(key: bytes, nonce: bytes, ad: bytes, sealed: bytes) -> bytes:
    """The plaintext of what seal() made; NoiseError if its tag does not check out."""
    if len(sealed) < TAG_SIZE:
        raise NoiseError("the message is shorter than its tag")
    ciphertext, tag = sealed[:-TAG_SIZE], sealed[-TAG_SIZE:]
    if not hmac.compare_digest(_tag(key, nonce, ad, ciphertext), tag):
        raise NoiseError("the message does not check out against its tag")
    return chacha20(key, 1, nonce, ciphertext)


# ------------------------------------------------------------------- Noise
class CipherState:
    """One direction's key, and its count of messages (Noise's nonce)."""

    def __init__(self, key: bytes | None = None) -> None:
        self.key = key
        self.nonce = 0

    def _nonce(self) -> bytes:
        return bytes(4) + self.nonce.to_bytes(8, "little")

    def encrypt(self, plaintext: bytes, ad: bytes = b"") -> bytes:
        if self.key is None:
            return plaintext
        sealed = seal(self.key, self._nonce(), ad, plaintext)
        self.nonce += 1
        return sealed

    def decrypt(self, sealed: bytes, ad: bytes = b"") -> bytes:
        if self.key is None:
            return sealed
        plaintext = open_sealed(self.key, self._nonce(), ad, sealed)
        self.nonce += 1
        return plaintext


def _hkdf(chaining_key: bytes, material: bytes, outputs: int) -> list[bytes]:
    temp = hmac.new(chaining_key, material, hashlib.sha256).digest()
    out, previous = [], b""
    for index in range(1, outputs + 1):
        previous = hmac.new(temp, previous + bytes([index]), hashlib.sha256).digest()
        out.append(previous)
    return out


class Handshake:
    """One end of the two-message NNpsk0 handshake - `psk, e` from the
    initiator (this tool), `e, ee` back - after which split() gives the
    ciphers for sending and for receiving.  `ephemeral`: this end's private
    key for the connection, for tests; a random one otherwise."""

    def __init__(self, psk: bytes, prologue: bytes, *, initiator: bool = True,
                 ephemeral: bytes | None = None) -> None:
        if len(psk) != KEY_SIZE:
            raise ValueError("the key is 32 bytes")
        self.h = hashlib.sha256(PROTOCOL_NAME).digest()  # the name is longer than a hash
        self.ck = self.h
        self.cipher = CipherState()
        self._mix_hash(prologue)
        self.psk = psk
        self.initiator = initiator
        self.private = ephemeral if ephemeral is not None else secrets.token_bytes(32)
        self.public = public_key(self.private)
        self.remote: bytes | None = None
        self.messages = 0

    def _mix_hash(self, data: bytes) -> None:
        self.h = hashlib.sha256(self.h + data).digest()

    def _mix_key(self, material: bytes) -> None:
        self.ck, key = _hkdf(self.ck, material, 2)
        self.cipher = CipherState(key)

    def _mix_key_and_hash(self, material: bytes) -> None:
        self.ck, hashed, key = _hkdf(self.ck, material, 3)
        self._mix_hash(hashed)
        self.cipher = CipherState(key)

    def _e(self, public: bytes) -> None:
        # In a handshake with a pre-shared key, each ephemeral key is mixed
        # into the key as well as the hash.
        self._mix_hash(public)
        self._mix_key(public)

    def _ee(self) -> None:
        assert self.remote is not None
        shared = x25519(self.private, self.remote)
        if not any(shared):
            raise NoiseError("the other end sent an unusable key")
        self._mix_key(shared)

    def write_message(self, payload: bytes = b"") -> bytes:
        if self.initiator and self.messages == 0:
            self._mix_key_and_hash(self.psk)
            self._e(self.public)
        elif not self.initiator and self.messages == 1:
            self._e(self.public)
            self._ee()
        else:
            raise RuntimeError("not this end's turn to write")
        self.messages += 1
        sealed = self.cipher.encrypt(payload, self.h)
        self._mix_hash(sealed)
        return self.public + sealed

    def read_message(self, message: bytes) -> bytes:
        if len(message) < KEY_SIZE + TAG_SIZE:
            raise NoiseError("the handshake message is too short")
        self.remote = message[:KEY_SIZE]
        if not self.initiator and self.messages == 0:
            self._mix_key_and_hash(self.psk)
            self._e(self.remote)
        elif self.initiator and self.messages == 1:
            self._e(self.remote)
            self._ee()
        else:
            raise RuntimeError("not this end's turn to read")
        self.messages += 1
        sealed = message[KEY_SIZE:]
        payload = self.cipher.decrypt(sealed, self.h)
        self._mix_hash(sealed)
        return payload

    def split(self) -> tuple[CipherState, CipherState]:
        """(sending, receiving), once both messages are through."""
        if self.messages != 2:
            raise RuntimeError("the handshake is not finished")
        first, second = _hkdf(self.ck, b"", 2)
        if self.initiator:
            return CipherState(first), CipherState(second)
        return CipherState(second), CipherState(first)


# ------------------------------------------------------------------ frames
def frame(payload: bytes) -> bytes:
    return bytes([FRAME]) + len(payload).to_bytes(2, "big") + payload


def read_frame(read: Callable[[int], bytes]) -> bytes:
    """One frame's payload, `read(n)` giving exactly n bytes or raising.
    NoiseError for what is not a frame: the other end does not speak Noise."""
    header = read(3)
    if header[0] != FRAME:
        raise NoiseError(f"not a Noise frame (it starts 0x{header[0]:02X})")
    return read(int.from_bytes(header[1:3], "big"))


def handshake_reply(handshake: Handshake, payload: bytes) -> None:
    """The responder's handshake frame, read into `handshake`: ESPHome's
    status byte, then its message.  A refusal raises NoiseError with
    ESPHome's reason - WRONG_KEY_REASON when it has another key."""
    if not payload:
        raise NoiseError("the handshake reply is empty")
    if payload[0] != HANDSHAKE_OK:
        raise NoiseError(payload[1:].decode("utf-8", "replace") or "the handshake was refused")
    try:
        handshake.read_message(payload[1:])
    except NoiseError:
        # The plug's own refusal is the usual way to say so; this is the
        # same, said by the reply not checking out.
        raise NoiseError(WRONG_KEY_REASON) from None
