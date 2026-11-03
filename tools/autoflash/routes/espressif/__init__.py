"""The Espressif routes (the Sonoff S60): every step of every direction,
and what the runner needs to know of each - which write to the plug, which
may be tried again, what each is called and what it leaves behind.
"""

from __future__ import annotations

from typing import Any, Callable, TYPE_CHECKING

from tools.autoflash import account
from tools.autoflash.routes.espressif import bundle, hub, installer, mover, sonoff, tasmota
from tools.autoflash.routes.espressif.evidence import ATTEMPTED_EVIDENCE, DESTRUCTIVE_STEPS  # noqa: F401
from tools.autoflash.routes.espressif.tasmota import STEP_VERIFIERS  # noqa: F401

# Sonoff's firmware goes back on from the plug's own backup: there is no other.
SONOFF_FROM_BACKUP = True

if TYPE_CHECKING:
    from tools.autoflash.context import Context

# What a plug of this family may run: the firmware a run installs, by its key.
FIRMWARES = {"tasmota": "Tasmota", "esphome": "ESPHome", "sonoff": "Sonoff"}
# After this step the plug no longer runs its Sonoff firmware.
POINT_OF_NO_RETURN = "C2"


def resume_check(ctx: Context) -> None:
    """A resumed run checks its bundle first (routes.FAMILY.RESUME_CHECK)."""
    bundle.bundle_of_this_version(ctx)


RESUME_CHECK = resume_check


# From Sonoff's firmware, after the start every route shares (routes.STOCK_START:
# A0, the plug found in eWeLink, the firmware chosen): stock Sonoff layout is
# still in place through D1 inclusive.
COMMON_STEPS: list[tuple[str, str, Callable[[Context], dict[str, Any]]]] = [
    ("A5", "Offline preconditions", bundle.step_check),
    ("A2", "Discover, metadata and device key", account.step_discover),
    ("C2", "Stock to recovery bridge", installer.step_bridge),
    ("C3", "Back up the Sonoff firmware", installer.step_backup),
    ("D1", "Berry-capable Tasmota via bridge AP", installer.step_berry_tasmota),
]
# The hub every route but Tasmota to ESPHome passes through: Sonoff's layout,
# the Berry Tasmota running from slot 1, ESPHome's bootloader in place.
HUB_STEPS: list[tuple[str, str, Callable[[Context], dict[str, Any]]]] = [
    ("D4", "Promote to high slot and freeze NVS", hub.step_promote),
    ("D5", "ESPHome's bootloader, once for every firmware", hub.step_bootloader),
]
# Full Tasmota product: repartition to Safeboot layout, then official image.
TASMOTA_FINAL_STEPS: list[tuple[str, str, Callable[[Context], dict[str, Any]]]] = [
    ("E1", "Preflight (read-only)", tasmota.step_preflight),
    ("E2", "Stage Safeboot into the spare slot", tasmota.step_stage),
    ("E3", "Commit the new layout", tasmota.step_commit),
    ("F2", "Canonical boot and official app", tasmota.step_canonical),
    ("F4", "Wi-Fi, SaveData and the device's template", tasmota.step_configure),
    ("G1", "Restore official Safeboot", tasmota.step_restore),
    ("H1", "Verify the four regions", tasmota.step_verify),
]
# ESPHome product: stay on stock dual-OTA table; flash ESPHome from interim Tasmota.
ESPHOME_FINAL_STEPS: list[tuple[str, str, Callable[[Context], dict[str, Any]]]] = [
    ("I1", "ESPHome into the other slot, every block checked", mover.step_esphome_install),
    ("I2", "ESPHome started and on the Wi-Fi", mover.step_esphome_check),
]
SONOFF_FINAL_STEPS: list[tuple[str, str, Callable[[Context], dict[str, Any]]]] = [
    ("R1", "The device's own Sonoff firmware and settings, from its backup", sonoff.step_sonoff_restore),
    ("R2", "Sonoff's firmware started and on the Wi-Fi", sonoff.step_sonoff_check),
]
# From a converted plug: Tasmota leaves its layout for the mover; ESPHome, or
# the mover, takes the startup firmware, which takes the Berry Tasmota.
LEAVE_TASMOTA_STEP = ("L1", "From Tasmota's layout to the mover on Sonoff's", mover.step_leave_tasmota)
TO_HUB_STEPS: list[tuple[str, str, Callable[[Context], dict[str, Any]]]] = [
    ("X1", "Startup firmware, sent as an ESPHome update", mover.step_startup_from_esphome),
    ("X2", "Berry Tasmota, uploaded to the startup firmware", mover.step_berry_from_startup),
]
# What every run of this family begins with, after A0: the checks on this computer.
START_STEPS = [step for step in COMMON_STEPS if step[0] == "A5"]
# "midway": a plug a run left part way, found by a new one: on Sonoff's layout,
# running the mover, the startup firmware or the interim Tasmota.
# "committed": one a run left past the commit (E3), on Tasmota's layout with the
# startup firmware or an official Tasmota not yet set up - which only this
# computer's record of that run can tell from the ESPHome route's startup
# firmware, on Sonoff's layout.
# "installer": one a run left on this tool's installer (the bridge), part way
# from Sonoff's firmware: the backup and D1 need only the installer, not eWeLink.
# "safeboot": a converted plug on Tasmota's own safeboot, its Tasmota missing -
# an upload through Tasmota's page that failed part way (another firmware's
# whole-flash image, say): official Tasmota goes back on, then on from there.
# "untemplated": official Tasmota without the plug's template, on a plug this
# computer knows, not left past a commit by its runs: a converted plug that
# lost it, which switches nothing.  It leaves Tasmota as any converted plug;
# staying on Tasmota, it is given the template.
SOURCES = ("sonoff", "tasmota", "esphome", "midway", "committed", "installer", "safeboot",
           "untemplated")
