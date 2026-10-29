#!/usr/bin/env python3
"""Capture Tasmota's default settings from an already converted plug, as a template.

With the template, bridge v5 can give a freshly installed Tasmota the home
Wi-Fi, so the conversion needs no visit to Tasmota's setup network. The
template must come from the same Tasmota release, and converted plugs run it.

What happens to the plug, in about three minutes:

1. Its configuration is backed up, into memory only.
2. `Reset 4`: Tasmota's defaults, keeping its Wi-Fi. The defaults are read
   back and the Wi-Fi taken out: that is the template.
3. A record built from the template and the plug's own Wi-Fi is restored onto
   it. Tasmota checks it, restarts and must rejoin the Wi-Fi: this proves the
   record format end to end.
4. The original backup is restored and checked.

While its settings are reset, the plug forgets its own template, so whatever is
plugged into it may switch off until step 4.

    python tools/espressif/capture_tasmota_template.py --host 192.168.0.53

With --verify, the saved template itself is tested instead (steps 1, 3 and 4,
no reset): exactly the file D1 will send, filled with the plug's own Wi-Fi.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.autoflash import tasmota_settings as ts  # noqa: E402
from tools.autoflash.device import Device, DeviceError  # noqa: E402
from tools.autoflash.prompts import QuitRun, ask_yes_no, section  # noqa: E402
from tools.espressif.safeboot.safeboot_migration import INTERIM_TASMOTA_COMMIT, PINNED_ARTIFACTS  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def say(message: str = "") -> None:
    print(message, flush=True)


def restart_and_wait(device: Device, action, what: str) -> None:
    marker = device.started_at()
    action()
    say(f"    {what}; waiting for the plug to restart and rejoin the Wi-Fi...")
    device.wait_for_reboot(timeout=180, started_at=marker)


def read_settings(device: Device) -> bytes:
    record = ts.decode_backup(device.download_settings())
    found = ts.problems(record)
    if found:
        raise ts.SettingsError("the plug's settings did not read back cleanly: " + "; ".join(found))
    return record


def capture(device: Device, saved: bytes | None = None) -> bytes:
    status = device.command("Status 2")
    firmware = status.get("StatusFWR", {}).get("Version", "") if isinstance(status, dict) else ""
    wanted = ".".join(ts.version_text(ts.TEMPLATE_VERSION).split(".")[:3])
    if not firmware.startswith(wanted + "("):
        raise ts.SettingsError(f"the plug runs Tasmota {firmware or 'unknown'}, not {wanted}")
    if INTERIM_TASMOTA_COMMIT != ts.TEMPLATE_SOURCE_COMMIT:
        raise ts.SettingsError("the tool no longer pins the interim Tasmota this supports")

    say("1. Backing up the plug's configuration (kept in memory only)...")
    original_backup = device.download_settings()
    original = ts.decode_backup(original_backup)
    if ts.problems(original):
        raise ts.SettingsError("the plug's backup is not in the expected format: "
                               + "; ".join(ts.problems(original)))
    ssid, password = ts.text(original, ts.SET_STASSID1), ts.text(original, ts.SET_STAPWD1)
    if not ssid:
        raise ts.SettingsError("the plug has no Wi-Fi in its first slot to test with")

    changed = False
    try:
        if saved is not None:
            say("2. Using the saved template (no reset).")
            template = saved
        else:
            say("2. Resetting to Tasmota's defaults, keeping the Wi-Fi (Reset 4)...")
            changed = True
            restart_and_wait(device, lambda: device.command("Reset 4"), "Reset sent")
            defaults = read_settings(device)
            if ts.version(defaults) != ts.TEMPLATE_VERSION:
                raise ts.SettingsError(f"the defaults are from {ts.version_text(ts.version(defaults))}")
            template = ts.make_template(defaults)
        for secret in (ssid, password):
            if len(secret) >= 4 and secret.encode() in template:
                raise ts.SettingsError("the template still contains the Wi-Fi details")
        if saved is None:
            say("    Defaults captured; Wi-Fi details removed from them.")

        say("3. Restoring a record built from the template and this plug's Wi-Fi...")
        test = ts.fill(template, ssid, password)
        changed = True
        restart_and_wait(device, lambda: device.restore_settings(ts.encode_backup(test)),
                         "Record restored")
        rejoined = read_settings(device)
        if ts.text(rejoined, ts.SET_STASSID1) != ssid:
            raise ts.SettingsError("Tasmota did not keep the Wi-Fi from the built record")
        say("    Tasmota accepted the built record and rejoined the Wi-Fi with it.")
        return template
    finally:
        if changed:
            say("4. Restoring the plug's own configuration...")
            try:
                restart_and_wait(device, lambda: device.restore_settings(original_backup),
                                 "Backup restored")
                back = read_settings(device)
                same = back[ts.TEXT_POOL : ts.TEXT_POOL + ts.TEXT_POOL_SIZE] == \
                    original[ts.TEXT_POOL : ts.TEXT_POOL + ts.TEXT_POOL_SIZE]
                say("    The plug has its own configuration back." if same else
                    "    Restored, but its text settings differ from before - check the plug.")
            except Exception as exc:  # noqa: BLE001 - the operator must get their settings back
                say(f"    Restoring did not complete: {exc}")
                offer_backup_file(original_backup)


def offer_backup_file(backup: bytes) -> None:
    """Last resort: the backup holds the Wi-Fi password, so only on request."""
    section("The plug's own configuration was not restored", [
        "It is still running Tasmota's defaults with your Wi-Fi. Its backup is in",
        "memory and contains your Wi-Fi password. It can be saved so you can",
        "restore it yourself (Configuration > Restore Configuration).",
    ])
    if ask_yes_no("Save the backup to a file", default=True):
        path = Path.cwd() / "plug-config-backup.dmp"
        path.write_bytes(backup)
        say(f"    Saved {path}. Delete it once restored: it contains your Wi-Fi password.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", required=True, help="a converted plug's address")
    parser.add_argument("--web-user")
    parser.add_argument("--web-password")
    parser.add_argument("--verify", action="store_true",
                        help="test the saved template on the plug instead of capturing one")
    args = parser.parse_args(argv)
    saved = None
    if args.verify:
        saved, reason = ts.load_template(ROOT, PINNED_ARTIFACTS["bluetooth"]["sha256"])
        if saved is None:
            say(f"STOPPED: {reason}")
            return 1

    device = Device(args.host, user=args.web_user, password=args.web_password)
    section("Test the saved Tasmota settings template" if args.verify else
            "Capture a Tasmota settings template", [
        f"The plug at {args.host} has its configuration backed up, "
        + ("" if args.verify else "reset to Tasmota's defaults, "),
        "tested, and restored - a few minutes. Meanwhile it forgets its own",
        "template, so what is plugged in may switch off.",
    ])
    try:
        if not ask_yes_no("Go ahead", default=False):
            return 0
        template = capture(device, saved)
    except QuitRun:
        return 130
    except (ts.SettingsError, DeviceError, OSError) as exc:
        say(f"STOPPED: {exc}")
        return 1
    if args.verify:
        say("\nThe saved template works: Tasmota took it and joined the Wi-Fi with it.")
        return 0
    path = ts.template_path(ROOT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(template)
    say(f"\nTemplate saved: {path.relative_to(ROOT)} (Tasmota "
        f"{ts.version_text(ts.version(template))}, no Wi-Fi details in it).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
