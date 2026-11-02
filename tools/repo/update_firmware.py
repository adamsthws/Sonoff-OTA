#!/usr/bin/env python3
"""Move the firmware the tool installs to a new Tasmota or ESPHome release.

    python3 tools/repo/update_firmware.py check
        what firmware.json pins, against each project's latest stable release;
        exits 1 when either is behind

    python3 tools/repo/update_firmware.py due [--today YYYY-MM-DD]
        the release of each project now due by the settled-release rule
        (SETTLE_DAYS below), as JSON: what the firmware-update workflow takes

    python3 tools/repo/update_firmware.py tasmota [VERSION]
        pin that official Tasmota release (default: the latest) - its ESP32-C3
        app, Safeboot and factory image, from the release's page on GitHub -
        once each is shown to fit the layout the conversion builds

    python3 tools/repo/update_firmware.py esphome [VERSION] [--image-version X.Y.Z]
                                     [--mover] [--new-bootloader]
        build devices/sonoff-s60/esphome.yaml with that ESPHome release (default: the
        latest) in ESPHome's own Docker image, check it, and publish it in
        artifacts/; --mover builds the mover too

    python3 tools/repo/update_firmware.py esphome [VERSION] --plug sonoff-s61 [--image-version X.Y.Z]
        the same for a Beken plug's ESPHome (devices/sonoff-s61/esphome.yaml, on
        LibreTiny): the rt_ota image its build makes, checked and published

    python3 tools/repo/update_firmware.py openbeken [--plug sonoff-s61] [--commit SHA]
                                     [--image-version X.Y.Z]
        build OpenBeken for a Beken plug - upstream at that commit (default: the
        one pinned, else main's head), this project's patches (the 4 MB
        table, the hook before an OTA's restart), its driver and the plug's
        table for it - in this project's build image, check it, and publish it
        in artifacts/

Each writes firmware.json, the published files and SHA256SUMS, and lists what
is left to do by hand. Nothing is pushed or committed. docs/updating-firmware.md
is the whole procedure, and what each check is for.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.autoflash import esphome, imprint, tasmota_settings  # noqa: E402
from tools.beken import rbl, uf2  # noqa: E402
from tools.espressif.safeboot.safeboot_migration import (  # noqa: E402
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

FIRMWARE_JSON = ROOT / "artifacts" / "firmware.json"
# Beside the files it lists, which it names bare: `sha256sum -c` runs in artifacts/.
SHA256SUMS = ROOT / "artifacts" / "SHA256SUMS"
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
    "image": ("devices/sonoff-s60/esphome.yaml", "artifacts/esphome-sonoff-s60-imprintable-{version}.bin"),
    "mover": ("devices/sonoff-s60/mover.yaml", "artifacts/autoflash-mover-{version}.bin"),
}
BOOTLOADER_FILE = "artifacts/esphome-sonoff-s60-bootloader-{version}.bin"
# Downloads ESPHome's builds need (ESP-IDF and its tools, a gigabyte or so),
# kept between builds. captures/ is never committed.
BUILD_CACHE = ROOT / "captures" / "esphome-build"
# The Beken plugs' firmware: each ends on OpenBeken or on ESPHome built on
# LibreTiny, both rt_ota images the stock bootloader applies from the download
# slot (devices/sonoff-s61/). OpenBeken is built from upstream at a pinned
# commit with this project's patches (tools/beken/openbeken/: the 4 MB table,
# offered upstream, and a hook before an OTA's restart, which the driver uses),
# this project's driver (OPENBEKEN_DRIVER, beside the patches) and the plug's
# table for it, in a build image of this project's (build.Dockerfile): the
# SDK's own toolchain wants a newer GLIBC than OpenBeken's image has. Each
# plug: its table and its ESPHome config, and the files their builds are
# published as.
OPENBEKEN_REPO = "openshwprojects/OpenBK7231T_App"
OPENBEKEN_DIR = ROOT / "tools" / "beken" / "openbeken"
# Each patch, and where in the clone it applies: OpenBeken's own tree, or the SDK it clones.
OPENBEKEN_PATCHES = {
    "openbk7231t_app-bk7238-4m-flash.patch": ".",
    "openbk7231t_app-ota-before-reboot-hook.patch": ".",
    "beken_freertos_sdk-4m-pack-profile.patch": "sdk/beken_freertos_sdk",
}
# The driver's source and its header, the first the one with its start-up and commands.
OPENBEKEN_DRIVER = ("drv_autoflash.c", "drv_autoflash.h")
OPENBEKEN_BUILDER = "autoflash-openbeken-build"
OPENBEKEN_BUILD_CACHE = ROOT / "captures" / "openbeken-build"
BEKEN_BUILDS = {
    "sonoff-s61": {
        "openbeken": ("devices/sonoff-s61/drv_autoflash_sonoff_s61.c", "artifacts/openbeken-sonoff-s61-{version}.rbl"),
        "esphome": ("devices/sonoff-s61/esphome.yaml", "artifacts/esphome-sonoff-s61-{version}.rbl"),
    },
}
# What the S60's config reaches into ESPHome for, beyond its documented
# options: its last on_boot lambda saves the Wi-Fi under the one fixed key an
# ESPHome build with no network of its own looks in (devices/sonoff-s60/esphome.md says
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


# -- which release is due ------------------------------------------------------
#
# A release is taken once it has settled: the newest of its series (Tasmota's
# major.minor, ESPHome's month), at least SETTLE_MIN_DAYS old, and either
# followed by a newer series or SETTLE_DAYS old. A fix for a bad release has
# come within days or not at all (Tasmota 14.4.0 to .1 in 4, 15.0.0 to .1 in
# 1, each .0 withdrawn), so this lets it come first; for ESPHome it means the
# month before's last point release, once the new month's .0 is out.
# firmware.json's "hold" names releases never to take.
SETTLE_MIN_DAYS = 7
SETTLE_DAYS = 14
PROJECTS = {"tasmota": TASMOTA_REPO, "esphome": ESPHOME_REPO}


def version_key(project: str, tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in tag.removeprefix("v").split("."))


def series(project: str, tag: str) -> tuple[int, ...]:
    return version_key(project, tag)[:2]


def stable_releases(repo: str) -> list[dict[str, Any]]:
    """Every stable release: its tag and when it was published."""
    found = []
    for page in range(1, 4):
        batch = github(f"repos/{repo}/releases?per_page=100&page={page}")
        found += [{"tag": release["tag_name"],
                   "published": dt.datetime.fromisoformat(release["published_at"].replace("Z", "+00:00"))}
                  for release in batch if not release["draft"] and not release["prerelease"]
                  and release.get("published_at")]
        if len(batch) < 100:
            break
    return found


def settled(project: str, releases: list[dict[str, Any]], today: dt.datetime) -> list[str]:
    """The tags settled by `today`, oldest first."""
    out = []
    published = [release for release in releases if release["published"] <= today]
    for release in published:
        own = series(project, release["tag"])
        newest = max((r for r in published if series(project, r["tag"]) == own),
                     key=lambda r: version_key(project, r["tag"]))
        if newest is not release:
            continue
        age = (today - release["published"]).days
        followed = any(series(project, r["tag"]) > own for r in published)
        if age >= SETTLE_MIN_DAYS and (followed or age >= SETTLE_DAYS):
            out.append(release["tag"])
    return sorted(out, key=lambda tag: version_key(project, tag))


def pinned_version(pins: dict[str, Any], project: str) -> str:
    return pins["tasmota"]["version"] if project == "tasmota" else pins["esphome"]["image"]["esphome"]


def due(project: str, releases: list[dict[str, Any]], today: dt.datetime, pinned: str,
        hold: list[str]) -> dict[str, Any]:
    """What is due for one project: the newest settled release newer than the
    pin, unless held. Where the pinned release is gone (withdrawn, as Tasmota
    has withdrawn a release days after making it), its series' newest release
    at once."""
    tags = {release["tag"].removeprefix("v") for release in releases}
    missing = pinned not in tags
    candidates = [tag.removeprefix("v") for tag in settled(project, releases, today)]
    if missing:
        candidates += [release["tag"].removeprefix("v") for release in releases
                       if series(project, release["tag"]) == series(project, pinned)]
    newer = sorted({tag for tag in candidates if tag not in hold
                    and version_key(project, tag) > version_key(project, pinned)},
                   key=lambda tag: version_key(project, tag))
    latest = max(tags, key=lambda tag: version_key(project, tag)) if tags else None
    return {"pinned": pinned, "pinned_missing": missing, "latest": latest,
            "due": newer[-1] if newer else None}


def command_due(args: argparse.Namespace) -> int:
    pins = load_pins()
    today = (dt.datetime.fromisoformat(args.today).replace(tzinfo=dt.timezone.utc) if args.today
             else dt.datetime.now(dt.timezone.utc))
    report = {project: due(project, stable_releases(repo), today, pinned_version(pins, project),
                           pins.get("hold", {}).get(project, []))
              for project, repo in PROJECTS.items()}
    for project, state in report.items():
        print(f"{project}: pinned {state['pinned']}{' (GONE from its releases)' if state['pinned_missing'] else ''}, "
              f"latest {state['latest']}, due {state['due'] or 'nothing'}", file=sys.stderr)
    print(json.dumps(report))
    return 0


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
    texts, release_version = tasmota_doc_texts(old["version"], version) if not args.no_docs and previous != version \
        else ({}, None)
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
    write_texts(texts)
    print(f"\nfirmware.json now pins Tasmota {version} (was {previous}).")
    if args.no_docs:
        left_to_do([f"CHANGELOG.md: the official Tasmota installed is {version}, with a link to its release notes"])
    elif release_version:
        print(f"CHANGELOG.md says so, under {release_version}.")
    left_to_do(["README.md and docs/: any line naming the Tasmota release installed",
                "python3 -m unittest discover -s tests",
                "the emulator runs in docs/updating-firmware.md, then a real plug"])
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
                    "devices/sonoff-s60/esphome.yaml must be checked against it (devices/sonoff-s60/esphome.md)")


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


def publish(section: dict[str, Any], key: str, file: str, data: bytes, record: dict[str, Any]) -> None:
    """`data` as `file` in artifacts/, pinned under `key` of `section` (a part of
    firmware.json) with `record`, in SHA256SUMS and let past .gitignore; the
    file it replaces removed, unless another key still names it."""
    old = section.get(key, {}).get("file", "")
    (ROOT / file).write_bytes(data)
    if old and old != file and (ROOT / old).exists() and not any(
            other["file"] == old for name, other in section.items() if name != key):
        (ROOT / old).unlink()
    SHA256SUMS.write_text(replace_sums_line(SHA256SUMS.read_text(encoding="utf-8"),
                                            Path(old).name if old and old != file else None, Path(file).name, data),
                          encoding="utf-8")
    GITIGNORE.write_text(with_ignore_exception(GITIGNORE.read_text(encoding="utf-8"), old, file),
                         encoding="utf-8")
    section[key] = {"file": file, **record, "size": len(data), "sha256": sha256_bytes(data)}
    print(f"  published {file}: {len(data):,} bytes, SHA-256 {sha256_bytes(data)}")


def command_esphome(args: argparse.Namespace) -> int:
    release = stable_release(ESPHOME_REPO, args.version)["tag_name"]
    pins = load_pins()
    old_image = dict(pins["esphome"]["image"])
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
    replaced = bool(args.new_bootloader and new_bootloader)
    texts, release_version = ({}, None) if args.no_docs else esphome_doc_texts(
        old_image, {"file": ESPHOME_BUILDS["image"][1].format(version=version), "version": version,
                    "esphome": release, "esp_idf": described["esp_idf"], "sha256": sha256_bytes(app)},
        not new_bootloader, replaced)

    for key, (version, app, _bootloader, described) in built.items():
        config_path = ROOT / ESPHOME_BUILDS[key][0]
        config_path.write_text(with_config_version(config_path.read_text(encoding="utf-8"), version),
                               encoding="utf-8")
        publish(pins["esphome"], key, ESPHOME_BUILDS[key][1].format(version=version), app,
                {"config": ESPHOME_BUILDS[key][0], "version": version, "esphome": release,
                 "esp_idf": described["esp_idf"], "builder": builder})
    version, _app, bootloader, described = built["image"]
    if args.new_bootloader and new_bootloader:
        publish(pins["esphome"], "bootloader", BOOTLOADER_FILE.format(version=version), bootloader,
                {"config": ESPHOME_BUILDS["image"][0], "version": version, "esphome": release,
                 "esp_idf": described["esp_idf"], "builder": builder})
    elif not new_bootloader:
        print("  the bootloader is byte for byte the published one: kept as it is")
    save_pins(pins)
    print("\nfirmware.json, SHA256SUMS, .gitignore and artifacts/ updated: git add the new "
          "files, and git rm the old.")
    by_hand = []
    if args.no_docs:
        by_hand += ["artifacts/README.md: each changed file's row (name, SHA-256) and a paragraph on what changed",
                    "NOTICES.md: the files' names, the ESPHome release and ESP-IDF version",
                    "devices/sonoff-s60/esphome.md: the ESPHome release that built the current image",
                    f"CHANGELOG.md: ESPHome is built with {release}"]
    else:
        write_texts(texts)
        print(f"artifacts/README.md, NOTICES.md, devices/sonoff-s60/esphome.md and CHANGELOG.md "
              f"({release_version}) say so.")
        if args.mover or replaced:
            by_hand.append("the mover's or bootloader's own rows and paragraphs, which only the image's are written for")
        if old_image["esphome"] == release:
            by_hand.append("what the change to the config does, in CHANGELOG.md and artifacts/README.md")
    left_to_do(by_hand + ["python3 -m unittest discover -s tests (tests/repo/test_firmware_pins.py checks the docs)",
                          "the emulator runs in docs/updating-firmware.md, then a real plug"])
    return 0


# -- the Beken plugs' firmware ---------------------------------------------------


def beken_section(pins: dict[str, Any], plug: str) -> dict[str, Any]:
    return pins.setdefault("beken", {}).setdefault(plug, {})


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), check=False, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode:
        raise UpdateError(f"git {' '.join(args[:2])} failed in {cwd.name}:\n{result.stdout}")
    return result.stdout.strip()


def openbeken_sources(plug: str) -> list[str]:
    """What a plug's OpenBeken is built with beyond upstream and the patches:
    the driver, then the plug's table for it."""
    return ([(OPENBEKEN_DIR / name).relative_to(ROOT).as_posix() for name in OPENBEKEN_DRIVER]
            + [BEKEN_BUILDS[plug]["openbeken"][0]])


