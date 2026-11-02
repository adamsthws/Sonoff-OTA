"""The kinds of plug the tool converts, and what is each one's own.

What differs from one kind of plug to the next is kept here: how eWeLink and
its stock firmware name it, the model its maker's updater wants a file
wrapped for, its wiring for Tasmota, what its ESPHome calls itself, and what
a person calls it.  The firmware published for a kind is pinned where it is
used, each file named for the kind it is for: its installer
(tools.autoflash.routes.espressif.installer.BRIDGE_OTA, wrapped for its updater), its ESPHome and the
bootloader built with it (esphome.IMAGE, esphome.BOOTLOADER), and its maker's
partition table (esphome.SONOFF_TABLE).  The final firmware's names are the
same for every kind - tasmota-abcdef, esphome-abcdef, openbeken-abcdef - so the
kind is told by what else each firmware says, which renaming the plug
leaves alone: Tasmota's template, ESPHome's comment, and the answer
OpenBeken's import module gives (openbeken.kind).

A run keeps the kind it converts (its "plug_kind"): from the eWeLink account
(A1), from what the stock firmware says it is (A2), or from a converted
Tasmota's template.  The kind names its chip family, which is whose steps the
run takes (tools/autoflash/routes).  Official Tasmota's template is named after the kind,
and ESPHome's comment by the image made for it; every firmware part way
through a conversion is the tool's own, the same for every kind.

Two kinds: the Sonoff S60, an Espressif plug, and the Sonoff S61, a Beken one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True, eq=False)
class Kind:
    """A kind of plug the tool converts."""

    # In file names (autoflash-bridge-sonoff-s60-4.4.4.ota) and a run's record.
    key: str
    # Its chip family (routes.FAMILIES: how firmware gets onto its flash), its
    # chip, and the ecosystem its stock firmware belongs to - how it is paired,
    # found and sent its first new firmware.  Only the first is code of its
    # own so far: every kind runs eWeLink's firmware.
    family: str
    chip: str
    ecosystem: str
    # What a person calls it, its maker, and its name for short in running text.
    name: str
    maker: str
    short: str
    # eWeLink's product model for one, as the account gives it.
    product: re.Pattern[str]
    # What its stock firmware calls itself, to eWeLink and to the updater.
    stock_model: str
    # The model a file must be wrapped for, for the stock updater to take it.
    wrapper_model: str
    # Its wiring, as a Tasmota template gives it; none for a kind Tasmota does not run on.
    tasmota_gpio: tuple[int, ...]
    # The names its own network had in earlier ESPHome builds: the first with
    # the last two bytes of the plug's MAC.
    esphome_legacy_setup_prefix: str
    esphome_legacy_prefix: str
    # Its ESPHome's friendly name in releases before its comment said the kind.
    esphome_legacy_friendly_name: str = ""
    # For a Beken plug: the command its OpenBeken (built with the plug's import
    # module) takes to fetch and apply Sonoff's firmware again, checking the
    # slot it goes to first; "" for a kind that has none.
    openbeken_revert: str = ""
    # Whether its conversion uses eWeLink's LAN API, so the plug must answer
    # eWeLink's discovery on the network (LAN Control on in the app). The S61's
    # goes through eWeLink alone, and the account's record of its hardware
    # address finds it on the network.
    uses_lan_control: bool = True

    def product_of(self, product_model: str | None) -> str:
        """`product_model` as eWeLink gives it, if it is one of this kind's; else ""."""
        product = (product_model or "").strip().upper()
        return product if self.product.fullmatch(product) else ""

    @property
    def display_name(self) -> str:
        """What the firmware the tool installs says the plug is (Sonoff-S60):
        Tasmota's template and ESPHome's comment.  One for every model of the
        kind, so one image serves them all."""
        return f"{self.maker}-{self.short}"

    def tasmota_template(self) -> str:
        """Its Tasmota template, as the Template command takes it."""
        return json.dumps({"NAME": self.display_name, "GPIO": list(self.tasmota_gpio),
                           "FLAG": 0, "BASE": 1}, separators=(",", ":"))

    def names_its_tasmota(self, template_name: str | None) -> bool:
        """Whether a Tasmota's template, by its name, is the one the tool gives
        this kind (Sonoff-S60) - or one an earlier release gave: the kind's
        name, or its maker and product model (Sonoff S60, Sonoff S60TPG)."""
        name = (template_name or "").strip().upper()
        if not self.tasmota_gpio or not name:
            return False
        maker, _, product = name.partition(" ")
        return (name in (self.display_name.upper(), self.name.upper())
                or (maker == self.maker.upper() and bool(self.product_of(product))))

    def names_its_esphome(self, title: str | None, comment: str | None) -> bool:
        """Whether an ESPHome, by its page's comment and title, is the one the
        tool installs on this kind: its comment is the kind's
        (devices/<kind>/esphome.yaml: Sonoff-S61), whatever the plug was
        renamed - or, from a release before the comment, its friendly name
        is, alone or with the MAC suffix its page shows (Sonoff S60 abcdef)."""
        if (comment or "").strip().lower() == self.display_name.lower():
            return True
        return bool(self.esphome_legacy_friendly_name) and re.fullmatch(
            re.escape(self.esphome_legacy_friendly_name.lower()) + r"( [0-9a-f]{6})?",
            (title or "").strip().lower()) is not None


