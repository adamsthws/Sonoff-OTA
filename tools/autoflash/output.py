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
import contextlib
import datetime
import ipaddress
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, Callable

from . import __version__

IS_WINDOWS = platform.system() == "Windows"

GREEN, RED, YELLOW, BOLD, RESET = "\x1b[32m", "\x1b[31m", "\x1b[33m", "\x1b[1m", "\x1b[0m"
# Every row on screen starts this far in, clear of the terminal's left edge.
MARGIN = " "
PURPLE = "\x1b[38;5;135m"  # #af5fff: link titles, opposite the green on the colour wheel
MAGENTA = "\x1b[35m"        # the same where only the basic colours are: rounded, 135 comes out blue

_lock = threading.RLock()

# Colour and hyperlink sequences, which take no room on screen.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\]8;;[^\x1b]*\x1b\\")

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
    waiting_since: float | None = None  # when the keyboard began to be waited for
    stage_waited = 0.0                # seconds of it in this stage, left out of its clock
    run_waited = 0.0                  # and in the whole run, left out of its time
    live = 0                          # characters of an unfinished line on screen
    live_is_stage = False
    stream_fresh = True               # a helper's output is at the start of a row
    after_stage = False               # a stage line was the last on screen: a gap before what follows
    paused = False                    # waiting for the keyboard: draw nothing
    held = False                      # between questions that belong together: no clock
    ticker: threading.Thread | None = None
    tool_tail: collections.deque = collections.deque(maxlen=40)  # the last helper's last lines
    stop_ticker = threading.Event()


_s = _State()


# ------------------------------------------------------------------ privacy
# The log is meant to be shared when asking for help, so what identifies the
# person, their network or their plug is replaced before it is written: the
# values the run learns (private()), and anything shaped like one of them
# (_PATTERNS) - a helper's output can carry them before the run knows them.
# The screen is left alone: it is theirs.  Addresses on the home network stay,
# as they are needed to follow a run and identify nobody.

