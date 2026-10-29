# The S61's emulator

[BekenEmulator](https://github.com/openshwprojects/BekenEmulator) at the commit
`UPSTREAM` pins, with this project's patches and the Python it imports, as a
container image - the way the S60's emulator is kept (`tools/espressif/emulator`). The
tools in `docs/s61-tools` run in it:

```sh
tools/beken/emulator/bk-emu.sh python tools/beken/seed_join.py
tools/beken/emulator/bk-emu.sh run captures/your-dump.bin --wifi-peer auto
tools/beken/emulator/bk-emu.sh test -k "Sonoff 4MB"    # upstream's self-tests, under the patches
```

Each ensures the image first: one labelled with the hash of what it is built
from (`bk-emu.sh tag`: `UPSTREAM`, `patches/`, `requirements.txt`,
`bk-emu.Dockerfile`), pulled from `ghcr.io/adamsthws/autoflash-beken-emu:<hash>`,
which the `beken-emulator-image` workflow publishes for every version pushed,
or built where it is not published - from the upstream source archive on this
repository's release
[`emulator-source-beken-d9a3772`](https://github.com/adamsthws/Sonoff-OTA/releases/tag/emulator-source-beken-d9a3772),
one archive named after its commit and never changed (`git archive` of the
pinned commit, which `bk-emu.sh source` reproduces byte for byte), downloaded
once into `captures/emulation/cache/` and checked against the SHA-256
`UPSTREAM` pins. So the emulator needs nothing outside this repository. Upstream
publishes no licence. `bk-emu.sh` with no argument lists the rest; what each
patch does and why, how to change them, and how the pin moves, is in
[docs/beken-s61-emulation.md](../../beken-s61-emulation.md#the-fork-s61-toolsemulator).

**No firmware is in the image or the archive.** The public S61s dump is Sonoff's
firmware, which this repository never holds, so `bk-emu.sh dump` fetches it
from upstream's repository, which publishes it, into
`captures/emulation/beken-firmwares/` (the tools do this before they run), and
`bk-emu.sh test` fetches upstream's other devices' dumps there too (about
280 MB). That directory is mounted into the container at the emulator's
`firmwares/`, and the checkout at `/work`, as your own user; `S61_DUMP` names a
dump of your own, under `captures/`.
