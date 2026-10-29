#!/usr/bin/env python3
"""Keep secrets and real identifiers out of Git: commits, their messages, pushes.

    python3 tools/repo/leak_scan.py staged         what is staged (the pre-commit hook)
    python3 tools/repo/leak_scan.py message FILE   a commit message (the commit-msg hook)
    python3 tools/repo/leak_scan.py push           what a push sends (the pre-push hook, from its stdin)
    python3 tools/repo/leak_scan.py range A..B     every commit in a range: its patch and its message
    python3 tools/repo/leak_scan.py tree           every tracked file as it stands
    python3 tools/repo/leak_scan.py install        this clone's hooks from .githooks/

Two kinds of finding:

- **This computer's own**, by value: each value in .env; the eWeLink account
  (captures/settings.json) and each plug's record (captures/plugs/*/plug.json);
  each backed-up plug's MAC, in any form, and the Sonoff settings in its backup
  (its eWeLink keys, the Wi-Fi it was set up on); the addresses real runs - not
  the emulator's - found plugs and this computer at; and each line of
  .leak-scan.local, which Git ignores: more of your own (a router, a name).
- **Anyone's**, by their shape: a MAC address that could be a real device's -
  not locally administered, and not one of the made-up ones .leak-scan-allow
  lists; a UUID that could be a device's apikey or id (same allow-list); a
  private key; a GitHub, AWS, Google or Slack token; disassembly or decompiler
  output (the S61 research tools make it - commit the tool, not its dump); and
  a packet capture (.pcap/.pcapng), whose frames carry a network's credentials.

An arbitrary secret with no safe shape - a device_secret, an app-apikey, a
secureCertSha256 - cannot be found by shape without flagging the repository's
own hashes, so list its value in .leak-scan.local (Git-ignored) to have it
found by value, as with anything else of yours.

Nothing found is printed: only where, and what kind. A finding stops the
commit or push; `--no-verify` lets one through that is meant to be there
(docs/technical-details.md#keeping-secrets-out-of-git).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

ALLOW_FILE = ROOT / ".leak-scan-allow"
LOCAL_FILE = ROOT / ".leak-scan.local"
CAPTURES = ROOT / "captures"
# The emulator's own network and the computer itself: not anyone's real address.
EMULATOR_NETWORKS = [ipaddress.ip_network(n) for n in ("192.168.60.0/24", "127.0.0.0/8", "0.0.0.0/32")]
SHORTEST = 4  # values shorter than this match too much to mean anything

MAC_TEXT = re.compile(r"(?<![0-9A-Fa-f:-])([0-9A-Fa-f]{2})([:-])([0-9A-Fa-f]{2})(?:\2[0-9A-Fa-f]{2}){4}(?![0-9A-Fa-f:-])")
TOKENS = {
    "a private key": re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"),
    "a GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})"),
    "an AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "a Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "a Slack token": re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}"),
}
# A UUID that could be a device's apikey or id; and the two shapes the S61
# research tools print, so a pasted dump of either is caught. disasm.py lines
# are "0xADDR:  BYTES  mnemonic"; decomp.py banners are "### FUNCTION 0x.. ###".
UUID_TEXT = re.compile(r"(?<![0-9A-Za-z-])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                       r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![0-9A-Za-z-])")
DISASM_LINE = re.compile(r"\b0x[0-9a-f]{4,8}:\s+[0-9a-f]{4,8}\s+[a-z]")
DECOMP_BANNER = re.compile(r"###\s+FUNCTION\s+0x[0-9a-f]{4,8}\s+###")
# File names never committed: a packet capture holds a network's frames, whose
# lengths carry the Wi-Fi credentials of whatever network it was taken on.
FORBIDDEN_SUFFIX = {
    ".pcap": "a packet capture (its frames carry a network's Wi-Fi credentials)",
    ".pcapng": "a packet capture (its frames carry a network's Wi-Fi credentials)",
}


@dataclass(frozen=True)
class Finding:
    where: str
    what: str


# -- what this computer knows of its own ----------------------------------------

def allowed() -> set[str]:
    if not ALLOW_FILE.exists():
        return set()
    return {line.strip().lower() for line in ALLOW_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")}


def mac_forms(mac: str) -> set[str]:
    hexes = mac.lower().replace(":", "").replace("-", "")
    pairs = [hexes[i:i + 2] for i in range(0, 12, 2)]
    return {":".join(pairs), "-".join(pairs), hexes, hexes[6:]}


def own_values() -> dict[str, str]:
    """Each value of this computer's own worth finding, and what kind it is."""
    found: dict[str, str] = {}

    def add(value: object, what: str) -> None:
        text = str(value or "").strip()
        if len(text) >= SHORTEST:
            found.setdefault(text, what)

    try:
        from tools.autoflash import credentials
        env = credentials.parse(credentials.ENV_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        env = {}
    for key, value in env.items():
        add(value, f"a value in .env ({key})")
    try:
        add(json.loads((CAPTURES / "settings.json").read_text())["ewelink_email"], "the eWeLink account")
    except (OSError, ValueError, KeyError):
        pass
    dumps: dict[str, bytes] = {}
    for plug in sorted((CAPTURES / "plugs").glob("*")):
        if re.fullmatch(r"([0-9a-f]{2}-){5}[0-9a-f]{2}", plug.name):  # each plug's folder is its MAC
            for form in mac_forms(plug.name):
                add(form, "a plug's MAC")
        try:
            record = json.loads((plug / "plug.json").read_text())
        except (OSError, ValueError):
            record = {}
        for field in ("ewelink_id", "ewelink_name"):
            add(record.get(field), f"a plug's eWeLink {field.split('_')[1]}")
        for field in ("mac", "test_copy_of"):
            if record.get(field):
                for form in mac_forms(record[field]):
                    add(form, "a plug's MAC")
        backup = plug / "sonoff-backup"
        try:
            from tools.autoflash.plugs import mac_from_efuse
            for form in mac_forms(mac_from_efuse((backup / "efuse.bin").read_bytes())):
                add(form, "a plug's MAC")
            flash = (backup / "flash.bin").read_bytes()
            dumps[hashlib.sha256(flash).hexdigest()] = flash
        except (OSError, ValueError, ImportError):
            pass
        for state in plug.glob("runs/*/autoflash-state.json"):
            try:
                run = json.loads(state.read_text())
            except (OSError, ValueError):
                continue
            for key, value in {**run.get("inputs", {}), **run.get("captures", {})}.items():
                if key.endswith(("plug_ip", "listen_ip")) and isinstance(value, str):
                    try:
                        address = ipaddress.ip_address(value)
                    except ValueError:
                        continue
                    if not any(address in network for network in EMULATOR_NETWORKS):
                        add(value, "an address a real run found")
    try:
        from tools.autoflash.sonoff_secrets import sonoff_values
        for flash in dumps.values():
            for value, where in sonoff_values(flash).items():
                if value.isascii() and value.decode("ascii", "ignore").isprintable():
                    add(value.decode("ascii"), f"a value of a Sonoff backup's settings ({where.split(' ')[0]})")
    except ImportError:
        pass
    if LOCAL_FILE.exists():
        for line in LOCAL_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                add(line.strip(), "a value in .leak-scan.local")
    allow = allowed()
    return {value: what for value, what in found.items() if value.lower() not in allow}


def needle_pattern(value: str) -> re.Pattern[str]:
    """A value, found as a whole: not inside a longer word or hex number."""
    edge = r"[0-9A-Za-z]" if value[:1].isalnum() else ""
    tail = r"[0-9A-Za-z]" if value[-1:].isalnum() else ""
    return re.compile((f"(?<!{edge})" if edge else "") + re.escape(value) + (f"(?!{tail})" if tail else ""),
                      re.IGNORECASE)


# -- looking --------------------------------------------------------------------

def own_bytes(values: dict[str, str]) -> list[tuple[str, bytes]]:
    """Each plug's MAC as a binary file holds it: six bytes, either way round."""
    raw = []
    for value, what in values.items():
        if what == "a plug's MAC" and re.fullmatch(r"[0-9a-f]{12}", value):
            raw += [(what, bytes.fromhex(value)), (what, bytes.fromhex(value)[::-1])]
    return raw


class Scanner:
    def __init__(self) -> None:
        values = own_values()
        self.own = [(needle_pattern(value), what, value.encode()) for value, what in values.items()]
        self.own_raw = own_bytes(values)
        self.allow = allowed()
        self.findings: list[Finding] = []

    def text(self, where: str, text: str) -> None:
        for pattern, what, _ in self.own:
            if pattern.search(text):
                self.findings.append(Finding(where, what))
        for match in MAC_TEXT.finditer(text):
            mac = match.group(0).lower().replace("-", ":")
            first = int(match.group(1), 16)
            if first & 0x02 or first & 0x01 or mac in self.allow or mac.replace(":", "-") in self.allow:
                continue  # locally administered or multicast: no device's own; or a known example
            self.findings.append(Finding(where, "a MAC address that could be a real device's "
                                                "(use 12:34:56:ab:cd:ef, or list it in .leak-scan-allow)"))
        for what, pattern in TOKENS.items():
            if pattern.search(text):
                self.findings.append(Finding(where, what))
        for match in UUID_TEXT.finditer(text):
            if match.group(0).lower() in self.allow:
                continue  # a known example: a fixed public constant, or one listed
            self.findings.append(Finding(where, "a UUID that could be a device's apikey or id "
                                                "(list a made-up example in .leak-scan-allow)"))
        if DISASM_LINE.search(text) or DECOMP_BANNER.search(text):
            self.findings.append(Finding(where, "disassembly or decompiler output "
                                                "(commit the tool that makes it, not its dump)"))

    def filename(self, where: str, path: str) -> None:
        """A file whose kind must never be committed, by its name."""
        what = FORBIDDEN_SUFFIX.get(Path(path).suffix.lower())
        if what:
            self.findings.append(Finding(where, what))

    def data(self, where: str, data: bytes) -> None:
        """A binary file: this computer's own values, as bytes."""
        for _, what, raw in self.own:
            if raw in data:
                self.findings.append(Finding(where, what))
        self.raw(where, data)

    def raw(self, where: str, data: bytes) -> None:
        """Any file, text or not: a plug's MAC as six bytes."""
        for what, raw in self.own_raw:
            if raw in data:
                self.findings.append(Finding(where, f"{what}, as bytes"))

    def report(self) -> int:
        unique = sorted(set(self.findings), key=lambda f: (f.where, f.what))
        for finding in unique:
            print(f"  {finding.where}: {finding.what}", file=sys.stderr)
        if unique:
            print(f"\nleak_scan: {len(unique)} finding(s) - nothing was committed or pushed. Take it out, "
                  "or, if it is meant to be there, --no-verify lets it through.", file=sys.stderr)
            return 1
        return 0


def git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True).stdout


