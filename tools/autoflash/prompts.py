"""Prompts written for someone doing this for the first time.

Rules this module enforces so individual steps do not have to:
  - every question explains what it wants and where to find it;
  - a wrong answer is re-asked, never fatal;
  - answers are validated before the run continues;
  - "quit" always works, at every prompt, and stops cleanly.
"""

from __future__ import annotations

import codecs
import getpass
import ipaddress
import os
import select
import shutil
import socket
import sys
import time
from typing import Callable, Iterable, Sequence

from . import hostsys


class QuitRun(SystemExit):
    """The operator chose to quit at a prompt.

    A SystemExit, so a standalone tool that does not catch it simply stops
    with a short message rather than a traceback; the main tool catches it and
    says how to resume.
    """

    def __init__(self, reason: str = "") -> None:
        super().__init__("Stopped.")
        self.reason = reason


BULLET = "    "
QUIT_WORDS = {"q", "quit", "exit", "abort"}
QUIT_HINT = " (q to quit)"
QUIT_OPTION = "Quit - stop here"


def _emit(lines: Iterable[str]) -> None:
    for line in lines:
        print(f"{BULLET}{line}", flush=True)


def _colour_ok() -> bool:
    """ANSI colour only on a real terminal that understands it."""
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    if hostsys.IS_WINDOWS:
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


def warning_box(title: str, body: Sequence[str]) -> None:
    """A warning that cannot be skimmed past: boxed, and bold red where possible."""
    width = max(len(title), *(len(line) for line in body)) + 4
    start, end = ("\x1b[1;31m", "\x1b[0m") if _colour_ok() else ("", "")
    rule = "!" * (width + 4)
    lines = [rule, f"!! {title.upper().center(width - 2)} !!", rule]
    lines += [f"!! {line.ljust(width - 2)} !!" for line in body]
    lines.append(rule)
    print("", flush=True)
    for line in lines:
        print(f"{start}{line}{end}", flush=True)


def section(title: str, body: Sequence[str] = ()) -> None:
    print("", flush=True)
    print(f"-- {title}", flush=True)
    if body:
        _emit(body)


def _raw(question: str, default: str | None) -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{question}{suffix}: ").strip()
    if answer.lower() in QUIT_WORDS:
        raise QuitRun("stopped at an operator prompt")
    if not answer and default is not None:
        return default
    return answer


class SecretLine:
    """What a password prompt holds and shows, without any terminal involved.

    The last character typed is shown until the next keystroke (or a pause),
    so a slip of the finger is visible; everything before it is an asterisk.
    """

    ERASE = {"\x7f", "\x08"}
    KILL = "\x15"  # Ctrl-U clears the line, as it does at a shell prompt

    def __init__(self) -> None:
        self.chars: list[str] = []

    def feed(self, char: str) -> bool:
        """Apply one keystroke.  True when it added a visible character."""
        if char in self.ERASE:
            if self.chars:
                self.chars.pop()
            return False
        if char == self.KILL:
            self.chars.clear()
            return False
        if char.isprintable():
            self.chars.append(char)
            return True
        return False  # any other control character is ignored

    def display(self, reveal_last: bool) -> str:
        if not self.chars:
            return ""
        last = self.chars[-1] if reveal_last else "*"
        return "*" * (len(self.chars) - 1) + last

    @property
    def value(self) -> str:
        return "".join(self.chars)


def ask_secret(prompt: str, reveal_seconds: float = 1.0) -> str:
    """Read a password, showing only the character just typed.

    Nothing is echoed by the terminal itself, nothing is written anywhere, and
    the line is redrawn as all asterisks before Enter moves on - so the
    scrollback never holds a readable character.  Without an interactive
    terminal it falls back to getpass, which shows nothing at all.
    """
    prompt = prompt.rstrip().rstrip(":") + QUIT_HINT + ": "
    value = _read_secret(prompt, reveal_seconds)
    # Real passwords here are at least eight characters, so a lone q cannot
    # be one.
    if value.strip().lower() in QUIT_WORDS:
        raise QuitRun("stopped at a password prompt")
    return value


