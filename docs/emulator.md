# The emulator: converting a virtual S60

`tools/emulator/` runs the whole conversion against an emulated Sonoff S60, so
changes can be tried end to end without converting, or risking, a real plug.

The emulated plug boots a real flash dump of a stock S60, so it runs Sonoff's
own firmware, unmodified apart from the changes listed below. A full run has
been completed this way from a UK S60TPG on stock 1.2.0, through every
stage, to the official Tasmota on the official layout. It is a development
tool: it says nothing about hardware the dump did not come from, such as
another regional version's power meter.

## What you need

- **Docker.** Everything that is built or run lives in containers; nothing is
  installed on the host.
- **A flash dump of a stock plug,** taken with `tools/s60_readout.py` (over
  Wi-Fi; the plug is put back to stock afterwards). Copy `flash.bin` and
  `efuse.bin` into `captures/emulation/`.
- **The NVS partition generator** in the repository's `.venv`:

  ```sh
  .venv/bin/python -m pip install esp-idf-nvs-partition-gen
  ```

**The dump is private.** It holds the home Wi-Fi password and the plug's
eWeLink keys in cleartext. Keep it, and everything the emulator writes, under
`captures/` (Git-ignored): the emulator puts all its state in
`captures/emulation/state/`.

**The emulated plug is a clone of the real one** - same eWeLink device id,
keys and MAC. If both are online, they fight over one device in the account,
and an upgrade sent by the conversion could reach the real plug. Unplug the
real plug before using `--cloud`.

## Using it

```sh
tools/emulator/emu.sh            # plug offline: nothing reaches the internet
tools/emulator/emu.sh --cloud    # plug and tool online - asks you to confirm the real plug is unplugged
tools/emulator/emu.sh --fresh    # put the plug back to stock, and forget the previous run and the eWeLink account
tools/emulator/emu.sh --pcap     # also record the plug's Wi-Fi traffic
tools/emulator/emu.sh --rebuild  # rebuild the image first
tools/emulator/emu.sh --detach   # start in the background; join with: docker attach s60-emu
tools/emulator/emu.sh --no-firewall  # without the "computer's" firewall, which is on by default
tools/emulator/emu.sh --windows  # Windows is the computer (see "On Windows"; started by emu.ps1 and windows-test.sh)
```

A conversion needs `--cloud`: the upgrade to the bridge firmware is sent
through eWeLink. The first start builds the image, which takes a few minutes.

The terminal becomes a tmux session. Window **conversion** is "your computer
on the home Wi-Fi": run the tool there exactly as on a real plug,

```sh
python tools/s60_autoflash.py run    # or just: run  (options pass through: run --verbose)
```

and window **plug** follows the plug's serial output, with the dump's secrets
masked (Ctrl-b n / Ctrl-b p switch windows; Ctrl-b d leaves, keeping the
plug's flash). When the tool asks you to do something to the plug or your
Wi-Fi:

| On a real plug | In the emulator |
|---|---|
| Switch it off and on | `plug restart` (or `plug off`, `plug on`) |
| Power-cycle it quickly, N times | `plug cycle N` |
| Your desktop's Wi-Fi menu | `wifi`, `wifi join NAME`, `wifi home` |
| Your home Wi-Fi's name and password | `PICSimLabWifi`, empty password |
| Your computer's password, when sudo asks | `s60` |
| The plug's web page | http://localhost:8080 |

The tool knows when it runs in the emulator (`S60_EMULATOR_URL`), and its
final link points at http://localhost:8080 instead of the plug's own address.
Where it warns that sudo may ask for a password, it gives the emulator's
(`S60_EMULATOR_SUDO_PASSWORD`); on a real computer it never knows one.

## On Windows

To test the tool's Windows code paths (`arp -a`, `netsh` firewall rules
behind the elevation prompt, the Windows firewall itself), Windows can be the
computer on the emulated home Wi-Fi instead of the container. A wire carries
Ethernet frames between the emulator and a TAP adapter on Windows, so Windows
sees the plug's MAC, its mDNS answers and its connections exactly as on a
real network. There are two ways to have it:

- **Windows from Linux** - nothing to install by hand, and a clean Windows
  every run: `tools/emulator/windows-test.sh`.
- **On a Windows computer**, the emulator in WSL beside it:
  `tools\emulator\emu.ps1`.

Both check what is needed first, list what is missing, and get all of it
after one question - and, where administrator rights are needed, one sudo or
User Account Control prompt. Joining the plug's own Wi-Fi from Windows is not
emulated either way.

