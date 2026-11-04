"""Ctrl-C, and the window being closed, while a run works.

One Ctrl-C by mistake should not stop a conversion part way: the first only
asks, at the next safe point - the end of the stage under way, where every
step is recorded and the plug is idle - with a countdown that carries on by
itself. A second within GRACE seconds stops at once, as Ctrl-C always did:
every step is made to be stopped there, and checks the plug again next time.

At a question, or while waiting on the person (joining a network, say),
nothing is in flight, so Ctrl-C stops at once there, as before.

Around the little that must not be cut off half done (`deferred`: a firewall
rule being added and recorded), a Ctrl-C that would stop the run stops it as
soon as that is done.

The window being closed is taken as stopping at once, and remembered, so the
next run can say why the last one stopped.

The helpers a run starts (the servers the plug fetches from) get no Ctrl-C of
their own: they are started in their own process group (`child_group`), so a
first Ctrl-C leaves them working, and the run stops them itself when it stops.
"""

from __future__ import annotations

import math
import os
import signal
import sys
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable

# A second Ctrl-C within this long stops at once.
GRACE = 10.0
# The question at the safe point carries on by itself after this long.
CARRY_ON_AFTER = 30.0

_lock = threading.Lock()
_state: dict[str, Any] = {"first": None, "asking": 0, "closed": False, "installed": False,
                         "deferring": 0, "deferred": False, "shown_by": None, "logged_by": None}
NOTICE_TITLE = "! Are you sure?"


def install() -> None:
    """Take over Ctrl-C (SIGINT) and the window closing (SIGHUP) for this run."""
    if threading.current_thread() is not threading.main_thread():
        return
    signal.signal(signal.SIGINT, _on_interrupt)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _on_closed)
    _state["installed"] = True


def _say(text: str) -> None:
    # From a signal handler, at any point: straight to the terminal, never
    # through the output module, whose lock the interrupted code may hold.
    try:
        sys.stderr.write(text)
        sys.stderr.flush()
    except (OSError, ValueError):
        pass  # no terminal any more


def _on_interrupt(signum: int, frame: Any) -> None:
    if _state["logged_by"]:
        _state["logged_by"]()
    now = time.monotonic()
    first = _state["first"]
    if _state["asking"] or (first is not None and now - first <= GRACE):
        _state["first"] = None
        if _state["deferring"] and not _state["asking"]:
            # A question inside the block (sudo's password) comes before
            # anything is done: there it stops at once, as at any question.
            _state["deferred"] = True  # stopped at the end of the block
            _say("\n  Stopping as soon as this is finished.\n")
            return
        raise KeyboardInterrupt
    _state["first"] = now
    shown_by = _state["shown_by"]
    title, line = notice()
    text = shown_by() if shown_by else f"\n\n {title}\n     {line}\n"
    if text:
        _say(text)


def shown_by(where: Callable[[], str | None] | None) -> None:
    """`where` gives what a first Ctrl-C writes out - notice() laid out for
    the screen as it is - or None when the screen draws it itself."""
    _state["shown_by"] = where


def logged_by(where: Callable[[], None] | None) -> None:
    """`where` records each Ctrl-C as it arrives, from the signal handler."""
    _state["logged_by"] = where


def notice() -> list[str]:
    """What a first Ctrl-C says while a second would stop at once, its
    seconds counting down; nothing once they are up."""
    first = _state["first"]
    if first is None or _state["asking"]:
        return []
    left = GRACE - (time.monotonic() - first)
    if left <= 0:
        return []
    seconds = math.ceil(left)
    return [NOTICE_TITLE, f"Press Ctrl-C again within {seconds} second{'s' if seconds != 1 else ''} to stop now."]


def _on_closed(signum: int, frame: Any) -> None:
    _state["closed"] = True
    raise KeyboardInterrupt


def window_closed() -> bool:
    """Whether the run is stopping because its window was closed."""
    return bool(_state["closed"])


def pending() -> bool:
    """A first Ctrl-C not yet answered at a safe point."""
    return _state["first"] is not None


def clear() -> None:
    _state["first"] = None


@contextmanager
def deferred():
    """Around something that must not be cut off half done - a firewall rule
    being added, and recorded so it is removed again: a Ctrl-C that would stop
    the run stops it as soon as the block is done."""
    _state["deferring"] += 1
    try:
        yield
    finally:
        _state["deferring"] -= 1
        if not _state["deferring"] and _state["deferred"]:
            _state["deferred"] = False
            raise KeyboardInterrupt


@contextmanager
def asking():
    """Around a question, or a wait on the person: Ctrl-C stops at once."""
    with _lock:
        _state["asking"] += 1
    try:
        yield
    finally:
        with _lock:
            _state["asking"] -= 1


def child_group() -> dict[str, Any]:
    """Popen arguments that keep a helper out of the terminal's Ctrl-C."""
    if os.name == "nt":
        import subprocess
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}
