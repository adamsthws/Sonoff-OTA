#!/usr/bin/env python3
"""The fault suite: every direction, every step that writes to or waits on the
plug, every fault - on emulators side by side, on one machine or several
(docs/testing.md).

    fault_suite.py --plug MAC prep        the starting plugs (needs the cloud)
    fault_suite.py list                   every scenario, and whether it needs the cloud
    fault_suite.py --plug MAC batch [FILE]
                                          the scenarios in FILE (one id a line; every
                                          scenario without), skipping those passed
    fault_suite.py report                 the latest result of each

Add --stub-cloud to prep/batch to run against a local eWeLink stub per emulator
(emu.sh --stub-cloud): fully offline, no cloud lock, every scenario in parallel
on S60_STUB_WORKERS emulators (from S60_FIRST_INSTANCE, default 1). Without it
the real cloud is used and the cloud scenarios take turns, as below.

Each scenario: a conversion from a saved plug; when the step under test has
been going for a share of its usual time (40%, or 10% and 90% at the riskiest
steps), the fault; then the tool run again, as a person would - switching the
plug off and on, or joining its own network, where the run says to - until
the plug is converted, or four runs fail. Every scenario also checks the fault
landed while its step was under way (MISTIMED if not), and that the run left
no firewall rule and no helper behind.

How many at once, from the environment:
    S60_CLOUD_WORKERS  emulators for the scenarios that need eWeLink (from
                       instance 1); they take turns at the cloud
    S60_CLOUD_MOST     try up to this many, one more at a time while they pass
    S60_CLOUD_BACK_OFF=1  start at S60_CLOUD_WORKERS and drop one at a time
                       whenever two of the last four fail
    S60_CLOUD_PAUSE_UP=1  start at S60_CLOUD_COOL_DOWN and add 5 s whenever two
                       of the last four fail with the cloud to blame
    S60_CLOUD_COOL_DOWN  seconds between two plugs' turns at the cloud (5)
    S60_ANY_WORKERS    emulators for the rest, after those
    S60_NO_CLOUD=1     a machine whose plugs never reach the internet: only
                       the scenarios that do not need it
    S60_SUITE_DIR      where results go (captures/emulation/suite; with
                       --stub-cloud, captures/emulation/suite-stub)

Results: results.jsonl, a log per scenario, and what a failure leaves (its
run records and the plug's serial output).
"""
import heapq
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EMU = REPO / "captures/emulation"
OUT = Path(os.environ.get("S60_SUITE_DIR", EMU / "suite"))
RESULTS = OUT / "results.jsonl"
LOGS = OUT / "logs"
# A machine whose plugs never get a turn at the cloud.
NO_CLOUD = os.environ.get("S60_NO_CLOUD") == "1"
# Each emulator serves its own local eWeLink stub (emu.sh --stub-cloud): the
# plugs never share a cloud, so nothing is serialised and every scenario can run
# in parallel. Set by --stub-cloud; see docs/cloud-stub.md.
STUB_CLOUD = os.environ.get("S60_STUB_CLOUD") == "1"


def saved(name):
    """A saved starting plug's name. A stub run's are kept apart from the real
    cloud's: their flash is the other kind (emu.sh), so each would be refused,
    or replaced, by a batch of the other."""
    return f"stub-{name}" if STUB_CLOUD else name


MAC = None  # --plug: the backed-up plug the emulators run copies of
PLUG_CMD = "/opt/s60emu/bin/plug"
MAX_RUNS = 4
TOOL = "tools/s60_autoflash.py"
HELPERS = ("serve_safeboot_migration.py", "serve_tasmota_ota.py")

# Each direction: the saved plug it starts from, its target, and the steps that
# write to or wait on the plug, with how long each usually takes (emulator).
ROUTES = {
    "tasmota>esphome": ("suite-tasmota", "esphome", {"L1": 40, "M1": 8, "I2": 5}),
    "tasmota>sonoff": ("suite-tasmota", "sonoff",
                       {"L1": 40, "X1": 12, "X2": 25, "D4": 31, "D5": 2, "R1": 57, "R2": 2}),
    "esphome>tasmota": ("suite-esphome", "tasmota",
                        {"X1": 12, "X2": 25, "D4": 31, "D5": 2, "E1": 2, "E2": 29, "E3": 5,
                         "F2": 35, "F4": 31, "G1": 33, "H1": 2}),
    "esphome>sonoff": ("suite-esphome", "sonoff",
                       {"X1": 12, "X2": 25, "D4": 31, "D5": 2, "R1": 57, "R2": 2}),
    "sonoff>tasmota": ("suite-stockC2-tasmota", "tasmota",
                       {"C3": 14, "C3fb": 14, "D1": 18, "D4": 31, "D5": 2, "E1": 2, "E2": 29,
                        "E3": 5, "F2": 35, "F4": 31, "G1": 33, "H1": 2}),
    "sonoff>esphome": ("suite-stockC2-esphome", "esphome",
                       {"C3": 14, "C3fb": 14, "D1": 18, "D4": 31, "D5": 2, "I1": 36, "I2": 5}),
}
# Past D1 a stock route needs the cloud no more: it starts from a plug saved
# there (prep), on the interim Tasmota, so any machine can run it.
AFTER_D1 = {"sonoff>tasmota": "suite-afterD1-tasmota", "sonoff>esphome": "suite-afterD1-esphome"}
CLOUD_STEPS = {"C2", "C3", "C3fb", "D1"}
# C2 goes through eWeLink: from a fresh stock plug.
C2_ROUTES = {"sonoff>tasmota": "tasmota", "sonoff>esphome": "esphome"}
OTHER = {"tasmota": "esphome", "esphome": "tasmota"}

# Where a step's work starts, if not at its own line, and how long it lasts from
# there. A run from a saved plug finds it back on Sonoff's firmware at C3 (the
# emulator starting is the plug switched on) and sends the installer again
# first: the backup itself starts at its own line. C3fb is the fault in that
# finding and sending again, at C3's own line.
STARTS = {"C3": (r"Backing up the plug's Sonoff firmware", 12), "C3fb": (r"\] C3  ", 14)}
STEP_OF = {"C3fb": "C3"}  # the tool's own name for a step

CORE = ([("outage", s) for s in (1, 5, 15, 30, 60, 120, 180)]
        + [("plugoff", s) for s in (1, 5, 30, 180)]
        + [("crash", s) for s in (1, 60, 180)]
        + [("cancel", "continue"), ("cancel", "new"), ("ctrlc", "once")])
NEW = [("hangup", "continue"), ("sleep", 120), ("sleep", 600), ("weak", 60),
       ("pcoff", 30), ("pcoff", 120), ("corrupt", "once"), ("ctrlc", "countdown"),
       ("ctrlc", "apart"), ("second", "run")]