def module_init(source: str) -> str:
    """The driver's one start-up function, `void Something_Init(void)`."""
    found = re.findall(r"^void (\w+_Init)\(void\)", source, re.MULTILINE)
    if len(found) != 1:
        raise UpdateError("the driver must define one `void <Name>_Init(void)` to be called at start-up")
    return found[0]


def module_commands(source: str) -> list[str]:
    """The commands the driver registers: each must be in the image built."""
    return re.findall(r'CMD_RegisterCommand\("(\w+)"', source)


def with_module(makefile: str, main: str, sources: list[str], init: str) -> tuple[str, str]:
    """platforms/obk_main.mk and src/user_main.c with the driver and the plug's
    table (`sources`, in src/driver/) built and run at start-up, as
    devices/sonoff-s61/openbeken.md says to add them: listed beside the other
    drivers, and called once the config is loaded."""
    anchor = "OBKM_SRC  += $(OBK_SRCS)driver/drv_main.c\n"
    if makefile.count(anchor) != 1:
        raise UpdateError("platforms/obk_main.mk no longer lists driver/drv_main.c once: where the driver goes")
    call = "\tCFG_InitAndLoad();\n"
    if main.count(call) != 1:
        raise UpdateError("src/user_main.c no longer calls CFG_InitAndLoad() once: where the driver's start goes")
    return (makefile.replace(anchor, anchor + "".join(f"OBKM_SRC  += $(OBK_SRCS)driver/{name}\n" for name in sources)),
            main.replace(call, call + f"\n\textern void {init}(void);\n\t{init}();\n"))


