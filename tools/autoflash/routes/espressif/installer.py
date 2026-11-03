"""From Sonoff's firmware to the interim Tasmota: the installer (the
bridge) sent through eWeLink, the backup read through it, and Tasmota
uploaded through it - steps C2, C3 and D1.
"""

from __future__ import annotations

import time
import urllib.error
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from tools.espressif import dump_stock_flash
from tools.autoflash import (
    detect,
    device,
    firewall,
    hostsys,
    interrupt,
    join,
    kinds,
    names,
    output,
    plugs,
    prompts,
    runs,
    safety_locks,
    setup_network,
    tasmota_settings,
)
from tools.autoflash.device import DeviceError
from tools.autoflash.errors import Abort, NothingWritten
from tools.autoflash.runs import ROOT
from tools.autoflash.state import ATTEMPTED, DONE, FAILED
from tools.espressif.safeboot.safeboot_migration import PINNED_ARTIFACTS

if TYPE_CHECKING:
    from tools.autoflash.context import Context

# 4.4.4: joins the home network the stock firmware was on, raising its own
# only if that fails, gives Tasmota its Wi-Fi at D1, and lets the plug's whole
# flash be read first, for the backup - and starts even once an upload into
# Sonoff's slot was cut off (from 4.4.3), where 4.4.2 would not, leaving a plug
# only a serial cable could recover; named, from 4.4.4, as the tool's.  Named,
# like every bridge from 4.4.1 on, by the version the stock updater is given;
# the file by the kind of plug its wrapper is for (kinds.SONOFF_S60's).
BRIDGE_OTA = Path("artifacts/autoflash-bridge-sonoff-s60-4.4.4.ota")


# What the sender says when the upgrade command never went out
# (serve_tasmota_ota.NOT_SENT).
BRIDGE_NOT_SENT = "The upgrade command was not sent"


def bridge_failure_advice(ctx: Context, reported: list[str], started: bool) -> list[str]:
    """What to do about a send of the installer that failed, by how far it got."""
    if started:
        return [
            "The device started downloading it from this computer, but stopped part",
            "way - most likely its Wi-Fi dropped: a router restarting, or a weak",
            "signal. It keeps its Sonoff firmware, so nothing is lost; run this",
            "again once its Wi-Fi is steady.",
        ]
    if output.tool_said("offline in eWeLink"):
        return [
            "eWeLink does not see the device, so the installer cannot be sent. It",
            "keeps its Sonoff firmware, so nothing is lost: switch the device off",
            "and on, wait for it to show online in the eWeLink app, and run this",
            "again.",
        ]
    if output.tool_said(BRIDGE_NOT_SENT):
        # Seen against the real cloud: eWeLink's servers dropping connections,
        # and the run blamed this computer's firewall for a plug never asked.
        if any("refusing" in line.lower() for line in reported):
            return ["Nothing was sent to the device, so it keeps its Sonoff firmware."]
        return [
            "This computer's connection to eWeLink failed before the device was told",
            "to fetch the installer - eWeLink's servers, or this computer's",
            "internet, having trouble. Nothing was sent, so the device keeps its",
            "Sonoff firmware: run this again in a few minutes.",
        ]
    return firewall.firewall_hint(ctx)


