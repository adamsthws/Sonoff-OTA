"""What this computer can do, on Linux, macOS or Windows.

Everything that shells out to an operating-system tool lives here, so the steps
never need to know which system they are on. Each function is best effort and
says so: a scan that finds nothing means "do not know", never "not there".

Commands that need administrator rights go through `elevated()`, which uses the
system's own prompt - sudo on Linux and macOS, the Windows elevation dialog -
and never sees or stores a password itself.
"""

from __future__ import annotations

import concurrent.futures
import getpass
import hashlib
import ipaddress
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from . import output

SYSTEM = platform.system()  # "Linux", "Darwin" or "Windows"
IS_WINDOWS = SYSTEM == "Windows"
IS_MAC = SYSTEM == "Darwin"
IS_LINUX = SYSTEM == "Linux"

MAC_RE = re.compile(r"([0-9a-fA-F]{1,2}[:-]){5}[0-9a-fA-F]{1,2}")

Runner = Callable[..., subprocess.CompletedProcess]


def system_name() -> str:
    return {"Darwin": "macOS"}.get(SYSTEM, SYSTEM)


def _run(command: Sequence[str], timeout: float = 15.0) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            list(command), capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None


def normalise_mac(text: str) -> str:
    """aa:bb:cc:dd:ee:ff, whatever separators and padding the system used."""
    parts = re.split(r"[:-]", text.strip())
    return ":".join(part.zfill(2).lower() for part in parts)


# -- reachability ---------------------------------------------------------------


def ping_command(address: str, timeout: int = 2) -> list[str]:
    if IS_WINDOWS:
        return ["ping", "-n", "1", "-w", str(timeout * 1000), address]
    if IS_MAC:
        return ["ping", "-c", "1", "-t", str(timeout), address]
    return ["ping", "-c", "1", "-W", str(timeout), address]


def ping_ok(address: str, timeout: int = 2) -> bool:
    completed = _run(ping_command(address, timeout), timeout=timeout + 3)
    if completed is None or completed.returncode != 0:
        return False
    # Windows ping exits 0 for "Destination host unreachable" replies from the
    # local stack, so look for an actual echo reply.
    return not IS_WINDOWS or "TTL=" in completed.stdout.upper()


