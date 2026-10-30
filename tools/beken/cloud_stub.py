"""The emulated S61's eWeLink: the local stub (tools/espressif/emulator/stub_cloud),
served on its router's address for the plug and for the tool alike, as
bk-emu.sh plug runs it (plug-net.sh).

    python3 -I -B cloud_stub.py --state DIR --bind ADDRESS --flash DIR/flash.bin

Dispatch is plaintext on :80, as the S61's factory dispatch URL is; the IoT
WebSocket and the account API are TLS on :443, under a CA of our own made once
into DIR (ca.crt, with the leaf beside it), which the plug is made to trust
(cloud_online.StockHooks' trust-anchor swap) and the tool is given
(SSL_CERT_FILE). The plug's identity is what it says as it registers; its
hardware address, which eWeLink's record carries, is read from its flash.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import ssl
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "espressif" / "emulator" / "stub_cloud"))
import backup  # noqa: E402
import cloud_online  # noqa: E402  (its compact JSON and certificates; needs the emulator image)
import stub_server as ss  # noqa: E402

# The S61's region of its own data, where the factory record keeps its MAC.
REGION = 0x3E0000
HEARTBEAT = 145


def certificates(state: Path, address: str) -> tuple[Path, Path]:
    """The leaf and its key in `state`, made with their CA for `address` - and
    made again for another (a plug started on another network): the plug
    checks the leaf names the address it dialled."""
    leaf, key, made_for = state / "leaf.crt", state / "leaf.key", state / "certs-for"
    try:
        current = made_for.read_text(encoding="utf-8").strip() == address
    except OSError:
        current = False
    if not (current and leaf.exists() and key.exists() and (state / "ca.crt").exists()):
        made = state / "certs.part"
        made.mkdir(exist_ok=True)
        cloud_online._make_certs(made, address)
        for name in ("ca.crt", "leaf.crt", "leaf.key"):
            os.replace(made / name, state / name)
        shutil.rmtree(made)
        made_for.write_text(address + "\n", encoding="utf-8")
    return leaf, key


def device(flash: Path) -> dict:
    """The account record's part the plug's register does not give."""
    record = {"model": "SN-BK7238-S61-01", "productModel": "S61STPF", "name": "Sonoff S61"}
    try:
        record["staMac"] = backup.factory_mac(flash.read_bytes()[REGION:REGION + 0x1000])
    except (OSError, ValueError):
        pass
    return record


def remember(stub: ss.Stub, path: Path) -> None:
    """What plugs have said of themselves, kept in `path` across restarts: an
    eWeLink account keeps its devices while they run another firmware."""
    try:
        stub.reported.update(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    heard = stub.heard

    def heard_and_kept(message: dict) -> None:
        heard(message)
        part = path.with_suffix(".part")
        part.write_text(json.dumps(stub.reported), encoding="utf-8")
        os.replace(part, path)
    stub.heard = heard_and_kept


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--bind", required=True)
    parser.add_argument("--flash", type=Path, required=True)
    args = parser.parse_args(argv)
    leaf, key = certificates(args.state, args.bind)
    stub = ss.Handler.stub = ss.Stub(args.bind, 443, HEARTBEAT, device(args.flash))
    remember(stub, args.state / "stub-reported.json")
    # The S61 parses the port as a string; the tool takes either.
    stub.dispatch_response = lambda: {"error": 0, "reason": "", "IP": args.bind, "domain": args.bind,
                                      "port": "443"}
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(leaf), str(key))
    servers = [ss.PlainServer((args.bind, 80), ss.Handler), ss.TLSServer((args.bind, 443), ss.Handler, context)]
    ss.log(f"stub listening on {args.bind}:80 (dispatch) and :443 (TLS)")
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    servers[0].serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
