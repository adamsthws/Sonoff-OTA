"""The firmware bundle: the pinned official files and the startup firmware
built from them, checked before a run starts and built again when this
version cannot use the one there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

from tools.autoflash import hostsys, output, prompts, safety_locks
from tools.autoflash.errors import Abort
from tools.autoflash.runs import ROOT
from tools.espressif.safeboot.prepare_imprinted_bundle import BundleError, build_bundle, validate_bundle
from tools.espressif.safeboot.safeboot_migration import earlier_release, load_manifest

if TYPE_CHECKING:
    from tools.autoflash.context import Context


DEFAULT_MANIFEST = Path("captures/safeboot-recovery-migration/manifest.json")


def step_check(ctx: Context) -> dict[str, Any]:
    """A5 - offline preconditions, building the bundle if there is none.  Touches no plug."""
    try:
        manifest = validate_bundle(ctx.manifest_path, current_only=True)
    except ValueError as exc:
        if ctx.manifest_path != (ROOT / DEFAULT_MANIFEST).resolve():
            raise Abort(f"the bundle at {ctx.manifest_path} cannot be used: {exc}") from exc
        manifest = make_bundle(ctx, problem=None if not ctx.manifest_path.exists() else str(exc))
    else:
        # Nothing is asked about Wi-Fi here: the details are read off the plug
        # at E1, and the bundle is rebuilt then if they are for another network.
        output.say("bundle and pinned artifacts: OK")

    problems = hostsys.check_sha256sums(ROOT / "artifacts" / "SHA256SUMS")
    if problems:
        raise Abort("artifact checksums failed: " + "; ".join(problems))
    output.say("published artifact checksums: OK")

    safety_locks.restore_left_locks(ROOT)
    for lock in safety_locks.NAMES:
        if not safety_locks.in_place(ROOT, lock):
            raise Abort(f"{lock} is not in place; refusing to start from an unsafe state")
    output.say("both safety locks in place")

    for phase in ("preflight", "stage", "commit", "restore"):
        report = ctx.evidence / f"{phase}-report.json"
        if report.exists():
            output.say(f"note: existing {phase} evidence in {ctx.evidence}")
    return {"mode": manifest.get("migration_mode", "official")}


def make_bundle(ctx: Context, problem: str | None) -> dict[str, Any]:
    """Build the bundle in place.  Asks nothing: the Wi-Fi is added at E1."""
    prompts.section("Prepare the firmware bundle", quiet=True, body=[
        *(["The existing bundle cannot be used:", f"  {problem}", ""] if problem else []),
        "This downloads the official Tasmota files and checks each one against",
        "its pinned fingerprint. It is done once; later plugs reuse it.",
        "",
        "Your Wi-Fi details are not needed for this. Later in the run they are",
        "read off the plug and written into an in-memory copy of the small",
        "temporary firmware, which has to rejoin your Wi-Fi by itself.",
    ])
    try:
        build_bundle(output=ctx.manifest_path.parent, release=release_on_the_plug(ctx))
    except BundleError as exc:
        raise Abort(f"the bundle could not be prepared: {exc}") from exc
    output.say("Bundle ready.")
    return validate_bundle(ctx.manifest_path)


def release_on_the_plug(ctx: Context) -> dict[str, Any] | None:
    """The official release pinned before whose app a run past F2 put on the
    plug, as the bundle that installed it holds it; None for the one pinned
    now, or before F2. A bundle built again must be of that release: its
    Safeboot is G1's, its files are H1's check. One 1.2.0 made, from another
    startup firmware, is built again (the fault suite's legacy-v1.2.0 at H1)."""
    if not ctx.state.is_done("F2"):
        return None
    try:
        return earlier_release(load_manifest(ctx.manifest_path))
    except (ValueError, OSError):
        return None


def bundle_of_this_version(ctx: Context) -> None:
    """A run carried on past A5 has its bundle checked, as A5 checks it, and
    built again if this version cannot use it: one an earlier version made
    before the firmware the tool installs changed, which every phase server
    refuses (docs/updating-firmware.md). Until F2, nothing on the plug depends
    on it - the startup firmware it pins is the published one with the same
    Wi-Fi, written in again when next needed (adopt_wifi), and a step whose
    checks were made with the old one does them again (of_this_bundle). From
    F2 the plug runs the official app the bundle holds, and the Safeboot G1
    writes, and H1's check, must be that release's: a bundle of a release
    pinned before is kept, or built again of it (release_on_the_plug), and
    the plug ends on it."""
    if not ctx.state.is_done("A5") or ctx.manifest_path != (ROOT / DEFAULT_MANIFEST).resolve():
        return
    try:
        validate_bundle(ctx.manifest_path, current_only=not ctx.state.is_done("F2"))
    except (ValueError, OSError) as exc:
        make_bundle(ctx, problem=str(exc))
