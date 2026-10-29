"""Complete the eWeLink cloud cycle: the emulated S61 comes ONLINE against a stub.

Builds on s61_cloud_dial.py (which gets the plug to dial GET /dispatch/device) by
standing up the local CoolKit stub (tools/emulator/stub_cloud/stub_server.py) on
loopback and bridging the emulator's netstack to it, then driving the whole
handshake:

    GET /dispatch/device   -> stub answers {error:0, IP, domain, port} (itself)
    wss:// connect, GET /api/ws, TLS handshake  -> WebSocket upgrade (RFC 6455)
    {"action":"register", ...}   -> stub answers {error:0, config:{hb,...}}
    => the plug is ONLINE on the eWeLink IoT channel (then heartbeats / TZ query).

Dispatch is plaintext (the S61's factory dispatch URL is http://...:80), but the
IoT WebSocket is wss:// and the firmware verifies the server certificate against
a baked-in CoolKit root CA (authmode REQUIRED), so a self-signed stub cert is
rejected (bad_certificate). We clear that the same honest way the S60 does
(tools/emulator/patch_stock.py --trust-ca): a *trust-anchor swap*. The S60 swaps
the CA PEM in its flash image; the S61 does not keep its CA as a static flash
blob (it is assembled into RAM at TLS-setup time), so the swap is applied at the
one point the firmware installs it - mbedtls_x509_crt_parse (0x125e4c): when the
firmware loads the CoolKit root CA into its trust store, we substitute our own
CA. Nothing in the verification path is weakened; only which anchor is trusted
changes, and the stub serves a leaf signed by that CA. Our CA/leaf are EC P-256
(made with openssl); the leaf carries a DNS and an IP SAN for the dialled IoT
address and serverAuth EKU, which the firmware's mbedTLS requires.

The bridge is the netstack's own relay (attach_wifi_peer(net_out=True)): the plug
dials a placeholder public IP (its factory dispatch host, off its own subnet so
it routes through the gateway), which we redirect to the loopback stub -
plaintext dispatch on one port, TLS IoT on another. Everything stays on this
host; nothing reaches the real cloud.

Two synthesised pieces, as in s61_cloud_dial.py: Wi-Fi creds seeded at
ef_get_env_blob, and the pairing-success trigger (force-call the start-cloud
handler 0x2c204) once Wi-Fi is up.

  $(docs/s61-tools/emulator/bk-emu.sh python) docs/s61-tools/s61_cloud_online.py
"""
import os
import sys
import threading
import time
import struct
import re
import types
import base64
import tempfile
import subprocess
from pathlib import Path
ROOT = Path(os.environ.get("BK_EMU_ROOT", "/opt/beken-emulator")); sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "emulator" / "stub_cloud"))
from src.main import build_emulator  # noqa: E402
from unicorn import UC_HOOK_CODE  # noqa: E402
from unicorn.arm_const import (  # noqa: E402
    UC_ARM_REG_CPSR,
    UC_ARM_REG_LR,
    UC_ARM_REG_PC,
    UC_ARM_REG_R0,
    UC_ARM_REG_R1,
    UC_ARM_REG_R10,
    UC_ARM_REG_R11,
    UC_ARM_REG_R12,
    UC_ARM_REG_R2,
    UC_ARM_REG_R3,
    UC_ARM_REG_R4,
    UC_ARM_REG_R5,
    UC_ARM_REG_R6,
    UC_ARM_REG_R7,
    UC_ARM_REG_R8,
    UC_ARM_REG_R9,
    UC_ARM_REG_SP)
import stub_server as ss  # noqa: E402
import json as _json  # noqa: E402
class _Compact:   # the S61's embedded JSON parser rejects whitespace between tokens
    @staticmethod
    def dumps(obj, *a, **k): return _json.dumps(obj, separators=(",", ":"))
    loads = staticmethod(_json.loads)
ss.json = _Compact()

