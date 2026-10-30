"""AES-256-CBC, in Python alone: for the rt_ota images the S61's bootloader
applies (rbl.py), whose body the Beken SDK's packer encrypts with RT-Thread's
public default key.  The tool runs on the standard library, which has no
AES; this is FIPS-197 as written, tables and all, and fast enough for the
one-off images it packs (a stock app in a few seconds).

Not for secrets: the key is public, and the point of the encryption here is
the bootloader's format, not confidentiality.
"""

from __future__ import annotations

BLOCK = 16

# The S-box, from the multiplicative inverse in GF(2^8) and the affine transform.
def _sbox() -> tuple[list[int], list[int]]:
    forward, inverse = [0] * 256, [0] * 256
    p = q = 1
    while True:
        # Multiply p by 3, q by 3^-1 (0xf6) in GF(2^8).
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ (q << 1) ^ (q << 2) ^ (q << 3) ^ (q << 4)
        x = (x ^ (x >> 8) ^ 0x63) & 0xFF
        forward[p] = x
        inverse[x] = p
        if p == 1:
            break
    forward[0] = 0x63
    inverse[0x63] = 0
    return forward, inverse


SBOX, INV_SBOX = _sbox()


def _xtime(value: int) -> int:
    value <<= 1
    return (value ^ 0x1B) & 0xFF if value & 0x100 else value


def _mul(a: int, b: int) -> int:
    product = 0
    while b:
        if b & 1:
            product ^= a
        a = _xtime(a)
        b >>= 1
    return product


MUL2 = [_xtime(i) for i in range(256)]
MUL3 = [_xtime(i) ^ i for i in range(256)]
MUL9 = [_mul(i, 9) for i in range(256)]
MUL11 = [_mul(i, 11) for i in range(256)]
MUL13 = [_mul(i, 13) for i in range(256)]
MUL14 = [_mul(i, 14) for i in range(256)]


def expand_key(key: bytes) -> list[list[int]]:
    """The round keys of a 128, 192 or 256-bit key, each 16 bytes."""
    if len(key) not in (16, 24, 32):
        raise ValueError("an AES key is 16, 24 or 32 bytes")
    words = [list(key[i:i + 4]) for i in range(0, len(key), 4)]
    n = len(words)
    rounds = n + 6
    rcon = 1
    for i in range(n, 4 * (rounds + 1)):
        word = list(words[i - 1])
        if i % n == 0:
            word = [SBOX[word[1]] ^ rcon, SBOX[word[2]], SBOX[word[3]], SBOX[word[0]]]
            rcon = _xtime(rcon)
        elif n > 6 and i % n == 4:
            word = [SBOX[b] for b in word]
        words.append([a ^ b for a, b in zip(words[i - n], word)])
    return [sum(words[4 * r:4 * r + 4], []) for r in range(rounds + 1)]


def encrypt_block(round_keys: list[list[int]], block: bytes) -> bytes:
    s = [b ^ k for b, k in zip(block, round_keys[0])]
    for round_key in round_keys[1:-1]:
        s = [SBOX[b] for b in s]
        s = [s[0], s[5], s[10], s[15], s[4], s[9], s[14], s[3], s[8], s[13], s[2], s[7], s[12], s[1], s[6], s[11]]
        mixed = []
        for c in range(0, 16, 4):
            a0, a1, a2, a3 = s[c:c + 4]
            mixed += [MUL2[a0] ^ MUL3[a1] ^ a2 ^ a3, a0 ^ MUL2[a1] ^ MUL3[a2] ^ a3,
                      a0 ^ a1 ^ MUL2[a2] ^ MUL3[a3], MUL3[a0] ^ a1 ^ a2 ^ MUL2[a3]]
        s = [b ^ k for b, k in zip(mixed, round_key)]
    s = [SBOX[b] for b in s]
    s = [s[0], s[5], s[10], s[15], s[4], s[9], s[14], s[3], s[8], s[13], s[2], s[7], s[12], s[1], s[6], s[11]]
    return bytes(b ^ k for b, k in zip(s, round_keys[-1]))


def decrypt_block(round_keys: list[list[int]], block: bytes) -> bytes:
    s = [b ^ k for b, k in zip(block, round_keys[-1])]
    for round_key in reversed(round_keys[1:-1]):
        s = [s[0], s[13], s[10], s[7], s[4], s[1], s[14], s[11], s[8], s[5], s[2], s[15], s[12], s[9], s[6], s[3]]
        s = [INV_SBOX[b] for b in s]
        s = [b ^ k for b, k in zip(s, round_key)]
        mixed = []
        for c in range(0, 16, 4):
            a0, a1, a2, a3 = s[c:c + 4]
            mixed += [MUL14[a0] ^ MUL11[a1] ^ MUL13[a2] ^ MUL9[a3], MUL9[a0] ^ MUL14[a1] ^ MUL11[a2] ^ MUL13[a3],
                      MUL13[a0] ^ MUL9[a1] ^ MUL14[a2] ^ MUL11[a3], MUL11[a0] ^ MUL13[a1] ^ MUL9[a2] ^ MUL14[a3]]
        s = mixed
    s = [s[0], s[13], s[10], s[7], s[4], s[1], s[14], s[11], s[8], s[5], s[2], s[15], s[12], s[9], s[6], s[3]]
    s = [INV_SBOX[b] for b in s]
    return bytes(b ^ k for b, k in zip(s, round_keys[0]))


def pad(data: bytes) -> bytes:
    """PKCS#7: always some padding, a whole block of it for a block-aligned length."""
    n = BLOCK - len(data) % BLOCK
    return data + bytes([n]) * n


def unpad(data: bytes) -> bytes:
    n = data[-1] if data else 0
    if not 1 <= n <= BLOCK or data[-n:] != bytes([n]) * n:
        raise ValueError("not PKCS#7 padded")
    return data[:-n]


def cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """`data`, padded, under AES-CBC with `key` and `iv`."""
    if len(iv) != BLOCK:
        raise ValueError("an AES-CBC IV is 16 bytes")
    round_keys = expand_key(key)
    out = bytearray()
    previous = iv
    padded = pad(data)
    for i in range(0, len(padded), BLOCK):
        previous = encrypt_block(round_keys, bytes(a ^ b for a, b in zip(padded[i:i + BLOCK], previous)))
        out += previous
    return bytes(out)


def cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """The data cbc_encrypt made of it, its padding taken off."""
    if len(iv) != BLOCK or len(data) % BLOCK:
        raise ValueError("not AES-CBC output")
    round_keys = expand_key(key)
    out = bytearray()
    previous = iv
    for i in range(0, len(data), BLOCK):
        block = data[i:i + BLOCK]
        out += bytes(a ^ b for a, b in zip(decrypt_block(round_keys, block), previous))
        previous = block
    return unpad(bytes(out))
