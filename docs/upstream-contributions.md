# Fixes sent upstream

Building this converter meant pushing ESP32/Beken firmware, HTTP clients and OTA
code harder than most projects do: downloading firmware in pieces, over Wi-Fi
that is allowed to drop at any moment, with a plug that has to survive it. When
something went wrong and the cause was not ours, we chased it into the project
it came from, fixed it there, and sent the fix back.

This page lists those fixes, grouped by what they were about, and how each
was proven.

## How it started

The conversion runs a Berry script on the plug that downloads a firmware image
in blocks with Tasmota's `webclient`. In testing, a network drop at the wrong
moment left a plug answering nothing - not even its web page - until it was
switched off and on. The cause was not in our script: Tasmota's HTTP client
waited for the rest of a response body for as long as the connection looked
open, with no time limit. A server that goes quiet without closing the
connection (its computer sleeps, a cable is pulled, a router restarts) leaves
it looking open for ever.

That same unbounded wait turned out to have been copied, over the years, into
several other HTTP clients, and following it led to a few more bugs nearby.

## What was sent, by topic

- [HTTP clients that waited for ever](#http-clients-that-waited-for-ever) -
  Tasmota, arduino-esp32, ESPEasy, LibreTiny
- [Firmware updates that never gave up](#firmware-updates-that-never-gave-up) -
  arduino-esp32, ESP-IDF, Tasmota
- [A build failure, followed to its root](#a-build-failure-followed-to-its-root) -
  LibreTiny, SCons
- [The emulator's Wi-Fi](#the-emulators-wi-fi) - QEMU (lcgamboa's fork)
- [OpenBeken on a 4 MB BK7238](#openbeken-on-a-4-mb-bk7238) - OpenBeken,
  beken_freertos_sdk
- [Smaller fixes found along the way](#smaller-fixes-found-along-the-way) -
  Tasmota, ESPHome
- [Other people's reports, reproduced on the emulator](#other-peoples-reports-reproduced-on-the-emulator) -
  Tasmota

## HTTP clients that waited for ever

Tasmota's `HttpClientLight` is a copy of arduino-esp32's `HTTPClient`, and so
are ESPEasy's `HttpClientLight` (copied from Tasmota's) and LibreTiny's
`HTTPClient`. All of them read a body in a loop that only stopped when the
connection closed or the body was complete. The fix is the same idea in each:
give up once no data has arrived for the client's timeout, as they already did
for the headers. A body that keeps arriving, however slowly, still completes.

| Project | Pull request | What it fixes |
|---|---|---|
| Tasmota | [#25139](https://github.com/arendst/Tasmota/pull/25139) | `HttpClientLight` waited for ever for the rest of a body |
| Tasmota | [#25141](https://github.com/arendst/Tasmota/pull/25141) | The same in `AsyncHttpClientLight`, in its sync and async paths, and it never noticed a server closing the connection |
| Tasmota | [#25142](https://github.com/arendst/Tasmota/pull/25142) | Every chunked response through `AsyncHttpClientLight` failed after 5 s |
| arduino-esp32 | [#12990](https://github.com/espressif/arduino-esp32/pull/12990) | `HTTPClient` waited for ever for the rest of a body, and polled without pause between TLS packets |
| ESPEasy | [#5649](https://github.com/letscontrolit/ESPEasy/pull/5649) | Its copy of `HttpClientLight` had the same unbounded wait |
| LibreTiny | [#420](https://github.com/libretiny-eu/libretiny/pull/420) | Its `HTTPClient` had the same unbounded wait |
| Tasmota | [#25180](https://github.com/arendst/Tasmota/pull/25180) | A read timeout over 65.5 s wrapped round: `webclient.set_timeouts(80000)` gave up after 14.5 s |

The details differed enough that each needed its own work:

- **arduino-esp32** reads a body of known length with a blocking `readBytes()`,
  so the Tasmota fix would not have worked there unchanged. Its review then
  turned up busy polling over TLS, which the fix also cures.
- **Tasmota's async client** has its own socket code, which never noticed a
  server closing the connection, and its own chunked reader, which failed at
  the end of every chunked body. Both are fixed, along with the async path
  and a race the maintainer spotted in review.
- **ESPEasy and LibreTiny** carry the original loop, so the fix carried over
  almost as it was.
- **ESPEasy's review** found its timeout kept in 16 bits, so one longer than
  65.5 s wrapped round. Tasmota's clients had the same, fixed in #25180.

**Related:** Berry's `webclient` had a `connected()` method that was written
but never exposed to scripts, which came up while fixing #25141; Tasmota's
maintainer exposed it in [#25145](https://github.com/arendst/Tasmota/pull/25145).

## Firmware updates that never gave up

With the HTTP clients done, we checked the other ways ESP32 firmware
commonly downloads an update:

- **arduino-esp32's `HTTPUpdate`** did give up, but only after 300 empty reads,
  each waiting the 8 s connection timeout first: about 40 minutes of a frozen
  sketch. Measured at 331 s with a 1 s timeout and 631 s with 2 s, exactly as
  predicted; with the fix, 33.8 s.
- **ESP-IDF's `esp_https_ota()`** retried a stalled download for ever: still
  retrying after 35 minutes. ESP-IDF retries timeouts on purpose, so the fix
  changes only the one-call wrapper, leaving step-by-step callers free to
  retry.
- **Tasmota's own OTA** (`Upgrade`) uses its copy of arduino-esp32's updater,
  with the same miscount: a stalled attempt blocked for 40 min 31 s on the
  emulator, and Tasmota tries 4 times, so about 2 h 40 min in safeboot. With
  the fix, each attempt gives up after about 33 s.

Testing that led to one more ESP-IDF bug: telling `esp_https_ota()` to write
into the same partition it downloads to crashed it on a NULL pointer. And
testing Tasmota's OTA showed its web server stuck for minutes after a long
download: requests that had given up while the device was busy were then
answered one by one, each piece of the page waiting 10 s on a client that no
longer answered. The cause was in arduino-esp32's `NetworkClient`.

| Project | Pull request | What it fixes |
|---|---|---|
| arduino-esp32 | [#12991](https://github.com/espressif/arduino-esp32/pull/12991) | A stalled `HTTPUpdate` blocked for about 40 minutes, not the 30 s its code intended |
| Tasmota | [#25177](https://github.com/arendst/Tasmota/pull/25177) | The same in Tasmota's own OTA, attempted 4 times: about 2 h 40 min |
| ESP-IDF | [#19246](https://github.com/espressif/esp-idf/pull/19246) | `esp_https_ota()` retried a stalled download for ever |
| ESP-IDF | [#19243](https://github.com/espressif/esp-idf/pull/19243) | `esp_https_ota()` crashed when the staging and final partitions were the same |
| arduino-esp32 | [#13015](https://github.com/espressif/arduino-esp32/pull/13015) | A write that gave up left the connection open, so a page sent to a client that had stopped answering held the device 10 s per piece |

**Related:** the endless retry had been reported before, in ESP-IDF issue
[#6364](https://github.com/espressif/esp-idf/issues/6364), which was closed
without it being fixed.

## A build failure, followed to its root

Testing the LibreTiny fix on its own toolchain found two more problems: sketches
using its `HTTPClient` did not link (a missing `extern "C"`), and with the
newest PlatformIO no LibreTiny build worked at all. The second came from SCons:
a change in 4.11 made every build script inherit SCons's own
`from __future__ import annotations`, which breaks `dataclasses` `InitVar`
fields. LibreTiny got a workaround and SCons got the root-cause fix, with a
test.

| Project | Pull request | What it fixes |
|---|---|---|
| LibreTiny | [#421](https://github.com/libretiny-eu/libretiny/pull/421) | Any sketch using `HTTPClient` failed to link on Beken and Realtek chips |
| LibreTiny | [#422](https://github.com/libretiny-eu/libretiny/pull/422) | Every build failed with PlatformIO 6.2.0 |
| SCons | [#4924](https://github.com/SCons/scons/pull/4924) | Build scripts were compiled with SCons's own `from __future__ import annotations`, the root cause of LibreTiny's build failure |

## The emulator's Wi-Fi

The [emulator](emulator.md) runs on [lcgamboa's QEMU fork](https://github.com/lcgamboa/qemu),
the one QEMU that emulates the ESP32-C3's Wi-Fi. Making an emulated plug behave
on its network as a real one does took four changes to that Wi-Fi emulation,
and one to its pins, each found because something went wrong on the emulator
where it would not on a plug. They are kept in this repository as patches, and
offered back:

| Project | Pull request | What it fixes |
|---|---|---|
| QEMU (lcgamboa) | [#13](https://github.com/lcgamboa/qemu/pull/13) | A received frame was written into a buffer the guest's driver had taken back, corrupting its heap - a crash when the guest stopped its Wi-Fi |
| QEMU (lcgamboa) | [#14](https://github.com/lcgamboa/qemu/pull/14) | On a tap or bridged network, the guest's frames carried the emulated access point's address, not its own: the host could not tell it apart, nor give it an address by its MAC |
| QEMU (lcgamboa) | [#15](https://github.com/lcgamboa/qemu/pull/15) | The host could not join a network the guest raised itself - Tasmota's or ESPHome's setup network - nor could the guest stay a station while it did |
| QEMU (lcgamboa) | [#16](https://github.com/lcgamboa/qemu/pull/16) | The signal strength reached the firmware 96 too high: Tasmota reported its Wi-Fi at +71 to +78 dBm |
| QEMU (lcgamboa) | [#17](https://github.com/lcgamboa/qemu/pull/17) | Run on its own, every input pin read low, so Tasmota saw its button held and reset itself; the levels can now be set |

#15 let the emulator test what a person does when a plug falls back
to its own setup network: join it, and let the conversion carry on through it.

## OpenBeken on a 4 MB BK7238

The Sonoff S61 is a Beken BK7238 with 4 MB of flash. OpenBeken's BK7238 build
assumed the SDK's 2 MB partition table, which on a 4 MB plug puts its OTA slot
and its config inside the stock app partition - where the stock bootloader
never looks - and the SDK's packager could not produce a 4 MB image at all.
Two changes make the build follow the chip: a `FLASH_SIZE=4M` switch, with the
OTA slot and the flash size read from the partition table the firmware was
built with, and a packager that names its outputs by flash size, sizes the
4 MB app to its partition and packs the image with a 4 MB bootloader - which
turned out to be, byte for byte, the S61's factory bootloader.

| Project | Pull request | What it fixes |
|---|---|---|
| beken_freertos_sdk (NonPIayerCharacter) | [#13](https://github.com/NonPIayerCharacter/beken_freertos_sdk/pull/13) | Packing a 4 MB BK7238 image failed on a `_2M` filename, the 4 MB app profile overran the OTA partition, and no 4 MB bootloader was shipped |
| OpenBeken (OpenBK7231T_App) | [#2290](https://github.com/openshwprojects/OpenBK7231T_App/pull/2290) | No way to build for a 4 MB flash; the REST/web-UI OTA slot, the flash-read and flash-write bounds and LittleFS's bounds were 2 MB constants whatever table the firmware used (on 4 MB, LittleFS inside the app partition) |

Proven in the BekenEmulator on a dump of the owner's own S61: the default
2 MB build unchanged, and the 4 MB build saving its config at `0x3e1000`,
serving flash reads at the top of the chip, and `ota_http` downloading to
`0x286000`, where the bootloader applies it.
[docs/beken-s61-emulation.md](beken-s61-emulation.md) has the detail.

## Smaller fixes found along the way

| Project | Pull request | What it fixes |
|---|---|---|
| Tasmota | [#25163](https://github.com/arendst/Tasmota/pull/25163) | Berry's `webclient` logged a connection timeout's error code as its time: "Connection timeout after -1 ms" |
| Tasmota | [#25169](https://github.com/arendst/Tasmota/pull/25169) | Its setup network went out under the chip's default name (`ESP_` and its MAC) for its first tenth of a second, which phones and computers then list beside the real one |
| ESPHome | [#21672](https://github.com/esphome/esphome/pull/21672) | An `api` action with an array variable that also responds did not compile; our calibration action, which takes a list of floats, had to go without an answer |

## Other people's reports, reproduced on the emulator

With the emulator built, we went through Tasmota's open reports for problems
it could reproduce, and picked those an S60 owner could meet. Two came from
one person changing an ESP32-C3 plug's Wi-Fi (discussions
[#24591](https://github.com/arendst/Tasmota/discussions/24591) and
[#24585](https://github.com/arendst/Tasmota/discussions/24585)), as someone
might after converting a plug. Both were in Tasmota's own setup and settings
pages, beside [#25169](https://github.com/arendst/Tasmota/pull/25169) above.

| Project | Pull request | What it fixes |
|---|---|---|
| Tasmota | [#25183](https://github.com/arendst/Tasmota/pull/25183) | On ESP32, `WifiConfig 2` gave a setup network whose page was the normal main page within a second: no captive portal, and Wi-Fi details sent to it were not taken |
| Tasmota | [#25184](https://github.com/arendst/Tasmota/pull/25184) | A Wi-Fi, web or MQTT password with a `;` saved from a settings page was cut there and the rest run as a command; an unpaired `{` took in the rest of the page |
| Tasmota | [#25185](https://github.com/arendst/Tasmota/pull/25185) | Matter could not encode a string longer than 255 bytes (a typo another user spotted in issue [#24624](https://github.com/arendst/Tasmota/issues/24624)) |

## How each fix was proven

Every fix was tested before and after the change, on the same cases, and the
results are in each pull request. "It compiles" was never enough.

### The emulator

Most of the testing ran on this project's [emulator](emulator.md): a real
Sonoff S60 flash dump, booted in QEMU with emulated Wi-Fi, behind an emulated
home router. It was built so the converter could be tested end to end without
risking a plug, and it turned out to be just as good a test bed for other
people's code:

- **Firmware runs unmodified.** Tasmota builds, plain Arduino sketches and
  ESP-IDF code all run on it as they would on the plug, so a test exercises
  the code that ships.
- **The network can misbehave on purpose.** A small test server sends whole
  bodies, bodies of unknown length, chunked bodies, bodies in pieces seconds
  apart, and bodies that stop part way with the connection left open, over
  plain HTTP or TLS. The emulated router can be restarted under the plug.
- **Several plugs side by side.** Before and after builds ran in parallel on
  separate emulated plugs, and a fix could be repeated on two at once.
- **A hang is cheap.** A test that freezes an emulated plug costs a restart,
  not a trip to the wall socket.

It took several fixes to the QEMU fork to get here (see
[What is changed, and why](emulator.md#what-is-changed-and-why)): real MAC
addresses on the wire, a button that is not held down, a router that can be
restarted, and Wi-Fi frames written only where the hardware would put them -
those that belong upstream are [offered back](#the-emulators-wi-fi).

### Real hardware

The Tasmota and arduino-esp32 fixes ran again on a real S60, with a second
smart plug switching its power so a hung test plug could be power-cycled
without a hand on the plug. Every hang the emulator showed, the real plug
showed too, and every fix behaved the same on both.

### Measuring, not guessing

- **CPU use.** A lowest-priority task counting while it got the CPU showed the
  TLS busy polling in arduino-esp32 (34 counts before the fix, 15,266 after)
  where a watchdog showed nothing.
- **Memory.** Thirty timeouts in a row were checked against thirty normal
  downloads for leaks.
- **Timing.** Expected times were worked out from the code first, then
  measured, so a result that disagreed showed a misunderstanding.
- **Our own mistakes.** When a test turned out to be wrong - an image that was
  never flashed, a fix that had leaked into a "before" build - it was
  re-run, and the PRs only report the re-runs.

## Related

[How the tool is tested](testing.md): the unit tests, the emulator and the
fault tests that found these bugs, and what has been run on real plugs.
