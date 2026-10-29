#!/usr/bin/env bash
# Capture a phone's eWeLink AirKiss/smartconfig broadcast to a pcap, for replay
# into the emulator (see s61_airkiss_replay.py). Needs root. Takes the Wi-Fi
# adapter into monitor mode, so the laptop drops off Wi-Fi while capturing.
# Intel AX200 (iwlwifi) does monitor CAPTURE fine.
#
# IMPORTANT: NetworkManager/wpa_supplicant will fight monitor mode (they put the
# card back to managed and the capture records ordinary Ethernet traffic, not
# 802.11). This script tells NetworkManager to release the interface first and
# restores it afterwards.
#
# Usage:  sudo ./s61_airkiss_capture.sh <channel> [iface] [out.pcap]
set -euo pipefail
CH="${1:?need channel, e.g. 6}"; IFACE="${2:-wlp1s0}"; OUT="${3:-airkiss-$(date +%Y%m%d-%H%M%S).pcap}"
echo "[cap] iface=$IFACE channel=$CH out=$OUT"

restore(){
  echo "[cap] restoring managed mode + NetworkManager"
  ip link set "$IFACE" down 2>/dev/null || true
  iw dev "$IFACE" set type managed 2>/dev/null || true
  ip link set "$IFACE" up 2>/dev/null || true
  nmcli dev set "$IFACE" managed yes 2>/dev/null || true
  nmcli dev connect "$IFACE" 2>/dev/null || true
}
trap restore EXIT

# 1) stop NetworkManager (and wpa_supplicant) from reclaiming the interface
nmcli dev set "$IFACE" managed no 2>/dev/null || true
command -v airmon-ng >/dev/null && airmon-ng check kill >/dev/null 2>&1 || true
pkill -f "wpa_supplicant.*$IFACE" 2>/dev/null || true
sleep 1

# 2) monitor mode on the target channel
ip link set "$IFACE" down
iw dev "$IFACE" set type monitor
ip link set "$IFACE" up
iw dev "$IFACE" set channel "$CH"

# 3) verify monitor mode actually took, and the channel is set
TYPE=$(iw dev "$IFACE" info 2>/dev/null | awk '/type/{print $2}')
CHNOW=$(iw dev "$IFACE" info 2>/dev/null | awk '/channel/{print $2}')
echo "[cap] iface type=$TYPE channel=$CHNOW"
if [ "$TYPE" != "monitor" ]; then
  echo "[cap] ERROR: interface is not in monitor mode (got '$TYPE'). Aborting."
  exit 1
fi

# 4) quick RX sanity: on a live channel we should see beacons within ~3s
echo "[cap] sanity: counting beacons for 3s..."
BE=$(timeout 3 tcpdump -i "$IFACE" -c 20 'type mgt subtype beacon' 2>/dev/null | wc -l || true)
echo "[cap] beacons seen in 3s: $BE  (0 means monitor RX is not working on this channel)"

echo "[cap] capturing on $IFACE ch $CH -> $OUT  (Ctrl-C to stop)"
tcpdump -i "$IFACE" -w "$OUT" -s 0 || true
echo "[cap] saved $OUT"
