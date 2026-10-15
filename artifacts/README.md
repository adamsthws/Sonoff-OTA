# Published firmware artifacts

Every file here is pinned in [`../SHA256SUMS`](../SHA256SUMS). Per-device
credentials and flash captures belong under the Git-ignored `captures/`
directory, never here.

| File | Purpose | SHA-256 |
|---|---|---|
| `s60-ota-bridge-4.4.2.ota` | Wrapped one-shot bridge the run installs; joins the home Wi-Fi stock was on, raising its own network only if that fails, lets the run back up the whole flash, and gives Tasmota its settings | `98dd677e405286a75a4b3ed0f1ce71407ed5cf2654a25b065f77be4dd62f5685` |
| `tasmota32c3-bluetooth.bin` | Berry-capable Tasmota used temporarily on the original partition layout | `a92cfadaff5a3c824f790471ba31464acd2076a24ff1fcad2de28574c5641708` |
| `s60-recovery-safeboot-imprintable.bin` | Credential-free recovery Safeboot template; saves the Wi-Fi for the official Tasmota installed after it | `aa32fafa4a9470152d5f8826fc6aedf94387575332353b50b8ce454ec1f9d364` |
| `tasmota-settings-template-15.6.0.0.bin` | Tasmota's default settings with no Wi-Fi in them, for the bridge to fill in at D1 | `d63f03cb99540407037f36dc8155ec9b02d24425d3b02c52299713233e98f4a5` |
| `esphome-s60-imprintable-1.0.0.bin` | ESPHome for the S60, installed instead of the final Tasmota when the run is asked for ESPHome; no Wi-Fi or passwords in it, for the run to imprint the plug's Wi-Fi | `ea054c7da18321f10f717ae6e4734552e442bd394d4749194ddff7fa603a0e0a` |

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
(`bridge 4.4.2`). `tools/build_vendor_ota.py` takes it from the image and
refuses any other. The next bridge is 4.4.3. 4.4.1 has the Wi-Fi behaviour tried on a
stock plug as "v6" on 8 October 2026; beyond that only its version, its name
and a larger buffer for the status reply differ. 4.4.2 is 4.4.1 with the
read-only `GET /flash` and `GET /efuse` of the research readout build, and
`GET /flash-sha256`, the SHA-256 of the whole flash worked out on the plug, which
read a stock plug's flash on hardware on 8 October 2026, added so the run can
keep a backup of the Sonoff firmware. 4.4.2 itself is first tried on hardware
by the next run.

The version is well above any Sonoff release so far (1.2.0). The stock 1.2.0
updater does not compare versions - the upgrade command decides what is
installed, and a plug on 1.2.0 took bridge v5 wrapped as `9.9.9` - but a later
Sonoff firmware might refuse anything older than itself, so it has to stay
higher than Sonoff's. The version field holds at most seven characters.

The earlier bridge v3, wrapped as `1.2.1`, is no longer published here. It
is in the Git history: `git log --all -- artifacts/s60-ota-bridge-v3-1.2.1.ota`
finds the last commit that has it.

The ESPHome image is built from [`../ESPHome/s60.yaml`](../ESPHome/s60.yaml)
(its `project` version is the one in the file name) with ESPHome 2026.9.1 and
ESP-IDF 5.5.5, as `esphome compile s60.yaml`; `firmware.ota.bin` is the file
published. Like the imprintable Safeboot it holds placeholders instead of a
network name and password, and the run writes the plug's Wi-Fi into them in
memory (`tools/autoflash/imprint.py`). It has no OTA password, web login or API
encryption key, as ESPHome's own ready-made images have none: the plug is open
on the home network until its owner adds them, as official Tasmota is. To
publish a new one, change the config and its `project` version, build it,
and replace the file, its row above and its line in `SHA256SUMS`.
