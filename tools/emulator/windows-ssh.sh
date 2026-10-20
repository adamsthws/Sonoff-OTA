#!/bin/bash
# SSH into the Windows VM that tools/emulator/windows-test.sh is running, as
# its user autoflash, into PowerShell: to drive a test run and read its log from
# another terminal, without the screen.
#
#   tools/emulator/windows-ssh.sh                   PowerShell on Windows
#   tools/emulator/windows-ssh.sh COMMAND...        a PowerShell command there
#   tools/emulator/windows-ssh.sh --wait [SECONDS]  wait until it lets the key in (300)
#   tools/emulator/windows-ssh.sh log [-f] [LINES]  the conversion's latest run.log,
#                                                   its last LINES (50); -f follows it
#   tools/emulator/windows-ssh.sh get REMOTE LOCAL  copy a file off Windows (scp -r)
#   tools/emulator/windows-ssh.sh put LOCAL REMOTE  ... onto it, e.g. C:/Autoflash/repo/tools/
#
# It logs on with the key windows-test.sh made in ~/.cache/autoflash-emu/windows/ssh,
# which Windows lets in once it has logged on (start.ps1), about a minute after
# the run starts; before that, --wait. Its host key is not checked: it is a VM
# on 127.0.0.1 that is set up afresh now and then. The port is
# AUTOFLASH_WINDOWS_SSH_PORT (2222), as windows-test.sh's. The checkout is
# C:\Autoflash\repo, the conversion's captures C:\Autoflash\repo\captures.
set -euo pipefail

CACHE=${AUTOFLASH_WINDOWS_CACHE:-$HOME/.cache/autoflash-emu/windows}
PORT=${AUTOFLASH_WINDOWS_SSH_PORT:-2222}
KEY=$CACHE/ssh/id_ed25519

case "${1:-}" in -h|--help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;; esac
[ -f "$KEY" ] || { echo "No key at $KEY - start a run with tools/emulator/windows-test.sh first."; exit 1; }
opts=(-i "$KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
      -o LogLevel=ERROR -o ServerAliveInterval=15)

win() {  # win SSH-OPTIONS... -- COMMAND...: ssh to Windows
    local options=()
    while [ "$1" != -- ]; do options+=("$1"); shift; done
    shift
    ssh "${opts[@]}" -p "$PORT" "${options[@]}" autoflash@127.0.0.1 "$@"
}

encoded() {  # $1: a PowerShell script, as a command that runs it with nothing to quote
    # No progress records: from a PowerShell nested like this they come out
    # as XML ("#< CLIXML"), around what the script prints.
    local script="\$ProgressPreference = 'SilentlyContinue'
$1"
    printf 'powershell -NoProfile -NonInteractive -EncodedCommand %s' \
        "$(printf '%s' "$script" | iconv -t UTF-16LE | base64 -w0)"
}

case "${1:-}" in
--wait)
    limit=${2:-300}
    for (( i = 0; i < limit; i += 5 )); do
        if win -o BatchMode=yes -o ConnectTimeout=5 -- exit 2>/dev/null; then echo "Windows is up."; exit 0; fi
        sleep 5
    done
    echo "Windows did not let the key in within ${limit}s."
    exit 1
    ;;
log)
    shift
    follow="" lines=50 tty=()
    for arg in "$@"; do
        case "$arg" in
        -f) follow=" -Wait" ;;
        *[!0-9]*|"") echo "log [-f] [LINES]"; exit 2 ;;
        *) lines=$arg ;;
        esac
    done
    [ -n "$follow" ] && [ -t 0 ] && tty=(-t)  # so Ctrl-C stops it on Windows too
    win "${tty[@]}" -- "$(encoded "
\$log = Get-ChildItem C:\\Autoflash\\repo\\captures -Recurse -Filter run.log -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime | Select-Object -Last 1
if (-not \$log) { 'No run.log yet: the conversion has not started.'; exit 1 }
'== ' + \$log.FullName
Get-Content \$log.FullName -Tail $lines$follow")"
    ;;
get|put)
    [ $# = 3 ] || { echo "$1 takes two paths"; exit 2; }
    if [ "$1" = get ]; then from="autoflash@127.0.0.1:$2" to=$3; else from=$2 to="autoflash@127.0.0.1:$3"; fi
    scp "${opts[@]}" -P "$PORT" -r "$from" "$to"
    ;;
*)
    win -- "$@"
    ;;
esac