def openbeken_source(commit: str) -> Path:
    """A fresh clone of OpenBeken at `commit`, its SDK with it, with this
    project's patches applied (tools/beken/openbeken/, OPENBEKEN_PATCHES)."""
    source = OPENBEKEN_BUILD_CACHE / "OpenBK7231T_App"
    if source.exists():
        # The SDK's toolchain unpacks read-only: made writable as it is removed.
        def writable_then(remove, path, _exc):
            os.chmod(Path(path).parent, 0o700)
            remove(path)
        shutil.rmtree(source, onerror=writable_then)
    OPENBEKEN_BUILD_CACHE.mkdir(parents=True, exist_ok=True)
    print(f"  cloning {OPENBEKEN_REPO} at {commit[:7]} and its SDK (a minute or two)...")
    git("clone", "--quiet", f"https://github.com/{OPENBEKEN_REPO}.git", str(source), cwd=OPENBEKEN_BUILD_CACHE)
    git("checkout", "--quiet", commit, cwd=source)
    git("submodule", "update", "--init", "--depth=1", "sdk/beken_freertos_sdk", cwd=source)
    for patch, where in OPENBEKEN_PATCHES.items():
        git("apply", str(OPENBEKEN_DIR / patch), cwd=source / where)
    return source


def openbeken_builder() -> str:
    """This project's build image, built from build.Dockerfile: named by the
    base image's digest and the Dockerfile's hash, which is what pins a build."""
    dockerfile = OPENBEKEN_DIR / "build.Dockerfile"
    base = re.search(r"^FROM (\S+)", dockerfile.read_text(encoding="utf-8"), re.MULTILINE).group(1)
    docker("pull", "--quiet", base)
    docker("build", "--quiet", "-t", OPENBEKEN_BUILDER, "-f", str(dockerfile), str(OPENBEKEN_DIR))
    digests = json.loads(docker("image", "inspect", "--format", "{{json .RepoDigests}}", base))
    pinned = next((entry for entry in digests if entry.startswith(base.split(":")[0] + "@")), base)
    return f"{pinned} with {dockerfile.relative_to(ROOT)} {sha256_bytes(dockerfile.read_bytes())[:16]}"


