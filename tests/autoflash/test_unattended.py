"""Unattended runs: answers given in advance, and the screen kept for review."""

import io
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.repo import unattended_run
from tools.autoflash import credentials, output, prompts

ROOT = Path(__file__).resolve().parents[2]


class CredentialsFileTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(credentials._values.clear)
        self.addCleanup(credentials.renamed.clear)
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in [*credentials.KEYS, *credentials.RENAMED]:
            os.environ.pop(key, None)

    def load(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(textwrap.dedent(text), encoding="utf-8")
            return credentials.load(path)

    def test_shell_style_lines_are_read(self):
        given = self.load("""
            # the account
            export EWELINK_EMAIL = me@example.com
            EWELINK_PASSWORD="pass word#1"
            AUTOFLASH_SUDO_PASSWORD=
        """)
        self.assertEqual(given, ["EWELINK_EMAIL", "EWELINK_PASSWORD"])
        self.assertEqual(credentials.get("EWELINK_PASSWORD"), "pass word#1")
        self.assertIsNone(credentials.get("AUTOFLASH_SUDO_PASSWORD"))  # empty is not given

    def test_the_wifi_for_a_plug_on_esphome_can_be_given(self):
        given = self.load("AUTOFLASH_WIFI_SSID=Home-2G\nAUTOFLASH_WIFI_PASSWORD=a pass\n")
        self.assertEqual(given, ["AUTOFLASH_WIFI_PASSWORD", "AUTOFLASH_WIFI_SSID"])
        self.assertEqual(credentials.get("AUTOFLASH_WIFI_PASSWORD"), "a pass")

    def test_the_environment_wins_over_the_file(self):
        self.load("EWELINK_EMAIL=file@example.com\n")
        os.environ["EWELINK_EMAIL"] = "shell@example.com"
        self.assertEqual(credentials.get("EWELINK_EMAIL"), "shell@example.com")

    def test_a_misspelt_name_is_refused_rather_than_ignored(self):
        with self.assertRaisesRegex(ValueError, "EWELINK_PASSWRD"):
            self.load("EWELINK_PASSWRD=x\n")
        with self.assertRaisesRegex(ValueError, "line 1"):
            self.load("just a password\n")

    def test_no_default_file_is_fine_but_a_named_one_must_exist(self):
        with mock.patch.object(credentials, "ENV_FILE", Path("/nonexistent/.env")):
            self.assertEqual(credentials.load(), [])
        with self.assertRaisesRegex(ValueError, "does not exist"):
            credentials.load(Path("/nonexistent/.env"))

    def test_the_passwords_are_kept_out_of_the_log(self):
        self.addCleanup(output._private.clear)
        self.load("EWELINK_PASSWORD=hunter2-secret\nAUTOFLASH_SUDO_PASSWORD=sudo-secret\n")
        self.assertEqual(output.scrub("x hunter2-secret sudo-secret"),
                         "x <ewelink-password> <sudo-password>")

    def test_a_dotenv_with_the_names_from_before_the_prefix_still_works(self):
        # Written for a checkout from before AUTOFLASH_: read, and noted for renaming.
        self.addCleanup(output._private.clear)
        given = self.load("EWELINK_EMAIL=me@example.com\nS60_SUDO_PASSWORD=sudo-old-secret\n"
                          "S60_WIFI_SSID=Home-2G\nS60_WIFI_PASSWORD=wifi-old-secret\n")
        self.assertEqual(given, ["AUTOFLASH_SUDO_PASSWORD", "AUTOFLASH_WIFI_PASSWORD",
                                 "AUTOFLASH_WIFI_SSID", "EWELINK_EMAIL"])
        self.assertEqual(credentials.get("AUTOFLASH_SUDO_PASSWORD"), "sudo-old-secret")
        self.assertEqual(credentials.renamed, ["S60_SUDO_PASSWORD", "S60_WIFI_PASSWORD", "S60_WIFI_SSID"])
        self.assertEqual(output.scrub("sudo-old-secret wifi-old-secret"),
                         "<sudo-password> <wifi-password>")  # as private as under the new names

    def test_a_new_name_wins_over_its_old_one_and_the_environment_is_read_too(self):
        self.load("S60_SUDO_PASSWORD=old\nAUTOFLASH_SUDO_PASSWORD=new\n")
        self.assertEqual(credentials.get("AUTOFLASH_SUDO_PASSWORD"), "new")
        os.environ["S60_WIFI_SSID"] = "From-Env"  # exported in the shell under the old name
        self.load("")
        self.assertEqual(credentials.get("AUTOFLASH_WIFI_SSID"), "From-Env")
        self.assertEqual(credentials.renamed, ["S60_WIFI_SSID"])


class UnattendedPromptTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(prompts, "UNATTENDED", True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.screen = io.StringIO()
        stdout = mock.patch("sys.stdout", self.screen)
        stdout.start()
        self.addCleanup(stdout.stop)
        self.addCleanup(output.finish)

    def test_no_key_is_waited_for(self):
        with mock.patch("builtins.input", side_effect=AssertionError("read the keyboard")):
            prompts.wait_for_key()

    def test_a_yes_no_question_takes_its_cautious_default(self):
        self.assertFalse(prompts.ask_yes_no("Carry on anyway", default=False))
        self.assertTrue(prompts.ask_yes_no("Enter it now", default=True))
        self.assertIn("Carry on anyway (y/N, q to quit): n   (unattended)", self.screen.getvalue())

    def test_any_other_question_stops_the_run_and_names_it(self):
        for ask in (lambda: prompts.ask_choice("Which plug", ["one", "two"]),
                    lambda: prompts.ask_yes_no("No default"),
                    lambda: prompts.ask_text("eWeLink account email"),
                    lambda: prompts.ask_secret("Wi-Fi password: ")):
            with self.assertRaises(prompts.NoOneToAnswer):  # a QuitRun: the run stops cleanly
                ask()
        self.assertIn("eWeLink account email", self.screen.getvalue())

    def test_proceed_is_taken_as_typed(self):
        import tools.autoflash.cli as autoflash
        with mock.patch("builtins.input", side_effect=AssertionError("read the keyboard")):
            self.assertTrue(autoflash.require_word("proceed"))
        self.assertIn("PROCEED   (unattended)", self.screen.getvalue())


class GivenPasswordTests(unittest.TestCase):
    def context(self, given, email="me@example.com", given_email=None):
        import tools.autoflash.cli as autoflash
        ctx = SimpleNamespace(_ewelink_password=None, _given_ewelink_password=given,
                              _given_email=given_email, email=lambda: email)
        quiet = mock.patch.object(autoflash, "say")
        quiet.start()
        self.addCleanup(quiet.stop)
        return autoflash, ctx

    def test_it_is_used_once_then_asked_for(self):
        autoflash, ctx = self.context("from-env")
        with mock.patch.object(autoflash, "ask_secret", return_value="typed") as asked:
            self.assertEqual(autoflash.Context.ewelink_env(ctx)["EWELINK_PASSWORD"], "from-env")
            asked.assert_not_called()
            ctx._ewelink_password = None  # eWeLink refused it
            self.assertEqual(autoflash.Context.ewelink_env(ctx)["EWELINK_PASSWORD"], "typed")

    def test_not_for_another_account_than_the_one_given(self):
        autoflash, ctx = self.context("from-env", email="other@example.com",
                                      given_email="me@example.com")
        with mock.patch.object(autoflash, "ask_secret", return_value="typed"):
            self.assertEqual(autoflash.Context.ewelink_env(ctx)["EWELINK_PASSWORD"], "typed")


class SudoPasswordTests(unittest.TestCase):
    def setUp(self):
        import tools.autoflash.cli as autoflash
        self.autoflash = autoflash
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("AUTOFLASH_SUDO_PASSWORD", "AUTOFLASH_EMULATOR_SUDO_PASSWORD"):
            os.environ.pop(key, None)

    def test_a_given_one_sudo_accepts_is_used(self):
        os.environ["AUTOFLASH_SUDO_PASSWORD"] = "right"
        with mock.patch.object(self.autoflash.hostsys, "check_sudo_password", return_value=None), \
                mock.patch.object(self.autoflash, "ask_admin_password") as asked:
            self.assertEqual(self.autoflash.admin_password(), "right")
        asked.assert_not_called()

    def test_unattended_without_one_says_what_to_give(self):
        with mock.patch.object(prompts, "UNATTENDED", True), \
                self.assertRaisesRegex(OSError, "AUTOFLASH_SUDO_PASSWORD"):
            self.autoflash.admin_password()


class ScreenTests(unittest.TestCase):
    def render(self, text, width=20, height=5, colour=True):
        screen = unattended_run.Screen(width, height)
        screen.feed(text)
        return screen.render(colour)

    def test_a_line_redrawn_in_place_shows_its_last_drawing(self):
        self.assertEqual(self.render("Stage 1  underway 3s\rStage 1  complete   \n", colour=False),
                         "Stage 1  complete\n")

    def test_rows_taken_away_are_gone(self):
        text = "kept\r\nnote\r\nquestion: y\r\n" + "\x1b[1F\x1b[2K" * 2 + "after\r\n"
        self.assertEqual(self.render(text, colour=False), "kept\nafter\n")

    def test_the_cursor_cannot_climb_into_the_scrollback(self):
        # Seven rows - six written, and the cursor's - in a window of five:
        # the window's top is row 2, and the cursor stops there.
        text = "".join(f"row {n}\r\n" for n in range(6)) + "\x1b[1F" * 9 + "X"
        self.assertEqual(self.render(text, colour=False).splitlines()[:3], ["row 0", "row 1", "Xow 2"])

    def test_long_lines_wrap_at_the_width(self):
        self.assertEqual(self.render("abcdefgh\r\n", width=5, colour=False), "abcde\nfgh\n")

    def test_colours_are_kept_and_links_left_as_text(self):
        text = ("\x1b[32mComplete!\x1b[0m \x1b]8;;http://x/\x1b\\http://x/\x1b]8;;\x1b\\\r\n")
        self.assertEqual(self.render(text, width=40), "\x1b[0m\x1b[32mComplete!\x1b[0m http://x/\n")


@unittest.skipUnless(hasattr(os, "fork"), "a pseudo-terminal needs Linux or macOS")
class RecordingTests(unittest.TestCase):
    def test_the_screen_a_person_would_see_is_rebuilt(self):
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(ROOT)!r})
            from tools.autoflash import output, prompts
            output.start(None, verbose=False)
            output.stage_begin(1, 1, "Checking the files")
            output.transient_begin()
            prompts.warning_box("Read this first", ["taken away once read"])
            prompts.UNATTENDED = True
            prompts.wait_for_key()
            output.transient_end()
            output.stage_end("complete")
            output.show("Conversion complete!", output.GREEN)
            output.finish()
        """)
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):  # the run gets colour regardless
            code, written = unattended_run.record([sys.executable, "-c", script],
                                                  (80, 24), live=False, timeout=60)
        self.assertEqual(code, 0, written)
        screen = unattended_run.Screen(80, 24)
        screen.feed(written.decode("utf-8", errors="replace"))
        plain = screen.render(colour=False)
        self.assertIn("Checking the files", plain)
        self.assertIn("complete", plain)
        self.assertNotIn("Read this first", plain)
        self.assertIn("\x1b[32mConversion complete!", screen.render())


if __name__ == "__main__":
    unittest.main()
