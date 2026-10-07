# Lessons learned

Pitfalls met while building and testing the converter, and what avoids them.
Each cost real time once. The research they came from is in
[automatic-wifi.md](automatic-wifi.md) and the other documents here.

## Building firmware

**Bridge build settings.** `.gitignore` had `sdkconfig.*`, which also ignored
`archive/bridge/sdkconfig.defaults`, so the repository silently lacked the
settings v3 was built with, and every rebuild got ESP-IDF's defaults. The
result booted, aborted at once and the bridge's own fallback put the plug back
on stock within seconds: it looked like the plug rejecting the image. Diffing
the rebuilt machine code against v3 (task stack size, `httpd_req_t` offsets,
the Wi-Fi NVS check) recovered four settings:

- `CONFIG_ESP_WIFI_NVS_ENABLED=n`: the bridge never initialises NVS for Wi-Fi,
  so with this on `esp_wifi_init()` fails and `ESP_ERROR_CHECK` aborts. Keep it
  off; read anything from NVS explicitly instead.
- `CONFIG_HTTPD_MAX_URI_LEN=128`, `CONFIG_HTTPD_MAX_REQ_HDR_LEN=1024`.
- `CONFIG_ESP_MAIN_TASK_STACK_SIZE=7168` (the task stack is this plus 512).

`.gitignore` now has `!sdkconfig.defaults`. Delete any generated `sdkconfig.*`
before rebuilding, or old settings are reused. Theories that looked plausible
and were wrong: image size alignment (a padded build bounced too) and the
updater itself (the shipped v3 stayed up for over an hour).

**Comparing a rebuild with a published image.** Neither build is byte-for-byte
reproducible. Compare with `cmp -l` and map the differing offsets:

- Bridge: the app descriptor's build time, date and ELF SHA-256, the image
  checksum and appended SHA-256, and the OTA wrapper's CRCs.
- Tasmota Safeboot: the ELF SHA-256 in the app descriptor, the `__DATE__` and
  `__TIME__` strings (around offset 1,250), and the checksum and SHA-256.

Anything else differing means the source or toolchain differs. Publish from a
clean build (`pio run -t clean` first), even though an incremental one came out
identical.

**`tools/build_recovery_safeboot.sh` expects a project-local PlatformIO**
(`.platformio-core`, `.pio-home`). On a machine with PlatformIO installed
normally (`~/.platformio/penv/bin/pio`) it stops with "No module named
platformio". The images published in this project were built by doing its
steps by hand: check the tree is clean at the pinned commit, apply
`volatile-wifi-defaults.patch` then `persist-settings.patch`, install the
header from `tools/configure_imprintable_safeboot.py`, `pio run -e
tasmota32c3-safeboot`, then `tools/sanitise_safeboot.py` and
`tools/validate_imprintable_safeboot.py`. Reverse the patches and remove the
header afterwards; the script's checks refuse a dirty tree.

**Compiler folding.** A test that padded the bridge with a `const` array had
the array optimised away; it needed a `const char *volatile` reference to be
kept. Check the image size actually changed.

## ESP-IDF and NVS

- ESP-IDF's HTTP server waits for the request body before calling a handler: a
  POST with no body gets 408 after 20 s, while one byte is enough for a size
  check to answer 400 at once.
- `nvs_flash_erase()` de-initialises NVS first if needed; call
  `nvs_flash_init()` again afterwards.
- Arduino's `initArduino()` wipes the whole NVS partition when
  `nvs_flash_init()` returns `ESP_ERR_NVS_NO_FREE_PAGES` or
  `NEW_VERSION_FOUND`, and nothing logs it at normal levels.
- `nvs_erase_all()` on a namespace leaves its entry, so opening it afterwards
  still succeeds. Count keys with `nvs_entry_find` to tell empty from absent.
- Opening a namespace read-write creates it. Open read-only first to check it
  exists.
- The radio calibration in NVS (`phy`: `cal_data`, `cal_mac`, `cal_version`)
  is measured again and saved on the next start if it is missing.

## Tasmota

