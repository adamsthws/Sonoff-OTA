"""The kinds of plug the tool converts, and what is each one's own.

What differs from one kind of plug to the next is kept here: how eWeLink and
its stock firmware name it, the model its maker's updater wants a file
wrapped for, its wiring for Tasmota, what its ESPHome calls itself, and what
a person calls it.  The firmware published for a kind is pinned where it is
used, each file named for the kind it is for: its installer
(autoflash_cli.BRIDGE_OTA, wrapped for its updater), its ESPHome and the
bootloader built with it (esphome.IMAGE, esphome.BOOTLOADER), and its maker's
partition table (esphome.SONOFF_TABLE).

A run keeps the kind it converts (its "plug_kind"): from the eWeLink account
(A1), from what the stock firmware says it is (A2), or from a converted
Tasmota's template.  Official Tasmota ends up named after the kind, by its
template, and ESPHome by the image made for it; every firmware part way
through a conversion is the tool's own, the same for every kind.

One kind so far: the Sonoff S60.
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
    # What a person calls it, its maker, and its name for short in running text.
    name: str
    maker: str
    short: str
    # eWeLink's product model for one, as the account gives it, and what the
    # last letter of that says of the plug's type and region.
    product: re.Pattern[str]
    regions: tuple[tuple[str, str], ...]
    # What its stock firmware calls itself, to eWeLink and to the updater.
    stock_model: str
    # The model a file must be wrapped for, for the stock updater to take it.
    wrapper_model: str
    # Its wiring, as a Tasmota template gives it.
    tasmota_gpio: tuple[int, ...]
    # Its ESPHome's friendly name; the network that raises of its own, which
    # ends in the last two bytes of the plug's MAC; and that network's name
    # in an earlier build.
    esphome_name: str
    esphome_setup_prefix: str
    esphome_legacy_prefix: str

    def product_of(self, product_model: str | None) -> str:
        """`product_model` as eWeLink gives it, if it is one of this kind's; else ""."""
        product = (product_model or "").strip().upper()
        return product if self.product.fullmatch(product) else ""

    def region(self, product_model: str | None) -> str | None:
        """The plug type and region a product model of this kind's stands for."""
        product = self.product_of(product_model)
        return dict(self.regions).get(product[-1:]) if product else None

    def template_name(self, product_model: str | None) -> str:
        """Tasmota's name for it: after its own product model where eWeLink gave
        that (Sonoff S60TPG), else the kind's (Sonoff S60).  Nothing but one of
        this kind's product models ever reaches a Tasmota command."""
        product = self.product_of(product_model)
        return f"{self.maker} {product}" if product else self.name

    def tasmota_template(self, product_model: str | None) -> str:
        """Its Tasmota template, as the Template command takes it."""
        return json.dumps({"NAME": self.template_name(product_model), "GPIO": list(self.tasmota_gpio),
                           "FLAG": 0, "BASE": 1}, separators=(",", ":"))

    def names_its_tasmota(self, named: str) -> bool:
        """Whether a Tasmota's module and template, as it names them, are this
        kind's: its template is named after it, whichever product model."""
        return self.short in named


SONOFF_S60 = Kind(
    key="sonoff-s60",
    name="Sonoff S60",
    maker="Sonoff",
    short="S60",
    # S60TP and the plug-type letter: the UK's S60TPG, Europe's S60TPF.
    product=re.compile(r"S60TP[A-Z]"),
    regions=(("G", "UK (Type G)"), ("F", "Europe (Type E/F)")),
    stock_model="SN-ESP32C3-S60-01",
    wrapper_model="FWSW-E32S60-S60-ESP32C3FN4",
    # Relay GPIO4, status LED GPIO5, power meter GPIO7, button GPIO9.  The UK
    # S60TPG and the European S60TPF publish the same pins.
    tasmota_gpio=(1, 1, 1, 1, 224, 544, 1, 3104, 1, 32, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1),
    # ESPHome/sonoff_s60.yaml; one built from it before its network took this
    # name raised sonoff-s60- and the MAC's last three bytes.
    esphome_name="Sonoff S60",
    esphome_setup_prefix="Sonoff-Setup-",
    esphome_legacy_prefix="sonoff-s60-",
)

# Every kind the tool converts.
ALL = (SONOFF_S60,)


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


def for_tasmota(named: str) -> Kind | None:
    """The kind of a converted Tasmota, by its module and template as it names them."""
    return next((kind for kind in ALL if kind.names_its_tasmota(named)), None)


def names(attribute: str = "name") -> str:
    """Every kind, as a person calls them (Sonoff S60): for before the plug is known."""
    return " or ".join(getattr(kind, attribute) for kind in ALL)


def stock_models() -> str:
    """Every kind, with what its stock firmware calls itself."""
    return ", ".join(f"{kind.name}: {kind.stock_model}" for kind in ALL)


def name_words() -> set[str]:
    """The words of every kind's name, in lower case: an eWeLink name of only
    these (and its product model) is the one eWeLink gave, which names nobody."""
    return {word for kind in ALL for word in kind.name.lower().split()}


def of_run(state) -> Kind:
    """The kind a run converts (`state`: its RunState): as it recorded once
    known, else the first the tool knows - the only one, so far."""
    return by_key(state.input("plug_kind")) or ALL[0]