def scan_patch(scanner: Scanner, patch: str, label: str) -> None:
    """The lines a patch adds, each by its file and line."""
    path, line = "?", 0
    for row in patch.splitlines():
        if row.startswith("+++ "):
            path = row[6:] if row.startswith("+++ b/") else row[4:]
        elif row.startswith("@@"):
            match = re.search(r"\+(\d+)", row)
            line = int(match.group(1)) if match else 0
        elif row.startswith("+"):
            scanner.text(f"{label}{path}:{line}", row[1:])
            line += 1
        elif not row.startswith("-"):
            line += 1


def binary_paths(numstat: str) -> list[str]:
    return [row.split("\t", 2)[2] for row in numstat.splitlines() if row.startswith("-\t-\t")]


def text_paths(numstat: str) -> list[str]:
    return [row.split("\t", 2)[2] for row in numstat.splitlines() if row and not row.startswith("-\t-\t")]


def scan_commit(scanner: Scanner, commit: str) -> None:
    short = commit[:8]
    scanner.text(f"{short} message", git("log", "-1", "--format=%B", commit).decode("utf-8", "replace"))
    scan_patch(scanner, git("show", "--format=", "-U0", "--no-color", commit).decode("utf-8", "replace"),
               f"{short} ")
    numstat = git("show", "--format=", "--numstat", commit).decode("utf-8", "replace")
    for path in binary_paths(numstat) + text_paths(numstat):
        try:
            blob = git("show", f"{commit}:{path}")
        except subprocess.CalledProcessError:
            continue  # deleted
        scanner.filename(f"{short} {path}", path)
        (scanner.data if path in binary_paths(numstat) else scanner.raw)(f"{short} {path}", blob)