def build_openbeken(source: Path, version: str) -> bytes:
    """OpenBeken for the BK7238 with the 4 MB table, at `version`: the rt_ota image."""
    # Through a shell: the SDK's makefiles find their toolchain by $PWD, which
    # only a shell sets.
    run = ["run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp", "-v", f"{source}:/app",
           OPENBEKEN_BUILDER, "sh", "-c"]
    print("  building OpenBeken (a minute or two)...")
    docker(*run, "cd /app && make APP_NAME=OpenBK7238 prebuild_OpenBK7238")
    docker(*run, f"cd /app && make APP_VERSION={version} APP_NAME=OpenBK7238 FLASH_SIZE=4M OpenBK7238")
    return (source / "output" / version / f"OpenBK7238_4M_{version}.rbl").read_bytes()


def check_rbl(image: bytes, label: str, version: str | None, needles: list[bytes]) -> bytes:
    """An rt_ota app image the stock bootloader applies, unpacked and checked:
    it fits the download slot, says it is `version` (where the build names it
    so), and holds each of `needles` - what the build was to put in it."""
    try:
        header = rbl.header(image)
        firmware = rbl.unpack(image)
    except rbl.RblError as exc:
        raise UpdateError(f"{label} is not an rt_ota image: {exc}") from exc
    if header.name != "app":
        raise UpdateError(f"{label} is an rt_ota {header.name!r} image, not an app")
    if version is not None and header.version != version:
        raise UpdateError(f"{label} says it is {header.version}, not {version}")
    if len(image) > uf2.DOWNLOAD_SIZE:
        raise UpdateError(f"{label} is {len(image):,} bytes; the download slot holds {uf2.DOWNLOAD_SIZE:,}")
    for needle in needles:
        if needle not in firmware:
            raise UpdateError(f"{label} does not hold {needle.decode('ascii', 'replace')!r}: not built as meant")
    return firmware


