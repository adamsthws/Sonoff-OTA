"""What the plug's networks are called, firmware by firmware: the interim
names the tool's own firmware goes by, the final ones, and the names older
releases used.  docs/device-names.md says why each is as it is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tools.autoflash import hostsys, kinds

if TYPE_CHECKING:
    from tools.autoflash.context import Context

# Every firmware a plug runs part way through a conversion - the installer,
# the mover, the startup firmware, a Tasmota not yet set up - goes by this and
# the last two bytes of the plug's Wi-Fi MAC (autoflash-recovery-CDEF): on the
# home network, and for the open network the installer and the mover raise
# when they cannot join that (the others, given the Wi-Fi, only retry it).
SETUP_SSID_PREFIX = "autoflash-recovery-"
# The networks of the same firmware before 4.4.4 (the installer) and 1.1.0
# (the mover), for a plug a run of an older checkout left on one; and the
# installer's before 4.4.3, with a fixed password.
LEGACY_SETUP_PREFIX = "Sonoff-Setup-"
LEGACY_BRIDGE_PREFIX = "S60-OTA-Bridge"
LEGACY_BRIDGE_PASSWORD = "s60-ota-bridge"
LEGACY_MOVER_PREFIX = "sonoff-s60-mover-"
TASMOTA_AP_PREFIX = "tasmota-"
# The final ESPHome's own name, and its network's (devices/sonoff-s60/esphome.yaml).
ESPHOME_PREFIX = "esphome-"


def _mac_tail(mac: str, add: int = 0) -> str:
    """The last three bytes of `mac` (plus `add`), as six hex digits."""
    value = int(hostsys.normalise_mac(mac).replace(":", ""), 16) + add
    return f"{value & 0xFFFFFF:06X}"


def final_name(ctx: "Context", firmware: str) -> str | None:
    """The name official Tasmota or ESPHome has once converted, for every kind
    of plug: the firmware's and the last three bytes of the plug's MAC
    (tasmota-abcdef, esphome-abcdef) - its own network's too.  None when the
    MAC is not known."""
    mac = ctx.state.input("plug_mac")
    return f"{firmware}-{_mac_tail(mac).lower()}" if mac else None


def setup_networks(ctx: "Context") -> tuple[str, ...]:
    """What a Tasmota's own setup network on this plug can be called, to look for.

    Tasmota raises one only without Wi-Fi details, named after itself:
    tasmota-abcdef once set up (F4 names it), tasmota-ABCDEF-1234 when it
    started from its defaults - Tasmota's own name, from the plug's MAC.  One
    the run named autoflash-recovery-CDEF has the details, and only retries
    them - and that name is the installer's network's, not looked for here:
    still up for a moment as the installer restarts into Tasmota, it was
    taken for Tasmota's (seen in the fault suite).  Tasmota has also been seen
    broadcasting ESP-IDF's default name instead: ESP_ and the MAC of its
    hotspot, one more than the plug's (ESP_ABCDF0).  Knowing the MAC, only
    this plug's are matched, never a neighbour's Tasmota; without it, any
    Tasmota's.
    """
    mac = ctx.state.input("plug_mac")
    if not mac:
        return (TASMOTA_AP_PREFIX,)
    return (final_name(ctx, "tasmota"), f"{TASMOTA_AP_PREFIX}{_mac_tail(mac)}-", f"ESP_{_mac_tail(mac, 1)}")


def setup_network_hint(ctx: "Context") -> str:
    """The setup network's name as a person should look for it in a list."""
    mac = ctx.state.input("plug_mac")
    if not mac:
        return "tasmota-xxxxxx or tasmota-XXXXXX-NNNN"
    return f"{final_name(ctx, 'tasmota')}, tasmota-{_mac_tail(mac)}-NNNN, or ESP_{_mac_tail(mac, 1)}"


def note_setup_network(ctx: "Context", network: str) -> None:
    """Keep the plug's name, from its setup network - only when that is
    Tasmota's own name for it, which its router shows too, not ESP-IDF's."""
    if network.startswith(TASMOTA_AP_PREFIX):
        ctx.state.set_input("device_name", network)


def named(ctx: "Context", prefix: str) -> str:
    """`prefix` and the last two bytes of the plug's Wi-Fi MAC, as this
    project's firmware names itself (XXXX when the MAC is not known)."""
    mac = ctx.state.input("plug_mac")
    return f"{prefix}{_mac_tail(mac)[-4:]}" if mac else f"{prefix}XXXX"


def our_setup_network(ctx: "Context") -> str:
    """This plug's name, and its network's, while it runs firmware of this
    project part way through a conversion (autoflash-recovery-CDEF)."""
    return named(ctx, SETUP_SSID_PREFIX)


def our_setup_networks(ctx: "Context", *legacy: str) -> tuple[str, ...]:
    """To look for: this plug's network (any plug's, its MAC not known), or
    those of `legacy`, what the same firmware raised before."""
    mac = ctx.state.input("plug_mac")
    return (our_setup_network(ctx) if mac else SETUP_SSID_PREFIX, *legacy)


def legacy_setup_network(ctx: "Context") -> str:
    """What the installer and the mover raised before 4.4.4 and 1.1.0."""
    return named(ctx, LEGACY_SETUP_PREFIX) if ctx.state.input("plug_mac") else LEGACY_SETUP_PREFIX


def bridge_networks(ctx: "Context") -> tuple[str, ...]:
    """The installer's own network, by every name it has had."""
    return our_setup_networks(ctx, legacy_setup_network(ctx), LEGACY_BRIDGE_PREFIX)


def mover_networks(ctx: "Context") -> tuple[str, ...]:
    """The mover's own network, by every name it has had."""
    return our_setup_networks(ctx, legacy_setup_network(ctx), LEGACY_MOVER_PREFIX)


def esphome_networks(ctx: "Context") -> tuple[str, ...]:
    """ESPHome's own network, once installed - its name (esphome-abcdef) -
    and the names an earlier build of this kind of plug's gave it."""
    kind = kinds.of_run(ctx.state)
    mac = ctx.state.input("plug_mac")
    return (final_name(ctx, "esphome") or ESPHOME_PREFIX,
            named(ctx, kind.esphome_legacy_setup_prefix) if mac else kind.esphome_legacy_setup_prefix,
            kind.esphome_legacy_prefix)


def mover_network_hint(ctx: "Context") -> str:
    """The mover's own network."""
    return our_setup_network(ctx)


def esphome_network_hint(ctx: "Context") -> str:
    """ESPHome's own network."""
    return final_name(ctx, "esphome") or f"{ESPHOME_PREFIX}xxxxxx"
