# Contributing

Thank you for helping. A report of how a conversion went is as useful as
code: what worked, what did not, and on which plug and computer.

## Reporting

- **How a conversion went** - clean, stopped part way, or only the read-only
  checks: [Conversions In The Wild](https://github.com/adamsthws/Sonoff-OTA/discussions/1).
- **A conversion that went wrong**: an [issue](https://github.com/adamsthws/Sonoff-OTA/issues/new/choose),
  with the run's log attached. The log is safe to share - your account,
  network and plug are already written in it as `<email>`, `<wifi-name>` and
  so on - but its folder is named after the plug's MAC address: attach the
  file, do not paste its path. [docs/troubleshooting.md](docs/troubleshooting.md)
  covers what has gone wrong before.
- **A plug we do not list yet** - if you got the tool working on a model or a
  stock firmware version that is not in the compatibility list, please
  contribute it back so the list keeps growing. Tell us what the plug was and
  what the conversion took - the addresses, config and wiring a new model
  needed, and anything that differed from a listed one. A report alone is
  welcome; a pull request that adds the support is more welcome still, and the
  notes behind it are what we most want to receive.
- **A security problem**: privately, never as an issue - see [SECURITY.md](SECURITY.md).

## Setting up

The tool needs nothing but Python 3.10 or newer. Working on it needs a few
tools more - the test helpers and the linters - which go in a virtual
environment in the checkout, never into the system's Python:

```sh
tools/repo/setup-dev                       # creates .venv, installs both files below
python3 tools/repo/leak_scan.py install
```

`tools/repo/setup-dev` is the one-command form of:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements/ci.txt -r requirements/lint.txt
```

[uv](https://docs.astral.sh/uv/) works as well and faster, from the same
files: `uv venv` and `uv pip install --require-hashes -r ...`.

The leak-scan line points Git at the hooks in `.githooks/`: before each commit
they scan what is staged for secrets and real identifiers, and lint it; before
each push they scan every commit it sends. A linked worktree reuses this
checkout's `.venv`, so run `tools/repo/setup-dev` once in the main checkout - and
in the worktree as well when its branch changes `requirements/`, or it lints
with the main checkout's versions.

Optional, for what they check: Docker, for the ESPHome configs' check and
the emulator; PowerShell 7.4.6 or newer (`pwsh`), with
`Install-Module PSScriptAnalyzer`, for the PowerShell scripts. A commit on a
computer without them is not stopped - the hook says what it skipped, and
the workflows run them all.

## Testing

```sh
python3 tests/run.py          # the unit tests, about 11 s
python3 tools/repo/lint.py all     # every linter on every file
```

Every fix comes with a test that fails without it. The tests run side by
side and in any order, so a new one leaves nothing behind, keeps a port it
picked, and touches nothing of the whole computer's:
[docs/testing.md](docs/testing.md#side-by-side-in-any-order). Coverage and
mutation testing show where the tests are thin - a guide, not a target:
[docs/testing.md](docs/testing.md#how-much-they-test). The emulator -
a virtual S60 running the plug's own firmware - tests whole conversions:
[docs/emulator.md](docs/emulator.md). Never try a change on a plug you
cannot afford to lose.

## Keeping private things out

The repository is public, and the tool handles people's Wi-Fi passwords,
eWeLink accounts and plugs' identities.

- `.env` (passwords for unattended runs) and `captures/` (each plug's
  backup and runs) are never committed; Git ignores both.
- In examples, tests and docs, use the made-up MAC `12:34:56:ab:cd:ef` and
  addresses that are nobody's - never your plug's, network's or account's.
- If the leak scan stops a commit, take the finding out. A new made-up
  example belongs in `tools/repo/leak-scan-allow`; nothing real is ever let through
  with `--no-verify`.
  [docs/technical-details.md](docs/technical-details.md#keeping-secrets-out-of-git)
- No third party's proprietary firmware - Sonoff's stock images, a plug's
  dump, anything else we have no right to publish - is ever committed, put on
  a release or built into a published image. Git ignores `*.bin` for this
  reason; the exceptions listed in `.gitignore` are open-source firmware this
  project ships. Where the tooling needs such an image, as the emulators'
  tests do, it is fetched at run time from wherever its owner or another
  project publishes it, never copied here.

## Commits and pull requests

- **One cohesive change per commit**, its message saying what changed and
  why. While a branch is unpushed, fold a fix to its own work into the
  commit it belongs to rather than adding one on top.
- **A pull request** is squashed into one commit when merged, once the
  required checks pass: the unit tests on Python 3.12 and 3.10, the
  emulator, the lint and the leak scan. A pull request from a fork runs them
  all - none needs a secret - once a maintainer has let a first-time
  contributor's workflows run. The form a pull request opens with lists
  what is asked of it.
- **Written with an AI assistant?** That's fine, and you don't need to say
  so: as with the Linux kernel, whoever submits a change answers for it -
  its correctness, its tests and its licence - however it was written.
- **A change people will notice** gets a line in `CHANGELOG.md`. The
  maintainer sets the version and releases.

## Code

- The tool uses Python's standard library only, on Python 3.10 or newer.
  Open text files with `encoding=`.
- `artifacts/` - its firmware, `firmware.json` and `SHA256SUMS` - changes only through
  `tools/repo/update_firmware.py` - most often run by the weekly firmware-update
  workflow: [docs/updating-firmware.md](docs/updating-firmware.md).
- Where things go: `tools/autoflash/` is the conversion tool (`python3 -m
  tools.autoflash` runs it) and `tools/repo/` the repository's own plumbing; `tools/espressif/` and `tools/beken/`
  hold each chip family's tooling, its emulator in `emulator/`; `tools/ewelink/`
  what talks to eWeLink whatever the chip; `devices/<plug>/` what is one plug's
  alone (its sheet, its ESPHome configs - at the paths converted plugs give
  Home Assistant, so a move means a new image - and modules); `tests/` mirrors `tools/`
  (`tests/autoflash/`, `tests/repo/`, `tests/ewelink/`, `tests/espressif/`,
  `tests/beken/`; `tests/run.py <directory>` runs one of them); `docs/` prose only;
  and `archive/` the work that got the project here and is no longer used,
  kept for the reasoning, linted like the rest.
- Docs, comments and commit messages match what is here already: plain,
  compact sentences saying what something does and why.

## Elsewhere

A bug found in another project - Tasmota, ESPHome, ESP-IDF, OpenBeken, the
emulator's QEMU - is reported there, and every pull request or issue opened
because of work here gets an entry in
[docs/upstream-contributions.md](docs/upstream-contributions.md) in the same
change, in that document's style: a row in the table of the topic it belongs
to (or a new topic, with a short introduction), giving the project, a link
and one line on what it fixes, and a note on how the fix was proven. Open the
pull request, then write the entry, so the link is real.

AI assistants working here: [AGENTS.md](AGENTS.md).