DUMP = os.environ.get("S61_DUMP", str(ROOT/"firmwares/Sonoff_S61s_EUPlug_WBBK_01P_V1.3.bin"))
SECS = float(os.environ.get("SECS", "85")); FORCE_AT = float(os.environ.get("FORCE_AT", "34"))
HB = int(os.environ.get("HB", "10"))
SSID = os.environ.get("SSID", "AutoflashNet").encode(); PW = os.environ.get("PASS", "autoflash1234").encode()
AFTER_MEMCMP = 0x29c3c; TIMER5 = 1 << 9; APPFW_LOOP = 0x319c4; EFGET = 0x12f438
CTXP = 0x4044ac; FLAG = 0x403e31; START_HANDLER = 0x2c204; URL_OFF = 0x5f
# mbedtls_x509_crt_parse: where the firmware installs a cert into a trust store
# (r0=chain, r1=PEM buffer, r2=len). We swap the CoolKit root CA here.
X509_CRT_PARSE = 0x125e4c
COOLKIT_CN = b"CoolKit Root CA ECC"   # the baked-in trust anchor's subject CN, inside its DER

# The dispatch server the firmware dials by default (ctx+0x5f): a placeholder
# public IP, so it routes off the device's subnet to the gateway (not loopback,
# which the device's own stack would keep). We leave the context untouched and
# redirect this target to the stub below, so nothing reaches the real address.
DISPATCH_IP, DISPATCH_PORT = "8.8.8.8", 80
WS_PORT = 443   # device-facing port dispatch hands back for the IoT WebSocket

