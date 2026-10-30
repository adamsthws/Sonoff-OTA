"""The plug onto the Wi-Fi, and found there: waiting for it to come back on the
home network, giving it the Wi-Fi through its own network when it cannot,
and how good its signal is.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, TYPE_CHECKING

from tools.autoflash import (
    device,
    ewelink_lan,
    hostsys,
    interrupt,
    names,
    output,
    prompts,
    setup_network,
)
from tools.autoflash.device import DeviceError
from tools.autoflash.errors import Abort
from tools.espressif.safeboot.prepare_imprinted_bundle import ask_credentials, ask_password, bundle_ssid

if TYPE_CHECKING:
    from tools.autoflash.context import Context

# Below this the startup firmware may struggle to rejoin on its own after E3.
WEAK_SIGNAL_DBM = -75


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
            output.say(f"    The plug is on your network at {found}.")
            return found
        output.progress(f"looking for the plug on your network... {time.monotonic() - started:.0f}s")
        time.sleep(5)
    output.progress_clear()
    other = None
    if setup:
        other = (f"It is showing its own Wi-Fi network ({names.setup_network_hint(ctx)})",
                 lambda: configure_plug_wifi(ctx, None))
    found = prompts.wait_or_ask(ctx.state.input("plug_ip"), what="the plug", timeout=60.0, other=other)
    ctx.set_plug_ip(found)
    return found


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
    output.say(f"    Connected to {network}.")


def configure_plug_wifi(ctx: Context, network: str | None) -> str:
    """Give Tasmota, waiting on its setup network, the bundle's Wi-Fi details.

    The operator joins the setup network; the details are sent from here.  Tasmota
    saves them only after it has connected with them, so a failure leaves the
    plug waiting to be set up, never lost.
    """
    network = network or names.setup_network_hint(ctx)
    credentials = ctx.wifi(why=[
        "Tasmota could not be given them automatically, so it is waiting on its",
        "own setup network for them. Type them here and this sends them to the",
        "plug for you once you have joined that network.",
    ])
    prompts.section("Join the plug's setup network", [
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
        name_with_the_wifi(ctx)
        ssid, password = credentials
        for attempt in range(1, SETUP_SEND_ATTEMPTS + 1):
            output.tell(f"    Sending the Wi-Fi details for '{ssid}'. The plug tests them first -")
            output.tell("    stay on its network for about 20 seconds...")
            try:
                outcome, address = setup_network.send_wifi(ssid, password)
            except setup_network.SetupError as exc:
                outcome, address = "unknown", None
                output.tell(f"    {exc}")
            if outcome == "saved" or attempt == SETUP_SEND_ATTEMPTS:
                break
            if outcome == "rejected":
                if prompts.UNATTENDED:
                    break  # nobody to type others: on to the plug's page, below
                output.tell(f"    The plug could not connect to '{ssid}' with those details. Type them")
                output.tell("    again - the network's name exactly as it appears, and its password:")
                ssid, password = ask_credentials()
            elif setup_network_gone(ctx, network):
                # No word, as it took them: tested, saved and restarted onto the
                # Wi-Fi before its answer was asked for, its network gone with
                # it (seen in the fault suite). Sent again, the run would wait
                # for this computer to join a network that is no more.
                outcome = "gone"
                break
            else:
                output.tell(f"    No word from the plug; sending them again (attempt {attempt + 1} "
                     f"of {SETUP_SEND_ATTEMPTS}).")
                time.sleep(5)
            wait_until_joined(network)  # dropped off it meanwhile, joined again
        if outcome == "saved":
            output.tell(f"    The plug connected to '{ssid}' and is restarting onto it"
                + (f" at {address}." if address else "."))
            if address:
                ctx.set_plug_ip(address)
        elif outcome == "rejected":
            output.tell(f"    The plug could not connect to '{ssid}' with the bundle's details.")
        elif outcome == "gone":
            output.tell("    No word from the plug, but its network has gone: it has most likely")
            output.tell(f"    joined '{ssid}' with them. It is looked for there.")
        else:
            output.tell("    The plug did not say whether it connected. If it does not turn up")
            output.tell("    on your Wi-Fi, it is still waiting on its setup network: run this")
            output.tell("    again to retry.")
    if outcome in ("rejected", "manual"):
        prompts.section("Set the Wi-Fi up on the plug's page instead", [
            "Stay on the plug's network for this.",
            f"Browse to http://{setup_network.SETUP_HOST}, choose your home Wi-Fi,",
            "type its password and save. When it says the connection worked:",
        ])
    prompts.section("Back to your usual Wi-Fi", [
        "Your computer should rejoin your usual Wi-Fi by itself; if it doesn't,",
        "join it now.",
    ])
    return locate_plug(ctx, timeout=600.0)


# How long the plug's setup network must stay gone, nothing answering on it,
# to be taken as gone - not a moment's drop as it tests the Wi-Fi.
SETUP_GONE_AFTER = 15.0


def setup_network_gone(ctx: Context, network: str) -> bool:
    """Whether the plug's setup network has gone: nothing answering on it for
    SETUP_GONE_AFTER, and not listed by a scan that sees any network (one that
    sees none cannot say)."""
    started = time.monotonic()
    while time.monotonic() - started < SETUP_GONE_AFTER:
        if setup_network.port_open(timeout=2.0):
            return False
        time.sleep(2.0)
    visible = prompts.scan_wifi_ssids(timeout=10.0)
    prefixes = (network,) if network.split()[0] == network else names.setup_networks(ctx)
    return bool(visible) and not any(name.startswith(prefixes) for name in visible)


# Tasmota's own name, from its defaults (tasmota.ino): what its settings
# erased - the 7x power-cycle reset - leave it with.
TASMOTA_DEFAULT_NAME_RE = re.compile(r"^tasmota-[0-9A-F]{6}-\d{4}$")


def name_with_the_wifi(ctx: Context) -> None:
    """A Tasmota on its setup network that has lost its name - its settings
    erased, as switching it off and on 7 times does - named again as it
    saves the Wi-Fi it is given: the name this run gives a Tasmota there
    (autoflash-recovery-CDEF part way through, tasmota-abcdef once F4 has
    named it), in the restart onto the Wi-Fi, which costs no other.  One
    with a name of its own keeps it."""
    if not ctx.state.input("plug_mac"):
        return
    plug = device.Device(setup_network.SETUP_HOST, timeout=8.0)
    try:
        # The name it goes by: Hostname gives the setting, which for the
        # default is the format it is made from (%s-%04d).
        status = plug.command("Status 5")
    except (DeviceError, OSError):
        return
    net = status.get("StatusNET") if isinstance(status, dict) else None
    current = net.get("Hostname") if isinstance(net, dict) else None
    if not (isinstance(current, str) and TASMOTA_DEFAULT_NAME_RE.match(current)):
        return
    name = names.final_name(ctx, "tasmota") if ctx.state.is_done("F4") else names.our_setup_network(ctx)
    if plug.at_next_save([f"Hostname {name}"]):
        output.say(f"    It lost its name with its settings ({current}): it is {name} again once it has the Wi-Fi.")


def check_plug_network(ctx: Context) -> dict[str, Any]:
    """The plug must be on the network the startup firmware will join.

    After E3 the startup firmware rejoins Wi-Fi by itself, with only the details
    in the bundle.  The plug's own connection proves a network name works from
    where it stands; the bundle must name that network.  The password cannot be
    checked - Tasmota never reports it.
    """
    wanted = bundle_ssid(ctx.manifest_path)
    if wanted is None:
        output.say("This bundle's Wi-Fi details are not readable, so the network check is skipped.")
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
        prompts.section("The bundle is set up for a different Wi-Fi network", [
            f"The plug is connected to        '{connected}'",
            f"The bundle's firmware will join '{wanted}'",
            "",
            "Later the plug has to rejoin Wi-Fi using only the bundle's details.",
            "With these it would never come back. Nothing has been written.",
            "",
            f"Give the password for '{connected}' and the startup firmware is set",
            "up for that network instead.",
        ])
        if not prompts.ask_yes_no(f"Enter the password for '{connected}' now", default=True):
            raise Abort(f"the plug is on '{connected}' but the bundle is set up for '{wanted}'")
        ctx.adopt_wifi(connected, ask_password(connected))
        wanted = bundle_ssid(ctx.manifest_path)
    output.transient_end()
    output.say(f"Network check: the plug is on '{connected}', the network the startup "
        f"firmware will join (signal {signal} dBm).")
    warn_if_weak_signal(ctx, signal, ["After the next stage the startup firmware has",
                                      "to rejoin on its own, and a weak signal makes that less certain."])
    return {"network": connected, "signal_dbm": signal}


def plug_signal(device: device.Device) -> int | None:
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
        output.say(f"Wi-Fi signal at the plug: {signal} dBm.")
        warn_if_weak_signal(ctx, signal, then)
    return signal


def warn_if_weak_signal(ctx: Context, signal: Any, then: list[str]) -> None:
    """A weak signal, asked about before going on; `then` says what has to join
    the Wi-Fi on its own afterwards.  Asked once a run: a weak signal agreed to
    is not asked about again at the next reading."""
    if not (isinstance(signal, int) and signal < WEAK_SIGNAL_DBM):
        return
    if ctx.state.input("weak_signal_agreed"):
        output.say("    The signal is still weak; carrying on, as agreed.")
        return
    output.transient_begin()
    prompts.section("The plug's Wi-Fi signal is weak", [
        f"It reports {signal} dBm. {then[0]}", *then[1:],
        "Moving the plug nearer the access point first is worth it.",
    ])
    if not prompts.ask_yes_no("Carry on anyway", default=False):
        raise Abort("stopped for a weak Wi-Fi signal; nothing was written")
    ctx.state.set_input("weak_signal_agreed", True)
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
    prompts.section("Join the plug's own network", [
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
            output.tell(f"    {exc}; sending them again (attempt {attempt + 1} of {SETUP_SEND_ATTEMPTS}).")
            time.sleep(5)
            continue
        output.tell(f"    Sent. The plug joins '{ssid}' now.")
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
    network = prompts.find_ssid(networks, timeout=30)
    if not network and on_a_plug_network():
        network = networks[0]  # joined already: carried on through it
    if not network:
        return None
    output.tell(f"    {what} cannot join your Wi-Fi, so it has raised its own network, {network}.")
    ssid, password = ctx.wifi(why=[
        f"{what} cannot join your Wi-Fi, and is waiting on its own network for its",
        "details. Type them here and this sends them to the plug for you.",
    ])
    for round_ in (1, 2):
        send_through_own_network(network, ssid, password)
        prompts.section("Back to your usual Wi-Fi", [
            "Your computer should rejoin your usual Wi-Fi by itself; if it doesn't,",
            "join it now.",
        ])
        try:
            wait_for(ctx, check, what, timeout=300.0)
            return ssid, password
        except Abort:
            if round_ == 2 or prompts.UNATTENDED:
                raise Abort(f"{what} did not join '{ssid}' with the details sent through its own "
                            "network. Check the network's name and password, then run this again") from None
        output.tell(f"    {what} did not join '{ssid}' with those details: most likely your Wi-Fi's name")
        output.tell("    or password changed since the plug last joined it.")
        output.transient_begin()
        prompts.section("The Wi-Fi details the plug should use", [
            "Type the name and password of the 2.4 GHz network the plug should be on",
            "now. They are sent to the plug through its own network.",
        ])
        ssid, password = ask_credentials()
        output.transient_end()
    raise AssertionError("unreachable")


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
    if (not host or not ctx.state.is_done("D1") or ctx.state.is_done(ctx.steps[-1][0])
            or ctx.state.is_done("I1")):
        return  # not running Tasmota yet, finished, or running ESPHome
    device = ctx.device
    if device.answers(timeout=3.0) or not hostsys.ping_ok(host):
        return  # answering, or not on the network at all: the steps say which
    output.tell("    The plug is on the network but not answering yet. It is most likely still")
    output.tell("    finishing the step that was stopped, and gives up within a minute or two.")
    started = time.monotonic()
    while time.monotonic() - started < BUSY_PLUG_WAIT:
        output.progress(f"waiting for the plug to finish... {time.monotonic() - started:.0f}s")
        if device.answers(timeout=3.0):
            output.progress_clear()
            output.tell("    It is answering again.")
            return
        time.sleep(3.0)
    output.progress_clear()
    output.tell("    It is still not answering. Switch it off and on - at this point that is safe -")
    output.tell("    and run this again; it carries on. Carrying on now, and each step says what it finds.")
