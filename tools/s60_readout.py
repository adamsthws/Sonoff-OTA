#!/usr/bin/env python3
"""Read a stock Sonoff S60's whole flash over Wi-Fi, then put it back to stock.

This is not the conversion. It installs a small read-only firmware into the
plug's spare app slot, reads the plug's whole memory through it, and saves
that under captures/flash-readout/. Switching the plug off and on afterwards
boots its Sonoff firmware again, as it was.

What it costs: the spare slot is overwritten, so whatever was there - often
the Sonoff version the plug had before its last update - is gone. The Sonoff
firmware it is running, its settings and its pairing are kept.

    python3 tools/s60_readout.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.analyze_vendor_ota import analyze  # noqa: E402
from tools.autoflash import hostsys  # noqa: E402
from tools.autoflash.output import PROBLEM_RE  # noqa: E402
from tools.autoflash.prompts import (  # noqa: E402
    QuitRun,
    ask_choice,
    ask_secret,
    ask_text,
    find_ssid,
    section,
    warning_box,
)
from tools.discover_ewelink import discover  # noqa: E402
from tools.dump_stock_flash import fetch, is_readout_bridge, save_dump  # noqa: E402
from tools.s60_autoflash import (  # noqa: E402
    BRIDGE_AP_PREFIX,
    BRIDGE_PASSWORD,
    DELIVERY_PORTS,
    SETTINGS_PATH,
    find_ewelink_plugs,
)
from tools.serve_tasmota_ota import S60_DEVICE_MODEL  # noqa: E402

# The readout build of the conversion bridge, at the same version.
READOUT_OTA = Path("captures/flash-readout/s60-ota-bridge-flash-readout-4.4.1.ota")
# --control: a conversion bridge, only to see whether it starts on this plug.
# 4.4.1 is the one the conversion sends.  Pinned as in SHA256SUMS.
CONTROL_FILES = {
    "4.4.1": (Path("artifacts/s60-ota-bridge-4.4.1.ota"),
              "c77ca1f3cde6de0d37f408cb59cb36c75fa5a6b1c6c1f4435a861e043b9bc6b5"),
}
CONFIRM_WORD = "readout"
# How a bridge names itself: "bridge v5" up to v5, "bridge 4.4.1" from then on.
BRIDGE_ID = rb"bridge (?:v\d+|\d+\.\d+\.\d+)"
BRIDGE_AP_HOST = "192.168.4.1"
STAGES = 6
STOCK_LAN_PORT = 8081


class Stop(RuntimeError):
    pass


def stage(number: int, title: str) -> None:
    print(f"\n[{number}/{STAGES}] {title}")


def check_bridge_image(path: Path, marker: bytes) -> None:
    """A locally built bridge is checked as an image here, not pinned."""
    try:
        data = path.read_bytes()
        report = analyze(data)
    except (OSError, ValueError) as exc:
        raise Stop(f"the firmware {path} cannot be used: {exc}. "
                   "Build it as archive/bridge/README.md describes") from exc
    wrapper, esp = report["wrapper"], report["esp_image"]
    if not (wrapper["payload_crc32_valid"] and wrapper["record_crc32_valid"]
            and esp["chip_is_esp32_c3"] and esp["checksum_valid"] and esp["sha256_valid"] is not False):
        raise Stop(f"{path} failed its integrity checks; rebuild it")
    if not re.search(marker, data):
        raise Stop(f"{path} is not the expected bridge build")


def check_readout_file(path: Path) -> None:
    check_bridge_image(path, BRIDGE_ID + rb" flash readout")


def check_control_file(path: Path, expected: str | None) -> None:
    if expected is None:
        check_bridge_image(path, BRIDGE_ID + rb"\n")
        return
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise Stop(f"cannot read {path}: {exc}") from exc
    if digest != expected:
        raise Stop(f"{path} does not match its pinned SHA-256")


def is_any_bridge(host: str) -> bool:
    """True when `host` answers as any bridge: "bridge v5" or "bridge 4.4.1" on."""
    try:
        status, _ = fetch(host, "/status", timeout=5)
    except OSError:
        return False
    return re.match(BRIDGE_ID, status) is not None


def choose_plug(plug_ip: str | None) -> str:
    if plug_ip:
        return plug_ip
    while True:
        print("Looking for Sonoff plugs on your network...")
        plugs = find_ewelink_plugs()
        if len(plugs) == 1:
            print(f"Found {plugs[0][1]} at {plugs[0][0]}.")
            return plugs[0][0]
        if plugs:
            index = ask_choice("Which plug should be read", [f"{name} at {ip}" for ip, name in plugs])
            return plugs[index][0]
        if ask_choice("No Sonoff plug answered. Check it is on, with LAN Control on in eWeLink",
                      ["Look again"]) == 0:
            continue


def account_email(given: str | None) -> str:
    if given:
        return given
    try:
        last = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")).get("ewelink_email")
    except (OSError, ValueError):
        last = None
    return ask_text("eWeLink account email", default=last)


def query_plug(plug_ip: str, email: str, env: dict[str, str], work: Path) -> str:
    """The plug's stock version, after making sure it is an S60."""
    report = discover(8.0, None, plug_ip)
    if not report["packets"]:
        raise Stop(f"the plug at {plug_ip} did not answer; check LAN Control is on in eWeLink")
    mdns = work / "mdns.json"
    mdns.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    meta = work / "ota-metadata.json"
    try:
        completed = subprocess.run(
            [sys.executable, "tools/query_ota.py", "--mdns-capture", str(mdns),
             "--email", email, "--output", str(meta)],
            cwd=ROOT, env=env, capture_output=True, text=True,
        )
        if completed.returncode != 0:
            last = (completed.stderr or completed.stdout).strip().splitlines()[-1:]
            raise Stop("the eWeLink query failed" + (f": {last[0]}" if last else ""))
        query = json.loads(meta.read_text(encoding="utf-8"))["query"]
    finally:
        # Its download links may work as access tokens.
        meta.unlink(missing_ok=True)
    model, version = str(query.get("model", "")), str(query["version"])
    if model != S60_DEVICE_MODEL:
        raise Stop(f"{plug_ip} is a {model or 'device of unknown model'}, not a Sonoff S60")
    return version


