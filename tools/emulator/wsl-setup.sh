#!/bin/bash
# Run as root in a fresh WSL distribution by windows-bootstrap.ps1 -LocalEmulator:
# what the emulator needs there. Safe to run again.
#   wsl-setup.sh USER
set -euo pipefail
user=${1:?the Linux user to set up}

id "$user" >/dev/null 2>&1 || useradd -m -s /bin/bash "$user"
# systemd, so Docker starts by itself; and the user, not root, by default.
printf '[boot]\nsystemd=true\n[user]\ndefault=%s\n' "$user" > /etc/wsl.conf

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io docker-buildx rsync python3-venv >/dev/null
usermod -aG docker "$user"
systemctl enable docker >/dev/null 2>&1 || true

# The NVS generator emu.sh uses to rewrite the dump's Wi-Fi: a Windows
# checkout's .venv is Windows' own, so it lives here instead.
venv=$(getent passwd "$user" | cut -d: -f6)/.local/share/s60-emu/venv
runuser -u "$user" -- python3 -m venv "$venv"
runuser -u "$user" -- "$venv/bin/pip" install -q esp-idf-nvs-partition-gen
echo "Set up for $user."