# Where a firmware raises its own network when it loses the home one: the
# installer (C3, D1: after 45 s), the mover and ESPHome (L1, M1, X1, I2: after
# 1.5 to 2 minutes). There the person joins it before being asked, and the run
# must carry on by itself through it.
FALLBACK_STEPS = {"C3", "D1", "L1", "M1", "X1", "I2"}
# Faults at 10% and 90% of the riskiest steps, and two in a row there.
CRITICAL = {"D4", "E3", "F2", "F4", "R1", "X2"}
TIMING_FAULTS = [("plugoff", 1), ("outage", 30), ("crash", 1)]
DOUBLE_FAULTS = [("double", "plugoff-1"), ("double", "outage-30")]


def sid(route, step, fault, arg, at=40):
    return f"{route}:{step}{'' if at == 40 else f'@{at}'}:{fault}-{arg}"


def scenarios():
    for route, (_, target, steps) in ROUTES.items():
        for step in steps:
            faults = CORE + NEW + ([("cancel", "switch")] if target in OTHER else [])
            if step in ("C3fb",):
                faults = CORE
            for fault, arg in faults:
                yield dict(id=sid(route, step, fault, arg), route=route, step=step, fault=fault, arg=arg, at=40)
            if step in CRITICAL:
                for at in (10, 90):
                    for fault, arg in TIMING_FAULTS:
                        yield dict(id=sid(route, step, fault, arg, at), route=route, step=step,
                                   fault=fault, arg=arg, at=at)
                for fault, arg in DOUBLE_FAULTS:
                    yield dict(id=sid(route, step, fault, arg), route=route, step=step, fault=fault, arg=arg, at=40)
            if step == "C3":
                yield dict(id=sid(route, step, "diskfull", "once"), route=route, step=step,
                           fault="diskfull", arg="once", at=40)
            if step in FALLBACK_STEPS:
                yield dict(id=sid(route, step, "fallback", 150), route=route, step=step,
                           fault="fallback", arg=150, at=40)
    for route, target in C2_ROUTES.items():
        for fault, arg in CORE + NEW + [("cancel", "switch"), ("token", "login")]:
            yield dict(id=sid(route, "C2", fault, arg), route=route, step="C2", fault=fault, arg=arg, at=40)


def needs_cloud(sc):
    return sc["route"].startswith("sonoff>") and sc["step"] in CLOUD_STEPS


# -- the cloud: one plug at a time ----------------------------------------------
# Shut: the plug's TCP to the internet answered with a reset, and eWeLink's to
# it, so each end lets go of their connection at once and the next turn starts
# clean. Only dropped, eWeLink kept the dead connection a while, and an update
# command sent in a turn soon after could go down it, never to arrive. The
# rest of the plug's way out is dropped.
SHUT = [["FORWARD", "-s", "192.168.60.50", "-o", "eth0", "-j", "DROP"],
        ["FORWARD", "-s", "192.168.60.50", "-o", "eth0", "-p", "tcp", "-j", "REJECT",
         "--reject-with", "tcp-reset"],
        ["FORWARD", "-d", "192.168.60.50", "-i", "eth0", "-p", "tcp", "-j", "REJECT",
         "--reject-with", "tcp-reset"]]


def plug_internet(n, on):
    """The plug's own way to the internet (eWeLink), on or off: off for every
    plug but the one whose turn it is, as every emulated plug is the same one."""
    if on:
        script = "; ".join(f"while iptables -D {' '.join(rule)} 2>/dev/null; do :; done" for rule in SHUT)
    else:
        script = "; ".join(f"iptables -C {' '.join(rule)} 2>/dev/null || "
                           f"iptables -I {rule[0]} 1 {' '.join(rule[1:])}" for rule in SHUT)
    sh("docker", "exec", name(n), "sh", "-c", script)


class CloudQueue:
    """Turns at the cloud, one plug at a time, by how long the run can wait:
    finding the plug in eWeLink (A1, A2: about a minute) first, then a plug
    that went back to Sonoff's firmware part way (FALLEN: three minutes), then
    the rest (FRESH), and last a C2 scenario's turn before its run starts
    (STARTING), which can wait for ever."""
    FINDING, FALLEN, FRESH, STARTING = -1, 0, 1, 2  # STARTING: a scenario not under way yet

    # Every emulated plug is the same eWeLink device: the last one's connection,
    # cut on its side, stays open at eWeLink a while, and the next to connect
    # is not seen online. A pause between turns lets eWeLink let go of it.
    COOL_DOWN = float(os.environ.get("S60_CLOUD_COOL_DOWN", "5"))

    def __init__(self):
        self.cv = threading.Condition()
        self.holder = None
        self.waiting = []
        self.tickets = 0
        self.released = 0.0

    def acquire(self, n, priority, give_up):
        if STUB_CLOUD:
            return True  # own stub per instance: no shared cloud to take turns at
        with self.cv:
            self.tickets += 1
            entry = (priority, self.tickets, n)
            heapq.heappush(self.waiting, entry)
            while not (self.holder is None and self.waiting[0] == entry
                       and time.monotonic() - self.released >= TUNER.cool_down):
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
        if STUB_CLOUD:
            return
        with self.cv:
            if self.holder == n:
                self.holder = None
                self.released = time.monotonic()
                self.cv.notify_all()


CLOUD = CloudQueue()


