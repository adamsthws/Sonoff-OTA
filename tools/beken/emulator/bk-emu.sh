#!/bin/bash
# The S61's emulator, BekenEmulator with this project's patches, as a container
# image - built from the source archive this repository's release
# emulator-source-beken-<commit> holds, or pulled where it is published - and
# the tools in tools/beken run in it.
#
#   tools/beken/emulator/bk-emu.sh python ARGS...
#                             run python in the emulator's container, with this checkout
#                             at /work (the working directory) and the fetched firmware
#                             mounted in; the image is ensured first:
#                             tools/beken/emulator/bk-emu.sh python tools/beken/seed_join.py
#   tools/beken/emulator/bk-emu.sh run DUMP [ARGS...]
#                             run a BK7238 dump as the S61 work does: main.py DUMP
#                             --with-boot -chip BK7238 --only-uart ARGS
#   tools/beken/emulator/bk-emu.sh plug
#                             the public S61 dump as a plug on a home Wi-Fi, on the S61's
#                             own Docker network (autoflash-beken, made the first time;
#                             nothing of the S60's emulator is on it), until docker stop: its
#                             radio bridged there through a TAP device, its router .1 - its
#                             DHCP (the plug at .50-.59), DNS and eWeLink, the local stub -
#                             in its container (autoflash-s61, plug-net.sh). Its flash, logs
#                             and the stub's CA are kept in captures/emulation/s61/autoflash-s61/,
#                             so it carries on as it was left; delete that for a new plug.
#                             It returns once the plug is in its stub's account
#   tools/beken/emulator/bk-emu.sh ui [PORT]
#                             the plug's web page, OpenBeken's or ESPHome's, for this computer's browser,
#                             at http://127.0.0.1:PORT/ (8088) through ui_proxy.py: opened
#                             straight, a browser's requests lock the emulated plug up
#                             (why, in ui_proxy.py)
#   tools/beken/emulator/bk-emu.sh power off|on|cycle
#                             the plug's power switch: its flash kept, as a plug unplugged
#   tools/beken/emulator/bk-emu.sh reset
#                             the plug as new: it and the computers on its Wi-Fi stopped,
#                             its state (flash, runs, captures) deleted, and started again
#                             on Sonoff's firmware from the dump
#   tools/beken/emulator/bk-emu.sh computer [COMMAND...]
#                             a computer on the plug's Wi-Fi: python COMMAND (a shell without
#                             one) in the image, as python does, there, with eWeLink the
#                             plug's stub (AUTOFLASH_EWELINK_API_BASE, its CA trusted) and
#                             the plug's power switch to hand (AUTOFLASH_EMULATOR_POWER), in
#                             its own copy of this checkout (the plug's state directory's
#                             work/), so its runs' captures are its own:
#                             tools/beken/emulator/bk-emu.sh computer tools/autoflash/cli.py run
#   tools/beken/emulator/bk-emu.sh test [ARGS...]
#                             upstream's own self-tests (tests/self_test.py ARGS,
#                             -k PATTERN selects by name), under the patches; they run
#                             upstream's firmwares/ (about 280 MB), fetched the first time
#   tools/beken/emulator/bk-emu.sh dump
#                             the public S61s dump (firmwares/Sonoff_S61s_..., the tools'
#                             default S61_DUMP), fetched from upstream's repository, which
#                             publishes it, into captures/emulation/beken-firmwares/: this
#                             repository, and the image, never hold Sonoff's firmware
#   tools/beken/emulator/bk-emu.sh ensure [--rebuild]
#                             the image, labelled with the hash of what it is built from:
#                             as it is, or pulled, or built where it is not published
#                             (image.sh's way); python/run/test do this first
#   tools/beken/emulator/bk-emu.sh tag        that hash (UPSTREAM, patches/,
#                             requirements.txt, bk-emu.Dockerfile)
#   tools/beken/emulator/bk-emu.sh build IMAGE | pull IMAGE
#                             build it from the archive (downloaded once into
#                             captures/emulation/cache/ and checked against UPSTREAM's
#                             hash), or pull the published one with this hash
#                             (ghcr.io/adamsthws/autoflash-beken-emu:<hash>, which the
#                             beken-emulator-image workflow builds)
#   tools/beken/emulator/bk-emu.sh source [DIR]
#                             make the archive UPSTREAM names, in DIR (default: the
#                             current directory), from upstream at the pinned commit:
#                             git archive, so the same commit gives the same bytes.
#                             Publishing it as the release emulator-source-beken-<commit>
#                             (one archive, never changed), and its hash in UPSTREAM, is
#                             how the pin moves; a change to the emulator is a patch
#
# AUTOFLASH_BEKEN_IMAGE names the image (default autoflash-beken-emu),
# AUTOFLASH_BEKEN_NETWORK the plug's network (default autoflash-beken),
# AUTOFLASH_BEKEN_PLUG_NAME the plug (default autoflash-s61; its state is
# captures/emulation/s61/<name>/), AUTOFLASH_BEKEN_COMPUTER a name for the
# computer's container,
# AUTOFLASH_BEKEN_PUBLISHED another place to pull from, AUTOFLASH_BEKEN_NO_PULL=1
# always builds, AUTOFLASH_BEKEN_SOURCE_URL another place to download the archive
# from. Tools run as your own user, so what they write in /work is yours.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
EMU=$ROOT/captures/emulation
CACHE=$EMU/cache
FIRMWARES=$EMU/beken-firmwares
S61_DUMP=Sonoff_S61s_EUPlug_WBBK_01P_V1.3.bin
IMAGE=${AUTOFLASH_BEKEN_IMAGE:-autoflash-beken-emu}
NETWORK=${AUTOFLASH_BEKEN_NETWORK:-autoflash-beken}
PLUG=${AUTOFLASH_BEKEN_PLUG_NAME:-autoflash-s61}
PLUG_STATE=$EMU/s61/$PLUG
PUBLISHED=${AUTOFLASH_BEKEN_PUBLISHED:-ghcr.io/adamsthws/autoflash-beken-emu}
# What the archive holds of upstream: the emulator, not its firmwares/.
ARCHIVED="src tests tools README.md requirements.txt .gitignore"

