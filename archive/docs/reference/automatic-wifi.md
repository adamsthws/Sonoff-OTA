# Automatic Wi-Fi: removing the network switches

How the conversion stopped needing the operator to join the plug's own Wi-Fi
networks, or to type their Wi-Fi details: what was found, what was built, what
was ruled out and why. This was "phase 2" of the project.

Source references are to Tasmota at the pinned commit
[`db3ae7e`](https://github.com/arendst/Tasmota/tree/db3ae7e0276fdc38b7aeb241a2a8e33d8ffd6892)
(the official release and the recovery Safeboot are both built from it) and to
`archive/bridge/main/main.c` in this repository. Pitfalls met along the way are
collected in [lessons-learned.md](lessons-learned.md).

## Result

Proven on 2026-10-07: a stock 1.2.0 plug converted end to end with no network
switch and no Wi-Fi typed. Its run record shows D1 uploading over the home
network with the Wi-Fi preset, E1 reading the Wi-Fi off Tasmota, F2's startup
firmware logging "Wi-Fi settings saved for the next firmware", F4 finding
official Tasmota joined by itself, and H1 verifying the result byte for byte.

| Step | Before | Now | Fallback |
|---|---|---|---|
| D1, first join | Bridge raised `S60-OTA-Bridge-XXXX` only | Bridge v5 also joins the network stock saved; the run finds it at the plug's MAC | Its own network, as v3 |
| D1, second join | Berry Tasmota started with no settings and raised `tasmota-XXXXXX-NNNN` | Bridge v5 saves a settings template, filled with the Wi-Fi it joined with, where Tasmota reads it | Tasmota's setup network |
| A5 / E1 | Wi-Fi typed when the bundle was built | Bundle built without Wi-Fi; E1 reads it off Tasmota and writes it into the startup firmware (`pin_wifi`) | Typed, with the reason shown |
| F4 | Official Tasmota's settings were lost in the repartition, so it raised its setup network | The startup firmware saves its settings, with the Wi-Fi; F4 starts with `Reset 4` | Tasmota's setup network |

Every fallback is the old behaviour, so a failure costs a network switch, not a
plug. The run records which route each step took (`upload_route`, `wifi`,
`startup_settings`, `wifi_route` in `autoflash-state.json`).

## D1, first join: the bridge reuses stock's saved Wi-Fi

### Finding where stock keeps it

Only a stock plug could answer this, and only firmware we put on it can read
its flash; its network shows nothing useful (the eWeLink LAN port, 8081, is the
only open port). A converted plug cannot answer it either: E1 sees only the
first 20 KiB of the 64 KiB stock NVS, and by then Berry Tasmota has written
there.

So a research bridge was built: env `s60-ota-bridge-nvs-probe`
(`-DBRIDGE_NVS_READOUT`) adds a read-only `GET /nvs` streaming the stock NVS
partition, and changes nothing else, so a power cycle still returns the plug to
stock. Research images are not kept, so build it first (`pio run -e
s60-ota-bridge-nvs-probe` as in `archive/bridge/README.md`, then wrap it with
`tools/build_vendor_ota.py`). It is delivered with the normal run and the
hidden `--bridge` option, which stops the run after C2:

```sh
python tools/s60_autoflash.py run --new --bridge artifacts/research/s60-ota-bridge-nvs-probe-1.2.1.ota
python tools/inspect_stock_nvs.py   # joined to the bridge's network
```

`tools/inspect_stock_nvs.py` keeps the dump in memory and lists namespaces and
key names, never values.

The first deliveries "bounced": the plug downloaded every byte, restarted, and
was back on stock within about five seconds. The cause was the build, not the
probe: see "Bridge build settings" in [lessons-learned.md](lessons-learned.md).
Built correctly, the probe stayed up and showed (stock 1.2.0, 64 KiB, 16 pages):

| Namespace | Key | Size | Holds |
|---|---|---|---|
| `nvs.net80211` | `sta.ssid` | 36 bytes | Wi-Fi name (u32 length + 32 bytes) |
| `nvs.net80211` | `sta.pswd` | 65 bytes | Wi-Fi password |
| `nvs.net80211` | `sta.apinfo` | 700 bytes | both, in a layout that varies by ESP-IDF version |
| `frame_params` | `content` | 415 bytes | both, in Sonoff's private format |

Other namespaces: `data_backups`, `user_usual`, `user_unusual`, `phy`,
`productId`, `histEnergy` (`histEnergy0`-`8`), `bt_config.conf`, `evtLogName`.
Stock also keeps the full `ap.*` and `sta.*` set, `opmode` and `auto.conn`.

`sta.ssid` and `sta.pswd` have kept the same layout for many ESP-IDF releases,
so the bridge reads those and leaves `sta.apinfo` and `frame_params` alone.

### Bridge v4 and v5

Bridge v4 (tested on stock 1.2.0) reads `sta.ssid` and `sta.pswd` read-only
after `nvs_flash_init()` (no erase on failure), then `nvs_flash_deinit()`. It
runs its own network and the home network together (`WIFI_MODE_APSTA`, hostname
`s60-ota-bridge`, PMF capable), and keeps to its own network if the home one is
not joined within 45 s. `GET /status` reports which. On the test plug it joined
at the plug's usual address, with the plug's own MAC, about 13 s after
delivery. D1 looks for it there (`bridge_on_home_network`) and uploads over the
home network, asking the operator to join the bridge's network only if it does
not appear within a minute.

Bridge v5 adds the settings hand-over below, and is the run's bridge
(`BRIDGE_OTA`). While on the home network it accepts an upload from any device
on it, for the few minutes before D1, much as its fallback network accepts one
from anyone with the public password.

## D1, second join: Tasmota starts with the Wi-Fi

### How Tasmota loads its settings

Tasmota on ESP32 loads its settings at boot from NVS (`main`/`Settings`, 4,096
bytes) and uses them whenever `cfg_holder` is 4617. That path checks nothing
else: no size, version or CRC (`settings.ino` `SettingsLoad`,
`support_esp32.ino` `SettingsRead`). Wi-Fi sits in the text pool (offset 0x017,
699 bytes; `SET_STASSID1` = 4, `SET_STAPWD1` = 6), entries found by counting
NULs, so changing one shifts what follows, as `SettingsUpdateText` does.

A configuration restore (`/rs`, then upload at `/u2`) is stricter: it checks
the CRC (reflected 0xEDB88320, initial 0, inverted, over the first 0xFFC bytes)
and `config_version` at 0xF36 (4 for ESP32-C3). Backups (`/dl`, `.dmp`) are
XORed with `0x5A + offset` from byte 2, or from byte 18 when a 16-byte file
header is present.

### The settings template

- **Bridge v5** adds `POST /tasmota-settings`: a 4,096-byte template plus its
  layout in the query. Only once it has joined the home network, it checks the
  template's CRC, fills in the network it joined with (from the Wi-Fi driver,
  so exactly what worked), seals it with Tasmota's CRC and saves it to NVS. The
  filling code (`archive/bridge/main/tasmota_settings.c`) is compiled by the
  host tests and must match `tools/autoflash/tasmota_settings.py` byte for byte.
