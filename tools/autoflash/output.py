"""Where the conversion's messages go.

Everything is written to the run's log file, with the time it happened.  What
the terminal shows depends on the mode:

- concise (the default for a run): one line per stage, redrawn in place as it
  goes from "underway" to "complete", plus anything the person running it has
  to read or answer;
- verbose: every message, as the tool has always printed them.

Before a run starts, and for the commands that only report (status, list,
cleanup), everything is shown: there is nothing to condense.
"""

from __future__ import annotations

import codecs
import collections
import datetime
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO

IS_WINDOWS = platform.system() == "Windows"

GREEN, RED, YELLOW, BOLD, RESET = "\x1b[32m", "\x1b[31m", "\x1b[33m", "\x1b[1m", "\x1b[0m"

_lock = threading.RLock()

# Colour and hyperlink sequences, which take no room on screen.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\]8;;[^\x1b]*\x1b\\\\")

# Words that mark a helper tool's line as the reason it failed.
PROBLEM_RE = re.compile(r"error|refus|fail|warning|interrupted|did not|denied|invalid|unable|timed? ?out",
                        re.IGNORECASE)


class _State:
    log: IO[str] | None = None
    log_path: Path | None = None
    concise = False
    stage: str | None = None          # "Stage 5 of 14 - Installing Tasmota", as logged
    stage_prefix = ""                 # "Stage  5 of 14", coloured with the outcome
    stage_screen = ""                 # the same, numbers and name padded into columns
    name_width = 0                    # the longest stage name, so the columns line up
    transient_rows: int | None = None  # rows on screen since transient_begin()
    pending_prompt: str | None = None  # a question on screen, waiting for its answer
    pending_answer = ""
    stage_started = 0.0
    live = 0                          # characters of an unfinished line on screen
    live_is_stage = False
    paused = False                    # waiting for the keyboard: draw nothing
    ticker: threading.Thread | None = None
    tool_tail: collections.deque = collections.deque(maxlen=40)  # the last helper's last lines
    stop_ticker = threading.Event()


_s = _State()


# --------------------------------------------------------------------- setup


def start(log_path: Path | None, *, verbose: bool) -> None:
    """Begin a run: open its log, and condense the terminal unless verbose."""
    with _lock:
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            _s.log = log_path.open("a", encoding="utf-8")
            _s.log_path = log_path
            _write_log(f"=== run started{' (verbose)' if verbose else ''} ===")
        _s.concise = not verbose
        _s.paused = False
        if _s.concise and sys.stdout.isatty() and _s.ticker is None:
            # Keeps the stage clock moving through waits that print nothing.
            _s.stop_ticker.clear()
            _s.ticker = threading.Thread(target=_tick_forever, daemon=True)
            _s.ticker.start()


def finish() -> None:
    """End cleanly: no half-drawn line left behind, and the log closed."""
    _s.stop_ticker.set()
    if _s.ticker is not None:
        _s.ticker.join(2.0)
        _s.ticker = None
    with _lock:
        _break()
        if _s.log is not None:
            _write_log("=== run ended ===")
            _s.log.close()
        _s.log = _s.log_path = None
        _s.concise = False
        _s.stage = None
        _s.transient_rows = None
        _s.name_width = 0


def log_path() -> Path | None:
    return _s.log_path


def concise() -> bool:
    return _s.concise


def colour_ok() -> bool:
    """ANSI colour only on a real terminal that understands it."""
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    if IS_WINDOWS:
        # Windows Terminal and current consoles understand ANSI; the classic
        # console does only once virtual-terminal mode is on.
        return bool(os.environ.get("WT_SESSION")) or _enable_windows_vt()
    return True


def _enable_windows_vt() -> bool:
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError):
        return False


def hyperlink(url: str) -> str:
    """The URL as a link the terminal can open on a click (OSC 8).

    The visible text is the URL itself, so a terminal that does not know the
    sequence, or picks out URLs on its own, still has it to work with.  The
    classic Windows console is left out: only Windows Terminal is known to
    take it.
    """
    if not colour_ok() or os.environ.get("TERM") == "dumb" or (IS_WINDOWS and not os.environ.get("WT_SESSION")):
        return url
    return f"\x1b]8;;{url}\x1b\\{url}\x1b]8;;\x1b\\"


def show_link(url: str, indent: str = "    ") -> None:
    """A link on a line of its own: clickable on screen, plain in the log."""
    with _lock:
        _write_log(indent + url)
        _print(indent + hyperlink(url))


def styled(text: str, *codes: str) -> str:
    return "".join(codes) + text + RESET if codes and colour_ok() else text


# ------------------------------------------------------------------ messages


def detail(text: str = "") -> None:
    """Logged always; on screen only in verbose mode, or outside a run."""
    with _lock:
        _write_log(text)
        if not _s.concise:
            _print(text)


