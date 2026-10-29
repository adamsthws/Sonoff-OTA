#!/usr/bin/env python3
"""Wire Windows into the emulator's home network (emu.sh --windows).

Carries Ethernet frames between the "Autoflash emulator" TAP adapter and the
container's windows-link, over TCP port 7860, so that Windows is a computer
on the emulated Wi-Fi: the plug's MAC in its ARP table, its mDNS answers, its
connections in through the Windows firewall - what the conversion tool meets
on a real network. Each frame goes preceded by its length, two bytes,
big-endian.

The emulator is on this computer, under WSL (127.0.0.1, started by
tools/espressif/emulator/emu.ps1), or elsewhere - for the Windows VM of
tools/espressif/emulator/windows-test.sh, the Linux machine running it:

    windows_link.py [--host ADDRESS]

The adapter is made by tools/espressif/emulator/windows-bootstrap.ps1. It shows as
connected only while the emulator is reachable, so at other times Windows
ignores it.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import socket
import struct
import sys
import threading
import time
import winreg

ADAPTER = "Autoflash emulator"
HOST, PORT = "127.0.0.1", 7860
NETWORK_CLASS = r"SYSTEM\CurrentControlSet\Control\Network\{4D36E972-E325-11CE-BFC1-08002BE10318}"

GENERIC_READ_WRITE = 0xC0000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_FLAG_OVERLAPPED = 0x40000000
ERROR_IO_PENDING = 997
ERROR_ACCESS_DENIED = 5
ERROR_GEN_FAILURE = 31  # what TAP-Windows says when another program has the adapter open
ERROR_SHARING_VIOLATION = 32
INVALID_HANDLE = ctypes.c_void_p(-1).value
# CTL_CODE(FILE_DEVICE_UNKNOWN, 6, METHOD_BUFFERED, FILE_ANY_ACCESS), from tap-windows.h
TAP_IOCTL_SET_MEDIA_STATUS = 0x220018

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class LinkError(Exception):
    """Something the person running the link has to sort out."""


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                ("Offset", wt.DWORD), ("OffsetHigh", wt.DWORD), ("hEvent", wt.HANDLE)]


kernel32.CreateFileW.restype = wt.HANDLE
kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                 wt.DWORD, wt.DWORD, wt.HANDLE]
kernel32.CreateEventW.restype = wt.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
kernel32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                              ctypes.POINTER(wt.DWORD), ctypes.POINTER(OVERLAPPED)]
kernel32.WriteFile.argtypes = kernel32.ReadFile.argtypes
kernel32.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p,
                                     wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.POINTER(OVERLAPPED)]
kernel32.GetOverlappedResult.argtypes = [wt.HANDLE, ctypes.POINTER(OVERLAPPED),
                                         ctypes.POINTER(wt.DWORD), wt.BOOL]
kernel32.CloseHandle.argtypes = [wt.HANDLE]


def adapter_guid(name: str) -> str | None:
    """The adapter's GUID, from its name as Network Connections shows it."""
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, NETWORK_CLASS) as root:
        for index in range(winreg.QueryInfoKey(root)[0]):
            guid = winreg.EnumKey(root, index)
            try:
                with winreg.OpenKey(root, guid + r"\Connection") as connection:
                    if winreg.QueryValueEx(connection, "Name")[0] == name:
                        return guid
            except OSError:
                continue
    return None


