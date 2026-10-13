# S60 one-shot OTA bridge

This is a native ESP-IDF ESP32-C3 application, not a factory image. It is
designed to be wrapped with `tools/build_vendor_ota.py` and written by the
stock S60 updater to its inactive application slot.

Bridge v3 first confirms itself so ESP-IDF rollback cannot later discard the
recovery slot, then selects the peer application for the next boot. If Wi-Fi or
the HTTP server fails, a power cycle returns to that peer. Immediately before
an upload erases the peer, the bridge selects itself instead. It selects the
new image only after `esp_ota_end` validates it.

The remaining irreducible risk is failure before `app_main` executes. The stock
1.2.0 bootloader was built without ESP-IDF application rollback, so no software
running in an app partition can eliminate that risk.

Bridge v5 added two things. It reads the Wi-Fi the stock firmware saved and
joins that network, so the run finds it where the plug was. And it takes
Tasmota's settings template, fills in the Wi-Fi it joined with, and saves it
where Tasmota reads its settings, so Tasmota joins by itself. The password
leaves the plug only inside the backup the run reads through `GET /flash`.

Bridge 4.4.2, the one the run installs, joins the stock network alone and
raises its own network only if it has had no address there for 45 seconds, at
start-up or later. v5 ran both at once on one radio, and on a plug the joined
network dropped out for seconds at a time while its own was up; tried on the
same plug, 4.4.1 (then called "v6") stayed on for ten minutes without one.
Where stock saved no Wi-Fi it raises its own network at once, as v3 did.

From 4.4.1 on, a bridge is named by one version: `BRIDGE_VERSION` in
`main/main.c` and `PROJECT_VER` in `CMakeLists.txt` (a test keeps them equal)
are the version the stock updater is given and the one `/status` reports.
Change both for the next bridge, 4.4.3.

From 4.4.2 every build has the read-only `GET /flash` (the whole chip, with
its size in `X-Flash-Size`), `GET /flash-sha256` (the SHA-256 of the whole
chip, worked out on the plug) and `GET /efuse` (the eFuse read registers), so
the run keeps a backup of the Sonoff firmware, checked byte for byte against
the plug, before Tasmota is written.

`sdkconfig.defaults` holds the settings v3 was built with; with them a rebuild
matches the published v3 except for the build time, date and ELF hash. Keep
`CONFIG_ESP_WIFI_NVS_ENABLED=n`: the bridge never initialises NVS, so with it
on `esp_wifi_init()` fails and the bridge aborts at start-up (the plug then
falls back to stock by itself). Before rebuilding, delete the generated
`sdkconfig.s60-ota-bridge*` files, or old settings are reused - but never
`sdkconfig.defaults`: a `sdkconfig.*` wildcard removes it too, the build
then silently turns Wi-Fi NVS back on, and the bridge downloads in full but
never starts. Check a rebuild against the published bridge: built from the
same source it differs only in the build time and date, the ELF hash and the
image checksum.

Build with PlatformIO from a path without spaces (the ESP-IDF builder used here
does not correctly quote every generated CMake path):

```sh
cp -a archive/bridge /tmp/s60-bridge-build
rm -f /tmp/s60-bridge-build/sdkconfig.s60-ota-bridge*
pio run -d /tmp/s60-bridge-build -e s60-ota-bridge
```

The application image is then:

```text
/tmp/s60-bridge-build/.pio/build/s60-ota-bridge/firmware.bin
```

Wrap the application for the stock updater - the version comes from the
image - and validate it:

```sh
python3 tools/build_vendor_ota.py \
  /tmp/s60-bridge-build/.pio/build/s60-ota-bridge/firmware.bin \
  artifacts/s60-ota-bridge-4.4.2.ota
python3 tools/analyze_vendor_ota.py artifacts/s60-ota-bridge-4.4.2.ota
```

When it cannot join the stock network, or stock saved none, it creates
`S60-OTA-Bridge-XXXX`, protected with `s60-ota-bridge`. Browse to `http://192.168.4.1/` to upload a native ESP32-C3
application image. The browser sends the selected file as a raw request body;
JavaScript must be enabled.

The research env `s60-ota-bridge-flash-readout` images a stock plug without
opening it or converting it: it has the same `GET /flash` and `GET /efuse`,
and leaves out `/update` and `/tasmota-settings`, so the only flash it writes
is the startup otadata selection that returns the plug to stock on the next
power cycle. The stock updater writes it over the inactive slot, so whatever
was there (often the previous Sonoff version) is lost; the running stock
version, bootloader, partition table and NVS are kept. Build it with
`pio run -e s60-ota-bridge-flash-readout`, wrap it as above into
`captures/flash-readout/s60-ota-bridge-flash-readout-4.4.2.ota`, then run
`tools/s60_readout.py`: it installs it, reads and checks the dump, and walks
you through putting the plug back to stock.

Do not deploy this bridge until its build artifact and wrapped OTA file pass
the offline validators and a compatible final Tasmota image has been prepared.