def step_bridge(ctx: Context) -> dict[str, Any]:
    """C - stock to recovery bridge.  First write; no rollback below this line."""
    plug_ip, version = ctx.state.require("plug_ip", "stock_version")
    listen_ip = ctx.listen_ip()
    email = ctx.email()
    port = firewall.choose_port(ctx, "delivery_port", firewall.DELIVERY_PORTS, "the firmware")
    firewall.ensure_firewall(ctx)
    env = ctx.ewelink_env()

    mac = ctx.state.input("plug_mac")
    backup = plugs.find_backup(mac) if mac else None
    if backup:
        kept = f"Its Sonoff firmware is backed up already ({backup.taken or 'earlier'})."
    elif getattr(getattr(ctx, "args", None), "no_backup", False):
        kept = "No backup of its Sonoff firmware is kept (--no-backup)."
    else:
        kept = "Its Sonoff firmware is backed up to this computer first, in full."
    output.transient_begin()
    prompts.warning_box("Before going on", [
        "From here the device's Sonoff firmware is replaced, so the eWeLink app",
        "will no longer control it. DO NOT UNPLUG IT during the process.",
        kept,
        "Only go ahead with a device you can afford to lose.",
    ])
    if not prompts.require_word(prompts.CONSENT_WORD):
        raise NothingWritten("not confirmed - nothing has been written to the device")
    output.transient_end()  # agreed: the note and the question have done their job

    started = time.monotonic()
    with safety_locks.lifted(ROOT, "RECOVERY_LOCK"):
        code = hostsys.run_tool(
            hostsys.python_script(
                "tools/espressif/serve_tasmota_ota.py",
                "--listen-ip", listen_ip, "--listen-port", str(port),
                "--device-ip", plug_ip,
                "--firmware", str(ctx.bridge),
                "--email", email, "--expected-current-version", version,
                "--mdns-capture", str(ctx.dir / "mdns.json"),
                "--i-understand-stock-has-no-automatic-rollback"),
            interactive=True,
            env=env,
        )
    safety_locks.say_lock_back(ROOT, "RECOVERY_LOCK")
    if code != 0:
        # The sender can fail after the plug already has everything - the cloud
        # reporting it offline is exactly what a successful install looks like.
        installed, reason = evidence_bridge_installed(ctx)
        if installed:
            output.say(f"  {reason}")
            return {"seconds": round(time.monotonic() - started, 1), "confirmed_by": "bridge network"}
        reported = output.tool_problems()
        # Some of it served means the plug reached this computer: not the
        # firewall, but its Wi-Fi going part way (a router restarting, say).
        started = output.tool_said("Served /user") or output.tool_said("did not request every")
        prompts.section("The bridge was not delivered", [
            *(["The sender reported:", *(f"  {line}" for line in reported), ""] if reported else []),
            *bridge_failure_advice(ctx, reported, started),
        ])
        raise Abort("bridge delivery stopped part way" if started else "bridge delivery failed")
    return {"seconds": round(time.monotonic() - started, 1)}


def bridge_on_home_network(ctx: Context, *, timeout: float = 60.0, tasmota: bool = False) -> str | None:
    """The bridge's address on the home network, if it joined it - or, with
    `tasmota`, TASMOTA_HOME if Tasmota is there instead (reach_bridge).

    Bridge v4 and later join the network the stock firmware was using.  It has
    the plug's own hardware address there, so it is found where the plug was.
    """
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        address = ctx.finder.poll()
        if address:
            body = setup_network.page(address, timeout=3.0)
            if setup_network.is_bridge_page(body):
                output.progress_clear()
                return address
            if tasmota and setup_network.is_tasmota_page(body) and tasmota_at_home(ctx):
                return TASMOTA_HOME
        output.progress(f"looking for the installer on your network... {time.monotonic() - started:.0f}s")
        time.sleep(2.0)
    output.progress_clear()
    return None


# Sent again at most this many times in a run, as the commit is tried: a plug
# whose power keeps going is told about, not sent the installer for ever.
BRIDGE_SENDS = 3


def back_on_sonoff(ctx: Context) -> bool:
    """Whether the plug answers as Sonoff's own firmware."""
    host = ctx.state.input("plug_ip") or ctx.finder.poll()
    return bool(host) and detect.identify(host) == "sonoff"


def bridge_answers(host: str) -> bool:
    body = setup_network.page(host, timeout=2.0)
    return setup_network.is_bridge_page(body)


