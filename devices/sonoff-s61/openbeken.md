# OpenBeken Sonoff S61 stock Wi-Fi import

The tool's OpenBeken driver,
[`tools/beken/openbeken/drv_autoflash.c`](../../tools/beken/openbeken/drv_autoflash.c),
on first boot of a freshly converted S61, reads the plug's **stock** Wi-Fi
credentials from the untouched EasyFlash store and applies them through
OpenBeken's own config API, so the plug rejoins your network with no typing
and no second device.

The driver serves any eWeLink plug on a Beken chip that keeps its stock
rt_ota bootloader: it reads whatever is specific to one plug from that plug's
table ([`drv_autoflash.h`](../../tools/beken/openbeken/drv_autoflash.h) has its
fields). The S61's is [`drv_autoflash_sonoff_s61.c`](drv_autoflash_sonoff_s61.c):
its kind and stock model, where its stock store (`0x3e0000`-`0x3ec000`),
download slot (`0x286000`) and OpenBeken's settings (`0x3e1000`) sit, its
wiring, and its BL0937 meter's calibration - how its `elecCalibCoef` record
reads, the datasheet constants, the 3840 W rating. A build carries one table,
for the plug it is built for; the driver itself names no plug. An image that
carries several tables and tells the plug by its stock store's model at run
time waits for a second Beken plug.

It goes through `CFG_SetWiFiSSID()` / `CFG_SetWiFiPass()` rather than writing the
config blob itself, so it keeps working across OpenBeken releases that change the
`mainConfig_t` layout or checksum. It reads the stock store only (never writes
it), so it does not stand in the way of reverting to stock. See the full findings
in [beken-s61-emulation.md](../../docs/beken-s61-emulation.md#carrying-wi-fi-across-the-conversion-the-openbeken-config).

## Integrating into OpenBeken

Three edits against `openshwprojects/OpenBK7231T_App`:

1. Copy `drv_autoflash.c`, `drv_autoflash.h` and `drv_autoflash_sonoff_s61.c`
   to `src/driver/`.
2. Add the two `.c` files to the build list in `platforms/obk_main.mk`,
   beside the other drivers:

   ```
   OBKM_SRC  += $(OBK_SRCS)driver/drv_autoflash.c
   OBKM_SRC  += $(OBK_SRCS)driver/drv_autoflash_sonoff_s61.c
   ```

3. Call it once from `Main_Init()` in `src/user_main.c`, right after
   `CFG_InitAndLoad();` (so the imported SSID is in place before the Wi-Fi
   connect):

   ```c
   CFG_InitAndLoad();

   extern void Autoflash_Init(void);
   Autoflash_Init();
   ```

`Autoflash_Init()` registers the driver's four commands, runs the import (a
no-op once a Wi-Fi SSID is set) and looks for the interim marker, so it is
safe on every boot.

## What the driver does on first boot

Only when OpenBeken has no Wi-Fi SSID yet (so your later changes are never
touched), it reads the stock EasyFlash store and:

1. imports **`wifi-ssid` / `wifi-pass`** via `CFG_SetWiFiSSID/Pass()`;
2. carries the **factory meter calibration** over: `elecCalibCoef`'s per-quantity
   trim `b/1e6` times the stock driver's datasheet constants gives OpenBeken's
   `cal` for current, voltage and power exactly (see the emulation reference for
   the derivation), and `PowerMax` becomes the plug's 3840 W;
3. applies the **S61 wiring** from the OpenBeken device database (S61STPF,
   board `WBBK_01P_V1.3`): relay P26, button P9, LED P0, Wi-Fi LED P1, BL0937
   CF P8 / CF1 P6 / SEL P24 — and `SetStartValue 1 -1`, so the relay restores
   its last state after a power cut; and names the plug as the conversion does
   on every firmware, `openbeken-abcdef` (the last three bytes of its MAC),
   both its short name (its mDNS hostname and its name in Home Assistant) and
   its full one (its DHCP hostname).

The driver also registers `AutoflashKind`, which the tool uses to tell what
plug an OpenBeken is on, because every firmware it installs is named alike and
you may rename it: `AutoflashKind sonoff-s61` answers OK, any other kind an
error, and an OpenBeken without the driver does not know the command. It
changes nothing. The tool asks this only of an OpenBeken on a chip that one of
the plugs it supports has (BK7238).

