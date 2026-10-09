#!/bin/bash
# Test the conversion on Windows from a Linux machine: the emulated plug in
# Docker as usual (emu.sh --windows), and beside it Windows Server 2025 in a
# container of its own (windows/vm.Dockerfile) - one layer of KVM, nothing
# nested. Windows is wired onto the plug's home network, you work in it over
# Remote Desktop, and every run starts from a clean copy of it.
#
#   tools/emulator/windows-test.sh [--cloud] [--fresh] [--no-firewall] [--rebuild]
#
# Options as emu.sh's (a conversion needs --cloud). The first run checks what
# this computer needs, lists it, and gets it all after one question:
# Microsoft's pre-installed evaluation disk (11.7 GB, no sign-up), made ready
# once - its first start, and the conversion tool's prerequisites installed by
# tools/emulator/windows-bootstrap.ps1 - in 10-20 minutes. Later runs start in
# about a minute. See docs/emulator.md, "Windows from Linux".
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
CACHE=${S60_WINDOWS_CACHE:-$HOME/.cache/s60-emu/windows}
VM_IMAGE=s60-win
# Microsoft's Windows Server 2025 Datacenter evaluation VHDX (en-US), from the
# Evaluation Center. Pinned: a changed file is reported, never used unseen.
DISK_URL="https://go.microsoft.com/fwlink/?linkid=2345826"
DISK_SHA256=${S60_WINDOWS_DISK_SHA256:-2d175924c8e647969a82e36f931b22397108bd94a030e0e947b7e66e47e0be9a}
# Not a secret: Remote Desktop listens on this computer's 127.0.0.1 only.
PASSWORD=S60-emulator

emu_flags=() rebuild=0
for arg in "$@"; do
    case "$arg" in
    --cloud|--fresh|--no-firewall|--pcap) emu_flags+=("$arg") ;;
    --rebuild) rebuild=1; emu_flags+=("$arg") ;;
    *) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
    esac
done

say() { printf '%s\n' "$*"; }

# -- what this computer needs ----------------------------------------------------
if [ "$(uname -s)" != Linux ] || [ ! -e /dev/kvm ]; then
    say "This needs Linux with KVM (/dev/kvm), to run Windows at full speed."
    say "On a Mac, or without KVM: use any Windows machine or VM instead, set up by"
    say "tools/emulator/windows-bootstrap.ps1 (docs/emulator.md, \"On a Windows computer\")."
    exit 1
fi
for f in "$ROOT/captures/emulation/flash.bin" "$ROOT/captures/emulation/efuse.bin"; do
    [ -f "$f" ] || { say "Missing $f - see docs/emulator.md"; exit 1; }
done

sudo_steps=() user_steps=() plan=()
pkg=""
command -v apt-get >/dev/null && pkg=apt
command -v dnf >/dev/null && pkg=${pkg:-dnf}

if ! command -v docker >/dev/null; then
    case "$pkg" in
    apt) sudo_steps+=("apt-get install -y docker.io" "systemctl enable --now docker" "usermod -aG docker $USER") ;;
    dnf) sudo_steps+=("dnf install -y moby-engine" "systemctl enable --now docker" "usermod -aG docker $USER") ;;
    *) say "Docker is needed: install it with your distribution's packages, then run this again."; exit 1 ;;
    esac
    plan+=("Docker, and you in its group (log out and in again after)")
elif ! docker info >/dev/null 2>&1; then
    sudo_steps+=("usermod -aG docker $USER")
    plan+=("You in the docker group, to use Docker without sudo (log out and in again after)")
fi

venv=$ROOT/.venv
[ -x "$venv/bin/python" ] || venv=$HOME/.local/share/s60-emu/venv
if ! "$venv/bin/python" -c 'import esp_idf_nvs_partition_gen' 2>/dev/null; then
    if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
        case "$pkg" in
        apt) sudo_steps+=("apt-get install -y python3-venv") ;;
        dnf) ;;  # part of python3 there
        *) say "Python's venv module is needed: install it, then run this again."; exit 1 ;;
        esac
    fi
    user_steps+=(nvs)
    plan+=("ESP-IDF's NVS generator, in $venv (emu.sh rewrites the dump's Wi-Fi with it)")
fi

if [ ! -f "$CACHE/base.qcow2" ]; then
    free=$(df -Pk "$(dirname "$CACHE")" 2>/dev/null | awk 'NR==2 {print int($4/1048576)}')
    if [ -n "$free" ] && [ "$free" -lt 30 ]; then
        say "Windows needs about 30 GB free under $CACHE while it is first made ready; there is ${free} GB."
        exit 1
    fi
    [ -f "$CACHE/server2025-eval.vhdx" ] || [ -f "$CACHE/server2025-eval.qcow2" ] || plan+=("Microsoft's Windows Server 2025 evaluation disk (11.7 GB download, checked against its pinned SHA-256)")
    plan+=("Windows made ready once (10-20 minutes): it installs Python 3.12 and OpenVPN's TAP-Windows6 driver itself, with windows-bootstrap.ps1")
    user_steps+=(windows)
