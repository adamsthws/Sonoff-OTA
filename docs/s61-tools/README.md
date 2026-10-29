# S61 emulation & reverse-engineering tools

Research tooling for the Sonoff S61 (BK7238) work — see
[../beken-s61-emulation.md](../beken-s61-emulation.md). These are not part of the
Autoflash tool; they drive the **BekenEmulator fork** (`../BekenEmulator`) or
analyse the firmware image statically.

**Requirements.** The static-analysis tools need `capstone` (all) and `angr`
(`s61_decomp.py` only); the emulator tools need the fork checked out. Install into
a virtual environment, never the system. The emulator tools take paths via env
`BK_EMU_ROOT` (default `~/Git/BekenEmulator`) and `S61_DUMP`.

## Static analysis

Work on the **logical image** (the dump with its CRC stripes removed, so a CPU
address equals a file offset). Produce it once with `s61_unstripe.py`, then point
the other tools at it.

- **`s61_unstripe.py`** — un-stripe a raw Beken dump to a logical image and decode
  its RBL update headers. Uses the fork's CRC routine.
  `BK_EMU_ROOT=../BekenEmulator python3 s61_unstripe.py <raw-dump> logical.bin`
- **`s61_dis.py`** — disassemble a window (capstone), Thumb by default.
  `python3 s61_dis.py logical.bin 0x4203c 24`
- **`s61_xref.py`** — find references to an address: BL/BLX callers over the code
  region plus 32-bit data-word refs (for `addr` and `addr|1`).
  `python3 s61_xref.py logical.bin 0x3eba0 0x21e50`
- **`s61_strxref.py`** — locate code by the log/format string it uses: finds the
  string, its literal-pool refs, and disassembles the `ldr` that loads each.
  `python3 s61_strxref.py logical.bin "pairingMode by" "softap:ssid"`
- **`s61_decomp.py`** — decompile functions to pseudo-C with angr (best-effort;
  a reading aid alongside `s61_dis.py`). No Java/Ghidra needed.
  `python3 s61_decomp.py logical.bin 0x4203c`

## Driving the emulator

- **`s61_integrity_gate.py`** — lets the stock image boot under emulation past the
  flash-UID integrity gate that otherwise hangs the app layer (see the main doc).
  Forces the identity `memcmp` at `0x29c3c` to "equal" (the outcome it has on real
  hardware, whose UID the emulator's synthetic one can't match) and shows the
  firmware then boot the app framework naturally: `key_add` registers the button,
  the provisioned-check runs, and `ais_appfw_task` is created. This operates only
  inside the emulator, on a dump of the owner's own plug; it is never written to or
  run on a real plug.
  `BK_EMU_ROOT=../BekenEmulator python3 s61_integrity_gate.py`

- **`s61_long_press.py`** — with the gate cleared, drive the button long-press end
  to end through the firmware's own key stack. Two interventions: the integrity
  bypass, and raising Timer 5's interrupt status (`timer3_5_ctl` bit 9) each slow
  tick — the key-driver's periodic runs on the `bk_timer` = Timer 5, which the
  emulator does not raise (it raises only Timer 3, the RTOS tick). With both, the
  key-driver timer thread (`0x3eba0`) wakes naturally, its scan (`0x421e0`) runs,
  and a press-and-hold of pins 0/2 trips the real long-press action `0x42250`.
  `BK_EMU_ROOT=../BekenEmulator python3 s61_long_press.py`

- **`s61_pair_drive.py`** — drive the firmware into pairing mode and watch the
  emulated radio follow. With the gate cleared and the app framework cycling,
  nothing sets a pairing method, so the device idles; this tool calls the
  firmware's own pairing-method dispatcher `0x21eb4` from inside the appfw task's
  context. Method 0 (coexist) brings up **soft-AP** — the MAC leaves monitor mode
  and transmits beacons, and the virtual peer sees the device as an access point;
  method 1 enters **station** mode and probes/associates. `airkiss` instead drives
  the sniffer-start `0x223b0` (the RX-FIQ handler object goes live) — but note the
  AirKiss monitor callback `0x403b20` is only ever a **stub** in this build, so
  there is no decoder behind it (see the main doc). Shares the integrity bypass +
  Timer-5 raise with `s61_long_press.py`.
  `BK_EMU_ROOT=../BekenEmulator python3 s61_pair_drive.py 0` (soft-AP) `| 1`
  (station) `| airkiss`. Env `SSID`/`PASS`, `SECS`, `FIRE_AT`.