def command_openbeken(args: argparse.Namespace) -> int:
    file_pattern = BEKEN_BUILDS[args.plug]["openbeken"][1]
    sources = openbeken_sources(args.plug)
    pins = load_pins()
    section = beken_section(pins, args.plug)
    current = section.get("openbeken", {})
    commit = args.commit or current.get("app_commit") or github(f"repos/{OPENBEKEN_REPO}/commits/main")["sha"]
    version = args.image_version or (next_minor(current["version"]) if current else "1.0.0")
    driver = (ROOT / sources[0]).read_text(encoding="utf-8")
    init, commands = module_init(driver), module_commands(driver)
    builder = openbeken_builder()
    print(f"OpenBeken {version} for the {args.plug}: {OPENBEKEN_REPO} at {commit[:7]}, built in {builder}")
    source = openbeken_source(commit)
    for path in sources:
        target = source / "src" / "driver" / Path(path).name
        if target.exists():
            raise UpdateError(f"OpenBeken has a src/driver/{target.name} of its own: the driver's would replace it")
        shutil.copyfile(ROOT / path, target)
    makefile_path, main_path = source / "platforms" / "obk_main.mk", source / "src" / "user_main.c"
    makefile, main = with_module(makefile_path.read_text(encoding="utf-8"), main_path.read_text(encoding="utf-8"),
                                 [Path(path).name for path in sources if path.endswith(".c")], init)
    makefile_path.write_text(makefile, encoding="utf-8")
    main_path.write_text(main, encoding="utf-8")
    image = build_openbeken(source, version)
    firmware = check_rbl(image, "the OpenBeken built", version, [command.encode("ascii") for command in commands])
    sdk_commit = git("rev-parse", "HEAD", cwd=source / "sdk" / "beken_freertos_sdk")
    print(f"  OpenBeken {version}: {len(image):,} bytes ({len(firmware):,} unpacked), the driver's "
          f"{', '.join(commands)} in it, {uf2.DOWNLOAD_SIZE - len(image):,} bytes to spare in the download slot")
    if args.dry_run:
        print("\nBuilt and checked; nothing published (--dry-run).")
        return 0
    publish(section, "openbeken", file_pattern.format(version=version), image, {
        "version": version, "app_commit": commit, "sdk_commit": sdk_commit,
        "sources": {path: sha256_bytes((ROOT / path).read_bytes()) for path in sources},
        "patches": {patch: sha256_bytes((OPENBEKEN_DIR / patch).read_bytes()) for patch in OPENBEKEN_PATCHES},
        "builder": builder})
    save_pins(pins)
    print("\nfirmware.json, SHA256SUMS, .gitignore and artifacts/ updated: git add the new file, and git rm the old.")
    left_to_do(["artifacts/README.md: the file's row (name, SHA-256) and what changed",
                "NOTICES.md: the file's name, the OpenBeken commit and the SDK's",
                "CHANGELOG.md: the plug's OpenBeken is this version",
                "python3 tests/run.py (tests/repo/test_firmware_pins.py checks the docs)",
                "the emulator: tools/beken/cloud_push.py with the file, then the tool's own route"])
    return 0