def _read_secret(prompt: str, reveal_seconds: float) -> str:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return getpass.getpass(prompt)
    out = sys.stdout
    # Keep the whole field on one row so a carriage return can redraw it.
    if len(prompt) + 65 >= shutil.get_terminal_size().columns:
        out.write(prompt.rstrip() + "\n")
        prompt = "> "
    if hostsys.IS_WINDOWS:
        return _ask_secret_windows(prompt, reveal_seconds)
    try:
        import termios
        import tty
    except ImportError:  # not a POSIX terminal
        return getpass.getpass(prompt)

    fd = sys.stdin.fileno()
    line = SecretLine()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")

    def draw(reveal_last: bool) -> None:
        out.write("\r\x1b[K" + prompt + line.display(reveal_last))
        out.flush()

    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)  # no echo, no line buffering; Ctrl-C still interrupts
        draw(False)
        revealed = False
        while True:
            timeout = reveal_seconds if revealed else None
            ready, _, _ = select.select([fd], [], [], timeout)
            if not ready:
                draw(False)
                revealed = False
                continue
            byte = os.read(fd, 1)
            if not byte or byte == b"\x04":
                raise EOFError
            if byte == b"\x1b":
                # Arrow and function keys arrive as escape sequences: discard.
                while select.select([fd], [], [], 0.02)[0]:
                    os.read(fd, 16)
                continue
            text = decoder.decode(byte)
            if not text:
                continue  # the middle of a multi-byte character
            if text in ("\n", "\r"):
                break
            revealed = line.feed(text)
            draw(revealed)
    finally:
        draw(False)
        out.write("\n")
        out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    return line.value


def _ask_secret_windows(prompt: str, reveal_seconds: float) -> str:
    """The same behaviour through the Windows console's key-by-key reader."""
    import msvcrt

    out = sys.stdout
    line = SecretLine()

    def draw(reveal_last: bool) -> None:
        out.write("\r" + " " * (len(prompt) + 66) + "\r" + prompt + line.display(reveal_last))
        out.flush()

    try:
        draw(False)
        revealed_at: float | None = None
        while True:
            if not msvcrt.kbhit():
                if revealed_at is not None and time.monotonic() - revealed_at >= reveal_seconds:
                    draw(False)
                    revealed_at = None
                time.sleep(0.02)
                continue
            char = msvcrt.getwch()
            if char in ("\x00", "\xe0"):
                msvcrt.getwch()  # arrow and function keys arrive as two characters
                continue
            if char == "\x03":
                raise KeyboardInterrupt
            if char == "\x1a":
                raise EOFError
            if char in ("\r", "\n"):
                break
            if line.feed("\x7f" if char == "\x08" else char):
                revealed_at = time.monotonic()
                draw(True)
            else:
                revealed_at = None
                draw(False)
    finally:
        draw(False)
        out.write("\n")
        out.flush()
    return line.value


