"""liberate.sh and liberate.ps1, the README's one line: the latest release is
fetched, updated in place and started, and nothing of the user's - above
all captures/, with the plugs' Sonoff backups - is ever touched. Against a
GitHub of their own, on a port it picked."""

import http.server
import io
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPO = "adamsthws/Sonoff-OTA"
# Windows PowerShell where there is no PowerShell 7: what every Windows runs the line on.
POWERSHELL = shutil.which("pwsh") or (shutil.which("powershell") if os.name == "nt" else None)
# The fake releases' start scripts: they write down how they were started.
START_SH = 'echo "$*" > "$(dirname "$0")/started"\n'
START_PS1 = 'Set-Content (Join-Path (Split-Path $PSScriptRoot) "started") ($args -join " ")\n'


class FakeGitHub:
    """GitHub's releases, with the assets the release workflow gives each one:
    autoflash-version, and the release's files as autoflash.tar.gz and .zip
    under a top folder. releases/latest/download/ redirects to the latest's,
    as GitHub's does. `releases` maps a tag to its files; the last one is the
    latest."""

    def __init__(self):
        self.releases: dict[str, dict[str, str]] = {}
        self.downloads: list[str] = []
        self.online = True
        github = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                latest = f"/{REPO}/releases/latest/download/"
                if github.online and github.releases and self.path.startswith(latest):
                    self.send_response(302)
                    self.send_header("Location", "%s/%s/releases/download/%s/%s" % (
                        github.url, REPO, list(github.releases)[-1], self.path[len(latest):]))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = github.answer(self.path)
                if body is None:
                    self.send_error(404 if github.online else 503)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def answer(self, path):
        if not self.online:
            return None
        for tag, files in self.releases.items():
            top = "Sonoff-OTA-" + tag.lstrip("v")
            for script in ("liberate.sh", "liberate.ps1"):
                if path == f"/{REPO}/releases/download/{tag}/{script}":
                    return (ROOT / script).read_bytes()
            if path == f"/{REPO}/releases/download/{tag}/autoflash-version":
                return f"{tag}\n".encode()
            if path == f"/{REPO}/releases/download/{tag}/autoflash.tar.gz":
                self.downloads.append(path)
                data = io.BytesIO()
                with tarfile.open(fileobj=data, mode="w:gz") as archive:
                    for name, text in files.items():
                        member = tarfile.TarInfo(f"{top}/{name}")
                        member.size = len(text.encode())
                        archive.addfile(member, io.BytesIO(text.encode()))
                return data.getvalue()
            if path == f"/{REPO}/releases/download/{tag}/autoflash.zip":
                self.downloads.append(path)
                data = io.BytesIO()
                with zipfile.ZipFile(data, "w") as archive:
                    for name, text in files.items():
                        archive.writestr(f"{top}/{name}", text)
                return data.getvalue()
        return None


def release(extra=None):
    return {"start.sh": START_SH, "tools/windows-start.ps1": START_PS1,
            "tools/autoflash/__init__.py": "", "README.md": "readme", **(extra or {})}


