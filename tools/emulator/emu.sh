#!/bin/bash
# Start the virtual Sonoff S60 and a "computer" to convert it from, in Docker.
#
#   tools/emulator/emu.sh            plug offline: nothing reaches the internet
#   tools/emulator/emu.sh --cloud    plug and tool online (real plug must be unplugged)
#   tools/emulator/emu.sh --stub-cloud  a local eWeLink stub serves this instance alone, so
#                                    it comes online with no real cloud; the plug stays
#                                    offline (docs/cloud-stub.md). Not with --cloud.
#   tools/emulator/emu.sh --yes      with --cloud: the real plug is known to be unplugged; don't ask
#   tools/emulator/emu.sh --plug MAC which backed-up plug to run; asked when there are several
#   tools/emulator/emu.sh --plug-dir DIR  a plug with no Sonoff backup: a public one
#                                    (sanitise_plug.py), whose own flash and eFuses are in DIR
#   tools/emulator/emu.sh --fresh    put the plug back to its Sonoff backup, and forget the run and the account
#   tools/emulator/emu.sh --pcap     also record the plug's Wi-Fi traffic (captures/emulation/state/logs)
#   tools/emulator/emu.sh --rebuild  rebuild the image first
#   tools/emulator/emu.sh --detach   start in the background; join: docker exec -it -u ubuntu autoflash-emu tmux attach -t autoflash
#   tools/emulator/emu.sh --no-firewall  without the "computer's" firewall (ufw), on by default
#   tools/emulator/emu.sh --windows  Windows is the computer, wired in by windows_link.py
#                                    (from Windows, start it all with tools/emulator/emu.ps1)
#   tools/emulator/emu.sh --instance N  another emulator beside the first (N = 1-30): its own
#                                    container (autoflash-emu-N), state (state-N) and browser
#                                    port (8080+N), so tests can run side by side
#
# Runs a plug from its Sonoff backup, which every conversion keeps in
# captures/plugs/<MAC>/sonoff-backup/ (or a dump in captures/emulation/; see
# docs/emulator.md). The backup is only read: the plug runs a copy. Everything
# the emulator writes stays in captures/emulation/state/, which is private and
# Git-ignored (from a Windows checkout under WSL: ~/.local/share/autoflash-emu/state,
# on WSL's own disk); each plug's flash is kept there between runs.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
EMU=$ROOT/captures/emulation
STATE=$EMU/state
# A Windows checkout under WSL (/mnt/c, reached through 9p) is far too slow for
# the plug's flash: a write can take 100 ms, and the bridge then trips its
# interrupt watchdog while flashing Tasmota. Its state lives on WSL's own disk.
case "$ROOT" in /mnt/[a-z]/*) STATE=$HOME/.local/share/autoflash-emu/state ;; esac
PY=$ROOT/.venv/bin/python
# A Windows checkout's .venv is Windows' own: under WSL the NVS generator lives here.
[ -x "$PY" ] || PY=$HOME/.local/share/autoflash-emu/venv/bin/python
# The image tag; override to build/run an isolated image (e.g. the cloud-stub
# work) without disturbing the shared one.
IMAGE=${AUTOFLASH_EMU_IMAGE:-autoflash-emu}
NAME=autoflash-emu PORT=8080 LINK_PORT=7860

cloud=0 fresh=0 pcap=0 rebuild=0 firewall=1 windows=0 yes=0 stub=0 tty=-it plug="" plug_dir="" instance=""
while [ $# -gt 0 ]; do
    arg=$1
    case "$arg" in
    --plug) [ $# -ge 2 ] || { echo "--plug needs a MAC address" >&2; exit 2; }; plug=$2; shift ;;
    --plug=*) plug=${arg#--plug=} ;;
    --plug-dir=*) plug_dir=$(cd "${arg#--plug-dir=}" && pwd) ;;
    --instance) [ $# -ge 2 ] || { echo "--instance needs a number, 1-30" >&2; exit 2; }; instance=$2; shift ;;
    --instance=*) instance=${arg#--instance=} ;;
    --cloud) cloud=1 ;;
    --stub-cloud) stub=1 ;;
    --yes) yes=1 ;;
    --fresh) fresh=1 ;;
    --pcap) pcap=1 ;;
    --rebuild) rebuild=1 ;;
    --detach) tty=-dt ;;
    --no-firewall) firewall=0 ;;
    --windows) windows=1 ;;
    *) sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
    esac
    shift
done
if [ -n "$instance" ]; then
    case "$instance" in [1-9]|[12][0-9]|30) ;; *) echo "--instance needs a number, 1-30" >&2; exit 2 ;; esac
    STATE=$STATE-$instance NAME=autoflash-emu-$instance
    PORT=$((8080 + instance)) LINK_PORT=$((7860 + instance))
fi
# The local cloud stub stands in for eWeLink for this instance alone, so it
# cannot be combined with the real cloud (--cloud). See docs/cloud-stub.md.
STUB_CA=$EMU/stub-cloud
if [ "$stub" = 1 ] && [ "$cloud" = 1 ]; then
    echo "--stub-cloud cannot be combined with --cloud" >&2; exit 2
fi
"$PY" -c 'import esp_idf_nvs_partition_gen' 2>/dev/null || {
    venv=$(dirname "$(dirname "$PY")")
    echo "The emulator needs ESP-IDF's NVS generator in $venv:" >&2
    echo "    python3 -m venv $venv && $PY -m pip install --require-hashes -r $ROOT/requirements/ci.txt" >&2
    exit 1
}
# The stub's CA and the trust swap are made with `cryptography`.
[ "$stub" = 0 ] || "$PY" -c 'import cryptography' 2>/dev/null || {
    echo "--stub-cloud needs the cryptography package in $(dirname "$(dirname "$PY")"):" >&2
    echo "    $PY -m pip install --require-hashes -r $ROOT/requirements/ci.txt" >&2
    exit 1
}
if docker ps -q --filter "name=^$NAME$" | grep -q .; then
    echo "The emulator is already running. Join it with: docker exec -it -u ubuntu $NAME tmux attach -t autoflash" >&2
    exit 1
fi

# -- which plug ------------------------------------------------------------------
# shellcheck disable=SC2174  # each folder named is made 0700; its parents need not be
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
if [ -n "$plug_dir" ]; then
    # A public plug: nothing of Sonoff's anywhere. What reads the Sonoff
    # backup - the stub's device identity, the log's redaction - is given the
    # plug's own flash, in which they find nothing; it cannot go back to Sonoff.
    [ -f "$plug_dir/flash.bin" ] && [ -f "$plug_dir/efuse.bin" ] || {
        echo "--plug-dir: $plug_dir has no flash.bin and efuse.bin" >&2; exit 2; }
    [ "$fresh" = 0 ] || { echo "--fresh: a public plug has no Sonoff backup to go back to" >&2; exit 2; }
    printf '%s\n%s\n' "$plug_dir" "$(efuse_mac "$plug_dir/efuse.bin")" > "$STATE/plug"
else
    "$PY" -I "$ROOT/tools/emulator/choose_plug.py" --out "$STATE/plug" ${plug:+--plug "$plug"} || exit $?
fi
SRC=$(sed -n 1p "$STATE/plug")
mac=$(sed -n 2p "$STATE/plug")
# Another plug than last time: its own flash, kept from its last run, comes
# back, and the one that was running is put by for its next - each with the
# mode its flash was built for (below).
if [ "$mac" != "$was" ]; then
    if [ -n "$was" ] && [ -f "$STATE/flash.bin" ]; then
        mv "$STATE/flash.bin" "$STATE/plugs/${was//:/-}.bin"
        if [ -f "$STATE/.flash-mode" ]; then mv "$STATE/.flash-mode" "$STATE/plugs/${was//:/-}.mode"; fi
    fi
    if [ -f "$STATE/plugs/${mac//:/-}.bin" ]; then
        mv "$STATE/plugs/${mac//:/-}.bin" "$STATE/flash.bin"
        rm -f "$STATE/.flash-mode"
        if [ -f "$STATE/plugs/${mac//:/-}.mode" ]; then mv "$STATE/plugs/${mac//:/-}.mode" "$STATE/.flash-mode"; fi
    fi
fi
# A stub run's flash differs from a cloud run's: it trusts the stub's CA and
# points dispatch at the stub. A plug's flash is the plug as the last run left
# it, so it is never thrown away to switch: --fresh does that, knowingly.
# (A flash from before modes were recorded is a cloud one.)
# A public plug's runs nothing of Sonoff's, so it is the same either way.
mode=plain; [ "$stub" = 1 ] && mode=stub; [ -n "$plug_dir" ] && mode=public
built_for=$(cat "$STATE/.flash-mode" 2>/dev/null || echo plain)
if [ "$fresh" = 0 ] && [ -f "$STATE/flash.bin" ] && [ "$built_for" != "$mode" ]; then
    want=$([ "$stub" = 1 ] && echo "--stub-cloud" || echo "a run without --stub-cloud")
    echo "This instance's plug was set up for $([ "$built_for" = stub ] && echo "the local stub" || echo "the real cloud")," >&2
    echo "not for $want. Add --fresh to put it back to its Sonoff backup, or use another --instance." >&2
    exit 1
fi
# The plug's product model (S60TPG UK, S60TPF Europe) is eWeLink's name for it,
# given when it was paired, and is not in its dump. The stub reports the one
# eWeLink gave this plug in the tool's own records of earlier runs (a real
# conversion's, a kept plug's, an instance's), or AUTOFLASH_STUB_PRODUCT_MODEL; with
# neither, none - and the run's report leaves it blank rather than guess.
product_seen() {
    "$PY" -I - "$ROOT/captures" "$EMU/saved" "$(dirname "$STATE")" "${mac//:/-}" <<'EOF'
import glob, json, os, re, sys
captures, saved, states, plug = sys.argv[1:]
seen = []
for runs in (f"{captures}/plugs/{plug}/runs", f"{saved}/*/captures/plugs/{plug}/runs",
             f"{states}/state*/work/captures/plugs/{plug}/runs"):
    for path in glob.glob(f"{runs}/*/autoflash-state.json"):
        try:
            with open(path) as f:
                model = str((json.load(f).get("inputs") or {}).get("product_model") or "").strip().upper()
        except (OSError, ValueError, AttributeError):
            continue
        if re.fullmatch(r"S60TP[A-Z]", model):
            seen.append((os.path.getmtime(path), model))