def reach_bridge(ctx: Context, *, tasmota: bool = False) -> str:
    """The bridge's address: setup_network.SETUP_HOST when this computer is on
    the bridge's own network, else where it joined the home network.  Asks the
    person to join the bridge's network only when it raised one.

    With `tasmota` (D1), Tasmota found instead, installed already - by a run
    cut off after the upload, or an upload that took though its answer was
    lost (a weak signal, seen in the fault suite): TASMOTA_HOME once it
    answers on the home network, its address noted (it joins by itself with
    the Wi-Fi the installer gave it), or TASMOTA_THERE on its own network -
    named as the installer was (tasmota_named_alone), by the same name."""
    if bridge_answers(setup_network.SETUP_HOST):
        return setup_network.SETUP_HOST  # joined at C3, or by hand
    if tasmota and setup_network.tasmota_answers():
        return TASMOTA_THERE
    address = bridge_on_home_network(ctx, tasmota=tasmota)
    if address == TASMOTA_HOME:
        return address
    if address:
        output.say(f"    The installer joined your network at {address}.")
        return address
    # The installer hands the plug back to Sonoff's firmware when it is switched
    # off and on before its work is done - by design, so a power cut never
    # strands it.  Waiting for the installer then waits for ever: looked for
    # before the wait, and during it.
    asked = False
    while True:
        address = resend_if_back_on_sonoff(ctx)
        if address:
            return address
        bridge = prompts.find_ssid(names.bridge_networks(ctx)) or names.our_setup_network(ctx)
        if not asked:
            ask_to_join_bridge(bridge, tasmota=tasmota and bool(ctx.state.input("tasmota_named_alone")))
            asked = True

        def gone_back() -> bool:
            return getattr(ctx, "bridge_sends", 0) < BRIDGE_SENDS and back_on_sonoff(ctx)

        found = wait_for_bridge(ctx, bridge.split()[0], gone_back=gone_back, tasmota=tasmota)
        if found != BACK_ON_SONOFF:
            return found or setup_network.SETUP_HOST
        output.tell("    While waiting, the device went back to Sonoff's own firmware (switched off")
        output.tell("    and on, say).")


def resend_if_back_on_sonoff(ctx: Context) -> str | None:
    """The installer sent again while the plug is back on Sonoff's firmware, up
    to BRIDGE_SENDS in a run; its address once it joined the home network."""
    sends = getattr(ctx, "bridge_sends", 0)
    # A send that failed left the plug on Sonoff's firmware, whether or not it
    # answers here this moment (offline in eWeLink, it can be slow to): the
    # next goes ahead - never on to asking for the installer's network.
    failed = False
    while sends < BRIDGE_SENDS and (failed or back_on_sonoff(ctx)):
        failed = False
        if not ctx.state.input("stock_version"):
            # A run started on the installer has none of eWeLink's details.
            raise Abort("the device is back on Sonoff's own firmware, so the installer has to "
                        "be sent again through eWeLink: run this again with --new")
        sends += 1
        ctx.bridge_sends = sends
        output.tell("    The device is running Sonoff's own firmware again: it goes back to it when")
        output.tell("    switched off and on before the installer has done its work, so nothing")
        output.tell(f"    is lost. Sending the installer again (attempt {sends} of {BRIDGE_SENDS}).")
        ctx.state.mark("C2", FAILED, error="redo: the device went back to Sonoff's firmware")
        try:
            ctx.state.mark("C2", DONE, result=step_bridge(ctx))
        except Abort as exc:
            # Cut off part way, the plug keeps Sonoff's firmware: another go,
            # while there are goes left.
            if sends == BRIDGE_SENDS:
                raise
            output.tell(f"    {exc}.")
            failed = True
            continue
        address = bridge_on_home_network(ctx, timeout=120.0)
        if address:
            output.say(f"    The installer joined your network at {address}.")
            return address
    return None


def ask_to_join_bridge(bridge: str, *, tasmota: bool = False) -> None:
    if bridge.startswith(names.LEGACY_BRIDGE_PREFIX):
        # An earlier release's installer: its network has a password.
        copied = hostsys.copy_to_clipboard(names.LEGACY_BRIDGE_PASSWORD)
        joining = [f"    network   {bridge}",
                   f"    password  {names.LEGACY_BRIDGE_PASSWORD}"
                   + ("      (already copied - just paste it)" if copied else "")]
    else:
        joining = [f"    {bridge}      (open - no password)"]
    prompts.section("Join the bridge's Wi-Fi network", [
        *(["The device is running our small installer - or Tasmota, if the installer",
           "had sent it before the run stopped: either has made its own Wi-Fi network,",
           f"by the same name. In {hostsys.wifi_menu_hint()}, join:"] if tasmota else
          ["The device is running our small installer, which has made its own Wi-Fi",
           f"network. In {hostsys.wifi_menu_hint()}, join:"]),
        *joining,
        "",
        "Your Wi-Fi internet drops while joined - expected. A wired connection,",
        "if this computer has one, can stay connected. This carries on by itself.",
        "",
        "If that network is not there, wait: the installer may still be joining",
        "your own Wi-Fi, and this carries on by itself when it does. Until",
        "someone joins it, it goes for up to 20 seconds every three minutes, to",
        "try your Wi-Fi again.",
    ])


