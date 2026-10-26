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
on AUTOFLASH_STUB_WORKERS emulators (from AUTOFLASH_FIRST_INSTANCE, default 1). Without it
the real cloud is used and the cloud scenarios take turns, as below.

Each scenario: a conversion from a saved plug; when the step under test has
been going for a share of its usual time (40%, or 10% and 90% at the riskiest
steps), the fault; then the tool run again, as a person would - switching the
plug off and on, or joining its own network, where the run says to - until
the plug is converted, or four runs fail. Every scenario also checks the fault
landed while its step was under way (MISTIMED if not), and that the run left
no firewall rule and no helper behind.

How many at once, from the environment:
    AUTOFLASH_CLOUD_WORKERS  emulators for the scenarios that need eWeLink (from
                       instance 1); they take turns at the cloud
    AUTOFLASH_CLOUD_MOST     try up to this many, one more at a time while they pass
    AUTOFLASH_CLOUD_BACK_OFF=1  start at AUTOFLASH_CLOUD_WORKERS and drop one at a time
                       whenever two of the last four fail
    AUTOFLASH_CLOUD_PAUSE_UP=1  start at AUTOFLASH_CLOUD_COOL_DOWN and add 5 s whenever two
                       of the last four fail with the cloud to blame
    AUTOFLASH_CLOUD_COOL_DOWN  seconds between two plugs' turns at the cloud (5)
    AUTOFLASH_ANY_WORKERS    emulators for the rest, after those
    AUTOFLASH_MOST_WORKERS   start up to this many of those instead, of which the
                       number in <AUTOFLASH_SUITE_DIR>/workers take scenarios
                       when that file is there: a batch scaled while it runs
    AUTOFLASH_NO_CLOUD=1     a machine whose plugs never reach the internet: only
                       the scenarios that do not need it
    AUTOFLASH_SUITE_DIR      where results go (captures/emulation/suite; with
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
OUT = Path(os.environ.get("AUTOFLASH_SUITE_DIR", EMU / "suite"))
RESULTS = OUT / "results.jsonl"
LOGS = OUT / "logs"
# A machine whose plugs never get a turn at the cloud.
NO_CLOUD = os.environ.get("AUTOFLASH_NO_CLOUD") == "1"
# Each emulator serves its own local eWeLink stub (emu.sh --stub-cloud): the
# plugs never share a cloud, so nothing is serialised and every scenario can run
# in parallel. Set by --stub-cloud; see docs/cloud-stub.md.
STUB_CLOUD = os.environ.get("AUTOFLASH_STUB_CLOUD") == "1"


def saved(name):
    """A saved starting plug's name. A stub run's are kept apart from the real
    cloud's: their flash is the other kind (emu.sh), so each would be refused,
    or replaced, by a batch of the other. Each plug has its own (a test copy
    under another MAC: tools/emulator/copy_plug.py): one saved from another
    plug - its run records filed under that plug's MAC - is that plug's, and
    this one's goes beside it, its MAC's last four digits after the name."""
    base = f"stub-{name}" if STUB_CLOUD else name
    plugs_saved = EMU / "saved" / base / "captures/plugs"
    owners = {d.name for d in plugs_saved.iterdir() if d.name != "unidentified"} if plugs_saved.is_dir() else set()
    if MAC and owners and MAC.replace(":", "-").lower() not in owners:
        return f"{base}-{MAC.replace(':', '').lower()[-4:]}"
    return base


MAC = None  # --plug: the backed-up plug the emulators run copies of
PLUG_URL = "http://192.168.60.50"
# An MQTT host for a plug set up by its owner (notemplate): nobody's (RFC 5737).
NOTEMPLATE_MQTT_HOST = "192.0.2.10"
APP0_OFFSET = 0xE0000  # Tasmota's own firmware, on its layout (tools/safeboot_migration.py)
PLUG_CMD = "/opt/autoflash-emu/bin/plug"
ESPHOME_OTA_PORT = 3232
MAX_RUNS = 4
TOOL = "tools/autoflash_cli.py"
HELPERS = ("serve_safeboot_migration.py", "serve_tasmota_ota.py")

# Each direction: the saved plug it starts from, its target, and the steps that
# write to or wait on the plug, with how long each usually takes (emulator).
# Each step's usual length in seconds, from passing runs: a fault lands at a
# share of it. F4 is 10 s from 2026-10-21 (one restart; it was 31 s with two).
# The steps that write firmware are shorter from 2.0.2 - each block checked on
# the plug, not sent back: E2, G1 and L1 by 12 s, R1 (two images) by 25 s - so
# a fault at 90% of the old lengths landed in the step after.
ROUTES = {
    "tasmota>esphome": ("suite-tasmota", "esphome", {"L1": 23, "M1": 8, "I2": 5}),
    "tasmota>sonoff": ("suite-tasmota", "sonoff",
                       {"L1": 23, "X1": 12, "X2": 25, "D4": 31, "D5": 2, "R1": 32, "R2": 2}),
    "esphome>tasmota": ("suite-esphome", "tasmota",
                        {"X1": 12, "X2": 25, "D4": 31, "D5": 2, "E1": 2, "E2": 17, "E3": 5,
                         "F2": 35, "F4": 10, "G1": 21, "H1": 2}),
    "esphome>sonoff": ("suite-esphome", "sonoff",
                       {"X1": 12, "X2": 25, "D4": 31, "D5": 2, "R1": 32, "R2": 2}),
    "sonoff>tasmota": ("suite-stockC2-tasmota", "tasmota",
                       {"C3": 14, "C3fb": 14, "D1": 18, "D4": 31, "D5": 2, "E1": 2, "E2": 17,
                        "E3": 5, "F2": 35, "F4": 10, "G1": 21, "H1": 2}),
    "sonoff>esphome": ("suite-stockC2-esphome", "esphome",
                       {"C3": 14, "C3fb": 14, "D1": 18, "D4": 31, "D5": 2, "I1": 30, "I2": 5}),
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
# F4rs is the moment F4's reset restarts the plug and saves the defaults it
# rebuilt, with the plug's name and template put in (one restart): a fault
# there may cut it off before the save, or after.
# F4tp is the restart after F4's fallback sent the template itself (the
# reset's save did not take it): cut off before Tasmota saves, the plug comes
# back without it.
STARTS = {"C3": (r"Backing up the plug's Sonoff firmware", 12), "C3fb": (r"\] C3  ", 14),
          "F4rs": (r"Settings reset to Tasmota's defaults", 3), "F4tp": (r"Template applied as", 2)}
STEP_OF = {"C3fb": "C3", "F4rs": "F4", "F4tp": "F4"}  # the tool's own name for a step

# Each length where it can lead somewhere the others do not (2026-10-21): an
# outage of 5 s is within every timeout of the tool's (8 s and up) and takes
# the plug off the Wi-Fi, so it covers 1 s; 15 s outlasts the 8-10 s ones but
# not the 15-20 s ones. A crash's rerun after 1 s finds the step still going,
# after 180 s finished and any fallback network up; after 60 s - finished, no
# fallback yet - is a place of its own only where there is one (FALLBACK_STEPS).
CORE = ([("outage", s) for s in (5, 15, 30, 60, 120, 180)]
        + [("plugoff", s) for s in (1, 5, 30, 180)]
        + [("crash", s) for s in (1, 180)]
        + [("cancel", "continue"), ("cancel", "new"), ("ctrlc", "once")])
NEW = [("hangup", "continue"), ("sleep", 600), ("weak", 60),
       ("pcoff", 30), ("pcoff", 120), ("corrupt", "once")]
# Steps that send nothing a changed packet could reach: R2 knows Sonoff's
# firmware by its LAN port taking a connection, and reads nothing over it, so
# corrupt-once there only ever came back NO-TRAFFIC.
NOTHING_TO_CORRUPT = {"R2"}
# Where a firmware raises its own network when it loses the home one: the
# installer (C3, D1: after 45 s), the mover and ESPHome (L1, M1, X1, I2: after
# 1.5 to 2 minutes). There the person joins it before being asked, and the run
# must carry on by itself through it.
FALLBACK_STEPS = {"C3", "D1", "L1", "M1", "X1", "I2"}
# Faults at 10% and 90% of the riskiest steps, and two in a row there.
CRITICAL = {"D4", "E3", "F2", "F4", "R1", "X2"}
# The tool's own handling - a second run refused, Ctrl-C twice apart or at its
# question - is the same at every step: tried at the riskiest, and where each
# firmware is first written from (C2, L1, X1). A 2-minute sleep, too, but also
# wherever a fallback network may come up during it.
OWN_HANDLING = [("second", "run"), ("ctrlc", "countdown"), ("ctrlc", "apart")]
OWN_HANDLING_STEPS = CRITICAL | {"C2", "L1", "X1"}
TIMING_FAULTS = [("plugoff", 1), ("outage", 30), ("crash", 1)]
DOUBLE_FAULTS = [("double", "plugoff-1"), ("double", "outage-30")]


def sid(route, step, fault, arg, at=40):
    return f"{route}:{step}{'' if at == 40 else f'@{at}'}:{fault}-{arg}"


def step_faults(step):
    """The one-moment faults tried at `step` (40% of the way through)."""
    faults = list(CORE)
    if STEP_OF.get(step, step) in FALLBACK_STEPS or step == "C2":
        faults += [("crash", 60)]
    if step == "C3fb":
        return faults
    faults += [fault for fault in NEW if not (fault[0] == "corrupt" and step in NOTHING_TO_CORRUPT)]
    if step in OWN_HANDLING_STEPS:
        faults += OWN_HANDLING
    if step in OWN_HANDLING_STEPS or step in FALLBACK_STEPS:
        faults += [("sleep", 120)]
    return faults


def scenarios():
    for route, (_, target, steps) in ROUTES.items():
        for step in steps:
            faults = step_faults(step) + ([("cancel", "switch")] if target in OTHER and step != "C3fb" else [])
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
    for route in C2_ROUTES:
        for fault, arg in step_faults("C2") + [("cancel", "switch"), ("token", "login")]:
            yield dict(id=sid(route, "C2", fault, arg), route=route, step="C2", fault=fault, arg=arg, at=40)
    yield from composite_scenarios()


def composite_scenarios():
    """Faults a moment in a step cannot make alone (docs/testing.md):

    ownnet-cut, ownnet-dropupload, ownnet-dropsetup (at C3): the home Wi-Fi
        gone from the backup, so D1 goes through the installer's own network;
        then the run cut off just after the upload, or this computer back on
        the home network during the upload, or while Tasmota is given the Wi-Fi.
    lostanswer-once (D1): the upload takes, its answer never arrives.
    seven-times (D4, F4, G1): the plug switched off and on 7 times, which
        erases a Tasmota's settings; seven-advised (D1): Tasmota cannot join
        the home Wi-Fi, and that is done when the run advises it.
    nohook-run (F4): Tasmota will not take F4's name and template at the save.
    nohook-cut (F4tp): that, and the plug switched off for a second at 10, 50
        and 90% of the restart after F4 sent the template itself.
    F4rs: a power cut or crash at F4's reset, at 10, 50 and 90% of it.
    legacy-v1.2.0, legacy-v2.0.2 (LEGACY_STEPS): a run of that release
        (legacy_tree) killed at 40% of each of its steps from Sonoff, then
        this checkout's tool, over the same captures, as a person updating
        their checkout would - its bundle and pins that release's; and (step
        "start") a plug that release converted, then taken on by this one.
    blips-3, lossy-phase (BLIP_STEPS): two 3 s outages from 10% to 90% of a
        Berry phase, or one packet in ten lost for all of it: converted with
        no step tried again, or FAIL-RETRIED.
    lostapp-factory: a converted plug's Tasmota written over by an ESPHome
        factory image uploaded through Tasmota's page (go_lostapp), then the
        tool, to Tasmota, ESPHome or Sonoff; lostapp-switched: only switched
        to its safeboot, its Tasmota unharmed, which must be started again,
        never replaced.
    notemplate-generic: a converted plug's template replaced with Tasmota's
        generic one, and an MQTT host set, as a person might (go_notemplate);
        then the tool, to Tasmota, ESPHome or Sonoff.
    bootloop-fallback: a plug converted to Tasmota, then put on its
        fallback module, as Tasmota does in a boot loop (go_bootloop): it
        must still run its own template (BAD-TEMPLATE).
    safemode-ten: a plug converted to ESPHome, switched off within a minute
        of starting ten times, which ESPHome takes for a boot loop and starts
        in safe mode (go_safemode); then the tool, to ESPHome, Tasmota or
        Sonoff: it must know the plug for ESPHome in safe mode, not the mover
        (FAIL-TAKEN-FOR-MOVER)."""
    def one(route, step, fault, arg, at=40):
        return dict(id=sid(route, step, fault, arg, at), route=route, step=step, fault=fault, arg=arg, at=at)

    for route in C2_ROUTES:
        for arg in ("cut", "dropupload", "dropsetup"):
            yield one(route, "C3", "ownnet", arg)
        yield one(route, "D1", "lostanswer", "once")
        yield one(route, "D1", "seven", "advised")
        yield one(route, "D4", "seven", "times")
    for route in ("sonoff>tasmota", "esphome>tasmota"):
        for step in ("F4", "G1"):
            yield one(route, step, "seven", "times")
        yield one(route, "F4", "nohook", "run")
        for at in (10, 50, 90):
            yield one(route, "F4tp", "nohook", "cut", at)
        for at, fault, arg in ((10, "plugoff", 1), (50, "plugoff", 1), (90, "plugoff", 1),
                               (50, "plugoff", 5), (50, "crash", 1)):
            yield one(route, "F4rs", fault, arg, at)
    for tag, routes in LEGACY_STEPS.items():
        for route, steps in routes.items():
            for step in steps:
                yield one(route, step, "legacy", tag)
        for route in LEGACY_STARTS[tag]:
            yield one(route, "start", "legacy", tag)
    # Killed at F4, past its commit; then the plug switched off part way
    # through G1 of the run after, writing over 1.2.0's startup firmware,
    # which it cannot rebuild.
    yield one("sonoff>tasmota", "F4", "legacy", "v1.2.0+G1off")
    for route in ("tasmota>tasmota", "tasmota>esphome", "tasmota>sonoff"):
        yield one(route, "start", "lostapp", "factory")
    for route in ("tasmota>tasmota", "tasmota>esphome"):
        yield one(route, "start", "lostapp", "switched")
    for route in ("tasmota>tasmota", "tasmota>esphome", "tasmota>sonoff"):
        yield one(route, "start", "notemplate", "generic")
    for route in ("sonoff>tasmota", "esphome>tasmota"):
        yield one(route, "start", "bootloop", "fallback")
    for route in ("esphome>esphome", "esphome>tasmota", "esphome>sonoff"):
        yield one(route, "start", "safemode", "ten")
    for route, step in BLIP_STEPS:
        yield one(route, step, "blips", 3, at=10)
        yield one(route, step, "lossy", "phase", at=10)


# The steps that run a Berry phase on the plug (stage, restore, plan), each on
# one way it is taken: a phase must ride out blips and a weak signal by
# itself, never needing the step tried again (PR #5: one early blip used up
# the retrying, and a later one failed on its first try). In I1 and R1 the
# two blips are more than its 20 s of retrying apart; from 2.0.2, E2, G1 and
# L1 are too short for that.
BLIP_STEPS = (("sonoff>tasmota", "E2"), ("esphome>tasmota", "G1"), ("sonoff>esphome", "I1"),
              ("tasmota>esphome", "L1"), ("tasmota>sonoff", "R1"))


# The releases whose runs this checkout must carry on: each one's steps from
# Sonoff, by route, and their usual lengths on the emulator then.  1.2.0 is
# the last before 2.0.0's renames; the latest release is the one people have
# when the firmware the tool installs changes (docs/updating-firmware.md),
# its runs pinned to the firmware before.
LEGACY_STEPS = {
    "v1.2.0": {"sonoff>tasmota": {"C2": 20, "C3": 14, "D1": 18, "D4": 31, "E1": 2, "E2": 29,
                                  "E3": 5, "F2": 35, "F4": 31, "G1": 33, "H1": 2}},
    "v2.0.2": {"sonoff>tasmota": {"C2": 20, "C3": 14, "D1": 18, "D4": 31, "D5": 2, "E1": 2,
                                  "E2": 17, "E3": 5, "F2": 35, "F4": 10, "G1": 21, "H1": 2},
               "sonoff>esphome": {"C2": 20, "C3": 14, "D1": 18, "D4": 31, "D5": 2, "I1": 30,
                                  "I2": 5}},
}
# Plugs each release converted, then taken on by this checkout ("start").
# Before 2.0.0 the tool installed only Tasmota.
LEGACY_STARTS = {
    "v1.2.0": ("tasmota>esphome", "tasmota>sonoff"),
    "v2.0.2": ("tasmota>esphome", "tasmota>sonoff", "esphome>tasmota", "esphome>sonoff"),
}
LEGACY = EMU / "legacy"
LEGACY_TOOL = "tools/s60_autoflash.py"  # its name until 2.0.0
# What a run says when an error it did not expect stops it (autoflash_cli._main).
UNEXPECTED = "something unexpected went wrong"
LEGACY_NAMED = {"G1", "H1"}  # killed past its F4: the plug keeps the name that release gave it
_legacy_lock = threading.Lock()


def legacy_tree(tag):
    """The tool as release `tag` shipped, to run in the emulator: its tagged
    files, and only tools/emulator/legacy/<tag>.patch on top, where it has
    one - 1.2.0's points it at the cloud stub, as AUTOFLASH_EWELINK_API_BASE
    has done since 2.0.0."""
    tree = LEGACY / tag
    with _legacy_lock:
        if not (tree / ".complete").exists():
            shutil.rmtree(tree, ignore_errors=True)
            tree.mkdir(parents=True)
            archive = subprocess.run(["git", "-C", str(REPO), "archive", tag], capture_output=True,
                                     check=True).stdout
            subprocess.run(["tar", "-x", "-C", str(tree)], input=archive, check=True)
            patch = REPO / f"tools/emulator/legacy/{tag}.patch"
            if patch.exists():
                subprocess.run(["patch", "-s", "-p1", "-d", str(tree), "-i", str(patch)], check=True)
            (tree / ".complete").write_text(f"{tag}\n")
    return tree


def esphome_factory_image():
    """A whole-flash image as ESPHome's builder makes one (factory.bin), for
    a serial flasher: its bootloader from 0, a partition table at 0x8000 -
    Sonoff's, which ESPHome's S60 config keeps - and the app in the first
    slot, at 0x20000. It starts with the bootloader's 0xE9, as an app does."""
    artifacts = REPO / "artifacts"
    image = bytearray(b"\xff" * 0x20000)
    bootloader = next(artifacts.glob("esphome-sonoff-s60-bootloader-*.bin")).read_bytes()
    table = (artifacts / "sonoff-s60-partition-table.bin").read_bytes()
    image[:len(bootloader)] = bootloader
    image[0x8000:0x8000 + len(table)] = table
    app = next(artifacts.glob("esphome-sonoff-s60-imprintable-*.bin")).read_bytes()
    return bytes(image) + app


def needs_cloud(sc):
    # A legacy run starts from Sonoff, at C2, whatever step it stops at.
    return sc["fault"] == "legacy" or (sc["route"].startswith("sonoff>") and sc["step"] in CLOUD_STEPS)


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
    COOL_DOWN = float(os.environ.get("AUTOFLASH_CLOUD_COOL_DOWN", "5"))

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
        self.workers = int(os.environ.get("AUTOFLASH_CLOUD_WORKERS", "0"))
        self.most = int(os.environ.get("AUTOFLASH_CLOUD_MOST", str(self.workers)))
        self.cool_down = float(os.environ.get("AUTOFLASH_CLOUD_COOL_DOWN", "5"))
        self.phase = "workers" if self.most > self.workers else "cool-down"
        if os.environ.get("AUTOFLASH_CLOUD_PAUSE_UP") == "1":
            # From a short pause up: 5 s longer at each trouble the cloud is to
            # blame for, never shorter; how many at once left as it is.
            self.phase = "pause-up"
        elif os.environ.get("AUTOFLASH_CLOUD_BACK_OFF") == "1":
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
        with open(OUT / "tuning.log", "a", encoding="utf-8") as f:
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
                    text = Path(log).read_text(encoding="utf-8", errors="replace")
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
BOOTS = re.compile(r"(?P<sonoff>\[BUILD TIME\]|colinkTcp|tmp ip = )|(?P<bridge>autoflash_bridge)|"
                   r"(?P<tasmota>Project tasmota)|(?P<esphome>\[I\]\[app)")
INTENT = re.compile(r"\] (?:A1|A2|C2)  |Sending the installer again")
HOLD_AT_MOST = 240.0


def plug_busy(state, since):
    """Each Berry step's longest busy time, from the runs since `since`."""
    busy = {}
    for path in (state / "work/captures/plugs").glob("*/runs/*/autoflash-state.json"):
        try:
            if path.stat().st_mtime < since:
                continue
            captures = json.loads(path.read_text()).get("captures", {})
        except (OSError, ValueError):
            continue
        for key, seconds in captures.items():
            if key.endswith(".plug_busy_seconds") and isinstance(seconds, (int, float)):
                step = key.split(".")[0]
                busy[step] = max(busy.get(step, 0), round(seconds, 1))
    return busy


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
                with open(uart, encoding="utf-8", errors="replace") as f:
                    f.seek(pos); boots = f.read(); pos = f.tell()
                if any(m.lastgroup != "sonoff" for m in BOOTS.finditer(boots)):
                    break
                self.stop.wait(0.5)
        finally:
            plug_internet(self.n, False)
            CLOUD.release(self.n)
            self.sc.say(f"CLOUD: turn given up after {time.time() - since:.0f}s")


def name(n):
    return f"autoflash-emu-{n}"


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
        self.earlier_release_wrote = {}  # each run log, as far as a legacy run left it

    def ours(self, log, text):
        """What this checkout's runs wrote to a run log: past what an earlier
        release's run (go_legacy), carried on in the same run, wrote first."""
        return text[self.earlier_release_wrote.get(log, 0):]

    def say(self, text):
        with open(self.log, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%T')} {text}\n")

    def ex(self, *args, timeout=60):
        return sh("docker", "exec", name(self.n), *args, timeout=timeout)

    def run_tool(self, target, extra=(), source=None, fresh=False, code=None):
        """A run to `target`: of this checkout's tool, or of the one in `code`
        (legacy_tree) - which stays in place for the runs after, until another
        `code`. A release before 2.0.0 had no --firmware: Tasmota only."""
        args = [str(REPO / "tools/emulator/unattended-test.sh"), f"--instance={self.n}",
                f"--plug={MAC}", "--keep"]
        if STUB_CLOUD:
            args.append("--stub-cloud")
        if fresh:
            args.append("--fresh")
        if source:
            args.append(f"--from={saved(source)}")
        if code:
            args.append(f"--code={code}")
        legacy = code is not None and (Path(code) / LEGACY_TOOL).exists()
        args += ["--", *([] if legacy else ["--firmware", target]), *extra]
        env = dict(os.environ)
        if (self.sc["fault"], self.sc["arg"]) == ("ctrlc", "countdown"):
            env["AUTOFLASH_EMULATOR_COUNTDOWN"] = "30"
        if self.sc["fault"] == "nohook":
            env["AUTOFLASH_EMULATOR_NO_AT_SAVE"] = "1"
        with open(self.log, "a", encoding="utf-8") as out:
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

    def wait_for_step(self, since, headers, stop, step=None):
        step = step or self.sc["step"]
        pattern = re.compile(STARTS.get(step, (rf"\] {step}  ",))[0])
        while not stop.is_set():
            text = self.current_run(since, headers)
            if text and pattern.search(text):
                return True
            time.sleep(0.3)
        return False

    def inject(self, since, headers, delay, stop, fault, arg, first=True, step=None):
        """The fault, `delay` seconds into the step under test - or `step`."""
        at = step or self.sc["step"]
        if not self.wait_for_step(since, headers, stop, at):
            return
        time.sleep(delay)
        if stop.is_set():
            return
        step = STEP_OF.get(at, at)
        text = self.current_run(since, headers) or ""
        if re.search(rf"\] {step} done", text):
            self.notes["mistimed"] = True  # the step was over before the fault
        if first:
            self.fired = time.time()
        self.say(f"FAULT {fault} {arg} at {at} +{delay:.1f}s")
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
        elif fault == "legacy":
            self.signal("KILL", *HELPERS, LEGACY_TOOL, TOOL)
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
                network = self.ex("cat", "/run/autoflash-emu/softap").stdout.strip()
                if network and not joined:
                    time.sleep(5)
                    result = self.ex("/opt/autoflash-emu/bin/wifi-root", "join", network, timeout=90)
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
        elif fault == "blips":
            # Two short outages in one Berry phase, from 10% to 90% of it:
            # further apart than its 20 s of retrying (BLIP_STEPS).
            self.ex(p, "router", "off"); time.sleep(arg); self.ex(p, "router", "on")
            time.sleep(max(0.0, 0.8 * self.step_length - arg))
            self.say(f"FAULT blips: the second, {0.8 * self.step_length:.0f}s after the first")
            self.ex(p, "router", "off"); time.sleep(arg); self.ex(p, "router", "on")
        elif fault == "lossy":
            # One packet in ten lost, as "weak", for the rest of the phase.
            self.ex("tc", "qdisc", "add", "dev", "tap0", "root", "netem",
                    "delay", "150ms", "100ms", "loss", "10%")
            self.ex("tc", "filter", "add", "dev", "tap0", "ingress", "pref", "6", "protocol", "ip",
                    "u32", "match", "u32", "0", "0", "action", "gact", "ok", "random", "netrand", "drop", "10")
            time.sleep(self.step_length)
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
                     "-e", "HOME=/tmp", "-e", f"AUTOFLASH_EMULATOR_URL=http://localhost:{8080 + self.n}",
                     "-e", "AUTOFLASH_EMULATOR_SUDO_PASSWORD=autoflash", name(self.n), "timeout", "150",
                     "/opt/venv/bin/python", TOOL, "run", "--unattended", "--env-file", "/tmp/autoflash-unattended.env",
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
            out = self.ex("sh", "-c", "cd /work && set -a && . /tmp/autoflash-unattended.env && "
                          "/opt/venv/bin/python -c 'import os; from tools.get_device_key import login; "
                          "login(os.environ[\"EWELINK_EMAIL\"], os.environ[\"EWELINK_PASSWORD\"], \"+44\", \"eu\"); "
                          "print(\"logged in again\")'", timeout=60)
            self.notes["token"] = (out.stdout + out.stderr).strip()[-200:]
        elif fault == "ownnet":
            # D1 through the installer's own network (the fallback, below);
            # and, on it, when the run gets that far: the run cut off just
            # after the upload, or this computer back on the home network.
            words, act = {"cut": ("the plug is restarting into Tasmota", self.cut_run),
                          "dropupload": ("Sending Tasmota (", self.drop_join),
                          "dropsetup": ("Sending the Wi-Fi details", self.drop_join)}[arg]
            threading.Thread(target=self.when_said, args=(since, words, act), daemon=True).start()
            self.fault("fallback", 150, since, headers)
        elif fault == "lostanswer":
            # The installer's answer to the upload ("Upload verified") never
            # reaches this computer, every retransmission dropped too: on only
            # now, as the backup's flash - which holds the same words - is read.
            rule = ["INPUT", "-s", "192.168.60.50", "-p", "tcp", "--sport", "80", "-m", "string",
                    "--string", "Upload verified", "--algo", "bm", "-j", "DROP"]
            self.notes["answer_dropped"] = self.ex("iptables", "-I", *rule).returncode == 0
            deadline = time.time() + 600
            while time.time() < deadline:
                text = self.current_run(since, headers) or ""
                if "did not complete" in text or "=== run ended ===" in text or re.search(r"\] D1 done", text):
                    break
                time.sleep(1)
            self.ex("iptables", "-D", *rule)
        elif fault == "seven" and arg == "times":
            self.ex(PLUG_CMD, "cycle", "7", timeout=300)
            self.notes["seven_times"] = True
        elif fault == "seven" and arg == "advised":
            # Tasmota sent through the installer cannot join the home Wi-Fi
            # (gone from just after the upload), and the run advises the 7
            # power cycles: done, and the Wi-Fi back.
            if not self.when_said(since, "the plug is restarting into Tasmota", lambda: None):
                return
            self.ex(PLUG_CMD, "router", "off")
            self.say("FAULT: the home Wi-Fi gone after the upload")
            if self.when_said(since, "Switch the plug off and on 7 times", lambda: None, limit=900):
                time.sleep(10)  # a person reads it
                self.say("PERSON: switching the plug off and on 7 times")
                self.ex(PLUG_CMD, "cycle", "7", timeout=300)
                self.notes["seven_times"] = True
            self.ex(PLUG_CMD, "router", "on")
        elif fault == "nohook":
            pass  # the run's own condition (run_tool): there is nothing to do to it

    def when_said(self, since, words, act, limit=1800):
        """Once any run since `since` has said `words`: act(). False if none
        did within `limit` seconds, or the scenario ended first."""
        deadline = time.time() + limit
        while time.time() < deadline and not getattr(self, "ended", False):
            if any(words in text for m, _, text in run_logs(self.state) if m >= since):
                act()
                return True
            time.sleep(0.3)
        return False

    def cut_run(self):
        self.say("FAULT: the tool killed just after the upload")
        self.signal("KILL", *HELPERS, TOOL)
        self.notes["cut_after_upload"] = True

    def drop_join(self):
        time.sleep(3)  # part way
        self.ex("/opt/autoflash-emu/bin/wifi-root", "home", timeout=60)
        self.say("FAULT: this computer back on the home network, off the plug's")
        self.notes["dropped_off"] = True
        self.dropped = True  # the person stand-in joins again when asked

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
        text = Path(self.log).read_text(encoding="utf-8", errors="replace")
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
        silent = 0  # looks in a row the plug has not answered on its network
        while not stop.is_set():
            if getattr(self, "dropped", False):
                joined = self.joined = None
                self.dropped = False
            joined = joined or getattr(self, "joined", None)
            on_air = self.ex("cat", "/run/autoflash-emu/softap").stdout.strip()
            if joined:
                if on_air:
                    gone_since = None
                elif gone_since is None:
                    gone_since = time.time()
                elif time.time() - gone_since > 5:
                    self.ex("/opt/autoflash-emu/bin/wifi-root", "home", timeout=60)
                    self.say(f"PERSON: {joined} gone; this computer back on the home network by itself")
                    joined = self.joined = None
                    gone_since = None
            if joined and on_air and wanted:
                # Dropped off a network that came back by the same name - the
                # plug restarted on it (the installer into Tasmota, say), too
                # quickly to be seen gone: joined again while the run asks.
                gateway = self.ex("cat", "/run/autoflash-emu/gateway").stdout.strip() or "192.168.4.1"
                silent = 0 if self.ex("ping", "-c1", "-W1", gateway).returncode == 0 else silent + 1
                if silent >= 5:  # about 15 s: not a moment's drop as Tasmota tests the details
                    self.say(f"PERSON: dropped off {joined}; joining it again")
                    joined = self.joined = None
                    silent = 0
            else:
                silent = 0
            for _, log, text in run_logs(self.state):
                if log.stat().st_mtime < self.started:
                    continue
                joins, backs = JOIN.findall(text), text.count(BACK)
                if log not in seen:
                    seen[log] = (len(joins), backs)  # history from before this scenario
                    continue
                done_joins, done_backs = seen[log]
                if len(joins) > done_joins:
                    wanted = (log, joins[-1].rstrip(","))
                    stop.wait(8)  # a person takes a moment
                if backs > done_backs:
                    wanted = None
                    if joined:
                        self.ex("/opt/autoflash-emu/bin/wifi-root", "home", timeout=60)
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
                on_air = self.ex("cat", "/run/autoflash-emu/softap").stdout.strip()
                if on_air:
                    result = self.ex("/opt/autoflash-emu/bin/wifi-root", "join", on_air, timeout=90)
                    self.say(f"PERSON: joined {on_air} (as told: {wanted[1]}; rc {result.returncode})")
                    if result.returncode == 0:
                        joined = on_air
                        self.notes.setdefault("joined", []).append(on_air)
                    else:
                        stop.wait(5)
            stop.wait(2)
        if joined:
            self.ex("/opt/autoflash-emu/bin/wifi-root", "home", timeout=60)

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
        if (self.sc.get("step") == "C2" or self.sc["fault"] == "legacy") and not NO_CLOUD and not STUB_CLOUD:
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
        uart = self.state / "logs/uart.log"
        self.uart_from = uart.stat().st_size if uart.exists() else 0  # this scenario's networks: from here
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
        if sc["fault"] == "legacy":
            return self.go_legacy()
        if sc["fault"] == "lostapp":
            return self.go_lostapp()
        if sc["fault"] == "notemplate":
            return self.go_notemplate()
        if sc["fault"] == "bootloop":
            return self.go_bootloop()
        if sc["fault"] == "safemode":
            return self.go_safemode()
        if step == "C2":
            source, self.target, steps = None, C2_ROUTES[route], {"C2": 20}
        else:
            source, self.target, steps = ROUTES[route]
            if route in AFTER_D1 and step not in CLOUD_STEPS:
                source = AFTER_D1[route]
        length = STARTS[step][1] if step in STARTS else steps[step]
        self.step_length = length
        delay = max(0.3, sc["at"] / 100 * length)
        if sc["fault"] == "corrupt":
            # From the step's start: its data can be over well before 40% of
            # its usual time (X1's upload), and a short step's in seconds.
            delay = 0.3
        fault, arg = sc["fault"], sc["arg"]
        first_fault = ((arg.split("-")[0], int(arg.split("-")[1])) if fault == "double" else
                       ("plugoff", 1) if (fault, arg) == ("nohook", "cut") else (fault, arg))
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
        return self.again(results, target, extra, delay, first_fault)

    def go_legacy(self):
        """An earlier release's run (sc["arg"]) on a plug fresh from Sonoff,
        killed at 40% of the step under test - or, at "start", let convert it
        to the route's first firmware - then this checkout's tool again and
        again, as a person would, until the plug is converted (to the route's
        target)."""
        sc = self.sc
        step, (source, self.target) = sc["step"], sc["route"].split(">")
        # "+G1off": the plug switched off, too, part way through G1 of the
        # first run after - G1 then writing over that release's startup
        # firmware, unproven (step_restore), and carrying on after a cut.
        # That G1 is this checkout's, and as long as its.
        tag, _, then = sc["arg"].partition("+")
        steps = LEGACY_STEPS[tag].get(sc["route"], {})
        later = ("G1", "plugoff", 1, 0.6 * ROUTES[sc["route"]][2]["G1"]) if then == "G1off" else None
        old = legacy_tree(tag)
        stop = threading.Event()
        if step != "start":
            self.step_length = steps[step]
            threading.Thread(target=self.inject, args=(time.time(), None, 0.4 * self.step_length, stop,
                                                       "legacy", sc["arg"]), daemon=True).start()
        first = source if step == "start" else self.target
        results = [(self.run_tool(first, fresh=True, code=old), None)]
        results[-1] = (results[-1][0], self.screen_result())
        stop.set()
        self.earlier_release_wrote = {log: len(text) for _, log, text in run_logs(self.state)}
        if step == "start":
            if "Converted" not in results[-1][1]:
                return self.finish(results, "OLD-RUN-FAILED")
            # Its starting point, not the end: the conversion under test is next.
            results[-1] = (results[-1][0], f"on {dict(tasmota='Tasmota', esphome='ESPHome')[source]}, by {sc['arg']}")
        elif self.fired is None:
            return self.finish(results, "NOT-FIRED")
        return self.again(results, self.target, [], code=REPO, later=later)

    def go_lostapp(self):
        """A converted plug on Tasmota, and what was asked on Reddit: another
        firmware's whole-flash image (ESPHome's factory.bin) uploaded through
        Tasmota's page, which switches to its safeboot for a file that big,
        whose upload takes the image, writes it over Tasmota and refuses it at
        the end. The plug then starts the safeboot, even switched off and on.
        Then the tool, as a person would run it, until the plug is converted."""
        sc = self.sc
        self.target = sc["route"].split(">")[1]
        # The emulator up with the converted plug: asked for what it runs, a
        # run says there is nothing to do.
        results = [(self.run_tool("tasmota", source="suite-tasmota"), None)]
        results[-1] = (results[-1][0], self.screen_result())
        if "nothing to convert" not in results[-1][1]:
            return self.finish(results, "NOT-FIRED")
        factory = self.state / "work/captures/esphome-factory.bin"
        factory.write_bytes(esphome_factory_image())
        plug = "http://192.168.60.50"
        # From the plug's own page, as a browser on it is: Tasmota turns away
        # a request without (CSRF protection).
        page_of = ["curl", "-s", "-e", f"{plug}/"]
        self.say("LOSTAPP: Tasmota switched to its safeboot, as its page does for a large file")
        self.ex(*page_of, "-m", "10", f"{plug}/u4?u4=fct&api=")
        if not self.until_page("/", "safeboot", 120):
            return self.finish(results, "NOT-FIRED")
        if sc["arg"] == "factory":  # "switched": its Tasmota left as it is
            self.say(f"LOSTAPP: ESPHome's factory image ({factory.stat().st_size} bytes) uploaded to it")
            page = self.ex(*page_of, "-m", "300", "-F", "u2=@/work/captures/esphome-factory.bin",
                           f"{plug}/u2?fsz={factory.stat().st_size}", timeout=320).stdout
            self.notes["upload_page"] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))[-300:]
            self.say(f"LOSTAPP: it answered: {self.notes['upload_page'][-120:]}")
            # The emulated Tasmota 15.6.0 safeboot says the upload worked and
            # writes nothing (app0 seen unchanged, 2026-10-21); a real one
            # writes the image over Tasmota as it arrives, and checks it only
            # at the end. That, as it is on such a plug: the plug off, the
            # image where Tasmota was, and on again.
            sh("docker", "stop", name(self.n), timeout=120)
            with open(self.state / "flash.bin", "r+b") as flash:
                flash.seek(APP0_OFFSET)
                flash.write(factory.read_bytes())
            self.say("LOSTAPP: off; the image written over Tasmota (app0), as a real safeboot does; on again")
            sh(str(REPO / "tools/emulator/emu.sh"), *(["--stub-cloud"] if STUB_CLOUD else ["--cloud", "--yes"]),
               "--detach", f"--plug={MAC}", f"--instance={self.n}", timeout=300)
        else:
            self.say("LOSTAPP: switched off and on")
            self.ex(PLUG_CMD, "restart")
            time.sleep(5)
        if not self.until_page("/", "safeboot", 120):
            self.say("LOSTAPP: not on its safeboot after all")
            return self.finish(results, "NOT-FIRED")
        self.notes["stuck_on_safeboot"] = True
        results[-1] = (results[-1][0], "on Tasmota's safeboot, Tasmota missing")
        return self.again(results, self.target, [])

    def tasmota(self, command):
        """What the plug's Tasmota answers to `command`, as JSON - or None."""
        out = self.ex("curl", "-s", "-m", "10", "-G", "-e", f"{PLUG_URL}/",
                      "--data-urlencode", f"cmnd={command}", f"{PLUG_URL}/cm").stdout
        try:
            return json.loads(out)
        except ValueError:
            return None

    def template_ok(self):
        """Whether the plug runs Tasmota with the S60's template, asked a few
        times: it may still be starting."""
        for _ in range(6):
            template, module = self.tasmota("Template"), self.tasmota("Module")
            if isinstance(template, dict) and isinstance(module, dict):
                self.notes["template"] = template.get("NAME")
                return "S60" in str(template.get("NAME")) and "0" in (module.get("Module") or {})
            time.sleep(5)
        return False

    def go_notemplate(self):
        """A converted plug on Tasmota whose template a person replaced with
        Tasmota's generic one - which switches nothing (Sonoff-OTA discussion
        #8) - having set it up for themselves (an MQTT host). Then the tool:
        to Tasmota it gives the template alone, the MQTT host kept; to the
        others it leaves Tasmota as from any converted plug, never by X1."""
        self.target = self.sc["route"].split(">")[1]
        results = [(self.run_tool("tasmota", source="suite-tasmota"), None)]
        results[-1] = (results[-1][0], self.screen_result())
        if "nothing to convert" not in results[-1][1]:
            return self.finish(results, "NOT-FIRED")
        generic = json.dumps({"NAME": "Generic", "GPIO": [0] * 22, "FLAG": 0, "BASE": 1}, separators=(",", ":"))
        self.say("NOTEMPLATE: the generic template and an MQTT host set, as a person might")
        self.tasmota(f"Backlog MqttHost {NOTEMPLATE_MQTT_HOST}; Template {generic}; Module 0")
        deadline = time.time() + 120
        while time.time() < deadline:
            time.sleep(5)
            template = self.tasmota("Template")
            if isinstance(template, dict) and template.get("NAME") == "Generic":
                break
        else:
            self.say("NOTEMPLATE: the plug did not take the generic template")
            return self.finish(results, "NOT-FIRED")
        results[-1] = (results[-1][0], "on Tasmota, without the plug's template")
        return self.again(results, self.target, [])

    def go_bootloop(self):
        """A plug converted to Tasmota, then put through what Tasmota does
        at the sixth start in a row within 10 s of the last: taking that for
        a boot loop, it goes over to its fallback module - Generic, unless
        the plug's own template is set there - keeping its Wi-Fi. Left on
        Generic, the plug switches nothing (Sonoff-OTA discussion #8).

        Done here as Tasmota would, by `Module` to the fallback `Module2`
        names: Tasmota counts those starts in RTC memory, which the emulator
        loses at every restart (plug-supervisor starts QEMU afresh). The
        restarts themselves would also do harm a real plug may not: each
        start ending before 8 s counts as a quick power cycle, kept in flash,
        and at the seventh Tasmota erases every setting, its Wi-Fi too."""
        route = self.sc["route"]
        source, self.target, _ = ROUTES[route]
        source = AFTER_D1.get(route, source)
        results = [(self.run_tool(self.target, source=source), None)]
        results[-1] = (results[-1][0], self.screen_result())
        while "Converted" not in results[-1][1] and len(results) < MAX_RUNS:
            results.append((self.run_tool(self.target), None))
            results[-1] = (results[-1][0], self.screen_result())
        self.target_final = self.target
        if "Converted" not in results[-1][1]:
            return self.finish(results, None)
        fallback = self.tasmota("Module2")
        fallback = fallback.get("Module2") if isinstance(fallback, dict) else None
        if not isinstance(fallback, dict) or len(fallback) != 1:
            self.say(f"BOOTLOOP: the plug did not say its fallback module ({fallback})")
            return self.finish(results, "NOT-FIRED")
        (number, name), = fallback.items()
        self.notes["fallback_module"] = name
        self.say(f"BOOTLOOP: the plug goes over to its fallback module, {name} ({number})")
        self.tasmota(f"Module {number}")
        time.sleep(15)  # Module restarts the plug; template_ok waits on from there
        return self.finish(results, None)

    def ports_open(self, *ports):
        """Which of `ports` the plug takes a connection on now."""
        probe = ("import socket, sys\n"
                 "for port in sys.argv[1:]:\n"
                 "    try:\n"
                 "        socket.create_connection(('192.168.60.50', int(port)), timeout=2).close()\n"
                 "        print(port)\n"
                 "    except OSError:\n"
                 "        pass\n")
        out = self.ex("python3", "-I", "-c", probe, *map(str, ports)).stdout
        return {int(port) for port in out.split()}

    def go_safemode(self):
        """A plug converted to ESPHome, then switched off within a minute of
        starting ten times in a row: ESPHome takes that for a boot loop and
        starts in safe mode - Wi-Fi and its update service only, no page, no
        relay - for five minutes, then restarts as itself. All the network
        sees is the update service, as the mover has. Then the tool, to
        ESPHome, Tasmota or Sonoff.

        ESPHome keeps its count in flash, so the emulator keeps it too. Each
        start counts as it begins, before Wi-Fi: cut once the update service
        answers. A normal start has its page up by then; safe mode never."""
        self.target = self.sc["route"].split(">")[1]
        results = [(self.run_tool("esphome", source="suite-esphome"), None)]
        results[-1] = (results[-1][0], self.screen_result())
        if "nothing to convert" not in results[-1][1]:
            return self.finish(results, "NOT-FIRED")
        cuts = 0
        while True:
            deadline, open_now = time.time() + 50, set()
            while time.time() < deadline and ESPHOME_OTA_PORT not in open_now:
                time.sleep(0.5)
                open_now = self.ports_open(ESPHOME_OTA_PORT, 80)
            if ESPHOME_OTA_PORT not in open_now:
                self.say(f"SAFEMODE: ESPHome did not answer within 50 s of starting ({cuts} cuts)")
                return self.finish(results, "NOT-FIRED")
            time.sleep(2)
            if 80 not in open_now and 80 not in self.ports_open(80):
                break  # in safe mode
            if cuts == 12:
                self.say("SAFEMODE: still starting normally after 12 cuts")
                return self.finish(results, "NOT-FIRED")
            self.ex(PLUG_CMD, "off"); time.sleep(1); self.ex(PLUG_CMD, "on")
            cuts += 1
        self.notes["safemode_cuts"] = cuts
        self.say(f"SAFEMODE: in safe mode after {cuts} cuts: its update service only")
        results[-1] = (results[-1][0], "on ESPHome's safe mode")
        return self.again(results, self.target, [])

    def until_page(self, path, words, limit):
        """Whether the plug's page `path` says `words`, in any case, within `limit` seconds."""
        deadline = time.time() + limit
        while time.time() < deadline:
            page = self.ex("curl", "-s", "-m", "5", "-e", "http://192.168.60.50/",
                           f"http://192.168.60.50{path}").stdout
            if words.lower() in page.lower():
                return True
            time.sleep(3)
        return False

    def again(self, results, target, extra, delay=None, first_fault=None, code=None, later=None):
        """The tool run again, as a person would - switching the plug off and
        on, or joining its own network, where the run says to - until the plug
        is converted, or MAX_RUNS runs in all. `code`: the first of them from
        there (run_tool), and the rest after it. `later`: (step, fault, arg,
        delay), a fault in the first of them too (noted as later_fired)."""
        sc = self.sc
        fault, arg = sc["fault"], sc["arg"]
        while "Converted" not in results[-1][1] and len(results) < MAX_RUNS:
            said = re.sub(r"\s+", " ", Path(self.log).read_text(encoding="utf-8", errors="replace").split("--- ")[-1])
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
            if later and len(results) == 1:
                step, later_fault, later_arg, later_delay = later

                # Given its stop event, not reading stop2 when done: by then the
                # loop may have made a new one, never set, for the next run.
                def later_inject(since, headers, stop, step, fault, arg, delay):
                    self.inject(since, headers, delay, stop, fault, arg, False, step)
                    if not stop.is_set():
                        self.notes["later_fired"] = f"{fault}-{arg} at {step}"
                threading.Thread(target=later_inject, daemon=True,
                                 args=(time.time(), self.headers_now(), stop2, step,
                                       later_fault, later_arg, later_delay)).start()
            results.append((self.run_tool(target, extra, code=code), None))
            results[-1] = (results[-1][0], self.screen_result())
            stop2.set()
            extra, code = [], None
            said = re.sub(r"\s+", " ", Path(self.log).read_text(encoding="utf-8", errors="replace").split("--- ")[-1])
            if (fault, arg) == ("cancel", "switch") and "cannot switch to" in said:
                # Refused, as it should be: started again for the other one, as it says.
                self.notes["switch_refused"] = True
                extra = ["--new"]
        self.target_final = target
        return self.finish(results, None)

    def names_seen(self):
        """The names the plug went by on the home network (DHCP, in order,
        repeats folded) and the networks it put out (its serial log)."""
        dhcp = self.ex("sh", "-c", "grep DHCPACK /run/autoflash-emu/dnsmasq.log 2>/dev/null"
                                   " | grep ' 192.168.60.50 '").stdout
        names, networks = [], []
        for line in dhcp.splitlines():
            name = line.split()[-1]
            if ":" not in name and (not names or names[-1] != name):
                names.append(name)
        try:
            with open(self.state / "logs/uart.log", "rb") as uart:
                uart.seek(getattr(self, "uart_from", 0))
                text = uart.read().decode("utf-8", "replace")
            for network in re.findall(r"WIFIAP: guest SoftAP (\S+) up", text):
                if not networks or networks[-1] != network:
                    networks.append(network)
        except OSError:
            pass
        return names, networks

    def backup_whole(self):
        backups = list((self.state / "work/captures/plugs").glob("*/sonoff-backup/flash.bin"))
        return bool(backups) and all(b.stat().st_size == 4 * 1024 * 1024 for b in backups)

    def leftovers(self):
        found = []
        rules = self.ex("ufw", "status").stdout
        if "8088" in rules or "8089" in rules or "autoflash" in rules.lower():
            found.append("firewall rule")
        if self.ex("pgrep", "-f", "|".join(HELPERS)).stdout.strip():
            found.append("helper running")
        if self.ex("pgrep", "-f", TOOL).stdout.strip():
            found.append("tool running")
        return found

    def finish(self, results, special):
        self.ended = True
        sc = self.sc
        converted = "Converted" in results[-1][1]
        fault, arg = sc["fault"], sc["arg"]
        left = self.leftovers()
        names, networks = self.names_seen()
        self.notes["names"], self.notes["networks"] = names, networks
        unknown, final = name_problems(names, networks, getattr(self, "target_final", self.target))
        # How long each Berry phase kept the plug busy, the longest try of
        # each (the run's report: "Plug busy"). Kept, not judged: a real
        # S60TPG once stopped answering at 70-90 s, an S60TPF was fine at 2
        # minutes (report, test-reports discussion) - report lists the longest.
        self.notes["plug_busy"] = plug_busy(self.state, self.started)
        if fault == "legacy" and sc["step"] in LEGACY_NAMED and final and re.match(r"^tasmota-[0-9A-F]{6}-\d{4}$", final):
            final = None  # set up by that release's F4, under Tasmota's own name, as it left every plug
        if fault == "legacy" and "+" in arg:
            # Whether the restore after the cut carried on after sectors it had
            # written, or the cut came before it wrote any (the plug says).
            uart = self.state / "logs/uart.log"
            with open(uart, "rb") as log:
                log.seek(getattr(self, "uart_from", 0))
                found = re.findall(rb"AUTOFLASH RESTORE: carrying on after (\d+) sectors", log.read())
            self.notes["restore_carried_on_after"] = int(found[-1]) if found else 0
            if not self.notes.get("later_fired") and not special:
                special = "NOT-FIRED"
        ours = re.sub(r"\s+", " ", "".join(self.ours(p, t) for m, p, t in run_logs(self.state)
                                           if m >= self.started))
        converted_from_untemplated = "Converting the plug from Tasmota without the plug's template" in ours
        target_final = getattr(self, "target_final", self.target)
        template_bad = converted and target_final == "tasmota" and not self.template_ok()
        # The report of the last run, from a converted plug (its backup taken
        # by the run that converted it): not a legacy release's.
        line = re.findall(r"Sonoff backup:\s+(.*)", re.sub(r"\x1b\[[0-9;]*m", "", Path(self.log).read_text(
            encoding="utf-8", errors="replace").split("--- ")[-1]))
        backup_line = (line[-1].strip() if line and not sc["route"].startswith("sonoff>")
                       and fault != "legacy" else None)
        if not special:
            if self.notes.get("mistimed"):
                special = "MISTIMED"
            elif any(UNEXPECTED in self.ours(p, t) for m, p, t in run_logs(self.state) if m >= self.started):
                # A run stopped by an error the tool did not see coming - a bug,
                # whatever the fault - though the next run may convert the plug
                # (seen: a report that could not be made, KeyError: 'safeboot').
                # Not an earlier release's, which is out already (1.2.0's
                # TimeoutError as G1 starts).
                special = "FAIL-UNEXPECTED"
            elif fault == "corrupt" and not self.notes.get("corrupted_packets"):
                special = "NO-TRAFFIC"
            elif fault == "lostapp" and (arg == "switched") == ("Official Tasmota (app0) uploaded" in "".join(
                    t for m, _, t in run_logs(self.state) if m >= self.started)):  # not the saved plug's own
                # Its own Tasmota, there all along, written over - or none there, not put back.
                special = "FAIL-REPLACED" if arg == "switched" else "FAIL-NOT-PUT-BACK"
            elif fault in ("blips", "lossy") and converted and (len(results) > 1 or any(
                    "That may have been the network - trying again" in t
                    for m, _, t in run_logs(self.state) if m >= self.started)):
                special = "FAIL-RETRIED"  # the phase did not ride it out by itself
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
            elif fault == "nohook" and "Settings reset to Tasmota's defaults, keeping the Wi-Fi; the plug" \
                    not in re.sub(r"\s+", " ", "".join(t for _, _, t in run_logs(self.state))):
                special = "NOT-FIRED"  # F4 took the name and template at the save after all
            elif any(len(re.findall(r"Type PROCEED to go ahead", run)) > 1
                     for m, p, t in run_logs(self.state) if m >= self.started
                     for run in self.ours(p, t).split("=== run started")):
                special = "FAIL-ASKED-AGAIN"  # the go-ahead is asked once a run
            elif fault == "safemode" and "ESPHome in safe mode" not in ours:
                special = "FAIL-TAKEN-FOR-MOVER"  # sent the startup firmware, even to stay on ESPHome
            elif fault == "notemplate" and not converted_from_untemplated:
                special = "FAIL-NOT-UNTEMPLATED"  # taken for something else
            elif fault == "notemplate" and "ESPHome's update service did not answer" in ours:
                special = "FAIL-X1-ON-TASMOTA"  # sent the hub's steps, as in #8
            elif fault == "notemplate" and self.target == "tasmota" and converted and (
                    "Settings reset to Tasmota's defaults" in ours
                    or (self.tasmota("MqttHost") or {}).get("MqttHost") != NOTEMPLATE_MQTT_HOST):
                special = "FAIL-SETTINGS-LOST"  # its owner's settings reset with the template
            elif converted and template_bad:
                special = "BAD-TEMPLATE"  # a Tasmota that switches nothing
            elif converted and backup_line and backup_line != "Taken by an earlier run":
                special = "FAIL-BACKUP-LINE"  # its backup is on this computer
            elif converted and (unknown or final):
                # A name this plug should never have, or not its converted
                # one at the end (docs/device-names.md).
                special = "BAD-NAME"
                self.notes["bad_names"] = unknown + ([final] if final else [])
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
        with open(RESULTS, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        if verdict != "PASS":
            # This scenario's own screens: the run's log goes back further.
            if STRANDED.search(re.sub(r"\s+", " ", Path(self.log).read_text(encoding="utf-8", errors="replace"))):
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


def name_problems(names, networks, target):
    """The names (DHCP) and networks the plug had that this plug should never
    have, and - when the last name is not the converted plug's - that name.
    A plug's names are made from its MAC (docs/device-names.md); those of
    earlier releases are allowed, for a plug saved by one."""
    raw = (MAC or "").replace(":", "").replace("-", "").lower()
    if len(raw) != 12:
        return [], None
    two, three = raw[-4:].upper(), raw[-6:]
    ap = f"{(int(raw, 16) + 1) & 0xFFFFFFFFFFFF:012X}"[-6:]
    allowed = {"espressif", f"autoflash-recovery-{two}", f"tasmota-{three}", f"esphome-{three}", f"ESP_{ap}",
               f"Sonoff-Setup-{two}", f"sonoff-s60-{three}", "s60-ota-bridge", f"sonoff-s60-mover-{three}",
               f"S60-OTA-Bridge-{two}"}
    # Tasmota's own (its settings erased, or set up by 1.2.0), and 1.2.0's
    # Safeboot's, four decimal digits of no fixed rule - and official Tasmota's
    # after that Safeboot, until 1.2.0's F4: its saved name had a format in it
    # (s60-recovery-%04d), which Tasmota takes for its own (artifacts/README.md).
    numbered = re.compile(rf"^((tasmota-{three.upper()}|s60-recovery)-|x)\d{{4}}$")
    unknown = [n for n in dict.fromkeys(names + networks) if n not in allowed and not numbered.match(n)]
    converted = {"tasmota": f"tasmota-{three}", "esphome": f"esphome-{three}", "sonoff": "espressif"}.get(target)
    final = names[-1] if names and converted and names[-1] != converted else None
    return unknown, final


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


def allowed(default):
    """How many of a batch's emulators may take a scenario now: the number in
    OUT/workers when it holds one, so a running batch can be scaled to what
    its machine has room for; else how many the batch began with."""
    try:
        return int((OUT / "workers").read_text())
    except (OSError, ValueError):
        return default


def worker(n, todo, lock, cloud=False, room=None):
    while True:
        # A cloud worker beyond how many the tuner allows now waits its turn,
        # and one beyond OUT/workers until it is raised or nothing is left.
        while cloud and n > TUNER.workers:
            time.sleep(10)
        while room and not room(n) and not todo.empty():
            time.sleep(10)
        try:
            sc = todo.get_nowait()
        except queue.Empty:
            return
        skip = OUT / "skip.txt"
        if skip.exists() and sc["id"] in skip.read_text().split():
            continue
        while int(re.search(r"MemAvailable:\s+(\d+)", Path("/proc/meminfo").read_text(encoding="utf-8")).group(1)) < 350_000:
            time.sleep(10)
        log = LOGS / (key(sc["id"]) + ".log")
        log.unlink(missing_ok=True)
        started = time.time()
        try:
            row = Scenario(n, sc, log).go()
        except Exception as exc:  # noqa: BLE001
            row = {**sc, "instance": n, "verdict": "HARNESS-ERROR", "error": repr(exc), "trace": traceback.format_exc(),
                   "finished": time.strftime("%Y-%m-%d %H:%M:%S")}
            with open(RESULTS, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            sh("docker", "stop", name(n), timeout=120)
        with lock:
            print(f"{time.strftime('%T')} [{n}] {row['verdict']:16} {sc['id']}", flush=True)
        TUNER.finished(row, log, started)


def batch(ids_file=None):
    """The scenarios in ids_file (every one without) not yet passed here: the
    cloud ones on AUTOFLASH_CLOUD_WORKERS emulators from instance 1, the rest on
    AUTOFLASH_ANY_WORKERS after them."""
    LOGS.mkdir(parents=True, exist_ok=True)
    known = {sc["id"]: sc for sc in scenarios()}
    done = {i for i, row in latest().items() if row["verdict"] in ("PASS", "EMULATOR-LIMIT", "NO-TRAFFIC")}
    cloud, anywhere = queue.Queue(), queue.Queue()
    ids = Path(ids_file).read_text(encoding="utf-8").split() if ids_file else list(known)
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
        na = int(os.environ.get("AUTOFLASH_STUB_WORKERS", os.environ.get("AUTOFLASH_ANY_WORKERS", "3")))
        first = int(os.environ.get("AUTOFLASH_FIRST_INSTANCE", "1"))
    else:
        nc = int(os.environ.get("AUTOFLASH_CLOUD_WORKERS", "0"))
        na = int(os.environ.get("AUTOFLASH_ANY_WORKERS", "0"))
        first = int(os.environ.get("AUTOFLASH_FIRST_INSTANCE", nc + 1))  # clear of another batch's emulators
    print(f"batch: {cloud.qsize()} cloud on {nc}, {anywhere.qsize()} others on {na}", flush=True)
    lock = threading.Lock()
    threads = [] if STUB_CLOUD else [threading.Thread(target=worker, args=(n, cloud, lock, True))
                                     for n in range(1, max(nc, TUNER.most) + 1)]
    most = max(na, int(os.environ.get("AUTOFLASH_MOST_WORKERS", na)))
    threads += [threading.Thread(target=worker, args=(n, anywhere, lock, False, lambda n: n - first < allowed(na)))
                for n in range(first, first + most)]
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
    n = int(os.environ.get("AUTOFLASH_PREP_INSTANCE", "1"))
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
        with open(log, "w", encoding="utf-8") as out:
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
    # The plug kept busiest by one Berry phase (plug_busy): a real S60TPG once
    # stopped answering at 70-90 s, so the longest are worth a look.
    busiest = sorted(((max(busy.values()), r["id"], step) for r in rows.values()
                      for busy in [r.get("notes", {}).get("plug_busy") or {}] if busy
                      for step, s in busy.items() if s == max(busy.values())), reverse=True)[:10]
    if busiest:
        print("Longest the plug was kept busy by one phase:")
        for seconds, scenario, step in busiest:
            print(f"  {seconds:6.1f}s  {step:3}  {scenario}")


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
    if STUB_CLOUD and "AUTOFLASH_SUITE_DIR" not in os.environ:
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
