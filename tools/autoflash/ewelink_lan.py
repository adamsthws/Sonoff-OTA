"""What a stock S60 says of itself in its eWeLink LAN advert.

On the network the plug advertises `_ewelink._tcp` over mDNS. Its TXT record
carries its state - switch, power readings, Wi-Fi signal - encrypted with its
LAN key: AES-128-CBC, keyed with the MD5 of the device key, the IV in the
record (`iv`) and the ciphertext in base64, split over `data1` to `data4`.
The tool installs nothing, so AES is here in plain Python: decryption only,
of a few hundred bytes, where speed does not matter.
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

def _xtime(a: int) -> int:
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else a << 1


def _mul(a: int, b: int) -> int:
    product = 0
    while b:
        if b & 1:
            product ^= a
        a, b = _xtime(a), b >> 1
    return product


def _sboxes() -> tuple[list[int], list[int]]:
    """FIPS-197's S-box from its definition: the inverse in GF(2^8), then the
    affine transform; and its inverse."""
    sbox, inverse = [0] * 256, [0] * 256
    for x in range(256):
        inv = next((y for y in range(1, 256) if _mul(x, y) == 1), 0)
        s = inv
        for shift in range(1, 5):
            s ^= ((inv << shift) | (inv >> (8 - shift))) & 0xFF
        s ^= 0x63
        sbox[x], inverse[s] = s, x
    return sbox, inverse


_SBOX, _INV_SBOX = _sboxes()


def _round_keys(key: bytes) -> list[list[int]]:
    words = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    rcon = 1
    for i in range(4, 44):
        word = list(words[i - 1])
        if i % 4 == 0:
            word = [_SBOX[b] for b in word[1:] + word[:1]]
            word[0] ^= rcon
            rcon = _xtime(rcon)
        words.append([a ^ b for a, b in zip(words[i - 4], word)])
    return [sum(words[r * 4:r * 4 + 4], []) for r in range(11)]


def _decrypt_block(block: bytes, keys: list[list[int]]) -> bytes:
    state = [b ^ k for b, k in zip(block, keys[10])]
    for rnd in range(9, -1, -1):
        # InvShiftRows (state is column-major: byte r + 4c is row r, column c)
        state = [state[(r + 4 * ((c - r) % 4))] for c in range(4) for r in range(4)]
        state = [_INV_SBOX[b] for b in state]
        state = [b ^ k for b, k in zip(state, keys[rnd])]
        if rnd:
            mixed = []
            for c in range(4):
                a = state[4 * c:4 * c + 4]
                mixed += [_mul(a[0], 14) ^ _mul(a[1], 11) ^ _mul(a[2], 13) ^ _mul(a[3], 9),
                          _mul(a[0], 9) ^ _mul(a[1], 14) ^ _mul(a[2], 11) ^ _mul(a[3], 13),
                          _mul(a[0], 13) ^ _mul(a[1], 9) ^ _mul(a[2], 14) ^ _mul(a[3], 11),
                          _mul(a[0], 11) ^ _mul(a[1], 13) ^ _mul(a[2], 9) ^ _mul(a[3], 14)]
            state = mixed
    return bytes(state)


def decrypt_cbc(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-128-CBC with PKCS#7 padding; ValueError if it does not unpad."""
    if len(key) != 16 or len(iv) != 16 or not data or len(data) % 16:
        raise ValueError("not AES-128-CBC data")
    keys = _round_keys(key)
    plain, previous = bytearray(), iv
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        plain += bytes(a ^ b for a, b in zip(_decrypt_block(block, keys), previous))
        previous = block
    pad = plain[-1]
    if not 1 <= pad <= 16 or plain[-pad:] != bytes([pad]) * pad:
        raise ValueError("bad padding: not this key")
    return bytes(plain[:-pad])


def advert_fields(report: dict[str, Any], device_id: str) -> dict[str, str] | None:
    """The TXT fields of `device_id`'s eWeLink advert in a discovery report."""
    for packet in report.get("packets", []):
        for record in packet.get("records", []):
            if record.get("type") != "TXT":
                continue
            fields = dict(str(value).partition("=")[::2] for value in record.get("value") or [])
            if fields.get("id") == device_id:
                return fields
    return None


def advert_state(fields: dict[str, str], device_key: str) -> dict[str, Any] | None:
    """The state the advert carries, or None when this key does not open it."""
    try:
        data = base64.b64decode("".join(fields.get(f"data{i}", "") for i in range(1, 5)))
        if fields.get("encrypt") == "true":
            data = decrypt_cbc(hashlib.md5(device_key.encode()).digest(),
                               base64.b64decode(fields["iv"]), data)
        state = json.loads(data)
    except (KeyError, ValueError):  # binascii.Error and bad UTF-8 are ValueErrors too
        return None
    return state if isinstance(state, dict) else None


def signal(state: dict[str, Any] | None) -> int | None:
    """The Wi-Fi signal in dBm a decrypted state gives, if any."""
    rssi = (state or {}).get("rssi")
    return rssi if isinstance(rssi, int) and not isinstance(rssi, bool) else None