- **D1** sends the template before uploading Tasmota, then waits for Tasmota on
  the home network at the plug's MAC.
- **The template** is official Tasmota 15.6.0's defaults with every network
  detail blank or default: Wi-Fi names and passwords, last channel and access
  point, and the addresses DHCP filled in. It was captured with
  `tools/capture_tasmota_template.py` from an already converted plug: backup
  into memory, `Reset 4`, read the defaults and strip them, restore a record
  built from them with the plug's own Wi-Fi (the restore checks the CRC and
  chip family, and the plug must rejoin), then restore the original backup.
  `--verify` tests the saved file the same way without the reset. The committed
  file passed `--verify`, and the plug's own configuration came back unchanged.
- **Pinning:** the template is installed into Berry Tasmota 15.6.0.1 (`f5b34a2`)
  but captured from official 15.6.0 (`db3ae7e`). Between the two the record
  layout, text pool, `cfg_holder`, CRC and load code are identical, and the
  newest `SettingsDelta` step is older than both. The version is not stored as
  data, so the pins are in code (`tools/autoflash/tasmota_settings.py`), and
  `tests/test_tasmota_settings.py` fails with instructions if either Tasmota pin
  changes.
- **Recovery:** Tasmota with details that stop working retries them and never
  opens its setup network. Seven quick power cycles (each under 8 s,
  `UpdateQuickPowerCycle`) erase its settings and it opens the setup network.
  D1 shows this if Tasmota does not appear, and keeps waiting.

