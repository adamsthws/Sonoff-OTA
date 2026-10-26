"""The S60's meter calibration, as Sonoff's firmware applies it, for Tasmota
and ESPHome to read as Sonoff's did.

Two parts, decoded from stock 1.2.0 (ESPHome/sonoff_s60.yaml applies both):

- The board's factor: at start-up stock reads GPIO6, GPIO10 and GPIO19 with
  pull-ups, and scales voltage and power by 1.02 unless all three read high.
  Nothing records it, so it is read from the plug each time.
- The plug's factory calibration, in Sonoff's NVS (namespace user_unusual,
  blob content, JSON field calib_coefficient0): "1,1,[9 numbers],[9 numbers]",
  two sets of a, b, c for current, voltage and power - the first below 300 W,
  the second from 300 W on - applied as a*x^2/1e7 + b*x/1e6 + c/1e6 in mA, mV
  and mW.  A plug converted through Tasmota has lost it (the new layout
  clears NVS), but its Sonoff backup keeps it (autoflash_cli.backup_calibration).

Tasmota scales each reading by one whole number per quantity (VoltageCal,
CurrentCal, PowerCal), which it starts from the meter chip's own numbers
whenever one holds Tasmota's default.  So the board's factor and each set-1
b term carry over exactly; the a and c terms (none on the plugs examined so
far) and the set from 300 W on do not.  Its energy is the meter's pulses
times PowerCal, so it is calibrated with the power.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

BOARD_PINS = (6, 10, 19)
BOARD_FACTOR = 1.02
# Tasmota's defaults (include/tasmota.h at the pinned release): a setting that
# holds one is taken from the chip again at the meter's next reading.
TASMOTA_DEFAULTS = {"VoltageCal": 1950, "CurrentCal": 3500, "PowerCal": 12530}
# Which set-1 b term scales each, and whether the board's factor does.
_TASMOTA_TERMS = {"VoltageCal": (4, True), "CurrentCal": (1, False), "PowerCal": (7, True)}
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def parse(text: str) -> list[float] | None:
    """The 18 numbers of a calib_coefficient0 after its two 1s, or None if it
    is not one - or one whose b terms would not scale a reading (from 0.5 to
    1.5)."""
    numbers = [float(number) for number in _NUMBER.findall(text)]
    if len(numbers) != 20 or not all(0.5e6 <= numbers[2 + term] <= 1.5e6 for term in range(1, 18, 3)):
        return None
    return numbers[2:]


def board_factor(levels: list[int]) -> float:
    """The factor for the board pins' levels, read with pull-ups."""
    return 1.0 if all(levels) else BOARD_FACTOR


def tasmota_values(chip: dict[str, int], factor: float,
                   coefficients: list[float] | None = None) -> dict[str, int]:
    """Tasmota's three calibration settings for the chip's own (`chip`), the
    board's factor and, if known, the plug's factory calibration.  Never one
    of Tasmota's defaults, which it would take from the chip again."""
    values = {}
    for name, (term, scaled) in _TASMOTA_TERMS.items():
        value = chip[name] * (factor if scaled else 1.0)
        if coefficients:
            value *= coefficients[term] / 1e6
        value = round(value)
        values[name] = value + 1 if value == TASMOTA_DEFAULTS[name] else value
    return values


def calibrate_tasmota(command: Callable[[str], Any], coefficients: list[float] | None = None,
                      wait: float = 10.0, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Tasmota's meter calibrated as Sonoff's firmware calibrated it, through
    its console (`command`).  Done again, it comes out the same: the chip's
    own numbers are taken afresh each time.  One calibrated another way -
    by hand, against a meter (PowerSet and the like) - is left as it is.
    What was set, and whether that "changed" it; or why nothing was
    ("skipped")."""
    levels = []
    for pin in BOARD_PINS:
        command(f"Br gpio.pin_mode({pin}, gpio.INPUT_PULLUP)")
        answer = command(f"Br gpio.digital_read({pin})")
        command(f"Br gpio.pin_mode({pin}, gpio.INPUT)")
        level = answer.get("Br") if isinstance(answer, dict) else None
        if level not in ("0", "1"):
            return {"skipped": "the plug's board could not be read"}
        levels.append(int(level))
    factor = board_factor(levels)
    before = {}
    for name in TASMOTA_DEFAULTS:
        answer = command(name)
        before[name] = answer.get(name) if isinstance(answer, dict) else None
    if not all(isinstance(value, int) for value in before.values()):
        return {"skipped": "Tasmota did not say how its meter is calibrated", "factor": factor}
    # Each on its own, not in a Backlog, which runs them later: each answers
    # once it has taken effect.
    if not _set(command, TASMOTA_DEFAULTS):
        return {"skipped": "Tasmota did not take its default calibration", "factor": factor}
    chip: dict[str, int] = {}
    deadline = time.monotonic() + wait
    while True:
        for name, default in TASMOTA_DEFAULTS.items():
            answer = command(name)
            value = answer.get(name) if isinstance(answer, dict) else None
            if isinstance(value, int) and value != default:
                chip[name] = value
        if len(chip) == len(TASMOTA_DEFAULTS):
            break
        if time.monotonic() >= deadline:
            # Left as it was: without its readings, the chip's numbers are not known.
            _set(command, before)
            return {"skipped": "the meter did not report", "factor": factor}
        sleep(0.5)
    values = tasmota_values(chip, factor, coefficients)
    if before not in (chip, values, TASMOTA_DEFAULTS):
        _set(command, before)
        return {"skipped": "it was calibrated another way already",
                "factor": factor, "kept": before}
    if not _set(command, values):
        return {"skipped": "Tasmota did not take its calibration", "factor": factor}
    return {"factor": factor, "factory": coefficients is not None, "chip": chip, "changed": before != values,
            **values}


def _set(command: Callable[[str], Any], values: dict[str, int]) -> bool:
    """Each of Tasmota's calibration settings set, and whether each says so."""
    for name, value in values.items():
        answer = command(f"{name} {value}")
        if not isinstance(answer, dict) or answer.get(name) != value:
            return False
    return True
