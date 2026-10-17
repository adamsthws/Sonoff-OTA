"""The operating system's own file locks, held by an open file for as long as
the process that took them lives: let go however it ends, a crash included.
On Windows a lock on the file's first byte (msvcrt), elsewhere flock."""

from __future__ import annotations

import os
import time
from typing import IO


def try_lock(handle: IO[str]) -> bool:
    """Take the lock if nobody else has it; False if another process does."""
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def lock(handle: IO[str], timeout: float = 60.0) -> None:
    """Wait for the lock, for a short critical section."""
    deadline = time.monotonic() + timeout
    while not try_lock(handle):
        if time.monotonic() > deadline:
            raise TimeoutError(f"{handle.name} stayed locked for {timeout:.0f}s")
        time.sleep(0.05)


def unlock(handle: IO[str]) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def held_elsewhere(path: os.PathLike[str] | str) -> bool:
    """Whether another live process holds the lock on `path` now."""
    try:
        handle = open(path, "a+", encoding="utf-8")
    except OSError:
        return False
    try:
        if try_lock(handle):
            unlock(handle)
            return False
        return True
    finally:
        handle.close()