# The steps after the backup: once any has started, the installer it reads
# through may be gone.
AFTER_BACKUP = ("D1", "D4", "E1", "E2", "E3", "F2", "F4", "G1", "H1")


def step_backup(ctx: Context) -> dict[str, Any]:
    """C3 - the plug's Sonoff firmware, kept whole, before D1 writes over it.
    Its stage is "skipped" when no backup could be taken, as the conversion
    does not need one."""
    if any(ctx.state.status(step) in (DONE, ATTEMPTED) for step in AFTER_BACKUP):
        output.say("    The device is past the installer already, so there is nothing to back up from.")
        outcome = "not taken"
    else:
        skip = backup_not_needed(ctx)
        if skip:
            outcome, message = skip
            output.say(f"    {message}")
        else:
            outcome = back_up_stock(ctx, reach_bridge(ctx))
    ctx.state.set_input("backup_outcome", outcome)
    return {"outcome": outcome, "stage_outcome": plugs.BACKUP_OUTCOMES[outcome][0]}


def step_berry_tasmota(ctx: Context) -> dict[str, Any]:
    """D1/D2 - Berry-capable Tasmota through the bridge."""
    image = ROOT / "captures/safeboot-recovery-migration/tasmota32c3-bluetooth.bin"
    output.say("Looking for the device's Wi-Fi network...")
    bridge = prompts.find_ssid(names.bridge_networks(ctx))
    setup = None if bridge else prompts.find_ssid(names.setup_networks(ctx))
    if setup:
        # Resuming after the upload: Tasmota is already installed and waiting.
        output.say(f"    Tasmota is already installed and waiting on {setup}.")
        names.note_setup_network(ctx, setup)
        ip = join.configure_plug_wifi(ctx, setup)
        return {"plug_ip": ip, "device_name": setup, "resumed": True}

    address = reach_bridge(ctx, tasmota=True)
    if address == TASMOTA_HOME:
        output.say("    Tasmota is already installed, and joined your network by itself.")
        return {"plug_ip": ctx.state.input("plug_ip"), "upload_route": "home network", "wifi": "preset",
                "device_name": ctx.state.input("device_name"), "resumed": True}
    if address == TASMOTA_THERE:
        # Run again after the upload: Tasmota is waiting on its own network -
        # the one the installer had, by the same name, when it was named so.
        network = names.our_setup_network(ctx) if ctx.state.input("tasmota_named_alone") else None
        output.say(f"    Tasmota is already installed and waiting on {network or 'its own network'}.")
        ip = join.configure_plug_wifi(ctx, network)
        return {"plug_ip": ip, "device_name": ctx.state.input("device_name"), "resumed": True}
    if address == setup_network.SETUP_HOST:
        if not ctx.state.input("tasmota_wifi_preset"):
            give_tasmota_the_wifi(ctx, address)  # its name, and the Wi-Fi if proven
        upload_tasmota(image)
        if ctx.state.input("tasmota_wifi_preset"):
            # An earlier try, cut off, had the bridge save Tasmota's settings
            # with your Wi-Fi: Tasmota joins it by itself, with no setup network
            # of its own, even sent through the bridge's.
            output.say("    Tasmota will start with your Wi-Fi details, so it joins by itself.")
            prompts.section("Back to your usual Wi-Fi", [
                "Your computer should rejoin your usual Wi-Fi by itself; if it doesn't,",
                "join it now.",
            ])
            return finish_tasmota_setup(ctx, route="bridge network", wait=240.0)
        return finish_tasmota_setup(ctx, route="bridge network")
    preset = give_tasmota_the_wifi(ctx, address)
    upload_tasmota(image, host=address)
    if preset:
        found = tasmota_on_home_network(ctx)
        if found:
            return {"plug_ip": found, "upload_route": "home network", "wifi": "preset",
                    "device_name": ctx.state.input("device_name")}
        prompts.section("Tasmota has not appeared on your network", quick_power_cycle_hint(ctx))
        return finish_tasmota_setup(ctx, route="home network", wait=900.0)
    return finish_tasmota_setup(ctx, route="home network")


