# Published firmware artifacts

Every file here is pinned in [`../SHA256SUMS`](../SHA256SUMS). Per-device
credentials and flash captures belong under the Git-ignored `captures/`
directory, never here.

| File | Purpose | SHA-256 |
|---|---|---|
| `s60-ota-bridge-4.4.3.ota` | Wrapped one-shot bridge the run installs; joins the home Wi-Fi stock was on, raising its own network only if that fails, lets the run back up the whole flash, gives Tasmota its settings, and starts even after an upload cut off part way | `5c020b8b1105d681d5ce95875d883c26104d360cf519694e79ddec36d74c787b` |
| `tasmota32c3-bluetooth.bin` | Berry-capable Tasmota used temporarily on the original partition layout | `a92cfadaff5a3c824f790471ba31464acd2076a24ff1fcad2de28574c5641708` |
| `s60-recovery-safeboot-imprintable.bin` | Credential-free recovery Safeboot template; saves the Wi-Fi for the official Tasmota installed after it | `aa32fafa4a9470152d5f8826fc6aedf94387575332353b50b8ce454ec1f9d364` |
| `tasmota-settings-template-15.6.0.0.bin` | Tasmota's default settings with no Wi-Fi in them, for the bridge to fill in at D1 | `d63f03cb99540407037f36dc8155ec9b02d24425d3b02c52299713233e98f4a5` |
| `esphome-sonoff-s60-bootloader-1.0.0.bin` | ESPHome's bootloader, from the same build: it replaces Sonoff's when ESPHome is installed, so ESPHome can roll back an update that fails to start | `3792bb665916d69c410c9fd10f69d1a6968e61f2a9e4969829a1c49f9ea6f433` |
| `esphome-sonoff-s60-imprintable-1.0.0.bin` | ESPHome for the S60, installed instead of the final Tasmota when the run is asked for ESPHome; no Wi-Fi or passwords in it, for the run to imprint the plug's Wi-Fi | `5b69737350aeffb140e915c6764213dca2ac30a2b4daa6754ce1454fb66b6c6b` |
| `esphome-sonoff-s60-mover-1.0.0.bin` | A small ESPHome the run uses for a minute or two when leaving Tasmota: it fits in front of Tasmota in Sonoff's layout, and takes the next firmware as an ordinary ESPHome update; two minutes away from the home Wi-Fi, it raises its own network, whose page takes new Wi-Fi details. Wi-Fi placeholders, like the image above | `14c3ce4426508ec697c4f87544f1830332ae8dbd59f3271197debd12e7f709bf` |
| `sonoff-s60-partition-table.bin` | Sonoff's partition table, read from a stock plug: the one every conversion starts from, which the Tasmota route's preflight requires byte for byte. Written back when a plug leaves Tasmota's layout. Holds no secrets | `f63f66bbf23b9e291c7eb5dcf24be820190dacf4bf52af515d9664526a4f4daf` |

The Bluetooth image is the exact file used by the successful migrations. It
was retrieved from Tasmota's official moving `install/firmware` branch at
`firmware/unofficial/tasmota32c3-bluetooth.bin`. Its embedded source identifier
is `f5b34a2`; corresponding source is
[`arendst/Tasmota@f5b34a26be51be2469737d2c43a62acb237c264f`](https://github.com/arendst/Tasmota/tree/f5b34a26be51be2469737d2c43a62acb237c264f).

The settings template is Tasmota 15.6.0's defaults, captured from a converted
plug with `tools/capture_tasmota_template.py` and tested on one with
`--verify`. Its Wi-Fi names, passwords, last access point, and addresses are
blank or Tasmota's defaults; it holds nothing about any network. It is only
used with the Berry Tasmota pinned above (15.6.0.1, same settings layout). If
either Tasmota pin changes, re-check and re-capture it: see
`tools/autoflash/tasmota_settings.py`.

The imprintable Safeboot is built from the pinned Tasmota source with
`repartition/recovery-safeboot/volatile-wifi-defaults.patch` and
`persist-settings.patch`. On its first boot after the repartition it wipes NVS -
by then only what the Sonoff firmware left and part of an earlier Tasmota
record - saves its settings, with the Wi-Fi, where Tasmota reads them, and
reads them back, so the official Tasmota installed next joins the network by
itself. It logs whether that worked, and the run records it at F2. It never
touches settings that are already there. The
saved record has only what a Safeboot sets, so F4 starts with `Reset 4`, which
rebuilds every other setting from official Tasmota's defaults and keeps the
Wi-Fi. A rebuild differs from the published file only in its build date, time
and the hashes covering them.

The imprintable Safeboot contains reserved placeholder bytes instead of real
Wi-Fi credentials. It **must not be flashed as published**: the private
bundle-preparation path must imprint and validate a per-device copy first.

From 4.4.1 on, a bridge is named by one version: the one the stock updater is
given, the one in its app description, and the one its status page reports
(`bridge 4.4.3`). `tools/build_vendor_ota.py` takes it from the image and
refuses any other. The next bridge is 4.4.4. 4.4.1 has the Wi-Fi behaviour tried on a
stock plug as "v6" on 8 October 2026; beyond that only its version, its name
and a larger buffer for the status reply differ. 4.4.2 is 4.4.1 with the
read-only `GET /flash` and `GET /efuse` of the research readout build, and
`GET /flash-sha256`, the SHA-256 of the whole flash worked out on the plug, which
read a stock plug's flash on hardware on 8 October 2026, added so the run can
keep a backup of the Sonoff firmware. 4.4.3 is 4.4.2 that starts even when
Sonoff's slot is no longer bootable - an upload of Tasmota into it cut off part
way - where 4.4.2 refused to start, leaving a plug with no network at all; it
reports `fallback: none` on its status page then. On its own network it also
tries the home network again every three minutes while nobody is joined to it,
and that network is open and called `Sonoff-Setup-XXXX` (the last two bytes of
the plug's Wi-Fi MAC), the name every setup network of this project's firmware
has - it was `S60-OTA-Bridge-XXXX`, with a fixed password. Tried on the
emulator; on hardware by the next run.

The version is well above any Sonoff release so far (1.2.0). The stock 1.2.0
updater does not compare versions - the upgrade command decides what is
installed, and a plug on 1.2.0 took bridge v5 wrapped as `9.9.9` - but a later
Sonoff firmware might refuse anything older than itself, so it has to stay
higher than Sonoff's. The version field holds at most seven characters.

The earlier bridge v3, wrapped as `1.2.1`, is no longer published here. It
is in the Git history: `git log --all -- artifacts/s60-ota-bridge-v3-1.2.1.ota`
finds the last commit that has it.

The ESPHome image is built from [`../ESPHome/sonoff_s60.yaml`](../ESPHome/sonoff_s60.yaml)
(its `project` version is the one in the file name) with ESPHome 2026.9.1 and
ESP-IDF 5.5.5, as `esphome compile sonoff_s60.yaml`; `firmware.ota.bin` is the file
published, and `bootloader/bootloader.bin` from the same build is the bootloader. The mover is
[`../ESPHome/sonoff_s60_mover.yaml`](../ESPHome/sonoff_s60_mover.yaml), built the same way. Like the imprintable Safeboot it holds placeholders instead of a
network name and password, and the run writes the plug's Wi-Fi into them in
memory (`tools/autoflash/imprint.py`). It has no OTA password, web login or API
encryption key, as ESPHome's own ready-made images have none: the plug is open
on the home network until its owner adds them, as official Tasmota is. To
publish a new one, change the config and its `project` version, build it,
and replace both files, their rows above and their lines in `SHA256SUMS`.
