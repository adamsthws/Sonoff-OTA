"""The Beken routes (the Sonoff S61): every step of every direction, and what
the runner needs to know of each.

Every direction is one move: an rt_ota image put in the plug's download slot
by whatever update the firmware running takes - the stock firmware's through
eWeLink (B2), OpenBeken's by URL (O1, O2), ESPHome's as a UF2 through its OTA
(E1, E2) - which the stock bootloader applies on the restart that follows.
The plug's own store, with its Wi-Fi and calibration, is never written, so
each firmware reads it on first boot and nothing is typed or copied across.
The backup (B4) comes after the first conversion, through OpenBeken's flash
reads: the stock firmware has no way to read the store out, and ESPHome no
reader of its own to keep for good. So a run to ESPHome from Sonoff's
firmware installs OpenBeken first, as the interim firmware (J1), backs the
plug up through it, and has it fetch ESPHome (J2). B2 sends it with a marker
after the image (tools/beken/interim_marker.py), by which it knows itself the
interim firmware: named so from its first start, saying so when asked, and
leaving nothing of itself once it has fetched the next firmware. So a run that
lost its record still finds a plug left on it part way ("interim"), and goes
on: to ESPHome as the run to ESPHome would have, to Sonoff's firmware as from
OpenBeken (J4), and to OpenBeken by keeping it, as the plug's own (J3).
"""

from __future__ import annotations

from typing import Any, Callable, TYPE_CHECKING

from tools.autoflash.routes.beken import arrived, images, onward, stock

if TYPE_CHECKING:
    from tools.autoflash.context import Context

Steps = list[tuple[str, str, Callable[["Context"], dict[str, Any]]]]

# What a plug of this family may run: the firmware a run installs, by its key.
FIRMWARES = {"openbeken": "OpenBeken", "esphome": "ESPHome", "sonoff": "Sonoff"}
# What a plug of this family may be running when a run starts.
SOURCES = ("sonoff", "openbeken", "esphome", "interim")
# After this step the plug no longer runs its Sonoff firmware.
POINT_OF_NO_RETURN = "B2"
RESUME_CHECK = None

STOCK_STEPS: Steps = [
    ("B1", "What the device reports, through eWeLink", stock.step_identify),
    ("B2", "The new firmware, sent through eWeLink", stock.step_push),
]
OPENBEKEN_FINAL_STEPS: Steps = [
    ("K1", "OpenBeken started and on the Wi-Fi", arrived.step_openbeken_check),
    ("B4", "Back up the device's own data, through its new firmware", arrived.step_backup),
]
INTERIM_STEPS: Steps = [
    ("J1", "The interim firmware started and on the Wi-Fi", arrived.step_interim_check),
    ("B4", "Back up the device's own data, through the interim firmware", arrived.step_backup),
    ("J2", "ESPHome, fetched by the interim firmware", onward.step_interim_to_esphome),
]
ESPHOME_FINAL_STEPS: Steps = [("K2", "ESPHome started and on the Wi-Fi", arrived.step_esphome_check)]
SONOFF_FINAL_STEPS: Steps = [("K3", "Sonoff's firmware started and back in eWeLink", arrived.step_sonoff_check)]
FROM_OPENBEKEN: dict[str, Steps] = {
    "esphome": [("O1", "ESPHome, fetched by OpenBeken", onward.step_openbeken_to_esphome)],
    "sonoff": [("O2", "Sonoff's firmware, fetched by OpenBeken", onward.step_openbeken_to_sonoff)],
}
# From a plug left on the interim firmware, found by a run with no record of it.
FROM_INTERIM: dict[str, Steps] = {
    "esphome": INTERIM_STEPS[1:],
    "openbeken": [("J3", "OpenBeken, in place of the interim firmware", onward.step_interim_to_openbeken)],
    "sonoff": INTERIM_STEPS[1:2] + [
        ("J4", "Sonoff's firmware, fetched by the interim firmware", onward.step_interim_to_sonoff)],
}
FROM_ESPHOME: dict[str, Steps] = {
    "openbeken": [("E1", "OpenBeken, sent to ESPHome as an update", onward.step_esphome_to_openbeken)],
    "sonoff": [("E2", "Sonoff's firmware, sent to ESPHome as an update", onward.step_esphome_to_sonoff)],
}


