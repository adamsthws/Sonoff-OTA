# Published firmware artifacts

Every file here is pinned in [`../SHA256SUMS`](../SHA256SUMS). Per-device
credentials and flash captures belong under the Git-ignored `captures/`
directory, never here.

| File | Purpose | SHA-256 |
|---|---|---|
| `s60-ota-bridge-v3-1.2.1.ota` | The previous bridge (its own network only); kept for the archived guides | `10d79d33856bb842b26f0a1b6748751c091ec9738a72dd4de2033fbd0c329ff7` |
| `tasmota32c3-bluetooth.bin` | Berry-capable Tasmota used temporarily on the original partition layout | `a92cfadaff5a3c824f790471ba31464acd2076a24ff1fcad2de28574c5641708` |
| `s60-recovery-safeboot-imprintable.bin` | Credential-free recovery Safeboot template; saves the Wi-Fi for the official Tasmota installed after it | `aa32fafa4a9470152d5f8826fc6aedf94387575332353b50b8ce454ec1f9d364` |
| `tasmota-settings-template-15.6.0.0.bin` | Tasmota's default settings with no Wi-Fi in them, for bridge v5 to fill in at D1 | `d63f03cb99540407037f36dc8155ec9b02d24425d3b02c52299713233e98f4a5` |

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

The bridge is wrapped as version `9.9.9`, well above any Sonoff release so far.
The stock 1.2.0 updater does not compare versions - the upgrade command decides
what is installed - but a later Sonoff firmware might refuse anything older than
itself, and a high version keeps working if it does. It begins with `9` so it
also comes out higher if versions are compared as text rather than numbers. The
version field holds at most seven characters. Only the version and the two
wrapper checksums differ from the 1.2.1 file; the bridge inside is byte for byte
the same.
