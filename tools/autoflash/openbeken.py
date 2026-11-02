"""OpenBeken on a converted plug: what the tool asks of it over HTTP.

OpenBeken answers on port 80: GET /api/info says what it is (its chipset,
build, name and MAC); POST /api/cmnd runs one of its console commands and
answers with a JSON envelope; GET /api/flash/<start>-<length>, both in hex,
reads flash as raw bytes (the S61 build reads the whole 4 MB).  Every
address and command goes to a plug that has already answered as OpenBeken.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import kinds

PORT = 80
# One request per 16 KiB, paced: the plug's HTTP server is single-threaded and
# its TCP stack keeps each closed connection a while, so few, larger, spaced
# requests are what it copes with (tools/beken/backup.py found the pace).
FLASH_CHUNK = 0x4000
FLASH_PAUSE = 1.0
FLASH_TRIES = 4


class CommandError(RuntimeError):
    """OpenBeken refused a command, or did not know it."""


def _url(host: str, path: str) -> str:
    return f"http://{host}:{PORT}{path}" if ":" not in host else f"http://{host}{path}"


def _get(host: str, path: str, timeout: float) -> bytes:
    with urllib.request.urlopen(_url(host, path), timeout=timeout) as reply:
        return reply.read()


def info(host: str, timeout: float = 4.0) -> dict[str, Any] | None:
    """What /api/info says, or None where OpenBeken does not answer there."""
    try:
        found = json.loads(_get(host, "/api/info", timeout))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return found if isinstance(found, dict) and "chipset" in found and "build" in found else None


def kind(host: str, found: dict[str, Any], timeout: float = 4.0) -> kinds.Kind | None:
    """The kind of plug the OpenBeken at `host` (its /api/info `found`) is
    on, when the tool installed it: its import module answers AutoflashKind
    for its own kind alone - its name says nothing, being alike on every
    firmware and the user's to change.  None for an OpenBeken the tool did
    not install, which knows no such command, or one that cannot say."""
    for candidate in kinds.on_chip(str(found.get("chipset") or "")):
        try:
            command(host, f"AutoflashKind {candidate.key}", timeout)
        except (CommandError, OSError):
            continue
        return candidate
    return None


def is_interim(host: str, timeout: float = 4.0) -> bool | None:
    """Whether the OpenBeken at `host` - one the tool installed (kind) - is
    the interim firmware: its import module answers AutoflashInterim for
    that alone (an OpenBeken whose module predates it does not know the
    command).  None where it does not answer."""
    try:
        command(host, "AutoflashInterim", timeout)
    except CommandError:
        return False
    except OSError:
        return None
    return True


def set_interim(host: str, interim: bool, timeout: float = 10.0) -> None:
    """The OpenBeken at `host` made the interim firmware (autoflash-recovery-CDEF),
    or the plug's OpenBeken for good (openbeken-abcdef), as its import module
    names each; its DHCP and mDNS names follow on its next start."""
    command(host, f"AutoflashInterim {'on' if interim else 'off'}", timeout)


def is_openbeken(host: str, timeout: float = 4.0) -> bool:
    return info(host, timeout) is not None


def version(found: dict[str, Any]) -> str:
    """The version in the build string OpenBeken reports ("... version 1.18.320")."""
    build = str(found.get("build", ""))
    return build.rsplit("version ", 1)[1].strip() if "version " in build else build


def command(host: str, text: str, timeout: float = 10.0) -> str:
    """Run a console command, and give what it answered (the envelope's `msg`)."""
    request = urllib.request.Request(_url(host, "/api/cmnd"), data=text.encode("utf-8"), method="POST",
                                     headers={"Content-Type": "text/plain"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            body = reply.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
    try:
        envelope = json.loads(body)
    except ValueError:
        return body.strip()
    if not isinstance(envelope, dict):
        return body.strip()
    if "error" in envelope:
        raise CommandError(f"{text.split()[0]}: {envelope.get('msg') or envelope['error']}"
                           + (" (not a command this OpenBeken knows)" if envelope["error"] == 501 else ""))
    return str(envelope.get("msg", ""))


def channels(host: str, timeout: float = 4.0) -> dict[str, int]:
    """Each channel's value, by number, as /api/channels gives them."""
    found = json.loads(_get(host, "/api/channels", timeout))
    return {str(k): int(v) for k, v in found.items()} if isinstance(found, dict) else {}


def read_flash(host: str, start: int, length: int, *, timeout: float = 30.0,
               progress=None) -> bytes:
    """`length` bytes of flash from `start`, in paced chunks, each tried again
    on a short or failed read."""
    out = bytearray()
    for address in range(start, start + length, FLASH_CHUNK):
        wanted = min(FLASH_CHUNK, start + length - address)
        data = b""
        for attempt in range(FLASH_TRIES):
            try:
                data = _get(host, f"/api/flash/{address:x}-{wanted:x}", timeout)
            except (OSError, urllib.error.URLError):
                data = b""
            if len(data) == wanted:
                break
            time.sleep(FLASH_PAUSE * (attempt + 1))
        if len(data) != wanted:
            raise OSError(f"short read of the device's flash at 0x{address:x}: {len(data)} of {wanted} bytes")
        out += data
        if progress:
            progress(len(out), length)
        if address + wanted < start + length:
            time.sleep(FLASH_PAUSE)
    return bytes(out)


# How long OpenBeken takes to restart and answer again.
RESTART_WAIT = 120.0


def restart(host: str, wait: float = RESTART_WAIT) -> bool:
    """Restart OpenBeken, and say whether it has, answering again, within
    `wait` seconds: its uptime back below what it was."""
    before = (info(host) or {}).get("uptime_s")
    try:
        command(host, "reboot", timeout=5.0)
    except (OSError, CommandError):
        pass   # the plug may restart before it answers
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(3)
        found = info(host, timeout=3.0)
        if found and (not isinstance(before, int) or int(found.get("uptime_s") or 0) < before):
            return True
    return False


def ota_http(host: str, url: str, timeout: float = 15.0) -> str:
    """Have OpenBeken download the rt_ota image at `url` into its OTA slot - the
    stock bootloader's download slot, on the 4 MB-table build - and restart to
    apply it.  The reply comes before the download."""
    return command(host, f"ota_http {urllib.parse.quote(url, safe=':/')}", timeout)
