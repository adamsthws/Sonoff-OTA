"""The emulated S61 as a plug: on until switched off, through every firmware it
is converted to and back, as bk-emu.sh plug runs it (plug-net.sh).

    python plug.py --state DIR [--tap tap0]

Its flash is DIR/flash.bin: the public dump the first time, with the home
Wi-Fi's details added to its store, as a plug provisioned in the app has
them; from then on what the plug left there. Each boot starts from the
bootloader, which applies an image waiting in the download slot as a real
plug's does, and every restart the firmware asks for is a boot of its own,
from the flash as it was left.

Sonoff's firmware needs the emulator's help to come online
(cloud_online.StockHooks); those hooks are at Sonoff's addresses, so they
act only while Sonoff's app is the one that starts: it is told by the app's
first bytes, as they were on the plug's first boot.

PLUG_TRACE=ADDR,ADDR (hex) logs each call there to the serial output.

The power switch is DIR/power: "off" stops the plug and keeps its flash,
"on" starts it again (bk-emu.sh power). The serial output is DIR/serial.log.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import signal
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cloud_online  # noqa: E402  (needs the emulator image)
import stage_apply  # noqa: E402
from src.main import build_emulator  # noqa: E402
from unicorn import UC_HOOK_CODE  # noqa: E402
from unicorn.arm_const import UC_ARM_REG_LR, UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3  # noqa: E402

SSID, PASSWORD = cloud_online.SSID, cloud_online.PW
APP_ENTRY = 0x10000
# Enough of the app to tell Sonoff's from another firmware's.
APP_SIGNATURE = 0x20000
PEER_MAC = b"\x02\x00\x00\x00\x00\x01"


def trace(mu, address: int) -> None:
    """Log each call at `address` (PLUG_TRACE): its first four arguments - as
    strings where they point at one - and where it returns to."""
    def text(value: int) -> str:
        try:
            raw = bytes(mu.mem_read(value, 32)).split(b"\0")[0]
        except Exception:
            return "%#x" % value
        return repr(raw.decode()) if raw and all(32 <= c < 127 for c in raw) else "%#x" % value

    def called(uc, at, size, user):
        args = [text(uc.reg_read(r)) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)]
        print("[trace] %#x(%s) from %#x" % (at, ", ".join(args), uc.reg_read(UC_ARM_REG_LR)), flush=True)
    mu.hook_add(UC_HOOK_CODE, called, None, address, address)


class Plug:
    def __init__(self, state: Path, tap: str):
        self.state, self.tap = state, tap
        self.flash_path = state / "flash.bin"
        self.power_path = state / "power"
        self.stock_path = state / "stock-app.sha256"
        self.stopping = False
        self.netlog: list[str] = []

    def prepare(self) -> None:
        """The flash, the first time: the dump with the Wi-Fi in its store."""
        if self.flash_path.exists():
            return
        flash = bytearray(Path(cloud_online.DUMP).read_bytes())
        stage_apply.stage(flash, None, {"wifi-ssid": SSID, "wifi-pass": PASSWORD})
        part = self.flash_path.with_suffix(".part")
        part.write_bytes(flash)
        os.replace(part, self.flash_path)

    def powered(self) -> bool:
        try:
            return self.power_path.read_text(encoding="utf-8").strip() != "off"
        except OSError:
            return True

    def save(self, emu) -> None:
        flash = bytes(emu.physical_flash if emu.physical_flash is not None else emu.raw_flash)
        part = self.flash_path.with_suffix(".part")
        part.write_bytes(flash)
        os.replace(part, self.flash_path)

    def is_stock(self, app: bytes) -> bool:
        """Whether the app starting is Sonoff's, by its first bytes: as they
        were at the plug's first boot ever, of the dump as it came."""
        digest = hashlib.sha256(app).hexdigest()
        if not self.stock_path.exists():
            self.stock_path.write_text(digest + "\n", encoding="utf-8")
        return digest == self.stock_path.read_text(encoding="utf-8").strip()

    def boot(self):
        emu = build_emulator(str(self.flash_path), chip="BK7238", with_boot=True, only_uart=True,
                             save_decrypted=False, wifi=True, log=self.netlog.append)
        emu.flash_writethrough = True
        emu.setup()
        emu.net_sink = self.netlog.append
        emu.attach_wifi_peer(role="ap", ssid=SSID.decode(), passphrase=PASSWORD.decode(), channel=1,
                             peer_mac=PEER_MAC, tap=self.tap)
        ca = (self.state / "ca.crt").read_bytes()
        hooks = cloud_online.StockHooks(emu, SSID, PASSWORD, ca, cloud_online.FORCE_AT, self.netlog)
        hooks.active = False

        def app_starts(mu, address, size, user):
            hooks.active = self.is_stock(bytes(mu.mem_read(APP_ENTRY, APP_SIGNATURE)))
            if hooks.active:
                hooks.boot()
            print("[plug] the app starts: %s" % ("Sonoff's firmware" if hooks.active else "another firmware"),
                  flush=True)
        emu.mu.hook_add(UC_HOOK_CODE, app_starts, None, APP_ENTRY, APP_ENTRY)
        for address in filter(None, os.environ.get("PLUG_TRACE", "").split(",")):
            trace(emu.mu, int(address, 16))
        return emu

    def run_once(self) -> None:
        """One boot, until the firmware restarts, the power goes off or the
        container stops; the flash kept as it was left."""
        emu = self.boot()
        thread = threading.Thread(target=emu.run, daemon=True)
        thread.start()
        resets = emu.reset_count
        why = None
        while why is None:
            time.sleep(0.5)
            if self.stopping:
                why = "stopped"
            elif not self.powered():
                why = "switched off"
            elif emu.reset_count != resets:
                why = "restarted"
            elif not thread.is_alive():
                why = "halted"
        emu.stop()
        thread.join(10)
        self.save(emu)
        emu.close_network()
        print("[plug] %s; its flash is kept" % why, flush=True)
        if why == "halted":
            time.sleep(5)   # not round again at once, should it halt at every boot

    def run(self) -> None:
        self.prepare()
        while not self.stopping:
            if not self.powered():
                time.sleep(0.5)
                continue
            self.run_once()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--tap", default="tap0")
    parser.add_argument("--prepare", action="store_true", help="only make the flash, the first time")
    args = parser.parse_args(argv)
    plug = Plug(args.state, args.tap)
    if args.prepare:
        plug.prepare()
        return 0

    def stop(*_):
        plug.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    plug.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
