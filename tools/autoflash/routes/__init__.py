"""The route a run takes: from what the plug runs to the firmware asked for,
as a list of steps.  Settled once at the run's start, then kept in STEPS
for the run.

The steps are a chip family's (FAMILIES, by the kind of plug's family): a
package under here with the same handful of names - FIRMWARES, SOURCES and
steps_for(firmware, source) for its routes, and what the runner needs to know
of each step: POINT_OF_NO_RETURN, DESTRUCTIVE_STEPS, RETRY_STEPS,
ERASED_CHECK_STEPS, STAGE_NAMES, TARGET_STAGE_NAMES, PLAIN_DESCRIPTION,
ATTEMPTED_EVIDENCE and STEP_VERIFIERS.  One so far: espressif/.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any, Callable, TYPE_CHECKING

from tools.autoflash import detect, kinds, output, plugs, prompts, runs
from tools.autoflash.errors import Abort
from tools.autoflash.routes import espressif
from tools.autoflash.routes.espressif import source
from tools.autoflash.state import RunState

if TYPE_CHECKING:
    from tools.autoflash.context import Context

Steps = list[tuple[str, str, Callable[["Context"], dict[str, Any]]]]

# Each chip family's routes, by the name a kinds.Kind gives its family.
FAMILIES: dict[str, ModuleType] = {"espressif": espressif}
# What a run may install, every family's together: for the command line, and
# for naming one.  Which a plug can have is its family's FIRMWARES.
FIRMWARES = {name: label for family in FAMILIES.values() for name, label in family.FIRMWARES.items()}


def family_of(state: RunState) -> ModuleType:
    """The family whose steps a run takes: its kind's (kinds.of_run)."""
    return FAMILIES[kinds.of_run(state).family]


def steps_for(firmware: str, source: str = "sonoff", family: ModuleType = espressif) -> Steps:
    """The steps from `source` firmware to `firmware` on a plug of `family`."""
    return family.steps_for(firmware, source)


def steps_of(state: RunState) -> Steps:
    return steps_for(runs.run_firmware(state), runs.run_source(state), family_of(state))


# This run's route: Tasmota from Sonoff on an Espressif plug until it is known
# (apply_target).  FAMILY is the module whose tables the runner reads.
FAMILY = espressif
TASMOTA_STEPS = steps_for("tasmota")
STEPS = TASMOTA_STEPS
STEP_INDEX = {step: (title, handler) for step, title, handler in STEPS}


ROUTE = ("tasmota", "sonoff")


def apply_target(firmware: str, source: str = "sonoff", family: ModuleType = espressif) -> None:
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


def settle_firmware(ctx: Context) -> str:
    """Which firmware this run installs (settle_route)."""
    return settle_route(ctx)[0]


def nothing_to_convert(ctx: Context, source: str) -> None:
    """Asked for what it runs: nothing to do, and nothing left unfinished."""
    ctx.state.set_input("source", source)
    ctx.state.set_input("firmware", source)
    raise NothingToDo(f"The plug already runs {detect.NAMES[source]}: there is nothing to convert.")


def settle_route(ctx: Context) -> tuple[str, str]:
    """What the plug runs now and which firmware it is to have: kept from the
    run's start, given on the command line, or found out and asked - once,
    before anything is done.  A plug this tool converted is found on the
    network by what it answers as; otherwise it is on Sonoff's firmware."""
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
    started = [step for step, _, _ in espressif.COMMON_STEPS if ctx.state.is_done(step) and step != "A0"]
    if started:
        running = "sonoff"  # begun before a plug could be found on another firmware
    else:
        running = source.find_current_firmware(ctx)
    # Tasmota stays a choice on Tasmota: its meter checked (rerun_on_tasmota).
    offered = family_of(ctx.state).FIRMWARES
    choices = [name for name in offered if name != running or running == "tasmota"]
    if running != "sonoff" and not sonoff_backup_exists(ctx):
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
    elif started:
        firmware = "tasmota"  # begun before there was a choice: it was Tasmota then
    else:
        firmware = ask_firmware(running, choices)
        if firmware == running:
            nothing_to_convert(ctx, running)
    ctx.state.set_input("source", running)
    ctx.state.set_input("firmware", firmware)
    output.say(f"Converting the plug from {detect.NAMES[running]} to {FIRMWARES[firmware]}."
         if running in ("tasmota", "esphome", "untemplated") else f"Converting the plug to {FIRMWARES[firmware]}.")
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
              "esphome": "ESPHome", "sonoff": "Sonoff's own firmware (back to eWeLink, from its backup)"}
    try:
        choice = prompts.ask_choice("Which firmware would you like", [labels[name] for name in choices])
    except prompts.NoOneToAnswer:
        output.transient_end()
        raise Abort("an unattended run needs to be told which firmware: "
                    + " or ".join(f"--firmware {name}" for name in choices)) from None
    output.transient_end()
    return choices[choice]


def stage_name(step: str) -> str:
    named = FAMILY.TARGET_STAGE_NAMES.get(ROUTE[0], {})
    if step in named:
        return named[step]
    return FAMILY.STAGE_NAMES.get(step, STEP_INDEX[step][0])


def begin_stage(step: str) -> None:
    number = next(index for index, (name, _, _) in enumerate(STEPS, 1) if name == step)
    output.set_stage_width(max(len(stage_name(name)) for name, _, _ in STEPS))
    output.stage_begin(number, len(STEPS), stage_name(step))