fi

if [ ${#plan[@]} -gt 0 ]; then
    say "To test the conversion on Windows, this computer needs:"
    for line in "${plan[@]}"; do say "    - $line"; done
    [ ${#sudo_steps[@]} -gt 0 ] && say "Some of it needs administrator rights: sudo asks once, for all of it."
    read -r -p "Get all of it now? [y/N] " answer
    case "$answer" in y|Y|yes|YES) ;; *) say "Nothing was changed."; exit 1 ;; esac
    if [ ${#sudo_steps[@]} -gt 0 ]; then
        script=""
        [ "$pkg" = apt ] && script="apt-get update -qq; "
        for step in "${sudo_steps[@]}"; do script+="$step; "; done
        sudo sh -ec "$script"
        if ! docker info >/dev/null 2>&1; then
            say "Docker is installed. Log out and in again (so your account is in its group),"
            say "then run this again - it carries on from here."
            exit 0
        fi
    fi
    for step in "${user_steps[@]}"; do
        case "$step" in
        nvs)
            [ -x "$venv/bin/python" ] || python3 -m venv "$venv"
            "$venv/bin/python" -m pip install -q esp-idf-nvs-partition-gen
            ;;
        windows)
            mkdir -p "$CACHE"
            if [ ! -f "$CACHE/server2025-eval.vhdx" ] && [ ! -f "$CACHE/server2025-eval.qcow2" ]; then
                say "Downloading Microsoft's disk..."
                curl -fL -C - --retry 5 -o "$CACHE/server2025-eval.vhdx.part" "$DISK_URL"
                got=$(sha256sum "$CACHE/server2025-eval.vhdx.part" | cut -d' ' -f1)
                if [ "$got" != "$DISK_SHA256" ]; then
                    say "Microsoft's disk is not the one this was made for (SHA-256 $got)."
                    say "If they have published a new one, try it with S60_WINDOWS_DISK_SHA256=$got"
                    exit 1
                fi
                mv "$CACHE/server2025-eval.vhdx.part" "$CACHE/server2025-eval.vhdx"
            fi
            ;;
        esac
    done
fi

# -- the images ----------------------------------------------------------------------
if [ "$rebuild" = 1 ] || ! docker image inspect "$VM_IMAGE" >/dev/null 2>&1; then
    say "Building the Windows VM's image..."
    docker build -q -t "$VM_IMAGE" -f "$ROOT/tools/emulator/windows/vm.Dockerfile" \
        "$ROOT/tools/emulator/windows" >/dev/null
fi
vm() {  # the Windows VM container, with its disks and this checkout
    docker run "$@" --device /dev/kvm -e "KEYBOARD=${LANG:-}" -e "WINDOWS_PASSWORD=$PASSWORD" \
        -v "$CACHE:/cache" -v "$ROOT:/repo:ro" "$VM_IMAGE"
}
if [ ! -f "$CACHE/base.qcow2" ]; then
    say "Making Windows ready - watch it with a VNC viewer on localhost:5900 if you like."
    vm --rm --name s60-win-prepare -p 127.0.0.1:5900:5900 prepare
fi

# -- a run -----------------------------------------------------------------------------
for name in s60-emu s60-win; do
    if docker ps -q --filter "name=^$name\$" | grep -q .; then
        say "$name is already running. Stop it first: docker stop $name"
        exit 1
    fi
done
# Its own output stays: with --cloud it asks you to confirm the real plug is unplugged.
"$ROOT/tools/emulator/emu.sh" --windows --detach "${emu_flags[@]}"
stop_all() {
    say "Shutting Windows down (its captures are kept for next time)..."
    docker stop -t 150 s60-win >/dev/null 2>&1 || true
    docker stop s60-emu >/dev/null 2>&1 || true
}
trap stop_all EXIT
emu_ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' s60-emu)
docker rm -f s60-win >/dev/null 2>&1 || true
vm -d --name s60-win --stop-timeout 150 -e "LINK_HOST=$emu_ip" \
    -p 127.0.0.1:3389:3389 -p 127.0.0.1:5900:5900 run >/dev/null

say "Windows is starting (about a minute). Connect with Remote Desktop:"
say "    localhost:3389    user s60, password $PASSWORD"
say "    e.g.  xfreerdp /v:localhost /u:s60 /p:$PASSWORD /cert:ignore /dynamic-resolution"
say "(or watch it with a VNC viewer on localhost:5900). It opens the link and a"
say "window to run the conversion in. This terminal is the emulator: plug restart,"
say "plug log, ... - leaving it (exit, or Ctrl-b d) shuts Windows down too."
read -r -p "Press Enter to open the emulator here. " _
docker attach s60-emu || true
