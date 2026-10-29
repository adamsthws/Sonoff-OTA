"""Synthesise the eWeLink BLE provisioning session (no BLE radio needed).

The emulator has no BLE radio, but the firmware's entire BLE host, GATT server and
provisioning logic run. eWeLink provisioning messages enter at one choke point: the
command dispatcher `0x72940(type, payload, len)`, which posts `{index,payload,len}`
to a queue (`*0x402dd0`) that a firmware consumer task drains and routes to the
per-command handler. So this feeds the provisioning messages *through the
dispatcher* — the firmware's own consumer then runs the handlers in their proper
task context (driving the handlers directly instead corrupts the shared pairing
state; going through the dispatcher does not).

The eWeLink BLE command set (named by the firmware's `[BLE CMD: …]` logs):

    0x02 AUTH_TOKEN  0x04 REQ_SCAN/REQ_AUTH  0x07 REQ_CONNECT
    0x09 DEVICE_INFO_REQUEST  0x0b SERVER_INFO_REQUEST  0x0d HIDDEN_AP_CONNECT(wifi)

Data messages are base64 + AES-128-CBC/PKCS#7; the AES key *is* the device apikey,
a 16-byte secret readable at RAM `0x40ce90` (from `factory_apikey`) — so AUTH_TOKEN
is satisfied by echoing that value, and field payloads are injected as plaintext by
short-circuiting the decoder (`0x1350d8`) and the AES (`0x711dc`). Integrity gate
(`0x29c3c`) and the Timer-5 wake are applied as elsewhere.

**What works (verified 2026-10-27):** the full sequence
`DEVICE_INFO → AUTH_TOKEN → HIDDEN_AP_CONNECT → SERVER_INFO` runs end to end — each
handler executes in the consumer task — and the plug associates (WPA2) and gets a
DHCP lease on our AP, through the firmware's real eWeLink provisioning path.

**What this does NOT reach:** the eWeLink cloud dial. After provisioning the plug
still enters Matter commissioning (`_matterc`), not the eWeLink dispatch/`/api/ws`
path. The eWeLink-provisioned/bound state that gates the cloud task (`ais_link_task`,
`0x35aa8`) is not flipped by these messages as driven, and that decision point is
not locatable by static xref (the firmware loads it `adr`-relative). See the main
doc's "eWeLink BLE provisioning protocol" section.

  $(tools/beken/emulator/bk-emu.sh python) archive/s61-research/ble_provision.py
  SEQ=devinfo,auth,wifi,server SSID=MyNet PASS=mypass python3 ble_provision.py
"""
import os
import sys
import threading
import time
import struct
import collections
import types
from pathlib import Path
ROOT = Path(os.environ.get("BK_EMU_ROOT", "/opt/beken-emulator")); sys.path.insert(0, str(ROOT))
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

DUMP = os.environ.get("S61_DUMP", str(ROOT/"firmwares/Sonoff_S61s_EUPlug_WBBK_01P_V1.3.bin"))
SECS = float(os.environ.get("SECS", "55")); FIRE_AT = float(os.environ.get("FIRE_AT", "9")); GAP = float(os.environ.get("GAP", "5"))
SSID = os.environ.get("SSID", "AutoflashNet").encode(); PW = os.environ.get("PASS", "autoflash1234").encode()
SERVER = os.environ.get("SERVER", "iot.autoflash.test").encode()
SEQ = os.environ.get("SEQ", "devinfo,auth,wifi,server").split(",")

AFTER_MEMCMP = 0x29c3c; TIMER5 = 1 << 9; APPFW_LOOP = 0x319c4; DISPATCH = 0x72940
TYPES = {"auth": 0x02, "scan": 0x04, "connect": 0x07, "devinfo": 0x09, "server": 0x0b, "wifi": 0x0d}
HENTRY = {0x02: 0x725e4, 0x04: 0x727bc, 0x07: 0x722a4, 0x09: 0x72258, 0x0b: 0x718a0, 0x0d: 0x712e0}
APIKEY_CACHE = 0x40ce90; PAY = 0x406000

netlog = []
emu = build_emulator(DUMP, chip="BK7238", with_boot=True, only_uart=True, save_decrypted=False, wifi=True, log=lambda m: netlog.append(str(m)))
emu.setup(); emu.net_sink = lambda m: netlog.append(str(m))
emu.attach_wifi_peer(role="ap", ssid=SSID.decode(), passphrase=PW.decode(), channel=1, peer_mac=b"\x02\x00\x00\x00\x00\x01")
mac, mu = emu.wifimac, emu.mu; t0 = time.time()

def _read(a, n=24):
    try:
        b = bytes(mu.mem_read(a, n)); z = b.find(0); return b[:z if z >= 0 else n]
    except Exception:
        return b""
def _pkcs7(b, blk=16):
    pad = blk - (len(b) % blk) if (len(b) % blk) else blk; return b + bytes([pad]) * pad

