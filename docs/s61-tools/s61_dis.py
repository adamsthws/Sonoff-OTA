"""Disassemble a window of the logical S61 image (capstone).

Addresses are offsets into the logical image (see s61_unstripe.py), which equal
CPU addresses. Thumb by default; pass 'arm' for ARM. A Thumb function address
with its low bit set (addr|1) is accepted.

Usage:
  s61_dis.py IMAGE ADDR [N] [thumb|arm]
    IMAGE   logical image (destriped)
    ADDR    start address (hex ok, e.g. 0x4203c)
    N       number of instructions (default 24)
    mode    'thumb' (default) or 'arm'
"""
import sys
import capstone

img = open(sys.argv[1], "rb").read()
addr = int(sys.argv[2], 0)
n = int(sys.argv[3]) if len(sys.argv) > 3 else 24
mode = sys.argv[4] if len(sys.argv) > 4 else "thumb"

cs = capstone.Cs(capstone.CS_ARCH_ARM,
                 capstone.CS_MODE_THUMB if mode == "thumb" else capstone.CS_MODE_ARM)
start = addr & ~1
width = 2 if mode == "thumb" else 4
data = img[start:start + n * width + 8]

for cnt, insn in enumerate(cs.disasm(data, start)):
    print(f"0x{insn.address:06x}:  {insn.bytes.hex():<8}  {insn.mnemonic}\t{insn.op_str}")
    if cnt + 1 >= n:
        break
