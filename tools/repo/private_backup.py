#!/usr/bin/env python3
"""The maintainer's Sonoff backup, sealed, kept off this computer and private.

The emulator boots one real plug's Sonoff backup - firmware that is Sonoff's,
with that plug's eWeLink keys and the Wi-Fi it was set up on - of which there
is no other copy to be had. This seals it (compressed, then encrypted with a
key only its owner has) and keeps the sealed file in a private package on
GitHub's container registry, beside the public repository but never in it:
not in its history, not in a clone, not in a release (docs/private-backup.md).

    python3 tools/repo/private_backup.py key
        a new key, written into .env as AUTOFLASH_BACKUP_KEY - never shown
    python3 tools/repo/private_backup.py seal BACKUP_DIR SEALED
        the backup (flash.bin, efuse.bin, report.json, README.txt), sealed
    python3 tools/repo/private_backup.py verify SEALED BACKUP_DIR
        whether SEALED opens to exactly BACKUP_DIR, writing nothing
    python3 tools/repo/private_backup.py unseal SEALED OUT_DIR
        the backup again, into a new directory, every file checked
    python3 tools/repo/private_backup.py restore
        fetched and unsealed into captures/plugs/<MAC>/, where the emulator
        looks - unless it is there already, as it was sealed
    python3 tools/repo/private_backup.py upload SEALED
        into the private package, then shown to be private
    python3 tools/repo/private_backup.py fetch SEALED
        out of it
    python3 tools/repo/private_backup.py check-private
        shown that nobody but its owner can get the package
    python3 tools/repo/private_backup.py secret
        the key, from .env, as the repository's Actions secret

The key comes from AUTOFLASH_BACKUP_KEY, in the environment (a GitHub
Actions secret, say) or .env. The registry is reached with `gh auth token`
(read:packages / write:packages), or GITHUB_TOKEN in a workflow given
`packages: read` and access to the package.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import lzma
import os
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.autoflash import credentials, plugs  # noqa: E402

KEY_NAME = "AUTOFLASH_BACKUP_KEY"
OWNER, PACKAGE, TAG = "adamsthws", "autoflash-private", "sonoff-backup"
REFERENCE = f"ghcr.io/{OWNER}/{PACKAGE}:{TAG}"
REPOSITORY = "adamsthws/Sonoff-OTA"
ARTIFACT_TYPE = "application/vnd.autoflash.sealed-backup.v1"
SEALED_NAME = "sonoff-backup.sealed"
# ORAS, which keeps a file in a container registry, run from its own image:
# nothing to install beyond Docker. Pinned by digest.
ORAS = "ghcr.io/oras-project/oras:v1.2.3@sha256:63266a046d1cf5ebebb1461733ed7548148f122e0d422096e177cfa70b521cb1"
# A public package: what the privacy check expects of one, to show the check
# would see the difference.
PUBLIC_CONTROL = ("oras-project/oras", "v1.2.3")

# The sealed file: this line, a 12-byte nonce, then the xz-compressed tar of
# the backup encrypted with AES-256-GCM under the key, this line its
# associated data - so any AES-GCM implementation can open it.
MAGIC = b"AUTOFLASH-SEALED-BACKUP 1\n"
NONCE_SIZE, KEY_SIZE = 12, 32
MANIFEST = "MANIFEST.json"
# The plug's own record, beside its backup directory (its eWeLink device and
# model, which the emulator's cloud stub gives as eWeLink would): sealed with it.
PLUG_RECORD = "plug.json"


class SealError(RuntimeError):
    pass


# -- the key -----------------------------------------------------------------

def env_file_values() -> dict[str, str]:
    try:
        return credentials.parse(credentials.ENV_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def key() -> bytes:
    text = os.environ.get(KEY_NAME) or env_file_values().get(KEY_NAME)
    if not text:
        raise SealError(f"no {KEY_NAME}: in the environment or .env (`key` makes one)")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError:
        raw = b""
    if len(raw) != KEY_SIZE:
        raise SealError(f"{KEY_NAME} is not a key this tool made")
    return raw


def command_key(_args: argparse.Namespace) -> int:
    if os.environ.get(KEY_NAME) or KEY_NAME in env_file_values():
        raise SealError(f"{KEY_NAME} is set already: a new key would not open what the old one sealed")
    text = base64.urlsafe_b64encode(secrets.token_bytes(KEY_SIZE)).decode().rstrip("=")
    existing = credentials.ENV_FILE.read_text(encoding="utf-8") if credentials.ENV_FILE.exists() else ""
    with open(credentials.ENV_FILE, "a", encoding="utf-8") as env:
        env.write(("" if not existing or existing.endswith("\n") else "\n")
                  + "\n# Opens the maintainer's sealed Sonoff backup (tools/repo/private_backup.py).\n"
                  + f"# Keep a copy somewhere else too: nothing else opens it.\n{KEY_NAME}={text}\n")
    os.chmod(credentials.ENV_FILE, 0o600)
    print(f"A new key is in {credentials.ENV_FILE} as {KEY_NAME} (not shown). Copy it into your password "
          "manager: without it the sealed backup cannot be opened, by anyone.")
    return 0


# -- sealing -----------------------------------------------------------------

def backup_files(directory: Path) -> dict[str, bytes]:
    """A backup directory's files, and its plug's record beside it."""
    files = {path.name: path.read_bytes() for path in sorted(directory.iterdir()) if path.is_file()}
    if "flash.bin" not in files or "efuse.bin" not in files:
        raise SealError(f"{directory} is not a Sonoff backup: no flash.bin and efuse.bin")
    if PLUG_RECORD in files:
        raise SealError(f"{directory} holds a {PLUG_RECORD} of its own")
    record = directory.parent / PLUG_RECORD
    if record.is_file():
        files[PLUG_RECORD] = record.read_bytes()
    return files


def write_backup(files: dict[str, bytes], directory: Path) -> None:
    """Into a new directory, each file read-only as a backup is kept; the
    plug's record beside it."""
    directory.mkdir(parents=True, mode=0o700)
    for name, data in files.items():
        path = directory.parent / name if name == PLUG_RECORD else directory / name
        path.write_bytes(data)
        path.chmod(0o600 if name == PLUG_RECORD else 0o400)


