# Updating the firmware the tool installs

A converted plug ends on official Tasmota or on this project's ESPHome. Both
follow their projects' releases, a little behind: the repository's
`firmware-update` workflow takes each once it has settled, tests it and
releases it, by itself ([below](#by-itself)). By hand, it is the same steps
([after that](#by-hand)).

## What follows a release, and what does not

| Firmware | On the plug | Follows | Pinned in |
|---|---|---|---|
| Official Tasmota: `tasmota32c3.bin`, `tasmota32c3-safeboot.bin` (and `tasmota32c3.factory.bin`, for its partition table) | for good | every Tasmota release | [`firmware.json`](../firmware.json) |
| ESPHome for the S60 (`artifacts/esphome-sonoff-s60-imprintable-*.bin`) | for good | every ESPHome release | `firmware.json` |
| ESPHome's bootloader (`artifacts/esphome-sonoff-s60-bootloader-*.bin`) | for good | only when it has to change | `firmware.json` |
| The mover (`artifacts/autoflash-mover-*.bin`) | a minute or two | only when its config changes | `firmware.json` |
| The Berry Tasmota, the recovery Safeboot, the settings template, the bridge | minutes | no | [`artifacts/README.md`](../artifacts/README.md) says where, and how each is changed |

The official Tasmota files are not kept here: each run downloads them from
the release's own page on GitHub and checks them against `firmware.json`. The
ESPHome builds are: the run writes the plug's Wi-Fi into a copy of each.

**The interim firmware** - everything a plug runs only part way through a
conversion - stays as it is. Each has been through every fault the suite puts
in its way, a change to one means all of that again, and none of it is left
on a plug. The Tasmota they are made from is `INTERIM_TASMOTA_COMMIT` in
`tools/espressif/safeboot/safeboot_migration.py`, which need not be the official release's: the
recovery Safeboot saves the Wi-Fi in its release's settings record, and the
official release takes that record and moves it on to its own layout, as
Tasmota does on any upgrade - as long as `CFG_HOLDER` is the same in both,
which the update checks.

**The bootloader** is written in about a second, and a power cut in that
second leaves a plug that has to be opened - the only step of a conversion
that can. So a new ESPHome release brings a new app, and the published
bootloader stays, unless the new one fixes something an S60 needs (an ESP32-C3
with no secure boot or flash encryption); the bootloader of ESP-IDF 5.x starts
the apps of later 5.x releases. Replacing it is a decision of its own:
`--new-bootloader`, then the whole fault suite and real plugs.

## By itself

Every Monday `.github/workflows/firmware-update.yml` asks which release of each
project has **settled** (`python3 tools/update_firmware.py due`). A release has
settled when it is

1. a stable release - never a beta, a pre-release or a development build;
2. the newest of its series (Tasmota's `16.0.x`, ESPHome's month, `2026.10.x`):
   no later point release; and
3. at least 7 days old, and either a newer series has begun or it is at least
   14 days old (`SETTLE_MIN_DAYS`, `SETTLE_DAYS`).

A fix for a bad release has come within days or not at all - Tasmota 14.4.0
was followed by 14.4.1 in four days, 15.0.0 by 15.0.1 in one, each `.0`
withdrawn - so this lets it come first. For Tasmota, a release every two months
or so, it means 14 quiet days; for ESPHome, a series a month, the month
before's last point release once the new month's `.0` is out: always a month
behind, on a series that has had its fixes. A release listed under `hold` in
`firmware.json` (`"hold": {"tasmota": ["16.1.0"]}`) is never taken.

What is due, it makes into a pull request: the update (steps 1 and 2 below,
its docs and changelog line written, the version raised a minor version),
then the unit tests and the final-firmware emulator test (step 4). Nothing is
pushed if the leak scan finds anything in it. The pull request says which
releases, what each check found, and the tests' results; the workflow then
starts the lint, leak-scan and emulator-check workflows on its branch, as a
pull request opened with a workflow's own token starts none itself, so the
checks `main` requires are there. One whose tests passed **merges itself 7
days after it was opened, which is the release**, unless it is labelled
`hold`; one whose tests failed, or a check `main` requires, is labelled
`tests-failed` and never merged by the workflow. A pull request is merged only
if `main` has not moved on since it was made and tested; otherwise it is
closed, and the next week's run makes it again.

The same run checks the pinned Tasmota release is still published - each
conversion downloads its files - and opens an issue if it has gone, taking its
series' newest release at once where there is one. It also opens one once
nothing has reached `main` for 50 days: GitHub switches a public repository's
schedules off after 60 days with no activity, this one with them.

By hand, from the Actions tab: `tasmota` or `esphome` takes a given release
whether settled or not, and `dry_run` makes and tests the update and opens
nothing. `python3 tools/update_firmware.py check` says what is pinned and what
is latest.

## By hand

Work on a branch from `main`. Each step below is one commit.

### 1. Tasmota

```sh
python3 tools/update_firmware.py tasmota            # the latest release
python3 tools/update_firmware.py tasmota 16.0.0     # or that one
```

It reads the release from GitHub (refusing a draft or pre-release) and its
tag's commit, downloads its three ESP32-C3 files, and checks:

- the factory image's partition table is, byte for byte, the one the
  conversion writes (`TARGET_TABLE_SHA256`), and the Safeboot and the app in
  it are the release's own files, at the offsets the conversion writes them
  to;
- the Safeboot fits its partition and the app fits app0, each a valid ESP32-C3
  image;
- `CFG_HOLDER` in the release's `my_user_config.h` is the one the recovery
  Safeboot's settings record has (`tools/autoflash/tasmota_settings.py`).

Then it writes the release, its commit and each file's size and SHA-256 into
`firmware.json`. `--dry-run` checks without writing.

Where it stops:

- **Another partition table**: Tasmota has changed its layout. The repartition
  (`TARGET_PARTITIONS`, the Berry phases in `tools/espressif/safeboot/repartition/berry/`) was reviewed
  for this one; a new layout is a new design, not an update.
- **Another `CFG_HOLDER`**: the release would start without the Wi-Fi the
  recovery Safeboot saved, and every conversion would end on its setup
  network. The recovery Safeboot would have to be rebuilt from that release
  (`artifacts/README.md`).
- **A file too large**: the Safeboot or app no longer fits Tasmota's own
  layout - which Tasmota would have changed first.

### 2. ESPHome

```sh
python3 tools/update_firmware.py esphome            # the latest release
python3 tools/update_firmware.py esphome 2026.10.0 --image-version 1.3.0
```

A change to `ESPHome/sonoff_s60.yaml` is published the same way, naming the
release already pinned, so that only the config changes.

It needs Docker: the image is built in ESPHome's own image for that release
(`ghcr.io/esphome/esphome:<release>`), pinned by its digest, so the build does
not depend on what is installed on the computer building it. It checks, then
builds:

- the parts of ESPHome that `ESPHome/sonoff_s60.yaml` reaches into beyond its
  options - the fixed key its last `on_boot` lambda saves the Wi-Fi under, and
  the layout of what it saves ([ESPHome/README.md](../ESPHome/README.md) says
  why) - are as they were; if not, the lambda must be checked first;
- the config's `project` version is raised (the next minor version, or
  `--image-version`), and it is built;
- the app is an ESP32-C3 app that fits Sonoff's slot, says it is that ESPHome
  release, holds each Wi-Fi placeholder once and untouched and takes a Wi-Fi
  written in, and holds no path from the computer that built it;
- its bootloader is compared with the published one. The same: kept as it is.
  Different (a new ESP-IDF): the published one is kept, and it says so - see
  above.

Then it publishes the image in `artifacts/` (removing the one before), and
updates its line in `SHA256SUMS`, the config's version and `firmware.json`.
`--mover` builds and publishes the mover the same way, only for a change to
its config. `--dry-run` builds and checks, and publishes nothing. ESP-IDF and
its tools are downloaded once, to `captures/esphome-build/`.

An ESPHome build is not byte for byte repeatable - it holds the time it was
built and where - so what pins a build is the Docker image it was made in, in
`firmware.json`. The bootloader is repeatable: built again with the release
that made the published one, it comes out the same.

### 3. The docs

Each update writes what it changed, and `tests/repo/test_firmware_pins.py` fails
while any of it is missing:

- `artifacts/README.md`: the image's row (its name and SHA-256), the ESPHome
  release that builds it, and a paragraph on the new build;
- `THIRD_PARTY_NOTICES.md`: the image's name, ESPHome release and ESP-IDF
  version;
- `ESPHome/README.md`: the ESPHome release that built the current image;
- `CHANGELOG.md`: a line for each firmware, with a link to its release notes,
  under the release to come - a new section, a minor version on, once the one
  above has been released, with `tools/autoflash/__init__.py` raised to it.

For a change to the config, built with the release already pinned, they say
the config changed, and what the change does is left to write by hand in
`CHANGELOG.md` and `artifacts/README.md`.

Each passage is found and rewritten, and if one is not found nothing is
changed: `--no-docs` leaves them all to be written by hand, as a change to
the mover or the bootloader (`--mover`, `--new-bootloader`) needs anyway. The
README's badges read `firmware.json` on `main`, and change by themselves.

### 4. Test

**Unit tests**, in a checkout with no emulator state linked in:

```sh
python3 tests/run.py
```

**The emulator**, from just before the final firmware: nothing before it
changes, and it was tested by the fault suite when it last did. Two public
plugs ([emulator.md](emulator.md#public-plugs)) - one after E3 on the way to
Tasmota, one after D5 on the way to ESPHome - are taken to their final
firmware (F2 to H1, and I1 and I2), and the plugs that makes are then taken
each to the other (L1 to I2, and X1 to H1): every step that writes, starts,
checks or runs code on the final firmware, in four or five minutes on two
emulators - one for each way, each going on to its second run as soon as its
first is done. The plugs hold nothing of Sonoff's, so no cloud is needed, real or
stub, nor an account:

```sh
python -I tools/espressif/emulator/public_plugs.py test
```

All four must convert. The repository's `emulator-check` workflow runs the
same, with the unit tests, on GitHub's own runners, from the published public
plugs and emulator image, whenever the tool or its firmware changes. The faults are not put in their way: they test the
tool's own recovery, which a new final firmware does not change. Where the
recovery leans on the firmware's own behaviour, a major release (16 to 17,
say) or release notes touching its Wi-Fi, settings, updates or Berry, run
those few scenarios of the fault suite too - F4's reset, G1, `seven-times`,
and the network gone at L1 and X1 ([testing.md](testing.md#the-fault-suite)).

**A real plug**, converted from Sonoff to each firmware and between them,
before the release: an emulator does not show real flash, radios or timings
([testing.md](testing.md#real-plugs)).

### 5. Release

The update raised the version in `tools/autoflash/__init__.py` to the
changelog's; merging to `main` publishes the release
(`.github/workflows/release.yml`), once the tests, lint and leak scan pass on
`main` again, with the firmware files attached. Runs left
part way by the release before carry on with the new firmware by themselves:
the bundle is built again for the new pins - at A5, or as a run carried on
past A5 starts - and a step whose checks were made with the old bundle does
them again. One that had installed the official app already (F2) finishes
with its bundle - or, if this version must build that again (1.2.0's, from
another startup firmware), one of the same release - on the release it
began with, read back against that
release's files as any other, and says so: `firmware.json` keeps each
release pinned before (`earlier`) by its files, and `update_firmware.py
tasmota` adds the one it replaces.
