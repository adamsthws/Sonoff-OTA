"""The tests reach nothing beyond this computer.  Each test package switches
this on (its __init__.py calls install()), so it holds however the tests are
run - tests/run.py, or `python -m unittest discover -s tests` as the
workflows run them.

Every address in the tests is made up, but a made-up address on a private
network can be a real device on the network the tests run on: a test that
misses a stub then really asks it.  So a connection to anything but
loopback fails the test that made it, straight away - OutsideConnection is a
BaseException, which the code under test's own `except Exception` lets
through - and, should something catch even that, once the test is over.
"""

from __future__ import annotations

import ipaddress
import socket
import unittest


class OutsideConnection(BaseException):
    """A test connected to an address beyond this computer: what it asks is to be stubbed."""


TRIED: list[str] = []   # each address a test tried, since it started


def beyond(address: object) -> bool:
    """Whether `address`, as a socket is given it, is beyond this computer:
    anything but loopback - a name too, which may resolve to anywhere."""
    if not isinstance(address, tuple) or not address:
        return False   # a Unix socket's path
    host = str(address[0])
    if host == "localhost":
        return False
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return True
    return not (ip.is_loopback or ip.is_unspecified)


def refuse(sock: socket.socket, address: object) -> None:
    if sock.family in (socket.AF_INET, socket.AF_INET6) and beyond(address):
        where = f"{address[0]}:{address[1]}"
        TRIED.append(where)
        raise OutsideConnection(f"a test connected to {where}, beyond this computer: stub what it asks")


def install() -> None:
    """Refuse every connection beyond loopback from here on, once."""
    if getattr(socket.socket, "no_network", False):
        return
    connect, connect_ex, sendto, run = (socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto,
                                        unittest.TestCase.run)

    def guarded_connect(self, address):
        refuse(self, address)
        return connect(self, address)

    def guarded_connect_ex(self, address):
        refuse(self, address)
        return connect_ex(self, address)

    def guarded_sendto(self, data, *args):
        refuse(self, args[-1])
        return sendto(self, data, *args)

    def checked_run(self, result=None):
        # Its own tries alone: a test that runs another (tests/repo/test_no_network.py) has its own.
        outer = TRIED[:]
        TRIED.clear()
        try:
            result = run(self, result)
        finally:
            tried = TRIED[:]
            TRIED[:] = outer
        failed = any(test is self for test, _ in result.errors + result.failures)
        if tried and not failed:
            result.addError(self, (OutsideConnection, OutsideConnection(
                f"the test connected to {', '.join(tried)}, beyond this computer, and the error was caught: "
                "stub what it asks"), None))
        return result

    socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto = (
        guarded_connect, guarded_connect_ex, guarded_sendto)
    unittest.TestCase.run = checked_run
    socket.socket.no_network = True
