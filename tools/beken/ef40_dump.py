"""Enumerate EasyFlash V4 (EF40) KV40 nodes from an S61 flash dump.

Config regions are stored raw (un-striped). A node is:
  "KV40"(4) | len(4) | crc32(4) | name_len(1)+0xff*3 | value_len(4) | name | value | trailer
  crc = zlib.crc32(bytes from the name_len field [+12] through end of value)
Last valid instance of a key wins (EasyFlash overwrites by appending + GC).
"""
import sys
import struct
import zlib

REGIONS = {
    "app  (0x3e0000-0x3ebfff)": (0x3e0000, 0x3ec000),
    "sys  (0x3f6000-0x3fcfff)": (0x3f6000, 0x3fd000),
}


def parse(region):
    out = {}  # name -> (value, crc_ok, order)
    i = 0
    n = 0
    while True:
        j = region.find(b"KV40", i)
        if j < 0:
            break
        i = j + 4
        if j + 20 > len(region):
            break
        crc = struct.unpack_from("<I", region, j + 8)[0]
        name_len = region[j + 12]
        value_len = struct.unpack_from("<I", region, j + 16)[0]
        if name_len == 0 or name_len > 64 or value_len > 0x2000:
            continue
        name = region[j + 20:j + 20 + name_len]
        value = region[j + 20 + name_len:j + 20 + name_len + value_len]
        if len(value) != value_len:
            continue
        calc = zlib.crc32(region[j + 12:j + 20 + name_len + value_len]) & 0xffffffff
        try:
            key = name.decode("latin1")
        except Exception:
            key = repr(name)
        out[key] = (value, calc == crc, n)
        n += 1
    return out


def preview(v):
    printable = all(32 <= b < 127 for b in v) and len(v) > 0
    if printable:
        return "str  %r" % (v.decode("latin1"),)
    if len(v) <= 16:
        return "hex  " + v.hex()
    return "hex  %s... (%d bytes)" % (v[:16].hex(), len(v))


def main():
    path = sys.argv[1]
    only_valid = "--all" not in sys.argv
    d = open(path, "rb").read()
    for label, (a, b) in REGIONS.items():
        region = d[a:b]
        nodes = parse(region)
        print("\n===== %s : %d distinct keys =====" % (label, len(nodes)))
        for key, (value, ok, _order) in sorted(nodes.items(), key=lambda kv: kv[1][2]):
            if only_valid and not ok:
                continue
            flag = "" if ok else "  [CRC MISMATCH/stale]"
            print("  %-22s (%4d B) %s%s" % (key, len(value), preview(value), flag))


if __name__ == "__main__":
    main()
