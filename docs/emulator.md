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

- **Docker,** on Linux, macOS, or Windows through WSL 2. Everything that is
  built or run lives in containers; nothing is installed on the host.
- **A Sonoff backup of a stock plug.** Every conversion keeps one for each
  plug, in `captures/plugs/<MAC>/sonoff-backup/`;
  `tools/flash_readout.py` takes one without converting (over Wi-Fi; the plug is
  put back to stock afterwards). `emu.sh` lists them by MAC address - on the
  plug's label, and in your router and the eWeLink app - and asks which plug
  to run when there are several (`--plug <MAC>` says beforehand; with nobody
  at the keyboard the plug run last is used). It runs a copy: the backup is
  never written. A dump set up the older way, `flash.bin` and `efuse.bin` in
  `captures/emulation/`, is offered too. Each plug's emulated flash is kept
  in `captures/emulation/state/` between runs, so switching plugs and back
  carries on where each was; `--fresh` puts the chosen one back to its
  backup.
- **The NVS partition generator** in the repository's `.venv`:

  ```sh
  .venv/bin/python -m pip install esp-idf-nvs-partition-gen
  ```

### Hardware

| | The emulator alone | With Windows beside it ("Windows from Linux") |
|---|---|---|
| Processor | any 64-bit one Docker runs on | x86-64 with hardware virtualisation (AMD-V/SVM or Intel VT-x) switched on in the firmware, so Linux has `/dev/kvm` |
| Memory | 256 MB, the emulator's cap | 3.3 GB at most: Windows 2 GB, QEMU and the disk tools 1 GB, the emulator 256 MB. An 8 GB laptop with a desktop open manages it |
| Processors | 2, the emulator's cap | 2 more for Windows (caps, shared with everything else, not set aside) |
| Disk | about 0.5 GB: the image, and the plug's state under `captures/emulation/state/` | about 30 GB free while Windows installs; then about 21 GB: the ISO 8.2 GB, the layers 10.8, 0.9 and 0.4 GB, the images 0.8 GB |
| Downloads | the QEMU fork's source, once | Microsoft's ISO (8.2 GB), Windows Terminal (12 MB) and virtio-win's guest tools (32 MB), once; Python (11 MB) and OpenVPN (6 MB) each time a layer that gets them is set up |
| Time | see "How long it takes", below | |

Measured on an AMD Ryzen 5 4500U (6 cores) with 8 GB: Windows peaked at
2.1 GB, the emulator at 80 MB, and the layers took 7, 2 and 1 minutes to set
up. The caps can be changed (below), and a computer short of memory is warned
before Windows starts.

### How long it takes

For a computer like the one these were measured on - an AMD Ryzen 5 4500U
laptop with 8 GB, from 2020 - or faster. A faster one is nearer the low end.

**Once, the first time:**

| Step | Takes |
|---|---|
| Building the emulator's image (QEMU from source) | 2-3 minutes |
| Building the Windows VM's image | about a minute |
| Downloading Microsoft's ISO, 8.2 GB | depends on the connection: about 20 minutes at 50 Mbit/s, 2-3 at 500 Mbit/s |
| Installing Windows (layer `installed`) | 5-10 minutes |
| Setting up the `rig` and `tool` layers | 2-4 minutes together |

So the emulator alone is ready in a few minutes, and Windows beside it in
about 10-20 minutes plus the download.

`windows-test.sh`, and `emu.sh` while you are in it, keep the computer from
going to sleep: an install or a conversion cut off half way has to start
again. They hold a lock (`systemd-inhibit`) for as long as they run, and
change no setting, so nothing is left to put back; the lock goes when they
exit, however they exit. A flat battery cannot be held off, so on a laptop
running on its battery `windows-test.sh` says to plug it in.

**Each run:**

| Step | Takes |
|---|---|
| The emulator starting, until the plug is on its Wi-Fi | under 10 seconds |
| Windows from Linux: starting, until the link says Connected | 30-60 seconds |
| A conversion | about 7 minutes on a real plug, through all 15 stages; about 5 in the emulator (4m 35s for an unattended run from stock) |
| Windows shutting down at the end | about 10 seconds |

**After changing what Windows is set up with:** the layers whose scripts
changed are set up again before the run starts - about a minute for `tool`,
2-4 for `rig` (and `tool` above it), or the full install again for
`installed`, from the kept ISO, without the download.

