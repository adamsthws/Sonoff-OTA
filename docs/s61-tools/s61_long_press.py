"""Drive the S61 button long-press in the emulator, end to end, through the
firmware's own key stack.

Two emulator-side interventions, both explained in ../beken-s61-emulation.md:

  1. Integrity-gate bypass. The app layer hangs in a flash-UID factory self-check
     (SHA256(hex(flash_uid)+fields) vs a stored cert; the emulator's synthetic
     UID can't match). We force the compare at 0x29c3c to "equal" (the real-HW
     outcome), so the app framework boots and the button registers itself.

  2. Timer-5 wake. The key-driver's periodic runs on the `bk_timer` hardware
     timer = Timer 5, but the emulator only raises Timer 3's interrupt (the RTOS
     tick). We raise Timer 5's status bit (timer3_5_ctl bit 9) each slow tick so
     the key-driver timer thread (0x3eba0) wakes, runs its scan (0x421e0), and
     times the hold. (Doing this in the emulator's _pacing for every BK7238
     firmware regresses 3 others that use Timers 4/5 differently -- a faithful
     fix needs per-timer-period modelling -- so we scope it to this tool.)

With both, a press-and-hold of the button pins drives the firmware's real
ISR -> handler -> periodic scan -> long-press action 0x42250. (That action is
the relay/LED path via bk_gpio_output; the eWeLink pairing long-press is a
further app-layer step -- see the doc.)

  $(tools/beken/emulator/bk-emu.sh python) docs/s61-tools/s61_long_press.py
"""
import sys
import os
import threading
import time
import collections
import types
from pathlib import Path
ROOT = Path(os.environ.get("BK_EMU_ROOT", "/opt/beken-emulator")); sys.path.insert(0, str(ROOT))
from src.main import build_emulator  # noqa: E402
from unicorn import UC_HOOK_CODE  # noqa: E402
from unicorn.arm_const import (  # noqa: E402
    UC_ARM_REG_R0)

DUMP = os.environ.get("S61_DUMP", str(ROOT/"firmwares/Sonoff_S61s_EUPlug_WBBK_01P_V1.3.bin"))
SECS = float(os.environ.get("SECS", "30"))
HOLD_AT = float(os.environ.get("HOLD_AT", "8"))
PINS = [int(x) for x in os.environ.get("PINS", "0 2").split()]
AFTER_MEMCMP = 0x29c3c          # integrity-gate compare; force r0=0 ("equal")
TIMER5_BIT = 1 << 9             # timer3_5_ctl int-status for Timer 5 (bk_timer)
KEYADD, SCAN, LONGACT = 0x41f64, 0x421e0, 0x42250

emu = build_emulator(DUMP, chip="BK7238", with_boot=True, only_uart=True,
                     save_decrypted=False, wifi=True, log=lambda m: None)
emu.setup(); emu.attach_wifi_peer(role="auto", peer_mac=b"\x02\x00\x00\x00\x00\x01")
mac, mu = emu.wifimac, emu.mu
cnt = collections.Counter(); first = {}; t0 = time.time()

def mk(n):
    def h(m, a, s, u):
        cnt[n] += 1
        if n not in first:
            first[n] = round(time.time()-t0, 1)
    return h
for n, a in (("key_add", KEYADD), ("scan", SCAN), ("longact", LONGACT)):
    mu.hook_add(UC_HOOK_CODE, mk(n), None, a, a)
def h_force(m, a, s, u): mu.reg_write(UC_ARM_REG_R0, 0)
mu.hook_add(UC_HOOK_CODE, h_force, None, AFTER_MEMCMP, AFTER_MEMCMP)

st = {"phase": "boot"}
def inp(p): emu.state.gpio_cfg[p] = (emu.state.gpio_cfg.get(p, 0) | 0x0c) & ~0x1
_tick = mac.tick
def hooked_tick(self, insns):
    r = _tick(insns); now = time.time() - t0
    if cnt["key_add"] > 0:                 # app + key subsystem are up
        emu.state.timer3_5_ctl |= TIMER5_BIT   # the Timer-5 wake
    if st["phase"] == "boot" and cnt["key_add"] > 0 and now >= HOLD_AT:
        for p in PINS:
            emu.set_gpio_input(p, 0); inp(p)
        st["phase"] = "bounce"; st["edges"] = 0; st["le"] = 0
        print("[PRESS+HOLD pins %s @%.1fs]" % (PINS, now), flush=True)
    if st["phase"] == "bounce":
        for p in PINS:
            inp(p)
        if st["edges"] < 6 and now - st["le"] > 0.03:
            for p in PINS:
                emu.state.pwmn_seen = True; emu.state.pwmn_stat |= (1 << p)
            st["edges"] += 1; st["le"] = now
        elif st["edges"] >= 6:
            st["phase"] = "hold"
    if st["phase"] == "hold":
        for p in PINS:
            inp(p); emu.set_gpio_input(p, 0)
    return r
mac.tick = types.MethodType(hooked_tick, mac)

w = threading.Thread(target=emu.run, daemon=True); w.start()
time.sleep(SECS)
try:
    emu.stop()
except Exception:
    pass
time.sleep(0.4)
print("=== S61 LONG-PRESS (integrity bypass + Timer-5 wake) ===", flush=True)
print("  key_add=%d  scan(0x421e0)=%d  longact(0x42250)=%d"
      % (cnt["key_add"], cnt["scan"], cnt["longact"]), flush=True)
print("  first-seen (s):", first, flush=True)
ok = cnt["scan"] > 0 and cnt["longact"] > 0
print("  %s: the key-driver timer thread woke, the scan ran, and the long-press %s fired."
      % ("PASS" if ok else "INCOMPLETE", "action" if ok else "did NOT"), flush=True)
os._exit(0)
