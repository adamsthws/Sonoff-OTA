# For AI assistants working on this repository

What an assistant needs to know, beyond [CONTRIBUTING.md](CONTRIBUTING.md).

## Private things never reach the repository

The repository is public; the tool handles Wi-Fi passwords, eWeLink accounts
and plugs' identities.

- **Never read `.env`.** **`captures/` is private.**
- **Examples use made-up identifiers**, e.g. MAC `12:34:56:ab:cd:ef` - never a
  real plug's, network's or account's.
- **The leak scan** (`tools/repo/leak_scan.py`, run by `.githooks/`) stops a
  commit or push holding one: take it out, never `--no-verify`. A new
  made-up example goes in `tools/repo/leak-scan-allow`.
  [More](docs/technical-details.md#keeping-secrets-out-of-git)
- **A plug's stock Sonoff backup cannot be made again**: never overwrite,
  move or delete one.
- **No third party's proprietary firmware** (Sonoff's images, a plug's dump)
  is committed, released or built into a published image. Tooling that
  needs one fetches it at run time from where it is published, as
  `tools/beken/emulator/bk-emu.sh dump` does.

## Real plugs and the emulator

- **Ask before doing anything to a real plug**: a conversion is not undone
  by running it again.
- **Stop each emulator** (`docker stop`) when a test batch ends, and run no
  more at once than there are cores, or they fail falsely.
  [docs/emulator.md](docs/emulator.md)
- **`tools/espressif/emulator/container/`, `patches/` and `emu.Dockerfile`
  are hashed into the emulator image's tag**: any change, a comment
  included, means a new image everywhere. Change them only for the
  emulator's sake.

## Git

- **Commits are signed**, rewritten ones too.
- **One cohesive change per commit**, its message saying what and why. A
  fix, test or doc change to unpushed work goes into the commit it belongs
  to; an unrelated bug found along the way gets a commit of its own.
- **Stage only what you changed**, file by file - never `git add -A` or
  `git commit -a`: someone may be editing the same checkout.
- **Before rewriting history**, make a backup branch
  (`backup/<branch>-before-<what>-<date>`); afterwards `git diff <old tip>
  <new tip>` is empty unless a change was meant.
- **No attribution** (`Co-Authored-By`, `Generated with`, ...) in a commit,
  pull request or comment.
- **Never push or force-push unasked.** Pushing `main` with a new version in
  `tools/autoflash/__init__.py` is a release.

## GitHub

- **The repository is `adamsthws/Sonoff-OTA`**: always pass
  `-R adamsthws/Sonoff-OTA` to `gh`, which otherwise picks the upstream
  `HG0161/SonoffS60OTA`.
- **Its settings** live in `tools/repo/github_settings.py` and are set with
  it, never by hand. Renaming a job a required check names: `apply`, push,
  `apply`.
- **Workflows pin each action to a commit**, version beside it; Dependabot
  moves them a week after a release. CI stays fast: Linux runners only.
- **A pull request or issue opened in another project** is recorded in
  [docs/upstream-contributions.md](docs/upstream-contributions.md) the same
  day: project, link, one line on what it fixes.
- **Editing anything already posted** keeps what it replaces: `~~old~~`
  then the new, new parts marked **Added:**, and an **Edit:** note saying
  what changed and why.

## Code and tests

- **Python's standard library only**, 3.10 or newer. Open text files with
  `encoding=`. Packages go only into `.venv`, never the system's.
- **Take the idiomatic design**: the usual way of the language and the code
  around it, over a clever one.
- **Before a commit**: `python3 tests/run.py` and `python3 tools/repo/lint.py`
  on what changed. [docs/testing.md](docs/testing.md)
- **Every fix has a test that fails without it.** A failing test or hook is
  fixed at its cause, never skipped, loosened or deleted.
- **Tests run side by side and in any order**: each leaves nothing behind,
  uses a port it picked, and touches nothing computer-wide.
- **Where things go**: `tools/autoflash/` the tool, `tools/repo/` the
  repository's plumbing, `tools/espressif/` and `tools/beken/` each chip
  family's tooling, `tools/ewelink/` eWeLink, `devices/<plug>/` one plug's
  own, `tests/` mirroring `tools/`, `docs/` prose, `archive/` retired work
  (still linted). A plug's ESPHome config path is its `package_import_url`:
  moving one means a new image.

## Firmware and releases

- **`artifacts/`** changes only through `tools/repo/update_firmware.py`.
  [docs/updating-firmware.md](docs/updating-firmware.md)
- **A release**: its `CHANGELOG.md` section, then the version in
  `tools/autoflash/__init__.py`; the release workflow tags it once tests
  pass on `main`. A published version never changes and is never redone:
  fix forward.

## Writing

Docs, comments and commit messages: plain, compact sentences saying what
and why, for someone who was not there. Comments as sparing as the code
around them.
