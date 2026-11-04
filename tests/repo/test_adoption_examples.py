"""The guide's configs, as tools/repo/adoption_examples.py hands them to ESPHome
(which the lint runs: Docker is not for the unit tests)."""

import unittest
from pathlib import Path

from tools.repo import adoption_examples, lint

ROOT = Path(__file__).resolve().parents[2]


class AdoptionExamplesTests(unittest.TestCase):
    def guide(self):
        return adoption_examples.examples((ROOT / adoption_examples.GUIDE).read_text(encoding="utf-8"))

    def test_it_finds_the_guides_examples(self):
        found = self.guide()
        self.assertEqual(len(found), 3)
        self.assertIn("packages:", found[0])
        self.assertIn("!include common/sonoff-s60.yaml", found[1])
        self.assertIn("!extend relay", found[2])

    def test_a_package_comes_from_this_checkout_at_any_release(self):
        config = adoption_examples.from_this_checkout(
            "  plug: github://adamsthws/firmware-liberator/devices/sonoff-s60/esphome.yaml@v2.3.0\n")
        self.assertEqual(config, "  plug: !include /config/devices/sonoff-s60/esphome.yaml\n")

    def test_the_guides_placeholder_key_is_replaced_by_one_of_a_real_keys_length(self):
        config = adoption_examples.from_this_checkout(f'key: "{adoption_examples.KEY}"')
        self.assertEqual(config, f'key: "{adoption_examples.MADE_UP_KEY}"')
        self.assertEqual(len(adoption_examples.MADE_UP_KEY), 44)

    def test_every_plug_gets_a_short_config(self):
        self.assertEqual(adoption_examples.short_configs(["sonoff-s60", "sonoff-s61"]),
                         {"short-sonoff-s60.yaml":
                          "packages:\n  plug: !include /config/devices/sonoff-s60/esphome.yaml\n",
                          "short-sonoff-s61.yaml":
                          "packages:\n  plug: !include /config/devices/sonoff-s61/esphome.yaml\n"})

    def test_the_lint_runs_it_for_the_guide_and_the_configs_it_builds_on(self):
        for path in ("docs/home-assistant-adoption.md", "devices/sonoff-s60/esphome.yaml"):
            self.assertTrue(lint.adoption_example(Path(path)), path)
        for path in ("docs/testing.md", "devices/sonoff-s60/mover.yaml", "esphome.yaml"):
            self.assertFalse(lint.adoption_example(Path(path)), path)

    def test_a_further_mount_goes_to_docker_before_the_image(self):
        command = lint.esphome_command(["/examples/a.yaml"], mounts=["/tmp/x:/examples:ro"])
        self.assertLess(command.index("/tmp/x:/examples:ro"), command.index(lint.esphome_pins()["image"]["builder"]))
        self.assertEqual(command[-1], "/examples/a.yaml")
