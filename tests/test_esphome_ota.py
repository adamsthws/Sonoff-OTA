"""ESPHome's update protocol, against a stand-in that speaks it as ESPHome does."""
import gzip
import hashlib
import secrets
import socket
import threading
import unittest

from tools.autoflash import esphome_ota as ota
from tools.autoflash import noise

KEY = bytes(range(32))


class Sealed:
    """The plug's end of an encrypted session, as ota_esphome_noise.cpp reads
    and writes it: each answer byte in a frame of its own, each value the
    client sends (type, size, checksum) in exactly one, and data in frames of
    at most 1024 bytes."""

    def __init__(self, fake, conn, ciphers):
        self.fake, self.conn = fake, conn
        self.sending, self.receiving = ciphers

    def sendall(self, data):
        for byte in data:
            self.conn.sendall(noise.frame(self.sending.encrypt(bytes([byte]))))

    def frame(self):
        return self.receiving.decrypt(noise.read_frame(lambda size: self.fake.recv(self.conn, size)))

    def value(self, size):
        data = self.frame()
        assert len(data) == size, f"a value of {size} bytes came in a frame of {len(data)}"
        return data

    def data(self, size):
        data = b""
        while len(data) < size:
            piece = self.frame()
            assert 0 < len(piece) <= ota.NOISE_MAX_PLAINTEXT, len(piece)
            data += piece
        assert len(data) == size, "a frame ran past the block"
        return data


