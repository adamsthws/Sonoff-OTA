#!/bin/bash
# The emulator's image: what it is built from, and getting it.
#
#   tools/emulator/image.sh tag          the hash of what it is built from
#                                        (emu.Dockerfile, fork/, container/)
#                                        - so a change to a patch rebuilds it
#   tools/emulator/image.sh build IMAGE  build it, labelled with that hash
#   tools/emulator/image.sh pull IMAGE   the published one with that hash
#                                        (ghcr.io/adamsthws/autoflash-emu:<hash>,
#                                        which .github/workflows/emulator-image.yml
#                                        builds), tagged IMAGE - only if its
#                                        label is that hash
#   tools/emulator/image.sh ensure IMAGE [--rebuild]
#                                        IMAGE labelled with that hash: as it is,
#                                        or pulled, or built where it cannot be -
#                                        one emulator at a time, so two started
#                                        together do not both build it
#
# emu.sh ensures its image every time: an image from an older checkout never
# runs unseen. AUTOFLASH_EMU_PUBLISHED names another place to pull from, and
# AUTOFLASH_EMU_NO_PULL=1 always builds.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
EMU=$ROOT/captures/emulation
PUBLISHED=${AUTOFLASH_EMU_PUBLISHED:-ghcr.io/adamsthws/autoflash-emu}

tag() {
    (cd "$ROOT/tools/emulator" && find emu.Dockerfile fork container -type f -print0 \
        | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16)
}

# The QEMU fork's source, as emu.Dockerfile pins it (QEMU_SOURCE_SHA256): from
# this repository's release emulator-source-qemu-<commit>, which holds that one
# archive and never changes (a new pin is a new release), downloaded once into
# captures/emulation/cache and checked, so the image builds from nothing but
# this repository - and again without a network once it is there.
QEMU_SOURCE=qemu-lcgamboa-e39508f.tar.xz
QEMU_COMMIT=${QEMU_SOURCE%.tar.xz}; QEMU_COMMIT=${QEMU_COMMIT##*-}
QEMU_SOURCE_URL=${AUTOFLASH_QEMU_SOURCE_URL:-https://github.com/adamsthws/Sonoff-OTA/releases/download/emulator-source-qemu-$QEMU_COMMIT/$QEMU_SOURCE}
qemu_source() {
    local want cache=$EMU/cache/$QEMU_SOURCE
    want=$(sed -n 's/^ARG QEMU_SOURCE_SHA256=//p' "$ROOT/tools/emulator/emu.Dockerfile")
    [ -n "$want" ] || return 0   # a Dockerfile that fetches the source itself
    mkdir -p "$EMU/cache"
    if [ ! -f "$cache" ] || [ "$(sha256sum < "$cache" | cut -c1-64)" != "$want" ]; then
        echo "Downloading the emulator's QEMU source ($QEMU_SOURCE, about 33 MB)..." >&2
        local part
        part=$(mktemp "$cache.XXXXXX.part")
        curl -fsSL --retry 3 -o "$part" "$QEMU_SOURCE_URL" \
            || { rm -f "$part"; echo "Could not download $QEMU_SOURCE_URL" >&2; exit 1; }
        mv "$part" "$cache"
    fi
    if [ "$(sha256sum < "$cache" | cut -c1-64)" != "$want" ]; then
        echo "$cache is not the source emu.Dockerfile pins; delete it and run this again" >&2
        exit 1
    fi
    cp "$cache" "$1"
}

context=""
trap '[ -z "$context" ] || rm -rf "$context"' EXIT
build() {
    context=$(mktemp -d)
    cp -r "$ROOT/tools/emulator/emu.Dockerfile" "$ROOT/tools/emulator/fork" \
          "$ROOT/tools/emulator/container" "$context/"
    qemu_source "$context/qemu-source.tar.xz"
    echo "Building the emulator image (the first time takes a few minutes)..." >&2
    docker build -q -t "$1" --label "autoflash.built-from=$(tag)" \
        --label "org.opencontainers.image.source=https://github.com/adamsthws/Sonoff-OTA" \
        -f "$context/emu.Dockerfile" "$context" >/dev/null
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
    echo "Using the published emulator image ($published)." >&2
}

labelled() {
    [ "$(docker image inspect -f '{{index .Config.Labels "autoflash.built-from"}}' "$1" 2>/dev/null)" = "$(tag)" ]
}

ensure() {
    local image=$1 rebuild=${2:-}
    if command -v flock >/dev/null; then
        mkdir -p "$EMU/cache"
        exec 9>"$EMU/cache/image.lock"
        flock 9
    fi
    if [ "$rebuild" != --rebuild ] && labelled "$image"; then
        return 0
    fi
    if [ "$rebuild" = --rebuild ] || [ "${AUTOFLASH_EMU_NO_PULL:-}" = 1 ] || ! pull "$image"; then
        build "$image"
    fi
}

case "${1:-}" in
    tag) tag ;;
    ensure) [ -n "${2:-}" ] || { echo "ensure needs an image name" >&2; exit 2; }; ensure "$2" "${3:-}" ;;
    build) [ -n "${2:-}" ] || { echo "build needs an image name" >&2; exit 2; }; build "$2" ;;
    pull) [ -n "${2:-}" ] || { echo "pull needs an image name" >&2; exit 2; }; pull "$2" ;;
    *) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
