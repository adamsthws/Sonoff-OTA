"""The web page at firmwareliberator.com, as _config.yml says what it shows."""

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# What the site is meant to serve, at the top level and under archive/: the
# README, the docs and what they link to, and the site's own files. Jekyll
# leaves out dot-files on its own. Anything else tracked must be excluded.
SITE = {"README.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md", "LICENSE", "THIRD_PARTY_NOTICES.md",
        "CNAME", "docs", "archive", "assets", "_config.yml", "_layouts", "404.md", "robots.txt"}
ARCHIVE_SITE = {"docs", "README.md"}


def excluded() -> list[str]:
    """The exclude list of _config.yml, as written."""
    paths, in_list = [], False
    for line in (ROOT / "_config.yml").read_text(encoding="utf-8").splitlines():
        if line.startswith("exclude:"):
            in_list = True
        elif in_list and line.startswith("  - "):
            paths.append(line[4:].strip())
        elif in_list and line.strip():
            in_list = False
    return paths


def tracked(directory: str = "") -> set[str]:
    """The names directly under a tracked directory."""
    out = subprocess.run(["git", "ls-files", "--", directory or "."], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout
    depth = len(Path(directory).parts)
    return {Path(path).parts[depth] for path in out.splitlines()}


class SiteTests(unittest.TestCase):
    def test_every_excluded_path_exists(self):
        for path in excluded():
            self.assertTrue((ROOT / path).exists(), f"_config.yml excludes {path}, which is gone")

    def test_everything_tracked_is_on_the_site_or_excluded(self):
        off = {path.rstrip("/") for path in excluded()}
        for name in sorted(tracked()):
            if name.startswith("."):
                continue
            self.assertTrue(name in SITE or name in off, f"{name} is neither meant for the site nor excluded")
        for name in sorted(tracked("archive")):
            self.assertTrue(name in ARCHIVE_SITE or f"archive/{name}" in off,
                            f"archive/{name} is neither meant for the site nor excluded")

    def test_nothing_meant_for_the_site_is_excluded(self):
        off = {path.rstrip("/") for path in excluded()}
        self.assertEqual(off & SITE, set())
        self.assertEqual(off & {f"archive/{name}" for name in ARCHIVE_SITE}, set())


if __name__ == "__main__":
    unittest.main()
