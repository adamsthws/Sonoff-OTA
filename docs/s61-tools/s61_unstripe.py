"""Un-stripe a raw Beken flash dump into a logical image, and decode its RBL
headers.

Beken stores flash CRC-striped: 32 data bytes then a 2-byte CRC, repeating. The
logical image (what the CPU sees, so that a CPU address equals a file offset) is
the dump with every block's 2 CRC bytes removed. Produce it once, then point the
disassembler / xref / decompiler tools at the logical image.

Uses the emulator fork's CRC routine (so it always matches the model); set
BK_EMU_ROOT if the fork is not where docs/s61-tools/emulator/bk-emu.sh puts it.

Usage:
  s61_unstripe.py [RAW_DUMP] [OUT_LOGICAL]
    RAW_DUMP      raw flash dump (default: $S61_DUMP, else the fork's S61 image)
    OUT_LOGICAL   output path (default: ./logical.bin)
"""
import os
import sys
import struct
import time
from pathlib import Path

ROOT = Path(os.environ.get("BK_EMU_ROOT", "/opt/beken-emulator"))
sys.path.insert(0, str(ROOT))
from src import crypto  # noqa: E402  (Beken CRC16 + strip_crcs, kept in one place)

raw = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "S61_DUMP", str(ROOT / "firmwares/Sonoff_S61s_EUPlug_WBBK_01P_V1.3.bin"))
out = sys.argv[2] if len(sys.argv) > 2 else "logical.bin"

data = Path(raw).read_bytes()
logical = crypto.strip_crcs(data)
Path(out).write_bytes(logical)

# Sanity: how many 34-byte blocks carry a valid CRC (vs erased / not strided).
blocks = len(data) // 34
valid = sum(1 for b in range(blocks)
            if crypto.block_crc_check(data[b*34:b*34+32], data[b*34+32:b*34+34]))
print(f"{raw}: {len(data):#x} physical -> {out}: {len(logical):#x} logical "
      f"({valid}/{blocks} blocks CRC-valid)")

# RBL update containers (bootloader, app, staged OTA). 96-byte header.
ALGO = {0: "none", 1: "XOR", 2: "AES256"}
CMPR = {0: "none", 1: "gzip", 2: "quicklz", 3: "fastlz"}
i = 0
while (i := logical.find(b"RBL\x00", i)) >= 0:
    h = logical[i:i+96]
    algo, algo2, ts = struct.unpack_from("<HHI", h, 4)
    name = h[12:28].split(b"\0")[0].decode(errors="replace")
    ver = h[28:52].split(b"\0")[0].decode(errors="replace")
    crc, fnv, rawsz, pkg, icrc = struct.unpack_from("<IIIII", h, 76)
    print(f"RBL @{i:#08x}: name={name} version={ver} "
          f"encrypt={ALGO.get(algo & 0xff, algo)} compress={CMPR.get(algo >> 8, algo >> 8)} "
          f"time={time.strftime('%Y-%m-%d', time.gmtime(ts))} "
          f"raw={rawsz:#x} package={pkg:#x} crc32={crc:08x}")
    i += 4
