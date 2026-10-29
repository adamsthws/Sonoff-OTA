# Sonoff S61 (BK7238) emulation — technical reference

A working technical reference for emulating a stock Sonoff **S61** plug (Beken
**BK7238**, not the ESP32-C3 the S60 uses) in the BekenEmulator fork. It records
what is known about the firmware: how it boots, how it stores its
configuration, how pairing and provisioning work, how its OTA update works, and
how all of that is used to convert the plug to OpenBeken and back. Each claim was
tested in the emulator unless the text says otherwise; the drivers that produced
the results are in [s61-tools/](s61-tools/README.md). Per-device identifiers are
kept out of this repository; examples use the made-up MAC `12:34:56:ab:cd:ef`.

The dumps and plugs are the owner's own devices. Observing, testing and
decompiling a program the owner lawfully owns, so other software can interoperate
with it, rests on the interoperability and study exceptions — EU Software
Directive 2009/24/EC, Arts. 5–6, and UK CDPA 1988, ss. 50A/50BA/50B; right-to-repair
law is background support, not the grant. This is the same footing for any device
the project adds. See [legal.md](legal.md).

## Goals

1. **Convert the plug to OpenBeken (or ESPHome) over OTA, and back to stock** — as
   the S60 is converted to Tasmota//OpenBeken/ESPHome, through the vendor's own update path,
   with no soldering. See [Firmware update and conversion](#firmware-update-and-conversion-the-ota-path).
2. **Read the stored Wi-Fi credentials from flash**, so the conversion retains the
   user's network with no re-typing — as the S60 conversion does. They are stored
   **plaintext** in the EasyFlash V4 store under `wifi-ssid`/`wifi-pass`, readable
   from a dump; see [Wi-Fi credentials](#wi-fi-credential-storage).
3. **Run the stock plug in the emulator, onto Wi-Fi and the cloud** — to study the
   firmware without a plug on the bench. Seeding credentials joins it to an AP, and
   synthesising the pairing-success trigger dials the eWeLink cloud and reaches
   ONLINE against a local stub (own account and plug only; `--net-out` for the real
   cloud). See [the cloud dial](#the-cloud-dial-synthesise-the-pairing-success-trigger).

## The emulator

- Base: **BekenEmulator** (`openshwprojects/BekenEmulator`), a Python emulator on
  Unicorn (QEMU's CPU core). It models the BK7238 peripherals and a RivieraWaves
  Wi-Fi MAC, and can attach a virtual 802.11 peer + user-mode IP network
  (`src/wifipeer.py` + `src/netstack.py`) — enough that stock firmware reaches a
  cloud TLS handshake over the station path.
- The BLE controller **registers** are modelled (`src/blecore.py`) and host
  commands complete, but there is **no BLE radio**: nothing is transmitted and no
  peer can connect.
- It comes with this repository as a container image, as the S60's QEMU does:
  its source at the commit `../tools/beken/emulator/UPSTREAM` pins, unmodified, is
  an archive of the
  [`emulator-source-beken-d9a3772`](https://github.com/adamsthws/Sonoff-OTA/releases/tag/emulator-source-beken-d9a3772)
  release - named after the commit, holding that one archive, never changed;
  a new pin is a new release - and this project's changes to it are the patches in
  [`../tools/beken/emulator/patches/`](../tools/beken/emulator/patches/).
  `../tools/beken/emulator/bk-emu.sh` builds the image from them
  (`bk-emu.Dockerfile`), or pulls the one the `beken-emulator-image` workflow
  published for that version (`ghcr.io/adamsthws/autoflash-beken-emu:<hash>`),
  and runs the tools in it with this checkout at `/work` (see
  [the fork](#the-fork-s61-toolsemulator)). Upstream publishes no licence;
  its source is kept here so the emulator depends on nothing outside this
  repository. Neither the archive nor the image holds any of upstream's
  `firmwares/`: the public S61s dump is Sonoff's firmware, which this
  repository never holds, so `bk-emu.sh` fetches it from upstream's
  repository, which publishes it, into `captures/emulation/beken-firmwares/`,
  mounted into the container.
- BK7238 CPU is ARM968E-S (ARMv5TE), which Unicorn runs directly.

Run the S61 dump:

```sh
tools/beken/emulator/bk-emu.sh run captures/<S61 dump>.bin --wifi-peer auto
# which is, in the container:
python /opt/beken-emulator/src/main.py <S61 dump>.bin --with-boot -chip BK7238 --only-uart --wifi-peer auto
```

Always pass `--only-uart`. Without it the emulator logs every MMIO access
(300k+ lines) and runs so slowly it never progresses past init.

## Boot

Stock boot once looped on a watchdog reset. Cause: right after `ais_evt_task`
starts, the Beken SDK reads the flash chip's **Unique ID** (command `0x4B`, 4
dummy bytes, 8 data bytes) over the **SPI master at `0x802700`** — a path separate
from the memory-mapped flash the emulator serves. That controller was unmodelled,
so its STATUS register never signalled "done", the read spun, and the task
watchdog reset the chip.

Fixed in the fork by `src/spi_flash.py` (`SpiFlashMaster`): it serves the SPI
STATUS/DATA registers and answers `0x4B` with a synthetic unique id, plus ~3 lines
wiring it into the two MMIO hooks in `src/emulator.py`. It engages only while a
transfer is configured (`CONFIG != 0`), so firmwares that never drive the SPI
master are unaffected. Result: 0 reboots, RTOS runs, 6/6 regression (SPI-LED
controllers, OpenBK7238 Wi-Fi/DHCP/HTTP, the S61 dump test).

## What the firmware does at boot

The eWeLink app runs. It boots to `ate_mode_state[0]` (normal, not ATE), runs its
`ais` event loop, drives its status LED through the **PWM block** (`0x802B00`,
~290k writes/run), brings up its **BLE stack** (`BLE_STACK_OK`), and initialises
**EasyFlash V4.1.99**. It then settles into a **quiescent idle**: all AIS tasks
created and blocked on their event queues, timers and interrupts (SYS/IRQ/FIQ/SVC)
firing, ~58k idle-loop hits, **zero** Wi-Fi frames transmitted. The central event
loop is a message pump at `0x23d6c` blocked on a queue receive (`0x134f7c`) from
the queue whose handle is `*(0x403c0c)+4 = 0x403c10` (created ~0.45 s into boot by
`0x17700`, stored at `0x16604`); it dispatches event messages 1–9 and otherwise
waits.

The device is **unprovisioned**: its EF40 store has no `wifi-ssid`/`wifi-pass`,
and its CHIP ENV has no Matter fabric. So it does not try to connect — it waits
for a **pairing trigger** (the button, or a command from the app over BLE/cloud).
The `0x7fffffde` RX filter and the "monitor mode … as in EZ pairing" UART line are
the MAC's **default** filter printed during `rwnxl_init`; they are not an active
AirKiss sniff (the monitor callback `0x403b20` is NULL at boot — and in any case
this build only ever registers a stub there, see
[AirKiss](#airkiss-smartconfig-relay--decoder-stubbed-in-this-build)). BLE does
not advertise at boot either (0 advertising payloads staged). This is a plug
sitting powered-on-but-untouched.

## Flash layout

Code regions are stored **CRC-striped**: 32 data bytes then a 2-byte CRC,
repeating. Un-stripe before searching code for strings or RBL headers. The config
regions are **not** striped — the firmware reads them as raw physical bytes
through the flash controller.

| Partition  | Offset     | Size      | Notes |
|------------|------------|-----------|-------|
| bootloader | `0x000000` | `0x010000`| RT-Thread + FAL + `rt_ota` (`V0.2.8-beken`) |
| app        | `0x010000` | `0x250000`| the running firmware |
| download   | `0x286000` | `0x15A000`| staged OTA slot (erased in this dump) |
| factory TLV| `0x3e0000` | (0x1e0 used)| manufacturing store — MAC + radio calibration |
| EF40 app   | `0x3e2000` | `0x9000`  | EasyFlash V4 KV store — device config + creds (active write sector `0x3e3000`) |
| EF40 sys   | `0x3f6000` | `0x8000`  | EasyFlash V4 CHIP/ENV store |

The three config regions are read raw (un-striped). The OTA app conversion
(`download`→`app`) **never touches any of them**, so everything below survives a
conversion to OpenBeken — the basis for both Wi-Fi handover and reversibility.

#### `0x3e0000` — factory TLV store (MAC + radio calibration)

A nested TLV: `"TLV\0"`, total-len `u32`, then sections. Each section is a header
`tag=0x00 | marker(3) | payload_len u32`, then entries `tag(1) | marker(3) |
len u32 | value`. Two sections on this dump:

- **Section `0x111111` (90 B) — device/manufacturing.** tag `0x02` len 6 = the
  **Wi-Fi/base MAC** (examples in this repo use `12:34:56:ab:cd:ef`); other tags
  (`0x01`,`0x03`–`0x07`, 4–8 B each) are per-device manufacturing constants.
- **Section `0x222222` (162 B) — radio (RF) calibration.** Two 14-byte tables
  (tags `0x02`,`0x03`) = per-Wi-Fi-channel TX-power calibration (descending
  values across the 14 channels), a 40-byte table (tag `0x07`) of per-channel/
  rate power, plus small scalars. This is the *radio* cal, distinct from the
  *energy-meter* cal in the EF40 store below.

#### `0x3e2000` — EF40 **app** KV store (device config + Wi-Fi creds)

EasyFlash V4; sector magic `"EF40"`, nodes are `KV40` records (format below). The
keys present on this (paired-then-reset, join-unprovisioned) dump — values of
per-device secrets redacted:

| Key | Size | What it is | Useful to OpenBeken? |
|-----|------|-----------|----------------------|
| `elecCalibCoef` | 80 B | **factory energy-meter calibration** (binary) | **yes — accurate V/A/W out of the box** |
| `elecCalibFlag` | 1 B | metering calibrated (`01`) | yes |
| `outlet&conf` | JSON | power-on/startup state, pulse, offBrightness | yes (relay boot behaviour) |
| `sled&conf` | JSON | status-LED behaviour (`sledOnline`, relayLight) | yes |
| `elec&app` / `elec&appex` | JSON | over-power / -voltage / -current protection | yes (protection limits) |
| `elec_region`, `elec_dst_hour` | 1/20 B | meter region + DST | maybe |
| `wifi-ssid` / `wifi-pass` | ≤32/≤64 B | **plaintext Wi-Fi creds** (provisioned plugs only; absent here) | **yes — the handover** |
| `ssid` / `pass` | 34/66 B | **obfuscated** (NOT plaintext; different encoding — undecoded) | unknown |
| `apiKey`, `app-apikey`, `domain`, `secureCertSha256`, `factory`, `user` | — | eWeLink cloud identity/certs (per-device; redacted) | no (cloud is dropped) |
| `timerInfo`, `timer&revision`, `outlet&protect`, `lan&info`, `ota&*`, `recordInfo`, `fabric&list`, `RecoverFlag`, `fc_rssi`, `port` | — | timers / misc eWeLink state | mostly no |

#### `0x3f6000` — EF40 **system/CHIP** ENV

Boot/reboot counters (`chip-counters.*`), `chip-factory.uniqueId`, and Matter
globals (`g/*`). No `f/1/n` fabric/NOC on this dump.

**EF40 enumeration tool:** `docs/s61-tools/ef40_dump.py <flash.bin>` lists every
KV40 node with CRC validation.

### Update (RBL) format

Images the bootloader applies are `rt_ota` **RBL** containers. The 96-byte header
is verified byte-for-byte against the dump (a packer, `rbl_pack.py`, reproduces
the real `bootloader` and `app` RBL headers exactly):

| offset | field | notes |
|--------|-------|-------|
| `0x00` | magic | `"RBL\0"` |
| `0x04` | algo | `u32`: `crypt` nibble (checked as a flag — must be non-zero) + compression nibble (LZMA-family; e.g. app = `0x402`) |
| `0x08` | timestamp | |
| `0x0c` | name[16] | `"app"`, `"bootloader"` |
| `0x1c` | version[24] | e.g. `"1.0.2"` |
| `0x34` | sn[24] | |
| `0x4c` | body CRC32 | **standard CRC-32** of the packaged (compressed) body |
| `0x50` | raw hash | **FNV-1a-32** of the raw (decompressed) body |
| `0x54` | size_raw | decompressed size |
| `0x58` | size_package | packaged size (< size_raw when compressed) |
| `0x5c` | header CRC32 | **standard zlib CRC32 over bytes `[0:0x5c]`** |

The two **CRC** fields (`body CRC32`, `header CRC32`) are the standard CRC-32
(reflected, polynomial `0xedb88320` — the zlib/PNG one; the bootloader's table
generator at `0x9674` builds exactly that from a 14-tap array), so they are
`zlib.crc32`, reproducible offline. The `raw hash` field is **FNV-1a-32** (offset
basis `0x811c9dc5`, prime `0x01000193`) over the decompressed image — confirmed byte-
exact against the stock app's stored value (`0x0d89f7b1`). None of these is a
proprietary "on-chip" hash.

The bootloader copies `download` → `app`, decompressing and validating CRCs. A
third-party image is accepted: the `crypt` nibble need only be non-zero (a flag),
and `rt_ota_custom_verify` is a link-address sanity check, **not** a signature — see
[Firmware update and conversion](#firmware-update-and-conversion-the-ota-path).

## Wi-Fi credential storage

On a provisioned plug the Wi-Fi credentials are stored **in the clear** as EF40 KV
nodes — no cipher, no transform. Reading them needs no decoder, no pairing and no
emulator: parse the KV store from a flash dump.

- **Setter `0x488dc`** ("put ssid to wifi-ssid" / "put password to wifi-pass")
  stores SSID and password **verbatim**: it `memcpy`s the input, takes its length
  with a plain `strlen` (`0x12214`), and calls config-set `0x5adb4` with the raw
  buffer as the value, under keys **`wifi-ssid`** / **`wifi-pass`**.
- **Reader `0x48124`** → config-get `0x5ad84(ctx, "wifi-ssid", out, …)` (via
  `0x12f438`), returning `0xd8` when the key is absent (the unprovisioned case).

**EF40 KV node format** (verified on real nodes):

```
"KV40"(4) | len(4) | crc32(4) | name_len(1)+0xff*3 | value_len(4) | name | value | 0x00 0x00 + 0xff*6
  len = 20 + name_len + value_len + 8
  crc = zlib.crc32( bytes from the name_len field through the end of value )   # standard crc32
```

Because the region is raw (un-striped), nodes can be written in place: craft valid
`wifi-ssid`/`wifi-pass` nodes and append them to the write sector (`0x3e3000`).
This is a working, reusable **synthetic-provisioning** technique (both re-parse
with correct CRC). It writes the creds but does not by itself make the emulated
device connect — connect is gated on the commissioned/bound state, not on cred
presence (see [The provisioned state](#the-provisioned-state)).

The one derived value is the **apikey**: a UUID-format apikey appears in RAM at
boot with no copy in flash — the writer at `0x427b8` formats it
character-by-character from a device MAC+serial buffer (memset `0x45ec`). This is
device identity, unrelated to the plaintext Wi-Fi creds.

### S60 vs S61 storage

Both devices store Wi-Fi credentials in the clear; only the container differs, so
the conversion approach transfers even though the parser does not:

- **S60 (ESP32-C3):** eWeLink config as **plaintext JSON** in NVS —
  `"apikey":"…"`, `"deviceid":"…"`, `"passwd":"…"`, `"ssid":"…"`.
- **S61 (BK7238):** plaintext values in the **EF40 KV store**, keys `wifi-ssid` /
  `wifi-pass`.

The conversion model is identical: read the stock store, pull the plaintext
SSID/password, re-apply after flashing Tasmota/ESPHome.

## Cloud protocol and trust

- Registration: TLS to **`/dispatch/device`** (which server to use), then a
  **WebSocket at `/api/ws`**.
- TLS trust is **mbedTLS against an embedded CA**, not leaf-cert pinning (the
  "pinned session" strings are Matter's CASE sessions). The S60 stub technique
  therefore applies, and is now carried through end to end: a local stub issues
  its own CA and the firmware is made to trust it in place of the CoolKit root.
  The S60's `patch_stock --trust-ca` swaps a small EC P-256 trust anchor in its
  flash image; the S61 keeps no static CA blob (it assembles its CA in RAM), so
  the swap is applied where the CA is installed — see
  [The cloud, completed](#the-cloud-handshake-trust-swap-to-online).

## The provisioned state

"Provisioned/commissioned" on this firmware is a cryptographic trust relationship
with an external authority, not a flag plus credentials. The firmware carries a
full Matter commissioning stack — `NOC` (node operational certificates), `fabric`
(110 refs), `PASE` (SPAKE2+ passcode handshake), `CASE` (certificate-authenticated
sessions), device `attestation`, setup-passcode — plus eWeLink cloud binding
(`apikey` / `deviceid` / `device_secret`). A provisioned device holds a valid
fabric (NOC/ICAC chain + operational keys established over CASE) and a cloud
binding issued by eWeLink.

The public dump carries only a cloud **identity** (`apiKey`, `app-apikey`,
`secureCertSha256`); it has no Wi-Fi creds and no Matter fabric. The
provisioned-vs-idle decision is `0x48cc6` (reads `commission_status` /
`recover_flag`; status in `[sp+0xc]` = 0/1/2); it is an **event-triggered
callback** (pointer loaded ~`0x54250`). A stock unprovisioned plug does not
auto-reconnect: with no Wi-Fi creds and no fabric it stays idle, waiting for a
pairing trigger. On a *provisioned* plug the app reads `wifi-ssid`/`wifi-pass`
through `ef_get_env_blob` (`0x12f438`) about 3.1 s into boot and the station
connect follows — provided the `app` task is past the
[integrity gate](#the-flash-uid-integrity-gate-root-cause), which otherwise hangs
it before the credential read.

A fabric and cloud binding are credentials signed/issued by the commissioner's /
cloud's private keys; synthesising them locally has not been achieved, because
forged values do not pass the firmware's cert/signature checks. The practical way
to a genuinely *cloud*-provisioned image is to pair a real plug with the eWeLink
app, then dump it. (This concerns a *bound* session a server will **accept**. The
cloud **dial itself** — `GET /dispatch/device` — needs none of it: it is reproduced
by synthesising the pairing-completion trigger, and the connect machinery it starts
*can* be driven without a live commissioning session; see
[The cloud dial, achieved](#the-cloud-dial-synthesise-the-pairing-success-trigger).)

### Seeding credentials joins the plug

Seeding the stored Wi-Fi credentials puts the emulated plug on a network for
real — the same end state a real provisioning reaches, without the BLE handoff the
radio-less emulator cannot do. `s61_seed_join.py` hooks the firmware's own
`ef_get_env_blob` (`0x12f438`) to return a chosen SSID/passphrase for
`wifi-ssid`/`wifi-pass`, and attaches the fork peer as a WPA2-PSK AP with the same
creds. The plug then runs the genuine sequence: station mode → authenticate →
associate → **WPA2 four-way handshake** → **IPv4 DHCP → `192.168.1.100`**. The
Matter `BekenWiFiDriver` connect path runs the same way.

### After the Wi-Fi join: Matter by default, eWeLink cloud on a synthesised trigger

Left to itself after the join, the plug does **Matter** work — it advertises
`_matterc._udp` (Matter commissionable) over mDNS and reads the Matter
fabric/attribute KVS (`g/fidx`, `g/fs/c`, `g/a/...`). It does not *spontaneously*
dial the eWeLink cloud: the cloud link is created on demand only when the device
becomes eWeLink-**paired**, so seeding creds reaches the network but not that
state. That pairing-completion is a single firmware event, however, and
synthesising it makes the plug dial the eWeLink cloud for real — see
[The cloud dial, achieved](#the-cloud-dial-synthesise-the-pairing-success-trigger).
The machinery below is what that trigger drives.

### The eWeLink cloud machinery

Tracing the app-layer drive to its root, the eWeLink cloud is a set of AIS tasks
created on demand by `ais_appfw`:

| task | entry | role |
|------|-------|------|
| `ais_appfw_task` | `0x319a8` | app-framework state machine (comes up even unbound, reads `wifi-ssid`, then waits) |
| `ais_link_task` | `0x35aa8` | **cloud link**: dispatch (`/dispatch/device`) → IoT WebSocket (`/api/ws`) |
| `ais_ws_task` | create near `0x385a4` | WebSocket pump |
| `ais_httpd_task` | — | LAN control server (`:8081`), online only |

`ais_link_task` is a **context-driven state machine** (reads the global context
`*0x4044ac`, state byte at `ctx+0x290`); it is created (via the RTOS create
`0x1349f0`, name+entry literals at `0x35820`/`0x35824`) by `ais_cloud_start`
(`0x35744`) when the appfw reaches the eWeLink-**paired** state. Its context does
**not** have to be rebuilt by pairing — it is already populated at boot (device
UUID, model, MAC, a factory dispatch URL; see
[The cloud dial, achieved](#the-cloud-dial-synthesise-the-pairing-success-trigger))
— so the lever is purely the pairing-completion **trigger**, which the pairing
state machine fires on success and which can be synthesised with a single call. The
app's own provisioning input is the **`pairingMode` command** (`0x492ae`): a
JSON-**array** command (cJSON `0x264d6`/`0x265da`/`0x26712`) carried in a session
object (`session+0x8c` = JSON ptr, `+0x90` = length, `+0x88` = flag), with per-item
fields **base64/hex-decoded** (`0x12f8b8`) and run through crypto/list operations
(`0x5b078`, `0x4842e`/`0x48372`/`0x48400`). No eWeLink binding key is read at boot,
but none is needed for the dial — the dispatch context is factory-resident.

**Assessment.** Reconstructing eWeLink's *full* encoded provisioning command — the
JSON-array schema plus the base64/hex + crypto the app uses over the session — and
feeding it to `0x492ae` with a fabricated session would be a sizeable sub-project.
But that is not needed to reach the cloud: the only thing the full pairing produces
that the dial depends on is the **completion trigger**, and that is one firmware
call. Synthesising it (plus seeding Wi-Fi) reaches the eWeLink cloud dial directly
(`s61_cloud_dial.py`), with no protocol reconstruction and no bound dump. A
genuinely eWeLink-bound dump is still useful — for a *faithful* dispatch server
(the factory URL is a placeholder) and a real device-secret — but is no longer
required to reproduce the dial. **Stage 1 (on the network) is done and faithful
either way.**

### The eWeLink BLE provisioning protocol

The provisioning input is the **eWeLink BLE provisioning protocol**, now
reverse-engineered end to end. A BLE write is routed by a **dispatcher**
(`0x72940`): it looks the message type up in a table at `0x145430` and posts
`{index, payload, len}` to a queue (`0x134b6c` → queue `*0x402dd0`); a consumer
task then calls `table[index].handler(payload, len)`. The six commands (named by
their `[BLE CMD: …]` log strings):

| type | handler | command | purpose |
|------|---------|---------|---------|
| `0x02` | `0x725e4` | **AUTH_TOKEN** | app proves it knows the device apikey |
| `0x04` | `0x727bc` | REQ_SCAN / REQ_AUTH | scan APs; sets state byte `0x40cf14=2` |
| `0x07` | `0x722a4` | **REQ_CONNECT** | connect to a scanned AP (matches BSSID) |
| `0x09` | `0x72258` | DEVICE_INFO_REQUEST | device returns its info; state `=5` |
| `0x0b` | `0x718a0` | **SERVER_INFO_REQUEST** | cloud/dispatch server |
| `0x0d` | `0x712e0` | **HIDDEN_AP_CONNECT** | Wi-Fi creds (`recv_ssid`) |

**Crypto, solved.** Each data-carrying message is base64-decoded then
**AES-128-CBC + PKCS#7** decrypted. The AES key and the AUTH_TOKEN "cache" are the
**same 16-byte secret at RAM `0x40ce90`** (derived from `factory_apikey` at boot; a
16-byte value, with the apikey UUID in the same page — real device identifiers, not
reproduced here). So the shared secret is **readable from the emulator** — no
forging. AUTH_TOKEN (`0x725e4`) `memcmp`s the decrypted token
against that cache (`0x13456c` at `0x726d0`; error `auth_token cmp err,
recv[%s] cache[%s]`); in the emulator that compare can be read-and-matched or
forced equal.

**The Wi-Fi message (type `0x0d`) is reconstructed and DRIVEN to a real join.**
`recv_ssid(r0=packed_buf, r1=tag)`, buf =
`[ssid_len:1][ssid][pad:1][timeout:1][encflag:1][password…]`; `encflag=1` takes the
path that copies the password into the connect config (the `encflag=0` plaintext
path copies only the ssid — a firmware quirk). `recv_ssid` packs a config
(`method=1` station, ssid@+1, pass@+0x22, bssid@+0xcf) and dispatches it through
the method dispatcher `0x21eb4`. Driving it in the appfw context (ARM force-call)
with the password decoder bypassed, **the plug associates (WPA2) and gets DHCP
`192.168.1.100`** — the eWeLink provisioning path, end to end
(`docs/s61-tools/` scratchpad `recv_drive.py`).

**What this reaches, and what it does not.** Driving these messages through the
dispatcher runs the handlers and connects Wi-Fi (`s61_ble_provision.py`), but does
not fire the **pairing-completion event**: the handlers share a session state
machine (session flag `0x40cfe3`, step byte `0x40cf14`) and a field accumulator,
and the completion is normally posted by the BLE-connection lifecycle, which the
radio-less emulator does not produce. So after the full message sequence the plug
is on Wi-Fi but still *unpaired* — it keeps emitting the `0xFFF6` eWeLink pairing
advertisement — and dials nothing. That completion event is, however, a single
firmware call; synthesising **that** (rather than the entire BLE session) is what
reaches the cloud, next.

### The cloud dial: synthesise the pairing-success trigger

The emulated plug now performs the real eWeLink (CoolKit) cloud handshake. Driven
end to end by `docs/s61-tools/s61_cloud_dial.py`: seed Wi-Fi creds (join + DHCP),
then force-call the firmware's own **start-cloud handler** once Wi-Fi is up. The
plug opens a TCP connection to its dispatch server and sends the CoolKit
**`GET /dispatch/device`** request, retried with backoff — the genuine first step
of eWeLink cloud registration. The target is read from the AIS context and is
**steerable**. The tool **defaults to a local stub on the emulator's own AP gateway
(`192.168.1.1:80`), so the dial stays on this host and reaches nothing outside it**;
the firmware's factory placeholder (`8.8.8.8:80`) is used only if you pass
`DISPATCH=` empty. **Owner's own account only**: never steer this at the real eWeLink
cloud with anyone's account or plug but your own.

**The cloud-dial gate chain.** `ais_link_task` (`0x35aa8`) is created by
`ais_cloud_start` (`0x35744`) via the RTOS thread-create `0x1349f0` (name/entry
literals `0x35820`/`0x35824` → entry `0x35aa9`). `ais_cloud_start` has one call
site, the **start-cloud handler `0x2c204`**:

```
0x2c204  if (*0x403e31 != 0 && precond_0x24fd8() == 0)   // precond returns ctx[0x31]
             ais_cloud_start(0x35744)
                 ctx = *0x4044ac;  if (ctx && ctx[0xaeb] == 0)  create ais_link_task
```

The start-cloud handler has exactly **three callers — `0x48ffe`, `0x49b76`,
`0x49ebc` — all inside the eWeLink pairing state machine** (`0x48a84`..`0x49cxx`,
the `PAIR_EVT_*` / `ewelink pair ok` code). On hardware the dial is kicked off by
pairing success; force-calling `0x2c204` substitutes for that one event.

**The AIS context is pre-populated at boot.** The cloud context `*0x4044ac` (heap,
e.g. `0x42c870`) already holds everything the link task needs, with no pairing:

| offset | field | value in the public dump |
|--------|-------|--------------------------|
| `+0x0b` | device UUID | the device's apikey UUID (redacted) |
| `+0x30` | model | `SN-BK7238-S61-01` |
| `+0x51` | MAC | the device MAC (redacted) |
| `+0x5f` | **dispatch URL** | `http://8.8.8.8:80` (factory placeholder) |
| `+0x31` | precond byte | `0` (gate passes) |

(The UUID and MAC are real per-device identifiers; they are read from the dump at
runtime but kept out of this public repo — see the private `captures/` for a
device's own values.)

So the link task is not "spawned cold" — its context is ready at boot; only the
*trigger* was missing. The factory dispatch URL being a placeholder (not a real
CoolKit host) is the one reason a bound dump still helps — for a faithful server
and real device-secret — but the dial mechanism itself is fully reproduced here.
Overwriting `ctx+0x5f` points the dial at any server; a local stub then completes
the exchange (dispatch → `/api/ws`, with the S60 trust-swap for TLS).

**Verified** (`python3 s61_cloud_dial.py`, the default local stub at `192.168.1.1:80`):

```
device IP = 192.168.1.100
dial-chain: {'ais_cloud_start': 1, 'link_create': 1, 'ais_link_task': 1}
TCP 192.168.1.100:62636 <-> 192.168.1.1:80 sends: HTTP GET /dispatch/device HTTP/1.1
  (retried t=34.6, 35.7, 37.2, 39.3, 41.9, 45.0, 48.6 — backoff)
```

This is the full provisioning outcome on the radio-less emulator: **the plug joins
our AP and dials the eWeLink cloud.** The only piece not reproduced is the BLE GATT
link that, on hardware, carries the pairing that *naturally* fires this trigger;
`s61_cloud_dial.py` synthesises that single completion event instead.

### The cloud handshake: trust-swap to ONLINE

With the dial reproduced, the rest of the eWeLink handshake completes against a
local stub and the plug reaches **ONLINE** — driven end to end by
`docs/s61-tools/s61_cloud_online.py` and the CoolKit stub
(`tools/espressif/emulator/stub_cloud/stub_server.py`, extended with a `--plain` dispatch
mode). Nothing leaves the host: the netstack relay (`attach_wifi_peer(net_out=True)`)
is redirected so the factory dispatch IP resolves to loopback — plaintext dispatch
on one port, TLS IoT on another.

**The dispatch reply the S61 parser needs (differs from the S60).** The embedded
cJSON parser in the dispatch handler (`0x34ca0`; parse `0x264d6`, getters
`0x2669c`/`0x265e2`) is strict in three ways the S60 is not, and each silently makes
the plug re-dispatch until fixed:

- the response must be **compact** — whitespace between tokens is rejected;
- it reads a **`domain`** key (the host it connects to), not only `IP`;
- **`port` is a string** (`"443"`), formatted via `wss://%.*s:%.*s`.

Hand back `{"error":0,"reason":"","IP":...,"domain":...,"port":"443"}` and the plug
accepts dispatch and opens the IoT WebSocket.

**The IoT channel is `wss://` and the cert is verified** (authmode REQUIRED), so a
self-signed stub cert is rejected with `bad_certificate` — the same trust problem
the S60 solves with `patch_stock --trust-ca`, fixed the same honest way: a
**trust-anchor swap**, not a verify bypass. The baked-in anchor is
`CN=CoolKit Root CA ECC, O=CoolKit, C=CN` (an EC CA, 510-byte DER). Unlike the S60,
the S61 keeps no static CA in its image — it **assembles the CA into RAM at
TLS-setup time** (via snprintf, around `0x3c0b6`) and installs it through
`mbedtls_x509_crt_parse` (`0x125e4c`; call sites `0x3c0ca`/`0x3c5b8`, both reached
only here for the IoT client). So the swap is applied at that one point: when the
firmware loads the CoolKit CA into a trust store, substitute our own EC P-256 CA,
identified by the CoolKit CN inside the decoded DER so only the anchor is touched.
Every check in the verify path then runs unmodified; only which root is trusted
changes, and the stub serves a leaf signed by that CA.

**Two leaf details the firmware's mbedTLS requires.** The stub leaf must carry a
**`dNSName` SAN** for the dialled address — an `iPAddress` SAN alone is not matched
against the name the firmware verifies (it sends SNI `8.8.8.8`) — and a
**serverAuth EKU**. With those the verification passes.

**Verified** (`tools/beken/emulator/bk-emu.sh python docs/s61-tools/s61_cloud_online.py`):

```
dispatch -> {"error":0,...,"domain":"8.8.8.8","port":"443"}   (accepted)
CA trust-swaps = 1   (CoolKit root CA -> our own CA)
websocket open                                 (TLS handshake against our CA)
ws <- {"action":"register","version":8,"romVersion":"1.0.2",...}
ws -> {"error":0,...,"config":{"hb":1,"hbInterval":10}}        => ONLINE
ws <- {"action":"query",...,"params":["TZ"]}   (heartbeat / timezone loop, reconnects)
```

So the radio-less emulator now runs the **whole** eWeLink cloud sequence — dispatch
→ TLS IoT WebSocket (trusting our CA) → `register` → ONLINE, then heartbeats. The
one thing a stub cannot stand in for is a *bound* session the **real** cloud would
accept (that needs eWeLink's device-secret, from a paired dump); with the trust
swap, a local stub completes the exchange here.

## Firmware update and conversion (the OTA path)

The goal is the S60-style conversion to open firmware — here **OpenBeken / ESPHome**
for the BK7238 — driven through the vendor's own OTA, as the S60 is through the
CoolKit cloud. The headline: **a third-party, unsigned image is accepted and
written to the app partition — there is no signature and no secret key anywhere in
the OTA path.** Images are packed with AES-256 under RT-Thread's *public* default
key, which the bootloader has compiled in, so the encryption protects nothing; the
integrity checks are standard CRC-32 and an FNV-1a hash; and the RBL `crypt` field
is a header *flag* that need only be non-zero.

### Server redirect and OTA initiation

The S61 uses the **same CoolKit OTA mechanism as the S60**: the cloud pushes an
`{"action":"upgrade","params":{"model","version","binList":[{"downloadUrl",
"digest","name"}]}}` message over the IoT WebSocket; the device's OTA task
(`ais_qota_task`, user-agent `FOTA/1.0`) downloads from whatever `downloadUrl` the
command carries, over HTTP with a ranged `bytes=%lu-%lu` request, independent of
the WebSocket session. There is **no separate update-server setting** — the command
dictates the URL, exactly as `tools/ewelink/probe_ota_command.py` relies on for the S60.

Reproduced on the emulator: with the plug ONLINE on the local stub, pushing the
`upgrade` command (the stub holds the device's socket, so it sends the frame
directly) makes the plug issue `GET <our-url>` with `FOTA/1.0`. Served a 404, it
fetches without flashing — the safe-probe pattern. So we can both **initiate** the
update and **point the server at one we control**.

### The CoolKit "FOTA" download container

The OTA download is **not** a raw RBL — `ais_qota_task` expects a CoolKit **FOTA**
container, decoded from the device's own read offsets and parse code:

- the HTTP response must be **`206 Partial Content`** (the client checks the status
  is 206) and **keep-alive** (it reads several sections on one connection);
- layout is **header, trailer, descriptors, then payloads** (decoded empirically):
- **`[0:16]` header**: magic **`0x1beef11e`** (LE) at `+0`, a `u16` at `+12` that
  sizes a region before the trailer — set it `0` so the trailer sits at `+16` and the
  descriptor offset base is `16`;
- **`[16:40]` trailer**: descriptor count at byte `+1` (1–3), a version string at
  `+2` that must be **lexically greater than the running version** (`"1.0.2"`), and a
  **CRC32 over the first 20 bytes, stored big-endian** at `+0x14`;
- **`[40:40+76·n]` descriptors** (76 bytes each), data `[0:0x3c]` + a big-endian CRC32
  at `+0x3c` over `[0:0x3c]`. In the data region: an **18-byte image-type name at
  `+0x00`**, a version at `+0x20`, a **big-endian `u32` file-offset at `+0x30`** (the
  image's absolute offset = this + base `16`), and a **big-endian `u32` size at
  `+0x34`**. The name selects the handler that **stages to the download partition**;
  it must be **`"FWSW-01-S61-BK7238"`** (an `strncmp` at `0x2c45d`). A wrong/empty name
  falls through to a no-op handler that buffers in RAM and never writes flash — which
  is why early attempts downloaded but staged nothing;
- **payloads** follow the descriptors, each at the offset its descriptor gives.

The CRCs are standard CRC-32 (zlib poly), stored big-endian because the firmware
byte-swaps the field before comparing. With this container the plug (ONLINE on the
local stub) downloads via ranged `GET`s and **writes the payload verbatim to the
download partition** — confirmed byte-for-byte (an 8 KB test RBL staged with a
matching SHA-256; and a 507 KB real OpenBeken `.rbl` staged byte-exact over the
first 446 KB before the test's time budget ended the download).

### Bypassing FOTA: stage directly, and the bootloader gate chain

The FOTA container is only the cloud **transport**. The question "will the device
accept and boot a third-party image?" is answered by the **bootloader**, so the
quickest test stages an `rt_ota` RBL **directly in the download partition** and
boots, skipping the container.

Flash geography (physical offsets, found by logging the reads the bootloader issues
through the flash controller at `REG_FLASH 0x803000` during boot):

- the **`app` partition's own RBL header** sits at **`0x285f9a`** (just before the
  download partition) — overwriting it only makes the bootloader log `App verify
  failed! Need to recovery factory firmware.` and boot the existing app, so leave
  it alone;
- the **`download` partition starts at `0x286000`** (erased in the public dump — no
  update staged). Writing an RBL header at `0x286000` with its body after it stages
  an update.

On boot the bootloader runs `ota_main` → `rt_ota_check_upgrade` →
`rt_ota_upgrade` → `copy_fw_from_dl_part`. (It lives in the bootloader partition
`0x0–0x10000`; its message strings are loaded with `adr`, PC-relative, so a
data-word xref does not find them, but the code is at offset-equals-runtime and the
`__func__` pointers are ordinary literals.) Three gates, in order:

**Gate 1 — the `crypt` nibble.** `ota_main` reads the RBL algorithm's `crypt`
nibble and, if it is *zero*, logs **`none encryption Not allow!`** (`ota_main:125`)
and refuses the update, so it must be non-zero. The only key in play is RT-Thread's
**public default AES key** (`0123…`), the one the SDK's packer uses — no per-device
or secret key. The stock app declares `0x402` and OpenBeken's release RBLs `0x102`;
both satisfy the gate. (The emulator also offers an opt-in to force the branch.)

**Gate 2 — `rt_ota_custom_verify` is a link-address sanity check, not a signature.**
With a mis-linked image it logs
**`The application must link to the flash 64KB location!`** (`rt_ota_custom_verify`):
it CRC16s the vector table and checks that an address field in the image lies within
the `app` partition range `[0x10000, 0x10000+size)` — i.e. the firmware must be
**linked to run at `0x10000`**. A real OpenBeken / ESPHome BK7238 build (linked
there) passes it; only a random test body fails. **No cryptographic signature is
enforced anywhere in the apply path.**

**Gate 3 — the firmware CRC-32.** `copy_fw_from_dl_part` verifies the body against
the RBL header's CRC fields using **standard CRC-32** (polynomial `0xedb88320`;
`body CRC32` over the packaged body, `raw hash` over the decompressed image). These
are plain `zlib.crc32`, computed offline when building the image — no oracle needed.

### Result: the bootloader erases and writes a third-party image

With gate 1's flag set, gate 2 satisfied (or bypassed for a dummy body — it gates on
no secret), and gate 3's CRC supplied, the bootloader logs the full apply:

```
OTA firmware(app) upgrade(1.0.2->9.9.9) startup
The partition 'app' is erasing  →  erase success
OTA Write: [app] [====================================] 100%
```

It **erases the app partition and writes our unsigned, third-party image to it.**
So the conversion is **feasible** at the firmware level: no signature, no encryption,
no per-device binding is needed to have an image accepted and flashed.

### Emulator support for the in-emulator apply + boot

Performing the apply in the emulator needs two pieces of flash hardware the bootloader
relies on only when reprogramming its own flash; both are now modelled (behind a flag
enabled for self-reprogramming emulation — see
[The emulator performs the whole OTA apply](#the-emulator-performs-the-whole-ota-apply)):

- the SPI flash JEDEC id (`REG_FLASH` RDID result at `0x803010`), from which the
  bootloader computes the flash size — without it the size is 0 and all its writes are
  bounds-checked out;
- XIP write-through: each program/erase is mirrored into the memory-mapped flash image
  de-striped, so `part_fw_hash_verify` and the app jump read what was just written
  (unicorn memory `0x0–0x200000` is otherwise static once loaded).

Neither bears on real hardware, where the write and read-back are consistent.

### The apply path

`copy_fw_from_dl_part` reads the staged RBL body from the download partition
(`0x7654`/`0x7218`: bounds-check, then the flash device's read method), applies the
declared crypt and compression, verifies it against the header, and writes it to the
`app` partition. What matters for a conversion:

- The only key anywhere in the path is RT-Thread's **public default AES key**
  (`0123…`), the one the SDK's own packer uses — no per-device or secret key, and no
  cryptographic **signature**.
- Compression is selected by the algorithm's compression nibble; the SDK's
  decompressor is an LZMA-family range coder, and a genuine OpenBeken release RBL
  (gzip) decompresses on the stock bootloader.
- Integrity is the header's two **CRC-32**s (`body CRC32` over the packaged body,
  `header CRC32`) and the **FNV-1a** `raw hash` over the decompressed image — the
  "Beken on-chip hash" is plain CRC-32 (`0xedb88320`), all reproducible offline.
- The `0x0080b400` hardware-security block fires once at boot and is unrelated to
  the OTA payload.

### The download path writes bytes verbatim

The same question for the cloud download — does `ais_qota_task` transform the bytes
before storing them? — resolves the same way. The download loop issues ranged
`GET`s, copies each chunk into a RAM buffer, and calls the image type's write
callback, which lands in the FAL partition write (`0x1349f0`); the per-image
*validate* handler the stock type registers is literally `movs r0, #0; bx lr` (a
no-op success). The only checks are integrity: the FOTA magic, the trailer and
descriptor **CRC-32**s, the version-greater-than comparison, and the `digest` (a
SHA-256 of the file, from the `upgrade` command — which we compute over whatever we
serve). **Bytes are written to the download partition verbatim.**

### Proven: a real OpenBeken image converts over OTA

The compression need not be reverse-engineered, because **OpenBeken already ships
BK7238 OTA images in this exact `rt_ota` format** — the `OpenBK7238_<ver>.rbl` assets
on the [OpenBK7231T_App releases](https://github.com/openshwprojects/OpenBK7231T_App/releases).
The `1.18.319` image is a well-formed RBL: magic `RBL\0`, name `app`, `algo = 0x102`
(crypt flag `2`, **compression nibble `1`** — the stock app uses `4`), `body CRC32`
a standard `zlib.crc32`, `raw hash` an FNV-1a, `size_raw` 750544, `size_package`
506976.

Staged directly in the download partition of a dump copy and booted on the **stock
bootloader with no bypasses**, it passes every firmware gate:

```
OTA firmware(app) upgrade(1.0.2->1.18.319) startup
total_len(750544) >= size_raw(750544), _STREAM_END     ← decompresses (compress=1 supported)
The partition 'app' erase success
OTA Write: [====================================] 100%  ← full image written
```

So algorithm support, the `crypt` flag, `body CRC32`, **decompression of the whole
image**, the streaming FNV `raw hash`, `custom_verify` (OpenBeken links at `0x10000`),
and the final `part_fw_hash_verify` all pass for a genuine third-party conversion
image, and the bootloader then **boots the freshly-installed OpenBeken** — the emulator
performs the whole apply itself (see
[below](#the-emulator-performs-the-whole-ota-apply)).

End to end, then: push `upgrade` with a `downloadUrl` we control → the plug downloads
our FOTA-wrapped OpenBeken `.rbl` and **stages it verbatim** to the download partition
(confirmed byte-for-byte for the full 507 KB image) → on reboot the stock bootloader
decompresses and applies it. Nothing in the chain decrypts, signs, or needs a secret.
`s61_fota_wrap.py` builds the container for any `.rbl`. The remaining step — flashing a
real plug — is irreversible and is not done here.

### Booting the converted firmware in the emulator

The emulator runs OpenBeken. Booting `OpenBK7238_QIO_<ver>.bin` (OpenBeken's full
flash) comes up to its application layer — `OpenBK7238, version 1.18.319`, config
load, MQTT registration, and the config SoftAP. And the **conversion** itself boots:
splicing OpenBeken's app (from the QIO, CRC-striped) over the stock app partition while
keeping the **stock bootloader** and booting it gives

```
RT-Thread OTA package(V0.2.8-beken-...) init success   ← the stock S61 bootloader
go os_addr(0x10000)                                     ← jumps to the app
SDK Rev: 3.0.78 / OSK Rev: F-3.0.51                     ← OpenBeken's build (stock is 3.0.70)
OpenBK7238, version 1.18.319
Main_Init_After_Delay done
softap ssid=OpenBK7238_<id> channel=1 cipher=OPEN       ← OpenBeken's config AP is up
```

(The `<id>` OpenBeken appends is derived from the device identity in the dump and is
redacted here, like every other per-device value in these notes.)

So the stock bootloader boots the OpenBeken app end to end (this can also be shown
directly by splicing OpenBeken's app over the stock app partition and booting).

#### The emulator performs the whole OTA apply

The emulator reproduces the full apply itself — staged OpenBeken RBL in the download
partition → erase → decompress → program → `part_fw_hash_verify` → jump → OpenBeken
boots — once it models two pieces of flash hardware the bootloader relies on only when
reprogramming its own flash:

- **The SPI flash JEDEC id (`REG_FLASH` RDID result at `0x803010`).** The bootloader
  derives the total flash size from it (`2 << ((id & 0xff) - 1)`); an unmodelled
  register reads 0, so the size is 0 and *every* bootloader write fails its
  `(addr + len <= size)` bounds check and is silently skipped — the apply then logs
  success having written nothing. Modelled to return an id whose capacity byte matches
  the image size (and whose maker/type match the bootloader's chip table).
- **XIP write-through.** The memory-mapped flash image is loaded once at setup; real
  hardware reads it live through the controller's 32+2 CRC framing, so each
  program/erase is mirrored into the XIP map de-striped. Without this the hash-verify
  (and the jump) read the stale pre-apply bytes.

Both are gated behind an emulator flag enabled only when emulating self-reprogramming
firmware (modelling real flash writes is correct but makes flash-writing firmware run
slower), so normal device emulation is unaffected. With it on, the apply completes and
OpenBeken boots exactly as above; the written app de-stripes byte-for-byte to the
OpenBeken image and its FNV equals the RBL `raw hash`.

### Carrying Wi-Fi across the conversion: the OpenBeken config

Like the S60, the conversion should hand the user's Wi-Fi credentials to the new
firmware so the plug rejoins their network with no typing. The stock creds live
in the EF40 store (above), which the OTA does not overwrite. Two ways to get them
into OpenBeken were worked out; the **import module is the one to ship**.

**OpenBeken's config format (`mainConfig_t`), reverse-engineered and verified
byte-exact against a real OpenBK7238 1.18.319 config:**

- Layout (`src/new_pins.h`): `ident0/1/2 = 'C','F','G'` (ofs 0–2), **`crc` byte
  at ofs 3**, `version` `int = 5` at ofs 4, then flags/counters, **`wifi_ssid[64]`
  at `0x14`**, **`wifi_pass[64]` at `0x54`**.
- `sizeof(mainConfig_t)` on the BK7238 build is **3584** (proven: OpenBeken
  writes exactly 3584 bytes in one `HAL_Configuration_SaveConfigMemory` burst).
- Checksum: `Tiny_CRC8` (`src/tiny_crc8.c`) over bytes **`[4 : 3584]`**, result in
  the `crc` byte. **The shipped binary compiles `char crc` as *signed*, so
  `crc >>= 1` is an arithmetic (sign-extending) shift** — the plain unsigned
  CRC-8/MAXIM gives the wrong value (`0xac` vs the real `0x80`) and every seeded
  config is rejected until this is right. In Python:
  `crc = ((crc>>1)|0x80) if (crc&0x80) else (crc>>1)`, then `if bit: crc ^= 0x8C`.
- Stored at the start of `BK_PARTITION_NET_PARAM` — physical **`0x1e1000`** on the
  standalone BK7238 2 MB build (BK7231N's SDK uses `0x1D1000`).

**Proven in the emulator:** read `wifi-ssid`/`wifi-pass` from the stock EF40 →
build a `mainConfig_t` with those creds → write it at `0x1e1000` of a 2 MB-padded
OpenBK7238 image → boot: `Correct config has been loaded`, `Using SSID [...]`,
four-way handshake, DHCP lease. End-to-end `s61_wifi_migrate.py` does the whole
read→build→seed chain.

Emulator gotchas worth remembering: pad the QIO image to the real flash size or
the `NET_PARAM` write is out of range; set `emu.flash_writethrough = True` so
config save/read works; UART (incl. the `CFG:` verdict) prints to **stdout**, not
the `log`/`uart_sink` callbacks; and config writes are AND-without-clean-erase, so
a *later* re-save corrupts the blob — hook the flash-program path and capture the
**first** write burst for a clean reference.

#### Energy-meter calibration: the stock formula, and the OpenBeken mapping

The meter is a **BL0937** (the stock driver is registered as `bl0937b`). Its
factory calibration is the EF40 key **`elecCalibCoef`** (80 B): `u32` magic, a
flag byte (`1`), padding, then **three 24-byte records in the order current,
voltage, power**, each beginning with three `int32` **`a, b, c`** (the rest of
each record is unused). The setter at `0x31160` writes that shape; the getter at
`0x311ac` checks magic and flag and hands the driver init (`0x301c0`) the 72-byte
payload, which copies each record's `a,b,c` into the driver's calibration object
(`0x404bbc`, mode 1).

The driver's measurement function (`0x41a84`; `0x41918` is the uncalibrated
twin) works in doubles. From the pulse period it forms the frequency `f` (Hz) and
the **HLW8012-datasheet nominal value** with these board constants:

| quantity | nominal from `f` | constants |
|---|---|---|
| current | `I = f · 1.1 · 1000 / 94638` | 94638 (datasheet), 1 mΩ shunt |
| voltage | `V = f · 1.1 · 2040 / 15397` | 15397 (datasheet), 2040:1 divider |
| power   | `P = f · 1.21 · 2040 · 1000 / 1721506` | 1721506 (datasheet), 1.21 = 1.1² |

(1.1 plays the role of V_ref; BL0937's nominal is 1.218 V, which is why the
factory trims below come out ~0.88–0.95.) It then applies the factory trim

```
x_cal = a·x²/1e7 + b·x/1e6 + c/1e6
```

— the same CoolKit quadratic mvoss96 found in the S60's `calib_coefficient0`
(adamsthws/Sonoff-OTA PR #7). Currents below 3 mA zero current and power. On the
dump: `a = c = 0` for all three, `b = 954547 / 879893 / 930747` (I/V/P): a pure
linear trim.

**OpenBeken's BL0937 driver measures the same `f`** — it counts falling edges of
CF/CF1 per second (`drv_bl0937.c`) and scales `real = f · cal`
(`drv_pwrCal.c`, `PWR_CAL_MULTIPLY`). So for a linear trim the mapping is exact:

```
cal_I = (1.1·1000/94638)        · b_I/1e6   = 0.011623 · 0.954547 = 0.011095
cal_V = (1.1·2040/15397)        · b_V/1e6   = 0.145743 · 0.879893 = 0.128238
cal_P = (1.21·2040·1000/1721506)· b_P/1e6   = 1.433861 · 0.930747 = 1.334562
```

(OpenBeken's generic defaults are 0.0118577 / 0.1325301 / 1.5 — same magnitudes,
which is the independent check that `f` means the same thing in both.) The
import module applies these with `CFG_SetPowerMeasurementCalibrationFloat` on
first boot, and sets `PowerMax` to the plug's 3840 W rating. A non-zero `a` or
`c` has no OpenBeken equivalent; the module then uses `b` and logs a warning.
Not yet done: a check against a real metered load on hardware.

#### The S61's wiring (proven OpenBeken template)

The OpenBeken device database carries an entry for exactly this board,
**S61STPF / `WBBK_01P_V1.3`**, verified on hardware by its author
(elektroda.com topic 4157852; `OpenBekenIOT/webapp` `devices.json`):

| pin | role | channel |
|---|---|---|
| P26 | `Rel` (relay) | 1 |
| P9 | `Btn_Tgl_All` (button) | 1 |
| P0 | `LED_n` (relay/power LED, active-low) | 1 |
| P1 | `WifiLED_n` (Wi-Fi LED, active-low) | 2 |
| P8 | `BL0937CF` (power pulses) | – |
| P6 | `BL0937CF1` (V/I pulses) | – |
| P24 | `BL0937SEL` | – |

It agrees with the stock driver: the 6-byte parameter block its init passes to
`bl0937b::open()` (`[0x13548c]+0x24` = `26 00 00 00 02 18`) ends in `0x18` =
P24 for SEL (and `0x26` = `8<<2 | 6` looks like CF/CF1 packed). The template's
`loglevel 0` worked around UART logging disturbing pulse counting on BK7238,
fixed upstream in 1.18.235 (PR #1930); our 1.18.319 base has the fix. The import
module applies this template on first boot, plus `SetStartValue 1 -1` so the
relay restores its last state after a power cut (what the tool sets on the S60).
The ESPHome config ([`ESPHome/sonoff_s61.yaml`](../ESPHome/sonoff_s61.yaml),
on LibreTiny) uses the same pins (`hlw8012: model: BL0937, sel_pin: P24,
cf_pin: P8, cf1_pin: P6`); see [the ESPHome route](#the-esphome-route-libretiny).

#### Partition map: the SDK's 4 MB table *is* Sonoff's layout

OpenBeken's BK7238 build hard-wires `CFG_FLASH_SELECTION_TYPE =
FLASH_SELECTION_TYPE_2M` (`sys_config_bk7238.h:374`), so stock OpenBeken uses the
SDK's **2 MB** partition table even on the S61's 4 MB chip: OTA at `0x132000`,
NET_PARAM (config) at `0x1e1000` — both *inside* the stock app partition, and its
OTA slot is one the stock `rt_ota` bootloader never looks at. The SDK's **4 MB
table** (`BkDriverFlash.c`, `bk7231_partitions_4M`) is, to the byte, the Sonoff
layout — the same Beken SDK: `APPLICATION 0x11000`, **`OTA 0x286000`** (= the
stock download slot), **`RF_FIRMWARE 0x3e0000`** (= the factory TLV with the
radio calibration), `NET_PARAM 0x3e1000` (empty on a stock plug), `MATTER_FLASH
0x3e2000` (= the EF40 app store), `USR_CONFIG 0x3ee000`. Building the S61 image
with `FLASH_SELECTION_TYPE_4M` therefore makes OpenBeken's **own OTA download
land in the slot the stock bootloader applies from** (so both OpenBeken updates
and reversion to stock go through the ordinary `ota_http`), puts its config in a
dedicated empty sector, and lets it read the plug's real radio calibration.
`hal_ota.h` hard-codes the REST `/api/ota` address separately (`0x132000`), and
`rest_interface.c` bounds `GET /api/flash` at 2 MB; both now follow the partition
table. Verified in the emulator: the 4 MB build writes its config at `0x3e1000`
and `0x1e1000` stays blank.

The build is a `FLASH_SIZE=4M` switch, in two patches kept in
[`docs/s61-tools/openbeken/upstream/`](../s61-tools/openbeken/upstream/) and
offered upstream — OpenBK7231T_App#2290 and beken_freertos_sdk#13, see
[upstream-contributions.md](upstream-contributions.md#openbeken-on-a-4-mb-bk7238)
(the recipe is in [its README](../s61-tools/openbeken/README.md#building-bk7238-4-mb-table)).
Packing a whole 4 MB image needed a 4 MB bootloader, and that turned up a tidy
fact: **the S61's factory bootloader is the SDK's shipped
`bootloader_bk7238_uart1_v1.0.15.bin` with the 4 MB FAL table in its tail**,
byte for byte — three 64-byte `01PE` entries (`bootloader 0x0+0x10000`, `app
0x10000+0x250000`, `download 0x286000+0x15A000`; name, flash device, offset,
length, CRC-32 over the first 60 bytes). The SDK's packager generates that
bootloader from the shipped one (`make_bootloader_4m.py`), so a 4 MB QIO from
the build is a converted plug's whole flash, and the emulator boots it as such.
In that run OpenBeken's `ota_http` downloaded to `0x286000` and its own
bootloader applied it (`upgrade(4M.1220->s61v6)`, `_STREAM_END`, slot erased);
the emulator then stayed at the bootloader banner, but the persisted flash
verifies (the written app matches the RBL header's FNV-1a) and boots s61v6 when
loaded afresh — emulator state after an OpenBeken-initiated watchdog reboot, not
the image.

#### Reversion to stock, and the backup

The stock bootloader applies whatever valid RBL sits at `0x286000` — including a
**downgrade**: staging the stock app RBL on a converted plug logged `OTA
firmware(app) upgrade(s61v2->1.0.2)`, erased and wrote `app`, and the stock
firmware booted (`EasyFlash V4.1.99 is initialize success`) with the EF40
untouched (same 33 keys, same Wi-Fi creds, same `elecCalibCoef`). The stock app
RBL is rebuilt from the dump: the app partition holds the *decompressed* image
(the OTA body is gone), so the un-striped app (`FNV-1a` matches the stored
`raw_hash 0x0d89f7b1`, i.e. byte-exact) is repackaged with the SDK's own packer
exactly as the OpenBeken build does —
`rt_ota_packaging_tool_cli -f app.bin -o app.rbl -p app -c gzip -s aes -k
0123456789ABCDEF0123456789ABCDEF -i 0123456789ABCDEF -v 1.0.2` — using the same
public default AES key as every other RBL, which the bootloader accepts.
`S61RevertToStock <url>` in the import module checks the OTA slot is `0x286000`
in both the SDK table and `START_ADR_OF_BK_PARTITION_OTA` (the constant the REST
OTA path uses), refusing on a 2 MB-table build where the download would land at
`0x132000`, and hands the URL to OpenBeken's normal OTA download. Run end to end in
the emulator against a host HTTP server: `init OTA, startaddr 0x286000` → reboot →
`upgrade(s61v6->1.0.2)` → stock firmware up, EF40 untouched.

[`docs/s61-tools/s61_backup.py`](../s61-tools/s61_backup.py) copies the
per-device region `0x3e0000–0x400000` (factory TLV, EF40 app store, system ENV)
out of a converted plug over `GET /api/flash/<addr>-<len>` (the REST reader's
`DEFAULT_FLASH_LEN` is raised to 4 MB in the S61 build) into
`captures/plugs/<mac>/sonoff-backup/config-0x3e0000.bin` with a manifest and the
stock RBL beside it — the S60's layout. Byte-exact against the dump in the
emulator. With the generic stock app and this region, a plug can always be put
back to stock.

**The robust, update-proof mechanism (ship this): an OpenBeken import module**
([`docs/s61-tools/openbeken/drv_sonoff_s61_import.c`](../s61-tools/openbeken/drv_sonoff_s61_import.c)).
On first boot (only when no SSID is set) it reads the stock EF40 store with
`beken_hal_flash_read`, finds the newest CRC-valid `wifi-ssid`/`wifi-pass`, and
calls OpenBeken's own `CFG_SetWiFiSSID()`/`CFG_SetWiFiPass()`. Because it uses the
setters rather than writing the blob, it is **immune to `mainConfig_t` layout/CRC
changes across OpenBeken releases** — the offline-blob route above is version-
fragile (it broke on exactly the signed-CRC and `sizeof` details) and is kept only
as a fallback / emulator aid. The module's EF40 scan + CRC + extract logic is
validated against the dump; building it into an OpenBeken image and flashing it is
the remaining on-device step. It also only reads the stock store, so it is fully
reversible.

### The ESPHome route (LibreTiny)

The second open firmware for the S61 is ESPHome on **LibreTiny**, whose 1.13.0
(the version ESPHome 2026.10.0 pins) has the BK7238. The config,
[`ESPHome/sonoff_s61.yaml`](../ESPHome/sonoff_s61.yaml), is the S60's with the
S61's wiring and storage, and needs no change to LibreTiny or ESPHome; the
build, the reversion and the validation are in
[docs/s61-tools/esphome/README.md](s61-tools/esphome/README.md). What the
route rests on:

- **The image is the same `rt_ota` format** OpenBeken's is: LibreTiny's build
  produces `image_bk7238_app.ota.rbl` (name `app`, gzip, AES with RT-Thread's
  public key), which the stock bootloader applies from the download slot like
  any other. Staged at `0x286000` through the cloud-pushed `upgrade` (the
  FOTA container, verbatim), the bootloader logs
  `upgrade(1.0.2->26.10.29-bk7238)`, `_STREAM_END` with the raw size the
  build reported, erases and writes `app`, and jumps.
- **LibreTiny's partition table is the board's `flash` section**, which it
  wraps over the SDK's (`BkDriverFlash.c`), and PlatformIO's `board_flash.*`
  overrides reach both the firmware and ltchiptool (the build writes the
  merged board to `board.json`). So the config carries the S61's layout in
  `platformio_options`: app `0x11000+0x275000` with `bkrbl_size_app
  0x250000`, download `0x286000` (ESPHome's own OTA then lands where the stock
  bootloader looks), calibration `0x3e0000`, FlashDB preferences at
  `0x3ee000` (the empty 32 KB OpenBeken's 4 MB table calls `USR_CONFIG`),
  LittleFS at the empty `0x3e1000`. Nothing of the stock stores is touched.
- **Wi-Fi and calibration come from the stock EF40 store at boot**, read with
  `lt_flash_read` in a boot lambda (the import module's scan, in C++): there
  is no image imprinting on this route, as the packaged image cannot be
  patched, and none is needed. The calibration is applied with the stock
  formula above; ESPHome's `hlw8012` reckons the pulse rate with the BL0937
  datasheet's 1.218 V where the stock driver uses 1.1 V, so the readings are
  scaled by that ratio (squared for power) before the trim.
- **Reversion** is ESPHome's own OTA: LibreTiny writes a UF2's `download`
  part to the download slot, so a UF2 carrying the stock app RBL (ltchiptool
  builds one) brings the stock bootloader's apply on the next reboot - and
  one carrying OpenBeken's 4 MB-table `.rbl` moves the plug to OpenBeken,
  whose `ota_http` of the ESPHome `.rbl` moves it back. Both proven in the
  emulator: each first boot takes Wi-Fi, calibration and wiring from the
  stock store, and the two firmwares' own settings live in different sectors
  (`0x3ee000` ESPHome, `0x3e1000` OpenBeken), so neither disturbs the other.
- **The emulator needed one change** (in the fork): LibreTiny keeps its
  FreeRTOS tick on hardware **Timer 0** (the 26 MHz group, for a fine
  `micros()`; `cores/beken-72xx/base/fixups/clock_rtos.c`), where OpenBeken
  and the stock firmware use Timer 3. The emulator raised only Timer 3's
  status, so LibreTiny's timer ISR acknowledged a channel with no handler
  while `xTaskIncrementTick` never ran: every log line said `0.000`, the
  first `delay()` never returned, and the Wi-Fi start stalled at `Waiting for
  init_thread`. It now models the 26 MHz group too (Timer 3 as before):
  each enabled channel fires at its programmed period, at the slow tick's
  resolution. The period is not optional: OpenBeken's BK7238 build feeds its
  interrupt watchdog from a 10 s channel of that group (two key writes with
  a busy delay between), and a first version that raised every enabled
  channel on every tick ran that feed back to back and starved OpenBeken
  right after `Main_Init_Before_Delay`.

### Tools (reproducibility)

The drivers that produced the above: `find_dlpart.py` (locate the download
partition via `0x803000` read logging), `rbl_pack.py` (the verified RBL header
packer), `s61_ota_probe.py` (push `upgrade`, serve a 404, prove the server
redirect), `s61_ota_flash.py` (build the FOTA container, serve the ranged download,
log flash writes; an `OBK_RBL=` env serves a real OpenBeken `.rbl`), `s61_stage_boot.py`
(stage an RBL and boot the gate chain, with the crypt-flag / custom-verify opt-ins),
`app_apply_test.py` (boot the stock bootloader over a directly-staged RBL, no bypasses),
`obk_apply_test.py` (the same with a real OpenBeken `.rbl`), and `s61_fota_wrap.py`
(wrap an `.rbl` in the FOTA container and optionally serve it), plus a Thumb/ARM
disassembler over the de-striped image and `boot_qio.py` (boot an OpenBeken flash /
the spliced conversion image). The BK7238 UART transmit register is `0x0080210c` (for
a boot-proof image). No per-device identifier from any run is kept in these notes.

## The button press (the pairing trigger)

On real hardware you press and hold the button for 5 s to put the plug into
pairing. The button's hardware and driver path is understood and can be driven in
the emulator through the firmware's own code up to **arming the real 5-second
long-press timer**; what is not yet reached is the app-layer path that turns that
long-press into pairing entry (the physical long-press drives the relay, not the
eWeLink pairing trigger — see below).

### How the button works on this firmware

The button is a **GPIO 0–5 capture interrupt on the PWM/timer block at
`0x802B00`** (three groups of `0x40`, two channels each = pins 0–5). It is **not**
the GPIO `INTEN` bitmask at `0x802884` — which is why earlier attempts driving
pins 10/11 did nothing; the button interrupt lives on a different peripheral.

Interrupt chain:

```
pin edge
  -> PWM-capture interrupt (status in bits 30/31 of the group reg)
  -> ICU IRQ line 9
  -> ISR 0x1bfd8        reads group status, finds the channel,
                        dispatches callback(r0=pin, r1=level) from the
                        per-pin table at 0x4038f4
  -> key handler 0x4203c
  -> debounce           reads the pin via 0x3eb30, counter threshold > 2
  -> on a held press: 0x42078 arms a 5-unit timer (0x1348fc, r1=5 = "hold 5 s")
                      and posts a key event
```

### Registration

`key_add` (`0x41f64`) wires the button up:

- writes the pin to `0x404c25`, fills the key-id table `0x404bb4` (= the pin
  number), sets the driver flag `0x404c23 = 1`;
- registers the callback via `0x3f090` → `0x1c14c` (into the per-pin table
  `0x4038f4`);
- enables the capture interrupt via `0x3f0d0` → `0x12c214` → `0x1c2f0` (writes the
  per-pin field in the `0x802B00` group reg);
- creates the key-driver timer thread via `0x3ec6c` (`0x1349f0` thread-create,
  entry `0x3eba0`, startup `0x217ec`, period `0x64`).

The block ISR is registered by the block init `0x1bc18`
(`intc_register(9, 0x11, 0x1bfd8)`).

The long-press is driven by that **key-driver timer thread** (entry `0x3eba0`): it
waits on a signal (`0x134618`, timeout −1 = forever), then walks a timer list,
decrements each countdown, and fires expired callbacks (`blx` at `0x3ec18`); the
periodic scan is `0x421e0`. The armed 5-unit long-press timer sits in that list.

### After the long-press: into pairing

The long-press callback posts a key event that the eWeLink **app key task**
consumes and turns into pairing entry. Pairing then runs through the
pairing-method dispatcher `0x21eb4` (method byte `0`=airkiss → `airkiss_start`
`0x21e50`, `1` → `0x21d18`, `2` → `0x21d90`), fed by the config builder `0x74870`
/ method runner `0x748cc`, driven by `PAIR_EVT_START_{COEXIST,EWELINK,BCM}` (name
table `0x49c18`) through the AIS appfw state machine (state struct `0x404fb4`,
state byte `0x404fb8`, functions `0x48a88`..`0x496b8`, appfw loop ~`0x49b40`).
Reaching pairing starts soft-AP / BLE / AirKiss commissioning; completing a Wi-Fi
join from there still needs the app + cloud (or a Matter commissioner).

### Why it is dormant in the emulator

The whole eWeLink app layer — `ais_appfw_task`, the button registration
(`key_add`), the provisioned-check — never starts because the `app` RT-Thread
**hangs in a factory device-identity self-check**. See
[The flash-UID integrity gate](#the-flash-uid-integrity-gate-root-cause) below;
correcting it makes the firmware boot the app framework and **register the button
by itself**, in proper RTOS context. The block init `0x1bc18` *does* run at boot
(it is generic); `key_add` and the key subsystem do not, only because they are
downstream of the hung app-start.

One emulator-modelling fact still matters for a simulated press:

- **The emulator models `0x802B00` as PWM output only.** The capture/PWM
  interrupt-status bits 30/31 are kept virtually in `state.pwmn_stat` (overlaid on
  reads, W1C on writes, driving ICU bit 9) and are set from PWM activity, never
  from a GPIO-capture edge. So even a fully set-up channel would not see its edge
  raise the interrupt. Setting `state.pwmn_stat` for the channel is the hook that
  fires it.

### The flash-UID integrity gate (root cause)

Boot creates every RTOS task through `ais_evt_task`, then the `app` task runs AIS
app-start `0x24430` → `0x29b54`, which reaches a poll loop at `0x29c38`: it builds
`SHA256(hex(flash_uid) + identity_fields)` (hash `0x25454`, string via `0x19b70`)
and `memcmp`s it (`0x42788`) against a stored 32-byte blob at `ctx+0x84`. On a
**mismatch it branches to `0x29c44`, an infinite 1-second-delay loop** — a
deliberate integrity-gate hang, confirmed by a stack unwind that finds the `app`
task parked there (`0x24430`→`0x29b54`→`0x29c4a`→delay `0x1349b4`).

The 16-byte identity is the SPI flash chip's Unique ID: `0x2856c` → `0x21408`
opens the `flash` device and reads **`bk_flash_get_uid`** (SPI command `0x4B`).
The boot fix `spi_flash.py` answers `0x4B` with a *synthetic* UID (ASCII
`S61EMU\0\1` = `533631454d550001ffff…`), so the computed hash (`e3f30c7e…`) never
matches the dump's real-UID-derived stored value (`f009c2a1…`) → the app task
hangs forever → no appfw, no button, no pairing. A real *unprovisioned* plug
passes this gate (its own flash UID matches its own factory cert) and proceeds to
pairing-ready. The real UID is a 128-bit hardware value not recoverable from the
dump (the config's `secureCertSha256` / `chip-factory.uniqueId*` do not match the
runtime target), so a legitimate pass needs the real plug's flash UID; otherwise
the gate must be satisfied as an emulator opt-in.

**Proven effect of clearing the gate** (force the `0x29c3c` memcmp result to
"equal", the real-HW outcome — `docs/s61-tools` / scratchpad `force_integrity.py`):
within ~3.5 s the firmware takes the verified path, app-start continues,
**`key_add` runs (the button registers itself on GPIO pins 0 and 2)**, the
provisioned-check `0x48cc4` runs, and **`ais_appfw_task` is created** — all
naturally, none force-called.

### What the simulation achieves (reproducible)

`docs/s61-tools/s61_button_sim.py` drives the firmware's genuine key stack using
the Thumb-bit force-call harness (see [Tools and methods](#tools-and-methods)):

1. force-run the block init `0x1bc18`;
2. force-run `key_add` (`0x41f64`) with a descriptor
   `bytes([0,0,PIN,PIN,PIN,PIN,0,0,0,0,0,0])` at scratch `0x403d80`;
3. "press" = drive the GPIO low and set the channel's capture bit
   (`state.pwmn_stat |= 1 << (2*GROUP + CH)`, with `GROUP, CH = PIN//2, PIN&1`).

Holding the level low needs the pin kept input-readable: `0x3eb30` decodes the
per-pin mode (`0x1a198`) from `gpio_cfg` bits 2/3, and the capture config reads the
non-input path, so the tool forces `gpio_cfg[PIN] = (… | 0x0c) & ~0x1`. A single
capture edge only debounces to 1, so the press re-triggers ~6 edges like a real
held button to pass the `> 2` threshold.

Result, entirely through the firmware's own code:

```
ISR(0x1bfd8) -> handler(0x4203c, r0=pin) -> press_path -> 5s-longpress-timer armed @0x42078 (timer_start r1=5)
```

i.e. the real ISR runs, dispatches to the real key handler with the correct pin
id, the handler runs its real debounce, and arms the real 5-second long-press
timer.

### The key-driver timer thread's wake: the `bk_timer` = Timer 5

The long-press timing is driven by the **key-driver timer thread** (entry
`0x3eba0`): it blocks on the RTOS wait `0x134618` → `0x151a8` (`svc #0`, waits on
its TCB signal `[tcb+0x54]`), and on each wake walks its software-timer list once
and runs the **periodic key-scan `0x421e0`**, which reads the button pin and
counts the hold. Its wake is `rt_thread_resume` (`0x15274`, Thumb veneer
`0x134818`) called by the giver `0x3ec30`, registered by the thread startup
`0x217ec` on the **`bk_timer` hardware-timer device** (`0x19ed0` device-control,
`bk_timer_initialize`). `bk_timer` is **Timer 5** (int-status bit 9 of
`timer3_5_ctl`; period `0x7d00` at `0x802a44`).

The emulator's `_pacing` auto-raises only **Timer 3** (`timer3_5_ctl` bit 7,
ICU bit 8) — that is the RTOS tick on the OpenBK/Tuya firmwares — so Timer 5's
interrupt was never raised, the giver never ran, and the thread was signalled 0
times (`wait`=1, `nodeproc`=0 forever). Raise Timer 5's status bit and the giver
fires, the thread wakes, the scan runs, and the hold is timed — proven with
`docs/s61-tools/s61_long_press.py` (`scan` runs hundreds of times, long-press
action `0x42250` fires). The script raises `timer3_5_ctl` bit 9 itself because a
blanket per-enabled-channel raise in `_pacing` (what the BK7252 path does)
**regresses 3 other BK7238 self-tests** that drive Timers 4/5 at their own rate;
a clean fork fix needs faithful per-timer-period modelling, not a slow-tick raise.

The long-press action `0x42250` posts through `0x3eb6c` → `0x12c180` →
`bk_gpio_output` — i.e. it drives the **relay/LED**, and the scan reads pin
`[0x404c25]`. This is the relay path, not the eWeLink pairing trigger.

### Where it currently stops: the appfw pairing event

The pairing state machine is **alive and cycling** once the gate is cleared:
`ais_appfw_task` (entry `0x319a8`) loops at `0x319c4`, waiting (~30 Hz) on
`rt_event_recv` (`0x134da8`, mask 7) on the event object `[0x4040e0]`. Pairing
runs an appfw state handler → config builder `0x74870` → method runner `0x748cc`
→ dispatcher `0x21eb4` (→ AirKiss `0x21e50` / `0x21d18` / `0x21d90`) or
`Soft_AP_start 0x137c80`. Posting a raw event to the appfw (`rt_event_send`
`0x134674` to `[0x4040e0]`) wakes it and re-runs `0x748cc`, but it does **not**
pair: no pairing method/params are configured, so the runner is a no-op.

The real trigger sets the method first: the `pairingMode` command handler
`0x492ae` (which needs a cloud/BLE **session** argument — `arg1+0x88/0x90` — so it
cannot simply be force-called), or the button long-press's app-layer path. On
this firmware the physical-button long-press (`0x42250`) drives the relay; the
eWeLink pairing long-press is a separate app path that still has to be traced to
where it sets the pairing method and posts `PAIR_EVT_START` (names at `0x49c18`).

### Driving the pairing methods: soft-AP and station mode come up

The method **dispatcher `0x21eb4(r0=config)`** reads `config[0]`: **0 = coexist**
(`0x21e50` → soft-AP `0x21a80` **and** the AirKiss sniffer via the RW task),
**1** (`0x21d18`, station — sets Wi-Fi mode via `0x20e00`), **2** (`0x21d90`). The
runner `0x74870` hard-codes `config[0]=1`; the coexist/AirKiss method comes from a
different runner / the RAM-resident `0x21eb4` caller `0x3f60c`.

Called in the appfw task's own context (hook loop `0x319c4` + the force-call
harness, so the RTOS/RW-task messaging runs in a real task), the dispatcher drives
the emulated radio into the right mode — verified with
`docs/s61-tools/s61_pair_drive.py`:

- **Method 0 (coexist → soft-AP).** `0x21a80` builds the soft-AP struct, the MAC
  leaves monitor mode (`RX filter … back to normal reception`) and **transmits
  beacons**: the fork's virtual peer reports `device acts as an access point (it
  sends beacons)` and takes the station role, finding the device's BSS. **The
  soft-AP is observably beaconing** (but beacon-only — no dhcpd, no listener; see
  [What the soft-AP actually does](#what-the-soft-ap-actually-does-beacon-only-no-dhcpd-no-listener)).
  The beaconed SSID is the firmware's own default (`JIEWU`, baked in the image at
  logical `0x137ed8`), cipher-protected — so the auto-peer, which only joins open
  networks, does not finish associating.
- **Method 1 (station).** The MAC enters station mode and probes/associates; the
  peer takes the AP role and registers the device as a station.

So the firmware's pairing-start code runs **and the modelled radio follows it into
AP / station mode.**

#### What the soft-AP actually does: beacon only (no dhcpd, no listener)

Does a phone join the soft-AP and POST credentials, as in eWeLink's classic AP
pairing? **No.** The soft-AP in the COEXIST path is a bare beaconing AP in the
emulator, and it carries no credential-provisioning server in any case.

The coexist handler `0x21e50` runs in two parts. The **first** part is the
soft-AP bring-up `0x21a80` (build the AP-config struct — `0x219e8`, `0x21920`,
ending `0x24248`); this completes and the MAC beacons. The **second** part — the
continuation at `0x21e80` that calls the IP-services/dhcpd setup (`0x21ea4` →
`0xfdf0c`; the netif-IP config `0xfe4ac` and the role-gated path whose failure
logs `warning: dhcpd no open`) — sits **behind two RivieraWaves kernel sends**,
`0xee350`/`0xee35c` (messages `0x64`/`0x65` via `0xee048` → `0xedfb4`). A
call-sequence trace (method 0) ends exactly there:

```
disp → coexist_21e50 → softap_21a80 (→219e8→21920→24248) → rw350 → rw35c → ⊥
```

`0xee35c` never returns into the handler: in the unprovisioned/emulated state
`0xedfb4` returns -1 (its destination KE task at `*0x417564` is not registered),
and `0xee35c` is a **blocking retry-with-delay** request (`0x13499c` between
tries), which the synchronous force-call harness cannot carry through (it yields
the CPU). So the IP/dhcpd continuation is unreachable here, and **a dynamic probe
confirms it**: driving method 0 and watching `0xfe4ac` (netif IP), `0xfdf70` /
`0xfdfaa` / the `dhcpd no open` site, and the two port-8081 server builders —
**none fire**; only the beacon path runs. Whether `0xfdf0c`'s role-gated path
would start a DHCP server for the AP on real hardware is therefore inferred
(plausible — the dhcpd code is there) but **unobserved**.

Independently, **there is no AP-mode provisioning listener in the firmware at
all.** The only TCP/WebSocket server is the port-8081 LAN-control endpoint
(`http://%s:8081`, `ws://%s:8081`, advertised over mDNS `_ewelink._tcp`) — the
same LAN-control server the S60 opens *once online* (see
[cloud-stub.md](cloud-stub.md)), gated on the station/online path, never on the
soft-AP path; there is no `/ap`-style cred-POST endpoint in the image. Credentials
reach this plug over **BLE** (`EwelinkBleServer`, eWeLink + Matter), feeding the
common sink (`recv ssid/password/timeout` → store `wifi-ssid`/`wifi-pass` →
`switch wifi`). So the COEXIST soft-AP advertises presence during pairing (and, on
real hardware, likely stands up an AP IP/dhcpd); it is **not** a self-contained
credential-provisioning AP. This is why seeding `wifi-ssid`/`wifi-pass` directly
is the right way to get the emulated plug onto a network — it lands in the same
end state without the BLE handoff the emulator has no radio for.

> **If we ever implement our own LAN client** (to drive a plug on port 8081 once
> it is online, rather than over the cloud WebSocket), look to **iHost**, which
> drives eWeLink devices locally. Its "eWeLink Smart Home" add-on would contain a
> working implementation of the encrypted LAN-control protocol and the device-key
> handling — a useful reference for the mDNS `_ewelink._tcp` discovery, the port-8081
> request format, and the AES scheme keyed on the device secret. Note this is a
> control channel only: the stock LAN endpoint carries no firmware-push route (no
> `/zeroconf/ota_*`), so OTA still goes through the cloud-dial redirect — the LAN
> client would be for exercising an online plug, not for conversion.

**AirKiss decode is not implemented in this build — `0x403b20` is a stub
callback.** `0x403b20` is the monitor-mode frame **callback pointer**: the RX
consumers `0xaaf40` and `0xb37a8` do `bl 0x2220c` (read `*0x403b20`) then
`blx r3` — they call it as a function, once per received monitor frame. The only
code that writes it is the register helper `0x221fc`
(`set_monitor_cb(cb){ if(cb) *0x403b20=cb; }`) and the clear `0x221bc`
(`*0x403b20=0`). `0x221fc` has exactly two callers, both in the AirKiss start
handler (`0x12c666` and `0x12c6a4`), and the only non-zero value either stores is
the literal **`0x12c483`** — which is `bx lr`, a no-op (verified: runtime bytes ==
flash bytes at `0x12c482`). So the registered AirKiss decoder is a stub: monitor
frames routed to it are discarded. Confirmed end to end — setting
`*0x403b20 = 0x12c483` and replaying 11 000+ frames in monitor mode calls the
callback for every frame (consumer `0xaaf40` → `0x12c482`) with zero faults and no
decode. eWeLink kept the AirKiss **scaffolding** (the start/stop guards, the
`monitor (ie.airkiss) is not finish yet` log at `0x137c90`, the register/clear
helpers) but compiled out the payload decoder — consistent with the move to
**soft-AP + BLE** provisioning. The coexist RW path (`0xee35c` → msg `0x65`) does
not change this: its handler `0x12c640` registers the same stub, and in emulation
the send fails anyway (`0xedfb4` returns -1 because the destination KE task at
`*0x417564` is not registered in the unprovisioned state). So there is no AirKiss
decode loop in this firmware to close; the working pairing route is soft-AP
(above) plus BLE.

Open items (no route ruled out):

- **Confirm BLE/`pairingMode` is the real credential-decode path.** With AirKiss
  decode stubbed, the remaining provisioning decoders are the soft-AP CoolKit HTTP
  handler and the BLE/`pairingMode` command (`0x492ae`); trace where each writes
  the received `wifi-ssid`/`wifi-pass`.
- **Complete a soft-AP association.** Attach the peer with the device's soft-AP
  SSID/passphrase (or an open build) so the station link finishes and the CoolKit
  AP HTTP provisioning exchange can be exercised.
- **Trace the native pairing-start.** Find the app code that sets the pairing
  method and posts `PAIR_EVT_START` to the live (cycling) appfw, so pairing starts
  from the firmware itself rather than a forced dispatcher call — the cleanest
  end-to-end entry (names at `0x49c18`; `pairingMode` handler `0x492ae` needs a
  cloud/BLE session arg).
- **Faithful BK7238 timer model.** Model each enabled Timer 3-5 channel's own
  period so Timer 5 (`bk_timer`) is raised at its real rate without the slow-tick
  over-fire that regresses the Zengge/other BK7238 self-tests; then the key-driver
  thread wakes natively and `s61_long_press.py` / `s61_pair_drive.py` need no
  manual Timer-5 raise.
- **Integrity gate, legitimately.** With the real plug's flash UID the gate passes
  for real (`spi_flash.py` already takes `uid=`; its "nothing needs the real value"
  comment is wrong for the S61), removing the need for the `0x29c3c` bypass.

> Note: the registered button is on **GPIO pins 0 and 2**, not pin 3 —
> `docs/s61-tools/s61_button_sim.py` defaults to pin 3 and predates both the
> integrity-gate finding and natural registration.

## Pairing and provisioning routes

Each route below is a way to get the device into pairing / onto Wi-Fi. They share
one dependency: the pairing/commissioning machinery runs only once pairing is
actively entered (the button, or a command from the app). The button
([above](#the-button-press-the-pairing-trigger)) is the on-device trigger; the
other routes are listed with their current stopping point.

### AirKiss (smartconfig) relay — decoder stubbed in this build

AirKiss would suit a bridge because the phone never associates with the plug — it
broadcasts, the plug sniffs. A phone's broadcast is captured off-air and injected
into the emulated MAC. But **this firmware's AirKiss decoder is a stub**, so there
is nothing on the device to consume the frames (see
[Driving the pairing methods](#driving-the-pairing-methods-soft-ap-and-station-mode-come-up)
for the full trace). The two sub-results are worth keeping separate:

- **Delivery is solved (100 %).** `sent == rx` on both a synthetic self-test and a
  real 7446-frame capture, with full descriptor recycle, once injection is gated
  on the RX-FIQ handler object (`*0x403c10`) existing — see
  [Monitor-mode RX injection](#monitor-mode-rx-injection).
- **There is no decoder to reach.** `0x403b20` is the monitor-frame callback the
  RX path calls per frame (`0xaaf40`/`0xb37a8`: read `*0x403b20`, `blx` it). The
  AirKiss start handler (`0x12c666`) registers it via `0x221fc`, but the only
  non-zero value ever stored is `0x12c483` = `bx lr` (a no-op; runtime bytes ==
  flash). Proven: setting `*0x403b20 = 0x12c483` and replaying 11 000+ monitor
  frames calls the stub once per frame, 0 faults, nothing decoded. The scaffolding
  (`0x223b0` start / `0x221bc` stop, the `monitor (ie.airkiss) is not finish yet`
  log at `0x137c90`) is all that remains; eWeLink compiled out the EZ decoder in
  favour of soft-AP + BLE. There is no decoder behind the callback to reach.

The delivery pipeline tooling still stands for any future firmware that *does*
decode: `docs/s61-tools/s61_airkiss_capture.sh` (AX200 monitor capture to pcap)
and `s61_airkiss_replay.py` (replay a pcap into the MAC; `--selftest` checks the
pipeline, `S61_RX_OBJ_PTR` gates injection on the handler object). Real captures
stay in the scratchpad — credentials are carried in the frame lengths.

### Soft-AP setup network

The firmware supports `Soft_AP_start` / `bk_wlan_ap_init` and CoolKit AP HTTP
provisioning. Soft-AP is **command-driven**: the device enters COEXIST
unconditionally (`bk_wlan_set_coexist_at_init`) and switches mode on a runtime
`pairingMode` command. Relevant addresses: status printer `0x130c00`
(`softap:ssid=%s,channel=%d`), `Soft_AP_start` symbol `0x137c80` referenced by
`0x21a80`, `"pairingMode by %d"` `0x49548`. The command is registered name→handler
via `0x27568` (`register("pairingMode", 0x492ae)`); the handler `0x492ae` fetches
its value via `get_value` `0x12200` and then dereferences a per-command **session**
(`arg1+0x88/0x90/…`). Reaching soft-AP needs a real `pairingMode` command (from the
cloud WebSocket or BLE) so the handler runs with a genuine session/params context;
the on-device equivalent is the button entering pairing. Once an AP is up, the
emulator's peer takes the station role (device-as-AP is modelled — the OpenBK7238
AP self-test passes), so HTTP provisioning would run over the emulated link.

### BLE commissioning

The device offers `ewelink_ble_pairing` and Matter BLE commissioning. Current
findings:

- The BLE **controller model answers host commands**: the boot gapm commands
  (`GAPM_RESET` op 1, `GAPM_SET_DEV_CONFIG` op 3) complete with `gapm_cmp_evt`. So
  BLE advertising could complete if it were ever issued.
- **No auto-advertising at boot** (0 `[BLE_ADV]` payloads staged). Advertising
  starts from the pairing trigger. The Matter CHIP `BLEManagerImpl` /
  commissioning window (`COMMISSION_OPEN_WINDOW : ECM/BCM`, ~`0x49exx`) and the
  eWeLink/Matter pairing starter (`ewelink_ble_pairing=%d,
  matter_ble_commission=%d`, ~`0x48abc`) live in the pairing state machine.
- **No HCI splice point for a real-phone bridge.** `hci_send_2_controller`
  (`0xc1da4`) fires once at boot for a vendor command (`r3=0x848`); the GAP flow
  goes through RivieraWaves **KE messaging in shared memory**, not a serialized
  HCI boundary. Host and controller are coupled by in-process kernel messages, and
  the radio is the proprietary RW link layer behind the `0x900000` block +
  exchange memory. Bridging a real dongle would mean intercepting KE messages and
  reimplementing the controller's host-facing side.

## Monitor-mode RX injection

To accept relayed AirKiss frames, the MAC delivers received frames while in
monitor mode even though the smartconfig path never drives the normal RX-enable
handshake (it sets the wide-open filter `0x7fffffde` but the scan state machine
stays out of `HW_ACTIVE`). This delivery is general and correct; note only that in
*this* firmware the frames reach a stub callback (the EZ decoder is compiled out —
[see above](#airkiss-smartconfig-relay--decoder-stubbed-in-this-build)), so the
capability matters for other BK7238 images rather than for decoding this one.

Implemented in the fork (`src/wifimac.py`) as an **opt-in** so it cannot affect
normal operation: a flag `monitor_rx_deliver` (default off; env `BK_MONITOR_RX=1`
or `mac.monitor_rx_deliver = True`). When off, behaviour is identical to upstream
(the OpenBK7231T Wi-Fi station regression tests pass 3/3 with it off). When on,
`inject()` and `_rx_flush()` deliver frames if the MAC is `HW_ACTIVE` **or**
(`monitor` and `monitor_rx_deliver`).

Delivery is reliable (100 %, `sent == rx`) given two conditions, both handled by
the replay tool:

- **Gate injection on the RX-FIQ handler object existing.** The firmware sets the
  monitor RX *filter* very early (~0.15 s) but allocates the handler object
  `*0x403c10` ~0.4 s later (allocator `0x17700`, store `0x16604`). Frames injected
  into that gap drive the RX FIQ against a NULL object and hang it in a bogus
  `memcpy` (`0x427a8`, length read as the unused vector-table word `0xdeadbeef`),
  so descriptors are never recycled. Gating on `*0x403c10 != 0`
  (`S61_RX_OBJ_PTR`) reproduces real-hardware ordering (a phone never broadcasts
  that early).
- **Inject from the emulator's worker thread** via a `tick` hook, not the main
  thread (a cross-thread `rx_queue` access otherwise races).

RX FIQ path (for reference): vector `0x1c` → FreeRTOS context-save stub `0x108c0`
→ C dispatcher `0x10950` → RAM-resident UMAC glue → ARM veneer `0x3f1520` →
`0x16618`, which does `if (rtos_is_in_interrupt()) { obj = *0x403c10;
process(obj); }` (`rtos_is_in_interrupt` = the RAM pair `0x3f0fb4`/`0x3f0f9c`),
processors `0x17a20` → `0x1744c`. MAC RX interrupts are FIQ (lines 16–22); the
firmware drives RX off `TIMER_RX_TRIGGER` (bit 17).

## The connect path (reference)

When the device does connect, the path is: `BekenWiFiDriver` connect `0x74bfc`
("stop monitor, then connect to ssid", gated `if arg3!=0`) → `0x74b90` → `0x223f0`
(`bk_wlan_sta_init` config + scan/connect start). The config is a
`network_InitTypeDef_st` (`wifi_mode@0`, `wifi_ssid@1`, `wifi_key@~33`, `dhcp@161`;
~158 bytes); `sta_init` is `0x21bf8`. Connect first stops monitor via `0x21dbc` →
`0x20d14` → `sub_ee3c9` → `sub_ee049` → `sub_edfb5`.

`sub_edfb5` is the **RivieraWaves RW-task message send**: it builds a message and
either processes it synchronously (if `in_rw_task()` — `sub_134bed(0x417564)` — and
a flag bit is clear) or posts it to the RW task and waits on a semaphore for the
result. Stop-monitor is also queued via `wpa_hostapd_queue_command` (`sub_db0f9`)
to the wpa_supplicant command queue (handle `*0x417474`, queue `0x421cf8`, cmd
116), processed by `wpas_thread`. These operations complete only when driven by
the firmware's real task context and MAC-event sequencing; a mid-run force-call
from another context does not complete the round-trip. `IsWiFiStationProvisioned`
is `0x7388e` (reads `wifi-ssid` via `0x5ad84` → `0x12f438`), reached from CHIP
`DriveStationState` `0x73b28`.

## The fork (`../tools/beken/emulator/`)

The emulator is upstream BekenEmulator at the commit `UPSTREAM` pins - the
archive on the release `emulator-source-beken-<commit>`, whose hash `UPSTREAM` also pins -
with these patches applied in name order when the image is built
(`bk-emu.sh ensure` builds or pulls it; `bk-emu.sh dump` fetches the public
S61s dump from upstream's repository; `bk-emu.sh test` runs upstream's
self-tests under them, fetching its other devices' dumps the same way). Each is one commit's worth, with its reasoning in
its header; none has been offered upstream yet.

- **`0001` - monitor-mode RX delivery** (`src/wifimac.py`): sniffed frames
  reach the RX descriptors in monitor mode even when the scan state machine is
  not active, as the AirKiss path needs. Opt-in (`monitor_rx_deliver`), so
  nothing else changes.
- **`0002` - the SPI flash's Unique ID** (`src/spi_flash.py`, new, wired in
  `src/emulator.py`): the boot fix above. Without it the SDK's UID read spins
  and the watchdog reboots the chip.
- **`0003` - OTA self-reprogramming** (`src/emulator.py`): the flash JEDEC id
  and XIP write-through the stock bootloader needs to apply an update to its
  own flash (`flash_writethrough`, off by default), plus flash-write speedups.
- **`0004` - the 26 MHz timer group** (`src/emulator.py`, `tests/self_test.py`):
  Timer 0-2 fire at each channel's programmed period, which LibreTiny's
  FreeRTOS tick needs; OpenBeken's watchdog feed and the stock dump are
  unchanged.

To change the fork: extract the archive (`captures/emulation/cache/`), apply
the patches to it in order, make the change as a commit there, `git
format-patch` it into `patches/`, and `bk-emu.sh ensure` builds the image
again from the new set (its hash, `bk-emu.sh tag`, changed; the workflow
publishes that version once it is pushed).
Moving to a newer upstream commit: put its hash in `UPSTREAM`, `bk-emu.sh
source` makes the new archive (`git archive` of that commit, so anyone can
reproduce its bytes), publish it as the release `emulator-source-beken-<its
commit>` (draft first, with the archive attached, then publish: releases are
immutable), pin its SHA-256 and name in `UPSTREAM`, and rework whatever
patches no longer apply.

## Tools and methods

- **Emulator run:** `src/main.py <dump> --with-boot -chip BK7238 --only-uart
  --wifi-peer auto` (peer roles: `auto`, `ap --wifi-ssid <x> --wifi-pass <y>`;
  `--net-out` for the real cloud).
- **FLASHDBG** (`FLASHDBG=1`, built into `emulator.py`): logs config-region flash
  ops (read opcode `op=5`); located the live config reads at `0x3e0000`/`0x3f6000`.
- **RAM dump + search** (`emu.mu.mem_read(0x400000, 1<<20)`): found the decoded
  apikey UUID and the hardcoded URL template `http://%s-apid.coolkit.%s/...`.
- **Watchpoints** (`UC_HOOK_MEM_WRITE` / `UC_HOOK_MEM_READ`, scoped to a target
  buffer): located the apikey formatter `0x427b8` and memset `0x45ec`; general
  technique for finding the PC and registers that produce/consume a buffer.
- **Thumb-bit force-call harness:** run a chosen function mid-boot. Save registers,
  set args, set `LR = 0x5` (trap at `0x4`) and PC odd (`addr | 1` for Thumb), bump
  `emu._exc_epoch` so the chunked run loop re-reads PC + the Thumb bit; a hook at
  `0x4` restores registers on return. Set CPSR to `(cpsr & ~0x3f) | 0x1f | 0x20`.
  This reaches firmware code correctly, but cannot satisfy RTOS/RW-task context
  that only exists inside the owning task/thread (force-calls into the RW-task
  message path or the key-driver timer thread park or fault for this reason).
- **Static analysis on the logical image.** `s61_unstripe.py` removes the CRC
  stripes to produce `logical.bin`, where CPU address == file offset, so static
  addresses match runtime. Then: `s61_dis.py` (capstone disassembly),
  `s61_xref.py` (BL/BLX callers + data-word refs for an address), `s61_strxref.py`
  (find code by the log/format string it uses), and `s61_decomp.py` (angr
  pseudo-C decompiler — no Java/Ghidra needed).
- **s61-tools** (`docs/s61-tools/`): the static-analysis tools above, plus the
  provisioning drivers — `s61_seed_join.py` (seed creds → Wi-Fi join),
  `s61_ble_provision.py` (drive the eWeLink BLE messages through the dispatcher →
  Wi-Fi join), and `s61_cloud_dial.py` (seed + synthesise the pairing trigger →
  **eWeLink cloud dial**) — plus `s61_button_sim.py` (button press),
  `s61_long_press.py`, `s61_pair_drive.py`, and `s61_airkiss_capture.sh` +
  `s61_airkiss_replay.py` (AirKiss relay). Emulator tools take paths via env
  `BK_EMU_ROOT` / `S61_DUMP`; see the folder's README.
