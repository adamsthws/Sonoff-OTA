#!/bin/bash
# Inside the Windows VM container (vm.Dockerfile), started by windows-test.sh.
#
# Windows is kept in three layers, each a disk holding only its changes to
# the one below, so any of them can be set up again without those below it:
#
#   vm-run install     1, installed.qcow2: Windows Server 2025 (Server Core),
#                      installed from Microsoft's evaluation ISO by an
#                      unattended Setup, with a user who logs on by itself and
#                      a hook that runs start.ps1 at logon (firstlogon.ps1)
#   vm-run layer rig   2, rig.qcow2: what the tests need (rig.ps1) - Remote
#                      Desktop, SSH, the link's TAP driver, adapter and Python
#   vm-run layer tool  3, tool.qcow2: what the tool's first run leaves (tool.ps1)
#   vm-run run         a throwaway layer on rig or tool (LAYER), with this
#                      checkout in it, until Windows is shut down or the
#                      container is stopped
#
# Setting a layer up again drops those above it. /cache holds the disks (and
# the conversion's captures, kept between runs); /repo is the checkout,
# read-only. LINK_HOST is the emulator's address; CONSOLE what the conversion
# runs in: openconsole (Windows Terminal's console, from
# /cache/windows-terminal.zip) or classic (Windows' own); FRESH=1 forgets the
# conversion's captures first. What is written to /cache belongs to
# HOST_UID:HOST_GID, the person who started it, not root.
set -euo pipefail

CACHE=/cache
ISO=$CACHE/server2025-eval.iso
LAYERS=(installed rig tool)
# The image Setup installs from the ISO, by its name in install.wim:
# Datacenter, Server Core. (SERVERDATACENTER, without CORE, has the desktop.)
IMAGE_NAME=${WINDOWS_IMAGE:-Windows Server 2025 SERVERDATACENTERCORE}
GUEST=/opt/s60win/guest
MON=/run/qemu-monitor.sock
# Not a secret: the VM is reachable from this computer only (127.0.0.1).
PASSWORD=${WINDOWS_PASSWORD:-S60-emulator}

log() { echo "[vm] $*"; }

own_cache() {  # everything in /cache, the host user's to read and delete
    [ -n "${HOST_UID:-}" ] && chown -R "$HOST_UID:${HOST_GID:-$HOST_UID}" "$CACHE" 2>/dev/null
    return 0
}
trap own_cache EXIT

qemu() {  # $1 the disk; then any extra QEMU options (Setup's discs)
    local disk=$1
    shift
    cp /usr/share/OVMF/OVMF_VARS_4M.fd /run/vars.fd
    # SATA disk and e1000e network: Windows has drivers for both built in.
    # The hv_ flags tell Windows it is a guest and give it timers it trusts:
    # without them, under nested virtualisation, it spins at boot for good.
    qemu-system-x86_64 \
        -machine q35,accel=kvm \
        -cpu host,hv_relaxed,hv_vapic,hv_spinlocks=0x1fff,hv_time,hv_vpindex,hv_synic,hv_stimer,hv_frequencies,hv_runtime \
        -global kvm-pit.lost_tick_policy=discard -smp "${CPUS:-2}" -m "${RAM:-2G}" \
        -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd \
        -drive if=pflash,format=raw,file=/run/vars.fd \
        -device ahci,id=ahci -drive id=disk,if=none,format=qcow2,file="$disk" \
        -device ide-hd,drive=disk,bus=ahci.0,bootindex=2 \
        -netdev user,id=net,hostfwd=tcp::3389-:3389,hostfwd=tcp::22-:22 -device e1000e,netdev=net \
        -device qemu-xhci -device usb-tablet -rtc base=utc \
        -device virtio-serial-pci -chardev qemu-vdagent,id=vdagent,name=vdagent,clipboard=on \
        -device virtserialport,chardev=vdagent,name=com.redhat.spice.0 \
        -vnc :0 -monitor "unix:$MON,server,nowait" -display none "$@" &
    QEMU=$!
    # The screen in a browser too: noVNC's page, and its connection to VNC.
    # The clipboard goes both ways, through SPICE's agent in Windows (rig.ps1)
    # and the vdagent channel above, on VNC and here alike.
    # Once per container: each runs QEMU once.
    websockify --web /usr/share/novnc 6080 127.0.0.1:5900 >/run/websockify.log 2>&1 &
}

monitor() {  # one command to QEMU's monitor
    python3 -c 'import socket,sys,time; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); s.sendall(sys.argv[2].encode() + b"\n"); time.sleep(0.2)' \
        "$MON" "$1" 2>/dev/null || true
}

