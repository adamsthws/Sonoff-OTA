"""How short of memory this computer was during a fault scenario. An emulated
plug that is swapped out has its connections time out with nothing wrong, so
both fault suites note this beside a result, and say so of a failure."""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

# Under this much available, or with tasks held up on memory this much of the
# time, a plug is starved. The percentage is a judgement, not a measured limit.
LOW_MEMORY_KB = 350_000
STALLED_PERCENT = 15


def memory_now() -> tuple[int, int | None]:
    """KB available, and the microseconds tasks have been held up on memory since
    boot (None where the kernel does not keep that)."""
    available = int(re.search(r"MemAvailable:\s+(\d+)", Path("/proc/meminfo").read_text(encoding="utf-8")).group(1))
    try:
        stalled = int(re.search(r"^some .*total=(\d+)", Path("/proc/pressure/memory").read_text(encoding="utf-8"),
                                re.M).group(1))
    except OSError:
        stalled = None
    return available, stalled


def memory_note(lowest: int, stalled_from: int | None, stalled_to: int | None, seconds: float) -> dict:
    percent = None
    if stalled_from is not None and stalled_to is not None and seconds > 0:
        percent = round((stalled_to - stalled_from) / (seconds * 10_000), 1)
    return {"lowest_available_mb": lowest // 1024, "stalled_percent": percent,
            "starved": lowest < LOW_MEMORY_KB or (percent or 0) >= STALLED_PERCENT}


class MemoryWatch(threading.Thread):
    """The lowest memory available from now until note()."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.lowest, self.stalled_from = memory_now()
        self.started = time.monotonic()
        self.halt = threading.Event()

    def run(self) -> None:
        while not self.halt.wait(5):
            self.lowest = min(self.lowest, memory_now()[0])

    def note(self) -> dict:
        self.halt.set()
        return memory_note(self.lowest, self.stalled_from, memory_now()[1], time.monotonic() - self.started)
