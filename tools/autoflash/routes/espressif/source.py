"""What the plug runs when a run starts: found on the network by what it
answers as, or known from where an earlier run left it.
"""

from __future__ import annotations

import functools
import json
import re
import shutil
import time
from typing import TYPE_CHECKING

from tools.autoflash import detect, esphome, hostsys, kinds, output, plugs, prompts, runs, unlock
from tools.autoflash.device import DeviceError, PASSWORD_HINT
from tools.autoflash.errors import Abort
from tools.autoflash.routes.espressif import evidence
from tools.autoflash.state import ATTEMPTED, FAILED, PENDING, RunState

if TYPE_CHECKING:
    from tools.autoflash.context import Context


def known_macs() -> set[str]:
    """The MACs of the plugs this computer has records of: their backups and their runs."""
    return {name.replace("-", ":") for name in
            (path.name for path in plugs.PLUGS.glob("*")) if plugs.MAC_RE.match(name)}


def find_current_firmware(ctx: Context) -> str:
    """What the plug runs now: Tasmota, ESPHome or OpenBeken if it is one this
    tool can find converted on the network, else Sonoff's firmware (found
    later, through eWeLink).  With --plug-ip, the plug at that address."""
    given = ctx.state.input("plug_ip")
    if given:
        running = unlock.identify(ctx, given)
        if running == "locked":
            unlock.ask_web_password(ctx, given)
            running = unlock.identify(ctx, given)
        elif running == "esphome-login":
            unlock.ask_esphome_login(ctx, given)
            running = unlock.identify(ctx, given)
        mover = running == "mover"
        if running in ("mover", "startup", "hub"):
            running = "midway"
        mac = hostsys.mac_of(given)
        if running is None and mac and hostsys.normalise_mac(mac) in known_macs() \
                and detect.answers_as_tasmota(given):
            # A Tasmota without the plug's template, as the search finds it.
            running = "untemplated"
        if running in ("openbeken", "interim"):
            # Its kind is its import module's to say, as the search finds it.
            found = detect.openbeken_plug(given)
            if not found or found.firmware != running:
                raise Abort(f"the device at {given} did not say twice alike what it runs: run the same command "
                            "again")
            found.mac = mac
            adopt_plug(ctx, found)
            return running
        if running in ("tasmota", "esphome", "midway", "installer", "untemplated"):
            adopt_plug(ctx, detect.Found(given, running, mac))
            if mover:
                running = esphome_safe_mode(ctx, running)
            return past_the_commit(ctx, running)
        return "sonoff"
    # On screen while it looks - 5-10 s, before any stage line and its clock -
    # and taken off again once it has looked.
    output.transient_begin()
    output.tell("Looking for supported devices on your network...")
    known = known_macs()
    # The network this computer's internet goes through first; its other
    # Ethernet and Wi-Fi networks, read once, only if no plug is there.
    lan = functools.cache(hostsys.lan_networks)

    def search() -> list[detect.Found]:
        try:
            return detect.converted_plugs(hostsys.local_ip_towards("192.0.2.1"), others=lan,
                                          known_macs=known, password=ctx.web_password,
                                          esphome_login=ctx.esphome_login)
        except OSError:
            return []

    def open_ones(found: list[detect.Found]) -> list[detect.Found]:
        return [plug for plug in found if plug.firmware not in unlock.LOCKED]

    found = search()
    if not open_ones(found) and converted_here(ctx):
        # A plug this computer converted, or left part way, may be restarting -
        # a run stopped and started again at once finds it mid-restart.
        output.tell("    None answered. One this computer converted may be restarting: looking "
            f"again for up to {RESTART_GRACE:.0f} seconds...")
        started = time.monotonic()
        while not open_ones(found) and time.monotonic() - started < RESTART_GRACE:
            time.sleep(5)
            found = search()
    output.transient_end()
    # A Tasmota with a web password, or an ESPHome with a web login, says
    # whether it is a plug this tool converts only once given it: offered,
    # and it asked for if chosen, when someone can type it.
    locked = [plug for plug in found if plug.firmware in unlock.LOCKED]
    found = open_ones(found)
    if locked and prompts.UNATTENDED:
        ours = [plug for plug in locked if plug.mac and hostsys.normalise_mac(plug.mac) in known]
        if ours and not found:
            # A plug this computer converted: the one the run is for, surely.
            raise Abort(unlock.locked_out(ctx, ours[0]))
        for kind, given, what, hint in (("locked", ctx.web_password, "password", PASSWORD_HINT),
                                        ("esphome-login", ctx.esphome_login, "login", unlock.ESPHOME_LOGIN_HINT)):
            hosts = [plug.host for plug in locked if plug.firmware == kind]
            if hosts:
                output.tell(f"    Left out, for its web {what}{' (not the one given)' if given else ''}: "
                     f"the {'Tasmota' if kind == 'locked' else 'ESPHome'} at {', '.join(hosts)}. "
                     f"If it is the device, {hint}.")
        locked = []
    if not found and not locked:
        return "sonoff"
    stock = "A device still on Sonoff's firmware (it is set up in the eWeLink app)"
    if prompts.UNATTENDED and len(found) == 1:
        chosen = found[0]
        prompts.answered_unattended("Which device is this run for", chosen.describe())
    else:
        offered = found + locked
        output.transient_begin()
        choice = prompts.ask_choice("Which device is this run for",
                            [found_plug.describe()[0].upper() + found_plug.describe()[1:]
                             for found_plug in offered] + [stock])
        output.transient_end()
        if choice == len(offered):
            return "sonoff"
        chosen = offered[choice]
        if chosen.firmware in unlock.LOCKED:
            chosen = unlock.unlocked(ctx, chosen)
    adopt_plug(ctx, chosen)
    if chosen.firmware == "midway" and chosen.name == "the mover":
        return past_the_commit(ctx, esphome_safe_mode(ctx, chosen.firmware))
    return past_the_commit(ctx, chosen.firmware)


