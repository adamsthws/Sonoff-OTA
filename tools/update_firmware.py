#!/usr/bin/env python3
"""Move the firmware the tool installs to a new Tasmota or ESPHome release.

    python3 tools/update_firmware.py check
        what firmware.json pins, against each project's latest stable release;
        exits 1 when either is behind

    python3 tools/update_firmware.py tasmota [VERSION]
        pin that official Tasmota release (default: the latest) - its ESP32-C3
        app, Safeboot and factory image, from the release's page on GitHub -
        once each is shown to fit the layout the conversion builds

    python3 tools/update_firmware.py esphome [VERSION] [--image-version X.Y.Z]
                                     [--mover] [--new-bootloader]
        build ESPHome/sonoff_s60.yaml with that ESPHome release (default: the
        latest) in ESPHome's own Docker image, check it, and publish it in
        artifacts/; --mover builds the mover too

Each writes firmware.json, the published files and SHA256SUMS, and lists what
is left to do by hand. Nothing is pushed or committed. docs/updating-firmware.md
is the whole procedure, and what each check is for.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.autoflash import esphome, imprint, tasmota_settings  # noqa: E402
from tools.safeboot_migration import (  # noqa: E402
    APP0_OFFSET,
    APP0_SIZE,
    SAFEBOOT_OFFSET,
    SAFEBOOT_SIZE,
    TABLE_OFFSET,
    TABLE_SIZE,
    TARGET_PARTITIONS,
    TARGET_TABLE_SHA256,
    parse_partition_sector,
    partition_tuple,
    sha256_bytes,
    validate_native_image,
)

FIRMWARE_JSON = ROOT / "firmware.json"
SHA256SUMS = ROOT / "SHA256SUMS"
GITIGNORE = ROOT / ".gitignore"
TASMOTA_REPO = "arendst/Tasmota"
ESPHOME_REPO = "esphome/esphome"
ESPHOME_DOCKER = "ghcr.io/esphome/esphome"
# The ESP32-C3 files of a Tasmota release, by the name the bundle knows each by.
TASMOTA_FILES = {
    "factory": "tasmota32c3.factory.bin",
    "safeboot": "tasmota32c3-safeboot.bin",
    "app": "tasmota32c3.bin",
}
# Each ESPHome build: its config, and the file it is published as.
ESPHOME_BUILDS = {
    "image": ("ESPHome/sonoff_s60.yaml", "artifacts/esphome-sonoff-s60-imprintable-{version}.bin"),
    "mover": ("ESPHome/autoflash_mover.yaml", "artifacts/autoflash-mover-{version}.bin"),
}
BOOTLOADER_FILE = "artifacts/esphome-sonoff-s60-bootloader-{version}.bin"
# Downloads ESPHome's builds need (ESP-IDF and its tools, a gigabyte or so),
# kept between builds. captures/ is never committed.
BUILD_CACHE = ROOT / "captures" / "esphome-build"
# What the S60's config reaches into ESPHome for, beyond its documented
# options: its last on_boot lambda saves the Wi-Fi under the one fixed key an
# ESPHome build with no network of its own looks in (ESPHome/README.md says
# why). An ESPHome that changes either is one the lambda must be changed for.
ESPHOME_WIFI_SOURCE = "/esphome/esphome/components/wifi"
ESPHOME_INTERNALS = {
    "wifi_component.cpp": ("88491487UL",),
    "wifi_component.h": ("struct SavedWifiSettings {", "char ssid[33];", "char password[65];"),
}


class UpdateError(RuntimeError):
    pass


# -- what is pinned, and what is out ------------------------------------------

def load_pins() -> dict[str, Any]:
    return json.loads(FIRMWARE_JSON.read_text(encoding="utf-8"))


def save_pins(pins: dict[str, Any]) -> None:
    FIRMWARE_JSON.write_text(json.dumps(pins, indent=2) + "\n", encoding="utf-8")


def github(path: str) -> Any:
    """GitHub's API, read without signing in (or with GITHUB_TOKEN, if set)."""
    request = urllib.request.Request(f"https://api.github.com/{path}",
                                     headers={"Accept": "application/vnd.github+json",
                                              "User-Agent": "Autoflash-firmware-update"})
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Autoflash-firmware-update"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.read()


