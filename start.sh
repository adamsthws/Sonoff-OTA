#!/bin/sh
# Start the S60 conversion tool on macOS or Linux:
#
#   sh start.sh                 convert a plug (the same as: sh start.sh run)
#   sh start.sh run --verbose   any of the tool's own commands and options
#
# Which Python it runs on, in order:
#
#   Linux  a pinned portable Python, unpacked into .python/ in this folder -
#          the same Python for everyone, and nothing installed; else a Python
#          already installed (3.10 or newer); else it says how to get one.
#   macOS  a Python already installed (3.10 or newer); else python.org's,
#          installed quietly - macOS asks for your password - and offered for
#          removal again when the tool exits. (No portable Python here: macOS
#          is wary of unsigned programs, and python.org's signed one has to be
#          installed.)
set -eu

here=$(cd "$(dirname "$0")" && pwd)
PYTHON_HOME=${S60_PYTHON_HOME:-$here/.python}
# Left by a Python this script installed, so that only that one is ever removed.
MARKER=$PYTHON_HOME/installed-by-s60

# Linux: python-build-standalone (Astral's, as uv uses), pinned. The SHA-256s
# are the ones published in the release's SHA256SUMS.
PBS_BASE=https://github.com/astral-sh/python-build-standalone/releases/download/20250517
PBS_X86_64=0356bd1db292781a20e011d789f0c41ea5ec04731c8236c8185b53a54faf2871
PBS_AARCH64=012bba52c1a824e357b0152c2c6366f3ed65551f1751330ebb6c2f6bb730d987

# macOS: python.org's signed installer. python.org publishes its MD5
# (8f4989592c9412e51fcad70273442c22), which this file matches.
MAC_PKG_URL=https://www.python.org/ftp/python/3.12.10/python-3.12.10-macos11.pkg
MAC_PKG_SHA256=8373e58da4ea146b3eb1c1f9834f19a319440b6b679b06050b1f9ee3237aa8e4
MAC_VERSION=3.12
MAC_FRAMEWORK=/Library/Frameworks/Python.framework
MAC_PYTHON=$MAC_FRAMEWORK/Versions/$MAC_VERSION/bin/python3

say() { echo "$@" >&2; }   # to the screen, never into $(...)

usable() {  # $1: a Python 3.10 or newer
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

fetch() {  # $1 URL, $2 file
    if command -v curl >/dev/null 2>&1; then curl -fsSL -o "$2" "$1"
    else wget -qO "$2" "$1"
    fi
}

sha256() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
    else shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

portable_python() {  # Linux only: .python/<platform>/python/bin/python3
    [ "$(uname -s)" = Linux ] || return 1
    case "$(uname -m)" in
    x86_64|amd64) arch=x86_64 sum=$PBS_X86_64 ;;
    aarch64|arm64) arch=aarch64 sum=$PBS_AARCH64 ;;
    *) return 1 ;;
    esac
    dir=$PYTHON_HOME/linux-$arch-3.12.10
    exe=$dir/python/bin/python3
    if [ ! -x "$exe" ]; then
        if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
            say "No curl or wget to download the portable Python with: looking for an installed one."
            return 1
        fi
        mkdir -p "$PYTHON_HOME"
        archive=$PYTHON_HOME/python.tar.gz
        say "Downloading Python 3.12.10 (portable, into $PYTHON_HOME)..."
        if ! fetch "$PBS_BASE/cpython-3.12.10%2B20250517-$arch-unknown-linux-gnu-install_only_stripped.tar.gz" "$archive"; then
            rm -f "$archive"; say "The portable Python could not be downloaded."; return 1
        fi
        got=$(sha256 "$archive")
        if [ "$got" != "$sum" ]; then
            rm -f "$archive"; say "The portable Python download is not the expected file (SHA-256 $got)."; return 1
        fi
        rm -rf "$dir.partial" && mkdir -p "$dir.partial"
        tar -xzf "$archive" -C "$dir.partial" && mv "$dir.partial" "$dir"
        rm -f "$archive"
    fi
    if usable "$exe"; then echo "$exe"; else say "The portable Python will not run here."; return 1; fi
}

installed_python() {
    for name in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python; do
        path=$(command -v "$name" 2>/dev/null) || continue
        # On a Mac without Apple's developer tools, /usr/bin/python3 only
        # offers to install them (about 1 GB) - and gives Python 3.9.
        if [ "$path" = /usr/bin/python3 ] && [ "$(uname -s)" = Darwin ] && ! xcode-select -p >/dev/null 2>&1; then
            continue
        fi
        if usable "$path"; then echo "$path"; return 0; fi
    done
    # Installed but not on PATH: python.org's and Homebrew's on a Mac, pyenv,
    # conda, and uv's.
    for path in "$MAC_FRAMEWORK"/Versions/3.*/bin/python3 \
                /opt/homebrew/bin/python3 /usr/local/bin/python3 \
                "$HOME"/.pyenv/shims/python3 "$HOME"/miniconda3/bin/python3 "$HOME"/anaconda3/bin/python3 \
                "$HOME"/.local/bin/python3.1[0-9]; do
        if [ -x "$path" ] && usable "$path"; then echo "$path"; return 0; fi
    done
    return 1
}

