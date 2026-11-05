"""emu.sh puts the emulator on a /24 Docker network of its own, on a subnet
nothing on the computer uses yet - its routes, its other Docker networks - so
that making it never takes another network's addresses."""

import re
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "espressif" / "emulator" / "emu.sh"


def pick(taken: str) -> str:
    """The subnet emu.sh's picker chooses, given what `ip -4 route` and the Docker
    networks' subnets say is taken (one per line)."""
    program = re.search(r"\"\$PY\" -I -c '\n(.*?)'\)", SCRIPT.read_text(encoding="utf-8"), re.S).group(1)
    done = subprocess.run([sys.executable, "-I", "-c", program], input=taken,
                          capture_output=True, text=True, timeout=60)
    if done.returncode:
        raise AssertionError(done.stderr)
    return done.stdout.strip()


class EmulatorNetworkTests(unittest.TestCase):
    def test_the_first_free_candidate_is_taken(self):
        self.assertEqual(pick("default\n192.0.2.0/24\n172.17.0.0/16\n"), "198.18.60.0/24")

    def test_one_that_overlaps_a_route_or_network_is_passed_over(self):
        self.assertEqual(pick("198.18.0.0/15\n"), "172.31.60.0/24")          # a route wider than it
        self.assertEqual(pick("198.18.60.128/25\n172.16.0.0/12\n"), "10.231.60.0/24")

    def test_none_free_is_none(self):
        self.assertEqual(pick("198.18.0.0/15\n172.16.0.0/12\n10.0.0.0/8\n"), "")

    def test_ipv6_and_blank_lines_are_ignored(self):
        self.assertEqual(pick("\nfd00::/64\ndefault\n"), "198.18.60.0/24")


if __name__ == "__main__":
    unittest.main()
