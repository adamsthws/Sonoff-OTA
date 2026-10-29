#!/usr/bin/env python3
"""Put the emulated plug in a known state for the prototype's control tests.

    python3 ESPHome/prototype/emulator_flash.py FLASH nvs
    python3 ESPHome/prototype/emulator_flash.py FLASH image IMAGE SLOT

FLASH is the emulator's captures/emulation/state/flash.bin. Switch the plug
off first (`plug off` in the emulator) and on again after (`plug on`).

`nvs` erases the settings partition (0x9000, 64 KiB), so nothing is saved -
no Wi-Fi either - as after a real conversion. `image` does that and writes
IMAGE into the app slot at SLOT (0x20000 or 0x210000): the one the plug last
booted, which the serial log names ("Loaded app from partition at offset").
"""
import sys
from pathlib import Path

flash_path, action = sys.argv[1:3]
flash = bytearray(Path(flash_path).read_bytes())
if len(flash) != 0x400000:
    sys.exit(f"{flash_path} is not a 4 MiB flash image")
flash[0x9000:0x19000] = b"\xff" * 0x10000
if action == "image":
    image = Path(sys.argv[3]).read_bytes()
    slot = int(sys.argv[4], 16)
    if slot not in (0x20000, 0x210000) or len(image) > 0x1F0000:
        sys.exit("the image must go in 0x20000 or 0x210000, and fit its 0x1F0000 bytes")
    flash[slot:slot + len(image)] = image
Path(flash_path).write_bytes(flash)
print("settings erased" + (f"; {len(image):,} bytes written at {sys.argv[4]}" if action == "image" else ""))