def build_libretiny(builder: str, config_path: Path, version: str, workdir: Path) -> tuple[bytes, dict[str, Any], str]:
    """Build one LibreTiny config at this version: its rt_ota image, the board
    (its layout) the build used, and the LibreTiny version."""
    config = with_config_version(config_path.read_text(encoding="utf-8"), version)
    (workdir / config_path.name).write_text(config, encoding="utf-8")
    print(f"  building {config_path.name} {version} (minutes; a first build downloads LibreTiny and its SDK)...")
    docker("run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/config",
           "-v", f"{workdir}:/config", builder, "compile", config_path.name)
    name = re.search(r"^  name: (\S+)", config, re.MULTILINE).group(1)
    out = workdir / ".esphome" / "build" / name / ".pioenvs" / name
    board = json.loads((out / "board.json").read_text(encoding="utf-8"))
    libretiny = next(path.name for path in out.glob("*_lt*.uf2")).rsplit("_lt", 1)[1].removesuffix(".uf2")
    return (out / "image_bk7238_app.ota.rbl").read_bytes(), board, libretiny


def command_esphome_beken(args: argparse.Namespace) -> int:
    config_path, file_pattern = BEKEN_BUILDS[args.plug]["esphome"]
    release = stable_release(ESPHOME_REPO, args.version)["tag_name"]
    pins = load_pins()
    section = beken_section(pins, args.plug)
    current = section.get("esphome", {})
    config = (ROOT / config_path).read_text(encoding="utf-8")
    version = args.image_version or (next_minor(current["version"]) if current else config_version(config))
    builder = esphome_builder(release)
    print(f"ESPHome {release} for the {args.plug}: {builder}")
    BUILD_CACHE.mkdir(parents=True, exist_ok=True)
    image, board, libretiny = build_libretiny(builder, ROOT / config_path, version, BUILD_CACHE)
    project = re.search(r"^    name: (\S+)", config, re.MULTILINE).group(1)
    check_rbl(image, "the ESPHome built", None,
              [version.encode("ascii"), project.encode("ascii"), release.encode("ascii"), b"autoflash_wifi"])
    layout = board.get("flash", {})
    if layout.get("download") != f"0x{uf2.DOWNLOAD_OFFSET:06X}+0x{uf2.DOWNLOAD_SIZE:X}":
        raise UpdateError(f"the build's download partition is {layout.get('download')}, not the stock "
                          f"bootloader's slot at 0x{uf2.DOWNLOAD_OFFSET:x}: the config's platformio_options")
    print(f"  ESPHome {version}: {len(image):,} bytes, LibreTiny {libretiny}, "
          f"{uf2.DOWNLOAD_SIZE - len(image):,} bytes to spare in the download slot")
    if args.dry_run:
        print("\nBuilt and checked; nothing published (--dry-run).")
        return 0
    (ROOT / config_path).write_text(with_config_version(config, version), encoding="utf-8")
    publish(section, "esphome", file_pattern.format(version=version), image,
            {"config": config_path, "version": version, "esphome": release, "libretiny": libretiny,
             "builder": builder})
    save_pins(pins)
    print("\nfirmware.json, SHA256SUMS, .gitignore, the config's version and artifacts/ updated: "
          "git add the new file, and git rm the old.")
    left_to_do(["artifacts/README.md: the file's row (name, SHA-256) and what changed",
                "NOTICES.md: the file's name, the ESPHome release and LibreTiny version",
                f"devices/{args.plug}/esphome.md: the ESPHome release that built the current image",
                "CHANGELOG.md: the plug's ESPHome is this version",
                "python3 tests/run.py (tests/repo/test_firmware_pins.py checks the docs)",
                "the emulator: tools/beken/cloud_push.py with the file, then the tool's own route"])
    return 0


