#!/usr/bin/env python3
"""Run a whole conversion unattended, and keep the screen it drew for review.

    python3 tools/repo/unattended_run.py [--size 100x40] [--save DIR] [-- run options...]

The conversion (autoflash/cli.py run --unattended) runs on a terminal of its
own (a pseudo-terminal), so it draws exactly what a person would see - the
colours, the stage lines redrawn in place, the notes taken away once read.
Everything it writes is recorded, and afterwards the screen as it was left
is rebuilt from the recording and printed, colours and all.

The eWeLink account comes from .env (see .env.example), as does this
computer's sudo password where sudo would ask for one.  Anything after `--`
goes to the run itself: --plug-ip, --new, --verbose and so on.

Saved, under captures/unattended/<time>/ unless --save says otherwise:
    screen.ansi   the final screen, with its colours: `cat` or `less -R` it
    screen.txt    the same without colours, to read or compare anywhere
    screen.raw    every byte written, as written: `cat` replays it in a terminal

Linux and macOS only (Windows has no pseudo-terminal of this kind); there,
start.cmd run --unattended runs a conversion just as unattended, unrecorded.
"""

from __future__ import annotations

import argparse
import codecs
import contextlib
import os
import re
import select
import shutil
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "autoflash" / "cli.py"

# A control sequence (CSI), an operating-system command (OSC: links, titles),
# any other escape, a control character, or a run of ordinary text.
TOKEN = re.compile(
    r"\x1b\[(?P<params>[0-9;?]*)(?P<final>[@-~])"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[@-_]"
    r"|(?P<control>[\x00-\x1f\x7f])"
    r"|(?P<text>[^\x00-\x1f\x7f\x1b]+)"
)


