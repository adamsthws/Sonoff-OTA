"""AirKiss replay engine: read captured 802.11 frames from a pcap and inject them
into the emulated S61's Wi-Fi MAC (monitor mode), paced to the descriptor recycle
rate so none are lost. The firmware's own AirKiss decoder consumes them. After
decode, an optional AP-role peer (known SSID/pass) lets the device associate.

Stdlib-only pcap/radiotap parsing. DLT 127 (radiotap) and 105 (raw 802.11).

Usage:
  airkiss_replay.py <capture.pcap> [--ssid S --pass P] [--loops N] [--selftest]
With --selftest it generates a synthetic capture and replays it to prove the
pipeline (no real decode, just delivery/throughput).
"""
import sys
import os
import struct
import threading
import time
import tempfile
from pathlib import Path
ROOT=Path(os.environ.get("BK_EMU_ROOT", "/opt/beken-emulator")); sys.path.insert(0,str(ROOT))
from src.main import build_emulator  # noqa: E402
DUMP=os.environ.get("S61_DUMP", str(ROOT/"firmwares/Sonoff_S61s_EUPlug_WBBK_01P_V1.3.bin"))

# ---------- pcap parsing (stdlib) ----------
def read_pcap(path):
    data=Path(path).read_bytes()
    if len(data)<24:
        raise ValueError("pcap too short")
    magic=struct.unpack("<I",data[:4])[0]
    le = magic in (0xa1b2c3d4,0xa1b23c4d)
    end="<" if le else ">"
    ver_maj,=struct.unpack(end+"H",data[4:6])
    network,=struct.unpack(end+"I",data[20:24])
    frames=[]; off=24
    nano = magic in (0xa1b23c4d,0x4d3cb2a1)
    while off+16<=len(data):
        ts_s,ts_f,incl,orig=struct.unpack(end+"IIII",data[off:off+16]); off+=16
        pkt=data[off:off+incl]; off+=incl
        ts=ts_s + (ts_f/1e9 if nano else ts_f/1e6)
        frames.append((ts, strip_link(network, pkt)))
    return network, frames

def strip_link(network, pkt):
    if network==127:   # radiotap
        if len(pkt)<4:
            return b""
        it_len=struct.unpack("<H",pkt[2:4])[0]
        return pkt[it_len:]
    return pkt         # 105 raw 802.11, or already-MPDU

def write_pcap(path, frames, network=127):
    out=bytearray(struct.pack("<IHHIIII",0xa1b2c3d4,2,4,0,0,65535,network))
    t0=time.time()
    for i,fr in enumerate(frames):
        ts=t0+i*0.002
        body=fr
        if network==127:  # prepend a minimal 8-byte radiotap header
            body=struct.pack("<BBHI",0,0,8,0)+fr
        out+=struct.pack("<IIII",int(ts),int((ts%1)*1e6),len(body),len(body))+body
    Path(path).write_bytes(bytes(out))

def synth_airkiss_like(n=120):
    # data frames of varied lengths (AirKiss encodes data in frame LENGTH)
    frames=[]
    for i in range(n):
        fc=bytes([0x08,0x01]); dur=b"\x00\x00"
        a1=bytes([0x01,0x00,0x5e,0x7f,(i>>8)&0xff,i&0xff])  # multicast-ish
        a2=bytes([0x02,0,0,0,0,1]); a3=b"\xff\xff\xff\xff\xff\xff"
        seq=bytes([(i<<4)&0xff,(i>>4)&0xff])
        pad=bytes((i+j)&0xff for j in range(26+(i%80)))
        frames.append(fc+dur+a1+a2+a3+seq+pad)
    return frames