class Tuner:
    """How many cloud scenarios at once, and the pause between turns: raised
    (then the pause lowered) a step after every WINDOW cloud scenarios with no
    trouble, and the last step taken back at the first trouble - then held.
    Trouble: a cloud scenario that did not pass with the cloud to blame -
    eWeLink saying the plug is offline or the login gone, or a turn held to
    its limit for nothing."""
    WINDOW = 4
    NOT_THE_CLOUD = re.compile(r"stopped part way|did not request every firmware byte")
    TROUBLE = re.compile(r"offline in eWeLink|eWeLink shows it offline|access token|"
                         r"turn given up after 2[34]\ds")

    def __init__(self):
        self.workers = int(os.environ.get("S60_CLOUD_WORKERS", "0"))
        self.most = int(os.environ.get("S60_CLOUD_MOST", str(self.workers)))
        self.cool_down = float(os.environ.get("S60_CLOUD_COOL_DOWN", "5"))
        self.phase = "workers" if self.most > self.workers else "cool-down"
        if os.environ.get("S60_CLOUD_PAUSE_UP") == "1":
            # From a short pause up: 5 s longer at each trouble the cloud is to
            # blame for, never shorter; how many at once left as it is.
            self.phase = "pause-up"
        elif os.environ.get("S60_CLOUD_BACK_OFF") == "1":
            # From many at once down: one fewer at each trouble, any failure
            # counting (a machine short of memory fails all sorts of ways),
            # until WINDOW pass in a row.
            self.phase = "backing-off"
        self.recent = []
        self.changed = time.time()  # scenarios started before a change say nothing of it
        self.lock = threading.Lock()

    def note(self, text):
        line = f"{time.strftime('%T')} TUNER {text} (workers {self.workers}, pause {self.cool_down:.0f}s)"
        print(line, flush=True)
        with open(OUT / "tuning.log", "a") as f:
            f.write(line + "\n")

    def finished(self, row, log, started):
        """Judged on the cloud scenarios started since the last change, WINDOW
        at a time: two of them failing is trouble (one alone may be anything),
        WINDOW passing in a row a step on."""
        if STUB_CLOUD or not needs_cloud(row) or self.phase == "held" or started < self.changed:
            return
        with self.lock:
            ok = row["verdict"] in ("PASS", "EMULATOR-LIMIT", "NO-TRAFFIC")
            if not ok and self.phase != "backing-off":
                # A failure of the scenario's own - a join, a step - says
                # nothing of how many at once the cloud allows. Nor does a
                # download the plug began and gave up: that is its network, and
                # Sonoff's firmware stays away from eWeLink after one, which
                # would read as the cloud's fault.
                try:
                    text = Path(log).read_text(errors="replace")
                    ok = not self.TROUBLE.search(text) or bool(self.NOT_THE_CLOUD.search(text))
                except OSError:
                    pass
            self.recent.append((ok, row["id"], row["verdict"]))
            last = self.recent[-self.WINDOW:]
            failed = [(i, v) for ok, i, v in last if not ok]
            if len(failed) >= 2:
                why = "; ".join(f"{v} in {i}" for i, v in failed)
                if self.phase == "pause-up":
                    self.cool_down += 5
                    self.note(f"cloud trouble ({why}): pause 5s longer")
                elif self.phase == "backing-off":
                    self.workers = max(1, self.workers - 1)
                    self.note(f"trouble ({why}): one fewer at once")
                elif self.phase == "workers":
                    self.workers = max(1, self.workers - 1)
                    self.phase = "cool-down"
                    self.note(f"trouble ({why}): one fewer at once; now the pause")
                else:
                    self.cool_down += 5
                    self.phase = "held"
                    self.note(f"trouble ({why}): pause put back; held from here")
            elif len(last) == self.WINDOW and not failed:
                if self.phase in ("backing-off", "pause-up"):
                    self.note(f"{self.WINDOW} passed in a row as it is")  # still watched
                elif self.phase == "workers":
                    self.workers += 1
                    if self.workers >= self.most:
                        self.phase = "cool-down"
                    self.note(f"{self.WINDOW} passed in a row: one more at once")
                elif self.cool_down >= 5:
                    self.cool_down -= 5
                    self.note(f"{self.WINDOW} passed in a row: pause 5s shorter")
                else:
                    self.phase = "held"
                    self.note("no pause left to take off; held")
            else:
                return
            self.recent = []
            self.changed = time.time()


TUNER = Tuner()
BOOTS = re.compile(r"(?P<sonoff>\[BUILD TIME\]|colinkTcp|tmp ip = )|(?P<bridge>s60_bridge)|"
                   r"(?P<tasmota>Project tasmota)|(?P<esphome>\[I\]\[app)")
INTENT = re.compile(r"\] (?:A1|A2|C2)  |Sending the installer again")
HOLD_AT_MOST = 240.0


def run_logs(state):
    found = []
    for log in (state / "work/captures/plugs").glob("*/runs/*/run.log"):
        try:  # a scenario starting replaces the captures under it
            found.append((log.stat().st_mtime, log, log.read_text(errors="replace")))
        except FileNotFoundError:
            pass
    return sorted(found, key=lambda f: f[0])


class PlugMonitor:
    """The plug's way to the cloud, for one scenario: shut, but for a turn each
    time the tool is about to need it."""

    def __init__(self, scenario):
        self.sc, self.n, self.state = scenario, scenario.n, scenario.state
        self.stop = threading.Event()

    def start(self):
        if not STUB_CLOUD:  # a stub per instance: its plug never needs a turn
            threading.Thread(target=self.watch, daemon=True).start()
        return self

    def run_text(self):
        return "".join(text for _, _, text in run_logs(self.state))

    def live_run(self):
        text = self.run_text()
        at = text.rfind("=== run started")
        if at < 0 or "=== run ended ===" in text[at:]:
            return None
        line = text[text.rfind("\n", 0, at) + 1:text.find("\n", at)]
        return line, text[at:]

    def watch(self):
        # Shut first, and kept shut whatever goes wrong below.
        plug_internet(self.n, False)
        while not self.stop.is_set():
            try:
                self.watching()
                return
            except Exception as exc:  # noqa: BLE001
                plug_internet(self.n, False)
                self.sc.say(f"CLOUD: watcher error {exc!r}; carrying on")
                self.stop.wait(1.0)

    def watching(self):
        handled = {}
        live = self.live_run()
        if live:
            handled[live[0]] = len(INTENT.findall(live[1]))
        if self.sc.sc.get("step") == "C2" and not NO_CLOUD and not getattr(self, "started_online", False):
            # A fresh plug, as a real one is, online from the start: A1 gives it
            # about a minute to show in eWeLink, less than a turn asked for
            # then and Sonoff's firmware reconnecting take. Held through C2.
            self.started_online = True
            if getattr(self, "preheld", False) or CLOUD.acquire(self.n, CLOUD.FINDING, self.stop.is_set):
                # The first run only, as any turn: the next run asks again
                # (A1, at the front of the queue), and the cloud is free for
                # others while this one's fault plays out.
                self.hold("from the start")
            live = self.live_run()
            if live:
                handled[live[0]] = len(INTENT.findall(live[1]))
        while not self.stop.is_set():
            plug_internet(self.n, False)
            live = self.live_run()
            if live:
                run, text = live
                intents = list(INTENT.finditer(text))
                if len(intents) > handled.get(run, 0):
                    handled[run] = len(intents)
                    again = "again" in intents[-1].group(0)
                    finding = re.search(r"\] A[12]  ", intents[-1].group(0))
                    priority = (CLOUD.FINDING if finding else CLOUD.FALLEN if again else CLOUD.FRESH)
                    if NO_CLOUD:
                        self.sc.say("CLOUD: wanted, but this machine gives no turns")
                    elif CLOUD.acquire(self.n, priority, self.stop.is_set):
                        self.hold("finding" if finding else "fallen back" if again else "fresh")
                    live = self.live_run()
                    if live and live[0] == run:
                        handled[run] = len(INTENT.findall(live[1]))
            self.stop.wait(1.0)

    def hold(self, why, limit=HOLD_AT_MOST):
        uart = self.state / "logs/uart.log"
        pos = uart.stat().st_size if uart.exists() else 0
        ended = self.run_text().count("=== run ended ===")
        plug_internet(self.n, True)
        self.sc.say(f"CLOUD: turn taken ({why})")
        since = time.time()
        opened = since
        try:
            while not self.stop.is_set() and time.time() - since < limit:
                if time.time() - opened > 2:
                    # Again now and then: a plug's emulator started in the
                    # turn (a fresh one) starts with its way shut.
                    plug_internet(self.n, True)
                    opened = time.time()
                # Held through the download, not only to its start: Sonoff's
                # firmware that loses eWeLink part way gives the download up.
                # Its end is the installer starting, below.
                now_ended = self.run_text().count("=== run ended ===")
                if now_ended < ended:
                    # A fresh plug's emulator wiped the last scenario's runs
                    # after this turn began: counted from here.
                    ended = now_ended
                elif now_ended > ended:
                    break
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


