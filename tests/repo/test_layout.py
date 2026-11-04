"""Every script that finds the repository from its own path finds it: a file
moved to another depth (the 2026-10 reorganisation moved tools/emulator/ two
levels down) must count its parents again."""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.autoflash import hostsys

ROOT = Path(__file__).resolve().parents[2]
# `Path(__file__).resolve().parents[N]`, and any `/ "name"` parts joined to it.
PARENTS = re.compile(r'Path\(__file__\)\.resolve\(\)\.parents\[(\d+)\]((?:\s*/\s*"[^"]+")*)')
GITIGNORED = {line.strip().strip("/") for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
              if line.strip() and not line.startswith("#")}


class RepositoryRootTests(unittest.TestCase):
    def test_every_parents_index_reaches_the_repository(self):
        wrong = []
        for file in sorted(ROOT.glob("tools/**/*.py")):
            for match in PARENTS.finditer(file.read_text(encoding="utf-8")):
                parent = file.resolve().parents[int(match.group(1))]
                parts = re.findall(r'"([^"]+)"', match.group(2))
                if not parts:
                    ok = parent == ROOT   # bare: the repository root
                else:
                    target = parent.joinpath(*parts)
                    # Joined to it: something in the repository, or a directory
                    # the repository makes at run time (captures/) off its root.
                    ok = target.exists() or (parent == ROOT and parts[0] in GITIGNORED)
                if not ok:
                    wrong.append("%s: %s" % (file.relative_to(ROOT), match.group(0)))
        self.assertEqual(wrong, [])

    def test_every_relative_link_in_the_docs_names_a_file(self):
        """A Markdown link to a path, as GitHub follows it from the file it is in;
        code (where a link is only an example) aside."""
        link = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)|^\[[^\]]+\]:\s*(\S+)", re.MULTILINE)
        tracked = subprocess.run(["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True,
                                 check=True).stdout.split()
        wrong = []
        for name in tracked:
            text = (ROOT / name).read_text(encoding="utf-8")
            text = re.sub(r"`[^`\n]*`", "", re.sub(r"```.*?```", "", text, flags=re.DOTALL))
            for match in link.finditer(text):
                href = match.group(1) or match.group(2)
                if re.match(r"[a-z][a-z0-9+.-]*:|[#/<]", href, re.IGNORECASE):
                    continue
                target = ((ROOT / name).parent / href.split("#")[0]).resolve()
                if not target.exists() or ROOT not in target.parents and target != ROOT:
                    wrong.append(f"{name}: {href}")
        self.assertEqual(wrong, [])

    def test_every_windows_path_names_a_file(self):
        """`tools\\...\\name.ext` in the PowerShell, batch and prose: Windows runs them
        from the checkout root (C:\\Autoflash\\repo in the emulator's VM)."""
        windows = re.compile(r"tools\\[A-Za-z0-9_\\.-]+\.(?:ps1|py|cmd)")
        wrong = []
        for pattern in ("*.ps1", "*.cmd", "*.md"):
            for file in sorted(ROOT.rglob(pattern)):
                if any(part in ("archive", "captures", ".git", ".venv") for part in file.relative_to(ROOT).parts):
                    continue
                for path in set(windows.findall(file.read_text(encoding="utf-8"))):
                    if not (ROOT / path.replace("\\", "/")).is_file():
                        wrong.append("%s: %s" % (file.relative_to(ROOT), path))
        self.assertEqual(wrong, [])

    def test_every_script_the_docs_run_as_a_command_can_be_run(self):
        """`tools/.../name.py args` at the start of a line in a shell block - after
        any `NAME=value` settings - is run as it is, so it must be executable."""
        command = re.compile(r"^\s*(?:\\\s*)?(?:[A-Z_0-9]+=\S+\s+)*(tools/\S+\.(?:py|sh))\b", re.M)
        block = re.compile(r"^```(?:sh|bash)\n(.*?)^```", re.M | re.S)
        wrong = []
        for file in sorted(ROOT.rglob("*.md")):
            if any(part in ("archive", "captures", ".git", ".venv") for part in file.relative_to(ROOT).parts):
                continue
            for code in block.findall(file.read_text(encoding="utf-8")):
                for path in set(command.findall(code)):
                    if (ROOT / path).is_file() and not os.access(ROOT / path, os.X_OK):
                        wrong.append("%s: %s" % (file.relative_to(ROOT), path))
        self.assertEqual(sorted(set(wrong)), [])

    def test_every_helper_the_tool_launches_runs_on_a_bare_python(self):
        """The scripts the tool starts in a process of their own (hostsys.python_script),
        on a Python with nothing but the script's own folder on sys.path - the
        portable one start.cmd fetches, which -I stands in for here."""
        helpers = ("tools/ewelink/query_ota.py", "tools/espressif/serve_tasmota_ota.py",
                   "tools/espressif/safeboot/serve_safeboot_migration.py")
        with tempfile.TemporaryDirectory() as elsewhere:
            for helper in helpers:
                with self.subTest(helper=helper):
                    command = hostsys.python_script(ROOT / helper, "--help")
                    done = subprocess.run([command[0], "-I", *command[1:]], cwd=elsewhere,
                                          capture_output=True, text=True, timeout=120)
                    self.assertEqual(done.returncode, 0, done.stderr)
                    self.assertIn("usage:", done.stdout)


if __name__ == "__main__":
    unittest.main()