# --- our own CA + the stub's leaf: the trust-anchor swap material (EC P-256) ---
# Regenerated each run into a temp dir: the swap is applied at runtime, so the CA
# need not persist (unlike the S60, whose firmware is patched once to trust it).
def _make_certs(d, ip):
    ca_key, ca_crt = d/"ca.key", d/"ca.crt"
    lf_key, lf_crt, lf_csr = d/"leaf.key", d/"leaf.crt", d/"leaf.csr"
    ext = d/"leaf.ext"; ext.write_text(
        "subjectAltName=DNS:%s,IP:%s\nextendedKeyUsage=serverAuth\n"
        "basicConstraints=critical,CA:FALSE\n" % (ip, ip), encoding="utf-8")
    run = lambda *a: subprocess.run(a, check=True, capture_output=True)
    run("openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(ca_key))
    run("openssl", "req", "-x509", "-new", "-key", str(ca_key), "-sha256", "-days", "36500",
        "-subj", "/O=Autoflash stub/CN=Autoflash stub root CA",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-out", str(ca_crt))
    run("openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(lf_key))
    run("openssl", "req", "-new", "-key", str(lf_key), "-subj", "/CN=%s" % ip, "-out", str(lf_csr))
    run("openssl", "x509", "-req", "-in", str(lf_csr), "-CA", str(ca_crt), "-CAkey", str(ca_key),
        "-CAcreateserial", "-days", "36500", "-sha256", "-extfile", str(ext), "-out", str(lf_crt))
    return ca_crt.read_bytes(), str(lf_crt), str(lf_key)

_CD = Path(tempfile.mkdtemp(prefix="s61stub-"))
OURCA_PEM, LEAF_CRT, LEAF_KEY = _make_certs(_CD, DISPATCH_IP)

stublog = []
ss.log = lambda *p: stublog.append(" ".join(str(x) for x in p))
import ssl  # noqa: E402
ss.Handler.stub = ss.Stub(DISPATCH_IP, WS_PORT, HB)
# The S61's dispatch parser reads keys "error","domain","IP","port", formats the
# IoT URL as `wss://%.*s:%.*s` (PORT is a STRING; the S60 took a number), and
# connects to `domain`. Hand back all of it, compact (see _Compact above).
ss.Handler.stub.dispatch_response = lambda: {
    "error": 0, "reason": "", "IP": DISPATCH_IP, "domain": DISPATCH_IP, "port": str(WS_PORT)}
plain_srv = ss.PlainServer(("127.0.0.1", 0), ss.Handler); DISP_STUB = plain_srv.server_address[1]
_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); _ctx.load_cert_chain(LEAF_CRT, LEAF_KEY)
tls_srv = ss.TLSServer(("127.0.0.1", 0), ss.Handler, _ctx); WS_STUB = tls_srv.server_address[1]
threading.Thread(target=plain_srv.serve_forever, daemon=True).start()
threading.Thread(target=tls_srv.serve_forever, daemon=True).start()

netlog = []
emu = build_emulator(DUMP, chip="BK7238", with_boot=True, only_uart=True, save_decrypted=False, wifi=True, log=lambda m: netlog.append(str(m)))
emu.setup()
t0 = time.time()
emu.net_sink = lambda m: netlog.append(str(m))
emu.attach_wifi_peer(role="ap", ssid=SSID.decode(), passphrase=PW.decode(), channel=1,
                     peer_mac=b"\x02\x00\x00\x00\x00\x01", net_out=True)
mac, mu = emu.wifimac, emu.mu

# Keep "net_out" fully contained: the relay's outbound connects go to the stub
# for the dispatch IP (plaintext dispatch on :80, TLS IoT otherwise), and nowhere
# for anything else, so nothing leaves the host.
_io = emu.wifi_peer.net.io
_orig_connect = _io.connect
def _connect_redirect(conn, host, port):
    if host == DISPATCH_IP and port == DISPATCH_PORT:
        return _orig_connect(conn, "127.0.0.1", DISP_STUB)   # plaintext dispatch
    if host == DISPATCH_IP:
        return _orig_connect(conn, "127.0.0.1", WS_STUB)     # TLS IoT WebSocket
    return _orig_connect(conn, "127.0.0.1", 1)               # refuse anything else
_io.connect = _connect_redirect

def _rd(a, n):
    try:
        return bytes(mu.mem_read(a, n))
    except Exception:
        return b""
def _rstr(a, n=40):
    b = _rd(a, n); z = b.find(0); return b[:z if z >= 0 else n]

mu.hook_add(UC_HOOK_CODE, lambda m, a, s, u: mu.reg_write(UC_ARM_REG_R0, 0), None, AFTER_MEMCMP, AFTER_MEMCMP)

def h_ef(m, a, s, u):
    key = _rstr(mu.reg_read(UC_ARM_REG_R0)); buf = mu.reg_read(UC_ARM_REG_R1); outp = mu.reg_read(UC_ARM_REG_R3)
    val = SSID if key == b"wifi-ssid" else (PW if key == b"wifi-pass" else None)
    if val is None:
        return
    try:
        mu.mem_write(buf, val)
        if outp:
            mu.mem_write(outp, struct.pack("<I", len(val)))
    except Exception:
        pass
    mu.reg_write(UC_ARM_REG_R0, len(val)); mu.reg_write(UC_ARM_REG_PC, mu.reg_read(UC_ARM_REG_LR))
mu.hook_add(UC_HOOK_CODE, h_ef, None, EFGET, EFGET)

# --- the faithful trust swap (the S60's --trust-ca, applied at the parse point) ---
# When the firmware loads the CoolKit root CA into a trust store, replace the PEM
# it is about to parse with our own CA's PEM. Identified by the CoolKit CN inside
# the decoded DER, so only the trust anchor is touched; the server leaf and any
# other cert parse untouched. The firmware passes len incl. the NUL terminator.
swap = {"n": 0, "skip": []}
_PEM_B, _PEM_E = b"-----BEGIN CERTIFICATE-----", b"-----END CERTIFICATE-----"
def _is_coolkit(buf):
    i = buf.find(_PEM_B); j = buf.find(_PEM_E, i)
    if i < 0 or j < 0:
        return False
    try:
        der = base64.b64decode(buf[i+len(_PEM_B):j].replace(b"\r", b"").replace(b"\n", b""))
    except Exception:
        return False
    return COOLKIT_CN in der
def h_trust(m, a, s, u):
    r1 = mu.reg_read(UC_ARM_REG_R1); r2 = mu.reg_read(UC_ARM_REG_R2)
    if r1 == 0 or not (0 < r2 <= 8192):
        return
    if not _is_coolkit(_rd(r1, min(r2, 4096))):
        return
    repl = (OURCA_PEM if OURCA_PEM.endswith(b"\n") else OURCA_PEM + b"\n") + b"\x00"
    if len(repl) > r2:   # must fit the firmware's buffer; our EC CA PEM does
        swap["skip"].append("%d>%d" % (len(repl), r2)); return
    try:
        mu.mem_write(r1, repl); mu.reg_write(UC_ARM_REG_R2, len(repl)); swap["n"] += 1
    except Exception as e:
        swap["skip"].append(str(e))
mu.hook_add(UC_HOOK_CODE, h_trust, None, X509_CRT_PARSE, X509_CRT_PARSE)

cnt = {"ais_cloud_start": 0}
mu.hook_add(UC_HOOK_CODE, lambda m, a, s, u: cnt.__setitem__("ais_cloud_start", cnt["ais_cloud_start"] + 1),
            None, 0x35744, 0x35744)

REGS = [UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7, UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11, UC_ARM_REG_R12, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_CPSR]
st = {"forced": False, "calling": False, "prepped": False}
def h_trap(m, a, s, u):
    if st["calling"]:
        for r, v in zip(REGS, st["saved"]):
            mu.reg_write(r, v)
        emu._exc_epoch += 1; st["calling"] = False
mu.hook_add(UC_HOOK_CODE, h_trap, None, 0x4, 0x4)
def force_thumb(pc):
    st["saved"] = [mu.reg_read(x) for x in REGS]; cpsr = mu.reg_read(UC_ARM_REG_CPSR)
    mu.reg_write(UC_ARM_REG_LR, 0x5); mu.reg_write(UC_ARM_REG_CPSR, cpsr | 0x20); mu.reg_write(UC_ARM_REG_PC, pc | 1)
    emu._exc_epoch += 1; st["calling"] = True
def h_appfw(m, a, s, u):
    if st["forced"] or st["calling"] or time.time()-t0 < FORCE_AT:
        return
    if not st["prepped"]:
        st["prepped"] = True
        p = struct.unpack("<I", _rd(CTXP, 4))[0]
        netlog.append("[dispatch url (factory) = %r -> redirected to the stub]" % _rstr(p+URL_OFF, 32))
        try:
            mu.mem_write(FLAG, b"\x01")
        except Exception:
            pass
        return
    st["forced"] = True; netlog.append("[pairing-success trigger @%.1f]" % (time.time()-t0)); force_thumb(START_HANDLER)
mu.hook_add(UC_HOOK_CODE, h_appfw, None, APPFW_LOOP, APPFW_LOOP)
_tick = mac.tick
def hooked_tick(self, insns):
    r = _tick(insns); emu.state.timer3_5_ctl |= TIMER5; return r
mac.tick = types.MethodType(hooked_tick, mac)

w = threading.Thread(target=emu.run, daemon=True); w.start()
time.sleep(SECS)
try:
    emu.stop()
except Exception:
    pass
time.sleep(0.5)
p = emu.wifi_peer; nst = getattr(p, "net", None); ip = getattr(nst, "device_ip", None)
def ips(b): return ".".join(str(x) for x in b) if isinstance(b, (bytes, bytearray)) and len(b) == 4 else str(b)
print("=== S61 CLOUD ONLINE (dispatch :%d plaintext, ws :%d TLS, trust-swapped) ===" % (DISP_STUB, WS_STUB), flush=True)
print("  device IP = %s   dial-chain = %s   CA trust-swaps = %d%s"
      % (ips(ip), dict(cnt), swap["n"], (" skipped=%s" % swap["skip"] if swap["skip"] else "")), flush=True)
print("  --- stub (eWeLink CoolKit) log ---", flush=True)
for ln in stublog:
    print("    " + re.sub(r"^\S+\s", "", ln)[:150], flush=True)
reg = any("register" in ln for ln in stublog)
wsopen = any("websocket open" in ln for ln in stublog)
online = any("ws ->" in ln and '"config"' in ln and '"error":0' in ln for ln in stublog)
print("  --- verdict ---", flush=True)
print("  dispatch answered: %s | websocket open: %s | register: %s | online (error:0+config): %s"
      % (any("dispatch ->" in ln for ln in stublog), wsopen, reg, online), flush=True)
print("  %s" % ("PASS: the plug trusts our CA, completed the eWeLink cloud handshake and is ONLINE."
                if (wsopen and reg and online) else
                "INCOMPLETE: see the stub log + netstack trace below."), flush=True)
if not (wsopen and reg and online):
    print("    [raw netlog tail, last 30 of %d] ---" % len(netlog), flush=True)
    for ln in netlog[-30:]:
        print("    net: " + str(ln)[:150], flush=True)
os._exit(0)
