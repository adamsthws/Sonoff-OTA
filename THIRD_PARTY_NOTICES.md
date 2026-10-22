# Third-party software and corresponding source

## Tasmota-derived firmware

Tasmota is distributed under the GNU General Public License v3.0 only.

`artifacts/tasmota32c3-bluetooth.bin` is an unmodified Tasmota ESP32-C3
Bluetooth build retrieved from Tasmota's official `install` repository,
`firmware/unofficial/tasmota32c3-bluetooth.bin`. That moving branch no longer
serves these exact bytes. The image itself embeds source identifier `f5b34a2`;
the corresponding source is upstream commit
[`f5b34a26be51be2469737d2c43a62acb237c264f`](https://github.com/arendst/Tasmota/tree/f5b34a26be51be2469737d2c43a62acb237c264f).
Its build environment is `tasmota32c3-bluetooth` in
[`platformio_tasmota_cenv_sample.ini`](https://github.com/arendst/Tasmota/blob/f5b34a26be51be2469737d2c43a62acb237c264f/platformio_tasmota_cenv_sample.ini).

`artifacts/autoflash-recovery-safeboot-imprintable.bin` is a custom Safeboot derived
from upstream Tasmota commit
[`db3ae7e0276fdc38b7aeb241a2a8e33d8ffd6892`](https://github.com/arendst/Tasmota/tree/db3ae7e0276fdc38b7aeb241a2a8e33d8ffd6892).
The corresponding project changes and build tooling are
`repartition/recovery-safeboot/volatile-wifi-defaults.patch`,
`repartition/recovery-safeboot/persist-settings.patch`,
`tools/configure_imprintable_safeboot.py`, and
`tools/build_recovery_safeboot.sh`.

The older custom `s60-tasmota-15.6.0-*.bin` builds and their corresponding
configuration, patch, documentation and helper are retained together under
`archive/`.

## ESPHome firmware

ESPHome's firmware code is distributed under the GNU General Public License
v3.0.

`artifacts/esphome-sonoff-s60-imprintable-1.3.0.bin` is built with ESPHome
2026.10.0 and ESP-IDF 5.5.6 from this project's config
[`ESPHome/sonoff_s60.yaml`](ESPHome/sonoff_s60.yaml), with `esphome compile`.
Its corresponding source is the
[ESPHome 2026.10.0 release](https://github.com/esphome/esphome/tree/2026.10.0)
and that config.

`artifacts/autoflash-mover-1.1.0.bin` is built the same way with ESPHome
2026.9.1 and ESP-IDF 5.5.5 from
[`ESPHome/autoflash_mover.yaml`](ESPHome/autoflash_mover.yaml), and
`artifacts/esphome-sonoff-s60-bootloader-1.2.0.bin` is the ESP-IDF bootloader
from the build of the S60's config at 1.2.0, with the same ESPHome and
ESP-IDF. Their corresponding source is the
[ESPHome 2026.9.1 release](https://github.com/esphome/esphome/tree/2026.9.1)
and those configs (the S60's at 1.2.0 is in this repository's history).

How they are built and published is in
[`artifacts/README.md`](artifacts/README.md) and
[`docs/updating-firmware.md`](docs/updating-firmware.md).

## OTA bridge

`artifacts/autoflash-bridge-sonoff-s60-4.4.4.ota` is this project's own firmware, under
this repository's licence ([`LICENSE`](LICENSE)). Its source is in
`archive/bridge/`, and `tools/build_vendor_ota.py` wraps it for the stock
updater. It is built with ESP-IDF through PlatformIO, as pinned by
`archive/bridge/platformio.ini`.

## ESP-IDF

The bridge and the ESPHome images include Espressif's ESP-IDF, which is
distributed under the
[Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0). ESP-IDF's
bundled components keep their own copyright notices and licences, listed in
the ESP-IDF source for the version each build names.

## Data files

`artifacts/tasmota-settings-template-15.6.0.0.bin` is Tasmota 15.6.0's default
settings, with no network details in them, captured from a converted plug.

`artifacts/sonoff-s60-partition-table.bin` is the standard ESP-IDF partition
table every S60 ships with, read from a stock plug: a list of partition names,
offsets and sizes, with no code or settings in it. Why the project keeps it is
in [`docs/legal.md`](docs/legal.md).

## Availability of source

The Tasmota and ESPHome sources above are named by upstream commit or release.
If any of them stops being available upstream, ask in an issue and the project
will provide the corresponding source for any binary it has published.

## Trademarks

Sonoff and eWeLink are trademarks of their respective owners. This project is
independent and is not an official Sonoff, eWeLink, Tasmota or ESPHome
installer.
