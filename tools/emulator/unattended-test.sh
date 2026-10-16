#!/bin/bash
# A whole conversion of the emulated plug with nobody at the keyboard, and the
# screen it drew shown at the end (tools/unattended_run.py, docs/unattended.md).
#
#   tools/emulator/unattended-test.sh            from where the last run left the plug
#   tools/emulator/unattended-test.sh --fresh    the plug back to stock first, and no earlier run
#   tools/emulator/unattended-test.sh --keep     leave the emulator running afterwards
#   tools/emulator/unattended-test.sh --plug=MAC which backed-up plug (else the one run last)
#   tools/emulator/unattended-test.sh --instance=N  on emulator N, beside others (emu.sh --instance)
#   tools/emulator/unattended-test.sh --save=NAME   keep the plug as the run left it, and its
#                                                   captures (its backup, its runs), as NAME
#   tools/emulator/unattended-test.sh --from=NAME   start from a plug kept with --save: one
#                                                   converted earlier, on this computer
#   tools/emulator/unattended-test.sh -- --new   anything after -- goes to the run itself
#
# Needs .env with the eWeLink account the dump's plug is in. It starts the
# emulator with --cloud --yes: the real plug, which the emulated one is a
# clone of, must be unplugged - it is not asked.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
STATE=$ROOT/captures/emulation/state
case "$ROOT" in /mnt/[a-z]/*) STATE=$HOME/.local/share/s60-emu/state ;; esac
NAME=s60-emu PORT=8080
PLUG=192.168.60.50

emu_args=(--cloud --yes --detach) keep=0 run_args=() from="" save=""
while [ $# -gt 0 ]; do
    case "$1" in
    --fresh|--pcap|--rebuild|--no-firewall|--plug=*) emu_args+=("$1") ;;
    --instance=[1-9]|--instance=[12][0-9]|--instance=30)  # another emulator beside the first: see emu.sh
        n=${1#--instance=}; emu_args+=("$1")
        NAME=s60-emu-$n PORT=$((8080 + n)) STATE=$STATE-$n ;;
    --keep) keep=1 ;;
    --from=*) from=${1#--from=} ;;
    --save=*) save=${1#--save=} ;;
    --) shift; run_args=("$@"); break ;;
    *) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
    esac
    shift
done

[ -s "$ROOT/.env" ] || { echo "No .env with the eWeLink account - see docs/unattended.md" >&2; exit 1; }

# Plugs kept with --save: the flash, and the captures of the runs that made it.
SAVED=$ROOT/captures/emulation/saved
if [ -n "$from" ]; then
    [ -f "$SAVED/$from/flash.bin" ] || { echo "No plug kept as '$from' (--save=$from)" >&2; exit 1; }
    docker stop "$NAME" >/dev/null 2>&1 || true
    mkdir -p "$STATE/work"
    cp "$SAVED/$from/flash.bin" "$STATE/flash.bin"
    rm -rf "$STATE/work/captures" && cp -a "$SAVED/$from/captures" "$STATE/work/captures"
fi

# One left running (--keep) is carried on with as it is - the plug, its network
# and the computer's firewall - as a person running the tool again would find them.
if [ -n "$from" ] || [[ " ${emu_args[*]} " == *" --fresh "* ]] || \
        [ -z "$(docker ps -q -f "name=^$NAME\$")" ]; then
    "$ROOT/tools/emulator/emu.sh" "${emu_args[@]}" >/dev/null
fi
keep_plug() {
    [ -n "$save" ] || return 0
    docker exec "$NAME" /opt/s60emu/bin/plug off >/dev/null 2>&1 || true
    rm -rf "${SAVED:?}/$save" && mkdir -p "$SAVED/$save"
    cp "$STATE/flash.bin" "$SAVED/$save/flash.bin"
    cp -a "$STATE/work/captures" "$SAVED/$save/captures"
    rm -rf "$SAVED/$save/captures/unattended"
    echo "Kept as '$save' (--from=$save)."
}
[ "$keep" = 1 ] || trap 'keep_plug; docker stop "$NAME" >/dev/null 2>&1 || true' EXIT

as_you=(docker exec -u "$(id -u):$(id -g)" -w /work)
waited=0
until "${as_you[@]}" "$NAME" tmux has-session -t s60 >/dev/null 2>&1 &&
        docker exec "$NAME" ping -c1 -W1 "$PLUG" >/dev/null 2>&1; do
    [ $((waited += 2)) -le 120 ] || { echo "The emulated plug did not come up within 120s." >&2; exit 1; }
    sleep 2
done
echo "The emulated plug is up; converting it unattended..."

# The eWeLink account, for this run only: the emulator's copy of the
# repository has no .env, so a run you sit through asks for it. Kept in the
# container's /tmp, gone when it stops, and removed after the run anyway.
answers=/tmp/s60-unattended.env
# Not the Wi-Fi in it, if any: that is your real network's, and the emulated
# plug's is its own (given below).
grep -v '^ *\(export \)\?S60_WIFI_' "$ROOT/.env" | "${as_you[@]}" -i "$NAME" sh -c "umask 077 && cat > $answers"
[ "$keep" = 1 ] && trap 'docker exec "$NAME" rm -f "$answers" 2>/dev/null || true' EXIT

# On a terminal the run is shown live; otherwise its final screen is printed.
[ -t 1 ] && as_you+=(-t)
stamp=$(date +%Y%m%d-%H%M%S)
status=0
# The emulator's Wi-Fi, for a run from ESPHome, which cannot say what it is.
"${as_you[@]}" -e S60_EMULATOR_URL="http://localhost:$PORT" -e S60_EMULATOR_SUDO_PASSWORD=s60 \
    -e S60_WIFI_SSID=PICSimLabWifi \
    -e HOME=/tmp "$NAME" /opt/venv/bin/python tools/unattended_run.py --size 100x40 \
    --save "captures/unattended/$stamp" -- --env-file "$answers" "${run_args[@]}" || status=$?

echo "On this computer: ${STATE#"$ROOT/"}/work/captures/unattended/$stamp/"
[ "$keep" = 1 ] && echo "The emulator is still running: http://localhost:$PORT, or docker attach $NAME"
exit "$status"
