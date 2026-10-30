#!/usr/bin/env python3
"""Convert a plug over the air: from its stock eWeLink firmware to Tasmota or
ESPHome, from either of those to the other, or back to its stock firmware.

This orchestrates the reviewed tools in tools/.  It reimplements no validator,
no Berry phase and no safety gate: every destructive action still runs the same
audited program, behind the same locks, judged by the same host-side evidence.
What it removes is console pasting, transcription error and lost place.

Every step records itself in captures/<plug>/autoflash-state.json, so an
interrupted run resumes where it stopped.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import argparse  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402

from tools.autoflash import (  # noqa: E402
    __version__,
    awake,
    credentials,
    firewall,
    hostsys,
    interrupt,
    join,
    output,
    plugs,
    prompts,
    report,
    routes,
    runner,
    runs,
)
from tools.autoflash.context import Context  # noqa: E402
from tools.autoflash.errors import Abort  # noqa: E402
from tools.autoflash.hostsys import FirewallRule  # noqa: E402
from tools.autoflash.prompts import QuitRun  # noqa: E402
from tools.autoflash.routes.espressif import bundle, installer  # noqa: E402
from tools.autoflash.state import RunState  # noqa: E402
from tools.espressif.safeboot.prepare_imprinted_bundle import forget_stored_password  # noqa: E402



def adopt_older_runs() -> None:
    """Move runs kept straight under captures/ to their plug's directory, when
    they know its MAC.  Those that do not stay where they are, still listed."""
    pointer = runs.read_current()
    for directory in [d for d in runs.run_directories() if d.parent == plugs.CAPTURES]:
        try:
            mac = RunState.open(directory).input("plug_mac")
            if not mac or not plugs.MAC_RE.match(mac):
                continue
            target = plugs.runs_dir(mac) / directory.name
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            directory.rename(target)
        except (OSError, ValueError):
            continue  # left where it was; it still works from there
        if pointer == directory.name:
            runs.remember_current(runs.run_name(target))


def run_is_complete(directory: Path) -> bool:
    try:
        state = RunState.open(directory)
    except (OSError, ValueError):
        return False
    steps = routes.steps_of(state)
    if not steps:
        return state.input("firmware") is not None  # it already ran what was asked
    return state.is_done(steps[-1][0])


def describe_run(directory: Path) -> str:
    """Where a run stopped, briefly: the plug, the step, when, and how far in."""
    import datetime as dt

    state = RunState.open(directory)
    where = state.input("plug_ip") or "a plug not yet found"
    mac = plugs.mac_of_run(directory) or state.input("plug_mac")
    if mac and state.input("plug_ip"):
        where += f", MAC {mac}"
    pending = next((step for step, _, _ in routes.steps_of(state)
                    if not state.is_done(step)), None)
    try:
        when = dt.datetime.fromisoformat(state.data.get("updated_utc", "")).astimezone()
        today = when.date() == dt.date.today()
        stamp = when.strftime("%H:%M today" if today else "%d %b %H:%M")
    except ValueError:
        stamp = "an unknown time"
    details = [f"stopped at step {pending}" if pending else "finished", stamp]
    if pending and state.input("stopped_by") == "window":
        details.append("when its window was closed")
    if routes.past_the_point_of_no_return(state):
        details.append("past the point of no return")
    if not state.input("plug_ip"):
        return f"a run that had not found its plug yet ({', '.join(details)})"
    return f"the plug at {where} ({', '.join(details)})"


def start_new_run(plug_ip: str | None) -> str:
    name = runs.new_run_name(plug_ip)
    runs.remember_current(name)
    return name


# The answer to the unfinished-run menu.  The menu is answered before the run's
# log exists and leaves the screen once answered, so it is held here and
# written into the log when it opens - otherwise a plug left part converted by
# "start a new plug" leaves no trace of how.
RUN_CHOICE: list[str] = []


def log_run_choice() -> None:
    for line in RUN_CHOICE:
        output.say(line)
    RUN_CHOICE.clear()


def resolve_run_name(
    requested: str | None, force_new: bool, plug_ip: str | None, ask: bool = False
) -> str:
    """Work out which conversion this is, without asking for an identifier.

    One in-flight operation, remembered in a well-known place.  With `ask` - a
    `run` - an unfinished conversion is offered back rather than resumed
    silently, so starting the next plug is never mistaken for finishing the
    last.  Nothing opaque is ever typed by the operator.
    """
    if requested:
        runs.remember_current(requested)
        return requested
    if force_new:
        return start_new_run(plug_ip)

    unfinished = [d for d in runs.run_directories() if not run_is_complete(d)]
    if not unfinished:
        return start_new_run(plug_ip)

    pointer = runs.read_current()
    # Most recently touched first, so the likely answer is option 1.
    unfinished.sort(key=lambda d: (runs.run_name(d) != pointer, -d.stat().st_mtime))
    if not ask:
        chosen = unfinished[0]
        output.say(f"Continuing {describe_run(chosen)}.")
        RUN_CHOICE.append(f"Continuing {describe_run(chosen)}, the most recent unfinished run.")
        runs.remember_current(runs.run_name(chosen))
        return runs.run_name(chosen)

    while unfinished:
        # The menu leaves the screen once answered; after a removal it is
        # shown again, not stacked under the old one.
        output.transient_begin(before_start=True)
        any_past = any(routes.past_the_point_of_no_return(RunState.open(d)) for d in unfinished)
        keep = ("the unfinished one stays" if len(unfinished) == 1
                else "the unfinished ones stay")
        options = [f"Continue {describe_run(d)}" for d in unfinished] + [
            f"Start a new plug - {keep} on record, to finish later",
            "Remove an unfinished run",
        ]
        chosen = prompts.ask_choice(
            "A conversion did not finish last time. What now",
            options,
            help_lines=(
                ["A plug past the point of no return no longer runs its Sonoff",
                 "firmware - finish it before long; running this again offers it back."]
                if any_past else []
            ),
        )
        if chosen < len(unfinished):
            output.transient_end()
            RUN_CHOICE.append(f"Chosen at the start: continue {describe_run(unfinished[chosen])}.")
            runs.remember_current(runs.run_name(unfinished[chosen]))
            return runs.run_name(unfinished[chosen])
        if chosen == len(unfinished):
            output.transient_end()
            RUN_CHOICE.append("Chosen at the start: a new plug. Left unfinished, on record:")
            RUN_CHOICE.extend(f"    {describe_run(d)}" for d in unfinished)
            return start_new_run(plug_ip)
        target = unfinished[0] if len(unfinished) == 1 else unfinished[prompts.ask_choice(
            "Which run should be removed",
            [describe_run(d)[0].upper() + describe_run(d)[1:] for d in unfinished],
        )]
        if remove_run(target):
            unfinished.remove(target)
        output.transient_end()  # the menu and the removal: shown again, or gone
    return start_new_run(plug_ip)


def remove_run(directory: Path) -> bool:
    """Delete one run's record, after removing any firewall rule it left behind."""
    state = RunState.open(directory)
    description = describe_run(directory)
    if routes.past_the_point_of_no_return(state):
        prompts.section("This plug is part-converted", [
            "It no longer runs its Sonoff firmware, so it will not show up as a",
            "new plug either. Once its record is removed, this tool cannot finish",
            "it - finishing would mean doing the remaining steps by hand.",
        ])
    if not prompts.ask_yes_no(f"Remove the record of {description}", default=False):
        return False
    rules = [FirewallRule.from_json(item) for item in state.input("firewall_rules") or []]
    if rules:
        output.say("It left a temporary firewall rule letting that plug connect to this")
        output.say("computer. Removing it needs administrator rights, so you may be asked")
        output.say("for this computer's password.")
        for line in firewall.emulator_password_hint():
            output.say(line)
        left = [rule for rule in rules if not hostsys.close_firewall(rule)]
        if left:
            output.transient_end()  # this has to stay on screen
        for rule in left:
            for line in hostsys.undo_commands(rule):
                output.tell("Remove it yourself: " + line)
    shutil.rmtree(directory)
    if runs.read_current() == runs.run_name(directory):
        runs.CURRENT_POINTER.unlink(missing_ok=True)
    output.say(f"Removed the record of {description}.")
    return True


