"""The tests reach nothing beyond this computer (tests/no_network.py)."""
import socket
import unittest
from pathlib import Path

from tests import no_network

TESTS = Path(__file__).resolve().parents[1]


class NoNetworkTests(unittest.TestCase):
    @staticmethod
    def outcome(body):
        """The result of a test whose body is `body`, run on its own."""
        class Inner(unittest.TestCase):
            def runTest(self):
                body()
        result = unittest.TestResult()
        Inner().run(result)
        return result

    @staticmethod
    def connect(host):
        # 192.0.2.0/24 is for documentation: refused before anything is sent, it reaches nothing either way.
        with socket.socket() as sock:
            sock.settimeout(1)
            sock.connect((host, 80))

    def test_a_test_connecting_beyond_this_computer_fails(self):
        result = self.outcome(lambda: self.connect("192.0.2.1"))
        self.assertEqual(len(result.errors), 1)
        self.assertIn("192.0.2.1:80", result.errors[0][1])

    def test_even_where_the_code_under_test_catches_the_error(self):
        def caught():
            try:
                self.connect("192.0.2.1")
            except BaseException:  # noqa: BLE001 - as code under test may
                pass
        result = self.outcome(caught)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("the error was caught", result.errors[0][1])

    def test_loopback_is_this_computer(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            result = self.outcome(lambda: self.connect_to(server.getsockname()))
        self.assertEqual((result.errors, result.failures), ([], []))
        self.assertFalse(no_network.beyond(("::1", 80)) or no_network.beyond(("localhost", 80)))
        self.assertTrue(no_network.beyond(("192.168.0.50", 80)) and no_network.beyond(("plug.example", 80)))

    @staticmethod
    def connect_to(address):
        with socket.create_connection(address, timeout=2):
            pass

    def test_every_test_package_switches_it_on(self):
        for package in sorted(path.parent for path in TESTS.glob("*/__init__.py")):
            with self.subTest(package=package.name):
                self.assertIn("no_network.install()", (package / "__init__.py").read_text(encoding="utf-8"))
        self.assertTrue(getattr(socket.socket, "no_network", False))


if __name__ == "__main__":
    unittest.main()
