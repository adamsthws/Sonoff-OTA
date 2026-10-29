# For AI assistants working on this repository

What an assistant - Claude Code, Codex, Cursor, Copilot, Gemini, Aider - needs
to know before changing anything here, beyond what
[CONTRIBUTING.md](CONTRIBUTING.md) asks of everyone. The docs it points to say
more; this says what is easy to get wrong.

## Private things never reach the repository

The repository is public, and the tool handles people's Wi-Fi passwords,
eWeLink accounts and plugs' identities.

- **Never read `.env`**: it holds the passwords for unattended runs. The
  ignore files beside this one and `.claude/settings.json` keep tools out.
- **`captures/` is private** - each plug's backup, runs and logs - and never
  committed. Git ignores it.
- **Examples use made-up identifiers**: the MAC `12:34:56:ab:cd:ef`, and
  addresses that are nobody's. Never a real plug's, network's or account's.
- **The leak scan** (`tools/leak_scan.py`, run by the hooks in `.githooks/`)
  stops a commit or push holding one. Take it out; never pass `--no-verify`
  to get past a finding. A new made-up example goes in `.leak-scan-allow`.
  [docs/technical-details.md](docs/technical-details.md#keeping-secrets-out-of-git)
- **A plug's stock Sonoff backup cannot be made again** once the plug is
  converted: never overwrite, move or delete one.
- **No third party's proprietary firmware** - Sonoff's stock images, a plug's
  dump - is ever committed, put on a release or built into a published
  image, whatever the convenience. Where tooling needs one, fetch it at run
  time from where its owner or another project publishes it, as
  `docs/s61-tools/emulator/bk-emu.sh dump` does.

## Real plugs and the emulator

- **Ask before doing anything to a real plug.** A conversion is not undone
  by running it again.
- **Stop each emulator** (`docker stop`) as soon as a test batch ends, and
  run no more side by side than the computer has cores: more give false
  failures. [docs/emulator.md](docs/emulator.md)
- **`tools/emulator/container/`, `fork/` and `emu.Dockerfile` are hashed
  into the emulator image's tag** (`tools/emulator/image.sh`): any change
  there, a comment included, makes every computer and runner build or pull
  a new image. Change them only for the emulator's sake.

## Git

- **Commits are signed.** A rewritten commit is signed again.
- **One cohesive change per commit**, its message saying what and why. A
  fix, test or doc change to work not yet pushed goes into the commit it
  belongs to, so the history never shows a change made and then undone.
  A bug found along the way, unrelated to the task, is fixed in a commit of
  its own and said so.
- **Stage only what you changed**, file by file - never `git add -A` or
  `git commit -a`: someone may be editing the same checkout.
- **Before rewriting history**, make a backup branch
  (`backup/<branch>-before-<what>-<date>`), and afterwards check the tree is
  what it was (`git diff <old tip> <new tip>` is empty) unless a change was
  meant.
- **No attribution**: no `Co-Authored-By`, `Generated with` or similar line
  in a commit, pull request or comment. The person who commits a change
  answers for it, however it was written.
- **Never push, and never force-push, without being asked.** Pushing `main`
  with a new version in `tools/autoflash/__init__.py` is a release.

## GitHub

- **The repository is `adamsthws/Sonoff-OTA`.** In a clone with the
  upstream remote, `gh` picks `HG0161/SonoffS60OTA`: always pass
  `-R adamsthws/Sonoff-OTA`.
- **Its settings** - rulesets, required checks, security features - are
  written down in `tools/github_settings.py` and set with it, never by hand.
  A job a required check names is renamed with `apply`, push, `apply`.
- **The workflows pin each action to a commit**, with its version beside
  it; Dependabot moves them, a week after a release.
  [docs/updating-firmware.md](docs/updating-firmware.md) says how the
  firmware-update workflow opens, tests and merges its pull requests.
- **CI and the hooks stay fast**: Linux runners only, no Windows or macOS
  matrix.
- **A pull request or issue opened in another project** because of work
  here is recorded in
  [docs/upstream-contributions.md](docs/upstream-contributions.md) the same
  day: the project, the link, one line on what it fixes.
- **Editing anything already posted** - a pull request or issue
  description, a comment - keeps what it replaces: the old text as
  `~~old~~` followed by the new, new parts marked **Added:**, and an
  **Edit:** note saying what changed and why. Never a silent rewrite.

## Code and tests

- **The tool uses Python's standard library only**, on Python 3.10 or newer
  (the start scripts install 3.12.10 where there is none). Open text files
  with `encoding=`. Install packages only into a virtual environment
  (`.venv`), never the system's.
- **Where a design could go more than one way, take the idiomatic one**:
  accepted good practice and the usual way of the language and the code
  around it, over a clever or one-off way - it is what the next reader
  expects.
- **Before a commit**: `python3 tests/run.py` (about 11 s) and
  `python3 tools/lint.py` on what changed - the pre-commit hook runs the
  linters and the leak scan. [docs/testing.md](docs/testing.md)
- **Every fix has a test that fails without it.**
- **A failing test or hook is fixed at its cause**: never skipped, loosened
  or deleted to let a change through.
- **Tests run side by side and in any order**: one leaves nothing behind,
  keeps a port it picked, and touches nothing of the whole computer's.
  [docs/testing.md](docs/testing.md#side-by-side-in-any-order)
- **`archive/`** is kept as it was: not linted, not changed.

## Firmware and releases

- **`firmware.json`, `artifacts/` and `SHA256SUMS`** change only through
  `tools/update_firmware.py`, never by hand.
  [docs/updating-firmware.md](docs/updating-firmware.md)
- **A release**: its section in `CHANGELOG.md` first, then the version in
  `tools/autoflash/__init__.py`. The release workflow tags it once the tests
  pass on `main`.
- **A version is used once**: a published release and its tag never
  change, and a failed one is not redone - fix forward in the next version.

## Writing

Docs, comments and commit messages match what is here already: plain,
compact sentences that say what something does and why, for someone who
was not there. Comments as sparing as the code around them.
