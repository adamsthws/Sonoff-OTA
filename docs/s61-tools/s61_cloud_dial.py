"""Make the emulated S61 dial the eWeLink (CoolKit) cloud.

This is the whole provisioning outcome, reproduced on the radio-less emulator: the
plug joins our AP and then performs the real CoolKit cloud handshake
(`GET /dispatch/device` over TCP to its dispatch server), steerable at any server
we choose.

Two things have to be synthesised, because the emulator has no BLE radio:

1. Wi-Fi credentials. The firmware reads `wifi-ssid`/`wifi-pass` from EasyFlash
   (`ef_get_env_blob`, 0x12f438); hooking that call feeds our SSID/pass, so the
   plug associates (WPA2) and gets a DHCP lease. (Same technique as
   `s61_seed_join.py`.)

2. The pairing-success trigger. The cloud dial is gated on a chain that normally
   fires only when an eWeLink BLE pairing COMPLETES over a real GATT link:

       eWeLink pairing state machine (0x48a84..0x49cxx, "ewelink pair ok")
         -> start-cloud handler 0x2c204   (callers 0x48ffe / 0x49b76 / 0x49ebc)
            -> ais_cloud_start 0x35744     (gate: flag *0x403e31, precond 0x24fd8)
               -> creates ais_link_task 0x35aa8
                  -> reads the AIS context *0x4044ac and dials ctx+0x5f (dispatchUrl)

   Driving the BLE provisioning MESSAGES through the dispatcher (see
   `s61_ble_provision.py`) runs the handlers and connects Wi-Fi, but never fires
   the pairing-FINISH event, so the device stays unpaired and dials nothing. We
   synthesise that event directly by force-calling the start-cloud handler
   (0x2c204) once Wi-Fi is up.

The AIS context is already fully populated at boot: device UUID, model
`SN-BK7238-S61-01`, the MAC, and a factory-default dispatch URL
`http://8.8.8.8:80` at ctx+0x5f. By default this tool repoints that URL to a local
stub on the emulator's own AP gateway (`http://192.168.1.1:80`), so the dial stays
on this host and reaches nothing outside it. Set DISPATCH to steer it elsewhere, or
DISPATCH= (empty) to leave the factory placeholder in place.

Owner's own account only: never point this at the real eWeLink cloud with anyone's
account or plug but your own. The default dials a local stub and leaves the host.

Verified 2026-10-27: the default dials our AP gateway `192.168.1.1:80`; `DISPATCH=`
leaves the factory `8.8.8.8:80`. Either way the netstack logs
`TCP connect .. -> ..:80` + `HTTP GET /dispatch/device`, retried with backoff.

  $(docs/s61-tools/emulator/bk-emu.sh python) docs/s61-tools/s61_cloud_dial.py              # local stub (default)
  DISPATCH= $(docs/s61-tools/emulator/bk-emu.sh python) docs/s61-tools/s61_cloud_dial.py    # factory placeholder
"""
import os
import sys
import threading
import time
import struct
import collections
import re
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
SECS = float(os.environ.get("SECS", "70")); FORCE_AT = float(os.environ.get("FORCE_AT", "34"))
SSID = os.environ.get("SSID", "AutoflashNet").encode(); PW = os.environ.get("PASS", "autoflash1234").encode()
DISPATCH = os.environ.get("DISPATCH", "http://192.168.1.1:80")  # default: local stub; DISPATCH= keeps factory

AFTER_MEMCMP = 0x29c3c; TIMER5 = 1 << 9; APPFW_LOOP = 0x319c4; EFGET = 0x12f438
CTXP = 0x4044ac; FLAG = 0x403e31; START_HANDLER = 0x2c204; URL_OFF = 0x5f

netlog = []; net = []
emu = build_emulator(DUMP, chip="BK7238", with_boot=True, only_uart=True, save_decrypted=False, wifi=True, log=lambda m: netlog.append(str(m)))
emu.setup()
t0 = time.time()
emu.net_sink = lambda m: (netlog.append(str(m)), net.append((round(time.time()-t0, 1), str(m))))
emu.attach_wifi_peer(role="ap", ssid=SSID.decode(), passphrase=PW.decode(), channel=1, peer_mac=b"\x02\x00\x00\x00\x00\x01")
mac, mu = emu.wifimac, emu.mu

def _rd(a, n):
    try:
        return bytes(mu.mem_read(a, n))
    except Exception:
        return b""
def _rstr(a, n=40):
    b = _rd(a, n); z = b.find(0); return b[:z if z >= 0 else n]

mu.hook_add(UC_HOOK_CODE, lambda m, a, s, u: mu.reg_write(UC_ARM_REG_R0, 0), None, AFTER_MEMCMP, AFTER_MEMCMP)

# (1) seed wifi creds: ef_get_env_blob(key, buf, size, &outlen)
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

cnt = collections.Counter()
for nm, a in (("ais_cloud_start", 0x35744), ("ais_link_task", 0x35aa8), ("link_create", 0x357f2)):
    def mk(name):
        def h(m, aa, s, u): cnt[name] += 1
        return h
    mu.hook_add(UC_HOOK_CODE, mk(nm), None, a, a)

# (2) synthesise the pairing-success trigger: force-call the start-cloud handler
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
        netlog.append("[ctx *0x4044ac=%#x dispatch-url=%r]" % (p, _rstr(p+URL_OFF, 32)))
        if DISPATCH and p:
            try:
                mu.mem_write(p+URL_OFF, DISPATCH.encode()+b"\x00"); netlog.append("[repointed dispatch url -> %s]" % DISPATCH)
            except Exception as e:
                netlog.append("[url err %r]" % e)
        try:
            mu.mem_write(FLAG, b"\x01")
        except Exception:
            pass
        return
    st["forced"] = True; netlog.append("[synthesised pairing-success trigger @%.1f]" % (time.time()-t0)); force_thumb(START_HANDLER)
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
time.sleep(0.4)
p = emu.wifi_peer; nst = getattr(p, "net", None); ip = getattr(nst, "device_ip", None)
def ips(b): return ".".join(str(x) for x in b) if isinstance(b, (bytes, bytearray)) and len(b) == 4 else str(b)
dials = [(t, s) for t, s in net if "/dispatch/device" in s and "sends" in s]
print("=== S61 CLOUD DIAL (dispatch=%s) ===" % (DISPATCH or "factory default http://8.8.8.8:80"), flush=True)
print("  device IP = %s" % ips(ip), flush=True)
print("  dial-chain: %s" % dict(cnt), flush=True)
for ln in netlog:
    if re.search(r"dispatch-url|repointed|synthesised", str(ln)):
        print("  " + str(ln), flush=True)
print("  CoolKit dispatch requests (%d):" % len(dials), flush=True)
for t, s in dials[:12]:
    print("    %5.1fs  %s" % (t, s[:120]), flush=True)
ok = ip not in (None, b"\x00\x00\x00\x00", b"\xff\xff\xff\xff") and len(dials) > 0
print("  %s: the plug joined our AP and dialled the eWeLink cloud (GET /dispatch/device)." % ("PASS" if ok else "FAIL"), flush=True)
os._exit(0)