# -- faults ---------------------------------------------------------------------
# One TCP packet in 20 of 128 bytes or more (one in 2 in a short step, whose
# few packets may be small: a final check's answers), a byte of its data
# changed - at 64, past any header - and its checksum made good again, so only
# the receiver's own checks can tell. By length band (u32 matches masks, not
# ranges): the tap passes uploads as segments of up to 64 KB (GSO), not only
# packets of one frame.
SHORT_STEP = 5  # seconds or less: every other packet
LENGTHS = [("0x0080", "0xff80"), ("0x0100", "0xff00"), ("0x0200", "0xfe00"), ("0x0400", "0xfc00"), ("0x0800", "0xf800"), ("0x1000", "0xf000"),
           ("0x2000", "0xe000"), ("0x4000", "0xc000"), ("0x8000", "0x8000")]


def corrupt_filter(value, mask, every=20):
    return (f"protocol ip u32 match ip protocol 6 0xff match u16 {value} {mask} at 2 "
            f"action gact ok random determ pipe {every} "
            "action pedit ex munge offset 64 u8 set 0x5a pipe action csum ip4h tcp").split()


# The plug unable to reach this computer, though it answers: rare, and until it
# restarts. Looked at when a scenario fails with it (probe, below), before the
# plug is used again - never while a run is using it.
STRANDED = re.compile(r"did not fetch the \w+ instructions|returned HTTP -1")

# What the run tells the person to join, and to go back from (the run's log).
JOIN = re.compile(r"join:\s*\n\[[^\]]*\]\s+(?:network\s+)?(\S+)")
BACK = "Back to your usual Wi-Fi"
# Said while the run waits on, and carries on by itself once it is done.
# (In the run's log each line has its time first.)
SWITCH_NOW = re.compile(r"switch it off and on:\s+(?:\[[\d:]+\]\s+)?at this stage that is safe\. "
                        r"This carries on by itself")


