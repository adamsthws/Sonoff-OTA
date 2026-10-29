"""Wrap an rt_ota .rbl (e.g. an OpenBeken BK7238 build) in the CoolKit "FOTA"
download container the S61's OTA client (ais_qota_task) expects, so it can be
served to the plug through the eWeLink `upgrade` command the same way the S60 is
converted - see docs/beken-s61-emulation.md#firmware-update-and-conversion-the-ota-path.

The stock bootloader accepts a genuine third-party rt_ota image (no signature, no
encryption, no secret key); OpenBeken already ships BK7238 images in this format
(OpenBK7238_<ver>.rbl). This tool only frames one for the cloud transport.

Container layout (decoded from the firmware; all multi-byte CRCs big-endian):

    [0:16]   header      magic 0x1beef11e; u16 @12 = 0 (sets the descriptor base to 16)
    [16:40]  trailer     [1]=descriptor count; [2:]=version (> the running one);
                         CRC32 over [0:20] at [20]
    [40:..]  descriptor  (76 bytes) data[0:0x3c] + CRC32 over [0:0x3c] at [0x3c];
                         data: name[18]@0 ("FWSW-01-S61-BK7238" - selects the staging
                         handler), version@0x20, u32 file-offset @0x30 (abs = this+16),
                         u32 size @0x34  (offset/size big-endian)
    [..]     payload     the .rbl, right after the descriptors

The device verifies only integrity: the magic, the trailer/descriptor CRC-32s, a
version-greater-than check, and the `upgrade` command's digest (SHA-256 of the file).

Usage:
    fota_wrap.py IN.rbl [-o OUT.fota] [--name NAME] [--version VER]
                     [--serve [HOST:PORT]]

With --serve it hosts the container over HTTP (206 ranged, keep-alive) exactly as
the plug fetches it, and prints the `upgrade` command params (downloadUrl + digest)
to push over the IoT WebSocket (see tools/ewelink/probe_ota_command.py for the S60 push).
Standard library only.
"""
import argparse
import hashlib
import struct
import sys
import zlib

STAGING_NAME = "FWSW-01-S61-BK7238"   # the image-type name the staging handler matches
# The version is written NUL-terminated into the trailer (bytes 2..20, before its
# CRC) and the descriptor (0x20..0x30, before the file offset): 15 characters at most.
VERSION_MAX = 15


def _rbl_version(rbl: bytes) -> str:
    """The version string from an rt_ota RBL header (offset 0x1c, 24 bytes)."""
    if len(rbl) < 96 or rbl[0:4] != b"RBL\x00":
        raise ValueError("input is not an rt_ota RBL (missing 'RBL\\0' magic)")
    return rbl[0x1c:0x34].split(b"\x00")[0].decode("ascii", "replace")


def wrap(rbl: bytes, name: str = STAGING_NAME, version: str | None = None) -> bytes:
    """Build the FOTA container around one .rbl payload."""
    if version is None:
        version = _rbl_version(rbl)
    ver = version.encode("ascii")
    if not ver or len(ver) > VERSION_MAX:
        raise ValueError("version %r must be 1 to %d ASCII characters to fit the container's fields"
                         % (version, VERSION_MAX))

    header = bytearray(16)
    struct.pack_into("<I", header, 0, 0x1beef11e)
    struct.pack_into("<H", header, 12, 0)                 # base for descriptor offsets = 16

    trailer = bytearray(24)
    trailer[1] = 1                                        # one image descriptor
    trailer[2:2 + len(ver)] = ver                         # must be > the running version
    struct.pack_into(">I", trailer, 0x14, zlib.crc32(bytes(trailer[:0x14])) & 0xffffffff)

    desc = bytearray(76)
    nm = name.encode("ascii")
    desc[0:len(nm)] = nm                                  # name[18] @ 0x00
    desc[0x20:0x20 + len(ver)] = ver                      # version @ 0x20
    payload_off = 16 + len(trailer) + len(desc)           # = 116
    struct.pack_into(">I", desc, 0x30, payload_off - 16)  # file-offset field (abs = this + 16)
    struct.pack_into(">I", desc, 0x34, len(rbl))          # image size
    struct.pack_into(">I", desc, 0x3c, zlib.crc32(bytes(desc[:0x3c])) & 0xffffffff)

    return bytes(header) + bytes(trailer) + bytes(desc) + rbl


def _server(container: bytes, host: str, port: int):
    """An HTTP server answering every GET with a 206 range of the container, keep-alive."""
    import http.server
    import socketserver

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"          # keep-alive: the plug reads several ranges per connection

        def log_message(self, *a):
            print("  [ota] %s %s" % (self.command, self.headers.get("Range", "-")), file=sys.stderr)

        def do_GET(self):
            rng = self.headers.get("Range", "")
            start, end = 0, len(container) - 1
            if rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                start = int(a) if a else 0
                end = int(b) if b else len(container) - 1
            end = min(end, len(container) - 1)
            chunk = container[start:end + 1]
            self.close_connection = False
            head = ("HTTP/1.1 206 Partial Content\r\n"
                    "Content-Type: application/octet-stream\r\n"
                    "Content-Range: bytes %d-%d/%d\r\n"
                    "Content-Length: %d\r\nConnection: keep-alive\r\n\r\n"
                    % (start, end, len(container), len(chunk))).encode("latin1")
            try:
                self.wfile.write(head + chunk)
            except Exception:
                pass

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    return Server((host, port), Handler)


def _serve(container: bytes, host: str, port: int, path: str = "/user1.bin") -> None:
    srv = _server(container, host, port)
    bound = srv.server_address
    digest = hashlib.sha256(container).hexdigest()
    url = "http://%s:%d%s" % (bound[0], bound[1], path)
    print("serving FOTA container on %s" % url)
    print("`upgrade` command params to push over the IoT WebSocket:")
    print('  downloadUrl: %s' % url)
    print('  digest:      %s' % digest)
    print('  name:        user1.bin')
    print("Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Wrap an rt_ota .rbl in the CoolKit FOTA container for the S61.")
    ap.add_argument("input", help="the .rbl to wrap (e.g. OpenBK7238_<ver>.rbl)")
    ap.add_argument("-o", "--output", help="write the container here (default: INPUT with .fota)")
    ap.add_argument("--name", default=STAGING_NAME, help="image-type name (default: %(default)s)")
    ap.add_argument("--version", help="version string (default: the RBL header's)")
    ap.add_argument("--serve", nargs="?", const="127.0.0.1:8080", metavar="HOST:PORT",
                    help="serve the container over HTTP instead of only writing it")
    args = ap.parse_args(argv)

    with open(args.input, "rb") as f:
        rbl = f.read()
    try:
        container = wrap(rbl, name=args.name, version=args.version)
    except ValueError as e:
        ap.error(str(e))

    ver = args.version or _rbl_version(rbl)
    print("wrapped %s (%d bytes, RBL version %r) -> FOTA container %d bytes"
          % (args.input, len(rbl), ver, len(container)))
    print("sha256(container) = %s" % hashlib.sha256(container).hexdigest())

    out = args.output or (args.input.rsplit(".", 1)[0] + ".fota")
    with open(out, "wb") as f:
        f.write(container)
    print("written: %s" % out)

    if args.serve is not None:
        host, _, port = args.serve.partition(":")
        _serve(container, host or "127.0.0.1", int(port or "8080"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