def local_ip_towards(target: str) -> str:
    """The address this machine would use to reach `target` (sends nothing)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((target, 9))
        return probe.getsockname()[0]
    finally:
        probe.close()


def port_is_free(address: str, port: int) -> bool:
    """Whether a TCP server could listen on `address`:`port` right now.

    Binds the way http.server does (SO_REUSEADDR), so a connection still in
    TIME_WAIT from an earlier run does not count as in use; a listener does.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((address, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def first_free_port(address: str, candidates: Sequence[int]) -> int | None:
    return next((port for port in candidates if port_is_free(address, port)), None)


def local_addresses() -> set[str]:
    """IPv4 addresses assigned to this machine, as far as can be told."""
    found: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    if IS_LINUX:
        completed = _run(["ip", "-4", "-o", "address"])
        if completed and completed.returncode == 0:
            found.update(re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/", completed.stdout))
    elif IS_MAC:
        completed = _run(["ifconfig"])
        if completed and completed.returncode == 0:
            found.update(re.findall(r"inet (\d+\.\d+\.\d+\.\d+) ", completed.stdout))
    elif IS_WINDOWS:
        completed = _run(["ipconfig"])
        if completed and completed.returncode == 0:
            found.update(re.findall(r"IPv4[^:]*:\s*(\d+\.\d+\.\d+\.\d+)", completed.stdout))
    return found


def interface_towards(target: str) -> str | None:
    """The name of the network connection that reaches `target`, if knowable."""
    if IS_LINUX:
        completed = _run(["ip", "route", "get", target])
        if completed and completed.returncode == 0:
            found = re.search(r"\bdev (\S+)", completed.stdout)
            return found.group(1) if found else None
    elif IS_MAC:
        completed = _run(["route", "-n", "get", target])
        if completed and completed.returncode == 0:
            found = re.search(r"interface:\s*(\S+)", completed.stdout)
            return found.group(1) if found else None
    elif IS_WINDOWS:
        completed = _run([
            "powershell", "-NoProfile", "-Command",
            f"(Find-NetRoute -RemoteIPAddress {target} | Select-Object -First 1).InterfaceAlias",
        ])
        if completed and completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout.strip().splitlines()[0]
    return None


# -- finding a device by its hardware address ------------------------------------


def neighbour_table() -> dict[str, str]:
    """{ip: mac} from this machine's ARP/neighbour cache."""
    if IS_LINUX:
        completed = _run(["ip", "neigh"])
        pattern = re.compile(r"^(\d+\.\d+\.\d+\.\d+) .*lladdr (\S+)", re.M)
    else:
        completed = _run(["arp", "-a"] if IS_WINDOWS else ["arp", "-an"])
        pattern = re.compile(
            r"(\d+\.\d+\.\d+\.\d+)\)?\s+(?:at\s+)?((?:[0-9a-fA-F]{1,2}[:-]){5}[0-9a-fA-F]{1,2})"
        )
    table: dict[str, str] = {}
    if completed and completed.returncode == 0:
        for ip, mac in pattern.findall(completed.stdout):
            table[ip] = normalise_mac(mac)
    return table


def mac_of(address: str) -> str | None:
    """The hardware address answering at `address`, after one ping."""
    ping_ok(address)
    return neighbour_table().get(address)


def sweep(subnet_of: str, timeout: int = 1) -> None:
    """Ping every address in `subnet_of`'s /24 once, to fill the neighbour cache."""
    network = ipaddress.ip_network(f"{subnet_of}/24", strict=False)
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        list(pool.map(lambda host: ping_ok(str(host), timeout), network.hosts()))


def find_by_mac(mac: str, subnet_of: str) -> str | None:
    """The address `mac` currently has on `subnet_of`'s network, if it is there."""
    wanted = normalise_mac(mac)
    for ip, seen in neighbour_table().items():
        if seen == wanted and ping_ok(ip):
            return ip
    sweep(subnet_of)
    for ip, seen in neighbour_table().items():
        if seen == wanted:
            return ip
    return None


# -- Wi-Fi ----------------------------------------------------------------------

MAC_AIRPORT = (
    "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/"
    "Resources/airport"
)


def wifi_ssids(timeout: float = 20.0) -> list[str]:
    """Names of Wi-Fi networks this machine can see.  Empty means "do not know"."""
    if IS_WINDOWS:
        completed = _run(["netsh", "wlan", "show", "networks"], timeout=timeout)
        if completed and completed.returncode == 0:
            return [name.strip() for name in re.findall(r"^SSID \d+ : (.*)$", completed.stdout, re.M)
                    if name.strip()]
        return []
    if IS_MAC:
        if Path(MAC_AIRPORT).exists():
            completed = _run([MAC_AIRPORT, "-s"], timeout=timeout)
            if completed and completed.returncode == 0:
                names = [line[:32].strip() for line in completed.stdout.splitlines()[1:]]
                if any(names):
                    return [name for name in names if name]
        completed = _run(["system_profiler", "SPAirPortDataType"], timeout=timeout + 20)
        if completed and completed.returncode == 0:
            # Network names are the indented headings ending in a colon.
            return [name.strip() for name in re.findall(r"^\s{12}(\S[^:\n]*):\s*$", completed.stdout, re.M)]
        return []
    for command in (
        ["nmcli", "-t", "-f", "SSID", "device", "wifi", "list", "--rescan", "auto"],
        ["nmcli", "-t", "-f", "SSID", "device", "wifi", "list"],
        ["iwlist", "scanning"],
    ):
        completed = _run(command, timeout=timeout)
        if completed is None or completed.returncode != 0:
            continue
        names: list[str] = []
        for line in completed.stdout.splitlines():
            line = line.strip()
            if command[0] == "iwlist":
                if "ESSID:" in line:
                    names.append(line.split("ESSID:", 1)[1].strip().strip('"'))
            elif line:
                names.append(line)
        if names:
            return names
    return []


def wifi_menu_hint() -> str:
    if IS_WINDOWS:
        return "the Wi-Fi icon in the taskbar"
    if IS_MAC:
        return "the Wi-Fi icon in the menu bar"
    return "your desktop's Wi-Fi menu"


# -- clipboard ------------------------------------------------------------------


def copy_to_clipboard(text: str) -> bool:
    """Put `text` on the clipboard.  False when no clipboard tool is available."""
    if IS_WINDOWS:
        candidates = [["clip"]]
    elif IS_MAC:
        candidates = [["pbcopy"]]
    else:
        candidates = [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    for command in candidates:
        if not shutil.which(command[0]):
            continue
        try:
            completed = subprocess.run(command, input=text, text=True, timeout=5,
                                       capture_output=True, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode == 0:
            return True
    return False


# -- files ----------------------------------------------------------------------


def replace_file(source: Path, target: Path) -> None:
    """os.replace, waiting out Windows' brief refusals.

    A virus scanner or indexer opening a file - one just written, or just
    renamed - makes Windows refuse to rename over it, or away from it, for a
    moment.  Elsewhere a refusal means what it says, at once.
    """
    for attempt in range(50):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if not IS_WINDOWS or attempt == 49:
                raise
            time.sleep(0.1)



def check_sha256sums(listing: Path) -> list[str]:
    """Verify a sha256sum-format file.  Returns the problems; empty means all good."""
    problems: list[str] = []
    for line in listing.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split(maxsplit=1)
        path = listing.parent / name.lstrip("*")
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            problems.append(f"{name}: {exc}")
            continue
        if actual != digest.lower():
            problems.append(f"{name}: checksum differs")
    return problems


# -- administrator commands -----------------------------------------------------


def elevated(command: Sequence[str]) -> int:
    """Run `command` with administrator rights, using the system's own prompt."""
    if IS_WINDOWS:
        program, *arguments = command
        # One command line, quoted the Windows way: given a list, Start-Process
        # joins it with spaces and no quotes, so "name=S60 autoflash 1.2.3.4"
        # would reach netsh as three arguments.
        line = subprocess.list2cmdline(arguments).replace("'", "''")
        # A declined or cancelled prompt makes Start-Process fail, leaving no
        # process - whose exit code would read as 0, success.
        script = (
            f"try {{ $p = Start-Process -FilePath '{program}' "
            + (f"-ArgumentList '{line}' " if line else "")
            + "-Verb RunAs -Wait -PassThru -WindowStyle Hidden -ErrorAction Stop } "
            "catch { Write-Output $_.Exception.Message; exit 1223 }; exit $p.ExitCode"
        )
        try:
            result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except OSError:
            return 127
        if result.stdout.strip():
            output.detail(result.stdout.rstrip("\n"))
        return result.returncode
    return elevated_output(command)[0]


# sudo's question, set rather than left to the system, so the screen knows how
# wide it is (%u is sudo's own placeholder for the user name).  Only used when
# no password_asker is set.
SUDO_PROMPT = "[sudo] password for %u: "

# Asks for this computer's password, in the tool's own words, until sudo
# accepts it (see check_sudo_password); returns it, or raises OSError.  Set by
# the tools that draw their own screen, so that every row on it is theirs: sudo
# asking by itself writes to the terminal directly, a row the screen cannot
# count - and a mistyped password adds more.
password_asker: Callable[[], str] | None = None


def sudo_will_ask() -> bool:
    """Whether sudo will ask for a password now, or has one remembered.  Asks nothing."""
    try:
        return subprocess.run(["sudo", "-n", "true"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0
    except OSError:
        return False


def check_sudo_password(password: str) -> str | None:
    """Try `password` with sudo, which then remembers it for a while.

    None when it was accepted.  A wrong one gives "wrong"; anything else - not
    allowed to use sudo at all, say - raises OSError with sudo's reason.  The
    password goes through a pipe, never a command line, and is not kept.
    """
    try:
        result = subprocess.run(["sudo", "-S", "-v", "-p", ""], input=password + "\n",
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError as exc:
        raise OSError(f"sudo could not be run: {exc}") from None
    if result.returncode == 0:
        return None
    reason = result.stderr.lower()
    if "incorrect password" in reason or "try again" in reason:
        return "wrong"
    lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    raise OSError(lines[0] if lines else f"sudo refused (exit {result.returncode})")


def elevated_output(command: Sequence[str]) -> tuple[int, str]:
    """Like `elevated`, also returning what the command printed.

    With a password_asker, sudo never writes to the terminal: the password,
    when one is needed, is asked for by the tool and given to sudo through a
    pipe for this one command.  Without one, sudo asks on the terminal itself
    and the output module is told, so its question is counted on screen.
    Windows has no capture here.
    """
    if IS_WINDOWS:
        return elevated(command), ""
    output.log_only("administrator rights requested: " + " ".join(command))
    asks = sudo_will_ask()
    if password_asker is not None:
        password = password_asker() if asks else None
        sudo = (["sudo", "-S", "-p", ""] if password is not None else ["sudo", "-n"])
        try:
            result = subprocess.run([*sudo, *command], input=(password + "\n") if password is not None else None,
                                    stdin=None if password is not None else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except OSError:
            return 127, ""
        finally:
            password = None
        if result.stdout:
            output.detail(result.stdout.rstrip("\n"))
        return result.returncode, result.stdout
    if asks:
        output.before_input(SUDO_PROMPT.replace("%u", getpass.getuser()))
    try:
        result = subprocess.run(["sudo", "-p", SUDO_PROMPT, *command], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
    except OSError:
        return 127, ""
    finally:
        if asks:
            output.after_input()
    if result.stdout:
        output.detail(result.stdout.rstrip("\n"))
    return result.returncode, result.stdout


# -- firewall -------------------------------------------------------------------

# What each firewall says when the rule was already there.  Such a rule belongs
# to someone else - perhaps kept on purpose for the next plug - so it stays.
ALREADY_PRESENT = ("skipping adding existing rule", "already_enabled")

RULE_NAME = "S60 autoflash"


@dataclass
class FirewallRule:
    """Enough to remove exactly what was added, and nothing else."""

    kind: str
    plug_ip: str
    ports: list[int]
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "FirewallRule":
        return cls(**data)


def active_firewall() -> str | None:
    """Which host firewall is filtering incoming connections, if one can be seen."""
    if IS_WINDOWS:
        completed = _run(["netsh", "advfirewall", "show", "allprofiles", "state"])
        if completed and re.search(r"State\s+ON", completed.stdout, re.I):
            return "windows"
        return None
    if IS_MAC:
        tool = "/usr/libexec/ApplicationFirewall/socketfilterfw"
        completed = _run([tool, "--getglobalstate"]) if Path(tool).exists() else None
        if completed and "enabled" in completed.stdout.lower():
            return "macos"
        return None
    completed = _run(["firewall-cmd", "--state"]) if shutil.which("firewall-cmd") else None
    if completed and completed.stdout.strip() == "running":
        return "firewalld"
    completed = _run(["systemctl", "is-active", "ufw"]) if shutil.which("systemctl") else None
    if completed and completed.stdout.strip() == "active":
        return "ufw"
    return None


def _ports(ports: Sequence[int]) -> str:
    return ",".join(str(port) for port in ports)


def firewall_commands(kind: str, plug_ip: str, ports: Sequence[int]) -> tuple[list[str], list[str]]:
    """(add, remove) commands for one rule.  Pure, so it can be shown and tested."""
    if kind == "ufw":
        spec = ["from", plug_ip, "to", "any", "port", _ports(ports), "proto", "tcp"]
        return (["ufw", "allow", *spec, "comment", RULE_NAME], ["ufw", "delete", "allow", *spec])
    if kind == "firewalld":
        low, high = min(ports), max(ports)
        rule = (f"rule family=ipv4 source address={plug_ip} port port={low}-{high} "
                "protocol=tcp accept")
        # Runtime only: gone at the next reboot even if cleanup never runs.
        return (["firewall-cmd", f"--add-rich-rule={rule}"],
                ["firewall-cmd", f"--remove-rich-rule={rule}"])
    if kind == "windows":
        name = f"{RULE_NAME} {plug_ip}"
        return (
            ["netsh", "advfirewall", "firewall", "add", "rule", f"name={name}", "dir=in",
             "action=allow", "protocol=TCP", f"localport={_ports(ports)}", f"remoteip={plug_ip}"],
            ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"],
        )
    if kind == "macos":
        tool = "/usr/libexec/ApplicationFirewall/socketfilterfw"
        # The macOS firewall allows programs, not ports.
        return ([tool, "--add", sys.executable, "--unblockapp", sys.executable],
                [tool, "--remove", sys.executable])
    raise ValueError(f"unknown firewall {kind!r}")


def display(command: Sequence[str]) -> str:
    prefix = "" if IS_WINDOWS else "sudo "
    return prefix + " ".join(
        part if re.fullmatch(r"[\w@%+=:,./-]+", part) else f"'{part}'" for part in command
    )


def mac_app_already_allowed() -> bool:
    completed = _run(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--listapps"])
    return bool(completed and sys.executable in completed.stdout)


def open_firewall(plug_ip: str, ports: Sequence[int]) -> FirewallRule | None:
    """Allow the plug in.  None when no firewall needs a rule; raises on failure."""
    kind = active_firewall()
    if kind is None:
        return None
    if kind == "macos" and mac_app_already_allowed():
        return FirewallRule(kind, plug_ip, list(ports), {"preexisting": True})
    add, _ = firewall_commands(kind, plug_ip, ports)
    code, output = elevated_output(add)
    if code != 0:
        raise OSError(f"could not add the firewall rule: {display(add)}")
    if any(phrase in output.lower() for phrase in ALREADY_PRESENT):
        return FirewallRule(kind, plug_ip, list(ports), {"preexisting": True})
    return FirewallRule(kind, plug_ip, list(ports))


def close_firewall(rule: FirewallRule) -> bool:
    """Remove exactly the rule that was added.  True when it is gone."""
    if rule.detail.get("preexisting"):
        return True  # it was allowed before this run; leave it as it was
    _, remove = firewall_commands(rule.kind, rule.plug_ip, rule.ports)
    return elevated(remove) == 0


# -- borrowing an address for the router test -----------------------------------


def alias_commands(interface: str, address: str) -> tuple[list[str], list[str]]:
    """(add, remove) for a temporary extra address on `interface`."""
    if IS_WINDOWS:
        return (["netsh", "interface", "ipv4", "add", "address", interface, address, "255.255.255.255"],
                ["netsh", "interface", "ipv4", "delete", "address", interface, address])
    if IS_MAC:
        return (["ifconfig", interface, "alias", address, "255.255.255.255"],
                ["ifconfig", interface, "-alias", address])
    return (["ip", "address", "add", f"{address}/32", "dev", interface],
            ["ip", "address", "delete", f"{address}/32", "dev", interface])
