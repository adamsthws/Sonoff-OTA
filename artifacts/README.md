# Published firmware artifacts

Every file here is pinned in [`SHA256SUMS`](SHA256SUMS) beside it (`sha256sum -c SHA256SUMS` in this directory), and the versions the tool installs in [`firmware.json`](firmware.json). Per-device
credentials and flash captures belong under the Git-ignored `captures/`
directory, never here.

| File | Purpose | SHA-256 |
|---|---|---|
| `autoflash-bridge-sonoff-s60-4.4.4.ota` | Wrapped one-shot bridge the run installs; joins the home Wi-Fi stock was on, raising its own network only if that fails, lets the run back up the whole flash, gives Tasmota its settings, and starts even after an upload cut off part way | `4424af5d59258f5c1998269c65bffc5d1341f519352fdc1bfd9c76e37b34f5bb` |
| `tasmota32c3-bluetooth.bin` | Berry-capable Tasmota used temporarily on the original partition layout | `a92cfadaff5a3c824f790471ba31464acd2076a24ff1fcad2de28574c5641708` |
| `autoflash-recovery-safeboot-imprintable.bin` | Credential-free recovery Safeboot template; saves the Wi-Fi for the official Tasmota installed after it | `ece36fd964c1443ef9fb74af87a0a257501c9e5ecfb1fb7a1c3aff1744bc29fd` |
| `tasmota-settings-template-15.6.0.0.bin` | Tasmota's default settings with no Wi-Fi in them, for the bridge to fill in at D1 | `d63f03cb99540407037f36dc8155ec9b02d24425d3b02c52299713233e98f4a5` |
| `esphome-sonoff-s60-bootloader-1.2.0.bin` | ESPHome's bootloader, from the build of 1.2.0: it replaces Sonoff's when ESPHome is installed, so ESPHome can roll back an update that fails to start | `3792bb665916d69c410c9fd10f69d1a6968e61f2a9e4969829a1c49f9ea6f433` |
| `esphome-sonoff-s60-imprintable-1.4.0.bin` | ESPHome for the S60, installed instead of the final Tasmota when the run is asked for ESPHome; no Wi-Fi or passwords in it, for the run to imprint the plug's Wi-Fi | `137da99ba6737d61d78a2c57a390f97c5a010a0e96c730d476a754c55cf4d49d` |
| `autoflash-mover-1.1.0.bin` | A small ESPHome the run uses for a minute or two when leaving Tasmota: it fits in front of Tasmota in Sonoff's layout, and takes the next firmware as an ordinary ESPHome update; two minutes away from the home Wi-Fi, it raises its own network, whose page takes new Wi-Fi details. Wi-Fi placeholders, like the image above | `f6e131e1599c886d742c151c3ba3f84a285bba462b438c36b611d577e01ad243` |
| `sonoff-s60-partition-table.bin` | Sonoff's partition table, read from a stock plug: the one every conversion starts from, which the Tasmota route's preflight requires byte for byte. Written back when a plug leaves Tasmota's layout. Holds no secrets | `f63f66bbf23b9e291c7eb5dcf24be820190dacf4bf52af515d9664526a4f4daf` |