### Windows from Linux

```sh
tools/emulator/windows-test.sh --cloud
```

The emulated plug runs in Docker as usual, and beside it **Windows Server 2025,
Server Core** in a container of its own (`tools/emulator/windows/`): QEMU with
KVM, so one layer of virtualisation, nothing nested. It needs Linux with
`/dev/kvm` and Docker; on a Mac, use a Windows computer or VM instead (below).
If `/dev/kvm` is missing on a PC, its processor's virtualisation is usually
off in the firmware set-up: "SVM Mode" on AMD, "Intel Virtualization
Technology" (VT-x) on Intel.

Both containers are capped, so they cannot take over the computer. Windows
gets 2 GB and 2 CPUs (`S60_WINDOWS_RAM=3G`, `S60_WINDOWS_CPUS=4` to change), and
its container at most 1 GB more, for QEMU itself, without swap. The emulator
gets 1 GB and 2 CPUs (`S60_EMU_MEMORY`, `S60_EMU_CPUS`).

The first run fetches Microsoft's **Windows Server 2025 evaluation ISO** - 8.2
GB, straight from Microsoft's Evaluation Center link, no sign-up, checked
against its pinned SHA-256 - and installs Windows from it once, in 20-40
minutes. A seed disc beside the ISO carries the answer file, the set-up
scripts and this checkout: Setup installs the Datacenter Server Core image,
the first logon copies the scripts off the seed disc, and Windows gets the
tool's portable Python and the TAP driver itself, with the same
`windows-bootstrap.ps1` a Windows computer uses. The prepared disk is kept in
`~/.cache/s60-emu/windows/base.qcow2`, and the ISO is deleted; every run
starts from a throwaway copy of that disk, with this checkout in
`C:\S60\repo`, in about a minute.

Windows logs on by itself, opens the link and a PowerShell window for the
conversion - in **OpenConsole**, Windows Terminal's own console, as on Windows
11, or with `--classic-console` in Windows' classic one, as on Windows 10. Work
in it over Remote Desktop - `localhost:3389`, user `s60`,
password `S60-emulator` (only this computer can reach it) - or watch on VNC,
`localhost:5900`. The terminal you started from is the emulator's, for `plug
restart` and the serial log; leaving it shuts Windows down. The conversion's
captures are kept between runs (`~/.cache/s60-emu/windows/captures`), so an
unfinished one can be resumed.

Server Core has no desktop, which is what makes it quick to install and run,
and leaves three things a person's Windows has untested here: the User
Account Control prompt (an administrator's console is already elevated, so the
firewall rule is added without asking), Windows Security's notifications, and
Windows Terminal's window itself. Terminal cannot start on Server Core, which
lacks the XAML it draws with; OpenConsole, from the same release (pinned, 12
MB, fetched once into `~/.cache/s60-emu/windows`), is the same code for what
the tool's output depends on - colours, the escape sequences that redraw a
line or take a block back off the screen, wrapping - but not Terminal's tabs,
or its clickable links. Everything the tool does on the network - finding the
plug, the firewall rule, serving the firmware - is the same as on any Windows.

The evaluation lasts 180 days from when the disk was installed; after that,
delete `base.qcow2` and the next run installs it again.

### On a Windows computer

From the repository in PowerShell:

```powershell
tools\emulator\emu.ps1 -Cloud     # opens the emulator and the link, each in a window
.\start.ps1                       # once the link says Connected - as a Windows user would
```

`emu.ps1` first runs `tools\emulator\windows-bootstrap.ps1 -LocalEmulator`,
which installs what is missing: WSL 2 (one restart - run it again after, and
it carries on without asking), Ubuntu in it with Docker and the NVS
generator, Python if there is no 3.10 or newer (python.org's, for your
account), OpenVPN's TAP-Windows6 driver (pinned installers, checked by
SHA-256), and the "S60 emulator" adapter - 192.168.60.10/24, no gateway, so
Windows' internet stays where it is, and the lowest interface metric, so the
tool's mDNS goes out on it. While the emulator is not running the adapter
shows as disconnected and does nothing. `windows-bootstrap.ps1 -ListPython`
shows every Python it finds, and which it would use.

The emulator window is the usual tmux session, for `plug restart` and the
serial log. To wire a Windows computer to an emulator running elsewhere,
`windows-bootstrap.ps1` without `-LocalEmulator`, then
`windows_link.py --host ADDRESS`.

From a Windows checkout the emulator keeps its state - the plug's flash, the
serial log, the in-container run's captures - in `~/.local/share/s60-emu/state`
inside WSL (`\\wsl$\<distribution>\home\<you>\.local\share\s60-emu\state` from
Windows), not in `captures/emulation/state/`. WSL reaches the Windows drive
through 9p, where one flash write can take 100 ms, and the bridge then trips its
interrupt watchdog while writing Tasmota. The Windows tool's own captures stay
in `captures/` as usual.

WSL stops its VM, and Docker with it, soon after its last window closes: with
`emu.sh --detach`, keep a WSL window open.

```
 Windows: s60_autoflash.py ── "S60 emulator" TAP 192.168.60.10
                                   │  windows_link.py ⇄ 127.0.0.1:7860 (length-prefixed frames)
 container s60-emu ────────────────┼───────────────────────────────────────
   windows-link ⇄ tap1 ─┐          │
                        br0 192.168.60.1 (dnsmasq, ufw) ── tap0 ── QEMU (plug .50)
