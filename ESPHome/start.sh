#! /bin/sh
set -e

esphome compile s60.yaml

../tools/s60_autoflash.py run --esphome ./ESPHome/.esphome/build/sonoff-s60/build/firmware.ota.bin