The Bluetooth image is the exact file used by the successful migrations. It
was retrieved from Tasmota's official moving `install/firmware` branch at
`firmware/unofficial/tasmota32c3-bluetooth.bin`. Its embedded source identifier
is `f5b34a2`; corresponding source is
[`arendst/Tasmota@f5b34a26be51be2469737d2c43a62acb237c264f`](https://github.com/arendst/Tasmota/tree/f5b34a26be51be2469737d2c43a62acb237c264f).

The settings template is Tasmota 15.6.0's defaults, captured from a converted
plug with `tools/espressif/capture_tasmota_template.py` and tested on one with
`--verify`. Its Wi-Fi names, passwords, last access point, and addresses are
blank or Tasmota's defaults; it holds nothing about any network. It is only
used with the Berry Tasmota pinned above (15.6.0.1, same settings layout). If
it, or the Tasmota the interim firmware is made from (15.6.0), changes,
re-check and re-capture it: see `tools/autoflash/tasmota_settings.py`. The
official release a converted plug ends on can change without it
([`../docs/updating-firmware.md`](../docs/updating-firmware.md)).

The imprintable Safeboot is built from the pinned Tasmota source with
`tools/espressif/safeboot/repartition/recovery-safeboot/volatile-wifi-defaults.patch` and
`persist-settings.patch`. On its first boot after the repartition it wipes NVS -
by then only what the Sonoff firmware left and part of an earlier Tasmota
record - saves its settings, with the Wi-Fi, where Tasmota reads them, and
reads them back, so the official Tasmota installed next joins the network by
itself. It logs whether that worked, and the run records it at F2. It never
touches settings that are already there. The
saved record has only what a Safeboot sets, so F4 starts with `Reset 4`, which
rebuilds every other setting from official Tasmota's defaults and keeps the
Wi-Fi. It names itself as the tool, not one device, as every firmware a plug
runs part way through a conversion does: `autoflash-recovery-XXXX`, the last
two bytes of the plug's Wi-Fi MAC. It saves that name, written out, with the
Wi-Fi, so the Tasmota after it goes by it too until F4's `Reset 4` gives it
Tasmota's own (a name with a format in it Tasmota would take for its own
default). What it logs starts "Autoflash recovery". Until 20 October 2026 it
was `s60-recovery-NNNN` (four decimal digits) and "S60 recovery", saving no
name, and the file was `s60-recovery-safeboot-imprintable.bin`; the run still
reads the older log lines, from a plug a run of an older checkout left on it.

To publish a new one: `tools/espressif/safeboot/configure_imprintable_safeboot.py` writes the
header with the placeholders, `tools/espressif/safeboot/build_recovery_safeboot.sh` builds it from
the pinned Tasmota source, `tools/espressif/safeboot/sanitise_safeboot.py` blanks the build
machine's paths in place, and `tools/espressif/safeboot/validate_imprintable_safeboot.py` must
then say it is publishable. A rebuild holds the same text as the published file
but its build date and time; one built in a directory whose path is not as
long as this one's (38 characters) has everything after the first such path a
few bytes along.

The imprintable Safeboot contains reserved placeholder bytes instead of real
Wi-Fi credentials. It **must not be flashed as published**: the private
bundle-preparation path must imprint and validate a per-device copy first.

From 4.4.1 on, a bridge is named by one version: the one the stock updater is
given, the one in its app description, and the one its status page reports
(`bridge 4.4.4`). `tools/espressif/build_vendor_ota.py` takes it from the image and
refuses any other. The next bridge is 4.4.5. 4.4.1 has the Wi-Fi behaviour tried on a
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
the plug's Wi-Fi MAC) - it was `S60-OTA-Bridge-XXXX`, with a fixed password.
4.4.4 is 4.4.3 named as the tool's, as every firmware a plug runs part way
through a conversion is: `autoflash-recovery-XXXX`, on the home network (4.4.3
joined as `s60-ota-bridge`) and as its own (`Sonoff-Setup-XXXX`), with its page
titled "Autoflash bridge" (it was "S60 OTA Bridge"). On its own network it
saves Tasmota's settings without Wi-Fi details, where 4.4.3 refused them, so
Tasmota starts under the run's name for it and makes its own network by that
name, not its default `tasmota-XXXXXX-NNNN`. Its code is the same for
any plug; the wrapper names the S60's model, which is why the file names the
S60. Tried on the emulator; on hardware by the next run.

The version is well above any Sonoff release so far (1.2.0). The stock 1.2.0
updater does not compare versions - the upgrade command decides what is
installed, and a plug on 1.2.0 took bridge v5 wrapped as `9.9.9` - but a later
Sonoff firmware might refuse anything older than itself, so it has to stay
higher than Sonoff's. The version field holds at most seven characters.

The earlier bridge v3, wrapped as `1.2.1`, is no longer published here. It
is in the Git history: `git log --all -- artifacts/s60-ota-bridge-v3-1.2.1.ota`
finds the last commit that has it.

