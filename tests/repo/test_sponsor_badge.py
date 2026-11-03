import unittest

from tools.repo import sponsor_badge


class SponsorBadgeTests(unittest.TestCase):
    def test_the_badge_is_found_on_the_achievements_page(self):
        page = ('<img src="https://github.githubassets.com/assets/public-sponsor-default-9fa68986b057.png" '
                'width="64" alt="Achievement: Public Sponsor">')
        self.assertEqual(sponsor_badge.badge_url(page),
                         "https://github.githubassets.com/assets/public-sponsor-default-9fa68986b057.png")

    def test_a_page_without_it_is_refused(self):
        with self.assertRaises(ValueError):
            sponsor_badge.badge_url('<img src="https://github.githubassets.com/assets/pull-shark-default-1234.png">')

    def test_only_a_png_is_taken(self):
        self.assertEqual(sponsor_badge.checked_png(sponsor_badge.PNG_SIGNATURE + b"x"),
                         sponsor_badge.PNG_SIGNATURE + b"x")
        with self.assertRaises(ValueError):
            sponsor_badge.checked_png(b"<html>Not found</html>")
        with self.assertRaises(ValueError):
            sponsor_badge.checked_png(sponsor_badge.PNG_SIGNATURE + bytes(sponsor_badge.MAX_BYTES))

    def test_the_copy_in_the_repository_is_a_png(self):
        sponsor_badge.checked_png(sponsor_badge.BADGE.read_bytes())


if __name__ == "__main__":
    unittest.main()
