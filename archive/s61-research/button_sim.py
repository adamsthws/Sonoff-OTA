"""Simulate the S61's physical button (press & hold 5 s -> pairing) in the
BekenEmulator fork, driving the firmware's own key-handling stack.

Background (see ../beken-s61-emulation.md "The button press CAN be simulated"):
the button is a GPIO 0-5 capture interrupt on the BK7238 PWM/timer block at
0x802B00 (ICU IRQ 9, ISR 0x1bfd8 -> key handler 0x4203c). The key/button driver
is not initialised during the emulator's boot-to-idle (its app main-init parks on
the same async wait as the Wi-Fi app), so this tool force-runs the firmware's own
block init (0x1bc18) and key_add (0x41f64) to register the button, then "presses"
it by driving the GPIO low and latching the capture-interrupt status that the
emulator keeps in state.pwmn_stat. The firmware's real ISR then runs, dispatches
to its real key handler, which debounces and arms its real 5-second long-press
timer - all in the firmware's own code.

The long-press *action* (enter pairing mode) is consumed in the app layer, which
stays parked, so this reaches "long-press timer armed", not pairing mode. It is a
research/validation tool, not a converter. Drives the fork, not this repo's code.

  $(tools/beken/emulator/bk-emu.sh python) archive/s61-research/button_sim.py [pin]   # default pin 3
"""
import sys
import os
import threading
import time
import collections
from pathlib import Path
ROOT = Path(os.environ.get("BK_EMU_ROOT", "/opt/beken-emulator"))
sys.path.insert(0, str(ROOT))
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
PIN = int(sys.argv[1]) if len(sys.argv) > 1 else 3      # button GPIO (0-5)
GROUP, CH = PIN // 2, PIN & 1
STATBIT = 2 * GROUP + CH                                # bit in state.pwmn_stat
# Firmware addresses (this build; CPU addr == offset in the destriped image):
BLOCKINIT, KEYADD = 0x1bc18, 0x41f64                   # force-run to register the button
ISR, HANDLER, LONGPRESS = 0x1bfd8, 0x4203c, 0x42078    # ISR / key handler / arms 5 s timer
PRESS_PATH = 0x4205a
DESC = 0x403d80                                         # scratch RAM for the key descriptor

REGS = [UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_R4,
        UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7, UC_ARM_REG_R8, UC_ARM_REG_R9,
        UC_ARM_REG_R10, UC_ARM_REG_R11, UC_ARM_REG_R12, UC_ARM_REG_SP, UC_ARM_REG_LR,
        UC_ARM_REG_PC, UC_ARM_REG_CPSR]

emu = build_emulator(DUMP, chip="BK7238", with_boot=True, only_uart=True,
                     save_decrypted=False, wifi=True, log=lambda m: None)
emu.setup()
emu.attach_wifi_peer(role="auto", peer_mac=b"\x02\x00\x00\x00\x00\x01")
mac, mu = emu.wifimac, emu.mu
cnt = collections.Counter()
for name, addr in (("isr", ISR), ("handler", HANDLER),
                   ("press_path", PRESS_PATH), ("longpress5s", LONGPRESS)):
    mu.hook_add(UC_HOOK_CODE, (lambda n: (lambda m, a, s, u: cnt.__setitem__(n, cnt[n] + 1)))(name),
                None, addr, addr)

st = {"phase": "boot", "idx": 0}
# Force-call trap: LR is set to 0x5 (Thumb), so the called function returns to the
# 0x4 trap, where we restore the saved context. PC is set odd (|1) and _exc_epoch
# bumped so the chunked run loop re-reads PC in Thumb (the "Thumb-bit" fix).
def h_trap(mu, a, s, u):
    if st["phase"] == "calling":
        for r, v in zip(REGS, st["saved"]):
            mu.reg_write(r, v)
        emu._exc_epoch += 1
        st["phase"] = "between"
        st["bt"] = time.time()
mu.hook_add(UC_HOOK_CODE, h_trap, None, 0x4, 0x4)