print(max(seen)[1] if seen else "")
EOF
}
product=""
if [ "$stub" = 1 ]; then
    product=$(printf '%s' "${AUTOFLASH_STUB_PRODUCT_MODEL:-}" | tr '[:lower:]' '[:upper:]')
    [ -n "$product" ] || product=$(product_seen)
    case "$product" in
    ""|S60TP[A-Z]) ;;
    *) echo "AUTOFLASH_STUB_PRODUCT_MODEL is S60TPG or S60TPF, not '$product'" >&2; exit 2 ;;
    esac
fi

# -- the image -----------------------------------------------------------------
# The image, labelled with a hash of what it is built from: the published one
# with that hash, or built here (tools/emulator/image.sh). An image from an
# older checkout never runs unseen. AUTOFLASH_EMU_NO_PULL=1 always builds.
ensure=(ensure "$IMAGE")
[ "$rebuild" = 0 ] || ensure+=(--rebuild)
"$ROOT/tools/emulator/image.sh" "${ensure[@]}"

# -- the plug's flash and eFuses ---------------------------------------------
if [ "$fresh" = 1 ]; then
    rm -rf "$STATE/flash.bin" "$STATE/work/captures" "$STATE/.flash-mode"
    echo "The plug is back to its Sonoff backup; the previous run and eWeLink account are forgotten."
