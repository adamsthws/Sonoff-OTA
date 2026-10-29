"""Drive the S61 into pairing mode in the emulator, and watch the emulated radio
follow -- soft-AP beacons or station-mode association, or the AirKiss sniffer.

Background (full story in ../beken-s61-emulation.md): with the integrity gate
bypassed (s61_integrity_gate.py) the AIS app framework boots and its state
machine (ais_appfw_task) cycles, but nothing sets a pairing method and posts the
start event, so the firmware sits idle. This tool sets a method and calls the
firmware's own pairing-method dispatcher `0x21eb4` from inside the appfw task's
context, so the RTOS/RivieraWaves messaging runs in a real task context rather
than a synthetic one. The dispatcher reads config[0]:

    0  coexist  -> soft-AP wrapper 0x21a80  + AirKiss sniffer RW-task
    1  station  -> 0x21d18 (sets mode via 0x20e00, probes/associates)
    2           -> 0x21d90

What you see when it works (verified 2026-10-27):

  * method 0: the MAC leaves monitor mode ("RX filter ... back to normal
    reception") and TRANSMITS BEACONS -- the virtual peer reports "device acts
    as an access point (it sends beacons)" and takes the station role, finding
    the device's BSS. Soft-AP pairing is up. The beaconed SSID is the firmware's
    own default ("JIEWU", baked in the image), cipher-protected; to complete an
    association attach the peer with role/ssid/passphrase that match.

  * method 1: the MAC enters station mode and probes/associates; the peer takes
    the AP role and registers the device as a station.

  * airkiss: the sniffer-start 0x223b0 posts its RW-task sends and the RX-FIQ
    handler object (0x403c10) goes live, so injected frames are accepted. The
    device is already in monitor mode from the RF init. The smartconfig decoder
    subscription (0x403b20) is set once the coexist path runs to completion; to
    feed and decode captured AirKiss frames, pair this with monitor_rx_deliver
    and s61_airkiss_replay.py.

Two emulator-side interventions are shared with s61_long_press.py: the
integrity-gate bypass (force the compare at 0x29c3c to "equal") and raising
Timer 5's interrupt status each slow tick (the key/app periodics run on
bk_timer = Timer 5, which the emulator does not raise on its own).

  $(docs/s61-tools/emulator/bk-emu.sh python) docs/s61-tools/s61_pair_drive.py 0        # soft-AP (coexist)
  $(docs/s61-tools/emulator/bk-emu.sh python) docs/s61-tools/s61_pair_drive.py 1        # station
  $(docs/s61-tools/emulator/bk-emu.sh python) docs/s61-tools/s61_pair_drive.py airkiss  # sniffer-start

Env: SECS (run seconds, 18), FIRE_AT (when to drive, 8), SSID/PASS (peer AP to
join for method 1 / credentials written into the config buffer).
"""
import sys
import os
import threading
import time
import struct
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
SECS = float(os.environ.get("SECS", "18"))
FIRE_AT = float(os.environ.get("FIRE_AT", "8"))
SSID = os.environ.get("SSID", "S61PAIR").encode()[:31]
PASS = os.environ.get("PASS", "test1234").encode()[:63]

arg = (sys.argv[1] if len(sys.argv) > 1 else "0").lower()
AIRKISS = arg in ("airkiss", "sniffer", "ak")
METHOD = 0 if AIRKISS else int(arg)

AFTER_MEMCMP = 0x29c3c           # integrity-gate compare; force r0=0 ("equal")
TIMER5_BIT = 1 << 9              # timer3_5_ctl int-status for Timer 5 (bk_timer)
DISP, SNIFF = 0x21eb4, 0x223b0   # pairing dispatcher / AirKiss sniffer-start
APPFW_LOOP = 0x319c4             # ais_appfw_task loop body (its own context)
CFG = 0x403d80                   # scratch RAM for the config we build
SNIFF_CTX, RX_OBJ = 0x403b20, 0x403c10

t0 = time.time(); tl = []
def log(m):
    s = str(m)
    if re.search(r"WIFI|beacon|monitor|BSS|role|cipher|station|RX filter|associat|auth|probe|DHCP", s, re.I):
        tl.append((round(time.time()-t0, 2), s[:150]))

emu = build_emulator(DUMP, chip="BK7238", with_boot=True, only_uart=True,
                     save_decrypted=False, wifi=True, log=log)
emu.setup(); emu.attach_wifi_peer(role="auto", peer_mac=b"\x02\x00\x00\x00\x00\x01")
mac, mu = emu.wifimac, emu.mu

def h_force(m, a, s, u): mu.reg_write(UC_ARM_REG_R0, 0)
mu.hook_add(UC_HOOK_CODE, h_force, None, AFTER_MEMCMP, AFTER_MEMCMP)