**The dump is private.** It holds the home Wi-Fi password and the plug's
eWeLink keys in cleartext. Keep it, and everything the emulator writes, under
`captures/` (Git-ignored): the emulator puts all its state in
`captures/emulation/state/`, and reads the backups without changing them. A
conversion run in the emulator keeps a backup of the emulated plug too, in
the emulator's own copy of the repository, never beside your real ones.

**The emulated plug is a clone of the real one** - same eWeLink device id,
keys and MAC. If both are online, they fight over one device in the account,
and an upgrade sent by the conversion could reach the real plug. Unplug the
real plug before using `--cloud`.

## Using it

On Linux or macOS (on Windows, see "On Windows" below):

```sh
tools/emulator/emu.sh            # plug offline: nothing reaches the internet
tools/emulator/emu.sh --cloud    # plug and tool online - asks you to confirm the real plug is unplugged
tools/emulator/emu.sh --cloud --yes  # ... without asking: you have unplugged it (for unattended tests)
tools/emulator/emu.sh --stub-cloud  # a local eWeLink stub serves this instance alone (its own cloud; see docs/cloud-stub.md)
tools/emulator/emu.sh --plug 58:cf:79:01:02:03  # run that backed-up plug, without asking which
tools/emulator/emu.sh --fresh    # put the plug back to its Sonoff backup, and forget the previous run and the eWeLink account
tools/emulator/emu.sh --pcap     # also record the plug's Wi-Fi traffic
tools/emulator/emu.sh --rebuild  # rebuild the image first
tools/emulator/emu.sh --detach   # start in the background; join with: docker attach autoflash-emu
tools/emulator/emu.sh --no-firewall  # without the "computer's" firewall, which is on by default
tools/emulator/emu.sh --windows  # Windows is the computer (see "On Windows"; started by emu.ps1 and windows-test.sh)
tools/emulator/emu.sh --instance 2  # a second emulator beside the first (see "Several at once")
```

A conversion needs the cloud: the upgrade to the bridge firmware is sent
through eWeLink. Use `--cloud` for the real one (the real plug must be
unplugged, since the emulated one is its clone), or `--stub-cloud` for a local
stand-in that serves this instance alone — then nothing reaches the real
eWeLink, and any number of instances convert at once. See
[cloud-stub.md](cloud-stub.md). The first start builds the image, which takes a
few minutes.

### Several at once

`--instance N` (1-30, on `emu.sh` and `unattended-test.sh`) runs another
emulator beside the first: its own container (`autoflash-emu-N`), state
(`captures/emulation/state-N/`) and browser port (8080+N), so tests can run side
by side. Each is its own small network, so the identical plugs do not meet.
They do meet in eWeLink, though: every clone has the same device id, the cloud
keeps one connection per id, and the upgrade at C2 is sent through it - so it can
reach another instance's plug. Run the eWeLink stages, up to C2, one instance at
a time; after C2 nothing uses the cloud. A new instance has no plug yet, so name
it with `--plug MAC` when there is more than one backup. Each takes about 0.6 of
a CPU and 115 MB.

The terminal becomes a tmux session. Window **conversion** is "your computer
on the home Wi-Fi": run the tool there exactly as on a real plug,

```sh
python tools/autoflash_cli.py run    # or just: run  (options pass through: run --verbose)
```

and window **plug** follows the plug's serial output, with the dump's secrets
masked (Ctrl-b n / Ctrl-b p switch windows; Ctrl-b d leaves, keeping the
plug's flash). When the tool asks you to do something to the plug or your
Wi-Fi:

| On a real plug | In the emulator |
|---|---|
| Switch it off and on | `plug restart` (or `plug off`, `plug on`) |
| Restart your router, so the plug's Wi-Fi goes | `plug router off [SECS]`, `plug router on` |
| Power-cycle it quickly, N times | `plug cycle N` |
| Your desktop's Wi-Fi menu | `wifi`, `wifi join NAME`, `wifi home` |
| Your home Wi-Fi's name and password | `PICSimLabWifi`, empty password |
| Your computer's password, when sudo asks | `autoflash` |
| The plug's web page | http://localhost:8080 |

The tool knows when it runs in the emulator (`AUTOFLASH_EMULATOR_URL`), and its
final link points at http://localhost:8080 instead of the plug's own address.
Where it warns that sudo may ask for a password, it gives the emulator's
(`AUTOFLASH_EMULATOR_SUDO_PASSWORD`); on a real computer it never knows one.

### Unattended, for testing

One command starts the emulator, converts the plug with nobody at the
keyboard, prints the screen the run left, and stops the emulator again
([testing.md](testing.md) says how that is used to test every route, and
every fault in its way):

```sh
tools/emulator/unattended-test.sh --fresh    # from stock, as a new user would
tools/emulator/unattended-test.sh            # from where the last run left the plug
tools/emulator/unattended-test.sh --keep     # leave the emulator running afterwards
tools/emulator/unattended-test.sh -- --new   # anything after -- goes to the run itself
```

It needs a `.env` with the eWeLink account the dump's plug is in (see
[unattended.md](unattended.md)). It starts the emulator with `--cloud --yes`,
so **the real plug must already be unplugged**: it is not asked. `--pcap`,
`--rebuild` and `--no-firewall` are `emu.sh`'s.

