"""Prompts written for someone doing this for the first time.

Rules this module enforces so individual steps do not have to:
  - every question explains what it wants and where to find it;
  - a wrong answer is re-asked, never fatal;
  - answers are validated before the run continues;
  - "quit" always works, at every prompt, and stops cleanly.
"""

from __future__ import annotations

import codecs
import contextlib
import getpass
import ipaddress
import os
import select
import shutil
import socket
import sys
import time
from typing import Callable, Iterable, Sequence

from . import hostsys, output


class QuitRun(SystemExit):
    """The operator chose to quit at a prompt.

    A SystemExit, so a standalone tool that does not catch it simply stops
    with a short message rather than a traceback; the main tool catches it and
    says how to resume.
    """

    def __init__(self, reason: str = "") -> None:
        super().__init__("Stopped.")
        self.reason = reason


class NoOneToAnswer(QuitRun):
    """An unattended run reached a question it cannot answer for itself."""


# An unattended run (--unattended): nobody is at the keyboard.  A key to press
# is not waited for, a yes/no question takes its default - the cautious answer
# wherever there is one - and any other question stops the run, naming it,
# rather than waiting for ever.
UNATTENDED = False

BULLET = "    "
# A password field on a row of its own, below a prompt too long to share one.
FIELD = output.MARGIN + "> "
QUIT_WORDS = {"q", "quit", "exit", "abort"}
QUIT_HINT = " (q to quit)"
QUIT_OPTION = "Quit - stop here"


def _emit(lines: Iterable[str]) -> None:
    for line in lines:
        output.show(f"{BULLET}{line}")


def _retry(mark: tuple[int, bool] | None, lines: Iterable[str]) -> None:
    """A wrong answer: take it and its question off the screen again, back to
    `mark`, then say why before asking again - so wrong answers do not pile
    up, one under another, until the question is taller than the window."""
    output.transient_rewind(mark)
    _emit(lines)


_colour_ok = output.colour_ok


def warning_box(title: str, body: Sequence[str]) -> None:
    """A short note to read before going on: a highlighted title, then the text."""
    output.show("")
    output.show(f"! {title}", output.BOLD, output.YELLOW)
    for line in body:
        output.show(f"{BULLET}{line}")


def section(title: str, body: Sequence[str] = (), *, quiet: bool = False) -> None:
    """A heading and its text.  quiet: for information only, so it is shown in
    verbose mode and otherwise just logged."""
    say = output.detail if quiet else output.show
    say("")
    say(f"-- {title}")
    for line in body:
        say(f"{BULLET}{line}")


def answered_unattended(question: str, answer: str) -> None:
    """Show and log a question as an unattended run answered it."""
    output.show(f"{question}: {answer}   (unattended)")


def _no_one_to_answer(question: str) -> NoOneToAnswer:
    output.show("")
    output.show("! An unattended run cannot answer this, so it stops here:",
                output.BOLD, output.RED)
    output.show(f"{BULLET}{question.rstrip(': ')}")
    return NoOneToAnswer(f"unattended: no answer for {question!r}")


def _raw(question: str, default: str | None, private: str | None = None) -> str:
    if UNATTENDED:
        raise _no_one_to_answer(question)
    suffix = f" [{default}]" if default else ""
    answer = input(output.question(f"{question}{suffix}", ": ")).strip()
    if private:
        output.private(answer, private)
    output.answered(answer)
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
    if UNATTENDED:
        raise _no_one_to_answer(prompt)
    prompt = prompt.rstrip().rstrip(":") + QUIT_HINT + ": "
    prompt = output.question(prompt)  # the question is logged; the answer never is
    value = ""
    try:
        value = _read_secret(prompt, reveal_seconds)
    finally:
        # What the screen holds: the prompt, then one asterisk a character -
        # on a row of their own when the prompt was too long to share one.
        stars = "*" * len(value)
        output.after_secret(f"\n{FIELD}{stars}" if _secret_field_below(prompt) else stars)
    # Real passwords here are at least eight characters, so a lone q cannot
    # be one.
    if value.strip().lower() in QUIT_WORDS:
        raise QuitRun("stopped at a password prompt")
    return value