def stable_release(repo: str, tag: str | None) -> dict[str, Any]:
    """A release that is neither a draft nor a pre-release: that tag's, or the latest."""
    release = github(f"repos/{repo}/releases/" + (f"tags/{tag}" if tag else "latest"))
    if release.get("draft") or release.get("prerelease"):
        raise UpdateError(f"{repo} {release['tag_name']} is not a stable release")
    return release


def tag_commit(repo: str, tag: str) -> str:
    ref = github(f"repos/{repo}/git/ref/tags/{tag}")["object"]
    while ref["type"] == "tag":  # an annotated tag points at its commit through itself
        ref = github(f"repos/{repo}/git/tags/{ref['sha']}")["object"]
    return ref["sha"]


def command_check(_args: argparse.Namespace) -> int:
    pins = load_pins()
    tasmota_latest = stable_release(TASMOTA_REPO, None)["tag_name"].removeprefix("v")
    esphome_latest = stable_release(ESPHOME_REPO, None)["tag_name"]
    behind = False
    print(f"Tasmota: pinned {pins['tasmota']['version']}, latest {tasmota_latest}"
          + ("" if pins["tasmota"]["version"] == tasmota_latest else "  <- update"))
    behind |= pins["tasmota"]["version"] != tasmota_latest
    for name, pin in pins["esphome"].items():
        late = name == "image" and pin["esphome"] != esphome_latest
        behind |= late
        print(f"ESPHome {name}: {Path(pin['file']).name}, built with {pin['esphome']}, "
              f"latest {esphome_latest}" + ("  <- update" if late else ""))
    if behind:
        print("\nSee docs/updating-firmware.md.")
    return 1 if behind else 0


# -- Tasmota -------------------------------------------------------------------

def check_tasmota_files(files: dict[str, bytes]) -> None:
    """Refuse a release whose ESP32-C3 files would not fit the conversion.

    The conversion writes the partition table it was reviewed with (the
    factory image's, byte for byte), the Safeboot into its partition and the
    app into app0, and at the end compares each with these files.
    """
    factory, safeboot, app = files["factory"], files["safeboot"], files["app"]
    table = factory[TABLE_OFFSET:TABLE_OFFSET + TABLE_SIZE]
    try:
        entries = [partition_tuple(entry) for entry in parse_partition_sector(table)["entries"]]
    except ValueError:
        entries = []
    if entries != list(TARGET_PARTITIONS) or sha256_bytes(table) != TARGET_TABLE_SHA256:
        raise UpdateError("the factory image's partition table is not the one the conversion "
                          "writes: the repartition itself would have to be reviewed again")
    if factory[SAFEBOOT_OFFSET:SAFEBOOT_OFFSET + len(safeboot)] != safeboot:
        raise UpdateError("the factory image holds another Safeboot than the release's")
    if factory[APP0_OFFSET:APP0_OFFSET + len(app)] != app:
        raise UpdateError("the factory image holds another app than the release's")
    try:
        validate_native_image(safeboot, SAFEBOOT_SIZE, "the Safeboot")
        validate_native_image(app, APP0_SIZE, "the app")
    except ValueError as exc:
        raise UpdateError(str(exc)) from exc


def cfg_holder(my_user_config: str) -> int:
    match = re.search(r"^#define\s+CFG_HOLDER\s+(\d+)", my_user_config, re.MULTILINE)
    if not match:
        raise UpdateError("my_user_config.h has no CFG_HOLDER")
    return int(match.group(1))


