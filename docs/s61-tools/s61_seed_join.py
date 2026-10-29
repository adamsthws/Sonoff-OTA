"""Seed Wi-Fi credentials and join our emulated AP — the faithful way.

A stock S61 boots unprovisioned and waits. This makes it a *provisioned* plug by
answering the firmware's own credential read: it hooks `ef_get_env_blob`
(EasyFlash V4, 0x12f438) and, for the keys `wifi-ssid` / `wifi-pass`, returns the
SSID/passphrase we choose (exactly as a real provisioning store would have left
them). Nothing else is forced — the firmware then runs its normal provisioned
boot path.

The fork's virtual peer is attached as a WPA2-PSK access point with the same
SSID/passphrase, so the plug performs the real sequence end to end:

    read creds -> station mode -> authenticate -> associate -> WPA2 four-way
    handshake -> IPv4 DHCP -> gets an address on our network

Verified 2026-10-27: the plug associates (`encrypted` link), DHCP hands it
192.168.1.100, and it then begins its post-join activity (Matter `_matterc` mDNS;
the eWeLink cloud dispatch needs the eWeLink identity too — see the main doc).

This is the integrity-gate bypass (0x29c3c) plus the credential injection; no
other intervention. Requires the BekenEmulator fork.

  BK_EMU_ROOT=../BekenEmulator python3 s61_seed_join.py
  SSID=MyNet PASS=mypassword CH=6 SECS=40 python3 s61_seed_join.py
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

ROOT = Path(os.environ.get("BK_EMU_ROOT", str(Path.home()/"Git"/"BekenEmulator"))); sys.path.insert(0, str(ROOT))
from src.main import build_emulator  # noqa: E402
from unicorn import UC_HOOK_CODE  # noqa: E402
from unicorn.arm_const import (  # noqa: E402
    UC_ARM_REG_LR,
    UC_ARM_REG_PC,
    UC_ARM_REG_R0,
    UC_ARM_REG_R1,
    UC_ARM_REG_R3)

DUMP = os.environ.get("S61_DUMP", str(ROOT/"firmwares/Sonoff_S61s_EUPlug_WBBK_01P_V1.3.bin"))
SECS = float(os.environ.get("SECS", "40"))
SSID = os.environ.get("SSID", "AutoflashNet").encode()
PASS = os.environ.get("PASS", "autoflash1234").encode()
CH = int(os.environ.get("CH", "1"))

AFTER_MEMCMP = 0x29c3c          # integrity-gate compare; force "equal"
TIMER5_BIT = 1 << 9             # bk_timer = Timer 5, which the emulator doesn't raise
EF_GET = 0x12f438               # ef_get_env_blob(key, value_buf, buf_len, saved_len_ptr)
VALS = {b"wifi-ssid": SSID, b"wifi-pass": PASS}

netlog = []
emu = build_emulator(DUMP, chip="BK7238", with_boot=True, only_uart=True,
                     save_decrypted=False, wifi=True, log=lambda m: netlog.append(str(m)))
emu.setup()
emu.net_sink = lambda m: netlog.append(str(m))
emu.attach_wifi_peer(role="ap", ssid=SSID.decode(), passphrase=PASS.decode(),
                     channel=CH, peer_mac=b"\x02\x00\x00\x00\x00\x01")
mac, mu = emu.wifimac, emu.mu
inj = collections.Counter()

def _read_cstr(a, n=24):
    try:
        b = bytes(mu.mem_read(a, n)); z = b.find(0)
        return b[:z if z >= 0 else n]
    except Exception:
        return b""

def h_efget(m, a, s, u):
    key = _read_cstr(mu.reg_read(UC_ARM_REG_R0), 16)
    if key in VALS:
        val = VALS[key]
        r1, r3 = mu.reg_read(UC_ARM_REG_R1), mu.reg_read(UC_ARM_REG_R3)
        try:
            if r1:
                mu.mem_write(r1, val + b"\x00")          # value_buf
            if r3:
                mu.mem_write(r3, struct.pack("<I", len(val)))  # saved length
        except Exception:
            pass
        mu.reg_write(UC_ARM_REG_R0, len(val))               # return bytes read
        mu.reg_write(UC_ARM_REG_PC, mu.reg_read(UC_ARM_REG_LR))  # return, skip body
        inj[key.decode()] += 1
mu.hook_add(UC_HOOK_CODE, h_efget, None, EF_GET, EF_GET)

def h_force(m, a, s, u): mu.reg_write(UC_ARM_REG_R0, 0)
mu.hook_add(UC_HOOK_CODE, h_force, None, AFTER_MEMCMP, AFTER_MEMCMP)

_tick = mac.tick
def hooked_tick(self, insns):
    r = _tick(insns); emu.state.timer3_5_ctl |= TIMER5_BIT; return r
mac.tick = types.MethodType(hooked_tick, mac)

w = threading.Thread(target=emu.run, daemon=True); w.start()
time.sleep(SECS)
try:
    emu.stop()
except Exception:
    pass
time.sleep(0.5)

p = emu.wifi_peer; net = getattr(p, "net", None)
ip = getattr(net, "device_ip", None)
got = ip not in (None, b"\xff\xff\xff\xff", b"\x00\x00\x00\x00")
def ips(b): return ".".join(str(x) for x in b) if isinstance(b, (bytes, bytearray)) and len(b) == 4 else str(b)
print("=== S61 SEED + JOIN ===", flush=True)
print("  injected creds: %s (ssid=%s)" % (dict(inj), SSID.decode()), flush=True)
print("  peer role=%s  stations=%s" % (getattr(p, "active_role", None),
      {k.hex(): v for k, v in getattr(p, "stations", {}).items()}), flush=True)
print("  device IP = %s  leases=%s" % (ips(ip),
      {k.hex(): ips(v) for k, v in (getattr(net, "leases", {}) or {}).items()}), flush=True)
ok = got and any(st.get("encrypted") for st in getattr(p, "stations", {}).values())
print("  %s: the plug associated (WPA2) and %s an IP on our network."
      % ("PASS" if ok else "INCOMPLETE", "got" if got else "did NOT get"), flush=True)
pat = re.compile(r"DHCP:|four-way|associated|authenticated|got 192|device IP", re.I)
for ln in netlog:
    if pat.search(str(ln)):
        print("    " + str(ln)[:140], flush=True)
os._exit(0)