# A wrong password more often than this, and some systems lock the account
# for a while; sudo itself stops after three.
ADMIN_PASSWORD_TRIES = 3


def ask_admin_password() -> str:
    """This computer's password, asked here and checked by sudo
    (hostsys.password_asker).  q quits; three wrong ones give up."""
    with output.hold():
        for attempt in range(ADMIN_PASSWORD_TRIES):
            password = ask_secret("This computer's password (for sudo): ")
            with _echo_off():  # sudo takes a moment over a wrong one; typing ahead waits unseen
                accepted = hostsys.check_sudo_password(password) is None
            if accepted:
                return password
            if attempt + 1 < ADMIN_PASSWORD_TRIES:
                output.show(f"{BULLET}That password was not accepted - try again.")
    raise OSError(f"this computer's password was not accepted {ADMIN_PASSWORD_TRIES} times")


@contextlib.contextmanager
def _echo_off():
    """The terminal shows nothing typed meanwhile; it is kept for the next read."""
    if hostsys.IS_WINDOWS or not sys.stdin.isatty():
        yield
        return
    import termios

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    quiet = list(saved)
    quiet[3] &= ~termios.ECHO
    termios.tcsetattr(fd, termios.TCSANOW, quiet)
    try:
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, saved)


def _secret_field_below(prompt: str) -> bool:
    """Whether the password field goes on the row below its prompt, so the
    whole field fits on one row and a carriage return can redraw it."""
    return len(prompt) + 65 >= shutil.get_terminal_size().columns


def _read_secret(prompt: str, reveal_seconds: float) -> str:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return getpass.getpass(prompt)
    out = sys.stdout
    if _secret_field_below(prompt):
        out.write(prompt.rstrip() + "\n")
        prompt = FIELD
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


def wait_for_key(prompt: str = "Press any key to continue, or q to quit...", *,
                 until: Callable[[], bool] | None = None) -> None:
    """Pause until one key is pressed; q quits, anything else carries on.
    `until`: stop waiting without a key, too, once it is true - looked at a
    few times a second.

    Without an interactive terminal, or in an unattended run, there is nobody
    to wait for, so it returns.
    """
    if UNATTENDED or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return
    sys.stdout.write(output.question(prompt))
    sys.stdout.flush()
    try:
        if hostsys.IS_WINDOWS:
            import msvcrt

            while until is not None and not msvcrt.kbhit():
                if until():
                    return
                time.sleep(0.25)
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
            while until is not None and not select.select([fd], [], [], 0.25)[0]:
                if until():
                    return
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
        output.after_input()


def wait_for_taller_window() -> None:
    """First of all, a window too short for answered questions to be cleared
    away is asked to be made taller.  Once it is, the note goes and the run
    goes on by itself; a key goes on without."""
    rows = output.short_window(before_start=True)
    if not rows:
        return
    output.transient_begin(before_start=True)
    warning_box("Make this window taller", [
        f"It is {rows} rows tall; make it at least {output.MIN_ROWS}.",
        "(Otherwise things wouldn't display properly for you)",
    ])
    wait_for_key("Or press any key to go on as it is, or q to quit...",
                 until=lambda: output.short_window(before_start=True) is None)
    output.transient_end()


def ask_text(
    question: str,
    *,
    help_lines: Sequence[str] = (),
    default: str | None = None,
    example: str | None = None,
    validate: Callable[[str], tuple[bool, str]] | None = None,
    quit_hint: bool = True,
    private: str | None = None,
) -> str:
    """`private`: the answer is kept out of the log, written as <private>."""
    if help_lines:
        _emit(help_lines)
    if example:
        _emit([f"Example: {example}"])
    mark = output.transient_mark()
    while True:
        answer = _raw(question + (QUIT_HINT if quit_hint else ""), default, private)
        if not answer:
            _retry(mark, ["An answer is needed here. Type quit to stop."])
            continue
        if validate is None:
            return answer
        ok, message = validate(answer)
        if ok:
            return answer
        _retry(mark, [message])