def command_tasmota(args: argparse.Namespace) -> int:
    release = stable_release(TASMOTA_REPO, f"v{args.version}" if args.version else None)
    tag = release["tag_name"]
    version = tag.removeprefix("v")
    commit = tag_commit(TASMOTA_REPO, tag)
    print(f"Tasmota {version}: {release['html_url']} (commit {commit[:10]})")
    assets = {asset["name"]: asset for asset in release["assets"]}
    files: dict[str, bytes] = {}
    for key, name in TASMOTA_FILES.items():
        if name not in assets:
            raise UpdateError(f"the release has no {name}")
        data = download(assets[name]["browser_download_url"])
        if len(data) != assets[name]["size"]:
            raise UpdateError(f"{name} came down as {len(data):,} bytes, not {assets[name]['size']:,}")
        files[key] = data
        print(f"  {name}: {len(data):,} bytes, SHA-256 {sha256_bytes(data)}")
    check_tasmota_files(files)
    print(f"  the partition table is the conversion's; the Safeboot fits with "
          f"{SAFEBOOT_SIZE - len(files['safeboot']):,} bytes to spare, the app with "
          f"{APP0_SIZE - len(files['app']):,}")
    # The recovery Safeboot saves the Wi-Fi in its own release's settings
    # record; the official release takes that record, and moves it on to its
    # own layout, only if CFG_HOLDER is the same. Otherwise it starts from its
    # defaults, with no Wi-Fi, and raises its setup network.
    holder = cfg_holder(download(f"https://raw.githubusercontent.com/{TASMOTA_REPO}/{commit}/"
                                 "tasmota/my_user_config.h").decode("utf-8"))
    if holder != tasmota_settings.CFG_HOLDER:
        raise UpdateError(f"CFG_HOLDER is {holder}, not {tasmota_settings.CFG_HOLDER}: the "
                          "release would not take the Wi-Fi the recovery Safeboot saves")
    print(f"  CFG_HOLDER {holder}, as the recovery Safeboot's record has it")

    pins = load_pins()
    old = pins["tasmota"]
    previous = old["version"]
    # The release before is kept, by its files: a run that installed it, left
    # part way, finishes on it (safeboot_migration.earlier_release).
    earlier = [entry for entry in old.get("earlier", []) if entry["version"] != version]
    if previous != version:
        earlier.insert(0, {key: old[key] for key in ("version", "tag", "commit", "files")})
    pins["tasmota"] = {
        "version": version,
        "tag": tag,
        "commit": commit,
        "cfg_holder": holder,
        "files": {key: {"name": TASMOTA_FILES[key], "size": len(data), "sha256": sha256_bytes(data)}
                  for key, data in files.items()},
        "earlier": earlier,
    }
    if args.dry_run:
        print("\nChecked; firmware.json left as it was (--dry-run).")
        return 0
    save_pins(pins)
    print(f"\nfirmware.json now pins Tasmota {version} (was {previous}).")
    left_to_do([
        f"CHANGELOG.md: the official Tasmota installed is {version}, with a link to its release notes",
        "README.md and docs/: any line naming the Tasmota release installed",
        "python3 -m unittest discover -s tests",
        "the emulator runs in docs/updating-firmware.md, then a real plug",
    ])
    return 0


# -- ESPHome -------------------------------------------------------------------

def docker(*args: str, capture: bool = True) -> str:
    result = subprocess.run(["docker", *args], check=False, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.STDOUT if capture else None)
    if result.returncode:
        raise UpdateError(f"docker {' '.join(args[:2])} failed" + (f":\n{result.stdout}" if capture else ""))
    return result.stdout or ""


def esphome_builder(version: str) -> str:
    """ESPHome's own image for that release, pulled, by its digest."""
    image = f"{ESPHOME_DOCKER}:{version}"
    docker("pull", "--quiet", image)
    digests = json.loads(docker("image", "inspect", "--format", "{{json .RepoDigests}}", image))
    digest = next((entry.split("@", 1)[1] for entry in digests if entry.startswith(ESPHOME_DOCKER + "@")), None)
    if not digest:
        raise UpdateError(f"{image} has no digest to pin")
    return f"{image}@{digest}"


def check_esphome_internals(builder: str) -> None:
    for name, needles in ESPHOME_INTERNALS.items():
        text = docker("run", "--rm", "--entrypoint", "cat", builder, f"{ESPHOME_WIFI_SOURCE}/{name}")
        for needle in needles:
            if needle not in text:
                raise UpdateError(
                    f"ESPHome's {name} no longer has '{needle}': the last on_boot lambda of "
                    "ESPHome/sonoff_s60.yaml must be checked against it (ESPHome/README.md)")


def next_minor(version: str) -> str:
    major, minor, _patch = (int(part) for part in version.split("."))
    return f"{major}.{minor + 1}.0"


PROJECT_VERSION = re.compile(r'(^  project:\n(?:    .*\n)*?    version: ")([^"]+)(")', re.MULTILINE)


def config_version(config: str) -> str:
    match = PROJECT_VERSION.search(config)
    if not match:
        raise UpdateError("the config has no esphome: project: version:")
    return match.group(2)


def with_config_version(config: str, version: str) -> str:
    config_version(config)
    return PROJECT_VERSION.sub(lambda match: match.group(1) + version + match.group(3), config, count=1)


