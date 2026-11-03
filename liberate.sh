#!/bin/sh
# Fetch or update Autoflash, the conversion tool, and start it, on macOS or
# Linux - with curl or wget, no git. The README's one line:
#
#   sh -c "$(curl -fsSL https://github.com/adamsthws/Sonoff-OTA/releases/latest/download/liberate.sh)"   macOS, which has curl
#   sh -c "$(wget -qO- https://github.com/adamsthws/Sonoff-OTA/releases/latest/download/liberate.sh)"    most Linux, which has wget
#
# It, the release's files and its tag are assets of every release
# (.github/workflows/release.yml), so only a released, tested version is
# ever run - and no GitHub API, with its hourly limit, is asked.
#
# (sh -c, not curl | sh: the tool asks questions, and piped, the keyboard would
# be the script.) Arguments after it - sh -c "$(...)" sh run --verbose - pass
# through to start.sh.
#
# The latest release is unpacked into ~/Sonoff-OTA and started there. Run
# again, the folder is updated in place: the files the release it holds put
# there are replaced or removed, nothing else - captures/, with the plugs'
# Sonoff backups and an unfinished conversion, and the portable Python in
# .python/ stay as they are. A git clone there is left to git.
set -eu

REPO=adamsthws/Sonoff-OTA
GITHUB=${AUTOFLASH_GITHUB:-https://github.com}
FOLDER=${AUTOFLASH_HOME:-$HOME/Sonoff-OTA}
# The installed release's tag, then every file it put in the folder.
MANIFEST=$FOLDER/.installed-release

say() { echo " $*" >&2; }   # one space in, as the tool's own output

fetch() {  # $1 URL, $2 file (- for the output)
    if command -v curl >/dev/null 2>&1; then curl -fsSL -o "$2" "$1"
    else wget -qO "$2" "$1"
    fi
}

latest_tag() {
    tag=$(fetch "$GITHUB/$REPO/releases/latest/download/autoflash-version" - 2>/dev/null | head -n 1 | tr -d ' \r')
    case $tag in v[0-9]*) echo "$tag" ;; esac
}

install_release() {  # $1 the tag
    work=$(mktemp -d)
    trap 'rm -rf "$work"' EXIT
    say "Downloading Autoflash $1..."
    if ! fetch "$GITHUB/$REPO/releases/download/$1/autoflash.tar.gz" "$work/release.tar.gz"; then
        say "The download failed."; exit 1
    fi
    mkdir "$work/release"
    tar -xzf "$work/release.tar.gz" -C "$work/release" --strip-components=1
    (cd "$work/release" && find . ! -type d | sed 's|^\./||' | LC_ALL=C sort) > "$work/files"
    mkdir -p "$FOLDER"
    (cd "$work/release" && tar -cf - .) | (cd "$FOLDER" && tar -xf -)
    # What the release before had and this one has not: never a file of the
    # user's own, which no release lists.
    if [ -f "$MANIFEST" ]; then
        tail -n +2 "$MANIFEST" | LC_ALL=C sort > "$work/before"
        LC_ALL=C comm -23 "$work/before" "$work/files" | while IFS= read -r gone; do
            rm -f "$FOLDER/$gone"
            # Its folder too, and theirs, once nothing is left in them.
            (cd "$FOLDER" && rmdir -p "$(dirname "$gone")" 2>/dev/null) || true
        done
    fi
    # Last, so an install cut short is done again on the next run.
    { echo "$1"; cat "$work/files"; } > "$MANIFEST.new"
    mv "$MANIFEST.new" "$MANIFEST"
    rm -rf "$work"
    trap - EXIT
    say "Autoflash $1 is in $FOLDER."
}

if [ -d "$FOLDER/.git" ]; then
    say "$FOLDER is a git clone: starting it as it is (git pull updates it)."
    exec sh "$FOLDER/start.sh" "$@"
fi
if [ -d "$FOLDER" ] && [ ! -f "$MANIFEST" ] && [ ! -d "$FOLDER/tools/autoflash" ] &&
   [ -n "$(ls -A "$FOLDER")" ]; then
    say "$FOLDER already holds something else: nothing was changed. Move it away, or"
    say "set AUTOFLASH_HOME to another folder, and run this again."
    exit 1
fi

installed=$(head -n 1 "$MANIFEST" 2>/dev/null || true)
latest=$(latest_tag)
if [ -z "$latest" ]; then
    if [ -z "$installed" ]; then
        say "The latest release could not be found on GitHub: is this computer online?"
        exit 1
    fi
    say "Could not check for a newer release: starting Autoflash $installed."
elif [ "$latest" != "$installed" ]; then
    install_release "$latest"
fi
exec sh "$FOLDER/start.sh" "$@"
