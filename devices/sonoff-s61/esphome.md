# ESPHome (LibreTiny) for the Sonoff S61

[`ESPHome/sonoff_s61.yaml`](../../ESPHome/sonoff_s61.yaml) is the ESPHome the
conversion installs on an S61, built on [LibreTiny](https://docs.libretiny.eu/)
for its BK7238. It is the S60's config ([`ESPHome/sonoff_s60.yaml`](../../ESPHome/sonoff_s60.yaml))
with the S61's wiring and storage: the same entities, actions, calibration
handling and energy totals, so the two plugs look alike in Home Assistant.

## How it differs from the S60's

- **Wi-Fi from the stock store, not written into the image.** The S60's image
  has two placeholders the tool fills with the plug's Wi-Fi. The S61's image is
  an rt_ota update (gzip, AES with RT-Thread's public key), which cannot be
  patched in place, and does not need to be: the plug's Wi-Fi is still in the
  stock EasyFlash store at `0x3e2000`, which no conversion touches. At boot
  (priority 1000, before Wi-Fi) the config reads `wifi-ssid` / `wifi-pass` from
  it with `lt_flash_read`, the way the OpenBeken import module does, and sets
  them as the station. After Wi-Fi is up (priority -100) they are saved the way
  the S60's are, under both keys ESPHome looks in.
- **The factory meter calibration** is read from the same store (`elecCalibCoef`:
  magic, flag, then `a, b, c` for current, voltage and power) at the same time,
  and applied the way the stock firmware applies it: the BL0937's pulse rate
  reckoned with a 1.1 V reference where the datasheet (and ESPHome) say
  1.218 V, then `a*x^2/1e7 + b*x/1e6 + c/1e6` on the reading in A, V or W
  ([the derivation](../../docs/beken-s61-emulation.md#energy-meter-calibration-the-stock-formula-and-the-openbeken-mapping)).
  One set of nine numbers, where the S60 has two sets for below and above
  300 W. The `Calibration` sensor says `stock store`, `from backup` (given
  through `autoflash_calibration`, nine numbers) or `missing`.
- **The wiring** is the OpenBeken template's (S61STPF / `WBBK_01P_V1.3`): relay
  P26, button P9 (active low, pull-up), relay LED P0 and Wi-Fi LED P1 (both lit
  low), BL0937 CF P8, CF1 P6, SEL P24 with the 1 mΩ shunt and 2040:1 divider.
  `hlw8012` reads the meter; it alternates CF1 between current and voltage
  (`change_mode_every: 1`, every 2 s), so each refreshes every 4 s.
- **The flash layout.** LibreTiny's generic BK7238 board is the SDK's 2 MB
  layout. The config overrides it through `platformio_options` (PlatformIO's
  `board_flash.*` / `board_build.*` overrides) to the S61's stock bootloader
  table: app `0x11000+0x275000` (`0x250000` logical, so `bkrbl_size_app`),
  download `0x286000+0x15A000` - the slot the stock bootloader applies an
  update from, so ESPHome's own updates (which LibreTiny writes there) go
  through it too - the radio calibration at `0x3e0000`, ESPHome's preferences
  (FlashDB) in the empty 32 KB at `0x3ee000`, and the unused LittleFS in the
  empty sector at `0x3e1000`. The stock EasyFlash stores at `0x3e2000` and
  `0x3f6000` are left alone, so the plug can always go back to stock.

Nothing in the S61 route needs a LibreTiny or ESPHome change: ESPHome 2026.10.0
pins LibreTiny 1.13.0, which has the BK7238.

## Building

As the S60's image is built: ESPHome's own Docker image, the config alone in a
directory. The first build fetches LibreTiny, its Beken SDK and the toolchain.

```sh
mkdir build && cp ESPHome/sonoff_s61.yaml build/ && cd build
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/config -v "$PWD":/config \
    ghcr.io/esphome/esphome:2026.10.0 compile sonoff_s61.yaml
```

The outputs, in `.esphome/build/esphome/.pioenvs/esphome/`:

- `image_bk7238_app.ota.rbl` - the **rt_ota update** (about 500 KB) for the
  conversion: wrapped with [`../fota_wrap.py`](../../tools/beken/fota_wrap.py) it is
  what the stock plug downloads; staged in OpenBeken's `ota_http` it converts
  an OpenBeken plug. The stock bootloader applies it from the download slot.
- `firmware.uf2` - LibreTiny's flasher/OTA image, which ESPHome's own OTA
  (and Home Assistant's ESPHome builder) send to a plug already on ESPHome.
- `board.json` - the layout the build used; check it says `download:
  0x286000+0x15A000` and `kvs: 0x3EE000+0x8000`.

## Reverting to stock, and to OpenBeken

A plug on ESPHome takes updates through ESPHome's OTA as UF2 files, and
LibreTiny writes a UF2's `download` part to the download slot. So a UF2 that
carries the stock app RBL (rebuilt from a dump with
[`../stock_app.py`](../../tools/beken/stock_app.py) and the SDK packer, as the
OpenBeken README says) as its download part, sent through the ordinary
ESPHome OTA, is a reversion: the stock bootloader applies it on the reboot
that follows. ltchiptool (LibreTiny's tool) builds it:

```sh
ltchiptool uf2 write -o stock_revert.uf2 -b board.json -L 1.13.0 \
    -F "sonoff-stock:1.0.2" --legacy "stock_app_1.0.2.rbl=device:download"
```

The same with OpenBeken's 4 MB-table `.rbl` moves the plug to OpenBeken, and
OpenBeken's ordinary `ota_http <url of the ESPHome .rbl>` brings it back: its
4 MB-table build downloads into the same slot. The stock EasyFlash store is
never touched, so whichever firmware comes up reads its Wi-Fi, calibration
and wiring from it on first boot - OpenBeken through its import module,
ESPHome through its boot lambdas - and nothing is typed or copied across.
Each firmware's own settings beyond those (names, thresholds, the relay's
last state) are its own and start afresh; ESPHome's live in the 32 KB at
`0x3ee000`, OpenBeken's in the sector at `0x3e1000`, and neither touches the
other's.

## Validated in the emulator

All in the BekenEmulator fork on a 4 MB flash shaped like the owner's plug
(stock firmware, provisioned with a test network), with no bypass of any check
the plug makes:

1. **The conversion**, the way the tool drives it: the plug on the local
   eWeLink stub is pushed an `upgrade` command; it fetches the FOTA-wrapped
   `.rbl` in ranged requests and stages it verbatim at `0x286000`.
2. **The apply and first boot**: booted from that flash, the stock bootloader
   logs `upgrade(1.0.2->26.10.29-bk7238)`, decompresses (`_STREAM_END`, the
   raw size matching the build), erases and writes `app`, then `go
   os_addr(0x10000)`; LibreTiny 1.13.0 starts, ESPHome logs `Calibration:
   stock store` and `Wi-Fi from the stock store: 'MyTestNet' (11-char
   password)`, completes the four-way handshake, gets a DHCP lease, announces
   itself over mDNS as `esphome-xxxxxx`, asks for NTP, and answers through
   the emulator's port forwards: the REST the tool reads (`/switch/Relay`
   `ON`, `/text_sensor/Calibration` `stock store`, `/sensor/WiFi Signal`), and
   the native API, which lists every entity and both actions, reports the
   relay restored on, and answers `autoflash_wifi` with the stock network's
   name and password.
3. **Reversion**: the stock UF2 above sent through ESPHome's OTA (on
   LibreTiny it listens on port 8892, not 3232; `esphome upload` and
   `espota2` know): `OTA successful`, `Rebooting safely`, then the stock
   bootloader's `upgrade(26.10.29-bk7238->1.0.2)`, erase and write. The flash
   afterwards holds the stock app byte for byte over its length and the
   EasyFlash store with the same keys, Wi-Fi and calibration; loaded afresh it
   boots the stock firmware (SDK 3.0.70, `EasyFlash V4.1.99 is initialize
   success`). As with OpenBeken, the emulator does not run the applied app
   in the same process after a firmware-initiated reboot; the persisted flash
   is what counts.
4. **To OpenBeken and back**: a UF2 carrying OpenBeken's 4 MB-table `.rbl`
   through ESPHome's OTA (`OTA successful`, `upgrade(26.10.29-bk7238->s61v6)`);
   the fresh boot imports the stock Wi-Fi, calibration and pin template and
   joins in 8 s; `ota_http` of the ESPHome `.rbl` served from the host (`init
   OTA, startaddr 0x286000`, `upgrade(s61v6->26.10.29-bk7238)`); and the fresh
   boot of that is ESPHome again, `Calibration: stock store`, joined, the API
   answering `autoflash_wifi` with the stock network.

One emulator change was needed, in the fork: LibreTiny keeps its FreeRTOS
tick on hardware Timer 0 (the 26 MHz group, for a fine `micros()`), where
OpenBeken and the stock firmware use Timer 3; the emulator raised only
Timer 3, so under it LibreTiny's tick never advanced and every delay waited
forever. It now models the 26 MHz group too, each enabled channel firing at
its own period - the period matters: OpenBeken's BK7238 build feeds its
interrupt watchdog from a 10 s channel of that group, and a first version
that raised every channel on every tick ran that feed back to back and
starved OpenBeken instead.

Not yet done: a real plug, and the tool's integration (publishing the `.rbl`
in `artifacts/`, the FOTA push, the first-boot checks).