def do_call(pc, argfn):
    st["saved"] = [mu.reg_read(x) for x in REGS]
    cpsr = mu.reg_read(UC_ARM_REG_CPSR)
    argfn()
    mu.reg_write(UC_ARM_REG_LR, 0x5)
    mu.reg_write(UC_ARM_REG_CPSR, (cpsr & ~0x3f) | 0x1f | 0x20)
    mu.reg_write(UC_ARM_REG_PC, pc | 1)
    emu._exc_epoch += 1
    st["phase"] = "calling"

def keyadd_args():
    # descriptor: button pin at offsets +2,+3,+4,+5 (key_add reads these)
    mu.mem_write(DESC, bytes([0, 0, PIN, PIN, PIN, PIN, 0, 0, 0, 0, 0, 0]))
    mu.reg_write(UC_ARM_REG_R0, DESC)

QUEUE = [(BLOCKINIT, lambda: None), (KEYADD, keyadd_args)]

def keep_input_low():
    # 0x3eb30 decodes the per-pin mode (0x1a198) from gpio_cfg bits 2/3; the
    # capture config otherwise routes the read off the input path. Force bits 2/3
    # (input-readable) and clear bit 0 so the firmware samples the driven low.
    emu.state.gpio_cfg[PIN] = (emu.state.gpio_cfg.get(PIN, 0) | 0x0c) & ~0x1

import types  # noqa: E402
_tick = mac.tick
def hooked_tick(self, insns):
    r = _tick(insns)
    now = time.time()
    if st["phase"] == "boot" and now - st.get("t0", 0) > 9:
        st["phase"], st["bt"] = "between", now
    if st["phase"] == "between" and now - st.get("bt", now) > 0.3:
        if st["idx"] < len(QUEUE):
            pc, fn = QUEUE[st["idx"]]
            st["idx"] += 1
            do_call(pc, fn)
        elif "pressed" not in st:
            st["pressed"] = now
            emu.set_gpio_input(PIN, 0)                  # press: drive low
            keep_input_low()
            emu.state.pwmn_seen = True
            emu.state.pwmn_stat |= (1 << STATBIT)       # latch capture interrupt
            st["phase"] = "held"
    if st["phase"] == "held":
        keep_input_low()
        # a held button re-triggers capture edges; re-latch a few so the handler's
        # debounce counter passes its >2 threshold and arms the long-press timer
        if st.get("relatch", 0) < 6 and now - st.get("ll", 0) > 0.2:
            emu.state.pwmn_seen = True
            emu.state.pwmn_stat |= (1 << STATBIT)
            st["relatch"] = st.get("relatch", 0) + 1
            st["ll"] = now
    return r
mac.tick = types.MethodType(hooked_tick, mac)

w = threading.Thread(target=emu.run)
w.start()
st["t0"] = time.time()
t0 = time.time()
while w.is_alive() and time.time() - t0 < 14:
    time.sleep(0.1)
time.sleep(0.3)
print("[BTN] pin=%d group=%d ch=%d (pwmn_stat bit %d)" % (PIN, GROUP, CH, STATBIT))
print("[BTN] firmware chain: ISR(0x1bfd8)=%d -> handler(0x4203c)=%d -> "
      "press_path=%d -> 5s-longpress-timer-armed=%d"
      % (cnt["isr"], cnt["handler"], cnt["press_path"], cnt["longpress5s"]))
print("[BTN] driver flag 0x404c23=%d  pin_global 0x404c25=%d  key-id table 0x404bb4=%s"
      % (mu.mem_read(0x404c23, 1)[0], mu.mem_read(0x404c25, 1)[0], mu.mem_read(0x404bb4, 3).hex()))
ok = cnt["longpress5s"] > 0
print("[BTN] %s: the simulated press reached the firmware's real handler and %s "
      "the 5-second long-press timer." % ("PASS" if ok else "INCOMPLETE",
                                          "armed" if ok else "did not arm"))
emu.stop()
w.join(8)