fi
if [ "$stub" = 1 ]; then
    # Our own CA + leaf for the stub domain (once; kept stable - it is flashed in).
    # The plug's own identity is read from its dump inside the container, so the
    # LAN key stays on that network (see entrypoint.sh).
    "$PY" -I "$ROOT/tools/emulator/stub_cloud/make_ca.py" --out-dir "$STUB_CA" >/dev/null
fi
if [ ! -f "$STATE/flash.bin" ]; then
    # Home Wi-Fi rewritten to the emulator's open network, Bluetooth start-up
    # skipped (the emulator has no Bluetooth), re-sealed for the bootloader. In
    # stub mode, dispatch is also redirected to the stub and our CA is trusted.
    nvs_args=() patch_args=()
    if [ "$stub" = 1 ]; then
        nvs_args=(--dis-server disp.autoflash-stub.test --dis-port 443)
        patch_args=(--trust-ca "$STUB_CA/ca.crt")
    fi
    "$PY" -I "$ROOT/tools/emulator/rewrite_nvs.py" "$SRC/flash.bin" "$STATE/wifi-flash.bin" "${nvs_args[@]}" \
        2>&1 | grep -v 'absolute path given' >/dev/null
    "$PY" -I "$ROOT/tools/emulator/patch_stock.py" "$STATE/wifi-flash.bin" "$STATE/flash.bin" "${patch_args[@]}" >/dev/null
    rm -f "$STATE/wifi-flash.bin"
    echo "$mode" > "$STATE/.flash-mode"
