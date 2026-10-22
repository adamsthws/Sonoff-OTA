"""Drive the reviewed migration phase servers without console pasting.

This starts tools/serve_safeboot_migration.py exactly as the runbook does,
reads the URL and arm token it prints, and delivers the loader to the plug over
Tasmota's ordinary command endpoint.  It changes the transport only: the Berry
that executes on the device is byte-for-byte the reviewed template the server
renders, and the host server remains the sole judge of PASS.
"""

from __future__ import annotations

import http.client
import json
import re
import subprocess
import threading
import time
import urllib.error
from pathlib import Path
from typing import Callable

from . import hostsys, interrupt, output
from .device import Device

ARMED_PHASES = {"commit", "restore"}
CLOSURE_NAME = {"commit": "autoflash_commit", "restore": "autoflash_restore"}

LOADER_RE = re.compile(r"autoflash_urlbeload\('(?P<url>http://[^']+)'\)")
# The server prints instructions for a human pasting into the Berry console.
# This tool delivers the same program over HTTP instead, so echoing them would
# invite someone to run it a second time - next to the irreversible step.
MANUAL_INSTRUCTION_RE = re.compile(
    r"paste this as one line|do not prefix it with|paste the loader|"
    r"^\s*def autoflash_urlbeload|^\s*autoflash_(commit|restore)\(|"
    r"loading performs no writes|arm separately with",
    re.IGNORECASE,
)
ARM_RE = re.compile(r"(?P<name>autoflash_(?:commit|restore))\(\"(?P<token>[^\"]+)\"\)")


class PhaseError(RuntimeError):
    pass


REQUEST_RE = re.compile(r'"(?:GET|POST) (?P<path>/\S*) HTTP')
SECTOR = 4096
# Lines from the server worth showing as they are; everything else is folded
# into a progress line and kept, in full, in the phase's log file.
SHOWN_RE = re.compile(r"RESULT:|PLUG SAYS|REFUS|TIMEOUT|ERROR|Traceback|Stopped", re.IGNORECASE)
# The plug's own console lines worth showing when a phase fails: its Berry
# prints and errors, which say why when it could not tell this computer.
PLUG_CONSOLE_RE = re.compile(r'AUTOFLASH |migration_refused|BRY:|"Br":"\[')
PLUG_SAYS_RE = re.compile(r"THE PLUG SAYS: (?P<reason>.+)")
# A Berry error as Tasmota logs a console command's result:
#   RSL: RESULT = {"Br":"[migration_refused] POST /capture/staged/22 returned HTTP -1 ..."}
BERRY_ERROR_RE = re.compile(r'"Br":"\[(?P<kind>[^\]]+)\] (?P<message>.*)"\}')
TIMEOUT_RE = re.compile(r"TIMEOUT: (?P<reason>.+)")


def failure_reason(captured: str) -> str | None:
    """Why a phase did not pass, in the server's words, if it said."""
    for line in reversed(captured.splitlines()):
        found = PLUG_SAYS_RE.search(line)
        if found:
            return "the plug says: " + found.group("reason").strip()
        found = TIMEOUT_RE.search(line)
        if found:
            return found.group("reason").strip()
    return None


class PhaseDisplay:
    """Turns the phase server's request log into a few readable lines."""

    def __init__(self, phase: str, blocks: int | None, write: Callable[[str], None],
                 log: Callable[[str], None], title: str | None = None) -> None:
        self.phase = phase
        self.title = title
        self.blocks = blocks
        self.write = write
        self.log = log
        self.progress = False
        self.copied_chunks: set[str] = set()
        self.read = 0

    def _line(self, text: str) -> None:
        if self.progress:
            self.write("\n")
            self.progress = False
        self.log(text)

    def _status(self, text: str) -> None:
        self.write("\r" + text + "   ")
        self.progress = True

    def feed(self, line: str) -> None:
        request = REQUEST_RE.search(line)
        if request:
            path = request.group("path")
            if path.startswith("/berry/"):
                self._line("    The plug fetched its instructions.")
            elif path.startswith("/chunk/"):
                self.copied_chunks.add(path)
                copied = len(self.copied_chunks)
                total = f"/{self.blocks}" if self.blocks else ""
                what = self.title or "the startup firmware"
                self._status(f"    Copying {what}: block {copied}{total}")
            elif path.startswith("/capture/") and self.phase == "preflight":
                self.read += 1
                self._status(f"    Reading the plug's memory layout: {self.read} blocks")
            elif path == "/target-table":
                self._line("    The plug fetched the new memory layout.")
            return
        if SHOWN_RE.search(line):
            self._line("    " + line.strip())

    def close(self) -> None:
        if self.progress:
            self.write("\n")
            self.progress = False


