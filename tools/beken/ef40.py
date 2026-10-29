"""EasyFlash V4 (EF40) KV40 reader/writer for S61 stock flash.

Node: "KV40"(4) | len u32 | crc32 u32 | name_len(1)+0xff*3 | value_len u32 |
      name | value | trailer(0x00 0x00 + 0xff*6)
  len = 20 + name_len + value_len + 8
  crc = zlib.crc32(bytes from the name_len field [+12] through end of value)
The app KV store spans 0x3e0000-0x3ebfff (active sector 0x3e3000); read raw.
"""
import struct
import zlib

APP_STORE = (0x3e0000, 0x3ec000)


def parse_store(data, region=APP_STORE):
    """Return {name: value} for CRC-valid KV40 nodes (last write wins)."""
    blob = data[region[0]:region[1]]
    out = {}
    i = 0
    while True:
        j = blob.find(b"KV40", i)
        if j < 0:
            break
        i = j + 4
        if j + 20 > len(blob):
            break
        name_len = blob[j + 12]
        value_len = struct.unpack_from("<I", blob, j + 16)[0]
        if name_len == 0 or name_len > 64 or value_len > 0x2000:
            continue
        name = blob[j + 20:j + 20 + name_len]
        value = blob[j + 20 + name_len:j + 20 + name_len + value_len]
        if len(value) != value_len:
            continue
        crc = struct.unpack_from("<I", blob, j + 8)[0]
        if zlib.crc32(blob[j + 12:j + 20 + name_len + value_len]) & 0xffffffff != crc:
            continue
        out[name.decode("latin1")] = value
    return out


def encode_node(name, value):
    if isinstance(name, str):
        name = name.encode()
    if isinstance(value, str):
        value = value.encode()
    name_len = len(name)
    value_len = len(value)
    length = 20 + name_len + value_len + 8
    body = bytes([name_len]) + b"\xff\xff\xff" + struct.pack("<I", value_len) + name + value
    crc = zlib.crc32(body) & 0xffffffff
    node = b"KV40" + struct.pack("<I", length) + struct.pack("<I", crc) + body
    node += b"\x00\x00" + b"\xff" * 6
    return node


def provision(data, creds, region=APP_STORE):
    """Append wifi-ssid/wifi-pass KV40 nodes into free (0xff) space of the store.
    data is a mutable bytearray of the whole flash. creds = {key: value}.
    Raises ValueError if the store has no room for them; data is then unchanged."""
    store_lo, store_hi = region
    # find end of the last well-formed KV40 node (append after it, into 0xff
    # space); "KV40" inside a value, with nonsense lengths, does not count
    blob = bytes(data[store_lo:store_hi])
    last = 0
    i = 0
    while True:
        j = blob.find(b"KV40", i)
        if j < 0:
            break
        i = j + 4
        if j + 20 > len(blob):
            break
        name_len = blob[j + 12]
        value_len = struct.unpack_from("<I", blob, j + 16)[0]
        if name_len == 0 or name_len > 64 or value_len > 0x2000:
            continue
        end = j + 20 + name_len + value_len + 8
        if end <= len(blob) and end > last:
            last = end
    pos = store_lo + ((last + 3) & ~3)  # 4-byte align
    nodes = [encode_node(name, value) for name, value in creds.items()]
    if pos + sum((len(node) + 3) & ~3 for node in nodes) > store_hi:
        raise ValueError("no room for %d bytes of nodes at 0x%x in the store 0x%x-0x%x"
                         % (sum(len(node) for node in nodes), pos, store_lo, store_hi))
    for node in nodes:
        data[pos:pos + len(node)] = node
        pos += (len(node) + 3) & ~3
    return data


if __name__ == "__main__":
    import sys
    data = open(sys.argv[1], "rb").read()
    kv = parse_store(data)
    for k in ("wifi-ssid", "wifi-pass", "ssid", "pass"):
        if k in kv:
            print("  %-10s = %r" % (k, kv[k]))
    print("total keys:", len(kv))