# Microsoft's ISO boots only after "Press any key to boot from CD or DVD", and
# then its boot menu waits, once a key has been pressed, for "Windows Setup"
# to be chosen. Enter does both - but later Setup has a Cancel button that
# Enter would press. So Enter is pressed only until Setup's own image is being
# read off the ISO in bulk, which is after both and before any Setup screen.
# Later boots, with nothing pressed, fall through to the disk Windows is on.
iso_read() {  # MB read off the ISO so far
    python3 -c '
import re, socket, sys, time
s = socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); time.sleep(0.2); s.recv(65536)
s.sendall(b"info blockstats\n"); time.sleep(0.5)
for line in s.recv(65536).decode(errors="replace").splitlines():
    if line.startswith("iso:"):
        print(int(re.search(r"rd_bytes=(\d+)", line).group(1)) >> 20)' "$MON" 2>/dev/null || echo 0
}

press_any_key() {
    for _ in $(seq 600); do
        sleep 1
        [ "$(iso_read)" -lt 32 ] || return 0
        monitor "sendkey ret"
    done
    log "Setup did not start from the ISO within 10 minutes"
}

repo_tar() {  # $1: this checkout, without its private captures, as a tar
    tar -C /repo -cf "$1" --exclude=./.git --exclude=./.venv --exclude=./.python --exclude=./captures \
        --exclude='*/__pycache__' .
}

set_up() {  # $1: a disk $2 (a layer's name) has just been set up on; shows its log
    guestfish --ro -a "$1" -i <<EOF > /run/setup.log 2>&1 || true
cat /S60/$2.log
EOF
    cat /run/setup.log
    guestfish --ro -a "$1" -i is-file "/S60/$2.done" | grep -q true
}

drop_above() {  # the layers above $1: built on what it was, so no longer valid
    local found=0 name
    for name in "${LAYERS[@]}"; do
        [ "$found" = 1 ] && rm -f "$CACHE/$name.qcow2"
        [ "$name" = "$1" ] && found=1
    done
    return 0
}

seed_disc() {  # the answer file and the first logon's scripts, on a disc
    local layout=0409:00000409
    case "${KEYBOARD:-}" in en_GB*|en-GB*) layout=0809:00000809 ;; esac
    rm -rf /run/seed && mkdir -p /run/seed/S60
    sed -e "s|@PASSWORD@|$PASSWORD|g" -e "s|@LAYOUT@|$layout|g" -e "s|@IMAGE@|$IMAGE_NAME|g" \
        "$GUEST/unattend.xml" > /run/seed/autounattend.xml
    cp "$GUEST/firstlogon.ps1" "$GUEST/start.ps1" /run/seed/S60/
    genisoimage -quiet -J -joliet-long -r -V S60SEED -o /run/seed.iso /run/seed
}

# A clean shutdown when the container is stopped, so the disk can be read after.
stop_windows() {
    [ -n "${QEMU:-}" ] || return 0
    log "shutting Windows down..."
    python3 -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); s.sendall(b"system_powerdown\n")' "$MON" 2>/dev/null || true
    for _ in $(seq 120); do kill -0 "$QEMU" 2>/dev/null || return 0; sleep 1; done
    kill "$QEMU" 2>/dev/null || true
}

install() {
    [ -f "$ISO" ] || { log "missing $ISO"; exit 1; }
    local disk=$CACHE/installed.partial.qcow2
    log "the answer file and the first logon's scripts, on a seed disc..."
    seed_disc
    rm -f "$disk"
    qemu-img create -q -f qcow2 "$disk" 64G
    log "installing Windows Server Core from the ISO, unattended; it shuts itself"
    log "down when done (watch at http://localhost:6080, or on VNC, localhost:5900)..."
    qemu "$disk" \
        -drive id=iso,if=none,media=cdrom,readonly=on,file="$ISO" -device ide-cd,drive=iso,bus=ahci.1,bootindex=1 \
        -drive id=seed,if=none,media=cdrom,readonly=on,file=/run/seed.iso -device ide-cd,drive=seed,bus=ahci.2
    press_any_key
    wait "$QEMU" || true
    QEMU=
    if ! set_up "$disk" installed; then
        log "Setup did not finish - see above. Running this again starts it afresh."
        exit 1
    fi
    drop_above installed
    mv "$disk" "$CACHE/installed.qcow2"
    log "layer 1 ready: installed.qcow2 (the ISO is kept, for installing again)"
}

# A layer on the one below it: its script, start.ps1 and this checkout copied
# in, with layer.txt, which makes start.ps1 run that script at logon; it shuts
# Windows down. The layer replaces the old one only once that worked.
layer() {
    local name=$1 below
    case "$name" in
    rig) below=installed ;;
    tool) below=rig ;;
    *) log "no layer '$name' (rig or tool)"; exit 2 ;;
    esac
    [ -f "$CACHE/$below.qcow2" ] || { log "no $below.qcow2 to build $name on"; exit 1; }
    local disk=$CACHE/$name.partial.qcow2
    log "setting up $name.qcow2 on $below.qcow2 (a few minutes; watch at http://localhost:6080)..."
    rm -f "$disk"
    # The layer below by its name alone, so the chain holds wherever /cache is.
    (cd "$CACHE" && qemu-img create -q -f qcow2 -b "$below.qcow2" -F qcow2 "$name.partial.qcow2")
    repo_tar /run/repo.tar
    printf '%s\n' "$name" > /run/layer.txt
    guestfish --rw -a "$disk" -i <<EOF
