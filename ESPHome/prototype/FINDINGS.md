# ESPHome route: findings

Notes from reviewing and testing [PR #4, "add ESPHome support"](https://github.com/adamsthws/Sonoff-OTA/pull/4)
(October 2026). PR #4 was merged into this branch, `esphome`, where the work
continues. The prototype of a pre-built image described here became
`ESPHome/sonoff_s60.yaml`, which p1otr extended (overload
protection, a countdown, more energy sensors); this folder keeps these notes
and the scripts used to test it.

## The PR as submitted

The PR adds `--esphome firmware.ota.bin`: the usual stages up to the temporary
Tasmota (D1), then the user's own ESPHome build uploaded through Tasmota's
firmware upgrade, leaving the plug on Sonoff's original partition table.

| Test | Result |
|---|---|
| Emulator, Linux computer (S60TPG, stock 1.2.0) | stock to ESPHome in 1m 11s, 8 stages; booted from 0x210000, table unchanged |
| Relay through ESPHome's web API | switched off and on |
| A second update, ESPHome to ESPHome | written to 0x20000 and booted: updates alternate slots |
| Emulator, Windows computer (Server 2025) | conversion completed with an image built on Windows |
| Test suite | one failure: `I1` is in `DESTRUCTIVE_STEPS` but not in the default `STEPS` |

Issues found (detailed in the review on the PR): the run doesn't remember it
is an ESPHome run; the ending, hardware check and report are still Tasmota's;
stages 7 and 8 and the opening lines print too much in normal mode; the wait
step's bare `raise`, its dry run and its "anything not Tasmota" check; the
example config's missing `api:` and hard-coded S60TPF name; `ESPHome/start.sh`
works from neither folder.

## Building ESPHome per user doesn't fit the tool

Measured with ESPHome 2026.9.1 (ESP-IDF 5.5.5):

- **Size:** about 3.5 GB: ESP-IDF and its tools 3.0 GB, the build 0.2 GB,
  Python and packages 0.3 GB.
- **Time:** 11 minutes on Windows (2 virtual CPUs), including downloads.
- **Python:** ESPHome needs 3.12-3.14 with `pip` and `venv`. The Windows start
  script's embeddable Python has neither, and the "any Python 3.10 or newer"
  fallback can be too old.

[uv](https://github.com/astral-sh/uv) 0.13.0 solves the Python part: one pinned
download (SHA-256 published) that fetches its own Python 3.12 and runs a pinned
ESPHome. With `UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR`, `UV_TOOL_DIR` and
`ESPHOME_ESP_IDF_PREFIX`, everything stays in one folder - nothing was written
anywhere else on Linux or Windows. On Windows the folder needs a short path
(ESP-IDF's folders are deep; the build worked with `LongPathsEnabled` off at
`C:\S60\b`). On macOS, `start.sh` uses python.org's signed Python, which uv can
use with `--python`; macOS is untested.

Even so, 3.5 GB and 11 minutes per user is the opposite of fast and
leave-no-trace, so the image should be built once and shipped.

## A pre-built image, with the Wi-Fi imprinted

The tool already ships a pre-built firmware with no Wi-Fi in it: the bridge.
`tools/autoflash/imprint.py` writes the plug's Wi-Fi into placeholders reserved
at build time and recalculates the checksum and SHA-256. The prototype (now
`ESPHome/sonoff_s60.yaml`) does the same for ESPHome:

- `s60_imprint.h` held the bridge's placeholders (in the published config they
  are in its boot lambda, so Home Assistant can adopt it from that one file):
  `S60-IMPRINT-SSID>` plus 33
  filler bytes, `S60-IMPRINT-PASS>` plus 64.
- The config has no network; `on_boot` hands the imprinted values to ESPHome's
  `wifi.configure` action, whose name and password are lambdas, so the config
  fields' 32 and 63 character limits don't apply. `ap:` stays as a fallback
  hotspot.
- No passwords: no OTA password, web login or API encryption key, as in
  ESPHome's own ready-made image (`esphome/firmware`, `esphome-web`) and as
  Tasmota starts. Owners add them through Home Assistant adoption or their
  own build.

Tested on the emulator (ESPHome 2026.9.1; image 889,984 bytes, 44% of a slot):

| Test | Result |
|---|---|
| `imprint.py` on the ESPHome image | unchanged code; one of each placeholder, checksum and SHA-256 valid, same length, no build paths |
| Imprinted, installed over ESPHome's web updater | joined the Wi-Fi in 5 s; web page, updates (3232) and API (6053) open, no login |
| Power cut | back on the Wi-Fi in 5 s |
| What the firmware reads, settings erased first | exactly the imprint: "PICSimLabWifi (13 characters)", "NoSuchNetwork (13 characters)" |
| Template never imprinted, settings erased | stays off the Wi-Fi; its hotspot `sonoff-s60-<mac>` appears |
| Bad imprint over a plug that has saved Wi-Fi | after 30 s, `wifi.configure` goes back to the saved network |

Details that matter:

- **`on_boot` needs `priority: -100`.** The default, 600, runs before ESPHome
  sets Wi-Fi up (250).
- **Read the placeholders through a `volatile` pointer.** The compiler knows
  their build-time text and could fold in its length or contents.
- **`save: true` is cheap.** `wifi.configure` does nothing if the plug is
  already on that network, so settings aren't rewritten at every boot. With no
  network in the config, ESPHome saves Wi-Fi under a fixed key (88491487), so
  it survives updates of the same image.

Not shown by the emulator: a wrong name or password against a real router. The
emulator's Wi-Fi is open and joins its own network whatever name is asked for,
so "NoSuchNetwork" still came online (in 25 s rather than 5). By ESPHome's code,
a fresh plug with a wrong imprint falls back to its hotspot; that needs a real
plug to confirm.

## Other findings

- **ESPHome's official images** (`esphome/firmware` releases, e.g.
  `esphome-web-esp32c3.ota.bin`, with SHA-256s) are blank: no relay, meter or
  web page, Wi-Fi set afterwards through a hotspot, Bluetooth or USB. They
  could be pinned like official Tasmota, but leave a plug that doesn't switch
  until someone builds a config.
