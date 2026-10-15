# Fixes sent upstream

Building this converter meant pushing ESP32 firmware, HTTP clients and OTA
code harder than most projects do: downloading firmware in pieces, over Wi-Fi
that is allowed to drop at any moment, with a plug that has to survive it. When
something went wrong and the cause was not ours, we chased it into the project
it came from, fixed it there, and sent the fix back.

This page lists those fixes and how each was proven.

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

## The fixes

| Project | Pull request | What it fixes |
|---|---|---|
| Tasmota | [#25139](https://github.com/arendst/Tasmota/pull/25139) | `HttpClientLight` waited for ever for the rest of a body |
| Tasmota | [#25141](https://github.com/arendst/Tasmota/pull/25141) | The same in `AsyncHttpClientLight`, in its sync and async paths, and it never noticed a server closing the connection |
| Tasmota | [#25142](https://github.com/arendst/Tasmota/pull/25142) | Every chunked response through `AsyncHttpClientLight` failed after 5 s |
| arduino-esp32 | [#12990](https://github.com/espressif/arduino-esp32/pull/12990) | `HTTPClient` waited for ever for the rest of a body, and polled without pause between TLS packets |
| arduino-esp32 | [#12991](https://github.com/espressif/arduino-esp32/pull/12991) | A stalled `HTTPUpdate` blocked for about 40 minutes, not the 30 s its code intended |
| ESP-IDF | [#19243](https://github.com/espressif/esp-idf/pull/19243) | `esp_https_ota()` crashed when the staging and final partitions were the same |
| ESP-IDF | [#19246](https://github.com/espressif/esp-idf/pull/19246) | `esp_https_ota()` retried a stalled download for ever |
| ESPEasy | [#5649](https://github.com/letscontrolit/ESPEasy/pull/5649) | Its copy of `HttpClientLight` had the same unbounded wait |
| LibreTiny | [#420](https://github.com/libretiny-eu/libretiny/pull/420) | Its `HTTPClient` had the same unbounded wait |
| LibreTiny | [#421](https://github.com/libretiny-eu/libretiny/pull/421) | Any sketch using `HTTPClient` failed to link on Beken and Realtek chips |
| LibreTiny | [#422](https://github.com/libretiny-eu/libretiny/pull/422) | Every build failed with PlatformIO 6.2.0 |
| SCons | [#4924](https://github.com/SCons/scons/pull/4924) | Build scripts were compiled with SCons's own `from __future__ import annotations`, the root cause of LibreTiny's build failure |

### One bug, five HTTP clients

Tasmota's `HttpClientLight` is a copy of arduino-esp32's `HTTPClient`, and so
are ESPEasy's `HttpClientLight` (copied from Tasmota's) and LibreTiny's
`HTTPClient`. All of them read a body in a loop that only stopped when the
connection closed or the body was complete. The fix is the same idea in each:
give up once no data has arrived for the client's timeout, as they already did
for the headers. A body that keeps arriving, however slowly, still completes.

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

### OTA updates that never give up

With the HTTP clients done, we checked the two other ways ESP32 firmware
commonly downloads an update:

- **arduino-esp32's `HTTPUpdate`** did give up, but only after 300 empty reads,
  each waiting the 8 s connection timeout first: about 40 minutes of a frozen
  sketch. Measured at 331 s with a 1 s timeout and 631 s with 2 s, exactly as
  predicted; with the fix, 33.8 s.
- **ESP-IDF's `esp_https_ota()`** retried a stalled download for ever: still
  retrying after 35 minutes. ESP-IDF retries timeouts on purpose, so the fix
  changes only the one-call wrapper, leaving step-by-step callers free to
  retry.

Testing that led to one more ESP-IDF bug: telling `esp_https_ota()` to write
into the same partition it downloads to crashed it on a NULL pointer.

### A build failure, followed to its root

Testing the LibreTiny fix on its own toolchain found two more problems: sketches
using its `HTTPClient` did not link (a missing `extern "C"`), and with the
newest PlatformIO no LibreTiny build worked at all. The second came from SCons:
a change in 4.11 made every build script inherit SCons's own
`from __future__ import annotations`, which breaks `dataclasses` `InitVar`
fields. LibreTiny got a workaround and SCons got the root-cause fix, with a
test.

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
restarted, and Wi-Fi frames written only where the hardware would put them.

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
