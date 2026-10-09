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
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.autoflash import hostsys, output, setup_network  # noqa: E402
from tools.autoflash import tasmota_settings  # noqa: E402
from tools.autoflash.device import Device, DeviceError  # noqa: E402
from tools.autoflash.hostsys import FirewallRule  # noqa: E402
from tools.autoflash.phases import PhaseError, PhaseRunner, PlugNeverConnected  # noqa: E402
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
    warning_box,
    wait_for_ssid,
    wait_or_ask,
)
from tools.autoflash.state import ATTEMPTED, DONE, FAILED, RunState  # noqa: E402
from tools.discover_ewelink import discover  # noqa: E402
from tools.get_device_key import EwelinkError, get_devices, login  # noqa: E402
from tools.prepare_imprinted_bundle import (  # noqa: E402
    BundleError,
    ask_credentials,
    ask_password,
    build_bundle,
    bundle_ssid,
    forget_stored_password,
    pin_wifi,
    recovery_image,
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
BRIDGE_AP_PREFIX = "S60-OTA-Bridge"
# Fixed in the published bridge firmware; not a secret.
BRIDGE_PASSWORD = "s60-ota-bridge"
# 4.4.1: joins the home network the stock firmware was on, raising its own
# only if that fails, and gives Tasmota its Wi-Fi at D1.  Named, like every
# bridge from now on, by the version the stock updater is given.
BRIDGE_OTA = Path("artifacts/s60-ota-bridge-4.4.1.ota")
# Ports the migration phase servers (serve_safeboot_migration.py) may listen
# on, in order of preference.  Nothing on the plug keeps the port: each phase
# starts its own server and sends the plug a loader URL that names it.
PHASE_PORTS = (8089, *range(8100, 8110))
SETTINGS_PATH = ROOT / "captures" / "settings.json"
# Where people share how their conversion went: the pinned "Test reports" thread.
REPORTS_URL = "https://github.com/adamsthws/Sonoff-OTA/discussions/1"
# Where people ask for help: a discussion or issue of their own, so the
# reports thread stays a record of runs.
DISCUSSIONS_URL = "https://github.com/adamsthws/Sonoff-OTA/discussions/new/choose"
ISSUES_URL = "https://github.com/adamsthws/Sonoff-OTA/issues/new"
# Shown once a conversion has succeeded, after the report.
DONATE_URL = "https://www.paypal.com/biz/profile/adamsthws"
TASMOTA_AP_PREFIX = "tasmota-"


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
    output.before_input(question)
    answer = input(question)
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
    process = subprocess.Popen(args, cwd=str(cwd), env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return output.stream(process)


def run_directories() -> list[Path]:
    captures = ROOT / "captures"
    if not captures.is_dir():
        return []
    return sorted(
        (child for child in captures.iterdir() if (child / "autoflash-state.json").is_file()),
        key=lambda child: (child / "autoflash-state.json").stat().st_mtime,
    )


def run_is_complete(directory: Path) -> bool:
    try:
        state = RunState.open(directory)
    except (OSError, ValueError):
        return False
    return state.is_done(STEPS[-1][0])


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
    pending = next((step for step, _, _ in STEPS if not state.is_done(step)), None)
    try:
        when = dt.datetime.fromisoformat(state.data.get("updated_utc", "")).astimezone()
        today = when.date() == dt.date.today()
        stamp = when.strftime("%H:%M today" if today else "%d %b %H:%M")
    except ValueError:
        stamp = "an unknown time"
    details = [f"stopped at step {pending}" if pending else "finished", stamp]
    if state.is_done(POINT_OF_NO_RETURN):
        details.append("past the point of no return")
    return f"the plug at {where} ({', '.join(details)})"


def new_run_name(plug_ip: str | None) -> str:
    """A name the operator never has to type, but can recognise if they look."""
    base = ("plug-" + plug_ip.replace(".", "-")) if plug_ip else (
        "conversion-" + time.strftime("%Y%m%d-%H%M"))
    existing = {d.name for d in run_directories()}
    name, suffix = base, 2
    while name in existing:
        name, suffix = f"{base}-{suffix}", suffix + 1
    return name


def start_new_run(plug_ip: str | None) -> str:
    name = new_run_name(plug_ip)
    remember_current(name)
    return name


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
    unfinished.sort(key=lambda d: (d.name != pointer, -d.stat().st_mtime))
    if not ask:
        chosen = unfinished[0]
        say(f"Continuing {describe_run(chosen)}.")
        remember_current(chosen.name)
        return chosen.name

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
            remember_current(unfinished[chosen].name)
            return unfinished[chosen].name
        if chosen == len(unfinished):
            output.transient_end()
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
            tell("Remove it yourself: "
                 + hostsys.display(hostsys.firewall_commands(rule.kind, rule.plug_ip, rule.ports)[1]))
    shutil.rmtree(directory)
    if read_current() == directory.name:
        CURRENT_POINTER.unlink(missing_ok=True)
    say(f"Removed the record of {description}.")
    return True


class Context:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.plug = args.run
        self.dir = ROOT / "captures" / self.plug
        self.evidence = self.dir / "live"
        self.state = RunState.open(self.dir)
        self.manifest_path = (ROOT / args.manifest).resolve()
        self.dry_run = args.dry_run
        # Research only: a different bridge, after which the run stops (see
        # archive/docs/reference/automatic-wifi.md).  The plug goes back to stock on a power cycle.
        self.bridge = Path(getattr(args, "bridge", None) or BRIDGE_OTA)
        self.research_bridge = self.bridge != BRIDGE_OTA
        if args.plug_ip:
            self.state.set_input("plug_ip", args.plug_ip)
        if args.email:
            self.remember_email(args.email)
        if args.web_user:
            self.state.set_input("web_user", args.web_user)
        self.web_password = args.web_password
        self._ewelink_password: str | None = None
        self._finder: PlugFinder | None = None
        # Held only in memory: the Wi-Fi details, and the recovery image built
        # from them.  Neither is ever written to disk.
        self._wifi: tuple[str, str] | None = None
        self._recovery: bytes | None = None
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
        work, which typed ones are not until F2 tries them.  Asking is the
        fallback, with `why` saying what it is for; `--wifi` forces it.
        """
        forced = getattr(self.args, "wifi", False)
        if self._wifi is None and not forced:
            found = read_wifi_from_plug(self)
            if found is not None:
                self.adopt_wifi(*found)
                # A run stopped at the question earlier must not report it as typed.
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

    def email(self) -> str:
        """The eWeLink account email, chosen once per run.

        The last account used on this computer is offered first; a plug in
        another account needs that account's email instead.
        """
        email = self.state.input("email")
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
        return PhaseRunner(
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
PHASE_PROBLEM_RE = re.compile(r"REFUS|TIMEOUT|ERROR|Traceback|Stopped|FAIL|full output is in", re.IGNORECASE)


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
    known = os.environ.get("S60_EMULATOR_SUDO_PASSWORD")
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
    kind = hostsys.active_firewall()
    if kind is None:
        return
    add, _ = hostsys.firewall_commands(kind, plug_ip, ports)
    prompt_note = [
        "Windows will ask whether to allow the change - choose Yes. This window",
        "does not need to be run as administrator; only this rule asks for it.",
        "If Windows Security then asks whether Python may use networks, choose",
        "Allow: cancelling can block the plug even with this rule in place.",
    ] if hostsys.IS_WINDOWS else ["You may be asked for this computer's password.",
                                  *emulator_password_hint()]
    what = ("lets Python accept incoming connections - the macOS firewall allows "
            "programs, not ports" if kind == "macos"
            else f"allows only {plug_ip} in, on TCP ports {', '.join(map(str, ports))}")
    output.transient_begin()  # the note, and the system's password question
    section("Let the plug through this computer's firewall", [
        f"This computer's firewall ({kind}) would block the plug from fetching its",
        f"firmware. Adding one rule that {what}.",
        "This temporary rule is always removed when this tool exits (no matter how it exits).",
        *prompt_note,
    ])
    try:
        rule = hostsys.open_firewall(plug_ip, ports)
    except OSError:
        output.transient_end()
        section("The firewall rule could not be added", [
            "Add it yourself, in another window, with:",
            f"  {hostsys.display(add)}",
            "and remove it after the conversion. Carrying on - if the plug is still",
            "blocked, the next step stops and says so.",
        ])
        return
    output.transient_end()
    if rule is not None:
        rules.append(rule)
        ctx.state.set_input("firewall_rules", [r.to_json() for r in rules])
        say("The rule was already there, so it is left in place afterwards."
            if rule.detail.get("preexisting") else "Firewall rule added.")


def firewall_hint(ctx: Context) -> list[str]:
    """What to say when the plug never reached this computer."""
    plug_ip = ctx.state.input("plug_ip") or "PLUG_IP"
    lines = [
        "The plug never connected to this computer. That is almost always this",
        "computer's firewall, or this computer having changed address.",
    ]
    kind = hostsys.active_firewall()
    if kind:
        add, _ = hostsys.firewall_commands(kind, plug_ip, firewall_ports(ctx))
        lines += ["", f"A {kind} firewall is on. This allows the plug in:", f"  {hostsys.display(add)}"]
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
            hostsys.display(hostsys.firewall_commands(r.kind, r.plug_ip, r.ports)[1])
            for r in left
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
            tell("    " + hostsys.display(hostsys.firewall_commands(rule.kind, rule.plug_ip, rule.ports)[1]))
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


def locate_plug(ctx: Context, *, timeout: float = 300.0) -> str:
    """Wait for the plug on the home network; ask only if it cannot be found."""
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
    found = wait_or_ask(ctx.state.input("plug_ip"), what="the plug", timeout=60.0)
    ctx.set_plug_ip(found)
    return found


# --------------------------------------------------- the plug's own networks


def wait_until_joined(network: str, marker: str | None = None) -> None:
    """Block until this computer has joined the plug's network.

    With `marker`, the page must mention it (the bridge).  Without, only the web
    port is touched: Tasmota scans for networks on every page request in setup
    mode, which drops this computer off its network.
    """
    def tick(elapsed: float) -> None:
        output.progress(f"waiting for this computer to join {network}... {elapsed:.0f}s")

    joined = (setup_network.wait_for_page(marker, tick=tick) if marker
              else setup_network.wait_for_port(tick=tick))
    if not joined:
        output.progress_clear()
        raise Abort(f"this computer did not join {network} within 15 minutes")
    output.progress_clear()
    say(f"    Connected to {network}.")


def configure_plug_wifi(ctx: Context, network: str | None) -> str:
    """Give Tasmota, waiting on its setup network, the bundle's Wi-Fi details.

    The operator joins the setup network; the details are sent from here.  Tasmota
    saves them only after it has connected with them, so a failure leaves the
    plug waiting to be set up, never lost.
    """
    network = network or "tasmota-XXXXXX-NNNN"
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
    ])
    wait_until_joined(network)
    outcome = "manual"
    if credentials is not None:
        ssid, password = credentials
        tell(f"    Sending the Wi-Fi details for '{ssid}'. The plug tests them first -")
        tell("    stay on its network for about 20 seconds...")
        try:
            outcome, address = setup_network.send_wifi(ssid, password)
        except setup_network.SetupError as exc:
            outcome, address = "unknown", None
            tell(f"    {exc}")
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
        "Your computer should rejoin your usual Wi-Fi by itself, if it doesn't,",
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
    try:
        started = time.monotonic()
        device.upload_firmware(path)
        seconds = round(time.monotonic() - started, 1)
        say(f"{what} uploaded in {seconds}s")
        return f"http:{seconds}s"
    except Exception as exc:  # noqa: BLE001 - any failure falls back to the operator
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
    section("Before you start", [
        "- The eWeLink app on your phone, signed in to an eWeLink account",
        "  (free - a throwaway account is fine).",
        "- The S60 set up in the app on the 2.4 GHz Wi-Fi it should stay on:",
        "  it keeps that network after the conversion, and cannot use 5 GHz.",
        "- LAN Control turned on in the plug's settings in the app.",
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

    for lock in ("RECOVERY_LOCK", "REPARTITION_LOCK"):
        if not (ROOT / lock).exists():
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
    if isinstance(signal, int) and signal < WEAK_SIGNAL_DBM:
        output.transient_begin()
        section("The plug's Wi-Fi signal is weak", [
            f"It reports {signal} dBm. After the next stage the startup firmware has",
            "to rejoin on its own, and a weak signal makes that less certain.",
            "Moving the plug nearer the access point first is worth it.",
        ])
        if not ask_yes_no("Carry on anyway", default=False):
            raise Abort("stopped for a weak Wi-Fi signal; nothing was written")
        output.transient_end()
    return {"network": connected, "signal_dbm": signal}



def step_pair(ctx: Context) -> dict[str, Any]:
    """A1 - find the plug: an S60 in the owner's eWeLink account, answering
    on this network, asking only when there is a choice to make."""
    ip = ctx.state.input("plug_ip")
    if not ip:
        say("Signing in to eWeLink to see which S60 plugs are in the account...")
        devices = ctx.account_devices()
        ip = find_any_plug() if devices is None else find_account_plug(ctx, devices)
    ctx.state.set_input("plug_ip", ip)
    mac = hostsys.mac_of(ip)
    if mac:
        ctx.state.set_input("plug_mac", mac)
    return {"plug_ip": ip, "plug_mac": mac}


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


def why_not_found(sighting: Sighting) -> str:
    """What to do about an account plug that is not answering here."""
    plug = sighting.plug
    if plug.online is False:
        return (f"{describe_plug(plug)}: eWeLink shows it offline - check it is switched "
                "on at the wall and on your Wi-Fi.")
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


def find_account_plug(ctx: Context, devices: list[dict[str, Any]]) -> str:
    """The address of the S60 to convert, from the account's S60s that answer here."""
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
            ready = [sighting for sighting in sightings if sighting.answering]
            if len(ready) == 1:
                # The stage line completing says it all; the log keeps which plug.
                say(f"    Found {describe_plug(ready[0].plug)} at {ready[0].ip}.")
                return str(ready[0].ip)
            if ready:
                output.transient_begin()
                choice = ask_choice(
                    "More than one S60 in the account answered. Which one is this run for",
                    [f"{describe_plug(sighting.plug)} at {sighting.ip}" for sighting in ready],
                )
                output.transient_end()
                return str(ready[choice].ip)
            output.transient_begin()
            section("Your S60 did not answer on this network", [
                f"- {why_not_found(sighting)}" for sighting in sightings
            ])
            choice = ask_choice("What now", [
                "Look again",
                "Use a different eWeLink account",
                "Type the plug's address myself",
            ])
            output.transient_end()
        if choice == 2:
            return ask_ipv4(
                "The plug's address",
                help_lines=["It is in your router's device list, and in eWeLink's device information."],
                must_respond=True,
            )
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
            section("No Sonoff plug answered on this network", [
                "Check, in the eWeLink app:",
                "- the S60 is added, on the same 2.4 GHz Wi-Fi network as this",
                "  computer - the plug cannot use 5 GHz",
                "- LAN Control is turned on in the plug's settings",
                "- the plug is switched on at the wall",
            ])
            choice = ask_choice(
                "What now",
                ["Look again", "Type the plug's address myself"],
            )
            if choice == 1:
                ip = ask_ipv4(
                    "The plug's address",
                    help_lines=["It is in your router's device list, and in eWeLink's device information."],
                    must_respond=True,
                )
            output.transient_end()
    return ip


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




def step_discover(ctx: Context) -> dict[str, Any]:
    """A2/A3/A4 - identify the device and read its OTA metadata."""
    plug_ip = ctx.state.require("plug_ip")[0]
    mdns = ctx.dir / "mdns.json"
    meta = ctx.dir / "ota-metadata.json"

    # The same report the discovery tool writes, without its console output.
    report = discover(8.0, None, plug_ip)
    if not report["packets"]:
        raise Abort(f"the plug at {plug_ip} did not answer the eWeLink discovery; check "
                    "it is switched on and LAN Control is on in the eWeLink app")
    mdns.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    email = ctx.email()
    env = ctx.ewelink_env()
    if run_tool([sys.executable, "tools/query_ota.py", "--mdns-capture", str(mdns),
                 "--email", email, "--output", str(meta)], interactive=True, env=env) != 0:
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
    ctx.state.set_input("stock_version", version)
    ctx.state.set_input("product_model", product)
    return {"stock_version": version, "update_offered": bool(endpoints), "model": model}



def step_bridge(ctx: Context) -> dict[str, Any]:
    """C - stock to recovery bridge.  First write; no rollback below this line."""
    plug_ip, version = ctx.state.require("plug_ip", "stock_version")
    listen_ip = ctx.listen_ip()
    email = ctx.email()
    port = choose_port(ctx, "delivery_port", DELIVERY_PORTS, "the firmware")
    ensure_firewall(ctx)
    env = ctx.ewelink_env()

    output.transient_begin()
    warning_box("Before going on", [
        "From here the plug's Sonoff firmware is replaced, so the eWeLink app",
        "will no longer control it. Later its memory layout is rewritten; losing",
        "power during that minute would mean opening the plug to recover it.",
        "Only go ahead with a plug you can afford to lose. You won't be asked again.",
    ])
    if not require_word(CONSENT_WORD):
        raise Abort("not confirmed - nothing has been written to the plug")
    output.transient_end()  # agreed: the note and the question have done their job

    lock = ROOT / "RECOVERY_LOCK"
    moved = ROOT / "RECOVERY_LOCK.owner-authorized"
    started = time.monotonic()
    hostsys.replace_file(lock, moved)
    try:
        code = run_tool(
            [sys.executable, "tools/serve_tasmota_ota.py",
             "--listen-ip", listen_ip, "--listen-port", str(port),
             "--device-ip", plug_ip,
             "--firmware", str(ctx.bridge),
             "--email", email, "--expected-current-version", version,
             "--mdns-capture", str(ctx.dir / "mdns.json"),
             "--i-understand-stock-has-no-automatic-rollback"],
            interactive=True,
            env=env,
        )
    finally:
        hostsys.replace_file(moved, lock)
        say("RECOVERY_LOCK restored")
    if code != 0:
        # The sender can fail after the plug already has everything - the cloud
        # reporting it offline is exactly what a successful install looks like.
        installed, reason = evidence_bridge_installed(ctx)
        if installed:
            say(f"  {reason}")
            return {"seconds": round(time.monotonic() - started, 1), "confirmed_by": "bridge network"}
        reported = output.tool_problems()
        section("The bridge was not delivered", [
            *(["The sender reported:", *(f"  {line}" for line in reported), ""] if reported else []),
            *firewall_hint(ctx),
        ])
        raise Abort("bridge delivery failed")
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
            if body is not None and setup_network.BRIDGE_PAGE_MARKER.lower() in body.lower():
                output.progress_clear()
                return address
        output.progress(f"looking for the installer on your network... {time.monotonic() - started:.0f}s")
        time.sleep(2.0)
    output.progress_clear()
    return None


def step_berry_tasmota(ctx: Context) -> dict[str, Any]:
    """D1/D2 - Berry-capable Tasmota through the bridge."""
    image = ROOT / "captures/safeboot-recovery-migration/tasmota32c3-bluetooth.bin"
    say("Looking for the plug's Wi-Fi network...")
    bridge = find_ssid(BRIDGE_AP_PREFIX)
    setup = None if bridge else find_ssid(TASMOTA_AP_PREFIX)
    if setup:
        # Resuming after the upload: Tasmota is already installed and waiting.
        say(f"    Tasmota is already installed and waiting on {setup}.")
        ctx.state.set_input("device_name", setup)
        ip = configure_plug_wifi(ctx, setup)
        return {"plug_ip": ip, "device_name": setup, "resumed": True}

    address = bridge_on_home_network(ctx)
    if address:
        say(f"    The installer joined your network at {address}.")
        preset = give_tasmota_the_wifi(ctx, address)
        upload_tasmota(image, host=address)
        if preset:
            found = tasmota_on_home_network(ctx)
            if found:
                return {"plug_ip": found, "upload_route": "home network", "wifi": "preset",
                        "device_name": ctx.state.input("device_name")}
            section("Tasmota has not appeared on your network", QUICK_POWER_CYCLE_HINT)
            return finish_tasmota_setup(ctx, route="home network", wait=900.0)
        return finish_tasmota_setup(ctx, route="home network")
    bridge = bridge or "S60-OTA-Bridge-XXXX   (the XXXX varies)"
    copied = hostsys.copy_to_clipboard(BRIDGE_PASSWORD)
    section("Join the bridge's Wi-Fi network", [
        "The plug is running our small installer, which has made its own Wi-Fi",
        f"network. In {hostsys.wifi_menu_hint()}, join:",
        f"    network   {bridge}",
        f"    password  {BRIDGE_PASSWORD}"
        + ("      (already copied - just paste it)" if copied else ""),
        "",
        "Your Wi-Fi internet drops while joined - expected. A wired connection,",
        "if this computer has one, can stay connected. This carries on by itself.",
    ])
    wait_until_joined(bridge.split()[0], setup_network.BRIDGE_PAGE_MARKER)
    upload_tasmota(image)
    return finish_tasmota_setup(ctx, route="bridge network")


# Tasmota with Wi-Fi details that do not work keeps retrying them and never
# opens its setup network.  Its quick power-cycle reset is the way out:
# settings.ino UpdateQuickPowerCycle, 7 boots in a row each under
# POWER_CYCLE_TIME (8 s), erases its settings and it starts as new.
QUICK_POWER_CYCLE_HINT = [
    "It was given the Wi-Fi details the installer had just joined with, so",
    "this is unusual. If a few more minutes pass and it still has not appeared:",
    "",
    "  Switch the plug off and on 7 times in a row, leaving it on for only",
    "  2-3 seconds each time, then leave it on.",
    "",
    "That makes Tasmota clear its settings and open its setup network",
    "(tasmota-XXXXXX-NNNN). This carries on from there by itself.",
]


def give_tasmota_the_wifi(ctx: Context, address: str) -> bool:
    """Have bridge v5 save Tasmota's settings with the Wi-Fi it joined with."""
    template, reason = tasmota_settings.load_template(ROOT, PINNED_ARTIFACTS["bluetooth"]["sha256"])
    if template is None:
        say(f"    Tasmota will need its Wi-Fi given by hand: {reason}.")
        return False
    try:
        setup_network.send_tasmota_settings(template, host=address,
                                            layout=tasmota_settings.layout_query())
    except setup_network.SetupError as exc:
        say(f"    Tasmota will need its Wi-Fi given by hand: {exc}")
        return False
    say("    Tasmota will start with your Wi-Fi details, so it joins by itself.")
    return True


def tasmota_on_home_network(ctx: Context, *, timeout: float = 240.0) -> str | None:
    """Tasmota's address once it has started and joined, at the plug's own MAC."""
    say("Waiting for Tasmota to start and join your network...")
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        address = ctx.finder.poll()
        body = setup_network.page(address, timeout=3.0) if address else None
        if body and "tasmota" in body.lower() and \
                setup_network.BRIDGE_PAGE_MARKER.lower() not in body.lower():
            output.progress_clear()
            ctx.set_plug_ip(address)
            try:
                status = Device(address).command("Status 5")
                name = status.get("StatusNET", {}).get("Hostname") if isinstance(status, dict) else None
            except (DeviceError, OSError):
                name = None
            if name:
                ctx.state.set_input("device_name", name)
            say(f"    Tasmota is on your network at {address}" + (f" as {name}." if name else "."))
            return address
        output.progress(f"waiting for Tasmota on your network... {time.monotonic() - started:.0f}s")
        time.sleep(3.0)
    output.progress_clear()
    return None


def upload_tasmota(image: Path, host: str = setup_network.SETUP_HOST) -> None:
    say(f"    Sending Tasmota ({image.stat().st_size:,} bytes) - about a minute...")
    started = time.monotonic()
    try:
        setup_network.upload_to_bridge(image, host=host)
    except setup_network.SetupError as exc:
        raise Abort(f"{exc}. The plug still has the installer; run this again") from exc
    say(f"    Upload verified in {time.monotonic() - started:.0f}s; the plug is restarting into Tasmota.")


def finish_tasmota_setup(ctx: Context, *, route: str, wait: float = 120.0) -> dict[str, Any]:
    say("")
    say("Waiting for Tasmota to start and make its setup network...")
    device_name = wait_for_ssid(TASMOTA_AP_PREFIX, timeout=wait)
    if device_name:
        ctx.state.set_input("device_name", device_name)
        say(f"    This plug is {device_name} - the same name shows in your router.")
    ip = configure_plug_wifi(ctx, device_name)
    return {"plug_ip": ip, "device_name": device_name, "upload_route": route}



def step_promote(ctx: Context) -> dict[str, Any]:
    """D4/D5 - move Tasmota to the high slot and freeze NVS."""
    device = ctx.device
    image = ROOT / "captures/safeboot-recovery-migration/tasmota32c3-bluetooth.bin"
    device.wait_reachable(timeout=120)
    slot = device.current_ota()
    say(f"The plug is running from slot {slot}. The migration needs slot 1.")
    if slot == 1:
        device.command("SaveData 0")
        say("Already there. SaveData 0 - settings frozen so the migration's checks stay stable")
        return {"current_ota": slot, "uploads": 0}
    if ctx.dry_run:
        say("[dry-run] would upload the same image through Firmware Upgrade")
        return {"current_ota": slot, "uploads": 0}

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
        slot = device.current_ota()
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


def step_preflight(ctx: Context) -> dict[str, Any]:
    """E1 - read-only inspection.  Re-runnable at any time."""
    ctx.wifi()  # read them off the plug first, so the check below compares the right network
    network = check_plug_network(ctx)
    code, _ = ctx.phase_runner().run("preflight")
    if code != 0:
        raise Abort(f"preflight did not pass (exit {code})")
    return {"exit": code, **network}


def step_stage(ctx: Context) -> dict[str, Any]:
    """E2 - writes the inactive slot only.  Re-runnable; changes no boot state."""
    code, _ = ctx.phase_runner().run("stage")
    if code != 0:
        raise Abort(f"stage did not pass (exit {code})")
    return {"exit": code}


def step_commit(ctx: Context) -> dict[str, Any]:
    """E3 - the single destructive operation.  Never automatically re-run."""
    # E1 and E2 pinned the bundle as it was then.  If it has been rebuilt since,
    # or the plug has moved networks, send the run back to E1 rather than fail
    # inside the commit server with nothing the operator can act on.
    def staged_bundle_is_current() -> bool:
        try:
            report = json.loads((ctx.evidence / "stage-report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return report.get("manifest_sha256") == sha256_bytes(ctx.manifest_path.read_bytes())

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

    lock = ROOT / "REPARTITION_LOCK"
    moved = ROOT / "REPARTITION_LOCK.owner-authorized"
    hostsys.replace_file(lock, moved)
    try:
        code, _ = ctx.phase_runner().run(
            "commit",
            extra_flags=["--i-accept-power-loss-may-require-opening-the-plug"],
            confirm=gate,
        )
    finally:
        hostsys.replace_file(moved, lock)
        say("REPARTITION_LOCK restored")
    if code != 0:
        raise Abort(f"commit did not pass (exit {code})")
    say("The new layout is written. The plug restarts by itself, and the run carries on.")
    return {"exit": code}



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
    how = upload_or_prompt(ctx, app, "Official Tasmota (app0)")

    # The startup firmware normally saved the Wi-Fi where Tasmota reads it, so
    # Tasmota comes back on the network.  If it did not, Tasmota raises its own
    # setup network instead and F4 sets it up through that.  Both mean it worked.
    say("")
    say("Waiting for the full Tasmota to start. It will either rejoin your")
    say("Wi-Fi, or put up its own setup network - both are normal.")
    started = time.monotonic()
    outcome = None
    while time.monotonic() - started < 300:
        time.sleep(5)
        if ctx.finder.poll() and not ctx.device.is_safeboot():
            outcome = "rejoined the network"
            say(f"    The plug is back at {ctx.device.host} running the full Tasmota.")
            break
        setup = find_ssid(TASMOTA_AP_PREFIX, timeout=10)
        if setup:
            outcome = f"raised its setup network {setup}"
            say(f"    The plug has started and is asking to be set up on {setup}.")
            ctx.state.set_input("device_name", setup)
            break
    if outcome is None:
        raise Abort(
            "the plug neither rejoined the network nor put up a setup network "
            "within five minutes"
        )
    return {"safeboot_seconds": round(waited, 1), "app_upload": how, "outcome": outcome,
            "startup_settings": saved}


STARTUP_LOG_PREFIX = "S60 recovery:"


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
    notes = [line[line.index(STARTUP_LOG_PREFIX):] for line in lines if STARTUP_LOG_PREFIX in line]
    if not notes:
        say("    The recovery firmware's log does not say whether it saved your Wi-Fi.")
        return None
    note = notes[-1]
    if "saved for the next firmware" in note:
        say("    It has saved your Wi-Fi where official Tasmota reads it, so Tasmota")
        say("    should join your network by itself.")
    elif "left alone" in note:
        say("    Tasmota settings from before were still there; it left them as they were.")
    else:
        say(f"    It could not save your Wi-Fi for official Tasmota: {note[len(STARTUP_LOG_PREFIX):].strip()}.")
        say("    Tasmota will ask to be set up through its own network instead, as before.")
    return note




def step_configure(ctx: Context) -> dict[str, Any]:
    """F3/F4 - Wi-Fi, settings persistence and the S60 template."""
    if ctx.finder.poll():
        route = "joined by itself"
        say("Official Tasmota joined your Wi-Fi by itself, with the details the")
        say("startup firmware saved - no setup network needed.")
    else:
        setup = find_ssid(TASMOTA_AP_PREFIX, timeout=15)
        if setup:
            route = "setup network"
            tell("    Official Tasmota did not join your Wi-Fi by itself, so it is set up")
            tell("    through its own network instead.")
            configure_plug_wifi(ctx, setup)
        else:
            route = "located"
            locate_plug(ctx)
    device = ctx.device
    device.wait_reachable(timeout=300)
    # Tasmota may be running on the record the startup firmware saved, which
    # holds the Wi-Fi but only what a Safeboot sets (it skips most defaults,
    # never expecting to save them).  Reset 4 rebuilds every setting from this
    # firmware's own defaults and keeps the Wi-Fi.  Harmless when Tasmota
    # started from defaults anyway.
    booted, uptime = device.started_at(), device.uptime_seconds()
    device.command("Reset 4")
    say("Settings reset to Tasmota's defaults, keeping the Wi-Fi; the plug restarts")
    device.wait_for_reboot(timeout=180, started_at=booted, before=uptime)
    booted, uptime = device.started_at(), device.uptime_seconds()
    device.command("SaveData 1")
    template = s60_template(ctx.state.input("product_model"))
    device.command(f"Template {template}")
    device.command("Module 0")
    say(f"Template applied as {json.loads(template)['NAME']}; the plug restarts")
    device.wait_for_reboot(timeout=180, started_at=booted, before=uptime)
    return {"template": "applied", "wifi_route": route}



def step_restore(ctx: Context) -> dict[str, Any]:
    """G - replace the private recovery Safeboot with official bytes."""
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

    code, _ = runner.run(
        "restore", extra_flags=["--i-confirm-normal-app-is-stable"], confirm=gate
    )
    if code != 0:
        raise Abort(f"restore did not pass (exit {code})")
    return {"restore": code}


def step_verify(ctx: Context) -> dict[str, Any]:
    """H1 - the three hashes, read back off the device itself."""
    manifest = load_manifest(ctx.manifest_path)
    artifacts = manifest["artifacts"]
    device = ctx.device
    device.wait_reachable(timeout=180)

    expected = {
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
    say("All three regions match the pinned official artifacts.")
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
    if ctx.state.is_done("E3"):
        return True, ""  # superseded: the old two-slot layout no longer exists
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


STEPS: list[tuple[str, str, Callable[[Context], dict[str, Any]]]] = [
    ("A0", "Before you start", step_prerequisites),
    ("A5", "Offline preconditions", step_check),
    ("A1", "Pair the plug in eWeLink", step_pair),
    ("A2", "Discover, metadata and device key", step_discover),
    ("C2", "Stock to recovery bridge", step_bridge),
    ("D1", "Berry-capable Tasmota via bridge AP", step_berry_tasmota),
    ("D4", "Promote to high slot and freeze NVS", step_promote),
    ("E1", "Preflight (read-only)", step_preflight),
    ("E2", "Stage Safeboot into the spare slot", step_stage),
    ("E3", "Commit the new layout", step_commit),
    ("F2", "Canonical boot and official app", step_canonical),
    ("F4", "Wi-Fi, SaveData and S60 template", step_configure),
    ("G1", "Restore official Safeboot", step_restore),
    ("H1", "Verify the three regions", step_verify),
]
STEP_INDEX = {step: (title, handler) for step, title, handler in STEPS}


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


DESTRUCTIVE_STEPS = {"C2", "E2", "E3", "F2", "G1"}



def stage_name(step: str) -> str:
    return STAGE_NAMES.get(step, STEP_INDEX[step][0])


def begin_stage(step: str) -> None:
    number = next(index for index, (name, _, _) in enumerate(STEPS, 1) if name == step)
    output.set_stage_width(max(len(stage_name(name)) for name, _, _ in STEPS))
    output.stage_begin(number, len(STEPS), stage_name(step))


def execute(ctx: Context, step: str) -> int:
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
        tell(f"FAILED: {exc}")
        return 1
    except (Abort, DeviceError, ValueError, PhaseError) as exc:
        ctx.state.mark(step, FAILED, error=str(exc))
        output.stage_end("stopped")
        tell(f"FAILED: {exc}")
        return 1
    ctx.state.mark(step, DONE, **{"result": captured})
    for key, value in (captured or {}).items():
        ctx.state.capture(f"{step}.{key}", value)
    output.stage_end("complete")
    say(f"{step} done")
    return 0



def cmd_step(ctx: Context, step: str) -> int:
    if step not in STEP_INDEX:
        say(f"unknown step {step}; known: {', '.join(s for s, _, _ in STEPS)}")
        return 2
    return execute(ctx, step)


# Each step's name on its stage line, in words that need no knowledge of the
# internals.
STAGE_NAMES = {
    "A0": "Confirming prerequisites",
    "A5": "Validating local files",
    "A1": "Discovering the plug",
    "A2": "Querying Sonoff firmware",
    "C2": "Installing bridge firmware",
    "D1": "Installing interim firmware",
    "D4": "Preparing the plug's memory",
    "E1": "Verifying the memory layout",
    "E2": "Staging the startup firmware",
    "E3": "Committing the new memory layout",
    "F2": "Installing final Tasmota",
    "F4": "Configuring Wi-Fi and settings",
    "G1": "Installing official Safeboot",
    "H1": "Verifying the conversion",
}


# What each step does, in words that assume no knowledge of the internals.
PLAIN_DESCRIPTION = {
    "A0": "lists what needs to be in place before starting",
    "A5": "checks the files on this computer before anything is touched",
    "A1": "gets the plug onto your network through the eWeLink app",
    "A2": "asks the plug what firmware it is running",
    "C2": "replaces the plug's Sonoff firmware with our small installer firmware",
    "D1": "puts a full Tasmota onto the plug through the installer's own Wi-Fi",
    "D4": "moves Tasmota into the other half of the plug's memory, out of the way",
    "E1": "reads the plug's memory layout and checks it, changing nothing",
    "E2": "copies the new startup firmware into the unused half of the memory",
    "E3": "rewrites the plug's memory map - the one step that cannot be undone",
    "F2": "installs the full Tasmota now that the new layout is in place",
    "F4": "sets up Wi-Fi, saves settings and applies the S60 plug template",
    "G1": "replaces the temporary startup firmware with the official one",
    "H1": "checks the finished plug against the official files",
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
    bridge = find_ssid(BRIDGE_AP_PREFIX)
    if bridge:
        return True, (
            f"The plug is broadcasting a Wi-Fi network called '{bridge}'.\n"
            "  Only the installer firmware does that, so this step did finish."
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
        setup = find_ssid(TASMOTA_AP_PREFIX)
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



# Consulted when a step was interrupted, to answer "did it finish?" from the
# device rather than from the operator's memory.
ATTEMPTED_EVIDENCE: dict[str, Callable[["Context"], tuple[bool | None, str]]] = {
    "C2": evidence_bridge_installed,
    "F2": evidence_official_app_installed,
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
    say("")
    tell(f"{step} is recorded as done, but {message}.")
    tell("The record and the device disagree, so the checklist cannot be trusted here.")
    return not ask_yes_no(f"Run {step} again", default=False)



def cmd_run(ctx: Context) -> int:
    try:
        return run_steps(ctx)
    finally:
        # However the run ends - finished, failed, quit, Ctrl-C or stopped.
        forget_run_secrets(ctx.dir)


def run_steps(ctx: Context) -> int:
    started = time.monotonic()
    resumed = any(ctx.state.is_done(step) for step, _, _ in STEPS)
    tell("")
    tell("Converting a plug usually takes under 10 minutes. Some stages take a")
    tell("minute or two with little to see - that's normal. Keep the plug plugged")
    tell("in, and this computer awake, until the last stage says complete.")
    tell("")
    for step, title, _ in STEPS:
        if ctx.state.is_done(step):
            begin_stage(step)
            if confirm_skip(ctx, step, title):
                output.stage_end("already done")
                continue
        if ctx.state.status(step) == ATTEMPTED:
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
                ctx.state.mark(step, DONE, note="the plug showed it had completed")
                continue
            output.transient_begin()
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
            choice = ask_choice("What would you like to do", options)
            output.transient_end()
            if choice == 1:
                ctx.state.mark(step, DONE, note="operator confirmed it completed")
                tell(f"{step} marked done on your say-so.")
                continue
        code = execute(ctx, step)
        if code == 0 and step == POINT_OF_NO_RETURN and ctx.research_bridge:
            section("Research bridge installed - stopping here", [
                f"{ctx.bridge.name} is on the plug. Join the bridge's network",
                f"(password {BRIDGE_PASSWORD}) and run its research tool, e.g.:",
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
            tell("")
            tell("Fix the cause, then carry on with:")
            tell("    python3 tools/s60_autoflash.py run")
            if output.log_path():
                tell(f"What happened, in full: {display_path(output.log_path())}")
            tell("Stuck? Start a discussion or open an issue:")
            output.show_link(DISCUSSIONS_URL)
            output.show_link(ISSUES_URL)
            return code
    say("")
    close_firewall_rules(ctx)
    tell("")
    output.show("Conversion complete!", output.GREEN)
    tell("The plug is running official Tasmota:")
    url = plug_page_url(ctx.state.input("plug_ip"))
    if url:
        output.show_link(url)
    invite_report(ctx, time.monotonic() - started, resumed)
    return 0


def plug_page_url(plug_ip: str | None) -> str | None:
    """The address to open the converted plug's Tasmota page at.

    Inside the emulator (tools/emulator) the plug's address only exists in its
    container; the emulator says where your browser reaches it instead.
    """
    emulated = os.environ.get("S60_EMULATOR_URL")
    if emulated:
        return emulated
    return f"http://{plug_ip}/" if plug_ip else None


def computer_description() -> str:
    """The operating system and its version, as someone else would want to know it."""
    if hostsys.IS_WINDOWS:
        system = f"Windows {platform.release()} (build {platform.version()})"
    elif hostsys.IS_MAC:
        system = f"macOS {platform.mac_ver()[0]}"
    else:
        try:
            system = platform.freedesktop_os_release().get("PRETTY_NAME") or ""
        except OSError:
            system = ""
        system = system or f"Linux {platform.release()}"
    return f"{system}, Python {platform.python_version()}"


# The plug type and region each S60 model letter stands for.
REGIONS = {"G": "UK (Type G)", "F": "Europe (Type E/F)"}


def report_fields(ctx: Context, seconds: float, resumed: bool,
                  hardware: dict[str, str] | None = None) -> list[tuple[str, str]]:
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
    took = output.elapsed(seconds) + (" (this session; the run was resumed)" if resumed else "")
    fields = [
        ("Result", "converted"),
        ("Plug model", model or "__"),
        ("Plug type / region", REGIONS.get(model[-1:], "__") if model else "__"),
        ("Stock firmware", ctx.state.input("stock_version") or "__"),
        ("Computer", computer_description()),
        ("Time taken", took),
        ("Typed the Wi-Fi", "yes" if ctx.state.input("wifi_typed") else "no"),
        ("Joined setup network", "yes" if joined else "no"),
        ("Name in Tasmota", json.loads(s60_template(product))["NAME"]),
        ("Relay", hardware.get("Relay", "not checked")),
        ("Power meter", hardware.get("Power meter", "not checked")),
    ]
    # Each answer starts with a capital: "No", "Converted".  Only the first
    # letter, so "S60TPG" and "6m 32s" stay as they are.
    return [(label, value[:1].upper() + value[1:]) for label, value in fields]


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
        relay = "did not switch on command"
    elif seen:
        relay = "switches (seen on the meter)"
    else:
        relay = "switches on command (no change on the meter)"
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
        say(f"    {label}: {value}")
    return result


def invite_report(ctx: Context, seconds: float, resumed: bool) -> None:
    """Ask for a report, with this run's details ready to paste."""
    fields = report_fields(ctx, seconds, resumed, check_hardware(ctx))
    copied = hostsys.copy_to_clipboard(report_markdown(fields))
    tell("")
    output.show("Please share how it went!", output.GREEN)
    tell("Copy/paste your results below into the Test-Reports discussion thread:")
    output.show_link(REPORTS_URL)
    tell("")
    for line in report_screen(fields):
        tell(line)
    if any(value == "__" for _, value in fields):
        tell("")
        tell("Fill in the blanks (__) if you know them.")
    tell("")
    output.show("And finally, you've reached the bit where I hold out my hat!", output.GREEN)
    tell("Free software, not free biscuits... Throw your friendly neighbourhood dev a biscuit:")
    output.show_link(DONATE_URL)


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
    return 0



def main() -> int:
    try:
        return _main()
    finally:
        # However it ended - finished, failed, quit or Ctrl-C - leave a blank
        # line between the last message and the shell's prompt.
        print(flush=True)


def _main() -> int:
    # Asked for in this tool's own words, so every row on screen is its own.
    hostsys.password_asker = ask_admin_password
    # Keep piping into head/less quiet rather than raising BrokenPipeError.
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    # Being stopped, or the terminal being closed, must still run the cleanup
    # in the finally blocks: the stored secrets and the firewall rule.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, lambda *_: sys.exit(129))
    parser = argparse.ArgumentParser(description=__doc__)
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
    parser.add_argument("--web-user", help="Tasmota web username when the UI has a password")
    parser.add_argument("--web-password", help="Tasmota web password (not stored in run state)")
    parser.add_argument("--wifi", action="store_true",
                        help="type the Wi-Fi details instead of reading them from the plug")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show every action as it happens (it is all in the run's log either way)")
    parser.add_argument("--bridge", help=argparse.SUPPRESS)  # research: archive/docs/reference/automatic-wifi.md
    args = parser.parse_args()

    try:
        if args.command == "list":
            args.run = args.run or "-"
        elif args.command in ("status", "cleanup") and not args.run:
            # Report on the latest run; never start a new one just to look.
            args.run = read_current() or (run_directories()[-1].name if run_directories() else None)
            if not args.run:
                say("No conversions recorded yet.")
                return 0
        else:
            args.run = resolve_run_name(args.run, args.new, args.plug_ip,
                                        ask=args.command == "run")
        ctx = Context(args)
    except QuitRun:
        say("Stopped. Nothing was changed.")
        return 130
    except (Abort, ValueError, OSError) as exc:
        say(f"REFUSING: {exc}")
        return 2

    if args.command == "list":
        directories = run_directories()
        if not directories:
            say("No conversions recorded yet.")
            return 0
        for directory in directories:
            state = RunState.open(directory)
            done = sum(1 for step, _, _ in STEPS if state.is_done(step))
            mark = "verified" if run_is_complete(directory) else "in progress"
            say(f"  {directory.name:<22} {done}/{len(STEPS)} steps  {mark}"
                f"  {state.input('plug_ip') or ''}")
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
    code = None  # 130 only when the operator chose to quit, not for Ctrl-C
    try:
        code = cmd_run(ctx)
        return code
    except QuitRun:
        output.stage_end("stopped")
        code = 130
        return code
    except KeyboardInterrupt:
        output.stage_end("stopped")
        tell("")
        tell("Stopped with Ctrl-C. Run again to pick up where you left off or to start again")
        tell("(Every step checks the plug first, so running again is safe.)")
        return 130
    finally:
        if code == 130:
            say_quit(ctx)
        else:
            release_firewall(ctx)
        output.finish()


if __name__ == "__main__":
    raise SystemExit(main())
