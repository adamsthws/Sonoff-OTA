# Updating the firmware the tool installs

A converted plug ends on official Tasmota or on this project's ESPHome. Both
follow their projects' releases: each new stable release of either is taken
the same way, by the steps below, and goes out as a release of the tool.

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
`tools/safeboot_migration.py`, which need not be the official release's: the
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

## When

```sh
python3 tools/update_firmware.py check
```

says what `firmware.json` pins and each project's latest stable release, and
exits 1 when either is behind. Only stable releases are taken: never an
ESPHome beta (`2026.10.0b1`) or a Tasmota development build. Both projects
often follow a release with a fix within a week or so (ESPHome's `.1`); one
that has been out a few days is the safer pick.

## Steps

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
  (`TARGET_PARTITIONS`, the Berry phases in `repartition/berry/`) was reviewed
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

It needs Docker: the image is built in ESPHome's own image for that release
(`ghcr.io/esphome/esphome:<release>`), pinned by its digest, so the build does
not depend on what is installed here. It checks, then builds:

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

### 3. By hand

The update lists these when it finishes; `tests/test_firmware_pins.py` fails
on most of them while they are missing:

- `artifacts/README.md`: each changed file's row (its name and SHA-256), and a
  paragraph on the new build: what changed, and that the config did not.
- `THIRD_PARTY_NOTICES.md`: each ESPHome file's name, the ESPHome release and
  the ESP-IDF version it was built with.
- `ESPHome/README.md`: the ESPHome release that built the current image.
- `README.md` and `docs/`: any line naming the release installed.
- `CHANGELOG.md`: a section for the next minor version - it adds something -
  saying which release each firmware is now, with a link to its release notes.

### 4. Test

**Unit tests**, in a checkout with no emulator state linked in:

```sh
python3 -m unittest discover -s tests
```

**The emulator**, from just before the final firmware: nothing before it
changes, and it was tested by the fault suite when it last did. Two public
plugs ([emulator.md](emulator.md#public-plugs)) - one after E3 on the way to
Tasmota, one after D5 on the way to ESPHome - are taken to their final
firmware (F2 to H1, and I1 and I2), and the plugs that makes are then taken
each to the other (L1 to I2, and X1 to H1): every step that writes, starts,
checks or runs code on the final firmware, in about five minutes on two
emulators. The plugs hold nothing of Sonoff's, so no cloud is needed, real or
stub, nor an account:

```sh
python -I tools/emulator/public_plugs.py test
```

All four must convert. The faults are not put in their way: they test the
tool's own recovery, which a new final firmware does not change. Where the
recovery leans on the firmware's own behaviour, a major release (16 to 17,
say) or release notes touching its Wi-Fi, settings, updates or Berry, run
those few scenarios of the fault suite too - F4's reset, G1, `seven-times`,
and the network gone at L1 and X1 ([testing.md](testing.md#the-fault-suite)).

**A real plug**, converted from Sonoff to each firmware and between them,
before the release: an emulator does not show real flash, radios or timings
([testing.md](testing.md#real-plugs)).

### 5. Release

Raise the version in `tools/autoflash/__init__.py` to the changelog's; merging
to `main` publishes the release (`.github/workflows/release.yml`). Runs left
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
