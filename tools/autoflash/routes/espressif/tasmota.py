"""Official Tasmota on its own layout: the repartition (E1 to E3), the
official app, its set-up and the checks (F2 to H1), and Tasmota put back
from its safeboot (T1).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from tools.autoflash import (
    calibration,
    detect,
    device,
    errors,
    esphome,
    join,
    kinds,
    names,
    output,
    phases,
    prompts,
    runs,
    safety_locks,
    unlock,
    wifi,
)
from tools.autoflash.device import DeviceError, UploadRefused
from tools.autoflash.errors import Abort
from tools.autoflash.phases import PhaseError, failure_reason
from tools.autoflash.routes.espressif import evidence, hub
from tools.autoflash.runs import ROOT
from tools.autoflash.state import DONE, FAILED
from tools.espressif.safeboot.prepare_imprinted_bundle import (
    BundleError,
    pin_wifi,
    recovery_image,
    startup_image,
)
from tools.espressif.safeboot.safeboot_migration import (
    APP0_OFFSET,
    OFFICIAL_TASMOTA_VERSION,
    SAFEBOOT_OFFSET,
    TABLE_OFFSET,
    TABLE_SIZE,
    earlier_release,
    load_manifest,
    official_release,
    sha256_bytes,
)

if TYPE_CHECKING:
    from tools.autoflash.context import Context


# A phase server's line that says why it stopped, rather than how it is getting on.
# The commit's own checks, made before its first write, that the plug is still
# as E1 and E2 left it (tools/espressif/safeboot/repartition/berry/commit.be.tmpl).
COMMIT_REFUSED_AS_CHANGED_RE = re.compile(r"NVS changed since preflight|staged Safeboot")
# The commit's checks of what it wrote: after any of these it put back what was
# there before (a bit read wrong once, say), so it can be tried again.
COMMIT_READBACK_RE = re.compile(r"read-back differs|SHA-256 differs|did not erase|"
                                r"differ from the originals|changed while being moved|"
                                r"padding differs|erased tail differs")
# A commit that did not finish is tried again, up to this many times in all, from
# the plug as it was before: after a power cut part way (it starts the same
# Tasmota, its settings moved aside - safeboot_migration says how), a read-back
# that differed, or the plug changing since its checks.  Each try has the two
# moments a power cut leaves a plug that cannot start - the interim table and
# the final one, a few milliseconds each - so power that keeps going is not
# tried for ever.
COMMIT_ATTEMPTS = 3


def step_preflight(ctx: Context) -> dict[str, Any]:
    """E1 - read-only inspection.  Re-runnable at any time."""
    hub.plug_back_on_wifi(ctx)
    hub.sonoff_layout_back(ctx)
    ctx.wifi()  # read them off the plug first, so the check below compares the right network
    network = join.check_plug_network(ctx)
    code, captured = ctx.phase_runner().run("preflight")
    if code != 0:
        raise phases.phase_failed("preflight", code, captured)
    return {"exit": code, **network}


def step_stage(ctx: Context) -> dict[str, Any]:
    """E2 - writes the inactive slot only.  Re-runnable; changes no boot state."""
    if not of_this_bundle(ctx, "preflight-report.json"):
        # E1 checked the plug against another bundle: one a run of an earlier
        # version built, which this one rebuilt at A5 (1.2.0's, seen in the
        # fault suite), so the stage server refuses its report every time.
        # E1 only reads: done again here.
        output.say("E1 again: its checks were made with another bundle than this run's")
        ctx.state.mark("E1", DONE, result=step_preflight(ctx))
    code, captured = ctx.phase_runner().run("stage")
    if code != 0:
        raise phases.phase_failed("stage", code, captured)
    return {"exit": code}


def step_commit(ctx: Context) -> dict[str, Any]:
    """E3 - the single destructive operation, tried again only from the plug as
    it was before it (COMMIT_ATTEMPTS)."""
    # Already written: the plug now runs the startup firmware, which cannot
    # answer the checks below, so failing them would send the run back to E1
    # with no way forward.
    if evidence.commit_report(ctx) == "PASS":
        output.say("The plug already reported the new layout written and checked;")
        output.say("there is nothing to do again here.")
        return {"exit": 0, "already_written": True}
    # Stopped as the commit ran - its window closed, say - and finished by the
    # plug alone: it fetched the new layout and runs the startup firmware,
    # which starts only once that is in place. Its report never got here, and
    # a first try would fail its network check on the startup firmware (seen
    # in the fault suite), sending the run back to E1 with no way forward.
    if evidence.table_fetched(ctx.evidence) and plug_on_startup_firmware(ctx):
        output.say("The plug finished the commit after this computer's run was stopped: it")
        output.say("fetched the new layout, and runs the startup firmware now.")
        return {"exit": 0, "finished_unseen": True}

    for attempt in range(1, COMMIT_ATTEMPTS + 1):
        ctx.state.capture("E3.attempts", attempt)
        try:
            if attempt > 1 and commit_recovered(ctx):
                output.say("The new layout is written: the plug finished the commit before it was lost.")
                return {"exit": 0, "finished_unseen": True}
            return commit_once(ctx)
        except (Abort, PhaseError, DeviceError, OSError) as exc:
            # OSError: the plug not answering while it is brought back, which
            # is the network, as TRANSIENT_RE says - not a reason to stop.
            why = commit_retry_reason(ctx, exc)
            if why is None or attempt == COMMIT_ATTEMPTS:
                raise
            output.tell(f"    {why} Putting the plug back as it was and trying again "
                 f"(attempt {attempt + 1} of {COMMIT_ATTEMPTS}).")
            for step in ("E1", "E2"):
                ctx.state.mark(step, FAILED, error="redo: before the commit is tried again")
    raise AssertionError("unreachable")


class CommitFailed(Abort):
    """The commit phase did not pass; `captured` is what the plug said."""

    def __init__(self, message: str, captured: str) -> None:
        super().__init__(message)
        self.captured = captured


def commit_retry_reason(ctx: Context, exc: Exception) -> str | None:
    """Why a failed commit is worth trying again - or None if it is not."""
    captured = getattr(exc, "captured", "")
    if "ROLLBACK FAILED" in captured:
        return None  # the plug could not put the old bytes back: never restarted from here
    text = str(exc)
    if COMMIT_REFUSED_AS_CHANGED_RE.search(text):
        return "The plug changed after its checks (after a power cut, say), so it refused."
    if COMMIT_READBACK_RE.search(text):
        return "A read-back differed, and the plug put back what was there before."
    if isinstance(exc, PhaseError) or errors.TRANSIENT_RE.search(text):
        return "The plug stopped part way - a power cut, or its Wi-Fi going."
    return None


def plug_on_startup_firmware(ctx: Context) -> bool:
    """Whether the plug runs the startup firmware now, once it is back."""
    try:
        hub.plug_back_on_wifi(ctx)
        return ctx.device.is_safeboot()
    except (Abort, DeviceError, OSError):
        return False


def commit_recovered(ctx: Context) -> bool:
    """Before another try: the plug back, and as it was before the commit.

    True when it turns out the commit did finish - its report lost as the
    plug restarted, or never sent with the Wi-Fi gone - the plug then runs the
    startup firmware.  Otherwise Sonoff's layout put back if a power cut left
    the interim one, and the checks and staging done again: a cut part way
    through the copy overwrites the staged image.
    """
    join.wait_while_plug_busy(ctx)  # still finishing the commit this computer lost
    hub.plug_back_on_wifi(ctx)
    if evidence.commit_report(ctx) == "PASS" or ctx.device.is_safeboot():
        return True
    for step, handler in (("E1", step_preflight), ("E2", step_stage)):
        output.say(f"{step} again, before the commit is tried again")
        ctx.state.mark(step, DONE, result=handler(ctx))
    return False


def of_this_bundle(ctx: Context, report_name: str) -> bool:
    """Whether a step's report in the run's evidence (E1's preflight-report.json,
    E2's stage-report.json) was made with the bundle the run has now."""
    try:
        report = json.loads((ctx.evidence / report_name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return report.get("manifest_sha256") == sha256_bytes(ctx.manifest_path.read_bytes())


def commit_once(ctx: Context) -> dict[str, Any]:
    """One try of the commit, from a plug its checks and staging just saw."""
    # E1 and E2 pinned the bundle as it was then.  If it has been rebuilt since,
    # or the plug has moved networks, send the run back to E1 rather than fail
    # inside the commit server with nothing the operator can act on.
    hub.plug_back_on_wifi(ctx)
    hub.sonoff_layout_back(ctx)
    try:
        join.check_plug_network(ctx)  # may rebuild the bundle
        if not of_this_bundle(ctx, "stage-report.json"):
            raise Abort(
                "the bundle has changed since the checks and staging were done, so "
                "they need to run again - run the same command. Nothing was written"
            )
    except Abort:
        for step in ("E1", "E2"):
            ctx.state.mark(step, FAILED, error="redo: bundle or network changed before E3")
        raise
    prompts.section("Rewriting the plug's memory layout", quiet=True, body=[
        "Both checks above passed. This rewrites the one small area of memory",
        "that tells the plug where its firmware lives - the step you agreed to",
        "at the start. It takes about a minute.",
        "",
        "Leave the plug in its socket and this window open. Nothing needs",
        "unplugging, now or later: the run carries on by itself afterwards.",
    ])

    def gate(_: str) -> bool:
        # The commit phase insists on an explicit go-ahead.  It was given once,
        # by typing PROCEED before the first write (C2), and covers this step.
        return True

    with safety_locks.lifted(ROOT, "REPARTITION_LOCK"):
        code, captured = ctx.phase_runner().run(
            "commit",
            extra_flags=["--i-accept-power-loss-may-require-opening-the-plug"],
            confirm=gate,
        )
    safety_locks.say_lock_back(ROOT, "REPARTITION_LOCK")
    if code != 0:
        reason = failure_reason(captured) or ""
        if COMMIT_REFUSED_AS_CHANGED_RE.search(reason):
            # Refused before the first write. The commit checks the plug against
            # what E1 and E2 saw, and only those can see it afresh - running
            # the commit again as it was would be refused the same way, for good.
            for step in ("E1", "E2"):
                ctx.state.mark(step, FAILED, error="redo: the plug changed after its checks")
            raise CommitFailed(
                f"commit refused before writing anything - {reason}. The plug has "
                "changed since it was checked, as it does after a power cut "
                "during a commit or after being set up again. Its checks and "
                "staging run again: run the same command", captured
            )
        raise CommitFailed(str(phases.phase_failed("commit", code, captured)), captured)
    output.say("The new layout is written. The plug restarts by itself, and the run carries on.")
    return {"exit": code}


# An upload making no progress for this long has stalled: official Tasmota takes
# well under a minute, even on a busy plug.
UPLOAD_STALL = 120.0


def upload_to_startup_firmware(ctx: Context, app: Path) -> str:
    """Official Tasmota to the startup firmware (F2); a stalled upload is tried
    again from a restart, up to UPLOAD_ATTEMPTS in all, before it is left to
    the person.

    Seen once in the fault matrix: an upload began and never finished, the
    startup firmware neither taking it nor saying why.  A restart clears that,
    and is safe here: until a whole image is taken, the plug starts the startup
    firmware again."""
    device = ctx.device
    for attempt in range(1, hub.UPLOAD_ATTEMPTS + 1):
        started = time.monotonic()
        try:
            device.upload_firmware(app, timeout=UPLOAD_STALL)
        except Exception as exc:  # noqa: BLE001 - tried again, then the person
            if attempt == hub.UPLOAD_ATTEMPTS:
                return hub.upload_or_prompt_by_hand(ctx, app, "Official Tasmota (app0)", exc)
            what_happened = (f"The plug refused the upload ({exc.reason})" if isinstance(exc, UploadRefused)
                             else f"The upload stopped ({exc})")
            output.tell(f"    {what_happened}; restarting the plug and trying again "
                 f"(attempt {attempt + 1} of {hub.UPLOAD_ATTEMPTS}).")
            try:
                device.command("Restart 1", timeout=10)
            except (DeviceError, OSError):
                output.tell("    The plug is not answering: switch it off and on - at this stage that is")
                output.tell("    safe, it starts the startup firmware again - and this carries on.")
            restart = time.monotonic()
            # Gone first: the restart comes some seconds after its answer, and
            # the old start still answering is no sign of the new one (seen in
            # the fault suite: the upload sent again went to a plug about to go).
            gone_by = time.monotonic() + 30
            while time.monotonic() < gone_by and device.reachable(timeout=2.0):
                time.sleep(1)
            if not recovery_firmware_up(ctx, restart, within=300):
                raise Abort("the startup firmware did not come back after the upload stopped") from exc
            continue
        seconds = round(time.monotonic() - started, 1)
        output.say(f"Official Tasmota (app0) uploaded in {seconds}s")
        return f"http:{seconds}s"
    raise AssertionError("unreachable")


def recovery_firmware_up(ctx: Context, started: float, *, within: float) -> bool:
    """Wait up to `within` seconds for the plug to answer on its recovery firmware."""
    window = time.monotonic()
    while True:
        if ctx.finder.poll():
            device = ctx.device
            try:
                version = device.firmware_version()
            except (DeviceError, OSError):
                version = ""
            if "safeboot" in version.lower() or (not version and device.is_safeboot()):
                output.say(f"    Recovery firmware is up at {device.host} "
                    f"({version or 'safeboot'}, after {time.monotonic() - started:.0f}s).")
                return True
        if time.monotonic() - window >= within:
            return False
        time.sleep(5)


def step_canonical(ctx: Context) -> dict[str, Any]:
    """F - first canonical boot, then the official application."""
    app = ROOT / "captures/safeboot-recovery-migration/tasmota32c3.bin"
    if runs.run_source(ctx.state) == "committed" and ctx.finder.poll() and not ctx.device.is_safeboot():
        # A run stopped after official Tasmota went on, before it was set up.
        # Only on this route: after E3 the interim Tasmota still answers for a
        # moment, which this would take for the official one.
        version = ctx.device.firmware_version() or ""
        if version and "bluetooth" not in version:
            output.say(f"Official Tasmota is installed already ({version}).")
            return {"already": version}
    prompts.section("The plug has restarted with its new layout", quiet=True, body=[
        "It is now running a small recovery firmware whose only job is to accept",
        "the real Tasmota. It joins your Wi-Fi by itself.",
    ])
    # Answering a ping proves nothing here: the old firmware is still up for a
    # second or two after Restart 99.  Wait for the firmware itself to change.
    output.say("")
    output.say("Waiting for the plug to restart into the recovery firmware.")
    time.sleep(10)
    started = time.monotonic()
    while not recovery_firmware_up(ctx, started, within=300):
        # Five minutes and no sign of it: keep waiting, or look elsewhere.
        output.transient_begin()
        choice = prompts.ask_choice(
            "The recovery firmware has not appeared. What now",
            [
                "Keep waiting",
                "It is on a different address - let me type it",
            ],
            help_lines=[
                "It should restart into a small recovery firmware whose version",
                "contains the word safeboot, on your Wi-Fi.",
            ],
        )
        if choice == 1:
            ctx.set_plug_ip(prompts.ask_ipv4("What address is it on now", must_respond=True))
        output.transient_end()
    waited = time.monotonic() - started
    device = ctx.device
    saved = startup_settings_report(device)
    if ctx.dry_run:
        output.say("[dry-run] would upload the official application")
        return {"safeboot_seconds": round(waited, 1), "startup_settings": saved}
    how, outcome = install_official(ctx, app)
    return {"safeboot_seconds": round(waited, 1), "app_upload": how, "outcome": outcome,
            "startup_settings": saved}


def step_tasmota_back(ctx: Context) -> dict[str, Any]:
    """T1 - Tasmota back on a plug left on Tasmota's own safeboot (SOURCES:
    "safeboot"): its own started again if it is there - switched to the
    safeboot by hand, say - else official Tasmota uploaded as F2 does, which
    writes app0 only, so its settings stay.  It must then be a plug this tool
    converts."""
    app = ROOT / "captures/safeboot-recovery-migration/tasmota32c3.bin"
    how = outcome = None
    if ctx.finder.poll() and not ctx.device.is_safeboot():
        output.say("Tasmota is back on the plug already.")
    else:
        prompts.section("Putting Tasmota back", quiet=True, body=[
            "The plug runs Tasmota's safeboot, the small firmware Tasmota keeps for",
            "updates. Its own Tasmota is started again if it is there; if not - after",
            "an upload through its page that failed part way, say - official Tasmota",
            "goes back on. Its settings stay as they were either way.",
        ])
        if ctx.dry_run:
            output.say("[dry-run] would start Tasmota again, or upload official Tasmota")
            return {}
        if ctx.device.wants_password() is True:  # it keeps Tasmota's settings, and so its password
            unlock.ask_web_password(ctx, ctx.device.host)
        if tasmota_started_again(ctx):
            how, outcome = "started again", "rejoined the network"
        else:
            how, outcome = install_official(ctx, app)
        if not outcome.startswith("rejoined"):
            raise Abort(f"official Tasmota is back on the plug, but {outcome}: it lost its settings. "
                        "Set it up there - its Wi-Fi, and the plug's template - then run this again")
    found = None
    for _ in range(4):
        try:
            found = detect.tasmota(ctx.device.host, timeout=10, password=ctx.web_password)
            break
        except detect.Unreadable:  # just started, too busy to say its template
            time.sleep(5)
        except detect.Locked:
            unlock.ask_web_password(ctx, ctx.device.host)
    if not found or found[0] != "tasmota":
        raise Abort("official Tasmota is back on the plug, with its settings, but it is not set up as "
                    "a plug this tool converts (no template for one). It runs as it did before")
    _, name, kind = found
    if kind:
        ctx.state.set_input("plug_kind", kind)
    output.say(f"    Tasmota is back{f' as {name}' if name else ''}, with its settings.")
    return {"app_upload": how, "outcome": outcome, "name": name}


# How long a plug asked to start Tasmota again may take before it is back on
# the safeboot - nothing there to start - or Tasmota answers.
TASMOTA_START_WAIT = 90.0
BACK_ON_SAFEBOOT_AFTER = 30.0


def tasmota_started_again(ctx: Context) -> bool:
    """Whether the Tasmota a plug on Tasmota's safeboot holds starts when the
    safeboot is asked to start it (Device.start_app) - so is there, unharmed."""
    output.say("Asking the safeboot to start the Tasmota it holds, as its own page's switch does...")
    try:
        ctx.device.start_app()
    except (DeviceError, OSError) as exc:
        output.say(f"    It did not take the request ({exc}).")
        return False
    started = time.monotonic()
    time.sleep(5)  # it answers once more before it restarts
    while time.monotonic() - started < TASMOTA_START_WAIT:
        try:
            version = ctx.device.firmware_version() or ""
        except (DeviceError, OSError):
            version = ""
        if version and "safeboot" not in version.lower():
            output.say(f"    Tasmota started again ({version}): it was there, unharmed.")
            return True
        if "safeboot" in version.lower() and time.monotonic() - started > BACK_ON_SAFEBOOT_AFTER:
            break
        time.sleep(3)
    output.say("    It came back on its safeboot: there is no Tasmota there that starts.")
    return False


def install_official(ctx: Context, app: Path) -> tuple[str, str]:
    """Official Tasmota uploaded to the startup firmware, and seen running -
    sent again when the plug comes back on the startup firmware instead (the
    upload did not take), up to UPLOAD_ATTEMPTS in all."""
    for attempt in range(1, hub.UPLOAD_ATTEMPTS + 1):
        how = upload_to_startup_firmware(ctx, app)
        outcome = wait_for_official(ctx)
        if outcome != "startup firmware":
            return how, outcome
        if attempt < hub.UPLOAD_ATTEMPTS:
            output.tell("    The plug came back on the startup firmware: the upload did not take. "
                 f"Sending it again (attempt {attempt + 1} of {hub.UPLOAD_ATTEMPTS}).")
    raise Abort("official Tasmota did not take: the plug came back on its startup firmware "
                f"after {hub.UPLOAD_ATTEMPTS} uploads")


# Still on the startup firmware this long after an upload, it did not take:
# a good one restarts the plug into Tasmota within seconds.
STILL_STARTUP_AFTER = 60.0


def wait_for_official(ctx: Context) -> str:
    """After the upload: Tasmota back on the network, or on its own setup
    network - or "startup firmware" when the plug is still, or again, on that.

    Seen running only from what it says it is: a plug restarting does not
    answer its page for a moment, which is no sign of Tasmota."""
    # The startup firmware normally saved the Wi-Fi where Tasmota reads it, so
    # Tasmota comes back on the network.  If it did not, Tasmota raises its own
    # setup network instead and F4 sets it up through that.  Both mean it worked.
    output.say("")
    output.say("Waiting for the full Tasmota to start. It will either rejoin your")
    output.say("Wi-Fi, or put up its own setup network - both are normal.")
    started = time.monotonic()
    while time.monotonic() - started < 300:
        time.sleep(5)
        if ctx.finder.poll():
            try:
                version = ctx.device.firmware_version()
            except (DeviceError, OSError):
                version = ""
            if version and "safeboot" not in version.lower():
                output.say(f"    The plug is back at {ctx.device.host} running the full Tasmota ({version}).")
                return "rejoined the network"
            if "safeboot" in version.lower() and time.monotonic() - started > STILL_STARTUP_AFTER:
                return "startup firmware"
            continue
        setup = prompts.find_ssid(names.setup_networks(ctx), timeout=10)
        if setup:
            output.say(f"    The plug has started and is asking to be set up on {setup}.")
            names.note_setup_network(ctx, setup)
            return f"raised its setup network {setup}"
    raise Abort(
        "the plug neither rejoined the network nor put up a setup network "
        f"({names.setup_network_hint(ctx)}) within five minutes"
    )


# How the startup firmware begins what it logs of that; the second, before it
# was renamed - on a plug a run from an older checkout left part way.
STARTUP_LOG_PREFIXES = ("Autoflash recovery:", "S60 recovery:")


def startup_settings_report(device: device.Device) -> str | None:
    """What the startup firmware logged about saving the Wi-Fi for Tasmota.

    Kept in the run's record either way, so a fall back to the setup network
    at F4 can be explained.  None if its log could not be read or says nothing.
    """
    try:
        lines = device.console_log(timeout=10.0)
    except (DeviceError, OSError, ValueError) as exc:
        output.say(f"    Could not read the recovery firmware's log ({exc}).")
        return None
    notes = [(line[line.index(prefix):], prefix) for line in lines
             for prefix in STARTUP_LOG_PREFIXES if prefix in line]
    if not notes:
        output.say("    The recovery firmware's log does not say whether it saved your Wi-Fi.")
        return None
    note, prefix = notes[-1]
    if "saved for the next firmware" in note:
        output.say("    It has saved your Wi-Fi where official Tasmota reads it, so Tasmota")
        output.say("    should join your network by itself.")
    elif "left alone" in note:
        output.say("    Tasmota settings from before were still there; it left them as they were.")
    else:
        output.say(f"    It could not save your Wi-Fi for official Tasmota: {note[len(prefix):].strip()}.")
        output.say("    Tasmota will ask to be set up through its own network instead, as before.")
    return note


def step_configure(ctx: Context) -> dict[str, Any]:
    """F3/F4 - Wi-Fi, settings persistence and the plug's template; then the
    meter calibrated, which the template's reset put back to the chip's own."""
    result = configure_tasmota(ctx)
    result["meter"] = calibrate_tasmota_meter(ctx)
    return result


def calibrate_tasmota_meter(ctx: Context, show: bool = False) -> dict[str, Any]:
    """Tasmota's meter calibrated as Sonoff's firmware calibrates it: by the
    board's factor and, from the plug's Sonoff backup, its own factory
    calibration (calibration says what carries over).  Never fails the step:
    a plug left as Tasmota calibrates it still works."""
    try:
        outcome = calibration.calibrate_tasmota(lambda text: ctx.device.command(text, timeout=SLOW_COMMAND),
                                                wifi.backup_calibration(ctx))
    except (DeviceError, OSError, ValueError) as exc:
        outcome = {"skipped": str(exc)}
    if "skipped" in outcome:
        output.tell(f"    The meter is left as it is: {outcome['skipped']}.")
    elif not outcome["changed"]:
        output.say("The meter is calibrated as Sonoff's firmware had it already", show=show)
    else:
        output.say("Meter calibrated as Sonoff's firmware had it"
            + (", with the plug's factory calibration from its backup" if outcome["factory"] else "")
            + f" (board factor {outcome['factor']:.2f})", show=show)
    return outcome


def configure_tasmota(ctx: Context) -> dict[str, Any]:
    """Wi-Fi, settings persistence and the plug's template (step_configure)."""
    if ctx.finder.poll():
        route = "joined by itself"
        output.say("Official Tasmota joined your Wi-Fi by itself, with the details the")
        output.say("startup firmware saved - no setup network needed.")
    else:
        setup = prompts.find_ssid(names.setup_networks(ctx), timeout=15)
        if setup:
            route = "setup network"
            output.tell("    Official Tasmota did not join your Wi-Fi by itself, so it is set up")
            output.tell("    through its own network instead.")
            join.configure_plug_wifi(ctx, setup)
        else:
            route = "located"
            join.locate_plug(ctx, setup=True)
    device = ctx.device
    device.wait_reachable(timeout=300)
    if device.is_safeboot():
        # F2 saw it as done, but official Tasmota did not take (a run from
        # before F2 checked for that): sent again, as F2 would.
        output.tell("    The plug is still on its startup firmware: official Tasmota is sent again.")
        install_official(ctx, ROOT / "captures/safeboot-recovery-migration/tasmota32c3.bin")
        device = ctx.device
        device.wait_reachable(timeout=300)
    template = kinds.of_run(ctx.state).tasmota_template(ctx.state.input("product_model"))
    hostname = names.final_name(ctx, "tasmota")
    if template_in_place(device, template, hostname):
        # Tried again after the plug took the template but its answer was slow
        # (a busy plug): resetting again would only restart it again.
        output.say("The plug's template and settings are in place already.")
        return {"template": "applied", "wifi_route": route}
    # Tasmota may be running on the record the startup firmware saved, which
    # holds the Wi-Fi but only what a Safeboot sets (it skips most defaults,
    # never expecting to save them).  Reset 4 rebuilds every setting from this
    # firmware's own defaults and keeps the Wi-Fi.  Harmless when Tasmota
    # started from defaults anyway.
    #
    # Its own defaults name it Tasmota's way (tasmota-ABCDEF-1234), and a name
    # sent with the reset goes before it (Tasmota resets once its commands are
    # done) and is lost.  So the name and the template are put in as the reset
    # restart saves the rebuilt defaults: the plug comes back as converted,
    # under no other name, in that one restart.
    # (SaveData 1 is a default the reset puts back, and a command that saves
    # cannot go in there: see at_next_save.)
    if runs.run_source(ctx.state) != "untemplated":
        hooked = device.at_next_save([*([f"Hostname {hostname}"] if hostname else []),
                                      f"Template {template}", "Module2 0", "Module 0"],
                                     timeout=SLOW_COMMAND)
        booted, uptime = device.started_at(), device.uptime_seconds()
        device.command("Reset 4", timeout=SLOW_COMMAND)
        output.say("Settings reset to Tasmota's defaults, keeping the Wi-Fi"
            + (", with the plug's template and name" if hooked else "") + "; the plug restarts")
        back_after_reset(ctx, booted, uptime)
        device = ctx.device
        if hooked and template_in_place(device, template, hostname):
            output.say(f"Template applied as {json.loads(template)['NAME']}"
                + (f", and named {hostname}" if hostname else ""))
            return {"template": "applied", "wifi_route": route, "restarts": 1}
    # Not taken - a power cut before the save, say, or a Tasmota whose Berry
    # would not have it: put in after the reset, at the cost of a restart.
    # Or a Tasmota in use that lost its template: put in without the reset,
    # its other settings (its MQTT, say) kept.
    give_template(device, template, hostname)
    return {"template": "applied", "wifi_route": route,
            "restarts": 1 if runs.run_source(ctx.state) == "untemplated" else 2}


def give_template(device: device.Device, template: str, hostname: str | None) -> None:
    """The plug's template and name put in, its other settings kept, at the
    cost of a restart - and asked for after it.  Left without them, the plug
    switches nothing (seen: a user's Home Assistant showed only its restart
    button and Wi-Fi)."""
    booted, uptime = device.started_at(), device.uptime_seconds()
    device.command("SaveData 1", timeout=SLOW_COMMAND)
    device.command(f"Template {template}", timeout=SLOW_COMMAND)
    # Named as every converted Tasmota is (tasmota-abcdef), and its own network
    # with it; one restart for both, as each would restart the plug alone.
    device.command(f"Backlog {f'Hostname {hostname}; ' if hostname else ''}Module2 0; Module 0",
                   timeout=SLOW_COMMAND)
    output.say(f"Template applied as {json.loads(template)['NAME']}"
        + (f", and named {hostname}" if hostname else "") + "; the plug restarts")
    device.wait_for_reboot(timeout=180, started_at=booted, before=uptime)
    # Restarted is not yet answering: the next step asks the plug at once.
    device.wait_reachable(timeout=120)
    if not template_in_place(device, template, hostname):
        raise Abort("the plug did not keep its template and settings; it is safe to do this step again")


def back_after_reset(ctx: Context, booted: str | None, uptime: int | None) -> None:
    """The plug back after Reset 4 - or set up through its own network, when it
    came back without its Wi-Fi details.

    Tasmota keeps the Wi-Fi through Reset 4 only once it has saved its new
    defaults; cut off in the second before (a power cut, seen in the fault
    suite) it starts with none, and raises its setup network."""
    device = ctx.device
    try:
        device.wait_for_reboot(timeout=180, started_at=booted, before=uptime)
        device.wait_reachable(timeout=120)
        return
    except DeviceError:
        pass
    setup = prompts.find_ssid(names.setup_networks(ctx), timeout=30)
    if not setup:
        raise Abort(
            "the plug did not come back after its settings were reset, and its own "
            f"network ({names.setup_network_hint(ctx)}) is not to be seen either. Switch it "
            "off and on, then run this again")
    output.tell("    The plug restarted without its Wi-Fi details - cut off just after the")
    output.tell("    reset, say - and raised its own network, so it is set up through that.")
    join.configure_plug_wifi(ctx, setup)
    ctx.device.wait_reachable(timeout=300)


# Commands that rewrite Tasmota's settings answer slowly on a busy plug: given
# longer than the usual few seconds before taken as not answering.
SLOW_COMMAND = 30.0


def template_in_place(device: device.Device, template: str, hostname: str | None = None,
                      fallback: bool = True) -> bool:
    """Whether F4 has done its work already: the plug's template in use,
    settings saved, its name `hostname` (when known) - and, with `fallback`,
    the template as Tasmota's fallback module too.

    Restarted six times in a row, each within 10 s of starting, Tasmota takes
    itself for a boot loop and goes over to its fallback module - Generic,
    by default - keeping its Wi-Fi: a plug that switches nothing (seen: a
    user's, half an hour after it was converted). Its own template there, it
    goes on switching."""
    try:
        current = device.command("Template", timeout=SLOW_COMMAND)
        module = device.command("Module", timeout=SLOW_COMMAND)
        saving = device.command("SaveData", timeout=SLOW_COMMAND)
        named = device.command("Hostname", timeout=SLOW_COMMAND) if hostname else {"Hostname": None}
        fallen = device.command("Module2", timeout=SLOW_COMMAND) if fallback else {"Module2": {"0": ""}}
    except (DeviceError, OSError):
        return False
    name = json.loads(template)["NAME"]
    return (isinstance(current, dict) and current.get("NAME") == name
            and isinstance(module, dict) and "0" in (module.get("Module") or {})
            and isinstance(saving, dict) and saving.get("SaveData") in ("ON", "1", 1)
            and isinstance(named, dict) and named.get("Hostname") == hostname
            and isinstance(fallen, dict) and "0" in (fallen.get("Module2") or {}))


def recovery_image_record(ctx: Context) -> str:
    """The SHA-256 of the startup firmware this run put on the plug.

    The commit's report records it; when that report was lost, the staging's
    does, of the image the commit then copied as it was.
    """
    for name, key in (("commit-report.json", "safeboot_sha256"), ("stage-report.json", "staged_sha256")):
        try:
            report = json.loads((ctx.evidence / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(report, dict) and report.get("status") == "PASS" and report.get(key):
            return str(report[key]).lower()
    raise Abort("this run has no record of the startup firmware it put on the plug")


def conversion_wifi(ctx: Context) -> None:
    """Hold the Wi-Fi details the plug was converted with, for G1.

    G1 needs the exact startup firmware on the plug, and only those details
    rebuild it.  They are proven against the commit's record of the image it
    put in place, not against the bundle: the bundle may have been set up for
    other Wi-Fi since, and the plug's own Wi-Fi may have changed.  Only proven
    details are pinned back into the bundle, which the migration server needs.
    """
    wanted = recovery_image_record(ctx)

    def proven(ssid: str, password: str) -> bytes | None:
        try:
            image = startup_image(ssid, password)
        except BundleError:
            return None
        return image if sha256_bytes(image) == wanted else None

    details = None if getattr(ctx.args, "wifi", False) else wifi.read_wifi_from_plug(ctx)
    image = proven(*details) if details else None
    if image is None:
        output.transient_begin()  # until details that match are given
        prompts.section("The Wi-Fi details the plug was converted with are needed", [
            *(["The plug is on different Wi-Fi details now."] if details else []),
            "Its temporary startup firmware was built with the Wi-Fi details",
            "used during the conversion, and only those rebuild it exactly, so",
            "it can be swapped for the official one. They are checked against",
            "the plug's record before anything is used.",
        ])
        while image is None:
            ssid = prompts.ask_text("Wi-Fi network name used during the conversion", private="wifi-name")
            password = prompts.ask_secret(f"Wi-Fi password for '{ssid}' at the time: ")
            output.private(password, "wifi-password")
            details = (ssid, password)
            image = proven(ssid, password)
            if image is None:
                output.tell("    Those are not the details the plug was converted with - try again.")
        output.transient_end()
    ssid, password = details
    try:
        recovery_image(ctx.manifest_path, ssid, password)
    except BundleError:
        output.say(f"Setting the firmware bundle back up for '{ssid}', the Wi-Fi the plug was converted with.")
        try:
            pin_wifi(ctx.manifest_path, ssid, password)
        except BundleError as exc:
            raise Abort(f"the bundle could not be set up for '{ssid}': {exc}") from exc
    ctx.remember_wifi(ssid, password, image)


# How many times G1 asks the plug whether it runs official Tasmota, when the
# network drops the asking.
RESTORE_CHECK_TRIES = 3


def step_restore(ctx: Context) -> dict[str, Any]:
    """G - replace the private recovery Safeboot with official bytes.

    A run begun by 1.2.0 past its commit (startup_unproven) has that version's
    startup firmware on the plug, which this checkout cannot rebuild, so no
    Wi-Fi details prove it: the restore goes without it, writing over
    whatever follows the official sectors, once the plug shows the official
    table and official app0 running (the plug's own checks, and the gate
    below) - the factory partition is unused then, and H1 checks the result."""
    unproven = bool(ctx.state.input("startup_unproven"))
    if not unproven:
        conversion_wifi(ctx)  # before the runner, which would otherwise pin the plug's own
    runner = ctx.phase_runner(with_recovery=not unproven)
    output.say("== Restore official Safeboot")
    if unproven:
        output.say("This run was begun by an earlier version of this tool, whose startup firmware")
        output.say("this one cannot rebuild to check: the official one goes over it all the same.")

    def gate(_: str) -> bool:
        output.say("Swapping the temporary startup firmware - the one with your Wi-Fi")
        output.say("details in it - for the official one. The plug keeps running throughout.")
        for attempt in range(1, RESTORE_CHECK_TRIES + 1):
            try:
                matches, problem = verify_app(ctx)
                break
            except errors.NETWORK_ERRORS as exc:
                # Seen in the fault suite: a Wi-Fi blip as it asked stopped the
                # step, as if the plug had failed the check. Asked again once
                # the plug answers.
                matches, problem = False, f"the check could not run: {exc}"
                if attempt < RESTORE_CHECK_TRIES:
                    try:
                        ctx.device.wait_reachable(timeout=30.0)
                    except DeviceError:
                        pass
            except (DeviceError, OSError, ValueError) as exc:
                matches, problem = False, f"the check could not run: {exc}"
                break
        if matches:
            output.say("  Checked first: the plug is running the official Tasmota, unmodified.")
            return True
        output.tell(f"    Stopping: {problem}. Swapping the startup firmware is only safe")
        output.tell("    under the official Tasmota, so nothing was written.")
        return False

    code, captured = runner.run(
        "restore", extra_flags=["--i-confirm-normal-app-is-stable", *(["--recovery-unproven"] if unproven else [])],
        confirm=gate,
    )
    if code != 0:
        raise phases.phase_failed("restore", code, captured)
    return {"restore": code}


def step_verify(ctx: Context) -> dict[str, Any]:
    """H1 - the four hashes, read back off the device itself."""
    manifest = load_manifest(ctx.manifest_path)
    artifacts = manifest["artifacts"]
    device = ctx.device
    device.wait_reachable(timeout=180)

    expected = {
        # Written once at D5, for whichever firmware comes after.
        "bootloader": (0, esphome.BOOTLOADER_SIZE, esphome.BOOTLOADER_SHA256),
        "table": (TABLE_OFFSET, TABLE_SIZE, artifacts["target_table"]["sha256"]),
        "safeboot": (SAFEBOOT_OFFSET, artifacts["safeboot"]["size"], artifacts["safeboot"]["sha256"]),
        "app0": (APP0_OFFSET, artifacts["app"]["size"], artifacts["app"]["sha256"]),
    }
    if ctx.state.input("bootloader_kept"):
        # D5 left out, for a run begun by 1.2.0: Sonoff's, as that version left it.
        del expected["bootloader"]
        output.say("  bootloader: Sonoff's, kept - this run was begun by an earlier version")
    results: dict[str, Any] = {}
    ok = True
    for name, (address, length, wanted) in expected.items():
        actual = device.sha_region(address, length)
        matched = actual == wanted.lower()
        ok = ok and matched
        results[name] = {"expected": wanted, "actual": actual, "match": matched}
        report = output.tell if not matched else output.say
        report(f"  {name:<9} 0x{address:06X} {length:>9,}  {'MATCH' if matched else 'DIFFERS'}")
        if not matched:
            output.tell(f"    expected {wanted}")
            output.tell(f"    actual   {actual}")
    if not ok:
        raise Abort("device does not match the pinned artifacts")
    output.say("")
    output.say(f"All {'four' if len(expected) == 4 else 'three'} regions match the pinned artifacts.")
    template = kinds.of_run(ctx.state).tasmota_template(ctx.state.input("product_model"))
    hostname = names.final_name(ctx, "tasmota")
    if not template_in_place(device, template, hostname):
        if template_in_place(device, template, hostname, fallback=False):
            # Converted by an earlier version: the fallback alone, no restart.
            device.command("Module2 0", timeout=SLOW_COMMAND)
            output.say("  The plug's template is now its fallback module too, should Tasmota take")
            output.say("  itself for a boot loop.")
            ctx.state.capture("H1.fallback_set", True)
        else:
            # A run is not complete on a plug that switches nothing.
            output.tell("    The plug's template is not in place: it is put in again, its other settings kept.")
            ctx.state.capture("H1.template_again", True)
            give_template(device, template, hostname)
    earlier = earlier_release(manifest) if manifest.get("official_release") != official_release() else None
    if earlier is not None:
        # Begun by an earlier version, which installed that release at F2.
        output.tell(f"  The plug has Tasmota {earlier['version']}, the release this conversion began with.")
        output.tell(f"  Its own page (Firmware Upgrade) takes it to {OFFICIAL_TASMOTA_VERSION}, the latest.")
        results["release"] = earlier["version"]
    return results


def _hash_matches(ctx: "Context", name: str, offset: int, size_key: str) -> tuple[bool, str]:
    manifest = load_manifest(ctx.manifest_path)
    artifact = manifest["artifacts"][size_key]
    actual = ctx.device.sha_region(offset, artifact["size"])
    if actual == artifact["sha256"].lower():
        return True, ""
    return False, f"{name} on the plug no longer matches the pinned image"


def verify_high_slot(ctx: "Context") -> tuple[bool, str]:
    """Was the plug promoted to the high slot?  Only meaningful before the commit.

    After the layout is replaced there is no high slot to be in, and Safeboot has
    no Berry to ask.  Both are 'cannot check', which must not be reported as
    'the device disagrees'.
    """
    if ctx.state.is_done("E3") or evidence.commit_report(ctx) == "PASS":
        return True, ""  # superseded: the old two-slot layout no longer exists
    if ctx.state.is_done("I1"):
        return True, ""  # superseded: ESPHome runs now, not the Tasmota it asked
    slot = ctx.device.current_ota()
    if slot == 1:
        return True, ""
    if slot is None:
        raise DeviceError(
            "the running slot cannot be read - the plug is not running a "
            "Berry-capable build"
        )
    return False, f"expected the plug to be running from the high slot, it reports {slot}"


def verify_table(ctx: "Context") -> tuple[bool, str]:
    manifest = load_manifest(ctx.manifest_path)
    wanted = manifest["artifacts"]["target_table"]["sha256"].lower()
    actual = ctx.device.sha_region(TABLE_OFFSET, TABLE_SIZE)
    if actual == wanted:
        return True, ""
    return False, "the plug's partition table is not the migrated one"


def verify_app(ctx: "Context") -> tuple[bool, str]:
    return _hash_matches(ctx, "app0", APP0_OFFSET, "app")


def verify_safeboot(ctx: "Context") -> tuple[bool, str]:
    return _hash_matches(ctx, "safeboot", SAFEBOOT_OFFSET, "safeboot")


# Checked before a completed step is skipped on resume.  A step with no entry
# here is skipped on the record alone; these are the ones worth proving.
STEP_VERIFIERS: dict[str, Callable[["Context"], tuple[bool, str]]] = {
    "D4": verify_high_slot,
    "E3": verify_table,
    "F2": verify_app,
    "G1": verify_safeboot,
}