class Screen:
    """Just enough of a terminal to rebuild what one shows from what was
    written to it: the sequences the conversion's output uses - colours,
    carriage returns, erasing a line, moving up a row - with lines wrapping
    at the width and the scrollback kept.  As on a real terminal, the cursor
    cannot go up past the top of the window into the scrollback."""

    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self.rows: list[list[tuple[str, str]]] = [[]]
        self.row = self.col = 0
        self.style = ""        # the colour sequences in force, as written
        self.wrap_next = False  # at the last column: the next character wraps

    def feed(self, text: str) -> None:
        for match in TOKEN.finditer(text):
            if match.group("text") is not None:
                for char in match.group("text"):
                    self._put(char)
            elif match.group("control") is not None:
                self._control(match.group("control"))
            elif match.group("final") is not None:
                self._csi(match.group("params"), match.group("final"))
            # Anything else - links, titles - leaves the text unchanged.

    def _top(self) -> int:
        return max(0, len(self.rows) - self.height)

    def _line(self) -> list[tuple[str, str]]:
        while len(self.rows) <= self.row:
            self.rows.append([])
        return self.rows[self.row]

    def _put(self, char: str) -> None:
        if self.wrap_next:
            self.row, self.col, self.wrap_next = self.row + 1, 0, False
        line = self._line()
        while len(line) < self.col:
            line.append((" ", ""))
        cell = (char, self.style)
        if self.col < len(line):
            line[self.col] = cell
        else:
            line.append(cell)
        if self.col == self.width - 1:
            self.wrap_next = True
        else:
            self.col += 1

    def _control(self, char: str) -> None:
        if char == "\n":
            self.row += 1
            self._line()
        elif char == "\r":
            self.col = 0
        elif char == "\b":
            self.col = max(0, self.col - 1)
        elif char == "\t":
            self.col = min(self.width - 1, (self.col // 8 + 1) * 8)
        else:
            return  # a bell, say: nothing to see
        self.wrap_next = False

    def _csi(self, params: str, final: str) -> None:
        numbers = [int(part) if part.isdigit() else 0 for part in params.lstrip("?").split(";")]
        count = max(1, numbers[0])
        self.wrap_next = False
        if final == "m":
            # Kept as written; a reset (or nothing) clears what came before.
            self.style = "" if params in ("", "0") else self.style + f"\x1b[{params}m"
        elif final in "AF":  # up; F also to the start of the row
            self.row = max(self._top(), self.row - count)
            if final == "F":
                self.col = 0
        elif final in "BE":  # down; E also to the start of the row
            self.row += count
            self._line()
            if final == "E":
                self.col = 0
        elif final == "C":
            self.col = min(self.width - 1, self.col + count)
        elif final == "D":
            self.col = max(0, self.col - count)
        elif final == "G":
            self.col = min(self.width - 1, count - 1)
        elif final == "K":
            line = self._line()
            if numbers[0] == 0:
                del line[self.col:]
            elif numbers[0] == 1:
                line[:self.col + 1] = [(" ", "")] * min(len(line), self.col + 1)
            else:
                line.clear()
        elif final == "J":
            if numbers[0] == 0:
                del self._line()[self.col:]
                del self.rows[self.row + 1:]
            elif numbers[0] in (2, 3):
                for line in self.rows[self._top():]:
                    line.clear()
        # Anything else - hiding the cursor, say - changes no text.

    def render(self, colour: bool = True) -> str:
        """Every row, scrollback and all, without the blank rows at the end."""
        rows = []
        for line in self.rows:
            while line and line[-1] == (" ", ""):
                line = line[:-1]
            text, style = "", ""
            for char, cell_style in line:
                if colour and cell_style != style:
                    text += "\x1b[0m" + cell_style
                    style = cell_style
                text += char
            if colour and style:
                text += "\x1b[0m"
            rows.append(text.rstrip() if not colour else text)
        while rows and not rows[-1].strip():
            rows.pop()
        return "\n".join(rows) + "\n"


def parse_size(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)x(\d+)", text)
    if not match or int(match.group(1)) < 40 or int(match.group(2)) < 20:
        raise argparse.ArgumentTypeError("COLUMNSxROWS, at least 40x20 - 100x40, say")
    return int(match.group(1)), int(match.group(2))


def record(command: list[str], size: tuple[int, int], live: bool,
           timeout: float | None) -> tuple[int, bytes]:
    """Run `command` on a pseudo-terminal of `size`; its exit code and every byte it wrote."""
    import fcntl
    import pty
    import struct
    import termios

    environment = {**os.environ, "COLUMNS": str(size[0]), "LINES": str(size[1])}
    environment.pop("NO_COLOR", None)
    if environment.get("TERM", "dumb") in ("", "dumb"):
        environment["TERM"] = "xterm-256color"
    pid, fd = pty.fork()
    if pid == 0:  # the child: the conversion, on the new terminal, sized before it looks
        try:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", size[1], size[0], 0, 0))
            os.execve(command[0], command, environment)
        finally:
            os._exit(127)
    started, written, stopping = time.monotonic(), bytearray(), False
    while True:
        try:
            if timeout and not stopping and time.monotonic() - started > timeout:
                stopping = True
                os.kill(pid, signal.SIGTERM)  # the tool tidies up on SIGTERM too
            if not select.select([fd], [], [], 0.5)[0]:
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:  # EIO: the conversion has exited and its terminal closed
                break
            if not chunk:
                break
            written += chunk
            if live:
                os.write(sys.stdout.fileno(), chunk)
        except KeyboardInterrupt:
            os.kill(pid, signal.SIGINT)  # passed on: the tool stops cleanly, as at a keyboard
    os.close(fd)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), bytes(written)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--size", type=parse_size,
                        help="the terminal the run gets, COLUMNSxROWS (default: this one's, "
                        "or 100x40 when there is none)")
    parser.add_argument("--save", type=Path, help="where to keep the recording "
                        "(default: captures/unattended/<time>/)")
    parser.add_argument("--timeout", type=float, metavar="MINUTES",
                        help="stop the run (as Ctrl-C would) if it is still going after this long")
    parser.add_argument("run_options", nargs=argparse.REMAINDER,
                        help="after --: options for the run itself, e.g. -- --plug-ip 192.168.1.50")
    args = parser.parse_args()
    if not hasattr(os, "fork"):
        print("This needs Linux or macOS. On Windows, start.cmd run --unattended "
              "runs a conversion unattended, without the recording.")
        return 2

    extra = args.run_options[1:] if args.run_options[:1] == ["--"] else args.run_options
    live = sys.stdout.isatty()
    # Python 3.10 gives 0 for a side a terminal does not report; 3.11 the fallback.
    size = args.size or (tuple(n or d for n, d in zip(shutil.get_terminal_size(), (80, 24)))
                         if live else (100, 40))
    save = args.save or ROOT / "captures" / "unattended" / time.strftime("%Y%m%d-%H%M%S")
    command = [sys.executable, str(TOOL), "run", "--unattended", *extra]

    code, written = record(command, size, live, args.timeout * 60 if args.timeout else None)

    screen = Screen(*size)
    screen.feed(codecs.decode(written, "utf-8", errors="replace"))
    save.mkdir(parents=True, exist_ok=True)
    (save / "screen.raw").write_bytes(written)
    (save / "screen.ansi").write_text(screen.render(), encoding="utf-8")
    (save / "screen.txt").write_text(screen.render(colour=False), encoding="utf-8")

    shown = save.resolve()
    with contextlib.suppress(ValueError):
        shown = shown.relative_to(Path.cwd())
    if live:
        # The screen above is the run's own; the rule marks where it ends.
        print("-" * min(size[0], 72))
    else:
        rule = "=" * 72
        print(f"{rule}\nThe screen at the end of the run ({size[0]}x{size[1]}):\n{rule}")
        sys.stdout.write(screen.render(colour=os.environ.get("NO_COLOR") is None))
        print(rule)
    outcome = {0: "finished", 130: "stopped at a question, or by Ctrl-C",
               143: "stopped by --timeout"}.get(code, "failed")
    print(f"Exit code {code} - {outcome}")
    print(f"Kept in {shown}/: screen.ansi (cat it), screen.txt, screen.raw")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
