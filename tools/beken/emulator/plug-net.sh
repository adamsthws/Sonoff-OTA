#!/bin/bash
# Runs as root in the S61 emulator's container (bk-emu.sh plug): puts the
# emulated plug on the S61's Docker network as a plug on a home Wi-Fi, with
# this container as that Wi-Fi's router, then runs the plug as your user
# until the container stops.
#
# The plug's radio is tap0 (the fork's --net-tap), bridged with the
# container's eth0, so the plug's frames are on the network as they are. The
# router is .1 (the network leaves it free: its gateway is .254, and Docker
# hands out .128 and up): DHCP, the plug's addresses .50-.59, as a home
# router's pool; DNS, every name this router; and eWeLink, the local stub
# (tools/beken/cloud_stub.py), which every connection the plug makes off the
# network is sent to. Nothing reaches the internet. The tool, on a computer
# on the same network (bk-emu.sh computer), finds the plug as on a home
# Wi-Fi, and the stub's account API at the router.
set -euo pipefail

: "${HOST_UID:?}" "${HOST_GID:?}"
STATE=${PLUG_STATE:-/state}
RUN=${PLUG_NET_RUN:-/run/autoflash-s61}
mkdir -p "$RUN"

read -r address prefix < <(ip -4 -o address show dev eth0 | awk '{split($4, a, "/"); print a[1], a[2]; exit}')
[ -n "$address" ] && [ "$prefix" = 24 ] || { echo "plug-net: eth0 is not on a /24" >&2; exit 1; }
net=${address%.*}
router=$net.1

ip tuntap add dev tap0 mode tap user "$HOST_UID" group "$HOST_GID"
ip link add br0 type bridge mcast_snooping 0
ip address flush dev eth0
ip link set eth0 master br0
ip link set tap0 master br0
ip link set tap0 up
ip link set br0 up
ip address add "$router/24" dev br0
# The plug's connections off the network: to the stub, whatever address it dialled.
iptables -t nat -A PREROUTING -i br0 -s "$net.48/28" ! -d "$net.0/24" -j DNAT --to-destination "$router"

# The router's DHCP and DNS, as a script that can start them again with the
# pool from another first address - the plug's own forgotten, as a router may
# forget it (the fault suite's readdress): $RUN/router [FIRST], .50 by default.
cat > "$RUN/router" <<ROUTER
#!/bin/sh
[ -f "$RUN/dnsmasq.pid" ] && kill "\$(cat "$RUN/dnsmasq.pid")" 2>/dev/null && sleep 1
[ -n "\${1:-}" ] && : > "$RUN/leases"
exec dnsmasq --user=root --interface=br0 --bind-interfaces --except-interface=lo --no-resolv \\
    --address="/#/$router" --dhcp-authoritative \\
    --dhcp-range="$net.\${1:-50},$net.59,255.255.255.0,1h" \\
    --dhcp-option=option:router,"$router" --dhcp-option=option:dns-server,"$router" \\
    --dhcp-leasefile="$RUN/leases" --pid-file="$RUN/dnsmasq.pid" \\
    --log-dhcp --log-facility="$RUN/dnsmasq.log"
ROUTER
chmod +x "$RUN/router"
"$RUN/router"

as_you() { setpriv --reuid="$HOST_UID" --regid="$HOST_GID" --clear-groups env HOME=/tmp "$@"; }
as_you python /work/tools/beken/plug.py --state "$STATE" --prepare
# The stub binds the router's ports 80 and 443, so it runs as root; what it
# makes in the state directory is yours.
python3 -I -B /work/tools/beken/cloud_stub.py --state "$STATE" --bind "$router" --flash "$STATE/flash.bin" \
    >> "$STATE/stub.log" 2>&1 &
for _ in $(seq 50); do [ -f "$STATE/ca.crt" ] && break; sleep 0.2; done
touch "$STATE/serial.log"
chown "$HOST_UID:$HOST_GID" "$STATE"/ca.crt "$STATE"/leaf.* "$STATE/stub.log" "$STATE/serial.log"
echo "The plug's router, and its eWeLink, is $router." >&2

exec setpriv --reuid="$HOST_UID" --regid="$HOST_GID" --clear-groups \
    env HOME=/tmp python /work/tools/beken/plug.py --state "$STATE" >> "$STATE/serial.log" 2>&1
