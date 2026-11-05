"""Example addresses are made up (CONTRIBUTING.md, Keeping private things out):
one in a test can be a real device where the tests run.  The leak scan knows
only this computer's own, so this holds every tracked file to the rule."""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADDRESS = re.compile(r"(?<![0-9.])192\.168\.(\d+)\.\d+")
SUBNETS = {
    "4",    # the setup networks plugs really make
    "60",   # the S60 emulator's network
    "99",   # examples people read
    "122",  # libvirt's bridge, which discovery leaves out
}
# Where the Beken emulator's built-in network, which its plug really joins, is used or described.
BEKEN_EMULATOR_SUBNET = "1"
BEKEN_EMULATOR_FILES = {
    "devices/sonoff-s61/openbeken.md",
    "docs/beken-s61-emulation.md",
    "tools/beken/README.md",
    "tools/beken/cloud_dial.py",
    "tools/beken/emulator/patches/0005-emulator-bridge-the-Wi-Fi-peer-to-a-host-TAP-device-.patch",
    "tools/beken/seed_join.py",
}


def strays(path: str, text: str) -> list[str]:
    allowed = SUBNETS | ({BEKEN_EMULATOR_SUBNET} if path in BEKEN_EMULATOR_FILES else set())
    return [found.group() for found in ADDRESS.finditer(text) if found.group(1) not in allowed]


class ExampleAddressTests(unittest.TestCase):
    def test_no_tracked_file_holds_an_address_that_could_be_real(self):
        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                                 encoding="utf-8", check=True).stdout.splitlines()
        for path in tracked:
            try:
                text = (ROOT / path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):  # a firmware image, or a file not in this tree
                continue
            with self.subTest(path=path):
                self.assertEqual(strays(path, text), [])

    def test_it_catches_a_home_address_and_leaves_the_rest(self):
        home = "192.168."
        self.assertEqual(strays("tests/x.py", f"{home}0.50 {home}1.1 {home}2.7"), [f"{home}0.50", f"{home}1.1", f"{home}2.7"])
        self.assertEqual(strays("docs/x.md", f"{home}99.50 {home}4.1 {home}60.1 {home}122.1 192.0.2.50"), [])
        self.assertEqual(strays("tools/beken/cloud_dial.py", f"{home}1.1"), [])
        self.assertEqual(strays("tools/beken/cloud_dial.py", f"{home}0.1"), [f"{home}0.1"])
