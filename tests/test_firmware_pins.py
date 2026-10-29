"""firmware.json, the files it pins, and everything that names them, agreeing.

tools/update_firmware.py writes firmware.json and the files; the rest is by
hand (docs/updating-firmware.md), and these say what was missed.
"""

import io
import json
import unittest
from pathlib import Path

from tools import update_firmware as uf
from tools.autoflash import tasmota_settings
from tools.espressif.safeboot.safeboot_migration import OFFICIAL_URLS, PINNED_ARTIFACTS, sha256_bytes

ROOT = Path(__file__).resolve().parents[1]
PINS = json.loads((ROOT / "firmware.json").read_text(encoding="utf-8"))


class TasmotaPinTests(unittest.TestCase):
    def test_the_files_come_from_the_release_pinned(self):
        tasmota = PINS["tasmota"]
        self.assertEqual(tasmota["tag"], f"v{tasmota['version']}")
        for key, name in uf.TASMOTA_FILES.items():
            self.assertEqual(tasmota["files"][key]["name"], name)
            self.assertEqual(OFFICIAL_URLS[key],
                             f"https://github.com/arendst/Tasmota/releases/download/{tasmota['tag']}/{name}")
            self.assertEqual(PINNED_ARTIFACTS[key]["sha256"], tasmota["files"][key]["sha256"])

    def test_each_earlier_release_is_kept_by_its_files(self):
        current = {(f["size"], f["sha256"]) for f in PINS["tasmota"]["files"].values()}
        versions = [entry["version"] for entry in PINS["tasmota"].get("earlier", [])]
        self.assertNotIn(PINS["tasmota"]["version"], versions)
        self.assertEqual(len(versions), len(set(versions)))
        for entry in PINS["tasmota"].get("earlier", []):
            self.assertEqual(set(entry["files"]), set(uf.TASMOTA_FILES), entry["version"])
            for file in entry["files"].values():
                self.assertNotIn((file["size"], file["sha256"]), current, entry["version"])

    def test_the_release_takes_the_record_the_recovery_safeboot_saves(self):
        self.assertEqual(PINS["tasmota"]["cfg_holder"], tasmota_settings.CFG_HOLDER)


class ESPHomePinTests(unittest.TestCase):
    def test_each_file_is_the_one_pinned_and_in_sha256sums(self):
        sums = (ROOT / "SHA256SUMS").read_text().splitlines()
        for key, pin in PINS["esphome"].items():
            data = (ROOT / pin["file"]).read_bytes()
            self.assertEqual((len(data), sha256_bytes(data)), (pin["size"], pin["sha256"]), key)
            self.assertIn(f"{pin['sha256']}  {pin['file']}", sums)

    def test_each_app_was_built_as_firmware_json_says(self):
        for key in ("image", "mover"):
            pin = PINS["esphome"][key]
            described = uf.app_description((ROOT / pin["file"]).read_bytes())
            self.assertEqual(described["version"], pin["esphome"], key)
            self.assertEqual(described["esp_idf"], pin["esp_idf"], key)

    def test_each_config_is_at_the_version_published(self):
        # A config changed and its version raised, but not yet built and
        # published, fails here: what Home Assistant adopts must be what the
        # tool installs.
        for key in ("image", "mover"):
            pin = PINS["esphome"][key]
            config = (ROOT / pin["config"]).read_text(encoding="utf-8")
            self.assertEqual(uf.config_version(config), pin["version"], key)

    def test_no_older_build_is_left_in_artifacts(self):
        published = {Path(pin["file"]).name for pin in PINS["esphome"].values()}
        for pattern in ("esphome-sonoff-s60-*.bin", "autoflash-mover-*.bin"):
            for path in (ROOT / "artifacts").glob(pattern):
                self.assertIn(path.name, published)