def backup_not_needed(ctx: Context) -> tuple[str, str] | None:
    """Why no backup is to be read, if none is, as (outcome, message): one
    taken already, or none wanted."""
    if ctx.state.input("stock_backup"):
        return ctx.state.input("backup_outcome") or "existing", "Backed up before this run was resumed."
    if getattr(getattr(ctx, "args", None), "no_backup", False):
        return "not wanted", "No backup of the Sonoff firmware (--no-backup)."
    known = ctx.state.input("plug_mac")
    existing = plugs.find_backup(known) if known else None
    if existing:
        # The first backup is the original; a plug converted again keeps it.
        ctx.state.set_input("stock_backup", runs.display_path(existing.directory))
        return "existing", f"Its Sonoff firmware is backed up already, in {runs.display_path(existing.directory)}."
    return None


# Rounds of two reads at the backup before the person is asked what now: the
# reads only read, so another round is safe, and is what they would choose.
BACKUP_ROUNDS = 2
# Testing only (run --simulate-backup-failure): how many backup reads still
# to fail.  Every round's (2 * BACKUP_ROUNDS), so the menu that follows can be tried.
SIMULATED_BACKUP_FAILURES = 0
BACKUP_RETRY, BACKUP_WITHOUT = 0, 1


def read_for_backup(host: str) -> tuple[bytes, bytes, dict[str, Any]]:
    global SIMULATED_BACKUP_FAILURES
    if SIMULATED_BACKUP_FAILURES > 0:
        SIMULATED_BACKUP_FAILURES -= 1
        raise OSError("simulated failure (--simulate-backup-failure)")
    return dump_stock_flash.read_dump(host)


def backup_failed(problem: str) -> int:
    """Ask what to do about a backup that failed twice: BACKUP_RETRY or
    BACKUP_WITHOUT; Quit stops the run, which carries on from here next time."""
    options = ["Try again", "Carry on without a backup"]
    output.transient_begin()
    prompts.section("The Sonoff firmware could not be backed up", [
        f"Reading the device's memory failed {2 * BACKUP_ROUNDS} times: {problem}.",
        "",
        "The conversion can be performed without a backup, though that's not recommended.",
    ])
    if prompts.UNATTENDED:
        prompts.answered_unattended("What now", options[BACKUP_WITHOUT])
        choice = BACKUP_WITHOUT
    else:
        choice = prompts.ask_choice("What now", options, help_lines=[
            "Quitting leaves the device on the installer: run this again to carry on",
            "from here, or switch the device off and on to go back to Sonoff.",
        ])
    output.transient_end()
    return choice


def back_up_stock(ctx: Context, host: str) -> str:
    """Keep the plug's whole flash, Sonoff firmware and all, before D1 writes over it.

    The bridge only reads it.  Taken automatically, under the consent typed at
    C2; if it fails the person decides whether to carry on without one, and an
    unattended run carries on.  Returns how it went: a plugs.BACKUP_OUTCOMES key.
    """
    skip = backup_not_needed(ctx)
    if skip:
        output.say(f"    {skip[1]}")
        return skip[0]
    known = ctx.state.input("plug_mac")
    output.say("Backing up the device's Sonoff firmware - about a minute...")
    first = True
    rounds = 0
    while True:
        problem = ""
        for _ in range(2):
            if not first:
                # Read where the installer is now: when the home network goes
                # it raises its own, where it is read once this computer has
                # joined that - not again at the address it had (seen in the
                # fault suite: both reads timed out there, and an unattended
                # run went on without a backup).
                host = reach_bridge(ctx)
            first = False
            try:
                flash, efuse, report = read_for_backup(host)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    # A bridge from before 4.4.2, given with --bridge.
                    output.say("    This installer cannot read the device's memory, so no backup is kept.")
                    return "unreadable"
                problem = str(exc)
            except OSError as exc:  # a short transfer included
                problem = str(exc)
            output.say(f"    The backup did not come through: {problem}")
        else:
            rounds += 1
            if rounds < BACKUP_ROUNDS:
                output.say("    Trying the backup again...")
                continue
            rounds = 0
            if backup_failed(problem) == BACKUP_RETRY:
                output.say("    Trying the backup again...")
                continue
            output.say("    Carrying on without a backup.")
            return "failed"
        break

    mac = plugs.mac_from_efuse(efuse)
    if known and plugs.folder_name(known) != plugs.folder_name(mac):
        output.say(f"    The network reported {known} for the device; its chip says {mac}, which is used.")
    if hasattr(ctx, "file_under_plug"):
        ctx.file_under_plug(mac)  # if A1 could not tell its MAC
    out, _ = plugs.save_backup(flash, efuse, report, ctx.state.input("stock_version"),
                               kind=kinds.of_run(ctx.state).name)
    ctx.state.set_input("stock_backup", runs.display_path(out))
    for line in dump_stock_flash.summary(report):
        output.say(f"    {line}")
    output.say(f"    Saved in {runs.display_path(out)}, under the device's MAC (private: it holds the")
    output.say("    Wi-Fi password).")
    if not dump_stock_flash.is_good(report):
        output.tell("    It did not check out as a whole, bootable flash, so it may not restore;")
        output.tell("    its report.json says what was wrong. Carrying on.")
    return "saved"


