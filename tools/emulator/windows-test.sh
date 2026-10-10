#!/bin/bash
# Test the conversion on Windows from a Linux machine: the emulated plug in
# Docker as usual (emu.sh --windows), and beside it Windows Server 2025 (Server
# Core) in a container of its own (windows/vm.Dockerfile) - one layer of KVM, nothing
# nested. Windows is wired onto the plug's home network, you work in it over
# Remote Desktop, and every run starts from a clean copy of it.
#
#   tools/emulator/windows-test.sh [--cloud] [--fresh] [--no-firewall] [--rebuild]
#       [--classic-console] [--first-run] [--rebuild-layer=installed|rig|tool]
#
# Options as emu.sh's (a conversion needs --cloud). The conversion runs in
# OpenConsole, Windows Terminal's own console, as on Windows 11; with
# --classic-console in Windows' classic one, as on Windows 10. Windows is kept
# in layers: installed (from Microsoft's evaluation ISO, 8.2 GB, no sign-up),
# rig (what the tests need) and tool (the tool's own Python, as its first run
# leaves it). A run starts from tool, or with --first-run from rig, so the tool
# sets itself up as on a new computer. A layer whose scripts changed is set up
# again before the run, with those above it - a few minutes, or 10-15 for
# installed; --rebuild-layer does it anyway. The first run lists what this
# computer needs and gets it after one question. Windows gets 2 GB and 2 CPUs
# (S60_WINDOWS_RAM=3G, S60_WINDOWS_CPUS=4). See docs/emulator.md.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
CACHE=${S60_WINDOWS_CACHE:-$HOME/.cache/s60-emu/windows}
VM_IMAGE=s60-win
# Microsoft's Windows Server 2025 evaluation ISO (en-US, build 26100.32230),
# from the Evaluation Center. Pinned: a changed file is reported, never used unseen.
ISO_URL="https://go.microsoft.com/fwlink/?linkid=2345730"
ISO_SHA256=${S60_WINDOWS_ISO_SHA256:-7b052573ba7894c9924e3e87ba732ccd354d18cb75a883efa9b900ea125bfd51}
# Windows Terminal's release, for its console, OpenConsole: Terminal itself
# cannot start on Server Core, which has no XAML. Pinned like the ISO.
TERMINAL_URL="https://github.com/microsoft/terminal/releases/download/v1.25.2733.0/Microsoft.WindowsTerminal_1.25.2733.0_x64.zip"
TERMINAL_SHA256=bf3ef2012f6c44d8340a4c58125acc9498d19b580f9890dc043cdf831852e796
# Not a secret: Remote Desktop listens on this computer's 127.0.0.1 only.
PASSWORD=S60-emulator
# What Windows gets: Server Core with only the tool to run is content in 2 GB.
# Its container is capped at that plus 1 GB for QEMU itself and the disk tools
# between runs, without swap, so it can never take more of this computer.
RAM=${S60_WINDOWS_RAM:-2G}
CPUS=${S60_WINDOWS_CPUS:-2}
if [[ $RAM =~ ^([1-9][0-9]*)G$ ]]; then ram_mb=$(( BASH_REMATCH[1] * 1024 ))
elif [[ $RAM =~ ^([1-9][0-9]*)M$ ]]; then ram_mb=${BASH_REMATCH[1]}
else echo "S60_WINDOWS_RAM is a size such as 2G or 3072M, not $RAM"; exit 2
fi
cap_mb=$(( ram_mb + 1024 ))

emu_flags=() rebuild=0 console=openconsole from=tool rebuild_layer="" fresh=0
for arg in "$@"; do
    case "$arg" in
    --first-run) from=rig ;;
    --rebuild-layer=installed|--rebuild-layer=rig|--rebuild-layer=tool) rebuild_layer=${arg#*=} ;;
    --classic-console) console=classic ;;
    --fresh) fresh=1; emu_flags+=("$arg") ;;
    --cloud|--no-firewall|--pcap) emu_flags+=("$arg") ;;
    --rebuild) rebuild=1; emu_flags+=("$arg") ;;
    *) sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
    esac
done

say() { printf '%s\n' "$*"; }

# -- Windows' layers -------------------------------------------------------------------
# What each layer is set up from, with what the layer below it was: when that
# changes, the layer is set up again, and so are those above it.
LAYERS=(installed rig tool)
built_from() {  # $1: a layer
    local guest=emulator/windows/guest
    case "$1" in
    installed) (cd "$ROOT/tools" && sha256sum $guest/unattend.xml $guest/firstlogon.ps1; echo "$ISO_SHA256") ;;
    rig) (built_from installed; cd "$ROOT/tools" && sha256sum $guest/rig.ps1 emulator/windows-bootstrap.ps1 windows-python.ps1) ;;
    tool) (built_from rig; cd "$ROOT/tools" && sha256sum $guest/tool.ps1 windows-python.ps1) ;;
    esac | sha256sum | cut -c1-16
}
stale=() forced=0
for name in "${LAYERS[@]}"; do
    [ "$name" = "$rebuild_layer" ] && forced=1
    if [ "$forced" = 1 ] || [ ! -f "$CACHE/$name.qcow2" ] ||
       [ "$(cat "$CACHE/$name.built-from" 2>/dev/null)" != "$(built_from "$name")" ]; then
        stale+=("$name")
        forced=1  # and every layer above it
    fi
done

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

