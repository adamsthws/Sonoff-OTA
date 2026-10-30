"""Running the route: each step in turn, with its retries, its evidence when
it was cut off, and what is said when the run stops.
"""

from __future__ import annotations

import time
import traceback
from typing import TYPE_CHECKING

from tools.autoflash import (
    device,
    errors,
    esphome,
    firewall,
    hostsys,
    interrupt,
    join,
    kinds,
    names,
    output,
    prompts,
    report,
    routes,
    runs,
    safety_locks,
    unlock,
)
from tools.autoflash.device import DeviceError
from tools.autoflash.errors import Abort, NothingWritten
from tools.autoflash.phases import PhaseError, PlugNeverConnected
from tools.autoflash.prompts import QuitRun
from tools.autoflash.routes import espressif
from tools.autoflash.routes.espressif import bundle, evidence, tasmota
from tools.autoflash.runs import ROOT
from tools.autoflash.state import ATTEMPTED, DONE, FAILED

if TYPE_CHECKING:
    from tools.autoflash.context import Context

# Where people ask for help: a discussion or issue of their own, so the
# reports thread stays a record of runs.
DISCUSSIONS_URL = "https://github.com/adamsthws/Sonoff-OTA/discussions"
ISSUES_URL = "https://github.com/adamsthws/Sonoff-OTA/issues/new"


def execute(ctx: Context, step: str) -> int:
    ctx.last_runner = None
    started, waited = time.monotonic(), output.waited()
    try:
        return _execute(ctx, step)
    finally:
        # For the report, however the stage ended: its time without the
        # keyboard waits, and how long a Berry phase kept the plug busy.
        ctx.state.capture(f"{step}.seconds",
                          round(time.monotonic() - started - (output.waited() - waited)))
        busy = getattr(ctx.last_runner, "busy_seconds", None)
        if busy is not None:
            # The longest of its tries: how near the plug came to stopping.
            before = ctx.state.data.get("captures", {}).get(f"{step}.plug_busy_seconds")
            ctx.state.capture(f"{step}.plug_busy_seconds",
                              max(busy, before) if isinstance(before, (int, float)) else busy)


