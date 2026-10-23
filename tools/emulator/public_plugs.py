#!/usr/bin/env python3
"""Public starting plugs, and the test of the final firmware run from them.

    python -I tools/emulator/public_plugs.py make --plug MAC --stock DIR
        the suite's plugs saved after D1 (fault_suite.py --stub-cloud prep, on
        MAC, a test copy: copy_plug.py), carried on to just before the final
        firmware - after E3 on the way to Tasmota, after D5 on the way to
        ESPHome - and made public (sanitise_plug.py, DIR the real plug's
        Sonoff backup): captures/emulation/saved/public-pre-final-tasmota and
        -esphome. Again only when what runs before the final firmware changes.

    python -I tools/emulator/public_plugs.py test
        from those, the final firmware firmware.json pins, both ways: each
        installed (F2 to H1, I1 and I2), then each plug taken to the other
        (L1 to I2, X1 to H1). No cloud, real or stub, no account and nothing
        of Sonoff's: docs/updating-firmware.md.

Both use emulators --instances (default 1,2), two at a time.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SAVED = ROOT / "captures/emulation/saved"
LOGS = ROOT / "captures/emulation/public-plugs"
UNATTENDED = ROOT / "tools/emulator/unattended-test.sh"
# Where each way stops before its final firmware, and what it is made into.
STARTS = {"tasmota": ("E3", "public-pre-final-tasmota"), "esphome": ("D5", "public-pre-final-esphome")}
# The test: the final firmware installed, then each plug taken to the other.
TEST = [
    [("public-pre-final-tasmota", "tasmota", "public-on-tasmota"),
     ("public-pre-final-esphome", "esphome", "public-on-esphome")],
    [("public-on-tasmota", "esphome", None), ("public-on-esphome", "tasmota", None)],
]


def run(instance: int, source: str, firmware: str, save: str | None, log: Path,
        extra: tuple[str, ...] = (), stop_after: str | None = None) -> tuple[bool, str]:
    """One run from a saved plug; converted, and its result line."""
    args = [str(UNATTENDED), f"--instance={instance}", f"--from={source}", *extra,
            *([f"--save={save}"] if save else []), "--", "--firmware", firmware]
    log.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with open(log, "w") as out:
        process = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT)
        if stop_after:
            stop_at(instance, stop_after, started, process)
        code = process.wait()
    text = log.read_text(errors="replace")
    result = re.findall(r"Result:\s+(.+)", text)
    if stop_after:
        return step_done(SAVED / save, stop_after), f"stopped after {stop_after}"
    return code == 0 and bool(result) and result[-1].startswith("Converted"), (result[-1] if result else f"exit {code}")


def step_done(saved: Path, step: str) -> bool:
    """Whether the kept plug's current run has the step done, and the next not begun."""
    import json
    try:
        current = (saved / "captures/.autoflash-current").read_text().strip()
        steps = json.loads((saved / "captures" / current / "autoflash-state.json").read_text())["steps"]
    except (OSError, ValueError, KeyError):
        return False
    return (saved / "flash.bin").exists() and steps.get(step, {}).get("status") == "done"


def stop_at(instance: int, step: str, since: float, process: subprocess.Popen) -> None:
    """Ctrl-C twice once the run's own log says the step is done, as
    fault_suite.prep does: the run stops, and --save keeps the plug."""
    from tools.emulator import fault_suite as fs
    scenario = fs.Scenario(instance, {"id": "public", "step": "", "fault": "prep", "arg": ""}, os.devnull)

    def watch() -> None:
        while process.poll() is None:
            if re.search(rf"\] {step} done", scenario.current_run(since, None) or ""):
                scenario.signal("INT"); time.sleep(0.5); scenario.signal("INT")
                return
            time.sleep(0.2)
    threading.Thread(target=watch, daemon=True).start()


def side_by_side(jobs: list) -> list:
    results: list = [None] * len(jobs)
    threads = [threading.Thread(target=lambda i=i, job=job: results.__setitem__(i, job())) for i, job in enumerate(jobs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def command_make(args: argparse.Namespace) -> int:
    os.environ["AUTOFLASH_STUB_CLOUD"] = "1"
    from tools.emulator import fault_suite as fs
    fs.STUB_CLOUD, fs.MAC = True, args.plug
    jobs, made = [], []
    for (firmware, (step, public)), instance in zip(STARTS.items(), args.instances):
        source = fs.saved(fs.AFTER_D1[f"sonoff>{firmware}"])
        if not (SAVED / source / "flash.bin").exists():
            print(f"No {source}: run fault_suite.py --plug={args.plug} --stub-cloud prep first", file=sys.stderr)
            return 1
        work = f"{public}-unsanitised"
        made.append((firmware, work, public))
        jobs.append(lambda i=instance, s=source, f=firmware, w=work, st=step: run(
            i, s, f, w, LOGS / f"make-{f}.log", extra=("--stub-cloud",), stop_after=st))
    failed = 0
    for (firmware, work, public), (ok, said) in zip(made, side_by_side(jobs)):
        if not ok:
            print(f"{firmware}: the run did not stop where it should ({said}); see {LOGS}/make-{firmware}.log")
            failed += 1
            continue
        shutil.rmtree(SAVED / f"{public}.new", ignore_errors=True)
        code = subprocess.call([sys.executable, "-I", str(ROOT / "tools/emulator/sanitise_plug.py"),
                                str(SAVED / work), str(SAVED / f"{public}.new"), "--stock", str(args.stock)])
        shutil.rmtree(SAVED / work, ignore_errors=True)  # it holds Sonoff's: never left lying
        if code:
            failed += 1
            continue
        shutil.rmtree(SAVED / public, ignore_errors=True)
        (SAVED / f"{public}.new").rename(SAVED / public)
        print(f"{firmware}: {SAVED / public}")
    return 1 if failed else 0


def command_test(args: argparse.Namespace) -> int:
    for start in (public for _, public in STARTS.values()):
        mode = SAVED / start / "flash-mode"
        if not mode.is_file() or mode.read_text().strip() != "public":
            print(f"No public plug {start}: make them (public_plugs.py make) or fetch them", file=sys.stderr)
            return 1
    failed = 0
    started = time.time()
    for stage in TEST:
        jobs = [lambda i=instance, s=source, f=firmware, k=keep: run(
                    i, s, f, k, LOGS / f"test-{s}-to-{f}.log")
                for (source, firmware, keep), instance in zip(stage, args.instances)]
        for (source, firmware, _), (ok, said) in zip(stage, side_by_side(jobs)):
            print(f"{'PASS' if ok else 'FAIL'}  {source} to {firmware}: {said}", flush=True)
            failed += not ok
        if failed:
            break  # the second needs the first's plugs
    print(f"{'All passed' if not failed else f'{failed} failed'} in {int(time.time() - started)} s; logs in {LOGS}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--instances", default="1,2", help="the two emulators to use (default 1,2)")
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("make", help="make the public starting plugs (needs the real dump)")
    make.add_argument("--plug", required=True, help="the test copy's MAC (copy_plug.py)")
    make.add_argument("--stock", type=Path, required=True, help="the real plug's Sonoff backup directory")
    make.set_defaults(run=command_make)
    commands.add_parser("test", help="the final firmware, from the public plugs").set_defaults(run=command_test)
    args = parser.parse_args(argv)
    args.instances = [int(n) for n in args.instances.split(",")][:2]
    if len(args.instances) != 2:
        parser.error("--instances needs two")
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
