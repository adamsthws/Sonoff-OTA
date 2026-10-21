"""ESPHome instead of the final Tasmota: the image, and the plug once it runs it.

The run installs a pre-built ESPHome (artifacts/esphome-sonoff-s60-imprintable-*.bin,
from ESPHome/sonoff_s60.yaml) rather than asking anyone to build one: building it
takes gigabytes of tools and minutes of compiling. Like the recovery Safeboot,
it holds placeholders instead of a Wi-Fi name and password, and the plug's own
Wi-Fi is written into them in memory (imprint.py) - so the image on disk never
holds it, and the plug joins the home network by itself when it starts.
"""

from __future__ import annotations

import gzip
import hashlib
import http.client
import json
import math
import re
import socket
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable
from pathlib import Path

from . import imprint, noise

ROOT = Path(__file__).resolve().parents[2]
# The published image, the ESPHome made for the Sonoff S60 (kinds.SONOFF_S60),
# pinned: a changed file is refused, never used unseen.
IMAGE = ROOT / "artifacts" / "esphome-sonoff-s60-imprintable-1.2.0.bin"
IMAGE_SHA256 = "bf9863ca68811d871e52e02bf0cdec63571f1baba087b271107a2c45766f45d4"
IMAGE_SIZE = 955_376
# ESPHome's bootloader, from the same build, put in place of Sonoff's: Sonoff's
# (ESP-IDF 4.4) starts ESPHome but cannot roll back an update that fails to
# start ("Bootloader too old for OTA rollback").  It is all that sits before
# the partition table at 0x8000, and the one part the plug cannot start without.
BOOTLOADER = ROOT / "artifacts" / "esphome-sonoff-s60-bootloader-1.2.0.bin"
BOOTLOADER_SHA256 = "3792bb665916d69c410c9fd10f69d1a6968e61f2a9e4969829a1c49f9ea6f433"
BOOTLOADER_SIZE = 21_088
BOOTLOADER_ROOM = 0x8000
# Sonoff's app slots, which the run leaves as they are: ESPHome goes into ota_0.
SLOT_ADDRESS = 0x20000
SLOT_SIZE = 0x1F0000
# What makes a file an app rather than, say, a factory image (which starts with
# the bootloader): the app description ESP-IDF puts first in every app.
APP_DESC_MAGIC = 0xABCD5432
ESP32_C3_CHIP_ID = 5


class ImageError(ValueError):
    pass


def check_app(image: bytes, label: str) -> None:
    """Refuse anything but an ESP32-C3 app that fits Sonoff's slot."""
    if len(image) > SLOT_SIZE:
        raise ImageError(f"{label} is {len(image):,} bytes; the plug's app slot holds {SLOT_SIZE:,}")
    if len(image) < 64 or image[0] != 0xE9:
        raise ImageError(f"{label} is not an ESP32 firmware image")
    if struct.unpack_from("<H", image, 12)[0] != ESP32_C3_CHIP_ID:
        raise ImageError(f"{label} is not built for the plug's ESP32-C3")
    if struct.unpack_from("<I", image, 32)[0] != APP_DESC_MAGIC:
        raise ImageError(
            f"{label} is not an app image: use firmware.ota.bin, not firmware.factory.bin"
        )
    try:
        report = imprint.parse_esp_image(image)
    except ValueError as exc:
        raise ImageError(f"{label} is not a valid firmware image: {exc}") from exc
    if not report["checksum_valid"] or report["sha256_valid"] is False:
        raise ImageError(f"{label} fails its own integrity check")


def published_image() -> bytes:
    """The pinned image, its placeholders untouched."""
    image = IMAGE.read_bytes()
    if len(image) != IMAGE_SIZE or hashlib.sha256(image).hexdigest() != IMAGE_SHA256:
        raise ImageError(f"{IMAGE.name} differs from the pinned file")
    check_app(image, IMAGE.name)
    if not imprint.has_placeholders(image):
        raise ImageError(f"{IMAGE.name} has no untouched Wi-Fi placeholders")
    return image


