# Sonoff → Tasmota, OpenBeken, ESPHome
> **Firmware Flasher**  
(Fully OTA - 'Over The Air' - I.e Flash over Wi-Fi)

<br>

[![Runs on Linux, macOS and Windows](https://img.shields.io/badge/Runs%20on-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey)](#what-it-supports)

[![Installs ESPHome](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fadamsthws%2FSonoff-OTA%2Fmain%2Ffirmware.json&query=%24.esphome.image.esphome&label=Installs%20ESPHome&color=blue)](docs/updating-firmware.md) [![Installs Tasmota](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fadamsthws%2FSonoff-OTA%2Fmain%2Ffirmware.json&query=%24.tasmota.version&label=Installs%20Tasmota&color=blue)](docs/updating-firmware.md) [![Installs OpenBeken](https://img.shields.io/badge/Installs%20OpenBeken-Coming%20Soon-blue)](docs/updating-firmware.md)

[![Release](https://img.shields.io/github/v/release/adamsthws/Sonoff-OTA?label=Release)](https://github.com/adamsthws/Sonoff-OTA/releases/latest) [![Tests](https://img.shields.io/github/actions/workflow/status/adamsthws/Sonoff-OTA/emulator-check.yml?branch=main&label=Tests)](https://github.com/adamsthws/Sonoff-OTA/actions/workflows/emulator-check.yml) [![License: GPL-3.0](https://img.shields.io/github/license/adamsthws/Sonoff-OTA?label=License)](LICENSE)

[![Discussions](https://img.shields.io/badge/dynamic/regex?url=https%3A%2F%2Fimg.shields.io%2Fgithub%2Fdiscussions%2Fadamsthws%2FSonoff-OTA.json&search=(%5Cd%2B)%20total&replace=%241&label=Discussions&color=blue)](https://github.com/adamsthws/Sonoff-OTA/discussions) [![Issues](https://img.shields.io/badge/dynamic/regex?url=https%3A%2F%2Fimg.shields.io%2Fgithub%2Fissues%2Fadamsthws%2FSonoff-OTA.json&search=(%5Cd%2B)%20open&replace=%241&label=Issues&color=blue)](https://github.com/adamsthws/Sonoff-OTA/issues) [![Pull Requests](https://img.shields.io/badge/dynamic/regex?url=https%3A%2F%2Fimg.shields.io%2Fgithub%2Fissues-pr%2Fadamsthws%2FSonoff-OTA.json&search=(%5Cd%2B)%20open&replace=%241&label=Pull%20Requests&color=blue)](https://github.com/adamsthws/Sonoff-OTA/pulls) [![Downloads](https://img.shields.io/github/downloads/adamsthws/Sonoff-OTA/total?label=Downloads&color=blue)](https://github.com/adamsthws/Sonoff-OTA/releases)

<br>

## Keep this tool alive and growing  
Your donation goes toward expansion, supporting many more devices - Thankyou!  


[![GitHub Sponsors](https://img.shields.io/badge/GitHub%20Sponsors-Support-EA4AAA?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/adamsthws) [![Ko-fi](https://img.shields.io/badge/Ko--fi-Support-FF5E5B?logo=ko-fi&logoColor=white)](https://ko-fi.com/adamsthws) [![PayPal](https://img.shields.io/badge/PayPal-Support-00457C?logo=paypal&logoColor=white)](https://www.paypal.me/adamsthws)  

> Sponsor through GitHub and a sponsor badge appears beside your name in this repository's issues and discussions, and the Sponsor achievement on your profile.

<br>

### Liberation From The Cloud

Your device answers to you alone, on your own network.
> Removes dependence on external cloud servers. After flashing you'll no longer require a cloud account or third party app, and even after a cloud service is switched off your device keeps working and remains fully under your control.

<br>

### Any Direction

And every route between them.
> Stock Sonoff to Tasmota, OpenBeken or ESPHome, either to the other, and back to Sonoff's own firmware if you change your mind.

<br>


![Sonoff (eWeLink), Tasmota or OpenBeken, and ESPHome: any one converts to any other](docs/FirmwareRoutes.svg)

<br>

### Fully automated 'Over The Air' firmware conversion

Turn your stock Sonoff S60/S61 smart plug into one running **Tasmota**, **OpenBeken** or **ESPHome** using OTA method - no soldering, no taking the plug apart. Move the plug between the two, or back to Sonoff's own firmware.

> Conversion with this tool is fully automated end to end, with checks at every stage. It is resumable and can be restarted at any point.

<br>

### No More Soldering

Until now, the established way to put Tasmota on an S60 was to open it, perform some soldering and
flash it over USB-serial - hard work, because the case is plastic-welded shut.

> The community threads for the soldering method:
>
> - UK version: [arendst/Tasmota#22554](https://github.com/arendst/Tasmota/discussions/22554)
> - EU version: [arendst/Tasmota#21255](https://github.com/arendst/Tasmota/discussions/21255)

<br>

### Indistinguishable

Converted plugs end up identical to a plug flashed over USB
> The final automated checks verify this by reading their flash back at the end of the conversion. Every block of it is read back before the plug switches to it.

<br>

## What you need

**The plug** 
> A Wi-Fi Sonoff S60, e.g:  
UK: **S60TPG** | European: **S60TPF**  
(Not the Zigbee S60)

**An eWeLink account**
> Free. A throwaway one is fine. The plug has to be added to it first.

**A computer**
> Linux, macOS or Windows, with Wi-Fi.

**Administrator rights**
> Required to auto-add one temporary firewall rule (allowing the plug to communicate with the computer, removed when the run finishes).

<br>

## What it supports
> Support for more devices coming soon.

**Model**

- ✅ **Sonoff S60TPG** - UK / Type G
- ✅ **Sonoff S60TPF** - EU / Type E/F

**Stock firmware**

- ✅ 1.1.1
- ✅ 1.2.0

**Computer**

- ✅ Linux
- ✅ Windows
- ✅ macOS

<br>

## Let's go

> You're walked through every step of the conversion.

**Linux / MacOS**  (Terminal)
```sh
git clone https://github.com/adamsthws/Sonoff-OTA.git
cd Sonoff-OTA
sh start.sh
```
**Windows** (Terminal)

```sh
git clone https://github.com/adamsthws/Sonoff-OTA.git
cd Sonoff-OTA
.\start.ps1
```
  -- or -- 
  [download as a zip](https://github.com/adamsthws/Sonoff-OTA/archive/refs/heads/main.zip), double-click `start.cmd`  

<br>

## What you'll see

**Tasmota:**

![A successful conversion to Tasmota](docs/SuccessRun.svg)

**ESPHome:**

![A successful conversion to ESPHome](docs/SuccessRunESPHome.svg)

<br>

## If you have to stop

E.g. you close the window, lose your Wi-Fi, run out of time - it doesn't matter. Run the
same command again - `start.cmd` or `sh start.sh`.

> It remembers which plug it was working on and what it had done. Where a step
was interrupted, it checks the plug to work out where to resume from.

<br>

## What you end up with

**Tasmota:** Official Tasmota

**ESPHome:** Official ESPHome

**Sonoff:** A full backup of the plug's Sonoff firmware to restore later.

> Afterward, Tasmota and ESPhome updates are applied as normal.

<br>

## If something goes wrong

Start with **[docs/troubleshooting.md](docs/troubleshooting.md)** - it covers
the problems that actually came up while this was being built.

<br>

## Reporting how it went

Whether it worked first time or stopped partway, please
report back on how it went here:  
- **[Conversions In The Wild (Conversion Reports)](https://github.com/adamsthws/Sonoff-OTA/discussions/1)**

> A clean conversion, a run that stopped partway, or only the read-only checks - all of it is useful. 

> Reading the thread is the quickest way to see how it went for others.

<br>

## More detail

- **[docs/technical-details.md](docs/technical-details.md)** - the firmware
  bundle, secrets handling, memory layout, notes for devs.
- **[docs/what-happens.md](docs/what-happens.md)** - how it works and what
  happens to the plug, step by step.
- **[docs/troubleshooting.md](docs/troubleshooting.md)** - problems met so far
  and their fixes.
- **[docs/emulator.md](docs/emulator.md)** - the whole conversion run against
  an emulated s60 plug.
- **[docs/unattended.md](docs/unattended.md)** - running a conversion with
  nobody at the keyboard - for testing.
- **[docs/testing.md](docs/testing.md)** - how the tool is tested: unit tests,
  the emulator, every fault in the way of every step, and real plugs.
- **[docs/updating-firmware.md](docs/updating-firmware.md)** - how the
  Tasmota and ESPHome the tool installs are moved to each new release.
- **[docs/private-backup.md](docs/private-backup.md)** - for the maintainer:
  how their own Sonoff backup is kept sealed and private, off their computer.
- **[docs/device-names.md](docs/device-names.md)** - the name the plug goes by
  at each stage, where each is set, and how to change them.
- **[docs/upstream-contributions.md](docs/upstream-contributions.md)** - bugs
  found and fixed while building this project.
- **[archive/docs/reference/](archive/docs/reference/)** - the original
  research, the manual procedure and how the firmware format was decoded.
- **[docs/legal.md](docs/legal.md)** - how the project keeps backing up
  Sonoff's firmware lawful, and what it asks of you.

<br>

## Thanks

A big thank you to [HG0161](https://github.com/HG0161) and the
[SonoffS60OTA](https://github.com/HG0161/SonoffS60OTA) repository for laying the groundwork; working out the update mechanism, decoding the firmware format
and proving the OTA route on real plugs. This repository carries that work on.

Thanks to [p1otr](https://github.com/p1otr) for the ESPHome option and its
S60 config.

Thanks too to everyone in the Tasmota threads for the
[UK](https://github.com/arendst/Tasmota/discussions/22554) and
[EU](https://github.com/arendst/Tasmota/discussions/21255) S60, whose
open-the-case method came first.

Worked out with the Tasmota community's discussion of this device.

See [`LICENSE`](LICENSE) and [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