rm-rf /S60/repo
mkdir-p /S60/repo
tar-in /run/repo.tar /S60/repo
upload $GUEST/$name.ps1 /S60/$name.ps1
upload $GUEST/start.ps1 /S60/start.ps1
upload /run/layer.txt /S60/layer.txt
EOF
    if [ "$name" = rig ]; then
        # Fetched and checked by windows-test.sh; rig.ps1 installs them.
        [ -f "$CACHE/virtio-win-guest-tools.exe" ] || { log "missing $CACHE/virtio-win-guest-tools.exe"; exit 1; }
        guestfish --rw -a "$disk" -i upload "$CACHE/virtio-win-guest-tools.exe" /S60/virtio-win-guest-tools.exe
    fi
    qemu "$disk"
    wait "$QEMU" || true
    QEMU=
    if ! set_up "$disk" "$name"; then
        rm -f "$disk"
        log "$name did not finish - see above. The layers are as they were."
        exit 1
    fi
    guestfish --rw -a "$disk" -i rm-rf /S60/repo
    drop_above "$name"
    mv "$disk" "$CACHE/$name.qcow2"
    log "layer ready: $name.qcow2"
}

run() {
    local from=${LAYER:-tool}
    [ -f "$CACHE/$from.qcow2" ] || { log "no $from.qcow2 - set it up first (vm-run layer $from)"; exit 1; }
    : "${LINK_HOST:?set LINK_HOST to the address of the emulator}"
    local disk=/run/run.qcow2
    qemu-img create -q -f qcow2 -b "$CACHE/$from.qcow2" -F qcow2 "$disk"
    # This checkout, without its private captures; the conversion's own
    # captures from earlier runs, so an unfinished one can be resumed.
    repo_tar /run/repo.tar
    # --fresh: as a new user would, with no earlier run and no email to offer.
    [ "${FRESH:-0}" = 1 ] && rm -rf "$CACHE/captures"
    mkdir -p "$CACHE/captures"
    if [ "${FRESH:-0}" != 1 ] && [ ! -f "$CACHE/captures/settings.json" ] && [ -f /repo/captures/settings.json ]; then
        cp /repo/captures/settings.json "$CACHE/captures/"   # the eWeLink email, to save typing it
    fi
    tar -C "$CACHE/captures" -cf /run/captures.tar .
    local console=${CONSOLE:-openconsole}
    rm -rf /run/openconsole && mkdir /run/openconsole
    if [ "$console" = openconsole ]; then
        # OpenConsole.exe, and the notice its licence asks to go with it.
        python3 -c '
import os, sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
    for name in z.namelist():
        if os.path.basename(name) in ("OpenConsole.exe", "NOTICE.html"):
            open(os.path.join(sys.argv[2], os.path.basename(name)), "wb").write(z.read(name))
' "$CACHE/windows-terminal.zip" /run/openconsole
        [ -f /run/openconsole/OpenConsole.exe ] || { log "no OpenConsole.exe in $CACHE/windows-terminal.zip"; exit 1; }
    fi
    tar -C /run/openconsole -cf /run/openconsole.tar .
    printf '{"link_host": "%s", "console": "%s"}\n' "$LINK_HOST" "$console" > /run/config.json
    # The key windows-ssh.sh logs on with; start.ps1 lets it in. None: SSH by password.
    : > /run/authorized_keys
    [ -f "$CACHE/ssh/id_ed25519.pub" ] && cp "$CACHE/ssh/id_ed25519.pub" /run/authorized_keys
    # start.ps1 too, so a change to it needs no layer set up again.
    guestfish --rw -a "$disk" -i <<EOF
mkdir-p /S60/repo/captures
mkdir-p /S60/openconsole
tar-in /run/repo.tar /S60/repo
tar-in /run/captures.tar /S60/repo/captures
tar-in /run/openconsole.tar /S60/openconsole
upload $GUEST/start.ps1 /S60/start.ps1
upload /run/config.json /S60/run.json
upload /run/authorized_keys /S60/authorized_keys
EOF
    trap 'stop_windows' TERM INT
    log "starting Windows - Remote Desktop: localhost:3389, browser: http://localhost:6080, VNC: localhost:5900, SSH: port 22"
    qemu "$disk"
    # A stop signal interrupts the first wait; the second sees Windows off.
    wait "$QEMU" || true
    wait "$QEMU" 2>/dev/null || true
    QEMU=
    log "Windows is off; keeping the conversion's captures for next time"
    rm -rf /run/captures-out && mkdir /run/captures-out
    if guestfish --ro -a "$disk" -i copy-out /S60/repo/captures /run/captures-out 2>/dev/null; then
        cp -a /run/captures-out/captures/. "$CACHE/captures/"
    fi
    rm -f "$disk"
}

case "${1:-}" in
install) install ;;
layer) layer "${2:-}" ;;
run) run ;;
*) echo "usage: vm-run install | layer rig|tool | run" >&2; exit 2 ;;
esac
