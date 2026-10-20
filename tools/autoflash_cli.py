#!/usr/bin/env python3
"""Take a stock eWeLink Sonoff S60 to the canonical Tasmota layout.

This orchestrates the reviewed tools in tools/.  It reimplements no validator,
no Berry phase and no safety gate: every destructive action still runs the same
audited program, behind the same locks, judged by the same host-side evidence.
What it removes is console pasting, transcription error and lost place.

Every step records itself in captures/<plug>/autoflash-state.json, so an
interrupted run resumes where it stopped.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
from dataclasses import dataclass
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback
import urllib.error
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.autoflash import credentials, hostsys, output, prompts, setup_network  # noqa: E402
from tools.autoflash import __version__, esphome, imprint, plugs, tasmota_settings  # noqa: E402
from tools.autoflash import awake, detect, esphome_ota, ewelink_lan, interrupt, noise, plans, safety_locks  # noqa: E402
from tools.autoflash.device import Device, DeviceError, NeedsPassword, PASSWORD_HINT, UploadRefused  # noqa: E402
from tools.autoflash.hostsys import FirewallRule  # noqa: E402
from tools.autoflash.phases import (  # noqa: E402
    PhaseError,
    PhaseRunner,
    PlugNeverConnected,
    failure_reason,
)
from tools.autoflash.prompts import (  # noqa: E402
    QuitRun,
    ask_admin_password,
    ask_choice,
    ask_ipv4,
    ask_secret,
    ask_text,
    ask_yes_no,
    find_ssid,
    section,
    wait_for_key,
    wait_for_taller_window,
    warning_box,
    wait_or_ask,
)
from tools.autoflash.state import ATTEMPTED, DONE, FAILED, RunState  # noqa: E402
from tools.discover_ewelink import answered_ids, discover  # noqa: E402
from tools.dump_stock_flash import is_good, read_dump, summary  # noqa: E402
from tools.get_device_key import EwelinkError, get_devices, login  # noqa: E402
from tools import inspect_stock_nvs  # noqa: E402
from tools.prepare_imprinted_bundle import (  # noqa: E402
    BundleError,
    ask_credentials,
    ask_password,
    build_bundle,
    bundle_ssid,
    forget_stored_password,
    pin_wifi,
    recovery_image,
    startup_image,
    validate_bundle,
)
from tools.query_ota import ota_endpoints  # noqa: E402
from tools.serve_tasmota_ota import S60_DEVICE_MODEL  # noqa: E402
from tools.safeboot_migration import (  # noqa: E402
    APP0_OFFSET,
    PINNED_ARTIFACTS,
    SAFEBOOT_OFFSET,
    TABLE_OFFSET,
    TABLE_SIZE,
    load_manifest,
    sha256_bytes,
)

DEFAULT_MANIFEST = Path("captures/safeboot-recovery-migration/manifest.json")
# Ports the bridge may be served on, in order of preference.  The upgrade
# command names whichever is chosen, and the plug fetches from that port:
# 8088 matches the vendor OTA port, but the research probe was served on
# 8765.  The phase servers have their own ports, so the two never share one.
DELIVERY_PORTS = (8088, *range(8090, 8100))
# The S60's wiring: relay GPIO4, status LED GPIO5, power meter GPIO7, button
# GPIO9.  The UK S60TPG and the European S60TPF publish the same pins.
S60_GPIO = [1, 1, 1, 1, 224, 544, 1, 3104, 1, 32, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1]
# eWeLink's product model for a Wi-Fi S60: S60TP and the plug-type letter.
S60_PRODUCT = re.compile(r"S60TP[A-Z]")
# Typed once, before the first write that cannot be undone, for the whole conversion.
CONSENT_WORD = "proceed"
# After this step the plug no longer runs its Sonoff firmware.
POINT_OF_NO_RETURN = "C2"
# Below this the startup firmware may struggle to rejoin on its own after E3.
WEAK_SIGNAL_DBM = -75
# Every firmware a plug runs part way through a conversion - the installer,
# the mover, the startup firmware, a Tasmota not yet set up - goes by this and
# the last two bytes of the plug's Wi-Fi MAC (autoflash-recovery-CDEF): on the
# home network, and for the open network the installer and the mover raise
# when they cannot join that (the others, given the Wi-Fi, only retry it).
SETUP_SSID_PREFIX = "autoflash-recovery-"
# The networks of the same firmware before 4.4.4 (the installer) and 1.1.0
# (the mover), for a plug a run of an older checkout left on one; and the
# installer's before 4.4.3, with a fixed password.
LEGACY_SETUP_PREFIX = "Sonoff-Setup-"
LEGACY_BRIDGE_PREFIX = "S60-OTA-Bridge"
LEGACY_BRIDGE_PASSWORD = "s60-ota-bridge"
LEGACY_MOVER_PREFIX = "sonoff-s60-mover-"
# ESPHome for the S60's own network, which it keeps after the conversion
# (ESPHome/sonoff_s60.yaml); and its name before that (an ESPHome built from an
# earlier sonoff_s60.yaml).
ESPHOME_SETUP_PREFIX = "Sonoff-Setup-"
LEGACY_ESPHOME_PREFIX = "sonoff-s60-"
# 4.4.4: joins the home network the stock firmware was on, raising its own
# only if that fails, gives Tasmota its Wi-Fi at D1, and lets the plug's whole
# flash be read first, for the backup - and starts even once an upload into
# Sonoff's slot was cut off (from 4.4.3), where 4.4.2 would not, leaving a plug
# only a serial cable could recover; named, from 4.4.4, as the tool's.  Named,
# like every bridge from 4.4.1 on, by the version the stock updater is given;
# the file by the plug its wrapper is for.
BRIDGE_OTA = Path("artifacts/autoflash-bridge-sonoff-s60-4.4.4.ota")
# Ports the migration phase servers (serve_safeboot_migration.py) may listen
# on, in order of preference.  Nothing on the plug keeps the port: each phase
# starts its own server and sends the plug a loader URL that names it.
PHASE_PORTS = (8089, *range(8100, 8110))
SETTINGS_PATH = ROOT / "captures" / "settings.json"
# Where people share how their conversion went: the pinned "Test reports" thread.
REPORTS_URL = "https://github.com/adamsthws/Sonoff-OTA/discussions/1"
# Where people ask for help: a discussion or issue of their own, so the
# reports thread stays a record of runs.
DISCUSSIONS_URL = "https://github.com/adamsthws/Sonoff-OTA/discussions"
ISSUES_URL = "https://github.com/adamsthws/Sonoff-OTA/issues/new"
# Shown once a conversion has succeeded, after the report.
KOFI_URL = "https://ko-fi.com/adamsthws"
DONATE_URL = "https://www.paypal.me/adamsthws"
TASMOTA_AP_PREFIX = "tasmota-"


def _mac_tail(mac: str, add: int = 0) -> str:
    """The last three bytes of `mac` (plus `add`), as six hex digits."""
    value = int(hostsys.normalise_mac(mac).replace(":", ""), 16) + add
    return f"{value & 0xFFFFFF:06X}"


def setup_networks(ctx: "Context") -> tuple[str, ...]:
    """What a Tasmota's own setup network on this plug can be called, to look for.

    Tasmota raises one only without Wi-Fi details: started from its defaults,
    so named as it names itself, from the plug's MAC (tasmota-ABCDEF-1234).
    One the run named (autoflash-recovery-CDEF) has the details, and only
    retries them - and that name is the installer's network's, not looked for
    here: still up for a moment as the installer restarts into Tasmota, it
    was taken for Tasmota's (seen in the fault suite).  Tasmota has also been
    seen broadcasting ESP-IDF's default name instead: ESP_ and the MAC of its
    hotspot, one more than the plug's (ESP_ABCDF0).  Knowing the MAC, only
    this plug's are matched, never a neighbour's Tasmota; without it, any
    Tasmota's.
    """
    mac = ctx.state.input("plug_mac")
    if not mac:
        return (TASMOTA_AP_PREFIX,)
    return (f"{TASMOTA_AP_PREFIX}{_mac_tail(mac)}-", f"ESP_{_mac_tail(mac, 1)}")


def setup_network_hint(ctx: "Context") -> str:
    """The setup network's name as a person should look for it in a list."""
    mac = ctx.state.input("plug_mac")
    if not mac:
        return "tasmota-XXXXXX-NNNN"
    return f"tasmota-{_mac_tail(mac)}-NNNN, or ESP_{_mac_tail(mac, 1)}"


def note_setup_network(ctx: "Context", network: str) -> None:
    """Keep the plug's name, from its setup network - only when that is
    Tasmota's own name for it, which its router shows too, not ESP-IDF's."""
    if network.startswith(TASMOTA_AP_PREFIX):
        ctx.state.set_input("device_name", network)


def named(ctx: "Context", prefix: str) -> str:
    """`prefix` and the last two bytes of the plug's Wi-Fi MAC, as this
    project's firmware names itself (XXXX when the MAC is not known)."""
    mac = ctx.state.input("plug_mac")
    return f"{prefix}{_mac_tail(mac)[-4:]}" if mac else f"{prefix}XXXX"


def our_setup_network(ctx: "Context") -> str:
    """This plug's name, and its network's, while it runs firmware of this
    project part way through a conversion (autoflash-recovery-CDEF)."""
    return named(ctx, SETUP_SSID_PREFIX)


def our_setup_networks(ctx: "Context", *legacy: str) -> tuple[str, ...]:
    """To look for: this plug's network (any plug's, its MAC not known), or
    those of `legacy`, what the same firmware raised before."""
    mac = ctx.state.input("plug_mac")
    return (our_setup_network(ctx) if mac else SETUP_SSID_PREFIX, *legacy)


def legacy_setup_network(ctx: "Context") -> str:
    """What the installer and the mover raised before 4.4.4 and 1.1.0."""
    return named(ctx, LEGACY_SETUP_PREFIX) if ctx.state.input("plug_mac") else LEGACY_SETUP_PREFIX


def bridge_networks(ctx: "Context") -> tuple[str, ...]:
    """The installer's own network, by every name it has had."""
    return our_setup_networks(ctx, legacy_setup_network(ctx), LEGACY_BRIDGE_PREFIX)


def mover_networks(ctx: "Context") -> tuple[str, ...]:
    """The mover's own network, by every name it has had."""
    return our_setup_networks(ctx, legacy_setup_network(ctx), LEGACY_MOVER_PREFIX)


def esphome_networks(ctx: "Context") -> tuple[str, ...]:
    """ESPHome's own network, once installed: the S60's, and its name before."""
    mac = ctx.state.input("plug_mac")
    return (named(ctx, ESPHOME_SETUP_PREFIX) if mac else ESPHOME_SETUP_PREFIX, LEGACY_ESPHOME_PREFIX)


def mover_network_hint(ctx: "Context") -> str:
    """The mover's own network."""
    return our_setup_network(ctx)


def esphome_network_hint(ctx: "Context") -> str:
    """ESPHome's own network."""
    return named(ctx, ESPHOME_SETUP_PREFIX)


class Abort(RuntimeError):
    pass


# ---------------------------------------------------------------- utilities


def say(message: str = "", *, show: bool = False) -> None:
    """A message for the run's log; on screen too in verbose mode, or with show."""
    (output.show if show else output.detail)(message)


def tell(message: str = "") -> None:
    """A message the person running the tool needs to see, in every mode."""
    say(message, show=True)


def ask(prompt: str) -> str:
    return ask_text(prompt)


def confirm(prompt: str) -> bool:
    return ask_yes_no(prompt, default=False)


def require_word(word: str) -> bool:
    """True only if the operator types `word` - any capitalisation - and Enter."""
    tell("")
    # Read directly: an empty answer must stop here, not be asked again.
    question = f"Type {word.upper()} to go ahead - anything else stops, changing nothing: "
    if prompts.UNATTENDED:
        # Given in advance by starting the run with --unattended.
        prompts.answered_unattended(question.rstrip(": "), word.upper())
        return True
    answer = input(output.question(question))
    output.answered(answer)
    return answer.strip().lower() == word.lower()


def run_tool(
    args: list[str],
    cwd: Path = ROOT,
    interactive: bool = False,
    env: dict[str, str] | None = None,
) -> int:
    """Run one of the reviewed tools.  Its output goes to the run's log, and to
    the screen as well in verbose mode; it reads the keyboard as before, but
    none of the tools run here asks anything (the password reaches them
    through the environment), so `interactive` no longer changes anything.
    """
    say(f"$ {' '.join(str(a) for a in args)}")
    # Its own process group: a first Ctrl-C asks, and must not stop it meanwhile.
    process = subprocess.Popen(args, cwd=str(cwd), env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               **interrupt.child_group())
    return output.stream(process)


def run_name(directory: Path) -> str:
    """How a run is named: its place under captures/, such as
    plugs/aa-bb-cc-dd-ee-ff/runs/20261013-031500 (tools/autoflash/plugs.py)."""
    try:
        return directory.relative_to(plugs.CAPTURES).as_posix()
    except ValueError:
        return directory.name


def run_directories() -> list[Path]:
    captures = plugs.CAPTURES
    if not captures.is_dir():
        return []
    # Runs from before plugs/ were kept straight under captures/.
    candidates = [*captures.glob(f"plugs/*/{plugs.RUNS}/*"), *captures.iterdir()]
    return sorted(
        (child for child in candidates if (child / "autoflash-state.json").is_file()),
        key=lambda child: (child / "autoflash-state.json").stat().st_mtime,
    )


def adopt_older_runs() -> None:
    """Move runs kept straight under captures/ to their plug's directory, when
    they know its MAC.  Those that do not stay where they are, still listed."""
    pointer = read_current()
    for directory in [d for d in run_directories() if d.parent == plugs.CAPTURES]:
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
            remember_current(run_name(target))


def run_is_complete(directory: Path) -> bool:
    try:
        state = RunState.open(directory)
    except (OSError, ValueError):
        return False
    steps = steps_of(state)
    if not steps:
        return state.input("firmware") is not None  # it already ran what was asked
    return state.is_done(steps[-1][0])


CURRENT_POINTER = ROOT / "captures" / ".autoflash-current"


def remember_current(name: str) -> None:
    try:
        CURRENT_POINTER.parent.mkdir(parents=True, exist_ok=True)
        CURRENT_POINTER.write_text(name + "\n", encoding="utf-8")
    except OSError:
        pass  # the pointer is a convenience, never a requirement


def read_current() -> str | None:
    try:
        name = CURRENT_POINTER.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return name or None


