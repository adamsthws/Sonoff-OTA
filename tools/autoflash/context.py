"""The run's context: its arguments, state and record, the secrets held in
memory for it, and the plug and servers it talks through.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import time
from pathlib import Path
from typing import Any

from tools.autoflash import (
    account,
    credentials,
    device,
    firewall,
    hostsys,
    join,
    noise,
    output,
    phases,
    plugs,
    prompts,
    routes,
    runs,
    unlock,
    wifi,
)
from tools.autoflash.errors import Abort
from tools.autoflash.phases import PhaseRunner
from tools.autoflash.routes.espressif import bundle, installer
from tools.autoflash.runs import ROOT
from tools.autoflash.state import RunState
from tools.espressif.safeboot.prepare_imprinted_bundle import (
    BundleError,
    ask_credentials,
    build_bundle,
    bundle_ssid,
    pin_wifi,
    recovery_image,
)
from tools.ewelink.get_device_key import EwelinkError, get_devices, login


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
        self.bridge = Path(getattr(args, "bridge", None) or installer.BRIDGE_OTA)
        self.research_bridge = self.bridge != installer.BRIDGE_OTA
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
        self._finder: join.PlugFinder | None = None
        # The go-ahead to replace the plug's firmware, given in this run.
        self.consented = False
        # The account's record of the plug A1 chose, for its plug.json.
        self.account_plug: account.AccountPlug | None = None
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
        them too, from 1.1.0 (its Wi-Fi action); from an ESPHome or mover
        that does not, they come from the plug's Sonoff backup, when this
        computer has one and the plug is on that network still.  Asking is the
        fallback, with `why` saying what it is for; `--wifi` forces it.
        """
        forced = getattr(self.args, "wifi", False)
        # ESPHome, or the mover, says nothing of its Wi-Fi password: only a
        # Tasmota can be read.
        readable = runs.plug_runs_tasmota(self.state)
        if self._wifi is None and not forced and readable:
            found = wifi.read_wifi_from_plug(self)
            if found is not None:
                self.adopt_wifi(*found)
                # A run stopped at the question earlier must not report it as typed.
                self.state.set_input("wifi_typed", False)
        if self._wifi is None and not forced and not readable:
            found = wifi.wifi_from_esphome(self) or wifi.wifi_from_backup(self)
            if found is not None:
                self.adopt_wifi(*found)
                self.state.set_input("wifi_typed", False)
        if self._wifi is None and not forced and self.esphome_api_locked and not prompts.UNATTENDED:
            # ESPHome gives them, its API unlocked: its key typed instead.
            found = unlock.ask_esphome_key(self, self.state.input("plug_ip"), wifi=True) and wifi.wifi_from_esphome(self)
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
        prompts.section("Your Wi-Fi details are needed", [
            *(["You chose to type them (--wifi)."] if forced else why or [
                "They could not be read off the device (the reason is above).",
                "The startup firmware has to rejoin your Wi-Fi by itself after the",
                "next stage, so it needs them written in.",
            ]),
            "",
            "Use the 2.4 GHz network the device should stay on. The password is held",
            "in memory for this run only, never written to disk, and you are not",
            "asked again later in the run.",
        ])
        while self._wifi is None:
            ssid = bundle_ssid(self.manifest_path)
            if ssid is None:
                self.adopt_wifi(*ask_credentials())
                continue
            output.say(f"The bundle is set up for '{ssid}'.")
            password = prompts.ask_secret(f"Wi-Fi password for '{ssid}': ")
            try:
                self.remember_wifi(ssid, password, recovery_image(self.manifest_path, ssid, password))
            except BundleError as exc:
                output.tell(f"    {exc}.")
                choice = prompts.ask_choice("What now", [
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
            output.say(f"    Writing the Wi-Fi for '{ssid}' into the startup firmware (in memory)...")
            try:
                image = pin_wifi(self.manifest_path, ssid, password)
            except BundleError as first:
                # A bundle this checkout can no longer use: build it again.
                output.say(f"    {first}; preparing the firmware bundle again...")
                try:
                    _, image = build_bundle(ssid, password, self.manifest_path.parent,
                                            release=bundle.release_on_the_plug(self))
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
            output.say("The eWeLink password is the one given in advance (.env or EWELINK_PASSWORD).")
        if self._ewelink_password is None:
            email = self.email()
            output.transient_begin()
            self._ewelink_password = prompts.ask_secret(f"eWeLink password for {email}: ")
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
                account.keep_account_private(devices)
                return devices
            except EwelinkError as exc:
                if "login failed" not in str(exc):
                    output.tell(f"    Could not reach eWeLink ({exc}), so every device that answers is listed.")
                    return None
                self._ewelink_password = None
                output.transient_begin()
                output.tell(f"    {exc}.")
                choice = prompts.ask_choice("What now", [
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
            last = runs.load_settings().get("ewelink_email")
            if last and prompts.ask_choice("Which eWeLink account is this device in", [
                f"The one used last time: {last}",
                "A different account - enter its email",
            ]) == 0:
                email = last
            else:
                email = prompts.ask_text(
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
        settings = runs.load_settings()
        if settings.get("ewelink_email") != email:
            settings["ewelink_email"] = email
            runs.save_settings(settings)

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
        self.plug = runs.run_name(target)
        runs.remember_current(self.plug)
        output.say(f"This run's record is now in {runs.display_path(target)}.")

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
            raise Abort("the device address is not known yet, so this computer's "
                        "address cannot be worked out; pass --listen-ip")
        try:
            derived = hostsys.local_ip_towards(plug_ip)
        except OSError as exc:
            raise Abort(
                f"could not work out how this computer reaches {plug_ip} ({exc}); "
                "check you are on the same network as the device, or pass --listen-ip"
            ) from exc
        address = ipaddress.ip_address(derived)
        if not (address.is_private and address.version == 4):
            raise Abort(f"this computer's address towards the device, {derived}, is not "
                        "a home-network address")
        remembered = self.state.input("listen_ip")
        if remembered != derived:
            if remembered:
                output.say(f"This computer's address towards the device is now {derived} "
                    f"(it was {remembered}).")
            else:
                output.say(f"This computer's address towards the device: {derived}")
            self.state.set_input("listen_ip", derived)
        return derived

    def set_plug_ip(self, address: str) -> None:
        if address != self.state.input("plug_ip"):
            output.say(f"The device is now at {address}.")
            self.state.set_input("plug_ip", address)

    @property
    def steps(self) -> list[tuple[str, str, Any]]:
        """The run's route, as settled at its start (routes.steps_of)."""
        return routes.steps_of(self.state)

    @property
    def finder(self) -> join.PlugFinder:
        if self._finder is None:
            self._finder = join.PlugFinder(self)
        return self._finder

    @property
    def device(self) -> device.Device:
        host = self.state.input("plug_ip")
        if not host:
            raise Abort("plug_ip is not set; pass --plug-ip")
        return device.Device(
            host,
            user=self.state.input("web_user"),
            password=self.web_password,
        )

    def phase_runner(self, with_recovery: bool = True) -> PhaseRunner:
        """The migration server's runner, given the startup firmware - unless
        `with_recovery` is False: a restore with none to prove (step_restore)."""
        port = firewall.choose_port(self, "phase_port", firewall.PHASE_PORTS, "the migration steps")
        firewall.ensure_firewall(self)
        self.last_runner = PhaseRunner(
            repo_root=ROOT,
            manifest=self.manifest_path,
            evidence_dir=self.evidence,
            listen_ip=self.listen_ip(),
            device=self.device,
            log=phases.phase_log,
            listen_port=port,
            # Piped, never a file; and only hashes of captures that hold secrets.
            recovery=self.recovery_bytes() if with_recovery else None,
        )
        return self.last_runner