SONOFF_S60 = Kind(
    key="sonoff-s60",
    family="espressif",
    chip="esp32c3",
    ecosystem="ewelink",
    name="Sonoff S60",
    maker="Sonoff",
    short="S60",
    # S60TP and the plug-type letter: the UK's S60TPG, Europe's S60TPF.
    product=re.compile(r"S60TP[A-Z]"),
    stock_model="SN-ESP32C3-S60-01",
    wrapper_model="FWSW-E32S60-S60-ESP32C3FN4",
    # Relay GPIO4, status LED GPIO5, power meter GPIO7, button GPIO9.  The UK
    # S60TPG and the European S60TPF publish the same pins.
    tasmota_gpio=(1, 1, 1, 1, 224, 544, 1, 3104, 1, 32, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1),
    # devices/sonoff-s60/esphome.yaml. 1.1.0 raised Sonoff-Setup-CDEF; builds before
    # that sonoff-s60- and the MAC's last three bytes.
    esphome_legacy_setup_prefix="Sonoff-Setup-",
    esphome_legacy_prefix="sonoff-s60-",
    esphome_legacy_friendly_name="Sonoff S60",
)

SONOFF_S61 = Kind(
    key="sonoff-s61",
    family="beken",
    chip="bk7238",
    ecosystem="ewelink",
    name="Sonoff S61",
    maker="Sonoff",
    short="S61",
    # The S61S (iPlug Gen2): S61STP and the plug-type letter, Europe's S61STPF.
    product=re.compile(r"S61STP[A-Z]"),
    stock_model="SN-BK7238-S61-01",
    # The descriptor name in the FOTA container its stock updater stages (tools/beken/fota_wrap.py).
    wrapper_model="FWSW-01-S61-BK7238",
    tasmota_gpio=(),  # Tasmota does not run on the BK7238: OpenBeken does
    # devices/sonoff-s61/esphome.yaml; no earlier builds, so no older names.
    esphome_legacy_setup_prefix="",
    esphome_legacy_prefix="",
    openbeken_revert="S61RevertToStock",
    uses_lan_control=False,
)

# Every kind the tool converts.
ALL = (SONOFF_S60, SONOFF_S61)


def by_key(key: str | None) -> Kind | None:
    return next((kind for kind in ALL if kind.key == key), None)


def for_account(product_model: str | None, stock_model: str | None) -> Kind | None:
    """The kind of a device in an eWeLink account, by its product model or by
    what its firmware calls itself (the account's extra.model); None for one
    the tool does not convert."""
    return next((kind for kind in ALL
                 if kind.product_of(product_model) or stock_model == kind.stock_model), None)


def for_stock_model(stock_model: str | None) -> Kind | None:
    """The kind whose stock firmware calls itself `stock_model`."""
    return next((kind for kind in ALL if stock_model == kind.stock_model), None)


def for_tasmota(template_name: str | None) -> Kind | None:
    """The kind of a converted Tasmota, by its template's name."""
    return next((kind for kind in ALL if kind.names_its_tasmota(template_name)), None)


def on_chip(chipset: str | None) -> tuple[Kind, ...]:
    """The kinds an OpenBeken on this chip (as /api/info names it: BK7238)
    could be on: the ones to ask it about (openbeken.kind)."""
    chip = (chipset or "").strip().lower()
    return tuple(kind for kind in ALL if kind.family == "beken" and kind.chip == chip)


def for_esphome(title: str | None, comment: str | None) -> Kind | None:
    """The kind of a converted ESPHome, by its page's title and comment."""
    return next((kind for kind in ALL if kind.names_its_esphome(title, comment)), None)


def names(attribute: str = "name") -> str:
    """Every kind, as a person calls them (Sonoff S60): for before the plug is known."""
    return " or ".join(getattr(kind, attribute) for kind in ALL)


def name_words() -> set[str]:
    """The words of every kind's name, in lower case: an eWeLink name of only
    these (and its product model) is the one eWeLink gave, which names nobody."""
    return {word for kind in ALL for word in kind.name.lower().split()}


def of_run(state) -> Kind:
    """The kind a run converts (`state`: its RunState): as it recorded once
    known, else the first the tool knows - the only one, so far."""
    return by_key(state.input("plug_kind")) or ALL[0]
