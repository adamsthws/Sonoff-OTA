#!/usr/bin/env python3
"""The linters, on what a commit holds or on every tracked file.

    python3 tools/lint.py staged     the files a commit changes (the pre-commit hook)
    python3 tools/lint.py all        every tracked file (the lint workflow)
    python3 tools/lint.py FILE...    those files

Each linter is handed the files of its kind:

    ruff          Python (ruff.toml)
    shellcheck    shell scripts: *.sh, and files starting #!/bin/sh or bash
    actionlint    the workflows (with shellcheck on their scripts)
    zizmor        the workflows and dependabot.yml, for what an attacker could use;
                  with GH_TOKEN set, also against GitHub (a pinned commit is the
                  action's own, a version with a known advisory)
    PSScriptAnalyzer  PowerShell (PSScriptAnalyzerSettings.psd1), through pwsh

All but the last come from requirements/lint.txt, in the repository's .venv:

    .venv/bin/python -m pip install --require-hashes -r requirements/lint.txt

PSScriptAnalyzer needs PowerShell 7.4.6 or newer (pwsh), and then:

    pwsh -Command Install-Module PSScriptAnalyzer -Scope CurrentUser

A linter that is not installed is skipped, with a warning, on a commit - the
lint workflow runs them all - and fails `all`. On a commit, a file is linted as
it is on disk: staged in part, its unstaged part is linted too.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
# Where the hook's Python is, and the repository's .venv: the linters it installs.
TOOL_DIRS = [Path(sys.executable).parent, ROOT / ".venv" / "bin", ROOT / ".venv" / "Scripts"]


@dataclass(frozen=True)
class Linter:
    name: str
    wants: Callable[[Path], bool]       # whether it lints this file
    command: Callable[[list[str]], list[str]]
    needs: str                          # the program it runs
    install: str = "requirements/lint.txt"
    ready: Callable[[str], bool] | None = None   # given PATH: more than `needs` there


SHEBANG = re.compile(rb"#!\s*(?:/usr/bin/env\s+|/usr/bin/|/bin/)(?:ba|da)?sh\b")
# The container's scripts are hashed into the emulator image's tag (image.sh), so
# what shellcheck cannot see of them is said here rather than in them: the env
# file entrypoint.sh writes (SC1091), the variables udhcpc sets (SC2154).
CONTAINER = Path("tools/emulator/container")


def python(path: Path) -> bool:
    return path.suffix == ".py" and path.parts[0] != "archive"   # archive/ is kept as it was


def workflow(path: Path) -> bool:
    return path.parent == Path(".github/workflows") and path.suffix in (".yml", ".yaml")


def shell(path: Path) -> bool:
    if path.parts[0] == "archive":
        return False
    if path.suffix == ".sh":
        return True
    with open(ROOT / path, "rb") as file:
        return bool(SHEBANG.match(file.read(80)))


def powershell(path: Path) -> bool:
    return path.suffix == ".ps1" and path.parts[0] != "archive"


def has_psscriptanalyzer(path: str) -> bool:
    check = "exit [int](-not (Get-Module -ListAvailable PSScriptAnalyzer))"
    return subprocess.run([shutil.which("pwsh", path=path) or "pwsh", "-NoProfile", "-NonInteractive",
                           "-Command", check], capture_output=True).returncode == 0


LINTERS = [
    Linter("ruff", python, lambda files: ["ruff", "check", "--quiet", "--force-exclude", *files], "ruff"),
    Linter("shellcheck", lambda path: shell(path) and CONTAINER not in path.parents,
           lambda files: ["shellcheck", *files], "shellcheck"),
    Linter("shellcheck", lambda path: shell(path) and CONTAINER in path.parents,
           lambda files: ["shellcheck", "--exclude=SC1091,SC2154", *files], "shellcheck"),
    Linter("actionlint", workflow, lambda files: ["actionlint", *files], "actionlint"),
    Linter("zizmor", lambda path: workflow(path) or path == Path(".github/dependabot.yml"),
           lambda files: ["zizmor", "--quiet", *([] if os.environ.get("GH_TOKEN") else ["--offline"]), *files],
           "zizmor"),
    Linter("PSScriptAnalyzer", powershell,
           lambda files: ["pwsh", "-NoProfile", "-NonInteractive", "-File", "tools/lint_powershell.ps1", *files],
           "pwsh", "pwsh, then Install-Module PSScriptAnalyzer", has_psscriptanalyzer),
]


def git(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout
    return [line for line in out.split("\0") if line]


def staged() -> list[str]:
    return git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")


def tracked() -> list[str]:
    return git("ls-files", "-z")


def run(paths: list[str], strict: bool) -> int:
    env = dict(os.environ, PATH=os.pathsep.join([*map(str, TOOL_DIRS), os.environ.get("PATH", "")]))
    files = [Path(p) for p in paths if (ROOT / p).is_file()]
    failed = []
    for linter in LINTERS:
        mine = [str(p) for p in files if linter.wants(p)]
        if not mine:
            continue
        if not shutil.which(linter.needs, path=env["PATH"]) or (linter.ready and not linter.ready(env["PATH"])):
            print(f"lint: {linter.name} skipped, not installed ({linter.install})", file=sys.stderr)
            if strict:
                failed.append(linter.name)
            continue
        if subprocess.run(linter.command(mine), cwd=ROOT, env=env).returncode:
            failed.append(linter.name)
    if failed:
        print(f"lint: stopped by {', '.join(failed)} (python3 tools/lint.py FILE... runs them "
              "again; git commit --no-verify skips them)", file=sys.stderr)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["staged"]:
        return run(staged(), strict=False)
    if args == ["all"]:
        return run(tracked(), strict=True)
    if not args or args[0].startswith("-"):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    return run([os.path.relpath(Path(arg).resolve(), ROOT) for arg in args], strict=True)


if __name__ == "__main__":
    sys.exit(main())
