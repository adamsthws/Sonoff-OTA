#!/usr/bin/env python3
"""The fault matrix: every direction, every step that writes to or waits on
the plug, every fault - on emulators side by side (docs/testing.md).

    fault_matrix.py --plug MAC prep     the starting plugs, one at a time (eWeLink)
    fault_matrix.py --plug MAC run K    the matrix on K emulators (instances 2..K+1),
                                        C2 on instance 1
    fault_matrix.py --plug MAC rerun K  every scenario not yet passed, again
    fault_matrix.py --plug MAC subset K the faults during the backup and D1,
                                        the shortest and longest of each kind
    fault_matrix.py report              results so far

Each scenario: a conversion from a saved plug (unattended-test.sh --from);
when the step under test has been going for about 40% of its usual time, the
fault; then the tool run again, as a person would - switching the plug off and
on where the run says to - until the plug is converted, or four runs fail.
Results go to captures/emulation/matrix/: results.jsonl, a log per scenario,
and what a failure leaves (its run records and the plug's serial output).
"""
import heapq
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EMU = REPO / "captures/emulation"
RESULTS = EMU / "matrix" / "results.jsonl"
LOGS = EMU / "matrix" / "logs"
MAC = None  # --plug: the backed-up plug the emulators run copies of
PLUG_CMD = "/opt/s60emu/bin/plug"
MAX_RUNS = 4

# Each direction: the saved plug it starts from, its target, and the steps that
# write to or wait on the plug, with how long each usually takes (emulator).
ROUTES = {
    "tasmota>esphome": ("matrix-tasmota", "esphome", {"L1": 40, "M1": 8, "I2": 5}),
    "tasmota>sonoff": ("matrix-tasmota", "sonoff",
                       {"L1": 40, "X1": 12, "X2": 25, "D4": 31, "D5": 2, "R1": 57, "R2": 2}),
    "esphome>tasmota": ("matrix-esphome", "tasmota",
                        {"X1": 12, "X2": 25, "D4": 31, "D5": 2, "E1": 2, "E2": 29, "E3": 5,
                         "F2": 35, "F4": 31, "G1": 33, "H1": 2}),
    "esphome>sonoff": ("matrix-esphome", "sonoff",
                       {"X1": 12, "X2": 25, "D4": 31, "D5": 2, "R1": 57, "R2": 2}),
    "sonoff>tasmota": ("matrix-stockC2-tasmota", "tasmota",
                       {"C3": 14, "D1": 18, "D4": 31, "D5": 2, "E1": 2, "E2": 29, "E3": 5,
                        "F2": 35, "F4": 31, "G1": 33, "H1": 2}),
    "sonoff>esphome": ("matrix-stockC2-esphome", "esphome",
                       {"C3": 14, "D1": 18, "D4": 31, "D5": 2, "I1": 36, "I2": 5}),
}
# C2 goes through eWeLink: one at a time, from a fresh stock plug.
C2_ROUTES = {"sonoff>tasmota": "tasmota", "sonoff>esphome": "esphome"}

FAULTS = ([("outage", s) for s in (1, 5, 15, 30, 60, 120, 180)]
          + [("plugoff", s) for s in (1, 5, 30, 180)]
          + [("crash", s) for s in (1, 60, 180)]
          + [("cancel", "continue"), ("cancel", "new"), ("ctrlc", "once")])

# The bridge, off its home network for longer than this, raises its own and keeps
# to it - and the emulator cannot be both, nor join its WPA2 one.
BRIDGE_STA_WINDOW = 45
BRIDGE_RUNNING = {("sonoff>tasmota", "C3"), ("sonoff>tasmota", "D1"),
                  ("sonoff>esphome", "C3"), ("sonoff>esphome", "D1")}
ESPHOME_RUNNING = {("esphome>tasmota", "X1"), ("esphome>sonoff", "X1"),
                   ("tasmota>esphome", "I2"), ("sonoff>esphome", "I2")}


def scenarios():
    for route, (_, _, steps) in ROUTES.items():
        for step in steps:
            for fault, arg in FAULTS:
                yield {"id": f"{route}:{step}:{fault}-{arg}", "route": route, "step": step,
                       "fault": fault, "arg": arg}