class PlugNeverConnected(PhaseError):
    """The plug did not fetch its instructions - nearly always a firewall."""


# The server logs each request; the plug fetching its program is the first.
FETCH_RE = re.compile(r'"GET /berry/')
# While a phase runs, the plug answers nothing else: Berry holds it.  So once
# it has been this quiet, an answer from its web page means it has stopped -
# with nothing more to send.  Without that, only the server's own two-minute
# silence limit ends the wait.
SILENCE_CHECK = 20.0
# How many times the command that starts a phase is sent, when it does not
# reach the plug at all.
START_TRIES = 3
# A backstop only: a phase keeps the plug busy for about a minute at most,
# retrying included; the server's silence limit is two minutes.
EXEC_TIMEOUT = 300.0
# Long enough for a slow plug, short enough not to look hung.
FETCH_DEADLINE = 30.0


def short_loader(url: str, assign_to: str | None = None) -> str:
    """A compact equivalent of the server's printed loader.

    The printed loader defines a helper function first, which makes the command
    line long enough to risk Tasmota's console buffer.  This fetches and
    compiles the same served program in far fewer characters.
    """
    fetch = f"var c=webclient() c.begin('{url}') c.GET() "
    if assign_to:
        return fetch + f"{assign_to}=compile(c.get_string())()"
    return fetch + "compile(c.get_string())()"