class LiberateTests:
    """The same checks for each script: `run_script` runs it, with its arguments."""

    def run_script(self, *arguments):
        raise NotImplementedError

    def setUp(self):
        self.github = FakeGitHub()
        self.addCleanup(self.github.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name, "Sonoff-OTA")
        self.env = {**os.environ, "AUTOFLASH_GITHUB": self.github.url, "AUTOFLASH_HOME": str(self.folder), "HOME": tmp.name, "USERPROFILE": tmp.name}

    def readme_line(self, pattern):
        """The README's line that matches, fetching from this GitHub instead."""
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        line = re.search(r"^.*%s.*$" % pattern, readme, re.M).group(0)
        return line.replace("https://github.com", self.github.url)

    def started(self):
        return (self.folder / "started").read_text(encoding="utf-8").strip()

    def test_installs_the_latest_release_and_starts_it_with_the_arguments(self):
        self.github.releases = {"v1.0.0": release(), "v1.1.0": release({"README.md": "new"})}
        done = self.run_script("run", "--verbose")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.started(), "run --verbose")
        self.assertTrue((self.folder / "tools/autoflash/__init__.py").is_file())
        self.assertEqual(self.github.downloads, [f"/{REPO}/releases/download/v1.1.0/autoflash.{self.archive}"])
        self.assertEqual((self.folder / ".installed-release").read_text(encoding="utf-8-sig").splitlines()[0],
                         "v1.1.0")

    def test_an_update_replaces_the_release_and_keeps_everything_else(self):
        self.github.releases = {"v1.0.0": release({"tools/old.py": "old", "tools/gone/deeper/x.py": "x",
                                           "docs/a.md": "a", "docs/b.md": "b"})}
        self.assertEqual(self.run_script().returncode, 0)
        backup = self.folder / "captures/plugs/12-34-56-ab-cd-ef/sonoff-backup/flash.bin"
        backup.parent.mkdir(parents=True)
        backup.write_text("irreplaceable", encoding="utf-8")
        (self.folder / ".python").mkdir()
        (self.folder / ".python/python3").write_text("portable", encoding="utf-8")
        (self.folder / ".env").write_text("answers", encoding="utf-8")
        (self.folder / "docs/mine.md").write_text("mine", encoding="utf-8")
        self.github.releases["v1.1.0"] = release({"docs/a.md": "a, changed"})

        done = self.run_script()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual((self.folder / "docs/a.md").read_text(encoding="utf-8"), "a, changed")
        self.assertFalse((self.folder / "tools/old.py").exists())
        self.assertFalse((self.folder / "tools/gone").exists())   # emptied, so gone too
        self.assertTrue((self.folder / "tools/autoflash").is_dir())
        self.assertFalse((self.folder / "docs/b.md").exists())
        self.assertEqual((self.folder / "docs/mine.md").read_text(encoding="utf-8"), "mine")
        self.assertEqual(backup.read_text(encoding="utf-8"), "irreplaceable")
        self.assertEqual((self.folder / ".python/python3").read_text(encoding="utf-8"), "portable")
        self.assertEqual((self.folder / ".env").read_text(encoding="utf-8"), "answers")

    def test_the_installed_latest_release_starts_without_a_download(self):
        self.github.releases = {"v1.0.0": release()}
        self.assertEqual(self.run_script().returncode, 0)
        self.github.downloads.clear()
        done = self.run_script("status")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.github.downloads, [])
        self.assertEqual(self.started(), "status")

    def test_offline_the_installed_release_starts(self):
        self.github.releases = {"v1.0.0": release()}
        self.assertEqual(self.run_script().returncode, 0)
        self.github.online = False
        done = self.run_script("status")
        self.assertEqual(self.started(), "status")
        self.assertIn("Could not check for a newer release: starting Autoflash v1.0.0", done.stdout + done.stderr)

    def test_offline_with_nothing_installed_says_so(self):
        self.github.online = False
        done = self.run_script()
        self.assertIn("could not be found on GitHub", done.stdout + done.stderr)
        self.assertFalse(self.folder.exists())

    def test_a_git_clone_is_started_as_it_is(self):
        self.github.releases = {"v1.0.0": release()}
        (self.folder / ".git").mkdir(parents=True)
        for name, text in release().items():
            (self.folder / name).parent.mkdir(parents=True, exist_ok=True)
            (self.folder / name).write_text(text, encoding="utf-8")
        done = self.run_script("run")
        self.assertEqual(self.started(), "run", done.stdout + done.stderr)
        self.assertEqual(self.github.downloads, [])
        self.assertFalse((self.folder / ".installed-release").exists())

    def test_a_folder_holding_something_else_is_left_alone(self):
        self.github.releases = {"v1.0.0": release()}
        self.folder.mkdir()
        (self.folder / "mine.txt").write_text("mine", encoding="utf-8")
        done = self.run_script()
        self.assertIn("already holds something else", done.stdout + done.stderr)
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["mine.txt"])
        self.assertEqual(self.github.downloads, [])


@unittest.skipIf(shutil.which("sh") is None or shutil.which("curl") is None, "no sh or curl")
class LiberateShTests(LiberateTests, unittest.TestCase):
    archive = "tar.gz"

    def run_script(self, *arguments):
        return subprocess.run(["sh", str(ROOT / "liberate.sh"), *arguments], env=self.env,
                              capture_output=True, text=True, timeout=60)


    def test_the_readme_lines_fetch_it_from_the_latest_release_and_start_it(self):
        self.github.releases = {"v1.0.0": release()}
        for fetcher in ("curl", "wget"):
            if shutil.which(fetcher):
                with self.subTest(fetcher=fetcher):
                    shutil.rmtree(self.folder, ignore_errors=True)
                    done = subprocess.run(["sh", "-c", self.readme_line(r"sh -c \"\$\(%s " % fetcher)],
                                          env=self.env, capture_output=True, text=True, timeout=60)
                    self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                    self.assertEqual(self.started(), "")


@unittest.skipIf(POWERSHELL is None, "no PowerShell")
class LiberatePs1Tests(LiberateTests, unittest.TestCase):
    archive = "zip"

    def test_the_readme_line_fetches_it_from_the_latest_release_and_starts_it(self):
        self.github.releases = {"v1.0.0": release()}
        line = shlex.split(self.readme_line(r"powershell -c \"irm "))
        done = subprocess.run([POWERSHELL, *line[1:]], env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.started(), "")

    def run_script(self, *arguments):
        return subprocess.run([POWERSHELL, "-NoProfile", "-File", str(ROOT / "liberate.ps1"), *arguments], env=self.env,
                              capture_output=True, text=True, timeout=120)


if __name__ == "__main__":
    unittest.main()
