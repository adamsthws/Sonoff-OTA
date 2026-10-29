"""Test: force the app-start identity-integrity memcmp (0x29c38) to report
'equal' (the real-HW outcome for a device whose flash UID matches its factory
cert), and observe whether the app framework comes up and pairing becomes
reachable."""
import sys
import os
import threading
import time
import collections
import re
from pathlib import Path
ROOT = Path(os.environ.get("BK_EMU_ROOT", str(Path.home()/"Git"/"BekenEmulator"))); sys.path.insert(0, str(ROOT))
from src.main import build_emulator  # noqa: E402
from unicorn import UC_HOOK_CODE  # noqa: E402
from unicorn.arm_const import (  # noqa: E402
    UC_ARM_REG_R0)
DUMP = os.environ.get("S61_DUMP", str(ROOT/"firmwares/Sonoff_S61s_EUPlug_WBBK_01P_V1.3.bin"))
SECS = float(os.environ.get("SECS","25"))
AFTER_MEMCMP = 0x29c3c
MARK = {"verified_29c82":0x29c82,"appstart_24430":0x24430,"n29b54_ret_24474":0x24474,
        "appfwcreate_31b54":0x31b54,"appfw_taskentry_319a8":0x319a8,"appfw_loop_49b40":0x49b40,
        "provchk_48cc4":0x48cc4,"key_add_41f64":0x41f64,"pair_disp_21eb4":0x21eb4,
        "softap_21a80":0x21a80,"airkiss_21e50":0x21e50,"pairingmode_492ae":0x492ae}
netlog=[]
emu = build_emulator(DUMP, chip="BK7238", with_boot=True, only_uart=True, save_decrypted=False, wifi=True, log=lambda m: netlog.append(str(m)))
emu.setup(); emu.attach_wifi_peer(role="auto", peer_mac=b"\x02\x00\x00\x00\x00\x01")
mu=emu.mu; cnt=collections.Counter(); first={}
t0=time.time()
def mk(n):
    def h(m,a,s,u):
        cnt[n]+=1
        if n not in first:
            first[n]=round(time.time()-t0,1)
    return h
for n,a in MARK.items():
    mu.hook_add(UC_HOOK_CODE, mk(n), None, a, a)
def h_force(m,a,s,u):
    cnt["memcmp_site"]+=1
    mu.reg_write(UC_ARM_REG_R0, 0)   # report 'equal'
mu.hook_add(UC_HOOK_CODE,h_force,None,AFTER_MEMCMP,AFTER_MEMCMP)
w=threading.Thread(target=emu.run,daemon=True); w.start()
time.sleep(SECS)
try:
    emu.stop()
except Exception:
    pass
time.sleep(0.4)
print("=== FORCE INTEGRITY-MATCH EXPERIMENT (%.0fs) ==="%SECS,flush=True)
print("  marker counts:",{k:v for k,v in cnt.items() if v>0},flush=True)
print("  first-seen (s):",first,flush=True)
print("  UART tail (new app output, filtered):",flush=True)
pat=re.compile(r"pair|mode|commission|ble|soft.?ap|airkiss|monitor|adv|wifi|connect|key|EZ|smart|appfw|ais|THD|readl|start",re.I)
for ln in netlog[-60:]:
    if pat.search(str(ln)):
        print("    "+str(ln)[:130],flush=True)
os._exit(0)
