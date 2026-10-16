"""ESPHome's update protocol, against a stand-in that speaks it as ESPHome does."""
import gzip
import hashlib
import socket
import threading
import unittest

from tools.autoflash import esphome_ota as ota


class FakeESPHome:
    """One update session at a time, answering as ota_esphome.cpp does."""

    def __init__(self, *, flags=ota.SERVER_COMPRESSION, auth=ota.AUTH_OK, refuse_at=None):
        self.flags, self.auth, self.refuse_at = flags, auth, refuse_at
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        self.received: list[tuple[int, bytes]] = []
        threading.Thread(target=self.serve, daemon=True).start()

    def recv(self, conn, count):
        data = b""
        while len(data) < count:
            piece = conn.recv(count - len(data))
            if not piece:
                raise ConnectionError
            data += piece
        return data

    def serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                try:
                    self.session(conn)
                except (ConnectionError, OSError):
                    pass

    def session(self, conn):
        assert self.recv(conn, 5) == ota.MAGIC
        conn.sendall(bytes([ota.OK, 2]))
        self.recv(conn, 1)  # the client's features
        conn.sendall(bytes([ota.FEATURE_FLAGS, self.flags]))
        conn.sendall(bytes([self.auth]))
        if self.auth != ota.AUTH_OK:
            return
        kind = self.recv(conn, 1)[0]
        size = int.from_bytes(self.recv(conn, 4), "big")
        conn.sendall(bytes([ota.UPDATE_PREPARE_OK]))
        md5 = self.recv(conn, 32).decode()
        conn.sendall(bytes([ota.BIN_MD5_OK]))
        data = b""
        while len(data) < size:
            data += self.recv(conn, min(ota.BLOCK, size - len(data)))
            if self.refuse_at is not None and len(data) >= self.refuse_at:
                conn.sendall(bytes([0x83]))
                return
            conn.sendall(bytes([ota.CHUNK_OK]))
        assert hashlib.md5(data).hexdigest() == md5
        if self.flags & ota.SERVER_COMPRESSION:
            data = gzip.decompress(data)
        self.received.append((kind, data))
        conn.sendall(bytes([ota.RECEIVE_OK, ota.UPDATE_END_OK]))
        self.recv(conn, 1)

    def close(self):
        self.server.close()


class ProtocolTests(unittest.TestCase):
    def fake(self, **kwargs):
        fake = FakeESPHome(**kwargs)
        self.addCleanup(fake.close)
        return fake

    def test_an_app_arrives_whole_compressed_or_not(self):
        image = bytes(range(256)) * 300
        for flags in (ota.SERVER_COMPRESSION, 0):
            fake = self.fake(flags=flags)
            sent = []
            ota.upload("127.0.0.1", image, port=fake.port, progress=lambda a, b: sent.append(a))
            self.assertEqual(fake.received, [(ota.APP, image)])
            self.assertEqual(sent[-1], sent[-1])  # progress reported to the end

    def test_the_probe_says_what_the_firmware_offers(self):
        fake = self.fake(flags=ota.SERVER_COMPRESSION | ota.SERVER_PARTITION_ACCESS)
        offer = ota.probe("127.0.0.1", port=fake.port)
        self.assertTrue(offer.extended and offer.compression and offer.partition_access)
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            port = unused.getsockname()[1]
        self.assertIsNone(ota.probe("127.0.0.1", port=port, timeout=1))

    def test_an_update_password_is_reported_not_guessed(self):
        fake = self.fake(auth=ota.REQUEST_SHA256_AUTH)
        with self.assertRaises(ota.NeedsPassword):
            ota.upload("127.0.0.1", b"\xe9" * 100, port=fake.port)

    def test_a_refusal_part_way_is_said_in_words(self):
        fake = self.fake(flags=0, refuse_at=ota.BLOCK)
        with self.assertRaisesRegex(ota.OTAError, "writing the flash failed"):
            ota.upload("127.0.0.1", b"\xe9" * (3 * ota.BLOCK), port=fake.port)

    def test_a_partition_table_needs_a_firmware_that_allows_it(self):
        fake = self.fake(flags=0)
        with self.assertRaisesRegex(ota.OTAError, "allow_partition_access"):
            ota.upload("127.0.0.1", b"\0" * 3072, kind=ota.PARTITION_TABLE, port=fake.port)


if __name__ == "__main__":
    unittest.main()