# What wait_for_bridge says when the plug went back to Sonoff's firmware meanwhile.
BACK_ON_SONOFF = "back on Sonoff's firmware"
# What reach_bridge says when Tasmota answers instead, installed already: on
# the installer's network (by the same name), or on the home network.
TASMOTA_THERE = "Tasmota on the installer's network"
TASMOTA_HOME = "Tasmota on the home network"
GONE_BACK_EVERY = 30.0
# Nothing at all at the plug's address this long, and the person is told to
# switch it off and on: Sonoff's firmware, its Wi-Fi gone while it started, does
# not try it again (seen after an outage), and a restart is safe here - the
# installer carries on (4.4.3), or the plug starts Sonoff's firmware again.
NOTHING_THERE_HINT_AFTER = 120.0


def wait_for_bridge(ctx: Context, network: str, *, timeout: float | None = None,
                    gone_back: Callable[[], bool] | None = None, tasmota: bool = False) -> str | None:
    with interrupt.asking():  # waiting on the person: Ctrl-C stops at once
        return _wait_for_bridge(ctx, network, timeout=timeout, gone_back=gone_back, tasmota=tasmota)


def _wait_for_bridge(ctx: Context, network: str, *, timeout: float | None = None,
                     gone_back: Callable[[], bool] | None = None, tasmota: bool = False) -> str | None:
    """Wait for this computer to join the bridge's network, or for the bridge to
    turn up on the home network instead.  The bridge's address on the home
    network, or None once this computer has joined the bridge's own - or,
    with `tasmota`, TASMOTA_THERE or TASMOTA_HOME once Tasmota answers on it
    or on the home network instead (reach_bridge).

    A bridge on a weak signal can take longer to join the home network than
    the first look allows; it then never raises its own network, so waiting
    only for that would wait forever.
    """
    if timeout is None:
        timeout = join.user_action_limit()
    started = time.monotonic()
    looked = started
    heard = started  # the last time anything answered at the plug's address
    hinted = False
    while time.monotonic() - started < timeout:
        host = ctx.state.input("plug_ip")
        if host and hostsys.ping_ok(host):
            heard = time.monotonic()
        elif not hinted and time.monotonic() - heard >= NOTHING_THERE_HINT_AFTER:
            hinted = True
            output.progress_clear()
            output.tell("    Nothing is answering at the device's address. If it is not on your Wi-Fi at all -")
            output.tell("    after a Wi-Fi outage, Sonoff's firmware can stay off it - switch it off and on:")
            output.tell("    at this stage that is safe. This carries on by itself.")
        if gone_back is not None and time.monotonic() - looked >= GONE_BACK_EVERY:
            looked = time.monotonic()
            if gone_back():
                output.progress_clear()
                return BACK_ON_SONOFF
        if tasmota and setup_network.tasmota_answers():
            output.progress_clear()
            return TASMOTA_THERE
        body = setup_network.page(setup_network.SETUP_HOST, timeout=2.0)
        if setup_network.is_bridge_page(body):
            output.progress_clear()
            output.say(f"    Connected to {network}.")
            return None
        address = ctx.finder.poll()
        if address:
            body = setup_network.page(address, timeout=3.0)
            if setup_network.is_bridge_page(body):
                output.progress_clear()
                return address
            if tasmota and setup_network.is_tasmota_page(body) and tasmota_at_home(ctx):
                return TASMOTA_HOME
        output.progress(f"waiting for this computer to join {network}, or the installer "
                        f"to join your Wi-Fi... {time.monotonic() - started:.0f}s")
        time.sleep(2.0)
    output.progress_clear()
    raise Abort(f"this computer did not join {network} within {timeout / 60:.0f} minutes, "
                "and the installer did not appear on your network")


