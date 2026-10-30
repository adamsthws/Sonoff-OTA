#!/bin/bash
# A computer on the emulated S61's home Wi-Fi (bk-emu.sh computer): this
# system's roots and the plug's stub's CA trusted, so trusting the stub takes
# nothing away from any other site; then python ARGS, or a shell.
set -euo pipefail
cat /etc/ssl/certs/ca-certificates.crt /plug/ca.crt > /tmp/ca-bundle.pem
export SSL_CERT_FILE=/tmp/ca-bundle.pem
[ $# -gt 0 ] || exec bash
exec python "$@"
