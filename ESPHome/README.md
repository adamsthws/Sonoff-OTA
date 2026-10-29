# ESPHome for the S60

[`sonoff_s60.yaml`](sonoff_s60.yaml) is the ESPHome the conversion installs when ESPHome is
chosen at the start. Nobody converting a plug needs to build it: the tool
installs the pre-built image in [`../artifacts/`](../artifacts/README.md) and
writes the plug's Wi-Fi into it, in memory only.

It is also the config Home Assistant takes when it adopts a converted plug
(`dashboard_import`), so it is kept as one self-contained file: no files of
its own included (system headers only), and no partition file - an app image
does not contain the partition table, and the plug keeps Sonoff's.

## What it reports

Power, voltage and current are corrected as Sonoff's own firmware corrects
them, and the diagnostic `Calibration` sensor says how:

- `stock NVS, factor 1.02` (or `1.00`): the plug's factory calibration, read
  at start-up from what Sonoff's firmware left in its memory, and the factor
  its board calls for, read from three of its pins.
- `from backup, factor 1.02` (or `1.00`): Sonoff's memory has lost it - on a
  plug converted through Tasmota, whose new layout clears it - so the
  conversion tool gave it back from the plug's Sonoff backup.
- `missing, factor 1.02` (or `1.00`): no factory calibration is left, and the
  tool had no backup to give it from, so only the board's factor applies.
  That costs little: on the plugs examined so far the factory calibration
  changes nothing below 300 W.

`Energy` is what the meter has counted since the plug started, in Wh,
calibrated the same way. `Daily energy`, `Monthly energy`, `Yearly energy` and
`Total energy` are counted from the same pulses, so they agree with it, and
are kept through restarts and updates; the first three start again at
midnight, on the first of the month and on 1 January. From 1.3.0 or earlier,
they carry on from where they were.

## Changing it

Edit `sonoff_s60.yaml`, then build and publish it with
`python3 tools/update_firmware.py esphome`, as for a new ESPHome release
([docs/updating-firmware.md](../docs/updating-firmware.md)): it raises the
config's `project` `version`, builds it in ESPHome's own Docker image, checks
the image can still be imprinted, and publishes it in `artifacts/`, with its
lines in `SHA256SUMS` and `firmware.json`. ESPHome 2026.10.0 built the current
image.

It keeps the published bootloader unless told otherwise (`--new-bootloader`):
a different one is written to every plug on its next route through D5 or L1,
and a power cut in that second leaves a plug that has to be opened. A change
to the config's app alone leaves it the same (1.1.0's is 1.0.0's).

The `WiFi Signal` sensor is read by the tool, by that name, before it changes
the firmware of a plug on ESPHome: keep the name. From a plug whose ESPHome has
no such sensor (one built before 1.1.0, or from a config without it), the tool
asks the native API for the configuration dump instead, which prints the
signal - unless the API has an encryption key, when it is not read there.

Keep the names of the four energy totals: the first boot after 1.3.0 finds
what each had saved by its name, and Home Assistant keeps its history by it.
Keep the `autoflash_calibration` action's name too: the tool gives the plug's
factory calibration through it (from 1.4.0).

The `autoflash_wifi` action (`s60_wifi` up to 1.1.0) gives the tool the
plug's Wi-Fi name and password when the plug leaves ESPHome: the next
firmware needs them written in, and ESPHome says neither otherwise. Keep its
name. Anything that can use the API can call it - anything on the home
network, while the API has no encryption key - just as it could send the plug
new firmware while OTA has no password. Giving the API a key closes it; the
tool then takes the details from the plug's Sonoff backup, if your computer
has it, or asks for them.

An update password added later (`password:` under `ota:`) is asked for when
the tool takes the plug off ESPHome, or given with `--esphome-ota-password`
or as `AUTOFLASH_ESPHOME_OTA_PASSWORD`.
An API encryption key (`encryption:` under `api:`) is asked for, or given with
`--esphome-api-key` or as `AUTOFLASH_ESPHOME_API_KEY`: the tool then reads the
plug through the encrypted API, and sends its update encrypted where the plug
takes that - as it must where `encryption:` is under `ota:`.

The last `on_boot` lambda also saves the Wi-Fi in use under the one fixed key
ESPHome looks in when a build has no network of its own at start-up. A build
that has one - this image, once the tool writes the Wi-Fi in - saves it under a
key made from its config and ESPHome version instead. A build of this config
with no Wi-Fi in it, such as Home Assistant's ESPHome builder makes when it
adopts the plug, would otherwise find nothing saved, and raise its own network
rather than join yours. Keep it.

Keep the two `AUTOFLASH-IMPRINT-` placeholders as they are (`S60-IMPRINT-` up
to 1.1.0): the tool finds and fills them, and refuses an image in which either
appears more than once.

The plug is named `esphome` and ESPHome's MAC suffix (`esphome-abcdef`), as
every ESPHome the conversion installs is, whatever the plug, and its own
network has that name too; the friendly name says what plug it is. Up to 1.1.0
it was `sonoff-s60-abcdef`, its network `Sonoff-Setup-CDEF`.

[`archive/esphome-prototype/FINDINGS.md`](../archive/esphome-prototype/FINDINGS.md) records how this route was
worked out and tested.

## The S61

[`sonoff_s61.yaml`](sonoff_s61.yaml) is the same config for the Sonoff S61
(a Beken BK7238, on LibreTiny): the same entities, actions and energy totals,
the S61's wiring, and its Wi-Fi and factory calibration read at boot from the
stock firmware's own store, which stays on the plug through every conversion -
so it has no placeholders to fill. The tool installs it as an rt_ota update
the plug's stock bootloader applies. How it is built and what the emulator
validated: [devices/sonoff-s61/esphome.md](../devices/sonoff-s61/esphome.md).