Once the plug answers on the emulated Wi-Fi, it runs
`tools/unattended_run.py` in the container's copy of the repository, as your
user. `.env` is handed to that run alone (`--env-file`, from the container's
`/tmp`, removed afterwards): the copy of the repository never holds it, so a
run you start yourself with `emu.sh` asks for the eWeLink account and
everything else, as a real one does. The same goes for Windows: its copy of
the checkout has no `.env`. The run's sudo
password is the emulator's, and everything else is answered as
[unattended.md](unattended.md) describes. What `.env` holds of your real
network and plugs - the Wi-Fi details, a plug's Tasmota or ESPHome password,
web login or API encryption key - is left out: the emulated plug's are its
own. `AUTOFLASH_TASMOTA_PASSWORD`, `AUTOFLASH_ESPHOME_OTA_PASSWORD`,
`AUTOFLASH_ESPHOME_WEB_USER`, `AUTOFLASH_ESPHOME_WEB_PASSWORD` and
`AUTOFLASH_ESPHOME_API_KEY` set where the script runs go to the run instead,
for an emulated plug given them to test with. On a terminal the run is shown live;
otherwise, as in a CI log, the final screen is printed at the end, colours
and all. The recording stays in
`captures/emulation/state/work/captures/unattended/<time>/`, beside the run's
own `run.log`. The exit code is the run's: 0 when the plug was converted.

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
gets 2 GB and 2 CPUs (`AUTOFLASH_WINDOWS_RAM=3G`, `AUTOFLASH_WINDOWS_CPUS=4` to change), and
its container at most 1 GB more, for QEMU itself, without swap. The emulator
gets 256 MB and 2 CPUs (`AUTOFLASH_EMU_MEMORY=512m`, `AUTOFLASH_EMU_CPUS`).

```sh
tools/emulator/windows-test.sh --cloud                    # a conversion, in OpenConsole, as on Windows 11
tools/emulator/windows-test.sh --cloud --classic-console  # ... in the classic console, as on Windows 10
tools/emulator/windows-test.sh --cloud --first-run        # ... on a Windows that has never seen the tool
tools/emulator/windows-test.sh --cloud --fresh            # ... with the plug back to stock and no earlier run
tools/emulator/windows-test.sh --rebuild-layer=rig        # set Windows' rig layer (and tool) up again first
```

`--fresh`, `--pcap`, `--no-firewall`, `--rebuild` and `--plug=<MAC>` are `emu.sh`'s;
which plug runs is asked before Windows starts, when there are several.

The first run fetches Microsoft's **Windows Server 2025 evaluation ISO** - 8.2
GB, straight from Microsoft's Evaluation Center link, no sign-up, checked
against its pinned SHA-256, and kept - and sets Windows up from it in three
**layers**, each a disk in `~/.cache/autoflash-emu/windows` holding only its changes
to the one below:

