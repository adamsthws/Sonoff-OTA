"""The docs speak to their reader, not from the project's side
(CONTRIBUTING.md, Writing docs).  Released changelog sections are history, so
of CHANGELOG.md only the top section is read."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIRST_PERSON = re.compile(r"\b(?:[Ww]e|[Oo]urs?|us)\b|\b[Ff]rom here\b")


def top_section(changelog: str) -> str:
    return "## " + changelog.split("\n## ")[1]


def docs() -> dict[str, str]:
    pages = [ROOT / "README.md", ROOT / "artifacts/README.md", ROOT / "website/README.md",
             *(ROOT / "docs").rglob("*.md"), *(ROOT / "devices").rglob("*.md")]
    found = {page.relative_to(ROOT).as_posix(): page.read_text(encoding="utf-8") for page in pages}
    found["CHANGELOG.md"] = top_section((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    return found


class DocsVoiceTests(unittest.TestCase):
    def test_no_page_speaks_from_the_projects_side(self):
        for name, text in docs().items():
            for number, line in enumerate(text.splitlines(), 1):
                with self.subTest(page=name, line=number):
                    self.assertIsNone(FIRST_PERSON.search(line), line)

    def test_it_catches_the_wording_and_leaves_alike_words(self):
        for line in ("What we ask of you", "changes nothing of ours", "fetches nothing from here", "Our config"):
            self.assertTrue(FIRST_PERSON.search(line), line)
        for line in ("US law: 17 U.S.C.", "yours to change", "your plug", "colours", "hours"):
            self.assertFalse(FIRST_PERSON.search(line), line)

    def test_only_the_changelogs_top_section_counts(self):
        self.assertEqual(top_section("# Changelog\n\n## 2.0.0\nnew\n\n## 1.0.0\nwe did\n"), "## 2.0.0\nnew\n")