def ask_yes_no(
    question: str,
    *,
    help_lines: Sequence[str] = (),
    default: bool | None = None,
) -> bool:
    if help_lines:
        _emit(help_lines)
    hint = {True: "Y/n", False: "y/N", None: "y/n"}[default] + ", q to quit"
    if UNATTENDED and default is not None:
        answered_unattended(f"{question} ({hint})", "y" if default else "n")
        return default
    mark = output.transient_mark()
    while True:
        answer = _raw(f"{question} ({hint})", None).lower()
        if not answer and default is not None:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        _retry(mark, ["Please answer y or n, or q to quit."])


def ask_choice(question: str, options: Sequence[str], *, help_lines: Sequence[str] = ()) -> int:
    """Returns the zero-based index of the chosen option.

    Every menu ends with a Quit option, which stops the run cleanly.
    """
    if help_lines:
        _emit(help_lines)
    shown = [*options, QUIT_OPTION]
    for index, option in enumerate(shown, 1):
        _emit([f"{index}. {option}"])
    mark = output.transient_mark()
    while True:
        answer = _raw(question, None)
        try:
            chosen = int(answer)
        except ValueError:
            _retry(mark, ["Type the number of one of the options above."])
            continue
        if chosen == len(shown):
            raise QuitRun("stopped at an operator prompt")
        if 1 <= chosen <= len(options):
            return chosen - 1
        _retry(mark, [f"Choose a number between 1 and {len(shown)}."])


def ping_ok(address: str, timeout: int = 2) -> bool:
    return hostsys.ping_ok(address, timeout)


def ask_ipv4(
    question: str,
    *,
    help_lines: Sequence[str] = (),
    default: str | None = None,
    example: str | None = "192.168.1.50",
    must_respond: bool = False,
    check: Callable[[str], list[str]] | None = None,
) -> str:
    """Ask for an address on the local network, and sanity-check it.

    `must_respond`: the address has to answer a ping.  `check` (which implies
    it): what else is wrong with an address that answered, or nothing when it
    will do.  There is no using an address anyway: the run could not reach
    the plug there, so it is asked for again, under what was wrong.
    """

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

    mark = output.transient_mark()
    problem: list[str] = []  # what was wrong with the last address, shown above the question
    # No stage clock under the address while it is checked: two more rows
    # would make a try taller than a short window, and its first row would
    # scroll out of reach of being taken away again.
    with output.hold():
        while True:
            _emit(problem)
            answer = ask_text(
                question, help_lines=help_lines, default=default,
                example=example, validate=validate,
            )
            if not (must_respond or check):
                return answer
            with _echo_off():  # typing ahead meanwhile waits unseen
                if not ping_ok(answer):
                    problem = [
                        f"Nothing at {answer} answered a ping.",
                        "That usually means the address is wrong, or the device is still",
                        "starting up, or it is on a different network from this computer.",
                    ]
                else:
                    problem = check(answer) if check is not None else []
            if not problem:
                return answer
            # Each try taken away again, or the question would climb the screen
            # one try at a time; when it stays, it has been explained once already.
            if not output.transient_rewind(mark):
                help_lines = ()
                example = None


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
    output.detail(f"{BULLET}Watching for {what} at {address} - this can take a minute.")
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if ping_ok(address):
            elapsed = time.monotonic() - started
            output.progress_clear()
            output.detail(f"{BULLET}{what.capitalize()} is back at {address} "
                          f"(after {elapsed:.0f}s).")
            return True
        elapsed = int(time.monotonic() - started)
        output.progress(f"waiting for {what}... {elapsed}s")
        time.sleep(poll)
    output.progress_clear()
    output.show(f"{BULLET}No answer from {address} after {timeout:.0f}s.")
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
        # "No answer" and the question leave the screen once answered, so
        # keeping on waiting does not stack another copy of them.
        output.transient_begin()
        if wait_for_host(address, what=what, timeout=timeout):
            output.transient_end()
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
            output.transient_end()
            continue
        if choice == 1:
            found = ask_ipv4(f"What address is {what} on now", must_respond=True)
            output.transient_end()
            return found
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
        output.progress(f"looking for a '{prefix}' network... {elapsed}s")
        time.sleep(poll)
    output.progress_clear()
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