# Force-call harness: call a firmware function in the current (appfw) context by
# setting LR to a trap address (0x4) and bumping the exception epoch; on return
# the trap restores the saved register file so the task resumes untouched.
REGS = [UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_R4,
        UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7, UC_ARM_REG_R8, UC_ARM_REG_R9,
        UC_ARM_REG_R10, UC_ARM_REG_R11, UC_ARM_REG_R12, UC_ARM_REG_SP, UC_ARM_REG_LR,
        UC_ARM_REG_PC, UC_ARM_REG_CPSR]
st = {"fired": False, "armed": False}
def h_trap(m, a, s, u):
    if st.get("calling"):
        for r, v in zip(REGS, st["saved"]):
            mu.reg_write(r, v)
        emu._exc_epoch += 1; st["calling"] = False
        tl.append((round(time.time()-t0, 2), "[pairing call returned]"))
mu.hook_add(UC_HOOK_CODE, h_trap, None, 0x4, 0x4)
def do_call(pc, a0):
    st["saved"] = [mu.reg_read(x) for x in REGS]
    cpsr = mu.reg_read(UC_ARM_REG_CPSR)
    mu.reg_write(UC_ARM_REG_R0, a0); mu.reg_write(UC_ARM_REG_LR, 0x5)
    mu.reg_write(UC_ARM_REG_CPSR, (cpsr & ~0x3f) | 0x1f | 0x20)
    mu.reg_write(UC_ARM_REG_PC, pc | 1); emu._exc_epoch += 1; st["calling"] = True

def h_appfw(m, a, s, u):
    if st["armed"] and not st["fired"] and not st.get("calling"):
        st["fired"] = True
        if AIRKISS:
            tl.append((round(time.time()-t0, 2), "[FIRE AirKiss sniffer-start 0x223b0]"))
            do_call(SNIFF, 0)
        else:
            buf = bytearray(0x104); buf[0] = METHOD
            buf[1:1+len(SSID)] = SSID
            buf[0x22:0x22+len(PASS)] = PASS
            mu.mem_write(CFG, bytes(buf))
            tl.append((round(time.time()-t0, 2), "[FIRE dispatcher 0x21eb4 method=%d]" % METHOD))
            do_call(DISP, CFG)
mu.hook_add(UC_HOOK_CODE, h_appfw, None, APPFW_LOOP, APPFW_LOOP)

_tick = mac.tick
def hooked_tick(self, insns):
    r = _tick(insns); emu.state.timer3_5_ctl |= TIMER5_BIT
    if time.time()-t0 >= FIRE_AT:
        st["armed"] = True
    return r
mac.tick = types.MethodType(hooked_tick, mac)

w = threading.Thread(target=emu.run, daemon=True); w.start()
time.sleep(SECS)
def rd(a):
    try:
        return struct.unpack("<I", mu.mem_read(a, 4))[0]
    except Exception:
        return None
snf, rxo = rd(SNIFF_CTX), rd(RX_OBJ)
try:
    emu.stop()
except Exception:
    pass
time.sleep(0.3)

p = emu.wifi_peer
role = getattr(p, "active_role", None)
bss = (p.bss or {}).get("ssid") if getattr(p, "bss", None) else None
stations = list(getattr(p, "stations", {}).keys())
print("=== S61 PAIR DRIVE (%s) ===" % ("AirKiss sniffer" if AIRKISS else "method %d" % METHOD), flush=True)
print("  mode timeline:", flush=True)
for t, s in tl:
    print("    %6.2f  %s" % (t, s), flush=True)
print("  --- final ---", flush=True)
print("  wifimac.monitor=%s  rx-obj(0x403c10)=%s  sniffer-ctx(0x403b20)=%s"
      % (getattr(mac, "monitor", None), hex(rxo or 0), hex(snf or 0)), flush=True)
print("  peer.active_role=%s  bss=%s  stations=%s" % (role, bss, stations), flush=True)
if AIRKISS:
    ok = (rxo or 0) != 0
    print("  %s: sniffer-start ran; RX-FIQ handler object %s (frame injection %s). "
          "Decoder subscription 0x403b20=%s." % ("PASS" if ok else "INCOMPLETE",
          "live" if ok else "absent", "gated-ready" if ok else "not ready", hex(snf or 0)), flush=True)
elif METHOD == 0:
    ok = role == "sta"
    print("  %s: the device %s soft-AP mode (beaconing); the peer took the station role."
          % ("PASS" if ok else "INCOMPLETE", "entered" if ok else "did NOT enter"), flush=True)
elif METHOD == 1:
    ok = role == "ap" or bool(stations)
    print("  %s: the device %s station mode (probing/associating); the peer took the AP role."
          % ("PASS" if ok else "INCOMPLETE", "entered" if ok else "did NOT enter"), flush=True)
os._exit(0)