def show(text: str = "", *codes: str) -> None:
    """Logged and always on screen: things to read, do or answer."""
    with _lock:
        _write_log(text)
        _print(styled(text, *codes))


def log_only(text: str) -> None:
    with _lock:
        _write_log(text)


def before_input(prompt: str = "") -> None:
    """Called just before reading the keyboard: the question must start on its own line."""
    with _lock:
        _break()
        _s.paused = True
        _s.pending_prompt, _s.pending_answer = prompt, ""
        if prompt:
            _write_log(f"? {prompt.strip()}")


def after_input() -> None:
    """The keyboard has been read: the stage line may be drawn again."""
    with _lock:
        if _s.transient_rows is not None and _s.pending_prompt is not None:
            # The question and the answer echoed after it, then Enter.
            _s.transient_rows += _rows(_s.pending_prompt + _s.pending_answer)
        _s.pending_prompt, _s.pending_answer = None, ""
        _s.paused = False


def after_secret(shown: str) -> None:
    """after_input() for a password: `shown` is what the screen holds after
    the prompt - asterisks only, never the password."""
    _s.pending_answer = shown
    after_input()


def answered(answer: str) -> None:
    """Record a non-secret answer in the log."""
    _s.pending_answer = answer
    after_input()
    log_only(f"  -> {answer}")


# ------------------------------------------------------- answered questions


def transient_begin() -> None:
    """Start a block - a note and its question - to take off the screen once
    answered, so a finished stage leaves just its line.  Only on a terminal
    that can move the cursor back up; the log keeps everything regardless."""
    with _lock:
        if not (_s.concise and sys.stdout.isatty() and colour_ok()):
            return
        _s.transient_rows = 0
        _break()  # the stage line, if drawn, is taken away and drawn again too


def transient_end() -> None:
    """Take the block since transient_begin() off the screen."""
    with _lock:
        rows, _s.transient_rows = _s.transient_rows, None
        if rows is None:
            return
        if _s.live:
            sys.stdout.write("\r\x1b[K")  # a stage line redrawn after the answer
            _s.live = 0
        # Up a row and erase it, row by row.  Not one erase-to-end from the top:
        # when the block starts on the screen's first row that clears the whole
        # screen, which tmux (scroll-on-clear) and some terminals first copy
        # into the scrollback - the block would stay there.
        sys.stdout.write("\x1b[1F\x1b[2K" * rows)
        sys.stdout.flush()
        if _s.stage is not None and not _s.paused:
            _draw_stage()


# ------------------------------------------------------------------- stages


def set_stage_width(width: int) -> None:
    """The longest stage name in the run, so every stage line's status lines up."""
    _s.name_width = width


def stage_begin(number: int, total: int, name: str) -> None:
    with _lock:
        label = f"Stage {number} of {total} - {name}"
        if _s.stage == label:
            return  # already begun: keep its line and its clock
        _break()
        digits = len(str(total))
        _s.stage = label
        _s.stage_prefix = f"Stage {number:>{digits}} of {total}"
        _s.stage_screen = f"{_s.stage_prefix} - {name:<{_s.name_width}}"
        _s.stage_started = time.monotonic()
        _write_log(f"--- {_s.stage}")
        if _s.concise:
            if sys.stdout.isatty():
                _draw_stage()
            else:
                _print(f"{_s.stage_screen} - Underway")
        else:
            _print("")
            _print(f"== {_s.stage}")


def stage_end(outcome: str) -> None:
    """outcome: "complete", "stopped" or "already done"."""
    with _lock:
        if _s.stage is None:
            return
        elapsed = _elapsed(time.monotonic() - _s.stage_started)
        timing = f" ({elapsed})" if outcome == "complete" else ""
        line = f"{_s.stage} - {outcome}{timing}"
        _write_log(line)
        if _s.concise:
            colour = {"complete": GREEN, "stopped": RED}.get(outcome)
            word = outcome.capitalize()
            plain = f"{_s.stage_screen} - {word}{timing}"
            text = plain if not colour else (
                styled(_s.stage_prefix, colour) + _s.stage_screen[len(_s.stage_prefix):]
                + " - " + styled(word, colour) + timing)
            if _s.live and _s.live_is_stage:
                # Replace the "underway" line in place.
                sys.stdout.write("\r" + text + " " * max(0, _s.live - len(plain)) + "\n")
                sys.stdout.flush()
                _s.live = 0
            else:
                _print(text)
        _s.stage = None


