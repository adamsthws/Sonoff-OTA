#!/bin/bash
# Inside the Windows VM container (vm.Dockerfile), started by windows-test.sh.
#
#   vm-run prepare   once: Windows Server 2025 (Server Core), installed from
#                    Microsoft's evaluation ISO by an unattended Setup ->
#                    base.qcow2, with the conversion tool's prerequisites
#                    installed by its own windows-bootstrap.ps1, and a user
#                    who logs on by itself
#   vm-run refresh   base.qcow2 set up again by the current provision.ps1 and
#                    windows-bootstrap.ps1, without installing Windows again
#   vm-run run       a throwaway copy of base.qcow2, with this checkout in it,
#                    until Windows is shut down or the container is stopped
#
# /cache holds the disks (and the conversion's captures, kept between runs);
# /repo is the checkout, read-only. LINK_HOST is the emulator's address;
# CONSOLE what the conversion runs in: openconsole (Windows Terminal's console,
# from /cache/windows-terminal.zip) or classic (Windows' own).
set -euo pipefail

CACHE=/cache
ISO=$CACHE/server2025-eval.iso
BASE=$CACHE/base.qcow2
# The image Setup installs from the ISO, by its name in install.wim:
# Datacenter, Server Core. (SERVERDATACENTER, without CORE, has the desktop.)
IMAGE_NAME=${WINDOWS_IMAGE:-Windows Server 2025 SERVERDATACENTERCORE}
GUEST=/opt/s60win/guest
MON=/run/qemu-monitor.sock
# Not a secret: the VM is reachable from this computer only (127.0.0.1).
PASSWORD=${WINDOWS_PASSWORD:-S60-emulator}

log() { echo "[vm] $*"; }

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
        -netdev user,id=net,hostfwd=tcp::3389-:3389 -device e1000e,netdev=net \
        -device qemu-xhci -device usb-tablet -rtc base=utc \
        -vnc :0 -monitor "unix:$MON,server,nowait" -display none "$@" &
    QEMU=$!
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

provisioned() {  # $1: a disk provision.ps1 has just run on; shows its log
    guestfish --ro -a "$1" -i <<EOF > /run/provision.log 2>&1 || true
cat /S60/provision.log
EOF
    cat /run/provision.log
    guestfish --ro -a "$1" -i is-file /S60/provisioned | grep -q true
}

seed_disc() {  # the answer file, the set-up scripts and this checkout, on a disc
    local layout=0409:00000409
    case "${KEYBOARD:-}" in en_GB*|en-GB*) layout=0809:00000809 ;; esac
    rm -rf /run/seed && mkdir -p /run/seed/S60/repo
    sed -e "s|@PASSWORD@|$PASSWORD|g" -e "s|@LAYOUT@|$layout|g" -e "s|@IMAGE@|$IMAGE_NAME|g" \
        "$GUEST/unattend.xml" > /run/seed/autounattend.xml
    cp "$GUEST/provision.ps1" "$GUEST/start.ps1" /run/seed/S60/
    tar -C /repo -cf - --exclude=./.git --exclude=./.venv --exclude=./.python --exclude=./captures \
        --exclude='*/__pycache__' . | tar -C /run/seed/S60/repo -xf -
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

prepare() {
    [ -f "$BASE" ] && { log "base.qcow2 is already prepared"; return 0; }
    [ -f "$ISO" ] || { log "missing $ISO"; exit 1; }
    log "the answer file, the set-up scripts and this checkout, on a seed disc..."
    seed_disc
    rm -f "$CACHE/base.partial.qcow2"
    qemu-img create -q -f qcow2 "$CACHE/base.partial.qcow2" 64G
    log "installing Windows Server Core from the ISO, unattended, then the tool's"
    log "prerequisites; it shuts itself down when done (watch on VNC, localhost:5900)..."
    qemu "$CACHE/base.partial.qcow2" \
        -drive id=iso,if=none,media=cdrom,readonly=on,file="$ISO" -device ide-cd,drive=iso,bus=ahci.1,bootindex=1 \
        -drive id=seed,if=none,media=cdrom,readonly=on,file=/run/seed.iso -device ide-cd,drive=seed,bus=ahci.2
    press_any_key
    wait "$QEMU" || true
    QEMU=
    if ! provisioned "$CACHE/base.partial.qcow2"; then
        log "set-up did not finish - see above. Running this again starts it afresh."
        exit 1
    fi
    guestfish --rw -a "$CACHE/base.partial.qcow2" -i rm-rf /S60/repo
    mv "$CACHE/base.partial.qcow2" "$BASE"
    rm -f "$ISO"   # only Setup needed it; a new base downloads it again
    log "prepared: base.qcow2"
}

# The base set up again, on a copy that replaces it only once that worked: the
# current scripts and checkout copied in, and a marker for start.ps1, which at
# logon runs provision.ps1 instead of a test run; that shuts Windows down.
refresh() {
    [ -f "$BASE" ] || { log "not prepared yet - run prepare first"; exit 1; }
    local disk=$CACHE/base.refresh.qcow2
    log "setting the base up again with the current scripts (a few minutes)..."
    cp --sparse=always "$BASE" "$disk"
    repo_tar /run/repo.tar
    guestfish --rw -a "$disk" -i <<EOF
rm-f /S60/provisioned
rm-rf /S60/repo
mkdir-p /S60/repo
tar-in /run/repo.tar /S60/repo
upload $GUEST/provision.ps1 /S60/provision.ps1
upload $GUEST/start.ps1 /S60/start.ps1
touch /S60/refresh
EOF
    qemu "$disk"
    wait "$QEMU" || true
    QEMU=
    if ! provisioned "$disk"; then
        rm -f "$disk"
        log "set-up did not finish - see above. The base is as it was."
        exit 1
    fi
    guestfish --rw -a "$disk" -i rm-rf /S60/repo
    mv "$disk" "$BASE"
    log "refreshed: base.qcow2"
}

run() {
    [ -f "$BASE" ] || { log "not prepared yet - run prepare first"; exit 1; }
    : "${LINK_HOST:?set LINK_HOST to the address of the emulator}"
    local disk=/run/run.qcow2
    qemu-img create -q -f qcow2 -b "$BASE" -F qcow2 "$disk"
    # This checkout, without its private captures; the conversion's own
    # captures from earlier runs, so an unfinished one can be resumed.
    repo_tar /run/repo.tar
    mkdir -p "$CACHE/captures"
    if [ ! -f "$CACHE/captures/settings.json" ] && [ -f /repo/captures/settings.json ]; then
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
    # start.ps1 too, so a change to it needs no new base disk.
    guestfish --rw -a "$disk" -i <<EOF
mkdir-p /S60/repo/captures
mkdir-p /S60/openconsole
tar-in /run/repo.tar /S60/repo
tar-in /run/captures.tar /S60/repo/captures
tar-in /run/openconsole.tar /S60/openconsole
upload $GUEST/start.ps1 /S60/start.ps1
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
refresh) refresh ;;
run) run ;;
*) echo "usage: vm-run prepare | refresh | run" >&2; exit 2 ;;
esac