```

## How it fits together

```
 your browser ── localhost:8080 ─┐
                                 │  container s60-emu ("your computer")
 ┌───────────────────────────────┼──────────────────────────────────────────┐
 │  browser-forward ─────────────┘                                          │
 │  s60_autoflash.py, nmcli stand-in, wifi / plug commands                  │
 │        │                                                                 │
 │  tap0 192.168.60.1/24 ── dnsmasq (DHCP + DNS; plug fixed at .50)         │
 │        │                    └─ internet: only with --cloud (iptables)    │
 │  QEMU (lcgamboa fork) ── emulated Wi-Fi ── open AP PICSimLabWifi         │
 │        └─ flash.bin / efuse.bin in captures/emulation/state/             │
 └──────────────────────────────────────────────────────────────────────────┘
```

- **QEMU.** Espressif's QEMU has no Wi-Fi; the emulator uses
  [lcgamboa's fork](https://github.com/lcgamboa/qemu) (branch `picsimlab-esp32`,
  pinned in `emu.Dockerfile`), which emulates the ESP32-C3's Wi-Fi hardware
  and offers three open access points. Its Wi-Fi is wired to a tap device in
  the container.
- **The home network.** `tap0` is the home network, and the container is its
  router: dnsmasq hands out addresses (the plug's MAC always gets
  192.168.60.50), and mDNS discovery works as on a real LAN. Without
  `--cloud`, iptables stops everything in the container reaching the
  internet, so the cloned identity cannot be used by mistake.
- **Wi-Fi scans.** The tool lists networks with `nmcli`; a stand-in reports
  the fork's access points, plus the plug's own network while it is
  broadcasting one.
- **The browser.** `browser-forward` takes localhost:8080 to the plug,
  wherever it is (see the Referer check below).
- **The firewall and sudo.** As on a typical Linux desktop, ufw is on and
  blocks incoming connections, and sudo asks for your password (`s60`). So
  the conversion has to add its temporary rule for the plug, through sudo,
  and remove it again, and any exit path that leaves it behind shows up. ufw
  allows only what the emulator itself needs: the plug's DHCP and DNS, and
  port 8080 for your browser. The tool checks for ufw with
  `systemctl is-active ufw`; the container has no systemd, so a stand-in
  answers from ufw's own setting. The emulator's `wifi` command needs no
  password, as a desktop's Wi-Fi menu does not. `--no-firewall` leaves ufw
  off.
- **Power.** `plug-supervisor` keeps QEMU running and starts it again when it
  exits, which is how a power cut looks.

## What is changed, and why

### The dump (`patch_stock.py`, `rewrite_nvs.py`)

Copies only; the original dump is never written.

- **Bluetooth start-up is skipped.** Neither QEMU emulates Bluetooth: the
  controller's clock set-up fails and its ROM code faults. Sonoff's
  Bluetooth set-up and a second controller start are stubbed to `return 0`,
  checked against the original bytes first, and the app image is re-sealed
  (checksum and SHA-256). Bluetooth is only used for first-time pairing in
  the eWeLink app, which a plug being converted has already done.
- **The home Wi-Fi becomes the emulator's.** Stock 1.2.0 keeps the network it
  joins in two places: ESP-IDF's `nvs.net80211` (`sta.ssid`, `sta.pswd`) and
  Sonoff's own `frame_params` (`ssid`, `passwd`). Both are rewritten to
  `PICSimLabWifi` with no password, so the real home Wi-Fi is not in the
  emulated plug at all. The partition is rebuilt with ESP-IDF's
  `nvs_partition_gen`, which adds `sta.apinfo`, `sta.pmk` and `sta.apsw` itself.
- **The eFuse dump needs no conversion.** QEMU's ESP32-C3 eFuse file is the
  read registers `EFUSE_RD_WR_DIS_REG` to `EFUSE_RD_SYS_PART2_DATA7_REG` in
  order, which is what `efuse.bin` holds; it is only padded to 1024 bytes. The
  plug's MAC comes from it.

### The QEMU fork (`tools/emulator/fork/*.patch`)

- **`station-bridge.patch` - real addresses on the wire.** The fork rewrote
  every frame's MAC addresses to its access point's, which works behind
  QEMU's NAT (slirp) but not on a real network: Linux dropped the frames, so
  the plug never got an address, and the tool could not find it by MAC. It now
  bridges the real addresses. It also gives the fork's virtual station a
  proper MAC after a cold start (it was all zeros).
- **`softap-status.patch` - the plug's own network is visible.** The fork
  writes the name of the network the plug broadcasts, and the MAC its virtual
  station joins it with, to a file the `nmcli` stand-in and `wifi join` read.
- **`gpio-pullups.patch` - the button is not held down.** Run on its own, the
  fork drives no input pins, so they all read low. The S60's button (GPIO9)
  is active low, so Tasmota saw it held, and after 40 seconds reset itself to
  its defaults, losing its Wi-Fi. Inputs now idle high, as with pull-ups.

### How QEMU is run (`container/bin/plug-supervisor`)

- **`-no-reboot`.** The fork keeps its peripherals running across a software
  reset, so firmware restarting into a different image (stock into the
  bridge, the bridge into Tasmota) crashed at start-up on what the previous
  one left behind. QEMU now exits on a reset and is started again, so every
  restart is a clean power-on. Nothing in the conversion depends on the
  reset type.
- **`-serial file:/dev/stdout`, not `-serial stdio`.** With stdin at
  `/dev/null`, the fork's stdio serial port prints nothing.
- **Only ARP requests for the plug reach it** (a `tc` filter on `tap0`, in
  `entrypoint.sh`). When the plug does not answer, the conversion tool looks
  for it by pinging every address on the network, and that burst of ARP
  requests stops the fork's Wi-Fi receiving for good: the plug keeps sending,
  but hears nothing until it is restarted. A real plug ignores requests for
  other addresses anyway.

### The browser forward (`container/bin/browser-forward`)

Tasmota serves every page except `/` only when the `Referer` names its own
address - a guard against other sites driving it. A browser at
localhost:8080 names localhost, so a plain port forward got empty replies on
every menu. The forward rewrites `Host`, `Referer` and `Origin` to the plug's
address, sends a redirect that names the plug back through localhost, and
gives each request its own connection.

## Known gaps

- **Open networks only.** The fork's access points have no WPA2, so the home
  network has no password, and the bridge's own network (WPA2) cannot be
  joined. The bridge rejoins the home network instead, so the conversion
  does not need it.
- **Joining the plug's own network does not work yet.** `wifi join` takes the
  station MAC, but gets no address: the fork does its own DHCP as the
  station before it passes traffic. Tasmota's setup-network path is untested
  here.
- **No power meter.** The CSE7766 is not emulated, so readings are absent and
  the end-of-run check reports no change on the meter.
- **One plug, one region.** The emulator runs whatever the dump came from. A
  dump of another model or region (with that owner's account) is needed to
  emulate it.

## Debugging

- **The serial log** is `captures/emulation/state/logs/uart.log`, redacted by
  `tools/emulator/redact.py`, which masks the dump's secrets and drops panic
  memory dumps (they show RAM as words the masking cannot see into). Put any
  other log through it before reading it.
- **The QEMU monitor** is at `/run/s60emu/monitor.sock` in the container.
  It does not answer while the guest is in a tight crash loop.
- **gdb** (PlatformIO's `riscv32-esp-elf-gdb`) against QEMU's `-s` needs a
  pseudo-terminal (run it under `script -qfc`), and hardware breakpoints
  (`hbreak`): the bootloader overwrites software breakpoints in IRAM when it
  loads the app. For the bridge's symbols, build its ELF with
  `pio run -e s60-ota-bridge` in `archive/bridge`.
- **Without a NIC** (`-nic none`) the fork does not map its Wi-Fi device, and
  firmware hangs in Wi-Fi initialisation. That is a test set-up problem, not
  a firmware one.
- **The image's build environment** alone is `tools/emulator/qemu-build.Dockerfile`,
  handy for building the fork incrementally while working on a patch.