def published_bootloader() -> bytes:
    """The pinned bootloader, checked to be one for this plug."""
    bootloader = BOOTLOADER.read_bytes()
    if (len(bootloader) != BOOTLOADER_SIZE
            or hashlib.sha256(bootloader).hexdigest() != BOOTLOADER_SHA256):
        raise ImageError(f"{BOOTLOADER.name} differs from the pinned file")
    if len(bootloader) > BOOTLOADER_ROOM or bootloader[0] != 0xE9:
        raise ImageError(f"{BOOTLOADER.name} is not an ESP32 bootloader that fits before 0x8000")
    if struct.unpack_from("<H", bootloader, 12)[0] != ESP32_C3_CHIP_ID:
        raise ImageError(f"{BOOTLOADER.name} is not built for the plug's ESP32-C3")
    report = imprint.parse_esp_image(bootloader)
    if not report["checksum_valid"] or report["sha256_valid"] is not True:
        raise ImageError(f"{BOOTLOADER.name} fails its own integrity check")
    return bootloader


# The mover (ESPHome/autoflash_mover.yaml): a small ESPHome, run only while a
# plug leaves Tasmota, that takes the next firmware as an ESPHome update.
MOVER = ROOT / "artifacts" / "autoflash-mover-1.1.0.bin"
MOVER_SHA256 = "f6e131e1599c886d742c151c3ba3f84a285bba462b438c36b611d577e01ad243"
MOVER_SIZE = 756_960
# Sonoff's partition table, as a stock S60 has it: written back when a plug
# leaves Tasmota's layout.
SONOFF_TABLE = ROOT / "artifacts" / "sonoff-s60-partition-table.bin"


def mover_for(ssid: str, password: str) -> bytes:
    """The pinned mover with this Wi-Fi written in."""
    image = MOVER.read_bytes()
    if len(image) != MOVER_SIZE or hashlib.sha256(image).hexdigest() != MOVER_SHA256:
        raise ImageError(f"{MOVER.name} differs from the pinned file")
    check_app(image, MOVER.name)
    return imprint.imprint(image, ssid, password)


def sonoff_table() -> bytes:
    from .plans import SONOFF_TABLE_SHA256
    table = SONOFF_TABLE.read_bytes()
    if hashlib.sha256(table).hexdigest() != SONOFF_TABLE_SHA256:
        raise ImageError(f"{SONOFF_TABLE.name} is not Sonoff's partition table")
    return table


def image_for(ssid: str, password: str, own: Path | None = None) -> bytes:
    """The image to install: the published one with this Wi-Fi written in, or
    one built by hand (--esphome PATH), imprinted too if it has placeholders."""
    if own is None:
        return imprint.imprint(published_image(), ssid, password)
    image = own.read_bytes()
    check_app(image, own.name)
    if imprint.has_placeholders(image):
        return imprint.imprint(image, ssid, password)
    return image  # built with its own Wi-Fi


# ------------------------------------------------------------ the plug, running it
# Its web page (web_server version 3) and REST paths, named after each entity's
# name in ESPHome/sonoff_s60.yaml: /switch/Relay, /sensor/Voltage, /sensor/Power.

# Web logins (web_server's auth:), each for the plug it was given for
# (use_login), answering only that plug's own request for one: Basic, or
# Digest from ESPHome 2027.1 (and before it, with type: digest).
_LOGINS = urllib.request.HTTPPasswordMgrWithDefaultRealm()


def use_login(host: str, user: str, password: str) -> None:
    """Answer ESPHome's web login at `host` with these, from now on."""
    _LOGINS.add_password(None, f"http://{host}/", user, password)


