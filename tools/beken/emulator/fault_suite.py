#!/usr/bin/env python3
"""The S61's fault suite: every direction, every step that writes to or waits
on the plug, every fault - on emulated S61s side by side (bk-emu.sh plug), as
tools/espressif/emulator/fault_suite.py does for the S60.

    fault_suite.py prep                 the starting plugs: one on the interim firmware,
                                        one on OpenBeken, one on ESPHome, converted by
                                        the tool from the dump
    fault_suite.py list                 every scenario
    fault_suite.py list openbeken       those a new OpenBeken build is checked with
                                        (docs/updating-firmware.md)
    fault_suite.py batch [FILE]         the scenarios in FILE (one id a line; every
                                        scenario without), skipping those passed
    fault_suite.py report               the latest result of each

Each scenario: a run of the tool from a starting plug (a fresh dump for
Sonoff's firmware, a saved one otherwise); when the step under test has been
going for a share of its usual time (40%, and 10% and 90% at the steps that
write), the fault; then the tool run again, as a person would - switching the
plug off and on where the run says to - until the plug is converted, or four
runs fail. Every scenario also checks the fault landed while its step was
under way (MISTIMED if not).

The faults: the plug switched off for 1 or 30 s (plugoff), this computer off
the plug's Wi-Fi for 30 s (outage), the tool killed and run again after 1 or
180 s (crash), and Ctrl-C (ctrlc); at the steps that write, a power cut and
another as the tool sends it again (double), and the router giving the plug
a new address once the step is done (readdress); and at each route's
"apply", the plug switched off 10, 50 or 90% of the way through its
bootloader writing the new firmware in - timed off the plug's own output.
And at each step that sends an image the stock bootloader installs - Sonoff's
firmware's (B2) and OpenBeken's fetch of ESPHome (J2, O1) - an image it
refuses (badimage): the tool sends it spoilt once (--simulate-bad-image
STEP), fetched whole but failing the bootloader's CRC check, so the plug
starts the firmware it had again; the scenario passes when the tool sends
it again by itself and the plug is converted.

The routes from the interim firmware start from a plug a run to ESPHome left
there, found by a new run. A plug converted through the interim firmware is
also checked: by the names it asked DHCP for, it was autoflash-recovery-CDEF
there from its first request and never OpenBeken's own name; and once ESPHome
is on it, the interim's settings are erased from its flash (interim_problems).

Each emulator is a plug of its own (autoflash-s61-N, on autoflash-beken-N),
AUTOFLASH_S61_WORKERS of them at once (default 2: each takes a core, and the
tool's runs share what is left). Sonoff's firmware restarts by itself once
it has an update, in the emulator as on a real plug
(tools/beken/emulator/README.md).

Results: captures/emulation/s61-suite/results.jsonl, and each scenario's run
logs and the plug's serial output beside it.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from tools.autoflash.routes import beken  # noqa: E402

BK_EMU = REPO / "tools/beken/emulator/bk-emu.sh"
EMU = REPO / "captures/emulation"
SAVED = EMU / "s61/saved"
OUT = Path(os.environ.get("AUTOFLASH_S61_SUITE_DIR", EMU / "s61-suite"))
RESULTS = OUT / "results.jsonl"
# A made-up eWeLink account: the plug's eWeLink is the local stub, which takes any.
ACCOUNT = "EWELINK_EMAIL=someone@example.invalid\nEWELINK_PASSWORD=made-up-password\n"

# Each direction's steps that write to or wait on the plug, with how long each
# took in passing runs (2026-10-30; sonoff>esphome 2026-11-02, seconds): a fault
# lands at a share of it.
ROUTES = {
    "sonoff>openbeken": {"B2": 30, "K1": 27, "B4": 12},
    "sonoff>esphome": {"B2": 32, "J1": 28, "B4": 12, "J2": 14, "K2": 30},
    "openbeken>esphome": {"B4": 12, "O1": 12, "K2": 31},
    "openbeken>sonoff": {"O2": 24, "K3": 75},
    "esphome>openbeken": {"E1": 6, "K1": 27, "B4": 12},
    "esphome>sonoff": {"E2": 15, "K3": 80},
    # A plug a run to ESPHome left on the interim firmware, found by a new run.
    # Kept as OpenBeken, J3 waits for its restart: K1 finds it answering.
    "interim>esphome": {"B4": 12, "J2": 14, "K2": 30},
    "interim>openbeken": {"J3": 15, "B4": 12},
    "interim>sonoff": {"B4": 12, "J4": 24, "K3": 75},
}
CORE = [("plugoff", 1), ("plugoff", 30), ("outage", 30), ("crash", 1), ("crash", 180), ("ctrlc", "once")]
# At the steps that write, the riskiest moments too: just begun, nearly done.
TIMING = [("plugoff", 1), ("crash", 1)]
RUN_LIMIT = 1500       # seconds one run of the tool may take
RECOVERY_RUNS = 4      # runs after the fault before a scenario fails
POWER_CYCLE = re.compile(r"switch the (plug|device) off and on", re.IGNORECASE)
# The steps whose image the stock bootloader installs, and the tool can send
# spoilt (--simulate-bad-image STEP): Sonoff's firmware's, and OpenBeken's
# fetch of ESPHome.
BAD_IMAGE_STEPS = {"B2", "J2", "O1"}
# The interim firmware's settings, which it erases before the firmware it
# fetched starts (devices/sonoff-s61/openbeken.md#the-interim-firmware): its
# own sector, and the flash variables its Wi-Fi keeps.
INTERIM_SETTINGS = ((0x3E1000, 0x1000), (0x1E5000, 0x8000))
# The name a plug asks its router's DHCP for, by its firmware.
DHCP_NAME = re.compile(r"DHCPACK\(\S+\) \S+ \S+ (\S+)")
NAME_PREFIX = {"interim": "autoflash-recovery-", "openbeken": "openbeken-", "esphome": "esphome-"}
# A new OpenBeken build's check: each thing this project's driver and patches
# do on it, once - its Wi-Fi and settings taken from Sonoff's (B4 after B2),
# the interim firmware named, fetching ESPHome and wiping itself (J2), kept
# as OpenBeken (J3), each way back to Sonoff's firmware (J4, O2), and
# installed from ESPHome (K1).
OPENBEKEN_CHECK = ["sonoff>openbeken:B4:crash-1", "sonoff>esphome:J2:badimage-once", "interim>openbeken:J3:crash-1",
                   "interim>sonoff:J4:crash-1", "openbeken>sonoff:O2:crash-1", "esphome>openbeken:K1:crash-1"]


def sid(route: str, step: str, fault: str, arg, at: int = 40) -> str:
    return f"{route}:{step}{'' if at == 40 else f'@{at}'}:{fault}-{arg}"


def scenarios() -> list[dict]:
    found = []
    for route, steps in ROUTES.items():
        for step in steps:
            for fault, arg in CORE:
                found.append(dict(id=sid(route, step, fault, arg), route=route, step=step,
                                  fault=fault, arg=arg, at=40))
            if step in beken.DESTRUCTIVE_STEPS:
                for at in (10, 90):
                    for fault, arg in TIMING:
                        found.append(dict(id=sid(route, step, fault, arg, at), route=route, step=step,
                                          fault=fault, arg=arg, at=at))
                # A power cut there, then another as the tool sends it again; and the
                # plug at a new address once it is sent, as a router may give it.
                for fault, arg in (("double", "plugoff-1"), ("readdress", "new")):
                    found.append(dict(id=sid(route, step, fault, arg), route=route, step=step,
                                      fault=fault, arg=arg, at=40))
        # An image the bootloader refuses, sent again.
        for step in set(steps) & BAD_IMAGE_STEPS:
            found.append(dict(id=sid(route, step, "badimage", "once"), route=route, step=step,
                              fault="badimage", arg="once", at=40))
        # The plug switched off while its bootloader writes the new firmware in:
        # timed off the plug's own output, a share of the way through the write.
        for at in (10, 50, 90) if set(steps) & beken.DESTRUCTIVE_STEPS else ():
            found.append(dict(id=sid(route, "apply", "plugoff", 1, at), route=route, step="apply",
                              fault="plugoff", arg=1, at=at))
    return found


def route_steps(route: str) -> list[str]:
    """Every step a run of `route` records, after the start all routes share."""
    source, target = route.split(">")
    return [step for step, _, _ in beken.steps_for(target, source)]


def interim_problems(route: str, names: list[str], flash: bytes | None) -> list[str]:
    """What a converted plug shows wrong of the interim firmware: from the
    names it asked DHCP for, in order, and its flash once stopped (None when
    the route does not need it). On the interim firmware its name is
    autoflash-recovery-CDEF from its first request - never OpenBeken's own -
    and once it has fetched ESPHome its settings are gone."""
    source, target = route.split(">")
    steps = route_steps(route)
    problems = []
    if "J1" in steps and not any(name.startswith(NAME_PREFIX["interim"]) for name in names):
        problems.append("the interim firmware never asked DHCP by its own name")
    if "J2" in steps and any(name.startswith(NAME_PREFIX["openbeken"]) for name in names):
        problems.append("the interim firmware asked DHCP by OpenBeken's own name")
    if target in NAME_PREFIX and not (names and names[-1].startswith(NAME_PREFIX[target])):
        problems.append(f"the plug's last name was {names[-1] if names else 'none'}, not {target}'s")
    if "J2" in steps and flash is not None:
        for start, size in INTERIM_SETTINGS:
            if flash[start:start + size] != b"\xff" * size:
                problems.append(f"the interim firmware's settings at {start:#x} were not erased")
    return problems


def converted(state: dict, route: str) -> bool:
    """Whether a run's record (autoflash-state.json) has every step of the
    route done: the plug on its new firmware, and checked there."""
    steps = state.get("steps", {})
    return all((steps.get(step) or {}).get("status") == "done" for step in route_steps(route))


class Instance:
    """One emulated plug, its Wi-Fi and its computer."""

    def __init__(self, n: int):
        self.n = n
        self.plug = f"autoflash-s61-{n}"
        self.network = f"autoflash-beken-{n}"
        self.computer = f"autoflash-s61-computer-{n}"
        self.state = EMU / "s61" / self.plug
        self.before: set[Path] = set()   # runs the starting plug came with, not this scenario's
        self.env = {**os.environ, "AUTOFLASH_BEKEN_PLUG_NAME": self.plug,
                    "AUTOFLASH_BEKEN_NETWORK": self.network, "AUTOFLASH_BEKEN_COMPUTER": self.computer}

    def sh(self, *args, timeout=None, check=False):
        return subprocess.run([str(BK_EMU), *args], env=self.env, capture_output=True, text=True,
                              timeout=timeout, check=check)

    def stop(self) -> None:
        # Long enough for the plug to keep its flash as it stops (plug.py).
        for name in (self.computer, self.plug):
            subprocess.run(["docker", "stop", "-t", "30", name], capture_output=True)
        # And gone (--rm), before a container of the same name is started again.
        for _ in range(30):
            found = subprocess.run(["docker", "ps", "-aq", "--filter", f"name=^({self.computer}|{self.plug})$"],
                                   capture_output=True, text=True).stdout.strip()
            if not found:
                return
            time.sleep(1)

    def reset(self, start: str) -> None:
        """The plug as `start` (sonoff: the dump, untouched; else saved by prep)."""
        self.stop()
        shutil.rmtree(self.state, ignore_errors=True)
        if start != "sonoff":
            shutil.copytree(SAVED / start, self.state)
        (self.state / "work/captures").mkdir(parents=True, exist_ok=True)
        (self.state / "work/captures/tool.env").write_text(ACCOUNT, encoding="utf-8")
        self.before = set(self.all_runs())

    def dhcp_names(self) -> list[str]:
        """The names the plug asked its router's DHCP for, in order."""
        log = subprocess.run(["docker", "exec", self.plug, "cat", "/run/autoflash-s61/dnsmasq.log"],
                             capture_output=True, text=True).stdout
        return [found.group(1) for found in DHCP_NAME.finditer(log)]

    def lease(self) -> str | None:
        found = subprocess.run(["docker", "exec", self.plug, "sh", "-c",
                                "sort -n /run/autoflash-s61/leases | tail -1"],
                               capture_output=True, text=True).stdout.split()
        return found[2] if len(found) > 2 else None

    def answering(self) -> bool:
        address = self.lease()
        if not address:
            return False
        try:
            with socket.create_connection((address, 80), timeout=3):
                return True
        except OSError:
            return False

    def online(self) -> bool:
        """Sonoff's firmware in its eWeLink: the time zone it asks once there."""
        try:
            return '"TZ":{' in (self.state / "stub.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False

    def start(self, firmware: str, wait: float = 300) -> bool:
        self.sh("plug", timeout=600, check=True)
        return self.ready(firmware, wait)

    def ready(self, firmware: str, wait: float = 300) -> bool:
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if (self.online() if firmware == "sonoff" else self.answering()):
                return True
            time.sleep(3)
        return False

    def power(self, how: str) -> None:
        self.sh("power", how, timeout=60)

    def tool(self, target: str, log: Path, new: bool = False, more: tuple[str, ...] = ()) -> subprocess.Popen:
        args = ["computer", "tools/autoflash/cli.py", "run", "--unattended", "--env-file",
                "captures/tool.env", "--firmware", target] + (["--new"] if new else []) + list(more)
        out = open(log, "a", encoding="utf-8")
        return subprocess.Popen([str(BK_EMU), *args], env=self.env, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)

    def all_runs(self) -> list[Path]:
        return sorted((self.state / "work/captures/plugs").glob("*/runs/*/run.log"),
                      key=lambda path: path.stat().st_mtime)

    def runs(self) -> list[Path]:
        """This scenario's runs, oldest first."""
        return [path for path in self.all_runs() if path not in self.before]

    def last_state(self) -> dict:
        logs = self.runs()
        try:
            return json.loads((logs[-1].parent / "autoflash-state.json").read_text(encoding="utf-8"))
        except (IndexError, OSError, ValueError):
            return {}

    def last_log(self) -> str:
        logs = self.runs()
        return logs[-1].read_text(encoding="utf-8", errors="replace") if logs else ""


def wait_for_step(instance: Instance, step: str, run: subprocess.Popen, limit: float = RUN_LIMIT) -> bool:
    """Until the run starts `step`; False if it ends, or takes too long, first."""
    deadline = time.monotonic() + limit
    begun = re.compile(rf"\] {re.escape(step)}  ")
    while time.monotonic() < deadline and run.poll() is None:
        if begun.search(instance.last_log()):
            return True
        time.sleep(0.5)
    return False


def inject(instance: Instance, fault: str, arg, notes: dict) -> None:
    if fault == "plugoff":
        instance.power("off")
        time.sleep(arg)
        instance.power("on")
    elif fault == "outage":
        address = subprocess.run(["docker", "inspect", "-f",
                                  "{{(index .NetworkSettings.Networks \"%s\").IPAddress}}" % instance.network,
                                  instance.computer], capture_output=True, text=True).stdout.strip()
        subprocess.run(["docker", "network", "disconnect", instance.network, instance.computer],
                       capture_output=True)
        time.sleep(arg)
        subprocess.run(["docker", "network", "connect", "--ip", address, instance.network, instance.computer],
                       capture_output=True)
        notes["outage_address"] = address
    elif fault == "crash":
        subprocess.run(["docker", "kill", instance.computer], capture_output=True)
    elif fault == "ctrlc":
        subprocess.run(["docker", "kill", "--signal", "INT", instance.computer], capture_output=True)


def recover(instance: Instance, target: str, notes: dict) -> None:
    """What a person does before running the tool again: switch the plug off
    and on, where the last run said to."""
    if POWER_CYCLE.search(instance.last_log()[-3000:]):
        instance.power("cycle")
        instance.ready(target, wait=300)
        notes["power_cycles"] = notes.get("power_cycles", 0) + 1


# The bootloader's own lines as it writes an image in (rt_ota), and as it is done.
APPLY_BEGINS = re.compile(rb"OTA firmware\(app\) upgrade\(")
APPLY_PROGRESS = re.compile(rb"OTA Write: \[[=> ]*\] (\d+)%")
APPLY_ENDS = re.compile(rb"ota_erase_dl_rbl erase")


class ApplyCut(threading.Thread):
    """Switch the plug off for a second when its bootloader is `at`% of the
    way through writing an image in, whichever run, or restart, that is in."""

    def __init__(self, instance: Instance, at: int):
        super().__init__(daemon=True)
        self.instance, self.at = instance, at
        self.fired: int | None = None
        self.done = threading.Event()

    def run(self) -> None:
        offset, seen, begun = 0, b"", False
        while not self.done.is_set():
            try:
                with open(self.instance.state / "serial.log", "rb") as serial:
                    serial.seek(offset)
                    new = serial.read()
            except OSError:
                new = b""
            offset += len(new)
            seen = seen[-4096:] + new
            # One read may hold an apply's end and the next one's start, and more.
            while True:
                if not begun:
                    start = APPLY_BEGINS.search(seen)
                    if not start:
                        break
                    begun, seen = True, seen[start.end():]
                end = APPLY_ENDS.search(seen)
                for found in APPLY_PROGRESS.finditer(seen[:end.start()] if end else seen):
                    if int(found.group(1)) >= self.at:
                        self.instance.power("off")
                        time.sleep(1)
                        self.instance.power("on")
                        self.fired = int(found.group(1))
                        return
                if not end:
                    break
                begun, seen = False, seen[end.end():]   # over before its moment: the next apply, if any
            time.sleep(0.2)


def readdress(instance: Instance, notes: dict) -> None:
    """The router forgets the plug's address and gives it another when it next asks."""
    address = instance.lease() or ""
    last = int(address.rsplit(".", 1)[1]) if address else 50
    subprocess.run(["docker", "exec", instance.plug, "/run/autoflash-s61/router", "55" if last < 55 else "50"],
                   capture_output=True, timeout=60)
    notes["moved_from"] = address


def wait_for(run: subprocess.Popen, seen: Callable[[], bool], limit: float = RUN_LIMIT) -> bool:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline and run.poll() is None:
        if seen():
            return True
        time.sleep(0.5)
    return False


def finish(run: subprocess.Popen, instance: Instance) -> None:
    try:
        run.wait(timeout=RUN_LIMIT)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", instance.computer], capture_output=True)


def run_scenario(instance: Instance, sc: dict) -> dict:
    source, target = sc["route"].split(">")
    folder = OUT / "logs" / sc["id"].replace(":", "_").replace(">", "-")
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    log = folder / "tool.log"
    started = time.monotonic()
    notes: dict = {}
    instance.reset(source)
    if not instance.start(source):
        return dict(result="NOT-READY", notes=notes)
    if sc["step"] == "apply":
        outcome = apply_scenario(instance, sc, target, log, notes)
    elif sc["fault"] == "badimage":
        outcome = bad_image_scenario(instance, sc, target, log, notes)
    else:
        outcome = step_scenario(instance, sc, target, log, notes)
    if outcome["result"] == "PASS":
        names = instance.dhcp_names()
        flash = None
        if "J2" in route_steps(sc["route"]):
            instance.stop()   # its flash kept as it stops
            flash = (instance.state / "flash.bin").read_bytes()
        notes["dhcp_names"] = names
        problems = interim_problems(sc["route"], names, flash)
        if problems:
            outcome = dict(outcome, result="FAIL")
            notes["interim"] = problems
    for index, path in enumerate(instance.runs()):
        shutil.copy(path, folder / f"run-{index + 1}.log")
    try:
        shutil.copy(instance.state / "serial.log", folder / "serial.log")
    except OSError:
        pass
    return dict(**outcome, notes=notes, seconds=round(time.monotonic() - started))


def apply_scenario(instance: Instance, sc: dict, target: str, log: Path, notes: dict) -> dict:
    cut = ApplyCut(instance, sc["at"])
    cut.start()
    try:
        runs = 0
        while not converted(instance.last_state(), sc["route"]) and runs <= RECOVERY_RUNS + 1:
            if runs:
                recover(instance, target, notes)
            finish(instance.tool(target, log, new=not runs), instance)
            runs += 1
    finally:
        cut.done.set()
        cut.join(5)
    notes["cut_at"] = cut.fired
    if cut.fired is None:
        return dict(result="NO-STEP", runs=runs)
    return dict(result="PASS" if converted(instance.last_state(), sc["route"]) else "FAIL", runs=runs)


# What a run says as it goes back to send an image again (errors.GoBack).
SENT_AGAIN = re.compile(r"was not installed: the device restarted on the firmware it had")


def bad_image_scenario(instance: Instance, sc: dict, target: str, log: Path, notes: dict) -> dict:
    """The step's image sent spoilt, once - each run is asked, as a route may
    reach the step only after a restart, and the tool spoils it once a run's
    record - and runs again, as a person would, until the plug is converted:
    which must come from a run that saw the image was not installed and sent
    it again."""
    spoil = ("--simulate-bad-image", sc["step"])
    finish(instance.tool(target, log, new=True, more=spoil), instance)
    runs = 1
    while not converted(instance.last_state(), sc["route"]) and runs <= RECOVERY_RUNS:
        recover(instance, target, notes)
        finish(instance.tool(target, log, more=spoil), instance)
        runs += 1
    notes["sent_again"] = any(SENT_AGAIN.search(path.read_text(encoding="utf-8", errors="replace"))
                              for path in instance.runs())
    ok = converted(instance.last_state(), sc["route"]) and notes["sent_again"]
    return dict(result="PASS" if ok else "FAIL", runs=runs)


def step_scenario(instance: Instance, sc: dict, target: str, log: Path, notes: dict) -> dict:
    run = instance.tool(target, log, new=True)
    # A run a fault ended reaches its later steps only through runs again, with
    # the switch off and on the tool asks for, as a person would.
    reruns = 0
    while not wait_for_step(instance, sc["step"], run):
        run.wait(timeout=60)
        reruns += 1
        if reruns > RECOVERY_RUNS or converted(instance.last_state(), sc["route"]):
            return dict(result="NO-STEP")
        recover(instance, target, notes)
        run = instance.tool(target, log)
    step = re.escape(sc["step"])
    if sc["fault"] == "readdress":
        if not wait_for(run, lambda: re.search(rf"\] {step} done", instance.last_log())):
            notes["mistimed"] = True
        readdress(instance, notes)
    else:
        time.sleep(ROUTES[sc["route"]][sc["step"]] * sc["at"] / 100)
        if re.search(rf"\] {step} done", instance.last_log()):
            notes["mistimed"] = True
        fault, arg = ("plugoff", 1) if sc["fault"] == "double" else (sc["fault"], sc["arg"])
        inject(instance, fault, arg, notes)
        if fault == "crash":
            run.wait(timeout=60)
            time.sleep(arg)
    finish(run, instance)
    second = sc["fault"] == "double"
    runs = 1
    while not converted(instance.last_state(), sc["route"]) and runs <= RECOVERY_RUNS:
        recover(instance, target, notes)
        run = instance.tool(target, log)
        if second and wait_for_step(instance, sc["step"], run):
            # The second cut, as the tool sends the step again.
            time.sleep(ROUTES[sc["route"]][sc["step"]] * sc["at"] / 100)
            inject(instance, "plugoff", 1, notes)
            notes["second_cut"] = True
            second = False
        finish(run, instance)
        runs += 1
    if sc["fault"] == "readdress":
        notes["moved_to"] = instance.lease()
    ok = converted(instance.last_state(), sc["route"])
    return dict(result="MISTIMED" if notes.get("mistimed") else ("PASS" if ok else "FAIL"), runs=runs)


def record(sc: dict, outcome: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("a", encoding="utf-8") as results:
        results.write(json.dumps({"id": sc["id"], "at": time.strftime("%Y-%m-%dT%H:%M:%S"), **outcome}) + "\n")


def latest() -> dict[str, dict]:
    found: dict[str, dict] = {}
    try:
        for line in RESULTS.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            found[entry["id"]] = entry
    except (OSError, ValueError):
        pass
    return found


def batch(ids_file: str | None = None) -> None:
    every = {sc["id"]: sc for sc in scenarios()}
    wanted = (Path(ids_file).read_text(encoding="utf-8").split() if ids_file else list(every))
    passed = {i for i, entry in latest().items() if entry.get("result") == "PASS"}
    todo: queue.Queue = queue.Queue()
    for i in wanted:
        if i in every and i not in passed:
            todo.put(every[i])
    workers = int(os.environ.get("AUTOFLASH_S61_WORKERS", "2"))
    lock = threading.Lock()

    def work(n: int) -> None:
        instance = Instance(n)
        try:
            while True:
                try:
                    sc = todo.get_nowait()
                except queue.Empty:
                    return
                try:
                    outcome = run_scenario(instance, sc)
                except Exception as exc:  # noqa: BLE001 - one scenario's failure is recorded, not the batch's
                    outcome = dict(result="ERROR", notes={"error": repr(exc)})
                with lock:
                    record(sc, outcome)
                    print(f"{outcome['result']:9} {sc['id']}", flush=True)
        finally:
            instance.stop()   # an emulator never left running

    threads = [threading.Thread(target=work, args=(n,)) for n in range(1, workers + 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def prep() -> None:
    """The starting plugs, converted by the tool from the dump: on the interim
    firmware (a run to ESPHome stopped once that has started), on OpenBeken,
    then on ESPHome, each saved with the captures its runs left."""
    instance = Instance(1)
    try:
        instance.reset("sonoff")
        if not instance.start("sonoff"):
            raise SystemExit("the plug did not come online")
        log = OUT / "prep-interim.log"
        OUT.mkdir(parents=True, exist_ok=True)
        run = instance.tool("esphome", log, new=True)
        if not wait_for_step(instance, "B4", run):
            raise SystemExit(f"the plug did not start the interim firmware: see {log}")
        subprocess.run(["docker", "kill", instance.computer], capture_output=True)
        save(instance, "interim")
        instance.reset("sonoff")
        if not instance.start("sonoff"):
            raise SystemExit("the plug did not come online")
        for target, route in (("openbeken", "sonoff>openbeken"), ("esphome", "openbeken>esphome")):
            log = OUT / f"prep-{target}.log"
            OUT.mkdir(parents=True, exist_ok=True)
            for attempt in range(RECOVERY_RUNS):
                instance.tool(target, log, new=attempt == 0).wait(timeout=RUN_LIMIT)
                if converted(instance.last_state(), route):
                    break
                if POWER_CYCLE.search(instance.last_log()[-3000:]):
                    instance.power("cycle")
                    instance.ready(target)
            else:
                raise SystemExit(f"the plug was not converted to {target}: see {log}")
            save(instance, target)
            instance.start(target)
    finally:
        instance.stop()


def save(instance: Instance, firmware: str) -> None:
    instance.stop()
    shutil.rmtree(SAVED / firmware, ignore_errors=True)
    shutil.copytree(instance.state, SAVED / firmware, ignore=keep_for_saving)
    print(f"saved the plug on {firmware} in {SAVED / firmware}", flush=True)


def keep_for_saving(folder: str, names: list[str]) -> list[str]:
    """What a saved plug leaves out: its logs and power switch, its stub's
    certificates (for its own network's router: each instance makes its own),
    the backup B4 made (so a route that takes B4 has it to do), and of its
    computer's copy of the checkout all but the captures its runs left."""
    if Path(folder).name == "work":
        return [name for name in names if name != "captures"]
    return [name for name in names if name.endswith(".log")
            or name in ("power", "certs.part", "certs-for", "ca.crt", "leaf.crt", "leaf.key", "sonoff-backup")]


def report() -> None:
    found = latest()
    for sc in scenarios():
        entry = found.get(sc["id"])
        print(f"{(entry or {}).get('result', '-'):9} {sc['id']}")


def main(argv: list[str]) -> int:
    command = argv[0] if argv else ""
    if command == "prep":
        prep()
    elif command == "list":
        for i in (OPENBEKEN_CHECK if argv[1:] == ["openbeken"] else [sc["id"] for sc in scenarios()]):
            print(i)
    elif command == "batch":
        batch(argv[1] if len(argv) > 1 else None)
    elif command == "report":
        report()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