def wait_for_key(prompt: str = "Press any key to continue, or q to quit...") -> None:
    """Pause until one key is pressed; q quits, anything else carries on.

    Without an interactive terminal there is nobody to wait for, so it returns.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        if hostsys.IS_WINDOWS:
            import msvcrt

            char = msvcrt.getwch()
            if char in ("\x00", "\xe0"):
                msvcrt.getwch()
            if char == "\x03":
                raise KeyboardInterrupt
            if char.lower() == "q":
                raise QuitRun("stopped before starting")
            return
        import termios
        import tty

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            key = os.read(fd, 1)
            while select.select([fd], [], [], 0.02)[0]:
                os.read(fd, 16)  # the rest of an arrow or function key
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        if key.lower() == b"q":
            raise QuitRun("stopped before starting")
    finally:
        sys.stdout.write("\n")
        sys.stdout.flush()


def ask_text(
    question: str,
    *,
    help_lines: Sequence[str] = (),
    default: str | None = None,
    example: str | None = None,
    validate: Callable[[str], tuple[bool, str]] | None = None,
    quit_hint: bool = True,
) -> str:
    if help_lines:
        _emit(help_lines)
    if example:
        _emit([f"Example: {example}"])
    while True:
        answer = _raw(question + (QUIT_HINT if quit_hint else ""), default)
        if not answer:
            _emit(["An answer is needed here. Type quit to stop."])
            continue
        if validate is None:
            return answer
        ok, message = validate(answer)
        if ok:
            return answer
        _emit([message])


def ask_yes_no(
    question: str,
    *,
    help_lines: Sequence[str] = (),
    default: bool | None = None,
) -> bool:
    if help_lines:
        _emit(help_lines)
    hint = {True: "Y/n", False: "y/N", None: "y/n"}[default] + ", q to quit"
    while True:
        answer = _raw(f"{question} ({hint})", None).lower()
        if not answer and default is not None:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        _emit(["Please answer y or n, or q to quit."])


def ask_choice(question: str, options: Sequence[str], *, help_lines: Sequence[str] = ()) -> int:
    """Returns the zero-based index of the chosen option.

    Every menu ends with a Quit option, which stops the run cleanly.
    """
    if help_lines:
        _emit(help_lines)
    shown = [*options, QUIT_OPTION]
    for index, option in enumerate(shown, 1):
        _emit([f"{index}. {option}"])
    while True:
        answer = _raw(question, None)
        try:
            chosen = int(answer)
        except ValueError:
            _emit(["Type the number of one of the options above."])
            continue
        if chosen == len(shown):
            raise QuitRun("stopped at an operator prompt")
        if 1 <= chosen <= len(options):
            return chosen - 1
        _emit([f"Choose a number between 1 and {len(shown)}."])


def ping_ok(address: str, timeout: int = 2) -> bool:
    return hostsys.ping_ok(address, timeout)


def ask_ipv4(
    question: str,
    *,
    help_lines: Sequence[str] = (),
    default: str | None = None,
    example: str | None = "192.168.1.50",
    must_respond: bool = False,
) -> str:
    """Ask for an address on the local network, and sanity-check it."""

    def validate(answer: str) -> tuple[bool, str]:
        try:
            address = ipaddress.ip_address(answer)
        except ValueError:
            return False, (
                f"'{answer}' is not an address. It should be four numbers "
                "separated by dots, like 192.168.1.50 - not a name and not an "
                "interface like eth0."
            )
        if address.version != 4:
            return False, "This needs an IPv4 address, like 192.168.1.50."
        if not address.is_private:
            return False, (
                f"{answer} is not a home-network address. Those normally start "
                "192.168., 10., or 172.16-31."
            )
        return True, ""

    while True:
        answer = ask_text(
            question, help_lines=help_lines, default=default,
            example=example, validate=validate,
        )
        help_lines = ()  # only explain once
        example = None
        if not must_respond or ping_ok(answer):
            return answer
        _emit([
            f"Nothing at {answer} answered a ping.",
            "That usually means the address is wrong, or the device is still",
            "starting up, or it is on a different network from this computer.",
        ])
        if ask_yes_no("Use it anyway?", default=False):
            return answer


def wait_for_host(
    address: str,
    *,
    what: str = "the plug",
    timeout: float = 240.0,
    poll: float = 5.0,
) -> bool:
    """Watch for a device coming back, instead of asking whether it has.

    Prints a live count so it never looks hung, and returns False on timeout so
    the caller can offer something more useful than failing.
    """
    print(f"{BULLET}Watching for {what} at {address} - this can take a minute.",
          flush=True)
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if ping_ok(address):
            elapsed = time.monotonic() - started
            sys.stdout.write("\r" + " " * 60 + "\r")
            print(f"{BULLET}{what.capitalize()} is back at {address} "
                  f"(after {elapsed:.0f}s).", flush=True)
            return True
        elapsed = int(time.monotonic() - started)
        sys.stdout.write(f"\r{BULLET}still waiting... {elapsed}s")
        sys.stdout.flush()
        time.sleep(poll)
    sys.stdout.write("\r" + " " * 60 + "\r")
    print(f"{BULLET}No answer from {address} after {timeout:.0f}s.", flush=True)
    return False


def wait_or_ask(
    address: str,
    *,
    what: str = "the plug",
    timeout: float = 240.0,
    help_lines: Sequence[str] = (),
) -> str:
    """Wait for `address`; if it never answers, offer the sensible ways out."""
    while True:
        if wait_for_host(address, what=what, timeout=timeout):
            return address
        choice = ask_choice(
            "What would you like to do",
            [
                "Keep waiting",
                f"{what.capitalize()} is on a different address - let me type it",
            ],
            help_lines=help_lines or [
                "It may still be starting up, or it may have been given a new",
                "address. Your router's list of connected devices will show it.",
            ],
        )
        if choice == 0:
            continue
        if choice == 1:
            return ask_ipv4(f"What address is {what} on now", must_respond=True)
        raise QuitRun(f"{what} did not come back")


def scan_wifi_ssids(timeout: float = 20.0) -> list[str]:
    """Names of Wi-Fi networks this machine can currently see.

    Best effort: returns an empty list when no supported scanner is present, so
    callers must treat "nothing found" as "do not know", never as "not there".
    """
    return hostsys.wifi_ssids(timeout=timeout)


def find_ssid(prefix: str, timeout: float = 20.0) -> str | None:
    """The first visible network whose name starts with `prefix`, if any."""
    for name in scan_wifi_ssids(timeout=timeout):
        if name.startswith(prefix):
            return name
    return None


def wait_for_ssid(prefix: str, *, timeout: float = 90.0, poll: float = 8.0) -> str | None:
    """Watch for a network to appear, e.g. a device rebooting into setup mode."""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        found = find_ssid(prefix, timeout=poll * 2)
        if found:
            return found
        elapsed = int(time.monotonic() - started)
        sys.stdout.write(f"\r{BULLET}looking for a '{prefix}' network... {elapsed}s")
        sys.stdout.flush()
        time.sleep(poll)
    sys.stdout.write("\r" + " " * 60 + "\r")
    return None


def resolve_host(name: str) -> str | None:
    """The address a device name resolves to, if the network can answer.

    Many routers publish DHCP hostnames in local DNS, and most desktop Linux
    resolves .local through mDNS, so a Tasmota device is often reachable by name
    without anyone reading an address off a router page.
    """
    for candidate in (name, f"{name}.local"):
        try:
            address = socket.gethostbyname(candidate)
        except OSError:
            continue
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if parsed.version == 4 and parsed.is_private:
            return address
    return None
