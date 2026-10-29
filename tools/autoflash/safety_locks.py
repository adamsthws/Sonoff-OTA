"""The safety locks - RECOVERY_LOCK and REPARTITION_LOCK - and the runs that
lift them.

Each is a file in tools/espressif (DIR, under the repository root) whose
presence makes its helper refuse to start (the bridge's sender, the
memory-layout server). A step that needs the helper lifts the lock
- moves it aside - for as long as it works, and puts it back after.

Two runs at once, on two plugs, may both need the same one. So each run that
lifts a lock says so with a file of its own (under captures/.safety-locks),
held with the operating system's file lock for as long as it needs the lock
lifted. The lock goes back when the last of them is done - and a run that
stopped without putting it back (a crash, a power cut) no longer counts, its
file lock let go with it, so the next run to look puts the lock back.
"""

from __future__ import annotations

import os
import secrets
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import hostsys, oslock

NAMES = ("RECOVERY_LOCK", "REPARTITION_LOCK")
# Where the lock files live, under the repository root: with the routes they guard.
DIR = Path("tools", "espressif")


def _record(root: Path) -> Path:
    return root / "captures" / ".safety-locks"


def _paths(root: Path, name: str) -> tuple[Path, Path, Path]:
    return root / DIR / name, root / DIR / f"{name}.owner-authorized", _record(root) / f"{name}.lifted-by"


@contextmanager
def _guarded(root: Path, name: str) -> Iterator[None]:
    """One run at a time between looking at a lock and moving it."""
    _record(root).mkdir(parents=True, exist_ok=True)
    with open(_record(root) / f"{name}.guard", "a+", encoding="utf-8") as guard:
        oslock.lock(guard)
        try:
            yield
        finally:
            oslock.unlock(guard)


def lifters(root: Path, name: str) -> list[Path]:
    """The runs that have this lock lifted now; files left by runs that ended
    without saying so are removed."""
    owners = _paths(root, name)[2]
    live = []
    if owners.is_dir():
        for marker in owners.iterdir():
            if oslock.held_elsewhere(marker):
                live.append(marker)
            else:
                marker.unlink(missing_ok=True)
    return live


def _put_back_if_unused(root: Path, name: str) -> bool:
    lock, moved, _ = _paths(root, name)
    if moved.exists() and not lock.exists() and not lifters(root, name):
        hostsys.replace_file(moved, lock)
        return True
    return False


@contextmanager
def lifted(root: Path, name: str) -> Iterator[None]:
    """The lock moved aside for the length of the block, and put back after
    unless another run still needs it lifted."""
    owners = _paths(root, name)[2]
    owners.mkdir(parents=True, exist_ok=True)
    marker = owners / f"{os.getpid()}-{secrets.token_hex(4)}"
    handle = open(marker, "w", encoding="utf-8")
    oslock.lock(handle)
    try:
        with _guarded(root, name):
            lock, moved, _ = _paths(root, name)
            if lock.exists():
                hostsys.replace_file(lock, moved)
        yield
    finally:
        with _guarded(root, name):
            oslock.unlock(handle)
            handle.close()
            marker.unlink(missing_ok=True)
            _put_back_if_unused(root, name)


def restore_left(root: Path) -> list[str]:
    """Put back every lock a run left lifted and no longer needs; the names put back."""
    restored = []
    for name in NAMES:
        with _guarded(root, name):
            if _put_back_if_unused(root, name):
                restored.append(name)
    return restored


def in_place(root: Path, name: str) -> bool:
    """In place, or lifted by a run that is working now (another plug's)."""
    return _paths(root, name)[0].exists() or bool(lifters(root, name))