Commands: `AutoflashImportWiFi` (run the above by hand),
`AutoflashRevertToStock <url>` (below) and `AutoflashInterim` (next). Its log
lines begin `AutoflashImport:`, or `AutoflashRevert:` for reverting. Before
the driver was split from the S61's table it was `drv_sonoff_s61_import.c`,
with the commands `S61ImportWiFi` and `S61RevertToStock` and log lines
beginning `S61Import:` and `S61Revert:`, as the runs quoted below show.

## The interim firmware

A run to ESPHome from Sonoff's firmware installs this same OpenBeken first,
to back the plug up through it, then has it fetch ESPHome. The tool tells it
that it is the interim firmware with a marker sent in the same update: B2 sends
the image, `0xff` to the next 4 KB boundary (from the image's start) and one
4 KB sector -
`AFINTERM`, a format version, the image's size, the size and FNV-1a hash of
the app it installs (its RBL header's `size_raw` and `raw_hash`) and a CRC-32
([`tools/beken/interim_marker.py`](../../tools/beken/interim_marker.py) has
the layout). Sonoff's updater writes the whole file into the download slot,
and the stock bootloader, having applied the image, erases only the slot's
first sector (the image's header), so the marker is still there on the first
boot. A run to OpenBeken sends the image alone.

On every start, after the first-boot import and before Wi-Fi starts, the
driver looks at each 4 KB boundary of the slot past the first for a valid
marker (its magic, version and CRC, and at the boundary after the image size
it gives): 345 reads of 28 bytes. Found, it checks the marker is for the app
running: the FNV-1a hash of the app partition's first `size_raw` bytes, read
raw from `0x11000` with its CRC stripes (32 bytes, then 2 of CRC) skipped -
the hash the bootloader checked the app against once it wrote it. For that
app, it names the plug `autoflash-recovery-CDEF` (the MAC's last two bytes,
upper case, as the S60's interim firmwares are named), short and full name,
saves the config, and only then erases the marker, so a power cut between
leaves it to be found again. The plug asks for its first address under that
name. A marker for any other app - from an update that did not install, sent
with another image, left by anything but the install it came with - is erased
and changes nothing: the bootloader installs an image before any app starts,
so a marker whose app is not running never will be. With no marker nothing
changes, and the app is not read.

That name is the record of it being the interim firmware. The driver keeps no
flag in OpenBeken's config: upstream may give its flags and unused bytes a
meaning, and a config from a later OpenBeken could then read as interim. A name
set through the setters survives any change of the config's layout. A plug you
have renamed is your own, and no longer the interim firmware.

`AutoflashInterim` answers OK on the interim firmware and an error on any
other; an OpenBeken without the driver, or with one from before it, does not
know the command. `AutoflashInterim off` makes it the plug's OpenBeken for
good (`openbeken-abcdef`), for a run that finds a plug left on it and is to
end on OpenBeken; `AutoflashInterim on` makes it the interim firmware, for the
run that sent it as one when the marker did not arrive (its update cut off in
its last few kilobytes, after the whole image). The DHCP and mDNS names follow
on its next start: after `off` the tool restarts it; after `on` it does not,
and the interim firmware keeps the name it started with until the next
firmware replaces it.

Once the interim firmware has fetched the next firmware, ESPHome or Sonoff's
own, it leaves nothing of itself: OpenBeken's HTTP OTA calls
`HAL_OTA_BeforeReboot` just before the restart that applies the download (the
hook patch below), and the driver, only when it is the interim firmware and
the slot holds a whole rt_ota app image (its header's CRC-32 and its body's),
erases OpenBeken's settings (`0x3e1000`), its flash variables (OpenBeken's
EasyFlash at `0x1e5000`-`0x1ed000`: boot counts, kept channel values, energy
totals) and any marker past the image. A download cut off leaves it the
interim firmware, to be sent the image again; a power cut after the erase
still applies the image, which is whole in the slot. A later move back to
OpenBeken starts afresh, named by its first boot's import. Any other
OpenBeken - your own, updating itself - is left as it is. OpenBeken's
LittleFS, which would sit at the top of the download slot, is never made on
the interim firmware, so there is none to erase.

## Building (BK7238, 4 MB table)

OpenBeken's BK7238 build hard-wires the SDK's 2 MB partition table. The S61 has a
4 MB chip, and the SDK's 4 MB table is Sonoff's own layout: built with it,
OpenBeken's OTA slot becomes the stock download partition (`0x286000`) that the
stock bootloader applies from — so both OpenBeken updates and reversion to stock
work through the ordinary `ota_http` — its config moves to the dedicated empty
`0x3e1000`, and it reads the plug's real radio calibration at `0x3e0000`.

Two patches, kept in [`tools/beken/openbeken/`](../../tools/beken/openbeken/) and offered upstream as
[OpenBK7231T_App#2290](https://github.com/openshwprojects/OpenBK7231T_App/pull/2290)
and [beken_freertos_sdk#13](https://github.com/NonPIayerCharacter/beken_freertos_sdk/pull/13),
add a `FLASH_SIZE=4M` build switch, and a third gives the driver its hook
before an OTA's restart:

- [`openbk7231t_app-bk7238-4m-flash.patch`](../../tools/beken/openbeken/openbk7231t_app-bk7238-4m-flash.patch)
  (against `openshwprojects/OpenBK7231T_App`): `make FLASH_SIZE=4M` builds with
  the 4 MB table; `hal_ota.h` takes the OTA slot from the partition table the
  firmware was built with instead of a hard-coded `0x132000` (the REST and web-UI
  OTA path used the constant — on the first 4 MB build without this, the download
  still went to `0x132000`); and `DEFAULT_FLASH_LEN` follows the table too, so
  `GET /api/flash/...` can read the config region for the backup. LittleFS's
  bounds and the raw flash-write cap, both 2 MB constants as well, follow the
  table (on a 4 MB build LittleFS would otherwise have sat inside the app
  partition); and a change of flash size between builds rebuilds the app's
  objects, which the SDK build would otherwise reuse from the previous size.
- [`beken_freertos_sdk-4m-pack-profile.patch`](../../tools/beken/openbeken/beken_freertos_sdk-4m-pack-profile.patch)
  (against the SDK it clones, `NonPIayerCharacter/beken_freertos_sdk`): the pack
  step names its outputs by flash size instead of always `_2M`; the 4 MB QIO
  profile's app size matches the table (2516 K — the 2890 K it had ran into the
  OTA slot); and a 4 MB QIO is packed with a 4 MB bootloader — the shipped
  `bootloader_bk7238_uart1_v1.0.15.bin` with the 4 MB partition table written
  into its tail, which is byte for byte the S61's factory bootloader. The pull
  request ships that file; the patch here has `make_bootloader_4m.py` generate
  it on the first pack.
- [`openbk7231t_app-ota-before-reboot-hook.patch`](../../tools/beken/openbeken/openbk7231t_app-ota-before-reboot-hook.patch)
  (against `openshwprojects/OpenBK7231T_App`, alone or after the first):
  `HAL_OTA_BeforeReboot(ok)`, weak and empty unless a driver defines it, called
  in the Beken HTTP OTA path once the download is written and the config saved,
  just before `bk_reboot()` - `ok` when no error was seen while it downloaded.
  OpenBeken's HTTP client ends a download the same way whether it finished or
  failed, and can take a connection closed early for the end, so a hook that
  acts on it checks the image too; this driver's does
  ([the interim firmware](#the-interim-firmware)). Offered upstream as
  [OpenBeken #2293](https://github.com/openshwprojects/OpenBK7231T_App/pull/2293).

The Beken SDK bundles its own `arm-none-eabi-gcc` that needs **GLIBC ≥ 2.34**, so
build in a current environment (Ubuntu 24.04 / 22.04 — OpenBeken's own Docker
image is too old and fails with `GLIBC_2.34 not found`). The SDK's pack step calls
`python` (not `python3`), so `python-is-python3` is required.

```sh
git clone https://github.com/openshwprojects/OpenBK7231T_App
cd OpenBK7231T_App
cp <this repo>/tools/beken/openbeken/*.patch .
git apply openbk7231t_app-bk7238-4m-flash.patch
git apply openbk7231t_app-ota-before-reboot-hook.patch
# ...the three driver edits from the section above...
docker run --rm -v "$(pwd)":/app ubuntu:24.04 bash -c '
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq \
      cmake srecord make git python3 python-is-python3 wget xxd build-essential
  cd /app && git config --global --add safe.directory "*"
  make APP_NAME=OpenBK7238 prebuild_OpenBK7238      # clones the SDK
  git -C sdk/beken_freertos_sdk apply /app/beken_freertos_sdk-4m-pack-profile.patch
  make APP_VERSION=s61 APP_NAME=OpenBK7238 FLASH_SIZE=4M OpenBK7238'
```

`tools/repo/update_firmware.py openbeken` does all of the above - clone, patches,
driver and table, build in `build.Dockerfile` - and publishes the image as
`artifacts/openbeken-sonoff-s61-<version>.rbl`, the OpenBeken and SDK commits
pinned in `firmware.json` ([docs/updating-firmware.md](../../docs/updating-firmware.md)).
By hand, `output/s61/OpenBK7238_4M_s61.rbl` is the `rt_ota` image for the **OTA
conversion**: wrap it in the CoolKit FOTA container with
[`../fota_wrap.py`](../../tools/beken/fota_wrap.py) to push it to a stock plug.
`OpenBK7238_4M_QIO_s61.bin` beside it is a whole 4 MB flash — bootloader included
— for the emulator or a UART flash.

## Validated

Built with the module, then booted in the BekenEmulator on a 4 MB flash shaped
like a converted plug (OpenBeken app + the stock EF40 at `0x3e0000`, provisioned
with a test SSID). On first boot:

```
Info:CFG:S61Import: imported stock Wi-Fi SSID 'MyTestNet' (11-char pass)
Info:MAIN:Using SSID [MyTestNet]
[WIFI] ...completed the four-way handshake; the link is encrypted
[NET]  DHCP: device ... got 192.168.1.100
```

The module read the stock creds and OpenBeken joined — no typing, no offline
config surgery.

With the full module (4 MB-table build) converted over the stock bootloader:

```
[I/OTA] OTA firmware(app) upgrade(1.0.2->s61v6) startup.
S61Import: imported stock Wi-Fi SSID 'MyTestNet' (11-char pass)
S61Import: stock BL0937 factory trim b=954547/879893/930747 (I/V/P) -> OpenBeken cal I=0.011095 V=0.128238 P=1.334562, PowerMax 3840 W
S61Import: applied the S61 pin template (Rel P26, Btn P9, LED P0, WifiLED P1, BL0937 CF P8 CF1 P6 SEL P24); relay restores its last state
Info:MAIN:Started BL0937.
```

and the config OpenBeken then saved at `0x3e1000` decodes to: name `S61` (the module's name then; now `openbeken-abcdef`); P26
`IOR_Relay` ch 1, P9 `IOR_Button_ToggleAll` ch 1, P0 `IOR_LED_n` ch 1, P1
`IOR_LED_WIFI_n` ch 2, P8 `IOR_BL0937_CF`, P6 `IOR_BL0937_CF1`, P24
`IOR_BL0937_SEL`; `startChannelValues[1] = -1`; cal `V=0.128238 I=0.011095
P=1.334562 PowerMax=3840`. On a 2 MB-table build `S61RevertToStock` (now
`AutoflashRevertToStock`) refuses
(`OTA slot is 0x132000, not the stock download slot 0x286000 - refusing`), and
on the first 4 MB-table build without the `hal_ota.h` patch the downloader still
wrote to `0x132000` and the stock bootloader rebooted straight back into
OpenBeken — which is why the command checks both addresses.

It was also validated through the **full OTA conversion**: the `.rbl`, staged in
the stock download partition (`0x286000`), is applied by the stock `rt_ota`
bootloader (`OTA firmware(app) upgrade(1.0.2->s61import)` → `partition 'app' erase
success`), OpenBeken boots, and the module imports from the untouched EF40 and
joins.

The two upstream patches were checked on a clean clone. The default 2 MB build is
unchanged: config at `0x1e1000`, a REST read beyond 2 MB refused. The
`FLASH_SIZE=4M` QIO boots in the emulator on a 4 MB flash, saves its config at
`0x3e1000`, answers `GET /api/flash/3e0000-1000`, and `ota_http` logs `init OTA,
startaddr 0x286000`, where its own 4 MB bootloader applies the download on
reboot: `upgrade(4M.1220->s61v6)` → `_STREAM_END` → slot erased. The emulator
then stays at the bootloader banner; the flash it persisted is right — the
written app verifies against its RBL header's FNV-1a hash and, loaded afresh,
boots s61v6 and joins — so that is emulator state after an OpenBeken-initiated
watchdog reboot, not the image: the same RBL applied from a bootloader-only boot
of the same flash comes up first time.

## Post-conversion config (via OpenBeken's API)

OpenBeken keeps a stable command/HTTP surface, so the conversion tool sets the
rest over `http://<plug>/cm?cmnd=...` after the plug joins — no firmware change
needed. To make the relay **remember its last state across power loss** (matching
what the tool sets on S60 Tasmota/ESPHome):

```
SetStartValue <relayChannel> -1      # -1 = remember last state (persisted on change)
```

(The relay channel depends on the S61 pin mapping, which the conversion also
applies as an OpenBeken pin template — the generic build ships none.)

## Updating

A plug on an earlier release of this OpenBeken is updated by the tool
(`--firmware openbeken`; tools/autoflash/routes/update.py): it answers
`AutoflashKind` for its kind and gives its release in its build string
(`make APP_VERSION=`), and fetches the published image by `ota_http` into
the download slot, as any OpenBeken update. Its settings stay: the hook
erases them only on the interim firmware, and the driver's first-boot
import, finding a Wi-Fi SSID set, imports nothing again - so what a later
driver does on first boot reaches only a plug converted with it. An
upstream build is for the 2 MB table and has no driver, so it is not an
update for an S61.

## Reverting to stock

On a converted plug the stock `rt_ota` bootloader is still in place and applies
any valid RBL it finds at `0x286000` — downgrades included. With the 4 MB-table
build that is OpenBeken's own OTA slot, so reversion is an ordinary OTA download
of a **stock app RBL**:

```
AutoflashRevertToStock http://<tool>/stock_app_1.0.2.rbl
```

The command first checks the OTA slot really is `0x286000` and refuses otherwise
(a 2 MB-table build would write inside the app partition, where the stock
bootloader never looks). The stock EasyFlash store is never touched, so the plug
comes back with its own Wi-Fi, calibration and identity.

The stock app RBL is rebuilt from a dump — the app partition holds only the
decompressed image — with [`../stock_app.py`](../../tools/beken/stock_app.py)
(extracts and verifies it against the stored FNV-1a hash) and
[`../rbl.py`](../../tools/beken/rbl.py), which packs it as the SDK's own packer
does (`-c gzip -s aes`, RT-Thread's public key): the same body byte for byte,
and the header's version, serial and time as the dump's own header has them.
`stock_app.stock_rbl(dump)` does both, with nothing but Python; the conversion
tool makes the image this way, from the public S61 dump, when a plug goes back.
The SDK's packer gives the same file:

```sh
python3 ../stock_app.py dump.bin -o stock_app_1.0.2.bin
sdk/beken_freertos_sdk/tools/rtt_ota/rt_ota_packaging_tool_cli \
    -f stock_app_1.0.2.bin -o stock_app_1.0.2.rbl -p app -c gzip -s aes \
    -k 0123456789ABCDEF0123456789ABCDEF -i 0123456789ABCDEF -v 1.0.2
```

Verified in the emulator both ways. Staged by hand on a converted plug, the
bootloader logs `upgrade(s61v2->1.0.2)`, rewrites `app`, and the stock firmware
boots with the EasyFlash store intact. And end to end through OpenBeken itself —
the stock RBL served from the host, `S61RevertToStock <url>` sent over HTTP —
the log reads `init OTA, startaddr 0x286000` → `Rebooting in 1 seconds` →
`OTA firmware(app) upgrade(s61v6->1.0.2) startup` → `The partition 'app' erase
success` → `EasyFlash V4.1.99 is initialize success`, the same 33 EF40 keys
still there afterwards.

## Backing up the plug

[`../backup.py`](../../tools/beken/backup.py) copies the per-device region
`0x3e0000–0x400000` (factory TLV with MAC and radio calibration, EasyFlash app
store with Wi-Fi creds and `elecCalibCoef`, system ENV) out of a converted plug
over OpenBeken's `GET /api/flash/<addr>-<len>`, and files it the way the S60
conversion files its backup — never overwriting one that exists:

```
captures/plugs/<mac-with-dashes>/sonoff-backup/
    config-0x3e0000.bin    the 128 KiB region, as read
    stock-app-1.0.2.rbl    the stock app, repackaged (with --stock-rbl)
    backup.json            offsets, hashes, when, how to restore
```

```sh
python3 ../backup.py --plug http://<plug> --stock-rbl stock_app_1.0.2.rbl
```

Byte-exact against the dump in the emulator.
