#!/usr/bin/env python3
"""Keep docs/PublicSponsor.png the same as GitHub's own Public Sponsor badge.

    python3 tools/repo/sponsor_badge.py [PROFILE]   (default: adamsthws)

Finds the badge on PROFILE's achievements page - its address changes with
the image - downloads it, and writes it over docs/PublicSponsor.png if it
differs, printing where it came from. Used by
.github/workflows/sponsor-badge.yml, which opens a pull request with it.
"""

from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BADGE = ROOT / "docs" / "PublicSponsor.png"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_BYTES = 1 << 20
BADGE_URL = re.compile(r'https://github\.githubassets\.com/assets/public-sponsor-default-[0-9a-f]+\.png')


def badge_url(page: str) -> str:
    """The badge's address on an achievements page."""
    match = BADGE_URL.search(page)
    if not match:
        raise ValueError("no Public Sponsor badge on the page: has the profile lost it, or the page changed?")
    return match.group(0)


def checked_png(data: bytes) -> bytes:
    """The data, if it is a PNG of a believable size."""
    if not data.startswith(PNG_SIGNATURE) or len(data) > MAX_BYTES:
        raise ValueError("the badge downloaded is not a PNG of under 1 MB")
    return data


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "sponsor-badge-check"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read(MAX_BYTES + 1)


def main(argv: list[str]) -> int:
    profile = argv[0] if argv else "adamsthws"
    try:
        page = fetch(f"https://github.com/{profile}?tab=achievements").decode("utf-8", "replace")
        url = badge_url(page)
        data = checked_png(fetch(url))
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    if BADGE.read_bytes() == data:
        print(f"Unchanged: {url}")
    else:
        BADGE.write_bytes(data)
        print(f"Changed: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