class Scenario:
    def __init__(self, n, sc, log):
        self.n, self.sc, self.log = n, sc, log
        self.state = EMU / f"state-{n}"
        self.fired = None
        self.notes = {}
        self.step_length = 60  # the step under test's usual length (go_watched)

    def say(self, text):
        with open(self.log, "a") as f:
            f.write(f"{time.strftime('%T')} {text}\n")

    def ex(self, *args, timeout=60):
        return sh("docker", "exec", name(self.n), *args, timeout=timeout)

    def run_tool(self, target, extra=(), source=None, fresh=False):
        args = [str(REPO / "tools/emulator/unattended-test.sh"), f"--instance={self.n}",
                f"--plug={MAC}", "--keep"]
        if STUB_CLOUD:
            args.append("--stub-cloud")
        if fresh:
            args.append("--fresh")
        if source:
            args.append(f"--from={saved(source)}")
        args += ["--", "--firmware", target, *extra]
        env = dict(os.environ)
        if (self.sc["fault"], self.sc["arg"]) == ("ctrlc", "countdown"):
            env["S60_EMULATOR_COUNTDOWN"] = "30"
        with open(self.log, "a") as out:
            out.write(f"--- {' '.join(args[1:])}\n")
            out.flush()
            try:
                return subprocess.call(args, stdout=out, stderr=subprocess.STDOUT, timeout=3600, env=env)
            except subprocess.TimeoutExpired:
                out.write("HARNESS: the run took over an hour; stopped\n")
                return 124

    def current_run(self, since, headers):
        """The text of the run going on now: in a log written since `since`,
        after its last header, once there are more headers than `headers`."""
        logs = [(m, p, t) for m, p, t in run_logs(self.state) if m >= since]
        if not logs:
            return None
        text = logs[-1][2]
        if headers is not None and text.count("=== run started") <= headers:
            return None
        return text[text.rfind("=== run started"):]

    def headers_now(self):
        logs = run_logs(self.state)
        return logs[-1][2].count("=== run started") if logs else 0

    def wait_for_step(self, since, headers, stop):
        pattern = re.compile(STARTS.get(self.sc["step"], (rf"\] {self.sc['step']}  ",))[0])
        while not stop.is_set():
            text = self.current_run(since, headers)
            if text and pattern.search(text):
                return True
            time.sleep(0.3)
        return False

    def inject(self, since, headers, delay, stop, fault, arg, first=True):
        if not self.wait_for_step(since, headers, stop):
            return
        time.sleep(delay)
        if stop.is_set():
            return
        step = STEP_OF.get(self.sc["step"], self.sc["step"])
        text = self.current_run(since, headers) or ""
        if re.search(rf"\] {step} done", text):
            self.notes["mistimed"] = True  # the step was over before the fault
        if first:
            self.fired = time.time()
        self.say(f"FAULT {fault} {arg} at {self.sc['step']} +{delay:.1f}s")
        self.fault(fault, arg, since, headers)
        self.say("FAULT done")

    def signal(self, sig, *patterns):
        for pattern in patterns or (TOOL,):
            self.ex("pkill", f"-{sig}", "-f", pattern)

    def fault(self, fault, arg, since, headers):
        p = PLUG_CMD
        if fault == "outage":
            self.ex(p, "router", "off"); time.sleep(arg); self.ex(p, "router", "on")
        elif fault == "plugoff":
            self.ex(p, "off"); time.sleep(arg); self.ex(p, "on")
        elif fault == "crash":
            self.signal("KILL", *HELPERS, TOOL)
        elif fault == "cancel":
            self.signal("INT"); time.sleep(1); self.signal("INT")
        elif fault == "ctrlc" and arg == "once":
            self.signal("INT")
        elif fault == "ctrlc" and arg == "apart":
            self.signal("INT"); time.sleep(11); self.signal("INT")
        elif fault == "ctrlc" and arg == "countdown":
            self.signal("INT")
            deadline = time.time() + 900
            while time.time() < deadline:
                text = self.current_run(since, headers) or ""
                if "You pressed Ctrl-C" in text:
                    time.sleep(3)
                    self.signal("INT")
                    self.notes["countdown_pressed"] = True
                    break
                time.sleep(0.5)
        elif fault == "hangup":
            self.signal("HUP")
        elif fault == "sleep":
            # The computer asleep: everything of the run stopped, and nothing
            # of it on the network, then all back at once.
            self.offline(True)
            for pattern in (TOOL, *HELPERS, "tools/unattended_run.py"):
                self.ex("pkill", "-STOP", "-f", pattern)
            time.sleep(arg)
            for pattern in ("tools/unattended_run.py", *HELPERS, TOOL):
                self.ex("pkill", "-CONT", "-f", pattern)
            self.offline(False)
        elif fault == "fallback":
            # The home network gone long enough for the plug to raise its own;
            # the person joins that as soon as it appears, before being asked.
            self.ex(PLUG_CMD, "router", "off")
            ends = time.time() + arg
            joined = None
            while time.time() < ends:
                network = self.ex("cat", "/run/s60emu/softap").stdout.strip()
                if network and not joined:
                    time.sleep(5)
                    result = self.ex("/opt/s60emu/bin/wifi-root", "join", network, timeout=90)
                    joined = network
                    self.joined = network
                    self.say(f"PERSON: joined {network} before being asked (rc {result.returncode})")
                    self.notes["joined_early"] = network
                time.sleep(2)
            self.ex(PLUG_CMD, "router", "on")
            if not joined:
                self.notes["no_own_network"] = True
        elif fault == "pcoff":
            self.offline(True); time.sleep(arg); self.offline(False)
        elif fault == "weak":
            self.ex("tc", "qdisc", "add", "dev", "tap0", "root", "netem",
                    "delay", "150ms", "100ms", "loss", "10%")
            self.ex("tc", "filter", "add", "dev", "tap0", "ingress", "pref", "6", "protocol", "ip",
                    "u32", "match", "u32", "0", "0", "action", "gact", "ok", "random", "netrand", "drop", "10")
            time.sleep(arg)
            self.ex("tc", "filter", "del", "dev", "tap0", "ingress", "pref", "6")
            self.ex("tc", "qdisc", "del", "dev", "tap0", "root")
        elif fault == "corrupt":
            filters = [(way, str(base + i), value, mask) for way, base in (("egress", 10), ("ingress", 20))
                       for i, (value, mask) in enumerate(LENGTHS)]
            every = 2 if self.step_length <= SHORT_STEP else 20
            # All in one go: one call each would take a second, most of a short step.
            self.ex("sh", "-c", "; ".join(
                "tc filter add dev tap0 " + " ".join([way, "pref", pref, *corrupt_filter(value, mask, every)])
                for way, pref, value, mask in filters))
            # On until a packet has been changed: a step sending little (the
            # bootloader, a plan) may not pass 20 large packets in seconds.
            # Looked at often, so a changed packet or two is all there is.
            edited, deadline = 0, time.time() + 60
            while not edited and time.time() < deadline and \
                    "=== run ended ===" not in (self.current_run(since, headers) or ""):
                time.sleep(0.2)
                edited = sum(int(n) for n in re.findall(r"pedit.*?Sent \d+ bytes (\d+) pkt", self.ex(
                    "sh", "-c", "tc -s filter show dev tap0 egress; tc -s filter show dev tap0 ingress").stdout,
                    re.S))
            self.ex("sh", "-c", "; ".join(f"tc filter del dev tap0 {way} pref {pref}"
                                          for way, pref, _, _ in filters))
            self.notes["corrupted_packets"] = edited
            step = STEP_OF.get(self.sc["step"], self.sc["step"])
            if edited and re.search(rf"\] {step} done", self.current_run(since, headers) or ""):
                self.notes["mistimed"] = True  # changed only once the step was over
        elif fault == "second":
            # A second window, same command, while this one works.
            out = sh("docker", "exec", "-u", f"{os.getuid()}:{os.getgid()}", "-w", "/work",
                     "-e", "HOME=/tmp", "-e", f"S60_EMULATOR_URL=http://localhost:{8080 + self.n}",
                     "-e", "S60_EMULATOR_SUDO_PASSWORD=s60", name(self.n), "timeout", "150",
                     "/opt/venv/bin/python", TOOL, "run", "--unattended", "--env-file", "/tmp/s60-unattended.env",
                     "--firmware", self.target, timeout=200)
            said = re.sub(r"\s+", " ", re.sub(r"\x1b\[[0-9;]*m", "", out.stdout + out.stderr))
            self.notes["second_refused"] = "another run is converting this plug" in said
            self.say("SECOND RUN: " + said[-600:])
        elif fault == "diskfull":
            pid = self.ex("pgrep", "-f", "-o", TOOL).stdout.strip()
            self.ex("prlimit", f"--pid={pid}", "--fsize=1000000")
            self.notes["diskfull_pid"] = pid
            time.sleep(60)
            self.ex("prlimit", f"--pid={pid}", "--fsize=unlimited")
        elif fault == "token":
            out = self.ex("sh", "-c", "cd /work && set -a && . /tmp/s60-unattended.env && "
                          "/opt/venv/bin/python -c 'import os; from tools.get_device_key import login; "
                          "login(os.environ[\"EWELINK_EMAIL\"], os.environ[\"EWELINK_PASSWORD\"], \"+44\", \"eu\"); "
                          "print(\"logged in again\")'", timeout=60)
            self.notes["token"] = (out.stdout + out.stderr).strip()[-200:]

    def offline(self, off):
        """The computer off the network: nothing between it and the plug, and
        no internet - the plug's own way to the internet is left as it is."""
        rules = [("INPUT", "-s", "192.168.60.50"), ("OUTPUT", "-d", "192.168.60.50"),
                 ("OUTPUT", "-o", "eth0")]
        # The router is in the same container, and stays up: its address
        # leases and names still answer the plug.
        router = [("INPUT", "-s", "192.168.60.50", "-p", "udp", "-m", "multiport", "--dports", "53,67"),
                  ("OUTPUT", "-d", "192.168.60.50", "-p", "udp", "-m", "multiport", "--sports", "53,67")]
        for (chain, *match), action in [(r, "DROP") for r in rules] + [(r, "ACCEPT") for r in router]:
            if off:
                self.ex("iptables", "-I", chain, "1", *match, "-j", action)
            else:
                while self.ex("iptables", "-D", chain, *match, "-j", action).returncode == 0:
                    pass

    def screen_result(self):
        text = Path(self.log).read_text(errors="replace")
        runs = text.split("--- ")[-1]
        clean = re.sub(r"\x1b\[[0-9;]*m", "", runs)
        if "there is nothing to convert" in clean:
            return "Converted (already: nothing to convert)"
        found = re.findall(r"Result:\s+(.*)", clean)
        return found[-1].strip() if found else "no report"

    def person(self, stop):
        """As a person at the computer would: join the plug's own network when
        the run says to - again if dropped off it, while the run still asks -
        and go back to the home one when it says that, or when the network it
        is on goes (as a computer's Wi-Fi does by itself)."""
        seen = {}  # log -> (joins, backs) already acted on, or there before
        switched = {}  # log -> power-cycle requests already acted on, or there before
        joined = None
        wanted = None  # (log, the name told) while the run asks for a join
        gone_since = None
        while not stop.is_set():
            joined = joined or getattr(self, "joined", None)
            on_air = self.ex("cat", "/run/s60emu/softap").stdout.strip()
            if joined:
                if on_air:
                    gone_since = None
                elif gone_since is None:
                    gone_since = time.time()
                elif time.time() - gone_since > 5:
                    self.ex("/opt/s60emu/bin/wifi-root", "home", timeout=60)
                    self.say(f"PERSON: {joined} gone; this computer back on the home network by itself")
                    joined = self.joined = None
                    gone_since = None
            for _, log, text in run_logs(self.state):
                if log.stat().st_mtime < self.started:
                    continue
                joins, backs = JOIN.findall(text), text.count(BACK)
                if log not in seen:
                    seen[log] = (len(joins), backs)  # history from before this scenario
                    continue
                done_joins, done_backs = seen[log]
                if len(joins) > done_joins:
                    name = joins[-1].rstrip(",")
                    if not name.startswith("S60-OTA-Bridge"):
                        wanted = (log, name)
                        stop.wait(8)  # a person takes a moment
                if backs > done_backs:
                    wanted = None
                    if joined:
                        self.ex("/opt/s60emu/bin/wifi-root", "home", timeout=60)
                        self.say("PERSON: back on the home network")
                        joined = self.joined = None
                seen[log] = (len(joins), backs)
                asked = len(SWITCH_NOW.findall(text))
                if asked > switched.setdefault(log, asked):
                    switched[log] = asked
                    stop.wait(30)  # a person reads it, and gets up
                    if not stop.is_set():
                        self.ex(PLUG_CMD, "restart")
                        self.say("PERSON: switched the plug off and on, as the run said")
                        self.notes.setdefault("switched", 0)
                        self.notes["switched"] += 1
                if wanted and wanted[0] == log and text.rstrip().endswith("=== run ended ==="):
                    wanted = None  # the run that asked is over
            if wanted and not joined:
                # The network picked from the list once it shows: the run may
                # give a pattern (tasmota-ABCDEF-NNNN) or two names.
                on_air = self.ex("cat", "/run/s60emu/softap").stdout.strip()
                if on_air:
                    result = self.ex("/opt/s60emu/bin/wifi-root", "join", on_air, timeout=90)
                    self.say(f"PERSON: joined {on_air} (as told: {wanted[1]}; rc {result.returncode})")
                    if result.returncode == 0:
                        joined = on_air
                        self.notes.setdefault("joined", []).append(on_air)
                    else:
                        stop.wait(5)
            stop.wait(2)
        if joined:
            self.ex("/opt/s60emu/bin/wifi-root", "home", timeout=60)

    def probe(self):
        """What the network shows while the plug is asked to connect here: does
        its SYN reach this computer, and what comes back."""
        import urllib.parse
        out = []

        def run(*args, timeout=30):
            try:
                done = self.ex(*args, timeout=timeout)
                out.append(f"$ {' '.join(args)}  (exit {done.returncode})\n{done.stdout}{done.stderr}")
            except subprocess.TimeoutExpired:
                out.append(f"$ {' '.join(args)}  (no answer in {timeout}s)")

        capture = subprocess.Popen(["docker", "exec", name(self.n), "timeout", "25", "tcpdump", "-n", "-e",
                                    "-i", "tap0", "-c", "300", "host", "192.168.60.50"],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        time.sleep(2)
        connect = 'Br var c=tcpclient() var ok=c.connect("192.168.60.1",8089) c.close() return ok'
        for command in (connect, "Ping4 192.168.60.1", "Status 0", connect):
            # Tasmota answers only requests with a Referer, as the tool's are.
            run("curl", "-sS", "-m", "15", "-H", "Referer: http://192.168.60.50/",
                "http://192.168.60.50/cm?cmnd=" + urllib.parse.quote(command), timeout=20)
        out.append("$ tcpdump\n" + capture.communicate(timeout=40)[0])
        run("ss", "-tanp")
        run("iptables-save", "-c")
        run("ip", "neigh")
        run("ip", "-s", "link", "show", "tap0")
        LOGS.mkdir(parents=True, exist_ok=True)
        (LOGS / (key(self.sc["id"]) + "-probe.txt")).write_text("\n".join(out))
        self.notes["probed"] = True
        self.say("PROBE: the plug not reaching this computer, looked at")

    def go(self):
        monitor = PlugMonitor(self)
        if self.sc.get("step") == "C2" and not NO_CLOUD and not STUB_CLOUD:
            # The turn before the run, not beside it: A1 looks the plug up in
            # eWeLink within seconds of starting, and finds it offline if it
            # is still waiting for its turn then.
            self.say("CLOUD: waiting for a turn before starting")
            # Last in the queue: a run under way that needs the cloud cannot
            # wait as long as one not started yet can, and this turn lasts.
            CLOUD.acquire(self.n, CLOUD.STARTING, lambda: False)
            monitor.preheld = True
        monitor.start()
        self.started = time.time()
        attending = threading.Event()
        threading.Thread(target=self.person, args=(attending,), daemon=True).start()
        try:
            return self.go_watched()
        finally:
            attending.set()
            monitor.stop.set()
            plug_internet(self.n, False)
            CLOUD.release(self.n)

    def go_watched(self):
        sc = self.sc
        route, step = sc["route"], sc["step"]
        if step == "C2":
            source, self.target, steps = None, C2_ROUTES[route], {"C2": 20}
        else:
            source, self.target, steps = ROUTES[route]
            if route in AFTER_D1 and step not in CLOUD_STEPS:
                source = AFTER_D1[route]
        length = STARTS.get(step, (None, steps[step]))[1]
        self.step_length = length
        delay = max(0.3, sc["at"] / 100 * length)
        if sc["fault"] == "corrupt":
            # From the step's start: its data can be over well before 40% of
            # its usual time (X1's upload), and a short step's in seconds.
            delay = 0.3
        fault, arg = sc["fault"], sc["arg"]
        first_fault = (arg.split("-")[0], int(arg.split("-")[1])) if fault == "double" else (fault, arg)
        stop = threading.Event()
        since = time.time()
        injector = threading.Thread(target=self.inject, args=(since, None, delay, stop, *first_fault),
                                    daemon=True)
        injector.start()
        results = [(self.run_tool(self.target, source=source, fresh=source is None), None)]
        results[-1] = (results[-1][0], self.screen_result())
        stop.set()
        injector.join(120)  # what the fault did is part of the verdict
        if self.fired is None:
            return self.finish(results, "NOT-FIRED")
        if fault == "crash":
            time.sleep(arg)
        target = OTHER.get(self.target, self.target) if (fault, arg) == ("cancel", "switch") else self.target
        extra = ["--new"] if (fault, arg) == ("cancel", "new") else []
        while "Converted" not in results[-1][1] and len(results) < MAX_RUNS:
            said = re.sub(r"\s+", " ", Path(self.log).read_text(errors="replace").split("--- ")[-1])
            if "switch the plug off and on" in said or "switch it off and on" in said:
                self.say("following the run's advice: the plug switched off and on")
                self.ex(PLUG_CMD, "restart")
                time.sleep(5)
            elif "this computer did not join" in said:
                # Asked for the plug's own network, which the person stand-in
                # could not join (it was not there), and nothing else to try.
                self.notes["needs_join"] = True
                break
            stop2 = threading.Event()
            if fault == "double" and len(results) == 1:
                # The same fault again, in the run that recovers from the first.
                threading.Thread(target=self.inject, args=(time.time(), self.headers_now(), delay, stop2,
                                                           *first_fault, False), daemon=True).start()
            results.append((self.run_tool(target, extra), None))
            results[-1] = (results[-1][0], self.screen_result())
            stop2.set()
            extra = []
            said = re.sub(r"\s+", " ", Path(self.log).read_text(errors="replace").split("--- ")[-1])
            if (fault, arg) == ("cancel", "switch") and "cannot switch to" in said:
                # Refused, as it should be: started again for the other one, as it says.
                self.notes["switch_refused"] = True
                extra = ["--new"]
        self.target_final = target
        return self.finish(results, None)

    def backup_whole(self):
        backups = list((self.state / "work/captures/plugs").glob("*/sonoff-backup/flash.bin"))
        return bool(backups) and all(b.stat().st_size == 4 * 1024 * 1024 for b in backups)

    def leftovers(self):
        found = []
        rules = self.ex("ufw", "status").stdout
        if "8088" in rules or "8089" in rules or "S60" in rules:
            found.append("firewall rule")
        if self.ex("pgrep", "-f", "|".join(HELPERS)).stdout.strip():
            found.append("helper running")
        if self.ex("pgrep", "-f", TOOL).stdout.strip():
            found.append("tool running")
        return found

    def finish(self, results, special):
        sc = self.sc
        converted = "Converted" in results[-1][1]
        fault, arg = sc["fault"], sc["arg"]
        left = self.leftovers()
        if not special:
            if self.notes.get("mistimed"):
                special = "MISTIMED"
            elif fault == "corrupt" and not self.notes.get("corrupted_packets"):
                special = "NO-TRAFFIC"
            elif fault == "ctrlc" and arg in ("once", "apart") and converted and len(results) > 1:
                special = "FAIL-STOPPED"  # one Ctrl-C (or two, apart) must not stop the run
            elif fault == "ctrlc" and arg == "countdown" and not self.notes.get("countdown_pressed"):
                special = "NOT-FIRED"
            elif fault == "ctrlc" and arg == "countdown" and len(results) == 1:
                special = "FAIL-NOT-STOPPED"  # Ctrl-C at the question must stop at once
            elif fault == "hangup" and len(results) == 1:
                special = "FAIL-NOT-STOPPED"
            elif fault == "hangup" and "when its window was closed" not in re.sub(
                    r"\s+", " ", "".join(t for _, _, t in run_logs(self.state))):
                special = "FAIL-NO-NOTE"  # the next run must say why the last one stopped
            elif fault == "second" and not self.notes.get("second_refused"):
                special = "FAIL-NOT-REFUSED"
            elif fault == "second" and converted and len(results) > 1:
                special = "FAIL-DISTURBED"  # the refused run must not disturb the first
            elif fault == "diskfull" and not self.backup_whole():
                special = "FAIL-BAD-BACKUP"  # never on without the whole backup
            elif left and converted:
                special = "LEFTOVER"
        bricked = False
        if not converted:
            uart = self.state / "logs/uart.log"
            tail = uart.read_text(errors="replace")[-20000:] if uart.exists() else ""
            bricked = "invalid header" in tail or "No bootable app" in tail or "ets_main.c" in tail
        # Every setup network can be joined (the fork's wifi-to-softap.patch,
        # and the bridge's open since 4.4.3): none is beyond the emulator.
        verdict = special or ("PASS" if converted else "BRICK" if bricked else
                              # Asked for the plug's own network to be joined: right only
                              # where the plug really is on it - each is looked at.
                              "NEEDS-JOIN" if self.notes.get("needs_join") else "FAIL")
        row = {**sc, "instance": self.n, "verdict": verdict, "runs": len(results),
               "results": [r for _, r in results], "codes": [c for c, _ in results],
               "notes": self.notes, "leftovers": left, "log": str(self.log),
               "finished": time.strftime("%Y-%m-%d %H:%M:%S"), "host": os.uname().nodename}
        with open(RESULTS, "a") as f:
            f.write(json.dumps(row) + "\n")
        if verdict != "PASS":
            # This scenario's own screens: the run's log goes back further.
            if STRANDED.search(re.sub(r"\s+", " ", Path(self.log).read_text(errors="replace"))):
                try:
                    self.probe()
                except Exception as exc:  # noqa: BLE001 - a look, never the verdict
                    self.say(f"PROBE: failed: {exc!r}")
            keep = LOGS / (key(sc["id"]) + "-evidence")
            shutil.rmtree(keep, ignore_errors=True)
            shutil.copytree(self.state / "work/captures/plugs", keep, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("*.bin"))
            uart = self.state / "logs/uart.log"
            if uart.exists():
                (keep / "uart-tail.log").write_text(uart.read_text(errors="replace")[-300_000:])
        sh("docker", "stop", name(self.n), timeout=120)
        return row


def key(scenario_id):
    return scenario_id.replace(">", "-to-").replace(":", "_").replace("@", "_at")


def latest():
    rows = {}
    if RESULTS.exists():
        for line in RESULTS.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                rows[row["id"]] = row
    return rows


def worker(n, todo, lock, cloud=False):
    while True:
        # A cloud worker beyond how many the tuner allows now waits its turn.
        while cloud and n > TUNER.workers:
            time.sleep(10)
        try:
            sc = todo.get_nowait()
        except queue.Empty:
            return
        skip = OUT / "skip.txt"
        if skip.exists() and sc["id"] in skip.read_text().split():
            continue
        while int(re.search(r"MemAvailable:\s+(\d+)", Path("/proc/meminfo").read_text()).group(1)) < 350_000:
            time.sleep(10)
        log = LOGS / (key(sc["id"]) + ".log")
        log.unlink(missing_ok=True)
        started = time.time()
        try:
            row = Scenario(n, sc, log).go()
        except Exception as exc:  # noqa: BLE001
            row = {**sc, "instance": n, "verdict": "HARNESS-ERROR", "error": repr(exc), "trace": traceback.format_exc(),
                   "finished": time.strftime("%Y-%m-%d %H:%M:%S")}
            with open(RESULTS, "a") as f:
                f.write(json.dumps(row) + "\n")
            sh("docker", "stop", name(n), timeout=120)
        with lock:
            print(f"{time.strftime('%T')} [{n}] {row['verdict']:16} {sc['id']}", flush=True)
        TUNER.finished(row, log, started)


def batch(ids_file=None):
    """The scenarios in ids_file (every one without) not yet passed here: the
    cloud ones on S60_CLOUD_WORKERS emulators from instance 1, the rest on
    S60_ANY_WORKERS after them."""
    LOGS.mkdir(parents=True, exist_ok=True)
    known = {sc["id"]: sc for sc in scenarios()}
    done = {i for i, row in latest().items() if row["verdict"] in ("PASS", "EMULATOR-LIMIT", "NO-TRAFFIC")}
    cloud, anywhere = queue.Queue(), queue.Queue()
    ids = Path(ids_file).read_text().split() if ids_file else list(known)
    for i in ids:
        if i in done:
            continue
        # With a stub per instance nothing is shared, so every scenario runs in
        # the one parallel pool; otherwise the cloud ones take turns at eWeLink,
        # and a no-cloud machine skips them.
        if STUB_CLOUD:
            anywhere.put(known[i])
        elif not (NO_CLOUD and needs_cloud(known[i])):
            (cloud if needs_cloud(known[i]) else anywhere).put(known[i])
    if STUB_CLOUD:
        nc = 0
        na = int(os.environ.get("S60_STUB_WORKERS", os.environ.get("S60_ANY_WORKERS", "3")))
        first = int(os.environ.get("S60_FIRST_INSTANCE", "1"))
    else:
        nc = int(os.environ.get("S60_CLOUD_WORKERS", "0"))
        na = int(os.environ.get("S60_ANY_WORKERS", "0"))
        first = int(os.environ.get("S60_FIRST_INSTANCE", nc + 1))  # clear of another batch's emulators
    print(f"batch: {cloud.qsize()} cloud on {nc}, {anywhere.qsize()} others on {na}", flush=True)
    lock = threading.Lock()
    threads = [] if STUB_CLOUD else [threading.Thread(target=worker, args=(n, cloud, lock, True))
                                     for n in range(1, max(nc, TUNER.most) + 1)]
    threads += [threading.Thread(target=worker, args=(n, anywhere, lock)) for n in range(first, first + na)]
    for t in threads:
        t.start()
        time.sleep(20)  # staggered: start-ups are the busiest moment
    for t in threads:
        t.join()
    report()


def prep():
    """The starting plugs, saved: one converted to each firmware; a stock plug
    stopped just after C2 (the installer sent, before the backup) for each;
    and one of those carried on as far as D1 (on the interim Tasmota), so the
    steps after D1 need no cloud."""
    n = int(os.environ.get("S60_PREP_INSTANCE", "1"))
    unattended = str(REPO / "tools/emulator/unattended-test.sh")

    def stopped_after(step, sc, since):
        while True:
            if re.search(rf"\] {step} done", sc.current_run(since, None) or ""):
                sc.signal("INT"); time.sleep(0.5); sc.signal("INT")
                return
            time.sleep(0.2)

    def make(save, args, stop_after=None):
        log = OUT / f"prep-{save}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        # A fresh plug has the cloud from the start, as a C2 scenario's does.
        fresh = "--fresh" in args
        sc = Scenario(n, {"id": "prep", "step": "C2" if fresh else "", "fault": "prep", "arg": ""}, log)
        monitor = PlugMonitor(sc).start()
        since = time.time()
        if stop_after:
            threading.Thread(target=stopped_after, args=(stop_after, sc, since), daemon=True).start()
        stub = ["--stub-cloud"] if STUB_CLOUD else []
        with open(log, "w") as out:
            subprocess.call([unattended, f"--instance={n}", f"--plug={MAC}", f"--save={saved(save)}",
                             *stub, *args], stdout=out, stderr=subprocess.STDOUT)
        monitor.stop.set()
        plug_internet(n, False)
        CLOUD.release(n)
        print(saved(save), "saved" if (EMU / "saved" / saved(save) / "flash.bin").exists() else "MISSING",
              flush=True)

    for target in ("tasmota", "esphome"):
        make(ROUTES[f"{target}>sonoff"][0], ["--fresh", "--", "--firmware", target])
        stock = ROUTES[f"sonoff>{target}"][0]
        make(stock, ["--fresh", "--", "--firmware", target], stop_after="C2")
        make(AFTER_D1[f"sonoff>{target}"], [f"--from={saved(stock)}", "--", "--firmware", target],
             stop_after="D1")


def listing():
    for sc in scenarios():
        print(sc["id"], "cloud" if needs_cloud(sc) else "any")


def report():
    from collections import Counter
    rows = latest()
    print(Counter(r["verdict"] for r in rows.values()))
    for r in rows.values():
        if r["verdict"] != "PASS":
            print(f"  {r['verdict']:16} {r['id']}  runs={r.get('runs')}  {r.get('results', r.get('error'))}")


def main():
    import argparse
    global MAC, STUB_CLOUD, OUT, RESULTS, LOGS
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plug", help="the backed-up plug's MAC, as emu.sh --plug takes it")
    parser.add_argument("--stub-cloud", action="store_true",
                        help="each emulator serves its own local eWeLink stub (offline, fully "
                             "parallel, no cloud lock); the default uses the real cloud")
    parser.add_argument("command", choices=["prep", "list", "batch", "report"])
    parser.add_argument("file", nargs="?", help="batch: the scenario ids to run (else all)")
    args = parser.parse_args()
    STUB_CLOUD = STUB_CLOUD or args.stub_cloud
    if STUB_CLOUD and "S60_SUITE_DIR" not in os.environ:
        # Apart from the real cloud's results, whose batch would otherwise
        # take a stub run's passes for its own.
        OUT = EMU / "suite-stub"
        RESULTS, LOGS = OUT / "results.jsonl", OUT / "logs"
    if args.command in ("prep", "batch") and not args.plug:
        parser.error("--plug is needed: the backed-up plug the emulators run copies of")
    MAC = args.plug
    {"prep": prep, "list": listing, "report": report,
     "batch": lambda: batch(args.file)}[args.command]()


if __name__ == "__main__":
    main()