# -- what is written about it ----------------------------------------------------
#
# Each update writes what it changes into the docs the tests hold to it
# (tests/repo/test_firmware_pins.py), and its line in CHANGELOG.md: under the
# section not yet released, or a new one, a minor version on (raising
# tools/autoflash/__init__.py), so that merging it is the release. --no-docs
# leaves them for a person to write.
CHANGELOG = ROOT / "CHANGELOG.md"
VERSION_FILE = ROOT / "tools/autoflash/__init__.py"
ARTIFACTS_README = ROOT / "artifacts/README.md"
NOTICES = ROOT / "NOTICES.md"
ESPHOME_README = ROOT / "devices/sonoff-s60/esphome.md"


def released(version: str) -> bool:
    return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "-q", "--verify", f"refs/tags/v{version}"],
                          capture_output=True).returncode == 0


def bullet(text: str) -> str:
    return textwrap.fill(text, width=76, initial_indent="- ", subsequent_indent="  ",
                         break_long_words=False, break_on_hyphens=False)


def changelog_with(entry: str, texts: dict[Path, str]) -> str:
    """CHANGELOG.md and tools/autoflash/__init__.py, in `texts`, with the entry
    under the release to come; that release's version."""
    text = texts.get(CHANGELOG) or CHANGELOG.read_text(encoding="utf-8")
    heading = re.search(r"^## (\d+\.\d+\.\d+)\n", text, re.MULTILINE)
    version = heading.group(1)
    if released(version):
        version = next_minor(version)
        text = text[:heading.start()] + f"## {version}\n\n{bullet(entry)}\n\n" + text[heading.start():]
    else:
        following = re.search(r"^## ", text[heading.end():], re.MULTILINE)
        end = heading.end() + following.start() if following else len(text)
        text = text[:heading.end()] + text[heading.end():end].rstrip("\n") + "\n" + bullet(entry) + "\n\n" + text[end:]
    texts[CHANGELOG] = text
    texts[VERSION_FILE] = re.sub(r'^__version__ = "[^"]+"', f'__version__ = "{version}"',
                                 VERSION_FILE.read_text(encoding="utf-8"), count=1, flags=re.MULTILINE)
    return version


def write_texts(texts: dict[Path, str]) -> None:
    for path, text in texts.items():
        path.write_text(text, encoding="utf-8")


def substituted(text: str, pattern: str, replacement, what: str) -> str:
    new, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if not count:
        raise UpdateError(f"could not find {what} to update: nothing was changed; --no-docs leaves the docs "
                          "to be written by hand")
    return new


def tasmota_doc_texts(old: str, new: str) -> tuple[dict[Path, str], str]:
    texts: dict[Path, str] = {}
    version = changelog_with(f"A converted plug ends on Tasmota "
                             f"[{new}](https://github.com/{TASMOTA_REPO}/releases/tag/v{new}), where it ended on {old}.",
                             texts)
    return texts, version


def esphome_doc_texts(old: dict[str, Any], new: dict[str, Any], bootloader_same: bool,
                      replaced: bool = False) -> tuple[dict[Path, str], str]:
    """artifacts/README.md, NOTICES.md, devices/sonoff-s60/esphome.md and the
    changelog for a new image: the same config with a new ESPHome release, or,
    with the same release, a change to the config, which a person describes -
    worked out before anything is written, so a passage not found changes
    nothing."""
    old_name, new_name = Path(old["file"]).name, Path(new["file"]).name
    kept = ("Its build's bootloader is the one it starts with now." if replaced else
            "Its build's bootloader is byte for byte the published one." if bootloader_same else
            f"The bootloader that build makes (ESP-IDF {new['esp_idf']}) differs from the published one, "
            "which is kept: no plug is given a new one.")
    readme = ARTIFACTS_README.read_text(encoding="utf-8")
    readme = substituted(readme, rf"^\| `{re.escape(old_name)}` \|.*$",
                         lambda row: row.group(0).replace(old_name, new_name).replace(old["sha256"], new["sha256"]),
                         f"{old_name}'s row in artifacts/README.md")
    readme = substituted(readme, r"(\(its `project` version is the one in the file name\) with ESPHome )\S+( and\s+ESP-IDF )[\d.]+",
                         lambda m: f"{m.group(1)}{new['esphome']}{m.group(2)}{new['esp_idf']}",
                         "the ESPHome release that builds the image, in artifacts/README.md")
    built = (f"a change to {old['version']}'s config, built with ESPHome {new['esphome']} "
             f"(ESP-IDF {new['esp_idf']}) as {old['version']} was" if new["esphome"] == old["esphome"] else
             f"{old['version']}'s config built with ESPHome {new['esphome']} (ESP-IDF {new['esp_idf']}), "
             f"where {old['version']} was built with {old['esphome']} (ESP-IDF {old['esp_idf']}): the config "
             "is unchanged but for its version")
    paragraph = textwrap.fill(
        f"ESPHome {new['version']} is {built}. {kept} {old['version']} is in the Git history:",
        width=79, break_long_words=False, break_on_hyphens=False)
    texts = {ARTIFACTS_README: readme.rstrip("\n") + f"\n\n{paragraph}\n`git log --all -- {old['file']}`.\n"}
    notices = NOTICES.read_text(encoding="utf-8")
    notices = substituted(notices, r"`artifacts/esphome-sonoff-s60-imprintable-[\d.]+\.bin` is built with ESPHome(\s+)\S+ and ESP-IDF [\d.]+",
                          lambda m: f"`{new['file']}` is built with ESPHome{m.group(1)}{new['esphome']} and ESP-IDF {new['esp_idf']}",
                          "the image's paragraph in NOTICES.md")
    texts[NOTICES] = substituted(notices, r"\[ESPHome \S+ release\]\(https://github.com/esphome/esphome/tree/\S+\)",
                                 f"[ESPHome {new['esphome']} release](https://github.com/esphome/esphome/tree/{new['esphome']})",
                                 "the image's source release in NOTICES.md")
    texts[ESPHOME_README] = substituted(ESPHOME_README.read_text(encoding="utf-8"), r"ESPHome \S+ built the current",
                                        f"ESPHome {new['esphome']} built the current", "devices/sonoff-s60/esphome.md's release")
    release = f"[{new['esphome']}](https://github.com/{ESPHOME_REPO}/releases/tag/{new['esphome']})"
    version = changelog_with(
        f"ESPHome for the S60 is {new['version']}: "
        + (f"a change to its config, built with ESPHome {release} as {old['version']} was."
           if new["esphome"] == old["esphome"] else
           f"the same config, built with ESPHome {release}, where {old['version']} was built with {old['esphome']}.")
        + ("" if replaced else " Its bootloader is unchanged, so a plug on ESPHome already is not given a new one."),
        texts)
    return texts, version