def confirmed(version: str, control: bool = False) -> bool:
    if control:
        opening = [
            "This replaces the plug's spare app slot with a conversion bridge, only",
            "to see whether it starts. Nothing is read, and nothing is installed",
            "after it. The",
        ]
    else:
        opening = ["This replaces the plug's spare app slot with a read-only firmware. The"]
    warning_box("Before going on", [
        *opening,
        f"Sonoff {version} it is running, its settings and its eWeLink pairing stay;",
        "whatever is in the spare slot (often an older Sonoff version) is lost.",
        "Until you switch the plug off and on at the end, eWeLink cannot control it.",
        "As with every first install on a stock plug, there is no automatic",
        "rollback if the new firmware failed to start at all.",
    ])
    question = f"Type {CONFIRM_WORD.upper()} to go ahead - anything else stops, changing nothing: "
    return input(question).strip().lower() == CONFIRM_WORD


def deliver(plug_ip: str, listen_ip: str, port: int, email: str, version: str,
            work: Path, env: dict[str, str], firmware: Path = READOUT_OTA,
            verbose: bool = False) -> int:
    """Run the reviewed sender with the recovery lock lifted only for it.

    Its output is kept in the run's sender.log. On screen, the per-request
    HTTP lines become one progress line and the cloud's running commentary is
    left out, unless in verbose mode; anything reporting a problem is shown.
    """
    lock = ROOT / "RECOVERY_LOCK"
    moved = ROOT / "RECOVERY_LOCK.owner-authorized"
    lock.rename(moved)
    try:
        process = subprocess.Popen(
            [sys.executable, "-u", "tools/serve_tasmota_ota.py",
             "--listen-ip", listen_ip, "--listen-port", str(port),
             "--device-ip", plug_ip, "--firmware", str(firmware),
             "--email", email, "--expected-current-version", version,
             "--mdns-capture", str(work / "mdns.json"),
             "--i-understand-stock-has-no-automatic-rollback"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        total = (ROOT / firmware).stat().st_size if (ROOT / firmware).is_file() else 0
        progress = SenderProgress(total)
        with open(work / "sender.log", "w", encoding="utf-8") as log:
            for line in process.stdout:
                log.write(line)
                if verbose:
                    print(line, end="")
                    continue
                kind = sender_line_kind(line)
                if kind == "served":
                    progress.update(line)
                elif kind == "show":
                    progress.finish()
                    print(line, end="")
        progress.finish()
        return process.wait()
    finally:
        moved.rename(lock)


SERVED_RE = re.compile(r"Served /\S+ bytes ([\d,]+)-([\d,]+)")
REQUEST_RE = re.compile(r'^\s*\[[\d.]+\] "GET [^"]*" (\d{3})')


def is_cloud_chatter(line: str) -> bool:
    """A sender line about routine cloud messages, rather than a problem."""
    return line.lstrip().startswith("Cloud:") and not PROBLEM_RE.search(line)


def sender_line_kind(line: str) -> str:
    """"served" for a delivered range, "hide" for routine chatter, else "show"."""
    if SERVED_RE.search(line):
        return "served"
    request = REQUEST_RE.match(line)
    if request:
        return "hide" if request.group(1).startswith("2") else "show"
    return "hide" if is_cloud_chatter(line) else "show"


class SenderProgress:
    """One line, redrawn in place, for how far the plug has got."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.reached = 0
        self.drawn = False

    def update(self, line: str) -> None:
        match = SERVED_RE.search(line)
        self.reached = max(self.reached, int(match.group(2).replace(",", "")) + 1)
        done = (f"{100 * min(self.reached, self.total) // self.total}%" if self.total
                else f"{self.reached:,} bytes")
        sys.stdout.write(f"\r  The plug is downloading: {done}   ")
        sys.stdout.flush()
        self.drawn = True

    def finish(self) -> None:
        if self.drawn:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.drawn = False


def stock_answers(plug_ip: str) -> bool:
    """True when the Sonoff firmware's LAN service is listening at `plug_ip`."""
    try:
        with socket.create_connection((plug_ip, STOCK_LAN_PORT), timeout=3):
            return True
    except OSError:
        return False


def find_bridge(plug_ip: str, mac: str | None, *, timeout: float = 120.0,
                probe: Callable[[str], bool] = is_readout_bridge,
                by_mac: Callable[[str, str], str | None] = hostsys.find_by_mac,
                clock: Callable[[], float] = time.monotonic,
                sleep: Callable[[float], None] = time.sleep) -> str | None:
    """The readout bridge's address on the home network, if it joined it.

    It joins with the plug's own hardware address, so it is usually where the
    plug was; if the router moved it, it is found by that address.
    """
    started = clock()
    while clock() - started < timeout:
        if probe(plug_ip):
            return plug_ip
        moved = by_mac(mac, plug_ip) if mac else None
        if moved and moved != plug_ip and probe(moved):
            return moved
        sleep(3.0)
    return None


def wait_for_stock(plug_ip: str, timeout: float = 180.0) -> bool:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if any(ip == plug_ip for ip, _ in find_ewelink_plugs(timeout=5.0)):
            return True
    return False


def run(args: argparse.Namespace) -> int:
    print(__doc__.split("\n\n")[1])
    print("Nothing on the plug changes until you type READOUT.")

    stage(1, "Checking this computer")
    if args.control:
        firmware, expected = CONTROL_FILES[args.control]
        probe = is_any_bridge
        name = f"the published bridge ({args.control})"
        check_control_file(ROOT / firmware, expected)
    else:
        firmware, probe, name = Path(args.firmware), is_readout_bridge, "the readout firmware"
        check_readout_file(ROOT / firmware)
    if not (ROOT / "RECOVERY_LOCK").exists():
        raise Stop("RECOVERY_LOCK is not in place; refusing to start from an unsafe state")
    print(f"{name[0].upper()}{name[1:]} and safety lock: OK")

    stage(2, "Finding the plug")
    plug_ip = choose_plug(args.plug_ip)
    mac = hostsys.mac_of(plug_ip)
    listen_ip = args.listen_ip or hostsys.local_ip_towards(plug_ip)
    port = hostsys.first_free_port(listen_ip, DELIVERY_PORTS)
    if port is None:
        raise Stop(f"ports {DELIVERY_PORTS[0]} and {DELIVERY_PORTS[1]}-{DELIVERY_PORTS[-1]} are all in use")

    stage(3, "Asking the plug what it is running")
    email = account_email(args.email)
    env = {**os.environ, "EWELINK_PASSWORD": ask_secret(f"eWeLink password for {email}: ")}
    work = ROOT / "captures" / "flash-readout" / f"run-{time.strftime('%Y%m%d-%H%M%S')}"
    work.mkdir(parents=True, exist_ok=True)
    version = query_plug(plug_ip, email, env, work)
    print(f"Sonoff S60 at {plug_ip}{f' ({mac})' if mac else ''}, running Sonoff {version}.")
    if not confirmed(version, args.control):
        print("Stopped. Nothing was sent to the plug.")
        return 1

    rule = None
    try:
        stage(4, f"Installing {name}")
        if hostsys.active_firewall():
            print("Adding a temporary firewall rule so the plug can fetch the firmware;")
            print("you may be asked for this computer's password.")
            try:
                rule = hostsys.open_firewall(plug_ip, [port])
            except OSError as exc:
                print(f"Could not add it ({exc}); carrying on.")
        code = deliver(plug_ip, listen_ip, port, email, version, work, env, firmware, args.verbose)
        env.pop("EWELINK_PASSWORD", None)
        if code == 2:
            raise Stop("the sender refused before contacting the plug; nothing was sent")

        stage(5, f"Checking {name} started" if args.control else "Reading the plug's memory")
        print(f"Waiting for {name} to start and join your Wi-Fi...")
        host = find_bridge(plug_ip, mac, probe=probe)
        while host is None:
            network = find_ssid(BRIDGE_AP_PREFIX)
            if network is None and stock_answers(plug_ip):
                raise Stop(
                    f"the plug restarted on its Sonoff firmware, so {name} is not running"
                    f"{'' if args.control else ' and nothing was read'}. The plug is working as "
                    "before (only its spare slot changed); there is no need to switch it off and "
                    f"on. The sender's output is in {(work / 'sender.log').relative_to(ROOT)}")
            if network is not None and args.control:
                host = f"its own network, {network}"
                break
            if network is None:
                section(f"Neither {name} nor the Sonoff firmware is answering", [
                    "The plug may still be starting up.",
                ])
                ask_choice("Type 1 to look again", ["Look again"])
                host = find_bridge(plug_ip, mac, timeout=30.0, probe=probe)
                continue
            section("The readout firmware did not join your Wi-Fi, but its own network is up", [
                f"Connect this computer to the Wi-Fi network {network}",
                f"(password {BRIDGE_PASSWORD}), then come back here.",
            ])
            ask_choice("Type 1 once this computer is on that network",
                       [f"I've joined {network} - read the plug"])
            host = BRIDGE_AP_HOST if is_readout_bridge(BRIDGE_AP_HOST) else None
            if host is None:
                print(f"Nothing answers as the readout firmware at {BRIDGE_AP_HOST} yet.")
        if args.control:
            print(f"{name[0].upper()}{name[1:]} started on this plug ({host}): the Sonoff updater")
            print("accepted it and it runs.")
        elif save_dump(host, args.label or f"stock-{version}") != 0:
            raise Stop("the dump was not complete or not well formed. The readout firmware is "
                       f"still running: retry with python3 tools/dump_stock_flash.py --host {host}")
    finally:
        if rule is not None and not hostsys.close_firewall(rule):
            print("Remove the temporary firewall rule yourself:")
            print("  " + hostsys.display(hostsys.firewall_commands(rule.kind, rule.plug_ip, rule.ports)[1]))

    stage(6, "Putting the plug back to stock")
    print("Switch the plug off at the wall, wait five seconds, and switch it on again.")
    ask_choice("Type 1 once the plug is switched back on", ["It's switched back on"])
    print("Waiting for the Sonoff firmware to answer...")
    if wait_for_stock(plug_ip):
        print(f"The plug is answering as an eWeLink device at {plug_ip} again: back on stock.")
    else:
        print(f"Nothing has answered at {plug_ip} yet. Check the eWeLink app; if the plug")
        print("is missing, switch it off and on once more.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plug-ip", help="the plug's address, instead of looking for it")
    parser.add_argument("--email", help="the eWeLink account the plug is in")
    parser.add_argument("--listen-ip", help="this computer's address as the plug sees it")
    parser.add_argument("--label", help="name for the saved dump (default: stock-<version>)")
    parser.add_argument("--firmware", default=str(READOUT_OTA), help=argparse.SUPPRESS)
    parser.add_argument("--control", nargs="?", const="4.4.1", choices=sorted(CONTROL_FILES),
                        help="send a conversion bridge instead, only to see whether it starts "
                             "(nothing is read): 4.4.1, the one the conversion sends")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="also show the cloud's messages while the firmware is delivered")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except Stop as exc:
        print(f"\nStopped: {exc}.", file=sys.stderr)
        return 1
    except (QuitRun, KeyboardInterrupt, EOFError):
        print("\nStopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
