"""The route a run takes: from what the plug runs to the firmware asked for,
as a list of steps.  Settled once at the run's start, then kept in STEPS
for the run.

Every route begins with A0, what has to be in place.  A plug still on
Sonoff's firmware is then found in its eWeLink account (A1), which says
what kind of plug it is, and only then is the firmware chosen (A3): which
firmware a plug can have is its chip family's.  A converted plug says its
kind by what it answers as, so its route is whole from the start.

The steps are a chip family's (FAMILIES, by the kind of plug's family): a
package under here with the same handful of names - FIRMWARES, SOURCES and
steps_for(firmware, source) for its routes, none of them beginning with the
steps shared here, and what the runner needs to know of each step:
POINT_OF_NO_RETURN, DESTRUCTIVE_STEPS, RETRY_STEPS, ERASED_CHECK_STEPS,
STAGE_NAMES, TARGET_STAGE_NAMES, PLAIN_DESCRIPTION, ATTEMPTED_EVIDENCE,
STEP_VERIFIERS, REDO_STEPS (those its verifiers' doubt sends round again
unasked), SONOFF_FROM_BACKUP (whether going back to Sonoff's firmware needs
the plug's own backup), and RESUME_CHECK, what a resumed run checks first (or None).
"""

from __future__ import annotations

from types import ModuleType
from typing import Any, Callable, TYPE_CHECKING

from tools.autoflash import account, detect, kinds, output, plugs, prompts, runs
from tools.autoflash.errors import Abort
from tools.autoflash.routes import beken, espressif
from tools.autoflash.routes.espressif import source
from tools.autoflash.state import RunState

if TYPE_CHECKING:
    from tools.autoflash.context import Context

Steps = list[tuple[str, str, Callable[["Context"], dict[str, Any]]]]

# Each chip family's routes, by the name a kinds.Kind gives its family.
FAMILIES: dict[str, ModuleType] = {"espressif": espressif, "beken": beken}
# What a run may install, every family's together: for the command line, and
# for naming one.  Which a plug can have is its family's FIRMWARES.
FIRMWARES = {name: label for family in FAMILIES.values() for name, label in family.FIRMWARES.items()}
# The steps of a run from Sonoff's firmware a run of 1.x recorded: one with no
# route recorded that has these done began from Sonoff's, for Tasmota.
LEGACY_STOCK_STEPS = ("A5", "A1", "A2", "C2", "C3", "D1")


def family_of(state: RunState) -> ModuleType:
    """The family whose steps a run takes: its kind's (kinds.of_run)."""
    return FAMILIES[kinds.of_run(state).family]


def step_prerequisites(ctx: Context) -> dict[str, Any]:
    """A0 - what has to be in place first, for anyone who skipped the README."""
    if any(ctx.state.is_done(step) for step in ctx.state.data["steps"] if step != "A0"):
        return {"shown": False}  # a run from before this step existed
    output.transient_begin()
    if runs.run_source(ctx.state) == "sonoff":
        needed = [
            "- The eWeLink app on your phone, signed in to an eWeLink account",
            "  (free - a throwaway account is fine).",
            "- The plug set up in the app on the 2.4 GHz Wi-Fi it should stay on:",
            "  it keeps that network after the conversion, and cannot use 5 GHz.",
            "- LAN Control turned on in the plug's settings in the app.",
        ]
    else:
        needed = [
            "- The plug on the 2.4 GHz Wi-Fi it should stay on, as it is now: the",
            "  firmware it gets keeps that network.",
            "- This computer on the same network.",
        ]
    prompts.section("Before you start", needed + [
        "- The plug in a socket nobody will switch off until the run finishes.",
        "",
        "Nothing on the plug changes until you confirm, once, just before the",
        "point of no return - you will be asked then.",
    ])
    prompts.wait_for_key()
    output.transient_end()  # read: the list leaves the screen, the stage line stays
    return {"shown": True}


def step_choose(ctx: Context) -> dict[str, Any]:
    """A3 - which firmware the plug is to have, now that A1 has said what kind
    of plug it is: the one given on the command line, kept from an earlier
    run, or asked for from its family's; and the route it takes from here."""
    kind = kinds.of_run(ctx.state)
    family = FAMILIES[kind.family]
    kept = ctx.state.input("firmware")
    if kept:
        apply_target(kept, "sonoff", family)
        return {"firmware": kept, "asked": False}
    given = ctx.firmware_given
    choices = [name for name in family.FIRMWARES if name != "sonoff"]
    if given == "sonoff":
        raise Abort("the plug already runs Sonoff's firmware: there is nothing to convert")
    if given and given not in choices:
        raise Abort(f"this plug cannot go to {FIRMWARES[given]}: a {kind.name} takes "
                    f"{' or '.join(family.FIRMWARES[name] for name in choices)}")
    firmware = given or ask_firmware("sonoff", choices)
    ctx.state.set_input("firmware", firmware)
    output.say(f"Converting the plug to {FIRMWARES[firmware]}.")
    apply_target(firmware, "sonoff", family)
    return {"firmware": firmware, "asked": not given}


