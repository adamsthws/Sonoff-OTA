"""Decompile S61 functions with angr (pseudo-C).

Builds a small CFG around each target (Thumb) and runs angr's decompiler. angr is
heavy and best-effort on a raw blob — treat the output as a reading aid alongside
disasm.py, not ground truth. (No Java is needed; this is the Ghidra-free path.)

Addresses are offsets into the logical image (see unstripe.py), which equal
CPU addresses.

Usage:
  decomp.py IMAGE ADDR [ADDR...]
"""
import sys
import logging

for noisy in ("angr", "cle", "pyvex", "claripy"):
    logging.getLogger(noisy).setLevel(logging.ERROR)
import angr  # noqa: E402

BIN = sys.argv[1]
targets = [int(x, 0) for x in sys.argv[2:]]

proj = angr.Project(BIN, main_opts={"backend": "blob", "arch": "ARMEL", "base_addr": 0x0},
                    auto_load_libs=False)

for t in targets:
    taddr = t | 1  # Thumb
    try:
        cfg = proj.analyses.CFGFast(regions=[(t - 0x40, t + 0x400)], normalize=True,
                                    function_starts=[taddr], force_complete_scan=False)
    except Exception as e:
        print(f"CFG err @0x{t:x}: {e}")
        continue
    func = next((cfg.kb.functions[a] for a in (taddr, t) if a in cfg.kb.functions), None)
    if func is None:
        try:
            proj.kb.functions.function(addr=taddr, create=True)
            func = proj.kb.functions.get_by_addr(taddr)
        except Exception as e:
            print(f"no func @0x{t:x}: {e}")
            continue
    try:
        dec = proj.analyses.Decompiler(func, cfg=cfg.model)
        print("=" * 70)
        print(f"### FUNCTION 0x{t:06x} ###")
        print("=" * 70)
        print(dec.codegen.text if dec.codegen else "(no codegen)")
    except Exception as e:
        print(f"decompile err @0x{t:x}: {e}")