def command_staged(scanner: Scanner) -> None:
    scan_patch(scanner, git("diff", "--cached", "-U0", "--no-color").decode("utf-8", "replace"), "")
    numstat = git("diff", "--cached", "--numstat").decode("utf-8", "replace")
    binary = binary_paths(numstat)
    for path in binary + text_paths(numstat):
        try:
            blob = git("show", f":{path}")
        except subprocess.CalledProcessError:
            continue  # deleted
        scanner.filename(path, path)
        (scanner.data if path in binary else scanner.raw)(path, blob)


def command_push(scanner: Scanner, lines: list[str]) -> None:
    zero = "0" * 40
    for line in lines:
        parts = line.split()
        if len(parts) != 4 or parts[1] == zero:
            continue  # a deletion sends nothing
        local, remote = parts[1], parts[3]
        excluded = [f"^{remote}"] if remote != zero else ["--not", "--remotes"]
        for commit in git("rev-list", local, *excluded).decode().split():
            scan_commit(scanner, commit)


def command_tree(scanner: Scanner) -> None:
    for path in git("ls-files", "-z").decode().split("\0"):
        if not path or path == ALLOW_FILE.name:
            continue
        scanner.filename(path, path)
        try:
            data = (ROOT / path).read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            scanner.data(path, data)
        else:
            for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                scanner.text(f"{path}:{number}", line)
            scanner.raw(path, data)


def command_install() -> int:
    git("config", "core.hooksPath", ".githooks")
    for hook in (ROOT / ".githooks").iterdir():
        hook.chmod(0o755)
    print("This clone's hooks are .githooks/ now: each commit, its message and each push are scanned,\n"
          "and each commit linted (tools/repo/lint.py) by the linters in .venv:\n"
          "    .venv/bin/python -m pip install --require-hashes -r requirements/lint.txt")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] not in ("staged", "message", "push", "range", "tree", "install"):
        print(__doc__, file=sys.stderr)
        return 2
    if args[0] == "install":
        return command_install()
    scanner = Scanner()
    if args[0] == "staged":
        command_staged(scanner)
    elif args[0] == "message":
        text = Path(args[1]).read_text(encoding="utf-8", errors="replace")
        scanner.text("the commit message", "\n".join(line for line in text.splitlines() if not line.startswith("#")))
    elif args[0] == "push":
        command_push(scanner, sys.stdin.read().splitlines())
    elif args[0] == "range":
        for commit in git("rev-list", args[1]).decode().split():
            scan_commit(scanner, commit)
    else:
        command_tree(scanner)
    return scanner.report()


if __name__ == "__main__":
    raise SystemExit(main())
