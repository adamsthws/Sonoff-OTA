# The S61's emulator

[BekenEmulator](https://github.com/openshwprojects/BekenEmulator) at the commit
`UPSTREAM` pins, with this project's patches and the Python it imports, as a
container image - the way the S60's emulator is kept (`tools/espressif/emulator`). The
tools in `tools/beken` run in it:

```sh
tools/beken/emulator/bk-emu.sh python tools/beken/seed_join.py
tools/beken/emulator/bk-emu.sh run captures/your-dump.bin --wifi-peer auto
tools/beken/emulator/bk-emu.sh test -k "Sonoff 4MB"    # upstream's self-tests, under the patches
tools/beken/emulator/bk-emu.sh plug                     # the S61 on a home Wi-Fi of its own
tools/beken/emulator/bk-emu.sh computer tools/autoflash/cli.py run   # the tool, on that Wi-Fi
tools/beken/emulator/bk-emu.sh power cycle              # the plug switched off and on
tools/beken/emulator/bk-emu.sh reset                    # the plug as new, on Sonoff's firmware again
tools/beken/emulator/bk-emu.sh ui                       # OpenBeken's page, at http://127.0.0.1:8088/
```

`bk-emu.sh plug` runs the public S61 dump as a plug provisioned on a home
Wi-Fi, on a Docker network of its own (`autoflash-beken`, apart from the
S60's emulator and everything else). The fork's `--net-tap` puts the plug's
radio on a TAP device, which `plug-net.sh` bridges with the container's
interface; the container is that Wi-Fi's router, `.1`: DHCP (the plug at
`.50`-`.59`), DNS (every name the router), and eWeLink, the local stub
(`tools/beken/cloud_stub.py`), which every connection the plug makes off the
network is sent to - nothing reaches the internet. `tools/beken/plug.py` is
the plug itself: it boots from its flash, kept in
`captures/emulation/s61/autoflash-s61/` with its logs and the stub's CA, so it
carries on across firmware - each restart the firmware asks for a boot of its
own, from the bootloader, as on a real plug - and Sonoff's firmware is helped
online (`cloud_online.StockHooks`) only while it is the one that starts.

`bk-emu.sh computer` is a computer on that Wi-Fi: the tool there finds the
plug as on a home network, and its eWeLink is the stub. It works in its own
copy of this checkout (the plug's `work/`), so its runs' captures are its own;
an unattended run takes its eWeLink account from `work/captures/` (a made-up
one: the stub takes any). Sonoff's firmware, having fetched and staged a new
image whole and checked its CRC, restarts a second later, and the bootloader
installs it, as a real plug's does (the firmware's own code says so,
[docs/beken-s61-emulation.md](../../../docs/beken-s61-emulation.md)). A plug
that does not come back is switched off and on - `bk-emu.sh power cycle`, as
the tool then asks; an attended run there offers to do it itself: the plug's
power switch is passed to the computer (`AUTOFLASH_EMULATOR_POWER`).

**The plug's web page goes through a proxy** (`bk-emu.sh ui`, `ui_proxy.py`),
OpenBeken's or ESPHome's - `ui` tells which by the name the plug gave its
router - never opened straight. The emulated plug runs the real OpenBeken, but far
slower than a chip: each request leaves a connection in TIME_WAIT on the plug,
lwIP has room for only a few, and on the emulator's clock they clear slowly. A
browser's parallel requests and the page's once-a-second polling fill them in
seconds, and the plug stops answering though its firmware still runs. The proxy
lets one request at a time reach the plug, at most one every 0.8 s, answers the
page's polling from a 3-second cache, and passes every action straight on;
[`ui_proxy.py`](ui_proxy.py) says more. ESPHome's page keeps its live values
coming on one open connection (`/events`), which the proxy passes straight
through, one browser tab at a time.

Each ensures the image first: one labelled with the hash of what it is built
from (`bk-emu.sh tag`: `UPSTREAM`, `patches/`, `requirements.txt`,
`bk-emu.Dockerfile`), pulled from `ghcr.io/adamsthws/autoflash-beken-emu:<hash>`,
which the `beken-emulator-image` workflow publishes for every version pushed,
or built where it is not published - from the upstream source archive on this
repository's release
[`emulator-source-beken-d9a3772`](https://github.com/adamsthws/firmware-liberator/releases/tag/emulator-source-beken-d9a3772),
one archive named after its commit and never changed (`git archive` of the
pinned commit, which `bk-emu.sh source` reproduces byte for byte), downloaded
once into `captures/emulation/cache/` and checked against the SHA-256
`UPSTREAM` pins. So the emulator needs nothing outside this repository. Upstream
publishes no licence. `bk-emu.sh` with no argument lists the rest; what each
patch does and why, how to change them, and how the pin moves, is in
[docs/beken-s61-emulation.md](../../../docs/beken-s61-emulation.md#the-fork-toolsbekenemulator).

**No firmware is in the image or the archive.** The public S61s dump is Sonoff's
firmware, which this repository never holds, so `bk-emu.sh dump` fetches it
from upstream's repository, which publishes it, into
`captures/emulation/beken-firmwares/` (the tools do this before they run), and
`bk-emu.sh test` fetches upstream's other devices' dumps there too (about
280 MB). That directory is mounted into the container at the emulator's
`firmwares/`, and the checkout at `/work`, as your own user; `S61_DUMP` names a
dump of your own, under `captures/`.