- **`s61_seed_join.py`** — put the emulated plug on a network the faithful way.
  Hooks the firmware's own `ef_get_env_blob` (EasyFlash V4, `0x12f438`) and returns
  our SSID/passphrase for `wifi-ssid`/`wifi-pass` — making it a provisioned plug at
  the credential read, with nothing else forced. Attaches the fork peer as a
  WPA2-PSK AP with the same creds; the plug then does the real sequence (station →
  associate → WPA2 four-way → **IPv4 DHCP → 192.168.1.100**). This corrects the old
  "seeding creds leaves it idle" result — that was the integrity gate, which this
  clears too. Env `SSID`/`PASS`/`CH`/`SECS`.
  `BK_EMU_ROOT=../BekenEmulator python3 s61_seed_join.py`

- **`s61_ble_provision.py`** — synthesise the eWeLink **BLE provisioning session**
  without a BLE radio: feed the provisioning messages (DEVICE_INFO, AUTH_TOKEN,
  HIDDEN_AP_CONNECT/Wi-Fi, SERVER_INFO) through the firmware's command dispatcher
  `0x72940`, so its own consumer task runs the handlers in context (driving them
  directly corrupts the shared state; the dispatcher does not). Data fields are
  injected plaintext past the AES (the key is the device apikey at RAM `0x40ce90`).
  The handlers run and the plug joins our AP (WPA2 + DHCP). This exercises the real
  eWeLink provisioning path, but does not by itself dial the cloud — that needs the
  pairing-completion trigger (`s61_cloud_dial.py`). Env `SEQ`/`SSID`/`PASS`/`SECS`.
  `BK_EMU_ROOT=../BekenEmulator python3 s61_ble_provision.py`

- **`s61_cloud_dial.py`** — the full provisioning outcome: the emulated plug dials
  the **eWeLink (CoolKit) cloud**. Seeds Wi-Fi creds (join + DHCP), then synthesises
  the pairing-success trigger by force-calling the firmware's own start-cloud
  handler `0x2c204` (the single call the pairing state machine makes on success).
  `ais_link_task` then opens a TCP connection and sends `GET /dispatch/device`,
  retried with backoff. The dispatch target lives in the AIS context (`*0x4044ac`,
  factory placeholder `http://8.8.8.8:80`); **by default this tool repoints it to a
  local stub on the emulator's AP gateway (`http://192.168.1.1:80`), so the dial
  stays on this host.** `DISPATCH` steers it elsewhere; `DISPATCH=` keeps the factory
  placeholder. **Owner's own account only:** never point it at the real eWeLink cloud
  with anyone's account or plug but your own. Env `DISPATCH`/`SSID`/`PASS`/`SECS`/`FORCE_AT`.
  `BK_EMU_ROOT=../BekenEmulator python3 s61_cloud_dial.py`

- **`s61_cloud_online.py`** — complete the eWeLink cloud cycle: the plug reaches
  **ONLINE**. Stands up the CoolKit stub (`tools/emulator/stub_cloud/stub_server.py`,
  `--plain` dispatch mode) on loopback and bridges the netstack relay to it
  (plaintext dispatch + TLS IoT, fully contained — nothing leaves the host), seeds
  Wi-Fi creds and fires the pairing trigger as `s61_cloud_dial.py` does, then drives
  dispatch → TLS `/api/ws` → `register` → `{error:0, config}` ONLINE. Two things it
  handles that the S60 does not: the S61 dispatch reply must be **compact** JSON
  with a **`domain`** key and a **string** `port`; and the IoT `wss://` cert is
  verified, cleared by a **trust-anchor swap** — the honest S60 `--trust-ca`
  technique — applied at `mbedtls_x509_crt_parse` (`0x125e4c`) because the S61
  assembles its CoolKit CA in RAM rather than keeping it in flash. It generates its
  own EC P-256 CA + leaf (openssl) and serves the leaf; the firmware's verify path
  is untouched. Env `SSID`/`PASS`/`SECS`/`FORCE_AT`/`HB`.
  `BK_EMU_ROOT=../BekenEmulator python3 s61_cloud_online.py`

