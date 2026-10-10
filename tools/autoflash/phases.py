"""Drive the reviewed migration phase servers without console pasting.

This starts tools/serve_safeboot_migration.py exactly as the runbook does,
reads the URL and arm token it prints, and delivers the loader to the plug over
Tasmota's ordinary command endpoint.  It changes the transport only: the Berry
that executes on the device is byte-for-byte the reviewed template the server
renders, and the host server remains the sole judge of PASS.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
from pathlib import Path
from typing import Callable

from . import hostsys, output
from .device import Device

ARMED_PHASES = {"commit", "restore"}
CLOSURE_NAME = {"commit": "s60_commit", "restore": "s60_restore"}

LOADER_RE = re.compile(r"s60_urlbeload\('(?P<url>http://[^']+)'\)")
# The server prints instructions for a human pasting into the Berry console.
# This tool delivers the same program over HTTP instead, so echoing them would
# invite someone to run it a second time - next to the irreversible step.
MANUAL_INSTRUCTION_RE = re.compile(
    r"paste this as one line|do not prefix it with|paste the loader|"
    r"^\s*def s60_urlbeload|^\s*s60_(commit|restore)\(|"
    r"loading performs no writes|arm separately with",
    re.IGNORECASE,
)
ARM_RE = re.compile(r"(?P<name>s60_(?:commit|restore))\(\"(?P<token>[^\"]+)\"\)")


class PhaseError(RuntimeError):
    pass


REQUEST_RE = re.compile(r'"(?:GET|POST) (?P<path>/\S*) HTTP')
SECTOR = 4096
# Lines from the server worth showing as they are; everything else is folded
# into a progress line and kept, in full, in the phase's log file.
SHOWN_RE = re.compile(r"RESULT:|REFUS|TIMEOUT|ERROR|Traceback|Stopped", re.IGNORECASE)


class PhaseDisplay:
    """Turns the phase server's request log into a few readable lines."""

    def __init__(self, phase: str, blocks: int | None, write: Callable[[str], None],
                 log: Callable[[str], None]) -> None:
        self.phase = phase
        self.blocks = blocks
        self.write = write
        self.log = log
        self.progress = False
        self.copied = 0
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
            elif path.startswith("/chunk/safeboot/"):
                self.copied += 1
                total = f"/{self.blocks}" if self.blocks else ""
                self._status(f"    Copying the startup firmware: block {self.copied}{total}")
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

    def _blocks(self, phase: str) -> int | None:
        """How many 4 KiB blocks of startup firmware a phase copies, for progress."""
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

    def _command(self, phase: str, extra_flags: list[str]) -> list[str]:
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
            *(["--recovery-from-stdin", "--no-sensitive-captures"] if self.recovery else []),
            *extra_flags,
            unbuffered=True,
        )

    def run(
        self,
        phase: str,
        extra_flags: list[str] | None = None,
        confirm: Callable[[str], bool] | None = None,
        exec_timeout: float = 1800.0,
    ) -> tuple[int, str]:
        """Run one phase end to end.  Returns (exit code, captured output)."""
        if phase in ARMED_PHASES and confirm is None:
            raise PhaseError(f"{phase} requires an explicit confirmation callback")

        process = subprocess.Popen(
            self._command(phase, extra_flags or []),
            cwd=str(self.repo_root),
            stdin=subprocess.PIPE if self.recovery else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        if self.recovery:
            assert process.stdin is not None
            try:
                process.stdin.buffer.write(self.recovery)
                process.stdin.close()
            except BrokenPipeError:
                pass  # the server stopped early; its output says why
        captured: list[str] = []
        url: str | None = None
        arm: tuple[str, str] | None = None
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.evidence_dir / f"{phase}-server.log"
        log_file = log_path.open("w", encoding="utf-8")
        display = PhaseDisplay(
            phase, self._blocks(phase),
            write=output.raw_progress,
            log=self.log,
        )

        def pump() -> None:
            nonlocal url, arm
            assert process.stdout is not None
            for line in process.stdout:
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
                               lambda: url, lambda: arm)
        finally:
            if process.poll() is None:
                process.terminate()
            reader.join(5.0)
            display.close()
            log_file.close()
        if code != 0:
            self.log(f"    The server's full output is in {log_path}")
        return code, "\n".join(captured)

    def _drive(self, phase, process, captured, reader, confirm, exec_timeout,
               get_url, get_arm) -> int:
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
            self.device.berry_fire(f'{name}("{token}")', timeout=60.0)
        else:
            self.log("Sending the instructions to the plug and starting.")
            self.device.berry_fire(short_loader(url), timeout=60.0)
            self._require_fetch(process, captured, reader, phase)

        try:
            return process.wait(timeout=exec_timeout)
        except subprocess.TimeoutExpired as exc:
            raise PhaseError(f"{phase} did not complete within {exec_timeout:.0f}s") from exc
