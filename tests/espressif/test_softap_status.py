import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "tools/espressif/emulator/container/bin/softap-status"


def run(*lines: bytes) -> tuple[bytes, bytes]:
    with tempfile.TemporaryDirectory() as directory:
        state = Path(directory) / "softap"
        state.write_bytes(b"")
        result = subprocess.run([sys.executable, "-I", str(SCRIPT), str(state)], input=b"".join(lines),
                                stdout=subprocess.PIPE, check=True)
        return result.stdout, state.read_bytes()


class SoftapStatusTests(unittest.TestCase):
    def test_the_network_on_air_is_kept(self):
        self.assertEqual(run(b"esp32_wifi: guest SoftAP tasmota-ABCDEF-1234 up\n")[1], b"tasmota-ABCDEF-1234")

    def test_gone_empties_it(self):
        lines = b"esp32_wifi: guest SoftAP plug up\n", b"esp32_wifi: guest SoftAP plug gone (no beacons)\n"
        self.assertEqual(run(*lines)[1], b"")

    def test_a_rename_replaces_it(self):
        lines = b"esp32_wifi: guest SoftAP one up\n", b"esp32_wifi: guest SoftAP two up\n"
        self.assertEqual(run(*lines)[1], b"two")

    def test_a_name_with_spaces_and_a_line_the_guest_had_begun(self):
        self.assertEqual(run(b"I (52) wifi:esp32_wifi: guest SoftAP my plug up\r\n")[1], b"my plug")

    def test_every_line_is_passed_on_and_others_change_nothing(self):
        lines = b"esp32_wifi: guest SoftAP plug up\n", b"boot line \xff\n", b"last, no newline"
        output, state = run(*lines)
        self.assertEqual(output, b"".join(lines))
        self.assertEqual(state, b"plug")


if __name__ == "__main__":
    unittest.main()
