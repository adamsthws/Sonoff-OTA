"""Mock-up of the firmware choice, drawn with the tool's own prompt code.

Run from the repository root:  python3 ESPHome/prototype/firmware_choice_mockup.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from tools.autoflash import output  # noqa: E402
from tools.autoflash.prompts import QuitRun, ask_choice, section  # noqa: E402


def tell(message: str = "") -> None:
    output.show(message)


output.start(None, verbose=False)
tell("")
tell("Converting a plug usually takes under 10 minutes. Some stages take a")
tell("minute or two with little to see - that's normal. Keep the plug plugged")
tell("in, and this computer awake, until the last stage says complete.")

output.transient_begin()
section("Choose the plug's new firmware", [
    "Tasmota has its own web page for settings, and works with Home Assistant",
    "and many other systems. ESPHome is made for Home Assistant, and is set",
    "up and customised from there.",
])
try:
    choice = ask_choice("Which firmware would you like", ["Tasmota", "ESPHome"])
except QuitRun:
    choice = None
output.transient_end()
tell("")
tell({0: "Converting to Tasmota.", 1: "Converting to ESPHome.", None: "Stopped."}[choice])
output.finish()