def packed(files: dict[str, bytes]) -> bytes:
    """A tar of the files and their manifest, the same bytes every time."""
    files = dict(sorted(files.items()))
    manifest = {"schema": 1, "files": {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                                       for name, data in files.items()}}
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in [(MANIFEST, json.dumps(manifest, indent=2).encode() + b"\n"), *files.items()]:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o444, 0
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def unpacked(data: bytes) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
        for member in tar.getmembers():
            if not member.isfile() or "/" in member.name or member.name in ("", ".", ".."):
                raise SealError("the sealed backup holds something other than plain files")
            files[member.name] = tar.extractfile(member).read()
    manifest = json.loads(files.pop(MANIFEST, b"{}"))
    listed = manifest.get("files", {})
    if set(listed) != set(files):
        raise SealError("the sealed backup's files are not its manifest's")
    for name, data in files.items():
        if (len(data), hashlib.sha256(data).hexdigest()) != (listed[name]["size"], listed[name]["sha256"]):
            raise SealError(f"{name} in the sealed backup is not what was sealed")
    return files


def seal(files: dict[str, bytes], secret: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(NONCE_SIZE)
    plain = lzma.compress(packed(files), preset=9)
    return MAGIC + nonce + AESGCM(secret).encrypt(nonce, plain, MAGIC)


def opened(sealed: bytes, secret: bytes) -> dict[str, bytes]:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not sealed.startswith(MAGIC):
        raise SealError("not a sealed backup")
    nonce = sealed[len(MAGIC):len(MAGIC) + NONCE_SIZE]
    try:
        plain = AESGCM(secret).decrypt(nonce, sealed[len(MAGIC) + NONCE_SIZE:], MAGIC)
    except InvalidTag:
        raise SealError("the key does not open it, or it has been changed") from None
    return unpacked(lzma.decompress(plain))


def command_seal(args: argparse.Namespace) -> int:
    if args.sealed.exists():
        raise SealError(f"{args.sealed} is there already")
    files = backup_files(args.backup)
    secret = key()
    sealed = seal(files, secret)
    if opened(sealed, secret) != files:  # proved before it is written
        raise SealError("the sealed backup does not open to the backup")
    args.sealed.write_bytes(sealed)
    print(f"Sealed {len(files)} files ({sum(map(len, files.values())):,} bytes) into {args.sealed}: "
          f"{len(sealed):,} bytes, SHA-256 {hashlib.sha256(sealed).hexdigest()}")
    print(f"  flash.bin SHA-256 {hashlib.sha256(files['flash.bin']).hexdigest()}")
    return 0


def command_verify(args: argparse.Namespace) -> int:
    files = opened(args.sealed.read_bytes(), key())
    original = backup_files(args.backup)
    if files != original:
        differing = sorted(set(files) ^ set(original) | {n for n in files if original.get(n) != files[n]})
        raise SealError(f"it differs from {args.backup}: {', '.join(differing)}")
    print(f"{args.sealed} opens to exactly {args.backup} ({len(files)} files; "
          f"flash.bin SHA-256 {hashlib.sha256(files['flash.bin']).hexdigest()})")
    return 0


def command_unseal(args: argparse.Namespace) -> int:
    if args.out.exists():
        raise SealError(f"{args.out} is there already: unsealing never writes over anything")
    files = opened(args.sealed.read_bytes(), key())
    files.pop(PLUG_RECORD, None)  # a backup directory of its own: not beside anything
    args.out.mkdir(parents=True, mode=0o700)
    for name, data in files.items():
        path = args.out / name
        path.write_bytes(data)
        path.chmod(0o400)  # as a backup is kept: read-only
    print(f"Unsealed {len(files)} files into {args.out}, each checked against the manifest sealed with it "
          f"(flash.bin SHA-256 {hashlib.sha256(files['flash.bin']).hexdigest()})")
    return 0


# -- the registry ------------------------------------------------------------

def registry_token() -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        return subprocess.run(["gh", "auth", "token"], check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SealError("no GITHUB_TOKEN and no `gh auth token` to reach the registry") from exc


def oras(*args: str, workdir: Path) -> None:
    command = ["docker", "run", "--rm", "-i", "-u", f"{os.getuid()}:{os.getgid()}",
               "-v", f"{workdir}:/workspace", "-w", "/workspace", ORAS, *args,
               "--username", OWNER, "--password-stdin"]
    done = subprocess.run(command, input=registry_token(), text=True, capture_output=True)
    if done.returncode:
        raise SealError(f"oras {args[0]} failed: {(done.stderr or done.stdout).strip()}")


def command_upload(args: argparse.Namespace) -> int:
    sealed = args.sealed.read_bytes()
    if not sealed.startswith(MAGIC):
        raise SealError(f"{args.sealed} is not a sealed backup: only a sealed one is ever uploaded")
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copyfile(args.sealed, Path(tmp) / SEALED_NAME)
        oras("push", REFERENCE, "--artifact-type", ARTIFACT_TYPE,
             "--annotation", "org.opencontainers.image.description=Sealed Sonoff backup (private)",
             f"{SEALED_NAME}:application/octet-stream", workdir=Path(tmp))
    print(f"Uploaded {args.sealed} to {REFERENCE} ({len(sealed):,} bytes).")
    return command_check_private(args)


def command_fetch(args: argparse.Namespace) -> int:
    if args.sealed.exists():
        raise SealError(f"{args.sealed} is there already")
    data = fetched()
    args.sealed.write_bytes(data)
    print(f"Fetched {REFERENCE} into {args.sealed}: {len(data):,} bytes, "
          f"SHA-256 {hashlib.sha256(data).hexdigest()}")
    return 0


def fetched() -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        oras("pull", REFERENCE, workdir=Path(tmp))
        data = (Path(tmp) / SEALED_NAME).read_bytes()
    if not data.startswith(MAGIC):
        raise SealError("what came down is not a sealed backup")
    return data


def restore() -> str:
    """The sealed backup, fetched and unsealed into captures/plugs/<MAC>/, unless
    it is there already exactly as sealed; never written over. Its MAC."""
    files = opened(fetched(), key())
    mac = plugs.mac_from_efuse(files["efuse.bin"])
    directory = plugs.PLUGS / plugs.folder_name(mac) / "sonoff-backup"
    if directory.exists():
        here = backup_files(directory)
        if {name: data for name, data in here.items() if name != PLUG_RECORD} != \
                {name: data for name, data in files.items() if name != PLUG_RECORD}:
            raise SealError(f"{directory} is there and is not the sealed backup: left as it is")
        return mac
    write_backup(files, directory)
    return mac


def command_restore(_args: argparse.Namespace) -> int:
    mac = restore()
    print(f"The sealed backup of {mac} is in {plugs.PLUGS / plugs.folder_name(mac)}, as it was sealed.")
    return 0


def anonymous_manifest(repository: str, tag: str) -> int:
    """The HTTP status of the registry's answer, asked as nobody, for a tag's
    manifest: 200 is what anyone could pull."""
    url = f"https://ghcr.io/token?scope=repository:{repository}:pull&service=ghcr.io"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            token = json.load(response).get("token", "")
    except urllib.error.HTTPError as exc:
        return exc.code  # not even an anonymous token for it
    request = urllib.request.Request(
        f"https://ghcr.io/v2/{repository}/manifests/{tag}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.oci.image.manifest.v1+json, application/vnd.oci.image.index.v1+json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def command_check_private(_args: argparse.Namespace) -> int:
    problems = []
    control = anonymous_manifest(*PUBLIC_CONTROL)
    print(f"  a public package, asked for as nobody: HTTP {control} (200: the check can tell)")
    if control != 200:
        problems.append("the check cannot tell a public package from a private one just now")
    ours = anonymous_manifest(f"{OWNER}/{PACKAGE}", TAG)
    print(f"  this package, asked for as nobody: HTTP {ours} (anything but 200: nobody can pull it)")
    if ours == 200:
        problems.append("ANYONE CAN PULL IT")
    asked = subprocess.run(["gh", "api", f"/user/packages/container/{PACKAGE}", "--jq", ".visibility"],
                           capture_output=True, text=True)
    visibility = asked.stdout.strip() if asked.returncode == 0 else f"unknown ({asked.stderr.strip()})"
    print(f"  GitHub's own setting for it: {visibility}")
    if visibility != "private":
        problems.append(f"its visibility is {visibility}")
    # And it is there, under its tag: a refusal to nobody is not just "no such thing".
    asked = subprocess.run(["gh", "api", f"/user/packages/container/{PACKAGE}/versions",
                            "--jq", "[.[].metadata.container.tags[]]"], capture_output=True, text=True)
    tags = json.loads(asked.stdout) if asked.returncode == 0 and asked.stdout.strip() else []
    print(f"  its tags, as its owner sees them: {', '.join(tags) or 'none'}")
    if TAG not in tags:
        problems.append(f"it has no {TAG} tag")
    if problems:
        print("NOT SHOWN PRIVATE: " + "; ".join(problems), file=sys.stderr)
        return 1
    print(f"{REFERENCE} is private: only its owner, and what they give access, can get it.")
    return 0


def command_secret(_args: argparse.Namespace) -> int:
    text = os.environ.get(KEY_NAME) or env_file_values().get(KEY_NAME)
    key()  # one this tool made
    subprocess.run(["gh", "secret", "set", KEY_NAME, "--repo", REPOSITORY], input=text, text=True, check=True,
                   stdout=subprocess.DEVNULL)
    print(f"{KEY_NAME} is now the Actions secret of {REPOSITORY} (GitHub never shows it back).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("key").set_defaults(run=command_key)
    for name, run, *paths in (("seal", command_seal, "backup", "sealed"), ("verify", command_verify, "sealed", "backup"),
                              ("unseal", command_unseal, "sealed", "out"), ("upload", command_upload, "sealed"),
                              ("fetch", command_fetch, "sealed")):
        sub = commands.add_parser(name)
        for path in paths:
            sub.add_argument(path, type=Path)
        sub.set_defaults(run=run)
    commands.add_parser("restore").set_defaults(run=command_restore)
    commands.add_parser("check-private").set_defaults(run=command_check_private)
    commands.add_parser("secret").set_defaults(run=command_secret)
    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except SealError as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