## A5 and E1: the Wi-Fi is read, not typed

At D1 Tasmota is on the home network, so its configuration backup holds Wi-Fi
details that are proven to work. `Password1` only ever answers `****`, but
`/dl` contains the password. E1 (`read_wifi_from_plug`) reads `Status 11` for the
network the plug is on and decodes the backup in memory; it uses the details
only if the first network slot is the one connected, and it has a password.
Each reason it cannot is said on screen.

The bundle no longer needs the Wi-Fi when it is built: A5 builds it without,
and `pin_wifi` writes the details into an in-memory copy of the startup
firmware, records its SHA-256 and the network name in the manifest, and
checks it, with no new download. A bundle built from a different startup
firmware is refused at A5 and rebuilt. Typing is the fallback, and `--wifi`
forces it.

## F4: official Tasmota starts with the Wi-Fi

Official Tasmota cannot be changed (H1 verifies it byte for byte), so its Wi-Fi
has to come from settings it reads at first boot.

### Why the settings were lost

The old layout's NVS is 64 KiB at 0x9000; the canonical one is its first
20 KiB (`CANONICAL_NVS_OFFSET`/`SIZE` in `tools/safeboot_migration.py`).
Preflight evidence from four conversions:

- four of the five pages were full of Sonoff's data (`histEnergy*`,
  `user_usual`, `user_unusual`, `data_backups` and others);
- two had no empty page left;
- Berry Tasmota's `Settings` record was always incomplete: 960 to 1,376 of
  4,096 bytes survived.

So official Tasmota started with defaults and raised its setup network.

### Routes that do not work

- **Restoring a backup through the Safeboot.** Its restore page `rs` is inside
  `#ifndef FIRMWARE_MINIMAL`; the upload handler only treats a file as settings
  after that page sets `UPL_SETTINGS`; and the firmware upload rejects any file
  not starting `0xE9` (or `0x1F`, gzip).
- **Compacting NVS before E3.** Every `Reset` mode on ESP32 erases only
  Tasmota's own namespaces (`qpc`, `main`) or the radio calibration;
  `nvs_flash_erase()` is commented out in every case (`SettingsErase`). Berry
  has no NVS binding, and editing NVS pages by raw flash access is ruled out by
  the migration runbook.
- **A settings file.** `SettingsLoad` reads `/.settings` from the filesystem
  before NVS, and official tasmota32c3 has one, but nothing can write it at the
  right moment: the canonical `spiffs` area (0x3B0000-0x400000) overlaps the
  last ~100 KiB of the running Berry image (0x210000-0x3C92C0) during E3, and
  the Safeboot cannot write files.

### The settings-saving Safeboot

`repartition/recovery-safeboot/persist-settings.patch`, applied with
`volatile-wifi-defaults.patch` in the published recovery Safeboot. On boot, in
`SettingsLoad`, before Wi-Fi starts:

1. If NVS already holds a record with a valid `cfg_holder`, leave it alone.
2. Otherwise wipe NVS (`nvs_flash_erase`, which de-initialises it first, then
   `nvs_flash_init`). Nothing holds NVS open that early in Tasmota's boot.
3. Save the in-memory settings (its compiled defaults, with the Wi-Fi imprinted
   into this copy) to `main`/`Settings`.
4. Read the record back and compare it byte for byte, and log one of:
   `S60 recovery: Wi-Fi settings saved for the next firmware`,
   `...settings not saved, NVS wipe failed (n)`,
   `...settings not saved, read-back failed (n)`, or
   `...settings already stored, left alone`.

F2 reads that line from the Safeboot's console log (`startup_settings_report`)
and keeps it in the run record.

