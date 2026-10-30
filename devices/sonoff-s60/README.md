# Sonoff S60

Sonoff's ESP32-C3 smart plug: the **S60TPG** (UK, Type G) and the **S60TPF**
(Europe, Type E/F), the same board behind different pins. The stock firmware
calls it `SN-ESP32C3-S60-01`. What is the S60's alone lives here or is linked
from here; the tooling it shares with other Espressif plugs is
[`tools/espressif/`](../../tools/espressif/).

| | |
|---|---|
| Chip | ESP32-C3 (4 MB flash) |
| Relay | GPIO4 |
| Status LED | GPIO5, not inverted |
| Power meter | CSE7766 on UART, RX GPIO7, 4800 baud, even parity |
| Button | GPIO9, active low, pull-up |
| Board variant | read from GPIO6, GPIO10 and GPIO19 with pull-ups at start-up; it chooses the meter's correction factor (1.02 or 1.00) |

- **Tasmota**: the official release, unchanged, given this template -
  `{"NAME":"Sonoff S60TPG","GPIO":[1,1,1,1,224,544,1,3104,1,32,1,0,0,0,0,0,0,0,1,1,1,1],"FLAG":0,"BASE":1}` -
  and a settings record the bridge fills the Wi-Fi into, from
  [`artifacts/tasmota-settings-template-*.bin`](../../artifacts/README.md),
  captured from a plug on the pinned release by
  [`tools/espressif/capture_tasmota_template.py`](../../tools/espressif/capture_tasmota_template.py)
  when a release changes its layout
  ([docs/updating-firmware.md](../../docs/updating-firmware.md#1-tasmota)). Its
  names on the network: [docs/device-names.md](../../docs/device-names.md).
- **ESPHome**: [`esphome.yaml`](esphome.yaml); what it reports and how to
  change it: [`esphome.md`](esphome.md). Its path is the `package_import_url`
  a converted plug gives Home Assistant to adopt it from, so moving it means a
  new image (2.3.0 did). The mover that changes a plug's memory layout is
  [`mover.yaml`](mover.yaml).
- **Flash layout**: Sonoff's, kept by Tasmota and ESPHome
  ([`artifacts/sonoff-s60-partition-table.bin`](../../artifacts/README.md)); the
  recovery Safeboot and the repartitioning route are
  [`tools/espressif/safeboot/`](../../tools/espressif/safeboot/).
- **Calibration**: the factory values in the stock NVS and the board's factor,
  read by [`tools/autoflash/calibration.py`](../../tools/autoflash/calibration.py).
- **How it is converted and what happens on the plug**:
  [`docs/what-happens.md`](../../docs/what-happens.md),
  [`docs/technical-details.md`](../../docs/technical-details.md).