fi
# Copied by content: the backup's files are read-only, and a copy would be too.
rm -f "$STATE/efuse.bin" && cat "$SRC/efuse.bin" > "$STATE/efuse.bin" && truncate -s 1024 "$STATE/efuse.bin"
chmod 0600 "$STATE"/*.bin

# -- the "computer": a copy of this repository ---------------------------------
# A copy, so a run's lock renames and captures never touch the real checkout.
# The code is refreshed on every start, and the run's captures are kept.
# Not .env: a run you sit through asks for the eWeLink account, as a real
# one does. unattended-test.sh hands it to its run alone.
# Excluded whatever they are: in a git worktree .git is a file, and captures or
# .venv may be symlinks to the main checkout's - copied, a dangling link would
# replace the run's own captures.
mkdir -p "$STATE/work/captures"
rsync -a --delete --exclude /captures --exclude /.venv --exclude /.python --exclude /.git \
      --exclude /.env --exclude __pycache__/ "$ROOT/" "$STATE/work/"
rm -f "$STATE/work/.env"  # one copied before it was left out: --delete spares excluded files
# Not the firmware bundle in captures/safeboot-recovery-migration: a real
# bundle is set up for the real home Wi-Fi. The run builds its own, as a new
# user's would (it downloads the official files: --cloud or --stub-cloud).
# The eWeLink account email last used here (captures/settings.json) is offered
# to save typing it - but not with --fresh, which starts as a new user would.
# Not to a public plug's run, which is kept as public (--plug-dir).
if [ "$fresh" = 0 ] && [ -z "$plug_dir" ] && [ ! -f "$STATE/work/captures/settings.json" ] \
        && [ -f "$ROOT/captures/settings.json" ]; then
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
# Developing the QEMU fork: AUTOFLASH_EMU_QEMU=path/to/qemu-system-riscv32 runs that
# build in place of the image's (tools/emulator/qemu-build.Dockerfile builds one).
dev=()
[ -n "${AUTOFLASH_EMU_QEMU:-}" ] && dev=(-v "$(realpath "$AUTOFLASH_EMU_QEMU"):/opt/qemu/bin/qemu-system-riscv32:ro")
# The stub's CA and leaf cert, read-only, for the in-container stub server.
stubvol=()
[ "$stub" = 1 ] && stubvol=(-v "$STUB_CA:/stub-cloud:ro")
# Awake while you are in it, so a conversion is not cut off by sleep: a lock
# held only while this runs (systemd-inhibit), changing no setting.
awake=()
if [ "$tty" = -it ] && systemd-inhibit --what=sleep:idle true 2>/dev/null; then
    awake=(systemd-inhibit --what=sleep:idle --who="Autoflash emulator" --why="the emulated plug's conversion" --mode=block)
fi
# Capped: the plug, its router and the tool used about 80 MB between them.
exec "${awake[@]}" docker run --rm $tty --name "$NAME" --hostname autoflash-emu-computer \
    --memory "${AUTOFLASH_EMU_MEMORY:-256m}" --cpus "${AUTOFLASH_EMU_CPUS:-2}" \
    --cap-add NET_ADMIN --device /dev/net/tun \
    --sysctl net.ipv4.ip_forward="$(( cloud || windows ))" \
    "${publish[@]}" "${dev[@]}" \
    -v "$STATE:/state" -v "$SRC/flash.bin:/secrets/stock-flash.bin:ro" "${stubvol[@]}" \
    -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" -e PLUG_MAC="$mac" \
    -e CLOUD="$cloud" -e PCAP="$pcap" -e FIREWALL="$firewall" -e WINDOWS="$windows" \
    -e STUB_CLOUD="$stub" -e STUB_PRODUCT_MODEL="$product" \
    -e AUTOFLASH_EMULATOR_URL="http://localhost:$PORT" \
    "$IMAGE"
