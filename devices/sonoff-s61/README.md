# Sonoff S61

Sonoff's Beken smart plug, sold as the **S61S** (iPlug Gen2, Matter over
Wi-Fi), model **S61STPF** for the European plug. The firmware calls itself
`S61`: its eWeLink model is `SN-BK7238-S61-01`, its board id
`FWSW-01-S61-BK`, and the module is the `WBBK_01P_V1.3`. What is the S61's
alone lives here or is linked from here; the tooling it shares with other
Beken plugs is [`tools/beken/`](../../tools/beken/), and the emulation that
worked all of this out is [`docs/beken-s61-emulation.md`](../../docs/beken-s61-emulation.md).

| | |
|---|---|
| Chip | BK7238 (ARM968E-S), 4 MB flash |
| Relay | P26 |
| Button | P9, active low, pull-up |
| Relay LED, Wi-Fi LED | P0, P1, both lit low |
| Power meter | BL0937: CF P8, CF1 P6, SEL P24; 1 mΩ shunt, 2040:1 divider |
| Stock stores | EasyFlash V4 at `0x3e2000` (Wi-Fi, `elecCalibCoef`) and `0x3f6000`; never written by a conversion |
| Stock bootloader table | app `0x11000`, download slot `0x286000` (where it applies an rt_ota update), radio calibration `0x3e0000` |

- **OpenBeken**: [`drv_sonoff_s61_import.c`](drv_sonoff_s61_import.c), the
  module that reads the stock Wi-Fi and meter calibration on first boot and
  applies the wiring; the build recipe, the 4 MB table and reverting to stock:
  [`openbeken.md`](openbeken.md). The 4 MB patches offered
  upstream are [`tools/beken/openbeken/`](../../tools/beken/openbeken/).
- **ESPHome** (LibreTiny): [`esphome.yaml`](esphome.yaml), the path converted
  plugs will name as their `package_import_url`; how it differs from the S60's, the build, the reversion and what the
  emulator validated: [`esphome.md`](esphome.md).
- **Stock firmware**: the public dump upstream BekenEmulator publishes is
  what the emulator and its tests run; this repository holds no Sonoff
  firmware ([`tools/beken/emulator/`](../../tools/beken/emulator/)).
- **The images the tool installs**: OpenBeken with the module and the 4 MB
  table (`artifacts/openbeken-sonoff-s61-<version>.rbl`) and ESPHome
  (`artifacts/esphome-sonoff-s61-<version>.rbl`), built and pinned by
  `tools/repo/update_firmware.py` ([docs/updating-firmware.md](../../docs/updating-firmware.md)).
- **The conversion tool's route**: [`tools/autoflash/routes/beken/`](../../tools/autoflash/routes/beken/)
  - the push through eWeLink from Sonoff's firmware, OpenBeken and ESPHome each
  taking the next firmware as an update of its own, Sonoff's firmware back
  from the public dump, the backup through OpenBeken after the first
  conversion. Written against the emulator's findings and tested against
  stand-ins; not yet run end to end in the emulator, and never on a real plug.