def c2_scenarios():
    for route in C2_ROUTES:
        for fault, arg in FAULTS:
            yield {"id": f"{route}:C2:{fault}-{arg}", "route": route, "step": "C2",
                   "fault": fault, "arg": arg}


# eWeLink keeps one connection per plug, and every emulated plug is the same one:
# each plug's way to the internet is shut but for its turn (CLOUD, PlugMonitor).
BLOCK = ["iptables", "FORWARD", "-s", "192.168.60.50", "-o", "eth0", "-j", "DROP"]


def plug_internet(n, on):
    """The plug's own way to the internet (eWeLink), on or off.  Off for every
    plug but the one whose turn it is: Sonoff's firmware, booted by a fault or
    at the end of a route back to it, would otherwise connect as the same plug
    and knock that one off the cloud."""
    e = name(n)
    if on:
        while sh("docker", "exec", e, BLOCK[0], "-D", *BLOCK[1:]).returncode == 0:
            pass
    elif sh("docker", "exec", e, BLOCK[0], "-C", *BLOCK[1:]).returncode != 0:
        sh("docker", "exec", e, BLOCK[0], "-I", BLOCK[1], "1", *BLOCK[2:])


class CloudQueue:
    """Turns at the cloud, one plug at a time: a plug that went back to Sonoff's
    firmware part way through a run (FALLEN) before one starting a scenario
    (FRESH), then first come, first served."""
    FALLEN, FRESH = 0, 1

    def __init__(self):
        self.cv = threading.Condition()
        self.holder = None
        self.waiting = []  # (priority, ticket, instance)
        self.tickets = 0

    def acquire(self, n, priority, give_up):
        with self.cv:
            self.tickets += 1
            entry = (priority, self.tickets, n)
            heapq.heappush(self.waiting, entry)
            while not (self.holder is None and self.waiting[0] == entry):
                if give_up():
                    self.waiting.remove(entry)
                    heapq.heapify(self.waiting)
                    self.cv.notify_all()
                    return False
                self.cv.wait(1.0)
            heapq.heappop(self.waiting)
            self.holder = n
            return True

    def release(self, n):
        with self.cv:
            if self.holder == n:
                self.holder = None
                self.cv.notify_all()


CLOUD = CloudQueue()
# What a boot shows on the serial output, by firmware.
BOOTS = re.compile(r"(?P<sonoff>\[BUILD TIME\]|colinkTcp|tmp ip = )|(?P<bridge>s60_bridge)|"
                   r"(?P<tasmota>Project tasmota)|(?P<esphome>\[I\]\[app)")


# The tool about to need the cloud: the eWeLink steps (A1, A2, C2) starting, or
# the installer about to be sent again to a plug back on Sonoff's firmware.
INTENT = re.compile(r"\] (?:A1|A2|C2)  |Sending the installer again")
# Sonoff's firmware, given its internet back, has taken up to two minutes to
# reach the cloud again; then the send.
HOLD_AT_MOST = 240.0