def cmd_status(ctx: Context) -> int:
    output.say(f"Plug:      {ctx.plug}")
    output.say(f"Address:   {ctx.state.input('plug_ip') or '(not set)'}")
    output.say(f"Manifest:  {ctx.manifest_path}")
    output.say(f"State:     {ctx.state.path}")
    output.say("")
    output.say(ctx.state.summary([(step, title) for step, title, _ in routes.STEPS]))
    captures = ctx.state.data.get("captures", {})
    if captures:
        output.say("")
        output.say("Captures:")
        for key, value in sorted(captures.items()):
            output.say(f"  {key}: {json.dumps(value)}")
    return 0


def cmd_step(ctx: Context, step: str) -> int:
    if step not in routes.STEP_INDEX:
        output.say(f"unknown step {step}; known: {', '.join(s for s, _, _ in routes.STEPS)}")
        return 2
    join.wait_while_plug_busy(ctx)
    return runner.execute_with_retries(ctx, step)


def cmd_run(ctx: Context) -> int:
    # Asleep part way, this computer would leave the plug waiting on it.
    output.say(f"This computer is {awake.keep_awake()} while the run works.")
    try:
        return runner.run_steps(ctx)
    finally:
        # However the run ends - finished, failed, quit, Ctrl-C or stopped.
        runs.forget_run_secrets(ctx.dir)
        awake.let_sleep()