class _Quietly:
    """urllib's own login handlers raise ValueError at a request for a login
    they cannot answer - in a scheme they do not do, or Digest when given
    none for it - which here ends as the 401 it is: on a network sweep, any
    device's page may ask."""

    def http_error_auth_reqed(self, *args):
        try:
            return super().http_error_auth_reqed(*args)  # type: ignore[misc]
        except ValueError:
            return None


class _Basic(_Quietly, urllib.request.HTTPBasicAuthHandler):
    pass


class _Digest(_Quietly, urllib.request.HTTPDigestAuthHandler):
    pass


def _opener() -> urllib.request.OpenerDirector:
    # Straight to the plug: a proxy set for the internet has no route to it.
    # A new one each time: urllib's Digest handler stops answering for good
    # once one login has failed.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _Digest(_LOGINS), _Basic(_LOGINS))


# What _fetch() says for a page that wants a web login it was not given.
LOGIN = 401


def _fetch(host: str, path: str, *, post: bool = False, timeout: float = 4.0) -> str | int | None:
    """The page at `path`, LOGIN if it wants a web login it was not given (or
    not the one given), None if it does not come."""
    request = urllib.request.Request(f"http://{host}{path}", data=b"" if post else None,
                                     method="POST" if post else "GET")
    try:
        with _opener().open(request, timeout=timeout) as response:
            body = response.read()
            # The page itself (web_server's local: true) comes gzipped, asked or not.
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                body = gzip.decompress(body)
            return body.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return LOGIN if exc.code == 401 else None
    # zlib.error: a page damaged on its way (seen in the fault suite: one
    # changed byte), which is the network, as a connection dropped is.
    except (urllib.error.URLError, OSError, http.client.HTTPException, EOFError, gzip.BadGzipFile,
            zlib.error):
        return None


def _request(host: str, path: str, *, post: bool = False, timeout: float = 4.0) -> str | None:
    page = _fetch(host, path, post=post, timeout=timeout)
    return page if isinstance(page, str) else None


def page(host: str, timeout: float = 4.0) -> str | None:
    """What the web page at `host` is: "esphome" for ESPHome's own (not
    merely "not Tasmota"), "login" for one that wants a web login it was not
    given (use_login) - Tasmota's asks the same way, so it is ESPHome's only
    if the update service answers too - "other" for another, None if none
    comes."""
    found = _fetch(host, "/", timeout=timeout)
    if found == LOGIN:
        return "login"
    if isinstance(found, str):
        return "esphome" if "<esp-app" in found else "other"
    return None


def is_esphome(host: str, timeout: float = 4.0) -> bool:
    """Whether ESPHome's own web page answers at `host` (not merely "not Tasmota")."""
    return page(host, timeout) == "esphome"


def _state(host: str, path: str) -> object:
    body = _request(host, path)
    try:
        return json.loads(body)["value"] if body else None
    except (ValueError, KeyError, TypeError):
        return None


def relay(host: str) -> bool | None:
    value = _state(host, "/switch/Relay")
    return value if isinstance(value, bool) else None


def switch_relay(host: str, on: bool) -> bool:
    """Switch the relay, and say whether it then reads as asked."""
    _request(host, f"/switch/Relay/{'turn_on' if on else 'turn_off'}", post=True)
    return relay(host) is on


# The Wi-Fi signal sensor, by its name in ESPHome/sonoff_s60.yaml (from 1.1.0).
SIGNAL_SENSOR = "WiFi Signal"


def signal(host: str, key: bytes | None = None) -> int | None:
    """The plug's Wi-Fi signal in dBm: from its Wi-Fi signal sensor, else from
    its native API (encrypted with `key`, where it is); None when neither
    gives it."""
    reading = sensor_signal(host)
    return reading if reading is not None else api_signal(host, key=key)


def sensor_signal(host: str) -> int | None:
    """The plug's Wi-Fi signal in dBm, from its Wi-Fi signal sensor: None from
    an ESPHome built without one (ours before 1.1.0), or before its first
    reading."""
    value = _state(host, "/sensor/" + urllib.parse.quote(SIGNAL_SENSOR))
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return round(value)
    return None


