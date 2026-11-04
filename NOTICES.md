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
`tools/espressif/safeboot/repartition/recovery-safeboot/volatile-wifi-defaults.patch`,
`tools/espressif/safeboot/repartition/recovery-safeboot/persist-settings.patch`,
`tools/espressif/safeboot/configure_imprintable_safeboot.py`, and
`tools/espressif/safeboot/build_recovery_safeboot.sh`.

The older custom `s60-tasmota-15.6.0-*.bin` builds and their corresponding
configuration, patch, documentation and helper are retained together under
`archive/`.

## ESPHome firmware

ESPHome's firmware code is distributed under the GNU General Public License
v3.0.

`artifacts/esphome-sonoff-s60-imprintable-1.6.0.bin` is built with ESPHome
2026.10.0 and ESP-IDF 5.5.6 from this project's config
[`devices/sonoff-s60/esphome.yaml`](devices/sonoff-s60/esphome.yaml), with `esphome compile`.
Its corresponding source is the
[ESPHome 2026.10.0 release](https://github.com/esphome/esphome/tree/2026.10.0)
and that config.

`artifacts/autoflash-mover-1.1.0.bin` is built the same way with ESPHome
2026.9.1 and ESP-IDF 5.5.5 from
[`devices/sonoff-s60/mover.yaml`](devices/sonoff-s60/mover.yaml), and
`artifacts/esphome-sonoff-s60-bootloader-1.2.0.bin` is the ESP-IDF bootloader
from the build of the S60's config at 1.2.0, with the same ESPHome and
ESP-IDF. Their corresponding source is the
[ESPHome 2026.9.1 release](https://github.com/esphome/esphome/tree/2026.9.1)
and those configs (the S60's at 1.2.0 is in this repository's history).

`artifacts/esphome-sonoff-s61-1.1.0.rbl` is built with ESPHome 2026.10.0 and
LibreTiny 1.13.0 from this project's config
[`devices/sonoff-s61/esphome.yaml`](devices/sonoff-s61/esphome.yaml), with
`esphome compile`, the rt_ota image LibreTiny's build makes. Its corresponding
source is the [ESPHome 2026.10.0 release](https://github.com/esphome/esphome/tree/2026.10.0),
[LibreTiny 1.13.0](https://github.com/libretiny-eu/libretiny/tree/v1.13.0)
(MIT), the Beken SDK LibreTiny builds on, and that config.

How they are built and published is in
[`artifacts/README.md`](artifacts/README.md) and
[`docs/updating-firmware.md`](docs/updating-firmware.md).

## OpenBeken firmware

`artifacts/openbeken-sonoff-s61-1.1.0.rbl` is OpenBeken
([openshwprojects/OpenBK7231T_App](https://github.com/openshwprojects/OpenBK7231T_App))
at commit
[`ce1abad8dc931720167a7628840adae30b92afaf`](https://github.com/openshwprojects/OpenBK7231T_App/tree/ce1abad8dc931720167a7628840adae30b92afaf)
with its Beken SDK
([NonPIayerCharacter/beken_freertos_sdk](https://github.com/NonPIayerCharacter/beken_freertos_sdk))
at commit
[`f5a64d4cda03bac6d885f4f30c27bbd3435c5e3a`](https://github.com/NonPIayerCharacter/beken_freertos_sdk/tree/f5a64d4cda03bac6d885f4f30c27bbd3435c5e3a),
built for the BK7238 with a 4 MB flash. This project's changes to each are
the patches in [`tools/beken/openbeken/`](tools/beken/openbeken/) (the 4 MB
table, offered upstream as OpenBK7231T_App#2290 and beken_freertos_sdk#13,
and a hook before an OTA's restart, offered as OpenBK7231T_App#2293), the driver
[`tools/beken/openbeken/drv_autoflash.c`](tools/beken/openbeken/drv_autoflash.c)
and the S61's table for it,
[`devices/sonoff-s61/drv_autoflash_sonoff_s61.c`](devices/sonoff-s61/drv_autoflash_sonoff_s61.c),
built into the image; `firmware.json` records the commits and each file's
hash.

## OTA bridge

`artifacts/autoflash-bridge-sonoff-s60-4.4.4.ota` is this project's own firmware, under
this repository's licence ([`LICENSE`](LICENSE)). Its source is in
`archive/bridge/`, and `tools/espressif/build_vendor_ota.py` wraps it for the stock
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

## Website stylesheet

`website/public/assets/github-markdown-dark.css` and
`website/public/assets/github-markdown-light.css` are
the dark and light stylesheets of
[github-markdown-css](https://github.com/sindresorhus/github-markdown-css)
5.9.0, unmodified, which the web page at firmwareliberator.com uses to show
the README as GitHub does. It is distributed under the MIT License:

> Copyright (c) Sindre Sorhus <sindresorhus@gmail.com> (https://sindresorhus.com)
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to
> deal in the Software without restriction, including without limitation the
> rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
> sell copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in
> all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
> FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
> IN THE SOFTWARE.

## Availability of source

The Tasmota and ESPHome sources above are named by upstream commit or release.
If any of them stops being available upstream, ask in an issue and the project
will provide the corresponding source for any binary it has published.

## Trademarks

Sonoff and eWeLink are trademarks of their respective owners. The GitHub
mark on the web page's GitHub button (the `mark-github` icon of GitHub's
Octicons, MIT) links to the repository, as GitHub's logo guidelines allow. This project is
independent and is not an official Sonoff, eWeLink, Tasmota or ESPHome
installer.

`docs/PublicSponsor.png` is GitHub's Public Sponsor achievement badge, a copy
of GitHub's own image, shown in the README to point to GitHub Sponsors. GitHub
and GitHub Sponsors are trademarks of GitHub, Inc.
