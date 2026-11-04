"""The website at firmwareliberator.com (website/), as far as Python can see it:
the one line's redirects, against what each release carries."""

import unittest
from pathlib import Path

from tools.repo.github_settings import REPOSITORY

ROOT = Path(__file__).resolve().parents[2]


class SiteTests(unittest.TestCase):
    def test_each_redirect_names_a_file_every_release_carries(self):
        # The README's one line fetches firmwareliberator.com/liberate.sh: _redirects
        # sends it on to that file of the latest release, so the file must be one
        # the release workflow uploads.
        release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        uploaded = release.split("gh release create", 1)[1].split()
        latest = f"https://github.com/{REPOSITORY}/releases/latest/download/"
        redirects = (ROOT / "website/public/_redirects").read_text(encoding="utf-8")
        lines = [line.split() for line in redirects.splitlines() if line.strip() and not line.startswith("#")]
        self.assertEqual({source for source, *_ in lines}, {"/liberate.sh", "/liberate.ps1"})
        for source, target, status in lines:
            self.assertEqual(target, latest + source.lstrip("/"))
            self.assertIn(source.lstrip("/"), uploaded)
            self.assertEqual(status, "302", "a permanent redirect would be cached past the next release")

    def test_the_site_links_to_this_repository(self):
        site = (ROOT / "website/src/site.mjs").read_text(encoding="utf-8")
        self.assertIn(f'export const REPOSITORY = "{REPOSITORY}";', site)


if __name__ == "__main__":
    unittest.main()