install_mac_python() {
    mkdir -p "$PYTHON_HOME"
    pkg=$PYTHON_HOME/python.pkg
    say "No Python 3.10 or newer here: installing Python $MAC_VERSION from python.org (signed;"
    say "about 45 MB). You can remove it again when the tool finishes."
    fetch "$MAC_PKG_URL" "$pkg" || { rm -f "$pkg"; say "The download failed."; return 1; }
    got=$(sha256 "$pkg")
    if [ "$got" != "$MAC_PKG_SHA256" ]; then
        rm -f "$pkg"; say "The download is not the expected file (SHA-256 $got) - not installed."; return 1
    fi
    # Recorded only if it was not there before: removing it later must never
    # take away a Python of the user's own.
    [ -d "$MAC_FRAMEWORK/Versions/$MAC_VERSION" ] || fresh=yes
    say "macOS asks for your password, to install it:"
    status=0
    sudo installer -pkg "$pkg" -target / >/dev/null || status=$?
    rm -f "$pkg"
    [ "$status" = 0 ] || { say "Installing Python failed."; return 1; }
    [ "${fresh:-}" = yes ] && echo "python.org Python $MAC_VERSION" > "$MARKER"
    echo "$MAC_PYTHON"
}

remove_mac_python() {
    # Everything python.org's installer put in place for this version, in one
    # sudo: the framework version, its app folder, its links in
    # /usr/local/bin, and the installer's receipts. Current then points at
    # whichever version is left, if any.
    sudo sh -c '
        framework=$1 version=$2
        for link in /usr/local/bin/*; do
            [ -L "$link" ] || continue
            case "$(readlink "$link")" in *Python.framework/Versions/$version/*) rm -f "$link" ;; esac
        done
        rm -rf "$framework/Versions/$version" "/Applications/Python $version"
        left=$(ls "$framework/Versions" 2>/dev/null | grep -v "^Current$" | sort -t. -k1,1n -k2,2n | tail -n 1)
        if [ -n "$left" ]; then ln -sfn "$left" "$framework/Versions/Current"; else rm -rf "$framework"; fi
        for receipt in $(pkgutil --pkgs | grep "^org\.python\.Python\..*$version"); do
            pkgutil --forget "$receipt" >/dev/null
        done
    ' sh "$MAC_FRAMEWORK" "$MAC_VERSION"
}

offer_removal() {
    [ -f "$MARKER" ] || return 0
    echo
    echo "$(cat "$MARKER") was installed for this tool. Remove it now, to leave nothing"
    echo "behind? If you will run the tool again - for another plug, or to finish this"
    echo "one - keep it: the next run then starts straight away."
    printf 'Remove Python now? [y/N] '
    read -r answer || answer=""
    case "$answer" in
    y|Y|yes|YES|Yes)
        if remove_mac_python; then rm -f "$MARKER"; echo "Removed."; else echo "Removing it failed."; fi ;;
    *) echo "Kept." ;;
    esac
}

python=$(portable_python || installed_python || true)
if [ -z "$python" ]; then
    case "$(uname -s)" in
    Darwin)
        python=$(install_mac_python) || exit 1 ;;
    *)
        say "The conversion tool needs Python 3.10 or newer, and none could be found or"
        say "downloaded."
        if command -v python3 >/dev/null 2>&1; then
            say "This computer has $(python3 --version 2>&1), which is too old. Newer Linux"
            say "releases have one (Ubuntu 22.04, Debian 12, Fedora 36 and later)."
        elif command -v apt-get >/dev/null 2>&1; then say "Install it with:  sudo apt install python3"
        elif command -v dnf >/dev/null 2>&1; then say "Install it with:  sudo dnf install python3"
        elif command -v pacman >/dev/null 2>&1; then say "Install it with:  sudo pacman -S python"
        else say "Install python3 with your distribution's package manager."
        fi
        say "Then run this again."
        exit 1 ;;
    esac
fi

# A Mac Python that cannot find web certificates of its own - python.org's,
# until its "Install Certificates" step has run - is pointed at the ones
# macOS keeps, for this run only: nothing in that Python is changed.
if [ "$(uname -s)" = Darwin ] && [ -f /etc/ssl/cert.pem ] && ! "$python" -c 'import os, ssl, sys
p = ssl.get_default_verify_paths()
sys.exit(0 if (p.cafile and os.path.exists(p.cafile)) or (p.capath and os.path.isdir(p.capath) and os.listdir(p.capath)) else 1)' >/dev/null 2>&1; then
    export SSL_CERT_FILE=/etc/ssl/cert.pem
fi

[ $# -gt 0 ] || set -- run
# The tool runs as a child, so the offer to remove Python still comes after a
# Ctrl-C (which the tool handles itself).
trap ':' INT
status=0
"$python" "$here/tools/s60_autoflash.py" "$@" || status=$?
trap - INT
offer_removal
exit "$status"
