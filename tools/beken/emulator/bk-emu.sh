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
        | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16)
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
    if [ "$rebuild" != --rebuild ] && labelled "$IMAGE"; then
        return 0
    fi
    if [ "$rebuild" = --rebuild ] || [ "${AUTOFLASH_BEKEN_NO_PULL:-}" = 1 ] || ! pull "$IMAGE"; then
        build "$IMAGE"
    fi
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
    local tty=()
    [ -t 0 ] && [ -t 1 ] && tty=(-t)
    mkdir -p "$FIRMWARES"
    docker run --rm -i "${tty[@]}" --user "$(id -u):$(id -g)" \
        -v "$ROOT:/work" -v "$FIRMWARES:/opt/beken-emulator/firmwares:ro" \
        -e S61_DUMP="/opt/beken-emulator/firmwares/$S61_DUMP" \
        -w "/work${PWD#"$ROOT"}" "$IMAGE" "$@"
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
    test) shift; ensure; firmwares; container /opt/beken-emulator/tests/self_test.py "$@" ;;
    source) source_archive "${2:-}" ;;
    *) sed -n "2,$(($(grep -n '^set -euo pipefail' "$0" | cut -d: -f1)-1))p" "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
