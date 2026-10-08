#!/bin/bash
# Start the virtual Sonoff S60 and a "computer" to convert it from, in Docker.
#
#   tools/emulator/emu.sh            plug offline: nothing reaches the internet
#   tools/emulator/emu.sh --cloud    plug and tool online (real plug must be unplugged)
#   tools/emulator/emu.sh --fresh    put the plug back to stock 1.2.0, and forget the run and the account
#   tools/emulator/emu.sh --pcap     also record the plug's Wi-Fi traffic (captures/emulation/state/logs)
#   tools/emulator/emu.sh --rebuild  rebuild the image first
#   tools/emulator/emu.sh --detach   start in the background; join with: docker attach s60-emu
#   tools/emulator/emu.sh --no-firewall  without the "computer's" firewall (ufw), on by default
#
# Needs the private stock dump copies in captures/emulation/ (flash.bin,
# efuse.bin; see docs/emulator.md). Everything the emulator writes stays in
# captures/emulation/state/, which is private and Git-ignored.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
EMU=$ROOT/captures/emulation
STATE=$EMU/state
PY=$ROOT/.venv/bin/python
IMAGE=s60-emu

cloud=0 fresh=0 pcap=0 rebuild=0 firewall=1 tty=-it
for arg in "$@"; do
    case "$arg" in
    --cloud) cloud=1 ;;
    --fresh) fresh=1 ;;
    --pcap) pcap=1 ;;
    --rebuild) rebuild=1 ;;
    --detach) tty=-dt ;;
    --no-firewall) firewall=0 ;;
    *) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
    esac
done

for f in "$EMU/flash.bin" "$EMU/efuse.bin"; do
    [ -f "$f" ] || { echo "Missing $f - see docs/emulator.md" >&2; exit 1; }
done
"$PY" -c 'import esp_idf_nvs_partition_gen' 2>/dev/null || {
    echo "The emulator needs ESP-IDF's NVS generator in .venv:" >&2
    echo "    .venv/bin/python -m pip install esp-idf-nvs-partition-gen" >&2
    exit 1
}
if docker ps -q --filter "name=^s60-emu$" | grep -q .; then
    echo "The emulator is already running. Attach to it with: docker attach s60-emu" >&2
    exit 1
fi

# -- the image -----------------------------------------------------------------
if [ "$rebuild" = 1 ] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    context=$(mktemp -d)
    trap 'rm -rf "$context"' EXIT
    cp -r "$ROOT/tools/emulator/emu.Dockerfile" "$ROOT/tools/emulator/fork" \
          "$ROOT/tools/emulator/container" "$ROOT/requirements.txt" "$context/"
    echo "Building the emulator image (the first time takes a few minutes)..."
    docker build -q -t "$IMAGE" -f "$context/emu.Dockerfile" "$context" >/dev/null
fi

# -- the plug's flash and eFuses ---------------------------------------------
mkdir -p -m 0700 "$STATE" "$STATE/logs"
if [ "$fresh" = 1 ]; then
    rm -rf "$STATE/flash.bin" "$STATE/work/captures"
    echo "The plug is back to stock 1.2.0; the previous run and eWeLink account are forgotten."
fi
if [ ! -f "$STATE/flash.bin" ]; then
    # Home Wi-Fi rewritten to the emulator's open network, Bluetooth start-up
    # skipped (the emulator has no Bluetooth), re-sealed for the bootloader.
    "$PY" -I "$ROOT/tools/emulator/rewrite_nvs.py" "$EMU/flash.bin" "$STATE/wifi-flash.bin" \
        2>&1 | grep -v 'absolute path given' >/dev/null
    "$PY" -I "$ROOT/tools/emulator/patch_stock.py" "$STATE/wifi-flash.bin" "$STATE/flash.bin" >/dev/null
    rm -f "$STATE/wifi-flash.bin"
fi
cp "$EMU/efuse.bin" "$STATE/efuse.bin" && truncate -s 1024 "$STATE/efuse.bin"
chmod 0600 "$STATE"/*.bin
mac=$("$PY" -I -c 'import sys; d=open(sys.argv[1],"rb").read(); print(":".join(f"{b:02x}" for b in (d[0x18:0x1C]+d[0x1C:0x1E])[::-1]))' "$EMU/efuse.bin")

# -- the "computer": a copy of this repository ---------------------------------
# A copy, so a run's lock renames and captures never touch the real checkout.
# The code is refreshed on every start, and the run's captures are kept.
mkdir -p "$STATE/work/captures"
rsync -a --delete --exclude /captures/ --exclude /.venv/ --exclude /.git/ \
      --exclude __pycache__/ "$ROOT/" "$STATE/work/"
# Not the firmware bundle in captures/safeboot-recovery-migration: a real
# bundle is set up for the real home Wi-Fi. The run builds its own, as a new
# user's would (it downloads the official files, so it needs --cloud).
# The eWeLink account email last used here (captures/settings.json) is offered
# to save typing it - but not with --fresh, which starts as a new user would.
if [ "$fresh" = 0 ] && [ ! -f "$STATE/work/captures/settings.json" ] && [ -f "$ROOT/captures/settings.json" ]; then
    cp "$ROOT/captures/settings.json" "$STATE/work/captures/"
fi

# -- the cloud ----------------------------------------------------------------
if [ "$cloud" = 1 ]; then
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

exec docker run --rm $tty --name s60-emu --hostname s60-emu-computer \
    --cap-add NET_ADMIN --device /dev/net/tun \
    --sysctl net.ipv4.ip_forward="$cloud" \
    -p 127.0.0.1:8080:8080 \
    -v "$STATE:/state" -v "$EMU/flash.bin:/secrets/stock-flash.bin:ro" \
    -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" -e PLUG_MAC="$mac" \
    -e CLOUD="$cloud" -e PCAP="$pcap" -e FIREWALL="$firewall" \
    -e S60_EMULATOR_URL=http://localhost:8080 \
    "$IMAGE"