TASMOTA_BACK_STEP = ("T1", "Official Tasmota back, from Tasmota's safeboot", tasmota.step_tasmota_back)


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
                              ("M1", "ESPHome, sent to the mover as an update", mover.step_esphome_from_mover),
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
    if source == "safeboot":
        # Tasmota back first, as it was; then on from Tasmota, as any converted plug.
        return START_STEPS + [TASMOTA_BACK_STEP] + steps_for(firmware, "tasmota")[len(START_STEPS):]
    if source == "untemplated":
        if firmware == "tasmota":
            return START_STEPS + [step for step in TASMOTA_FINAL_STEPS if step[0] == "F4"]
        return steps_for(firmware, "tasmota")
    leave = [LEAVE_TASMOTA_STEP] if source == "tasmota" else []
    return START_STEPS + leave + TO_HUB_STEPS + HUB_STEPS + final  # esphome and midway alike


# Steps run again by themselves when they fail for a reason that may pass -
# the network, mostly.  Each is safe to repeat: it only reads, or writes only
# what it checks first (D1 the bridge's peer slot, which the bridge selects
# itself away from before writing, D4 the slot it is in, E2 the spare slot, G1
# only over the recovery Safeboot), or sets what it sets again.  Not the steps that ask
# for anything, nor C2 or E3, which must happen once.
# I1 too: until the plug restarts it runs Tasmota from ota_1, and writes the
# same checked image again; once it runs ESPHome, I1 finds it and stops.  I2
# only waits for ESPHome and gives it the calibration again; T1 finds Tasmota
# back already, or uploads to Tasmota's safeboot only.
RETRY_STEPS = {"A2", "D1", "D4", "E1", "E2", "F4", "G1", "H1", "I1", "I2",
               "L1", "M1", "X1", "X2", "D5", "R1", "T1"}
# The steps run on a Tasmota given the Wi-Fi: one that has lost its settings
# instead raises its own network (F4 looks for that itself, F2 starts on the
# startup firmware).
ERASED_CHECK_STEPS = {"D4", "D5", "E1", "E2", "E3", "I1", "G1", "H1"}


# Each step's name on its stage line, in words that need no knowledge of the
# internals.
STAGE_NAMES = {
    "A5": "Validating local files",
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
    "T1": "Putting Tasmota back",
}
# Where a shared step means something else on the way to another firmware: D4
# frees the half of the memory it goes into, and the layout is not changed.
TARGET_STAGE_NAMES = {
    "esphome": {"D4": "Making room for ESPHome"},
    "sonoff": {"D4": "Making room for Sonoff firmware"},
}


# What each step does, in words that assume no knowledge of the internals.
PLAIN_DESCRIPTION = {
    "A5": "checks the files on this computer before anything is touched",
    "A2": "asks the device what firmware it is running",
    "C2": "replaces the device's Sonoff firmware with our small installer firmware",
    "C3": "keeps a backup of the device's Sonoff firmware on this computer",
    "D1": "puts a full Tasmota onto the device through the installer's own Wi-Fi",
    "D4": "moves Tasmota into the other half of the device's memory, out of the way",
    "E1": "reads the device's memory layout and checks it, changing nothing",
    "E2": "copies the new startup firmware into the unused half of the memory",
    "E3": "rewrites the device's memory map - the one step that cannot be undone",
    "F2": "installs the full Tasmota now that the new layout is in place",
    "F4": "sets up Wi-Fi, saves settings and applies the device's template",
    "G1": "replaces the temporary startup firmware with the official one",
    "H1": "checks the finished device against the official files",
    "I1": "writes ESPHome into the device, checks every block, and switches to it",
    "I2": "checks the device has started ESPHome and joined your Wi-Fi",
    "L1": "moves the device off Tasmota's memory layout, onto a small firmware that takes the next",
    "M1": "sends ESPHome to the device, which checks it and switches to it",
    "X1": "sends the startup firmware to the device as an ESPHome update",
    "X2": "puts a full Tasmota onto the device through the startup firmware",
    "D5": "updates the device's bootloader, the program that starts its firmware",
    "R1": "writes the device's own Sonoff firmware and settings back, from its backup",
    "R2": "checks the device has started its Sonoff firmware and joined your Wi-Fi",
    "T1": "puts official Tasmota back from its safeboot, its settings kept as they are",
}