- **`s61_button_sim.py`** — older button sim (predates the integrity-gate finding
  and natural registration; defaults to the wrong pin 3). Force-runs the block init
  and `key_add`, then presses by driving the GPIO low and latching the capture
  status in `state.pwmn_stat`, reaching the armed long-press timer.
  `BK_EMU_ROOT=../BekenEmulator python3 s61_button_sim.py [pin]`

- **`s61_airkiss_capture.sh`** — put the laptop's Intel AX200 into monitor mode on
  the router's channel and capture a phone's eWeLink AirKiss/smartconfig broadcast
  to a pcap. Needs root; takes over the Wi-Fi adapter while capturing.
  `sudo ./s61_airkiss_capture.sh <channel> [iface] [out.pcap]`

- **`s61_airkiss_replay.py`** — replay captured 802.11 frames into the emulated
  S61's Wi-Fi MAC (monitor mode) so the firmware's own AirKiss decoder can consume
  them; an AP-role peer with the known SSID/pass then lets the device associate.
  Stdlib-only pcap/radiotap parsing (DLT 127/105).
  - `BK_EMU_ROOT=../BekenEmulator python3 s61_airkiss_replay.py capture.pcap --ssid MyNet --pass MyPass`
  - `python3 s61_airkiss_replay.py --selftest`  (synthetic-frame pipeline check)

### AirKiss end-to-end procedure

1. Pick an SSID/password to provision to (a real or throwaway network).
2. `sudo ./s61_airkiss_capture.sh <router-channel> wlp1s0 airkiss.pcap`
3. On the phone: eWeLink app → add the plug → Wi-Fi/quick (AirKiss) pairing →
   enter that SSID/password → start. Let it run ~30-60 s, Ctrl-C the capture.
4. `BK_EMU_ROOT=../BekenEmulator python3 s61_airkiss_replay.py airkiss.pcap --ssid <SSID> --pass <PASS>`
5. Watch whether the firmware leaves monitor mode, probes/associates, gets DHCP.

## Conversion and config migration

