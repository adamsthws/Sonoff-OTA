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

**The emulator.** The fault suite's starting plugs are converted again, now
with the new firmware - that is every conversion from Sonoff - and then the
faults that matter to a firmware update are run: at each step that writes,
starts or checks the new firmware, the network gone for 30 s, the power off
for 5 s and the computer crashing; and every run of the release people have
now, killed part way and finished by this one (`legacy-v<release>`). On a
test copy of a backed-up plug, with the cloud stub:

```sh
python -I tools/emulator/copy_plug.py --from MAC --to 12:34:56:ab:cd:ef
export AUTOFLASH_STUB_WORKERS=3
tools/emulator/fault_suite.py --plug=12:34:56:ab:cd:ef --stub-cloud prep
tools/emulator/fault_suite.py --stub-cloud list | awk '{print $1}' | grep -E \
  ':(E2|E3|F2|F4|G1|H1|I1|I2|L1|M1|X1|X2|R1):(outage-30|plugoff-5|crash-1)$|:legacy-v2\.0\.2$' \
  > firmware-update.txt
tools/emulator/fault_suite.py --plug=12:34:56:ab:cd:ef --stub-cloud batch firmware-update.txt
tools/emulator/fault_suite.py --stub-cloud report
```

Every scenario must pass. The release people have now is added to
`LEGACY_STEPS` in `tools/emulator/fault_suite.py` before the runs: its steps
from Sonoff by route, and their lengths as `ROUTES` has them, and to
`LEGACY_STARTS`. Then change `legacy-v2.0.2` above to it.

**A real plug**, converted from Sonoff to each firmware and between them,
before the release: an emulator does not show real flash, radios or timings
([testing.md](testing.md#real-plugs)).

### 5. Release

Raise the version in `tools/autoflash/__init__.py` to the changelog's; merging
to `main` publishes the release (`.github/workflows/release.yml`). Runs left
part way by the release before carry on with the new firmware by themselves:
the bundle is rebuilt for the new pins at A5, and a step whose report names
the old bundle is done again.