def forget_all_secrets() -> list[str]:
    """Delete every run's secret files, and the bundle's stored password."""
    removed = [item for directory in runs.run_directories() for item in runs.forget_run_secrets(directory)]
    if forget_stored_password((ROOT / bundle.DEFAULT_MANIFEST).resolve()):
        removed.append("the Wi-Fi password in the bundle's recovery image")
    return removed


def cmd_cleanup(ctx: Context) -> int:
    """Remove this run's temporary firewall rules, and every stored secret."""
    if firewall.recorded_rules(ctx):
        firewall.close_firewall_rules(ctx)
    else:
        output.say("This run added no firewall rules.")
    removed = forget_all_secrets()
    if removed:
        output.say("Removed stored secrets:")
        for item in removed:
            output.say(f"  {item}")
    else:
        output.say("No stored secrets were found.")
    if plugs.backups():
        output.say(f"Kept the Sonoff firmware backups in {runs.display_path(plugs.PLUGS)}/*/{plugs.BACKUP};")
        output.say("they hold the Wi-Fi password too. Delete them yourself if you will not want them.")
    return 0


def refused_for_another_run(command: str, ctx: Context) -> bool:
    """Another run working on this plug - or, to clean up, on any - is asked
    about first: before this one's log, report, firewall rule or safety locks
    are touched, which are the other run's too. True when refused (and said)."""
    mac = ctx.state.input("plug_mac")
    if command in ("run", "step") and mac:
        try:
            plugs.hold(mac, runs.run_name(ctx.dir))
        except plugs.PlugBusy as busy:
            output.tell(f"Stopped before anything was done: another run is converting this plug ({mac}) "
                 f"right now{f' - {busy}' if str(busy) else ''}. Let it finish, or stop it first, "
                 "then run this again.")
            return True
    if command == "cleanup":
        busy = plugs.busy()
        if busy:
            output.tell(f"Not cleaning up: a run is converting a plug right now ({'; '.join(busy)}), and "
                 "its firewall rule and secrets are in use. Let it finish, or stop it first, then "
                 "run this again.")
            return True
    return False


def main() -> int:
    try:
        return _main()
    finally:
        # However it ended - finished, failed, quit or Ctrl-C - leave a blank
        # line between the last message and the shell's prompt.
        print(flush=True)


def admin_password() -> str:
    """This computer's password for sudo (hostsys.password_asker): the one
    given in advance if sudo takes it, else asked for in this tool's own
    words, so every row on screen is its own.  An unattended run has nobody
    to ask, so it says what to give instead (OSError, as for a refused one)."""
    given = credentials.get("AUTOFLASH_SUDO_PASSWORD")
    if given is None and prompts.UNATTENDED:
        # Inside the emulator (tools/espressif/emulator) its password is known.
        given = os.environ.get("AUTOFLASH_EMULATOR_SUDO_PASSWORD")
    if given is not None:
        if hostsys.check_sudo_password(given) is None:
            return given
        output.tell("    The sudo password given in advance (AUTOFLASH_SUDO_PASSWORD) was not accepted.")
    if prompts.UNATTENDED:
        raise OSError("sudo needs this computer's password, and an unattended run cannot "
                      "ask for it: put it in .env as AUTOFLASH_SUDO_PASSWORD")
    return prompts.ask_admin_password()


