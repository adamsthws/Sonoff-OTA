# ESPHome for the S60

[`sonoff_s60.yaml`](sonoff_s60.yaml) is the ESPHome the conversion installs when ESPHome is
chosen at the start. Nobody converting a plug needs to build it: the tool
installs the pre-built image in [`../artifacts/`](../artifacts/README.md) and
writes the plug's Wi-Fi into it, in memory only.

It is also the config Home Assistant takes when it adopts a converted plug
(`dashboard_import`), so it is kept as one self-contained file: no includes,
and no partition file - an app image does not contain the partition table,
and the plug keeps Sonoff's.

## Changing it

1. Edit `sonoff_s60.yaml`, and raise its `project` `version`.
2. Build it: `esphome compile sonoff_s60.yaml` (ESPHome 2026.9.1 built the current
   image).
3. Publish `.esphome/build/sonoff-s60/build/firmware.ota.bin` as
   `artifacts/esphome-sonoff-s60-imprintable-<version>.bin`, and
   `.esphome/build/sonoff-s60/build/bootloader/bootloader.bin` from the same
   build as `artifacts/esphome-sonoff-s60-bootloader-<version>.bin` (the
   install puts it in place of Sonoff's). Update their rows in
   `artifacts/README.md`, their lines in `SHA256SUMS`, and the pins in
   `tools/autoflash/esphome.py`. `tests/test_published_artifacts.py` checks
   the image can still be imprinted.

Keep the two `S60-IMPRINT-` placeholders as they are: the tool finds and fills
them, and refuses an image in which either appears more than once.

[`prototype/FINDINGS.md`](prototype/FINDINGS.md) records how this route was
worked out and tested.
