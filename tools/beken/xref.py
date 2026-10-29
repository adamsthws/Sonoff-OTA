"""Find cross-references to an address in the logical S61 image.

For each target address it reports:
  - BL/BLX callers (Thumb) over the code region, and
  - 32-bit data-word references (literal pools / tables), for both addr and addr|1
    (Thumb pointers carry the low bit set).

Addresses are offsets into the logical image (see unstripe.py), which equal
CPU addresses. The code region defaults to 0x10000..0x130000 (the app); override
with S61_CODE_LO / S61_CODE_HI if needed.

Usage:
  xref.py IMAGE ADDR [ADDR...]
"""
import os
import sys
import struct
import capstone

img = open(sys.argv[1], "rb").read()
targets = [int(x, 0) for x in sys.argv[2:]]
LO = int(os.environ.get("S61_CODE_LO", "0x10000"), 0)
HI = int(os.environ.get("S61_CODE_HI", "0x130000"), 0)

cs = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)

# BL/BLX callers: scan the code region for 32-bit Thumb branch-with-link.
bl_callers = {t: [] for t in targets}
a = LO
while a < HI - 4:
    w = img[a:a+4]
    if (w[1] & 0xf0) == 0xf0:  # second halfword of a BL/BLX pair
        try:
            insn = next(cs.disasm(w, a))
            if insn.mnemonic in ("bl", "blx"):
                op = insn.op_str.strip()
                if op.startswith("#"):
                    dst = int(op[1:], 0)
                    for t in targets:
                        if dst == t or dst == (t & ~1):
                            bl_callers[t].append(a)
                    a += insn.size
                    continue
        except Exception:
            pass
    a += 2


def word_refs(value):
    out, off, needle = [], 0, struct.pack("<I", value)
    while (i := img.find(needle, off)) >= 0:
        out.append(i)
        off = i + 1
    return out


for t in targets:
    print(f"TARGET 0x{t:06x}")
    print("  BL/BLX callers:", [hex(x) for x in bl_callers[t]])
    print(f"  word refs(0x{t:x}):   ", [hex(x) for x in word_refs(t)])
    print(f"  word refs(0x{t:x}|1): ", [hex(x) for x in word_refs(t | 1)])