# Tasmota with Wi-Fi details that do not work keeps retrying them and never
# opens its setup network.  Its quick power-cycle reset is the way out:
# settings.ino UpdateQuickPowerCycle, 7 boots in a row each under
# POWER_CYCLE_TIME (8 s), erases its settings and it starts as new.
def quick_power_cycle_hint(ctx: Context) -> list[str]:
    return [
        "It was given the Wi-Fi details the installer had just joined with, so",
        "this is unusual. If a few more minutes pass and it still has not appeared:",
        "",
        "  Switch the device off and on 7 times in a row, leaving it on for only",
        "  2-3 seconds each time, then leave it on.",
        "",
        "That makes Tasmota clear its settings and open its setup network",
        f"({names.setup_network_hint(ctx)}). This carries on from there by itself.",
    ]


def give_tasmota_the_wifi(ctx: Context, address: str) -> bool:
    """Have bridge v5 save Tasmota's settings with the Wi-Fi it joined with -
    and named as every firmware part way through is (autoflash-recovery-CDEF),
    until F4's reset gives it Tasmota's own name.  True when they have the
    Wi-Fi: on its own network the bridge has joined none, and 4.4.4 saves them
    without (tasmota_named_alone) - Tasmota then makes its own network under
    that name, not its default (tasmota-ABCDEF-1234)."""
    template, reason = tasmota_settings.load_template(ROOT, PINNED_ARTIFACTS["bluetooth"]["sha256"])
    alone = address == setup_network.SETUP_HOST
    if template is None:
        if not alone:
            output.say(f"    Tasmota will need its Wi-Fi given by hand: {reason}.")
        return False
    if ctx.state.input("plug_mac"):
        template = tasmota_settings.named(template, names.our_setup_network(ctx))
    elif alone:
        return False  # nothing to give it: the Wi-Fi is given on its network, as ever
    try:
        answer = setup_network.send_tasmota_settings(template, host=address,
                                                     layout=tasmota_settings.layout_query())
    except setup_network.SetupError as exc:
        if not alone:  # an installer before 4.4.4 refuses them there: Tasmota's own name
            output.say(f"    Tasmota will need its Wi-Fi given by hand: {exc}")
        return False
    if setup_network.SETTINGS_WITH_WIFI not in answer:
        ctx.state.set_input("tasmota_named_alone", True)
        output.say(f"    Tasmota will start as {names.our_setup_network(ctx)}, and make its own network by that name.")
        return False
    # Kept for the run: saved in the bridge, they outlast an upload cut off.
    ctx.state.set_input("tasmota_wifi_preset", True)
    ctx.state.set_input("tasmota_named_alone", False)
    output.say("    Tasmota will start with your Wi-Fi details, so it joins by itself.")
    return True


def tasmota_on_home_network(ctx: Context, *, timeout: float = 240.0) -> str | None:
    """Tasmota's address once it has started and joined, at the plug's own MAC."""
    output.say("Waiting for Tasmota to start and join your network...")
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        address = tasmota_at_home(ctx)
        if address:
            return address
        output.progress(f"waiting for Tasmota on your network... {time.monotonic() - started:.0f}s")
        time.sleep(3.0)
    output.progress_clear()
    return None


def tasmota_at_home(ctx: Context) -> str | None:
    """Tasmota's address if it answers on the home network now, at the plug's
    own MAC - the bridge's page, there before it, is not Tasmota's."""
    address = ctx.finder.poll()
    body = setup_network.page(address, timeout=3.0) if address else None
    if not setup_network.is_tasmota_page(body):
        return None
    output.progress_clear()
    ctx.set_plug_ip(address)
    try:
        status = device.Device(address).command("Status 5")
        name = status.get("StatusNET", {}).get("Hostname") if isinstance(status, dict) else None
    except (DeviceError, OSError):
        name = None
    if name and not name.startswith(names.SETUP_SSID_PREFIX):  # a name it keeps
        ctx.state.set_input("device_name", name)
    output.say(f"    Tasmota is on your network at {address}" + (f" as {name}." if name else "."))
    return address


