"""Keep this computer awake while a run works, with what each system has already.

A computer going to sleep part way through a conversion stops the run where
it stands - the plug carries on, waiting for a computer that is not there.
So for the length of a run, sleep is held off, with nothing installed:

- Linux (systemd): `systemd-inhibit`, around a watcher that ends with the run;
- macOS: `caffeinate -i -w`, which ends with the run by itself;
- Windows: SetThreadExecutionState, which ends with the run's process.

Each lets go when the run ends, however it ends - a crash included - so it
leaves nothing behind. Where none is to be had, the run says so and goes on.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

_held: dict[str, object] = {}


def keep_awake() -> str:
    """Hold off sleep until this process ends; what did it, or why not."""
    if "how" in _held:
        return str(_held["how"])
    how = _windows() if os.name == "nt" else _mac() if sys.platform == "darwin" else _linux()
    _held["how"] = how
    return how


def _linux() -> str:
    tool = shutil.which("systemd-inhibit")
    if not tool:
        return "not kept awake: no systemd-inhibit on this system"
    # The watcher ends within seconds of this process, however it ends, so
    # the hold on sleep never outlives the run.
    watcher = f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 5; done"
    try:
        process = subprocess.Popen(
            [tool, "--what=sleep:idle", "--who=Autoflash",
             "--why=A plug is being converted", "--mode=block", "sh", "-c", watcher],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except OSError as exc:
        return f"not kept awake: {exc}"
    _held["process"] = process
    return "kept awake with systemd-inhibit"


def _mac() -> str:
    tool = shutil.which("caffeinate")
    if not tool:
        return "not kept awake: no caffeinate on this system"
    try:
        process = subprocess.Popen([tool, "-i", "-w", str(os.getpid())],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        return f"not kept awake: {exc}"
    _held["process"] = process
    return "kept awake with caffeinate"


def _windows() -> str:
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        if not ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
            return "not kept awake: Windows refused"
    except (OSError, AttributeError) as exc:
        return f"not kept awake: {exc}"
    return "kept awake with SetThreadExecutionState"


def let_sleep() -> None:
    """Let go early, at the end of a run (it lets go by itself at exit anyway)."""
    process = _held.pop("process", None)
    if process is not None:
        try:
            process.terminate()
        except OSError:
            pass
    if os.name == "nt" and _held.pop("how", None):
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        except (OSError, AttributeError):
            pass
    _held.pop("how", None)