def app_description(image: bytes) -> dict[str, str]:
    """What ESP-IDF's app description says, at its fixed place in an app."""
    desc = image[32:32 + 256]
    field = lambda start, end: desc[start:end].split(b"\0", 1)[0].decode("ascii", "replace")  # noqa: E731
    return {"version": field(16, 48), "project": field(48, 80), "esp_idf": field(112, 144).removeprefix("v")}


def build(builder: str, config_path: Path, version: str, workdir: Path) -> tuple[bytes, bytes]:
    """Build one config at this version: its app and bootloader."""
    config = with_config_version(config_path.read_text(encoding="utf-8"), version)
    (workdir / config_path.name).write_text(config, encoding="utf-8")
    print(f"  building {config_path.name} {version} (minutes; a first build downloads ESP-IDF)...")
    docker("run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/config",
           "-v", f"{workdir}:/config", builder, "compile", config_path.name)
    name = re.search(r"^  name: (\S+)", config, re.MULTILINE).group(1)
    out = workdir / ".esphome" / "build" / name / "build"
    return (out / "firmware.ota.bin").read_bytes(), (out / "bootloader" / "bootloader.bin").read_bytes()


def check_esphome_app(image: bytes, label: str, release: str) -> dict[str, str]:
    try:
        esphome.check_app(image, label)
        if not imprint.has_placeholders(image):
            raise UpdateError(f"{label} has no untouched Wi-Fi placeholders")
        imprint.imprint(image, "Autoflash-check", "a-password")  # each found once, and writable
    except (esphome.ImageError, imprint.ImprintError) as exc:
        raise UpdateError(str(exc)) from exc
    if imprint.find_build_paths(image):
        raise UpdateError(f"{label} holds paths from the computer that built it")
    described = app_description(image)
    if described["version"] != release:
        raise UpdateError(f"{label} says it is ESPHome {described['version']}, not {release}")
    return described


def replace_sums_line(sums: str, old: str | None, new: str, data: bytes) -> str:
    """SHA256SUMS with the old file's line made the new one's (or the new one added)."""
    line = f"{sha256_bytes(data)}  {new}"
    lines = [entry for entry in sums.splitlines() if not entry.endswith(f"  {new}")]
    if old is not None:
        at = next((index for index, entry in enumerate(lines) if entry.endswith(f"  {old}")), None)
        if at is not None:
            lines[at] = line
            return "\n".join(lines) + "\n"
    return "\n".join([*lines, line]) + "\n"


def with_ignore_exception(gitignore: str, old: str, new: str) -> str:
    """.gitignore letting the new file in where it let the old one: firmware is
    left out (*.bin), but for the files published, each named."""
    lines = gitignore.splitlines()
    if f"!{new}" in lines:
        return gitignore
    if f"!{old}" in lines:
        lines[lines.index(f"!{old}")] = f"!{new}"
    else:
        last = max(index for index, line in enumerate(lines) if line.startswith("!artifacts/"))
        lines.insert(last + 1, f"!{new}")
    return "\n".join(lines) + "\n"


def publish(pins: dict[str, Any], key: str, file: str, data: bytes, record: dict[str, Any]) -> None:
    old = pins["esphome"][key]["file"]
    (ROOT / file).write_bytes(data)
    if old != file and (ROOT / old).exists() and not any(
            other["file"] == old for name, other in pins["esphome"].items() if name != key):
        (ROOT / old).unlink()
    SHA256SUMS.write_text(replace_sums_line(SHA256SUMS.read_text(encoding="utf-8"),
                                            old if old != file else None, file, data), encoding="utf-8")
    GITIGNORE.write_text(with_ignore_exception(GITIGNORE.read_text(encoding="utf-8"), old, file),
                         encoding="utf-8")
    pins["esphome"][key] = {"file": file, **record, "size": len(data), "sha256": sha256_bytes(data)}
    print(f"  published {file}: {len(data):,} bytes, SHA-256 {sha256_bytes(data)}")