- **`/cm` and `/cs` need a `Referer` header** (CSRF protection,
  `HttpCheckPriviledgedAccess`). Without one Tasmota closes the connection,
  which looks exactly like the device being absent. `Device._get` sends one.
- **The console log** is at `/cs?c2=0`, lines separated by `\x1e`. It keeps
  lines from boot for a while, so a Safeboot's start-up messages can be read
  later. Timestamps can jump backwards around an NVS wipe.
- **`NvmLoad` returns true even when the blob read fails**; check the result's
  contents (`cfg_holder`), not the return value.
- **Settings load vs restore:** loading from NVS checks only `cfg_holder`; a
  restore checks the CRC and chip family too. See
  [automatic-wifi.md](automatic-wifi.md) for the layout, CRC and backup XOR.
- **`Password1` answers `****`**, but the `/dl` backup contains the password.
- **`Reset 4`** restores every default and keeps the Wi-Fi, with no flash erase.
  **Seven quick power cycles** (each under 8 s) erase the settings and open the
  setup network.
- **A Safeboot** (`FIRMWARE_MINIMAL`) skips most defaults and compiles out
  `SettingsSave`'s body, but `NvmLoad`, `NvmSave` and `NvmErase` still work.
  Its partition switching (`EspPrepSwitchPartition`) only acts on single-slot
  layouts.
- **Settings records are tied to a firmware's layout.** The template's pins are
  enforced by tests; re-check `tasmota_types.h`, `SettingsTextIndex`,
  `settings_text_size`, `CFG_HOLDER` and `SettingsDelta` before moving either
  Tasmota pin.

## Testing on hardware

**Use a converted plug where you can.** The settings template was captured and
verified on one, backing up its configuration first and restoring it after;
`tools/capture_tasmota_template.py` keeps the backup in memory and only offers
to save it as a file if the restore fails.

**Testing firmware with ESPHome's rollback.** An ESPHome plug built with
ESP-IDF has bootloader rollback (`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`
in its build's `sdkconfig`). A firmware that never confirms itself gets exactly
one boot: any restart, crash or power cycle returns to ESPHome. Tasmota builds
never confirm themselves, because the Arduino core only does so when its own
`CONFIG_APP_ROLLBACK_ENABLE` is set, and it is not for the C3.

- Check the plug's build first: the config hash it announces over mDNS matches
  the build folder's.
- Upload with `esphome upload <config> --device <ip> --file <image>`. ESPHome's
  update accepts any valid app image unless signing or downgrade protection is
  configured.
- Don't restart the plug until the test is read: that is its one boot.
- Revert by power-cycling. Whatever the test wrote to NVS stays; a temporary
  ESPHome build (an `includes:` header plus an `interval:` lambda) erased it,
  then a normal build went back on. A test that wipes NVS also resets
  ESPHome's own saved values.
- The spare slot keeps the tested image until the next ESPHome update.

**Stale connections can lock you out of an ESPHome plug.** ESPHome accepts
only a few API connections at once. `docker compose run` containers outlive the
command: Ctrl-C or a `timeout` stops the client, not the container, so each
stopped `esphome logs` session stayed connected. Six of them, one left over from
each attempt, used up the plug's connections, and it refused every new one with
"EOF received" (`SocketClosedAPIError`). The plug looked broken; it was only
full. Give the container a `--name` and `docker stop` it, and check `docker ps`
for left-over `esphome-run` containers before suspecting the plug.

**Piped Python output is buffered.** `esphome logs ... | grep` showed nothing
until `-e PYTHONUNBUFFERED=1` was set.

**Watching a plug.** A network that lasts five seconds is missed by a 10 s scan;
ping and scan in separate loops. `pkill -f` can match its own command line.

**A one-off startup message can be missed.** A log session that reconnects
after the message was printed never sees it; have the device report the state
repeatedly instead, as the temporary key-count build did.

## Process

- **Firewall rules:** a rule the system already had must be recorded as
  pre-existing, or the cleanup deletes it.
- **Secrets stay in memory.** The Wi-Fi password is never written to a file;
  when a tool outside Python needs an image with it written in, use `/dev/shm`
  (RAM), readable only by the owner, and delete it straight after.
