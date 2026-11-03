"""The plug in its eWeLink account: finding one of a kind this tool converts on
this network, and the steps a run from Sonoff's firmware starts with - the
only part of a run that reaches eWeLink.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, TYPE_CHECKING

from tools.ewelink import discover_ewelink
from tools.autoflash import hostsys, join, kinds, output, plugs, prompts, runs, setup_network
from tools.autoflash.errors import Abort
from tools.ewelink.query_ota import ota_endpoints

if TYPE_CHECKING:
    from tools.autoflash.context import Context


def step_pair(ctx: Context) -> dict[str, Any]:
    """A1 - find the plug: one of a kind this tool converts in the owner's
    eWeLink account, answering on this network, asking only when there is a
    choice to make."""
    ip = ctx.state.input("plug_ip")
    if not ip:
        output.say("Signing in to eWeLink to see which supported devices are in the account...")
        devices = ctx.account_devices()
        ip = find_any_plug() if devices is None else find_account_plug(ctx, devices)
    ctx.state.set_input("plug_ip", ip)
    account = getattr(ctx, "account_plug", None)
    if account:
        ctx.state.set_input("plug_kind", account.kind)
    mac = hostsys.mac_of(ip) or (account.mac if account else None)
    if mac and plugs.MAC_RE.match(mac):
        ctx.state.set_input("plug_mac", mac)
        runs.file_plug(ctx, mac, account)
    return {"plug_ip": ip, "plug_mac": mac}


@dataclass(frozen=True)
class AccountPlug:
    """A plug in the owner's eWeLink account of a kind this tool converts:
    what the account says about it.  `kind`: its kinds.Kind's key."""
    device_id: str
    name: str
    model: str
    online: bool | None
    mac: str | None
    ssid: str | None
    kind: str