class DocumentedTests(unittest.TestCase):
    def test_artifacts_readme_has_each_files_row(self):
        readme = (ROOT / "artifacts/README.md").read_text(encoding="utf-8")
        for pin in PINS["esphome"].values():
            row = next((line for line in readme.splitlines()
                        if line.startswith(f"| `{Path(pin['file']).name}` |")), "")
            self.assertIn(f"`{pin['sha256']}`", row, pin["file"])

    def test_third_party_notices_name_each_file_and_its_build(self):
        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        for pin in PINS["esphome"].values():
            self.assertIn(f"`{pin['file']}`", notices)
            self.assertIn(f"ESPHome {pin['esphome']}", notices)
            self.assertIn(f"ESP-IDF {pin['esp_idf']}", notices)

    def test_the_esphome_readme_names_the_release_that_built_the_image(self):
        readme = (ROOT / "ESPHome/README.md").read_text(encoding="utf-8")
        self.assertIn(f"ESPHome {PINS['esphome']['image']['esphome']} built the current", readme)


class UpdateHelperTests(unittest.TestCase):
    CONFIG = ('esphome:\n  name: esphome\n  project:\n    name: adamsthws.sonoff-s60\n'
              '    version: "1.2.0"\n  on_boot:\n    - priority: 1000\n'
              'ota:\n  - platform: esphome\n    version: 2\n')

    def test_the_project_version_and_only_it_is_changed(self):
        changed = uf.with_config_version(self.CONFIG, "1.3.0")
        self.assertEqual(uf.config_version(changed), "1.3.0")
        self.assertEqual(changed, self.CONFIG.replace('"1.2.0"', '"1.3.0"'))

    def test_a_config_without_a_project_version_is_refused(self):
        with self.assertRaises(uf.UpdateError):
            uf.with_config_version("esphome:\n  name: esphome\n", "1.3.0")

    def test_the_next_image_version_is_the_next_minor(self):
        self.assertEqual(uf.next_minor("1.2.0"), "1.3.0")
        self.assertEqual(uf.next_minor("1.9.4"), "1.10.0")

    def test_a_published_files_line_takes_the_old_ones_place(self):
        sums = "a" * 64 + "  artifacts/one.bin\n" + "b" * 64 + "  artifacts/old-1.0.0.bin\n" + "c" * 64 + "  x\n"
        updated = uf.replace_sums_line(sums, "artifacts/old-1.0.0.bin", "artifacts/old-1.1.0.bin", b"new")
        self.assertEqual(updated.splitlines(), [
            "a" * 64 + "  artifacts/one.bin",
            sha256_bytes(b"new") + "  artifacts/old-1.1.0.bin",
            "c" * 64 + "  x",
        ])
        added = uf.replace_sums_line(sums, None, "artifacts/new.bin", b"new")
        self.assertEqual(added.splitlines()[-1], sha256_bytes(b"new") + "  artifacts/new.bin")

    def test_the_new_file_is_let_in_where_the_old_one_was(self):
        ignore = "*.bin\n!artifacts/a-1.0.0.bin\n!artifacts/b.bin\n\n.env\n"
        self.assertEqual(uf.with_ignore_exception(ignore, "artifacts/a-1.0.0.bin", "artifacts/a-1.1.0.bin"),
                         "*.bin\n!artifacts/a-1.1.0.bin\n!artifacts/b.bin\n\n.env\n")
        self.assertEqual(uf.with_ignore_exception(ignore, "artifacts/gone.bin", "artifacts/c.bin"),
                         "*.bin\n!artifacts/a-1.0.0.bin\n!artifacts/b.bin\n!artifacts/c.bin\n\n.env\n")

    def test_each_published_file_is_let_past_gitignore(self):
        lines = (ROOT / ".gitignore").read_text().splitlines()
        for pin in PINS["esphome"].values():
            self.assertIn(f"!{pin['file']}", lines)

    def test_cfg_holder_is_read_from_tasmotas_config(self):
        text = "// CFG_HOLDER below\n#define CFG_HOLDER             4617              // [Reset 1]\n"
        self.assertEqual(uf.cfg_holder(text), 4617)
        with self.assertRaises(uf.UpdateError):
            uf.cfg_holder("#define OTHER 1\n")

    def test_a_factory_image_with_another_layout_is_refused(self):
        blank = bytes(0x400000)
        with self.assertRaisesRegex(uf.UpdateError, "partition table"):
            uf.check_tasmota_files({"factory": blank, "safeboot": b"", "app": b""})


