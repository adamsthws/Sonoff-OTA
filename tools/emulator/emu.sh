#!/bin/bash
# Start the virtual Sonoff S60 and a "computer" to convert it from, in Docker.
#
#   tools/emulator/emu.sh            plug offline: nothing reaches the internet
#   tools/emulator/emu.sh --cloud    plug and tool online (real plug must be unplugged)
#   tools/emulator/emu.sh --yes      with --cloud: the real plug is known to be unplugged; don't ask
#   tools/emulator/emu.sh --plug MAC which backed-up plug to run; asked when there are several
#   tools/emulator/emu.sh --fresh    put the plug back to its Sonoff backup, and forget the run and the account
#   tools/emulator/emu.sh --pcap     also record the plug's Wi-Fi traffic (captures/emulation/state/logs)
#   tools/emulator/emu.sh --rebuild  rebuild the image first
#   tools/emulator/emu.sh --detach   start in the background; join with: docker attach s60-emu
#   tools/emulator/emu.sh --no-firewall  without the "computer's" firewall (ufw), on by default
#   tools/emulator/emu.sh --windows  Windows is the computer, wired in by windows_link.py
#                                    (from Windows, start it all with tools/emulator/emu.ps1)
#   tools/emulator/emu.sh --instance N  another emulator beside the first (N = 1-30): its own
#                                    container (s60-emu-N), state (state-N) and browser
#                                    port (8080+N), so tests can run side by side
#
# Runs a plug from its Sonoff backup, which every conversion keeps in
# captures/plugs/<MAC>/sonoff-backup/ (or a dump in captures/emulation/; see
# docs/emulator.md). The backup is only read: the plug runs a copy. Everything
# the emulator writes stays in captures/emulation/state/, which is private and
# Git-ignored (from a Windows checkout under WSL: ~/.local/share/s60-emu/state,
# on WSL's own disk); each plug's flash is kept there between runs.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
EMU=$ROOT/captures/emulation
STATE=$EMU/state
# A Windows checkout under WSL (/mnt/c, reached through 9p) is far too slow for
# the plug's flash: a write can take 100 ms, and the bridge then trips its
# interrupt watchdog while flashing Tasmota. Its state lives on WSL's own disk.
case "$ROOT" in /mnt/[a-z]/*) STATE=$HOME/.local/share/s60-emu/state ;; esac
PY=$ROOT/.venv/bin/python
# A Windows checkout's .venv is Windows' own: under WSL the NVS generator lives here.
[ -x "$PY" ] || PY=$HOME/.local/share/s60-emu/venv/bin/python
IMAGE=s60-emu
NAME=s60-emu PORT=8080 LINK_PORT=7860

cloud=0 fresh=0 pcap=0 rebuild=0 firewall=1 windows=0 yes=0 tty=-it plug="" instance=""
while [ $# -gt 0 ]; do
    arg=$1
    case "$arg" in
    --plug) [ $# -ge 2 ] || { echo "--plug needs a MAC address" >&2; exit 2; }; plug=$2; shift ;;
    --plug=*) plug=${arg#--plug=} ;;
    --instance) [ $# -ge 2 ] || { echo "--instance needs a number, 1-30" >&2; exit 2; }; instance=$2; shift ;;
    --instance=*) instance=${arg#--instance=} ;;
    --cloud) cloud=1 ;;
    --yes) yes=1 ;;
    --fresh) fresh=1 ;;
    --pcap) pcap=1 ;;
    --rebuild) rebuild=1 ;;
    --detach) tty=-dt ;;
    --no-firewall) firewall=0 ;;
    --windows) windows=1 ;;
    *) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
    esac
    shift
done
if [ -n "$instance" ]; then
    case "$instance" in [1-9]|[12][0-9]|30) ;; *) echo "--instance needs a number, 1-30" >&2; exit 2 ;; esac
    STATE=$STATE-$instance NAME=s60-emu-$instance
    PORT=$((8080 + instance)) LINK_PORT=$((7860 + instance))
fi
"$PY" -c 'import esp_idf_nvs_partition_gen' 2>/dev/null || {
    venv=$(dirname "$(dirname "$PY")")
    echo "The emulator needs ESP-IDF's NVS generator in $venv:" >&2
    echo "    python3 -m venv $venv && $PY -m pip install esp-idf-nvs-partition-gen" >&2
    exit 1
}
if docker ps -q --filter "name=^$NAME$" | grep -q .; then
    echo "The emulator is already running. Attach to it with: docker attach $NAME" >&2
    exit 1
fi

# -- which plug ------------------------------------------------------------------
mkdir -p -m 0700 "$STATE" "$STATE/logs" "$STATE/plugs"
efuse_mac() {
    "$PY" -I -c 'import sys; d=open(sys.argv[1],"rb").read(); print(":".join(f"{b:02x}" for b in (d[0x18:0x1C]+d[0x1C:0x1E])[::-1]))' "$1"
}
# The plug whose flash is in $STATE/flash.bin. From before plugs were chosen,
# that flash came from the dump in captures/emulation/.
if [ -f "$STATE/plug" ]; then
    was=$(sed -n 2p "$STATE/plug")
elif [ -f "$EMU/efuse.bin" ]; then
    was=$(efuse_mac "$EMU/efuse.bin")
else
    was=""
fi
"$PY" -I "$ROOT/tools/emulator/choose_plug.py" --out "$STATE/plug" ${plug:+--plug "$plug"} || exit $?
SRC=$(sed -n 1p "$STATE/plug")
mac=$(sed -n 2p "$STATE/plug")
# Another plug than last time: its own flash, kept from its last run, comes
# back, and the one that was running is put by for its next.
if [ "$mac" != "$was" ]; then
    [ -n "$was" ] && [ -f "$STATE/flash.bin" ] && mv "$STATE/flash.bin" "$STATE/plugs/${was//:/-}.bin"
    [ -f "$STATE/plugs/${mac//:/-}.bin" ] && mv "$STATE/plugs/${mac//:/-}.bin" "$STATE/flash.bin"
fi

# -- the image -----------------------------------------------------------------
# Labelled with a hash of what it is built from, and rebuilt when that changes:
# an image from an older checkout would otherwise run unseen.
built_from=$(cd "$ROOT/tools/emulator" && find emu.Dockerfile fork container -type f -print0 \
    | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16)
if [ "$rebuild" = 1 ] || [ "$(docker image inspect -f '{{index .Config.Labels "s60.built-from"}}' \
        "$IMAGE" 2>/dev/null)" != "$built_from" ]; then
    context=$(mktemp -d)
    trap 'rm -rf "$context"' EXIT
    cp -r "$ROOT/tools/emulator/emu.Dockerfile" "$ROOT/tools/emulator/fork" \
          "$ROOT/tools/emulator/container" "$context/"
    echo "Building the emulator image (the first time takes a few minutes)..."
    docker build -q -t "$IMAGE" --label "s60.built-from=$built_from" \
        -f "$context/emu.Dockerfile" "$context" >/dev/null
fi

# -- the plug's flash and eFuses ---------------------------------------------
if [ "$fresh" = 1 ]; then
    rm -rf "$STATE/flash.bin" "$STATE/work/captures"
    echo "The plug is back to its Sonoff backup; the previous run and eWeLink account are forgotten."
fi
if [ ! -f "$STATE/flash.bin" ]; then
    # Home Wi-Fi rewritten to the emulator's open network, Bluetooth start-up
    # skipped (the emulator has no Bluetooth), re-sealed for the bootloader.
    "$PY" -I "$ROOT/tools/emulator/rewrite_nvs.py" "$SRC/flash.bin" "$STATE/wifi-flash.bin" \
        2>&1 | grep -v 'absolute path given' >/dev/null
    "$PY" -I "$ROOT/tools/emulator/patch_stock.py" "$STATE/wifi-flash.bin" "$STATE/flash.bin" >/dev/null
    rm -f "$STATE/wifi-flash.bin"
fi
# Copied by content: the backup's files are read-only, and a copy would be too.
rm -f "$STATE/efuse.bin" && cat "$SRC/efuse.bin" > "$STATE/efuse.bin" && truncate -s 1024 "$STATE/efuse.bin"
chmod 0600 "$STATE"/*.bin

# -- the "computer": a copy of this repository ---------------------------------
# A copy, so a run's lock renames and captures never touch the real checkout.
# The code is refreshed on every start, and the run's captures are kept.
# Not .env: a run you sit through asks for the eWeLink account, as a real
# one does. unattended-test.sh hands it to its run alone.
mkdir -p "$STATE/work/captures"
rsync -a --delete --exclude /captures/ --exclude /.venv/ --exclude /.python/ --exclude /.git/ \
      --exclude /.env --exclude __pycache__/ "$ROOT/" "$STATE/work/"
rm -f "$STATE/work/.env"  # one copied before it was left out: --delete spares excluded files
# Not the firmware bundle in captures/safeboot-recovery-migration: a real
# bundle is set up for the real home Wi-Fi. The run builds its own, as a new
# user's would (it downloads the official files, so it needs --cloud).
# The eWeLink account email last used here (captures/settings.json) is offered
# to save typing it - but not with --fresh, which starts as a new user would.
if [ "$fresh" = 0 ] && [ ! -f "$STATE/work/captures/settings.json" ] && [ -f "$ROOT/captures/settings.json" ]; then
    cp "$ROOT/captures/settings.json" "$STATE/work/captures/"
fi

# -- the cloud ----------------------------------------------------------------
if [ "$cloud" = 1 ] && [ "$yes" = 1 ]; then
    echo "  --yes: the real plug is taken to be unplugged, as it must be while this runs."
elif [ "$cloud" = 1 ]; then
    cat <<EOF

  The emulated plug is a clone of your real plug: same eWeLink device id,
  keys and MAC. Online at the same time, the two fight over
  one device in your eWeLink account, and an upgrade the conversion sends
  could reach the real plug instead.

  Unplug the real plug now, and keep it unplugged while this runs.

EOF
    read -r -p "  Type 'unplugged' once it is: " answer
    [ "$answer" = unplugged ] || { echo "Not confirmed; start without --cloud for an offline plug."; exit 1; }
fi

# Your browser, and with --windows the wire windows_link.py connects to.
publish=(-p "127.0.0.1:$PORT:8080")
[ "$windows" = 1 ] && publish+=(-p "127.0.0.1:$LINK_PORT:7860")
# Developing the QEMU fork: S60_EMU_QEMU=path/to/qemu-system-riscv32 runs that
# build in place of the image's (tools/emulator/qemu-build.Dockerfile builds one).
dev=()
[ -n "${S60_EMU_QEMU:-}" ] && dev=(-v "$(realpath "$S60_EMU_QEMU"):/opt/qemu/bin/qemu-system-riscv32:ro")
# Awake while you are in it, so a conversion is not cut off by sleep: a lock
# held only while this runs (systemd-inhibit), changing no setting.
awake=()
if [ "$tty" = -it ] && systemd-inhibit --what=sleep:idle true 2>/dev/null; then
    awake=(systemd-inhibit --what=sleep:idle --who="S60 emulator" --why="the emulated plug's conversion" --mode=block)
fi
# Capped: the plug, its router and the tool used about 80 MB between them.
exec "${awake[@]}" docker run --rm $tty --name "$NAME" --hostname s60-emu-computer \
    --memory "${S60_EMU_MEMORY:-256m}" --cpus "${S60_EMU_CPUS:-2}" \
    --cap-add NET_ADMIN --device /dev/net/tun \
    --sysctl net.ipv4.ip_forward="$(( cloud || windows ))" \
    "${publish[@]}" "${dev[@]}" \
    -v "$STATE:/state" -v "$SRC/flash.bin:/secrets/stock-flash.bin:ro" \
    -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" -e PLUG_MAC="$mac" \
    -e CLOUD="$cloud" -e PCAP="$pcap" -e FIREWALL="$firewall" -e WINDOWS="$windows" \
    -e S60_EMULATOR_URL="http://localhost:$PORT" \
    "$IMAGE"