class FakeESPHome:
    """One update session at a time, answering as ota_esphome.cpp does
    (ESPHome 2026.9.1): with an update `password`, a SHA256 challenge - or,
    as older ESPHome, an `md5` one - and refused if the client offers no
    SHA256.  With an API `key`, encrypted updates offered (and then no
    password asked for), and, `encrypted_only`, nothing else taken."""

    def __init__(self, *, flags=ota.SERVER_COMPRESSION, password=None, md5=False, encrypted_only=False,
                 refuse_at=None, key=None):
        self.flags, self.refuse_at = flags, refuse_at
        self.password, self.md5, self.encrypted_only = password, md5, encrypted_only
        self.key = key
        self.encrypted: list[bool] = []  # whether each update came encrypted
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        self.received: list[tuple[int, bytes]] = []
        self.features: list[int] = []  # what the client offered, each session
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
        features = self.recv(conn, 1)[0]
        self.features.append(features)
        wanted = ota.CLIENT_NOISE | ota.CLIENT_EXTENDED
        sealing = self.key is not None and features & wanted == wanted
        if self.encrypted_only and not sealing:
            conn.sendall(bytes([ota.ENCRYPTION_REQUIRED]))
            return
        flags = self.flags | (ota.SERVER_NOISE if self.key is not None else 0)
        conn.sendall(bytes([ota.FEATURE_FLAGS, flags]))
        if sealing:
            # The prologue: every byte of the session so far, both ways.
            prologue = ota.NOISE_PROLOGUE + ota.MAGIC + bytes([ota.OK, 2, features, ota.FEATURE_FLAGS, flags])
            responder = noise.Handshake(self.key, prologue, initiator=False)
            message = noise.read_frame(lambda size: self.recv(conn, size))
            assert message[0] == noise.HANDSHAKE_OK
            try:
                responder.read_message(message[1:])
            except noise.NoiseError:
                conn.sendall(noise.frame(b"\x01" + noise.WRONG_KEY_REASON.encode()))
                return
            conn.sendall(noise.frame(bytes([noise.HANDSHAKE_OK]) + responder.write_message()))
            self.sealed(Sealed(self, conn, responder.split()))
            return
        if self.password is not None:
            if not self.md5 and not features & ota.CLIENT_SHA256_AUTH:
                conn.sendall(bytes([ota.AUTH_INVALID]))  # "SHA256 required"
                return
            hash_function, size = (hashlib.md5, 32) if self.md5 else (hashlib.sha256, 64)
            nonce = secrets.token_hex(size // 2).encode()
            conn.sendall(bytes([ota.REQUEST_AUTH if self.md5 else ota.REQUEST_SHA256_AUTH]) + nonce)
            answer = self.recv(conn, 2 * size)
            theirs, proof = answer[:size], answer[size:]
            if proof != hash_function(self.password.encode() + nonce + theirs).hexdigest().encode():
                conn.sendall(bytes([ota.AUTH_INVALID]))
                return
        conn.sendall(bytes([ota.AUTH_OK]))
        self.encrypted.append(False)  # before the update: the client returns as it ends
        self.update(conn, value=lambda size: self.recv(conn, size), data=lambda size: self.recv(conn, size))

    def sealed(self, sealed):
        sealed.sendall(bytes([ota.AUTH_OK]))  # no password asked for: the key is the proof
        self.encrypted.append(True)
        self.update(sealed, value=sealed.value, data=sealed.data)

    def update(self, conn, *, value, data):
        kind = value(1)[0]
        size = int.from_bytes(value(4), "big")
        conn.sendall(bytes([ota.UPDATE_PREPARE_OK]))
        md5 = value(32).decode()
        conn.sendall(bytes([ota.BIN_MD5_OK]))
        received = b""
        while len(received) < size:
            received += data(min(ota.BLOCK, size - len(received)))
            if self.refuse_at is not None and len(received) >= self.refuse_at:
                conn.sendall(bytes([0x83]))
                return
            conn.sendall(bytes([ota.CHUNK_OK]))
        assert hashlib.md5(received).hexdigest() == md5
        if self.flags & ota.SERVER_COMPRESSION:
            received = gzip.decompress(received)
        self.received.append((kind, received))
        conn.sendall(bytes([ota.RECEIVE_OK, ota.UPDATE_END_OK]))
        value(1)

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
            # Held, not listening, while asked: refused, and no other test can take it.
            self.assertIsNone(ota.probe("127.0.0.1", port=port, timeout=1))

    def test_an_update_password_is_reported_not_guessed(self):
        fake = self.fake(password="s3cret-71")
        with self.assertRaises(ota.NeedsPassword) as none:
            ota.upload("127.0.0.1", b"\xe9" * 100, port=fake.port)
        with self.assertRaises(ota.NeedsPassword) as another:
            ota.upload("127.0.0.1", b"\xe9" * 100, port=fake.port, password="not-it-9")
        self.assertEqual((none.exception.given, another.exception.given), (False, True))
        self.assertEqual(fake.received, [])

    def test_given_its_password_the_update_goes_through(self):
        image = bytes(range(256)) * 40
        for md5 in (False, True):  # current ESPHome's challenge, and an older one's
            fake = self.fake(password="s3cret-71", md5=md5)
            ota.upload("127.0.0.1", image, port=fake.port, password="s3cret-71")
            self.assertEqual(fake.received, [(ota.APP, image)])
        # SHA256 offered: current ESPHome with a password refuses a client without it.
        self.assertTrue(fake.features[0] & ota.CLIENT_SHA256_AUTH)

    def test_its_password_is_checked_without_an_update(self):
        fake = self.fake(password="s3cret-71")
        self.assertEqual(ota.wants("127.0.0.1", port=fake.port), ota.PASSWORD)
        self.assertEqual(ota.wants("127.0.0.1", "not-it-9", port=fake.port), ota.PASSWORD)
        self.assertEqual(ota.wants("127.0.0.1", "s3cret-71", port=fake.port), ota.NOTHING)
        self.assertEqual(ota.wants("127.0.0.1", port=self.fake().port), ota.NOTHING)  # none to want
        self.assertEqual(fake.received, [])
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            port = unused.getsockname()[1]
            # Held, not listening, while asked: refused, and no other test can take it.
            self.assertIsNone(ota.wants("127.0.0.1", port=port, timeout=1))

    def test_one_taking_only_encrypted_updates_wants_its_key(self):
        fake = self.fake(encrypted_only=True, key=KEY)
        self.assertTrue(ota.probe("127.0.0.1", port=fake.port).encrypted_only)  # it answers
        self.assertEqual(ota.wants("127.0.0.1", port=fake.port), ota.KEY)
        self.assertEqual(ota.wants("127.0.0.1", key=bytes(32), port=fake.port), ota.KEY)  # not its own
        self.assertEqual(ota.wants("127.0.0.1", key=KEY, port=fake.port), ota.NOTHING)
        with self.assertRaisesRegex(ota.NeedsEncryption, "API encryption key"):
            ota.upload("127.0.0.1", b"\xe9" * 100, port=fake.port, password="s3cret-71")
        with self.assertRaises(ota.WrongKey):
            ota.upload("127.0.0.1", b"\xe9" * 100, port=fake.port, key=bytes(32))
        self.assertEqual(fake.received, [])

    def test_given_its_key_the_update_goes_encrypted(self):
        image = bytes(range(256)) * 300  # whole blocks of whole frames, and a part of each
        for flags in (ota.SERVER_COMPRESSION, 0):
            for encrypted_only in (True, False):
                fake = self.fake(flags=flags, key=KEY, encrypted_only=encrypted_only)
                sent = []
                ota.upload("127.0.0.1", image, port=fake.port, key=KEY, progress=lambda a, b: sent.append(a))
                self.assertEqual(fake.received, [(ota.APP, image)])
                self.assertEqual(fake.encrypted, [True])
                self.assertTrue(fake.features[-1] & ota.CLIENT_NOISE)

    def test_its_key_does_instead_of_its_password(self):
        # ESPHome asks for no password in an encrypted session: the key is its proof.
        fake = self.fake(password="s3cret-71", key=KEY)
        self.assertEqual(ota.wants("127.0.0.1", key=KEY, port=fake.port), ota.NOTHING)
        self.assertEqual(ota.wants("127.0.0.1", port=fake.port), ota.PASSWORD)  # plainly, it does
        self.assertTrue(ota.probe("127.0.0.1", port=fake.port).encryption)
        ota.upload("127.0.0.1", b"\xe9" * 5000, port=fake.port, key=KEY)
        self.assertEqual(fake.received, [(ota.APP, b"\xe9" * 5000)])
        self.assertTrue(fake.encrypted[-1])  # the update's session; the check above reached one too

    def test_without_a_key_of_its_own_a_key_given_is_not_used(self):
        # An ESPHome with no API key offers no encrypted updates: sent plainly.
        fake = self.fake()
        ota.upload("127.0.0.1", b"\xe9" * 100, port=fake.port, key=KEY)
        self.assertEqual(fake.encrypted, [False])
        self.assertFalse(ota.probe("127.0.0.1", port=fake.port).encryption)

    def test_a_key_not_its_own_is_said_to_be(self):
        fake = self.fake(key=KEY)  # it takes plain updates too, but not with a wrong key offered
        with self.assertRaisesRegex(ota.WrongKey, "not the one ESPHome on the plug has"):
            ota.upload("127.0.0.1", b"\xe9" * 100, port=fake.port, key=bytes(32))
        self.assertEqual(ota.wants("127.0.0.1", key=bytes(32), port=fake.port), ota.KEY)
        self.assertEqual(fake.received, [])

    def test_a_refusal_part_way_is_said_in_words(self):
        for key in (None, KEY):
            fake = self.fake(flags=0, refuse_at=ota.BLOCK, key=key)
            with self.assertRaisesRegex(ota.OTAError, "writing the flash failed"):
                ota.upload("127.0.0.1", b"\xe9" * (3 * ota.BLOCK), port=fake.port, key=key)

    def test_a_partition_table_needs_a_firmware_that_allows_it(self):
        fake = self.fake(flags=0)
        with self.assertRaisesRegex(ota.OTAError, "allow_partition_access"):
            ota.upload("127.0.0.1", b"\0" * 3072, kind=ota.PARTITION_TABLE, port=fake.port)


if __name__ == "__main__":
    unittest.main()