class PlugMonitor:
    """The plug's way to the cloud, for one scenario: shut, but for a turn
    (CLOUD) each time the tool is about to need it - from then until the plug
    is downloading the installer, starts something but Sonoff's firmware, the
    run ends, or HOLD_AT_MOST.  Sonoff's firmware shows itself on the network
    with its internet shut, so the tool finds it, and asks."""

    def __init__(self, scenario):
        self.sc, self.n, self.state = scenario, scenario.n, scenario.state
        self.stop = threading.Event()
        self.turns = 0

    def start(self):
        threading.Thread(target=self.watch, daemon=True).start()
        return self

    def downloading(self):
        """The plug connected to this computer for the installer - seen at once
        in the socket table, where the run's log has it only after the send."""
        found = sh("docker", "exec", name(self.n), "ss", "-tn", "state", "established",
                   "( sport = :8088 or sport = :8089 )")
        return "192.168.60.50" in found.stdout

    def run_text(self):
        logs = sorted((self.state / "work/captures/plugs").glob("*/runs/*/run.log"),
                      key=lambda p: p.stat().st_mtime)
        return "".join(p.read_text(errors="replace") for p in logs)

    def live_run(self):
        """(which run, its text) for the run going on now, or None: only its own
        lines count, not a saved plug's runs, nor one that has ended."""
        text = self.run_text()
        at = text.rfind("=== run started")
        if at < 0 or "=== run ended ===" in text[at:]:
            return None
        # Which run: its own first line, with the time it started.
        line = text[text.rfind("\n", 0, at) + 1:text.find("\n", at)]
        return line, text[at:]

    def watch(self):
        handled = {}  # run -> how many of its intents have had their turn
        # What the logs say already is not this scenario's to act on - a run
        # left unended by a stopped matrix, say.
        live = self.live_run()
        if live:
            handled[live[0]] = len(INTENT.findall(live[1]))
        while not self.stop.is_set():
            plug_internet(self.n, False)  # a container started since has it open
            live = self.live_run()
            if live:
                run, text = live
                intents = list(INTENT.finditer(text))
                if len(intents) > handled.get(run, 0):
                    handled[run] = len(intents)
                    self.turns += 1
                    # A send again is a plug part way through its run: it goes first.
                    again = "again" in intents[-1].group(0)
                    priority = CLOUD.FALLEN if again else CLOUD.FRESH
                    if CLOUD.acquire(self.n, priority, self.stop.is_set):
                        self.hold("fallen back" if again else "fresh")
                    live = self.live_run()
                    if live and live[0] == run:
                        handled[run] = len(INTENT.findall(live[1]))
            self.stop.wait(1.0)

    def hold(self, why):
        uart = self.state / "logs/uart.log"
        pos = uart.stat().st_size if uart.exists() else 0
        served = self.run_text().count("Served /user1.bin")
        ended = self.run_text().count("=== run ended ===")
        plug_internet(self.n, True)
        self.sc.say(f"CLOUD: turn taken ({why})")
        since = time.time()
        try:
            while not self.stop.is_set() and time.time() - since < HOLD_AT_MOST:
                if self.downloading():
                    break  # the plug has the command: the cloud's part is done
                text = self.run_text()
                if text.count("Served /user1.bin") > served or text.count("=== run ended ===") > ended:
                    break  # downloading the installer, or the run is over
                with open(uart, errors="replace") as f:
                    f.seek(pos); boots = f.read(); pos = f.tell()
                if any(m.lastgroup != "sonoff" for m in BOOTS.finditer(boots)):
                    break
                self.stop.wait(0.5)
        finally:
            plug_internet(self.n, False)
            CLOUD.release(self.n)
            self.sc.say(f"CLOUD: turn given up after {time.time() - since:.0f}s")


def name(n):
    return f"s60-emu-{n}"


def sh(*args, timeout=None):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