class PhaseRunner:
    _started_at: float | None = None
    _resending = False  # the start, not delivered, waiting to go again

    def _require_fetch(self, process, captured: list[str], reader, phase: str) -> None:
        """Stop within seconds, not half an hour, when the plug cannot reach us."""
        waited = 0.0
        while waited < FETCH_DEADLINE and process.poll() is None:
            if any(FETCH_RE.search(line) for line in captured):
                return
            reader.join(0.5)
            waited += 0.5
        if any(FETCH_RE.search(line) for line in captured):
            return
        process.terminate()
        reader.join(5.0)
        raise PlugNeverConnected(
            f"the plug did not fetch the {phase} instructions from this computer "
            f"within {FETCH_DEADLINE:.0f}s; nothing was written"
        )

    def __init__(
        self,
        repo_root: Path,
        manifest: Path,
        evidence_dir: Path,
        listen_ip: str,
        device: Device,
        listen_port: int = 8089,
        log: Callable[[str], None] = print,
        recovery: bytes | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.manifest = manifest
        self.evidence_dir = evidence_dir
        self.listen_ip = listen_ip
        self.device = device
        self.listen_port = listen_port
        self.log = log
        # The credential-carrying recovery image: piped to the server, never a file.
        self.recovery = recovery
        # How long the last phase kept the plug busy: from telling it to start
        # to its last request.  Busy for 70-90s, a real S60TPG stops answering.
        self.busy_seconds: float | None = None

    def _blocks(self, phase: str, plan: "Plan | None" = None) -> int | None:
        """How many 4 KiB blocks of firmware a phase copies, for progress."""
        if plan is not None:
            return plan.blocks()
        try:
            artifacts = json.loads(self.manifest.read_text(encoding="utf-8"))["artifacts"]
            if phase == "stage":
                size = artifacts.get("recovery_safeboot", artifacts["safeboot"])["size"]
            elif phase == "restore":
                size = artifacts["safeboot"]["size"]
            else:
                return None
            return (int(size) + SECTOR - 1) // SECTOR
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _command(self, phase: str, extra_flags: list[str], plan: "Plan | None" = None) -> list[str]:
        if plan is not None:
            piped = ["--plan-from-stdin", "--no-sensitive-captures"]
        elif self.recovery:
            piped = ["--recovery-from-stdin", "--no-sensitive-captures"]
        else:
            piped = []
        return hostsys.python_script(
            self.repo_root / "tools" / "serve_safeboot_migration.py",
            phase,
            "--manifest",
            str(self.manifest),
            "--evidence-dir",
            str(self.evidence_dir),
            "--listen-ip",
            self.listen_ip,
            "--listen-port",
            str(self.listen_port),
            "--device-ip",
            self.device.host,
            *piped,
            *extra_flags,
            unbuffered=True,
        )

    def run(
        self,
        phase: str,
        extra_flags: list[str] | None = None,
        confirm: Callable[[str], bool] | None = None,
        exec_timeout: float = EXEC_TIMEOUT,
        plan: "Plan | None" = None,
    ) -> tuple[int, str]:
        """Run one phase end to end.  Returns (exit code, captured output).

        `plan`: what the plan phase writes (tools/autoflash/plans.py), with all
        its data - piped to the server like the recovery image, never a file.
        """
        if phase in ARMED_PHASES and confirm is None:
            raise PhaseError(f"{phase} requires an explicit confirmation callback")
        self.busy_seconds, self._started_at = None, None

        piped = plan.bundle() if plan is not None else self.recovery
        process = subprocess.Popen(
            self._command(phase, extra_flags or [], plan),
            cwd=str(self.repo_root),
            stdin=subprocess.PIPE if piped else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            **interrupt.child_group(),  # a first Ctrl-C asks, and must not stop it meanwhile
        )
        if piped:
            assert process.stdin is not None
            try:
                process.stdin.buffer.write(piped)
                process.stdin.close()
            except BrokenPipeError:
                pass  # the server stopped early; its output says why
        captured: list[str] = []
        url: str | None = None
        arm: tuple[str, str] | None = None
        heard = [time.monotonic()]  # the server's last line: the plug's last request
        requested: list[float | None] = [None]  # the plug's last request, exactly
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.evidence_dir / (f"plan-{plan.name}-server.log" if plan else f"{phase}-server.log")
        log_file = log_path.open("w", encoding="utf-8")
        display = PhaseDisplay(
            phase, self._blocks(phase, plan), title=plan.title if plan else None,
            write=output.raw_progress,
            log=self.log,
        )

        def pump() -> None:
            nonlocal url, arm
            assert process.stdout is not None
            for line in process.stdout:
                heard[0] = time.monotonic()
                if REQUEST_RE.search(line):
                    requested[0] = heard[0]
                captured.append(line.rstrip("\n"))
                if not log_file.closed:
                    log_file.write(output.scrub(line))
                    log_file.flush()
                if not MANUAL_INSTRUCTION_RE.search(line):
                    display.feed(line)
                if url is None:
                    found = LOADER_RE.search(line)
                    if found:
                        url = found.group("url")
                if arm is None:
                    found = ARM_RE.search(line)
                    if found:
                        arm = (found.group("name"), found.group("token"))

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            code = self._drive(phase, process, captured, reader, confirm, exec_timeout,
                               lambda: url, lambda: arm, lambda: heard[0])
        except PhaseError as exc:
            if isinstance(exc, PlugNeverConnected):  # it never started: nothing to say
                raise
            said = self._show_plug_console()
            self._advise_if_stuck()
            if said:
                raise PhaseError(f"{exc}; its console says: {said}") from exc
            raise
        finally:
            if process.poll() is None:
                process.terminate()
            reader.join(5.0)
            display.close()
            log_file.close()
            if (self._started_at is not None and requested[0] is not None
                    and requested[0] >= self._started_at):
                self.busy_seconds = round(requested[0] - self._started_at, 1)
        if code != 0:
            said = self._show_plug_console()
            self._advise_if_stuck()
            if said and not any(PLUG_SAYS_RE.search(line) for line in captured):
                captured.append(f"THE PLUG SAYS: {said}")  # what failure_reason() reports
            self.log(f"    The server's full output is in {log_path}")
        return code, "\n".join(captured)

    def _advise_if_stuck(self) -> None:
        """Say what to do when the plug does not answer at all after a phase.

        Tasmota can be left waiting for ever on a download this computer
        stopped sending part way (HTTPClientLight reads a body with no
        timeout), and then answers nothing.  Switching it off and on is the
        way out, and safe at every point a phase can be left there: staging
        writes only the spare slot, the commit downloads before it writes and
        reports after, and the restore carries on where it stopped.
        """
        if self._plug_answers():
            return
        # Said before this run decides whether to try the step again, so it
        # must hold either way: nothing here about running the tool again.
        self.log("    The plug is not answering at all - it may be off the Wi-Fi, or stuck waiting")
        self.log("    for this computer. If its web page still does not answer in a few minutes,")
        self.log("    switch it off and on: at this stage that is safe.")

    def _show_plug_console(self) -> str | None:
        """Show the plug's own last words on the phase, from its console, and
        return its error if that is the last of them.

        When the plug could not reach this computer, its console is the only
        place that says why; read it now rather than ask the owner to.  An
        error followed by more from the plug is an earlier attempt's, not why
        this one stopped.
        """
        try:
            lines = self.device.console_log(timeout=5.0)
        except Exception:  # noqa: BLE001 - a plug that cannot answer adds nothing here
            return None
        relevant = [line for line in lines if PLUG_CONSOLE_RE.search(line)][-8:]
        for line in relevant:
            self.log(f"    The plug's console: {line}")
        error = BERRY_ERROR_RE.search(relevant[-1]) if relevant else None
        if error is None:
            return None
        kind, message = error.group("kind"), error.group("message")
        return message if kind == "migration_refused" else f"{kind}: {message}"

    def _start(self, code: str) -> None:
        """Send the command that starts the phase, without waiting on its reply.

        The phase reports to the server, which is what is waited on; the reply
        can come only when the phase ends - or never, when it ends by
        restarting the plug, as the commit does.
        """
        self._started_at = time.monotonic()

        def fire() -> None:
            for attempt in range(1, START_TRIES + 1):
                try:
                    self.device.berry(code, timeout=60.0)
                    return
                except urllib.error.HTTPError:
                    return  # it answered: the server's evidence decides
                except urllib.error.URLError as exc:
                    # Not connected, or the request not sent (device._exchange):
                    # it never reached the plug, so sending it again cannot
                    # start the phase twice. Seen in the fault suite: a Wi-Fi
                    # blip as it went, and the plug never started.
                    if attempt == START_TRIES:
                        self.log(f"    Starting it on the plug: {exc}")
                        return
                    self._resending = True
                    try:
                        if not self._plug_back(30.0):
                            return
                    finally:
                        self._resending = False
                    self._started_at = time.monotonic()
                except (TimeoutError, ConnectionError, http.client.HTTPException):
                    return  # sent, its reply lost or still to come: the server's evidence decides
                except Exception as exc:  # noqa: BLE001 - the server's evidence decides
                    self.log(f"    Starting it on the plug: {exc}")
                    return
        threading.Thread(target=fire, daemon=True).start()

    def _plug_back(self, seconds: float) -> bool:
        """Whether the plug answers within `seconds`."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._plug_answers():
                return True
            time.sleep(1.0)
        return False

    def _plug_answers(self) -> bool:
        try:
            return self.device.answers(timeout=3.0)
        except Exception:  # noqa: BLE001 - not answering, however it fails
            return False

    def _wait(self, phase, process, exec_timeout, get_heard) -> int:
        started = time.monotonic()
        checked = 0.0
        while True:
            try:
                return process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            if now - started > exec_timeout:
                raise PhaseError(f"{phase} did not complete within {exec_timeout / 60:.0f} minutes")
            # Quiet since the plug last reached this computer, or since the
            # start last went - not while a start that never reached the plug
            # waits to go again: seen in the fault suite, a blip took the
            # plug off its Wi-Fi for 18 s, and this check failed the step
            # just before the start went again.
            quiet_since = max(get_heard(), self._started_at or 0.0)
            if (not self._resending and now - quiet_since >= SILENCE_CHECK
                    and now - checked >= SILENCE_CHECK):
                checked = now
                if self._plug_answers() and process.poll() is None:
                    raise PhaseError(
                        f"the plug stopped the {phase} without reaching this computer"
                    )

    def _drive(self, phase, process, captured, reader, confirm, exec_timeout,
               get_url, get_arm, get_heard=time.monotonic) -> int:
        deadline = 30.0
        waited = 0.0
        while get_url() is None and waited < deadline and process.poll() is None:
            reader.join(0.5)
            waited += 0.5
        url = get_url()
        if url is None:
            raise PhaseError(
                f"{phase} server did not print a loader URL:\n" + "\n".join(captured)
            )

        if phase in ARMED_PHASES:
            while get_arm() is None and process.poll() is None and waited < deadline + 10.0:
                reader.join(0.5)
                waited += 0.5
            arm = get_arm()
            if arm is None:
                raise PhaseError(f"{phase} server did not print an arm command")

            closure = CLOSURE_NAME[phase]
            self.log("Sending the instructions to the plug. This writes nothing yet.")
            self.device.berry_fire(short_loader(url, assign_to=closure), timeout=60.0)
            self._require_fetch(process, captured, reader, phase)

            if not confirm(phase):
                raise PhaseError(f"{phase} was not confirmed; nothing was armed")

            name, token = arm
            self.log("Telling the plug to start. Leave it powered and connected.")
            self._start(f'{name}("{token}")')
        else:
            self.log("Sending the instructions to the plug and starting.")
            self._start(short_loader(url))
            self._require_fetch(process, captured, reader, phase)

        return self._wait(phase, process, exec_timeout, get_heard)