The ESPHome image is built from [`../ESPHome/sonoff_s60.yaml`](../ESPHome/sonoff_s60.yaml)
(its `project` version is the one in the file name) with ESPHome 2026.10.0 and
ESP-IDF 5.5.6, as `esphome compile sonoff_s60.yaml`, in ESPHome's own Docker
image for that release (`firmware.json` pins it by digest);
`firmware.ota.bin` is the file published. The bootloader is
`bootloader/bootloader.bin` from the build of 1.2.0, with ESPHome 2026.9.1 and
ESP-IDF 5.5.5. The mover is
[`../ESPHome/autoflash_mover.yaml`](../ESPHome/autoflash_mover.yaml), built the same way, with ESPHome 2026.9.1: it drives no pins, so it
is the same for any plug with an ESP32-C3 and 4 MB of flash, and from 1.1.0 it
goes by `autoflash-recovery-XXXX` as the bridge does (1.0.0 was
`sonoff-s60-mover-xxxxxx` on the home network, and raised `Sonoff-Setup-XXXX`).
Like the imprintable Safeboot they hold placeholders instead of a
network name and password, and the run writes the plug's Wi-Fi into them in
memory (`tools/autoflash/imprint.py`). It has no OTA password, web login or API
encryption key, as ESPHome's own ready-made images have none: the plug is open
on the home network until its owner adds them, as official Tasmota is. To
publish a new one, change the config and build and publish it with
`tools/repo/update_firmware.py esphome`, which [`../docs/updating-firmware.md`](../docs/updating-firmware.md)
describes, as for a new ESPHome release; then update the files' rows above.

ESPHome 1.1.0 adds a Wi-Fi signal sensor, which the run reads before it
changes the firmware of a plug on ESPHome, and an `s60_wifi` action that gives
the run the plug's Wi-Fi name and password, so that leaving ESPHome needs
neither typed. It also saves its Wi-Fi where a build of the config with none
written in looks for it, so a plug that Home Assistant's ESPHome builder
rebuilds stays on the home Wi-Fi. Its bootloader is byte for byte
1.0.0's, so no plug is given a new one. 1.0.0 is in the Git history:
`git log --all -- artifacts/esphome-sonoff-s60-imprintable-1.0.0.bin` finds
the last commit that has it.

ESPHome 1.2.0 is named as every ESPHome the conversion installs is, whatever
the plug: `esphome-abcdef` (ESPHome's own MAC suffix), and its own network has
that name too - 1.1.0 was `sonoff-s60-abcdef`, its network `Sonoff-Setup-CDEF`.
Its friendly name, Sonoff S60, still says what plug it is. Its Wi-Fi action is
`autoflash_wifi` and its placeholders `AUTOFLASH-IMPRINT-`; the run still knows
1.1.0's `s60_wifi` and `S60-IMPRINT-`, on plugs converted with it. Its
bootloader is 1.1.0's, byte for byte, as is its every other behaviour. 1.1.0
is in the Git history:
`git log --all -- artifacts/esphome-sonoff-s60-imprintable-1.1.0.bin`.

ESPHome 1.3.0 is 1.2.0's config built with ESPHome 2026.10.0 (ESP-IDF 5.5.6)
where 1.2.0 was built with 2026.9.1 (ESP-IDF 5.5.5): the config is unchanged
but for its version. The bootloader that build makes is ESP-IDF 5.5.6's,
which differs from 5.5.5's in nothing an S60 uses (its changes are for other
chips, secure boot and flash encryption), so the published one - 1.2.0's,
which starts 1.3.0 - is kept, and no plug is given a new one. The ESP-IDF
5.5.5 bootloader built again from 1.2.0's config with ESPHome 2026.9.1 came
out the same, byte for byte. 1.2.0 is in the Git history:
`git log --all -- artifacts/esphome-sonoff-s60-imprintable-1.2.0.bin`.

ESPHome 1.4.0 is a change to 1.3.0's config, built with ESPHome 2026.10.0
(ESP-IDF 5.5.6) as 1.3.0 was. It applies the 1.02 board factor and the
plug's factory calibration, from Sonoff's NVS or, through its
`autoflash_calibration` action, from the plug's Sonoff backup, as the stock
firmware does; and counts energy from the meter's pulses, calibrated the
same way, its totals carrying on from what 1.3.0 saved. The bootloader that build makes (ESP-IDF 5.5.6)
differs from the published one, which is kept: no plug is given a new one.
1.3.0 is in the Git history:
`git log --all -- artifacts/esphome-sonoff-s60-imprintable-1.3.0.bin`.
