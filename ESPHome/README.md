# flashing ESPHome

`--esphome firmware.ota.bin` enables optional path to flash an ESPHome
application. The run stops after interim Tasmota on the *stock* partition table
(no Safeboot / E3 repartition) and uploads this image via Tasmota OTA. 

Build ESPHome against a partitions_sonoff_stock.csv that matches stock S60
offsets (ota_0 at 0x20000, ota_1 at 0x210000, size 0x1f0000 each).
Using the default ESPHome table will boot poorly or break later OTAs.

Use firmware.ota.bin (app/OTA image), not firmware.factory.bin.

Example s60.yaml and partitions file are provided in this directory.

`esphome compile s60.yaml
../start.sh run --esphome .esphome/build/sonoff-s60/build/firmware.ota.bin
`
