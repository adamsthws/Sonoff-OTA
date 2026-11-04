"""What a step that was cut off left on the plug: each destructive step's
evidence that it finished, judged from what the plug answers as.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from tools.autoflash import detect, esphome, names, prompts, unlock
from tools.autoflash.routes import update
from tools.autoflash.routes.espressif import installer

if TYPE_CHECKING:
    from tools.autoflash.context import Context


def evidence_esphome_installed(ctx: Context) -> tuple[bool | None, str]:
    host = ctx.state.input("plug_ip")
    if host and esphome.is_esphome(host):
        return True, "The device is running ESPHome, so the install did finish."
    if ctx.device.current_ota() == 1:
        return False, (
            "The plug is still running the temporary Tasmota, so it did not switch;\n"
            "  it is safe to do this step again."
        )
    return None, "The device neither runs ESPHome nor answers as the temporary Tasmota."


DESTRUCTIVE_STEPS = {"C2", "E2", "E3", "F2", "G1", "I1", "L1", "M1", "X1", "X2", "D5", "R1", "U1", "U5"}


def evidence_official_app_installed(ctx: "Context") -> tuple[bool | None, str]:
    """Safeboot still running means the official firmware never landed."""
    if not ctx.finder.poll():
        # Not answering may simply mean it is waiting to be set up: the migration
        # clears its Wi-Fi details, so the new firmware comes up asking for them.
        setup = prompts.find_ssid(names.setup_networks(ctx))
        if setup:
            return True, (
                f"The device is not on your network, but it is broadcasting '{setup}'.\n"
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
            f"The device is running {version}, not Safeboot, so the install did finish."
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
        return False, "The device reported that this step failed."
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
    return None, "The device sent no report of this step, and does not show how far it got."


# Consulted when a step was interrupted, to answer "did it finish?" from the
# device rather than from the operator's memory.
def evidence_by_what_answers(finished: set[str], not_yet: set[str]):
    """A check for a step that ends with the plug running something else:
    finished once it runs one of `finished`, not yet while one of `not_yet`."""
    def evidence(ctx: "Context") -> tuple[bool | None, str]:
        host = ctx.state.input("plug_ip") or ""
        running = unlock.identify(ctx, host) if host else None
        if running is None:
            moved = ctx.finder.poll()
            running = unlock.identify(ctx, moved) if moved else None
        name = {"mover": "the mover", "startup": "the startup firmware",
                "hub": "the interim Tasmota"}.get(
            running, detect.NAMES.get(running, "nothing this tool knows"))
        if running in finished:
            return True, f"The device is running {name}, so this step did finish."
        if running in not_yet:
            return False, f"The device is still running {name}, so this step has to be done again."
        return None, "The device is not answering, so it cannot show whether this step finished."
    return evidence


def evidence_bootloader(ctx: "Context") -> tuple[bool | None, str]:
    image = esphome.published_bootloader()
    actual = ctx.device.sha_region(0, len(image))
    if actual == esphome.BOOTLOADER_SHA256:
        return True, "ESPHome's bootloader is in place, so this step did finish."
    return False, "The device still has its old bootloader, so this step has to be done again."


ATTEMPTED_EVIDENCE: dict[str, Callable[["Context"], tuple[bool | None, str]]] = {
    "C2": installer.evidence_bridge_installed,
    "E3": evidence_commit_finished,
    "F2": evidence_official_app_installed,
    "I1": evidence_esphome_installed,
    "L1": evidence_by_what_answers({"mover", "startup", "hub"}, {"tasmota"}),
    "M1": evidence_by_what_answers({"esphome"}, {"mover"}),
    "X1": evidence_by_what_answers({"startup", "hub"}, {"esphome", "mover"}),
    "X2": evidence_by_what_answers({"hub"}, {"startup"}),
    "D5": evidence_bootloader,
    "R1": evidence_by_what_answers({"sonoff"}, {"hub"}),
    "T1": evidence_by_what_answers({"tasmota"}, {"startup"}),
    "U1": update.evidence_updated,
    "U5": update.evidence_updated,
}