- **The power meter is a CSE7766 on GPIO7 in both models.** The tool's Tasmota
  template is the same for the S60TPG and the S60TPF, and two S60TPF test
  reports in [discussion #1](https://github.com/adamsthws/Sonoff-OTA/discussions/1)
  read 236 V and 232 V with it.
- **Firmware choice prompt:** `firmware_choice_mockup.py` shows the question
  with the tool's own `section()` and `ask_choice()`.

## Still to do on real hardware

- A wrong Wi-Fi name or password: the plug should raise its hotspot.
- A WPA2 network (the emulator has open networks only).
- A full conversion with the imprinted image through Tasmota's upload.

## Files

| File | What it is |
|---|---|
| `imprint_image.py` | imprints a built image with the tool's `imprint.py` |
| `emulator_flash.py` | erases the emulated plug's settings, or writes an image to a slot, for control tests |
| `windows-uv-build.ps1` | builds `ESPHome/sonoff_s60.yaml` on Windows with a portable uv, all under `C:\S60\b` (tested with the prototype config: 889,984 bytes, as on Linux; 6 minutes with ESP-IDF already downloaded) |
| `firmware_choice_mockup.py` | the Tasmota or ESPHome question, as the tool would ask it |

The config itself is `ESPHome/sonoff_s60.yaml`, one self-contained file. Building on Linux, from `ESPHome/`: `esphome compile sonoff_s60.yaml`, then
`python3 prototype/imprint_image.py .esphome/build/sonoff-s60/build/firmware.ota.bin imprinted.ota.bin "Your Wi-Fi" "password"`.