# What every run from Sonoff's firmware begins with, whatever the plug: the
# family's own steps follow once the firmware is chosen.
STOCK_START: Steps = [
    ("A0", "Before you start", step_prerequisites),
    ("A1", "Pair the plug in eWeLink", account.step_pair),
    ("A3", "Choose the firmware", step_choose),
]
START: Steps = STOCK_START[:1]
STAGE_NAMES = {"A0": "Confirming prerequisites", "A1": "Discovering the plug", "A3": "Choosing the firmware"}
PLAIN_DESCRIPTION = {
    "A0": "lists what needs to be in place before starting",
    "A1": "gets the plug onto your network through the eWeLink app",
    "A3": "asks which firmware the plug is to have",
}


def steps_for(firmware: str | None, source: str = "sonoff", family: ModuleType = espressif) -> Steps:
    """The steps from `source` firmware to `firmware` on a plug of `family`:
    from Sonoff's firmware, the shared start, then the family's once the
    firmware is chosen (None: not yet)."""
    if source == "sonoff":
        return STOCK_START + (family.steps_for(firmware, source) if firmware else [])
    if firmware is None or firmware == source:
        return []
    return START + family.steps_for(firmware, source)


def recorded_firmware(state: RunState) -> str | None:
    """The firmware a run's record says it installs, None if not chosen yet:
    a run of 1.x, with steps done but no choice recorded, was for Tasmota -
    an Espressif plug's; a plug of another family has no such runs."""
    firmware = state.input("firmware")
    if (not firmware and "tasmota" in family_of(state).FIRMWARES
            and (runs.run_source(state) != "sonoff" or any(map(state.is_done, LEGACY_STOCK_STEPS)))):
        firmware = "tasmota"
    return firmware


def steps_of(state: RunState) -> Steps:
    """A run's route, as its record has it: its firmware once chosen, else
    the shared start alone."""
    return steps_for(recorded_firmware(state), runs.run_source(state), family_of(state))


# This run's route: Tasmota from Sonoff on an Espressif plug until it is known
# (apply_target).  FAMILY is the module whose tables the runner reads.
FAMILY = espressif
STEPS = steps_for("tasmota")
STEP_INDEX = {step: (title, handler) for step, title, handler in STEPS}
ROUTE: tuple[str | None, str] = ("tasmota", "sonoff")


def apply_target(firmware: str | None, source: str = "sonoff", family: ModuleType = espressif) -> None:
    global STEPS, STEP_INDEX, ROUTE, FAMILY
    ROUTE = (firmware, source)
    FAMILY = family
    STEPS = steps_for(firmware, source, family)
    STEP_INDEX = {step: (title, handler) for step, title, handler in STEPS}


def past_the_point_of_no_return(state: RunState) -> bool:
    """Whether a run has written to its plug: past its family's POINT_OF_NO_RETURN."""
    return state.is_done(family_of(state).POINT_OF_NO_RETURN)


def sonoff_backup_exists(ctx: Context) -> bool:
    """Whether this computer holds the plug's Sonoff backup: what going back
    to Sonoff's firmware needs."""
    mac = ctx.state.input("plug_mac")
    return bool(mac and plugs.find_backup(mac))


class NothingToDo(Exception):
    """The plug already runs the firmware asked for."""


def settle_firmware(ctx: Context) -> str | None:
    """Which firmware this run installs (settle_route); None until A3 chooses."""
    return settle_route(ctx)[0]


def nothing_to_convert(ctx: Context, source: str) -> None:
    """Asked for what it runs: nothing to do, and nothing left unfinished."""
    ctx.state.set_input("source", source)
    ctx.state.set_input("firmware", source)
    raise NothingToDo(f"The plug already runs {detect.NAMES[source]}: there is nothing to convert.")


