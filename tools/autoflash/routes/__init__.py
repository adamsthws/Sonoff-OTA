"""The route a run takes: from what the plug runs to the firmware asked for,
as a list of steps.  Settled once at the run's start, then kept in STEPS
for the run; each chip family has its own tables (espressif/).
"""

from __future__ import annotations

from typing import Any, Callable, TYPE_CHECKING

from tools.autoflash import detect, output, prompts, runs
from tools.autoflash.errors import Abort
from tools.autoflash.routes import espressif
from tools.autoflash.routes.espressif import source
from tools.autoflash.state import RunState

if TYPE_CHECKING:
    from tools.autoflash.context import Context

# What a run installs, and what the plug runs to begin with, as settled at its start.
FIRMWARES = {"tasmota": "Tasmota", "esphome": "ESPHome", "sonoff": "Sonoff"}


def steps_of(state: RunState) -> list[tuple[str, str, Callable[[Context], dict[str, Any]]]]:
    return espressif.steps_for(runs.run_firmware(state), runs.run_source(state))


# This run's steps: Tasmota's from Sonoff until the route is known (apply_target).
TASMOTA_STEPS = espressif.steps_for("tasmota")
STEPS = TASMOTA_STEPS
STEP_INDEX = {step: (title, handler) for step, title, handler in STEPS}


ROUTE = ("tasmota", "sonoff")


def apply_target(firmware: str, source: str = "sonoff") -> None:
    global STEPS, STEP_INDEX, ROUTE
    ROUTE = (firmware, source)
    STEPS = espressif.steps_for(firmware, source)
    STEP_INDEX = {step: (title, handler) for step, title, handler in STEPS}


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
    choices = [name for name in FIRMWARES if name != running or running == "tasmota"]
    if running != "sonoff" and not source.sonoff_backup_exists(ctx):
        choices.remove("sonoff")  # going back needs this plug's backup
    if given:
        if given == running:
            nothing_to_convert(ctx, running)
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
    named = espressif.TARGET_STAGE_NAMES.get(ROUTE[0], {})
    if step in named:
        return named[step]
    return espressif.STAGE_NAMES.get(step, STEP_INDEX[step][0])


def begin_stage(step: str) -> None:
    number = next(index for index, (name, _, _) in enumerate(STEPS, 1) if name == step)
    output.set_stage_width(max(len(stage_name(name)) for name, _, _ in STEPS))
    output.stage_begin(number, len(STEPS), stage_name(step))
