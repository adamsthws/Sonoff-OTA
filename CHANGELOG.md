# Changelog

The conversion tool's own versions (`x.y.z`), not any firmware's. A patch
release fixes something; a minor one adds something; a major one is for a
change that stops a run started on an earlier version from being resumed, or
removes an option.

The version is set in one place, `tools/autoflash/__init__.py`. Merging a new
one to `main` tags it and publishes a release with its notes from here, so add
the section below before changing the number.

## 1.1.0

Keeps going through a router restart or a dropped network, says why when it
cannot, and backs up each plug's Sonoff firmware before it is replaced.

- Backs up the plug's whole flash - Sonoff firmware, settings and eWeLink
  identity - as a stage of its own before Tasmota is written, so it can go
  back to Sonoff over USB-serial one day (`--no-backup` keeps none). Uses the
  installer firmware 4.4.2.
- Keeps each plug's records under its own MAC address.
- Rides out a router restart: the plug waits up to five minutes for its
  Wi-Fi to come back and carries on, and makes no request while it is down.
- When a stage on the plug stops, it says why straight away, in the plug's
  own words, instead of waiting half an hour; and if the plug could not tell
  this computer, its console is read for the reason.
- Notices a plug that has stopped within seconds; no stage waits more than
  eight minutes.
- Tries again by itself, up to three times, a stage that failed for a
  network reason and is safe to repeat.
- Picks up where it left off when the memory-layout rewrite's report was
  lost, or the official startup firmware's install was cut off part way -
  both used to leave a plug that could not be finished.
- Says what to do with a plug that answers nothing - switch it off and on,
  which is safe at that point - and waits for one still finishing a stage
  that was stopped.
- Waiting for you, to join a network say, no longer times out.
- Says when the installer download stopped part way, rather than blaming a
  firewall.
- Windows: sets up Python without progress bars, and no longer crashes
  looking for an installed Python that is not there.
- The emulator: runs several plugs side by side (`--instance N`), and
  restarts the router faithfully (`plug router off/on`).

## 1.0.0

The first numbered release: a stock Sonoff S60 to the official Tasmota, over
the air, with nothing opened or soldered.

- One command from start to finish (`start.sh`, or `start.cmd` on Windows),
  which also sets up the Python it needs.
- The UK S60TPG and the European S60TPF, on stock firmware 1.1.1 and 1.2.0.
- Ends byte for byte identical to a plug flashed over USB with the official
  Tasmota release, and checks that by reading the flash back.
- Resumable at any point, with every step checking the plug before it writes.
- Takes the plug's Wi-Fi from the plug itself; the password is never written
  to this computer's disk.
- Adds one temporary firewall rule for the plug when needed, and removes it
  again however the run ends.
- Retries Safeboot block transfers for up to a minute when the network drops.
- An unattended mode for testing (`--unattended`), and an emulated plug to
  test against (`tools/emulator/`).
- The tool's version in its log, in the test report, and from `--version`.
