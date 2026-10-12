#! /bin/sh
set -e

esphome compile ESPHome.yaml

../tools/s60_autoflash.py run --esphome ./ESPHome/.esphome/build/s60tpf/build/firmware.ota.bin