class Scenario:
    def __init__(self, n, sc, log):
        self.n, self.sc, self.log = n, sc, log
        self.state = EMU / f"state-{n}"
        self.fired = None

    def say(self, text):
        with open(self.log, "a") as f:
            f.write(f"{time.strftime('%T')} {text}\n")

    def run_tool(self, extra=(), source=None, fresh=False):
        args = [str(REPO / "tools/emulator/unattended-test.sh"), f"--instance={self.n}",
                f"--plug={MAC}", "--keep"]
        if fresh:
            args.append("--fresh")
        if source:
            args.append(f"--from={source}")
        route_target = self.target
        args += ["--", "--firmware", route_target, *extra]
        try:
            with open(self.log, "a") as out:
                out.write(f"--- {' '.join(args[1:])}\n")
                out.flush()
                code = subprocess.call(args, stdout=out, stderr=subprocess.STDOUT, timeout=3000)
        finally:
            pass
        return code

    def run_log(self, since):
        logs = [p for p in (self.state / "work/captures/plugs").glob("*/runs/*/run.log")
                if p.stat().st_mtime >= since]
        return max(logs, key=lambda p: p.stat().st_mtime) if logs else None

    def inject(self, since, delay, stop):
        pattern = re.compile(rf"\] {self.sc['step']}  ")
        while not stop.is_set():
            log = self.run_log(since)
            if log and pattern.search(log.read_text(errors="replace")):
                break
            time.sleep(0.3)
        else:
            return
        time.sleep(delay)
        if stop.is_set():
            return
        fault, arg = self.sc["fault"], self.sc["arg"]
        e = name(self.n)
        self.fired = time.time()
        self.say(f"FAULT {fault} {arg} at {self.sc['step']} +{delay:.1f}s")
        if fault == "outage":
            sh("docker", "exec", e, PLUG_CMD, "router", "off")
            time.sleep(arg)
            sh("docker", "exec", e, PLUG_CMD, "router", "on")
        elif fault == "plugoff":
            sh("docker", "exec", e, PLUG_CMD, "off")
            time.sleep(arg)
            sh("docker", "exec", e, PLUG_CMD, "on")
        elif fault == "crash":
            sh("docker", "exec", e, "pkill", "-KILL", "-f", "serve_safeboot_migration.py")
            sh("docker", "exec", e, "pkill", "-KILL", "-f", "serve_tasmota_ota.py")
            sh("docker", "exec", e, "pkill", "-KILL", "-f", "tools/s60_autoflash.py")
        elif fault == "cancel":
            # Stopping on purpose: Ctrl-C twice, as the first only asks.
            sh("docker", "exec", e, "pkill", "-INT", "-f", "tools/s60_autoflash.py")
            time.sleep(1)
            sh("docker", "exec", e, "pkill", "-INT", "-f", "tools/s60_autoflash.py")
        elif fault == "ctrlc":
            # Once, by mistake: the run asks at the end of the stage, and carries on.
            sh("docker", "exec", e, "pkill", "-INT", "-f", "tools/s60_autoflash.py")
        self.say(f"FAULT done")

    def screen_result(self):
        text = Path(self.log).read_text(errors="replace")
        runs = text.split("--- ")[-1]
        clean = re.sub(r"\x1b\[[0-9;]*m", "", runs)
        if "there is nothing to convert" in clean:
            return "Converted (already: nothing to convert)"
        found = re.findall(r"Result:\s+(.*)", clean)
        return found[-1].strip() if found else "no report"

    def go(self):
        monitor = PlugMonitor(self).start()
        try:
            return self.go_watched()
        finally:
            monitor.stop.set()
            plug_internet(self.n, False)
            CLOUD.release(self.n)

    def go_watched(self):
        sc = self.sc
        route = sc["route"]
        if sc["step"] == "C2":
            source, self.target = None, C2_ROUTES[route]
            steps = {"C2": 20}
        else:
            source, self.target, steps = ROUTES[route]
        delay = max(0.3, 0.4 * steps[sc["step"]])
        stop = threading.Event()
        since = time.time()
        injector = threading.Thread(target=self.inject, args=(since, delay, stop), daemon=True)
        injector.start()
        results = []
        code = self.run_tool(source=source, fresh=source is None)
        results.append((code, self.screen_result()))
        stop.set()
        if self.fired is None:
            return self.finish(results, "NOT-FIRED")
        # After a crash, the computer is back after a while; the plug carries on alone.
        if sc["fault"] == "crash":
            time.sleep(sc["arg"])
        extra = ["--new"] if (sc["fault"], sc["arg"]) == ("cancel", "new") else []
        while "Converted" not in results[-1][1] and len(results) < MAX_RUNS:
            # As a person would: switched off and on, where the run said to.
            said = re.sub(r"\s+", " ", Path(self.log).read_text(errors="replace").split("--- ")[-1])
            if "switch the plug off and on" in said or "switch it off and on" in said:
                self.say("following the run's advice: the plug switched off and on")
                sh("docker", "exec", name(self.n), PLUG_CMD, "restart")
                time.sleep(5)
            code = self.run_tool(extra)
            results.append((code, self.screen_result()))
            extra = []
        return self.finish(results, None)

    def finish(self, results, special):
        converted = "Converted" in results[-1][1]
        bricked = False
        if not converted:
            uart = self.state / "logs/uart.log"
            tail = uart.read_text(errors="replace")[-20000:] if uart.exists() else ""
            bricked = "invalid header" in tail or "No bootable app" in tail or "ets_main.c" in tail
        verdict = special or ("PASS" if converted else "BRICK" if bricked else
                              "EMULATOR-LIMIT" if (self.sc["fault"] == "outage" and (
                                  (self.sc["arg"] > 90 and (self.sc["route"], self.sc["step"]) in ESPHOME_RUNNING)
                                  or (self.sc["arg"] > BRIDGE_STA_WINDOW
                                      and (self.sc["route"], self.sc["step"]) in BRIDGE_RUNNING)))
                              else "FAIL")
        row = {**self.sc, "instance": self.n, "verdict": verdict, "runs": len(results),
               "results": [r for _, r in results], "codes": [c for c, _ in results],
               "log": str(self.log)}
        with open(RESULTS, "a") as f:
            f.write(json.dumps(row) + "\n")
        if verdict not in ("PASS",):
            # Keep what a failure leaves, before the next scenario replaces it.
            keep = LOGS / (self.sc["id"].replace(">", "-to-").replace(":", "_") + "-evidence")
            shutil.rmtree(keep, ignore_errors=True)
            shutil.copytree(self.state / "work/captures/plugs", keep, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("flash.bin", "efuse.bin", "*.bin"))
            uart = self.state / "logs/uart.log"
            if uart.exists():
                (keep / "uart-tail.log").write_text(uart.read_text(errors="replace")[-300_000:])
        sh("docker", "stop", name(self.n), timeout=120)
        return row