# ESPHome's native API, only as far as the run needs it: its configuration as
# it prints it, which gives the Wi-Fi signal, for an ESPHome without the signal
# sensor; and our Wi-Fi action.  Spoken plainly, as an ESPHome without an
# API encryption key speaks it (ours), or encrypted with the key
# (tools/autoflash/noise.py): each message then in a frame of its own, its
# type and length sealed with it.
API_PORT = 6053
# The handshake's prologue: ESPHome's, then the length and contents of the
# client's hello frame, which is empty.
API_PROLOGUE = b"NoiseAPIInit" + b"\x00\x00"
_HELLO, _CONNECT, _DISCONNECT, _PING, _PONG, _SUBSCRIBE_LOGS, _LOG = 1, 3, 5, 7, 8, 28, 29
_SIGNAL_LINE = re.compile(rb"Signal strength: (-?\d+) dB")
# The network it is joined to, printed with the signal in the same message.
_SSID_LINE = re.compile(rb"\bSSID: '([^'\r\n]*)'")


def _varint(number: int) -> bytes:
    out = bytearray()
    while True:
        byte, number = number & 0x7F, number >> 7
        out.append(byte | (0x80 if number else 0))
        if not number:
            return bytes(out)


def _frame(kind: int, payload: bytes = b"") -> bytes:
    return b"\x00" + _varint(len(payload)) + _varint(kind) + payload


def _read_varint(read: Callable[[int], bytes]) -> int:
    value = shift = 0
    while True:
        byte = read(1)[0]
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value
        shift += 7


def _fields(payload: bytes) -> dict[int, int | bytes]:
    """The varint and length-delimited fields of a protobuf message."""
    fields: dict[int, int | bytes] = {}
    at = 0

    def read(size: int) -> bytes:
        nonlocal at
        if at + size > len(payload):
            raise ValueError("protobuf message cut short")
        at += size
        return payload[at - size:at]

    while at < len(payload):
        key = _read_varint(read)
        if key & 7 == 0:
            fields[key >> 3] = _read_varint(read)
        elif key & 7 == 2:
            fields[key >> 3] = read(_read_varint(read))
        elif key & 7 == 5:
            fields[key >> 3] = struct.unpack("<I", read(4))[0]
        elif key & 7 == 1:
            fields[key >> 3] = struct.unpack("<Q", read(8))[0]
        else:
            raise ValueError(f"protobuf wire type {key & 7}")
    return fields


class ApiLocked(Exception):
    """ESPHome's API on the plug is encrypted, and no key was given - or
    (`wrong`) the key given is not its own."""

    def __init__(self, wrong: bool = False) -> None:
        super().__init__("the API encryption key given is not the plug's" if wrong
                         else "the plug's API is encrypted")
        self.wrong = wrong


