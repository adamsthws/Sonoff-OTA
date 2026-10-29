"""Locate code by the log/format string it uses.

For each string it finds the bytes in the logical image, the literal-pool words
that point at the string, and disassembles (Thumb) the `ldr rX, [pc, #imm]` that
loads each pool word plus its surrounding context. Good for turning a UART line
like "softap:ssid=%s" or "pairingMode by %d" into the function that prints it.

Addresses are offsets into the logical image (see s61_unstripe.py).

Usage:
  s61_strxref.py IMAGE STRING [STRING...]
"""
import struct
import sys
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB

img = open(sys.argv[1], "rb").read()
md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)

for s in sys.argv[2:]:
    off = img.find(s.encode() + b"\0")
    if off < 0:
        off = img.find(s.encode())
    print(f"\n### '{s}' at {off:#x}")
    if off < 0:
        continue
    lit = struct.pack("<I", off)
    pools = [i for i in range(0, len(img) - 3, 4) if img[i:i+4] == lit]
    print("   literal-pool refs:", [hex(p) for p in pools[:8]])
    for p in pools[:3]:
        # a Thumb 'ldr rX, [pc, #imm]' loading this pool word sits within ~1 KB before it
        for start in range(max(0, p - 0x400), p, 2):
            for ins in md.disasm(img[start:start+2], start):
                if ins.mnemonic == "ldr" and "pc" in ins.op_str:
                    imm = int(ins.op_str.split("#")[-1].rstrip("]"), 16) if "#" in ins.op_str else 0
                    if (((start + 4) & ~3) + imm) == p:
                        print(f"   loaded by ldr at {start:#x}; context:")
                        code = img[start - 0x30:start + 0x20]
                        for i in md.disasm(code, start - 0x30):
                            mark = "   <==" if i.address == start else ""
                            print(f"      {i.address:#x}: {i.mnemonic:7} {i.op_str}{mark}")
