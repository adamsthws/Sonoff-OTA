"""The report of a run, for the person to share: what was converted, how long
each stage took, what the plug's hardware does, and where to post it.
"""

from __future__ import annotations

import os
import platform
import re
import sys
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

from tools.autoflash import __version__, esphome, hostsys, kinds, output, plugs, routes, runs
from tools.autoflash.device import DeviceError
from tools.autoflash.runs import ROOT
from tools.ewelink import relay as ewelink_relay

if TYPE_CHECKING:
    from tools.autoflash.context import Context

# Where people share how their conversion went: the pinned "Test reports" thread.
REPORTS_URL = "https://github.com/adamsthws/Sonoff-OTA/discussions/1"
# Shown once a conversion has succeeded, after the report, in this order.
SPONSORS_URL = "https://github.com/sponsors/adamsthws"
KOFI_URL = "https://ko-fi.com/adamsthws"
DONATE_URL = "https://www.paypal.me/adamsthws"


def plug_page_url(plug_ip: str | None) -> str | None:
    """The address to open the converted plug's Tasmota page at.

    Inside the emulator (tools/espressif/emulator) the plug's address only exists in its
    container; the emulator says where your browser reaches it instead.
    """
    emulated = os.environ.get("AUTOFLASH_EMULATOR_URL")
    if emulated:
        return emulated
    return f"http://{plug_ip}" if plug_ip else None


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
    """The Python the tool ran on, and where it came from (start.sh, start.cmd)."""
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
    stopped = next(((number, step) for number, (step, _, _) in enumerate(routes.STEPS, 1)
                    if (step == failed if failed else not ctx.state.is_done(step))), None)
    # No total before the firmware is chosen: STEPS is then only the start
    # every route shares, and how many stages would follow is not known.
    total = f" of {len(routes.STEPS)}" if routes.ROUTE[0] else ""
    where = (f"stage {stopped[0]}{total} ({routes.stage_name(stopped[1])})"
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
               for number, (step, _, _) in enumerate(routes.STEPS, 1)
               if isinstance(captures.get(f"{step}.{key}"), (int, float))]
    return ", ".join(timings) or "not recorded"  # a run from before they were


# What a run's starting point is called in its report: one for each of SOURCES.
CONVERTED_FROM = {"sonoff": "Sonoff", "tasmota": "Tasmota", "esphome": "ESPHome", "openbeken": "OpenBeken",
                  "midway": "unfinished conversion", "committed": "unfinished conversion",
                  "installer": "unfinished conversion", "interim": "unfinished conversion",
                  "safeboot": "Tasmota's safeboot",
                  "untemplated": "Tasmota without its template"}


def report_fields(ctx: Context, seconds: float, resumed: bool,
                  hardware: dict[str, str] | None = None,
                  result: str = "converted") -> list[tuple[str, str]]:
    """This run's answers for a Test reports post, as (label, value) - nothing private.

    No addresses, Wi-Fi name, account or device id: only what is useful to
    someone deciding whether the tool will work for them.  `hardware` is what
    check_hardware() measured.  "__" marks what the run could not tell.
    """
    hardware = hardware or {}
    kind = kinds.of_run(ctx.state)
    # None for a run stopped before its firmware was chosen (A3).
    firmware = routes.recorded_firmware(ctx.state)
    espressif = kind.family == "espressif"
    # A run that starts on a converted plug never asks Sonoff's firmware or
    # eWeLink: what the plug's record kept from its first conversion - the
    # model, and the Sonoff firmware its backup holds - stands in.
    record = plugs.read_plug(ctx.state.input("plug_mac") or "")
    product = str(ctx.state.input("product_model") or record.get("model") or "")
    model = kind.product_of(product)
    stock_version = str(ctx.state.input("stock_version") or record.get("stock_version") or "")
    captures = ctx.state.data.get("captures", {})
    joined = (captures.get("D1.upload_route") == "bridge network"
              or captures.get("D1.resumed") is True
              or (captures.get("D1.upload_route") == "home network" and captures.get("D1.wifi") != "preset")
              or captures.get("F4.wifi_route") == "setup network")
    took = output.elapsed(seconds) + (" (since resuming)" if resumed else "")
    retries = {number: captures[f"{step}.attempts"] - 1 for number, (step, _, _) in enumerate(routes.STEPS, 1)
               if isinstance(captures.get(f"{step}.attempts"), int) and captures[f"{step}.attempts"] > 1}
    retried = [f"{number} ({count} {'retry' if count == 1 else 'retries'})"
               for number, count in retries.items()]
    # The lowest of the run's readings: from Sonoff's advert at A2, from Tasmota
    # at L1, D4, E1 or I1 - wherever the plug's firmware could give one.
    readings = [value for key, value in captures.items() if key.endswith(".signal_dbm")
                and isinstance(value, int) and not isinstance(value, bool)]
    signal = min(readings) if readings else None
    fields = [
        ("Result", result),
        ("Firmware", routes.FIRMWARES[firmware] if firmware else "not chosen"),
        ("Converted from", CONVERTED_FROM[runs.run_source(ctx.state)]),
        ("Tool version", __version__),
        ("Device model", model or "__"),
        ("Stock firmware", stock_version or "__"),
        # How it went, never where it is or which plug: the path holds the MAC.
        # A run from a converted plug takes none: one is there or not.
        ("Sonoff backup", plugs.BACKUP_OUTCOMES[ctx.state.input("backup_outcome")
                                      or ("existing" if routes.sonoff_backup_exists(ctx) else "not taken")][1]),
        ("Computer", computer_description()),
        ("Python", python_description()),
        ("Time taken", took),
        # By stage number.  Busy is how long a Berry stage kept the plug from
        # anything else: at 70-90s a real S60TPG stops answering for good.
        ("Stage times", stage_timings(ctx, "seconds")),
        # Only where the route has them: a Berry stage's busy time, the Wi-Fi
        # typed or a setup network joined are an Espressif route's.
        *([("Device busy", stage_timings(ctx, "plug_busy_seconds"))] if espressif else []),
        ("Retries", ", ".join(retried) or "none"),
        ("Device Wi-Fi signal", f"{signal} dBm" if isinstance(signal, int) else "not recorded"),
        *([("Typed the Wi-Fi", "yes" if ctx.state.input("wifi_typed") else "no"),
           ("Joined setup network", "yes" if joined else "no")] if espressif else []),
        # Whether the device had to be switched off and on to start its new
        # firmware: the emulated S61 never restarts by itself; a real one may.
        *([("Power cycled", "true" if ctx.state.input("power_cycles") else "false")] if not espressif else []),
        ("Checks passed", checks_passed(hardware)),
    ]
    if os.environ.get("AUTOFLASH_EMULATOR_URL"):
        fields.append(("Ran in the emulator", "yes"))
    return [(label, answer(value)) for label, value in fields]