_private: dict[str, str] = {}         # value -> the label it is replaced with

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Secrets in JSON, whatever else is in the line.
    (re.compile(r'("(?:apikey|devicekey|at|rt|token|password|pass|pwd)"\s*:\s*)"[^"]*"', re.I),
     r'\1"<redacted>"'),
    (re.compile(r"\bsign=[0-9a-fA-F]+"), "sign=<signature>"),
    (re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+"), "<email>"),
    (re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b"), "<mac>"),
    # An eWeLink device id, and Tasmota's default name, which ends with the MAC's.
    (re.compile(r"\b100[0-9a-f]{7}\b"), "<device-id>"),
    (re.compile(r"\btasmota-[0-9A-Fa-f]{6}-\d{4}\b"), "<device-name>"),
    (re.compile(r"(\[sudo\] password for )\S+?(:)"), r"\1<user>\2"),
]
_IPV4 = re.compile(r"(?<![\w.-])\d{1,3}(?:\.\d{1,3}){3}(?![\w.(-])")


def private(value: object, label: str) -> None:
    """Keep `value` out of the log from now on, written as <label> instead."""
    text = str(value or "").strip()
    if len(text) < 3:  # too short to replace without mangling ordinary words
        return
    with _lock:
        _private[text] = label


def scrub(text: str) -> str:
    """`text` as the log may hold it: see private()."""
    for value in sorted(_private, key=len, reverse=True):
        text = text.replace(value, f"<{_private[value]}>")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    text = _IPV4.sub(_public_ip, text)
    home = str(Path.home())
    if len(home) > 1:
        text = text.replace(home, "~")
    return text


def _public_ip(match: re.Match[str]) -> str:
    try:
        address = ipaddress.ip_address(match.group(0))
    except ValueError:
        return match.group(0)
    return "<public-ip>" if address.is_global else match.group(0)


# --------------------------------------------------------------------- setup


def start(log_path: Path | None, *, verbose: bool) -> None:
    """Begin a run: open its log, and condense the terminal unless verbose."""
    with _lock:
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            _s.log = log_path.open("a", encoding="utf-8")
            _s.log_path = log_path
            _write_log(f"=== run started, tool version {__version__}{' (verbose)' if verbose else ''} ===")
        _s.concise = not verbose
        _s.paused = False
        _s.run_waited = 0.0
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
        _s.after_stage = False


def move_log(move: Callable[[], Path]) -> None:
    """Close the log while `move` moves its directory, then carry on in the
    log at the path it returns - Windows will not move a directory with a
    file open in it."""
    with _lock:
        if _s.log is not None:
            _s.log.close()
            _s.log = None
        path = move()
        if _s.log_path is not None:
            _s.log = path.open("a", encoding="utf-8")
            _s.log_path = path


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


def links_ok() -> bool:
    """Whether the terminal takes OSC 8 links.

    The classic Windows console, macOS Terminal and the Linux console are
    left out: they show or drop the sequence rather than make a link.
    """
    if not colour_ok() or os.environ.get("TERM") in ("dumb", "linux"):
        return False
    if os.environ.get("TERM_PROGRAM") == "Apple_Terminal":
        return False
    return not IS_WINDOWS or bool(os.environ.get("WT_SESSION"))


def hyperlink(url: str, title: str | None = None) -> str:
    """The URL as a link the terminal can open on a click (OSC 8).

    The URL itself is always shown, after the title if there is one, so a
    terminal that does not take the sequence still has it to pick out or
    copy.  Only the URL is the link: the title stays outside it, coloured
    wherever colour is, as some terminals repaint link text in their own
    colour.
    """
    link = f"\x1b]8;;{url}\x1b\\{url}\x1b]8;;\x1b\\" if links_ok() else url
    return f"{styled(title + ':', BOLD, _purple())} {link}" if title else link


def _purple() -> str:
    """The link titles' purple, or the terminal's magenta where it has only
    the basic colours (tmux or screen named plainly, the Linux console)."""
    if IS_WINDOWS or os.environ.get("COLORTERM") in ("truecolor", "24bit"):
        return PURPLE
    return PURPLE if "256" in os.environ.get("TERM", "") else MAGENTA


def click_hint() -> str:
    """How to open a link here, or nothing where clicking is not known to work."""
    if not links_ok():
        return ""
    return "Cmd+click to open" if platform.system() == "Darwin" else "Ctrl+click to open"


def show_link(url: str, title: str | None = None, indent: str = "    ") -> None:
    """A link on a line of its own: clickable on screen, plain in the log."""
    show_links([(url, title)], indent)


def show_links(links: list[tuple[str, str | None]], indent: str = "    ", *, hint: bool = False) -> None:
    """Links one to a line, and with hint=True how to open them underneath."""
    with _lock:
        for url, title in links:
            _write_log(indent + (f"{title}: {url}" if title else url))
            _print(indent + hyperlink(url, title))
        how = click_hint() if hint else ""
        if how:
            _print(indent + styled(f"({how})", "\x1b[2m"))


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
        _gap("?")
        _s.paused = True
        if _s.waiting_since is None:
            _s.waiting_since = time.monotonic()
        _s.pending_prompt, _s.pending_answer = prompt, ""
        if prompt:
            _write_log(f"? {prompt.strip()}")


def question(text: str, tail: str = "") -> str:
    """before_input(), with the question laid out for the screen: every row
    but its last written here, the last returned for input() to show.
    `tail` - ": ", say - follows the question on screen but not in the log."""
    with _lock:
        before_input(text)
        body = text + tail
        rows = _fit(body.rstrip(" ")).split("\n")
        rows[-1] += body[len(body.rstrip(" ")):]
        for row in rows[:-1]:
            sys.stdout.write(row + "\n")
        sys.stdout.flush()
        _s.pending_prompt = "\n".join(rows)
        return rows[-1]


def after_input() -> None:
    """The keyboard has been read: the stage line may be drawn again."""
    with _lock:
        if _s.transient_rows is not None and _s.pending_prompt is not None:
            # The question and the answer echoed after it, then Enter.
            _s.transient_rows += _rows(_s.pending_prompt + _s.pending_answer)
        _s.pending_prompt, _s.pending_answer = None, ""
        _s.paused = False
        if _s.waiting_since is not None:
            waited = time.monotonic() - _s.waiting_since
            _s.stage_waited += waited
            _s.run_waited += waited
            _s.waiting_since = None


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


def transient_begin(*, before_start: bool = False) -> None:
    """Start a block - a note and its question - to take off the screen once
    answered, so a finished stage leaves just its line.  Only on a terminal
    that can move the cursor back up; the log keeps everything regardless.

    `before_start`: a question asked before start(), which has no log yet and
    does not know whether the run will be verbose.
    """
    with _lock:
        if not ((_s.concise or before_start) and sys.stdout.isatty() and colour_ok()):
            return
        _s.transient_rows = 0
        _break()  # the stage line, if drawn, is taken away and drawn again too


def transient_end() -> None:
    """Take the block since transient_begin() off the screen."""
    with _lock:
        rows, _s.transient_rows = _s.transient_rows, None
        if rows is None:
            return
        if rows >= shutil.get_terminal_size((100, 24)).lines:
            # Taller than the window (VS Code's terminal panel, say): the cursor
            # cannot go up past the window's top, so erasing would leave the
            # block's start and the stage line above it stranded. Keep it whole.
            if _s.stage is not None and not _s.paused:
                _draw_stage()
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


# Rows a window needs for the run's questions to be taken off the screen once
# answered: a block taller than the window has to stay (transient_end).
MIN_ROWS = 20


def short_window(*, before_start: bool = False) -> int | None:
    """The window's height in rows, when answered questions are being taken
    off the screen but the window is too short for that to always work.
    `before_start`: as for transient_begin()."""
    if not ((_s.concise or before_start) and sys.stdout.isatty() and colour_ok()):
        return None
    rows = shutil.get_terminal_size((100, 24)).lines
    return rows if rows < MIN_ROWS else None


def transient_mark() -> tuple[int, bool] | None:
    """Where the open block has got to, for transient_rewind() to go back to.
    None when nothing is being taken off the screen."""
    with _lock:
        if _s.transient_rows is None:
            return None
        _break()
        return _s.transient_rows, _s.after_stage


def transient_rewind(mark: tuple[int, bool] | None) -> bool:
    """Take the rows since transient_mark() off the screen and keep the block
    open, so a question asked again does not pile up under its earlier tries
    - a block taller than the window could no longer be taken away at all.
    True when they were taken off; the log keeps them regardless."""
    with _lock:
        if mark is None or _s.transient_rows is None:
            return False
        rows = _s.transient_rows - mark[0]
        if rows >= shutil.get_terminal_size((100, 24)).lines:
            return False  # the cursor cannot reach their first row
        if _s.live:
            sys.stdout.write("\r\x1b[K")  # a stage line drawn since
            _s.live = 0
        sys.stdout.write("\x1b[1F\x1b[2K" * rows)
        sys.stdout.flush()
        _s.transient_rows, _s.after_stage = mark
        return True


# ------------------------------------------------------------------- stages


@contextlib.contextmanager
def hold():
    """No stage clock drawn meanwhile: a question, the wait while its answer is
    checked, and the question again.  The clock would land between them, on a
    row the block does not count."""
    with _lock:
        before, _s.held = _s.held, True
    try:
        yield
    finally:
        with _lock:
            _s.held = before


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
        _s.stage_waited = 0.0
        _write_log(f"--- {_s.stage}")
        if _s.concise:
            if sys.stdout.isatty():
                _draw_stage()
            else:
                _print(f"{_s.stage_screen} - Underway", stage=True)
        else:
            _print("")
            _print(f"== {_s.stage}", stage=True)


def stage_end(outcome: str) -> None:
    """outcome: "complete", "stopped", "already done" or "skipped" (a stage
    the run does not need, passed over)."""
    with _lock:
        if _s.stage is None:
            return
        elapsed = _elapsed(_stage_seconds())
        timing = f" ({elapsed})" if outcome == "complete" else ""
        line = f"{_s.stage} - {outcome}{timing}"
        _write_log(line)
        if _s.concise:
            colour = {"complete": GREEN, "already done": GREEN, "skipped": GREEN,
                      "stopped": RED}.get(outcome)
            word = outcome.capitalize()
            plain = f"{_s.stage_screen} - {word}{timing}"
            text = plain if not colour else (
                styled(_s.stage_prefix, colour) + _s.stage_screen[len(_s.stage_prefix):]
                + " - " + styled(word, colour) + timing)
            if _s.live and _s.live_is_stage:
                # Replace the "underway" line in place.
                first, _, rest = _fit(text).partition("\n")
                pad = " " * max(0, _s.live - len(ANSI_RE.sub("", first)))
                sys.stdout.write("\r" + first + pad + ("\n" + rest if rest else "") + "\n")
                sys.stdout.flush()
                _s.live = 0
                _s.after_stage = True
            else:
                _print(text, stage=True)
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
        _gap(text)
        line = _clip(f"{MARGIN}    {text}")
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
        if _s.concise and _s.stage is not None and not _s.paused and not _s.held and sys.stdout.isatty():
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
                    _gap(text)
                    sys.stdout.write(_margin_stream(text))
                    sys.stdout.flush()
                pending += text
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    # A child redrawing a progress line: keep only what it ended on.
                    _keep(_last_redraw(line))
        if pending.strip():
            with _lock:
                _keep(_last_redraw(pending))

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        while process.poll() is None:
            tick()
            time.sleep(1.0)
    except BaseException:
        # The run stopping (Ctrl-C twice, the window closed): the child, in a
        # process group of its own, is not told by the terminal, so is told here.
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        raise
    finally:
        reader.join(5.0)
    return process.wait()


def tool_said(text: str) -> bool:
    """Whether the last helper tool's output (its kept tail) mentions `text`."""
    return any(text in line for line in _s.tool_tail)


def tool_problems(limit: int = 3) -> list[str]:
    """The last helper tool's own words on what went wrong: its lines that read
    like an error, or failing that its last few lines."""
    lines = [line for line in _s.tool_tail if line.strip()]
    problems = [line for line in lines if PROBLEM_RE.search(line)]
    return [line.strip() for line in (problems or lines)[-limit:]]


def _last_redraw(line: str) -> str:
    """What a line redrawn with "\\r" ended on.  Its own line ending comes off
    first: "\\r\\n" on Windows, which would otherwise leave nothing."""
    if line.endswith("\r"):
        line = line[:-1]
    return line.split("\r")[-1]


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


def _waiting_now() -> float:
    """Seconds the keyboard has been waited for, if it is being waited for now."""
    return time.monotonic() - _s.waiting_since if _s.waiting_since is not None else 0.0


def _stage_seconds() -> float:
    """How long the stage has been working: its time less the time spent
    waiting for answers, so a stage's clock is the tool's time, not the
    person's."""
    return time.monotonic() - _s.stage_started - _s.stage_waited - _waiting_now()


def waited() -> float:
    """Seconds this run has spent waiting for answers, to leave out of its time."""
    with _lock:
        return _s.run_waited + _waiting_now()


def _tick_forever() -> None:
    while not _s.stop_ticker.wait(1.0):
        _tick()


def _tick() -> None:
    """The stage clock, a second on.  Not while a block is open to be taken
    away again: drawn under its rows - while what it shows is still being
    worked out, say (Windows' security software takes PowerShell a few
    seconds) - the clock's line would be a second stage line in the block."""
    with _lock:
        if (_s.concise and _s.stage is not None and not _s.paused and not _s.held
                and _s.transient_rows is None):
            _draw_stage()


def _draw_stage() -> None:
    clock = _elapsed(_stage_seconds())
    line = f"{MARGIN}{_s.stage_screen} - Underway {clock}"
    shown = f"{MARGIN}{_s.stage_screen} - {styled('Underway', BOLD)} {clock}"
    if _clip(line) != line:
        line = shown = _clip(line)
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
            _s.after_stage = True
        else:
            sys.stdout.write("\r" + " " * _s.live + "\r")
        sys.stdout.flush()
        _s.live = 0


def _print(text: str, *, stage: bool = False) -> None:
    _break()
    if stage:
        _s.after_stage = False  # stage lines follow one another with no gap
    else:
        _gap(text)
    text = _fit(text)
    print(text, flush=True)
    if _s.transient_rows is not None:
        _s.transient_rows += _rows(text)
    if stage:
        _s.after_stage = True


def _screen_width() -> int | None:
    """Columns a row may fill after the margin, or None when not a terminal.
    One short of the edge: some consoles start a new row on reaching it."""
    if not sys.stdout.isatty():
        return None
    return max(20, shutil.get_terminal_size((100, 20)).columns - 1 - len(MARGIN))


def _clip(line: str) -> str:
    """A row redrawn in place, cut to fit: wrapped, it could not be redrawn."""
    width = _screen_width()
    if width is None or len(line) <= width + len(MARGIN):
        return line
    return line[: width + len(MARGIN) - 3] + "..."


def _fit(text: str) -> str:
    """Text for the screen: each row after the margin, long ones wrapped to
    the terminal's width."""
    width = _screen_width()
    rows = []
    for line in text.split("\n"):
        for row in (_wrap(line, width) if width else [line]):
            rows.append(MARGIN + row if ANSI_RE.sub("", row).strip() else row)
    return "\n".join(rows)


def _wrap(line: str, width: int) -> list[str]:
    """One line as rows of at most `width` visible characters, broken between
    words.  A row carrying on from the one above starts under its text - past
    a leading indent, or past a "Label:" set in from the edge - so lists and
    lined-up labels stay lined up.  A word longer than a row (a long URL) is
    never broken: it starts a row, at the edge if need be, and only past the
    terminal's width does the terminal itself wrap it."""
    plain = ANSI_RE.sub("", line)
    if len(plain) <= width:
        return [line]
    lead = len(line) - len(line.lstrip(" "))
    label = re.match(r"\s+[^\s:][^:]{0,30}:\s+(?=\S)", plain)
    hang = len(label.group(0)) if label else lead
    if hang > width // 2:
        hang = lead if lead <= width // 2 else 0
    rows: list[str] = []
    row, used, started = " " * lead, lead, False
    for word in line[lead:].split(" "):
        size = len(ANSI_RE.sub("", word))
        if started and size and used + 1 + size > width:
            rows.append(row.rstrip(" "))
            indent = hang if hang + size <= width else 0  # a long URL: as much of it on one row as can be
            row, used = " " * indent + word, indent + size
        elif started:
            row, used = row + " " + word, used + 1 + size
        else:
            row, used, started = row + word, used + size, True
    rows.append(row)
    return rows


def _margin_stream(text: str) -> str:
    """A helper's raw output, each row started at the margin."""
    out = []
    for char in text:
        if _s.stream_fresh and char not in "\r\n":
            out.append(MARGIN)
            _s.stream_fresh = False
        out.append(char)
        if char in "\r\n":
            _s.stream_fresh = True
    return "".join(out)


def _gap(text: str) -> None:
    """One blank line between a stage's line and whatever is shown under it -
    a message, a question, a helper's output - unless that starts with one."""
    if not _s.after_stage:
        return
    _s.after_stage = False
    if ANSI_RE.sub("", text).strip("\r\n\t "):
        print("", flush=True)
        if _s.transient_rows is not None:
            _s.transient_rows += 1


def _write_log(text: str) -> None:
    if _s.log is None:
        return
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    for line in scrub(str(text)).split("\n"):
        _s.log.write(f"[{stamp}] {line}\n")
    _s.log.flush()