class ESPHomeDocTests(unittest.TestCase):
    """What an ESPHome update writes, from the image published now to a made-up one."""

    def texts(self, esphome):
        from unittest import mock
        old = uf.load_pins()["esphome"]["image"]
        new = {"file": "artifacts/esphome-sonoff-s60-imprintable-9.9.0.bin", "version": "9.9.0",
               "esphome": esphome, "esp_idf": "5.5.9", "sha256": "0" * 64}
        with mock.patch.object(uf, "released", return_value=True):
            texts, _version = uf.esphome_doc_texts(old, new, bootloader_same=True)
        return old, texts

    def test_a_new_release_builds_the_same_config(self):
        old, texts = self.texts("2099.1.0")
        self.assertIn("ESPHome for the S60 is 9.9.0: the same config, built with ESPHome", texts[uf.CHANGELOG])
        self.assertIn("the config is unchanged but for its version", " ".join(texts[uf.ARTIFACTS_README].split()))

    def test_the_same_release_builds_a_changed_config(self):
        old, texts = self.texts(uf.load_pins()["esphome"]["image"]["esphome"])
        self.assertIn("ESPHome for the S60 is 9.9.0: a change to its config, built with ESPHome", texts[uf.CHANGELOG])
        self.assertNotIn("the same config", texts[uf.CHANGELOG].split("## ")[1])
        readme = " ".join(texts[uf.ARTIFACTS_README].split())
        self.assertIn(f"ESPHome 9.9.0 is a change to {old['version']}'s config", readme)
        self.assertNotIn("9.9.0 is " + old["version"] + "'s config built", readme)


class SettledReleaseTests(unittest.TestCase):
    """The settled-release rule (update_firmware.SETTLE_DAYS), on made-up histories."""

    @staticmethod
    def releases(*pairs):
        import datetime as dt
        return [{"tag": tag, "published": dt.datetime.fromisoformat(day + "T12:00:00+00:00")} for tag, day in pairs]

    @staticmethod
    def on(day):
        import datetime as dt
        return dt.datetime.fromisoformat(day + "T13:00:00+00:00")

    def test_tasmota_waits_fourteen_days(self):
        history = self.releases(("v15.6.0", "2026-08-25"), ("v16.0.0", "2026-10-16"))
        due = lambda day: uf.due("tasmota", history, self.on(day), "15.6.0", [])["due"]  # noqa: E731
        self.assertIsNone(due("2026-10-29"))
        self.assertEqual(due("2026-10-30"), "16.0.0")

    def test_a_fix_release_starts_the_wait_again(self):
        history = self.releases(("v15.6.0", "2026-08-25"), ("v16.0.0", "2026-10-16"), ("v16.0.1", "2026-10-27"))
        self.assertIsNone(uf.due("tasmota", history, self.on("2026-11-05"), "15.6.0", [])["due"])
        self.assertEqual(uf.due("tasmota", history, self.on("2026-11-10"), "15.6.0", [])["due"], "16.0.1")

    def test_esphome_takes_last_months_last_point_release_once_this_months_is_out(self):
        history = self.releases(("2026.9.0", "2026-09-16"), ("2026.9.1", "2026-09-29"),
                                ("2026.10.0", "2026-10-21"), ("2026.10.1", "2026-10-30"),
                                ("2026.11.0", "2026-11-18"))
        due = lambda day: uf.due("esphome", history, self.on(day), "2026.9.1", [])["due"]  # noqa: E731
        self.assertIsNone(due("2026-11-10"))                     # 2026.10.1 only 11 days old, no 2026.11 yet
        self.assertEqual(due("2026-11-13"), "2026.10.1")         # 14 days old, before 2026.11.0
        self.assertEqual(due("2026-11-18"), "2026.10.1")         # 2026.11.0 out, and never itself so soon

    def test_a_series_followed_by_a_newer_one_still_waits_seven_days(self):
        history = self.releases(("2026.9.1", "2026-09-29"), ("2026.10.0", "2026-10-21"),
                                ("2026.10.1", "2026-10-24"), ("2026.11.0", "2026-10-26"))
        self.assertIsNone(uf.due("esphome", history, self.on("2026-10-29"), "2026.9.1", [])["due"])
        self.assertEqual(uf.due("esphome", history, self.on("2026-10-31"), "2026.9.1", [])["due"], "2026.10.1")

    def test_a_release_on_hold_is_never_taken(self):
        history = self.releases(("v15.6.0", "2026-08-25"), ("v16.0.0", "2026-10-16"))
        self.assertIsNone(uf.due("tasmota", history, self.on("2026-12-01"), "15.6.0", ["16.0.0"])["due"])

    def test_nothing_older_than_the_pin_is_ever_due(self):
        history = self.releases(("2026.9.1", "2026-09-29"), ("2026.10.0", "2026-10-21"))
        self.assertIsNone(uf.due("esphome", history, self.on("2026-12-01"), "2026.10.0", [])["due"])

    def test_a_pinned_release_withdrawn_is_replaced_by_its_fix_at_once(self):
        # As 15.0.0 was: withdrawn the day after, 15.0.1 in its place.
        history = self.releases(("v14.6.0", "2025-04-16"), ("v15.0.1", "2025-06-14"))
        state = uf.due("tasmota", history, self.on("2025-06-15"), "15.0.0", [])
        self.assertTrue(state["pinned_missing"])
        self.assertEqual(state["due"], "15.0.1")