class Tap:
    """The TAP-Windows6 device behind the adapter: whole Ethernet frames in and out."""

    def __init__(self, guid: str) -> None:
        self.handle = kernel32.CreateFileW(
            rf"\\.\Global\{guid}.tap", GENERIC_READ_WRITE, 0, None, OPEN_EXISTING,
            FILE_ATTRIBUTE_SYSTEM | FILE_FLAG_OVERLAPPED, None)
        if self.handle in (None, INVALID_HANDLE):
            error = ctypes.get_last_error()
            if error in (ERROR_ACCESS_DENIED, ERROR_GEN_FAILURE, ERROR_SHARING_VIOLATION):
                raise LinkError(f"'{ADAPTER}' is already in use - another link is running. "
                                "Close its window first, or keep using it.")
            raise ctypes.WinError(error)

    def _start(self) -> OVERLAPPED:
        overlapped = OVERLAPPED()
        overlapped.hEvent = kernel32.CreateEventW(None, True, False, None)
        return overlapped

    def read(self) -> bytes:
        buffer = ctypes.create_string_buffer(65536)
        overlapped = self._start()
        started = kernel32.ReadFile(self.handle, buffer, len(buffer), None, ctypes.byref(overlapped))
        length = self._finish(overlapped, started)  # before buffer.raw: it copies the buffer as it is
        return buffer.raw[:length]

    def write(self, frame: bytes) -> None:
        overlapped = self._start()
        started = kernel32.WriteFile(self.handle, frame, len(frame), None, ctypes.byref(overlapped))
        self._finish(overlapped, started)

    def set_connected(self, connected: bool) -> None:
        status = wt.ULONG(1 if connected else 0)
        returned = wt.DWORD()
        overlapped = self._start()
        started = kernel32.DeviceIoControl(self.handle, TAP_IOCTL_SET_MEDIA_STATUS, ctypes.byref(status),
                                           ctypes.sizeof(status), ctypes.byref(status), ctypes.sizeof(status),
                                           ctypes.byref(returned), ctypes.byref(overlapped))
        self._finish(overlapped, started)

    def _finish(self, overlapped: OVERLAPPED, started: bool) -> int:
        """Wait for one overlapped operation to complete; its byte count."""
        try:
            if not started and ctypes.get_last_error() != ERROR_IO_PENDING:
                raise ctypes.WinError(ctypes.get_last_error())
            done = wt.DWORD()
            if not kernel32.GetOverlappedResult(self.handle, ctypes.byref(overlapped), ctypes.byref(done), True):
                raise ctypes.WinError(ctypes.get_last_error())
            return done.value
        finally:
            kernel32.CloseHandle(overlapped.hEvent)


class Link:
    """One reader for the adapter's whole life, sending to whichever connection is current."""

    def __init__(self, tap: Tap) -> None:
        self.tap = tap
        self.lock = threading.Lock()
        self.conn: socket.socket | None = None
        threading.Thread(target=self._from_windows, daemon=True).start()

    def _from_windows(self) -> None:
        while True:
            try:
                frame = self.tap.read()
            except OSError:
                # TAP-Windows aborts reads while the adapter is disconnected.
                time.sleep(0.2)
                continue
            with self.lock:
                conn = self.conn
            if conn is None:
                continue  # the emulator is not there: as if the cable were out
            try:
                conn.sendall(struct.pack(">H", len(frame)) + frame)
            except OSError:
                pass  # the other direction notices, and reconnects

    def carry(self, conn: socket.socket) -> None:
        """Frames from the emulator to Windows, until the connection ends."""
        with self.lock:
            self.conn = conn
        pending = b""
        try:
            while True:
                try:
                    data = conn.recv(65536)
                except socket.timeout:
                    continue  # lets Ctrl-C through
                if not data:
                    return
                pending += data
                while len(pending) >= 2:
                    (length,) = struct.unpack(">H", pending[:2])
                    if len(pending) < 2 + length:
                        break
                    self.tap.write(pending[2:2 + length])
                    pending = pending[2 + length:]
        finally:
            with self.lock:
                self.conn = None


def connect(host: str) -> socket.socket | None:
    try:
        conn = socket.create_connection((host, PORT), timeout=2)
    except OSError:
        return None
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    conn.settimeout(1.0)
    return conn


def main() -> int:
    parser = argparse.ArgumentParser(description="Wire Windows into the emulator's home network.")
    parser.add_argument("--host", default=HOST, help=f"where the emulator is (default {HOST})")
    host = parser.parse_args().host
    guid = adapter_guid(ADAPTER)
    if guid is None:
        raise LinkError(f"No network adapter called '{ADAPTER}': run tools\\emulator\\windows-bootstrap.ps1.")
    tap = Tap(guid)
    link = Link(tap)
    tap.set_connected(False)
    waiting = False
    try:
        while True:
            conn = connect(host)
            if conn is None:
                if not waiting:
                    print(f"Waiting for the emulator at {host}:{PORT}...", flush=True)
                    waiting = True
                time.sleep(2)
                continue
            waiting = False
            tap.set_connected(True)
            print(f"Connected: Windows is on the emulated home Wi-Fi through '{ADAPTER}'.", flush=True)
            try:
                link.carry(conn)
            except OSError as exc:
                print(f"Link error: {exc}", flush=True)
            finally:
                conn.close()
                tap.set_connected(False)
            print("The emulator went away.", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        tap.set_connected(False)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (LinkError, OSError) as exc:
        # In a window of its own (emu.ps1), which would close before this is read.
        print(exc)
        try:
            input("Press Enter to close this window.")
        except (EOFError, KeyboardInterrupt):
            pass
        sys.exit(1)