Reading the stock config and carrying the Wi-Fi credentials into OpenBeken. See
[the config-migration section of the emulation reference](../beken-s61-emulation.md#carrying-wi-fi-across-the-conversion-the-openbeken-config)
for the full findings (config partition map, OpenBeken `mainConfig_t` format, the
signed-char `Tiny_CRC8`, the `0x1e1000` config offset).

- **`ef40_dump.py`** — enumerate the stock EasyFlash V4 (EF40) key-value stores
  from a flash dump, with CRC validation. Shows `wifi-ssid`/`wifi-pass`,
  `elecCalibCoef` and the rest. `python3 ef40_dump.py <flash.bin>`
- **`s61_ef40.py`** — EF40 reader/writer library: `parse_store()` reads KV40
  nodes; `provision()` writes synthetic `wifi-ssid`/`wifi-pass` nodes (for
  building a provisioned test dump, since the public dump is join-unprovisioned).
- **`obk_config.py`** — build an OpenBeken `mainConfig_t` blob carrying given
  Wi-Fi credentials (correct ident/version/offsets, `sizeof` 3584, and the
  **signed-char** `Tiny_CRC8` the shipped binary uses). `build_config(ssid, pass)`.
- **`s61_wifi_migrate.py`** — the offline chain: read `wifi-ssid`/`wifi-pass` from
  a stock dump's EF40 → build an OpenBeken config → write it at the config
  partition of an OpenBeken image. Version-fragile (depends on the exact config
  format); prefer the import module below.
  `python3 s61_wifi_migrate.py --stock stock.bin --obk OpenBK7238_QIO.bin --out converted.bin`
- **`openbeken/drv_sonoff_s61_import.c`** — **the dependable mechanism to ship.**
  An OpenBeken module that, on first boot, reads the stock EF40 creds with
  `beken_hal_flash_read` and sets them via OpenBeken's own
  `CFG_SetWiFiSSID()`/`CFG_SetWiFiPass()` — so it survives OpenBeken config-format
  changes across releases — then carries the factory meter calibration over and
  applies the S61's wiring. Reads the stock store only (reversible). Build recipe
  and the `S61RevertToStock` command in [`openbeken/README.md`](openbeken/README.md).
- **`esphome/README.md`** — the **ESPHome route**: how
  [`ESPHome/sonoff_s61.yaml`](../../ESPHome/sonoff_s61.yaml) (LibreTiny) reads
  the stock Wi-Fi and meter calibration from the EF40 store at boot, its 4 MB
  flash layout, the build, the reversion through ESPHome's own OTA, and what
  the emulator validated.
- **`s61_stock_app.py`** — extract the stock app from a dump, verified against the
  FNV-1a hash in its RBL header, ready to repackage with the SDK packer for a
  revert to stock. `python3 s61_stock_app.py dump.bin -o stock_app_1.0.2.bin`
- **`s61_backup.py`** — copy a converted plug's per-device flash region
  (factory TLV, EasyFlash store, system ENV; `0x3e0000-0x400000`) out over
  OpenBeken's REST API into `captures/plugs/<mac>/sonoff-backup/`, the S60's
  layout, never overwriting. `python3 s61_backup.py --plug http://<plug> --stock-rbl stock_app_1.0.2.rbl`

## Status

- **Root cause:** the app layer never started because the `app` task
  hangs in a flash-UID factory integrity self-check (`bk_flash_get_uid` hashed vs a
  stored cert; the emulator's synthetic UID can't match). `s61_integrity_gate.py`
  clears it and the firmware boots the app framework and **registers the button by
  itself** (pins 0 and 2).
- **Key-driver timer thread wake (found + handled):** the periodic that times the
  button hold runs on the `bk_timer` hardware timer = **Timer 5**; the emulator
  raised only Timer 3 (the RTOS tick), so the thread (`0x3eba0`) never woke.
  `s61_long_press.py` raises Timer 5's status and the thread wakes natively, the
  scan (`0x421e0`) runs, and the long-press action `0x42250` fires (it drives the
  relay/LED via `bk_gpio_output`). A blanket raise in the emulator's `_pacing`
  regresses 3 other BK7238 self-tests, so it is scoped to the tool; a clean fork
  fix needs per-timer-period modelling.
- **Pairing mode reached:** the appfw state machine (`ais_appfw_task`)
  is alive and cycling, waiting on `rt_event_recv` (`0x134da8`, event `[0x4040e0]`).
  Calling the firmware's pairing-method dispatcher `0x21eb4` from that task's
  context (`s61_pair_drive.py`) brings the emulated radio up: **method 0 beacons as
  a soft-AP** (the peer sees an access point), **method 1 enters station mode** and
  associates. So pairing mode is observably entered.
- **On a network, the faithful way:** `s61_seed_join.py` seeds
  `wifi-ssid`/`wifi-pass` at the firmware's own `ef_get_env_blob` and the plug
  connects for real — associate → WPA2 four-way → **IPv4 DHCP → 192.168.1.100**.
  This corrects the main doc's old "seeding leaves it idle" note (that was the
  integrity gate). `s61_ble_provision.py` reaches the same Wi-Fi join via the real
  eWeLink BLE provisioning messages, driven through the firmware's dispatcher.