def done_ids():
    if not RESULTS.exists():
        return set()
    return {json.loads(line)["id"] for line in RESULTS.read_text().splitlines() if line.strip()}


def worker(n, todo, lock):
    while True:
        try:
            sc = todo.get_nowait()
        except queue.Empty:
            return
        # Leave memory for the machine: wait while it is short.
        while int(re.search(r"MemAvailable:\s+(\d+)", Path("/proc/meminfo").read_text()).group(1)) < 350_000:
            time.sleep(10)
        log = LOGS / (sc["id"].replace(">", "-to-").replace(":", "_") + ".log")
        log.unlink(missing_ok=True)
        try:
            row = Scenario(n, sc, log).go()
        except Exception as exc:  # noqa: BLE001 - recorded, and the next one goes on
            row = {**sc, "instance": n, "verdict": "HARNESS-ERROR", "error": repr(exc)}
            with open(RESULTS, "a") as f:
                f.write(json.dumps(row) + "\n")
            sh("docker", "stop", name(n), timeout=120)
        with lock:
            print(f"{time.strftime('%T')} [{n}] {row['verdict']:14} {sc['id']}", flush=True)


def rerun(k):
    """Every scenario whose latest result is not a pass, again, on k emulators."""
    latest = {}
    for line in RESULTS.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            latest[row["id"]] = row
    redo = [row for row in latest.values() if row["verdict"] != "PASS"]
    todo, c2 = queue.Queue(), queue.Queue()
    for row in redo:
        sc = {key: row[key] for key in ("id", "route", "step", "fault", "arg")}
        (c2 if sc["step"] == "C2" else todo).put(sc)
    print(f"rerunning {todo.qsize()} on {k} emulators and {c2.qsize()} C2 on one", flush=True)
    lock = threading.Lock()
    threads = [threading.Thread(target=worker, args=(n, todo, lock)) for n in range(2, k + 2)]
    threads.append(threading.Thread(target=worker, args=(1, c2, lock)))
    for t in threads:
        t.start()
        time.sleep(20)
    for t in threads:
        t.join()
    report()


# The shortest and longest of each kind of fault, during the backup and D1 in
# the Sonoff directions: where a plug goes back to Sonoff's firmware part way.
SUBSET_FAULTS = [("outage", 1), ("outage", 30), ("plugoff", 1), ("plugoff", 180),
                 ("crash", 1), ("crash", 180), ("cancel", "continue"), ("cancel", "new")]


def latest_verdict(sid):
    found = None
    if RESULTS.exists():
        for line in RESULTS.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                if row["id"] == sid:
                    found = row["verdict"]
    return found


def subset(k):
    """The C3 and D1 subset, on k emulators taking turns at the cloud."""
    todo = queue.Queue()
    for route in ("sonoff>tasmota", "sonoff>esphome"):
        for step in ("C3", "D1"):
            for fault, arg in SUBSET_FAULTS:
                sid = f"{route}:{step}:{fault}-{arg}"
                if latest_verdict(sid) == "PASS":
                    continue
                todo.put({"id": sid, "route": route, "step": step, "fault": fault, "arg": arg})
    print(f"subset: {todo.qsize()} scenarios on {k} emulators", flush=True)
    lock = threading.Lock()
    threads = [threading.Thread(target=worker, args=(n, todo, lock)) for n in range(2, k + 2)]
    for t in threads:
        t.start()
        time.sleep(20)
    for t in threads:
        t.join()