def _main() -> int:
    hostsys.password_asker = admin_password
    # Keep piping into head/less quiet rather than raising BrokenPipeError.
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    # Being stopped, or the terminal being closed, must still run the cleanup
    # in the finally blocks: the stored secrets and the firewall rule.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    # Ctrl-C asks before it stops a run part way; the window closing stops it,
    # and is remembered (tools/autoflash/interrupt.py).
    interrupt.install()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"Autoflash {__version__}")
    parser.add_argument("command", choices=["run", "status", "step", "list", "cleanup"])
    parser.add_argument("step_id", nargs="?")
    parser.add_argument(
        "--run",
        "--plug",
        dest="run",
        help=argparse.SUPPRESS,  # power-user escape hatch; never needed normally
    )
    parser.add_argument("--new", action="store_true", help="start a fresh run, never resume")
    parser.add_argument("--manifest", type=Path, default=bundle.DEFAULT_MANIFEST)
    parser.add_argument("--plug-ip")
    parser.add_argument(
        "--listen-ip",
        help="workstation address the plug fetches from; derived from the plug's "
        "address when omitted",
    )
    parser.add_argument("--email")
    parser.add_argument("--web-user", help="Tasmota web username, for a Tasmota built with another than admin")
    parser.add_argument("--web-password",
                        help="the plug's Tasmota web password, if it has one (asked for otherwise; never stored)")
    parser.add_argument("--esphome-ota-password",
                        help="the plug's ESPHome OTA update password, if it has one (asked for otherwise; never stored)")
    parser.add_argument("--esphome-web-user",
                        help="the user name of the plug's ESPHome web login, if it has one (with --esphome-web-password)")
    parser.add_argument("--esphome-web-password",
                        help="the password of the plug's ESPHome web login (asked for otherwise; never stored)")
    parser.add_argument("--esphome-api-key",
                        help="the plug's ESPHome API encryption key, if it has one: for its Wi-Fi details, "
                        "and its updates where they are encrypted (asked for otherwise; never stored)")
    parser.add_argument("--wifi", action="store_true",
                        help="type the Wi-Fi details instead of reading them from the plug")
    parser.add_argument("--no-backup", action="store_true",
                        help="keep no backup of the Sonoff firmware (it holds the Wi-Fi password)")
    parser.add_argument("--firmware", choices=list(routes.FIRMWARES),
                        help="which firmware to install, instead of being asked at the start")
    # An ESPHome built by hand (firmware.ota.bin), instead of the published one.
    parser.add_argument("--esphome", metavar="PATH", help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--unattended", action="store_true",
                        help="nobody at the keyboard: go ahead past the point of no return "
                        "without typing PROCEED, take the eWeLink details from .env, and "
                        "stop at any question that has no safe answer")
    parser.add_argument("--env-file", type=Path,
                        help="answers given in advance (default: .env, if there is one); "
                        "see .env.example")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show every action as it happens (it is all in the run's log either way)")
    parser.add_argument("--simulate-backup-failure", action="store_true",
                        help=argparse.SUPPRESS)  # testing: the backup's first two reads fail
    parser.add_argument("--bridge", help=argparse.SUPPRESS)  # research: archive/docs/reference/automatic-wifi.md
    args = parser.parse_args()
    prompts.UNATTENDED = args.unattended
    installer.SIMULATED_BACKUP_FAILURES = 2 if args.simulate_backup_failure else 0

    try:
        given = credentials.load(args.env_file)
        adopt_older_runs()
        if args.command == "run" and not args.verbose and not args.unattended:
            prompts.wait_for_taller_window()  # before anything else is shown
        if args.command == "list":
            args.run = args.run or "-"
        elif args.command in ("status", "cleanup") and not args.run:
            # Report on the latest run; never start a new one just to look.
            args.run = runs.read_current() or (runs.run_name(runs.run_directories()[-1]) if runs.run_directories() else None)
            if not args.run:
                output.say("No conversions recorded yet.")
                return 0
        else:
            args.run = resolve_run_name(args.run, args.new, args.plug_ip,
                                        ask=args.command == "run" and not args.unattended)
        ctx = Context(args)
        routes.apply_target(runs.run_firmware(ctx.state), runs.run_source(ctx.state),
                            routes.family_of(ctx.state))  # a run settles it at its start
    except QuitRun:
        output.say("Stopped. Nothing was changed.")
        return 130
    except (Abort, ValueError, OSError) as exc:
        output.say(f"REFUSING: {exc}")
        return 2

    if refused_for_another_run(args.command, ctx):
        return 2

    if args.command == "list":
        directories = runs.run_directories()
        if not directories:
            output.say("No conversions recorded yet.")
            return 0
        for directory in directories:
            state = RunState.open(directory)
            steps = routes.steps_of(state)
            done = sum(1 for step, _, _ in steps if state.is_done(step))
            mark = "verified" if run_is_complete(directory) else "in progress"
            output.say(f"  {runs.run_name(directory):<55} {done}/{len(steps)} steps  {mark}"
                f"  {state.input('plug_ip') or ''}")
        for backup in plugs.backups():
            output.say(f"  Sonoff backup: {backup.describe()} in {runs.display_path(backup.directory)}")
        return 0

    if args.command == "status":
        return cmd_status(ctx)
    if args.command == "cleanup":
        return cmd_cleanup(ctx)
    if args.command == "step":
        if not args.step_id:
            output.say("step requires a step id")
            return 2
        output.start(ctx.dir / "run.log", verbose=args.verbose)
        code = None
        try:
            code = cmd_step(ctx, args.step_id)
            return code
        finally:
            if code == 130:
                firewall.say_quit(ctx)
            else:
                firewall.release_firewall(ctx)
            output.finish()
    output.start(ctx.dir / "run.log", verbose=args.verbose)
    log_run_choice()
    if args.unattended:
        output.say("Unattended: PROCEED is taken as typed, yes/no questions take their "
            "default, and any other question stops the run.")
    if given:
        output.say(f"Given in advance ({runs.display_path(args.env_file or credentials.ENV_FILE)}): "
            f"{', '.join(given)}")
    if credentials.renamed:
        output.say("Old names, still read for now - rename them: " + ", ".join(
            f"{old} is now {credentials.RENAMED[old]}" for old in credentials.renamed))
    code = None  # 130 only when the operator chose to quit, not for Ctrl-C
    ending = None  # "crashed", or "cancelled" by the operator: see run_result()
    try:
        code = cmd_run(ctx)
        return code
    except QuitRun:
        output.stage_end("stopped")
        code = 130
        return code
    except Exception as exc:  # noqa: BLE001 - recorded and explained, not a bare traceback
        message = runner.unexpected(exc)
        ctx.last_failure = message
        output.stage_end("stopped")
        output.tell(f"FAILED: {message}")
        runner.tell_how_to_carry_on()
        code, ending = 1, "crashed"
        return code
    except KeyboardInterrupt:
        closed = interrupt.window_closed()
        try:
            ctx.state.set_input("stopped_by", "window" if closed else "ctrl-c")
        except Exception:  # noqa: BLE001 - nothing must stop the run stopping
            pass
        if closed:
            output.log_only("The run's window was closed: stopped at once.")
        output.stage_end("stopped")
        output.tell("")
        output.tell("Stopped with Ctrl-C. Run again to pick up where you left off or to start again")
        output.tell("(Every step checks the plug first, so running again is safe.)")
        ending = "cancelled"
        return 130
    finally:
        if code == 130:
            firewall.say_quit(ctx)
        else:
            firewall.release_firewall(ctx)
        reported = report.report_ending(code, ending)
        if reported and not ctx.reported:
            try:
                report.invite_stopped_report(ctx, reported)
            except Exception as exc:  # noqa: BLE001 - never let the report hide how it ended
                output.log_only(f"The report could not be made: {type(exc).__name__}: {exc}")
        output.finish()


if __name__ == "__main__":
    raise SystemExit(main())