class ChangelogTests(unittest.TestCase):
    """An update's line goes under the release to come: merging it is that release."""

    def write(self, directory, changelog, version):
        from unittest import mock
        changelog_path, version_path = Path(directory) / "CHANGELOG.md", Path(directory) / "__init__.py"
        changelog_path.write_text(changelog)
        version_path.write_text(f'"""x"""\n__version__ = "{version}"\n')
        return mock.patch.multiple(uf, CHANGELOG=changelog_path, VERSION_FILE=version_path)

    def test_after_a_release_a_new_minor_version_is_opened(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, self.write(tmp, "# Changelog\n\nRules.\n\n## 2.1.0\n\n- Old.\n", "2.1.0"), \
             mock.patch.object(uf, "released", return_value=True):
            texts = {}
            self.assertEqual(uf.changelog_with("A converted plug ends on Tasmota 16.1.0.", texts), "2.2.0")
            changelog, version = texts[uf.CHANGELOG], texts[uf.VERSION_FILE]
        self.assertIn("## 2.2.0\n\n- A converted plug ends on Tasmota 16.1.0.\n\n## 2.1.0\n\n- Old.", changelog)
        self.assertIn('__version__ = "2.2.0"', version)

    def test_before_it_the_line_joins_the_release_to_come(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, \
             self.write(tmp, "# Changelog\n\n## 2.2.0\n\n- First.\n\n## 2.1.0\n\n- Old.\n", "2.2.0"), \
             mock.patch.object(uf, "released", return_value=False):
            texts = {}
            self.assertEqual(uf.changelog_with("Second.", texts), "2.2.0")
            changelog = texts[uf.CHANGELOG]
        self.assertIn("## 2.2.0\n\n- First.\n- Second.\n\n## 2.1.0", changelog)

    def test_a_passage_not_found_changes_nothing(self):
        with self.assertRaisesRegex(uf.UpdateError, "nothing was changed"):
            uf.substituted("no such text", r"ESPHome \S+ built the current", "x", "the release")


class UpdateCommandTests(unittest.TestCase):
    """update_firmware.py's tasmota and esphome commands, on a made-up checkout
    and releases: what each refuses, and what it writes."""

    CONFIG = 'esphome:\n  name: {name}\n  project:\n    name: x\n    version: "{version}"\n'

    def setUp(self):
        import tempfile
        from unittest import mock
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = root = Path(directory.name)
        (root / "ESPHome").mkdir()
        (root / "artifacts").mkdir()
        for key, version in (("image", "1.4.0"), ("mover", "1.1.0")):
            (root / uf.ESPHOME_BUILDS[key][0]).write_text(self.CONFIG.format(name=key, version=version),
                                                          encoding="utf-8")
        pins = {"tasmota": self.tasmota_pin("15.0.0", earlier=[self.tasmota_pin("14.0.0")]), "esphome": {}}
        for key, file, version in (("image", "artifacts/esphome-sonoff-s60-imprintable-1.4.0.bin", "1.4.0"),
                                   ("bootloader", "artifacts/esphome-sonoff-s60-bootloader-1.2.0.bin", "1.2.0"),
                                   ("mover", "artifacts/autoflash-mover-1.1.0.bin", "1.1.0")):
            (root / file).write_bytes(f"{key} {version}".encode())
            pins["esphome"][key] = {"file": file, "version": version, "esphome": "2026.1.0", "esp_idf": "5.5.5"}
        (root / "firmware.json").write_text(json.dumps(pins), encoding="utf-8")
        (root / "SHA256SUMS").write_text("".join(f"{sha256_bytes(b'x')}  {pin['file']}\n"
                                                 for pin in pins["esphome"].values()), encoding="utf-8")
        (root / ".gitignore").write_text("*.bin\n" + "".join(f"!{pin['file']}\n" for pin in pins["esphome"].values()),
                                         encoding="utf-8")
        self.before = self.snapshot()
        for name, value in (("ROOT", root), ("FIRMWARE_JSON", root / "firmware.json"),
                            ("SHA256SUMS", root / "SHA256SUMS"), ("GITIGNORE", root / ".gitignore"),
                            ("BUILD_CACHE", root / "build")):
            mock.patch.object(uf, name, value).start()
        mock.patch.object(uf.shutil, "which", return_value="/usr/bin/docker").start()
        self.addCleanup(mock.patch.stopall)

    @staticmethod
    def tasmota_pin(version, earlier=None):
        pin = {"version": version, "tag": f"v{version}", "commit": version * 4,
               "files": {key: {"name": name, "size": 1, "sha256": "0" * 64} for key, name in uf.TASMOTA_FILES.items()}}
        return pin | ({"cfg_holder": tasmota_settings.CFG_HOLDER, "earlier": earlier} if earlier is not None else {})

    def snapshot(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def pins(self):
        return json.loads((self.root / "firmware.json").read_text(encoding="utf-8"))

    def before_pins(self):
        return json.loads(self.before["firmware.json"])["esphome"]

    def run_main(self, *argv):
        import contextlib
        errors = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
            code = uf.main(list(argv))
        return code, errors.getvalue()

    # -- Tasmota

    def tasmota(self, *argv, version="16.0.0", files=None, sizes=None, holder=tasmota_settings.CFG_HOLDER,
                check=None):
        from unittest import mock
        files = files if files is not None else {name: name.encode() for name in uf.TASMOTA_FILES.values()}
        sizes = sizes or {}
        release = {"tag_name": f"v{version}", "html_url": "https://example.invalid/release",
                   "assets": [{"name": name, "size": sizes.get(name, len(data)), "browser_download_url": name}
                              for name, data in files.items()]}
        downloads = dict(files)
        downloads[f"https://raw.githubusercontent.com/{uf.TASMOTA_REPO}/{'c' * 40}/tasmota/my_user_config.h"] = \
            f"#define CFG_HOLDER {holder}\n".encode()
        with mock.patch.object(uf, "stable_release", return_value=release), \
             mock.patch.object(uf, "tag_commit", return_value="c" * 40), \
             mock.patch.object(uf, "download", side_effect=downloads.__getitem__), \
             mock.patch.object(uf, "check_tasmota_files", side_effect=check) as checked:
            code, errors = self.run_main("tasmota", *argv)
        return code, errors, checked

    def test_tasmota_pins_the_release_and_keeps_the_one_before(self):
        code, _, checked = self.tasmota("--no-docs")
        self.assertEqual(code, 0)
        checked.assert_called_once()
        pin = self.pins()["tasmota"]
        self.assertEqual((pin["version"], pin["tag"], pin["commit"]), ("16.0.0", "v16.0.0", "c" * 40))
        self.assertEqual(pin["files"]["app"], {"name": "tasmota32c3.bin", "size": len(b"tasmota32c3.bin"),
                                               "sha256": sha256_bytes(b"tasmota32c3.bin")})
        self.assertEqual([entry["version"] for entry in pin["earlier"]], ["15.0.0", "14.0.0"])
        self.assertNotIn("earlier", pin["earlier"][0])

    def test_tasmota_pinned_again_is_not_kept_as_its_own_earlier_release(self):
        self.assertEqual(self.tasmota("--no-docs", version="15.0.0")[0], 0)
        self.assertEqual([entry["version"] for entry in self.pins()["tasmota"]["earlier"]], ["14.0.0"])

    def test_tasmota_going_back_to_an_earlier_release_takes_it_out_of_earlier(self):
        self.assertEqual(self.tasmota("--no-docs", version="14.0.0")[0], 0)
        self.assertEqual([entry["version"] for entry in self.pins()["tasmota"]["earlier"]], ["15.0.0"])

    def test_tasmota_dry_run_changes_nothing(self):
        self.assertEqual(self.tasmota("--dry-run")[0], 0)
        self.assertEqual(self.snapshot(), self.before)

    def test_tasmota_refusals_change_nothing(self):
        files = {name: name.encode() for name in uf.TASMOTA_FILES.values()}
        for case, kwargs, said in (
                ("a file short", {"sizes": {"tasmota32c3.bin": 999}}, "came down as"),
                ("a file missing", {"files": {k: v for k, v in files.items() if k != "tasmota32c3.bin"}},
                 "has no tasmota32c3.bin"),
                ("another CFG_HOLDER", {"holder": 1234}, "CFG_HOLDER is 1234")):
            code, errors, _ = self.tasmota("--no-docs", **kwargs)
            self.assertEqual(code, 1, case)
            self.assertIn(said, errors, case)
            self.assertEqual(self.snapshot(), self.before, case)

    def test_tasmota_files_the_conversion_cannot_take_change_nothing(self):
        code, errors, _ = self.tasmota("--no-docs", check=uf.UpdateError("another partition table"))
        self.assertEqual(code, 1)
        self.assertIn("another partition table", errors)
        self.assertEqual(self.snapshot(), self.before)

    # -- ESPHome

    def esphome(self, *argv, bootloader=b"bootloader 1.2.0", check=None):
        from unittest import mock
        built = []

        def build(builder, config_path, version, workdir):
            built.append((config_path.name, version))
            return f"app {config_path.name} {version}".encode(), bootloader

        with mock.patch.object(uf, "stable_release", return_value={"tag_name": "2099.1.0"}), \
             mock.patch.object(uf, "esphome_builder", return_value="esphome:2099.1.0@sha256:d"), \
             mock.patch.object(uf, "check_esphome_internals"), \
             mock.patch.object(uf, "build", side_effect=build), \
             mock.patch.object(uf, "check_esphome_app",
                               side_effect=check or (lambda image, label, release: {"esp_idf": "5.5.9"})):
            code, errors = self.run_main("esphome", *argv)
        return code, errors, built

    def test_esphome_publishes_the_image_at_the_next_minor_in_place_of_the_old(self):
        code, _, built = self.esphome("--no-docs")
        self.assertEqual(code, 0)
        self.assertEqual(built, [("sonoff_s60.yaml", "1.5.0")])
        new = "artifacts/esphome-sonoff-s60-imprintable-1.5.0.bin"
        pin = self.pins()["esphome"]["image"]
        self.assertEqual((pin["file"], pin["version"], pin["esphome"], pin["builder"]),
                         (new, "1.5.0", "2099.1.0", "esphome:2099.1.0@sha256:d"))
        self.assertEqual(pin["sha256"], sha256_bytes((self.root / new).read_bytes()))
        self.assertFalse((self.root / "artifacts/esphome-sonoff-s60-imprintable-1.4.0.bin").exists())
        self.assertIn(f"{pin['sha256']}  {new}", (self.root / "SHA256SUMS").read_text(encoding="utf-8"))
        self.assertNotIn("imprintable-1.4.0", (self.root / "SHA256SUMS").read_text(encoding="utf-8"))
        self.assertIn(f"!{new}\n", (self.root / ".gitignore").read_text(encoding="utf-8"))
        self.assertIn('version: "1.5.0"', (self.root / "ESPHome/sonoff_s60.yaml").read_text(encoding="utf-8"))
        # Neither the mover nor the bootloader, byte for byte the same, is touched.
        self.assertEqual(self.pins()["esphome"]["mover"], self.before_pins()["mover"])
        self.assertEqual(self.pins()["esphome"]["bootloader"]["version"], "1.2.0")

    def test_esphome_builds_the_mover_and_the_version_asked_for(self):
        code, _, built = self.esphome("--no-docs", "--mover", "--image-version", "2.0.0")
        self.assertEqual(code, 0)
        self.assertEqual(built, [("sonoff_s60.yaml", "2.0.0"), ("autoflash_mover.yaml", "1.2.0")])
        self.assertEqual(self.pins()["esphome"]["mover"]["file"], "artifacts/autoflash-mover-1.2.0.bin")

    def test_esphome_keeps_the_published_bootloader_unless_told(self):
        self.assertEqual(self.esphome("--no-docs", bootloader=b"another")[0], 0)
        self.assertEqual(self.pins()["esphome"]["bootloader"], self.before_pins()["bootloader"])
        self.assertTrue((self.root / "artifacts/esphome-sonoff-s60-bootloader-1.2.0.bin").exists())

    def test_esphome_replaces_a_changed_bootloader_when_told(self):
        self.assertEqual(self.esphome("--no-docs", "--new-bootloader", bootloader=b"another")[0], 0)
        pin = self.pins()["esphome"]["bootloader"]
        self.assertEqual((pin["file"], pin["sha256"]),
                         ("artifacts/esphome-sonoff-s60-bootloader-1.5.0.bin", sha256_bytes(b"another")))
        self.assertFalse((self.root / "artifacts/esphome-sonoff-s60-bootloader-1.2.0.bin").exists())

    def test_esphome_dry_run_changes_nothing(self):
        self.assertEqual(self.esphome("--dry-run", "--mover", "--new-bootloader", bootloader=b"another")[0], 0)
        self.assertEqual(self.snapshot(), self.before)

    def test_esphome_a_build_that_fails_its_checks_changes_nothing(self):
        def check(image, label, release):
            raise uf.UpdateError(f"{label} has no untouched Wi-Fi placeholders")
        code, errors, _ = self.esphome("--no-docs", check=check)
        self.assertEqual(code, 1)
        self.assertIn("placeholders", errors)
        self.assertEqual(self.snapshot(), self.before)

    def test_esphome_without_docker_stops_at_once(self):
        from unittest import mock
        with mock.patch.object(uf.shutil, "which", return_value=None):
            code, errors, built = self.esphome("--no-docs")
        self.assertEqual((code, built), (2, []))
        self.assertIn("Docker is needed", errors)


if __name__ == "__main__":
    unittest.main()