def run(k):
    LOGS.mkdir(parents=True, exist_ok=True)
    done = done_ids()
    todo, c2 = queue.Queue(), queue.Queue()
    for sc in scenarios():
        if sc["id"] not in done:
            todo.put(sc)
    for sc in c2_scenarios():
        if sc["id"] not in done:
            c2.put(sc)
    print(f"{todo.qsize()} scenarios on {k} emulators, {c2.qsize()} C2 scenarios on one", flush=True)
    lock = threading.Lock()
    threads = [threading.Thread(target=worker, args=(n, todo, lock)) for n in range(2, k + 2)]
    threads.append(threading.Thread(target=worker, args=(1, c2, lock)))
    for t in threads:
        t.start()
        time.sleep(20)  # staggered: start-ups are the busiest moment
    for t in threads:
        t.join()
    report()


def prep():
    """The starting plugs, saved: one converted to each firmware, and a stock
    plug stopped just after C2 (the bridge on, before the backup) for each."""
    (EMU / "matrix").mkdir(parents=True, exist_ok=True)
    for target in ("tasmota", "esphome"):
        with open(EMU / "matrix" / f"prep-{target}.log", "w") as out:
            subprocess.call([str(REPO / "tools/emulator/unattended-test.sh"), "--instance=1", "--fresh",
                             f"--plug={MAC}", f"--save=matrix-{target}", "--", "--firmware", target],
                            stdout=out, stderr=subprocess.STDOUT)
        print(f"matrix-{target}", "saved" if (EMU / "saved" / f"matrix-{target}" / "flash.bin").exists() else "MISSING")
    for target in ("tasmota", "esphome"):
        save = f"matrix-stockC2-{target}"
        log = EMU / "matrix" / f"prep-c2-{target}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        state = EMU / "state-1"
        since = time.time()

        def cancel_after_c2():
            while True:
                logs = [p for p in (state / "work/captures/plugs").glob("*/runs/*/run.log")
                        if p.stat().st_mtime >= since]
                if logs and re.search(r"\] C2 done", max(logs, key=lambda p: p.stat().st_mtime).read_text()):
                    sh("docker", "exec", name(1), "pkill", "-INT", "-f", "tools/s60_autoflash.py")
                    return
                time.sleep(0.3)

        threading.Thread(target=cancel_after_c2, daemon=True).start()
        with open(log, "w") as out:
            subprocess.call([str(REPO / "tools/emulator/unattended-test.sh"), "--instance=1", "--fresh",
                             f"--plug={MAC}", f"--save={save}", "--", "--firmware", target],
                            stdout=out, stderr=subprocess.STDOUT)
        print(save, "saved" if (EMU / "saved" / save / "flash.bin").exists() else "MISSING")


def report():
    rows = [json.loads(l) for l in RESULTS.read_text().splitlines() if l.strip()] if RESULTS.exists() else []
    latest = {}
    for row in rows:
        latest[row["id"]] = row
    from collections import Counter
    print(Counter(r["verdict"] for r in latest.values()))
    for r in latest.values():
        if r["verdict"] not in ("PASS",):
            print(f"  {r['verdict']:14} {r['id']}  runs={r.get('runs')}  {r.get('results', r.get('error'))}")


def main():
    import argparse
    global MAC
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plug", help="the backed-up plug's MAC, as emu.sh --plug takes it")
    parser.add_argument("command", choices=["prep", "run", "rerun", "subset", "report"])
    parser.add_argument("emulators", nargs="?", type=int, default=5)
    args = parser.parse_args()
    if args.command != "report" and not args.plug:
        parser.error("--plug is needed: the backed-up plug the emulators run copies of")
    MAC = args.plug
    {"prep": prep, "run": lambda: run(args.emulators), "rerun": lambda: rerun(args.emulators),
     "subset": lambda: subset(args.emulators), "report": report}[args.command]()


if __name__ == "__main__":
    main()
