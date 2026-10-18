# How the tool is tested

Three layers, each catching what the one before cannot:

1. **Unit tests** - fast, no plug: the pieces on their own.
2. **The emulator** - a virtual S60 running the plug's real Sonoff firmware
   ([emulator.md](emulator.md)): whole conversions, and every fault the
   suite below puts in their way.
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

## The fault suite

`tools/emulator/fault_suite.py` puts every fault in the way of every step, in
every direction - 1,453 scenarios:

- **Directions:** Sonoff to Tasmota, Sonoff to ESPHome, Tasmota to ESPHome,
  Tasmota to Sonoff, ESPHome to Tasmota, ESPHome to Sonoff.
- **Steps:** each step that writes to the plug or waits on it.
- **Faults:**
  - the network gone (the router off) for 1, 5, 15, 30, 60, 120 or 180 s, or
    weak - one packet in ten lost - for a minute;
  - the plug's power off for 1, 5, 30 or 180 s;
  - the computer crashing - the tool and what it serves killed outright -
    and started again 1, 60 or 180 s later; asleep for 2 or 10 minutes; or
    switched off for 30 s or 2 minutes, and its run started again;
  - Ctrl-C once, twice apart, or at the question it asks; Ctrl-C twice, then
    carrying on, starting again from the beginning (`--new`), or choosing the
    other firmware; the terminal window closed;
  - a packet changed on its way, its checksum made good, so that only the
    tool's and the plug's own checks can tell;
  - a second run started while the first works, which must be refused
    without disturbing it;
  - the disk filling during the backup;
  - the plug falling back to its own setup network, and the person joining
    it before being asked: the run must find the plug there and carry on by
    itself.
- **When:** at 40% of the step's usual time; at the riskiest steps also at 10%
  and 90%, and two faults running.

Each scenario converts a saved plug and brings the fault while the step under
test is under way - checked from the run's own log, or marked MISTIMED. Then
the tool is run again, as a person would - switching the plug off and on, or
joining the plug's own network, where the run says to - until the plug is
converted, or four runs have failed. A scenario **passes** when the plug ends
up converted and checked - Tasmota read back against the official files,
ESPHome answering and confirmed, Sonoff's firmware back on the network - and
the run left no firewall rule or helper behind.

```sh
tools/emulator/fault_suite.py --plug=MAC prep           # the starting plugs (eWeLink)
tools/emulator/fault_suite.py list > all.txt            # every scenario, cloud or not
S60_CLOUD_WORKERS=3 S60_ANY_WORKERS=3 \
    tools/emulator/fault_suite.py --plug=MAC batch all.txt   # what has not passed yet
tools/emulator/fault_suite.py report
```

Results, a log per scenario and what each failure leaves (its run records and
the plug's serial output) are kept in `captures/emulation/suite/`.

**Several machines.** The scenarios that do not need eWeLink (every step after
D1) run on any machine with Docker: copy the repository and the saved plugs
(`captures/emulation/saved/`), give each machine part of the list, and set
`S60_NO_CLOUD=1` there - only one machine may let its plugs reach eWeLink.

**How many at once.** Each emulator converting takes about half a CPU core
and 120 MB. Past one per core the plugs slow down enough for their timeouts to
fail where nothing is wrong, so the load is kept at or under the number of
cores.

**The cloud, one plug at a time.** Every emulated plug is a copy of the same
real one, and eWeLink keeps one connection per plug, so two online at once
knock each other off. A plug's way to the internet is shut - its connections
reset at both ends, so eWeLink lets go of them at once - and opened only for a
turn: when its run is about to need the cloud (the eWeLink steps, or the
installer sent again), until the plug has downloaded the installer and
started it - typically 30-60 s: Sonoff's firmware that loses eWeLink part way
through the download gives it up. Its run ending gives the turn
up too; a run after it asks again. Turns are given by how long the run can
wait: finding the plug in eWeLink first, then a plug back on Sonoff's
firmware part way, then the rest, and last a C2 scenario's plug, whose turn
starts before its run does. Between two plugs' turns there is a short pause
(5 s) while eWeLink lets go of the last one. The suite can feel out how many
at once the cloud allows - one more at a time while the cloud scenarios pass,
or one fewer at each trouble - and lengthen the pause when eWeLink is the
trouble (`S60_CLOUD_MOST`, `S60_CLOUD_BACK_OFF`, `S60_CLOUD_PAUSE_UP`).

### What it cannot show

- **Timings.** The emulated plug is slower than a real one, and its flash
  never wears or fails: the one real moment of risk, a power cut while the
  bootloader or partition table is written (well under a second), is not
  something it can measure.
- **Radio.** Its Wi-Fi is a network cable with a Wi-Fi driver on top: a weak
  signal is lost packets, not a plug at the edge of range.

Every network a plug raises of its own - Tasmota's, ESPHome's, the mover's and
the installer's - can be joined from the emulated computer, as a person's
laptop or phone would join it ([emulator.md](emulator.md)).

### Results

On 16-17 October 2026 the fault matrix this suite grew from (its first 736
scenarios) ran every one, and on 17 October the
faults during the backup and D1 in the Sonoff directions again, the shortest
and longest of each kind (32), on the code as of `e0a8cd3`. The latest result
of each scenario:

| Direction | Scenarios | Passed | Emulator's limit | Failed | Untested |
|---|---:|---:|---:|---:|---:|
| Sonoff to Tasmota | 192 | 176 | 6 | - | 10 |
| Sonoff to ESPHome | 112 | 102 | 6 | - | 4 |
| Tasmota to ESPHome | 48 | 48 | - | - | - |
| Tasmota to Sonoff | 112 | 112 | - | - | - |
| ESPHome to Tasmota | 176 | 175 | 1 | - | - |
| ESPHome to Sonoff | 96 | 96 | - | - | - |
| **All** | **736** | **709** | **13** | **-** | **14** |

The emulator's limit is ESPHome's hotspot after a 180 s outage, and the
installer's own network after outages longer than 45 s during the backup and
D1. The untested are the in-between lengths of the faults during the backup
and D1 (5 and 15 s outages, 5 and 30 s power cuts, the computer back after
60 s), left out of the 32 above.

### Still open

- **The first send after a restart.** Once, switched off and on after a send
  cut off part way, the plug came back to eWeLink but ignored the next update
  command; the run after it went through. Not seen again in six rounds of the
  same, sending 0, 30 and 90 s after the restart.
- **Real plugs.** The faults during the backup and D1 have been tried on the
  emulator only; a real plug switched off during the backup, and during D1,
  would show the same as the emulator does.

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
- A power cut, or Ctrl-C then a run started again, during D1 left a plug
  with no network at all: the installer refused to start once Sonoff's slot,
  half written, was no longer bootable - and only a serial cable could
  recover it. Installer 4.4.3 starts regardless; a run finds the plug on it
  and carries on; and an upload to it that stops is tried again by itself.
- A Wi-Fi outage just as the plug started Sonoff's firmware again left it off
  the Wi-Fi for good, and the run said only to wait. It now says to switch
  the plug off and on when nothing has answered for two minutes.
- Once, official Tasmota's upload to the startup firmware (F2) began and never
  finished. A stall is now noticed after two minutes and the upload tried
  again from a restart, three times in all - checked by cutting the traffic
  mid-upload, once and twice running.
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
