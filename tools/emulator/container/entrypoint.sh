#!/bin/bash
# Runs as root when the container starts (see tools/emulator/emu.sh), then
# hands over to you, as your own user id, in a tmux session.
set -euo pipefail

: "${HOST_UID:?}" "${HOST_GID:?}" "${PLUG_MAC:?}" "${CLOUD:=0}" "${PCAP:=0}" "${FIREWALL:=1}"
RUN=/run/s60emu
mkdir -p "$RUN" /state/logs

# -- you, with sudo that asks for your password, as on a real computer ----------
# The password is s60: the welcome message says so, and the conversion tool
# says so where it warns that sudo may ask. Only the emulator's own
# Wi-Fi switching runs without it: on a real computer that is the desktop's
# Wi-Fi menu, which asks for nothing.
user=$(getent passwd "$HOST_UID" | cut -d: -f1 || true)
if [ -z "$user" ]; then
    getent group "$HOST_GID" >/dev/null || groupadd -g "$HOST_GID" you
    useradd -m -u "$HOST_UID" -g "$HOST_GID" -s /bin/bash you
    user=you
fi
SUDO_PASSWORD=s60
echo "$user:$SUDO_PASSWORD" | chpasswd
printf '%s\n' "Defaults lecture=never" \
    "$user ALL=(root) NOPASSWD: /opt/s60emu/bin/wifi-root" \
    "$user ALL=(ALL) ALL" > /etc/sudoers.d/s60emu
chmod 0440 /etc/sudoers.d/s60emu
home=$(getent passwd "$user" | cut -d: -f6)

# -- a firewall, on, as on a typical Linux desktop ---------------------------
# ufw blocking incoming connections, so the conversion has to add its
# temporary rule for the plug, through sudo, and remove it again. Allowed
# here is only what the emulator itself needs: the plug's DHCP and DNS from
# this "computer" (its router), and your browser's port 8080. Turned on
# before the rules below, so it cannot disturb them. emu.sh --no-firewall
# leaves it off.
if [ "$FIREWALL" = 1 ]; then
    # A container may not change kernel settings, and has no IPv6 here.
    : > /etc/ufw/sysctl-none.conf
    sed -i -e 's|^IPT_SYSCTL=.*|IPT_SYSCTL=/etc/ufw/sysctl-none.conf|' \
           -e 's|^IPV6=.*|IPV6=no|' /etc/default/ufw
    ufw --force reset >/dev/null
    ufw default deny incoming >/dev/null
    ufw default allow outgoing >/dev/null
    ufw allow in on tap0 to any port 67 proto udp comment 'emulator: DHCP for the plug' >/dev/null
    ufw allow in on tap0 to any port 53 comment 'emulator: DNS for the plug' >/dev/null
    ufw allow in on eth0 to any port 8080 proto tcp comment 'emulator: your browser' >/dev/null
    ufw logging off >/dev/null
    ufw --force enable >/dev/null
else
    # The welcome message says the firewall is on: not this time.
    on="This computer's firewall (ufw) is on, as on a typical Linux desktop."
    off=$(printf '%-*s' "${#on}" "This computer's firewall is off (emu.sh --no-firewall).")
    sed -i "s|$on|$off|" /opt/s60emu/motd
fi

# -- internet: only with --cloud ----------------------------------------------
# Without it nothing in here reaches the internet - neither the plug nor the
# conversion tool - so the plug's shared eWeLink identity cannot be used by
# mistake. Replies to your browser (published port 8080) still get out.
iptables -P FORWARD DROP
if [ "$CLOUD" = 1 ]; then
    iptables -A FORWARD -i tap0 -o eth0 -j ACCEPT
    iptables -A FORWARD -i eth0 -o tap0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    iptables -t nat -A POSTROUTING -s 192.168.60.0/24 -o eth0 -j MASQUERADE
else
    iptables -A OUTPUT -o eth0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    iptables -A OUTPUT -o eth0 -j DROP
fi

# -- the plug's radio: a tap device QEMU's emulated Wi-Fi is wired to ---------
ip tuntap add dev tap0 mode tap user "$HOST_UID" group "$HOST_GID"
ip link set tap0 up
cat > "$RUN/env" <<EOF
PLUG_MAC=$PLUG_MAC
CLOUD=$CLOUD
PCAP=$PCAP
EOF
echo on > "$RUN/power"
if [ "$CLOUD" = 1 ]; then echo "internet: ON (cloud)" > "$RUN/cloud-label"; else echo "internet: off" > "$RUN/cloud-label"; fi
: > "$RUN/softap"
chown -R "$HOST_UID:$HOST_GID" "$RUN"
/opt/s60emu/bin/wifi-root home >/dev/null

ln -sfn /state/work /work
cp /opt/s60emu/tmux.conf "$home/.tmux.conf"
grep -q s60emu-motd "$home/.bashrc" 2>/dev/null || cat >> "$home/.bashrc" <<'EOF'
# s60emu-motd
[ -n "${TMUX:-}" ] && [ "$(tmux display -p '#W')" = conversion ] && cat /opt/s60emu/motd
EOF
chown "$HOST_UID:$HOST_GID" "$home/.tmux.conf" "$home/.bashrc"

as_you() { runuser -u "$user" -- env HOME="$home" PATH="$PATH" S60_EMULATOR_URL="${S60_EMULATOR_URL:-}" \
    S60_EMULATOR_SUDO_PASSWORD="$SUDO_PASSWORD" "$@"; }
as_you /opt/s60emu/bin/plug-supervisor >/dev/null 2>&1 &
as_you /opt/s60emu/bin/browser-forward >"$RUN/browser-forward.log" 2>&1 &

cd /work
as_you tmux new-session -d -s s60 -n conversion -c /work
as_you tmux new-window -d -t s60 -n plug "plug log"
exec runuser -u "$user" -- env HOME="$home" PATH="$PATH" S60_EMULATOR_URL="${S60_EMULATOR_URL:-}" \
    S60_EMULATOR_SUDO_PASSWORD="$SUDO_PASSWORD" tmux attach -t s60