def left_to_do(items: list[str]) -> None:
    print("\nLeft to do (docs/updating-firmware.md):")
    for item in items:
        print(f"  - {item}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="pinned against latest").set_defaults(run=command_check)
    due_parser = commands.add_parser("due", help="what the settled-release rule takes now, as JSON")
    due_parser.add_argument("--today", help="as on this date (YYYY-MM-DD), for a look ahead")
    due_parser.set_defaults(run=command_due)
    tasmota = commands.add_parser("tasmota", help="pin an official Tasmota release")
    tasmota.add_argument("version", nargs="?", help="e.g. 16.0.0 (default: the latest)")
    tasmota.add_argument("--dry-run", action="store_true", help="check it, change nothing")
    tasmota.add_argument("--no-docs", action="store_true", help="leave CHANGELOG.md to be written by hand")
    tasmota.set_defaults(run=command_tasmota)
    build_parser = commands.add_parser("esphome", help="build and publish ESPHome with a release")
    build_parser.add_argument("version", nargs="?", help="e.g. 2026.10.0 (default: the latest)")
    build_parser.add_argument("--image-version", help="the S60 image's own version (default: next minor)")
    build_parser.add_argument("--mover", action="store_true", help="build and publish the mover too")
    build_parser.add_argument("--new-bootloader", action="store_true",
                              help="publish this build's bootloader where it differs (see the docs first)")
    build_parser.add_argument("--dry-run", action="store_true", help="build and check, publish nothing")
    build_parser.add_argument("--no-docs", action="store_true", help="leave the docs to be written by hand")
    build_parser.add_argument("--plug", choices=sorted(BEKEN_BUILDS),
                              help="a Beken plug's ESPHome (on LibreTiny) instead of the S60's")
    build_parser.set_defaults(run=command_esphome)
    openbeken = commands.add_parser("openbeken", help="build and publish OpenBeken for a Beken plug")
    openbeken.add_argument("--plug", choices=sorted(BEKEN_BUILDS), default="sonoff-s61")
    openbeken.add_argument("--commit", help="OpenBK7231T_App's commit (default: the one pinned, else main's head)")
    openbeken.add_argument("--image-version", help="the image's own version (default: next minor, or 1.0.0)")
    openbeken.add_argument("--dry-run", action="store_true", help="build and check, publish nothing")
    openbeken.set_defaults(run=command_openbeken)
    args = parser.parse_args(argv)
    if args.command == "esphome" and args.plug:
        args.run = command_esphome_beken
    if shutil.which("docker") is None and args.command in ("esphome", "openbeken"):
        print("Firmware is built in Docker images: Docker is needed.", file=sys.stderr)
        return 2
    try:
        return args.run(args)
    except UpdateError as exc:
        print(f"\nStopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
