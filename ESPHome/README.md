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
4. Compare the new bootloader with the published one: it must be the same,
   byte for byte, unless replacing it is the point. A different one is written
   to every plug on its next route through D5 or L1, and a power cut in that
   second leaves a plug that has to be opened. A change to the config's app
   alone leaves it the same (1.1.0's is 1.0.0's).

The `WiFi Signal` sensor is read by the tool, by that name, before it changes
the firmware of a plug on ESPHome: keep the name. A plug whose ESPHome has no
such sensor (one built before 1.1.0, or from a config without it) is simply
not read there.

The `s60_wifi` action gives the tool the plug's Wi-Fi name and password when
the plug leaves ESPHome: the next firmware needs them written in, and ESPHome
says neither otherwise. Keep its name. Anything that can use the API can call
it - anything on the home network, while the API has no encryption key - just
as it could send the plug new firmware while OTA has no password. Giving the
API a key closes it; the tool then takes the details from the plug's Sonoff
backup, if this computer has it, or asks for them.

The last `on_boot` lambda also saves the Wi-Fi in use under the one fixed key
ESPHome looks in when a build has no network of its own at start-up. A build
that has one - this image, once the tool writes the Wi-Fi in - saves it under a
key made from its config and ESPHome version instead. A build of this config
with no Wi-Fi in it, such as Home Assistant's ESPHome builder makes when it
adopts the plug, would otherwise find nothing saved, and raise its own network
rather than join yours. Keep it.

Keep the two `S60-IMPRINT-` placeholders as they are: the tool finds and fills
them, and refuses an image in which either appears more than once.

[`prototype/FINDINGS.md`](prototype/FINDINGS.md) records how this route was
worked out and tested.
