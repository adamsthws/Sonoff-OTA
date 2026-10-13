# Sonoff S60 → Tasmota, OTA
## Fully automated 'Over The Air' conversion

![A successful conversion run](docs/SuccessRunScreenshot.png)

Turns a stock Sonoff S60 smart plug into one running **Tasmota** using
OTA method - no soldering, no taking the plug apart.

Until now, the established way to put Tasmota on an S60 was to open it and
flash it over USB-serial - hard work, because the case is plastic-welded shut.
The community threads for that method:

- UK version: [arendst/Tasmota#22554](https://github.com/arendst/Tasmota/discussions/22554)
- EU version: [arendst/Tasmota#21255](https://github.com/arendst/Tasmota/discussions/21255)

Conversion with this tool is fully automated end to end, with checks at every stage.  
It is resumable and can be restarted at any point.

Converted plugs end up byte-for-byte identical to a plug flashed over USB with
the official Tasmota release, the final automated checks verify this by reading their flash back at the end of the cenversion.

---

## Read this before you start

Please only do this with a plug you can afford to lose.

If the power is cut during memory write, the plug may not start again, and no amount of Wi-Fi cleverness
will bring it back - it would then need opening and a USB-serial adapter to recover.

**This is not official Sonoff software.** Converting the plug ends
any support or warranty you had, and the eWeLink app will no longer control it.

---

## What you need

| | |
|---|---|
| **The plug** | A Wi-Fi Sonoff S60 (ESP32-C3 inside): the UK **S60TPG** or the European **S60TPF**. Not the Zigbee S60 (S60ZB...), which has a different chip. |
| **An eWeLink account** | Free. A throwaway one is fine. The plug has to be added to it first. |
| **A computer** | Linux, macOS or Windows, with Wi-Fi. It needs Python 3.10 or newer, which the start script installs if need be. |
| **Administrator rights** | If the computer's firewall is on, the tool requests to add one temporary firewall rule (allowing the plug to communicate with the computer), and removes it when the run finishes - Adding this rule requires an admin/sudo elevation.  |

> The plug must be **allowed internet access** during the run: the conversion is sent to it
through eWeLink. If you normally keep it LAN-only, allow it for the run and block it again
afterwards - Tasmota needs no cloud.

### Wi-Fi

Your Wi-Fi must be **2.4 GHz** - the plug cannot use 5 GHz.

The plug keeps the Wi-Fi it was set up with in the eWeLink app, so set it up
there on the 2.4 GHz network it should stay on.  

[How your Wi-Fi details are handled](docs/technical-details.md#how-your-wi-fi-details-are-handled)

## What it has been run on

**Model**

- ✅ **S60TPG** - UK / Type G
- ✅ **S60TPF** - EU / Type E/F

**Stock firmware**

- ✅ 1.1.1
- ✅ 1.2.0

**Computer**

- ✅ Linux
- ✅ Windows
- ⬜ macOS (expected to work)

> ### MacOS
>
> If you are on Mac, please let us know how it went.
A report from a first run is what moves macOS
from "written for" to tested, so the next person can follow
knowing they have worked for someone already, and anything that
trips you up gets fixed for everyone after you.

## Doing it



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

- Run the same command for every plug: it carries on an unfinished run, or
starts a new one. 

- The you're walked through every step of the conversion.

- Finding the plug, backing up its Sonoff firmware,
installing Tasmota, rebuilding its memory and checking the result - happens automatically.

### If you have to stop

E.g. you close the window, lose your Wi-Fi, run out of time - it doesn't matter. Run the
same command again - `start.cmd` or `sh start.sh`.

It remembers which plug it was working on and what it had done. Where a step
was interrupted, it checks the plug to work out where to resume from.

## What you end up with

Official Tasmota, laid out in memory exactly as if the plug had been flashed
over USB. That means normal Tasmota upgrades from then on.

 [layout itself](docs/technical-details.md#the-finished-memory-layout) is
in the technical details.

You also keep a full backup of the plug's Sonoff firmware.

## When something goes wrong

Start with **[docs/troubleshooting.md](docs/troubleshooting.md)** - it covers
the problems that actually came up while this was being built.

## How it works

The tool sends the plug the same kind of update command
Sonoff uses, but pointing at your computer, which hands it Tasmota.  

[What actually happens to your plug](docs/what-happens.md)

## Reporting how it went

Whether it worked first time or stopped partway, please
report back on how it went here:  
- **[Test reports](https://github.com/adamsthws/Sonoff-OTA/discussions/1)**

> A clean conversion, a run that stopped partway, or only the read-only checks - all of it is useful. 

> Reading the thread is the quickest way to see what has
worked for others.


## Going on to ESPHome

> Coming soon: Sonoff to ESPhome direct conversion

Once the plug is running Tasmota, it is an ordinary Tasmota device, so if
you would rather run **ESPHome** you can follow ESPHome's own standard
route from there:
[Migrating from Tasmota](https://esphome.io/guides/migrate_sonoff_tasmota/).
It is also done over the air, through Tasmota's Firmware Upgrade page.

## More detail

- **[docs/technical-details.md](docs/technical-details.md)** - the firmware
  bundle, how secrets are handled, the finished memory layout, and notes for
  developers.
- **[docs/what-happens.md](docs/what-happens.md)** - how it works and what
  happens to the plug, step by step.
- **[docs/troubleshooting.md](docs/troubleshooting.md)** - problems met so far
  and their fixes.
- **[docs/unattended.md](docs/unattended.md)** - running a conversion with
  nobody at the keyboard, for testing, and keeping the screen it drew.
- **[archive/docs/reference/](archive/docs/reference/)** - the original
  research, the manual procedure and how the firmware format was decoded.

## Thanks

This is a fork of [HG0161/SonoffS60OTA](https://github.com/HG0161/SonoffS60OTA),
which did the groundwork of working out the OTA route.

A big thank you to [HG0161](https://github.com/HG0161) and the original
[SonoffS60OTA](https://github.com/HG0161/SonoffS60OTA) repository for working out the update mechanism, decoding the firmware format
and proving the OTA route on real plugs. This repository carries that work on.

Thanks too to everyone in the Tasmota threads for the
[UK](https://github.com/arendst/Tasmota/discussions/22554) and
[EU](https://github.com/arendst/Tasmota/discussions/21255) S60, whose
open-the-case method came first.

Worked out with the Tasmota community's discussion of this device, and built on
Tasmota itself. Tasmota is GPL-3.0-only; so is this.

See [`LICENSE`](LICENSE) and [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