# ---------- replay ----------
def main():
    args=sys.argv[1:]
    selftest="--selftest" in args
    ssid=None; pw=None; loops=1
    if "--ssid" in args:
        ssid=args[args.index("--ssid")+1]
    if "--pass" in args:
        pw=args[args.index("--pass")+1]
    if "--loops" in args:
        loops=int(args[args.index("--loops")+1])
    if selftest:
        tmp=tempfile.mktemp(suffix=".pcap")
        write_pcap(tmp, synth_airkiss_like(120), network=127)
        pcap=tmp
    else:
        pcap=[a for a in args if not a.startswith("--") and a.endswith(".pcap")][0]
    network, frames=read_pcap(pcap)
    frames=[f for (_ts,f) in frames if f]
    print("[RP] pcap=%s dlt=%d frames=%d (ssid=%s peer=%s)"%(os.path.basename(pcap),network,len(frames),ssid,"ap" if ssid else "auto"))

    netlog=[]
    emu=build_emulator(DUMP,chip="BK7238",with_boot=True,only_uart=True,save_decrypted=False,
                       wifi=True,log=lambda m:netlog.append(str(m)))
    emu.setup()
    if ssid:
        emu.attach_wifi_peer(role="ap", ssid=ssid, passphrase=pw, peer_mac=b"\x02\x00\x00\x00\x00\x01")
    else:
        emu.attach_wifi_peer(role="auto", peer_mac=b"\x02\x00\x00\x00\x00\x01")
    emu.wifimac.monitor_rx_deliver=True
    w=threading.Thread(target=emu.run); w.start()
    # Wait for monitor mode AND for the firmware's monitor-RX handler object to
    # exist. The firmware sets the monitor RX *filter* very early (~0.15 s) but
    # only allocates the object its RX FIQ consumes frames through a little later
    # (~0.5 s). A frame delivered in that gap drives the RX FIQ against a NULL
    # object and the handler hangs in a bogus memcpy (len read as the unused
    # vector-table word 0xdeadbeef). On real hardware the phone never broadcasts
    # that early, so gating injection on the object being ready reproduces the
    # real ordering. The pointer lives at S61_RX_OBJ_PTR (this firmware build:
    # 0x403c10; written by the allocator at 0x17700 via 0x165ec). If it never
    # appears we still start, so the tool degrades to the old behaviour.
    RX_OBJ_PTR = int(os.environ.get("S61_RX_OBJ_PTR", "0x403c10"), 0)
    def _rx_obj_ready():
        try:
            return struct.unpack("<I", emu.mu.mem_read(RX_OBJ_PTR, 4))[0] != 0
        except Exception:
            return False
    for _ in range(400):
        if not w.is_alive():
            break
        if getattr(emu.wifimac,"monitor",False) and _rx_obj_ready():
            break
        time.sleep(0.05)
    print("[RP] monitor mode: %s  rx-handler object ready: %s"%(
        getattr(emu.wifimac,"monitor",False), _rx_obj_ready()))

    def rx(): return emu.wifimac.stats.get("rx",0)
    # Inject IN the worker thread via a tick hook (avoids the cross-thread
    # rx_queue race). One frame per tick, only when the rx_queue is drained, so
    # pacing matches the firmware's descriptor recycle rate automatically.
    from collections import deque
    import types
    q=deque()
    for _ in range(loops):
        q.extend(frames)
    counters={"sent":0}
    mac=emu.wifimac
    _tick=mac.tick
    INFLIGHT=16            # keep the pipe partly full; don't require full drain
    def hooked_tick(self, insns):
        r=_tick(insns)
        try:
            if getattr(self,'monitor',False) and getattr(self,'monitor_rx_deliver',False):
                if self.rx_queue:
                    self._rx_flush(insns)          # deliver as descriptors free
                if q and len(self.rx_queue) < INFLIGHT:
                    fr=q.popleft(); self.inject(fr, insns); counters['sent']+=1
                    q.append(fr)                    # loop the capture (AirKiss repeats)
        except Exception:
            pass
        return r
    mac.tick=types.MethodType(hooked_tick, mac)
    t0=time.time()
    DURATION=float(os.environ.get('RP_SECS','20'))
    while w.is_alive() and time.time()-t0<DURATION:
        time.sleep(0.1)
    dur=time.time()-t0
    time.sleep(1.0)
    sent=counters["sent"]
    delivered=rx()
    print("[RP] sent=%d delivered(rx)=%d in %.1fs (%.0f frames/s) rate=%.0f%%"%(
        sent,delivered,dur, sent/dur if dur else 0, 100.0*delivered/max(1,sent)))
    # did the device react (leave monitor / scan / associate)?
    import re
    for ln in netlog:
        if re.search(r"acts as|associat|probe|found bss|role|got ip|dhcp|disconnect",ln,re.I):
            print("[RP] net:", re.sub(r"'[^']*'","'<SSID>'",ln))
    emu.stop(); w.join(8)

main()
