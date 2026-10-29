#!/usr/bin/env python3
"""The tool's version, and its release notes from CHANGELOG.md.

    python3 tools/repo/release_notes.py --version   the version, as set in tools/autoflash
    python3 tools/repo/release_notes.py [VERSION]   that version's notes (default: the current one)

Used by .github/workflows/release.yml, so the tag and the release are made from
the version in the code and the notes written for it - nothing typed twice.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.autoflash import __version__  # noqa: E402

CHANGELOG = ROOT / "CHANGELOG.md"


def notes(version: str, changelog: str) -> str:
    """The text under `## <version>`, up to the next version's heading."""
    lines = changelog.splitlines()
    heading = f"## {version}"
    if heading not in lines:
        raise ValueError(f"CHANGELOG.md has no '{heading}' section")
    section = []
    for line in lines[lines.index(heading) + 1:]:
        if line.startswith("## "):
            break
        section.append(line)
    text = "\n".join(section).strip()
    if not text:
        raise ValueError(f"CHANGELOG.md's '{heading}' section is empty")
    return text + "\n"


def main(argv: list[str]) -> int:
    if argv == ["--version"]:
        print(__version__)
        return 0
    version = argv[0] if argv else __version__
    try:
        sys.stdout.write(notes(version, CHANGELOG.read_text(encoding="utf-8")))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
