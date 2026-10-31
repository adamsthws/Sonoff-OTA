"""The ports this computer serves the plug on, and the firewall rules that
let the plug reach them for the run, closed again when it ends.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from tools.autoflash import hostsys, interrupt, output, prompts
from tools.autoflash.errors import Abort
from tools.autoflash.hostsys import FirewallRule

if TYPE_CHECKING:
    from tools.autoflash.context import Context

# Ports the bridge may be served on, in order of preference.  The upgrade
# command names whichever is chosen, and the plug fetches from that port:
# 8088 matches the vendor OTA port, but the research probe was served on
# 8765.  The phase servers have their own ports, so the two never share one.
DELIVERY_PORTS = (8088, *range(8090, 8100))
# Ports the migration phase servers (serve_safeboot_migration.py) may listen
# on, in order of preference.  Nothing on the plug keeps the port: each phase
# starts its own server and sends the plug a loader URL that names it.
PHASE_PORTS = (8089, *range(8100, 8110))


def choose_port(ctx: Context, key: str, candidates: tuple[int, ...], serving: str) -> int:
    """The first of `candidates` free on this computer, recorded as `key`.

    Chosen afresh each time, so a port that was busy last run is tried again.
    Raises Abort - before anything reaches the plug - when none is free.
    """
    port = hostsys.first_free_port(ctx.listen_ip(), candidates)
    if port is None:
        raise Abort(
            f"ports {candidates[0]} and {candidates[1]}-{candidates[-1]} are all in use on this "
            f"computer, so there is nowhere to serve {serving} from. Stop whatever is "
            "using them and run this again - nothing was sent to the device"
        )
    if port != candidates[0]:
        output.say(f"Port {candidates[0]} is in use on this computer; serving {serving} on {port} instead.")
    ctx.state.set_input(key, str(port))
    return port


def firewall_ports(ctx: Context) -> list[int]:
    """The ports the plug connects to this computer on: bridge delivery and phases."""
    return sorted({int(ctx.state.input("delivery_port") or DELIVERY_PORTS[0]),
                   int(ctx.state.input("phase_port") or PHASE_PORTS[0])})


def emulator_password_hint() -> list[str]:
    """Inside the emulator (tools/espressif/emulator) only: the password sudo asks for
    there, so nobody has to look it up.  A real computer's is never known."""
    known = os.environ.get("AUTOFLASH_EMULATOR_SUDO_PASSWORD")
    if not known or hostsys.IS_WINDOWS:
        return []
    return [f"In the emulator, the password is: {known}"]


def recorded_rules(ctx: Context) -> list[FirewallRule]:
    return [FirewallRule.from_json(item) for item in ctx.state.input("firewall_rules") or []]


def ensure_firewall(ctx: Context) -> None:
    """Let the plug's connections through this computer's firewall, once per address.

    The rule admits only the plug's address on the two ports the tool listens
    on, and is recorded so it can be removed when the tool exits, however it
    exits; a resumed run adds it again.
    """
    plug_ip = ctx.state.input("plug_ip")
    ports = firewall_ports(ctx)
    rules = recorded_rules(ctx)
    if not plug_ip or any(r.plug_ip == plug_ip and set(ports) <= set(r.ports) for r in rules):
        return
    # Other security software, or an organisation's policy: logged, and shown if
    # the plug then cannot connect (firewall_hint) - except a policy that
    # ignores rules added here, which is certain to stop it, so is shown now.
    advice = hostsys.firewall_advice(plug_ip, ports)
    for line in advice:
        output.say(line)
    if hostsys.local_rules_ignored(plug_ip):
        prompts.section("This computer's firewall is managed by an organisation", [
            "It is set (by Group Policy) to ignore rules added on the computer itself,",
            "so the device cannot be let in from here. Ask whoever manages it, or use",
            "another computer. Carrying on - if the device is blocked, the next step",
            "stops and says so.",
        ])
        return
    kind = hostsys.active_firewall()
    if kind is None:
        return
    program = hostsys.python_program() if kind in ("windows", "macos") else None
    add, _ = hostsys.firewall_commands(kind, plug_ip, ports, program)
    prompt_note = [
        "Windows will ask whether to allow the change - choose Yes. This window",
        "does not need to be run as administrator; only this rule asks for it.",
    ] if hostsys.IS_WINDOWS else ["You may be asked for this computer's password.",
                                  *emulator_password_hint()]
    what = ("lets Python accept incoming connections - the macOS firewall allows "
            "programs, not ports" if kind == "macos"
            else f"allows only {plug_ip} in, to Python, on TCP ports {', '.join(map(str, ports))}"
            if kind == "windows"
            else f"allows only {plug_ip} in, on TCP ports {', '.join(map(str, ports))}")
    output.transient_begin()  # the note, and the system's password question
    prompts.section("Let the device through this computer's firewall", [
        f"This computer's firewall ({kind}) would block the device from fetching its",
        f"firmware. Adding one rule that {what}.",
        "This temporary rule is always removed when this tool exits (no matter how it exits).",
        *prompt_note,
    ])
    try:
        with interrupt.deferred():  # added and recorded, or neither
            rule = hostsys.open_firewall(plug_ip, ports)
            if rule is not None:
                rules.append(rule)
                ctx.state.set_input("firewall_rules", [r.to_json() for r in rules])
    except OSError:
        output.transient_end()
        prompts.section("The firewall rule could not be added", [
            "Add it yourself, in another window, with:",
            f"  {hostsys.display(add)}",
            "and remove it after the conversion. Carrying on - if the device is still",
            "blocked, the next step stops and says so.",
            *([
                "",
                "Adding it needs administrator rights. On a work computer you may not be",
                "allowed them: then ask whoever manages it, or use another computer.",
            ] if hostsys.IS_WINDOWS else []),
        ])
        return
    output.transient_end()
    if rule is not None:
        output.say("The rule was already there, so it is left in place afterwards."
            if rule.detail.get("preexisting") else
            "The rule was there from an earlier run that stopped as it was added; "
            "it is removed afterwards." if rule.detail.get("left_by_earlier_run") else
            "Firewall rule added.")
        if rule.detail.get("paused"):
            output.say("Python had been blocked from networks here, most likely by a \"Cancel\" in")
            output.say("Windows Security once. That block is paused while this runs, and put back")
            output.say("as it was afterwards.")


