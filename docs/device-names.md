# Device names: where each comes from, and how to change them

Every firmware a plug runs during a conversion has a name, used two ways:

- its **hostname**, which your router shows in its list of devices;
- the name of its **own network** (its SSID), which the plug raises when it
  cannot join your Wi-Fi.

Each firmware uses one name for both. This page lists every name, where each
is set, and everything that has to change to rename them. It was written
after the 2026-10 renaming, which took every name off the S60, so that
another rename can be done in one pass.

## The names

The examples are for a plug whose Wi-Fi MAC is `12:34:56:ab:cd:ef` (made
up): `CDEF` is the last two bytes, upper case; `abcdef` is the last three,
lower case. Each plug works its own out, so no two plugs share a name.

| When | Firmware | Name (hostname and own network) | Where the name comes from |
|---|---|---|---|
| Before conversion | Sonoff's own | `espressif` (no own network the tool uses) | Sonoff; not changed |
| C2-D1 | the installer (bridge 4.4.4) | `autoflash-recovery-CDEF` | compiled in, from the MAC at boot |
| D1-E | interim (Berry) Tasmota | `autoflash-recovery-CDEF` | written into its settings by the installer, on the run's behalf |
| E-F2 | the startup firmware (Safeboot) | `autoflash-recovery-CDEF` | compiled in as a pattern; saved, written out, for what follows |
| F2-F4 | official Tasmota | `autoflash-recovery-CDEF` | the name the startup firmware saved |
| after F4 | official Tasmota, converted | `tasmota-abcdef` | given by F4 as its reset saves |
| L1-M1 | the mover (ESPHome) | `autoflash-recovery-CDEF` | compiled in, written over ESPHome's name at boot |
| after M1/I2 | ESPHome, converted | `esphome-abcdef` | compiled in (ESPHome's own MAC suffix) |

The names an earlier release used are still recognised (see
[Old names](#old-names)), and so are the ones Tasmota gives itself where its
settings are lost (see [Tasmota's own names](#tasmotas-own-names)).

## Where each name is set

### The installer (bridge)

- [`archive/bridge/main/main.c`](../archive/bridge/main/main.c):
  `BRIDGE_NAME_FORMAT` (`"autoflash-recovery-%02X%02X"`, from
  `esp_read_mac(..., ESP_MAC_WIFI_STA)`), used for the hostname and the
  network. The page title, which the tool recognises the installer by, is in
  `INDEX_HTML` ("Autoflash bridge"), and the project and log tag are
  `autoflash_bridge`.
- `POST /tasmota-settings` saves the settings record the run sends for the
  interim Tasmota, the name already in it. On the home network the installer
  adds the Wi-Fi it joined with; on its own network it saves the record
  without Wi-Fi (since 4.4.4), so that Tasmota still starts under the name.
- A changed installer is a new version: `PROJECT_VER` in
  [`archive/bridge/CMakeLists.txt`](../archive/bridge/CMakeLists.txt) and
  `BRIDGE_VERSION` in `main.c` (a test keeps them equal). Build and wrap it
  as [`archive/bridge/README.md`](../archive/bridge/README.md) says, into
  `artifacts/autoflash-bridge-<kind>-<version>.ota` (the wrapper names the
  plug's model, so there is one per kind of plug).

### The interim Tasmota (D1)

- It is Tasmota's own build (`artifacts/tasmota32c3-bluetooth.bin`), not
  changed. Its name comes from the settings record D1 sends:
  `give_tasmota_the_wifi()` in
  [`tools/autoflash/cli.py`](../tools/autoflash/cli.py) calls
  `tasmota_settings.named(template, our_setup_network(ctx))`
  ([`tools/autoflash/tasmota_settings.py`](../tools/autoflash/tasmota_settings.py):
  `SET_HOSTNAME`, at most `HOSTNAME_MAX` characters, with the record's
  checksum made again, as the installer checks it before saving).
- Tasmota names its own network after its hostname
  (`WiFi.softAP(TasmotaGlobal.hostname, ...)`), so one name does both.

### The startup firmware

- Our build of Tasmota's Safeboot. The pattern is `WIFI_DEFAULT_HOSTNAME`
  (`"autoflash-recovery-%4X"`, Tasmota's `%4X` being the MAC's last four hex
  digits), written into the build's `user_config_override.h` by
  [`tools/espressif/safeboot/configure_imprintable_safeboot.py`](../tools/espressif/safeboot/configure_imprintable_safeboot.py)
  and [`tools/espressif/safeboot/configure_recovery_safeboot.py`](../tools/espressif/safeboot/configure_recovery_safeboot.py).
- [`tools/espressif/safeboot/repartition/recovery-safeboot/persist-settings.patch`](../tools/espressif/safeboot/repartition/recovery-safeboot/persist-settings.patch)
  saves the name, written out, with the Wi-Fi, for the official Tasmota
  after it. It has to be written out: at every start Tasmota replaces a
  stored hostname that has a `%` in it with its own compiled default
  (`tasmota.ino`).
- Published as [`artifacts/README.md`](../artifacts/README.md) says: the
  header from `configure_imprintable_safeboot.py`, the build from
  [`tools/espressif/safeboot/build_recovery_safeboot.sh`](../tools/espressif/safeboot/build_recovery_safeboot.sh),
  then `tools/espressif/safeboot/sanitise_safeboot.py` and `tools/espressif/safeboot/validate_imprintable_safeboot.py`,
  into `artifacts/autoflash-recovery-safeboot-imprintable.bin`; a test checks
  the pattern is in the image (`tests/repo/test_published_artifacts.py`).

### Official Tasmota, and F4

- Tasmota's own release (`tasmota32c3.bin`), not changed. Before F4 it runs
  on the startup firmware's saved name.
- F4 (`step_configure()`) resets Tasmota's settings to its own defaults
  (`Reset 4`), which would name it Tasmota's way. Just before the reset it
  puts in a Berry driver, `Device.at_next_save()` in
  [`tools/autoflash/device.py`](../tools/autoflash/device.py), whose
  `save_before_restart` gives the final name (`final_name(ctx, "tasmota")`:
  `TASMOTA_AP_PREFIX` and the MAC's last six hex digits), the template and
  `Module 0` as the reset's restart saves the rebuilt defaults:
  one restart, and no other name in between. Should that not take, the same
  goes in after the reset (`Backlog Hostname ...; Module 0`), costing a
  second restart.
- A name cannot be sent *with* `Reset 4`: Tasmota resets only once its
  queued commands are done, so a `Hostname` sent with it is applied first and
  then wiped - and, setting a restart of its own, cancels the reset.
- The driver runs once, and is never given a command that saves the
  settings itself: `SaveData` does (`SettingsSaveAll`), which calls the
  driver again - in the emulator, round and round until Tasmota's stack
  overflowed and it restarted before saving, its settings lost.

### The mover

- [`ESPHome/autoflash_mover.yaml`](../ESPHome/autoflash_mover.yaml): ESPHome
  names must be lower case and ESPHome's own suffix is six hex digits, so the
  mover is built as `autoflash-moving` with `name_add_mac_suffix` and its
  `on_boot` writes `autoflash-recovery-%02X%02X` over that name before its
  Wi-Fi starts, and sets its own network to it.
- **The two must be the same length**: `autoflash-moving` (16) and
  ESPHome's `-xxxxxx` (7) make 23, as `autoflash-recovery-XXXX` does. A new
  interim name means a new compiled name of the matching length.
- A changed mover is a new version (`project: version:`), built with
  `esphome compile` into `artifacts/autoflash-mover-<version>.bin`.

### ESPHome, converted

- [`ESPHome/sonoff_s60.yaml`](../ESPHome/sonoff_s60.yaml): `name: esphome`
  with `name_add_mac_suffix: true` gives `esphome-abcdef`; `on_boot` sets
  its own network to `App.get_name()`. `friendly_name` says what plug it is.
- The file name and `package_import_url` stay as they are: they are built
  into plugs converted already, and Home Assistant adopts them by it.
- Rebuilt as [`ESPHome/README.md`](../ESPHome/README.md) says, a new version
  each time: `artifacts/esphome-<kind>-imprintable-<version>.bin` and its
  bootloader, then `IMAGE`, its SHA-256 and size in
  [`tools/autoflash/esphome.py`](../tools/autoflash/esphome.py).
- Its Wi-Fi action (`autoflash_wifi`, `WIFI_ACTIONS` in `esphome.py`) and
  the Wi-Fi placeholders (`AUTOFLASH-IMPRINT-SSID>` and `-PASS>`, in
  [`tools/autoflash/imprint.py`](../tools/autoflash/imprint.py)) are in the
  mover and the startup firmware too.

## What the tool looks for

All in [`tools/autoflash/cli.py`](../tools/autoflash/cli.py) unless said:

- `SETUP_SSID_PREFIX` (`autoflash-recovery-`), `named()` and
  `our_setup_network()`: the interim name.
- `final_name(ctx, firmware)`: `tasmota-abcdef`, `esphome-abcdef`.
- `bridge_networks()`, `mover_networks()`: the interim name and the old ones.
- `setup_networks()`, `setup_network_hint()`: a Tasmota's own network -
  `tasmota-abcdef`, Tasmota's own `tasmota-ABCDEF-` and `ESP_ABCDF0`. Not
  the interim name: the installer's network, listed for a moment after it
  restarts into a Tasmota that has the Wi-Fi, was taken for Tasmota's. The
  one Tasmota that does raise a network under the interim name (sent through
  the installer's own network, so given no Wi-Fi: `tasmota_named_alone`) is
  looked for by it in `finish_tasmota_setup()`, once the installer has gone
  (`installer_gone()`), and found when a run is tried again by asking it
  (`setup_network.tasmota_answers()`: on its own network Tasmota's page waits
  on a Wi-Fi scan, its status does not).
- `esphome_networks()`, `esphome_network_hint()`: ESPHome's.
- `TASMOTA_DEFAULT_NAME_RE` and `name_with_the_wifi()`: a Tasmota under its
  own default name, given the Wi-Fi on its setup network, is named again in
  that restart (the same Berry driver as F4's).
- `tasmota_at_home()` and `note_setup_network()`: the interim name is never
  kept as the plug's own.
- [`tools/autoflash/setup_network.py`](../tools/autoflash/setup_network.py):
  `BRIDGE_PAGE_MARKERS`, the installer's page title, old and new.
- [`tools/autoflash/kinds.py`](../tools/autoflash/kinds.py): per kind of
  plug, ESPHome's old names (`esphome_legacy_setup_prefix`,
  `esphome_legacy_prefix`) and its friendly name.
- [`tools/autoflash/prompts.py`](../tools/autoflash/prompts.py):
  `find_ssid()` takes a network by the names in the order given, with
  `PROVISIONAL_PREFIX` (`ESP_`) last and looked at again.
- [`tools/autoflash/output.py`](../tools/autoflash/output.py): the patterns
  that keep a plug's name out of the report one shares.
- [`tools/espressif/flash_readout.py`](../tools/espressif/flash_readout.py): the names it waits
  for while reading a stock plug.

## Tasmota's own names

Tasmota's compiled default, `%s-%04d` - its MQTT topic (`tasmota_ABCDEF`,
written `tasmota-ABCDEF`) and four decimal digits of the chip ID - gives
`tasmota-ABCDEF-1234`. The plug has it only where its settings are gone: in
a Tasmota whose settings were erased by switching it off and on 7 times, and
while it tests the Wi-Fi it is given on its setup network after that. The
setup network itself then has that name, and the tool looks for it.

For a moment as Tasmota turns its own network on, before it names it, the
network goes out as `ESP_` and the plug's access-point MAC (the Wi-Fi MAC
plus one: `ESP_ABCDF0`). Tasmota's `WifiManagerBegin()` turns the access
point on first (`WifiSetMode(WIFI_AP_STA)`, which ends with `delay(100)`)
and names it after (`WiFi.softAP()`), so for those 100 ms it beacons under
ESP-IDF's default name. Seen at nearly every Tasmota setup-network start in
the emulator, in both builds we install (the 15.6.0 release: 4 starts of 5).
Calling `WiFi.softAP()` first - which turns the access point on and names
it at once - ends it: built from the same commit, 8 starts of 8 without the
change went out as `ESP_ABCDF0` first, none of 8 with it, and the setup
page still worked. That is a change to Tasmota
([#25169](https://github.com/arendst/Tasmota/pull/25169)), so it reaches plugs
only with a Tasmota release that has it. Until then a computer can list that
name for a minute after, and the tool never takes it over the real one
(`find_ssid()`).

## Old names

A plug a run of an older checkout left part way through goes by the name it
had then, so the tool still looks for every one: `Sonoff-Setup-XXXX` (the
installer 4.4.3, the mover 1.0.0 and ESPHome 1.1.0's own networks),
`S60-OTA-Bridge-XXXX` with the password `s60-ota-bridge` (installers before
4.4.3), `s60-ota-bridge` and `sonoff-s60-mover-xxxxxx` (hostnames),
`sonoff-s60-xxxxxx` (ESPHome 1.1.0), the "S60 OTA Bridge" page, the
`S60-IMPRINT-` placeholders and the `s60_wifi` action. Renaming again, add
today's names to these lists rather than replacing them.

## Renaming: the checklist

1. Change the name where it is made (above): the installer, the mover, the
   startup firmware, the final ESPHome, and the tool's constants for the
   interim Tasmota and F4. Keep the mover's compiled name the length of the
   new interim name; keep every name within Tasmota's 32 characters and
   ESPHome's lower-case rule.
2. Add the old names to the lists in [Old names](#old-names), in the tool.
3. Rebuild every firmware whose name changed, each as a new version, and
   publish each artifact: the file in `artifacts/`, its line in `SHA256SUMS`,
   its row in [`artifacts/README.md`](../artifacts/README.md), the path,
   SHA-256 and size where the tool pins it (`BRIDGE_OTA`, `esphome.IMAGE`,
   `CONTROL_FILES` in `flash_readout.py`), and
   [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md).
4. Update the tests that name them: `tests/autoflash/test_setup_networks.py`,
   `tests/repo/test_published_artifacts.py`, `tests/autoflash/test_tasmota_settings.py`,
   `tests/autoflash/test_autoflash.py`, `tests/espressif/test_flash_readout.py`.
5. Update the pages that name them: [`troubleshooting.md`](troubleshooting.md)
   ("The plug's own network"), [`what-happens.md`](what-happens.md),
   [`technical-details.md`](technical-details.md), the ESPHome and
   installer READMEs, and the changelog.
6. Test every name in the emulator, as [`testing.md`](testing.md) says: each
   firmware's hostname is in the emulator's DHCP log
   (`/run/autoflash-emu/dnsmasq.log` in the container), the network it
   raises in `/run/autoflash-emu/softap`, and every network that went out in
   the plug's serial log (`WIFIAP: guest SoftAP <name> up`). Run every route
   from stock and between the firmwares, and the routes through each
   firmware's own network (the fault suite's `fallback` scenarios). Run one
   plug with another MAC too, so a name made from the MAC is seen to follow
   it: a copy of a backed-up plug under `captures/plugs/<mac>/` with the new
   MAC in `efuse.bin` (bytes `0x18`-`0x1D`, in reverse order) and
   `test_copy_of` in its `plug.json`.