class _Api:
    """A connection to ESPHome's native API, hello said: encrypted with `key`
    (32 bytes) where the plug's API is encrypted - one that is not is spoken
    to plainly, key or none.  ApiLocked when its API is encrypted and no key
    was given (once the first message comes), or not its own."""

    def __init__(self, host: str, port: int, timeout: float, key: bytes | None = None) -> None:
        self.deadline = time.monotonic() + timeout
        self.ciphers: tuple[noise.CipherState, noise.CipherState] | None = None
        self._connect(host, port, timeout)
        try:
            if key is not None and not self._encrypt(key):
                self.sock.close()  # a plain API, which this connection has upset
                self._connect(host, port, timeout)
            client = b"autoflash"
            hello = b"\x0a" + _varint(len(client)) + client + b"\x10\x01\x18\x0a"  # API 1.10
            # Hello; and Connect, which older servers want and newer ones let pass.
            self.send((_HELLO, hello), (_CONNECT, b""))
        except BaseException:
            self.sock.close()
            raise

    def _connect(self, host: str, port: int, timeout: float) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.reader = self.sock.makefile("rb")

    def _encrypt(self, key: bytes) -> bool:
        """The encryption handshake: True once through it, False from a plain
        API, which answers in its own framing ("Bad indicator byte")."""
        handshake = noise.Handshake(key, API_PROLOGUE)
        self.sock.sendall(noise.frame(b"") + noise.frame(bytes([noise.HANDSHAKE_OK])
                                                         + handshake.write_message()))
        try:
            chosen = noise.read_frame(self._read)
        except noise.NoiseError:
            return False
        if chosen[:1] != b"\x01":  # Noise_NNpsk0_25519_ChaChaPoly_SHA256, the one there is
            raise ConnectionError("the plug's API offers an encryption this tool does not know")
        try:
            noise.handshake_reply(handshake, noise.read_frame(self._read))
        except noise.NoiseError as exc:
            if str(exc) == noise.WRONG_KEY_REASON:
                raise ApiLocked(wrong=True) from None
            raise ConnectionError(f"the API's encryption handshake: {exc}") from None
        self.ciphers = handshake.split()
        return True

    def send(self, *messages: tuple[int, bytes]) -> None:
        """Each (kind, payload) as a message of its own, in one write.  A write
        the plug has already hung up on is left to the read that follows: what
        it said before hanging up ("Bad indicator byte" to a plain hello, from
        an encrypted API) is still there to read."""
        if self.ciphers is None:
            data = b"".join(_frame(kind, payload) for kind, payload in messages)
        else:
            data = b"".join(noise.frame(self.ciphers[0].encrypt(
                kind.to_bytes(2, "big") + len(payload).to_bytes(2, "big") + payload))
                for kind, payload in messages)
        try:
            self.sock.sendall(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read(self, size: int) -> bytes:
        self.sock.settimeout(max(0.1, self.deadline - time.monotonic()))
        data = self.reader.read(size)
        if len(data) < size:
            raise ConnectionError("the API connection closed")
        return data

    def _message(self) -> tuple[int, bytes]:
        if self.ciphers is None:
            if self._read(1) != b"\x00":
                raise ApiLocked()  # its answer in the encrypted framing
            size, kind = _read_varint(self._read), _read_varint(self._read)
            return kind, self._read(size)
        try:
            message = self.ciphers[1].decrypt(noise.read_frame(self._read))
        except noise.NoiseError as exc:
            raise ConnectionError(f"an API message did not check out: {exc}") from None
        if len(message) < 4:
            raise ConnectionError("an API message too short to say what it is")
        return int.from_bytes(message[:2], "big"), message[4:]

    def messages(self):
        """Each message the plug sends, (kind, payload), until the time is up,
        pings answered."""
        while time.monotonic() < self.deadline:
            kind, payload = self._message()
            if kind == _PING:
                self.send((_PONG, b""))
            else:
                yield kind, payload

    def close(self) -> None:
        try:
            self.send((_DISCONNECT, b""))
        except OSError:
            pass
        self.sock.close()


# What api_access() says of the API.
API_PLAIN, API_OPEN, API_KEY = "plain", "", "key"
_HELLO_ANSWER = 2


def api_access(host: str, key: bytes | None = None, port: int = API_PORT,
               timeout: float = 5.0) -> str | None:
    """Whether ESPHome's native API at `host` takes what it was given:
    API_PLAIN when it is not encrypted, API_OPEN when it is and `key` is its
    key, API_KEY when it is and no key was given, or not its own; None when
    no API answers."""
    try:
        api = _Api(host, port, timeout, key)
    except ApiLocked:
        return API_KEY
    except OSError:
        return None
    try:
        for kind, _ in api.messages():
            if kind == _HELLO_ANSWER:
                return API_PLAIN if api.ciphers is None else API_OPEN
    except ApiLocked:
        return API_KEY
    except (OSError, ValueError, IndexError):
        return None
    finally:
        api.close()
    return None


def api_signal(host: str, port: int = API_PORT, timeout: float = 8.0, key: bytes | None = None) -> int | None:
    """The plug's Wi-Fi signal in dBm from ESPHome's native API: asked for its
    logs with its configuration, ESPHome prints it ("Signal strength: -50 dB").
    None from an ESPHome whose API is encrypted and `key` is not its key, or
    that does not say."""
    wifi = api_wifi(host, port, timeout, key)
    return wifi["signal"] if wifi else None


def api_wifi(host: str, port: int = API_PORT, timeout: float = 8.0,
             key: bytes | None = None) -> dict[str, object] | None:
    """What ESPHome's configuration dump says of its Wi-Fi: its signal in dBm,
    and the network it is joined to (None if not printed with it). None from
    an ESPHome whose API is encrypted and `key` is not its key, or that does
    not say."""
    try:
        api = _Api(host, port, timeout, key)
    except (OSError, ApiLocked):
        return None
    try:
        api.send((_SUBSCRIBE_LOGS, b"\x08\x05\x10\x01"))  # at DEBUG, with the dump
        for kind, payload in api.messages():
            if kind == _LOG:
                text = _fields(payload).get(3, b"")
                found = _SIGNAL_LINE.search(text) if isinstance(text, bytes) else None
                if found:
                    ssid = _SSID_LINE.search(text)
                    return {"signal": int(found.group(1)),
                            "ssid": ssid.group(1).decode("utf-8", "replace") if ssid else None}
    except (OSError, ValueError, IndexError, ApiLocked):
        return None
    finally:
        api.close()
    return None


# Our ESPHome's action that gives the Wi-Fi it is on (ESPHome/sonoff_s60.yaml):
# its name from 1.2.0, and 1.1.0's; and the API messages that list and call it.
WIFI_ACTIONS = ("autoflash_wifi", "s60_wifi")
_LIST, _LISTED, _ACTION, _CALL, _ANSWER = 11, 19, 41, 42, 131


def api_wifi_details(host: str, port: int = API_PORT, timeout: float = 8.0,
                     key: bytes | None = None) -> tuple[str, str] | None:
    """The Wi-Fi the plug is on, name and password, from ESPHome itself - its
    Wi-Fi action, which ours has from 1.1.0. None from an ESPHome without
    it; ApiLocked from one whose API is encrypted and `key` is not its key."""
    try:
        api = _Api(host, port, timeout, key)
    except OSError:
        return None
    try:
        api.send((_LIST, b""))
        key = None
        for kind, payload in api.messages():
            if kind == _ACTION:
                action = _fields(payload)
                if action.get(1) in {name.encode() for name in WIFI_ACTIONS} and isinstance(action.get(2), int):
                    key = action[2]
            elif kind == _LISTED:
                if key is None:
                    return None
                # Called with no arguments, as call 1, its answer wanted.
                api.send((_CALL, b"\x0d" + struct.pack("<I", key) + b"\x18\x01\x20\x01"))
            elif kind == _ANSWER:
                answer = _fields(payload)
                if answer.get(1) != 1:
                    continue
                data = answer.get(4)
                details = json.loads(data) if answer.get(2) and isinstance(data, bytes) else {}
                ssid = details.get("ssid") if isinstance(details, dict) else None
                if isinstance(ssid, str) and ssid:
                    return ssid, str(details.get("password") or "")
                return None
    except (OSError, ValueError, IndexError):  # json.JSONDecodeError is a ValueError
        return None
    finally:
        api.close()
    return None


def meter(host: str) -> dict[str, float]:
    """The meter's voltage and power, as check_hardware's readings: each only
    if the plug has a reading (none, until its meter has reported)."""
    readings = {}
    for key in ("Voltage", "Power"):
        value = _state(host, f"/sensor/{key}")
        if isinstance(value, (int, float)):
            readings[key] = float(value)
    return readings