def _execute(ctx: Context, step: str) -> int:
    title, handler = routes.STEP_INDEX[step]
    routes.begin_stage(step)
    output.say(f"{step}  {title}")
    try:
        # Its errors are the step's: seen in the fault suite, an Abort from
        # it crashed the run.
        set_up_if_erased(ctx, step)
        if step in evidence.DESTRUCTIVE_STEPS:
            ctx.state.mark(step, ATTEMPTED, nothing_written=False)
        captured = handler(ctx)
    except QuitRun:
        output.stage_end("stopped")
        return 130  # main() says so, once it has cleaned up
    except PlugNeverConnected as exc:
        ctx.state.mark(step, FAILED, error=str(exc))
        output.stage_end("stopped")
        prompts.section(f"{step} could not start", firewall.firewall_hint(ctx))
        ctx.last_failure = str(exc)
        ctx.last_failed_step = step
        output.tell(f"FAILED: {exc}")
        return 1
    except (Abort, DeviceError, ValueError, PhaseError) as exc:
        # A phase that ended with no result may have got as far as writing:
        # where the plug can show whether the step finished, the step stays
        # "attempted" and the next run asks it.
        if not (isinstance(exc, PhaseError) and step in evidence.ATTEMPTED_EVIDENCE):
            ctx.state.mark(step, FAILED, error=str(exc),
                           **({"nothing_written": True} if isinstance(exc, NothingWritten) else {}))
        output.stage_end("stopped")
        ctx.last_failure = str(exc)
        ctx.last_failed_step = step
        output.tell(f"FAILED: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - recorded and explained, not a bare traceback
        # The plug or the network dropping a request part way - a reset, a
        # reply cut off, no answer - is no surprise, and worth another go.
        message = connection_lost(exc) if isinstance(exc, errors.NETWORK_ERRORS) else unexpected(exc)
        # As above: whether it got as far as writing is for the plug to say.
        if step not in evidence.ATTEMPTED_EVIDENCE:
            ctx.state.mark(step, FAILED, error=message)
        output.stage_end("stopped")
        ctx.last_failure = message
        ctx.last_failed_step = step
        output.tell(f"FAILED: {message}")
        return 1
    # A step may end its stage otherwise: "skipped" or "already done".
    outcome = captured.pop("stage_outcome", "complete") if captured else "complete"
    ctx.state.mark(step, DONE, **{"result": captured})
    for key, value in (captured or {}).items():
        ctx.state.capture(f"{step}.{key}", value)
    output.stage_end(outcome)
    output.say(f"{step} done")
    return 0


ATTEMPTS = 3
RETRY_PAUSE = 10.0
# A router restart takes a minute or two.  The plug gives up on a step within
# about a minute - busier for longer, it stops answering for good (its energy
# meter's readings pile up) - so this computer does the waiting, between tries.
PLUG_BACK_WAIT = 300.0


def execute_with_retries(ctx: Context, step: str) -> int:
    for attempt in range(1, ATTEMPTS + 1):
        code = execute(ctx, step)
        if step != "E3":  # it counts its own tries (COMMIT_ATTEMPTS)
            ctx.state.capture(f"{step}.attempts", attempt)
        if code != 1 or step not in espressif.RETRY_STEPS or attempt == ATTEMPTS:
            return code
        # A step the plug is asked about later (ATTEMPTED_EVIDENCE) keeps no
        # error in its record, so what it failed with is taken from this run.
        error = str(ctx.state.data.get("steps", {}).get(step, {}).get("error", "")
                    or ctx.last_failure)
        if not errors.TRANSIENT_RE.search(error):
            return code
        output.tell(f"    That may have been the network - trying again in {RETRY_PAUSE:.0f}s "
             f"(attempt {attempt + 1} of {ATTEMPTS}).")
        time.sleep(RETRY_PAUSE)
        wait_for_plug_back(ctx, step)
    return code


PLUG_BACK_HINT_AFTER = 45.0
ERASED_LOOK_EVERY = 30.0
# Well over the time a Tasmota restarting onto the Wi-Fi takes to answer there.
ERASED_SETTLE = 60.0


def answers_within(device: device.Device, seconds: float) -> bool:
    """Whether the plug answers within `seconds`, asked every few."""
    started = time.monotonic()
    while (elapsed := time.monotonic() - started) < seconds:
        output.progress(f"waiting for the plug, in case it is starting up... {elapsed:.0f}s")
        if device.answers(timeout=3.0):
            output.progress_clear()
            return True
        time.sleep(2.0)
    output.progress_clear()
    return False


def set_up_if_erased(ctx: Context, step: str) -> bool:
    """A Tasmota not on the Wi-Fi but on its own setup network has lost its
    settings - switched off and on 7 times, say, or an earlier run's advice
    taken late - so it is set up through that network again; and, past F4, as
    its template went with them, F4 sets it up again too.  True when it was:
    the step then goes on with the plug back.  Seen in the fault suite: G1
    asked for Wi-Fi details for a plug it could not reach, run after run."""
    # Every route reaches these steps on such a Tasmota: from stock through D1,
    # from ESPHome or back to Sonoff through the mover and the startup firmware.
    if step not in espressif.ERASED_CHECK_STEPS or not ctx.state.input("plug_mac"):
        return False
    if ctx.state.input("plug_ip") and ctx.device.answers(timeout=3.0):
        return False
    network = prompts.find_ssid(names.setup_networks(ctx), timeout=10)
    if not network:
        return False
    # A Tasmota starting up shows a network for a moment - ESP_ and its
    # access-point MAC - before it is on the Wi-Fi.  Seen in the fault suite:
    # one D1 had just restarted onto the Wi-Fi was taken for one that lost its
    # settings, and the run waited 15 minutes for a network gone at once.  So
    # it is given the time to start; not answering then, with its network
    # still there, it has lost them.
    if ctx.state.input("plug_ip") and answers_within(ctx.device, ERASED_SETTLE):
        return False
    network = prompts.find_ssid(names.setup_networks(ctx), timeout=10)
    if not network:
        return False
    output.progress_clear()
    output.tell("    The plug is not on your Wi-Fi but on its own setup network: its settings were")
    output.tell("    erased (switched off and on 7 times, say), so it is set up through that again.")
    join.configure_plug_wifi(ctx, network)
    if ctx.state.is_done("F4"):
        device = ctx.device
        device.wait_reachable(timeout=120)
        template = kinds.of_run(ctx.state).tasmota_template(ctx.state.input("product_model"))
        if not tasmota.template_in_place(device, template, names.final_name(ctx, "tasmota")):
            output.tell("    Its template went with its settings: Tasmota is set up again (F4).")
            ctx.state.capture("F4.again_after_erase", True)
            tasmota.step_configure(ctx)
    return True


def wait_for_plug_back(ctx: Context, step: str | None = None) -> None:
    """Before a step is tried again, wait for a plug running Tasmota to answer:
    after a router restart it is back within a minute or two."""
    if not ctx.state.input("plug_ip") or not ctx.state.is_done("D1"):
        return
    device = ctx.device
    host = ctx.state.input("plug_ip")
    esphome_route = runs.run_firmware(ctx.state) == "esphome"

    def back() -> bool:
        # On the way to ESPHome the plug may already have switched to it.
        return device.answers(timeout=3.0) or (esphome_route and esphome.is_esphome(host, 3.0))

    if back():
        return
    output.tell("    The plug is not answering yet - waiting for it (a router restarting, say).")
    started = time.monotonic()
    looked = started
    hinted = False
    while (now := time.monotonic()) - started < PLUG_BACK_WAIT:
        # Part way through, the plug runs a Tasmota holding your Wi-Fi details,
        # which keeps trying them and never opens a network of its own - all but
        # F4's official Tasmota, which may not have them yet (F4 looks for its
        # setup network itself), and one that has lost its settings, looked for.
        if not hinted and step != "F4" and now - started > PLUG_BACK_HINT_AFTER:
            output.progress_clear()
            output.tell("    At this stage the plug has no setup network of its own unless its settings")
            output.tell("    were erased: it rejoins your Wi-Fi by itself as soon as it can. Check your")
            output.tell("    router is on and the plug is plugged in; this keeps waiting.")
            hinted = True
        if step and now - looked >= ERASED_LOOK_EVERY:
            looked = now
            if set_up_if_erased(ctx, step):
                return
        output.progress(f"waiting for the plug... {now - started:.0f}s")
        if back():
            output.progress_clear()
            output.tell("    It is answering again.")
            return
        time.sleep(3.0)
    output.progress_clear()
    output.tell(f"    It has not answered for {PLUG_BACK_WAIT / 60:.0f} minutes - trying anyway.")


def confirm_skip(ctx: Context, step: str, title: str) -> bool:
    """Trust the checklist only as far as the plug agrees with it."""
    verifier = tasmota.STEP_VERIFIERS.get(step)
    if verifier is None:
        output.say(f"[skip] {step} {title} (recorded as done)")
        return True
    try:
        ok, message = verifier(ctx)
    except (DeviceError, Abort, OSError, ValueError) as exc:
        output.say(f"[skip] {step} {title} (recorded as done; could not re-check: {exc})")
        return True
    if ok:
        output.say(f"[skip] {step} {title} (done, and the plug still agrees)")
        return True
    output.transient_begin()  # the note and its question, gone once answered
    output.say("")
    output.tell(f"{step} is recorded as done, but {message}.")
    output.tell("The record and the device disagree, so the checklist cannot be trusted here.")
    again = prompts.ask_yes_no(f"Run {step} again", default=False)
    output.transient_end()
    return not again


def tell_how_to_carry_on() -> None:
    output.tell("")
    output.tell("Fix the cause, then carry on with the same command again:")
    output.tell("    start.cmd (double-click it, or .\\start.cmd in a terminal)" if hostsys.IS_WINDOWS else "    sh start.sh")
    if output.log_path():
        output.tell(f"What happened, in full: {runs.display_path(output.log_path())}")
    output.tell("Stuck? Start a discussion or open an issue:")
    output.show_links([(DISCUSSIONS_URL, "Start a discussion"), (ISSUES_URL, "Open an issue")])


def connection_lost(exc: BaseException) -> str:
    """A request the network or the plug dropped: in full in the log, in a line on screen."""
    output.log_only(traceback.format_exc().rstrip())
    return f"the connection to the plug was lost ({type(exc).__name__}: {exc})"


def unexpected(exc: BaseException) -> str:
    """An error nothing here anticipated: in full in the log, in a line on screen."""
    output.log_only(traceback.format_exc().rstrip())
    return f"something unexpected went wrong: {type(exc).__name__}: {exc}"


def stop_here_after_ctrl_c(step: str) -> bool:
    """At the end of a stage, a Ctrl-C pressed during it is asked about: the
    run stops here only if the person says so - by itself it carries on."""
    if not interrupt.pending():
        return False
    interrupt.clear()
    output.transient_begin()
    prompts.section("You pressed Ctrl-C", [
        "The stage that was under way is done, and everything is recorded, so",
        "stopping here is safe: running this again picks up where it left off.",
    ])
    choice = prompts.countdown_choice("Stop the run here", [
        "Carry on - the run goes on to the next stage",
        "Stop here",
    ])
    output.transient_end()
    if choice == 1:
        output.say(f"Stopped after {step}, as asked after Ctrl-C.")
        return True
    output.say(f"Carrying on after {step}: Ctrl-C was not confirmed.")
    return False


def rerun_on_tasmota(ctx: Context) -> int:
    """A plug kept on Tasmota: its meter calibrated, as F4 would have, if it
    is not - a plug converted before the tool did, or whose calibration was
    lost with its settings.  One calibrated by hand is left as it is."""
    try:
        unlock.unlock_tasmota(ctx)
    except Abort as refused:
        output.tell(f"Stopped before anything was done: {refused}.")
        return 2
    output.tell("Checking its meter's calibration...")
    tasmota.calibrate_tasmota_meter(ctx, show=True)
    return 0


def run_steps(ctx: Context) -> int:
    started = time.monotonic()
    safety_locks.restore_left_locks(ROOT)  # a resumed run skips A5, which looks too
    output.tell("")
    output.tell("Converting a plug usually takes under 10 minutes. Some stages take a")
    output.tell("minute or two with little to see - that's normal. Keep the plug plugged")
    output.tell("in, and this computer on, until the last stage says complete.")
    output.tell("")
    try:
        if ctx.state.input("plug_mac"):
            runs.hold_plug(ctx, ctx.state.input("plug_mac"))  # a run carried on: its plug is known
        firmware, source = routes.settle_route(ctx)
        unlock.unlock_tasmota(ctx)
    except routes.NothingToDo as nothing:
        output.tell(str(nothing))
        return rerun_on_tasmota(ctx) if runs.run_source(ctx.state) == "tasmota" else 0
    except Abort as refused:
        output.tell(f"Stopped before anything was done: {refused}.")
        return 2
    routes.apply_target(firmware, source)
    interrupt.clear()
    ctx.state.set_input("stopped_by", None)
    resumed = any(ctx.state.is_done(step) for step, _, _ in routes.STEPS)
    ctx.run_started, ctx.run_resumed = started, resumed
    if resumed:
        try:
            bundle.bundle_of_this_version(ctx)
        except Abort as refused:
            output.tell(f"Stopped before anything was done: {refused}.")
            return 2
    join.wait_while_plug_busy(ctx)
    for step, title, _ in routes.STEPS:
        if ctx.state.is_done(step):
            routes.begin_stage(step)
            if confirm_skip(ctx, step, title):
                output.stage_end("already done")
                continue
        if ctx.state.status(step) == ATTEMPTED:
            # Under its own stage line, like every other stage; the check, and
            # any question, are taken off the screen once settled.
            routes.begin_stage(step)
            output.transient_begin()
            output.say("")
            output.tell(f"{step} was interrupted last time - checking whether it finished...")
            finished, reason = None, ""
            check = evidence.ATTEMPTED_EVIDENCE.get(step)
            if check is not None:
                try:
                    finished, reason = check(ctx)
                except (DeviceError, Abort, OSError, ValueError) as exc:
                    finished, reason = None, f"The check itself could not run: {exc}"
                output.tell(f"    {reason}")
            if finished is True:
                # The plug itself shows the step completed: nothing to decide.
                output.transient_end()
                ctx.state.mark(step, DONE, note="the plug showed it had completed")
                output.stage_end("already done")
                continue
            prompts.section("The last step was interrupted", [
                f"The step that {espressif.PLAIN_DESCRIPTION.get(step, title.lower())} was",
                "stopped part way through, and the plug does not show whether it",
                "finished. Doing it again is normally safe: every step checks the",
                "plug before writing anything and refuses if it is not as expected.",
            ])
            options = [
                "Do that step again" + ("   <- recommended" if finished is False else ""),
                "It did finish - move on to the next step",
            ]
            if prompts.UNATTENDED:
                choice = 0  # doing it again is the safe answer, as above
                prompts.answered_unattended("What would you like to do", options[0])
            else:
                choice = prompts.ask_choice("What would you like to do", options)
            output.transient_end()
            if choice == 1:
                ctx.state.mark(step, DONE, note="operator confirmed it completed")
                output.say(f"{step} marked done on your say-so.")
                output.stage_end("already done")
                continue
        code = execute_with_retries(ctx, step)
        if code == 0 and stop_here_after_ctrl_c(step):
            firewall.close_firewall_rules(ctx)
            return 130
        if code == 0 and step == espressif.POINT_OF_NO_RETURN and ctx.research_bridge:
            prompts.section("Research bridge installed - stopping here", [
                f"{ctx.bridge.name} is on the plug. Join the bridge's network",
                f"({names.our_setup_network(ctx)}, open) and run its research tool, e.g.:",
                "  python tools/espressif/inspect_stock_nvs.py",
                "Then switch the plug off and on: it goes back to its Sonoff",
                "firmware. Or run this tool again to carry on the conversion.",
            ])
            firewall.close_firewall_rules(ctx)
            return 0
        if code == 130:
            return code  # the operator chose to quit; main() cleans up and says so
        if code != 0:
            firewall.close_firewall_rules(ctx)
            tell_how_to_carry_on()
            return code
    output.say("")
    firewall.close_firewall_rules(ctx)
    output.tell("")
    output.show("Conversion complete!", output.GREEN)
    if firmware == "sonoff":
        output.tell("The plug is running its own Sonoff firmware again, with the settings it")
        output.tell("had: it is back in the eWeLink app.")
    else:
        output.tell("The plug is running ESPHome..." if firmware == "esphome"
             else "The plug is running official Tasmota...")
        url = report.plug_page_url(ctx.state.input("plug_ip"))
        if url:
            output.show_link(url, "Your plug")
    report.invite_report(ctx, time.monotonic() - started - output.waited(), resumed)
    return 0