def upload_tasmota(image: Path, host: str = setup_network.SETUP_HOST) -> None:
    output.say(f"    Sending Tasmota ({image.stat().st_size:,} bytes) - about a minute...")
    started = time.monotonic()
    try:
        setup_network.upload_to_bridge(image, host=host)
    except setup_network.SetupError as exc:
        raise Abort(f"{exc}. The device still has the installer; run this again") from exc
    output.say(f"    Upload verified in {time.monotonic() - started:.0f}s; the device is restarting into Tasmota.")


def tasmota_or_its_network(ctx: Context, *, timeout: float,
                           networks: tuple[str, ...] | None = None) -> tuple[str, str] | None:
    """("network", its name) once Tasmota's setup network shows - one of
    `networks`, setup_networks' without - or ("home", its address) if Tasmota
    is on the home network instead: with settings the bridge saved earlier, it
    joins by itself and raises none."""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        name = prompts.find_ssid(networks or names.setup_networks(ctx), timeout=8.0)
        if name:
            output.progress_clear()
            return "network", name
        address = tasmota_at_home(ctx)
        if address:
            return "home", address
        output.progress(f"looking for Tasmota's setup network... {time.monotonic() - started:.0f}s")
        time.sleep(4.0)
    output.progress_clear()
    return None


def finish_tasmota_setup(ctx: Context, *, route: str, wait: float = 120.0) -> dict[str, Any]:
    output.say("")
    output.say("Waiting for Tasmota to start and make its setup network...")
    networks = names.setup_networks(ctx)
    if ctx.state.input("tasmota_named_alone"):
        # Named as the installer was, Tasmota's network has the installer's
        # name: looked for once the installer has gone, so that its network,
        # still listed for a moment, is not taken for Tasmota's.
        installer_gone(timeout=60.0)
        networks = (names.our_setup_network(ctx), *networks)
    found = tasmota_or_its_network(ctx, timeout=wait, networks=networks)
    if found and found[0] == "home":
        return {"plug_ip": found[1], "upload_route": route, "wifi": "preset",
                "device_name": ctx.state.input("device_name")}
    network = found[1] if found else None
    if network and network.startswith(names.TASMOTA_AP_PREFIX):
        names.note_setup_network(ctx, network)
        output.say(f"    This device is {network} - the same name shows in your router.")
    ip = join.configure_plug_wifi(ctx, network)
    named = network if network != names.our_setup_network(ctx) else ctx.state.input("device_name")
    return {"plug_ip": ip, "device_name": named, "upload_route": route}


def installer_gone(*, timeout: float) -> None:
    """Wait, up to `timeout`, until the installer no longer answers on its own
    network: restarting into Tasmota once the upload is verified."""
    started = time.monotonic()
    while time.monotonic() - started < timeout and bridge_answers(setup_network.SETUP_HOST):
        output.progress(f"waiting for the installer to restart into Tasmota... "
                        f"{time.monotonic() - started:.0f}s")
        time.sleep(2.0)
    output.progress_clear()


def evidence_bridge_installed(ctx: "Context") -> tuple[bool | None, str]:
    """Only the bridge serves its page, and only it broadcasts that network.

    Bridge 4.4.1 raises its own network only when it cannot join the home one,
    so it is looked for on the home network first.
    """
    address = bridge_on_home_network(ctx, timeout=60.0)
    if address:
        return True, (
            f"The installer firmware is answering on your network at {address},\n"
            "  so this step did finish."
        )
    bridge = prompts.find_ssid(names.bridge_networks(ctx))
    if bridge:
        return True, (
            f"The device is broadcasting a Wi-Fi network called '{bridge}'.\n"
            "  At this stage only the installer firmware does that, so this step did finish."
        )
    return None, (
        "No installer network is visible from this computer. That does not mean\n"
        "  it failed - the plug may still be starting up, or this computer's Wi-Fi\n"
        "  may be busy or switched off."
    )
