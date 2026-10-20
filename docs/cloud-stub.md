# Local cloud stub for parallel emulator runs

Give each emulated plug its **own** locally-served eWeLink "cloud", so emulator
conversions run fully in parallel instead of taking turns on the one shared real
cloud. Everything here runs inside the emulator's isolated per-instance network;
nothing touches the real eWeLink cloud, the real plugs, or the read-only stock
dump.

This one document holds the whole cloud-stub story: the goal, the design, the
firmware facts it rests on, what was built, and the verification log. Contents:

- [Status](#status)
- [The goal](#the-goal)
- [What the cloud is actually used for](#what-the-cloud-is-actually-used-for)
- [The approach](#the-approach) · [What we are explicitly NOT doing](#what-we-are-explicitly-not-doing)
- [Requirements, and where each came from](#requirements-and-where-each-came-from)
- [Firmware facts (static analysis)](#firmware-facts-static-analysis)
- [The private domain](#the-private-domain)
- [Firmware and config changes](#firmware-and-config-changes)
- [The stub server](#the-stub-server) · [Every exchange, with examples](#every-exchange-with-examples)
- [Harness wiring and how to run it](#harness-wiring-and-how-to-run-it)
- [Verification log](#verification-log)
- [Risks and limitations](#risks-and-limitations) · [Safety / rollback](#safety--rollback)
- [Files](#files) · [Ground rules](#ground-rules)

## Status

**Built and tested through the eWeLink-dependent part of a conversion, and
proven to run in parallel** (2026-10-19). Branch `cloud-stub` (rebased onto
`esphome`; a fast-forward of `esphome` takes the whole feature). All of the
following is done:

- The plug honours `frame_params.dis_server` — redirect is a pure NVS edit
  ([test 1](#test-1--does-the-firmware-honour-dis_server)).
- Trust swap: the plug trusts our CA and completes the TLS handshake to the stub
  ([test 2](#test-2--does-the-trust-swap-let-the-tls-handshake-complete)).
- The stub brings the plug online and it opens its LAN interface
  ([test 3](#test-3--does-the-stub-bring-the-plug-online-and-enable-lan-control)).
- A conversion against the stub completes stock → recovery bridge (step C2) with
  nothing reaching coolkit
  ([test 4](#test-4--end-to-end---stub-cloud-conversion-through-c2)).
- Two clones of the same device id are online at once, each on its own stub — the
  collision the real cloud forbids
  ([test 5](#test-5--parallel-runs)).
- Whole conversions to Tasmota and to ESPHome pass alike against the stub and the
  real cloud, reports included
  ([test 6](#test-6--full-conversions-stub-and-cloud)).
- The account record carries what eWeLink's does for the tool, the plug's
  reported params and its MAC included
  ([test 7](#test-7--the-whole-account-record)).

**The fault suite, in part:** 20 of its 1,453 scenarios have run against the
stub, every direction among them and 10 of the cloud scenarios (faults at C2,
C3, C3fb and D1, three of them sending the installer again through the stub),
and all passed ([test 8](#test-8--twenty-fault-suite-scenarios)). The whole
suite has not run against the stub.

**Out of scope:** eWeLink is needed at C2, and again at C3 or D1 only to send the
installer again, when the plug went back to Sonoff's firmware (switched off and
on before the installer had done its work). Once D1 is done (the plug on the
interim Tasmota), the rest runs over LAN and touches no eWeLink, the same under
`--stub-cloud` as under `--cloud`. The real cloud remains the fidelity reference;
keep a few `--cloud` smoke runs.

## The goal

Run many emulator conversions in parallel. The per-instance networks already
parallelise (`--instance N`, `autoflash-emu-N`, `state-N`, port `8080+N`). The one
thing they share is the **real eWeLink cloud**, which keeps **one connection per
device id**. Every emulated plug is a clone with the same device id, so only one
can be "online" at a time — the eWeLink-dependent part of each run has to be
serialised. The stub removes that shared dependency by giving each instance its
own cloud, served locally, so nothing is shared and runs go fully parallel.

## What the cloud is actually used for

Tracing the conversion ([`tools/autoflash_cli.py`](../tools/autoflash_cli.py)
`step_discover` / `step_bridge`) shows the cloud's role is smaller than it looks.
It is needed for two things:

- **Touchpoint A — the tool → eWeLink account API** (`*-apia.coolkit.cc`, via
  [`get_device_key.py`](../tools/get_device_key.py) and
  [`query_ota.py`](../tools/query_ota.py)). The tool logs into the account to
  (1) fetch the plug's **LAN-control key** — eWeLink LAN commands are
  AES-encrypted with a per-device key the cloud normally hands out — and (2) read
  the plug's model and firmware version and confirm it is online.

- **Touchpoint B — the plug → dispatch/IoT servers** (`dis_server`, over TLS).
  The stock firmware connects to its dispatch host, is handed an IoT server
  address, opens a WebSocket to it and stays connected. The plug only accepts
  LAN control while it believes it is online, so this connection is what makes
  touchpoint A's key usable.

Both collide on clones (one cloud connection / one account record per device id).
The stub replaces **both**, per instance, locally.

A nuance confirmed in [test 4](#test-4--end-to-end---stub-cloud-conversion-through-c2):
the bridge image is **not** delivered through the cloud. The conversion's
`serve_tasmota_ota.py` connects to the IoT WebSocket as the *app* and sends an
`upgrade` command carrying a download URL on **this computer**; the cloud relays
it to the device, which then fetches the image over plain **LAN HTTP**. So the
OTA bytes never go through the cloud (no cloud OTA, no router redirect) — the
cloud only carries the small `upgrade` message that triggers the LAN fetch.

## The approach

Point each emulated plug at a dispatch server **on a private domain we control
inside the emulator**, served by a small local stub with a **self-signed cert**
the plug is made to trust, so the plug comes online against our own server and
the tool reads the plug's already-known key from the stub — no coolkit hostname,
no coolkit cert, no shared cloud.

### What we are explicitly NOT doing

This is self-hosting — redirecting our own device to our own server — not
impersonating eWeLink:

- We never resolve `coolkit.cc` / `coolkit.cn` to our server. Those names appear
  nowhere in our DNS, our certs, or our config.
- We never use or forge coolkit's certificate or CA private key.
- We never extract or need the plug's embedded client key.
- The real eWeLink cloud and the real plugs are untouched; the stub only ever
  runs on the emulator's firewalled per-instance network.

The device genuinely talks to a domain we own, presenting a cert we legitimately
issued for that domain. The only firmware change on the trust side is making our
own firmware copy trust our own CA — the same thing any self-hosted-IoT setup
does.

## Requirements, and where each came from

What the stub and its harness have to do, each with the finding or the code that
makes it necessary. The evidence is in [the firmware facts](#firmware-facts-static-analysis),
[every exchange, with examples](#every-exchange-with-examples),
[the verification log](#verification-log) and the tool's own code.

**The plug (touchpoint B)**

- **Send it to our domain by NVS, not a firmware patch.** It dials the host
  stored in `frame_params.dis_server`/`dis_port`, not the one its
  `%s-disp.coolkit.cc` constructor builds
  ([test 1](#test-1--does-the-firmware-honour-dis_server)).
- **Make it trust our CA, in the same slots.** It pins one coolkit root CA, twice,
  in fixed 1040-byte slots with unrelated data straight after, so the replacement
  must fit: an EC P-256 CA (a 644-byte PEM) does, RSA-2048 would not. It offers
  ECDHE-ECDSA suites, so an EC CA costs nothing
  ([test 2](#test-2--does-the-trust-swap-let-the-tls-handshake-complete)).
- **Certificates that do not depend on its clock.** It asks for no time before
  dispatch (no NTP; test 1), so both certificates are long-dated, with a
  `notBefore` in the past.
- **Ask it for no client certificate.** The stub never needs or reads the plug's
  own key; its TLS context requests none.
- **Hand back the IoT server as a numeric IP.** It `inet_aton`s the dispatch
  reply's `IP` and does no DNS for it: a hostname made it dial
  `255.255.255.255` ([test 3](#test-3--does-the-stub-bring-the-plug-online-and-enable-lan-control)).
  So the leaf carries `IP:192.168.60.1` as well as the names.
- **Answer its WebSocket the way it waits for.** `GET /api/ws` (RFC 6455, pings
  answered). The `register` ack must echo its `d_seq` and carry
  `config {"hb":1,"hbInterval":145}`, or it drops the connection after ~10 s and
  dispatches again; `date` gets the date, `update` and `query` a bare `error:0`
  (test 3).

**The tool (touchpoint A, and the app side)**

- **Point it at the stub with no new code paths.** `AUTOFLASH_EWELINK_API_BASE`
  replaces every region's account-API base (`get_device_key.py`) and the
  hard-coded app dispatch (`probe_ota_command.py`). Trust comes from
  `SSL_CERT_FILE`, which the tool's `ssl.create_default_context()` already
  honours: a bundle of the system roots and our CA, so the firmware downloads
  still verify ([test 4](#test-4--end-to-end---stub-cloud-conversion-through-c2)).
- **An account record the tool takes for an S60.** The device list's
  `extra.model` must be `SN-ESP32C3-S60-01`: the tool knows an S60 by it (or by
  an `S60TP*` product model) and stops with "not a Sonoff S60" otherwise
  (`account_plug`, `step_discover`). It needs the `devicekey`
  (`get_device_key.py` fails without one), which must be the plug's LAN key:
  at A2 the tool opens the plug's encrypted eWeLink advert with it, for its
  Wi-Fi signal ([test 9](#test-9--the-signal-from-the-start)). It needs
  `params.fwVersion` (as does `query_ota.py`), and the OTA query must answer
  with a `data.otaInfoList`, even an empty one.
- **`online` as eWeLink means it.** True only while the plug's WebSocket is held:
  the tool waits on it before sending the installer (`serve_tasmota_ota.py`), and
  by it tells a plug cut off from the internet, one that answers here but shows
  offline (`why_not_found`).
- **The rest of the record, so the failure paths read the same.** `productModel`
  names the region in the report and the Tasmota template; it is not in the dump
  ([the stub server](#the-stub-server)). `params.staMac` lets the tool ask the
  plug's last-seen address first, and say "is at … but does not answer" when
  only its MAC is seen. `params.ssid` names its network in "not seen on this
  computer's network". The plug reports `ssid` and `bssid` in its `update`s,
  which the stub passes on as eWeLink does; it never reports its MAC here, so
  that is the MAC of the plug being run
  ([test 7](#test-7--the-whole-account-record)).
- **Relay the app's commands.** The installer goes as an `upgrade` from the tool's
  app connection (`userAgent:"app"`) to the plug, and the plug's replies come
  back; without the relay the command never reached the plug (test 4c). A
  watching app is told `sysmsg {"online": false}` when the plug drops (its
  install reboot), as at eWeLink.

**The harness**

- **One cloud per instance, nothing shared.** The stub listens on the instance's
  own router address (`192.168.60.1:443`, in its own network namespace), so
  clones of one device id are online at once
  ([test 5](#test-5--parallel-runs)), and the fault suite's cross-instance cloud
  lock goes with `--stub-cloud`.
- **Never stand in for eWeLink's own names.** Our own `.test` domain and
  certificate only; in stub mode eWeLink's hostnames resolve to `0.0.0.0`
  ([what we are not doing](#what-we-are-explicitly-not-doing)).
- **The key stays on the instance's network.** The identity is read from the dump
  inside the container and written to the container's `/run`, and secret values
  are redacted in the stub's log. It runs as root, so with `-B`: no bytecode left
  in your state directory that only root could delete.
- **One stalled client cannot stop the rest.** The TLS handshake is made in each
  connection's own thread, with a 30 s timeout, not while accepting.
- **Instances started together make one CA.** Each would otherwise have made its
  own, and only one is flashed in: `make_ca.py` takes a lock, writes atomically,
  keeps the CA once made, and reissues only a leaf that no longer fits.
- **A converted plug is never silently reflashed.** The flash's mode is recorded
  (`state-N/.flash-mode`); switching an instance between stub and cloud is
  refused without `--fresh`, and the mode moves with `--save`/`--from` and a
  plug swap ([harness wiring](#harness-wiring-and-how-to-run-it)).
- **Leave the shared image alone.** `AUTOFLASH_EMU_IMAGE` builds and runs a separate
  image (here `autoflash-emu-stub`), so a worktree's build does not replace the
  `autoflash-emu` that other work runs.

## Firmware facts (static analysis)

Reference findings from the stock Sonoff S60 1.2.0 flash dump, so they need not
be re-derived. **Facts only — no secrets:** the dump holds the plug's eWeLink
keys and Wi-Fi password in cleartext; those values are never reproduced here.
Where a per-device value matters, the field is named and left in the dump.

Source dump (read-only): `captures/plugs/<mac>/sonoff-backup/flash.bin`
(ESP32-C3, 4 MB). The app under analysis is `ota_0`, the stock app the plug boots.

### Flash layout

From `report.json` beside the dump:

| partition | offset     | size       | notes                                   |
|-----------|------------|------------|-----------------------------------------|
| nvs       | `0x009000` | `0x010000` | settings, Wi-Fi, eWeLink identity       |
| reserve   | `0x019000` | `0x004000` |                                         |
| otadata   | `0x01d000` | `0x002000` | selects `ota_0`                         |
| phy_init  | `0x01f000` | `0x001000` |                                         |
| ota_0     | `0x020000` | `0x1f0000` | stock app `esp32_fw`, v1.0.0-21-g28df76c, Feb 12 2025, IDF v4.4.2 |
| ota_1     | `0x210000` | `0x1f0000` | holds the conversion's `s60_ota_bridge` installer after a run |

### Dispatch / cloud connection (device side)

The plug maintains its eWeLink connection by contacting a **dispatch** host over
TLS, which hands back the IoT server to use. Strings in `ota_0`:

- `%s-disp.coolkit.cc` — region-prefixed dispatch host *constructor*.
- `disp.coolkit.cn`, `cn-disp.coolkit.cn`, `cn-gsm.disp.coolkit.cn`,
  `test-gsm.disp.coolkit.cn` — other dispatch hosts.
- `dispatchUrl`, `dispatchPort`, `resetDispatch`, `reassemble_and_dispatch` —
  other dispatch names, not exercised: the reply the plug acts on carries `IP`
  and `port` (test 3).
- `POST /dispatch/device HTTP/1.1` — the dispatch request. Its body template is
  `{"accept":"ws;%u","version":%u,"ts":%u,"deviceid":"%s","apikey":"%s","model":"%s","romVersion":"%s","fwVersion":"%s"}`.
- `GET /api/ws HTTP/1.1`, `HTTP/1.1 101 Switching Protocols`, `hbInterval` — the
  IoT channel is a WebSocket at `/api/ws` with a heartbeat.

**The plug reads its dispatch host from NVS `frame_params.dis_server`, not the
constructor** ([test 1](#test-1--does-the-firmware-honour-dis_server)): a clean
offline boot queried the stored `eu-dispd.coolkit.cc` (note `dispd`), never the
constructed `eu-disp.coolkit.cc` (`disp`). It retries on a backoff and issues no
NTP query first. **The IoT address the dispatch reply hands back is used as a
numeric IP without DNS** (the plug `inet_aton`s the `IP` field;
[test 3](#test-3--does-the-stub-bring-the-plug-online-and-enable-lan-control)).

### TLS trust (device side)

`ota_0` pins a single **self-signed coolkit root CA**, embedded twice,
byte-identical:

- Subject = Issuer: `C=cb, ST=gd, L=sz, O=coolkit, OU=dev, CN=coolkit.cn`
- Validity `2017-07-11`…`2117-06-17`; key **RSA-1024, SHA-1**.
- PEM SHA-256 starts `b81c05a5…`.

| copy | flash offset | `ota_0`-relative | slot                                     |
|------|--------------|------------------|------------------------------------------|
| 1    | `0x38d4c`    | `0x18d4c`        | 1040 B: 1036-B PEM + `\r\n\x00\x00`      |
| 2    | `0x39ed8`    | `0x19ed8`        | 1040 B: 1036-B PEM + `\r\n\x00\x00`      |

Each slot is a **1040-byte** null-terminated PEM C-string; the byte right after
it is unrelated data (e.g. `GET %s?deviceid=…`), so the replacement must keep the
slot length. A replacement CA PEM must therefore be < 1040 B, null-padded to the
slot. An **EC P-256** CA PEM (~644 B) fits; RSA-2048 (~1200 B) would not. mbedTLS
stops at the `END CERTIFICATE` marker, so trailing nulls are ignored. A straight
MITM of the unmodified firmware is impossible without coolkit's CA private key;
the stub instead replaces this trust anchor in a working copy with our own CA.

### eWeLink identity in NVS

`frame_params` (JSON blob in NVS namespace `frame_params`, key `content`) holds
the device identity and network config. Non-secret fields: `uiid` = `190`
(eWeLink's device type for the S60), `model` = `SN-ESP32C3-S60-01`, `dis_server`
= `eu-dispd.coolkit.cc`, `dis_port` = `443`, `is_encrypt`, `factory_uart_flag`,
`sledOnline`. Secret fields present (values **not** reproduced — read from the
dump if needed): `deviceid`, `apikey`, `encrypt_key`, `sha256`, `ssid`, `passwd`.
Other namespaces: `nvs.net80211` (ESP-IDF Wi-Fi sta.ssid/sta.pswd, which the
emulator already rewrites); `data_backups` / `user_usual` / `user_unusual` (a
second copy of `deviceid`/`apikey`, plus `finalUserApikey` / `flashCipher`,
energy and relay settings); `phy` (`cal_data`, `cal_mac`, `cal_version`),
`productId` (an opaque 14-digit number, not the product model), `histEnergy0..8`,
`bt_config.conf` and `evtLogName`. eWeLink's product model (`S60TPG`/`S60TPF`)
appears nowhere in the flash. In this plug's dump `apikey` and `encrypt_key`
hold the same value, and either opens its LAN advert (below), so which of the
two is the LAN key does not arise for it.

### What the plug says of itself on the network

Its eWeLink LAN advert - mDNS `_ewelink._tcp`, whose TXT record carries
`txtvers`, `id`, `type`, `apivers`, `seq`, `encrypt=true`, `iv` and `data1`,
`data2` - holds its state, encrypted: AES-128-CBC, keyed with the MD5 of the
LAN key, the IV in `iv`, the ciphertext in base64 split over `data1` to
`data4`. Opened, it is `configure`, `current`, `fwVersion`, `power`, `pulses`,
`rssi`, `sledOnline`, `staMac`, `switches`, `triggerType` and `voltage`: the
plug's Wi-Fi signal among them, read before anything is written. The same
`rssi` key sits with `fwVersion`, `staMac` and the meter readings among the
app's strings, for its full state report; that report never came in a stub
session, each of which lasted less than a minute before the installer went.
The app also has a serial factory-test command, `AT+WIFI_RSSI?`, that the
network cannot reach.

The messages the plug and the tool exchange with the cloud, each with an example,
are under [the stub server](#every-exchange-with-examples).

### Emulator boot patches (context)

[`patch_stock.py`](../tools/emulator/patch_stock.py) already edits `ota_0` only to
make it boot under QEMU (which emulates no Bluetooth): two routines
(`0x4200A66A`, `0x4201BB60`) are patched to `return 0` so BT init is skipped.
These do not touch dispatch or TLS, so dispatch/trust behaviour in the emulator
matches real hardware. The cloud-stub trust-anchor swap is an additional `ota_0`
edit applied the same way (bytes checked, then `appimage.reseal`).

## The private domain

Names resolved only by the emulator's `dnsmasq`, never registered publicly
(`.test` is reserved by RFC 6761, so it can never clash with a real domain):

- dispatch host: `disp.autoflash-stub.test`
- IoT host (handed back by dispatch): `iot.autoflash-stub.test`
- account API host: `apia.autoflash-stub.test`

All resolve, via `/etc/hosts` entries the stub mode adds (dnsmasq reads
`/etc/hosts`), to the router address on the instance's network, `192.168.60.1`,
where the stub listens. They only have to (a) not be coolkit's and (b) match the
cert SAN and `dis_server`.

## Firmware and config changes

All changes are applied to the emulator's **working copy** of the flash
(`captures/emulation/state-N/…`), built from the read-only stock dump. The dump
itself is never modified.

### Self-signed CA + server cert

[`tools/emulator/stub_cloud/make_ca.py`](../tools/emulator/stub_cloud/make_ca.py)
generates, into `captures/emulation/stub-cloud/` (git-ignored, private):

- a **root CA**: EC P-256 (for the slot-size reason above), self-signed,
  `notAfter` ~2125 so it never needs renewing;
- a **server leaf** signed by that CA, SAN
  `DNS:disp.autoflash-stub.test, DNS:iot.autoflash-stub.test, DNS:*.autoflash-stub.test,
  IP:192.168.60.1` (the IP SAN is for the plug's and tool's bare-IP connection to
  the IoT/app host), also long-dated with a past `notBefore` so an unset device
  clock does not fail validity.

The CA is **kept stable once made** — it is flashed into the trust store, so
`make_ca.py` reuses it and only reissues the leaf (e.g. to add a SAN); `--force`
regenerates the CA. The CA private key never enters the firmware.

### Firmware change A — trust our CA (trust-store swap in `ota_0`)

[`patch_stock.py --trust-ca <ca.crt>`](../tools/emulator/patch_stock.py) checks
both slots currently hold the coolkit CA (`O=coolkit, CN=coolkit.cn`), overwrites
each with our CA PEM null-padded to the original 1040-byte slot, then calls
`appimage.reseal` so the image checksum and SHA-256 are valid and the bootloader
accepts it. Applied only in stub mode, alongside the existing BLE patches. The
plain-boot path (no `--trust-ca`) is unchanged.

### Firmware change B — point the plug at our domain (NVS `dis_server`)

[`rewrite_nvs.py --dis-server disp.autoflash-stub.test --dis-port 443`](../tools/emulator/rewrite_nvs.py)
sets those `frame_params` fields the same way it already rewrites
`ssid`/`passwd`: the partition is decoded and regenerated with
`nvs_partition_gen`, so the value length is not constrained. A pure NVS edit; no
firmware string-patch is needed (test 1).

## The stub server

[`tools/emulator/stub_cloud/stub_server.py`](../tools/emulator/stub_cloud/stub_server.py)
is one stdlib-only HTTPS listener on `192.168.60.1:443` (the leaf cert), routed by
request. Device identity for the account API is read from the booted flash dump by
[`device_identity.py`](../tools/emulator/stub_cloud/device_identity.py), so the
LAN key never leaves the instance network. Every device/app message is logged with
secret values (`apikey`, `deviceid`, `devicekey`, `encrypt_key`, …) redacted.

- **`POST /dispatch/device`** (device, touchpoint B) → `{"error":0,"IP":"192.168.60.1","port":443,…}`.
  The `IP` must be a numeric address, not a hostname (the plug `inet_aton`s it).
- **`GET /api/ws`** (device, touchpoint B) → a minimal RFC 6455 server. The plug
  `register`s (the ack must echo its `d_seq` and include a heartbeat `config`),
  then `date` / `update` / `query`; the stub answers `error:0` and the plug
  reports online and enables LAN control.
- **`GET /dispatch/app`** (tool/app) → the IoT server for the app side.
- **`POST /v2/user/login`, `GET /v2/device/thing`, `POST /v2/device/ota/query`**
  (tool, touchpoint A) → a canned login token, a one-device list, and an empty
  OTA list (firmware comes over LAN, not from here). The device list carries the
  plug's `devicekey`, `online` (true while its IoT WebSocket is held, as at
  eWeLink), its model, firmware version and other params (`ssid`, `bssid`) as
  the plug itself reports them, its MAC (`params.staMac`: the plug never reports
  it here, so it is the one `emu.sh` runs the plug with), and its **product
  model** when known (below).
- **app↔device relay** — the real cloud passes an app's `upgrade` command to the
  device and its replies back. Device and app each hold their own WebSocket to the
  stub, so the stub routes between them by `deviceid`, and tells a watching app
  when the device drops (its install reboot). This is what lets
  `serve_tasmota_ota.py` drive the online plug.

**The product model** (`S60TPG` UK, `S60TPF` Europe) is eWeLink's name for the
plug, given when it was paired. It is not in the dump — no `S60TP*` string
anywhere in the flash — so the stub cannot read it from the plug. Instead
`emu.sh` looks up the one eWeLink gave this plug in the tool's own records of
earlier runs (`plugs/<mac>/runs/*/autoflash-state.json`: a real conversion's, a
kept plug's or an instance's), and `AUTOFLASH_STUB_PRODUCT_MODEL=S60TPG|S60TPF`
overrides it or covers a plug never seen by the real cloud. With neither, none is
served and the run's report leaves "Plug model" and "Plug type / region" blank
(and the Tasmota template is named plainly "Sonoff S60") rather than guess.

### Every exchange, with examples

The message shapes were first taken from the firmware's own strings (the dispatch
request template, `GET /api/ws`, `hbInterval`) and the repository's eWeLink
client (`serve_tasmota_ota.py`, `websocket_minimal.py`), then confirmed live: the
stub logs every message, secret values redacted, so each run is also a capture.
The examples are one stub run's (2026-10-19), from its log, in the order they
came. Values that identify the plug or the account are shown as `<…>`: the device
id is 10 hex digits starting `10`, the apikey and the LAN key are UUIDs, and the
chip id is 12 hex digits. `192.168.60.1` is the emulator's router, where this
computer and the stub both are; the plug is `192.168.60.50`.

**The plug and the stub**, each time the plug starts:

| message | the plug sends | the stub answers |
|---------|----------------|------------------|
| dispatch, `POST /dispatch/device` | `{"accept": "ws;2", "version": 3, "ts": 34738, "deviceid": "<device id>", "apikey": "<apikey>", "model": "SN-ESP32C3-S60-01", "romVersion": "1.2.0", "fwVersion": "1.2.0"}` | `{"error": 0, "reason": "", "IP": "192.168.60.1", "port": 443}` |
| `register`, first on `GET /api/ws` | `{"userAgent": "device", "version": 8, "romVersion": "1.2.0", "action": "register", "chipid": "<chip id>", "ts": "27433", "model": "SN-ESP32C3-S60-01", "apikey": "<apikey>", "deviceid": "<device id>", "d_seq": 60376}` | `{"error": 0, "deviceid": "<device id>", "apikey": "<apikey>", "d_seq": 60376, "config": {"hb": 1, "hbInterval": 145}}` |
| `date` | `{"userAgent": "device", "action": "date", "apikey": "<apikey>", "deviceid": "<device id>", "d_seq": 20710}` | `{"error": 0, "deviceid": "<device id>", "apikey": "<apikey>", "d_seq": 20710, "date": "2026-10-19T20:51:17.706Z"}` |
| `update`, its state | `{"userAgent": "device", "action": "update", "apikey": "<apikey>", "deviceid": "<device id>", "d_seq": 94076816, "seq": 2, "init": 1, "params": {"only_device": {"ota": "success", "ota_fail_reason": 0}, "fwVersion": "1.2.0"}}` | `{"error": 0, "deviceid": "<device id>", "apikey": "<apikey>", "d_seq": 94076816}` |
| an answer to the last command it had | `{"error": 0, "sequence": "1791445746113", "apikey": "<apikey>", "deviceid": "<device id>"}`, the same at every start: that `sequence` is the `upgrade` that installed the readout firmware the dump was read through (08:49 on 8 October; the dump was written at 08:50) | nothing |
| `update`, its network | `{"userAgent": "device", "action": "update", "apikey": "<apikey>", "deviceid": "<device id>", "d_seq": 5805, "seq": 2, "params": {"ssid": "<network name>", "bssid": "<access point MAC>"}}` | `{"error": 0, "deviceid": "<device id>", "apikey": "<apikey>", "d_seq": 5805}` |
| `query` | `{"userAgent": "device", "action": "query", "apikey": "<apikey>", "deviceid": "<device id>", "d_seq": 65519, "params": ["timers", "TZ", "sceneMaxPow", "sceneMinPow", "uploadDayKwh", "uploadMonthKwh"]}` | `{"error": 0, "deviceid": "<device id>", "apikey": "<apikey>", "d_seq": 65519}` |

The plug never reports its MAC (`staMac`) on this channel.

**The tool and the stub**, from A2 to C2:

| message | the tool sends | the stub answers |
|---------|----------------|------------------|
| sign-in, `POST /v2/user/login` | `{"password": "stub", "countryCode": "+44", "email": "stub@autoflash-stub.test"}` (the placeholder account), signed: `Authorization: Sign <HMAC-SHA256 of the body>`, `X-CK-Appid: <app id>`. The stub checks none of it. | `{"error": 0, "data": {"at": "stub-access-token", "rt": "stub-refresh-token", "region": "eu", "user": {"apikey": "<apikey>"}}}` |
| device list, `GET /v2/device/thing?num=0` | `Authorization: Bearer stub-access-token` | `{"error": 0, "data": {"thingList": [{"itemData": {"deviceid": "<device id>", "devicekey": "<LAN key>", "apikey": "<apikey>", "name": "S60 (stub)", "online": true, "extra": {"uiid": 190, "model": "SN-ESP32C3-S60-01"}, "params": {"only_device": {"ota": "success", "ota_fail_reason": 0}, "fwVersion": "1.2.0", "ssid": "<network name>", "bssid": "<access point MAC>", "model": "SN-ESP32C3-S60-01", "staMac": "<plug MAC>"}, "productModel": "S60TPG"}}]}}` |
| OTA query, `POST /v2/device/ota/query` | `{"deviceInfoList": [{"deviceid": "<device id>", "model": "SN-ESP32C3-S60-01", "version": "1.2.0"}]}` | `{"error": 0, "data": {"otaInfoList": []}}` |
| app dispatch, `GET /dispatch/app` | `Authorization: Bearer stub-access-token` | `{"error": 0, "reason": "", "domain": "192.168.60.1", "IP": "192.168.60.1", "port": 443}` |
| `userOnline`, first on `GET /api/ws` as the app | `{"action": "userOnline", "at": "stub-access-token", "apikey": "<apikey>", "appid": "<app id>", "nonce": "BucKkPAM", "ts": 1792443098, "userAgent": "app", "sequence": "1792443098404", "version": 8}` | `{"error": 0, "apikey": "<apikey>", "sequence": "1792443098404", "config": {"hb": 1, "hbInterval": 145}}` |
| `upgrade`, the installer | `{"action": "upgrade", "deviceid": "<device id>", "apikey": "<apikey>", "userAgent": "app", "sequence": "1792443098405", "params": {"model": "SN-ESP32C3-S60-01", "version": "4.4.3", "binList": [{"name": "user1.bin", "downloadUrl": "http://192.168.60.1:8088/user1.bin", "digest": "<SHA-256 of the image>"}, {"name": "user2.bin", "downloadUrl": "http://192.168.60.1:8088/user2.bin", "digest": "<SHA-256 of the image>"}]}}` | It passes this to the plug as it is. Within 0.1 s the plug sends `update` `{…, "d_seq": 153697133, "seq": 2, "params": {"only_device": {"ota": "start"}}}`, answered like any `update` and passed to the tool. Half a second later the plug drops its WebSocket, and the stub tells the tool `{"action": "sysmsg", "deviceid": "<device id>", "params": {"online": false}}`. The plug fetches the image from `downloadUrl` in ranges, each request carrying its device id, a `ts` and a `sign`. |

What the values are:

- `d_seq` is a number the plug picks for each message, which the stub's answer
  echoes. The `register` ack must, or the plug drops the connection
  ([test 3](#test-3--does-the-stub-bring-the-plug-online-and-enable-lan-control)).
- `sequence` is the app's time in milliseconds, as a string; the answer to a
  command carries the command's.
- `ts` is the app's time in seconds; the plug sends a number of its own (as a
  string in `register`).
- `version` is `3` in the plug's dispatch request, and `8` on the WebSocket, from
  plug and app alike.
- `upgrade`'s `version` is the installer's own, from its OTA wrapper's header
  (`4.4.3`). `binList` offers it under both of eWeLink's names, `user1.bin` and
  `user2.bin`; the plug fetched `user1.bin`.

## Harness wiring and how to run it

A single switch, **`--stub-cloud`** (mutually exclusive with `--cloud`), chooses
the local stub over the real cloud and toggles everything below. There is no
separate lock to flip, and no firmware copy to manage by hand — see the two notes
after the list.

- **`emu.sh --stub-cloud`** builds the stub flash (changes A + B), generates the
  CA, mounts the certs read-only at `/stub-cloud`, and sets `STUB_CLOUD=1`.
  `AUTOFLASH_EMU_IMAGE` builds/runs an isolated image (so a worktree's build need not
  disturb a shared `autoflash-emu`); instances run 1–30.
- **`container/entrypoint.sh`**, in stub mode, adds the `/etc/hosts` records for
  the three stub hosts → `192.168.60.1`, opens inbound 443 on the LAN in ufw,
  builds a CA bundle of the system roots plus the stub's CA, reads the plug's
  identity from its dump in-container, and starts the stub (non-fatally — a stub
  problem can't take the emulator down). It also exports `AUTOFLASH_EWELINK_API_BASE` /
  `SSL_CERT_FILE` for an interactive conversion.
- **The internet** is handled like a `--cloud` run with one difference: this
  computer may reach the internet (so the conversion downloads the official
  firmware files as any new user's run does), but eWeLink's own hostnames resolve
  to `0.0.0.0` and lead nowhere, and the plug stays on its own network. Nothing
  the plug or tool does reaches the real eWeLink.
- **The tool** trusts the stub by `SSL_CERT_FILE` pointing at a bundle of the
  system roots **and** our CA (so trusting the stub takes nothing away from other
  TLS, e.g. the firmware downloads), and is pointed at the stub by
  `AUTOFLASH_EWELINK_API_BASE`, honoured in `get_device_key.py` (the account API) and
  `probe_ota_command.py` (the app dispatch).
- **`unattended-test.sh --stub-cloud`** runs a whole conversion with no eWeLink
  account and a placeholder `.env` the stub ignores.
- **`fault_suite.py --stub-cloud`** (or `AUTOFLASH_STUB_CLOUD=1`) drops the
  cross-instance cloud lock entirely: every emulator has its own stub, so every
  scenario runs in parallel on `AUTOFLASH_STUB_WORKERS` emulators (from
  `AUTOFLASH_FIRST_INSTANCE`, default 1) with no turns at the cloud and no cloud-turn
  monitor. Its saved starting plugs are `stub-suite-*` and its results go to
  `captures/emulation/suite-stub/`, apart from the real cloud's: their flash is
  the other kind, which the other mode's batch would refuse, and a stub pass is
  not a pass against the real cloud.

**The stub/cloud switch and the lock.** `--stub-cloud` toggles the lock
automatically. With it, the fault suite's `CloudQueue` gate is a no-op (each
instance has its own stub → full parallel). Without it, the real cloud is used
and the gate serialises the cloud-connected sections and keeps the per-turn
cooldown, as before. So you choose per run.

**Two firmware images, kept apart safely.** A stub run and a cloud run use
*different* flash images — stub = CA-swap + `dis_server` redirect; cloud = the
plain image. `emu.sh` records the mode in `state-N/.flash-mode` (a flash from
before modes were recorded, and any ordinary cloud run, is "plain"). Each plug
keeps the flash its last run left, so switching an instance between modes is
**refused** rather than silently reflashing a converted plug — `--fresh` is the
knowing way to start it over, and `--save`/`--from` carry the mode with a kept
plug. Each parallel instance builds its own flash from the read-only stock dump.

### The Tasmota bundle

`step_check` (A5) builds the conversion bundle from the official Tasmota files
unless a valid one is already present, and `emu.sh` excludes the host's bundle
from the container (it is set up for the real home Wi-Fi). Since stub mode lets
this computer reach the firmware CDN, A5 just downloads them as a new user's run
would — no pre-staging needed. The eWeLink-gated part of the conversion (up to
and including C2) reaches no internet at all: the bridge image is a local
artifact, delivered over LAN.

## Verification log

Each entry: goal, how it was run safely, result, interpretation. Append new runs;
don't rewrite old results.

**Running tests safely.** Use a fresh, unused instance (`emu.sh --instance N`
with no `captures/emulation/state-N/` yet); a new instance sets up a clean stock
image from the backup. Never `--fresh` the default/shared low-numbered instances
other work uses — check `docker ps | grep autoflash-emu` first. Stay offline unless a
test needs the real cloud. `docker stop autoflash-emu-N` and `rm -rf
captures/emulation/state-N` when finished with a throwaway instance. Logs land in
`state-N/logs/` (`uart.log` and, in stub mode, `stub.log`, both appended across
runs, and `wifi-*.pcap` with `--pcap`):

```sh
D=captures/emulation/state-N/logs
tcpdump -nn -r "$D"/wifi-*.pcap 'udp port 53'       # DNS queries
tcpdump -nn -r "$D"/wifi-*.pcap 'tcp port 443'      # TLS attempts
strings "$D"/wifi-*.pcap | grep -iE 'coolkit|\.test' # hostnames, without tcpdump
sed 's/\x1b\[[0-9;]*m//g' "$D"/uart.log | tail -80  # de-coloured console
grep -E 'ws <-|ws ->|relay' "$D"/stub.log           # the channel, secrets redacted
```

### Test 1 — does the firmware honour `dis_server`?

**Goal:** decide whether redirecting the plug is a config edit
(`frame_params.dis_server`) or needs a firmware string-patch of the
`%s-disp.coolkit.cc` constructor.

**Run:** clean stock boot, offline, instance 15, `--pcap`, ~2 min.

**Result:** the plug repeatedly queried **`eu-dispd.coolkit.cc`** — the exact
stored `dis_server` value (`dispd`) — never the constructed `eu-disp.coolkit.cc`.
DNS was refused (offline), so it retried on a backoff (~5–90 s). No NTP query
preceded the lookup. No TLS/443 traffic (never resolved).

**Interpretation:** `dis_server` is honoured → redirect is a **pure NVS edit**.
The clock does not gate reaching dispatch. The moment DNS answers our domain, the
plug connects with no other prerequisite.

### Test 2 — does the trust swap let the TLS handshake complete?

**Goal:** with our CA swapped in and the plug pointed at our domain, confirm the
plug completes a TLS handshake to the stub.

**Run:** instance 16, offline, `--pcap`, `--no-firewall` (ufw denies inbound 443;
the internet block is separate iptables rules, so offline is preserved). Stub
wired by hand (the flag was not built yet): `/etc/hosts` + dnsmasq `SIGHUP`, and
`openssl s_server` on `192.168.60.1:443` with the leaf cert.

The patched image was checked first: only the two CA slots, the two BLE patches
and the appended SHA-256 differ from stock, `ota_0` re-verifies, and the bytes
after each slot are unchanged.

**Result:** the plug booted the patched `ota_0` (re-seal accepted) and queried
the dispatch host (`disp.s60-stub.test`, the domain's name then). The TLS 1.2
handshake ran to completion both directions —
ClientHello → ServerHello+Certificate+ServerKeyExchange+Done →
ClientKeyExchange+ChangeCipherSpec+Finished → NewSessionTicket+CCS+Finished — with
**no reset after the certificate**, and the plug then sent an encrypted 327-byte
application-data record (its `POST /dispatch/device`). Negotiated cipher:
**ECDHE-ECDSA-AES256-GCM-SHA384**. Evidence:
`captures/emulation/stub-cloud/test2-evidence/`.

**Interpretation:** the trust swap works. The **EC P-256** choice is viable even
though the stock CA is RSA-1024 — the plug offers ECDHE-ECDSA suites and
negotiated one. No clock/validity gate blocked the handshake.

### Test 3 — does the stub bring the plug online and enable LAN control?

**Goal:** make the stub answer dispatch + IoT well enough that the plug reports
online and opens its LAN-control interface.

**Run:** instance 17, offline, same hand-wiring as test 2 plus dnsmasq resolving
both stub hosts to `192.168.60.1` and `stub_server.py` started in the container.
Two firmware quirks were found and fixed, each re-tested with a clean power-cycle:

1. **Dispatch `IP` must be a numeric address, not a hostname** — the plug
   `inet_aton`s it (zero DNS lookups for the IoT host), so a hostname became
   `INADDR_NONE` and it dialled `255.255.255.255:443`. Returning `192.168.60.1`
   (with the matching IP SAN on the leaf) fixed it.
2. **The register ack must echo `d_seq`** (a number, not `sequence`); without it
   the plug dropped the WebSocket after ~10 s and re-dispatched.

**Result:** on a clean boot the plug ran the full exchange on one sustained
WebSocket — `register` → ack (`d_seq` + `config {"hb":1,"hbInterval":145}`),
`date`, `update` (`init:1`, ota success, fwVersion), its upgrade-result report,
`update` (ssid/bssid), `query` → bare `error:0` (accepted). The connection stayed
`ESTABLISHED`, and the plug began **advertising its eWeLink LAN service over
mDNS** (`_ewelink._tcp.local`, `SRV …:8081`, TXT `encrypt=true`, `type=plug`) and
**opened port 8081** — the LAN endpoint it only exposes while online. Evidence:
`captures/emulation/stub-cloud/test3-evidence/`.

**Interpretation:** the stub replaces touchpoint B end to end. A bare `error:0`
to `query` suffices (the plug does not need real timer/energy values to come
online).

### Test 4 — end-to-end `--stub-cloud` conversion through C2

**Goal:** a conversion with the stub as the only cloud — the tool reads the LAN
key + metadata from the stub (touchpoint A), the plug is online via the stub
(touchpoint B), and the bridge is delivered.

**4a — the tool-facing account API.** `stub_server.py` gained `/v2/user/login`,
`/v2/device/thing` and `/v2/device/ota/query`; `get_device_key.py` gained the
`AUTOFLASH_EWELINK_API_BASE` override; trust via `SSL_CERT_FILE` (the tool's `urlopen`
uses `ssl.create_default_context()`, which honours it: no code change for trust).
Live (instance 18):
`get_device_key.py` logged in and saved the LAN key, and `query_ota.py` read
`SN-ESP32C3-S60-01` v1.2.0 `online:true` — both against the stub, no coolkit
contact. (Fixed along the way: the stub redacts `devicekey`/`encrypt_key`.)

**4b — the `--stub-cloud` flag.** Wired into `emu.sh` / `entrypoint.sh` /
`unattended-test.sh` with `device_identity.py` and `AUTOFLASH_EMU_IMAGE`. Live:
`emu.sh --stub-cloud --instance 19` brought the plug online entirely hands-off
(stub auto-started, dispatch, `/api/ws` register, LAN 8081) with zero manual
steps.

**4c — conversion through C2.** The bridge is delivered by `serve_tasmota_ota.py`,
which drives the app side: login, app dispatch, a `userAgent:"app"` WebSocket,
and an `upgrade` the cloud passes to the device. So the stub gained
`/dispatch/app`, the `WS_DISPATCH` override (`probe_ota_command.py`), and the
**app↔device relay**: until then it answered each socket on its own, and the
command never reached the plug. Run: `emu.sh --stub-cloud`, then `autoflash_cli.py step
{A0,A5,A1,A2,C2}` pointed at the stub. A5 built the Tasmota bundle by downloading
the official files (stub mode lets this computer reach the firmware CDN while
eWeLink stays blocked). **Result:** A1 discover, A2 device key from the stub,
**C2 complete in ~18 s** — the stub log shows the app dispatch and
`relay app->device (upgrade)`, and the plug fetched the bridge over LAN and
rebooted into it (`Loaded app from partition at offset 0x210000`, `Project name:
s60_ota_bridge`, `joined the home network`). Evidence:
`captures/emulation/stub-cloud/test4-evidence/`.

**Interpretation:** the stub fully replaces eWeLink for the conversion's
eWeLink-dependent path — account API, online dispatch/IoT, and the relayed
upgrade — with nothing reaching the real cloud. The bridge download is plain LAN
HTTP, so C2 did not exercise the LAN-control AES key (the `apikey` vs
`encrypt_key` question only matters for direct LAN commands, which this path does
not use; the tool accepted `devicekey = apikey` at A2, and in this plug's dump
the two are the same value anyway). Past C2, eWeLink is
needed again only to send the installer again (see [Status](#status)).

### Test 5 — parallel runs

**Goal:** confirm the original goal — more than one emulated plug (the same
device id) online at once, which the real cloud forbids.

**Run:** `emu.sh --stub-cloud` on instances 24 and 25 at the same time (the same
plug MAC), each on the isolated `autoflash-emu-stub` image.

**Result:** both plugs came online **simultaneously** — each with an
`ESTABLISHED` WebSocket to its own stub on `192.168.60.1:443`, each registered,
each with LAN port 8081 open. Two clones of one device id online concurrently.

**Interpretation:** the shared-cloud serialisation is gone. Each instance's stub
is independent (its own `192.168.60.1:443` in its own network namespace), so runs
are fully parallel — the whole point of the cloud stub. (Bounded only by host RAM:
~3 emulators on a laptop, one per core on the test VMs.)

### Test 6 — full conversions, stub and cloud

**Goal:** whole conversions in both modes and to both firmwares, compared.

**Run:** `unattended-test.sh --fresh -- --firmware {tasmota,esphome}`, with
`--stub-cloud` (the two side by side) and against the real cloud (one at a
time, the real plug confirmed away from eWeLink first).

**Result:** all four converted with no retries, and the stub and cloud runs
alike: Tasmota in all 16 stages, 4m 11s (stub) against 4m 13s (cloud); ESPHome
in 11 stages, 2m 36s against 2m 38s. The one difference was the product model:
blank in the stub runs, since the dump does not record it, against `S60TPG` from
eWeLink. Once the stub served the product model eWeLink had given this plug (see
[the stub server](#the-stub-server)), the stub Tasmota run reported `S60TPG`,
"UK (Type G)" and named the Tasmota template "Sonoff S60TPG", as the cloud run
did.

**Interpretation:** a stub run stands in for a cloud run end to end, report
included.

### Test 7 — the whole account record

**Goal:** an account record with what eWeLink's carries beyond test 6's product
model, so the tool's failure paths read as they do against the real cloud (see
[the requirements](#requirements-and-where-each-came-from)).

**Change:** the stub passes on the params the plug reports in its `update`s
(`ssid`, `bssid`, `fwVersion`, `only_device`) and serves `params.staMac`, which
the plug never reports: it is the MAC `emu.sh` runs the plug with (`PLUG_MAC`,
to `device_identity.py --sta-mac`). `staMac` is redacted in the stub's log with
the other identifying values.

**Run:** `unattended-test.sh --stub-cloud --fresh -- --firmware
{tasmota,esphome}`, side by side, on an image with the emulator's Wi-Fi signal
fix ([`wifi-rssi.patch`](emulator.md#the-qemu-fork-toolsemulatorforkpatch)).

**Result:** the device list carried `ssid`, `bssid`, `fwVersion`,
`only_device`, `model`, `staMac` and the product model, the ESPHome conversion
passed, and the Tasmota report gave "Plug Wi-Fi signal: -17 dBm". But the
Tasmota run stopped at stage 14 (F4): "the plug did not come back after its
settings were reset", with the plug up and answering all along, its console
shows. The tool had missed the restart. It tells boots apart by
`StartupUTC#BootCount`, which read `#1` both times: with no clock `StartupUTC`
is empty, Reset 4 sets the boot count back, and Tasmota counts a boot at 10 s of
uptime. Its other check, the uptime going below its value before the reset
(10 s), missed too: the request the restart cut off was held for its full 10 s,
so the new boot first answered at 12 s. A cloud run escapes this only because
there the plug reaches NTP, which sets `StartupUTC`.

**Fix, in the tool:** `wait_for_reboot` also counts a plug that has been up for
less time than the wait has lasted as restarted, and asks again when the restart
cuts off the uptime question (unit tests replay the timeline).

**Rerun, with all of it in:** three conversions side by side, all converted
with no retries — Tasmota twice (4m 11s and 4m 22s, signal -22 and -19 dBm) and
ESPHome (2m 35s). One Tasmota run met the same race (Reset 4 at 9.3 s of uptime,
`#1` on both boots, the first answer at 12.4 s), and stage 14 completed in 38 s
where before it ran out after three minutes.

**Interpretation:** the stub's plug differs from one on the real cloud in having
no clock ([risks](#risks-and-limitations)), and that found a restart check that
leaned on one; the same race was open to any plug without NTP time, the plain
emulator's included.

### Test 8 — twenty fault-suite scenarios

**Goal:** the fault suite against the stub, in part: every direction, and the
cloud steps above all, where the stub stands in for eWeLink while a fault hits.

**Run:** `fault_suite.py --stub-cloud prep`, then `batch` with 20 scenarios on
three emulators (instances 27-29), the starting plugs and results kept apart
from the real cloud's (`stub-suite-*`, `captures/emulation/suite-stub/`):

- **Through the stub (10):** at C2 a second sign-in to the account
  (`token-login`; the stub's one fixed token makes this a pass that proves
  little), the network gone for 15 s, a packet changed, Ctrl-C twice then
  carrying on; at C3
  a power cut, the computer crashing for a minute, the disk filling; at C3fb
  the network gone for 30 s; at D1 a power cut of 30 s, a second run started.
- **The rest (10):** I1 network gone, F4 network gone for a minute, E3 power
  cut late in the step, F2 a weak network, L1 a power cut, M1 Ctrl-C, R1 the
  terminal closed, R2 the network gone, X1 the computer off, D4 two faults.

**Result:** all 20 passed, in 33 minutes. In three - the power cuts at C3 and
D1, the network gone at C3fb - the plug went back to Sonoff's firmware and the
installer was sent again through the stub (twice after the C3 power cut), and
the run converted the plug.

**Interpretation:** the stub carries the cloud steps through their faults as
the real cloud does, the installer sent again included. The rest of the suite
is still to run against it.

### Test 9 — the signal from the start

**Goal:** the plug's Wi-Fi signal read at the earliest stage of every
direction, so a weak one can be fixed by moving the plug before the conversion
is under way, and the report giving the lowest of the readings.

**Change:** at A2 the tool reads Sonoff's firmware's own advert (above),
opened with the LAN key from the account, before anything is written. From
Tasmota it reads `Status 11` at L1, before anything is changed. From ESPHome
it reads at X1, before anything is written: the plug's `WiFi Signal` sensor
where its ESPHome has one (this project's from 1.1.0, which adds it), else
the signal its native API prints in its configuration dump (`Signal strength:
-19 dB`), where the API has no encryption key - this project's has none,
1.0.0 included. From an ESPHome that gives neither, the first reading is at
D4, once the full Tasmota runs: the startup firmware between them, the
recovery Safeboot, is Tasmota's minimal build and gives none either - it
answers `Status 11`, `Status 0` and `WiFiScan` with `Unknown`, and neither its
pages nor its console show a signal (an emulated plug stopped after X1). D4,
E1 and I1 read it again on the way. A weak signal is asked about once a run,
and the report gives the lowest reading.

**Run:** one stub conversion for each kind of start: from Sonoff to Tasmota
and to ESPHome, from a saved ESPHome plug to Tasmota, and from a saved Tasmota
plug to ESPHome.

**Result:** all four converted, with the readings where they belong - Sonoff
to Tasmota: A2 -21, D4 -18, E1 -21 dBm, reported -21; Sonoff to ESPHome: A2
-17, D4 -24, I1 -21, reported -24; ESPHome to Tasmota: D4 -21, E1 -23,
reported -23; Tasmota to ESPHome: L1 -19. The A2 readings came from the stock
firmware's own advert, opened with the LAN key the stub's account served. The
ESPHome to Tasmota run started from a plug on ESPHome 1.0.0, which has no
sensor, so its first reading was at D4. Then with 1.1.0: a plug converted to
it (A2 -21, D4 -23, I1 -24, reported -24) answered `/sensor/WiFi%20Signal`
with `{"id":"sensor/WiFi Signal","value":-18,"state":"-18 dBm"}`, and taken
on to Tasmota it was read at X1 before anything was written: X1 -18, D4 -20,
E1 -18, reported -20. With the API read added, the plug on 1.0.0 was read at
X1 too, from its configuration dump: X1 -23, D4 -19, E1 -22, reported -23.

## Risks and limitations

- **Fidelity:** the stub tests *our tool and the bridge firmware*, not eWeLink's
  servers. Keep a small set of real-`--cloud` runs as an end-to-end smoke test.
- **Scope past C2:** once D1 is done the conversion is LAN-only and exercises no
  eWeLink; it runs the same under `--stub-cloud` as under `--cloud`. Sending the
  installer again at C3 or D1 goes through the stub, and passed there in three
  fault-suite scenarios ([test 8](#test-8--twenty-fault-suite-scenarios)).
- **The stub's record lives as long as the instance.** After the container
  starts it knows no version or params until the plug's first dispatch, where
  eWeLink keeps the last ones the plug sent. The plug dispatches as soon as it
  boots, and the tool needs it online anyway, so no run has met the gap.
- **`query` gets a bare `error:0`.** The plug comes online without timers or
  energy settings, and nothing in a conversion asks for them; testing those would
  need real answers.
- **One fixed token.** The stub's sign-in always gives the same token and never
  ends a session, so the fault suite's `token` fault (a second sign-in to the
  account during C2) tests nothing against it; that one belongs to the real
  cloud.
- **The plug has no clock.** It stays offline in stub mode, as in the plain
  emulator, so it gets no NTP time, which a plug on the real cloud does: Tasmota's
  `StartupUTC` stays empty. Nothing in the tool may lean on it;
  [test 7](#test-7--the-whole-account-record) found a restart check that did.
- **Resembles a MITM, so built deliberately:** the low-level mechanics (a
  trust-store edit plus a server answering the dispatch protocol) look like a TLS
  man-in-the-middle even though this is self-hosting our own device to our own
  server. It is kept confined to the firewalled per-instance network.

## Safety / rollback

- Stock dump untouched; all edits are on the per-instance working copy.
- The real eWeLink is never reached in stub mode: its hostnames resolve to
  `0.0.0.0`, the tool is pointed at the local stub, and the plug stays on its own
  network (this computer may reach the internet for the firmware downloads, as a
  `--cloud` run does). The real cloud and real plugs cannot be affected.
- `--fresh` restores the plug copy from the backup.
- The CA private key stays in `captures/emulation/stub-cloud/` (git-ignored) and
  never enters the firmware; device keys are read only inside the container and
  redacted in logs.

## Files

Added under `tools/emulator/stub_cloud/`: `__init__.py`, `make_ca.py`,
`device_identity.py`, `stub_server.py`. Changed:
`tools/emulator/patch_stock.py` (CA-swap patch), `rewrite_nvs.py`
(`--dis-server`/`--dis-port`), `emu.sh` and `unattended-test.sh` (`--stub-cloud`,
`AUTOFLASH_EMU_IMAGE`), `container/entrypoint.sh` (stub DNS/firewall/launch),
`fault_suite.py` (drop the cloud lock under `--stub-cloud`), and
`tools/get_device_key.py` / `tools/probe_ota_command.py` (the
`AUTOFLASH_EWELINK_API_BASE` override).

## Ground rules

- **No secrets in the repo.** The dump holds the plug's eWeLink keys and Wi-Fi
  password in cleartext; keep values out of docs and commits.
- **The stock dump is irreplaceable** — read-only; every edit is on a per-instance
  working copy under `captures/emulation/state-N/`.
- **Don't disturb the `esphome` emulator tests or files** when running cloud-stub
  work on the same machine (use a fresh unused `--instance N`; the isolated
  `AUTOFLASH_EMU_IMAGE` keeps the shared image untouched).
- **Change it deliberately.** The mechanics (a trust-store edit plus a server
  answering the dispatch protocol) resemble a TLS man-in-the-middle even though
  this is self-hosting; keep each change reviewed, and the stub confined to the
  firewalled per-instance network.