def command_esphome(args: argparse.Namespace) -> int:
    release = stable_release(ESPHOME_REPO, args.version)["tag_name"]
    pins = load_pins()
    builder = esphome_builder(release)
    print(f"ESPHome {release}: {builder}")
    check_esphome_internals(builder)
    print("  the Wi-Fi internals the S60's config relies on are as they were")
    BUILD_CACHE.mkdir(parents=True, exist_ok=True)
    built: dict[str, tuple[str, bytes, bytes, dict[str, str]]] = {}
    for key in ["image", *(["mover"] if args.mover else [])]:
        version = (args.image_version if key == "image" else None) or next_minor(pins["esphome"][key]["version"])
        app, bootloader = build(builder, ROOT / ESPHOME_BUILDS[key][0], version, BUILD_CACHE)
        described = check_esphome_app(app, f"the {key} built", release)
        built[key] = (version, app, bootloader, described)
        print(f"  {key} {version}: {len(app):,} bytes, ESP-IDF {described['esp_idf']}, "
              f"{esphome.SLOT_SIZE - len(app):,} bytes to spare in the slot")
    version, app, bootloader, described = built["image"]
    pinned_bootloader = (ROOT / pins["esphome"]["bootloader"]["file"]).read_bytes()
    new_bootloader = bootloader != pinned_bootloader
    if new_bootloader and not args.new_bootloader:
        print(f"  this build's bootloader differs from the published one "
              f"(ESP-IDF {described['esp_idf']}, published: {pins['esphome']['bootloader']['esp_idf']}): "
              "the published one is kept (--new-bootloader replaces it)")
    if args.dry_run:
        print("\nBuilt and checked; nothing published (--dry-run).")
        return 0

    for key, (version, app, _bootloader, described) in built.items():
        config_path = ROOT / ESPHOME_BUILDS[key][0]
        config_path.write_text(with_config_version(config_path.read_text(encoding="utf-8"), version),
                               encoding="utf-8")
        publish(pins, key, ESPHOME_BUILDS[key][1].format(version=version), app,
                {"config": ESPHOME_BUILDS[key][0], "version": version, "esphome": release,
                 "esp_idf": described["esp_idf"], "builder": builder})
    version, _app, bootloader, described = built["image"]
    if args.new_bootloader and new_bootloader:
        publish(pins, "bootloader", BOOTLOADER_FILE.format(version=version), bootloader,
                {"config": ESPHOME_BUILDS["image"][0], "version": version, "esphome": release,
                 "esp_idf": described["esp_idf"], "builder": builder})
    elif not new_bootloader:
        print("  the bootloader is byte for byte the published one: kept as it is")
    save_pins(pins)
    print("\nfirmware.json, SHA256SUMS, .gitignore and artifacts/ updated: git add the new "
          "files, and git rm the old.")
    left_to_do([
        "artifacts/README.md: each changed file's row (name, SHA-256) and a paragraph on what changed",
        "THIRD_PARTY_NOTICES.md: the files' names, the ESPHome release and ESP-IDF version",
        "ESPHome/README.md: the ESPHome release that built the current image",
        f"CHANGELOG.md: ESPHome is built with {release}",
        "python3 -m unittest discover -s tests (tests/test_firmware_pins.py checks the above)",
        "the emulator runs in docs/updating-firmware.md, then a real plug",
    ])
    return 0


def left_to_do(items: list[str]) -> None:
    print("\nLeft to do (docs/updating-firmware.md):")
    for item in items:
        print(f"  - {item}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="pinned against latest").set_defaults(run=command_check)
    tasmota = commands.add_parser("tasmota", help="pin an official Tasmota release")
    tasmota.add_argument("version", nargs="?", help="e.g. 16.0.0 (default: the latest)")
    tasmota.add_argument("--dry-run", action="store_true", help="check it, change nothing")
    tasmota.set_defaults(run=command_tasmota)
    build_parser = commands.add_parser("esphome", help="build and publish ESPHome with a release")
    build_parser.add_argument("version", nargs="?", help="e.g. 2026.10.0 (default: the latest)")
    build_parser.add_argument("--image-version", help="the S60 image's own version (default: next minor)")
    build_parser.add_argument("--mover", action="store_true", help="build and publish the mover too")
    build_parser.add_argument("--new-bootloader", action="store_true",
                              help="publish this build's bootloader where it differs (see the docs first)")
    build_parser.add_argument("--dry-run", action="store_true", help="build and check, publish nothing")
    build_parser.set_defaults(run=command_esphome)
    args = parser.parse_args(argv)
    if shutil.which("docker") is None and args.command == "esphome":
        print("ESPHome is built in its own Docker image: Docker is needed.", file=sys.stderr)
        return 2
    try:
        return args.run(args)
    except UpdateError as exc:
        print(f"\nStopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
