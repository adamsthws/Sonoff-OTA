# How the tool is tested

Three layers, each catching what the one before cannot:

1. **Unit tests** - fast, no plug: the pieces on their own.
2. **The emulator** - a virtual S60 running the plug's real Sonoff firmware
   ([emulator.md](emulator.md)): whole conversions, and every fault the
   matrix below puts in their way.
3. **Real plugs** - what no emulator shows: real flash, real radios, real
   timings.

## Unit tests

```sh
python3 -m unittest discover -s tests
```

Unit tests complete in under a minute. They cover the decisions the tool makes - which
route to take, what to ask, when to try again, what counts as done - with the
plug and the network stood in for; and the parts that have to be exact - the
plans the plug carries out, the update protocols, image checks and pins, what
is kept and what never is. Every bug found below has a test that fails without
its fix.

## The emulator

A conversion with nobody at the keyboard ([unattended.md](unattended.md)):

```sh
tools/emulator/unattended-test.sh --fresh --plug=MAC -- --firmware esphome
```

Several run side by side with `--instance=N` (1-30), each its own container,
plug and network. Only the eWeLink stages (up to C2) cannot: every emulated
plug is a copy of the same real one, and eWeLink keeps one connection per
plug - so those run one at a time.

A plug can be kept as a run left it, with what this computer recorded about
it (its backup, its runs), and started from again:

```sh
tools/emulator/unattended-test.sh --fresh --plug=MAC --save=on-tasmota -- --firmware tasmota
tools/emulator/unattended-test.sh --from=on-tasmota --plug=MAC -- --firmware esphome
```

That is how every direction is tested: from a plug the tool converted, as a
person converting it again would have.

## The fault matrix

`tools/emulator/fault_matrix.py` puts every fault in the way of every step, in
every direction:

- **Directions:** Sonoff to Tasmota, Sonoff to ESPHome, Tasmota to ESPHome,
  Tasmota to Sonoff, ESPHome to Tasmota, ESPHome to Sonoff.
- **Steps:** each step that writes to the plug or waits on it - 46 in all.
- **Faults** (16):
  - the network gone (the router off) for 1, 5, 15, 30, 60, 120 or 180 s;
  - the plug's power off for 1, 5, 30 or 180 s;
  - the computer crashing - the tool and what it serves killed outright -
    and started again 1, 60 or 180 s later;
  - Ctrl-C, then carrying on;
  - Ctrl-C, then starting again from the beginning (`--new`).

Each scenario converts a saved plug and, when the step under test has been
going for about 40% of its usual time, brings the fault. Then the tool is run
again, as a person would - switching the plug off and on where the run says
to - until the plug is converted, or four runs have failed. A scenario
**passes** when the plug ends up converted and checked: Tasmota read back
against the official files, ESPHome answering and confirmed, Sonoff's firmware
back on the network.

```sh
tools/emulator/fault_matrix.py --plug=MAC prep        # the starting plugs (eWeLink, one at a time)
tools/emulator/fault_matrix.py --plug=MAC run 5       # on 5 emulators, C2 on a sixth
tools/emulator/fault_matrix.py --plug=MAC rerun 5     # what has not passed yet, again
tools/emulator/fault_matrix.py --plug=MAC subset 5    # the backup and D1 faults, shortest and longest
tools/emulator/fault_matrix.py report
```

