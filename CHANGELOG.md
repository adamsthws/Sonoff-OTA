# Changelog

The conversion tool's own versions (`x.y.z`), not any firmware's. A patch
release fixes something; a minor one adds something; a major one is for a
change that stops a run started on an earlier version from being resumed, or
removes an option.

The version is set in one place, `tools/autoflash/__init__.py`. Merging a new
one to `main` tags it and publishes a release with its notes from here, so add
the section below before changing the number.

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