- **Cloud dial achieved:** `s61_cloud_dial.py` gets the plug to dial
  the **eWeLink cloud**. Left alone after the join the plug does **Matter**
  (`_matterc`); the eWeLink cloud link is created only on pairing success. That
  trigger is one firmware call (the start-cloud handler `0x2c204`, whose only
  callers are in the pairing state machine) — synthesising it, with the AIS cloud
  context already factory-populated (UUID/model/MAC/dispatch-URL at `*0x4044ac`),
  makes `ais_link_task` send the CoolKit **`GET /dispatch/device`** over TCP,
  steerable at a local stub. A genuinely bound dump is still useful only for a
  *faithful* server + device-secret. See the main doc for the full gate chain.
- **OTA / conversion path mapped:** the S61 flashes updates the same
  CoolKit way as the S60 — an `upgrade` command names a `downloadUrl` we control.
  The cloud download is a CoolKit **FOTA** container (magic `0x1beef11e`, HTTP 206,
  version gate, CRC32s); the bootloader then applies an `rt_ota` **RBL**. Tested by
  staging an RBL directly in the download partition and booting: the bootloader
  **erases + writes a third-party unsigned image to the app** — **no signature
  wall** (`rt_ota_custom_verify` is a link-address check, not crypto). The apply path
  is **LZMA decompression + standard CRC-32** (with an FNV-1a `raw_hash`), all in
  software, with **no payload decryption** — the `crypt` field is a header flag, not a
  cipher, and the download path writes bytes verbatim too. So the whole OTA chain has
  **no signature, no encryption and no secret key**. **Proven with a real image
 :** OpenBeken ships BK7238 `OpenBK7238_<ver>.rbl` OTA images; staged on
  the stock bootloader (no bypasses) it decompresses + writes the full image, passes
  every firmware gate including the final hash verify, and **boots OpenBeken** — the
  emulator performs the whole apply itself (once it models the flash JEDEC id + XIP
  write-through; see the main doc). The full image also **stages verbatim over the
  cloud path** (byte-for-byte). `s61_fota_wrap.py` wraps any `.rbl` in the FOTA
  container (and can serve it).
  (An earlier note here claimed a hardware-AES engine with a baked key — that was a
  mis-attribution, now corrected.) See
  [the main doc](../beken-s61-emulation.md#firmware-update-and-conversion-the-ota-path).
- **Cloud ONLINE achieved:** `s61_cloud_online.py` completes the
  handshake against a local stub — dispatch → TLS `/api/ws` → `register` → ONLINE,
  then heartbeats. Two S61-specific pieces: the dispatch reply must be compact JSON
  with a `domain` key and a string `port`; and the `wss://` IoT cert is verified,
  cleared by the S60's honest trust-anchor swap (`--trust-ca`) applied at
  `mbedtls_x509_crt_parse` (`0x125e4c`), since the S61 builds its CoolKit CA in RAM
  rather than storing it in flash. No verification is weakened — only the trusted
  root is swapped for our own, and the stub serves a leaf signed by it.
- **AirKiss — decoder stubbed in this build:** frame delivery into
  the MAC is solved (100 %, `sent == rx` on a synthetic self-test and a real
  7446-frame capture) once injection is gated on the RX-FIQ handler object
  (`S61_RX_OBJ_PTR`, default `0x403c10`); the tool sets `monitor_rx_deliver` on the
  fork's MAC. But there is **no decoder to reach**: `0x403b20` is the monitor-frame
  callback (RX path reads it and `blx`es it per frame), and the only value the
  firmware ever registers is the stub `0x12c483` (`bx lr`). Setting it and replaying
  11 000+ frames calls the stub once per frame, 0 faults, nothing decoded. eWeLink
  kept the AirKiss scaffolding but compiled out the EZ decoder; real pairing is
  soft-AP + BLE. The delivery/replay tooling is retained for firmware that *does*
  decode.
- A capture carries the credentials of whatever network it was taken on, in its
  frame lengths, so a `.pcap`/`.pcapng` is **never committed** — the leak scan
  refuses one by its name. A regression fixture uses synthetic frames instead
  (`s61_airkiss_replay.py --selftest`), not a captured file.
