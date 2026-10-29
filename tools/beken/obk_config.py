"""Build an OpenBeken mainConfig_t blob with Wi-Fi credentials.

Layout (src/new_pins.h, version 5 / MAIN_CFG_VERSION):
  ofs 0   ident0/1/2 = 'C','F','G'
  ofs 3   crc  (Tiny_CRC8 over bytes [4 : sizeof])
  ofs 4   version int = 5
  ofs 8   genericFlags u32
  ofs 0xC genericFlags2 u32
  ofs 0x10 changeCounter u16, otaCounter u16
  ofs 0x14 wifi_ssid[64]
  ofs 0x54 wifi_pass[64]
  ...
  sizeof(mainConfig_t) = 3584 (bk7231 group D, == MAGIC_CONFIG_SIZE_V4)
Checksum: Tiny_CRC8 (Dallas/Maxim 1-wire, poly 0x8C) over [4 : sizeof].
"""
import struct

CFG_SIZE = 3584
MAIN_CFG_VERSION = 5
OFS_WIFI_SSID = 0x14
OFS_WIFI_PASS = 0x54
SSID_LEN = 64
PASS_LEN = 64


def tiny_crc8(data):
    """OpenBeken's Tiny_CRC8 (tiny_crc8.c) AS BUILT for BK7238: `char crc` is a
    SIGNED char, so `crc >>= 1` is an arithmetic (sign-extending) shift. Verified
    byte-exact against a real OBK 1.18.319 config (crc 0x80 over [4:3584]). A
    plain unsigned CRC-8/MAXIM does NOT reproduce it."""
    crc = 0
    for b in data:
        e = b
        for _ in range(8):
            s = (crc ^ e) & 1
            crc = ((crc >> 1) | 0x80) if (crc & 0x80) else (crc >> 1)  # arithmetic
            if s:
                crc ^= 0x8C
            crc &= 0xFF
            e >>= 1
    return crc & 0xFF


def build_config(ssid, passwd, size=CFG_SIZE, template=None):
    """Return a size-byte OBK config blob carrying ssid/passwd.

    If template (a valid existing blob) is given, start from it so every other
    field is preserved; otherwise start from zeros (OBK fills its own defaults
    for the few zero-sensitive fields at load)."""
    if isinstance(ssid, str):
        ssid = ssid.encode()
    if isinstance(passwd, str):
        passwd = passwd.encode()
    if len(ssid) >= SSID_LEN or len(passwd) >= PASS_LEN:
        raise ValueError("ssid/pass too long for 64-byte fields")
    buf = bytearray(template[:size]) if template else bytearray(size)
    if len(buf) < size:
        buf += bytes(size - len(buf))
    buf[0:3] = b"CFG"
    struct.pack_into("<i", buf, 4, MAIN_CFG_VERSION)
    # credentials (zero-padded, NUL-terminated within the field)
    buf[OFS_WIFI_SSID:OFS_WIFI_SSID + SSID_LEN] = ssid.ljust(SSID_LEN, b"\x00")
    buf[OFS_WIFI_PASS:OFS_WIFI_PASS + PASS_LEN] = passwd.ljust(PASS_LEN, b"\x00")
    # changeCounter = 1 so a fresh config reads as "written once"
    if not template:
        struct.pack_into("<H", buf, 0x10, 1)
    buf[3] = tiny_crc8(bytes(buf[4:size]))
    return bytes(buf)


if __name__ == "__main__":
    import sys
    ssid = sys.argv[1] if len(sys.argv) > 1 else "AutoflashNet"
    pw = sys.argv[2] if len(sys.argv) > 2 else "autoflash1234"
    blob = build_config(ssid, pw)
    print("config %d bytes  ident=%r ver=%d crc=0x%02x ssid=%r pass=%r"
          % (len(blob), blob[:3], struct.unpack_from("<i", blob, 4)[0], blob[3], ssid, pw))