if [ "${stale[0]:-}" = installed ]; then
    free=$(df -Pk "$(dirname "$CACHE")" 2>/dev/null | awk 'NR==2 {print int($4/1048576)}')
    if [ -n "$free" ] && [ "$free" -lt 30 ]; then
        say "Windows needs about 30 GB free under $CACHE while it is installed; there is ${free} GB."
        exit 1
    fi
    [ -f "$CACHE/server2025-eval.iso" ] || plan+=("Microsoft's Windows Server 2025 evaluation ISO (8.2 GB download, checked against its pinned SHA-256, kept for installing again)")
    plan+=("Windows Server Core installed from it (10-15 minutes), then set up for the tests and the tool (a few minutes more)")
    [ -f "$CACHE/base.qcow2" ] && plan+=("The disk from before Windows was kept in layers removed (base.qcow2, about 10 GB)")
    user_steps+=(windows)
fi
if [ ! -f "$CACHE/windows-terminal.zip" ]; then
    plan+=("Windows Terminal's release (12 MB, checked against its pinned SHA-256), for its console, OpenConsole, to run the conversion in as on Windows 11")
    user_steps+=(terminal)
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
            rm -f "$CACHE/base.qcow2" "$CACHE/base.provisioned-from"
            if [ ! -f "$CACHE/server2025-eval.iso" ]; then
                say "Downloading Microsoft's ISO..."
                curl -fL -C - --retry 5 -o "$CACHE/server2025-eval.iso.part" "$ISO_URL"
                got=$(sha256sum "$CACHE/server2025-eval.iso.part" | cut -d' ' -f1)
                if [ "$got" != "$ISO_SHA256" ]; then
                    say "Microsoft's ISO is not the one this was made for (SHA-256 $got)."
                    say "If they have published a new one, try it with S60_WINDOWS_ISO_SHA256=$got"
                    exit 1
                fi
                mv "$CACHE/server2025-eval.iso.part" "$CACHE/server2025-eval.iso"
            fi
            ;;
        terminal)
            mkdir -p "$CACHE"
            curl -fsSL --retry 5 -o "$CACHE/windows-terminal.zip.part" "$TERMINAL_URL"
            got=$(sha256sum "$CACHE/windows-terminal.zip.part" | cut -d' ' -f1)
            if [ "$got" != "$TERMINAL_SHA256" ]; then
                say "Windows Terminal's release is not the one this was made for (SHA-256 $got)."
                exit 1
            fi
            mv "$CACHE/windows-terminal.zip.part" "$CACHE/windows-terminal.zip"
            ;;
        esac
    done
fi

# -- the images ----------------------------------------------------------------------
# Rebuilt when what it is built from changes, as emu.sh does the emulator's.
built_from=$(cd "$ROOT/tools/emulator/windows" && find . -type f -print0 \
    | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16)
if [ "$rebuild" = 1 ] || [ "$(docker image inspect -f '{{index .Config.Labels "s60.built-from"}}' \
        "$VM_IMAGE" 2>/dev/null)" != "$built_from" ]; then
    say "Building the Windows VM's image..."
    docker build -q -t "$VM_IMAGE" --label "s60.built-from=$built_from" \
        -f "$ROOT/tools/emulator/windows/vm.Dockerfile" "$ROOT/tools/emulator/windows" >/dev/null
fi
vm() {  # vm DOCKER-RUN-OPTIONS... -- VM-RUN-COMMAND...: the Windows VM container
    local options=()
    while [ "$1" != -- ]; do options+=("$1"); shift; done
    shift
    docker run "${options[@]}" --device /dev/kvm -e "KEYBOARD=${LANG:-}" -e "WINDOWS_PASSWORD=$PASSWORD" \
        -e "HOST_UID=$(id -u)" -e "HOST_GID=$(id -g)" \
        -e "RAM=$RAM" -e "CPUS=$CPUS" --memory "${cap_mb}m" --memory-swap "${cap_mb}m" --cpus "$CPUS" \
        -v "$CACHE:/cache" -v "$ROOT:/repo:ro" "$VM_IMAGE" "$@"
}
avail_mb=$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo)
if [ "$avail_mb" -lt "$(( cap_mb + 256 ))" ]; then  # the emulator's container too
    say "Windows and the emulator may use up to $(( cap_mb + 256 )) MB; this computer has ${avail_mb} MB free."
    say "Close what you can, or give Windows less (S60_WINDOWS_RAM=1536M), or it may be slow."
fi
for name in "${stale[@]}"; do
    rm -f "$CACHE/$name.built-from"  # until it is set up again
    if [ "$name" = installed ]; then
        say "Installing Windows - watch it with a VNC viewer on localhost:5900 if you like."
        vm --rm --name s60-win-setup -p 127.0.0.1:5900:5900 -- install
    else
        say "Setting up Windows' $name layer (a few minutes)..."
        vm --rm --name s60-win-setup -p 127.0.0.1:5900:5900 -- layer "$name"
    fi
    built_from "$name" > "$CACHE/$name.built-from"
done

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
vm -d --name s60-win --stop-timeout 150 -e "LINK_HOST=$emu_ip" -e "CONSOLE=$console" -e "LAYER=$from" -e "FRESH=$fresh" \
    -p 127.0.0.1:3389:3389 -p 127.0.0.1:5900:5900 -- run >/dev/null

say "Windows is starting (about a minute). Connect with Remote Desktop:"
say "    localhost:3389    user s60, password $PASSWORD"
say "    e.g.  xfreerdp /v:localhost /u:s60 /p:$PASSWORD /cert:ignore /dynamic-resolution"
say "(or watch it with a VNC viewer on localhost:5900). It opens the link and a"
say "window to run the conversion in. This terminal is the emulator: plug restart,"
say "plug log, ... - leaving it (exit, or Ctrl-b d) shuts Windows down too."
read -r -p "Press Enter to open the emulator here. " _
docker attach s60-emu || true
