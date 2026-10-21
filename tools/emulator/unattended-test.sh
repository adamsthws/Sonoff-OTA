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
#   tools/emulator/unattended-test.sh --stub-cloud  convert against the local cloud stub, with
#                                                   no eWeLink account and no .env (emu.sh --stub-cloud)
#   tools/emulator/unattended-test.sh -- --new   anything after -- goes to the run itself
#
# Needs .env with the eWeLink account the dump's plug is in. It starts the
# emulator with --cloud --yes: the real plug, which the emulated one is a
# clone of, must be unplugged - it is not asked. With --stub-cloud none of that
# applies: a local stub is the cloud, the plug stays offline, and the account
# is a placeholder the stub ignores.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
STATE=$ROOT/captures/emulation/state
case "$ROOT" in /mnt/[a-z]/*) STATE=$HOME/.local/share/autoflash-emu/state ;; esac
NAME=autoflash-emu PORT=8080
PLUG=192.168.60.50

emu_args=(--detach) keep=0 stub=0 run_args=() from="" save="" plug_mac=""
while [ $# -gt 0 ]; do
    case "$1" in
    --fresh|--pcap|--rebuild|--no-firewall) emu_args+=("$1") ;;
    --plug=*) emu_args+=("$1"); plug_mac=$(echo "${1#--plug=}" | tr 'A-F-' 'a-f:') ;;
    --stub-cloud) stub=1 ;;
    --instance=[1-9]|--instance=[12][0-9]|--instance=30)  # another emulator beside the first: see emu.sh
        n=${1#--instance=}; emu_args+=("$1")
        NAME=autoflash-emu-$n PORT=$((8080 + n)) STATE=$STATE-$n ;;
    --keep) keep=1 ;;
    --from=*) from=${1#--from=} ;;
    --save=*) save=${1#--save=} ;;
    --) shift; run_args=("$@"); break ;;
    *) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
    esac
    shift
done

# The stub is its own cloud and needs no account; otherwise the real cloud is
# used, which needs the account in .env and the real plug unplugged.
if [ "$stub" = 1 ]; then
    emu_args=(--stub-cloud "${emu_args[@]}")
else
    emu_args=(--cloud --yes "${emu_args[@]}")
    [ -s "$ROOT/.env" ] || { echo "No .env with the eWeLink account - see docs/unattended.md" >&2; exit 1; }
fi

# Plugs kept with --save: the flash, the mode it was built for (a stub run's
# flash differs from a cloud run's - see emu.sh), and the captures of the runs
# that made it. A plug kept before modes were recorded is a cloud one.
SAVED=$ROOT/captures/emulation/saved
mode=plain; [ "$stub" = 1 ] && mode=stub
if [ -n "$from" ]; then
    [ -f "$SAVED/$from/flash.bin" ] || { echo "No plug kept as '$from' (--save=$from)" >&2; exit 1; }
    kept_for=$(cat "$SAVED/$from/flash-mode" 2>/dev/null || echo plain)
    if [ "$kept_for" != "$mode" ]; then
        echo "'$from' was kept from a run $([ "$kept_for" = stub ] && echo "with" || echo "without") --stub-cloud;" \
             "carry it on the same way, or keep one with --save from a run like this one." >&2
        exit 1
    fi
    docker stop "$NAME" >/dev/null 2>&1 || true
    mkdir -p "$STATE/work"
    cp "$SAVED/$from/flash.bin" "$STATE/flash.bin"
    echo "$kept_for" > "$STATE/.flash-mode"
    rm -rf "$STATE/work/captures" && cp -a "$SAVED/$from/captures" "$STATE/work/captures"
    # The kept flash is the asked-for plug's now: emu.sh, seeing another plug
    # than ran here last, would put it by as the last one's and bring back
    # that plug's own - the run would not start from the kept plug at all.
    if [ -n "$plug_mac" ]; then
        printf '%s\n%s\n' "$SAVED/$from" "$plug_mac" > "$STATE/plug"
        rm -f "$STATE/plugs/${plug_mac//:/-}.bin" "$STATE/plugs/${plug_mac//:/-}.mode"
    fi
fi

# One left running (--keep) is carried on with as it is - the plug, its network
# and the computer's firewall - as a person running the tool again would find them.
# Not one running the other way, against the real cloud or the stub.
if [ -z "$from" ] && [[ " ${emu_args[*]} " != *" --fresh "* ]] && \
        [ -n "$(docker ps -q -f "name=^$NAME\$")" ]; then
    running=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$NAME" | sed -n 's/^STUB_CLOUD=//p')
    if [ "${running:-0}" != "$stub" ]; then
        echo "$NAME is running $([ "${running:-0}" = 1 ] && echo "against the local stub" || echo "against the real cloud")," \
             "not as asked: stop it (docker stop $NAME) or use another --instance." >&2
        exit 1
    fi
fi
if [ -n "$from" ] || [[ " ${emu_args[*]} " == *" --fresh "* ]] || \
        [ -z "$(docker ps -q -f "name=^$NAME\$")" ]; then
    "$ROOT/tools/emulator/emu.sh" "${emu_args[@]}" >/dev/null
fi
keep_plug() {
    [ -n "$save" ] || return 0
    docker exec "$NAME" /opt/autoflash-emu/bin/plug off >/dev/null 2>&1 || true
    rm -rf "${SAVED:?}/$save" && mkdir -p "$SAVED/$save"
    cp "$STATE/flash.bin" "$SAVED/$save/flash.bin"
    cat "$STATE/.flash-mode" 2>/dev/null > "$SAVED/$save/flash-mode" || echo plain > "$SAVED/$save/flash-mode"
    cp -a "$STATE/work/captures" "$SAVED/$save/captures"
    rm -rf "$SAVED/$save/captures/unattended"
    echo "Kept as '$save' (--from=$save)."
}
[ "$keep" = 1 ] || trap 'keep_plug; docker stop "$NAME" >/dev/null 2>&1 || true' EXIT

as_you=(docker exec -u "$(id -u):$(id -g)" -w /work)
waited=0
until "${as_you[@]}" "$NAME" tmux has-session -t autoflash >/dev/null 2>&1; do
    [ $((waited += 2)) -le 120 ] || { echo "The emulator did not start within 120s." >&2; exit 1; }
    sleep 2
done
# A plug off the network - on its own setup network after a power cut, say -
# is one the run must find for itself: waited for, but not required.
waited=0
until docker exec "$NAME" ping -c1 -W1 "$PLUG" >/dev/null 2>&1; do
    if [ $((waited += 2)) -gt 120 ]; then
        echo "The emulated plug is not on the network after 120s; running anyway."
        break
    fi
    sleep 2
done
echo "The emulated plug is up; converting it unattended..."

# The eWeLink account, for this run only: the emulator's copy of the
# repository has no .env, so a run you sit through asks for it. Kept in the
# container's /tmp, gone when it stops, and removed after the run anyway.
answers=/tmp/autoflash-unattended.env
if [ "$stub" = 1 ]; then
    # A placeholder account: the stub answers the account API and ignores it.
    printf 'EWELINK_EMAIL=%s\nEWELINK_PASSWORD=%s\n' stub@autoflash-stub.test stub \
        | "${as_you[@]}" -i "$NAME" sh -c "umask 077 && cat > $answers"
else
    # Not the Wi-Fi in it, if any, nor a plug's password, web login or key:
    # those are your real network's and plugs', and the emulated plug's are
    # its own (given below).
    grep -v '^ *\(export \)\?\(AUTOFLASH\|S60\)_\(WIFI_\|TASMOTA_PASSWORD\|ESPHOME_\)' "$ROOT/.env" \
        | "${as_you[@]}" -i "$NAME" sh -c "umask 077 && cat > $answers"
fi
[ "$keep" = 1 ] && trap 'docker exec "$NAME" rm -f "$answers" 2>/dev/null || true' EXIT

# On a terminal the run is shown live; otherwise its final screen is printed.
[ -t 1 ] && as_you+=(-t)
stamp=$(date +%Y%m%d-%H%M%S)
status=0
# In stub mode the tool talks to the local stub and trusts its CA.
stub_env=()
[ "$stub" = 1 ] && stub_env=(-e AUTOFLASH_EWELINK_API_BASE=https://apia.autoflash-stub.test
                             -e SSL_CERT_FILE=/run/autoflash-emu/stub-ca-bundle.pem)
# The emulator's Wi-Fi, for a run from an ESPHome that cannot say what it is
# (one older than this project's 1.1.0); and the emulated plug's passwords,
# for one given one to test with, as this script's environment has them.
"${as_you[@]}" -e AUTOFLASH_EMULATOR_URL="http://localhost:$PORT" -e AUTOFLASH_EMULATOR_SUDO_PASSWORD=autoflash \
    "${stub_env[@]}" -e AUTOFLASH_WIFI_SSID=PICSimLabWifi -e AUTOFLASH_EMULATOR_COUNTDOWN="${AUTOFLASH_EMULATOR_COUNTDOWN:-}" \
    -e AUTOFLASH_EMULATOR_NO_AT_SAVE="${AUTOFLASH_EMULATOR_NO_AT_SAVE:-}" \
    -e AUTOFLASH_TASMOTA_PASSWORD="${AUTOFLASH_TASMOTA_PASSWORD:-}" -e AUTOFLASH_ESPHOME_OTA_PASSWORD="${AUTOFLASH_ESPHOME_OTA_PASSWORD:-}" \
    -e AUTOFLASH_ESPHOME_WEB_USER="${AUTOFLASH_ESPHOME_WEB_USER:-}" -e AUTOFLASH_ESPHOME_WEB_PASSWORD="${AUTOFLASH_ESPHOME_WEB_PASSWORD:-}" \
    -e AUTOFLASH_ESPHOME_API_KEY="${AUTOFLASH_ESPHOME_API_KEY:-}" \
    -e HOME=/tmp "$NAME" /opt/venv/bin/python tools/unattended_run.py --size 100x40 \
    --save "captures/unattended/$stamp" -- --env-file "$answers" "${run_args[@]}" || status=$?

echo "On this computer: ${STATE#"$ROOT/"}/work/captures/unattended/$stamp/"
[ "$keep" = 1 ] && echo "The emulator is still running: http://localhost:$PORT, or docker attach $NAME"
exit "$status"
