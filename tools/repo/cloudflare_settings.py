#!/usr/bin/env python3
"""The website's settings on Cloudflare, written down here with why.

    python3 tools/repo/cloudflare_settings.py          what differs from them
    python3 tools/repo/cloudflare_settings.py apply    set them

With CLOUDFLARE_API_TOKEN, in the environment or .env, a token allowed to
read the zone and edit its settings and its single redirects. `apply` changes only what differs.
The Worker serving the site, and the domains pointing at it, are
wrangler.jsonc's, which the website workflow deploys.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.autoflash import credentials  # noqa: E402

ZONE = "firmwareliberator.com"
API = "https://api.cloudflare.com/client/v4"

# Plain HTTP is answered with a redirect to HTTPS, and TLS older than 1.2,
# which every browser of the last decade outgrew, is refused.
SETTINGS = {"always_use_https": "on", "min_tls_version": "1.2"}

# www.firmwareliberator.com, which wrangler.jsonc also points at the Worker,
# is sent on to firmwareliberator.com, keeping the path and query, so the
# site has one address. Cloudflare runs this before the Worker.
REDIRECT_PHASE = "http_request_dynamic_redirect"
REDIRECTS = [{
    "ref": "www-to-apex",
    "description": f"www.{ZONE} to {ZONE}",
    "expression": f'(http.host eq "www.{ZONE}")',
    "action": "redirect",
    "action_parameters": {"from_value": {
        "status_code": 301,
        "target_url": {"expression": f'concat("https://{ZONE}", http.request.uri.path)'},
        "preserve_query_string": True,
    }},
    "enabled": True,
}]


class SettingsError(RuntimeError):
    pass


def token() -> str:
    value = os.environ.get("CLOUDFLARE_API_TOKEN")
    if value:
        return value
    try:
        env_file = credentials.parse(credentials.ENV_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        env_file = {}
    value = env_file.get("CLOUDFLARE_API_TOKEN")
    if not value:
        raise SettingsError("no CLOUDFLARE_API_TOKEN: in the environment or .env")
    return value


def api(path: str, method: str = "GET", body: Any = None, ok_missing: bool = False) -> Any:
    request = urllib.request.Request(
        API + path, method=method, data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)["result"]
    except urllib.error.HTTPError as error:
        if ok_missing and error.code == 404:
            return None
        detail = error.read().decode("utf-8", "replace")
        try:
            detail = "; ".join(e.get("message", "") for e in json.loads(detail)["errors"]) or detail
        except (ValueError, KeyError, TypeError):
            pass
        raise SettingsError(f"{method} {path}: HTTP {error.code}: {detail}") from None
    except OSError as error:
        raise SettingsError(f"{method} {path}: {error}") from None


def comparable(rule: dict[str, Any]) -> dict[str, Any]:
    """What a rule says, as REDIRECTS writes it, from what Cloudflare returns."""
    return {key: rule.get(key) for key in REDIRECTS[0]}


def differences() -> list[tuple[str, str, Any]]:
    """(what, how it differs, the change that makes it right)."""
    zones = api(f"/zones?name={ZONE}")
    if not zones:
        raise SettingsError(f"the token cannot see the zone {ZONE}")
    zone = f"/zones/{zones[0]['id']}"
    found: list[tuple[str, str, Any]] = []
    for key, want in SETTINGS.items():
        have = api(f"{zone}/settings/{key}")["value"]
        if have != want:
            found.append((key, f"is {have}, should be {want}", ("PATCH", f"{zone}/settings/{key}", {"value": want})))
    entrypoint = api(f"{zone}/rulesets/phases/{REDIRECT_PHASE}/entrypoint", ok_missing=True)
    have = [comparable(rule) for rule in (entrypoint or {}).get("rules") or []]
    if have != REDIRECTS:
        found.append(("redirects", f"are {[rule['ref'] for rule in have]}, should be {[r['ref'] for r in REDIRECTS]}",
                      ("PUT", f"{zone}/rulesets/phases/{REDIRECT_PHASE}/entrypoint", {"rules": REDIRECTS})))
    return found


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args not in ([], ["apply"]):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        found = differences()
        if not found:
            print(f"{ZONE}: every setting is as tools/repo/cloudflare_settings.py says.")
            return 0
        for what, how, _ in found:
            print(f"  {what}: {how}")
        if not args:
            print("python3 tools/repo/cloudflare_settings.py apply sets them.")
            return 1
        for what, _, (method, path, body) in found:
            api(path, method, body)
            print(f"  {what}: set")
    except SettingsError as error:
        print(f"cloudflare_settings: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