def firewall_hint(ctx: Context) -> list[str]:
    """What to say when the plug never reached this computer."""
    plug_ip = ctx.state.input("plug_ip") or "PLUG_IP"
    lines = [
        "The device never connected to this computer. That is almost always this",
        "computer's firewall, or this computer having changed address.",
    ]
    kind = hostsys.active_firewall()
    ports = firewall_ports(ctx)
    if kind:
        program = hostsys.python_program() if kind in ("windows", "macos") else None
        add, _ = hostsys.firewall_commands(kind, plug_ip, ports, program)
        lines += ["", f"A {kind} firewall is on. This allows the device in:", f"  {hostsys.display(add)}"]
    advice = hostsys.firewall_advice(plug_ip if plug_ip != "PLUG_IP" else None, ports, after_failure=True)
    if advice:
        lines += ["", *advice]
    return lines


def close_firewall_rules(ctx: Context) -> None:
    """Remove every rule this run added, and say what is left, if anything."""
    rules = recorded_rules(ctx)
    if not rules:
        return
    permission = (
        "Windows will ask for permission again"
        if hostsys.IS_WINDOWS else "you may be asked for this computer's password again"
    )
    output.transient_begin()  # gone once the rule is
    output.tell("")
    output.tell("Removing the temporary firewall rule added at the start, which let the")
    output.tell("plug connect to this computer. Changing the firewall needs admin/sudo")
    output.tell(f"rights, so {permission}.")
    for line in emulator_password_hint():
        output.tell(line)
    left: list[FirewallRule] = []
    for rule in rules:
        if not hostsys.close_firewall(rule):
            left.append(rule)
    ctx.state.set_input("firewall_rules", [r.to_json() for r in left])
    output.transient_end()
    if left:
        prompts.section("Remove the temporary firewall rules yourself", [
            line for r in left for line in hostsys.undo_commands(r)
        ])
    else:
        output.say("The temporary firewall rules have been removed.")


def release_firewall(ctx: Context) -> bool:
    """On the way out, however the tool is leaving: remove the firewall rule.

    The usual exits have removed it already; this catches Ctrl-C, being
    stopped, and anything unexpected.  Nothing listens on its ports once the
    tool has exited, so it has no reason to stay, and a resumed run adds it
    again.  True when no rule is left behind.
    """
    try:
        if recorded_rules(ctx):
            close_firewall_rules(ctx)
        return not recorded_rules(ctx)
    except (KeyboardInterrupt, OSError) as exc:
        # Ctrl-C at the password prompt, or no terminal left to ask on.
        left = recorded_rules(ctx)
        output.tell("")
        output.tell("The temporary firewall rule was not removed"
             + (" (stopped with Ctrl-C)." if isinstance(exc, KeyboardInterrupt) else f": {exc}."))
        output.tell("Remove it yourself with:")
        for rule in left:
            for line in hostsys.undo_commands(rule):
                output.tell("    " + line)
        return False


def say_quit(ctx: Context) -> None:
    """The last word after the operator chose to quit, once everything is tidied:
    the run's secrets are gone and the firewall rule is removed."""
    if release_firewall(ctx):
        output.tell("")
        output.tell("Cleaned up. Nothing was left half done. Exiting.")