def checks_passed(hardware: dict[str, str]) -> str:
    """The end-of-run checks as one answer, whatever the device: for a plug,
    its relay switching off and on.  What each check found is in the log."""
    relay = hardware.get("Relay", "not checked")
    if relay == "not checked":
        return "not run"
    return "true" if relay.startswith("switches") else "false"


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

    output.say("Checking the relay and power meter...")
    try:
        before = device.command("Power")
        was_on = isinstance(before, dict) and str(before.get("POWER", "")).upper() == "ON"
        switched = switch(not was_on)
        first = meter()
        restored = switch(was_on)
        second = meter()
    except (DeviceError, OSError, ValueError) as exc:
        output.say(f"    The device did not answer the check: {exc}")
        return {key: "could not be checked" for key in ("Relay", "Power meter")}
    on, off = (second, first) if was_on else (first, second)
    result = describe_hardware(switched, restored, on, off)
    for label, value in result.items():
        output.say(f"    {label}: {answer(value)}")
    return result


def check_esphome_hardware(ctx: Context) -> dict[str, str]:
    """check_hardware's two switches and two readings, through ESPHome's web API."""
    host = ctx.state.input("plug_ip")
    output.say("Checking the relay and power meter...")
    was_on = esphome.relay(host)
    if was_on is None:
        output.say("    The device did not answer the check.")
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
        output.say(f"    {label}: {answer(value)}")
    return result


def check_ewelink_hardware(ctx: Context) -> dict[str, str]:
    """check_hardware's two switches for a device back on Sonoff's firmware,
    through eWeLink as the app switches it.  Its meter is not read: eWeLink
    gives it only while the app asks for it, in units each model has its own."""
    device_id = str(plugs.read_plug(ctx.state.input("plug_mac") or "").get("ewelink_id") or "")
    if not device_id:
        return {"Relay": "not checked", "Power meter": "not checked"}
    output.say("Checking the relay through eWeLink...")
    switched = ewelink_relay.check_relay(
        email=ctx.email(), password=ctx.ewelink_env()["EWELINK_PASSWORD"], country_code="+44", region="eu",
        device_id=device_id, say=output.say)
    result = {"Relay": "could not be checked" if switched is None
              else "switches (through eWeLink)" if all(switched) else "did not switch",
              "Power meter": "not checked"}
    output.say(f"    Relay: {answer(result['Relay'])}")
    return result


def invite_report(ctx: Context, seconds: float, resumed: bool) -> None:
    """Ask for a report, with this run's details ready to paste."""
    # OpenBeken takes Tasmota's commands (Power, Status 10): the same check.
    check = {"esphome": check_esphome_hardware, "tasmota": check_hardware, "openbeken": check_hardware,
             "sonoff": check_ewelink_hardware}.get(
        runs.run_firmware(ctx.state), lambda ctx: {})
    with output.working("Generating your report..."):
        fields = report_fields(ctx, seconds, resumed, check(ctx))
    offer_report(ctx, fields, "Please share how it went!")
    output.tell("")
    output.show("You've reached the bit where I hold out my hat!", output.GREEN)
    output.tell("Free software, not free biscuits... Throw your friendly neighbourhood dev a biscuit:")
    output.show_links([(SPONSORS_URL, "GitHub"), (KOFI_URL, "Ko-fi"), (DONATE_URL, "PayPal")], hint=True)


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
    output.tell("")
    output.show(heading, output.GREEN)
    if copied:
        output.tell("Paste your results into the thread (they're already on your clipboard):")
    else:
        output.tell("Copy your results below and post them in the thread...")
    output.show_link(REPORTS_URL, "Test reports")
    output.tell("")
    for line in report_screen(fields):
        output.tell(line)
    if any(value == "__" for _, value in fields):
        output.tell("")
        output.tell("Fill in the blanks (__) if you know them.")
