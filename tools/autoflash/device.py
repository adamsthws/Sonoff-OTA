"""Minimal Tasmota HTTP client used to drive a plug without console pasting.

Only stdlib.  Every method is deliberately explicit about timeouts, because the
long Berry phases hold the HTTP request open until they finish and a timeout is
an expected outcome rather than an error.
"""

from __future__ import annotations

import http.client
import json
import mimetypes
import re
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class DeviceError(RuntimeError):
    pass


# A plug is on the LAN.  Never let http_proxy/HTTPS_PROXY from the environment
# capture these requests: a proxy cannot route to 192.168.x.x and the failure
# looks exactly like the device being absent.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Device:
    def __init__(
        self,
        host: str,
        user: str | None = None,
        password: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.host = host
        self.user = user
        self.password = password
        self.timeout = timeout

    # -- plumbing -----------------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://{self.host}"

    def _auth_params(self) -> dict[str, str]:
        if self.user and self.password:
            return {"user": self.user, "password": self.password}
        return {}

    def _get(self, path: str, params: dict[str, str], timeout: float) -> str:
        query = urllib.parse.urlencode({**self._auth_params(), **params})
        url = f"{self.base_url}{path}?{query}" if query else f"{self.base_url}{path}"
        # Tasmota refuses /cm requests with no Referer as CSRF protection and
        # answers by closing the connection, which looks exactly like the device
        # being absent.  Presenting the device's own page satisfies the check
        # without asking the operator to set SetOption128.
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "S60Autoflash/1",
                "Referer": f"{self.base_url}/",
            },
        )
        try:
            with _OPENER.open(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise DeviceError(
                    f"{self.host} refused the request (HTTP {exc.code}); "
                    "the web UI has a password - pass --web-user/--web-password"
                ) from exc
            raise

    # -- commands -----------------------------------------------------------

    def console_log(self, timeout: float | None = None) -> list[str]:
        """The lines in Tasmota's web console log, oldest first.

        The page answers only requests with a Referer, which _get sends.
        """
        body = self._get("/cs", {"c2": "0"}, timeout or self.timeout)
        # Framed as "<index>}1<reset>}1<lines>}1": only the lines are the log.
        parts = body.split("}1", 2)
        if len(parts) == 3:
            body = parts[2].removesuffix("}1")
        return [line.strip() for line in re.split(r"[\x1e\n]", body) if line.strip()]

    def command(self, command: str, timeout: float | None = None) -> Any:
        """Run one Tasmota console command and return parsed JSON when possible."""
        body = self._get("/cm", {"cmnd": command}, timeout or self.timeout)
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return body.strip()

    @staticmethod
    def _reject_unknown_command(result: Any) -> Any:
        if isinstance(result, dict):
            for value in result.values():
                if isinstance(value, str) and value.strip().lower() == "unknown":
                    raise DeviceError(
                        "this firmware has no Berry console - Safeboot and other "
                        "minimal builds cannot run these checks"
                    )
        return result

    def berry(self, code: str, timeout: float | None = None) -> Any:
        """Run one Berry statement or expression through the ordinary console.

        `Br` is the console prefix for Berry; the Berry scripting console is not
        involved and nothing is pasted by hand.
        """
        return self._reject_unknown_command(self.command(f"Br {code}", timeout=timeout))

    def berry_fire(self, code: str, timeout: float = 15.0) -> bool:
        """Start long-running Berry and tolerate the request timing out.

        Berry runs synchronously in Tasmota's main loop, so a phase that takes
        minutes will not answer its HTTP request even though it is running and
        yielding.  Completion is detected from the host phase server's evidence,
        never from this response.  Returns True when a response did arrive.

        A phase that ends by restarting the plug (the commit does) cuts its own
        reply off part way, which is the same: no response.
        """
        try:
            self.berry(code, timeout=timeout)
            return True
        except (urllib.error.URLError, socket.timeout, TimeoutError,
                ConnectionError, http.client.HTTPException):
            return False

    def berry_value(self, code: str, name: str, timeout: float = 120.0) -> str:
        """Run `code` (which must assign the global `name`), then read it back."""
        self.berry(code, timeout=timeout)
        result = self.berry(name, timeout=self.timeout)
        if isinstance(result, dict):
            for key in ("Br", "BrResult", "berry"):
                if key in result:
                    return str(result[key]).strip()
            if len(result) == 1:
                return str(next(iter(result.values()))).strip()
        return str(result).strip()

    def sha_region(self, address: int, length: int, timeout: float = 180.0) -> str:
        """SHA-256 of a flash region, computed on the device in 4 KiB reads."""
        code = (
            "import flash import crypto var _h=crypto.SHA256() var _o=0 "
            f"while _o<{length} var _c={length}-_o if _c>4096 _c=4096 end "
            f"_h.update(flash.read({address}+_o,_c)) _o+=_c tasmota.yield() end "
            "_s=_h.out().tohex()"
        )
        digest = self.berry_value(code, "_s", timeout=timeout).lower().strip('"')
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise DeviceError(
                f"expected a SHA-256 from the plug, got {digest[:40]!r} - the "
                "running firmware probably cannot execute Berry"
            )
        return digest

    # -- state --------------------------------------------------------------

    def firmware_version(self) -> str:
        """Version string, from the command endpoint or failing that the UI.

        Minimal builds such as Safeboot do not serve /cm, so a version read that
        only knows about /cm reports nothing and the caller sees silence rather
        than an answer.
        """
        try:
            status = self.command("Status 2")
        except (DeviceError, urllib.error.URLError, OSError):
            status = None
        if isinstance(status, dict):
            version = str(status.get("StatusFWR", {}).get("Version", ""))
            if version:
                return version
        return self.version_from_page()

    def version_from_page(self) -> str:
        """Whatever the web UI says about itself, including 'safeboot'."""
        try:
            page = self._get("/", {}, self.timeout)
        except (DeviceError, urllib.error.URLError, OSError):
            return ""
        found = re.search(r"(\d+\.\d+\.\d+[^<\s]*)", page)
        version = found.group(1) if found else ""
        if "safeboot" in page.lower() and "safeboot" not in version.lower():
            version = (version + " (safeboot)").strip()
        return version

    def uptime_seconds(self) -> int | None:
        status = self.command("Status 11")
        if isinstance(status, dict):
            value = status.get("StatusSTS", {}).get("UptimeSec")
            if isinstance(value, int):
                return value
        return None

    def started_at(self) -> str | None:
        """Which boot this is: the plug's own boot time and boot count.

        A restart changes it whatever the uptime happened to be before, so it
        catches a restart that follows closely on another, which an uptime
        comparison misses.  The count covers a plug with no clock, whose boot
        time would read the same every time.
        """
        status = self.command("Status 1")
        if isinstance(status, dict):
            prm = status.get("StatusPRM", {})
            if prm.get("StartupUTC") or prm.get("BootCount") is not None:
                return f"{prm.get('StartupUTC')}#{prm.get('BootCount')}"
        return None

    def is_safeboot(self) -> bool:
        if "safeboot" in self.firmware_version().lower():
            return True
        try:
            return "safeboot" in self._get("/", {}, self.timeout).lower()
        except (DeviceError, urllib.error.URLError, OSError):
            return False

    def current_ota(self, timeout: float = 15.0) -> int | None:
        """Running OTA slot, or None when Berry is unavailable (e.g. Safeboot)."""
        try:
            value = self.berry_value(
                "import flash _s=str(flash.current_ota())", "_s", timeout=timeout
            )
            return int(value.strip().strip('"'))
        except (ValueError, DeviceError, urllib.error.URLError, socket.timeout, TimeoutError):
            return None

    def reachable(self, timeout: float = 3.0) -> bool:
        """True when the device answered at all, including refusing us.

        HTTPError is a subclass of URLError, so a password-protected plug
        answering 401 must be treated as present rather than absent.
        """
        try:
            self._get("/cm", {"cmnd": "Status 0"}, timeout)
            return True
        except urllib.error.HTTPError:
            return True
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError):
            return False

    def wait_reachable(self, timeout: float = 180.0, poll: float = 3.0) -> float:
        """Block until the plug answers.  Returns seconds waited."""
        started = time.monotonic()
        deadline = started + timeout
        while time.monotonic() < deadline:
            if self.reachable():
                return time.monotonic() - started
            time.sleep(poll)
        raise DeviceError(f"{self.host} did not answer within {timeout:.0f}s")

    def wait_for_reboot(
        self,
        timeout: float = 240.0,
        settle: float = 5.0,
        before: int | None = None,
        started_at: str | None = None,
    ) -> float:
        """Wait for the plug to restart.  Returns seconds waited.

        A restart is confirmed by uptime going backwards, not by catching the
        device offline between two pings - a reboot can easily complete inside
        one polling gap, and then nothing looks like it happened.
        """
        started = time.monotonic()
        deadline = started + timeout
        seen_offline = False
        while time.monotonic() < deadline:
            time.sleep(3.0)
            if not self.reachable(timeout=2.0):
                seen_offline = True
                continue
            if started_at is not None:
                try:
                    current = self.started_at()
                except (DeviceError, urllib.error.URLError, OSError):
                    continue
                if current is not None and current != started_at:
                    time.sleep(settle)
                    return time.monotonic() - started
                if before is None:
                    continue  # same boot as before the upload: keep waiting
            now = self.uptime_seconds()
            if before is not None:
                # The reliable signal: the device's clock went backwards.
                if now is not None and now < before:
                    time.sleep(settle)
                    return time.monotonic() - started
                continue
            # No baseline to compare against.  Fall back to having seen it go
            # away, or to a low uptime, rather than returning on the first
            # successful poll - which would prove nothing at all.
            if seen_offline or (now is not None and now < 60):
                time.sleep(settle)
                return time.monotonic() - started
        raise DeviceError(
            f"{self.host} did not restart within {timeout:.0f}s "
            f"(uptime never reset below {before}s)"
        )

    # -- firmware upload ----------------------------------------------------

    def upload_firmware(self, path: Path, timeout: float = 600.0) -> str:
        """POST an application image to Tasmota's or Safeboot's upgrade endpoint."""
        # Mirror what the browser does: fetch the upgrade page first, then post
        # to u2 with the file size in the query.  Tasmota's own form action is
        # "u2?fsz=" and its JavaScript fills the size in; without it the upload
        # is accepted, the device restarts, and nothing is ever committed.
        return self._upload("/up", path.name, path.read_bytes(), timeout)

    def download_settings(self, timeout: float = 30.0) -> bytes:
        """Tasmota's configuration backup (the .dmp file the web UI offers)."""
        query = urllib.parse.urlencode(self._auth_params())
        request = urllib.request.Request(
            f"{self.base_url}/dl" + (f"?{query}" if query else ""),
            headers={"User-Agent": "S60Autoflash/1", "Referer": f"{self.base_url}/"},
        )
        with _OPENER.open(request, timeout=timeout) as response:
            return response.read()

    def restore_settings(self, backup: bytes, timeout: float = 60.0) -> str:
        """Restore a configuration backup; Tasmota checks it, saves it and restarts."""
        # Visiting the restore page is what marks the next upload as settings.
        return self._upload("/rs", "Config.dmp", backup, timeout)

    def _upload(self, page: str, filename: str, payload: bytes, timeout: float) -> str:
        boundary = "----s60autoflash" + secrets.token_hex(8)
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        body = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="u2"; filename="{filename}"\r\n'.encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                payload,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        try:
            self._get(page, {}, self.timeout)
        except (DeviceError, urllib.error.URLError, OSError):
            if page != "/up":
                raise  # a restore must arm the restore page, or the file is taken as firmware
        query = urllib.parse.urlencode({**self._auth_params(), "fsz": str(len(payload))})
        url = f"{self.base_url}/u2?{query}"
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "S60Autoflash/1",
                "Referer": f"{self.base_url}/",
            },
        )
        with _OPENER.open(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
