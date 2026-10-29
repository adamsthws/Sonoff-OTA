"""The Noise protocol as ESPHome speaks it, in plain Python: against the RFCs'
test vectors, and a handshake recorded from noiseprotocol, the library
ESPHome's own Python tools use."""
import base64
import struct
import unittest

from tools.autoflash import noise


class X25519Tests(unittest.TestCase):
    def test_rfc_7748_vectors(self):
        # 5.2
        scalar = bytes.fromhex("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4")
        point = bytes.fromhex("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c")
        self.assertEqual(noise.x25519(scalar, point).hex(),
                         "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552")
        # 6.1: Alice and Bob agree.
        alice = bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
        bob = bytes.fromhex("5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb")
        self.assertEqual(noise.public_key(alice).hex(),
                         "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")
        self.assertEqual(noise.public_key(bob).hex(),
                         "de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f")
        shared = "4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742"
        self.assertEqual(noise.x25519(alice, noise.public_key(bob)).hex(), shared)
        self.assertEqual(noise.x25519(bob, noise.public_key(alice)).hex(), shared)


class ChaChaPolyTests(unittest.TestCase):
    def test_rfc_8439_block(self):  # 2.3.2
        block = noise._block(struct.unpack("<8I", bytes(range(32))), 1,
                             struct.unpack("<3I", bytes.fromhex("000000090000004a00000000")))
        self.assertEqual(block.hex(), "10f1e7e4d13b5915500fdd1fa32071c4c7d1f4c733c068030422aa9ac3d46c4e"
                                      "d2826446079faa0914c2d705d98b02a2b5129cd1de164eb9cbd083e8a2503c4e")

    def test_rfc_8439_poly1305(self):  # 2.5.2
        key = bytes.fromhex("85d6be7857556d337f4452fe42d506a80103808afb0db2fd4abff6af4149f51b")
        self.assertEqual(noise.poly1305(key, b"Cryptographic Forum Research Group").hex(),
                         "a8061dc1305136c6c22b8baf0c0127a9")

    def test_rfc_8439_aead(self):  # 2.8.2
        plaintext = (b"Ladies and Gentlemen of the class of '99: If I could offer you only one tip "
                     b"for the future, sunscreen would be it.")
        key, nonce = bytes(range(0x80, 0xA0)), bytes.fromhex("070000004041424344454647")
        aad = bytes.fromhex("50515253c0c1c2c3c4c5c6c7")
        sealed = noise.seal(key, nonce, aad, plaintext)
        self.assertEqual(sealed.hex(),
                         "d31a8d34648e60db7b86afbc53ef7ec2a4aded51296e08fea9e2b5a736ee62d6"
                         "3dbea45e8ca9671282fafb69da92728b1a71de0a9e060b2905d6a5b67ecd3b36"
                         "92ddbd7f2d778b8c9803aee328091b58fab324e4fad675945585808b4831d7bc"
                         "3ff4def08e4b7a9de576d26586cec64b6116"
                         "1ae10b594f09e26a7e902ecbd0600691")
        self.assertEqual(noise.open_sealed(key, nonce, aad, sealed), plaintext)
        for damaged in (sealed[:-1] + bytes([sealed[-1] ^ 1]), bytes([sealed[0] ^ 1]) + sealed[1:]):
            with self.assertRaises(noise.NoiseError):
                noise.open_sealed(key, nonce, aad, damaged)
        with self.assertRaises(noise.NoiseError):
            noise.open_sealed(key, nonce, b"other", sealed)  # sealed to its associated data too