The wipe replaced an earlier version that erased only Sonoff's namespaces and
saved into whatever room was left. That depended on NVS reclaiming space in the
crowded state above, which had never been seen on hardware; with the wipe the
save always lands in an empty area, the situation the hardware test proved.
The wipe discards the radio calibration too, which the chip measures again and
saves on its next start; the framework already does the same wipe when NVS
will not start (`initArduino` on `ESP_ERR_NVS_NO_FREE_PAGES`).

### The saved record is partial: F4 starts with `Reset 4`

A Safeboot skips most of `SettingsDefaultSet2` (`#ifndef FIRMWARE_MINIMAL`, "not
needed in minimal/safeboot because ... Settings are not saved anyways"). What it
saves has the Wi-Fi and everything needed to boot (Wi-Fi on, its transmit power
and retry mode, sleep, serial speed, version and size), but zeros where MQTT,
telemetry, energy calibration and other defaults go. Official Tasmota would
use them as they are.

So F4 sends `Reset 4` first. It copies the two Wi-Fi names and passwords aside,
runs official Tasmota's full `SettingsDefault()`, puts the Wi-Fi back, saves and
restarts (`support_tasmota.ino`, restart flags 214-216), so nothing else from
the partial record survives. It also restores the hostname (`%s-%04d`) from the
Safeboot's `s60-recovery-%04d`. Where Tasmota started from defaults anyway, it
costs one restart.

### Testing it without risking a plug

The Safeboot was tested on a plug running ESPHome, which gives firmware under
test exactly one boot. See "Testing firmware with ESPHome's rollback" in
[lessons-learned.md](lessons-learned.md). Both versions saved, joined Wi-Fi in
2-3 s and rolled back to ESPHome on a power cycle; the wipe version's log
confirmed the read-back.

## Options considered and not taken

- **Imprinting the home Wi-Fi into the bridge**, as with the recovery Safeboot.
  Unnecessary once stock's own saved Wi-Fi could be read. The bridge is
  delivered inside Sonoff's OTA wrapper, whose CRC-32s would have needed
  rebuilding after imprinting.
- **An open bridge fallback network** (`WIFI_AUTH_OPEN`). It would let anything
  nearby upload firmware during the bridge's few minutes; the public password
  is a speed bump, but not nothing.
- **Our own Berry Tasmota with imprinted Wi-Fi.** It would have replaced a file
  proven by every migration, which E1-E3 depend on; the template made it
  unnecessary.
- **A smaller Berry Tasmota freeing the filesystem area**, so E3 could also
  write a LittleFS image holding `/.settings`. A new build, a filesystem image
  builder and more writes inside E3; the Safeboot route needed none of them.
- **The tool joining the plug's networks itself** (NetworkManager, `netsh
  wlan`, `networksetup`). It takes control of the operator's Wi-Fi and behaves
  differently on each system. Rejected for this project.
- **One custom firmware doing the whole migration** in place of the bridge and
  Berry Tasmota: rewriting the reviewed Berry migration in C, and a full safety
  review again.
- **Improv Wi-Fi** needs serial or Bluetooth on the device; **WPS** is no longer
  in Tasmota.

## How it was tested with few plugs

Each piece was proven where a failure was cheapest, and stock plugs were used
last:

1. The settings template, on an already converted plug, which got its own
   configuration back.
2. Reading the Wi-Fi off Tasmota, on a converted plug: the password rebuilt the
   bundle's pinned recovery image exactly.
3. The settings-saving Safeboot, on an ESPHome plug with bootloader rollback.
4. The bridges, on one stock plug, each time returning it to stock with a power
   cycle.
5. One full run from stock.

## Build tooling, if it is taken further

A devcontainer could pin the toolchains: Espressif's `espressif/idf:v5.3.1`
image for the bridge, and PlatformIO with the Tasmota source at the pinned
commit for the Tasmota builds, so anyone could rebuild the published artifacts
and compare them. Byte-for-byte rebuilds would need ESP-IDF's reproducible-build
option and a fixed build date for Tasmota, alongside the build-path redaction
`tools/sanitise_safeboot.py` already does. The conversion tool itself still
runs on the host, which has the Wi-Fi, LAN and firewall it needs.
