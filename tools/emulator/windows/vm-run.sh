#!/bin/bash
# Inside the Windows VM container (vm.Dockerfile), started by windows-test.sh.
#
#   vm-run prepare   once: Microsoft's evaluation disk -> base.qcow2, with the
#                    conversion tool's prerequisites installed by its own
#                    windows-bootstrap.ps1, and a user who logs on by itself
#   vm-run run       a throwaway copy of base.qcow2, with this checkout in it,
#                    until Windows is shut down or the container is stopped
#
# /cache holds the disks (and the conversion's captures, kept between runs);
# /repo is the checkout, read-only. LINK_HOST is the emulator's address.
set -euo pipefail

CACHE=/cache
VHDX=$CACHE/server2025-eval.vhdx
PRISTINE=$CACHE/server2025-eval.qcow2
BASE=$CACHE/base.qcow2
GUEST=/opt/s60win/guest
MON=/run/qemu-monitor.sock
# Not a secret: the VM is reachable from this computer only (127.0.0.1).
PASSWORD=${WINDOWS_PASSWORD:-S60-emulator}

log() { echo "[vm] $*"; }

qemu() {
    local disk=$1
    cp /usr/share/OVMF/OVMF_VARS_4M.fd /run/vars.fd
    # SATA disk and e1000e network: Windows has drivers for both built in.
    # The hv_ flags tell Windows it is a guest and give it timers it trusts:
    # without them, under nested virtualisation, it spins at boot for good.
    qemu-system-x86_64 \
        -machine q35,accel=kvm \
        -cpu host,hv_relaxed,hv_vapic,hv_spinlocks=0x1fff,hv_time,hv_vpindex,hv_synic,hv_stimer,hv_frequencies,hv_runtime \
        -global kvm-pit.lost_tick_policy=discard -smp "${CPUS:-4}" -m "${RAM:-6G}" \
        -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd \
        -drive if=pflash,format=raw,file=/run/vars.fd \
        -device ahci,id=ahci -drive id=disk,if=none,format=qcow2,file="$disk" \
        -device ide-hd,drive=disk,bus=ahci.0 \
        -netdev user,id=net,hostfwd=tcp::3389-:3389 -device e1000e,netdev=net \
        -device qemu-xhci -device usb-tablet -rtc base=utc \
        -vnc :0 -monitor "unix:$MON,server,nowait" -display none &
    QEMU=$!
}

# A clean shutdown when the container is stopped, so the disk can be read after.
stop_windows() {
    [ -n "${QEMU:-}" ] || return 0
    log "shutting Windows down..."
    python3 -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); s.sendall(b"system_powerdown\n")' "$MON" 2>/dev/null || true
    for _ in $(seq 120); do kill -0 "$QEMU" 2>/dev/null || return 0; sleep 1; done
    kill "$QEMU" 2>/dev/null || true
}

# The emulator's interface name in Windows, the keyboard, and the account.
unattend() {
    local layout=0409:00000409
    case "${KEYBOARD:-}" in en_GB*|en-GB*) layout=0809:00000809 ;; esac
    sed -e "s|@PASSWORD@|$PASSWORD|g" -e "s|@LAYOUT@|$layout|g" "$GUEST/unattend.xml"
}

prepare() {
    [ -f "$BASE" ] && { log "base.qcow2 is already prepared"; return 0; }
    # Microsoft's disk, converted once and never written: base.qcow2 is a
    # layer over it, so a failed attempt is retried without converting again.
    if [ ! -f "$PRISTINE" ]; then
        [ -f "$VHDX" ] || { log "missing $VHDX"; exit 1; }
        log "converting Microsoft's disk (a few minutes)..."
        qemu-img convert -p -O qcow2 "$VHDX" "$PRISTINE.partial"
        mv "$PRISTINE.partial" "$PRISTINE"
        rm -f "$VHDX"
    fi
    rm -f "$CACHE/base.partial.qcow2"
    qemu-img create -q -f qcow2 -b "$(basename "$PRISTINE")" -F qcow2 "$CACHE/base.partial.qcow2"
    log "writing the answer file and the set-up scripts into it..."
    unattend > /run/unattend.xml
    tar -C /repo -cf /run/repo.tar --exclude=./.git --exclude=./.venv --exclude=./.python --exclude=./captures \
        --exclude='*/__pycache__' .
    guestfish --rw -a "$CACHE/base.partial.qcow2" -i <<EOF
mkdir-p /Windows/Panther
upload /run/unattend.xml /Windows/Panther/unattend.xml
mkdir-p /S60/repo
upload $GUEST/provision.ps1 /S60/provision.ps1
upload $GUEST/start.ps1 /S60/start.ps1
tar-in /run/repo.tar /S60/repo
EOF
    log "first start: Windows sets itself up, then installs the prerequisites"
    log "(watch on VNC, localhost:5900 - this takes 10-20 minutes)..."
    qemu "$CACHE/base.partial.qcow2"
    wait "$QEMU" || true
    QEMU=
    guestfish --ro -a "$CACHE/base.partial.qcow2" -i <<EOF > /run/provision.log 2>&1 || true
cat /S60/provision.log
EOF
    cat /run/provision.log
    if ! guestfish --ro -a "$CACHE/base.partial.qcow2" -i is-file /S60/provisioned | grep -q true; then
        log "set-up did not finish - see above. Running this again starts it afresh."
        exit 1
    fi
    guestfish --rw -a "$CACHE/base.partial.qcow2" -i rm-rf /S60/repo
    mv "$CACHE/base.partial.qcow2" "$BASE"
    log "prepared: base.qcow2"
}

run() {
    [ -f "$BASE" ] || { log "not prepared yet - run prepare first"; exit 1; }
    : "${LINK_HOST:?set LINK_HOST to the address of the emulator}"
    local disk=/run/run.qcow2
    qemu-img create -q -f qcow2 -b "$BASE" -F qcow2 "$disk"
    # This checkout, without its private captures; the conversion's own
    # captures from earlier runs, so an unfinished one can be resumed.
    tar -C /repo -cf /run/repo.tar --exclude=./.git --exclude=./.venv --exclude=./.python --exclude=./captures \
        --exclude='*/__pycache__' .
    mkdir -p "$CACHE/captures"
    if [ ! -f "$CACHE/captures/settings.json" ] && [ -f /repo/captures/settings.json ]; then
        cp /repo/captures/settings.json "$CACHE/captures/"   # the eWeLink email, to save typing it
    fi
    tar -C "$CACHE/captures" -cf /run/captures.tar .
    printf '{"link_host": "%s"}\n' "$LINK_HOST" > /run/config.json
    guestfish --rw -a "$disk" -i <<EOF
mkdir-p /S60/repo/captures
tar-in /run/repo.tar /S60/repo
tar-in /run/captures.tar /S60/repo/captures
upload /run/config.json /S60/run.json
EOF
    trap 'stop_windows' TERM INT
    log "starting Windows - Remote Desktop: localhost:3389, VNC: localhost:5900"
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
prepare) prepare ;;
run) run ;;
*) echo "usage: vm-run prepare | run" >&2; exit 2 ;;
esac