mu.hook_add(UC_HOOK_CODE, lambda m, a, s, u: mu.reg_write(UC_ARM_REG_R0, 0), None, AFTER_MEMCMP, AFTER_MEMCMP)
# field injection at the decoder, by caller: recv_ssid password / AUTH_TOKEN cache / else server
def h_decoder(m, a, s, u):
    if not st["go"]:
        return
    lr = mu.reg_read(UC_ARM_REG_LR); dest = mu.reg_read(UC_ARM_REG_R0); outp = mu.reg_read(UC_ARM_REG_R2)
    val = PW if lr == 0x713a8 else (_read(APIKEY_CACHE, 16) if lr == 0x72670 else SERVER)
    padded = _pkcs7(val)
    try:
        if dest:
            mu.mem_write(dest, padded)
        if outp:
            mu.mem_write(outp, struct.pack("<I", len(padded)))
    except Exception:
        pass
    mu.reg_write(UC_ARM_REG_R0, 0); mu.reg_write(UC_ARM_REG_PC, lr)
mu.hook_add(UC_HOOK_CODE, h_decoder, None, 0x1350d8, 0x1350d8)
def h_aes(m, a, s, u):
    if st["go"]:
        mu.reg_write(UC_ARM_REG_R0, 0); mu.reg_write(UC_ARM_REG_PC, mu.reg_read(UC_ARM_REG_LR))
mu.hook_add(UC_HOOK_CODE, h_aes, None, 0x711dc, 0x711dc)

hrun = collections.Counter()
for t, e in HENTRY.items():
    def mk(tt):
        def h(m, a, s, u):
            if st["go"]:
                hrun[hex(tt)] += 1
        return h
    mu.hook_add(UC_HOOK_CODE, mk(t), None, e, e)

REGS = [UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7, UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11, UC_ARM_REG_R12, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_CPSR]
st = {"go": False, "i": 0, "calling": False, "last": 0.0}
def h_trap(m, a, s, u):
    if st["calling"]:
        for r, v in zip(REGS, st["saved"]):
            mu.reg_write(r, v)
        emu._exc_epoch += 1; st["calling"] = False; st["i"] += 1; st["last"] = time.time()
mu.hook_add(UC_HOOK_CODE, h_trap, None, 0x4, 0x4)
def do_disp(typ, payload, ln):
    st["saved"] = [mu.reg_read(x) for x in REGS]; cpsr = mu.reg_read(UC_ARM_REG_CPSR)
    mu.reg_write(UC_ARM_REG_R0, typ); mu.reg_write(UC_ARM_REG_R1, payload); mu.reg_write(UC_ARM_REG_R2, ln)
    mu.reg_write(UC_ARM_REG_LR, 0x4); mu.reg_write(UC_ARM_REG_CPSR, (cpsr & ~0x20) & ~0x3f | 0x1f); mu.reg_write(UC_ARM_REG_PC, DISPATCH)
    emu._exc_epoch += 1; st["calling"] = True
def payload_for(name, addr):
    b = bytearray(0x80)
    if name == "wifi":
        n = len(SSID); b[0] = n; b[1:1+n] = SSID; b[n+2] = 60; b[n+3] = 1; ln = n + 4 + 16
    elif name in ("scan", "devinfo"):
        b[0] = 1; b[1] = 6; b[2] = 1; ln = 8
    else:
        ln = 0x20
    mu.mem_write(addr, bytes(b)); return ln
def h_appfw(m, a, s, u):
    if not st["go"] or st["calling"] or st["i"] >= len(SEQ):
        return
    if time.time() - st["last"] < GAP:
        return
    name = SEQ[st["i"]]; addr = PAY + st["i"] * 0x100; ln = payload_for(name, addr)
    netlog.append("[dispatch %s type=%#x @%.1f]" % (name, TYPES[name], time.time()-t0)); do_disp(TYPES[name], addr, ln)
mu.hook_add(UC_HOOK_CODE, h_appfw, None, APPFW_LOOP, APPFW_LOOP)
_tick = mac.tick
def hooked_tick(self, insns):
    r = _tick(insns); emu.state.timer3_5_ctl |= TIMER5
    if time.time()-t0 >= FIRE_AT and not st["go"]:
        st["go"] = True; st["last"] = time.time() - GAP
    return r
mac.tick = types.MethodType(hooked_tick, mac)
w = threading.Thread(target=emu.run, daemon=True); w.start()
time.sleep(SECS)
try:
    emu.stop()
except Exception:
    pass
time.sleep(0.4)
p = emu.wifi_peer; net = getattr(p, "net", None); ip = getattr(net, "device_ip", None)
def ips(b): return ".".join(str(x) for x in b) if isinstance(b, (bytes, bytearray)) and len(b) == 4 else str(b)
got = ip not in (None, b"\xff\xff\xff\xff", b"\x00\x00\x00\x00")
print("=== S61 BLE PROVISION (dispatcher-driven session) ===", flush=True)
print("  sequence: %s" % SEQ, flush=True)
print("  handlers run by the consumer: %s" % dict(hrun), flush=True)
print("  device IP = %s  stations=%s" % (ips(ip), {k.hex(): v for k, v in getattr(p, "stations", {}).items()}), flush=True)
ran = all(hex(TYPES[n]) in hrun for n in SEQ)
print("  %s: all %d provisioning messages ran through the firmware's handlers; the plug %s."
      % ("PASS" if ran and got else "PARTIAL", len(SEQ), "joined our AP (DHCP %s)" % ips(ip) if got else "did not get an IP"), flush=True)
import re  # noqa: E402
for ln in netlog:
    if re.search(r"DHCP:|four-way|associated|dispatch /|/api/ws|coolkit", str(ln)):
        print("    " + str(ln)[:130], flush=True)
os._exit(0)
