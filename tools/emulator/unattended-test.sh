#!/bin/bash
# A whole conversion of the emulated plug with nobody at the keyboard, and the
# screen it drew shown at the end (tools/unattended_run.py, docs/unattended.md).
#
#   tools/emulator/unattended-test.sh            from where the last run left the plug
#   tools/emulator/unattended-test.sh --fresh    the plug back to stock first, and no earlier run
#   tools/emulator/unattended-test.sh --keep     leave the emulator running afterwards
#   tools/emulator/unattended-test.sh -- --new   anything after -- goes to the run itself
#
# Needs .env with the eWeLink account the dump's plug is in. It starts the
# emulator with --cloud --yes: the real plug, which the emulated one is a
# clone of, must be unplugged - it is not asked.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
STATE=$ROOT/captures/emulation/state
case "$ROOT" in /mnt/[a-z]/*) STATE=$HOME/.local/share/s60-emu/state ;; esac
NAME=s60-emu
PLUG=192.168.60.50

emu_args=(--cloud --yes --detach) keep=0 run_args=()
while [ $# -gt 0 ]; do
    case "$1" in
    --fresh|--pcap|--rebuild|--no-firewall) emu_args+=("$1") ;;
    --keep) keep=1 ;;
    --) shift; run_args=("$@"); break ;;
    *) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
    esac
    shift
done

[ -s "$ROOT/.env" ] || { echo "No .env with the eWeLink account - see docs/unattended.md" >&2; exit 1; }

"$ROOT/tools/emulator/emu.sh" "${emu_args[@]}" >/dev/null
[ "$keep" = 1 ] || trap 'docker stop "$NAME" >/dev/null 2>&1 || true' EXIT

as_you=(docker exec -u "$(id -u):$(id -g)" -w /work)
waited=0
until "${as_you[@]}" "$NAME" tmux has-session -t s60 >/dev/null 2>&1 &&
        docker exec "$NAME" ping -c1 -W1 "$PLUG" >/dev/null 2>&1; do
    [ $((waited += 2)) -le 120 ] || { echo "The emulated plug did not come up within 120s." >&2; exit 1; }
    sleep 2
done
echo "The emulated plug is up; converting it unattended..."

# On a terminal the run is shown live; otherwise its final screen is printed.
[ -t 1 ] && as_you+=(-t)
stamp=$(date +%Y%m%d-%H%M%S)
status=0
"${as_you[@]}" -e S60_EMULATOR_URL=http://localhost:8080 -e S60_EMULATOR_SUDO_PASSWORD=s60 \
    -e HOME=/tmp "$NAME" /opt/venv/bin/python tools/unattended_run.py --size 100x40 \
    --save "captures/unattended/$stamp" -- "${run_args[@]}" || status=$?

echo "On this computer: ${STATE#"$ROOT/"}/work/captures/unattended/$stamp/"
[ "$keep" = 1 ] && echo "The emulator is still running: http://localhost:8080, or docker attach $NAME"
exit "$status"