| Layer | What it adds | Set up by | Takes |
|---|---|---|---|
| `installed` | Windows Server Core, as Setup leaves it: the user `autoflash`, who logs on by itself, and a hook that runs `start.ps1` at logon | the ISO, `unattend.xml`, `firstlogon.ps1` | 5-10 min |
| `rig` | what only the tests need: Remote Desktop, SSH, the clipboard over VNC (virtio-win's guest tools, pinned), no Windows Update or SConfig, and for the link OpenVPN's TAP driver, the "Autoflash emulator" adapter and a Python of its own, from the same `windows-bootstrap.ps1` a Windows computer uses | `rig.ps1` | a few min |
| `tool` | what the tool's own first run leaves: its portable Python - set up with progress bars on, as in a person's window, and failed if any shows | `tool.ps1` | about a min |

Every run starts from a throwaway layer on `tool`, with this checkout in
`C:\Autoflash\repo`, in about a minute. `--first-run` starts it on `rig` instead:
Windows has never seen the tool, so `start.cmd` gets its Python itself, as on
a new computer. When a layer's scripts change, the next run sets it up again
first, and every layer above it; `--rebuild-layer=installed|rig|tool` does
that anyway, from that layer up. A layer is replaced only once its set-up has
finished, so one that fails leaves the layers as they were.

Windows logs on by itself, opens the link and a PowerShell window for the
conversion - in **OpenConsole**, Windows Terminal's own console, as on Windows
11, or with `--classic-console` in Windows' classic one, as on Windows 10. Work
in it in your browser, at **http://localhost:6080** - nothing to install - or
over Remote Desktop, `localhost:3389`, user `autoflash`, password `Windows-emulator`, or
with a VNC viewer on `localhost:5900`, or over SSH (below). Only this computer can reach any of them.
Copy and paste works in all three: in the browser through the clipboard box in
noVNC's side panel (the browser keeps pages from reading the clipboard
directly; what you paste stays Windows' clipboard while the page is open), on VNC
through SPICE's agent in Windows, and on Remote Desktop with
its own clipboard (`xfreerdp /clipboard`). The browser view is there while
Windows installs and its layers are set up too. The converted plug's own page is at its
own address, **http://192.168.60.50**, as the conversion's last link gives it:
for as long as the run lasts, this computer has a route to the plug's network
through the emulator, which forwards only this computer's connections to the
plug (and still nothing to the internet without `--cloud`). It is set and
removed without sudo, and left out if this computer has a network of its own
at 192.168.60.0/24 - http://localhost:8080 works either way. The terminal you started from is the emulator's, for `plug
restart` and the serial log; leaving it shuts Windows down. The conversion's
captures are kept between runs (`~/.cache/autoflash-emu/windows/captures`), so an
unfinished one can be resumed.

#### SSH, to drive a run from a terminal

Windows runs OpenSSH's server (set up in the `rig` layer) with PowerShell as
its shell, on `127.0.0.1:2222` (`AUTOFLASH_WINDOWS_SSH_PORT` to change), logged on
to as `autoflash` with a key `windows-test.sh` makes once in
`~/.cache/autoflash-emu/windows/ssh`. `tools/emulator/windows-ssh.sh` wraps it:

```sh
tools/emulator/windows-ssh.sh --wait          # until Windows has logged on and lets the key in
tools/emulator/windows-ssh.sh                 # PowerShell, in C:\Users\autoflash
tools/emulator/windows-ssh.sh 'Get-ChildItem C:\Autoflash\repo\captures'  # any PowerShell command
tools/emulator/windows-ssh.sh log -f          # follow the conversion's latest run.log
tools/emulator/windows-ssh.sh get C:/Autoflash/repo/captures ./win-captures
tools/emulator/windows-ssh.sh put tools/autoflash_cli.py C:/Autoflash/repo/tools/
```

The key is let in at logon (`start.ps1`), so about a minute after a run
starts. SSH is a session of its own, beside the console the run opens, not
in it: what the conversion shows there is read through its `run.log`. A
conversion can also be run over SSH itself (`windows-ssh.sh`, then
`cd C:\Autoflash\repo; .\start.ps1`), in the console SSH gives it, which is neither
OpenConsole nor the classic one.

Server Core has no desktop, which is what makes it quick to install and run,
and leaves three things a person's Windows has untested here: the User
Account Control prompt (Server Core has neither the prompt nor the service
behind it, and an administrator's programs are always elevated there, so the
firewall rule is added without asking; tests/test_windows_uac.py answers it
every way instead, and a Windows 11 computer shows the real one), Windows
Security's notifications, and
Windows Terminal's window itself. Terminal cannot start on Server Core, which
lacks the XAML it draws with; OpenConsole, from the same release (pinned, 12
MB, fetched once into `~/.cache/autoflash-emu/windows`), is the same code for what
the tool's output depends on - colours, the escape sequences that redraw a
line or take a block back off the screen, wrapping - but not Terminal's tabs,
or its clickable links. Everything the tool does on the network - finding the
plug, the firewall rule, serving the firmware - is the same as on any Windows.

```
 ~/.cache/autoflash-emu/windows/
   server2025-eval.iso ─ Setup ─▶ installed.qcow2      Windows as Setup leaves it
                                    └─ rig.qcow2        + what the tests need
                                        └─ tool.qcow2   + the tool's Python
                                            └─ (a run)  thrown away when Windows shuts down
   captures/      the Windows conversion's captures, kept between runs
   windows-terminal.zip, *.built-from   OpenConsole's release; what each layer was set up from
```

The evaluation lasts 180 days from when Windows was installed; after that,
`--rebuild-layer=installed` installs it again from the kept ISO.

#### Starting over

| To get | Do |
|---|---|
| A clean Windows | nothing: every run starts from a throwaway layer, so nothing a run does - a firewall rule, a setting, a file - reaches the next one |
| The plug back to stock, and no earlier conversion to resume | `--fresh` (the plug's flash, the emulator's run, and the Windows run's captures; the eWeLink email is not offered either) |
| A Windows that has never seen the tool | `--first-run` |
| The test rig, or the tool's Python, set up again | `--rebuild-layer=rig` or `--rebuild-layer=tool`; changing their scripts does it by itself |
| Windows installed again | `--rebuild-layer=installed`, from the kept ISO |
| None of it on the computer | `docker rmi autoflash-win autoflash-emu`, and delete `~/.cache/autoflash-emu` |

Everything under `~/.cache/autoflash-emu` belongs to you, not root, though the
containers write it.

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
SHA-256), and the "Autoflash emulator" adapter - 192.168.60.10/24, no gateway, so
Windows' internet stays where it is, and the lowest interface metric, so the
tool's mDNS goes out on it. While the emulator is not running the adapter
shows as disconnected and does nothing. `windows-bootstrap.ps1 -ListPython`
shows every Python it finds, and which it would use.

The emulator window is the usual tmux session, for `plug restart` and the
serial log. To wire a Windows computer to an emulator running elsewhere,
`windows-bootstrap.ps1` without `-LocalEmulator`, then
`windows_link.py --host ADDRESS`.

From a Windows checkout the emulator keeps its state - the plug's flash, the
serial log, the in-container run's captures - in `~/.local/share/autoflash-emu/state`
inside WSL (`\\wsl$\<distribution>\home\<you>\.local\share\autoflash-emu\state` from
Windows), not in `captures/emulation/state/`. WSL reaches the Windows drive
through 9p, where one flash write can take 100 ms, and the bridge then trips its
interrupt watchdog while writing Tasmota. The Windows tool's own captures stay
in `captures/` as usual.

WSL stops its VM, and Docker with it, soon after its last window closes: with
`emu.sh --detach`, keep a WSL window open.

```
 Windows: autoflash_cli.py ── "Autoflash emulator" TAP 192.168.60.10
                                   │  windows_link.py ⇄ 127.0.0.1:7860 (length-prefixed frames)
 container autoflash-emu ──────────┼───────────────────────────────────────
   windows-link ⇄ tap1 ─┐          │
                        br0 192.168.60.1 (dnsmasq, ufw) ── tap0 ── QEMU (plug .50)
```

## How it fits together

```
 your browser ── localhost:8080 ─┐
                                 │  container autoflash-emu ("your computer")
 ┌───────────────────────────────┼──────────────────────────────────────────┐
 │  browser-forward ─────────────┘                                          │
 │  autoflash_cli.py, nmcli stand-in, wifi / plug commands                  │
 │        │                                                                 │
 │  tap0 192.168.60.1/24 ── dnsmasq (DHCP + DNS; plug fixed at .50)         │
 │        │                    └─ internet: only with --cloud (iptables)    │
 │  QEMU (lcgamboa fork) ── emulated Wi-Fi ── open AP PICSimLabWifi         │
 │        └─ flash.bin / efuse.bin in captures/emulation/state/             │
 └──────────────────────────────────────────────────────────────────────────┘
```

- **QEMU.** Espressif's QEMU has no Wi-Fi; the emulator uses
  [lcgamboa's fork](https://github.com/lcgamboa/qemu) (branch `picsimlab-esp32`),
  which emulates the ESP32-C3's Wi-Fi hardware and offers three open access
  points. Its Wi-Fi is wired to a tap device in the container. The fork's
  source at the pinned commit (`e39508f`), with the subprojects its build
  fetches, is a release asset of this repository
  ([`emulator-source`](https://github.com/adamsthws/Sonoff-OTA/releases/tag/emulator-source));
  `emu.sh` downloads it once into `captures/emulation/cache/`, checks it
  against the SHA-256 in `emu.Dockerfile`, and the image is built from it with
  no network - so building needs nothing outside this repository. QEMU is
  GPL-2.0: the archive is its unmodified source, and this project's changes
  to it are the patches in `tools/emulator/fork/`.
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
  blocks incoming connections, and sudo asks for your password (`autoflash`). So
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
- **`wifi-switch.patch` - the router can be restarted.** The fork's access
  points live inside QEMU, so blocking traffic outside it only cuts the plug
  off from the computer; its Wi-Fi stays up. While the file
  `$ESP32_WIFI_AP_OFF_FILE` exists (`plug router off`), they send no beacons
  and answer nothing, and deauthenticate the plug - a few times, and again
  whenever it sends data, as a real access point does to a station that is
  not joined. The plug sees its Wi-Fi go within a second or two, retries as
  a real one does ("Connect failed with AP timeout"), and rejoins within
  seconds of `plug router on`. The fork's frame builder leaves a frame's
  destination and signal strength unset, so the patch sets both, or the plug
  drops the frame. To cut the plug off from the computer instead, block its
  traffic in the container (`iptables -I INPUT -s 192.168.60.50 -j DROP`).
- **`wifi-to-softap.patch` - the computer can join the plug's own network,
  while the plug also tries the home one.** The fork treated a guest
  broadcasting its own network as a guest that was now only an access point:
  its virtual station probed it once (never answered), then ran a DHCP
  exchange of its own (never answered either), and the guest could not also
  be a station. Now the computer's station on the plug's network is a link of
  its own, with its own MAC (02:53:36:30:00:02), apart from the guest's
  station link with the fork's access points. It authenticates and associates
  once a second until the plug answers, passes the computer's traffic through
  untouched, and is dropped when the plug stops beaconing. The guest can run
  both at once (AP+STA), as Tasmota and ESPHome do while trying Wi-Fi details
  their setup page was given: an access point addressed by its MAC answers on
  whatever channel the radio is on, as the plug's own network keeps it on its
  channel. How far the station has got is written to
  `$ESP32_WIFI_SOFTAP_STATE_FILE`, which `wifi join` waits on.
- **`wifi-dma.patch` - frames go only where the hardware would put them.** The
  fork wrote each received frame through the next receive descriptor whether
  or not the driver had handed it over, and however big the buffer. Writing
  into buffers the driver had taken back corrupted its heap, which then failed
  in `free()` (`tlsf_free`) - seen as a crash when the plug's Wi-Fi stopped. A
  frame is now written only into a descriptor the driver owns with room for it,
  and dropped otherwise, as the hardware drops it.
- **`wifi-rssi.patch` - the signal strength has its minus sign.** The fork
  added 96 to the strength of every frame the plug received, so the -25 dBm of
  the plug's network reached the firmware as +71 to +80 dBm, and the firmware
  passes it on as it is: the conversion's report said "Plug Wi-Fi signal:
  72 dBm", where runs on the real plugs recorded -44 to -52 dBm. A frame now
  carries its access point's own strength, as the hardware gives it, plus the
  fork's jitter of up to 9 dB: -25 to -16 dBm for the plug's network.

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
  network has no password. Every setup network the conversion makes is open
  from the installer 4.4.3 on, so all can be joined (`wifi join NAME`, and
  `wifi home` to go back); an earlier release's installer, with WPA2, cannot.
- **No power meter.** The CSE7766 is not emulated, so readings are absent and
  the end-of-run check reports the meter unchanged.
- **One plug, one region.** The emulator runs whatever the dump came from. A
  dump of another model or region (with that owner's account) is needed to
  emulate it.

## Debugging

- **The serial log** is `captures/emulation/state/logs/uart.log`, redacted by
  `tools/emulator/redact.py`, which masks the dump's secrets and drops panic
  memory dumps (they show RAM as words the masking cannot see into). Put any
  other log through it before reading it.
- **A QEMU of your own build**: `AUTOFLASH_EMU_QEMU=path/to/qemu-system-riscv32
  tools/emulator/emu.sh ...` runs it in place of the image's, without
  rebuilding the image (`qemu-build.Dockerfile` builds one from a fork
  checkout with the patches applied). Patches in `tools/emulator/fork/` apply
  in name order, each on top of those before it.
- **The QEMU monitor** is at `/run/autoflash-emu/monitor.sock` in the container.
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