def progress(text: str = "") -> None:
    """What a stage is doing right now, redrawn in place; never logged.

    Verbose mode only: in concise mode the stage line and its clock already
    say the stage is under way, and anything to act on is shown in full.
    """
    with _lock:
        if _s.concise:
            return
        if not sys.stdout.isatty():
            return
        if _s.live and _s.live_is_stage:
            _break()
        line = f"    {text}"
        sys.stdout.write("\r" + line + " " * max(0, _s.live - len(line)))
        sys.stdout.flush()
        _s.live, _s.live_is_stage = len(line), False


def progress_clear() -> None:
    """The waiting is over: take the progress text away."""
    with _lock:
        if _s.concise:
            return
        if _s.live and not _s.live_is_stage:
            sys.stdout.write("\r" + " " * _s.live + "\r")
            sys.stdout.flush()
            _s.live = 0


def progress_keep() -> None:
    """Leave the last progress line on screen (verbose) and move on."""
    with _lock:
        if not _s.concise and _s.live and not _s.live_is_stage:
            sys.stdout.write("\n")
            sys.stdout.flush()
            _s.live = 0


def tick() -> None:
    """Redraw the stage line so its clock moves during a long wait."""
    with _lock:
        if _s.concise and _s.stage is not None and not _s.paused and sys.stdout.isatty():
            _draw_stage()


def raw_progress(text: str) -> None:
    """For writers that draw their own progress with \\r and finish with \\n."""
    if text == "\n":
        progress_keep()
    elif text.startswith("\r"):
        progress(text.strip())




# ------------------------------------------------------------- subprocesses


def stream(process: subprocess.Popen) -> int:
    """Copy a child's output to the log (and the screen when verbose) until it exits."""
    assert process.stdout is not None
    source = process.stdout
    _s.tool_tail.clear()

    def pump() -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        while True:
            chunk = os.read(source.fileno(), 4096)
            if not chunk:
                break
            text = decoder.decode(chunk)
            with _lock:
                if not _s.concise:
                    if _s.live:
                        _break()
                    sys.stdout.write(text)
                    sys.stdout.flush()
                pending += text
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    # A child redrawing a progress line: keep only what it ended on.
                    _keep(line.split("\r")[-1])
        if pending.strip():
            with _lock:
                _keep(pending.split("\r")[-1])

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        while process.poll() is None:
            tick()
            time.sleep(1.0)
    finally:
        reader.join(5.0)
    return process.wait()


def tool_problems(limit: int = 3) -> list[str]:
    """The last helper tool's own words on what went wrong: its lines that read
    like an error, or failing that its last few lines."""
    lines = [line for line in _s.tool_tail if line.strip()]
    problems = [line for line in lines if PROBLEM_RE.search(line)]
    return [line.strip() for line in (problems or lines)[-limit:]]


def _keep(line: str) -> None:
    _write_log("  | " + line)
    _s.tool_tail.append(line)


# ------------------------------------------------------------------ helpers


def elapsed(seconds: float) -> str:
    """48s, or 2m 05s."""
    return _elapsed(seconds)


def _elapsed(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def _tick_forever() -> None:
    while not _s.stop_ticker.wait(1.0):
        with _lock:
            if _s.concise and _s.stage is not None and not _s.paused:
                _draw_stage()


def _draw_stage() -> None:
    clock = _elapsed(time.monotonic() - _s.stage_started)
    width = shutil.get_terminal_size((100, 20)).columns - 1
    line = f"{_s.stage_screen} - Underway {clock}"
    shown = f"{_s.stage_screen} - {styled('Underway', BOLD)} {clock}"
    if len(line) > width:
        line = shown = line[: width - 3] + "..."
    if _s.live and not _s.live_is_stage:
        _break()
    sys.stdout.write("\r" + shown + " " * max(0, _s.live - len(line)))
    sys.stdout.flush()
    _s.live, _s.live_is_stage = len(line), True


def _rows(text: str) -> int:
    """Screen rows `text` takes once printed, long lines wrapping."""
    width = max(1, shutil.get_terminal_size((100, 20)).columns)
    plain = ANSI_RE.sub("", text)
    return sum(max(1, -(-len(line) // width)) for line in plain.split("\n"))


def _break() -> None:
    """Finish an unfinished line so the next output starts on a fresh one."""
    if _s.live:
        if _s.live_is_stage:
            sys.stdout.write("\n")            # the underway line stays as it was
            if _s.transient_rows is not None:
                _s.transient_rows += 1
        else:
            sys.stdout.write("\r" + " " * _s.live + "\r")
        sys.stdout.flush()
        _s.live = 0


def _print(text: str) -> None:
    _break()
    print(text, flush=True)
    if _s.transient_rows is not None:
        _s.transient_rows += _rows(text)


def _write_log(text: str) -> None:
    if _s.log is None:
        return
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    for line in str(text).split("\n"):
        _s.log.write(f"[{stamp}] {line}\n")
    _s.log.flush()
