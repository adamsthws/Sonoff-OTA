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
- **A security problem**: privately, never as an issue - see [SECURITY.md](SECURITY.md).

## Setting up

The tool needs nothing but Python 3.10 or newer. Working on it needs a few
tools more - the test helpers and the linters - which go in a virtual
environment in the checkout, never into the system's Python:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements/ci.txt -r requirements/lint.txt
python3 tools/leak_scan.py install
```

The last line points Git at the hooks in `.githooks/`: before each commit
they scan what is staged for secrets and real identifiers, and lint it;
before each push they scan every commit it sends.
[uv](https://docs.astral.sh/uv/) works as well and faster, from the same
files: `uv venv` and `uv pip install --require-hashes -r ...`.

Optional, for what they check: Docker, for the ESPHome configs' check and
the emulator; PowerShell 7.4.6 or newer (`pwsh`), with
`Install-Module PSScriptAnalyzer`, for the PowerShell scripts. A commit on a
computer without them is not stopped - the hook says what it skipped, and
the workflows run them all.

## Testing

```sh
python3 tests/run.py          # the unit tests, about 11 s
python3 tools/lint.py all     # every linter on every file
```

Every fix comes with a test that fails without it. The tests run side by
side and in any order, so a new one leaves nothing behind, keeps a port it
picked, and touches nothing of the whole computer's:
[docs/testing.md](docs/testing.md#side-by-side-in-any-order). The emulator -
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
  example belongs in `.leak-scan-allow`; nothing real is ever let through
  with `--no-verify`.
  [docs/technical-details.md](docs/technical-details.md#keeping-secrets-out-of-git)

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
- `firmware.json`, `artifacts/` and `SHA256SUMS` change only through
  `tools/update_firmware.py` - most often run by the weekly firmware-update
  workflow: [docs/updating-firmware.md](docs/updating-firmware.md).
- `archive/` is kept as it was, for the record: it is not linted or changed.
- Docs, comments and commit messages match what is here already: plain,
  compact sentences saying what something does and why.

## Elsewhere

A bug found in another project - Tasmota, ESPHome, ESP-IDF, the emulator's
QEMU - is reported there, and recorded in
[docs/upstream-contributions.md](docs/upstream-contributions.md).

AI assistants working here: [AGENTS.md](AGENTS.md).