pin() { sed -n "s/^$1 //p" "$HERE/UPSTREAM"; }
UPSTREAM_REPO=$(pin repo)
UPSTREAM_COMMIT=$(pin commit)
ARCHIVE=$(pin archive)
ARCHIVE_SHA256=$(pin sha256)
[ -n "$UPSTREAM_REPO" ] && [ -n "$UPSTREAM_COMMIT" ] && [ -n "$ARCHIVE" ] && [ -n "$ARCHIVE_SHA256" ] \
    || { echo "$HERE/UPSTREAM: needs repo, commit, archive and sha256" >&2; exit 2; }
ARCHIVE_URL=${AUTOFLASH_BEKEN_SOURCE_URL:-https://github.com/adamsthws/Sonoff-OTA/releases/download/emulator-source-beken-${UPSTREAM_COMMIT:0:7}/$ARCHIVE}

tag() {
    (cd "$HERE" && find UPSTREAM requirements.txt bk-emu.Dockerfile patches -type f -print0 \
        | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16)
}

sha256() { sha256sum < "$1" | cut -c1-64; }

# The archive, downloaded once and checked - as image.sh keeps the QEMU source.
archive() {
    local cache=$CACHE/$ARCHIVE part
    mkdir -p "$CACHE"
    if [ ! -f "$cache" ] || [ "$(sha256 "$cache")" != "$ARCHIVE_SHA256" ]; then
        echo "Downloading the emulator's source ($ARCHIVE, about 300 kB)..." >&2
        part=$(mktemp "$cache.XXXXXX.part")
        curl -fsSL --retry 3 -o "$part" "$ARCHIVE_URL" \
            || { rm -f "$part"; echo "Could not download $ARCHIVE_URL" >&2; exit 1; }
        mv "$part" "$cache"
    fi
    if [ "$(sha256 "$cache")" != "$ARCHIVE_SHA256" ]; then
        echo "$cache is not the archive UPSTREAM pins; delete it and run this again" >&2
        exit 1
    fi
    echo "$cache"
}

context=""
trap '[ -z "$context" ] || rm -rf "$context"' EXIT
build() {
    local source
    source=$(archive)
    context=$(mktemp -d)
    cp -r "$HERE/bk-emu.Dockerfile" "$HERE/patches" "$HERE/requirements.txt" "$context/"
    cp "$source" "$context/bekenemulator.tar.xz"
    echo "Building the S61 emulator image (the first time takes a minute or two)..." >&2
    docker build -q -t "$1" --build-arg SOURCE_SHA256="$ARCHIVE_SHA256" \
        --label "autoflash.built-from=$(tag)" \
        --label "org.opencontainers.image.source=https://github.com/adamsthws/Sonoff-OTA" \
        -f "$context/bk-emu.Dockerfile" "$context" >/dev/null
}

pull() {
    local want published
    want=$(tag)
    published=$PUBLISHED:$want
    docker pull -q "$published" >/dev/null 2>&1 || return 1
    if [ "$(docker image inspect -f '{{index .Config.Labels "autoflash.built-from"}}' "$published")" != "$want" ]; then
        echo "$published is not labelled $want: not used" >&2
        return 1
    fi
    docker tag "$published" "$1"
    echo "Using the published S61 emulator image ($published)." >&2
}

labelled() {
    [ "$(docker image inspect -f '{{index .Config.Labels "autoflash.built-from"}}' "$1" 2>/dev/null)" = "$(tag)" ]
}

ensure() {
    local rebuild=${1:-}
    if command -v flock >/dev/null; then
        mkdir -p "$CACHE"
        exec 9>"$CACHE/beken-image.lock"
        flock 9
    fi
    if [ "$rebuild" = --rebuild ] || ! labelled "$IMAGE"; then
        if [ "$rebuild" = --rebuild ] || [ "${AUTOFLASH_BEKEN_NO_PULL:-}" = 1 ] || ! pull "$IMAGE"; then
            build "$IMAGE"
        fi
    fi
    # Let go of it once the image is there: what runs next (exec docker run)
    # would hold it for as long as it runs, and every other emulator wait.
    exec 9>&-
}

# Firmware dumps come from upstream's repository, which publishes them, never
# from this one: the paths given (sparse-checkout patterns) from its firmwares/,
# at the pinned commit, into captures/emulation/beken-firmwares/.
fetch_firmwares() {
    local work f
    mkdir -p "$FIRMWARES"
    work=$(mktemp -d "$FIRMWARES.XXXXXX.part")
    trap 'rm -rf "$work"' EXIT
    git init -q "$work"
    git -C "$work" remote add origin "$UPSTREAM_REPO"
    git -C "$work" sparse-checkout set --no-cone "$@"
    git -C "$work" fetch -q --depth 1 --filter=blob:none origin "$UPSTREAM_COMMIT"
    git -C "$work" -c advice.detachedHead=false checkout -q "$UPSTREAM_COMMIT"
    for f in "$work"/firmwares/*; do [ -e "$FIRMWARES/$(basename "$f")" ] || cp "$f" "$FIRMWARES/"; done
    rm -rf "$work"
    trap - EXIT
}

dump() {
    [ -f "$FIRMWARES/$S61_DUMP" ] && return 0
    echo "Fetching the public S61s dump from $UPSTREAM_REPO (4 MB)..." >&2
    fetch_firmwares "/firmwares/$S61_DUMP"
}

# Upstream's self-tests run every firmware in firmwares/ (about 280 MB).
firmwares() {
    [ -f "$FIRMWARES/OpenBK7238_QIO_1.18.300.bin" ] && return 0
    echo "Fetching upstream's firmwares/ for its self-tests (about 280 MB)..." >&2
    fetch_firmwares /firmwares/
}

# python in the container: this checkout at /work, the dumps at the emulator's
# firmwares/, as your own user; a terminal when there is one.
container() {
    local tty=() workdir="/work${PWD#"$ROOT"}"
    [ -t 0 ] && [ -t 1 ] && tty=(-t)
    # WORK: another tree at /work (computer's copy), worked in at its top.
    [ -n "${WORK:-}" ] && workdir=/work
    mkdir -p "$FIRMWARES"
    # exec: a signal to this script (timeout, Ctrl-C) reaches the docker client,
    # which passes it to the container; a child docker would run on, holding the
    # image lock, with the container to be stopped by hand.
    # CONTAINER_OPTIONS: what a caller adds (computer's network and settings).
    exec docker run --rm -i "${tty[@]}" ${CONTAINER_OPTIONS[@]+"${CONTAINER_OPTIONS[@]}"} --user "$(id -u):$(id -g)" \
        -v "${WORK:-$ROOT}:/work" -v "$FIRMWARES:/opt/beken-emulator/firmwares:ro" \
        -e S61_DUMP="/opt/beken-emulator/firmwares/$S61_DUMP" \
        -w "$workdir" "$IMAGE" "$@"
}

# The plug's network, a /24 of its own: the tool sweeps the network it is on
# at its real size, and Docker's default is a /16. Its gateway is .254 and
# Docker hands out .128 and up, so .1-.127 are the plug's home network's own:
# its router .1 and the plug's pool (plug-net.sh). Made once, on a subnet
# neither this computer's routes nor another Docker network use, and kept.
network() {
    if docker network inspect "$NETWORK" >/dev/null 2>&1; then
        [ "$(docker network inspect -f '{{range .IPAM.Config}}{{.IPRange}}{{end}}' "$NETWORK")" = "$(subnet).128/25" ] && return 0
        echo "The Docker network $NETWORK is not laid out as the plug's home network:" >&2
        echo "remove it (docker network rm $NETWORK) and run this again" >&2
        exit 1
    fi
    local subnet attempt
    for attempt in 1 2 3 4 5 6 7 8; do
        subnet=$( { ip -4 route 2>/dev/null | awk '{print $1}'
                    docker network ls -q | xargs docker network inspect \
                        -f '{{range .IPAM.Config}}{{println .Subnet}}{{end}}' 2>/dev/null; } | python3 -I -c '
import ipaddress, sys
taken = [ipaddress.ip_network(line.strip(), strict=False) for line in sys.stdin
         if line.strip() and line.strip() != "default" and ":" not in line]
# One a plug: the fault suite runs several side by side, each on its own.
for candidate in [f"198.18.{n}.0/24" for n in range(61, 128)] + [f"172.31.{n}.0/24" for n in range(61, 128)]:
    if not any(ipaddress.ip_network(candidate).overlaps(network) for network in taken):
        print(candidate)
        break')
        [ -n "$subnet" ] || { echo "No free /24 for the S61's Docker network: make one, and name it with AUTOFLASH_BEKEN_NETWORK" >&2; exit 1; }
        docker network create --subnet "$subnet" --ip-range "${subnet%.*}.128/25" --gateway "${subnet%.*}.254" \
            --label autoflash.emulator=1 "$NETWORK" >/dev/null 2>&1 && return 0
        # Made by another run starting at the same moment - or that subnet taken
        # by another plug's network made since it was chosen: choose again.
        docker network inspect "$NETWORK" >/dev/null 2>&1 && return 0
        sleep "$attempt"
    done
    echo "Could not make the Docker network $NETWORK" >&2
    exit 1
}

# The plug's network's first three numbers (198.18.61, say).
subnet() {
    local cidr
    cidr=$(docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}' "$NETWORK")
    echo "${cidr%.*}"
}

source_archive() {
    local out=${1:-.} clone
    clone=$(mktemp -d)
    trap 'rm -rf "$clone"' EXIT
    echo "Cloning $UPSTREAM_REPO at ${UPSTREAM_COMMIT:0:7}..." >&2
    git init -q "$clone"
    git -C "$clone" remote add origin "$UPSTREAM_REPO"
    git -C "$clone" fetch -q --depth 1 origin "$UPSTREAM_COMMIT"
    # shellcheck disable=SC2086  # ARCHIVED is a list of paths
    git -C "$clone" -c tar.umask=0022 archive --format=tar --prefix=BekenEmulator/ "$UPSTREAM_COMMIT" $ARCHIVED \
        | xz -9 -T1 > "$out/$ARCHIVE"
    rm -rf "$clone"
    trap - EXIT
    sha256sum "$out/$ARCHIVE"
}

case "${1:-}" in
    tag) tag ;;
    ensure) ensure "${2:-}" ;;
    build) [ -n "${2:-}" ] || { echo "build needs an image name" >&2; exit 2; }; build "$2" ;;
    pull) [ -n "${2:-}" ] || { echo "pull needs an image name" >&2; exit 2; }; pull "$2" ;;
    dump) dump ;;
    python) shift; ensure; dump || echo "No public dump: S61_DUMP must name a dump of your own." >&2; container "$@" ;;
    run)
        [ -n "${2:-}" ] || { echo "run needs a dump" >&2; exit 2; }
        ensure
        dump_path=$(realpath "$2"); shift 2
        case "$dump_path" in "$ROOT"/*) dump_path="/work${dump_path#"$ROOT"}" ;;
            *) echo "$dump_path is outside this checkout: put it under captures/" >&2; exit 2 ;; esac
        container /opt/beken-emulator/src/main.py "$dump_path" --with-boot -chip BK7238 --only-uart "$@" ;;
    plug)
        ensure; dump; network
        mkdir -p "$PLUG_STATE"
        docker run -d --rm --name "$PLUG" --network "$NETWORK" --cap-add NET_ADMIN --device /dev/net/tun \
            -v "$ROOT:/work:ro" -v "$FIRMWARES:/opt/beken-emulator/firmwares:ro" -v "$PLUG_STATE:/state" \
            -e S61_DUMP="/opt/beken-emulator/firmwares/$S61_DUMP" \
            -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" -e PLUG_TRACE="${PLUG_TRACE:-}" \
            --entrypoint bash "$IMAGE" /work/tools/beken/emulator/plug-net.sh >/dev/null
        echo "The S61 is starting as $PLUG on $NETWORK, its router at $(subnet).1; its serial output is in" >&2
        echo "${PLUG_STATE#"$ROOT"/}/serial.log; docker stop $PLUG ends it." >&2
        # A run needs the plug in its account, which the stub only has once
        # the plug has registered with it: a minute or two from a new plug.
        for _ in $(seq 120); do
            grep -qs '"apikey"' "$PLUG_STATE/stub-reported.json" && { echo "$PLUG is online." >&2; exit 0; }
            docker ps -q --filter "name=^$PLUG\$" | grep -q . || { echo "$PLUG stopped: see its serial.log" >&2; exit 1; }
            sleep 5
        done
        echo "$PLUG has not reached its stub after 10 minutes: see its serial.log" >&2; exit 1 ;;
    reset)
        # Everything on its Wi-Fi first - the plug and any computer's run - so
        # nothing writes its state while it goes.
        mapfile -t running < <(docker ps -q --filter "network=$NETWORK")
        [ ${#running[@]} -eq 0 ] || docker stop -t 30 "${running[@]}" >/dev/null
        case "$PLUG_STATE" in "$EMU"/s61/?*) rm -rf "$PLUG_STATE" ;; esac
        echo "Reset $PLUG: its state is gone; starting it again from the dump." >&2
        exec "$0" plug ;;
    ui)
        # The plug's address as its router gave it last (its firmware may have
        # changed, and with it its lease).
        read -r _ _ lease named _ < <(docker exec "$PLUG" sh -c 'sort -n /run/autoflash-s61/leases | tail -1' 2>/dev/null)
        [ -n "${lease:-}" ] || { echo "No plug on its Wi-Fi yet: start it (bk-emu.sh plug) and give it a minute" >&2; exit 1; }
        # Which firmware, by the name it gave its router: esphome-abcdef, or
        # openbeken-abcdef (OpenBK7238_ABCDEF before its first start's naming).
        case "${named:-}" in
            esphome*) firmware=esphome ;;
            openbeken-*|OpenBK*) firmware=openbeken ;;
            *) echo "The plug at $lease runs neither OpenBeken nor ESPHome (it calls itself ${named:-nothing}): Sonoff's firmware has no page" >&2; exit 1 ;;
        esac
        exec python3 -I "$HERE/ui_proxy.py" --device "$lease" --firmware "$firmware" --listen "127.0.0.1:${2:-8088}" ;;
    power)
        case "${2:-}" in
            off|on) echo "$2" > "$PLUG_STATE/power" ;;
            cycle) echo off > "$PLUG_STATE/power"; sleep 2; echo on > "$PLUG_STATE/power" ;;
            *) echo "power off, on or cycle" >&2; exit 2 ;;
        esac ;;
    computer)
        shift; ensure; network
        [ -f "$PLUG_STATE/ca.crt" ] || { echo "No plug yet: start it first (bk-emu.sh plug)" >&2; exit 1; }
        # The computer's own copy of this checkout, refreshed each time, its
        # captures kept: a plug's runs are filed apart from every other's.
        # Excluded whatever they are, as emu.sh's: in a git worktree .git is a
        # file, and captures or .venv may be links to the main checkout's.
        WORK=$PLUG_STATE/work
        mkdir -p "$WORK/captures"
        rsync -a --delete --exclude /captures --exclude /.venv --exclude /.python --exclude /.git \
              --exclude /.env --exclude __pycache__/ "$ROOT/" "$WORK/"
        # The plug's power switch too, so a run can offer to switch it off and on.
        [ -f "$PLUG_STATE/power" ] || echo on > "$PLUG_STATE/power"
        CONTAINER_OPTIONS=(${AUTOFLASH_BEKEN_COMPUTER:+--name "$AUTOFLASH_BEKEN_COMPUTER"}
                           --network "$NETWORK" -v "$PLUG_STATE/ca.crt:/plug/ca.crt:ro"
                           -v "$PLUG_STATE/power:/plug/power" -e AUTOFLASH_EMULATOR_POWER=/plug/power
                           -e AUTOFLASH_EWELINK_API_BASE="https://$(subnet).1" --entrypoint bash)
        container /work/tools/beken/emulator/computer.sh "$@" ;;
    test) shift; ensure; firmwares; container /opt/beken-emulator/tests/self_test.py "$@" ;;
    source) source_archive "${2:-}" ;;
    *) sed -n "2,$(($(grep -n '^set -euo pipefail' "$0" | cut -d: -f1)-1))p" "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
