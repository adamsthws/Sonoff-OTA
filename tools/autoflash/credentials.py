"""Answers given in advance, for a run nobody sits through.

A `.env` file at the top of the checkout (git ignores it; `.env.example` shows
the keys) holds the eWeLink account, and optionally this computer's sudo
password, so `--unattended` can run a whole conversion.  The same names set in
the environment win over the file.

What is read here stays in this process: it is not added to the environment
the helper tools inherit, and each secret is kept out of the run's log.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import output

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"

# The names understood, and the label each secret is logged as.
KEYS = {
    "EWELINK_EMAIL": None,  # an email is scrubbed from the log anyway
    "EWELINK_PASSWORD": "ewelink-password",
    "S60_SUDO_PASSWORD": "sudo-password",
    # Only for a plug on an ESPHome that cannot say its Wi-Fi password (one
    # older than this project's 1.1.0, or whose API has an encryption key):
    # no password given means an open network.
    "S60_WIFI_SSID": "wifi-name",
    "S60_WIFI_PASSWORD": "wifi-password",
}

_values: dict[str, str] = {}


def parse(text: str) -> dict[str, str]:
    """KEY=VALUE lines, as a shell would accept them: # comments, blank lines,
    an optional `export `, and a value optionally in matching quotes."""
    values = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, equals, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not equals or not key:
            raise ValueError(f"line {number} is not KEY=VALUE")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def load(path: Path | None = None) -> list[str]:
    """Read `path` (default: .env, if there is one).  Returns the names it
    set; raises ValueError for a file that cannot be used."""
    explicit = path is not None
    path = path or ENV_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if explicit:
            raise ValueError(f"{path} does not exist") from None
        text = ""
    except OSError as exc:
        raise ValueError(f"{path} could not be read: {exc}") from None
    try:
        values = parse(text)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from None
    unknown = sorted(set(values) - set(KEYS))
    if unknown:
        raise ValueError(f"{path}: unknown name{'s' if len(unknown) > 1 else ''} "
                         f"{', '.join(unknown)} (known: {', '.join(KEYS)})")
    _values.clear()
    _values.update({key: value for key, value in values.items() if value})
    # Those set in the environment too: they are just as private.
    for key, label in KEYS.items():
        if label and get(key):
            output.private(get(key), label)
    return sorted(_values)


def get(key: str) -> str | None:
    """The value given for `key`: the environment's, else the file's."""
    return os.environ.get(key) or _values.get(key) or None