def settle_route(ctx: Context) -> tuple[str | None, str]:
    """What the plug runs now and which firmware it is to have: kept from the
    run's start, given on the command line, or found out and asked - once,
    before anything is done.  A plug this tool converted is found on the
    network by what it answers as, and says its kind; one still on Sonoff's
    firmware is found through eWeLink at A1, and its firmware chosen at A3,
    so for it the firmware is None here."""
    kept, running = ctx.state.input("firmware"), ctx.state.input("source")
    given = ctx.firmware_given
    if kept and given and given != kept:
        raise Abort(
            f"this plug is being converted to {FIRMWARES[kept]}, and a conversion cannot "
            f"switch to {FIRMWARES[given]} part way. Carry on without "
            f"{'--esphome' if ctx.esphome_firmware else '--firmware'}, or start again with --new"
        )
    if kept:
        return kept, running or "sonoff"
    if running == "sonoff":
        return None, "sonoff"  # found in eWeLink already; the firmware is chosen at A3
    started = [step for step in LEGACY_STOCK_STEPS if ctx.state.is_done(step)]
    if started:
        # Begun by 1.x, before there was a choice: from Sonoff's, for Tasmota.
        ctx.state.set_input("source", "sonoff")
        ctx.state.set_input("firmware", "tasmota")
        return "tasmota", "sonoff"
    running = source.find_current_firmware(ctx)
    if running == "sonoff":
        if given == "sonoff":
            nothing_to_convert(ctx, "sonoff")
        ctx.state.set_input("source", "sonoff")
        return None, "sonoff"
    # Tasmota stays a choice on Tasmota: its meter checked (rerun_on_tasmota).
    offered = family_of(ctx.state).FIRMWARES
    choices = [name for name in offered if name != running or running == "tasmota"]
    if family_of(ctx.state).SONOFF_FROM_BACKUP and not sonoff_backup_exists(ctx):
        choices.remove("sonoff")  # going back needs this plug's backup
    if given:
        if given == running:
            nothing_to_convert(ctx, running)
        if given not in offered:
            raise Abort(f"this plug cannot go to {FIRMWARES[given]}: a {kinds.of_run(ctx.state).name} "
                        f"takes {' or '.join(offered.values())}")
        if given not in choices:
            raise Abort(f"this plug cannot go to {FIRMWARES[given]}: there is no backup of its "
                        "Sonoff firmware on this computer")
        firmware = given
    else:
        firmware = ask_firmware(running, choices)
        if firmware == running:
            nothing_to_convert(ctx, running)
    ctx.state.set_input("source", running)
    ctx.state.set_input("firmware", firmware)
    output.say(f"Converting the plug from {detect.NAMES[running]} to {FIRMWARES[firmware]}.")
    return firmware, running


def ask_firmware(source: str, choices: list[str]) -> str:
    """The firmware to install: the choices by name alone, with a word only on
    what is particular to this plug - part way, on Tasmota's safeboot, or with
    no backup to go back to Sonoff's firmware from."""
    output.transient_begin()
    body = (["This plug was left part way through a conversion; it can go on to any",
             "of these from where it is."] if source in ("midway", "committed", "installer") else
            ["Its Tasmota goes back on first, with its settings; choosing Tasmota",
             "stops there. Or it can go on to either of the others."] if source == "safeboot" else
            ["It runs Tasmota without the plug's template, so it switches nothing;",
             "choosing Tasmota gives it the template, keeping its other settings."]
            if source == "untemplated" else [])
    if source != "sonoff" and "sonoff" not in choices:
        body += [""] * bool(body) + ["Going back to Sonoff's own firmware needs the backup this tool takes",
                                     "when it first converts a plug, and there is none for this one here."]
    prompts.section("Choose the plug's new firmware", body)
    labels = {"tasmota": "Tasmota" if source != "tasmota" else "Tasmota, as it is (its meter calibrated, if it is not)",
              "openbeken": "OpenBeken", "esphome": "ESPHome",
              "sonoff": "Sonoff's own firmware (back to eWeLink, from its backup)"}
    try:
        choice = prompts.ask_choice("Which firmware would you like", [labels[name] for name in choices])
    except prompts.NoOneToAnswer:
        output.transient_end()
        raise Abort("an unattended run needs to be told which firmware: "
                    + " or ".join(f"--firmware {name}" for name in choices)) from None
    output.transient_end()
    return choices[choice]


def stage_name(step: str) -> str:
    return _stage_name(step, STEP_INDEX[step][0], ROUTE[0], FAMILY)


def _stage_name(step: str, title: str, firmware: str | None, family: ModuleType) -> str:
    named = family.TARGET_STAGE_NAMES.get(firmware, {})
    if step in named:
        return named[step]
    return STAGE_NAMES.get(step) or family.STAGE_NAMES.get(step, title)


def stage_layout() -> tuple[int, int]:
    """The longest stage name the run may show, and the most stages it may
    have.  From Sonoff's firmware the rest of the route is known only once A1
    has found the kind of plug and A3 the firmware, so every stage line is
    laid out for the longest route from there there could be."""
    if ROUTE[1] != "sonoff":
        return max(len(stage_name(name)) for name, _, _ in STEPS), len(STEPS)
    possible = [(firmware, family, steps_for(firmware, "sonoff", family)) for family in FAMILIES.values()
                for firmware in family.FIRMWARES if firmware != "sonoff"]
    return (max(len(_stage_name(name, title, firmware, family))
                for firmware, family, steps in possible for name, title, _ in steps),
            max(len(steps) for _, _, steps in possible))


def plain_description(step: str, title: str) -> str:
    return PLAIN_DESCRIPTION.get(step) or FAMILY.PLAIN_DESCRIPTION.get(step, title.lower())


def begin_stage(step: str) -> None:
    number = next(index for index, (name, _, _) in enumerate(STEPS, 1) if name == step)
    output.set_stage_width(*stage_layout())
    output.stage_begin(number, stage_name(step))