def past_the_commit(ctx: Context, running: str) -> str:
    """"committed" for a plug part way that a run of this computer's left past
    its commit: on Tasmota's layout, not Sonoff's, so the way on is official
    Tasmota first - never the hub's steps, which need Sonoff's two slots.
    An official Tasmota without the plug's template is one too, if a run left
    it so; any other is "untemplated", never "midway": the hub's steps begin
    by sending ESPHome an update, which no Tasmota takes."""
    if running not in ("midway", "untemplated"):
        return running
    mac = ctx.state.input("plug_mac")
    for directory in reversed(runs.run_directories()):
        if directory.resolve() == ctx.dir.resolve() or plugs.mac_of_run(directory) != mac:
            continue
        try:
            state = RunState.open(directory)
            report = json.loads((directory / "live" / "commit-report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {}
            try:
                state = RunState.open(directory)
            except (OSError, ValueError):
                continue
        committed = state.is_done("E3") or report.get("status") == "PASS"
        # Stopped as the commit ran (seen in the fault suite: Ctrl-C a second
        # after the plug fetched the new table), with no report: it finished if
        # the plug fetched the table and runs the startup firmware now, which
        # starts only once the new layout is in place - as E3's own check says.
        finished_alone = (not committed and state.status("E3") == ATTEMPTED
                          and evidence.table_fetched(directory / "live") and ctx.device.is_safeboot())
        if not (committed or finished_alone) and wrote_nothing(state):
            # Stopped before it wrote anything - X1 sent to a Tasmota, finding
            # no update service: seen hiding the run before it.
            continue
        if (committed or finished_alone) and not state.is_done("H1"):
            if finished_alone:
                output.say("    Its last run was stopped as the commit ran, and the device finished it:")
                output.say("    it fetched the new layout, and runs the startup firmware now.")
            else:
                output.say("    This computer's record of it says its last run got past the commit:")
            output.say("    it is on Tasmota's layout, so official Tasmota goes on first.")
            # G1 puts the official startup firmware back over the one that run
            # put on, and checks it is that one by the run's own record of it.
            ctx.evidence.mkdir(parents=True, exist_ok=True)
            for name in ("commit-report.json", "stage-report.json"):
                kept = directory / "live" / name
                if kept.is_file() and not (ctx.evidence / name).exists():
                    shutil.copy2(kept, ctx.evidence / name)
            if not state.is_done("D5") or state.input("startup_unproven"):
                # That run never put ESPHome's bootloader on - begun by 1.2.0 -
                # and its commit put that version's startup firmware on.
                ctx.state.set_input("bootloader_kept", "sonoff")
                ctx.state.set_input("startup_unproven", True)
            return "committed"
        return tasmota_missing(ctx, running)  # its latest run says otherwise
    return tasmota_missing(ctx, running)


# ESPHome started ten times in a row, each stopped within a minute, takes that
# for a boot loop and starts in safe mode: Wi-Fi and its update service, no
# page and no relay, as the mover has. Five minutes on, it restarts as itself.
ESPHOME_SAFE_MODE_WAIT = 400.0


def left_on_esphome(ctx: Context) -> bool:
    """Whether this computer's latest run of the plug that wrote to it left
    it on ESPHome, checked (I2): one past that, the mover is gone for good."""
    mac = ctx.state.input("plug_mac")
    for directory in reversed(runs.run_directories()):
        if directory.resolve() == ctx.dir.resolve() or plugs.mac_of_run(directory) != mac:
            continue
        try:
            state = RunState.open(directory)
        except (OSError, ValueError):
            continue
        if wrote_nothing(state):
            continue
        return runs.run_firmware(state) == "esphome" and state.is_done("I2")
    return False


def esphome_safe_mode(ctx: Context, running: str) -> str:
    """What a plug that answers as the mover runs: ESPHome in safe mode, if
    a run left it on ESPHome, waited out until it restarts as itself - taken
    for part way, it was sent the startup firmware, even to stay on ESPHome."""
    if not left_on_esphome(ctx):
        return running
    host = ctx.state.input("plug_ip")
    output.say("    It answers only ESPHome's update service, though this computer left it on")
    output.say("    ESPHome: ESPHome in safe mode, after starting ten times in a row, each")
    output.say("    stopped within a minute. It restarts as itself within five minutes.")
    output.say("    Waiting for it - leave it switched on...")
    started = time.monotonic()
    while time.monotonic() - started < ESPHOME_SAFE_MODE_WAIT:
        found = unlock.identify(ctx, host)
        if found == "esphome-login":
            unlock.ask_esphome_login(ctx, host)
            found = unlock.identify(ctx, host)
        if found == "esphome":
            output.say(f"    ESPHome answered after {time.monotonic() - started:.0f}s.")
            return "esphome"
        time.sleep(5)
    raise Abort(
        "the device answers only ESPHome's update service, and has not started ESPHome "
        f"in {ESPHOME_SAFE_MODE_WAIT / 60:.0f} minutes. Leave it switched on for ten "
        "minutes, then run the same command again")


def wrote_nothing(state: RunState) -> bool:
    """Whether a run wrote nothing to the plug: no step that writes done or
    part way, and each that failed stopped before writing."""
    steps = state.data.get("steps", {})
    return all(state.status(step) == PENDING
               or (state.status(step) == FAILED and steps.get(step, {}).get("nothing_written"))
               for step in evidence.DESTRUCTIVE_STEPS)


# Tasmota's own safeboot, as a build of Tasmota's own says it is - "15.6.0
# (release-safeboot)" - where this tool's startup firmware, built from the
# same source here, says "15.6.0(safeboot)". Tasmota's own runs only on
# Tasmota's layout: no route puts it in Sonoff's slots, only G1 into Tasmota's
# safeboot partition. Its page is all it serves: no commands, no Information.
OFFICIAL_SAFEBOOT_RE = re.compile(r"\(\s*[a-z0-9]+-safeboot\s*\)", re.IGNORECASE)


def tasmota_missing(ctx: Context, running: str) -> str:
    """"safeboot" for a plug part way by the look of it - a safeboot answering -
    that no run of this computer's left past the commit, and whose safeboot is
    Tasmota's own: a converted plug, on Tasmota's layout, not starting its
    Tasmota (its own upload of another firmware failed part way, say, or it
    was switched to its safeboot). Taken for part way, it went down steps that
    need Sonoff's layout, and stopped at D4."""
    if running != "midway":
        return running
    try:
        version = ctx.device.firmware_version() if ctx.device.is_safeboot() else ""
    except (DeviceError, OSError):
        version = ""
    if not OFFICIAL_SAFEBOOT_RE.search(version or ""):
        return running
    output.say(f"    It runs Tasmota's own safeboot ({version}), so is on Tasmota's layout: its Tasmota")
    output.say("    is started again, or put back, first.")
    return "safeboot"


RESTART_GRACE = 45.0


def converted_here(ctx: Context) -> bool:
    """Whether this computer has a record of converting a plug, other than this run."""
    for directory in runs.run_directories():
        if directory.resolve() == ctx.dir.resolve():
            continue
        try:
            if RunState.open(directory).input("firmware"):
                return True
        except (OSError, ValueError):
            continue
    return False


def adopt_plug(ctx: Context, found: "detect.Found") -> None:
    """This run is for the plug `found`: its address, and its MAC to file it by."""
    ctx.set_plug_ip(found.host)
    mac = found.mac or hostsys.mac_of(found.host)
    if mac and plugs.MAC_RE.match(mac):
        mac = hostsys.normalise_mac(mac)
        ctx.state.set_input("plug_mac", mac)
        runs.file_plug(ctx, mac, None)
    kind = found.kind
    if kind is None and found.firmware == "esphome":
        # Every kind's ESPHome looks the same but for its comment: an S61's
        # run must never take the S60's way.
        named = kinds.for_esphome(*esphome.title(found.host))
        kind = named.key if named else None
    if kind:
        ctx.state.set_input("plug_kind", kind)
    output.say(f"    This run is for {found.describe()}.")