def describe_run(directory: Path) -> str:
    """Where a run stopped, briefly: the plug, the step, when, and how far in."""
    import datetime as dt

    state = RunState.open(directory)
    where = state.input("plug_ip") or "a plug not yet found"
    mac = plugs.mac_of_run(directory) or state.input("plug_mac")
    if mac and state.input("plug_ip"):
        where += f", MAC {mac}"
    pending = next((step for step, _, _ in steps_of(state)
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
    if state.is_done(POINT_OF_NO_RETURN):
        details.append("past the point of no return")
    if not state.input("plug_ip"):
        return f"a run that had not found its plug yet ({', '.join(details)})"
    return f"the plug at {where} ({', '.join(details)})"


def new_run_name(plug_ip: str | None) -> str:
    """A name the operator never has to type: when it started, filed under
    plugs/unidentified/ until A1 finds the plug and moves it under its MAC."""
    return run_name(plugs.new_run_dir(None))


def start_new_run(plug_ip: str | None) -> str:
    name = new_run_name(plug_ip)
    remember_current(name)
    return name


# The answer to the unfinished-run menu.  The menu is answered before the run's
# log exists and leaves the screen once answered, so it is held here and
# written into the log when it opens - otherwise a plug left part converted by
# "start a new plug" leaves no trace of how.
RUN_CHOICE: list[str] = []


def log_run_choice() -> None:
    for line in RUN_CHOICE:
        say(line)
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
        remember_current(requested)
        return requested
    if force_new:
        return start_new_run(plug_ip)

    unfinished = [d for d in run_directories() if not run_is_complete(d)]
    if not unfinished:
        return start_new_run(plug_ip)

    pointer = read_current()
    # Most recently touched first, so the likely answer is option 1.
    unfinished.sort(key=lambda d: (run_name(d) != pointer, -d.stat().st_mtime))
    if not ask:
        chosen = unfinished[0]
        say(f"Continuing {describe_run(chosen)}.")
        RUN_CHOICE.append(f"Continuing {describe_run(chosen)}, the most recent unfinished run.")
        remember_current(run_name(chosen))
        return run_name(chosen)

    while unfinished:
        # The menu leaves the screen once answered; after a removal it is
        # shown again, not stacked under the old one.
        output.transient_begin(before_start=True)
        any_past = any(RunState.open(d).is_done(POINT_OF_NO_RETURN) for d in unfinished)
        keep = ("the unfinished one stays" if len(unfinished) == 1
                else "the unfinished ones stay")
        options = [f"Continue {describe_run(d)}" for d in unfinished] + [
            f"Start a new plug - {keep} on record, to finish later",
            "Remove an unfinished run",
        ]
        chosen = ask_choice(
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
            remember_current(run_name(unfinished[chosen]))
            return run_name(unfinished[chosen])
        if chosen == len(unfinished):
            output.transient_end()
            RUN_CHOICE.append("Chosen at the start: a new plug. Left unfinished, on record:")
            RUN_CHOICE.extend(f"    {describe_run(d)}" for d in unfinished)
            return start_new_run(plug_ip)
        target = unfinished[0] if len(unfinished) == 1 else unfinished[ask_choice(
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
    if state.is_done(POINT_OF_NO_RETURN):
        section("This plug is part-converted", [
            "It no longer runs its Sonoff firmware, so it will not show up as a",
            "new plug either. Once its record is removed, this tool cannot finish",
            "it - finishing would mean doing the remaining steps by hand.",
        ])
    if not ask_yes_no(f"Remove the record of {description}", default=False):
        return False
    rules = [FirewallRule.from_json(item) for item in state.input("firewall_rules") or []]
    if rules:
        say("It left a temporary firewall rule letting that plug connect to this")
        say("computer. Removing it needs administrator rights, so you may be asked")
        say("for this computer's password.")
        for line in emulator_password_hint():
            say(line)
        left = [rule for rule in rules if not hostsys.close_firewall(rule)]
        if left:
            output.transient_end()  # this has to stay on screen
        for rule in left:
            for line in hostsys.undo_commands(rule):
                tell("Remove it yourself: " + line)
    shutil.rmtree(directory)
    if read_current() == run_name(directory):
        CURRENT_POINTER.unlink(missing_ok=True)
    say(f"Removed the record of {description}.")
    return True


class Context:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.plug = args.run
        self.dir = plugs.CAPTURES / self.plug
        self.evidence = self.dir / "live"
        self.state = RunState.open(self.dir)
        self.manifest_path = (ROOT / args.manifest).resolve()
        self.dry_run = args.dry_run
        # Research only: a different bridge, after which the run stops (see
        # archive/docs/reference/automatic-wifi.md).  The plug goes back to stock on a power cycle.
        self.bridge = Path(getattr(args, "bridge", None) or BRIDGE_OTA)
        self.research_bridge = self.bridge != BRIDGE_OTA
        # Optional path to an ESPHome application firmware.ota.bin. When set,
        # the run stops after interim Tasmota on the *stock* partition table
        # (no Safeboot / E3 repartition) and uploads this image via Tasmota OTA.
        esphome = getattr(args, "esphome", None)
        self.esphome_firmware = Path(esphome).expanduser().resolve() if esphome else None
        # --firmware: which to install, instead of being asked (--unattended needs it).
        self.firmware_given = "esphome" if esphome else getattr(args, "firmware", None)
        if args.plug_ip:
            self.state.set_input("plug_ip", args.plug_ip)
        if args.email:
            self.remember_email(args.email)
        if args.web_user:
            self.state.set_input("web_user", args.web_user)
        # The plug's Tasmota web password, if it has one: given, or asked for
        # when it is found wanting one (ask_web_password). Held in memory only.
        self.web_password = args.web_password or credentials.get("AUTOFLASH_TASMOTA_PASSWORD")
        output.private(self.web_password, "tasmota-password")
        # The same for ESPHome's update password (ask_esphome_password); its
        # web login (ask_esphome_login), user name and password; and its API
        # encryption key, for its encrypted API and updates (ask_esphome_key).
        self.esphome_password = args.esphome_ota_password or credentials.get("AUTOFLASH_ESPHOME_OTA_PASSWORD")
        output.private(self.esphome_password, "esphome-password")
        user = args.esphome_web_user or credentials.get("AUTOFLASH_ESPHOME_WEB_USER")
        password = args.esphome_web_password or credentials.get("AUTOFLASH_ESPHOME_WEB_PASSWORD")
        output.private(password, "esphome-web-password")
        self.esphome_login = (user, password) if user and password else None
        key = args.esphome_api_key or credentials.get("AUTOFLASH_ESPHOME_API_KEY")
        output.private(key, "esphome-api-key")
        try:
            self.esphome_key = noise.decode_key(key) if key else None
        except ValueError as exc:
            raise Abort(f"the ESPHome API encryption key given (--esphome-api-key, or "
                        f"AUTOFLASH_ESPHOME_API_KEY) is not one ({exc}): one is 32 bytes in base64, "
                        "as after key: under api: encryption: in the YAML config") from None
        # Its API found encrypted, its key not given: offered before the Wi-Fi is asked for.
        self.esphome_api_locked = False
        self._ewelink_password: str | None = None
        # From .env or the environment: tried once, then asked for if eWeLink refuses it.
        self._given_ewelink_password = credentials.get("EWELINK_PASSWORD")
        self._given_email = credentials.get("EWELINK_EMAIL")
        self._finder: PlugFinder | None = None
        # The account's record of the plug A1 chose, for its plug.json.
        self.account_plug: AccountPlug | None = None
        # Held only in memory: the Wi-Fi details, and the recovery image built
        # from them.  Neither is ever written to disk.
        self._wifi: tuple[str, str] | None = None
        self._recovery: bytes | None = None
        # For the report: the phase runner a step used (its plug-busy time), when
        # this session started, and whether the report has been offered.
        self.last_runner: PhaseRunner | None = None
        self.run_started = time.monotonic()
        self.run_resumed = False
        self.reported = False
        self.last_failure = ""  # what the last stage to fail said, for the report
        self.last_failed_step = ""  # and which it was: a failure may send earlier steps back
        try:
            output.private(bundle_ssid(self.manifest_path), "wifi-name")
        except (BundleError, OSError, ValueError):
            pass  # no bundle yet: its network is kept out of the log once known

    def remember_wifi(self, ssid: str, password: str, image: bytes) -> None:
        output.private(ssid, "wifi-name")
        output.private(password, "wifi-password")
        self._wifi, self._recovery = (ssid, password), image

    def wifi(self, why: list[str] | None = None) -> tuple[str, str]:
        """The Wi-Fi name and password: read from the plug where it can be.

        From D1 the plug runs Tasmota connected to the home network, so the
        details are read from there rather than typed - and they are known to
        work, which typed ones are not until F2 tries them.  ESPHome gives
        them too, from 1.1.0 (its s60_wifi action); from an ESPHome or mover
        that does not, they come from the plug's Sonoff backup, when this
        computer has one and the plug is on that network still.  Asking is the
        fallback, with `why` saying what it is for; `--wifi` forces it.
        """
        forced = getattr(self.args, "wifi", False)
        # ESPHome, or the mover, says nothing of its Wi-Fi password: only a
        # Tasmota can be read.
        readable = plug_runs_tasmota(self.state)
        if self._wifi is None and not forced and readable:
            found = read_wifi_from_plug(self)
            if found is not None:
                self.adopt_wifi(*found)
                # A run stopped at the question earlier must not report it as typed.
                self.state.set_input("wifi_typed", False)
        if self._wifi is None and not forced and not readable:
            found = wifi_from_esphome(self) or wifi_from_backup(self)
            if found is not None:
                self.adopt_wifi(*found)
                self.state.set_input("wifi_typed", False)
        if self._wifi is None and not forced and self.esphome_api_locked and not prompts.UNATTENDED:
            # ESPHome gives them, its API unlocked: its key typed instead.
            found = ask_esphome_key(self, self.state.input("plug_ip"), wifi=True) and wifi_from_esphome(self)
            if found:
                self.adopt_wifi(*found)
                self.state.set_input("wifi_typed", False)
        given = credentials.get("AUTOFLASH_WIFI_SSID")
        if self._wifi is None and not forced and given:
            # Given in advance (.env), for a run nobody sits through; no
            # password given means an open network.
            self.adopt_wifi(given, credentials.get("AUTOFLASH_WIFI_PASSWORD") or "")
            self.state.set_input("wifi_typed", False)
        if self._wifi is not None:
            return self._wifi
        output.transient_begin()  # until they are accepted, however many tries
        section("Your Wi-Fi details are needed", [
            *(["You chose to type them (--wifi)."] if forced else why or [
                "They could not be read off the plug (the reason is above).",
                "The startup firmware has to rejoin your Wi-Fi by itself after the",
                "next stage, so it needs them written in.",
            ]),
            "",
            "Use the 2.4 GHz network the plug should stay on. The password is held",
            "in memory for this run only, never written to disk, and you are not",
            "asked again later in the run.",
        ])
        while self._wifi is None:
            ssid = bundle_ssid(self.manifest_path)
            if ssid is None:
                self.adopt_wifi(*ask_credentials())
                continue
            say(f"The bundle is set up for '{ssid}'.")
            password = ask_secret(f"Wi-Fi password for '{ssid}': ")
            try:
                self.remember_wifi(ssid, password, recovery_image(self.manifest_path, ssid, password))
            except BundleError as exc:
                tell(f"    {exc}.")
                choice = ask_choice("What now", [
                    "Type the password again",
                    "Use a different network, or a changed password - enter both",
                ])
                if choice == 1:
                    self.adopt_wifi(*ask_credentials())
        output.transient_end()
        self.state.set_input("wifi_typed", True)
        return self._wifi

    def adopt_wifi(self, ssid: str, password: str) -> None:
        """Use these details, setting the bundle up for them if it is not already."""
        try:
            image = recovery_image(self.manifest_path, ssid, password)
        except BundleError:
            say(f"    Writing the Wi-Fi for '{ssid}' into the startup firmware (in memory)...")
            try:
                image = pin_wifi(self.manifest_path, ssid, password)
            except BundleError as first:
                # A bundle this checkout can no longer use: build it again.
                say(f"    {first}; preparing the firmware bundle again...")
                try:
                    _, image = build_bundle(ssid, password, self.manifest_path.parent)
                except BundleError as exc:
                    raise Abort(f"the bundle could not be prepared: {exc}") from exc
        self.remember_wifi(ssid, password, image)

    def recovery_bytes(self) -> bytes:
        self.wifi()
        assert self._recovery is not None
        return self._recovery

    def ewelink_env(self) -> dict[str, str]:
        """Ask for the account password once and reuse it for this run only.

        It is held in memory, never written to the run state, and never put on a
        command line.  It reaches the reviewed tools through EWELINK_PASSWORD,
        which serve_tasmota_ota.py already supported.
        """
        # A password given in advance is tried once, and only for the account
        # it was given with when one was named too.
        given, self._given_ewelink_password = self._given_ewelink_password, None
        if self._ewelink_password is None and given and self._given_email in (None, self.email()):
            self._ewelink_password = given
            say("The eWeLink password is the one given in advance (.env or EWELINK_PASSWORD).")
        if self._ewelink_password is None:
            email = self.email()
            output.transient_begin()
            self._ewelink_password = ask_secret(f"eWeLink password for {email}: ")
            output.transient_end()
        return {**os.environ, "EWELINK_PASSWORD": self._ewelink_password}

    def account_devices(self) -> list[dict[str, Any]] | None:
        """The devices in the owner's eWeLink account, or None when eWeLink
        cannot be reached.  A wrong password is asked again; the list, which
        holds each device's LAN key, stays in memory and is never logged."""
        while True:
            self.ewelink_env()
            try:
                region, token = login(self.email(), self._ewelink_password or "", "+44", "eu")
                devices = get_devices(region, token)
                keep_account_private(devices)
                return devices
            except EwelinkError as exc:
                if "login failed" not in str(exc):
                    tell(f"    Could not reach eWeLink ({exc}), so every plug that answers is listed.")
                    return None
                self._ewelink_password = None
                output.transient_begin()
                tell(f"    {exc}.")
                choice = ask_choice("What now", [
                    "Type the password again",
                    "Use a different eWeLink account",
                ])
                output.transient_end()
                if choice == 1:
                    self.forget_account()

    def forget_account(self) -> None:
        """Ask again which eWeLink account, and its password."""
        self.state.set_input("email", None)
        self._ewelink_password = None
        self._given_email = self._given_ewelink_password = None

    def email(self) -> str:
        """The eWeLink account email, chosen once per run.

        The last account used on this computer is offered first; a plug in
        another account needs that account's email instead.
        """
        email = self.state.input("email") or self._given_email
        if not email:
            output.transient_begin()
            last = load_settings().get("ewelink_email")
            if last and ask_choice("Which eWeLink account is this plug in", [
                f"The one used last time: {last}",
                "A different account - enter its email",
            ]) == 0:
                email = last
            else:
                email = ask_text(
                    "eWeLink account email",
                    validate=lambda answer: (
                        ("@" in answer, "That does not look like an email address.")
                    ),
                )
            output.transient_end()
        self.remember_email(email)
        return email

    def remember_email(self, email: str) -> None:
        self.state.set_input("email", email)
        settings = load_settings()
        if settings.get("ewelink_email") != email:
            settings["ewelink_email"] = email
            save_settings(settings)

    def file_under_plug(self, mac: str) -> None:
        """Move this run under its plug's MAC (plugs.py), once the plug is
        known and before anything is written to it."""
        if plugs.mac_of_run(self.dir) == plugs.folder_name(mac).replace("-", ":"):
            return
        target = plugs.runs_dir(mac) / self.dir.name
        suffix = 2
        while target.exists():
            target, suffix = target.with_name(f"{self.dir.name}-{suffix}"), suffix + 1
        source = self.dir

        def move() -> Path:
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
            return target / "run.log"

        output.move_log(move)
        self.dir, self.evidence = target, target / "live"
        self.state.path = target / "autoflash-state.json"
        self.plug = run_name(target)
        remember_current(self.plug)
        say(f"This run's record is now in {display_path(target)}.")

    def listen_ip(self) -> str:
        """This computer's address as the plug sees it, worked out afresh each time.

        Remembering it went wrong when a cable was unplugged mid-run: the tool
        kept offering an address the computer no longer had.  --listen-ip still
        overrides it.
        """
        if self.args.listen_ip:
            return self.args.listen_ip
        plug_ip = self.state.input("plug_ip")
        if not plug_ip:
            raise Abort("the plug address is not known yet, so this computer's "
                        "address cannot be worked out; pass --listen-ip")
        try:
            derived = hostsys.local_ip_towards(plug_ip)
        except OSError as exc:
            raise Abort(
                f"could not work out how this computer reaches {plug_ip} ({exc}); "
                "check you are on the same network as the plug, or pass --listen-ip"
            ) from exc
        address = ipaddress.ip_address(derived)
        if not (address.is_private and address.version == 4):
            raise Abort(f"this computer's address towards the plug, {derived}, is not "
                        "a home-network address")
        remembered = self.state.input("listen_ip")
        if remembered != derived:
            if remembered:
                say(f"This computer's address towards the plug is now {derived} "
                    f"(it was {remembered}).")
            else:
                say(f"This computer's address towards the plug: {derived}")
            self.state.set_input("listen_ip", derived)
        return derived

    def set_plug_ip(self, address: str) -> None:
        if address != self.state.input("plug_ip"):
            say(f"The plug is now at {address}.")
            self.state.set_input("plug_ip", address)

    @property
    def finder(self) -> "PlugFinder":
        if self._finder is None:
            self._finder = PlugFinder(self)
        return self._finder

    @property
    def device(self) -> Device:
        host = self.state.input("plug_ip")
        if not host:
            raise Abort("plug_ip is not set; pass --plug-ip")
        return Device(
            host,
            user=self.state.input("web_user"),
            password=self.web_password,
        )

    def phase_runner(self) -> PhaseRunner:
        port = choose_port(self, "phase_port", PHASE_PORTS, "the migration steps")
        ensure_firewall(self)
        self.last_runner = PhaseRunner(
            repo_root=ROOT,
            manifest=self.manifest_path,
            evidence_dir=self.evidence,
            listen_ip=self.listen_ip(),
            device=self.device,
            log=phase_log,
            listen_port=port,
            # Piped, never a file; and only hashes of captures that hold secrets.
            recovery=self.recovery_bytes(),
        )
        return self.last_runner


def load_settings() -> dict[str, Any]:
    """Preferences kept between runs on this computer - never secrets."""
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict[str, Any]) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")


# ----------------------------------------------------------------- firewall


def s60_template(product_model: str | None) -> str:
    """The Tasmota template, named after the plug's own model where eWeLink gave it.

    S60TPG or S60TPF when the model is one of those; otherwise, or for a run
    begun before the model was recorded, the series name.  Nothing but a
    model matching S60_PRODUCT ever reaches the Tasmota command.
    """
    product = (product_model or "").strip().upper()
    name = f"Sonoff {product}" if S60_PRODUCT.fullmatch(product) else "Sonoff S60"
    return json.dumps({"NAME": name, "GPIO": S60_GPIO, "FLAG": 0, "BASE": 1}, separators=(",", ":"))


# A phase server's line that says why it stopped, rather than how it is getting on.
# The commit's own checks, made before its first write, that the plug is still
# as E1 and E2 left it (repartition/berry/commit.be.tmpl).
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
PHASE_PROBLEM_RE = re.compile(
    r"REFUS|TIMEOUT|ERROR|Traceback|Stopped|FAIL|full output is in|PLUG SAYS|plug's console|"
    r"not answering at all|switch it off|and on \(at this stage",
    re.IGNORECASE,
)


def phase_failed(phase: str, code: int, captured: str) -> "Abort":
    """The Abort for a phase that did not pass, with its reason when known."""
    reason = failure_reason(captured)
    return Abort(f"{phase} did not pass: {reason}" if reason else f"{phase} did not pass (exit {code})")


def phase_log(text: str) -> None:
    say(text, show=bool(PHASE_PROBLEM_RE.search(text)))


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
            "using them and run this again - nothing was sent to the plug"
        )
    if port != candidates[0]:
        say(f"Port {candidates[0]} is in use on this computer; serving {serving} on {port} instead.")
    ctx.state.set_input(key, str(port))
    return port


def firewall_ports(ctx: Context) -> list[int]:
    """The ports the plug connects to this computer on: bridge delivery and phases."""
    return sorted({int(ctx.state.input("delivery_port") or DELIVERY_PORTS[0]),
                   int(ctx.state.input("phase_port") or PHASE_PORTS[0])})


def emulator_password_hint() -> list[str]:
    """Inside the emulator (tools/emulator) only: the password sudo asks for
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
        say(line)
    if hostsys.local_rules_ignored(plug_ip):
        section("This computer's firewall is managed by an organisation", [
            "It is set (by Group Policy) to ignore rules added on the computer itself,",
            "so the plug cannot be let in from here. Ask whoever manages it, or use",
            "another computer. Carrying on - if the plug is blocked, the next step",
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
    section("Let the plug through this computer's firewall", [
        f"This computer's firewall ({kind}) would block the plug from fetching its",
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
        section("The firewall rule could not be added", [
            "Add it yourself, in another window, with:",
            f"  {hostsys.display(add)}",
            "and remove it after the conversion. Carrying on - if the plug is still",
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
        say("The rule was already there, so it is left in place afterwards."
            if rule.detail.get("preexisting") else
            "The rule was there from an earlier run that stopped as it was added; "
            "it is removed afterwards." if rule.detail.get("left_by_earlier_run") else
            "Firewall rule added.")
        if rule.detail.get("paused"):
            say("Python had been blocked from networks here, most likely by a \"Cancel\" in")
            say("Windows Security once. That block is paused while this runs, and put back")
            say("as it was afterwards.")


def firewall_hint(ctx: Context) -> list[str]:
    """What to say when the plug never reached this computer."""
    plug_ip = ctx.state.input("plug_ip") or "PLUG_IP"
    lines = [
        "The plug never connected to this computer. That is almost always this",
        "computer's firewall, or this computer having changed address.",
    ]
    kind = hostsys.active_firewall()
    ports = firewall_ports(ctx)
    if kind:
        program = hostsys.python_program() if kind in ("windows", "macos") else None
        add, _ = hostsys.firewall_commands(kind, plug_ip, ports, program)
        lines += ["", f"A {kind} firewall is on. This allows the plug in:", f"  {hostsys.display(add)}"]
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
    tell("")
    tell("Removing the temporary firewall rule added at the start, which let the")
    tell("plug connect to this computer. Changing the firewall needs admin/sudo")
    tell(f"rights, so {permission}.")
    for line in emulator_password_hint():
        tell(line)
    left: list[FirewallRule] = []
    for rule in rules:
        if not hostsys.close_firewall(rule):
            left.append(rule)
    ctx.state.set_input("firewall_rules", [r.to_json() for r in left])
    output.transient_end()
    if left:
        section("Remove the temporary firewall rules yourself", [
            line for r in left for line in hostsys.undo_commands(r)
        ])
    else:
        say("The temporary firewall rules have been removed.")


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
        tell("")
        tell("The temporary firewall rule was not removed"
             + (" (stopped with Ctrl-C)." if isinstance(exc, KeyboardInterrupt) else f": {exc}."))
        tell("Remove it yourself with:")
        for rule in left:
            for line in hostsys.undo_commands(rule):
                tell("    " + line)
        return False


def say_quit(ctx: Context) -> None:
    """The last word after the operator chose to quit, once everything is tidied:
    the run's secrets are gone and the firewall rule is removed."""
    if release_firewall(ctx):
        tell("")
        tell("Cleaned up. Nothing was left half done. Exiting.")


# ------------------------------------------------------------ finding the plug


class PlugFinder:
    """Where the plug is now: its known address, or wherever its hardware address is.

    The hardware (MAC) address of the plug's Wi-Fi is the same under every
    firmware, so once recorded it finds the plug after a restart onto a new
    address, so nobody has to reserve one in their router.
    """

    SWEEP_EVERY = 30.0

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.last_sweep = 0.0

    def poll(self) -> str | None:
        current = self.ctx.state.input("plug_ip")
        mac = self.ctx.state.input("plug_mac")
        if current and hostsys.ping_ok(current):
            seen = hostsys.neighbour_table().get(current)
            if not mac or seen in (None, mac):
                return current
        if mac and current and time.monotonic() - self.last_sweep > self.SWEEP_EVERY:
            self.last_sweep = time.monotonic()
            found = hostsys.find_by_mac(mac, current)
            if found:
                self.ctx.set_plug_ip(found)
                return found
        return None


def locate_plug(ctx: Context, *, timeout: float = 300.0, setup: bool = False) -> str:
    """Wait for the plug on the home network; ask only if it cannot be found.

    With `setup`, the plug may instead be waiting on its own setup network,
    which this computer could not see (not every system can scan): offered as
    a way on, it takes the person through joining it by hand.
    """
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        found = ctx.finder.poll()
        if found:
            output.progress_clear()
            say(f"    The plug is on your network at {found}.")
            return found
        output.progress(f"looking for the plug on your network... {time.monotonic() - started:.0f}s")
        time.sleep(5)
    output.progress_clear()
    other = None
    if setup:
        other = (f"It is showing its own Wi-Fi network ({setup_network_hint(ctx)})",
                 lambda: configure_plug_wifi(ctx, None))
    found = wait_or_ask(ctx.state.input("plug_ip"), what="the plug", timeout=60.0, other=other)
    ctx.set_plug_ip(found)
    return found


# --------------------------------------------------- the plug's own networks


# Waiting on something only a person can do - join a network, say - has no
# time limit: they may have walked away, and the run should still be there
# when they come back.  Ctrl-C stops it.  Nobody comes back to an unattended
# run, so that one gives up.
UNATTENDED_ACTION_LIMIT = 900.0


def user_action_limit() -> float:
    return UNATTENDED_ACTION_LIMIT if prompts.UNATTENDED else float("inf")


def wait_until_joined(network: str, marker: str | None = None) -> None:
    """Block until this computer has joined the plug's network.

    With `marker`, the page must mention it (the bridge).  Without, only the web
    port is touched: Tasmota scans for networks on every page request in setup
    mode, which drops this computer off its network.
    """
    def tick(elapsed: float) -> None:
        output.progress(f"waiting for this computer to join {network}... {elapsed:.0f}s")

    limit = user_action_limit()
    with interrupt.asking():  # waiting on the person: Ctrl-C stops at once
        joined = (setup_network.wait_for_page(marker, timeout=limit, tick=tick) if marker
                  else setup_network.wait_for_port(timeout=limit, tick=tick))
    if not joined:
        output.progress_clear()
        raise Abort(f"this computer did not join {network} within {limit / 60:.0f} minutes")
    output.progress_clear()
    say(f"    Connected to {network}.")


def configure_plug_wifi(ctx: Context, network: str | None) -> str:
    """Give Tasmota, waiting on its setup network, the bundle's Wi-Fi details.

    The operator joins the setup network; the details are sent from here.  Tasmota
    saves them only after it has connected with them, so a failure leaves the
    plug waiting to be set up, never lost.
    """
    network = network or setup_network_hint(ctx)
    credentials = ctx.wifi(why=[
        "Tasmota could not be given them automatically, so it is waiting on its",
        "own setup network for them. Type them here and this sends them to the",
        "plug for you once you have joined that network.",
    ])
    section("Join the plug's setup network", [
        f"In {hostsys.wifi_menu_hint()}, join:",
        f"    {network}      (open - no password)",
        "",
        "If a setup page opens by itself, close it: while that page is open the",
        "plug keeps scanning for networks, which can drop this computer off its",
        "network.",
        "",
        "The network can vanish for a few seconds every three minutes, as the",
        "plug restarts to try your Wi-Fi again: if this computer is dropped off",
        "it, join it again.",
    ])
    wait_until_joined(network)
    outcome = "manual"
    if credentials is not None:
        ssid, password = credentials
        for attempt in range(1, SETUP_SEND_ATTEMPTS + 1):
            tell(f"    Sending the Wi-Fi details for '{ssid}'. The plug tests them first -")
            tell("    stay on its network for about 20 seconds...")
            try:
                outcome, address = setup_network.send_wifi(ssid, password)
            except setup_network.SetupError as exc:
                outcome, address = "unknown", None
                tell(f"    {exc}")
            if outcome == "saved" or attempt == SETUP_SEND_ATTEMPTS:
                break
            if outcome == "rejected":
                if prompts.UNATTENDED:
                    break  # nobody to type others: on to the plug's page, below
                tell(f"    The plug could not connect to '{ssid}' with those details. Type them")
                tell("    again - the network's name exactly as it appears, and its password:")
                ssid, password = ask_credentials()
            else:
                tell(f"    No word from the plug; sending them again (attempt {attempt + 1} "
                     f"of {SETUP_SEND_ATTEMPTS}).")
                time.sleep(5)
            wait_until_joined(network)  # dropped off it meanwhile, joined again
        if outcome == "saved":
            tell(f"    The plug connected to '{ssid}' and is restarting onto it"
                + (f" at {address}." if address else "."))
            if address:
                ctx.set_plug_ip(address)
        elif outcome == "rejected":
            tell(f"    The plug could not connect to '{ssid}' with the bundle's details.")
        else:
            tell("    The plug did not say whether it connected. If it does not turn up")
            tell("    on your Wi-Fi, it is still waiting on its setup network: run this")
            tell("    again to retry.")
    if outcome in ("rejected", "manual"):
        section("Set the Wi-Fi up on the plug's page instead", [
            "Stay on the plug's network for this.",
            f"Browse to http://{setup_network.SETUP_HOST}, choose your home Wi-Fi,",
            "type its password and save. When it says the connection worked:",
        ])
    section("Back to your usual Wi-Fi", [
        "Your computer should rejoin your usual Wi-Fi by itself; if it doesn't,",
        "join it now.",
    ])
    return locate_plug(ctx, timeout=600.0)



# -------------------------------------------------------------------- steps


def upload_or_prompt(ctx: "Context", path: Path, what: str) -> str:
    """Upload over HTTP, falling back to the operator's browser.

    The multipart path has not been exercised on this hardware.  A failure here
    must not strand a run: the same file uploaded through the web UI is
    identical, so offer that and carry on.
    """
    device = ctx.device
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        try:
            started = time.monotonic()
            device.upload_firmware(path)
            seconds = round(time.monotonic() - started, 1)
            say(f"{what} uploaded in {seconds}s")
            return f"http:{seconds}s"
        except UploadRefused as exc:
            # Refused whole, nothing written: most likely damaged on the way.
            if attempt == UPLOAD_ATTEMPTS:
                return upload_or_prompt_by_hand(ctx, path, what, exc)
            tell(f"    The plug refused the upload ({exc.reason}); sending it again "
                 f"(attempt {attempt + 1} of {UPLOAD_ATTEMPTS}).")
            time.sleep(5)
        except Exception as exc:  # noqa: BLE001 - any failure falls back to the operator
            return upload_or_prompt_by_hand(ctx, path, what, exc)
    raise AssertionError("unreachable")


def upload_or_prompt_by_hand(ctx: "Context", path: Path, what: str, exc: Exception) -> str:
    """The upload left to the operator's browser, after it failed with `exc`."""
    device = ctx.device
    output.transient_begin()
    tell(f"    Automatic upload failed: {exc}")
    say("")
    tell(f"    Upload it by hand instead - the file is identical:")
    tell(f"        open http://{device.host}/  ->  Firmware Upgrade  ->  choose file")
    tell(f"        {path}")
    if not confirm(f"{what} uploaded through the web UI?"):
        raise Abort(f"{what} was not uploaded")
    output.transient_end()
    return "manual"



def step_prerequisites(ctx: Context) -> dict[str, Any]:
    """A0 - what has to be in place first, for anyone who skipped the README."""
    if any(ctx.state.is_done(step) for step, _, _ in STEPS if step != "A0"):
        return {"shown": False}  # a run from before this step existed
    output.transient_begin()
    if run_source(ctx.state) == "sonoff":
        needed = [
            "- The eWeLink app on your phone, signed in to an eWeLink account",
            "  (free - a throwaway account is fine).",
            "- The S60 set up in the app on the 2.4 GHz Wi-Fi it should stay on:",
            "  it keeps that network after the conversion, and cannot use 5 GHz.",
            "- LAN Control turned on in the plug's settings in the app.",
        ]
    else:
        needed = [
            "- The plug on the 2.4 GHz Wi-Fi it should stay on, as it is now: the",
            "  firmware it gets keeps that network.",
            "- This computer on the same network.",
        ]
    section("Before you start", needed + [
        "- The plug in a socket nobody will switch off until the run finishes.",
        "",
        "Nothing on the plug changes until you confirm, once, just before the",
        "point of no return - you will be asked then.",
    ])
    wait_for_key()
    output.transient_end()  # read: the list leaves the screen, the stage line stays
    return {"shown": True}


def step_check(ctx: Context) -> dict[str, Any]:
    """A5 - offline preconditions, building the bundle if there is none.  Touches no plug."""
    try:
        manifest = validate_bundle(ctx.manifest_path)
    except ValueError as exc:
        if ctx.manifest_path != (ROOT / DEFAULT_MANIFEST).resolve():
            raise Abort(f"the bundle at {ctx.manifest_path} cannot be used: {exc}") from exc
        manifest = make_bundle(ctx, problem=None if not ctx.manifest_path.exists() else str(exc))
    else:
        # Nothing is asked about Wi-Fi here: the details are read off the plug
        # at E1, and the bundle is rebuilt then if they are for another network.
        say("bundle and pinned artifacts: OK")

    problems = hostsys.check_sha256sums(ROOT / "SHA256SUMS")
    if problems:
        raise Abort("artifact checksums failed: " + "; ".join(problems))
    say("published artifact checksums: OK")

    restore_left_locks()
    for lock in safety_locks.NAMES:
        if not safety_locks.in_place(ROOT, lock):
            raise Abort(f"{lock} is not in place; refusing to start from an unsafe state")
    say("both safety locks in place")

    for phase in ("preflight", "stage", "commit", "restore"):
        report = ctx.evidence / f"{phase}-report.json"
        if report.exists():
            say(f"note: existing {phase} evidence in {ctx.evidence}")
    return {"mode": manifest.get("migration_mode", "official")}



def make_bundle(ctx: Context, problem: str | None) -> dict[str, Any]:
    """Build the bundle in place.  Asks nothing: the Wi-Fi is added at E1."""
    section("Prepare the firmware bundle", quiet=True, body=[
        *(["The existing bundle cannot be used:", f"  {problem}", ""] if problem else []),
        "This downloads the official Tasmota files and checks each one against",
        "its pinned fingerprint. It is done once; later plugs reuse it.",
        "",
        "Your Wi-Fi details are not needed for this. Later in the run they are",
        "read off the plug and written into an in-memory copy of the small",
        "temporary firmware, which has to rejoin your Wi-Fi by itself.",
    ])
    try:
        build_bundle(output=ctx.manifest_path.parent)
    except BundleError as exc:
        raise Abort(f"the bundle could not be prepared: {exc}") from exc
    say("Bundle ready.")
    return validate_bundle(ctx.manifest_path)


def sonoff_backup_wifi(flash: bytes) -> tuple[str, str] | None:
    """The Wi-Fi Sonoff's firmware was set up with, from a backup of it: the
    name and password in its frame_params (an open network has none)."""
    names, values = inspect_stock_nvs.parse(flash[0x9000:0x19000])
    for (namespace, key), value in values.items():
        if names.get(namespace) == "frame_params" and key == "content":
            params = json.loads(value.data.split(b"\x00", 1)[0])
            ssid = params.get("ssid")
            if isinstance(ssid, str) and ssid:
                return ssid, str(params.get("passwd") or "")
    return None


def wifi_from_esphome(ctx: "Context") -> tuple[str, str] | None:
    """The Wi-Fi the plug is on, name and password, from ESPHome itself: our
    ESPHome gives them from 1.1.0, through its native API - encrypted with
    its key, where it has one.  None from one that does not, or whose key is
    not known (noted: it is offered before the Wi-Fi is asked for)."""
    host = ctx.state.input("plug_ip")
    try:
        found = esphome.api_wifi_details(host, key=ctx.esphome_key) if host else None
    except esphome.ApiLocked:
        ctx.esphome_api_locked = True
        return None
    if found is None:
        return None
    output.private(found[0], "wifi-name")
    output.private(found[1], "wifi-password")
    say(f"    Read the Wi-Fi details off ESPHome on the plug ('{found[0]}') - none to type.")
    return found


def wifi_from_backup(ctx: "Context") -> tuple[str, str] | None:
    """The Wi-Fi in the plug's Sonoff backup on this computer - the network
    its Sonoff firmware was set up on - when it is known to be the plug's
    still: the bundle set up for exactly these details, or the network
    ESPHome says it is on.  None otherwise, and the details are asked for.
    The password is read in memory only, as the backup holds it already."""
    mac = ctx.state.input("plug_mac")
    backup = plugs.find_backup(mac) if mac else None
    if backup is None:
        return None
    try:
        found = sonoff_backup_wifi((backup.directory / "flash.bin").read_bytes())
    except (OSError, ValueError, KeyError):
        return None
    if found is None:
        return None
    ssid, password = found
    output.private(ssid, "wifi-name")
    output.private(password, "wifi-password")
    try:
        pinned = bundle_ssid(ctx.manifest_path)
    except (BundleError, OSError, ValueError):
        pinned = None
    if pinned == ssid:
        # Its password rebuilds the bundle's own startup firmware, or it is not
        # the one the bundle was proven with.
        try:
            recovery_image(ctx.manifest_path, ssid, password)
        except BundleError:
            tell(f"    The plug's Sonoff backup has an older password for '{ssid}' than the bundle's.")
            return None
        say(f"    The Wi-Fi in the plug's Sonoff backup, '{ssid}', is the bundle's - none to type.")
        return found
    host = ctx.state.input("plug_ip")
    wifi = esphome.api_wifi(host, key=ctx.esphome_key) if host else None
    joined = wifi.get("ssid") if wifi else None
    if joined == ssid:
        say(f"    The plug is on '{ssid}', the Wi-Fi in its Sonoff backup on this computer - "
            "none to type.")
        return found
    if joined:
        tell(f"    The plug is on '{joined}' now, not on '{ssid}' as in its Sonoff backup.")
    return None


def read_wifi_from_plug(ctx: "Context") -> tuple[str, str] | None:
    """The Wi-Fi details the plug is connected with, or None if they cannot be read.

    Tasmota never reports its password to a command, but its own configuration
    backup carries one.  It is decoded in memory, never written down, and only
    used when the name in it matches the network the plug says it is on - so
    the details are known to work at that moment.
    """
    def unreadable(reason: str) -> None:
        tell(f"    The Wi-Fi details could not be read off the plug: {reason}.")

    try:
        device = ctx.device
        status = device.command("Status 11")
        wifi = status.get("StatusSTS", {}).get("Wifi", {}) if isinstance(status, dict) else {}
        connected = wifi.get("SSId")
        if not connected:
            return unreadable("it did not say which network it is on")
        record = tasmota_settings.decode_backup(device.download_settings())
        if tasmota_settings.problems(record):
            return unreadable("its settings backup is not in the expected format")
        ssid = tasmota_settings.text(record, tasmota_settings.SET_STASSID1)
        password = tasmota_settings.text(record, tasmota_settings.SET_STAPWD1)
        output.private(connected, "wifi-name")
        output.private(ssid, "wifi-name")
        output.private(password, "wifi-password")
    except (Abort, DeviceError, ValueError, OSError) as exc:
        return unreadable(f"it did not answer as Tasmota ({exc})")
    if ssid != connected:
        return unreadable(f"it is on '{connected}', not the network in its first slot")
    if not password:
        # Tasmota joins with exactly its first slot's details, and it is on
        # that network now - so an empty password means an open network.
        say(f"    The plug is on '{ssid}', an open network (no password) - none to type.")
        return ssid, ""
    say(f"    Read the Wi-Fi details the plug is connected with ('{ssid}') - none to type.")
    return ssid, password


def check_plug_network(ctx: Context) -> dict[str, Any]:
    """The plug must be on the network the startup firmware will join.

    After E3 the startup firmware rejoins Wi-Fi by itself, with only the details
    in the bundle.  The plug's own connection proves a network name works from
    where it stands; the bundle must name that network.  The password cannot be
    checked - Tasmota never reports it.
    """
    wanted = bundle_ssid(ctx.manifest_path)
    if wanted is None:
        say("This bundle's Wi-Fi details are not readable, so the network check is skipped.")
        return {"network_check": "skipped"}
    status = ctx.device.command("Status 11")
    wifi = status.get("StatusSTS", {}).get("Wifi", {}) if isinstance(status, dict) else {}
    connected = wifi.get("SSId")
    signal = wifi.get("Signal")
    output.private(wanted, "wifi-name")
    output.private(connected, "wifi-name")
    if not connected:
        raise Abort(
            "the plug did not say which Wi-Fi network it is on, so the bundle's "
            "network cannot be checked. Nothing was written"
        )
    output.transient_begin()
    while connected != wanted:
        section("The bundle is set up for a different Wi-Fi network", [
            f"The plug is connected to        '{connected}'",
            f"The bundle's firmware will join '{wanted}'",
            "",
            "Later the plug has to rejoin Wi-Fi using only the bundle's details.",
            "With these it would never come back. Nothing has been written.",
            "",
            f"Give the password for '{connected}' and the startup firmware is set",
            "up for that network instead.",
        ])
        if not ask_yes_no(f"Enter the password for '{connected}' now", default=True):
            raise Abort(f"the plug is on '{connected}' but the bundle is set up for '{wanted}'")
        ctx.adopt_wifi(connected, ask_password(connected))
        wanted = bundle_ssid(ctx.manifest_path)
    output.transient_end()
    say(f"Network check: the plug is on '{connected}', the network the startup "
        f"firmware will join (signal {signal} dBm).")
    warn_if_weak_signal(ctx, signal, ["After the next stage the startup firmware has",
                                      "to rejoin on its own, and a weak signal makes that less certain."])
    return {"network": connected, "signal_dbm": signal}


def plug_signal(device: Device) -> int | None:
    """The Wi-Fi signal in dBm that the Tasmota on the plug reports, or None
    when it does not say."""
    try:
        status = device.command("Status 11")
    except (DeviceError, OSError):
        return None
    wifi = status.get("StatusSTS", {}).get("Wifi", {}) if isinstance(status, dict) else {}
    signal = wifi.get("Signal")
    return signal if isinstance(signal, int) else None


def advert_signal(ctx: Context, report: dict[str, Any], plug_ip: str) -> int | None:
    """The Wi-Fi signal Sonoff's own firmware gives in its eWeLink advert, which
    is encrypted with the plug's LAN key: opened with the key the account holds
    for it.  None when it cannot be read."""
    ids = {str(value)[3:] for packet in report.get("packets", []) if packet.get("source") == plug_ip
           for record in packet.get("records", []) if record.get("type") == "TXT"
           for value in record.get("value") or [] if str(value).startswith("id=")}
    fields = ewelink_lan.advert_fields(report, next(iter(ids))) if len(ids) == 1 else None
    if not fields:
        return None
    key = next((device.get("devicekey") for device in ctx.account_devices() or []
                if device.get("deviceid") == fields.get("id")), None)
    return ewelink_lan.signal(ewelink_lan.advert_state(fields, key)) if key else None


def note_signal(ctx: Context, signal: int | None, then: list[str]) -> int | None:
    """Say a reading of the plug's Wi-Fi signal, and ask about a weak one."""
    if signal is not None:
        say(f"Wi-Fi signal at the plug: {signal} dBm.")
        warn_if_weak_signal(ctx, signal, then)
    return signal


def warn_if_weak_signal(ctx: Context, signal: Any, then: list[str]) -> None:
    """A weak signal, asked about before going on; `then` says what has to join
    the Wi-Fi on its own afterwards.  Asked once a run: a weak signal agreed to
    is not asked about again at the next reading."""
    if not (isinstance(signal, int) and signal < WEAK_SIGNAL_DBM):
        return
    if ctx.state.input("weak_signal_agreed"):
        say("    The signal is still weak; carrying on, as agreed.")
        return
    output.transient_begin()
    section("The plug's Wi-Fi signal is weak", [
        f"It reports {signal} dBm. {then[0]}", *then[1:],
        "Moving the plug nearer the access point first is worth it.",
    ])
    if not ask_yes_no("Carry on anyway", default=False):
        raise Abort("stopped for a weak Wi-Fi signal; nothing was written")
    ctx.state.set_input("weak_signal_agreed", True)
    output.transient_end()



def step_pair(ctx: Context) -> dict[str, Any]:
    """A1 - find the plug: an S60 in the owner's eWeLink account, answering
    on this network, asking only when there is a choice to make."""
    ip = ctx.state.input("plug_ip")
    if not ip:
        say("Signing in to eWeLink to see which S60 plugs are in the account...")
        devices = ctx.account_devices()
        ip = find_any_plug() if devices is None else find_account_plug(ctx, devices)
    ctx.state.set_input("plug_ip", ip)
    account = getattr(ctx, "account_plug", None)
    mac = hostsys.mac_of(ip) or (account.mac if account else None)
    if mac and plugs.MAC_RE.match(mac):
        ctx.state.set_input("plug_mac", mac)
        file_plug(ctx, mac, account)
    return {"plug_ip": ip, "plug_mac": mac}


def hold_plug(ctx: Context, mac: str) -> None:
    """One run on a plug at a time: two at once would each write over what the
    other is doing."""
    try:
        plugs.hold(mac, run_name(ctx.dir) if hasattr(ctx, "dir") else "")
    except plugs.PlugBusy as busy:
        raise Abort(
            f"another run is converting this plug ({mac}) right now"
            + (f" - {busy}" if str(busy) else "")
            + ". Let it finish, or stop it first, then run this again. Nothing was done"
        ) from None


def file_plug(ctx: Context, mac: str, account: AccountPlug | None) -> None:
    """Keep this run, and what is known of the plug, under its MAC; say if its
    Sonoff firmware is backed up from an earlier run."""
    hold_plug(ctx, mac)
    if hasattr(ctx, "file_under_plug"):
        ctx.file_under_plug(mac)
    if account:
        plugs.remember_plug(mac, ewelink_name=account.name, ewelink_id=account.device_id,
                            model=account.model)
    backup = plugs.find_backup(mac)
    if backup:
        say(f"    Its Sonoff firmware is backed up already{f' ({backup.taken})' if backup.taken else ''},"
            f" in {display_path(backup.directory)}.")


@dataclass(frozen=True)
class AccountPlug:
    """An S60 in the owner's eWeLink account: what the account says about it."""
    device_id: str
    name: str
    model: str
    online: bool | None
    mac: str | None
    ssid: str | None


@dataclass(frozen=True)
class Sighting:
    """Where an account's S60 was seen: answering eWeLink's local discovery
    at `ip`, or only by its hardware address there (LAN Control off)."""
    plug: AccountPlug
    ip: str | None
    answering: bool


def keep_account_private(devices: list[dict[str, Any]]) -> None:
    """Keep what the account says about each device out of the log: its id,
    the name it was given, its hardware address and its network."""
    for device in devices:
        params = device.get("params") or {}
        output.private(device.get("deviceid"), "device-id")
        name = str(device.get("name") or "")
        generic = {"sonoff", "s60", "plug", "smart", "socket",
                   str(device.get("productModel") or "").lower()}
        if not set(name.lower().split()) <= generic:  # "Sonoff S60" names nobody
            output.private(name, "plug-name")
        output.private(params.get("staMac"), "mac")
        output.private(params.get("ssid"), "wifi-name")


def account_plug(device: dict[str, Any]) -> AccountPlug | None:
    """The account's record of a device, if it is an S60; its LAN key is left out."""
    product = str(device.get("productModel") or "").strip().upper()
    family = str((device.get("extra") or {}).get("model") or "")
    if not (S60_PRODUCT.fullmatch(product) or family == S60_DEVICE_MODEL):
        return None
    params = device.get("params") or {}
    mac = params.get("staMac")
    online = device.get("online")
    return AccountPlug(
        device_id=str(device.get("deviceid") or ""),
        name=str(device.get("name") or "").strip() or "Sonoff S60",
        model=product or "S60",
        online=online if isinstance(online, bool) else None,
        mac=hostsys.normalise_mac(str(mac)) if mac else None,
        ssid=str(params.get("ssid") or "") or None,
    )


def match_plugs(plugs: list[AccountPlug], answers: dict[str, str],
                neighbours: dict[str, str]) -> list[Sighting]:
    """Each account plug against the network.

    `answers` is {device id: address} from eWeLink's local discovery - the id
    in each answer is the account's, so nothing else can be mistaken for the
    plug.  `neighbours` is {address: hardware address}, which finds a plug
    that is on the network but not answering.
    """
    by_mac = {mac: address for address, mac in neighbours.items()}
    sightings = []
    for plug in plugs:
        if plug.device_id in answers:
            sightings.append(Sighting(plug, answers[plug.device_id], True))
        else:
            sightings.append(Sighting(plug, by_mac.get(plug.mac or ""), False))
    return sightings


def describe_plug(plug: AccountPlug) -> str:
    """Its name in the app, and its model unless that is the name already."""
    if plug.name.upper() == plug.model.upper():
        return f"'{plug.name}'"
    return f"'{plug.name}' ({plug.model})"


def cut_off(sighting: Sighting) -> bool:
    """Answering here while eWeLink shows it offline: the plug is on the
    network but cannot reach the internet, so the upgrade command, which only
    reaches it through eWeLink, would never arrive."""
    return sighting.answering and sighting.plug.online is False


# An update cut off part way - this computer stopped while it was being sent -
# can leave the plug on Sonoff's firmware but away from eWeLink until it restarts.
AFTER_A_CUT_OFF_SEND = ("switch the plug off and on: after an update cut off part way "
                        "it can stay away from eWeLink until it restarts.")


def why_not_found(sighting: Sighting) -> str:
    """What to do about an account plug that is not answering here, or that
    is answering but cannot be converted from here."""
    plug = sighting.plug
    if cut_off(sighting):
        return (f"{describe_plug(plug)} answers at {sighting.ip}, but eWeLink shows it "
                "offline - it cannot reach the internet. The conversion is sent to the "
                "plug through eWeLink, so allow it internet access for this run (you can "
                "block it again once it runs Tasmota, which needs no cloud). If it has "
                f"that already, {AFTER_A_CUT_OFF_SEND}")
    if plug.online is False:
        return (f"{describe_plug(plug)}: eWeLink shows it offline - check it is switched "
                "on at the wall, on your Wi-Fi, and allowed to reach the internet. If "
                f"it is, {AFTER_A_CUT_OFF_SEND}")
    if sighting.ip:
        return (f"{describe_plug(plug)} is at {sighting.ip} but does not answer on the "
                "network - turn on LAN Control in its settings in the eWeLink app.")
    network = f" (it is on '{plug.ssid}')" if plug.ssid else ""
    return (f"{describe_plug(plug)} is online in eWeLink but was not seen on this "
            f"computer's network{network} - this computer must be on the same Wi-Fi "
            "network, on the same router.")


def sight_plugs(plugs: list[AccountPlug]) -> list[Sighting]:
    """Look for the account's S60s on this network."""
    macs = {plug.mac for plug in plugs if plug.mac}
    # Where this computer has already seen them: asked straight away.
    hints = [address for address, mac in hostsys.neighbour_table().items() if mac in macs]
    try:
        report = discover(6.0, hints=hints, wanted={plug.device_id for plug in plugs
                                                    if plug.online is not False})
    except OSError:
        report = {"packets": []}
    answers: dict[str, str] = {}
    for packet in report.get("packets", []):
        for record in packet.get("records", []):
            if record.get("type") == "TXT":
                for value in record.get("value") or []:
                    if str(value).startswith("id="):
                        answers.setdefault(str(value)[3:], packet["source"])
    # Asking every address filled the neighbour table: read it again.
    return match_plugs(plugs, answers, hostsys.neighbour_table())


# A plug that has just started shows offline in eWeLink for a while, answering
# here or not yet: how long to wait for it to catch up before asking.
CLOUD_CATCH_UP = 60.0


def find_account_plug(ctx: Context, devices: list[dict[str, Any]]) -> str:
    """The address of the S60 to convert, from the account's S60s that answer here."""
    started = time.monotonic()
    while True:
        plugs = [plug for plug in map(account_plug, devices) if plug is not None]
        if not plugs:
            output.transient_begin()
            others = [f"- '{str(d.get('name') or '').strip() or d.get('deviceid')}' "
                      f"({d.get('productModel') or 'unknown model'})" for d in devices]
            section(f"No Sonoff S60 in the eWeLink account {ctx.email()}", [
                *(["It has:", *others] if others else ["It has no devices."]),
                "",
                "Add the S60 in the eWeLink app, or use the account it is in.",
            ])
            choice = ask_choice("What now", [
                "Look again",
                "Use a different eWeLink account",
                "Type the plug's address myself",
            ])
            output.transient_end()
        else:
            say("Looking for it on your network...")
            sightings = sight_plugs(plugs)
            ready = [sighting for sighting in sightings
                     if sighting.answering and not cut_off(sighting)]
            if len(ready) == 1:
                # The stage line completing says it all; the log keeps which plug.
                say(f"    Found {describe_plug(ready[0].plug)} at {ready[0].ip}.")
                ctx.account_plug = ready[0].plug
                return str(ready[0].ip)
            if ready:
                output.transient_begin()
                choice = ask_choice(
                    "More than one S60 in the account answered. Which one is this run for",
                    [f"{describe_plug(sighting.plug)} at {sighting.ip}" for sighting in ready],
                )
                output.transient_end()
                ctx.account_plug = ready[choice].plug
                return str(ready[choice].ip)
            waited = time.monotonic() - started
            if all(sighting.plug.online is False for sighting in sightings) and waited < CLOUD_CATCH_UP:
                # Offline in eWeLink: usually a plug that has only just started -
                # plugged in as the run began - and is seconds from joining the
                # Wi-Fi and the cloud. Answering here already, or not yet.
                if all(map(cut_off, sightings)):
                    say(f"    It answers here but eWeLink shows it offline; checking again ({waited:.0f}s)...")
                else:
                    say(f"    eWeLink shows it offline - just switched on, perhaps; checking again ({waited:.0f}s)...")
                output.progress(f"waiting for eWeLink to see the plug online... {waited:.0f}s")
                time.sleep(10.0)
                output.progress_clear()
                refreshed = ctx.account_devices()
                if refreshed is None:
                    return find_any_plug()
                devices = refreshed
                continue
            output.transient_begin()
            if all(map(cut_off, sightings)):
                # Found here: only its own internet access is in the way.
                title, note = "Your S60 is on this network but cannot reach the internet", []
            else:
                title, note = ("Your S60 did not answer on this network",
                               hostsys.security_software_note())
            section(title, [
                *(f"- {why_not_found(sighting)}" for sighting in sightings),
                *([""] + note if note else []),
            ])
            choice = ask_choice("What now", [
                "Look again",
                "Use a different eWeLink account",
                "Type the plug's address myself",
            ])
            output.transient_end()
        if choice == 2:
            output.transient_begin()  # gone once answered, as the menu was
            ip = ask_ipv4(
                "The plug's address",
                help_lines=["Find your plug's IP address in your router's device list, or eWeLink's device information."],
                check=plug_at({plug.device_id for plug in plugs}),
            )
            output.transient_end()
            return ip
        if choice == 1:
            ctx.forget_account()
        refreshed = ctx.account_devices()  # its online status may have changed
        if refreshed is None:
            return find_any_plug()
        devices = refreshed


def find_any_plug() -> str:
    """Without eWeLink: any plug answering on the network, asking only when
    it is ambiguous."""
    ip = None
    while not ip:
        say("Looking for Sonoff plugs on your network...")
        plugs = find_ewelink_plugs()
        if len(plugs) == 1:
            ip, name = plugs[0]
            say(f"Found {name} at {ip}.")
        elif plugs:
            output.transient_begin()
            choice = ask_choice(
                "More than one Sonoff plug answered. Which one is this run for",
                [f"{name} at {address}" for address, name in plugs],
            )
            output.transient_end()
            ip = plugs[choice][0]
        else:
            # Taken off the screen once answered, so looking again does not
            # stack another copy under the stage line.
            output.transient_begin()
            note = hostsys.security_software_note()
            section("No Sonoff plug answered on this network", [
                "Check, in the eWeLink app:",
                "- the S60 is added, on the same 2.4 GHz Wi-Fi network as this",
                "  computer - the plug cannot use 5 GHz",
                "- LAN Control is turned on in the plug's settings",
                "- the plug is switched on at the wall",
                *([""] + note if note else []),
            ])
            choice = ask_choice(
                "What now",
                ["Look again", "Type the plug's address myself"],
            )
            if choice == 1:
                ip = ask_ipv4(
                    "The plug's address",
                    help_lines=["Find your plug's IP address in your router's device list, or eWeLink's device information."],
                    check=plug_at(None),
                )
            output.transient_end()
    return ip


def plug_at(wanted: set[str] | None) -> Callable[[str], list[str]]:
    """For a typed address: what is wrong with it, unless an S60 in the
    account (`wanted`'s ids; None: any eWeLink device) answers eWeLink's
    discovery there.  The next stage asks it there, and needs the answer to
    go on: an address that only answers a ping - the router, say - would
    stop the run then, with nothing to show what was wrong."""
    what = "a Sonoff plug" if wanted is None else "your S60"

    def check(address: str) -> list[str]:
        tell(f"    Checking that {address} is {what}...")
        try:
            found = answered_ids(discover(6.0, None, address)["packets"])
        except OSError:
            found = set()
        if found and (wanted is None or found & wanted):
            return []
        if found:
            return [f"{address} is a different eWeLink device, not an S60 in this account."]
        return [
            f"{address} answered, but not as an eWeLink device - it is probably something",
            "else, such as your router. If it is the plug, turn on LAN Control in its",
            "settings in the eWeLink app.",
        ]
    return check


def find_ewelink_plugs(timeout: float = 6.0) -> list[tuple[str, str]]:
    """(address, name) for each eWeLink device answering mDNS on this network."""
    try:
        report = discover(timeout)
    except OSError:
        return []
    found: dict[str, str] = {}
    for packet in report.get("packets", []):
        source = packet.get("source")
        if not source or "records" not in packet:
            continue
        label = found.get(source, "a Sonoff plug")
        for record in packet["records"]:
            if record.get("type") == "TXT":
                for value in record.get("value") or []:
                    if str(value).startswith("id="):
                        label = f"eWeLink device {str(value)[3:]}"
        found[source] = label
    return sorted(found.items())




def already_converting(plug_ip: str) -> None:
    """Stop with the real reason when the plug no longer runs Sonoff's firmware.

    eWeLink shows a plug part way through a conversion as offline, and it does
    not answer eWeLink discovery; "check LAN Control" would send its owner the
    wrong way.
    """
    body = setup_network.page(plug_ip, timeout=3.0)
    if body is None:
        return
    if setup_network.is_bridge_page(body):
        raise Abort(
            f"the plug at {plug_ip} is already running our installer firmware, from "
            "an earlier conversion. Run this again and choose to continue that "
            "conversion. If none is offered, switch the plug off and on: it goes "
            "back to its Sonoff firmware, and a new conversion can start"
        )
    if "tasmota" in body.lower():
        raise Abort(
            f"{plug_ip} is running Tasmota, not Sonoff's firmware. If that is this "
            "plug, part way through an earlier conversion, run this again and "
            "choose to continue that conversion"
        )


def step_discover(ctx: Context) -> dict[str, Any]:
    """A2/A3/A4 - identify the device and read its OTA metadata."""
    plug_ip = ctx.state.require("plug_ip")[0]
    mdns = ctx.dir / "mdns.json"
    meta = ctx.dir / "ota-metadata.json"

    # The same report the discovery tool writes, without its console output.
    report = discover(8.0, None, plug_ip)
    if not report["packets"]:
        already_converting(plug_ip)
        note = hostsys.security_software_note()
        if note:
            section("Something on this computer may be in the way", note)
        raise Abort(f"the plug at {plug_ip} did not answer the eWeLink discovery; check "
                    "it is switched on and LAN Control is on in the eWeLink app")
    mdns.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    email = ctx.email()
    env = ctx.ewelink_env()
    if run_tool(hostsys.python_script("tools/query_ota.py", "--mdns-capture", str(mdns),
                                      "--email", email, "--output", str(meta)),
                interactive=True, env=env) != 0:
        reasons = output.tool_problems(1)
        raise Abort("the eWeLink query failed" + (f": {reasons[0]}" if reasons else ""))

    # Read what query_ota just wrote instead of asking the operator to retype it.
    try:
        metadata = json.loads(meta.read_text(encoding="utf-8"))
        version = str(metadata["query"]["version"])
        model = str(metadata["query"].get("model", ""))
        product = str(metadata.get("product") or "")
        endpoints = ota_endpoints(metadata["response"]["data"]["otaInfoList"])
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise Abort(f"could not read the OTA metadata just written: {exc}") from exc
    finally:
        # Its download links may work as access tokens; what is needed is now read.
        meta.unlink(missing_ok=True)
    if model != S60_DEVICE_MODEL:
        raise Abort(
            f"{plug_ip} is a {model or 'device of unknown model'}, not a Sonoff S60 "
            f"({S60_DEVICE_MODEL}). This tool only converts the S60; nothing was written"
        )

    # Our upgrade command names this computer as the download address and
    # carries our firmware's hash, so the plug fetches our image directly
    # whether or not Sonoff is also offering it one.  We never redirect
    # Sonoff's own download, so no router rule is involved in any case.
    #
    # Earlier versions added a router DNAT rule whenever an update was on
    # offer, as a guard against the plug fetching Sonoff's genuine update in
    # the same window.  That guard was dropped; the "Getting in" note in
    # docs/what-happens.md and the DNAT recipe in
    # archive/docs/reference/ota-findings.md record how to reinstate it.
    if endpoints:
        offered = f"{endpoints[0][0]} port {endpoints[0][1]} (not used - we deliver directly)"
    else:
        offered = "none - this is the latest Sonoff firmware"
    section("What the plug reported", quiet=True, body=[
        f"Model            {model}",
        f"Product          {product or 'not given'}",
        f"Firmware now     {version}",
        f"Update offered   {offered}",
        "",
        "The plug fetches our firmware straight from this computer, so no",
        "router rule is needed.",
    ])
    # The earliest reading of a run from Sonoff's firmware: before anything is
    # written, while moving the plug costs nothing.
    signal = note_signal(ctx, advert_signal(ctx, report, plug_ip), [
        "Nothing has been written yet. The conversion restarts the plug",
        "several times, and each time it has to rejoin your Wi-Fi on its own;",
        "a weak signal makes that less certain."])
    ctx.state.set_input("stock_version", version)
    ctx.state.set_input("product_model", product)
    return {"stock_version": version, "update_offered": bool(endpoints), "model": model,
            "signal_dbm": signal}



def step_bridge(ctx: Context) -> dict[str, Any]:
    """C - stock to recovery bridge.  First write; no rollback below this line."""
    plug_ip, version = ctx.state.require("plug_ip", "stock_version")
    listen_ip = ctx.listen_ip()
    email = ctx.email()
    port = choose_port(ctx, "delivery_port", DELIVERY_PORTS, "the firmware")
    ensure_firewall(ctx)
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
    warning_box("Before going on", [
        "From here the plug's Sonoff firmware is replaced, so the eWeLink app",
        "will no longer control it. DO NOT UNPLUG IT during the process.",
        kept,
        "Only go ahead with a plug you can afford to lose",
    ])
    if not require_word(CONSENT_WORD):
        raise Abort("not confirmed - nothing has been written to the plug")
    output.transient_end()  # agreed: the note and the question have done their job

    started = time.monotonic()
    with safety_locks.lifted(ROOT, "RECOVERY_LOCK"):
        code = run_tool(
            hostsys.python_script(
                "tools/serve_tasmota_ota.py",
                "--listen-ip", listen_ip, "--listen-port", str(port),
                "--device-ip", plug_ip,
                "--firmware", str(ctx.bridge),
                "--email", email, "--expected-current-version", version,
                "--mdns-capture", str(ctx.dir / "mdns.json"),
                "--i-understand-stock-has-no-automatic-rollback"),
            interactive=True,
            env=env,
        )
    say_lock_back("RECOVERY_LOCK")
    if code != 0:
        # The sender can fail after the plug already has everything - the cloud
        # reporting it offline is exactly what a successful install looks like.
        installed, reason = evidence_bridge_installed(ctx)
        if installed:
            say(f"  {reason}")
            return {"seconds": round(time.monotonic() - started, 1), "confirmed_by": "bridge network"}
        reported = output.tool_problems()
        # Some of it served means the plug reached this computer: not the
        # firewall, but its Wi-Fi going part way (a router restarting, say).
        started = output.tool_said("Served /user") or output.tool_said("did not request every")
        offline = output.tool_said("offline in eWeLink")
        section("The bridge was not delivered", [
            *(["The sender reported:", *(f"  {line}" for line in reported), ""] if reported else []),
            *([
                "The plug started downloading it from this computer, but stopped part",
                "way - most likely its Wi-Fi dropped: a router restarting, or a weak",
                "signal. It keeps its Sonoff firmware, so nothing is lost; run this",
                "again once its Wi-Fi is steady.",
            ] if started else [
                "eWeLink does not see the plug, so the installer cannot be sent. It",
                "keeps its Sonoff firmware, so nothing is lost: switch the plug off",
                "and on, wait for it to show online in the eWeLink app, and run this",
                "again.",
            ] if offline else firewall_hint(ctx)),
        ])
        raise Abort("bridge delivery stopped part way" if started else "bridge delivery failed")
    return {"seconds": round(time.monotonic() - started, 1)}




def bridge_on_home_network(ctx: Context, *, timeout: float = 60.0) -> str | None:
    """The bridge's address on the home network, if it joined it.

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

    With `tasmota` - Tasmota named as the installer was, so its network has
    the installer's name - TASMOTA_THERE once Tasmota answers on it instead:
    installed already, by a run cut off after the upload."""
    if bridge_answers(setup_network.SETUP_HOST):
        return setup_network.SETUP_HOST  # joined at C3, or by hand
    if tasmota and setup_network.tasmota_answers():
        return TASMOTA_THERE
    address = bridge_on_home_network(ctx)
    if address:
        say(f"    The installer joined your network at {address}.")
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
        bridge = find_ssid(bridge_networks(ctx)) or our_setup_network(ctx)
        if not asked:
            ask_to_join_bridge(bridge, tasmota=tasmota)
            asked = True

        def gone_back() -> bool:
            return getattr(ctx, "bridge_sends", 0) < BRIDGE_SENDS and back_on_sonoff(ctx)

        found = wait_for_bridge(ctx, bridge.split()[0], gone_back=gone_back, tasmota=tasmota)
        if found != BACK_ON_SONOFF:
            return found or setup_network.SETUP_HOST
        tell("    While waiting, the plug went back to Sonoff's own firmware (switched off")
        tell("    and on, say).")


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
            raise Abort("the plug is back on Sonoff's own firmware, so the installer has to "
                        "be sent again through eWeLink: run this again with --new")
        sends += 1
        ctx.bridge_sends = sends
        tell("    The plug is running Sonoff's own firmware again: it goes back to it when")
        tell("    switched off and on before the installer has done its work, so nothing")
        tell(f"    is lost. Sending the installer again (attempt {sends} of {BRIDGE_SENDS}).")
        ctx.state.mark("C2", FAILED, error="redo: the plug went back to Sonoff's firmware")
        try:
            ctx.state.mark("C2", DONE, result=step_bridge(ctx))
        except Abort as exc:
            # Cut off part way, the plug keeps Sonoff's firmware: another go,
            # while there are goes left.
            if sends == BRIDGE_SENDS:
                raise
            tell(f"    {exc}.")
            failed = True
            continue
        address = bridge_on_home_network(ctx, timeout=120.0)
        if address:
            say(f"    The installer joined your network at {address}.")
            return address
    return None


def ask_to_join_bridge(bridge: str, *, tasmota: bool = False) -> None:
    if bridge.startswith(LEGACY_BRIDGE_PREFIX):
        # An earlier release's installer: its network has a password.
        copied = hostsys.copy_to_clipboard(LEGACY_BRIDGE_PASSWORD)
        joining = [f"    network   {bridge}",
                   f"    password  {LEGACY_BRIDGE_PASSWORD}"
                   + ("      (already copied - just paste it)" if copied else "")]
    else:
        joining = [f"    {bridge}      (open - no password)"]
    section("Join the bridge's Wi-Fi network", [
        *(["The plug is running our small installer - or Tasmota, if the installer",
           "had sent it before the run stopped: either has made its own Wi-Fi network,",
           f"by the same name. In {hostsys.wifi_menu_hint()}, join:"] if tasmota else
          ["The plug is running our small installer, which has made its own Wi-Fi",
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
        say("    The plug is past the installer already, so there is nothing to back up from.")
        outcome = "not taken"
    else:
        skip = backup_not_needed(ctx)
        if skip:
            outcome, message = skip
            say(f"    {message}")
        else:
            outcome = back_up_stock(ctx, reach_bridge(ctx))
    ctx.state.set_input("backup_outcome", outcome)
    return {"outcome": outcome, "stage_outcome": BACKUP_OUTCOMES[outcome][0]}


def step_berry_tasmota(ctx: Context) -> dict[str, Any]:
    """D1/D2 - Berry-capable Tasmota through the bridge."""
    image = ROOT / "captures/safeboot-recovery-migration/tasmota32c3-bluetooth.bin"
    say("Looking for the plug's Wi-Fi network...")
    bridge = find_ssid(bridge_networks(ctx))
    setup = None if bridge else find_ssid(setup_networks(ctx))
    if setup:
        # Resuming after the upload: Tasmota is already installed and waiting.
        say(f"    Tasmota is already installed and waiting on {setup}.")
        note_setup_network(ctx, setup)
        ip = configure_plug_wifi(ctx, setup)
        return {"plug_ip": ip, "device_name": setup, "resumed": True}

    address = reach_bridge(ctx, tasmota=bool(ctx.state.input("tasmota_named_alone")))
    if address == TASMOTA_THERE:
        # Run again after the upload: the network the installer had is
        # Tasmota's now, by the same name, and it is waiting on it.
        say(f"    Tasmota is already installed and waiting on {our_setup_network(ctx)}.")
        ip = configure_plug_wifi(ctx, our_setup_network(ctx))
        return {"plug_ip": ip, "device_name": ctx.state.input("device_name"), "resumed": True}
    if address == setup_network.SETUP_HOST:
        if not ctx.state.input("tasmota_wifi_preset"):
            give_tasmota_the_wifi(ctx, address)  # its name, and the Wi-Fi if proven
        upload_tasmota(image)
        if ctx.state.input("tasmota_wifi_preset"):
            # An earlier try, cut off, had the bridge save Tasmota's settings
            # with your Wi-Fi: Tasmota joins it by itself, with no setup network
            # of its own, even sent through the bridge's.
            say("    Tasmota will start with your Wi-Fi details, so it joins by itself.")
            section("Back to your usual Wi-Fi", [
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
        section("Tasmota has not appeared on your network", quick_power_cycle_hint(ctx))
        return finish_tasmota_setup(ctx, route="home network", wait=900.0)
    return finish_tasmota_setup(ctx, route="home network")


# How the backup went, for the stage line and the report: BACKUP_OUTCOMES.
BACKUP_OUTCOMES = {
    "saved": ("complete", "Completed"),
    "existing": ("already done", "Taken by an earlier run"),
    "failed": ("skipped", "Failed"),
    "not wanted": ("skipped", "Not taken (--no-backup)"),
    "unreadable": ("skipped", "Not taken (plug unreadable)"),
    "not taken": ("skipped", "Not taken"),
}


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
        ctx.state.set_input("stock_backup", display_path(existing.directory))
        return "existing", f"Its Sonoff firmware is backed up already, in {display_path(existing.directory)}."
    return None


# Testing only (run --simulate-backup-failure): how many backup reads still
# to fail.  Two fail one round, so the menu that follows can be tried.
SIMULATED_BACKUP_FAILURES = 0
BACKUP_RETRY, BACKUP_WITHOUT = 0, 1


def read_for_backup(host: str) -> tuple[bytes, bytes, dict[str, Any]]:
    global SIMULATED_BACKUP_FAILURES
    if SIMULATED_BACKUP_FAILURES > 0:
        SIMULATED_BACKUP_FAILURES -= 1
        raise OSError("simulated failure (--simulate-backup-failure)")
    return read_dump(host)


def backup_failed(problem: str) -> int:
    """Ask what to do about a backup that failed twice: BACKUP_RETRY or
    BACKUP_WITHOUT; Quit stops the run, which carries on from here next time."""
    options = ["Try again", "Carry on without a backup"]
    output.transient_begin()
    section("The Sonoff firmware could not be backed up", [
        f"Reading the plug's memory failed twice: {problem}.",
        "",
        "The conversion can be performed without a backup, though that's not recommended.",
    ])
    if prompts.UNATTENDED:
        prompts.answered_unattended("What now", options[BACKUP_WITHOUT])
        choice = BACKUP_WITHOUT
    else:
        choice = ask_choice("What now", options, help_lines=[
            "Quitting leaves the plug on the installer: run this again to carry on",
            "from here, or switch the plug off and on to go back to Sonoff.",
        ])
    output.transient_end()
    return choice


def back_up_stock(ctx: Context, host: str) -> str:
    """Keep the plug's whole flash, Sonoff firmware and all, before D1 writes over it.

    The bridge only reads it.  Taken automatically, under the consent typed at
    C2; if it fails the person decides whether to carry on without one, and an
    unattended run carries on.  Returns how it went: a BACKUP_OUTCOMES key.
    """
    skip = backup_not_needed(ctx)
    if skip:
        say(f"    {skip[1]}")
        return skip[0]
    known = ctx.state.input("plug_mac")
    say("Backing up the plug's Sonoff firmware - about a minute...")
    while True:
        problem = ""
        for _ in range(2):
            try:
                flash, efuse, report = read_for_backup(host)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    # A bridge from before 4.4.2, given with --bridge.
                    say("    This installer cannot read the plug's memory, so no backup is kept.")
                    return "unreadable"
                problem = str(exc)
            except OSError as exc:  # a short transfer included
                problem = str(exc)
            say(f"    The backup did not come through: {problem}")
        else:
            if backup_failed(problem) == BACKUP_RETRY:
                say("    Trying the backup again...")
                continue
            say("    Carrying on without a backup.")
            return "failed"
        break

    mac = plugs.mac_from_efuse(efuse)
    if known and plugs.folder_name(known) != plugs.folder_name(mac):
        say(f"    The network reported {known} for the plug; its chip says {mac}, which is used.")
    if hasattr(ctx, "file_under_plug"):
        ctx.file_under_plug(mac)  # if A1 could not tell its MAC
    out, _ = plugs.save_backup(flash, efuse, report, ctx.state.input("stock_version"))
    ctx.state.set_input("stock_backup", display_path(out))
    for line in summary(report):
        say(f"    {line}")
    say(f"    Saved in {display_path(out)}, under the plug's MAC (private: it holds the")
    say("    Wi-Fi password).")
    if not is_good(report):
        tell("    It did not check out as a whole, bootable flash, so it may not restore;")
        tell("    its report.json says what was wrong. Carrying on.")
    return "saved"


# What wait_for_bridge says when the plug went back to Sonoff's firmware meanwhile.
BACK_ON_SONOFF = "back on Sonoff's firmware"
# What reach_bridge says when Tasmota answers on the installer's network instead.
TASMOTA_THERE = "Tasmota on the installer's network"
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
    with `tasmota`, TASMOTA_THERE once Tasmota answers on it (reach_bridge).

    A bridge on a weak signal can take longer to join the home network than
    the first look allows; it then never raises its own network, so waiting
    only for that would wait forever.
    """
    if timeout is None:
        timeout = user_action_limit()
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
            tell("    Nothing is answering at the plug's address. If it is not on your Wi-Fi at all -")
            tell("    after a Wi-Fi outage, Sonoff's firmware can stay off it - switch it off and on:")
            tell("    at this stage that is safe. This carries on by itself.")
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
            say(f"    Connected to {network}.")
            return None
        address = ctx.finder.poll()
        if address:
            body = setup_network.page(address, timeout=3.0)
            if setup_network.is_bridge_page(body):
                output.progress_clear()
                return address
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
        "  Switch the plug off and on 7 times in a row, leaving it on for only",
        "  2-3 seconds each time, then leave it on.",
        "",
        "That makes Tasmota clear its settings and open its setup network",
        f"({setup_network_hint(ctx)}). This carries on from there by itself.",
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
            say(f"    Tasmota will need its Wi-Fi given by hand: {reason}.")
        return False
    if ctx.state.input("plug_mac"):
        template = tasmota_settings.named(template, our_setup_network(ctx))
    elif alone:
        return False  # nothing to give it: the Wi-Fi is given on its network, as ever
    try:
        answer = setup_network.send_tasmota_settings(template, host=address,
                                                     layout=tasmota_settings.layout_query())
    except setup_network.SetupError as exc:
        if not alone:  # an installer before 4.4.4 refuses them there: Tasmota's own name
            say(f"    Tasmota will need its Wi-Fi given by hand: {exc}")
        return False
    if setup_network.SETTINGS_WITH_WIFI not in answer:
        ctx.state.set_input("tasmota_named_alone", True)
        say(f"    Tasmota will start as {our_setup_network(ctx)}, and make its own network by that name.")
        return False
    # Kept for the run: saved in the bridge, they outlast an upload cut off.
    ctx.state.set_input("tasmota_wifi_preset", True)
    ctx.state.set_input("tasmota_named_alone", False)
    say("    Tasmota will start with your Wi-Fi details, so it joins by itself.")
    return True


def tasmota_on_home_network(ctx: Context, *, timeout: float = 240.0) -> str | None:
    """Tasmota's address once it has started and joined, at the plug's own MAC."""
    say("Waiting for Tasmota to start and join your network...")
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
        status = Device(address).command("Status 5")
        name = status.get("StatusNET", {}).get("Hostname") if isinstance(status, dict) else None
    except (DeviceError, OSError):
        name = None
    if name and not name.startswith(SETUP_SSID_PREFIX):  # a name it keeps
        ctx.state.set_input("device_name", name)
    say(f"    Tasmota is on your network at {address}" + (f" as {name}." if name else "."))
    return address


def upload_tasmota(image: Path, host: str = setup_network.SETUP_HOST) -> None:
    say(f"    Sending Tasmota ({image.stat().st_size:,} bytes) - about a minute...")
    started = time.monotonic()
    try:
        setup_network.upload_to_bridge(image, host=host)
    except setup_network.SetupError as exc:
        raise Abort(f"{exc}. The plug still has the installer; run this again") from exc
    say(f"    Upload verified in {time.monotonic() - started:.0f}s; the plug is restarting into Tasmota.")


def tasmota_or_its_network(ctx: Context, *, timeout: float,
                           networks: tuple[str, ...] | None = None) -> tuple[str, str] | None:
    """("network", its name) once Tasmota's setup network shows - one of
    `networks`, setup_networks' without - or ("home", its address) if Tasmota
    is on the home network instead: with settings the bridge saved earlier, it
    joins by itself and raises none."""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        name = find_ssid(networks or setup_networks(ctx), timeout=8.0)
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
    say("")
    say("Waiting for Tasmota to start and make its setup network...")
    networks = setup_networks(ctx)
    if ctx.state.input("tasmota_named_alone"):
        # Named as the installer was, Tasmota's network has the installer's
        # name: looked for once the installer has gone, so that its network,
        # still listed for a moment, is not taken for Tasmota's.
        installer_gone(timeout=60.0)
        networks = (our_setup_network(ctx), *networks)
    found = tasmota_or_its_network(ctx, timeout=wait, networks=networks)
    if found and found[0] == "home":
        return {"plug_ip": found[1], "upload_route": route, "wifi": "preset",
                "device_name": ctx.state.input("device_name")}
    network = found[1] if found else None
    if network and network.startswith(TASMOTA_AP_PREFIX):
        note_setup_network(ctx, network)
        say(f"    This plug is {network} - the same name shows in your router.")
    ip = configure_plug_wifi(ctx, network)
    named = network if network != our_setup_network(ctx) else ctx.state.input("device_name")
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



def running_slot(device: Device, timeout: float = 60.0) -> int:
    """Which slot Tasmota runs from, once it can say: just after a restart its
    Berry is not up, and the slot reads as unknown (seen in the fault suite,
    taken for "not slot 1", and another upload sent into a plug starting)."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            device.wait_reachable(timeout=30)
            slot = device.current_ota()
        except (DeviceError, OSError):
            slot = None
        if slot is not None:
            return slot
        if time.monotonic() > deadline:
            raise Abort(f"the plug at {device.host} did not say which slot it runs from "
                        f"within {timeout:.0f}s")
        time.sleep(3)


def step_promote(ctx: Context) -> dict[str, Any]:
    """D4/D5 - move Tasmota to the high slot and freeze NVS."""
    device = ctx.device
    image = ROOT / "captures/safeboot-recovery-migration/tasmota32c3-bluetooth.bin"
    device.wait_reachable(timeout=120)
    # The first reading of a run from an ESPHome that gives none (no signal
    # sensor, an encrypted API; the startup firmware gives none either), and
    # another of every other run through here.
    ctx.state.capture("D4.signal_dbm", note_signal(ctx, plug_signal(device), [
        "From here the plug restarts several times, and each time it has to",
        "rejoin your Wi-Fi on its own; a weak signal makes that less certain."]))
    slot = running_slot(device)
    say(f"The plug is running from slot {slot}. The next steps need slot 1.")
    if slot == 1:
        device.command("SaveData 0")
        say("Already there. SaveData 0 - settings frozen so the migration's checks stay stable")
        return {"current_ota": slot, "uploads": 0}
    if ctx.dry_run:
        say("[dry-run] would upload the same image through Firmware Upgrade")
        return {"current_ota": slot, "uploads": 0}

    # Tried again after an upload that was cut off (the Wi-Fi going part way),
    # Tasmota still holds that upload, and the next one hangs without a word
    # until it gives up minutes later: a restart clears it.  It starts the same
    # slot - an upload switches only once it is whole.
    if ctx.state.status("D4") == FAILED:
        say("Restarting the plug first, to clear the upload that was cut off...")
        booted, uptime = device.started_at(), device.uptime_seconds()
        device.command("Restart 1")
        device.wait_for_reboot(timeout=180, started_at=booted, before=uptime)
        device.wait_reachable(timeout=120)

    # Every upload writes the other slot and boots it, so this alternates.
    # One swap is expected; a second is allowed in case the run started from an
    # odd position.  Anything beyond that is a real fault, not bad luck.
    uploads = 0
    for attempt in range(2):
        booted, uptime = device.started_at(), device.uptime_seconds()
        upload_or_prompt(ctx, image, "Berry Tasmota")
        uploads += 1
        say("Waiting for the plug to restart into the other slot...")
        device.wait_for_reboot(timeout=300, started_at=booted, before=uptime)
        slot = running_slot(device)
        say(f"Now running from slot {slot}.")
        if slot == 1:
            break
        if attempt == 0:
            say("Each upload writes the other slot and boots it, so that swap put")
            say("it back where it started. Doing it once more to land on slot 1.")
    after = slot
    if after != 1:
        raise Abort(
            f"the plug is still running from slot {after} after {uploads} uploads; "
            "it should alternate on every upload, so something is stopping the new "
            "image being selected"
        )
    device.command("SaveData 0")
    say("SaveData 0 - settings frozen so the migration's checks stay stable")
    return {"current_ota": after, "uploads": uploads}


def plug_back_on_wifi(ctx: Context) -> None:
    """Before E1 or E3 asks the plug anything: find it, or bring it back.

    A power cut while the commit writes can cost Tasmota its settings: it comes
    back on its old layout, asking to be set up on its own network.  Found on
    the network is not yet answering: a plug just switched back on answers
    ping a few seconds before its web server, so that is waited for too.
    """
    if not ctx.finder.poll():
        setup = find_ssid(setup_networks(ctx), timeout=15)
        if setup:
            tell(f"    The plug is not on your Wi-Fi: it is asking to be set up on {setup},")
            tell("    as Tasmota does when it has lost its settings - after a power cut, say.")
            configure_plug_wifi(ctx, setup)
        else:
            locate_plug(ctx, setup=True)
    ctx.device.wait_reachable(timeout=120)


def sonoff_layout_back(ctx: Context) -> None:
    """A commit stopped part way - by a power cut, say - leaves the plug on the
    interim layout, its settings moved aside (safeboot_migration says why).
    Every check and plan expects Sonoff's: put back first, then a restart."""
    if ctx.dry_run:
        return
    device = ctx.device
    try:
        table = device.sha_region(plans.TABLE_ADDRESS, plans.SECTOR, timeout=30)
    except (DeviceError, OSError):
        return  # no Berry to ask: the step's own checks say what is wrong
    if table != plans.INTERIM_TABLE_SHA256:
        return
    tell("    The last commit stopped part way, with the plug's settings moved aside;")
    tell("    putting them back first.")
    booted, uptime = device.started_at(), device.uptime_seconds()
    run_plan(ctx, plans.sonoff_layout_back(esphome.sonoff_table()))
    device.wait_for_reboot(timeout=300, started_at=booted, before=uptime)
    say("    The plug is back on Sonoff's layout, with its settings.")


def step_preflight(ctx: Context) -> dict[str, Any]:
    """E1 - read-only inspection.  Re-runnable at any time."""
    plug_back_on_wifi(ctx)
    sonoff_layout_back(ctx)
    ctx.wifi()  # read them off the plug first, so the check below compares the right network
    network = check_plug_network(ctx)
    code, captured = ctx.phase_runner().run("preflight")
    if code != 0:
        raise phase_failed("preflight", code, captured)
    return {"exit": code, **network}


def step_stage(ctx: Context) -> dict[str, Any]:
    """E2 - writes the inactive slot only.  Re-runnable; changes no boot state."""
    code, captured = ctx.phase_runner().run("stage")
    if code != 0:
        raise phase_failed("stage", code, captured)
    return {"exit": code}


def step_commit(ctx: Context) -> dict[str, Any]:
    """E3 - the single destructive operation, tried again only from the plug as
    it was before it (COMMIT_ATTEMPTS)."""
    # Already written: the plug now runs the startup firmware, which cannot
    # answer the checks below, so failing them would send the run back to E1
    # with no way forward.
    if commit_report(ctx) == "PASS":
        say("The plug already reported the new layout written and checked;")
        say("there is nothing to do again here.")
        return {"exit": 0, "already_written": True}
    # Stopped as the commit ran - its window closed, say - and finished by the
    # plug alone: it fetched the new layout and runs the startup firmware,
    # which starts only once that is in place. Its report never got here, and
    # a first try would fail its network check on the startup firmware (seen
    # in the fault suite), sending the run back to E1 with no way forward.
    if table_fetched(ctx.evidence) and plug_on_startup_firmware(ctx):
        say("The plug finished the commit after this computer's run was stopped: it")
        say("fetched the new layout, and runs the startup firmware now.")
        return {"exit": 0, "finished_unseen": True}

    for attempt in range(1, COMMIT_ATTEMPTS + 1):
        ctx.state.capture("E3.attempts", attempt)
        try:
            if attempt > 1 and commit_recovered(ctx):
                say("The new layout is written: the plug finished the commit before it was lost.")
                return {"exit": 0, "finished_unseen": True}
            return commit_once(ctx)
        except (Abort, PhaseError, DeviceError, OSError) as exc:
            # OSError: the plug not answering while it is brought back, which
            # is the network, as TRANSIENT_RE says - not a reason to stop.
            why = commit_retry_reason(ctx, exc)
            if why is None or attempt == COMMIT_ATTEMPTS:
                raise
            tell(f"    {why} Putting the plug back as it was and trying again "
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
    if isinstance(exc, PhaseError) or TRANSIENT_RE.search(text):
        return "The plug stopped part way - a power cut, or its Wi-Fi going."
    return None


def plug_on_startup_firmware(ctx: Context) -> bool:
    """Whether the plug runs the startup firmware now, once it is back."""
    try:
        plug_back_on_wifi(ctx)
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
    wait_while_plug_busy(ctx)  # still finishing the commit this computer lost
    plug_back_on_wifi(ctx)
    if commit_report(ctx) == "PASS" or ctx.device.is_safeboot():
        return True
    for step, handler in (("E1", step_preflight), ("E2", step_stage)):
        say(f"{step} again, before the commit is tried again")
        ctx.state.mark(step, DONE, result=handler(ctx))
    return False


def commit_once(ctx: Context) -> dict[str, Any]:
    """One try of the commit, from a plug its checks and staging just saw."""
    # E1 and E2 pinned the bundle as it was then.  If it has been rebuilt since,
    # or the plug has moved networks, send the run back to E1 rather than fail
    # inside the commit server with nothing the operator can act on.
    def staged_bundle_is_current() -> bool:
        try:
            report = json.loads((ctx.evidence / "stage-report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return report.get("manifest_sha256") == sha256_bytes(ctx.manifest_path.read_bytes())

    plug_back_on_wifi(ctx)
    sonoff_layout_back(ctx)
    try:
        check_plug_network(ctx)  # may rebuild the bundle
        if not staged_bundle_is_current():
            raise Abort(
                "the bundle has changed since the checks and staging were done, so "
                "they need to run again - run the same command. Nothing was written"
            )
    except Abort:
        for step in ("E1", "E2"):
            ctx.state.mark(step, FAILED, error="redo: bundle or network changed before E3")
        raise
    section("Rewriting the plug's memory layout", quiet=True, body=[
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
    say_lock_back("REPARTITION_LOCK")
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
        raise CommitFailed(str(phase_failed("commit", code, captured)), captured)
    say("The new layout is written. The plug restarts by itself, and the run carries on.")
    return {"exit": code}


# An upload making no progress for this long has stalled: official Tasmota takes
# well under a minute, even on a busy plug.
UPLOAD_STALL = 120.0


# Tries at the upload, as the commit and the installer's sends are tried.
UPLOAD_ATTEMPTS = 3


def upload_to_startup_firmware(ctx: Context, app: Path) -> str:
    """Official Tasmota to the startup firmware (F2); a stalled upload is tried
    again from a restart, up to UPLOAD_ATTEMPTS in all, before it is left to
    the person.

    Seen once in the fault matrix: an upload began and never finished, the
    startup firmware neither taking it nor saying why.  A restart clears that,
    and is safe here: until a whole image is taken, the plug starts the startup
    firmware again."""
    device = ctx.device
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        started = time.monotonic()
        try:
            device.upload_firmware(app, timeout=UPLOAD_STALL)
        except Exception as exc:  # noqa: BLE001 - tried again, then the person
            if attempt == UPLOAD_ATTEMPTS:
                return upload_or_prompt_by_hand(ctx, app, "Official Tasmota (app0)", exc)
            what_happened = (f"The plug refused the upload ({exc.reason})" if isinstance(exc, UploadRefused)
                             else f"The upload stopped ({exc})")
            tell(f"    {what_happened}; restarting the plug and trying again "
                 f"(attempt {attempt + 1} of {UPLOAD_ATTEMPTS}).")
            try:
                device.command("Restart 1", timeout=10)
            except (DeviceError, OSError):
                tell("    The plug is not answering: switch it off and on - at this stage that is")
                tell("    safe, it starts the startup firmware again - and this carries on.")
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
        say(f"Official Tasmota (app0) uploaded in {seconds}s")
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
                say(f"    Recovery firmware is up at {device.host} "
                    f"({version or 'safeboot'}, after {time.monotonic() - started:.0f}s).")
                return True
        if time.monotonic() - window >= within:
            return False
        time.sleep(5)


def step_canonical(ctx: Context) -> dict[str, Any]:
    """F - first canonical boot, then the official application."""
    app = ROOT / "captures/safeboot-recovery-migration/tasmota32c3.bin"
    if run_source(ctx.state) == "committed" and ctx.finder.poll() and not ctx.device.is_safeboot():
        # A run stopped after official Tasmota went on, before it was set up.
        # Only on this route: after E3 the interim Tasmota still answers for a
        # moment, which this would take for the official one.
        version = ctx.device.firmware_version() or ""
        if version and "bluetooth" not in version:
            say(f"Official Tasmota is installed already ({version}).")
            return {"already": version}
    section("The plug has restarted with its new layout", quiet=True, body=[
        "It is now running a small recovery firmware whose only job is to accept",
        "the real Tasmota. It joins your Wi-Fi by itself.",
    ])
    # Answering a ping proves nothing here: the old firmware is still up for a
    # second or two after Restart 99.  Wait for the firmware itself to change.
    say("")
    say("Waiting for the plug to restart into the recovery firmware.")
    time.sleep(10)
    started = time.monotonic()
    while not recovery_firmware_up(ctx, started, within=300):
        # Five minutes and no sign of it: keep waiting, or look elsewhere.
        output.transient_begin()
        choice = ask_choice(
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
            ctx.set_plug_ip(ask_ipv4("What address is it on now", must_respond=True))
        output.transient_end()
    waited = time.monotonic() - started
    device = ctx.device
    saved = startup_settings_report(device)
    if ctx.dry_run:
        say("[dry-run] would upload the official application")
        return {"safeboot_seconds": round(waited, 1), "startup_settings": saved}
    how, outcome = install_official(ctx, app)
    return {"safeboot_seconds": round(waited, 1), "app_upload": how, "outcome": outcome,
            "startup_settings": saved}


def install_official(ctx: Context, app: Path) -> tuple[str, str]:
    """Official Tasmota uploaded to the startup firmware, and seen running -
    sent again when the plug comes back on the startup firmware instead (the
    upload did not take), up to UPLOAD_ATTEMPTS in all."""
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        how = upload_to_startup_firmware(ctx, app)
        outcome = wait_for_official(ctx)
        if outcome != "startup firmware":
            return how, outcome
        if attempt < UPLOAD_ATTEMPTS:
            tell("    The plug came back on the startup firmware: the upload did not take. "
                 f"Sending it again (attempt {attempt + 1} of {UPLOAD_ATTEMPTS}).")
    raise Abort("official Tasmota did not take: the plug came back on its startup firmware "
                f"after {UPLOAD_ATTEMPTS} uploads")


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
    say("")
    say("Waiting for the full Tasmota to start. It will either rejoin your")
    say("Wi-Fi, or put up its own setup network - both are normal.")
    started = time.monotonic()
    while time.monotonic() - started < 300:
        time.sleep(5)
        if ctx.finder.poll():
            try:
                version = ctx.device.firmware_version()
            except (DeviceError, OSError):
                version = ""
            if version and "safeboot" not in version.lower():
                say(f"    The plug is back at {ctx.device.host} running the full Tasmota ({version}).")
                return "rejoined the network"
            if "safeboot" in version.lower() and time.monotonic() - started > STILL_STARTUP_AFTER:
                return "startup firmware"
            continue
        setup = find_ssid(setup_networks(ctx), timeout=10)
        if setup:
            say(f"    The plug has started and is asking to be set up on {setup}.")
            note_setup_network(ctx, setup)
            return f"raised its setup network {setup}"
    raise Abort(
        "the plug neither rejoined the network nor put up a setup network "
        f"({setup_network_hint(ctx)}) within five minutes"
    )


# How the startup firmware begins what it logs of that; the second, before it
# was renamed - on a plug a run from an older checkout left part way.
STARTUP_LOG_PREFIXES = ("Autoflash recovery:", "S60 recovery:")


def startup_settings_report(device: Device) -> str | None:
    """What the startup firmware logged about saving the Wi-Fi for Tasmota.

    Kept in the run's record either way, so a fall back to the setup network
    at F4 can be explained.  None if its log could not be read or says nothing.
    """
    try:
        lines = device.console_log(timeout=10.0)
    except (DeviceError, OSError, ValueError) as exc:
        say(f"    Could not read the recovery firmware's log ({exc}).")
        return None
    notes = [(line[line.index(prefix):], prefix) for line in lines
             for prefix in STARTUP_LOG_PREFIXES if prefix in line]
    if not notes:
        say("    The recovery firmware's log does not say whether it saved your Wi-Fi.")
        return None
    note, prefix = notes[-1]
    if "saved for the next firmware" in note:
        say("    It has saved your Wi-Fi where official Tasmota reads it, so Tasmota")
        say("    should join your network by itself.")
    elif "left alone" in note:
        say("    Tasmota settings from before were still there; it left them as they were.")
    else:
        say(f"    It could not save your Wi-Fi for official Tasmota: {note[len(prefix):].strip()}.")
        say("    Tasmota will ask to be set up through its own network instead, as before.")
    return note




def step_configure(ctx: Context) -> dict[str, Any]:
    """F3/F4 - Wi-Fi, settings persistence and the S60 template."""
    if ctx.finder.poll():
        route = "joined by itself"
        say("Official Tasmota joined your Wi-Fi by itself, with the details the")
        say("startup firmware saved - no setup network needed.")
    else:
        setup = find_ssid(setup_networks(ctx), timeout=15)
        if setup:
            route = "setup network"
            tell("    Official Tasmota did not join your Wi-Fi by itself, so it is set up")
            tell("    through its own network instead.")
            configure_plug_wifi(ctx, setup)
        else:
            route = "located"
            locate_plug(ctx, setup=True)
    device = ctx.device
    device.wait_reachable(timeout=300)
    if device.is_safeboot():
        # F2 saw it as done, but official Tasmota did not take (a run from
        # before F2 checked for that): sent again, as F2 would.
        tell("    The plug is still on its startup firmware: official Tasmota is sent again.")
        install_official(ctx, ROOT / "captures/safeboot-recovery-migration/tasmota32c3.bin")
        device = ctx.device
        device.wait_reachable(timeout=300)
    template = s60_template(ctx.state.input("product_model"))
    if s60_settings_in_place(device, template):
        # Tried again after the plug took the template but its answer was slow
        # (a busy plug): resetting again would only restart it again.
        say("The S60 settings are in place already.")
        return {"template": "applied", "wifi_route": route}
    # Tasmota may be running on the record the startup firmware saved, which
    # holds the Wi-Fi but only what a Safeboot sets (it skips most defaults,
    # never expecting to save them).  Reset 4 rebuilds every setting from this
    # firmware's own defaults and keeps the Wi-Fi.  Harmless when Tasmota
    # started from defaults anyway.
    booted, uptime = device.started_at(), device.uptime_seconds()
    device.command("Reset 4", timeout=SLOW_COMMAND)
    say("Settings reset to Tasmota's defaults, keeping the Wi-Fi; the plug restarts")
    back_after_reset(ctx, booted, uptime)
    device = ctx.device
    booted, uptime = device.started_at(), device.uptime_seconds()
    device.command("SaveData 1", timeout=SLOW_COMMAND)
    device.command(f"Template {template}", timeout=SLOW_COMMAND)
    device.command("Module 0", timeout=SLOW_COMMAND)
    say(f"Template applied as {json.loads(template)['NAME']}; the plug restarts")
    device.wait_for_reboot(timeout=180, started_at=booted, before=uptime)
    # Restarted is not yet answering: the next step asks the plug at once.
    device.wait_reachable(timeout=120)
    return {"template": "applied", "wifi_route": route}


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
    setup = find_ssid(setup_networks(ctx), timeout=30)
    if not setup:
        raise Abort(
            "the plug did not come back after its settings were reset, and its own "
            f"network ({setup_network_hint(ctx)}) is not to be seen either. Switch it "
            "off and on, then run this again")
    tell("    The plug restarted without its Wi-Fi details - cut off just after the")
    tell("    reset, say - and raised its own network, so it is set up through that.")
    configure_plug_wifi(ctx, setup)
    ctx.device.wait_reachable(timeout=300)


# Commands that rewrite Tasmota's settings answer slowly on a busy plug: given
# longer than the usual few seconds before taken as not answering.
SLOW_COMMAND = 30.0


def s60_settings_in_place(device: Device, template: str) -> bool:
    """Whether F4 has done its work already: the S60's template in use, and
    settings saved."""
    try:
        current = device.command("Template", timeout=SLOW_COMMAND)
        module = device.command("Module", timeout=SLOW_COMMAND)
        saving = device.command("SaveData", timeout=SLOW_COMMAND)
    except (DeviceError, OSError):
        return False
    name = json.loads(template)["NAME"]
    return (isinstance(current, dict) and current.get("NAME") == name
            and isinstance(module, dict) and "0" in (module.get("Module") or {})
            and isinstance(saving, dict) and saving.get("SaveData") in ("ON", "1", 1))



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

    details = None if getattr(ctx.args, "wifi", False) else read_wifi_from_plug(ctx)
    image = proven(*details) if details else None
    if image is None:
        output.transient_begin()  # until details that match are given
        section("The Wi-Fi details the plug was converted with are needed", [
            *(["The plug is on different Wi-Fi details now."] if details else []),
            "Its temporary startup firmware was built with the Wi-Fi details",
            "used during the conversion, and only those rebuild it exactly, so",
            "it can be swapped for the official one. They are checked against",
            "the plug's record before anything is used.",
        ])
        while image is None:
            ssid = ask_text("Wi-Fi network name used during the conversion", private="wifi-name")
            password = ask_secret(f"Wi-Fi password for '{ssid}' at the time: ")
            output.private(password, "wifi-password")
            details = (ssid, password)
            image = proven(ssid, password)
            if image is None:
                tell("    Those are not the details the plug was converted with - try again.")
        output.transient_end()
    ssid, password = details
    try:
        recovery_image(ctx.manifest_path, ssid, password)
    except BundleError:
        say(f"Setting the firmware bundle back up for '{ssid}', the Wi-Fi the plug was converted with.")
        try:
            pin_wifi(ctx.manifest_path, ssid, password)
        except BundleError as exc:
            raise Abort(f"the bundle could not be set up for '{ssid}': {exc}") from exc
    ctx.remember_wifi(ssid, password, image)


def step_restore(ctx: Context) -> dict[str, Any]:
    """G - replace the private recovery Safeboot with official bytes."""
    conversion_wifi(ctx)  # before the runner, which would otherwise pin the plug's own
    runner = ctx.phase_runner()
    say("== Restore official Safeboot")

    def gate(_: str) -> bool:
        say("Swapping the temporary startup firmware - the one with your Wi-Fi")
        say("details in it - for the official one. The plug keeps running throughout.")
        try:
            matches, problem = verify_app(ctx)
        except (DeviceError, OSError, ValueError) as exc:
            matches, problem = False, f"the check could not run: {exc}"
        if matches:
            say("  Checked first: the plug is running the official Tasmota, unmodified.")
            return True
        tell(f"    Stopping: {problem}. Swapping the startup firmware is only safe")
        tell("    under the official Tasmota, so nothing was written.")
        return False

    code, captured = runner.run(
        "restore", extra_flags=["--i-confirm-normal-app-is-stable"], confirm=gate
    )
    if code != 0:
        raise phase_failed("restore", code, captured)
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
    results: dict[str, Any] = {}
    ok = True
    for name, (address, length, wanted) in expected.items():
        actual = device.sha_region(address, length)
        matched = actual == wanted.lower()
        ok = ok and matched
        results[name] = {"expected": wanted, "actual": actual, "match": matched}
        report = tell if not matched else say
        report(f"  {name:<9} 0x{address:06X} {length:>9,}  {'MATCH' if matched else 'DIFFERS'}")
        if not matched:
            tell(f"    expected {wanted}")
            tell(f"    actual   {actual}")
    if not ok:
        raise Abort("device does not match the pinned artifacts")
    say("")
    say("All four regions match the pinned artifacts.")
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
    if ctx.state.is_done("E3") or commit_report(ctx) == "PASS":
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


def step_esphome_install(ctx: Context) -> dict[str, Any]:
    """I1 - ESPHome into ota_0 from the temporary Tasmota, every block read back.

    Like staging Safeboot, it writes only the slot Tasmota does not run from,
    and the partition table is left as Sonoff made it.  Only once the whole
    image has been read back and matched does the plug erase its boot
    selection and restart, which starts ESPHome (the esphome phase).
    """
    host = ctx.state.input("plug_ip")
    if host and esphome.is_esphome(host):
        say("The plug already runs ESPHome; there is nothing to install again.")
        return {"already_installed": True}
    ssid, password = ctx.wifi()
    # Read off the temporary Tasmota, as E1 reads it on the way to Tasmota:
    # once ESPHome is written, it has to join the Wi-Fi on its own.
    signal = note_signal(ctx, plug_signal(ctx.device), [
        "Once ESPHome is written, it has to join",
        "your Wi-Fi on its own, and a weak signal makes that less certain."])
    try:
        image = esphome.image_for(ssid, password, own=ctx.esphome_firmware)
    except (esphome.ImageError, imprint.ImprintError, OSError) as exc:
        raise Abort(f"the ESPHome image cannot be used: {exc}") from exc
    name = ctx.esphome_firmware.name if ctx.esphome_firmware else esphome.IMAGE.name
    section("Installing ESPHome", quiet=True, body=[
        f"{name}, with your Wi-Fi written in (in memory only), goes into the half",
        "of the plug's memory Tasmota is not running from. Every block is read back",
        "and checked against the image; only then does the plug switch to it and",
        "restart. Its memory layout stays as Sonoff made it.",
    ])
    if ctx.dry_run:
        say(f"[dry-run] would install {name}")
        return {"dry_run": True, "image": name}
    return run_plan(ctx, plans.esphome(image)) | {"image": name, "size": len(image),
                                                   "signal_dbm": signal}


def run_plan(ctx: Context, plan: plans.Plan) -> dict[str, Any]:
    """Have the plug carry out `plan` (tools/autoflash/plans.py)."""
    if ctx.dry_run:
        say(f"[dry-run] would write {plan.title}: {plan.summary()}")
        return {"dry_run": True, "plan": plan.name}
    code, captured = ctx.phase_runner().run("plan", plan=plan)
    if code != 0:
        raise phase_failed(f"plan {plan.name}", code, captured)
    return {"plan": plan.name, "steps": len(plan.steps)}


def plug_runs_tasmota(state: RunState) -> bool:
    """Whether the plug runs a Tasmota at this point of its run: everywhere but
    on a route from ESPHome before X2, and after the plug has left Tasmota."""
    source = run_source(state)
    if source not in ("tasmota", "esphome"):
        return True
    if source == "tasmota" and not state.is_done("L1"):
        return True
    return state.is_done("X2") and not state.is_done("I1") and not state.is_done("R1")


def consent_to_leave(ctx: Context, what: str) -> None:
    """The go-ahead, once, before the first write of a run from Tasmota or ESPHome."""
    output.transient_begin()
    warning_box("Before going on", [
        f"From here the plug's {what} is replaced. DO NOT UNPLUG IT during the",
        "process. Changing firmware rewrites the memory map and the bootloader",
        "for a moment each, when a power cut would leave a plug that has to be",
        "opened to repair.",
        "Only go ahead with a plug you can afford to lose",
    ])
    if not require_word(CONSENT_WORD):
        raise Abort("not confirmed - nothing has been written to the plug")
    output.transient_end()


def wait_for(ctx: Context, check: Callable[[str], Any], what: str, timeout: float = 240.0) -> Any:
    """Wait for `check(address)` to answer, finding the plug again by its
    hardware address if it came back on a new address."""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        host = ctx.state.input("plug_ip")
        found = host and check(host)
        if found:
            output.progress_clear()
            return found
        moved = ctx.finder.poll()
        if moved and moved != host:
            continue
        output.progress(f"waiting for {what}... {time.monotonic() - started:.0f}s")
        time.sleep(3)
    output.progress_clear()
    raise Abort(f"{what} did not answer within {timeout / 60:.0f} minutes")


def step_leave_tasmota(ctx: Context) -> dict[str, Any]:
    """L1 - from Tasmota's layout to Sonoff's, the mover started there.

    Official Tasmota runs across both of Sonoff's app slots, so it cannot write
    a firmware into either; the mover is small enough to go in front of it.
    The plan writes ESPHome's bootloader, the mover, clears where Sonoff's
    layout keeps its settings and boot selection, and writes Sonoff's
    partition table last - then the plug restarts into the mover.
    """
    host = ctx.state.input("plug_ip")
    if host and esphome_ota.probe(host) is not None and not esphome.is_esphome(host):
        say("The plug already runs the mover.")
        return {"already": True}
    ssid, password = ctx.wifi()
    # The earliest reading of a run from Tasmota, before it makes way: the
    # mover, and each firmware after it, has to join the Wi-Fi on its own.
    signal = note_signal(ctx, plug_signal(ctx.device), [
        "After this stage the mover, and each firmware after it, has",
        "to join your Wi-Fi on its own, and a weak signal makes that less certain."])
    consent_to_leave(ctx, "Tasmota")
    try:
        plan = plans.leave_tasmota(esphome.mover_for(ssid, password), esphome.sonoff_table(),
                                   esphome.published_bootloader())
    except (esphome.ImageError, imprint.ImprintError, plans.PlanError, OSError) as exc:
        raise Abort(f"the mover cannot be used: {exc}") from exc
    try:
        result = run_plan(ctx, plan)
    except Abort:
        # Sonoff's table is the plan's last write, there only if every write
        # before it read back right: with it in place, all that is missing is
        # the restart into the mover.  Not left waiting for one.
        if not table_written(ctx):
            raise
        say("    Everything was written before the plan stopped; restarting the plug into the mover.")
        ctx.device.command("Restart 99")
        result = {"plan": plan.name, "restarted_after": "a stopped report"}
    if not ctx.dry_run:
        wait_for_mover(ctx, "the plug to start the mover")
        say("    The plug is running the mover, on Sonoff's memory layout.")
    return result | {"signal_dbm": signal}


# Sending Wi-Fi details through a plug's own network: tried this many times
# while this computer is still joined to it, as other sends to the plug are.
SETUP_SEND_ATTEMPTS = 3


def on_a_plug_network() -> bool:
    """Whether this computer is on a plug's own network now - joined to it
    already, before being asked, say: something answers at its address."""
    return setup_network.page(setup_network.SETUP_HOST, "/", timeout=2.0) is not None


def send_through_own_network(network: str, ssid: str, password: str) -> None:
    """ESPHome's (or the mover's) page given the details, up to
    SETUP_SEND_ATTEMPTS times, once this computer has joined its network."""
    section("Join the plug's own network", [
        f"In {hostsys.wifi_menu_hint()}, join:",
        f"    {network}      (open - no password)",
        "",
        "If a page opens by itself, close it: this sends the details for you.",
    ])
    for attempt in range(1, SETUP_SEND_ATTEMPTS + 1):
        wait_until_joined(network)
        try:
            setup_network.send_esphome_wifi(ssid, password)
        except setup_network.SetupError as exc:
            if attempt == SETUP_SEND_ATTEMPTS:
                raise Abort(f"the Wi-Fi details could not be sent to the plug: {exc}") from exc
            tell(f"    {exc}; sending them again (attempt {attempt + 1} of {SETUP_SEND_ATTEMPTS}).")
            time.sleep(5)
            continue
        tell(f"    Sent. The plug joins '{ssid}' now.")
        return


def set_up_through_own_network(ctx: Context, what: str, check: Callable[[str], Any],
                               networks: tuple[str, ...]) -> tuple[str, str] | None:
    """A plug on ESPHome (or the mover) that cannot join the home network, set
    up through its own: it raises one after a while away from the home one,
    whose page takes Wi-Fi details. The details this run has go first - right
    after a router outage, say - and new ones are asked for only if the plug
    does not join with them. `networks`: what its network can be called, its
    name now first. The details it joined with, or None when its network is
    not to be seen."""
    network = find_ssid(networks, timeout=30)
    if not network and on_a_plug_network():
        network = networks[0]  # joined already: carried on through it
    if not network:
        return None
    tell(f"    {what} cannot join your Wi-Fi, so it has raised its own network, {network}.")
    ssid, password = ctx.wifi(why=[
        f"{what} cannot join your Wi-Fi, and is waiting on its own network for its",
        "details. Type them here and this sends them to the plug for you.",
    ])
    for round_ in (1, 2):
        send_through_own_network(network, ssid, password)
        section("Back to your usual Wi-Fi", [
            "Your computer should rejoin your usual Wi-Fi by itself; if it doesn't,",
            "join it now.",
        ])
        try:
            wait_for(ctx, check, what, timeout=300.0)
            return ssid, password
        except Abort:
            if round_ == 2 or prompts.UNATTENDED:
                raise Abort(f"{what} did not join '{ssid}' with the details sent through its own "
                            "network. Check the network's name and password, then run this again")
        tell(f"    {what} did not join '{ssid}' with those details: most likely your Wi-Fi's name")
        tell("    or password changed since the plug last joined it.")
        output.transient_begin()
        section("The Wi-Fi details the plug should use", [
            "Type the name and password of the 2.4 GHz network the plug should be on",
            "now. They are sent to the plug through its own network.",
        ])
        ssid, password = ask_credentials()
        output.transient_end()
    raise AssertionError("unreachable")


def wait_for_mover(ctx: Context, what: str) -> bool:
    """The mover on the home network - or, when it cannot join that, set up
    through its own (set_up_through_own_network). True when it was given other
    details than the run had, which ESPHome then needs too."""
    def mover(address: str) -> Any:
        return esphome_ota.probe(address)

    if not on_a_plug_network():
        try:
            wait_for(ctx, mover, what)
            return False
        except Abort:
            pass
    before = ctx._wifi
    joined = set_up_through_own_network(ctx, what, mover, mover_networks(ctx))
    if joined is None:
        raise Abort(
            f"{what} did not answer on your Wi-Fi, and its own network "
            f"({mover_network_hint(ctx)}) is not to be seen either. If your Wi-Fi is "
            "on, switch the plug off and on: two minutes after it cannot join your "
            "Wi-Fi, it raises its own network, and running this again sets it up "
            "through that")
    if joined != before:
        ctx.adopt_wifi(*joined)
        return True
    return False


def table_written(ctx: Context) -> bool:
    try:
        return ctx.device.sha_region(plans.TABLE_ADDRESS, plans.SECTOR, timeout=30) == plans.SONOFF_TABLE_SHA256
    except (DeviceError, OSError):
        return False


def send_as_esphome_update(ctx: Context, image: bytes, what: str) -> float:
    """`image` as an ordinary ESPHome update, to ESPHome or the mover."""
    host = ctx.state.input("plug_ip")
    started = time.monotonic()

    def progress(sent: int, total: int) -> None:
        output.progress(f"sending {what}: {100 * sent // total}%")

    # Asked before the go-ahead (unlock_esphome); here again only for a plug
    # that changed since, or a run sending it once more.
    while True:
        try:
            esphome_ota.upload(host, image, progress=progress, password=ctx.esphome_password,
                               key=ctx.esphome_key)
            break
        except esphome_ota.NeedsPassword:
            output.progress_clear()
            ask_esphome_password(ctx, host)
        except (esphome_ota.NeedsEncryption, esphome_ota.WrongKey):
            output.progress_clear()
            ask_esphome_key(ctx, host)
        except (esphome_ota.OTAError, OSError) as exc:
            raise Abort(f"sending {what} to the plug failed: {exc}") from exc
        finally:
            output.progress_clear()
        started = time.monotonic()  # the time asked is not the time sending
    return round(time.monotonic() - started, 1)


def step_esphome_from_mover(ctx: Context) -> dict[str, Any]:
    """M1 - ESPHome, sent to the mover as an ESPHome update: it writes it into
    the other slot, checks it, and starts it."""
    host = ctx.state.input("plug_ip")
    if host and esphome.is_esphome(host):
        say("The plug already runs ESPHome.")
        return {"already": True}
    ssid, password = ctx.wifi()
    try:
        image = esphome.image_for(ssid, password, own=ctx.esphome_firmware)
    except (esphome.ImageError, imprint.ImprintError, OSError) as exc:
        raise Abort(f"the ESPHome image cannot be used: {exc}") from exc
    if ctx.dry_run:
        return {"dry_run": True}
    if wait_for_mover(ctx, "the mover"):
        # Given new Wi-Fi details on its own network: ESPHome gets those too.
        ssid, password = ctx.wifi()
        try:
            image = esphome.image_for(ssid, password, own=ctx.esphome_firmware)
        except (esphome.ImageError, imprint.ImprintError, OSError) as exc:
            raise Abort(f"the ESPHome image cannot be used: {exc}") from exc
    seconds = send_as_esphome_update(ctx, image, "ESPHome")
    say(f"    ESPHome sent in {seconds}s; the plug restarts into it.")
    return {"seconds": seconds, "size": len(image)}


def step_startup_from_esphome(ctx: Context, *, again: bool = False) -> dict[str, Any]:
    """X1 - the startup firmware, with the Wi-Fi written in, sent to ESPHome (or
    the mover) as an ESPHome update.  It joins the Wi-Fi by itself, and saves
    the details where Tasmota reads them.  `again`: sent once more by X2, the
    go-ahead given already."""
    host = ctx.state.input("plug_ip")
    if run_source(ctx.state) in ("esphome", "midway") and not again:
        unlock_esphome(ctx)
        # The earliest reading of a run from ESPHome, before anything is
        # written: from its Wi-Fi signal sensor (ours from 1.1.0), else from
        # its native API - encrypted, given its key (ours, 1.0.0 too, has
        # none). From an ESPHome that gives neither, the first is at D4.
        ctx.state.capture("X1.signal_dbm", note_signal(
            ctx, esphome.signal(host, ctx.esphome_key) if host else None, [
                "Nothing has been written yet. From here the plug restarts several",
                "times, and each time it has to rejoin your Wi-Fi on its own; a weak",
                "signal makes that less certain."]))
        consent_to_leave(ctx, "ESPHome" if run_source(ctx.state) == "esphome" else "firmware")
    running = identify(ctx, host) if host else None
    if running in ("startup", "hub"):
        say("The plug is already past this step.")
        return {"already": running}
    ssid, password = ctx.wifi(why=[
        "ESPHome cannot say what its Wi-Fi password is, and the firmware that",
        "goes on next needs it written in, to join your Wi-Fi by itself.",
    ])
    try:
        image = startup_image(ssid, password)
    except BundleError as exc:
        raise Abort(f"the startup firmware cannot be prepared: {exc}") from exc
    if ctx.dry_run:
        return {"dry_run": True}
    wait_for(ctx, lambda address: esphome_ota.probe(address), "ESPHome's update service")
    seconds = send_as_esphome_update(ctx, image, "the startup firmware")
    wait_for(ctx, lambda address: Device(address, timeout=4).is_safeboot(),
             "the startup firmware on your Wi-Fi")
    say(f"    The startup firmware is on your Wi-Fi (sent in {seconds}s).")
    return {"seconds": seconds}


def step_berry_from_startup(ctx: Context) -> dict[str, Any]:
    """X2 - the Berry Tasmota, uploaded to the startup firmware, which writes it
    into the other slot; it starts with the Wi-Fi the startup firmware saved."""
    image = ROOT / "captures/safeboot-recovery-migration/tasmota32c3-bluetooth.bin"
    if ctx.dry_run:
        return {"dry_run": True}
    running = identify(ctx, ctx.device.host)
    if running == "hub":
        say("The plug already runs Tasmota.")
        return {"already": True}
    if running in ("mover", "esphome", "esphome-login"):
        # The startup firmware confirms itself only once it has been used: a
        # plug switched off and on before then starts the firmware it ran
        # before (seen in the fault suite, a power cut early in X2).
        tell("    The plug is back on the firmware it ran before - switched off and on")
        tell("    before the startup firmware was settled in - so that is sent again.")
        step_startup_from_esphome(ctx, again=True)
    how = upload_or_prompt(ctx, image, "Berry Tasmota")

    def berry(address: str) -> bool:
        # Tasmota only once it says so: the page not answering, as while the
        # plug restarts, is no sign of it.
        try:
            version = Device(address, timeout=4).firmware_version()
        except (DeviceError, OSError):
            return False
        return bool(version) and "safeboot" not in version.lower()

    wait_for(ctx, berry, "Tasmota to start and join your Wi-Fi")
    say("    Tasmota is running and on your Wi-Fi.")
    return {"upload": how}


def step_bootloader(ctx: Context) -> dict[str, Any]:
    """D5 - ESPHome's bootloader, written once for every firmware: it starts
    Tasmota, ESPHome and Sonoff's firmware alike, and rolls back an update
    that fails to start.  Skipped when it is already in place."""
    sonoff_layout_back(ctx)
    return run_plan(ctx, plans.bootloader(esphome.published_bootloader()))


def sonoff_backup(ctx: Context) -> bytes:
    """The plug's own Sonoff backup, checked to be this plug's."""
    mac = ctx.state.input("plug_mac")
    backup = plugs.find_backup(mac) if mac else None
    if backup is None:
        raise Abort("there is no backup of this plug's Sonoff firmware on this computer")
    try:
        return (backup.directory / "flash.bin").read_bytes()
    except OSError as exc:
        raise Abort(f"the backup could not be read: {exc}") from exc


def step_sonoff_restore(ctx: Context) -> dict[str, Any]:
    """R1 - the plug's own Sonoff firmware and settings, from its backup, beside
    the Tasmota it runs; the boot selection set to it last, then a restart.
    Two plans, as Sonoff's firmware is more than one may write."""
    try:
        first, second = plans.back_to_sonoff(sonoff_backup(ctx), running=1)
    except plans.PlanError as exc:
        raise Abort(f"the backup cannot be written back: {exc}") from exc
    run_plan(ctx, first)
    return run_plan(ctx, second)


def sonoff_answers(address: str) -> bool:
    return detect.identify(address, timeout=3.0) == "sonoff"


def step_sonoff_check(ctx: Context, wait: float = 240.0) -> dict[str, Any]:
    """R2 - Sonoff's firmware started and back on the Wi-Fi it had."""
    if ctx.dry_run:
        return {"dry_run": True}
    started = time.monotonic()
    try:
        wait_for(ctx, sonoff_answers, "Sonoff's firmware on your Wi-Fi", timeout=wait)
    except Abort as exc:
        raise Abort(
            "Sonoff's firmware was written and checked, but has not answered on your Wi-Fi. "
            "If the network dropped while it first started, it does not try again: switch "
            "the plug off and on, then run the same command again to check it. It rejoins the "
            "network it was on when the backup was taken; if that has changed, add it again "
            "in the eWeLink app"
        ) from exc
    seconds = round(time.monotonic() - started, 1)
    say(f"    Sonoff's firmware answered after {seconds}s.")
    return {"seconds": seconds}


# ESPHome installed as an update (to the mover) confirms itself only after a
# minute running (safe_mode's boot_is_good_after); restarted before then, the
# bootloader takes the plug back to what it ran before.
ESPHOME_CONFIRMS_AFTER = 70.0


def step_esphome_check(ctx: Context, wait: float = 180.0) -> dict[str, Any]:
    """I2 - ESPHome started from what was written, and joined the home Wi-Fi.

    ESPHome sent to the mover as an update (from Tasmota) is not confirmed
    until it has run a minute: a power cut before then takes the plug back to
    the mover, which is then sent ESPHome again.  So it is waited out, and the
    plug is only done once ESPHome is there for good.
    """
    host = ctx.state.input("plug_ip")
    if not host:
        raise Abort("the plug's address is not known")
    if ctx.dry_run:
        say("[dry-run] would wait for ESPHome to answer")
        return {"dry_run": True}
    as_update = run_source(ctx.state) == "tasmota"
    say("Waiting for the plug to start ESPHome and join your Wi-Fi...")
    started = time.monotonic()
    first_seen = None
    resent = 0
    while time.monotonic() - started < wait + ESPHOME_CONFIRMS_AFTER * as_update:
        host = ctx.state.input("plug_ip")
        if esphome.is_esphome(host):
            if first_seen is None:
                first_seen = time.monotonic()
                say(f"    ESPHome answered at {host} after {first_seen - started:.0f}s.")
                if as_update:
                    say("    Waiting a minute while it confirms itself, so a power cut now")
                    say("    cannot take it back...")
            if not as_update or time.monotonic() - first_seen >= ESPHOME_CONFIRMS_AFTER:
                return {"seconds": round(first_seen - started, 1), "resent": resent}
        elif as_update and esphome_ota.probe(host) is not None:
            # Back on the mover: ESPHome restarted before it had confirmed itself.
            first_seen = None
            resent += 1
            if resent > 2:
                raise Abort("the plug keeps going back to the mover instead of starting ESPHome")
            say("    The plug went back to the mover: ESPHome restarted before it had")
            say("    confirmed itself. Sending ESPHome again...")
            step_esphome_from_mover(ctx)
            started = time.monotonic()
            continue
        else:
            moved = ctx.finder.poll()
            if moved and moved != host:
                continue
        if on_a_plug_network():
            break  # this computer joined its own network already: on through that
        output.progress(f"waiting for ESPHome... {time.monotonic() - started:.0f}s")
        time.sleep(3)
    output.progress_clear()
    if set_up_through_own_network(ctx, "ESPHome", esphome.is_esphome, esphome_networks(ctx)):
        return {"seconds": round(time.monotonic() - started, 1), "resent": resent,
                "set_up_through": "its own network"}
    raise Abort(
        "ESPHome was written and checked, but has not answered on your Wi-Fi within "
        "three minutes - most likely it cannot join it, and its own network "
        f"({esphome_network_hint(ctx)}) is not to be seen either. Switch the plug off and "
        "on, and run the same command again"
    )


def evidence_esphome_installed(ctx: Context) -> tuple[bool | None, str]:
    host = ctx.state.input("plug_ip")
    if host and esphome.is_esphome(host):
        return True, "The plug is running ESPHome, so the install did finish."
    if ctx.device.current_ota() == 1:
        return False, (
            "The plug is still running the temporary Tasmota, so it did not switch;\n"
            "  it is safe to do this step again."
        )
    return None, "The plug neither runs ESPHome nor answers as the temporary Tasmota."


# Shared prefix: stock Sonoff layout is still in place through D1 inclusive.
COMMON_STEPS: list[tuple[str, str, Callable[[Context], dict[str, Any]]]] = [
    ("A0", "Before you start", step_prerequisites),
    ("A5", "Offline preconditions", step_check),
    ("A1", "Pair the plug in eWeLink", step_pair),
    ("A2", "Discover, metadata and device key", step_discover),
    ("C2", "Stock to recovery bridge", step_bridge),
    ("C3", "Back up the Sonoff firmware", step_backup),
    ("D1", "Berry-capable Tasmota via bridge AP", step_berry_tasmota),
]
# The hub every route but Tasmota to ESPHome passes through: Sonoff's layout,
# the Berry Tasmota running from slot 1, ESPHome's bootloader in place.
HUB_STEPS: list[tuple[str, str, Callable[[Context], dict[str, Any]]]] = [
    ("D4", "Promote to high slot and freeze NVS", step_promote),
    ("D5", "ESPHome's bootloader, once for every firmware", step_bootloader),
]
# Full Tasmota product: repartition to Safeboot layout, then official image.
TASMOTA_FINAL_STEPS: list[tuple[str, str, Callable[[Context], dict[str, Any]]]] = [
    ("E1", "Preflight (read-only)", step_preflight),
    ("E2", "Stage Safeboot into the spare slot", step_stage),
    ("E3", "Commit the new layout", step_commit),
    ("F2", "Canonical boot and official app", step_canonical),
    ("F4", "Wi-Fi, SaveData and S60 template", step_configure),
    ("G1", "Restore official Safeboot", step_restore),
    ("H1", "Verify the four regions", step_verify),
]
# ESPHome product: stay on stock dual-OTA table; flash ESPHome from interim Tasmota.
ESPHOME_FINAL_STEPS: list[tuple[str, str, Callable[[Context], dict[str, Any]]]] = [
    ("I1", "ESPHome into the other slot, every block checked", step_esphome_install),
    ("I2", "ESPHome started and on the Wi-Fi", step_esphome_check),
]
SONOFF_FINAL_STEPS: list[tuple[str, str, Callable[[Context], dict[str, Any]]]] = [
    ("R1", "The plug's own Sonoff firmware and settings, from its backup", step_sonoff_restore),
    ("R2", "Sonoff's firmware started and on the Wi-Fi", step_sonoff_check),
]
# From a converted plug: Tasmota leaves its layout for the mover; ESPHome, or
# the mover, takes the startup firmware, which takes the Berry Tasmota.
LEAVE_TASMOTA_STEP = ("L1", "From Tasmota's layout to the mover on Sonoff's", step_leave_tasmota)
TO_HUB_STEPS: list[tuple[str, str, Callable[[Context], dict[str, Any]]]] = [
    ("X1", "Startup firmware, sent as an ESPHome update", step_startup_from_esphome),
    ("X2", "Berry Tasmota, uploaded to the startup firmware", step_berry_from_startup),
]
# What every run begins with: the checks on this computer.
START_STEPS = [step for step in COMMON_STEPS if step[0] in ("A0", "A5")]
# What a run installs, and what the plug runs to begin with, as settled at its start.
FIRMWARES = {"tasmota": "Tasmota", "esphome": "ESPHome", "sonoff": "Sonoff"}
# "midway": a plug a run left part way, found by a new one: on Sonoff's layout,
# running the mover, the startup firmware or the interim Tasmota.
# "committed": one a run left past the commit (E3), on Tasmota's layout with the
# startup firmware or an official Tasmota not yet set up - which only this
# computer's record of that run can tell from the ESPHome route's startup
# firmware, on Sonoff's layout.
# "installer": one a run left on this tool's installer (the bridge), part way
# from Sonoff's firmware: the backup and D1 need only the installer, not eWeLink.
SOURCES = ("sonoff", "tasmota", "esphome", "midway", "committed", "installer")


def steps_for(firmware: str, source: str = "sonoff") -> list[tuple[str, str, Callable[[Context], dict[str, Any]]]]:
    """The steps from `source` firmware to `firmware` (none, if it is the same)."""
    if firmware == source:
        return []
    final = {"tasmota": TASMOTA_FINAL_STEPS, "esphome": ESPHOME_FINAL_STEPS,
             "sonoff": SONOFF_FINAL_STEPS}[firmware]
    if source == "sonoff":
        return COMMON_STEPS + HUB_STEPS + final
    if source == "tasmota" and firmware == "esphome":
        # The mover takes ESPHome itself: no need to pass through the hub.
        return START_STEPS + [LEAVE_TASMOTA_STEP,
                              ("M1", "ESPHome, sent to the mover as an update", step_esphome_from_mover),
                              ESPHOME_FINAL_STEPS[-1]]
    if source == "installer":
        on_installer = [step for step in COMMON_STEPS if step[0] in ("C3", "D1")]
        return START_STEPS + on_installer + HUB_STEPS + final
    if source == "committed":
        # Official Tasmota first, from where the run left it; then on from Tasmota.
        rest = [step for step in TASMOTA_FINAL_STEPS if step[0] in ("F2", "F4", "G1", "H1")]
        if firmware != "tasmota":
            rest += steps_for(firmware, "tasmota")[len(START_STEPS):]
        return START_STEPS + rest
    leave = [LEAVE_TASMOTA_STEP] if source == "tasmota" else []
    return START_STEPS + leave + TO_HUB_STEPS + HUB_STEPS + final  # esphome and midway alike


def run_firmware(state: RunState) -> str:
    """A run's firmware; one from before there was a choice installs Tasmota."""
    return state.input("firmware") or "tasmota"


def run_source(state: RunState) -> str:
    """What the plug ran at a run's start; before there was a choice, Sonoff's."""
    return state.input("source") or "sonoff"


def steps_of(state: RunState) -> list[tuple[str, str, Callable[[Context], dict[str, Any]]]]:
    return steps_for(run_firmware(state), run_source(state))


# This run's steps: Tasmota's from Sonoff until the route is known (apply_target).
TASMOTA_STEPS = steps_for("tasmota")
STEPS = TASMOTA_STEPS
STEP_INDEX = {step: (title, handler) for step, title, handler in STEPS}


ROUTE = ("tasmota", "sonoff")


def apply_target(firmware: str, source: str = "sonoff") -> None:
    global STEPS, STEP_INDEX, ROUTE
    ROUTE = (firmware, source)
    STEPS = steps_for(firmware, source)
    STEP_INDEX = {step: (title, handler) for step, title, handler in STEPS}


class NothingToDo(Exception):
    """The plug already runs the firmware asked for."""


def settle_firmware(ctx: Context) -> str:
    """Which firmware this run installs (settle_route)."""
    return settle_route(ctx)[0]


def settle_route(ctx: Context) -> tuple[str, str]:
    """What the plug runs now and which firmware it is to have: kept from the
    run's start, given on the command line, or found out and asked - once,
    before anything is done.  A plug this tool converted is found on the
    network by what it answers as; otherwise it is on Sonoff's firmware."""
    kept, source = ctx.state.input("firmware"), ctx.state.input("source")
    given = ctx.firmware_given
    if kept and given and given != kept:
        raise Abort(
            f"this plug is being converted to {FIRMWARES[kept]}, and a conversion cannot "
            f"switch to {FIRMWARES[given]} part way. Carry on without "
            f"{'--esphome' if ctx.esphome_firmware else '--firmware'}, or start again with --new"
        )
    if kept:
        return kept, source or "sonoff"
    started = [step for step, _, _ in COMMON_STEPS if ctx.state.is_done(step) and step != "A0"]
    if started:
        source = "sonoff"  # begun before a plug could be found on another firmware
    else:
        source = find_current_firmware(ctx)
    choices = [name for name in FIRMWARES if name != source]
    if source != "sonoff" and not sonoff_backup_exists(ctx):
        choices.remove("sonoff")  # going back needs this plug's backup
    if given:
        if given == source:
            # Asked for what it runs: nothing to do, and nothing left unfinished.
            ctx.state.set_input("source", source)
            ctx.state.set_input("firmware", source)
            raise NothingToDo(f"The plug already runs {detect.NAMES[source]}: there is nothing to convert.")
        if given not in choices:
            raise Abort(f"this plug cannot go to {FIRMWARES[given]}: there is no backup of its "
                        "Sonoff firmware on this computer")
        firmware = given
    elif started:
        firmware = "tasmota"  # begun before there was a choice: it was Tasmota then
    else:
        firmware = ask_firmware(source, choices)
    ctx.state.set_input("source", source)
    ctx.state.set_input("firmware", firmware)
    tell(f"Converting the plug from {detect.NAMES[source]} to {FIRMWARES[firmware]}."
         if source in ("tasmota", "esphome") else f"Converting the plug to {FIRMWARES[firmware]}.")
    return firmware, source


# What each firmware is, in the words the choice is made in.
FIRMWARE_BLURBS = {
    "tasmota": ["Tasmota has its own web page for settings, and works with Home Assistant",
                "and many other systems."],
    "esphome": ["ESPHome is made for Home Assistant, and is set up and customised from",
                "there."],
    "sonoff": ["Sonoff's own firmware puts the plug back in the eWeLink app, with the",
               "settings it had."],
}


def ask_firmware(source: str, choices: list[str]) -> str:
    output.transient_begin()
    body = (["This plug was left part way through a conversion; it can go on to any",
             "of these from where it is.", ""] if source in ("midway", "committed", "installer") else [])
    for name in choices:
        body += FIRMWARE_BLURBS[name]
    if source != "sonoff" and "sonoff" not in choices:
        body += ["", "Going back to Sonoff's own firmware needs the backup this tool takes",
                 "when it first converts a plug, and there is none for this one here."]
    section("Choose the plug's new firmware", body)
    labels = {"tasmota": "Tasmota", "esphome": "ESPHome",
              "sonoff": "Sonoff's own firmware (back to eWeLink, from its backup)"}
    try:
        choice = ask_choice("Which firmware would you like", [labels[name] for name in choices])
    except prompts.NoOneToAnswer:
        output.transient_end()
        raise Abort("an unattended run needs to be told which firmware: "
                    + " or ".join(f"--firmware {name}" for name in choices)) from None
    output.transient_end()
    return choices[choice]


def sonoff_backup_exists(ctx: Context) -> bool:
    mac = ctx.state.input("plug_mac")
    return bool(mac and plugs.find_backup(mac))


def find_current_firmware(ctx: Context) -> str:
    """What the plug runs now: Tasmota or ESPHome if it is one this tool can
    find converted on the network, else Sonoff's firmware (found later,
    through eWeLink).  With --plug-ip, the plug at that address."""
    given = ctx.state.input("plug_ip")
    if given:
        running = identify(ctx, given)
        if running == "locked":
            ask_web_password(ctx, given)
            running = identify(ctx, given)
        elif running == "esphome-login":
            ask_esphome_login(ctx, given)
            running = identify(ctx, given)
        if running in ("mover", "startup", "hub"):
            running = "midway"
        if running in ("tasmota", "esphome", "midway", "installer"):
            adopt_plug(ctx, detect.Found(given, running, hostsys.mac_of(given)))
            return past_the_commit(ctx, running)
        return "sonoff"
    say("Looking for S60s already running Tasmota or ESPHome on your network...")

    # Plugs this computer has records of: their backups and their runs.
    known = {name.replace("-", ":") for name in
             (path.name for path in plugs.PLUGS.glob("*")) if plugs.MAC_RE.match(name)}

    def search() -> list[detect.Found]:
        try:
            return detect.converted_s60s(hostsys.local_ip_towards("192.0.2.1"), known_macs=known,
                                         password=ctx.web_password, esphome_login=ctx.esphome_login)
        except OSError:
            return []

    def open_ones(found: list[detect.Found]) -> list[detect.Found]:
        return [plug for plug in found if plug.firmware not in LOCKED]

    found = search()
    if not open_ones(found) and converted_here(ctx):
        # A plug this computer converted, or left part way, may be restarting -
        # a run stopped and started again at once finds it mid-restart.
        say("    None answered. One this computer converted may be restarting: looking "
            f"again for up to {RESTART_GRACE:.0f} seconds...")
        started = time.monotonic()
        while not open_ones(found) and time.monotonic() - started < RESTART_GRACE:
            time.sleep(5)
            found = search()
    # A Tasmota with a web password, or an ESPHome with a web login, says
    # whether it is an S60 only once given it: offered, and it asked for if
    # chosen, when someone can type it.
    locked = [plug for plug in found if plug.firmware in LOCKED]
    found = open_ones(found)
    if locked and prompts.UNATTENDED:
        ours = [plug for plug in locked if plug.mac and hostsys.normalise_mac(plug.mac) in known]
        if ours and not found:
            # A plug this computer converted: the one the run is for, surely.
            raise Abort(locked_out(ctx, ours[0]))
        for kind, given, what, hint in (("locked", ctx.web_password, "password", PASSWORD_HINT),
                                        ("esphome-login", ctx.esphome_login, "login", ESPHOME_LOGIN_HINT)):
            hosts = [plug.host for plug in locked if plug.firmware == kind]
            if hosts:
                tell(f"    Left out, for its web {what}{' (not the one given)' if given else ''}: "
                     f"the {'Tasmota' if kind == 'locked' else 'ESPHome'} at {', '.join(hosts)}. "
                     f"If it is the plug, {hint}.")
        locked = []
    if not found and not locked:
        return "sonoff"
    stock = "A plug still on Sonoff's firmware (it is set up in the eWeLink app)"
    if prompts.UNATTENDED and len(found) == 1:
        chosen = found[0]
        prompts.answered_unattended("Which plug is this run for", chosen.describe())
    else:
        offered = found + locked
        output.transient_begin()
        choice = ask_choice("Which plug is this run for",
                            [found_plug.describe()[0].upper() + found_plug.describe()[1:]
                             for found_plug in offered] + [stock])
        output.transient_end()
        if choice == len(offered):
            return "sonoff"
        chosen = offered[choice]
        if chosen.firmware in LOCKED:
            chosen = unlocked(ctx, chosen)
    adopt_plug(ctx, chosen)
    return past_the_commit(ctx, chosen.firmware)


# Found on the network, but saying whether it is an S60 only once given its
# web password (Tasmota) or web login (ESPHome).
LOCKED = ("locked", "esphome-login")


def identify(ctx: Context, host: str) -> str | None:
    """detect.identify, with the plug's web password and web login, where known."""
    return detect.identify(host, password=ctx.web_password, esphome_login=ctx.esphome_login)


def locked_out(ctx: Context, plug: detect.Found) -> str:
    """Why an unattended run stops at `plug`, locked: what to give."""
    if plug.firmware == "esphome-login":
        return (f"ESPHome on the plug at {plug.host} has a web login"
                f"{', and the one given is not it' if ctx.esphome_login else ''}: {ESPHOME_LOGIN_HINT}")
    return str(NeedsPassword(plug.host, bool(ctx.web_password)))


@dataclass
class Secret:
    """A secret a plug can be given: what it is called, ask() to have it
    typed (kept out of the log), and check(value): False once the plug takes
    it, True if it does not, None if it did not answer - or a ValueError
    saying why it is not one at all (a key that is not base64, say)."""
    kind: str
    ask: Callable[[], Any]
    check: Callable[[Any], bool | None]


def typed_secret(prompt: str, label: str) -> Callable[[], str]:
    def ask() -> str:
        value = ask_secret(f"{prompt}: ")
        output.private(value, label)
        return value
    return ask


def secret_or_retry(title: str, lines: list[str], secrets: list[Secret],
                    retry: Callable[[], bool | None], *, still: str,
                    last: str | None = None) -> tuple[Secret | None, Any]:
    """What the plug takes now: one of `secrets`, typed - or, the last
    choice, none, once its owner has removed what it wanted, `retry()` saying
    as a check does whether it takes that (`still`, while it does not).
    `last`: that choice in other words.  Asked until it takes one, or the run
    is stopped.  Returns the secret and its value, or (None, None)."""
    output.transient_begin()
    section(title, lines)
    options = [f"Type its {secret.kind}" for secret in secrets]
    options.append(last or f"Try again without {'one' if secrets else 'it'} - once you have removed it yourself")
    while True:
        choice = ask_choice("What now", options)
        secret = secrets[choice] if choice < len(secrets) else None
        try:
            if secret is None:
                value, answer = None, retry()
            else:
                value = secret.ask()
                answer = secret.check(value) if value else True
        except ValueError as exc:
            tell(f"    {exc}.")
            continue
        if answer is False:
            output.transient_end()
            return secret, value
        tell("    The plug did not answer; choose again to try once more." if answer is None
             else f"    That is not its {secret.kind}." if secret else f"    {still}")


def password_or_retry(title: str, lines: list[str], check: Callable[[str | None], bool | None], *,
                      still: str, kind: str | None = None, prompt: str = "", label: str = "") -> str | None:
    """secret_or_retry for one password, its `kind` (asked as `prompt`, kept
    out of the log as `label`), or none: `check(None)` for trying again."""
    secrets = [Secret(kind, typed_secret(prompt, label), check)] if kind else []
    return secret_or_retry(title, lines, secrets, lambda: check(None), still=still)[1]


def ask_web_password(ctx: Context, host: str) -> None:
    """The web password of the Tasmota at `host`, which wants one this run
    does not have: typed, each try checked on the plug, or none once it has
    been cleared on the plug.  Held in memory for this run only, as the Wi-Fi
    password is."""
    wrong = bool(ctx.web_password)
    if prompts.UNATTENDED:
        raise Abort(str(NeedsPassword(host, wrong)))
    ctx.web_password = password_or_retry("Tasmota's web password", [
        f"Tasmota on the plug at {host} has a web password.",
        *(["The one given (--web-password, or AUTOFLASH_TASMOTA_PASSWORD) is not it."] if wrong else []),
        "Type it to go on (Tasmota's one user name, admin, is filled in); or clear it",
        "in Tasmota yourself - Configuration > Other > Web Admin Password, emptied",
        "and saved, or the console command WebPassword 0 - and try again without one.",
        "A password typed is held in memory for this run only, never written to disk.",
    ], lambda password: Device(host, password=password).wants_password(),
        still="Tasmota there still has a web password.",
        kind="web password", prompt="Tasmota web password", label="tasmota-password")


ESPHOME_PASSWORD_HINT = ("give it with --esphome-ota-password, or as "
                         "AUTOFLASH_ESPHOME_OTA_PASSWORD in .env or the environment")
ESPHOME_KEY_HINT = ("give it with --esphome-api-key, or as "
                    "AUTOFLASH_ESPHOME_API_KEY in .env or the environment")
ESPHOME_LOGIN_HINT = ("give it with --esphome-web-user and --esphome-web-password, or as "
                      "AUTOFLASH_ESPHOME_WEB_USER and AUTOFLASH_ESPHOME_WEB_PASSWORD in .env "
                      "or the environment")


def esphome_key_secret(check: Callable[[bytes], bool | None]) -> Secret:
    """ESPHome's API encryption key, typed as its YAML config has it and
    `check`ed once it is one."""
    def checked(text: str) -> bool | None:
        try:
            key = noise.decode_key(text)
        except ValueError as exc:
            raise ValueError(f"That is not an API encryption key ({exc}): one is 32 bytes in base64, "
                             "as after key: under api: encryption: in the YAML config") from None
        return check(key)
    return Secret("API encryption key", typed_secret("ESPHome API encryption key", "esphome-api-key"),
                  checked)


def api_key_check(host: str, key: bytes) -> bool | None:
    """For secret_or_retry: whether ESPHome's API at `host` does not take `key`."""
    access = esphome.api_access(host, key)
    return None if access is None else access == esphome.API_KEY


def esphome_update_check(host: str, password: str | None, key: bytes | None) -> bool | None:
    """For secret_or_retry: whether ESPHome's updates at `host` still want
    more than `password` and `key`."""
    wants = esphome_ota.wants(host, password, key)
    return None if wants is None else wants != esphome_ota.NOTHING


def ask_esphome_password(ctx: Context, host: str) -> None:
    """The update password of ESPHome at `host`, which wants one this run does
    not have: typed, each try checked on the plug, or none once its owner has
    removed it - or, from an ESPHome that takes encrypted updates too, its
    API encryption key, with which it asks for no password.  Held in memory
    for this run only."""
    wrong = bool(ctx.esphome_password)
    if prompts.UNATTENDED:
        raise Abort(f"{esphome_ota.NeedsPassword(wrong)}: {ESPHOME_PASSWORD_HINT}")
    offer = esphome_ota.probe(host)
    keyed = bool(offer and offer.encryption)
    secrets = [Secret("update password", typed_secret("ESPHome update password", "esphome-password"),
                      lambda password: esphome_update_check(host, password, None))]
    if keyed:
        secrets.append(esphome_key_secret(lambda key: esphome_update_check(host, None, key)))
    secret, value = secret_or_retry("ESPHome's update password", [
        f"ESPHome on the plug at {host} has an update password (set in Home",
        "Assistant, say). The next firmware goes on as an ESPHome update, which needs",
        "it. Type it to go on; or remove it from the plug's ESPHome YAML config (the",
        "'password:' line under 'ota:'), re-flash, then try again without one.",
        *(["Its API encryption key does instead (key: under api: encryption: there):",
           "with it, the update goes encrypted."] if keyed else []),
        *(["The one given is not it."] if wrong else []),
        "What you type is held in memory for this run only, never written to disk.",
    ], secrets, lambda: esphome_update_check(host, None, None),
        still="ESPHome there still has an update password.")
    if secret is not None and secret.kind == "API encryption key":
        ctx.esphome_key = noise.decode_key(value)
    else:
        ctx.esphome_password = value


def ask_esphome_key(ctx: Context, host: str, *, wifi: bool = False) -> bool:
    """The API encryption key of ESPHome at `host`, typed, each try checked
    on the plug: for its updates, when it takes only encrypted ones - or none,
    once its owner has removed that - or, `wifi`, to unlock its API, which then
    gives its Wi-Fi details (they are typed otherwise).  Held in memory for
    this run only.  Returns whether it was given."""
    wrong = ctx.esphome_key is not None
    if prompts.UNATTENDED:
        if wifi:
            return False  # the Wi-Fi is asked for next, which stops the run
        raise Abort(f"{esphome_ota.WrongKey() if wrong else esphome_ota.NeedsEncryption()}: "
                    f"{ESPHOME_KEY_HINT}; or remove that encryption: line, re-flash, then run this again")
    if wifi:
        secret, value = secret_or_retry("ESPHome's API encryption key", [
            f"ESPHome on the plug at {host} gives its Wi-Fi details through its API (ours",
            "does from 1.1.0), but its API is encrypted (api: encryption: in its ESPHome",
            "YAML config). Type its key - after key: there - and it gives them; or type",
            "the Wi-Fi details yourself instead.",
            "A key typed is held in memory for this run only, never written to disk.",
        ], [esphome_key_secret(lambda key: api_key_check(host, key))],
            lambda: False, still="", last="Type the Wi-Fi details instead")
    else:
        secret, value = secret_or_retry("ESPHome's encrypted updates", [
            f"ESPHome on the plug at {host} takes only encrypted updates: the ota:",
            "section of its ESPHome YAML config has encryption:, which makes them with its",
            "API encryption key (after key: under api: encryption: there). Type that key",
            "to go on; or remove encryption: from under ota:, re-flash, then try again.",
            *(["The one given (--esphome-api-key, or AUTOFLASH_ESPHOME_API_KEY) is not it."]
              if wrong else []),
            "A key typed is held in memory for this run only, never written to disk.",
        ], [esphome_key_secret(lambda key: esphome_update_check(host, ctx.esphome_password, key))],
            lambda: wants_key(host, ctx.esphome_password),
            still="ESPHome there still takes only encrypted updates.")
    ctx.esphome_key = noise.decode_key(value) if secret is not None else None
    if not wifi and secret is None and esphome_update_check(host, ctx.esphome_password, None) is True:
        ask_esphome_password(ctx, host)  # encryption: gone, an update password put instead
    return ctx.esphome_key is not None


def wants_key(host: str, password: str | None) -> bool | None:
    """Whether ESPHome's updates at `host` still want a key (they take only
    encrypted ones); None when it does not answer."""
    wants = esphome_ota.wants(host, password, None)
    return None if wants is None else wants == esphome_ota.KEY


def ask_esphome_login(ctx: Context, host: str) -> None:
    """The web login of ESPHome at `host` (web_server's auth:), which wants
    one this run does not have: typed, each try checked on the plug, or none
    once its owner has removed it.  Held in memory for this run only."""
    wrong = ctx.esphome_login is not None
    if prompts.UNATTENDED:
        raise Abort(locked_out(ctx, detect.Found(host, "esphome-login")))

    def ask() -> tuple[str, str]:
        user = ask_text("ESPHome web user name")
        password = ask_secret("ESPHome web password: ")
        output.private(password, "esphome-web-password")
        return user, password

    def check(login: tuple[str, str] | None) -> bool | None:
        if login:
            esphome.use_login(host, *login)
        found = esphome.page(host)
        return None if found is None else found == "login"

    _, ctx.esphome_login = secret_or_retry("ESPHome's web login", [
        f"ESPHome on the plug at {host} has a web login (auth: under web_server: in",
        "its ESPHome YAML config). Type its user name and password to go on; or remove",
        "auth: from there, re-flash, then try again without one.",
        *(["The one given (--esphome-web-user and --esphome-web-password, or as",
           "AUTOFLASH_ESPHOME_WEB_USER and AUTOFLASH_ESPHOME_WEB_PASSWORD) is not it."]
          if wrong else []),
        "A password typed is held in memory for this run only, never written to disk.",
    ], [Secret("web login", ask, check)], lambda: check(None),
        still="ESPHome there still has a web login.")


def check_esphome_key(ctx: Context, host: str) -> None:
    """An API encryption key given in advance, checked on the plug's API:
    asked for again, or gone on without, when it is not the plug's.  One
    given for a plug whose API is plain is not needed, and left unused."""
    if ctx.esphome_key is None:
        return
    access = esphome.api_access(host, ctx.esphome_key)
    if access == esphome.API_PLAIN:
        say("    ESPHome's API on the plug is not encrypted: the API encryption key given is not needed.")
        ctx.esphome_key = None
    elif access == esphome.API_KEY:
        if prompts.UNATTENDED:
            raise Abort(f"{esphome_ota.WrongKey()}: {ESPHOME_KEY_HINT}")
        secret, value = secret_or_retry("ESPHome's API encryption key", [
            f"The API encryption key given (--esphome-api-key, or AUTOFLASH_ESPHOME_API_KEY)",
            f"is not the one ESPHome on the plug at {host} has: its YAML config has it, after",
            "key: under api: encryption:. Type that one; or go on without it, when the",
            "plug's Wi-Fi details and its update can be had another way.",
            "A key typed is held in memory for this run only, never written to disk.",
        ], [esphome_key_secret(lambda key: api_key_check(host, key))],
            lambda: False, still="", last="Go on without it")
        ctx.esphome_key = noise.decode_key(value) if secret is not None else None


def unlock_esphome(ctx: Context) -> None:
    """What ESPHome on the plug wants, known before the go-ahead - so that
    nothing is asked once the plug is being changed: an API encryption key
    given, checked; then for its updates, its update password, when it has
    one (or its key, which does instead where it takes encrypted updates
    too), or its key, when it takes only encrypted updates.  Each given
    (--esphome-ota-password, --esphome-api-key, or their AUTOFLASH_ names), or
    asked for."""
    host = ctx.state.input("plug_ip")
    if not host:
        return
    check_esphome_key(ctx, host)
    wants = esphome_ota.wants(host, ctx.esphome_password, ctx.esphome_key)
    if wants == esphome_ota.KEY:
        ask_esphome_key(ctx, host)
    elif wants == esphome_ota.PASSWORD:
        ask_esphome_password(ctx, host)


def unlocked(ctx: Context, plug: detect.Found) -> detect.Found:
    """`plug`, a Tasmota with a web password or an ESPHome with a web login,
    as what it is once given it."""
    if plug.firmware == "esphome-login":
        ask_esphome_login(ctx, plug.host)
        if identify(ctx, plug.host) != "esphome":
            raise Abort(f"the ESPHome at {plug.host} did not answer as an S60")
        return detect.Found(plug.host, "esphome", plug.mac)
    ask_web_password(ctx, plug.host)
    running = identify(ctx, plug.host)
    if running in ("mover", "startup", "hub"):
        running = "midway"
    if running not in ("tasmota", "midway"):
        raise Abort(f"the Tasmota at {plug.host} did not answer as an S60")
    return detect.Found(plug.host, running, plug.mac)


def unlock_tasmota(ctx: Context) -> None:
    """The plug's own Tasmota web password, when it has one, known before any
    step asks the plug for anything: on a run from Tasmota, until the plug
    leaves it (L1) - every firmware after that is one this tool put on.  A
    run carried on was not asked yet, nor one given the wrong password."""
    if run_source(ctx.state) != "tasmota" or ctx.state.is_done("L1"):
        return
    host = ctx.state.input("plug_ip")
    if host and ctx.device.wants_password() is True:
        ask_web_password(ctx, host)


def past_the_commit(ctx: Context, running: str) -> str:
    """"committed" for a plug part way that a run of this computer's left past
    its commit: on Tasmota's layout, not Sonoff's, so the way on is official
    Tasmota first - never the hub's steps, which need Sonoff's two slots."""
    if running != "midway":
        return running
    mac = ctx.state.input("plug_mac")
    for directory in reversed(run_directories()):
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
                          and table_fetched(directory / "live") and ctx.device.is_safeboot())
        if (committed or finished_alone) and not state.is_done("H1"):
            if finished_alone:
                say("    Its last run was stopped as the commit ran, and the plug finished it:")
                say("    it fetched the new layout, and runs the startup firmware now.")
            else:
                say("    This computer's record of it says its last run got past the commit:")
            say("    it is on Tasmota's layout, so official Tasmota goes on first.")
            # G1 puts the official startup firmware back over the one that run
            # put on, and checks it is that one by the run's own record of it.
            ctx.evidence.mkdir(parents=True, exist_ok=True)
            for name in ("commit-report.json", "stage-report.json"):
                kept = directory / "live" / name
                if kept.is_file() and not (ctx.evidence / name).exists():
                    shutil.copy2(kept, ctx.evidence / name)
            return "committed"
        return running  # its latest run says otherwise
    return running


RESTART_GRACE = 45.0


def converted_here(ctx: Context) -> bool:
    """Whether this computer has a record of converting a plug, other than this run."""
    for directory in run_directories():
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
        file_plug(ctx, mac, None)
    say(f"    This run is for {found.describe()}.")


# ---------------------------------------------------------------------- CLI


def cmd_status(ctx: Context) -> int:
    say(f"Plug:      {ctx.plug}")
    say(f"Address:   {ctx.state.input('plug_ip') or '(not set)'}")
    say(f"Manifest:  {ctx.manifest_path}")
    say(f"State:     {ctx.state.path}")
    say("")
    say(ctx.state.summary([(step, title) for step, title, _ in STEPS]))
    captures = ctx.state.data.get("captures", {})
    if captures:
        say("")
        say("Captures:")
        for key, value in sorted(captures.items()):
            say(f"  {key}: {json.dumps(value)}")
    return 0


DESTRUCTIVE_STEPS = {"C2", "E2", "E3", "F2", "G1", "I1", "L1", "M1", "X1", "X2", "D5", "R1"}



def stage_name(step: str) -> str:
    named = TARGET_STAGE_NAMES.get(ROUTE[0], {})
    if step in named:
        return named[step]
    return STAGE_NAMES.get(step, STEP_INDEX[step][0])


def begin_stage(step: str) -> None:
    number = next(index for index, (name, _, _) in enumerate(STEPS, 1) if name == step)
    output.set_stage_width(max(len(stage_name(name)) for name, _, _ in STEPS))
    output.stage_begin(number, len(STEPS), stage_name(step))


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
    title, handler = STEP_INDEX[step]
    begin_stage(step)
    say(f"{step}  {title}")
    if step in DESTRUCTIVE_STEPS:
        ctx.state.mark(step, ATTEMPTED)
    try:
        captured = handler(ctx)
    except QuitRun:
        output.stage_end("stopped")
        return 130  # main() says so, once it has cleaned up
    except PlugNeverConnected as exc:
        ctx.state.mark(step, FAILED, error=str(exc))
        output.stage_end("stopped")
        section(f"{step} could not start", firewall_hint(ctx))
        ctx.last_failure = str(exc)
        ctx.last_failed_step = step
        tell(f"FAILED: {exc}")
        return 1
    except (Abort, DeviceError, ValueError, PhaseError) as exc:
        # A phase that ended with no result may have got as far as writing:
        # where the plug can show whether the step finished, the step stays
        # "attempted" and the next run asks it.
        if not (isinstance(exc, PhaseError) and step in ATTEMPTED_EVIDENCE):
            ctx.state.mark(step, FAILED, error=str(exc))
        output.stage_end("stopped")
        ctx.last_failure = str(exc)
        ctx.last_failed_step = step
        tell(f"FAILED: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - recorded and explained, not a bare traceback
        message = unexpected(exc)
        # As above: whether it got as far as writing is for the plug to say.
        if step not in ATTEMPTED_EVIDENCE:
            ctx.state.mark(step, FAILED, error=message)
        output.stage_end("stopped")
        ctx.last_failure = message
        ctx.last_failed_step = step
        tell(f"FAILED: {message}")
        return 1
    # A step may end its stage otherwise: "skipped" or "already done".
    outcome = captured.pop("stage_outcome", "complete") if captured else "complete"
    ctx.state.mark(step, DONE, **{"result": captured})
    for key, value in (captured or {}).items():
        ctx.state.capture(f"{step}.{key}", value)
    output.stage_end(outcome)
    say(f"{step} done")
    return 0



def cmd_step(ctx: Context, step: str) -> int:
    if step not in STEP_INDEX:
        say(f"unknown step {step}; known: {', '.join(s for s, _, _ in STEPS)}")
        return 2
    wait_while_plug_busy(ctx)
    return execute_with_retries(ctx, step)


# Steps run again by themselves when they fail for a reason that may pass -
# the network, mostly.  Each is safe to repeat: it only reads, or writes only
# what it checks first (D1 the bridge's peer slot, which the bridge selects
# itself away from before writing, D4 the slot it is in, E2 the spare slot, G1
# only over the recovery Safeboot), or sets what it sets again.  Not the steps that ask
# for anything, nor C2 or E3, which must happen once.
# I1 too: until the plug restarts it runs Tasmota from ota_1, and writes the
# same checked image again; once it runs ESPHome, I1 finds it and stops.
RETRY_STEPS = {"A2", "D1", "D4", "E1", "E2", "F4", "G1", "H1", "I1",
               "L1", "M1", "X1", "X2", "D5", "R1"}
ATTEMPTS = 3
RETRY_PAUSE = 10.0
# A failure worth another go: the plug or the network not answering.  A refusal
# - the plug's checks, a password, the wrong model - would only say it again.
TRANSIENT_RE = re.compile(
    r"HTTP -1|timed out|timeout|sent nothing|did not answer|not answering|unreachable|"
    r"connection (refused|reset|aborted)|ConnectionError|IncompleteRead|URLError|"
    r"No route|stopped the \w+ without|did not complete within|Wi-Fi went down|"
    r"closed the connection|Broken pipe",
    re.IGNORECASE,
)
# A router restart takes a minute or two.  The plug gives up on a step within
# about a minute - busier for longer, it stops answering for good (its energy
# meter's readings pile up) - so this computer does the waiting, between tries.
PLUG_BACK_WAIT = 300.0


# A step keeps the plug busy for about a minute at most, retrying included.
BUSY_PLUG_WAIT = 120.0


def wait_while_plug_busy(ctx: Context) -> None:
    """Give a plug still running a stopped step the time to finish it.

    A migration step keeps the plug busy, answering nothing else, until it
    succeeds or gives up - for a while after this computer stopped waiting
    (Ctrl-C, say).  Run again at once, the plug looks unreadable, and the
    run would go on as if it were.  It is told apart by answering ping.
    """
    host = ctx.state.input("plug_ip")
    if (not host or not ctx.state.is_done("D1") or ctx.state.is_done(STEPS[-1][0])
            or ctx.state.is_done("I1")):
        return  # not running Tasmota yet, finished, or running ESPHome
    device = ctx.device
    if device.answers(timeout=3.0) or not hostsys.ping_ok(host):
        return  # answering, or not on the network at all: the steps say which
    tell("    The plug is on the network but not answering yet. It is most likely still")
    tell("    finishing the step that was stopped, and gives up within a minute or two.")
    started = time.monotonic()
    while time.monotonic() - started < BUSY_PLUG_WAIT:
        output.progress(f"waiting for the plug to finish... {time.monotonic() - started:.0f}s")
        if device.answers(timeout=3.0):
            output.progress_clear()
            tell("    It is answering again.")
            return
        time.sleep(3.0)
    output.progress_clear()
    tell("    It is still not answering. Switch it off and on - at this point that is safe -")
    tell("    and run this again; it carries on. Carrying on now, and each step says what it finds.")


def execute_with_retries(ctx: Context, step: str) -> int:
    for attempt in range(1, ATTEMPTS + 1):
        code = execute(ctx, step)
        if step != "E3":  # it counts its own tries (COMMIT_ATTEMPTS)
            ctx.state.capture(f"{step}.attempts", attempt)
        if code != 1 or step not in RETRY_STEPS or attempt == ATTEMPTS:
            return code
        # A step the plug is asked about later (ATTEMPTED_EVIDENCE) keeps no
        # error in its record, so what it failed with is taken from this run.
        error = str(ctx.state.data.get("steps", {}).get(step, {}).get("error", "")
                    or ctx.last_failure)
        if not TRANSIENT_RE.search(error):
            return code
        tell(f"    That may have been the network - trying again in {RETRY_PAUSE:.0f}s "
             f"(attempt {attempt + 1} of {ATTEMPTS}).")
        time.sleep(RETRY_PAUSE)
        wait_for_plug_back(ctx, step)
    return code


PLUG_BACK_HINT_AFTER = 45.0


def wait_for_plug_back(ctx: Context, step: str | None = None) -> None:
    """Before a step is tried again, wait for a plug running Tasmota to answer:
    after a router restart it is back within a minute or two."""
    if not ctx.state.input("plug_ip") or not ctx.state.is_done("D1"):
        return
    device = ctx.device
    host = ctx.state.input("plug_ip")
    esphome_route = run_firmware(ctx.state) == "esphome"

    def back() -> bool:
        # On the way to ESPHome the plug may already have switched to it.
        return device.answers(timeout=3.0) or (esphome_route and esphome.is_esphome(host, 3.0))

    if back():
        return
    tell("    The plug is not answering yet - waiting for it (a router restarting, say).")
    started = time.monotonic()
    hinted = False
    while time.monotonic() - started < PLUG_BACK_WAIT:
        # Part way through, the plug runs a Tasmota holding your Wi-Fi details,
        # which keeps trying them and never opens a network of its own - all but
        # F4's official Tasmota, which may not have them yet (F4 looks for its
        # setup network itself).
        if not hinted and step != "F4" and time.monotonic() - started > PLUG_BACK_HINT_AFTER:
            output.progress_clear()
            tell("    At this stage the plug has no setup network of its own: it rejoins")
            tell("    your Wi-Fi by itself as soon as it can. Check your router is on and")
            tell("    the plug is plugged in; this keeps waiting.")
            hinted = True
        output.progress(f"waiting for the plug... {time.monotonic() - started:.0f}s")
        if back():
            output.progress_clear()
            tell("    It is answering again.")
            return
        time.sleep(3.0)
    output.progress_clear()
    tell(f"    It has not answered for {PLUG_BACK_WAIT / 60:.0f} minutes - trying anyway.")


# Each step's name on its stage line, in words that need no knowledge of the
# internals.
STAGE_NAMES = {
    "A0": "Confirming prerequisites",
    "A5": "Validating local files",
    "A1": "Discovering the plug",
    "A2": "Querying Sonoff firmware",
    "C2": "Sending bridge firmware",
    "C3": "Backing up Sonoff firmware",
    "D1": "Installing interim firmware",
    "D4": "Preparing memory layout",
    "E1": "Verifying memory layout",
    "E2": "Staging startup firmware",
    "E3": "Committing memory layout",
    "F2": "Installing Tasmota",
    "F4": "Configuring Tasmota",
    "G1": "Installing Safeboot",
    "H1": "Verifying conversion",
    "I1": "Installing ESPHome",
    "I2": "Checking ESPHome",
    "L1": "Leaving Tasmota",
    "M1": "Installing ESPHome",
    "X1": "Installing startup firmware",
    "X2": "Installing interim firmware",
    "D5": "Updating the bootloader",
    "R1": "Restoring Sonoff firmware",
    "R2": "Checking Sonoff firmware",
}
# Where a shared step means something else on the way to another firmware: D4
# frees the half of the memory it goes into, and the layout is not changed.
TARGET_STAGE_NAMES = {
    "esphome": {"D4": "Making room for ESPHome"},
    "sonoff": {"D4": "Making room for Sonoff firmware"},
}


# What each step does, in words that assume no knowledge of the internals.
PLAIN_DESCRIPTION = {
    "A0": "lists what needs to be in place before starting",
    "A5": "checks the files on this computer before anything is touched",
    "A1": "gets the plug onto your network through the eWeLink app",
    "A2": "asks the plug what firmware it is running",
    "C2": "replaces the plug's Sonoff firmware with our small installer firmware",
    "C3": "keeps a backup of the plug's Sonoff firmware on this computer",
    "D1": "puts a full Tasmota onto the plug through the installer's own Wi-Fi",
    "D4": "moves Tasmota into the other half of the plug's memory, out of the way",
    "E1": "reads the plug's memory layout and checks it, changing nothing",
    "E2": "copies the new startup firmware into the unused half of the memory",
    "E3": "rewrites the plug's memory map - the one step that cannot be undone",
    "F2": "installs the full Tasmota now that the new layout is in place",
    "F4": "sets up Wi-Fi, saves settings and applies the S60 plug template",
    "G1": "replaces the temporary startup firmware with the official one",
    "H1": "checks the finished plug against the official files",
    "I1": "writes ESPHome into the plug, checks every block, and switches to it",
    "I2": "checks the plug has started ESPHome and joined your Wi-Fi",
    "L1": "moves the plug off Tasmota's memory layout, onto a small firmware that takes the next",
    "M1": "sends ESPHome to the plug, which checks it and switches to it",
    "X1": "sends the startup firmware to the plug as an ESPHome update",
    "X2": "puts a full Tasmota onto the plug through the startup firmware",
    "D5": "updates the plug's bootloader, the program that starts its firmware",
    "R1": "writes the plug's own Sonoff firmware and settings back, from its backup",
    "R2": "checks the plug has started its Sonoff firmware and joined your Wi-Fi",
}


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
    bridge = find_ssid(bridge_networks(ctx))
    if bridge:
        return True, (
            f"The plug is broadcasting a Wi-Fi network called '{bridge}'.\n"
            "  At this stage only the installer firmware does that, so this step did finish."
        )
    return None, (
        "No installer network is visible from this computer. That does not mean\n"
        "  it failed - the plug may still be starting up, or this computer's Wi-Fi\n"
        "  may be busy or switched off."
    )



def evidence_official_app_installed(ctx: "Context") -> tuple[bool | None, str]:
    """Safeboot still running means the official firmware never landed."""
    if not ctx.finder.poll():
        # Not answering may simply mean it is waiting to be set up: the migration
        # clears its Wi-Fi details, so the new firmware comes up asking for them.
        setup = find_ssid(setup_networks(ctx))
        if setup:
            return True, (
                f"The plug is not on your network, but it is broadcasting '{setup}'.\n"
                "  That is the full Tasmota asking to be set up - the temporary\n"
                "  startup firmware cannot do that, so the install did finish."
            )
        return None, (
            "The plug is not answering and no setup network is visible from here.\n"
            "  It may still be starting."
        )
    device = ctx.device
    if device.is_safeboot():
        return False, (
            "The plug's web page still shows Safeboot, the small temporary\n"
            "  firmware - so the full Tasmota is not installed yet."
        )
    version = device.firmware_version()
    if version:
        return True, (
            f"The plug is running {version}, not Safeboot, so the install did finish."
        )
    return None, (
        "The plug is answering but will not say what it is running, so there is\n"
        "  no way to tell from here."
    )


def commit_report(ctx: "Context") -> str | None:
    """PASS or FAIL as the plug reported its commit, or None if it never did.

    The plug sends its report only after reading the new layout back, and
    restarts straight after, so a run stopped at that moment has the report
    even though the command that started the commit never got its reply.
    """
    try:
        report = json.loads((ctx.evidence / "commit-report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(report, dict) or report.get("phase") != "commit":
        return None
    status = report.get("status")
    return status if status in {"PASS", "FAIL"} else None


def table_fetched(evidence: Path) -> bool:
    """Whether the plug fetched the new layout in the commit this evidence is of."""
    try:
        return "GET /target-table" in (evidence / "commit-server.log").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return False


def evidence_commit_finished(ctx: "Context") -> tuple[bool | None, str]:
    status = commit_report(ctx)
    if status == "PASS":
        return True, (
            "The plug reported the new layout written and checked, just before\n"
            "  it restarted - so this step did finish."
        )
    if status == "FAIL":
        return False, "The plug reported that this step failed."
    # No report: lost, or never sent (the plug's Wi-Fi was down as it
    # finished).  The commit writes nothing before it has the new table, and
    # the plug boots the startup firmware only once the new layout is in
    # place - so together those two say how far it got.
    if not table_fetched(ctx.evidence):
        return False, (
            "The plug never fetched the new layout, so it wrote nothing - it is safe\n"
            "  to do this step again."
        )
    device = ctx.device
    if device.is_safeboot():
        return True, (
            "The plug's report did not arrive, but it is running the temporary startup\n"
            "  firmware, which starts only once the new layout is in place - so this\n"
            "  step did finish."
        )
    if device.current_ota() == 1:
        return False, (
            "The plug is still running from the old layout, so the step did not get\n"
            "  as far as switching to the new one; it is safe to do it again."
        )
    return None, "The plug sent no report of this step, and does not show how far it got."



# Consulted when a step was interrupted, to answer "did it finish?" from the
# device rather than from the operator's memory.
def evidence_by_what_answers(finished: set[str], not_yet: set[str]):
    """A check for a step that ends with the plug running something else:
    finished once it runs one of `finished`, not yet while one of `not_yet`."""
    def evidence(ctx: "Context") -> tuple[bool | None, str]:
        host = ctx.state.input("plug_ip") or ""
        running = identify(ctx, host) if host else None
        if running is None:
            moved = ctx.finder.poll()
            running = identify(ctx, moved) if moved else None
        name = {"mover": "the mover", "startup": "the startup firmware",
                "hub": "the interim Tasmota"}.get(
            running, detect.NAMES.get(running, "nothing this tool knows"))
        if running in finished:
            return True, f"The plug is running {name}, so this step did finish."
        if running in not_yet:
            return False, f"The plug is still running {name}, so this step has to be done again."
        return None, "The plug is not answering, so it cannot show whether this step finished."
    return evidence


def evidence_bootloader(ctx: "Context") -> tuple[bool | None, str]:
    image = esphome.published_bootloader()
    actual = ctx.device.sha_region(0, len(image))
    if actual == esphome.BOOTLOADER_SHA256:
        return True, "ESPHome's bootloader is in place, so this step did finish."
    return False, "The plug still has its old bootloader, so this step has to be done again."


ATTEMPTED_EVIDENCE: dict[str, Callable[["Context"], tuple[bool | None, str]]] = {
    "C2": evidence_bridge_installed,
    "E3": evidence_commit_finished,
    "F2": evidence_official_app_installed,
    "I1": evidence_esphome_installed,
    "L1": evidence_by_what_answers({"mover", "startup", "hub"}, {"tasmota"}),
    "M1": evidence_by_what_answers({"esphome"}, {"mover"}),
    "X1": evidence_by_what_answers({"startup", "hub"}, {"esphome", "mover"}),
    "X2": evidence_by_what_answers({"hub"}, {"startup"}),
    "D5": evidence_bootloader,
    "R1": evidence_by_what_answers({"sonoff"}, {"hub"}),
}


def confirm_skip(ctx: Context, step: str, title: str) -> bool:
    """Trust the checklist only as far as the plug agrees with it."""
    verifier = STEP_VERIFIERS.get(step)
    if verifier is None:
        say(f"[skip] {step} {title} (recorded as done)")
        return True
    try:
        ok, message = verifier(ctx)
    except (DeviceError, Abort, OSError, ValueError) as exc:
        say(f"[skip] {step} {title} (recorded as done; could not re-check: {exc})")
        return True
    if ok:
        say(f"[skip] {step} {title} (done, and the plug still agrees)")
        return True
    output.transient_begin()  # the note and its question, gone once answered
    say("")
    tell(f"{step} is recorded as done, but {message}.")
    tell("The record and the device disagree, so the checklist cannot be trusted here.")
    again = ask_yes_no(f"Run {step} again", default=False)
    output.transient_end()
    return not again



def tell_how_to_carry_on() -> None:
    tell("")
    tell("Fix the cause, then carry on with the same command again:")
    tell("    start.cmd, or .\\start.ps1 in PowerShell" if hostsys.IS_WINDOWS else "    sh start.sh")
    if output.log_path():
        tell(f"What happened, in full: {display_path(output.log_path())}")
    tell("Stuck? Start a discussion or open an issue:")
    output.show_links([(DISCUSSIONS_URL, "Start a discussion"), (ISSUES_URL, "Open an issue")])


def unexpected(exc: BaseException) -> str:
    """An error nothing here anticipated: in full in the log, in a line on screen."""
    output.log_only(traceback.format_exc().rstrip())
    return f"something unexpected went wrong: {type(exc).__name__}: {exc}"


def cmd_run(ctx: Context) -> int:
    # Asleep part way, this computer would leave the plug waiting on it.
    say(f"This computer is {awake.keep_awake()} while the run works.")
    try:
        return run_steps(ctx)
    finally:
        # However the run ends - finished, failed, quit, Ctrl-C or stopped.
        forget_run_secrets(ctx.dir)
        awake.let_sleep()


def say_lock_back(name: str) -> None:
    if (ROOT / name).exists():
        say(f"{name} restored")
    else:
        say(f"{name} left lifted: another run, on another plug, still needs it")


def restore_left_locks() -> None:
    """Put back a safety lock a run left moved aside.

    A step that needs one moves it aside while it runs and puts it back after,
    however it ends - unless this computer itself stopped (a crash, a power cut)
    while it ran.  Moved aside, the lock then fails every run: put back, the
    step simply asks for it again.
    """
    for name in safety_locks.restore_left(ROOT):
        say(f"{name} put back: a run stopped while it was moved aside")


def stop_here_after_ctrl_c(step: str) -> bool:
    """At the end of a stage, a Ctrl-C pressed during it is asked about: the
    run stops here only if the person says so - by itself it carries on."""
    if not interrupt.pending():
        return False
    interrupt.clear()
    output.transient_begin()
    section("You pressed Ctrl-C", [
        f"The stage that was under way is done, and everything is recorded, so",
        "stopping here is safe: running this again picks up where it left off.",
    ])
    choice = prompts.countdown_choice("Stop the run here", [
        "Carry on - the run goes on to the next stage",
        "Stop here",
    ])
    output.transient_end()
    if choice == 1:
        say(f"Stopped after {step}, as asked after Ctrl-C.")
        return True
    say(f"Carrying on after {step}: Ctrl-C was not confirmed.")
    return False


def run_steps(ctx: Context) -> int:
    started = time.monotonic()
    restore_left_locks()  # a resumed run skips A5, which looks too
    tell("")
    tell("Converting a plug usually takes under 10 minutes. Some stages take a")
    tell("minute or two with little to see - that's normal. Keep the plug plugged")
    tell("in, and this computer on, until the last stage says complete.")
    tell("")
    try:
        if ctx.state.input("plug_mac"):
            hold_plug(ctx, ctx.state.input("plug_mac"))  # a run carried on: its plug is known
        firmware, source = settle_route(ctx)
        unlock_tasmota(ctx)
    except NothingToDo as nothing:
        tell(str(nothing))
        return 0
    except Abort as refused:
        tell(f"Stopped before anything was done: {refused}.")
        return 2
    apply_target(firmware, source)
    interrupt.clear()
    ctx.state.set_input("stopped_by", None)
    resumed = any(ctx.state.is_done(step) for step, _, _ in STEPS)
    ctx.run_started, ctx.run_resumed = started, resumed
    wait_while_plug_busy(ctx)
    for step, title, _ in STEPS:
        if ctx.state.is_done(step):
            begin_stage(step)
            if confirm_skip(ctx, step, title):
                output.stage_end("already done")
                continue
        if ctx.state.status(step) == ATTEMPTED:
            # Under its own stage line, like every other stage; the check, and
            # any question, are taken off the screen once settled.
            begin_stage(step)
            output.transient_begin()
            say("")
            tell(f"{step} was interrupted last time - checking whether it finished...")
            finished, reason = None, ""
            evidence = ATTEMPTED_EVIDENCE.get(step)
            if evidence is not None:
                try:
                    finished, reason = evidence(ctx)
                except (DeviceError, Abort, OSError, ValueError) as exc:
                    finished, reason = None, f"The check itself could not run: {exc}"
                tell(f"    {reason}")
            if finished is True:
                # The plug itself shows the step completed: nothing to decide.
                output.transient_end()
                ctx.state.mark(step, DONE, note="the plug showed it had completed")
                output.stage_end("already done")
                continue
            section("The last step was interrupted", [
                f"The step that {PLAIN_DESCRIPTION.get(step, title.lower())} was",
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
                choice = ask_choice("What would you like to do", options)
            output.transient_end()
            if choice == 1:
                ctx.state.mark(step, DONE, note="operator confirmed it completed")
                say(f"{step} marked done on your say-so.")
                output.stage_end("already done")
                continue
        code = execute_with_retries(ctx, step)
        if code == 0 and stop_here_after_ctrl_c(step):
            close_firewall_rules(ctx)
            return 130
        if code == 0 and step == POINT_OF_NO_RETURN and ctx.research_bridge:
            section("Research bridge installed - stopping here", [
                f"{ctx.bridge.name} is on the plug. Join the bridge's network",
                f"({our_setup_network(ctx)}, open) and run its research tool, e.g.:",
                "  python tools/inspect_stock_nvs.py",
                "Then switch the plug off and on: it goes back to its Sonoff",
                "firmware. Or run this tool again to carry on the conversion.",
            ])
            close_firewall_rules(ctx)
            return 0
        if code == 130:
            return code  # the operator chose to quit; main() cleans up and says so
        if code != 0:
            close_firewall_rules(ctx)
            tell_how_to_carry_on()
            return code
    say("")
    close_firewall_rules(ctx)
    tell("")
    output.show("Conversion complete!", output.GREEN)
    if firmware == "sonoff":
        tell("The plug is running its own Sonoff firmware again, with the settings it")
        tell("had: it is back in the eWeLink app.")
    else:
        tell("The plug is running ESPHome..." if firmware == "esphome"
             else "The plug is running official Tasmota...")
        url = plug_page_url(ctx.state.input("plug_ip"))
        if url:
            output.show_link(url, "Your plug")
    invite_report(ctx, time.monotonic() - started - output.waited(), resumed)
    return 0


def plug_page_url(plug_ip: str | None) -> str | None:
    """The address to open the converted plug's Tasmota page at.

    Inside the emulator (tools/emulator) the plug's address only exists in its
    container; the emulator says where your browser reaches it instead.
    """
    emulated = os.environ.get("AUTOFLASH_EMULATOR_URL")
    if emulated:
        return emulated
    return f"http://{plug_ip}/" if plug_ip else None


def computer_description() -> str:
    """The operating system and its version, as someone else would want to know it."""
    if hostsys.IS_WINDOWS:
        release, build = platform.release(), platform.version()
        # Python before 3.12 calls every 10.0 build "10"; Windows 11 is 10.0
        # from build 22000 (the server releases there are named otherwise).
        if (release == "10" and sys.getwindowsversion().product_type == 1
                and int(build.rpartition(".")[2] or 0) >= 22000):
            release = "11"
        system = f"Windows {release} (build {build})"
    elif hostsys.IS_MAC:
        system = f"macOS {platform.mac_ver()[0]}"
    else:
        try:
            system = platform.freedesktop_os_release().get("PRETTY_NAME") or ""
        except OSError:
            system = ""
        system = system or f"Linux {platform.release()}"
    return system


def python_description() -> str:
    """The Python the tool ran on, and where it came from (start.sh, start.ps1)."""
    running = Path(sys.executable).resolve()
    portable = Path(os.environ.get("AUTOFLASH_PYTHON_HOME") or ROOT / ".python").resolve()
    if portable in running.parents:
        source = "portable"
    elif (hostsys.IS_MAC and "Python.framework" in running.parts
          and any((portable / name).exists() for name in ("installed-by-autoflash", "installed-by-s60"))):
        source = "installed by start.sh"
    elif sys.prefix != sys.base_prefix:
        source = "in a virtual environment"
    else:
        source = "existing installation"
    return f"{platform.python_version()} ({source})"


# The plug type and region each S60 model letter stands for.
REGIONS = {"G": "UK (Type G)", "F": "Europe (Type E/F)"}


def public_text(text: str, limit: int = 240) -> str:
    """`text` fit for a public post: on one line, with nothing that identifies
    the plug, the network or the person - not even a private address."""
    text = output.scrub(" ".join(str(text).split()))
    text = re.sub(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w])", "<address>", text)
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def run_result(ctx: Context, ending: str) -> str:
    """How the run ended, for the report: "converted", or where and why it stopped.

    ending: "converted", "failed" or "crashed".
    """
    if ending == "converted":
        return "converted"
    # The stage that failed, not the first left to do: a failure may have sent
    # earlier steps back to be done again (E3 refused as changed sends E1, E2).
    failed = getattr(ctx, "last_failed_step", "")
    stopped = next(((number, step) for number, (step, _, _) in enumerate(STEPS, 1)
                    if (step == failed if failed else not ctx.state.is_done(step))), None)
    where = (f"stage {stopped[0]} of {len(STEPS)} ({stage_name(stopped[1])})"
             if stopped else "the end")
    reason = ctx.last_failure or (
        ctx.state.data.get("steps", {}).get(stopped[1], {}).get("error", "") if stopped else "")
    head = "crashed" if ending == "crashed" else "stopped"
    return f"{head} at {where}" + (f": {public_text(reason)}" if reason else "")


def stage_timings(ctx: Context, key: str) -> str:
    """Each stage's `key` capture as "10=36s, 14=43s", by stage number - with no
    space inside a pair, so the screen never wraps one across two lines."""
    captures = ctx.state.data.get("captures", {})
    timings = [f"{number}={output.elapsed(captures[f'{step}.{key}']).replace(' ', '')}"
               for number, (step, _, _) in enumerate(STEPS, 1)
               if isinstance(captures.get(f"{step}.{key}"), (int, float))]
    return ", ".join(timings) or "not recorded"  # a run from before they were


def report_fields(ctx: Context, seconds: float, resumed: bool,
                  hardware: dict[str, str] | None = None,
                  result: str = "converted") -> list[tuple[str, str]]:
    """This run's answers for a Test reports post, as (label, value) - nothing private.

    No addresses, Wi-Fi name, account or device id: only what is useful to
    someone deciding whether the tool will work for them.  `hardware` is what
    check_hardware() measured.  "__" marks what the run could not tell.
    """
    hardware = hardware or {}
    product = str(ctx.state.input("product_model") or "").strip().upper()
    model = product if S60_PRODUCT.fullmatch(product) else ""
    captures = ctx.state.data.get("captures", {})
    joined = (captures.get("D1.upload_route") == "bridge network"
              or captures.get("D1.resumed") is True
              or (captures.get("D1.upload_route") == "home network" and captures.get("D1.wifi") != "preset")
              or captures.get("F4.wifi_route") == "setup network")
    took = output.elapsed(seconds) + (" (since resuming)" if resumed else "")
    retries = {number: captures[f"{step}.attempts"] - 1 for number, (step, _, _) in enumerate(STEPS, 1)
               if isinstance(captures.get(f"{step}.attempts"), int) and captures[f"{step}.attempts"] > 1}
    retried = [f"{number} ({count} {'retry' if count == 1 else 'retries'})"
               for number, count in retries.items()]
    # The lowest of the run's readings: from Sonoff's advert at A2, from Tasmota
    # at L1, D4, E1 or I1 - wherever the plug's firmware could give one.
    readings = [value for key, value in captures.items() if key.endswith(".signal_dbm")
                and isinstance(value, int) and not isinstance(value, bool)]
    signal = min(readings) if readings else None
    try:
        name = json.loads(s60_template(product))["NAME"]
    except (ValueError, KeyError, TypeError):
        name = "__"
    fields = [
        ("Result", result),
        ("Firmware", FIRMWARES[run_firmware(ctx.state)]),
        ("Converted from", {"sonoff": "Sonoff", "tasmota": "Tasmota", "esphome": "ESPHome",
                            "midway": "unfinished conversion",
                            "committed": "unfinished conversion",
                            "installer": "unfinished conversion"}[run_source(ctx.state)]),
        ("Tool version", __version__),
        ("Plug model", model or "__"),
        ("Plug type / region", REGIONS.get(model[-1:], "__") if model else "__"),
        ("Stock firmware", ctx.state.input("stock_version") or "__"),
        # How it went, never where it is or which plug: the path holds the MAC.
        ("Sonoff backup", BACKUP_OUTCOMES.get(ctx.state.input("backup_outcome") or "not taken")[1]),
        ("Computer", computer_description()),
        ("Python", python_description()),
        ("Time taken", took),
        # By stage number.  Busy is how long a Berry stage kept the plug from
        # anything else: at 70-90s a real S60TPG stops answering for good.
        ("Stage times", stage_timings(ctx, "seconds")),
        ("Plug busy", stage_timings(ctx, "plug_busy_seconds")),
        ("Retries", ", ".join(retried) or "none"),
        ("Plug Wi-Fi signal", f"{signal} dBm" if isinstance(signal, int) else "not recorded"),
        ("Typed the Wi-Fi", "yes" if ctx.state.input("wifi_typed") else "no"),
        ("Joined setup network", "yes" if joined else "no"),
        *([("Name in ESPHome", "Sonoff S60")] if run_firmware(ctx.state) == "esphome"
          else [("Name in Tasmota", name)] if run_firmware(ctx.state) == "tasmota" else []),
        ("Relay", hardware.get("Relay", "not checked")),
        ("Power meter", hardware.get("Power meter", "not checked")),
    ]
    if os.environ.get("AUTOFLASH_EMULATOR_URL"):
        fields.append(("Ran in the emulator", "yes"))
    return [(label, answer(value)) for label, value in fields]


def answer(value: str) -> str:
    """A report answer as shown, here and as the run checks it: starting with a
    capital - "No", "Converted".  Only the first letter, so "S60TPG" and
    "6m 32s" stay as they are."""
    return value[:1].upper() + value[1:]


def report_markdown(fields: list[tuple[str, str]]) -> str:
    """For pasting into the thread, where the labels show in bold."""
    return "".join(f"**{label}:** {value}\n" for label, value in fields)


def report_screen(fields: list[tuple[str, str]]) -> list[str]:
    """For the terminal, which shows Markdown as typed: plain labels, lined up."""
    width = max(len(label) for label, _ in fields) + 1
    return [f"    {label + ':':<{width}} {value}" for label, value in fields]


# Long enough for the CSE7766 and Tasmota's energy driver to report a new reading.
METER_SETTLE = 4.0
MAINS_VOLTS, DEAD_VOLTS = 90.0, 20.0     # clearly live / clearly switched off
LOAD_WATTS, IDLE_WATTS = 1.0, 0.5


def describe_hardware(switched: bool, restored: bool,
                      on: dict[str, Any] | None, off: dict[str, Any] | None) -> dict[str, str]:
    """The report's Relay and Power meter lines, from the relay's
    replies and the meter read with it on and with it off.

    The relay answering both commands proves the firmware drives it; a reading
    that drops when it is off - the voltage, where the meter is on the switched
    side, or the power of whatever is plugged in - proves it really switches.
    """
    def number(reading: dict[str, Any] | None, key: str) -> float | None:
        value = (reading or {}).get(key)
        return float(value) if isinstance(value, (int, float)) else None

    def reading(values: dict[str, Any] | None) -> str:
        volts, watts = number(values, "Voltage"), number(values, "Power")
        if volts is None and watts is None:
            return "no reading"
        return ", ".join(part for part in (
            f"{volts:g} V" if volts is not None else "",
            f"{watts:g} W" if watts is not None else "") if part)

    v_on, v_off = number(on, "Voltage"), number(off, "Voltage")
    p_on, p_off = number(on, "Power"), number(off, "Power")
    seen = ((v_on is not None and v_off is not None and v_on >= MAINS_VOLTS and v_off < DEAD_VOLTS)
            or (p_on is not None and p_off is not None and p_on >= LOAD_WATTS and p_off < IDLE_WATTS))
    if not (switched and restored):
        relay = "did not switch"
    elif seen:
        relay = "switches (meter confirms)"
    else:
        relay = "switches (meter unchanged)"
    return {
        "Relay": relay,
        "Power meter": "no readings" if on is None and off is None
                       else f"on: {reading(on)}; off: {reading(off)}",
    }


def check_hardware(ctx: Context) -> dict[str, str]:
    """Switch the relay to the other state and back - two switches - reading the
    meter after each, so one reading is with it on and one with it off.

    Whatever is plugged into the plug is switched for a few seconds, without
    asking: the run has just finished, and the plug ends as it started.
    """
    device = ctx.device

    def switch(on: bool) -> bool:
        reply = device.command(f"Power {'ON' if on else 'OFF'}")
        return isinstance(reply, dict) and str(reply.get("POWER", "")).upper() == ("ON" if on else "OFF")

    def meter() -> dict[str, Any] | None:
        time.sleep(METER_SETTLE)
        status = device.command("Status 10")
        energy = status.get("StatusSNS", {}).get("ENERGY") if isinstance(status, dict) else None
        return energy if isinstance(energy, dict) else None

    say("Checking the relay and power meter...")
    try:
        before = device.command("Power")
        was_on = isinstance(before, dict) and str(before.get("POWER", "")).upper() == "ON"
        switched = switch(not was_on)
        first = meter()
        restored = switch(was_on)
        second = meter()
    except (DeviceError, OSError, ValueError) as exc:
        say(f"    The plug did not answer the check: {exc}")
        return {key: "could not be checked" for key in ("Relay", "Power meter")}
    on, off = (second, first) if was_on else (first, second)
    result = describe_hardware(switched, restored, on, off)
    for label, value in result.items():
        say(f"    {label}: {answer(value)}")
    return result


def check_esphome_hardware(ctx: Context) -> dict[str, str]:
    """check_hardware's two switches and two readings, through ESPHome's web API."""
    host = ctx.state.input("plug_ip")
    say("Checking the relay and power meter...")
    was_on = esphome.relay(host)
    if was_on is None:
        say("    The plug did not answer the check.")
        return {key: "could not be checked" for key in ("Relay", "Power meter")}
    switched = esphome.switch_relay(host, not was_on)
    time.sleep(METER_SETTLE)
    first = esphome.meter(host) or None
    restored = esphome.switch_relay(host, was_on)
    time.sleep(METER_SETTLE)
    second = esphome.meter(host) or None
    on, off = (second, first) if was_on else (first, second)
    result = describe_hardware(switched, restored, on, off)
    for label, value in result.items():
        say(f"    {label}: {answer(value)}")
    return result


def invite_report(ctx: Context, seconds: float, resumed: bool) -> None:
    """Ask for a report, with this run's details ready to paste."""
    check = {"esphome": check_esphome_hardware, "tasmota": check_hardware}.get(
        run_firmware(ctx.state), lambda ctx: {})
    fields = report_fields(ctx, seconds, resumed, check(ctx))
    offer_report(ctx, fields, "Please share how it went!")
    tell("")
    output.show("You've reached the bit where I hold out my hat!", output.GREEN)
    tell("Free software, not free biscuits... Throw your friendly neighbourhood dev a biscuit:")
    output.show_links([(KOFI_URL, "Ko-fi"), (DONATE_URL, "PayPal")], hint=True)


def report_ending(code: int | None, ending: str | None) -> str | None:
    """Which stopped run offers its report: "failed" or "crashed", else None.

    A run that failed offers it as one that converted does (that one already
    has).  Not one the operator stopped - quit at a question (130) or Ctrl-C -
    nor one this computer stopped (the terminal closed: nobody to read it).
    """
    if ending in ("crashed", "cancelled"):
        return "crashed" if ending == "crashed" else None
    return "failed" if code not in (None, 0, 130) else None


def invite_stopped_report(ctx: Context, ending: str) -> None:
    """A run that failed asks for its report too: the runs that fail say most
    about what to fix, and leaving them out would make the tool look better than
    it is.  Not one the operator stopped - that says nothing about the tool.
    ending: "failed" or "crashed"."""
    seconds = time.monotonic() - ctx.run_started - output.waited()
    fields = report_fields(ctx, seconds, ctx.run_resumed, result=run_result(ctx, ending))
    offer_report(ctx, fields, "Please share how it went - a run that stopped helps as much "
                              "as one that worked.")


def offer_report(ctx: Context, fields: list[tuple[str, str]], heading: str) -> None:
    ctx.reported = True
    copied = hostsys.copy_to_clipboard(report_markdown(fields))
    tell("")
    output.show(heading, output.GREEN)
    if copied:
        tell("Paste your results into the thread (they're already on your clipboard):")
    else:
        tell("Copy your results below and post them in the thread...")
    output.show_link(REPORTS_URL, "Test reports")
    tell("")
    for line in report_screen(fields):
        tell(line)
    if any(value == "__" for _, value in fields):
        tell("")
        tell("Fill in the blanks (__) if you know them.")


def display_path(path: Path) -> str:
    """A path as short as it can be: relative to the repository when it is inside."""
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


# Files a run can leave that hold secrets: the plug's eWeLink LAN key (no
# longer fetched), update links that may act as tokens, the plug's settings
# memory, and the staged copy of the recovery image with the Wi-Fi password in
# it.  Their reports keep only hashes, which is all later steps use.
SECRET_FILES = (
    "device-key.json",
    "ota-metadata.json",
    "live/live-canonical-nvs.bin",
    "live/staged-safeboot-readback.bin",
)


def forget_run_secrets(directory: Path) -> list[str]:
    """Delete one run's secret files.  Returns what was removed."""
    removed = []
    for name in SECRET_FILES:
        path = directory / name
        if path.exists():
            path.unlink()
            removed.append(str(path.relative_to(ROOT)))
    return removed


def forget_all_secrets() -> list[str]:
    """Delete every run's secret files, and the bundle's stored password."""
    removed = [item for directory in run_directories() for item in forget_run_secrets(directory)]
    if forget_stored_password((ROOT / DEFAULT_MANIFEST).resolve()):
        removed.append("the Wi-Fi password in the bundle's recovery image")
    return removed


def cmd_cleanup(ctx: Context) -> int:
    """Remove this run's temporary firewall rules, and every stored secret."""
    if recorded_rules(ctx):
        close_firewall_rules(ctx)
    else:
        say("This run added no firewall rules.")
    removed = forget_all_secrets()
    if removed:
        say("Removed stored secrets:")
        for item in removed:
            say(f"  {item}")
    else:
        say("No stored secrets were found.")
    if plugs.backups():
        say(f"Kept the Sonoff firmware backups in {display_path(plugs.PLUGS)}/*/{plugs.BACKUP};")
        say("they hold the Wi-Fi password too. Delete them yourself if you will not want them.")
    return 0



def refused_for_another_run(command: str, ctx: Context) -> bool:
    """Another run working on this plug - or, to clean up, on any - is asked
    about first: before this one's log, report, firewall rule or safety locks
    are touched, which are the other run's too. True when refused (and said)."""
    mac = ctx.state.input("plug_mac")
    if command in ("run", "step") and mac:
        try:
            plugs.hold(mac, run_name(ctx.dir))
        except plugs.PlugBusy as busy:
            tell(f"Stopped before anything was done: another run is converting this plug ({mac}) "
                 f"right now{f' - {busy}' if str(busy) else ''}. Let it finish, or stop it first, "
                 "then run this again.")
            return True
    if command == "cleanup":
        busy = plugs.busy()
        if busy:
            tell(f"Not cleaning up: a run is converting a plug right now ({'; '.join(busy)}), and "
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
        # Inside the emulator (tools/emulator) its password is known.
        given = os.environ.get("AUTOFLASH_EMULATOR_SUDO_PASSWORD")
    if given is not None:
        if hostsys.check_sudo_password(given) is None:
            return given
        tell("    The sudo password given in advance (AUTOFLASH_SUDO_PASSWORD) was not accepted.")
    if prompts.UNATTENDED:
        raise OSError("sudo needs this computer's password, and an unattended run cannot "
                      "ask for it: put it in .env as AUTOFLASH_SUDO_PASSWORD")
    return ask_admin_password()


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
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
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
    parser.add_argument("--firmware", choices=list(FIRMWARES),
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
    global SIMULATED_BACKUP_FAILURES
    SIMULATED_BACKUP_FAILURES = 2 if args.simulate_backup_failure else 0

    try:
        given = credentials.load(args.env_file)
        adopt_older_runs()
        if args.command == "run" and not args.verbose and not args.unattended:
            wait_for_taller_window()  # before anything else is shown
        if args.command == "list":
            args.run = args.run or "-"
        elif args.command in ("status", "cleanup") and not args.run:
            # Report on the latest run; never start a new one just to look.
            args.run = read_current() or (run_name(run_directories()[-1]) if run_directories() else None)
            if not args.run:
                say("No conversions recorded yet.")
                return 0
        else:
            args.run = resolve_run_name(args.run, args.new, args.plug_ip,
                                        ask=args.command == "run" and not args.unattended)
        ctx = Context(args)
        apply_target(run_firmware(ctx.state), run_source(ctx.state))  # a run settles it at its start
    except QuitRun:
        say("Stopped. Nothing was changed.")
        return 130
    except (Abort, ValueError, OSError) as exc:
        say(f"REFUSING: {exc}")
        return 2

    if refused_for_another_run(args.command, ctx):
        return 2

    if args.command == "list":
        directories = run_directories()
        if not directories:
            say("No conversions recorded yet.")
            return 0
        for directory in directories:
            state = RunState.open(directory)
            steps = steps_of(state)
            done = sum(1 for step, _, _ in steps if state.is_done(step))
            mark = "verified" if run_is_complete(directory) else "in progress"
            say(f"  {run_name(directory):<55} {done}/{len(steps)} steps  {mark}"
                f"  {state.input('plug_ip') or ''}")
        for backup in plugs.backups():
            say(f"  Sonoff backup: {backup.describe()} in {display_path(backup.directory)}")
        return 0

    if args.command == "status":
        return cmd_status(ctx)
    if args.command == "cleanup":
        return cmd_cleanup(ctx)
    if args.command == "step":
        if not args.step_id:
            say("step requires a step id")
            return 2
        output.start(ctx.dir / "run.log", verbose=args.verbose)
        code = None
        try:
            code = cmd_step(ctx, args.step_id)
            return code
        finally:
            if code == 130:
                say_quit(ctx)
            else:
                release_firewall(ctx)
            output.finish()
    output.start(ctx.dir / "run.log", verbose=args.verbose)
    log_run_choice()
    if args.unattended:
        say("Unattended: PROCEED is taken as typed, yes/no questions take their "
            "default, and any other question stops the run.")
    if given:
        say(f"Given in advance ({display_path(args.env_file or credentials.ENV_FILE)}): "
            f"{', '.join(given)}")
    if credentials.renamed:
        say("Old names, still read for now - rename them: " + ", ".join(
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
        message = unexpected(exc)
        ctx.last_failure = message
        output.stage_end("stopped")
        tell(f"FAILED: {message}")
        tell_how_to_carry_on()
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
        tell("")
        tell("Stopped with Ctrl-C. Run again to pick up where you left off or to start again")
        tell("(Every step checks the plug first, so running again is safe.)")
        ending = "cancelled"
        return 130
    finally:
        if code == 130:
            say_quit(ctx)
        else:
            release_firewall(ctx)
        reported = report_ending(code, ending)
        if reported and not ctx.reported:
            try:
                invite_stopped_report(ctx, reported)
            except Exception as exc:  # noqa: BLE001 - never let the report hide how it ended
                output.log_only(f"The report could not be made: {type(exc).__name__}: {exc}")
        output.finish()


if __name__ == "__main__":
    raise SystemExit(main())