@dataclass(frozen=True)
class Sighting:
    """Where an account's plug was seen: answering eWeLink's local discovery
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
        generic = {"plug", "smart", "socket", *kinds.name_words(),
                   str(device.get("productModel") or "").lower()}
        if not set(name.lower().split()) <= generic:  # "Sonoff S60" names nobody
            output.private(name, "plug-name")
        output.private(params.get("staMac"), "mac")
        output.private(params.get("ssid"), "wifi-name")


def account_plug(device: dict[str, Any]) -> AccountPlug | None:
    """The account's record of a device, if it is of a kind this tool
    converts; its LAN key is left out."""
    product = str(device.get("productModel") or "").strip().upper()
    family = str((device.get("extra") or {}).get("model") or "")
    kind = kinds.for_account(product, family)
    if kind is None:
        return None
    params = device.get("params") or {}
    mac = params.get("staMac")
    online = device.get("online")
    return AccountPlug(
        device_id=str(device.get("deviceid") or ""),
        name=str(device.get("name") or "").strip() or kind.name,
        model=product or kind.short,
        online=online if isinstance(online, bool) else None,
        mac=hostsys.normalise_mac(str(mac)) if mac else None,
        ssid=str(params.get("ssid") or "") or None,
        kind=kind.key,
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


def ready(sighting: Sighting) -> bool:
    """Found, and ready to convert from here: answering eWeLink's discovery
    and online in eWeLink - or, for a kind whose conversion needs no LAN
    Control, online and at the hardware address the account gives it."""
    if sighting.answering:
        return not cut_off(sighting)
    kind = kinds.by_key(sighting.plug.kind)
    return (bool(sighting.ip) and sighting.plug.online is True
            and kind is not None and not kind.uses_lan_control)


def cut_off(sighting: Sighting) -> bool:
    """Answering here while eWeLink shows it offline: the plug is on the
    network but cannot reach the internet, so the upgrade command, which only
    reaches it through eWeLink, would never arrive."""
    return sighting.answering and sighting.plug.online is False


# An update cut off part way - this computer stopped while it was being sent -
# can leave the plug on Sonoff's firmware but away from eWeLink until it restarts.
AFTER_A_CUT_OFF_SEND = ("switch the device off and on: after an update cut off part way "
                        "it can stay away from eWeLink until it restarts.")


def why_not_found(sighting: Sighting) -> str:
    """What to do about an account plug that is not answering here, or that
    is answering but cannot be converted from here."""
    plug = sighting.plug
    if cut_off(sighting):
        return (f"{describe_plug(plug)} answers at {sighting.ip}, but eWeLink shows it "
                "offline - it cannot reach the internet. The conversion is sent to the "
                "device through eWeLink, so allow it internet access for this run (you can "
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
    """Look for the account's plugs on this network."""
    macs = {plug.mac for plug in plugs if plug.mac}
    # Where this computer has already seen them: asked straight away.
    hints = [address for address, mac in hostsys.neighbour_table().items() if mac in macs]
    try:
        report = discover_ewelink.discover(6.0, hints=hints, wanted={plug.device_id for plug in plugs
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
# A plug that did not answer is looked for again this many times, this far
# apart, before the person is asked what now: one just plugged in, or a busy
# Wi-Fi, answers a moment later, and looking again is what they would choose.
LOOK_AGAIN = 2
LOOK_AGAIN_PAUSE = 15.0


def looking_again(looked: int) -> bool:
    """Whether to look again by itself, having looked `looked` times more;
    waits LOOK_AGAIN_PAUSE first, saying so."""
    if looked >= LOOK_AGAIN:
        return False
    output.say(f"    Not found yet - looking again in {LOOK_AGAIN_PAUSE:.0f}s.")
    output.progress("not found yet - looking again shortly...")
    time.sleep(LOOK_AGAIN_PAUSE)
    output.progress_clear()
    return True


def find_account_plug(ctx: Context, devices: list[dict[str, Any]]) -> str:
    """The address of the plug to convert, from the account's that answer here."""
    started = time.monotonic()
    looked = 0
    while True:
        plugs = [plug for plug in map(account_plug, devices) if plug is not None]
        if not plugs:
            output.transient_begin()
            others = [f"- '{str(d.get('name') or '').strip() or d.get('deviceid')}' "
                      f"({d.get('productModel') or 'unknown model'})" for d in devices]
            prompts.section(f"No supported device in the eWeLink account {ctx.email()}", [
                *(["It has:", *others] if others else ["It has no devices."]),
                "",
                "Add the device in the eWeLink app, or use the account it is in.",
            ])
            choice = prompts.ask_choice("What now", [
                "Look again",
                "Use a different eWeLink account",
                "Type the device's address myself",
            ])
            output.transient_end()
        else:
            output.say("Looking for it on your network...")
            sightings = sight_plugs(plugs)
            found = [sighting for sighting in sightings if ready(sighting)]
            if len(found) == 1:
                # The stage line completing says it all; the log keeps which plug.
                output.say(f"    Found {describe_plug(found[0].plug)} at {found[0].ip}.")
                ctx.account_plug = found[0].plug
                return str(found[0].ip)
            if found:
                output.transient_begin()
                choice = prompts.ask_choice(
                    "More than one device in the account answered. Which one is this run for",
                    [f"{describe_plug(sighting.plug)} at {sighting.ip}" for sighting in found],
                )
                output.transient_end()
                ctx.account_plug = found[choice].plug
                return str(found[choice].ip)
            waited = time.monotonic() - started
            if all(sighting.plug.online is False for sighting in sightings) and waited < CLOUD_CATCH_UP:
                # Offline in eWeLink: usually a plug that has only just started -
                # plugged in as the run began - and is seconds from joining the
                # Wi-Fi and the cloud. Answering here already, or not yet.
                if all(map(cut_off, sightings)):
                    output.say(f"    It answers here but eWeLink shows it offline; checking again ({waited:.0f}s)...")
                else:
                    output.say(f"    eWeLink shows it offline - just switched on, perhaps; checking again ({waited:.0f}s)...")
                output.progress(f"waiting for eWeLink to see the device online... {waited:.0f}s")
                time.sleep(10.0)
                output.progress_clear()
                refreshed = ctx.account_devices()
                if refreshed is None:
                    return find_any_plug()
                devices = refreshed
                continue
            if looking_again(looked):
                looked += 1
                refreshed = ctx.account_devices()
                if refreshed is None:
                    return find_any_plug()
                devices = refreshed
                continue
            output.transient_begin()
            if all(map(cut_off, sightings)):
                # Found here: only its own internet access is in the way.
                title, note = "Your device is on this network but cannot reach the internet", []
            else:
                title, note = ("Your device did not answer on this network",
                               hostsys.security_software_note())
            prompts.section(title, [
                *(f"- {why_not_found(sighting)}" for sighting in sightings),
                *([""] + note if note else []),
            ])
            choice = prompts.ask_choice("What now", [
                "Look again",
                "Use a different eWeLink account",
                "Type the device's address myself",
            ])
            output.transient_end()
        if choice == 2:
            output.transient_begin()  # gone once answered, as the menu was
            ip = prompts.ask_ipv4(
                "The device's address",
                help_lines=["Find your device's IP address in your router's device list, or eWeLink's device information."],
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
    looked = 0
    while not ip:
        output.say("Looking for supported devices on your network...")
        plugs = find_ewelink_plugs()
        if len(plugs) == 1:
            ip, name = plugs[0]
            output.say(f"Found {name} at {ip}.")
        elif plugs:
            output.transient_begin()
            choice = prompts.ask_choice(
                "More than one device answered. Which one is this run for",
                [f"{name} at {address}" for address, name in plugs],
            )
            output.transient_end()
            ip = plugs[choice][0]
        elif looking_again(looked):
            looked += 1
        else:
            # Taken off the screen once answered, so looking again does not
            # stack another copy under the stage line.
            output.transient_begin()
            note = hostsys.security_software_note()
            prompts.section("No supported device answered on this network", [
                "Check, in the eWeLink app:",
                "- the device is added, on the same 2.4 GHz Wi-Fi network as this",
                "  computer - the device cannot use 5 GHz",
                "- LAN Control is turned on in the device's settings",
                "- the device is switched on at the wall",
                *([""] + note if note else []),
            ])
            choice = prompts.ask_choice(
                "What now",
                ["Look again", "Type the device's address myself"],
            )
            if choice == 1:
                ip = prompts.ask_ipv4(
                    "The device's address",
                    help_lines=["Find your device's IP address in your router's device list, or eWeLink's device information."],
                    check=plug_at(None),
                )
            output.transient_end()
    return ip


def plug_at(wanted: set[str] | None) -> Callable[[str], list[str]]:
    """For a typed address: what is wrong with it, unless a plug in the
    account (`wanted`'s ids; None: any eWeLink device) answers eWeLink's
    discovery there.  The next stage asks it there, and needs the answer to
    go on: an address that only answers a ping - the router, say - would
    stop the run then, with nothing to show what was wrong."""
    what = "a supported device" if wanted is None else "your device"

    def check(address: str) -> list[str]:
        output.tell(f"    Checking that {address} is {what}...")
        try:
            found = discover_ewelink.answered_ids(discover_ewelink.discover(6.0, None, address)["packets"])
        except OSError:
            found = set()
        if found and (wanted is None or found & wanted):
            return []
        if found:
            return [f"{address} is a different eWeLink device, not one of this account's supported devices."]
        return [
            f"{address} answered, but not as an eWeLink device - it is probably something",
            "else, such as your router. If it is the device, turn on LAN Control in its",
            "settings in the eWeLink app.",
        ]
    return check


def find_ewelink_plugs(timeout: float = 6.0) -> list[tuple[str, str]]:
    """(address, name) for each eWeLink device answering mDNS on this network."""
    try:
        report = discover_ewelink.discover(timeout)
    except OSError:
        return []
    found: dict[str, str] = {}
    for packet in report.get("packets", []):
        source = packet.get("source")
        if not source or "records" not in packet:
            continue
        label = found.get(source, "an eWeLink device")
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
            f"the device at {plug_ip} is already running our installer firmware, from "
            "an earlier conversion. Run this again and choose to continue that "
            "conversion. If none is offered, switch the device off and on: it goes "
            "back to its Sonoff firmware, and a new conversion can start"
        )
    if "tasmota" in body.lower():
        raise Abort(
            f"{plug_ip} is running Tasmota, not Sonoff's firmware. If that is this "
            "device, part way through an earlier conversion, run this again and "
            "choose to continue that conversion"
        )


def step_discover(ctx: Context) -> dict[str, Any]:
    """A2/A3/A4 - identify the device and read its OTA metadata."""
    plug_ip = ctx.state.require("plug_ip")[0]
    mdns = ctx.dir / "mdns.json"
    meta = ctx.dir / "ota-metadata.json"

    # The same report the discovery tool writes, without its console output.
    report = discover_ewelink.discover(8.0, None, plug_ip)
    if not report["packets"]:
        already_converting(plug_ip)
        note = hostsys.security_software_note()
        if note:
            prompts.section("Something on this computer may be in the way", note)
        raise Abort(f"the device at {plug_ip} did not answer the eWeLink discovery; check "
                    "it is switched on and LAN Control is on in the eWeLink app")
    mdns.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    email = ctx.email()
    env = ctx.ewelink_env()
    if hostsys.run_tool(hostsys.python_script("tools/ewelink/query_ota.py", "--mdns-capture", str(mdns),
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
    kind = kinds.for_stock_model(model)
    if kind is None:
        raise Abort(
            f"{plug_ip} is a {model or 'device of unknown model'}, which this tool does not "
            "convert yet. Nothing was written"
        )
    ctx.state.set_input("plug_kind", kind.key)

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
    prompts.section("What the device reported", quiet=True, body=[
        f"Model            {model}",
        f"Product          {product or 'not given'}",
        f"Firmware now     {version}",
        f"Update offered   {offered}",
        "",
        "The device fetches our firmware straight from this computer, so no",
        "router rule is needed.",
    ])
    # The earliest reading of a run from Sonoff's firmware: before anything is
    # written, while moving the plug costs nothing.
    signal = join.note_signal(ctx, join.advert_signal(ctx, report, plug_ip), [
        "Nothing has been written yet. The conversion restarts the device",
        "several times, and each time it has to rejoin your Wi-Fi on its own;",
        "a weak signal makes that less certain."])
    ctx.state.set_input("stock_version", version)
    ctx.state.set_input("product_model", product)
    return {"stock_version": version, "update_offered": bool(endpoints), "model": model,
            "signal_dbm": signal}