def steps_for(firmware: str, source: str = "sonoff") -> Steps:
    """The steps from `source` firmware to `firmware` (none, if it is the same),
    after the start every route shares (routes.steps_for)."""
    if firmware == source:
        return []
    final = {"openbeken": OPENBEKEN_FINAL_STEPS, "esphome": ESPHOME_FINAL_STEPS, "sonoff": SONOFF_FINAL_STEPS}[firmware]
    if source == "sonoff":
        return STOCK_STEPS + (INTERIM_STEPS if firmware in images.INTERIM else []) + final
    if source == "openbeken":
        # The backup first, where the plug has none yet: only OpenBeken reads it out.
        return OPENBEKEN_FINAL_STEPS[1:] + FROM_OPENBEKEN[firmware] + final
    if source == "esphome":
        return FROM_ESPHOME[firmware] + final
    if source == "interim":
        return FROM_INTERIM[firmware] + final
    raise ValueError(f"no route from {source}")


# The steps that write to the plug, which happen once; and those tried again by
# themselves when they fail for a reason that may pass: each only reads, or
# waits - or, J3, sets the name it set again and restarts the plug.
DESTRUCTIVE_STEPS = {"B2", "J2", "J4", "O1", "O2", "E1", "E2"}
RETRY_STEPS = {"B1", "J1", "J3", "K1", "K2", "K3", "B4"}
# No Tasmota here to lose its settings and raise a network of its own.
ERASED_CHECK_STEPS: set[str] = set()
STAGE_NAMES = {
    "B1": "Querying Sonoff firmware",
    "B2": "Sending the new firmware",
    "B4": "Backing up the device's data",
    "J1": "Checking interim firmware",
    "J2": "Sending ESPHome",
    "J3": "Setting up OpenBeken",
    "J4": "Sending Sonoff firmware",
    "K1": "Checking OpenBeken",
    "K2": "Checking ESPHome",
    "K3": "Checking Sonoff firmware",
    "O1": "Sending ESPHome",
    "O2": "Sending Sonoff firmware",
    "E1": "Sending OpenBeken",
    "E2": "Sending Sonoff firmware",
}
# On a run to ESPHome what B2 sends, and B4 backs up through, is the interim firmware.
TARGET_STAGE_NAMES: dict[str, dict[str, str]] = {
    "esphome": {"B2": "Sending interim firmware", "B4": "Backing up the device"},
}
PLAIN_DESCRIPTION = {
    "B1": "asks eWeLink what model and firmware the device has",
    "B2": "sends the device a new firmware, which it installs as it restarts",
    "B4": "keeps a backup of the device's own data on this computer",
    "J1": "checks the device has started the interim firmware, there only to back it up, and joined your Wi-Fi",
    "J2": "has the interim firmware fetch ESPHome, which the device installs as it restarts",
    "J3": "sets the device up on OpenBeken in place of the interim firmware, and restarts it so your network "
          "knows it by its new name",
    "J4": "has the interim firmware fetch Sonoff's firmware, which the device installs as it restarts",
    "K1": "checks the device has started OpenBeken and joined your Wi-Fi",
    "K2": "checks the device has started ESPHome and joined your Wi-Fi",
    "K3": "checks the device has started its Sonoff firmware and is back in eWeLink",
    "O1": "has OpenBeken fetch ESPHome, which the device installs as it restarts",
    "O2": "has OpenBeken fetch Sonoff's firmware, which the device installs as it restarts",
    "E1": "sends ESPHome an update carrying OpenBeken, which the device installs as it restarts",
    "E2": "sends ESPHome an update carrying Sonoff's firmware, which the device installs as it restarts",
}
# What a step cut off part way may have left: the plug says, by what it answers as.
ATTEMPTED_EVIDENCE: dict[str, Callable[["Context"], tuple[bool | None, str]]] = {
    "B2": stock.evidence_pushed,
    "J2": onward.evidence_installed,
    "J4": onward.evidence_installed,
    "O1": onward.evidence_installed,
    "O2": onward.evidence_installed,
    "E1": onward.evidence_installed,
    "E2": onward.evidence_installed,
}
# A step from OpenBeken or ESPHome recorded done is doubted where the plug
# still answers as the firmware it was leaving: the image was never applied (a
# power cut while it was written, say), and sending it again is what is wanted.
# The check steps ask the plug afresh anyway.
STEP_VERIFIERS: dict[str, Callable[["Context"], tuple[bool, str]]] = {
    step: onward.still_installed for step in ("J2", "J4", "O1", "O2", "E1", "E2")}
# Sonoff's firmware goes back on from the public dump (images.stock_rbl), not
# the plug's backup - which B4 takes first, from OpenBeken, where there is none.
SONOFF_FROM_BACKUP = False