class HandshakeTests(unittest.TestCase):
    PSK = bytes(range(32))
    # Recorded from noiseprotocol 0.3.1 (aioesphomeapi's, and so espota2's)
    # with both ends' ephemeral keys given: an update session's prologue.
    INITIATOR = bytes.fromhex("e61ef9919cde45dd5f82166404bd08e38bceb5dfdfded0a34c8df7ed542214d1")
    RESPONDER = bytes.fromhex("4a3acbfdb163dec651dfa3194dece676d437029c62a408b4c5ea9114246e4893")
    PROLOGUE = bytes.fromhex("4e6f6973654f5441496e69746c26f75c4500020f4806")
    MESSAGE1 = bytes.fromhex("6bc3822a2aa7f4e6981d6538692b3cdf3e6df9eea6ed269eb41d93c22757b75a"
                             "218d678df76c9391cf3f91b65a692705")
    MESSAGE2 = bytes.fromhex("31e0303fd6418d2f8c0e78b91f22e8caed0fbe48656dcf4767e4834f701b8f62"
                             "949c756ccf12371d6c525ac25a0cbb38")
    FIRST = bytes.fromhex("6d18736391dc6b8a234a808b31aa9b9f85")  # 0x00, the update type
    REPLY = bytes.fromhex("cfad89eb811af192baf88b3485ea035290")  # 0x41, AUTH_OK
    SECOND = bytes.fromhex("d050c0de54d9084c9d0237697434ee99dfdeb69d")  # a size

    def test_it_matches_the_reference_byte_for_byte(self):
        ours = noise.Handshake(self.PSK, self.PROLOGUE, ephemeral=self.INITIATOR)
        self.assertEqual(ours.write_message(), self.MESSAGE1)
        self.assertEqual(ours.read_message(self.MESSAGE2), b"")
        sending, receiving = ours.split()
        self.assertEqual(sending.encrypt(b"\x00"), self.FIRST)
        self.assertEqual(receiving.decrypt(self.REPLY), b"\x41")
        self.assertEqual(sending.encrypt(b"\x00\x00\x10\x00"), self.SECOND)

    def test_as_the_responder_too(self):
        plug = noise.Handshake(self.PSK, self.PROLOGUE, initiator=False, ephemeral=self.RESPONDER)
        plug.read_message(self.MESSAGE1)
        self.assertEqual(plug.write_message(), self.MESSAGE2)
        sending, receiving = plug.split()
        self.assertEqual(receiving.decrypt(self.FIRST), b"\x00")
        self.assertEqual(sending.encrypt(b"\x41"), self.REPLY)

    def test_two_ends_with_the_same_key_agree_and_another_key_is_refused(self):
        client = noise.Handshake(self.PSK, b"prologue")
        plug = noise.Handshake(self.PSK, b"prologue", initiator=False)
        plug.read_message(client.write_message())
        client.read_message(plug.write_message())
        (to_plug, from_plug), (plug_sends, plug_receives) = client.split(), plug.split()
        for size in (0, 1, 1024, 3000):
            self.assertEqual(plug_receives.decrypt(to_plug.encrypt(b"x" * size)), b"x" * size)
            self.assertEqual(from_plug.decrypt(plug_sends.encrypt(b"y" * size)), b"y" * size)
        for psk, prologue in ((bytes(32), b"prologue"), (self.PSK, b"changed")):
            other = noise.Handshake(psk, prologue, initiator=False)
            with self.assertRaises(noise.NoiseError):
                other.read_message(noise.Handshake(self.PSK, b"prologue").write_message())

    def test_a_key_as_the_yaml_config_has_it(self):
        text = base64.b64encode(self.PSK).decode()
        for given in (text, f'"{text}"', f"  {text}\n"):
            self.assertEqual(noise.decode_key(given), self.PSK)
        for wrong, why in (("not base64!", "not base64"), (base64.b64encode(bytes(16)).decode(), "16 bytes")):
            with self.assertRaisesRegex(ValueError, why):
                noise.decode_key(wrong)


class FrameTests(unittest.TestCase):
    def reader(self, data):
        stream = bytearray(data)

        def read(size):
            piece = bytes(stream[:size])
            del stream[:size]
            return piece
        return read

    def test_frames_and_what_is_not_one(self):
        self.assertEqual(noise.frame(b"abc"), b"\x01\x00\x03abc")
        self.assertEqual(noise.read_frame(self.reader(noise.frame(b"abc"))), b"abc")
        with self.assertRaisesRegex(noise.NoiseError, "0x00"):
            noise.read_frame(self.reader(b"\x00Bad indicator byte"))  # a plain ESPHome API's answer

    def test_a_refused_handshake_says_why(self):
        client = noise.Handshake(bytes(32), b"")
        client.write_message()
        with self.assertRaisesRegex(noise.NoiseError, noise.WRONG_KEY_REASON):
            noise.handshake_reply(client, b"\x01" + noise.WRONG_KEY_REASON.encode())
        # A reply that does not check out says the same: another key.
        plug = noise.Handshake(bytes(range(32)), b"", initiator=False)
        plug.read_message(noise.Handshake(bytes(range(32)), b"").write_message())
        client = noise.Handshake(bytes(32), b"")
        client.write_message()
        with self.assertRaisesRegex(noise.NoiseError, noise.WRONG_KEY_REASON):
            noise.handshake_reply(client, bytes([noise.HANDSHAKE_OK]) + plug.write_message())


if __name__ == "__main__":
    unittest.main()