Results, a log per scenario and what each failure leaves (its run records and
the plug's serial output) are kept in `captures/emulation/matrix/`.

**How many at once.** Each emulator converting takes about half a CPU core
and 120 MB. Past one per core the plugs slow down enough for their timeouts to
fail where nothing is wrong, so the load is kept at or under the number of
cores.

**The cloud, one plug at a time.** Every emulated plug is a copy of the same
real one, and eWeLink keeps one connection per plug. So a plug's way to the
internet is open only while its run may need the cloud - to send the
installer - and shut for the rest, and runs take turns at that: from the
start until the plug is downloading the installer from this computer. The
Sonoff directions start from a plug saved just after the installer was sent,
switched off; it starts Sonoff's firmware again, as the installer means it
to, so each run sends the installer again first.

### What it cannot show

- **ESPHome's hotspot.** ESPHome away from its network for 90 s raises its
  own hotspot, and goes on trying its network every 30 s (its source says so);
  the emulator's Wi-Fi cannot be both at once, so a plug there never rejoins.
  Those scenarios are marked as the emulator's limit, not passed.
- **The installer's own network.** Away from its network for more than 45 s,
  the installer raises a network of its own, which the emulator cannot join
  (it has no WPA2). Longer outages during the backup or D1 are marked as the
  emulator's limit too.
- **A plug gone back to Sonoff's firmware, during the backup or D1.** A power
  cut or a fault there sends the plug back to Sonoff's firmware, and the run
  sends the installer again - once it sees Sonoff's firmware. In the emulator,
  Sonoff's firmware does not open its local port again after such a fault, so
  the run cannot see it. Those scenarios are left out (untested, not passed)
  until a real plug shows what it does.
- **Timings.** The emulated plug is slower than a real one, and its flash
  never wears or fails: the one real moment of risk, a power cut while the
  bootloader or partition table is written (well under a second), is not
  something it can measure.

### Results

On 16-17 October 2026, on the code as of `e3ae70e`, the latest result of each
scenario:

| Direction | Scenarios | Passed | Emulator's limit | Failed | Untested |
|---|---:|---:|---:|---:|---:|
| Sonoff to Tasmota | 192 | 161 | - | - | 31 |
| Sonoff to ESPHome | 112 | 90 | - | - | 22 |
| Tasmota to ESPHome | 48 | 48 | - | - | - |
| Tasmota to Sonoff | 112 | 112 | - | - | - |
| ESPHome to Tasmota | 176 | 175 | 1 | - | - |
| ESPHome to Sonoff | 96 | 96 | - | - | - |
| **All** | **736** | **682** | **1** | **-** | **53** |

The emulator's limit is ESPHome's hotspot after a 180 s outage. The untested
are the faults during the backup and D1 (above).

### Still open

- **Back on Sonoff's firmware, during the backup or D1.** Whether a real plug
  opens its local port again, so the run sees it and sends the installer
  again, or the run needs eWeLink to tell it the plug is back. A real plug
  switched off during the backup answers it.
- **The first send after a restart.** Switched off and on after a send cut
  off part way, the plug came back to eWeLink but ignored the first update
  command; the next run's went through. A run could send again by itself when
  nothing was downloaded - but a firewall in the way looks the same.
- **An upload that hung.** Once in all the runs, official Tasmota's upload to
  the startup firmware (F2) started and never finished; the run waited ten
  minutes, then asked for it by hand. Not seen again; F2 could restart the
  plug and try once more, as D4 now does.

### What it has found

Each fixed, with a test, and the scenario run again:

- Asked for the firmware the plug already ran (a run stopped during its last
  check, then started from the beginning), a run said it had crashed, and went
  on saying so.
- ESPHome sent as an update confirms itself only after a minute running; a
  power cut before then took the plug back to the firmware before - which the
  run then waited on for good, or left the plug on after saying it was done.
- A safety lock moved aside while a step ran was not put back when the
  computer itself stopped, and every later run failed.
- Sonoff's firmware, written back, does not try its network again if it was
  gone while it first started: the run now says to switch the plug off and on.
- A plug restarting, or left on the startup firmware, was missed by a run
  started from the beginning, and taken for one still on Sonoff's firmware.
- A plug switched off and on after the installer was sent goes back to
  Sonoff's firmware, as the installer means it to; the run then waited for
  the installer for a quarter of an hour, and stopped. It now sends it again.
- Started again from the beginning after the layout was rebuilt for Tasmota,
  a run took the startup firmware for the ESPHome route's, on Sonoff's
  layout, and sent the plug round steps it could never finish.
- Tried again after an upload of the interim Tasmota cut off by the network
  (D4), the next upload hung without a word: Tasmota still held the cut one.
  The plug is now restarted first.
- On a busy plug, setting Tasmota up (F4) gave up on a slow answer, then
  restarted the plug again on every try, and left the next step asking a
  plug not answering yet - which asked the person for the Wi-Fi.
- With this computer stopped part way through sending the installer, the
  plug stays away from eWeLink; the run refused at once and blamed the
  firewall. It now waits for it, then says to switch it off and on.
- A power cut while the memory layout was rebuilt for Tasmota cost Tasmota
  its settings - the rebuild wrote over where it kept them - so the plug came
  back waiting on its setup network, and the next run stopped for good. The
  settings are now moved aside first; cuts timed off the plug's own output at
  each point of the rebuild, then each way on from there (Tasmota, ESPHome,
  Sonoff's firmware), all came back with the settings and were converted -
  and the run now carries on by itself, trying the commit up to three times.

## Real plugs

Real plugs are converted with each one powered through a second smart plug,
so its power can be cut on purpose. So far, Tasmota to ESPHome and back has
been run on a real S60TPG: every write read back, the final check of the
Tasmota plug matching all four regions, and the relay and power meter checked
through each firmware. Going back to Sonoff's firmware has been tested on the
emulator only: neither test plug has its backup on the computer used.

## Related

[Fixes sent upstream](upstream-contributions.md): the bugs this testing
found in other projects - Tasmota, arduino-esp32, ESP-IDF and others - and the
fixes sent to them.
