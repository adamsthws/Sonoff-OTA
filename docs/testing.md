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
python3 tests/run.py
```

That runs them side by side, in about 11 seconds on six cores, where
`python3 -m unittest discover -s tests` - the same tests, one after another -
takes a minute: most of their time is spent waiting, not computing. It shows
only what fails, with what the test printed; `python3 tests/run.py test_output`
runs some, and `--shuffle` runs them in a random order, to find a test that
passes only after another.

They cover the decisions the tool makes - which
route to take, what to ask, when to try again, what counts as done - with the
plug and the network stood in for; and the parts that have to be exact - the
plans the plug carries out, the update protocols, image checks and pins, what
is kept and what never is. Every bug found below has a test that fails without
its fix. The `emulator-check` workflow runs them on Python 3.12.10 - the
portable Python the start scripts install, which most conversions run on -
and, beside it, on 3.10, the oldest the tool runs on: a Mac's own Python may
be older than 3.12, and so may one already installed where the portable one
cannot be had.

### Side by side, in any order

`tests/run.py` runs each test in its own turn on one of many processes, in
an order unittest never uses, so a test must pass whatever ran before it in
its process and whatever runs beside it in the others. A test still running
after 60 seconds (`--timeout`; the longest takes 10) is stopped, reported
with where each of its threads was, and the run fails, rather than waiting
for ever.

On 2026-10-24 that was tested to exhaustion: 2,000 runs in a row passed,
each in a new random order, on Python 3.10, 3.12 and 3.14 and on 2 to 64
processes, after five problems the first hundred runs found had been fixed:

| Found | What it was | Fixed by |
|---|---|---|
| A test saw a sleep hold another had just taken | the awake tests check the computer's systemd inhibitors, which one takes while another checks none is left | running them as one (`TOGETHER` in `tests/run.py`) |
| "recovery_safeboot size changed" | bundle tests left the recovery image in memory, for the next test's bundle | each starting and ending with none |
| A wait of -1,000,000 s | a question left unanswered by one test was taken as the next's wait | `output.start()` beginning with no question pending |
| "Address already in use" | a port picked free, let go, then taken by another test before the server bound it | the server started again on another |
| A clean-up waiting for ever, once | a test server's `shutdown()`; never seen again, so not known | the time limit, which would now show where it was |

Writing a test that keeps it so:

- **Leave nothing behind** in a module, a class or the environment: patch it
  for the test (`mock.patch...start()` with `addCleanup`), and put back what
  the code under test keeps for the life of a process.
- **Keep a port** picked as free: a server picked one for is started again
  on another if it cannot bind; a check that nobody answers keeps the socket
  bound, not listening - refused at once, and no one else's meanwhile.
- **Nothing of the whole computer's** - its sleep inhibitors, a fixed port, a
  file outside the test's own temporary directory - unless the tests sharing
  it are in `TOGETHER`, with why.

To soak them again, after a change to how tests share anything:

```sh
for i in $(seq 200); do python3 tests/run.py --shuffle -j 24 > soak.txt 2>&1 || { grep -m1 seed soak.txt; break; }; done
```

stops at the first failure and says its seed: `--seed N` with the same `-j`
runs that order again.

### How much they test

Neither measure below is a gate, and no number is a target: a test written
to raise one tends to test the stand-ins, not the tool. They say where to
look - a decision no test takes both ways, or a check whose reason no test
asks - and a person decides what is worth a test there. What only the
emulator or a plug can show is tested there.

**Coverage** says what the tests run. The `emulator-check` workflow writes
it into its summary, by file; to see it here, with what each file misses:

```sh
.venv/bin/python -m coverage run --branch --source=tools -m unittest discover -s tests
.venv/bin/python -m coverage report --sort=cover -m
```

`--branch` counts each way an `if` can go, not only each line: a line that
ran once counts as covered however its condition went.

**Mutation testing** says whether the tests would notice a mistake. A tool
([mutmut](https://mutmut.readthedocs.io/)) changes the code one small way at
a time - `>` to `>=`, `or` to `and`, an error's message to `None` - and runs
the tests on each change. A change some test fails on is caught; one they
all still pass is a mistake nobody would see. It takes minutes and asks for
judgement, so it is run by hand, on a module that decides something costly,
after its tests are written - never in a hook. It runs in a copy of the
checkout, from its own virtual environment (mutmut needs pytest, which
runs unittest's tests as they are):

```sh
work=$(mktemp -d)
git archive HEAD | tar -x -C "$work"
python3 -m venv "$work/.venv"
"$work/.venv/bin/pip" install mutmut==3.8.0 pytest
cat > "$work/pyproject.toml" <<EOF
[tool.mutmut]
source_paths = ["tools"]
only_mutate = ["tools/autoflash/plans.py"]
pytest_add_cli_args_test_selection = ["tests/autoflash/test_plans.py", "tests/autoflash/test_autoflash.py"]
also_copy = [$(git ls-files | cut -d/ -f1 | sort -u | grep -vx -e tools -e tests | sed 's/.*/"&"/' | paste -sd, -)]
EOF
cd "$work" && .venv/bin/mutmut run && .venv/bin/mutmut results
```

`only_mutate` is the module; the tests are every file that uses it
(`grep -l` its name in `tests/`) - one left out leaves the code only it
reaches "no tests". `mutmut results` lists each change that survived, and
`mutmut show NAME` what it changed. Some can never be caught, because they
change nothing: in `Plan.check`, `length <= 0` and `length <= 1` never
differ, every length being whole sectors. Say so in the tests' commit, so the
next run's reader need not work it out again.

On 2026-10-27, `plans.py`, its lines 92% covered, had 144 of 634 changes
survive - 31 in `Plan.check`, whose refusals were tested only as some
`PlanError`, and its limits only past them. Its tests now ask each
refusal's reason and each limit from both sides; 117 survive, most in the
plans it builds, which the emulator runs whole.

## Linters

```sh
tools/setup-dev               # once: creates .venv, installs the linters (and test helpers)
python3 tools/lint.py all
```

Before the tests, what is likely wrong in the code as written. The
pre-commit hook (`python3 tools/leak_scan.py install` sets it up) runs them on
the files a commit changes, and the `lint` workflow on every file:

| Linter | On | Looks for |
|---|---|---|
| [ruff](https://docs.astral.sh/ruff/) | Python (`ruff.toml`) | undefined or unused names, likely bugs, a text file opened without `encoding=`, syntax newer than Python 3.10 |
| [shellcheck](https://www.shellcheck.net/) | shell scripts | unquoted expansions, mistakes in tests and traps, what `sh` does not have |
| [actionlint](https://github.com/rhysd/actionlint) | the workflows | wrong keys, expressions and types; shellcheck on each `run:` script |
| [zizmor](https://docs.zizmor.sh/) | the workflows, `dependabot.yml` | what an attacker could use: write access a job does not need, a value expanded into a script, a token left in the checkout; with `GH_TOKEN` set (the workflow sets it), a pinned commit that is not the action's own, or a version with an advisory |
| [PSScriptAnalyzer](https://learn.microsoft.com/powershell/utility-modules/psscriptanalyzer/overview) | PowerShell (`PSScriptAnalyzerSettings.psd1`) | syntax Windows PowerShell 5.1 does not have, which every Windows runs `start.ps1` with; likely mistakes |
| `esphome config` | the ESPHome configs `firmware.json` builds | what ESPHome would refuse to build, checked by the ESPHome image the next build uses (Docker) |

The linters come from `requirements/lint.txt`, pinned with their hashes,
but for two: PSScriptAnalyzer needs PowerShell 7.4.6 or newer (`pwsh`), then
`pwsh -Command Install-Module PSScriptAnalyzer -Scope CurrentUser`; `esphome
config` needs Docker, and pulls the image the first time. A
commit on a computer without one is not stopped - the hook says it skipped
it, and the workflow runs it - and `git commit --no-verify` skips them all.
`archive/` is not linted: it is kept as it was.

## The emulator

A conversion with nobody at the keyboard ([unattended.md](unattended.md)):

```sh
tools/espressif/emulator/unattended-test.sh --fresh --plug=MAC -- --firmware esphome
```

Several run side by side with `--instance=N` (1-30), each its own container,
plug and network. Only the eWeLink stages (up to C2) cannot: every emulated
plug is a copy of the same real one, and eWeLink keeps one connection per
plug - so those run one at a time.

A plug can be kept as a run left it, with what your computer recorded about
it (its backup, its runs), and started from again:

```sh
tools/espressif/emulator/unattended-test.sh --fresh --plug=MAC --save=on-tasmota -- --firmware tasmota
tools/espressif/emulator/unattended-test.sh --from=on-tasmota --plug=MAC -- --firmware esphome
```

That is how every direction is tested: from a plug the tool converted, as a
person converting it again would have.

## The fault suite

`tools/espressif/emulator/fault_suite.py` puts every fault in the way of every step, in
every direction - 1,380 scenarios:

- **Directions:** Sonoff to Tasmota, Sonoff to ESPHome, Tasmota to ESPHome,
  Tasmota to Sonoff, ESPHome to Tasmota, ESPHome to Sonoff.
- **Steps:** each step that writes to the plug or waits on it.
- **Faults:**
  - the network gone (the router off) for 5, 15, 30, 60, 120 or 180 s, or
    weak - one packet in ten lost - for a minute;
  - the plug's power off for 1, 5, 30 or 180 s;
  - the computer crashing - the tool and what it serves killed outright -
    and started again 1 or 180 s later (and 60 s, where the plug raises a
    network of its own); asleep for 10 minutes (and 2, at the riskiest steps
    and where the plug raises a network of its own); or switched off for 30 s
    or 2 minutes, and its run started again;
  - Ctrl-C once; Ctrl-C twice, then carrying on, starting again from the
    beginning (`--new`), or choosing the other firmware; the terminal window
    closed;
  - a packet changed on its way, its checksum made good, so that only the
    tool's and the plug's own checks can tell - at each step but R2, which
    only sees that Sonoff's firmware takes a connection, and sends nothing
    to change;
  - the tool's own handling, the same at every step, so tried at the riskiest
    steps and where each firmware is first written from (C2, L1, X1): a second
    run started while the first works, which must be refused without
    disturbing it; Ctrl-C twice apart, or at the question it asks;
  - the disk filling during the backup;
  - the plug falling back to its own setup network, and the person joining
    it before being asked: the run must find the plug there and carry on by
    itself.
- **Faults that take two things at once** (`composite_scenarios()`):
  - Tasmota sent through the installer's own network (the home Wi-Fi gone
    from the backup), and then the run cut off just after the upload, or the
    computer back on the home network during the upload or while Tasmota is
    given the Wi-Fi (`ownnet-cut`, `ownnet-dropupload`, `ownnet-dropsetup`);
  - the installer's answer to the upload never arriving, though the upload
    took (`lostanswer-once`);
  - the plug switched off and on 7 times, which erases a Tasmota's settings:
    at D4, F4 and once converted (G1), and when the run advises it, for a
    Tasmota that cannot join the home Wi-Fi (`seven-times`, `seven-advised`);
  - F4's Berry driver refused, so F4 falls back to two restarts
    (`nohook-run`), and a power cut or crash at F4's reset itself, at 10, 50
    and 90% of it (step `F4rs`); and that fallback cut off by a second's
    power cut as the plug restarts to save the template it sent (`nohook-cut`,
    step `F4tp`), which can leave it without one.
- **Runs left by an earlier release** (`legacy-v1.2.0`): release 1.2.0 itself
  - its tagged files, with only `tools/espressif/emulator/legacy/v1.2.0.patch` on top,
  which points it at the cloud stub - converting a plug fresh from Sonoff,
  killed at 40% of each of its steps; then your checkout's tool over the same
  captures, as a person who updated their checkout would run it. And a plug
  1.2.0 converted to Tasmota, then taken to ESPHome or back to Sonoff (step
  `start`). And 1.2.0 killed at F4, then the plug switched off part way
  through G1 of the run after (`legacy-v1.2.0+G1off`): G1 writes over 1.2.0's
  startup firmware without checking it, which it cannot rebuild, and the next
  run must carry on after the sectors already written. And the same for the
  release before the firmware the tool installs last changed (`legacy-v2.0.2`),
  whose runs - their bundle pinned to the firmware before - your checkout
  must finish with its own: killed at each of its steps, to Tasmota and to
  ESPHome, and each kind of plug it converted, taken on from there
  ([updating-firmware.md](updating-firmware.md)). `unattended-test.sh
  --code=DIR` runs the tool from another tree.
- **A converted plug left on Tasmota's own safeboot** (`lostapp-factory`): an
  ESPHome factory image - bootloader, partition table and app, for a serial
  flasher - uploaded through Tasmota's page, as a person once asked about
  did: Tasmota switches to its safeboot for a file that big, which writes it
  over Tasmota, and the plug starts the safeboot from then on. Then the tool,
  to Tasmota, ESPHome or Sonoff. And one only switched to its safeboot, its
  Tasmota unharmed (`lostapp-switched`), which must be started again, never
  written over (FAIL-REPLACED).
- **A converted plug that lost its template** (`notemplate-generic`): the
  saved Tasmota given Tasmota's generic template and an MQTT host, as a person
  might, so it switches nothing - as a user's plug did. Then the tool, to
  Tasmota, ESPHome or Sonoff: it must know the plug for one that lost its
  template (FAIL-NOT-UNTEMPLATED), never send it the hub's steps, whose first
  is an ESPHome update no Tasmota takes (FAIL-X1-ON-TASMOTA), and staying on
  Tasmota, give it the template alone, the MQTT host kept
  (FAIL-SETTINGS-LOST).
- **A converted plug in a boot loop** (`bootloop-fallback`, to Tasmota):
  Tasmota started a sixth time in a row within 10 s of the last start goes
  over to its fallback module - Generic, unless the plug's template is set
  there - and switches nothing, as a user's plug did. The plug is put on its
  fallback module just as Tasmota would, since the emulator loses Tasmota's
  count of those starts at every restart; it must still run its template
  (BAD-TEMPLATE). Restarting it for real would also cost its settings:
  Tasmota counts each start that ends before 8 s as a quick power cycle, and
  at the seventh erases everything, its Wi-Fi too.
- **A converted plug in ESPHome's safe mode** (`safemode-ten`): the saved
  ESPHome switched off as soon as it answers, until it starts in safe mode -
  ESPHome stopped within a minute of starting ten times in a row does, its
  count kept in flash, so the emulator keeps it too. Safe mode has only Wi-Fi
  and the update service for five minutes, as the mover has. Then the tool,
  to ESPHome, Tasmota or Sonoff: it must know the plug for ESPHome in safe
  mode and wait for it (FAIL-TAKEN-FOR-MOVER), never take it for one left
  part way - to stay on ESPHome, that sent it the startup firmware.
- **A Berry phase riding out the network by itself** (`blips-3`,
  `lossy-phase`, at E2, G1, I1, L1 and R1): two 3 s outages from 10% to 90%
  of the phase - further apart than its 20 s of retrying - or one packet in
  ten lost for all of it. The plug must convert without the tool trying the
  step again: a phase given up and started over from its first block passes
  everywhere else, but is FAIL-RETRIED here. Each scenario also keeps how
  long each phase kept the plug busy (`plug_busy`), and `report` lists the
  longest: a real S60TPG once stopped answering after 70-90 s busy, though an
  S60TPF in the test reports was busy for two minutes without harm.
- **When:** at 40% of the step's usual time; at the riskiest steps also at 10%
  and 90%, and two faults running.

Each scenario converts a saved plug and brings the fault while the step under
test is under way - checked from the run's own log, or marked MISTIMED. Then
the tool is run again, as a person would - switching the plug off and on, or
joining the plug's own network, where the run says to - until the plug is
converted, or four runs have failed. A scenario **passes** when the plug ends
up converted and checked - Tasmota read back against the official files,
ESPHome answering and confirmed, Sonoff's firmware back on the network - and
the run left no firewall rule or helper behind. It is **BAD-NAME** when the
plug went by a name it never should (the names are made from its MAC:
[device-names.md](device-names.md)), on the home network or as a network of
its own, or did not end under its converted name; every name it had is kept
with the result. It is **FAIL-UNEXPECTED** when a run stopped with
"something unexpected went wrong" - an error the tool did not see coming, a
bug whatever the fault - even if the next run converts the plug. It is
**BAD-TEMPLATE** when a plug converted to Tasmota does not answer with the
plug's template - a Tasmota that switches nothing; **FAIL-ASKED-AGAIN** when a
run asked for the go-ahead twice, as a step tried again by itself once did;
and **FAIL-BACKUP-LINE** when a run from a converted plug reported no Sonoff
backup though the run that converted it took one.

The emulated computer's list of networks keeps one the plug put out for a
minute after it has gone or been renamed, as a real computer's does (the
emulator's `nmcli`; a number of seconds in `/run/autoflash-emu/scan-cache`
in the container changes it, 0 for only what is on air now). A run then sees
what a person would: the installer's network still listed as Tasmota starts,
or `ESP_` and the access-point MAC for a moment as Tasmota names its own.

To run the suite on another plug - so that names made from the MAC can be
seen to follow it - make a test copy of a backed-up one with another MAC, and
give that to `--plug`; its saved starting plugs are its own (the MAC's last
four digits after the name):

```sh
python -I tools/espressif/emulator/copy_plug.py --from MAC --to 12:34:56:ab:cd:ef
python -I tools/espressif/emulator/copy_plug.py --remove 12:34:56:ab:cd:ef
```

```sh
tools/espressif/emulator/fault_suite.py --plug=MAC prep           # the starting plugs (eWeLink)
tools/espressif/emulator/fault_suite.py list > all.txt            # every scenario, cloud or not
AUTOFLASH_CLOUD_WORKERS=3 AUTOFLASH_ANY_WORKERS=3 \
    tools/espressif/emulator/fault_suite.py --plug=MAC batch all.txt   # what has not passed yet
tools/espressif/emulator/fault_suite.py report
```

Add `--stub-cloud` to `prep` and `batch` to give each emulator its own local
eWeLink stand-in instead of the shared real cloud: no cloud lock, so every
scenario runs in parallel on `AUTOFLASH_STUB_WORKERS` emulators, and no eWeLink
account is needed. Its starting plugs (`stub-suite-*`) and results
(`captures/emulation/suite-stub/`) are its own: a stub plug's flash is the
other kind, and a stub run's passes are not the real cloud's. See
[cloud-stub.md](cloud-stub.md).

```sh
AUTOFLASH_STUB_WORKERS=3 tools/espressif/emulator/fault_suite.py --plug=MAC --stub-cloud prep
AUTOFLASH_STUB_WORKERS=3 tools/espressif/emulator/fault_suite.py --plug=MAC --stub-cloud batch all.txt
```

Results, a log per scenario and what each failure leaves (its run records and
the plug's serial output) are kept in `captures/emulation/suite/`.

**Several machines.** The scenarios that do not need eWeLink (every step after
D1) run on any machine with Docker: copy the repository and the saved plugs
(`captures/emulation/saved/`), give each machine part of the list, and set
`AUTOFLASH_NO_CLOUD=1` there - only one machine may let its plugs reach eWeLink.

**How many at once.** Each emulator converting takes about half a CPU core
and 120 MB. Past one per core the plugs slow down enough for their timeouts to
fail where nothing is wrong, so the load is kept at or under the number of
cores. A batch can be scaled while it runs: started with
`AUTOFLASH_MOST_WORKERS` above its workers, only the number written in
`workers` in its results directory take a scenario - raised, more start; lowered,
the last ones stop once their scenario ends.

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
trouble (`AUTOFLASH_CLOUD_MOST`, `AUTOFLASH_CLOUD_BACK_OFF`, `AUTOFLASH_CLOUD_PAUSE_UP`).

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

On 22 October 2026 the suite ran every scenario on 2.0.1, against the cloud
stub, on four machines at once - 22 emulators, about five hours. Every
failure was looked into, the cause fixed, and the scenario run again on the
fixed code. The latest result of each:

| Direction | Scenarios | Passed | A phase's retries spent | Nothing to corrupt |
|---|---:|---:|---:|---:|
| Sonoff to Tasmota | 369 | 368 | 1 | - |
| Sonoff to ESPHome | 206 | 204 | 2 | - |
| Tasmota to ESPHome | 83 | 82 | 1 | - |
| Tasmota to Sonoff | 199 | 198 | - | 1 |
| ESPHome to Tasmota | 318 | 317 | 1 | - |
| ESPHome to Sonoff | 168 | 167 | - | 1 |
| Tasmota to Tasmota | 2 | 2 | - | - |
| **All** | **1,345** | **1,338** | **5** | **2** |

Five had the Wi-Fi drop more than once in one Berry phase: the phase's 20
seconds of retries are counted from its first failure and never begun again,
so a later drop found them spent and the phase stopped. The run tried the
step again, and each plug converted; but a phase should ride out short drops
by itself. [#5](https://github.com/adamsthws/Sonoff-OTA/pull/5) fixed it: the
retries now count only time spent failing. With it, 38 scenarios of drops,
packet loss and a weak signal at every phase that moves data, run three times
over, all passed but one, whose drops came just before its phase began - as
G1 checked the plug, then as it was started. Those are fixed too (below). The
two with nothing to corrupt are a packet changed during R2, the check that
Sonoff's firmware is back: it carries no data to change.

The longest one phase kept the plug busy was 86 seconds (L1, on a weak
signal). A real S60TPG once stopped answering after 70-90 seconds busy, while
an S60TPF converted after two minutes: so how long is recorded and reported,
not judged.

Then a few against the real eWeLink, on a day when some of its EU servers for
plugs turned connections away - four of six in the morning, one by midday:
fresh conversions to each firmware, and two faults at C2. Sonoff to ESPHome
converted (2 min 45 s), and Sonoff to Tasmota on its third run after an
outage at C2; a fresh Sonoff to Tasmota never got the installer to the plug,
in four tries, as the plug's connection to eWeLink, or the test computer's,
kept failing. The run put some of that down to the test computer's firewall
(fixed, below).

**2.0.2.** Its steps that write firmware check each block on the plug rather
than sending it back, so they are shorter (below), and every fault the suite
times by them was retimed. Before release, 301 scenarios were run on it, on
the four machines in about an hour: every one at E2, G1, I1, L1 and R1, where
the change is, and samples at D5, H1 and C3 for the other fixes. All passed,
the retimed ones once their faults were placed by the new lengths, and the one
whose 1.2.0 run - which the scenario stops at G1 - first stopped by itself
before it got there, at its F4, when run again.

Between them, those steps kept the plug busy 18 seconds where they took 31,
over two runs of 52 scenarios of drops, loss, a weak signal and corrupted
packets at each. The longest any phase kept it busy was 78 seconds, on a
weak signal for a minute of it.

Before the stub, on 18-19 October, the suite ran 1,453 scenarios on the code
as of 19 October, the cloud ones against the real eWeLink: all passed but the
same two with nothing to corrupt.

### Still open

- **An update command ignored.** Now and then the stock plug, back on eWeLink
  after a send cut off part way and a restart, ignores the next update command
  (the run says "the plug never connected"); the run after it goes through.
  Once, two in a row. Every such scenario passed when run again.
- **A download of the installer that stalls.** Twice, after a short outage
  during the first send to ESPHome, each later download of the installer began
  and stalled part way. Both passed when run again; not yet explained.
- **Discovery right after A1.** Twice in about 70 fresh plugs, a plug found in
  eWeLink at A1 did not answer the LAN discovery at A2 for half a minute. Both
  passed when run again.
- **More than one emulator per core.** Then the emulated plug can slow down
  enough to fail where nothing is wrong - once leaving it unable to reach the
  tool until it restarted - so the suite is run at one per core or fewer.
  On 22 October it happened once at one per core, on a VM whose host was
  busy: after a fault at E2, the plug's own requests to the tool stalled
  at G1 and none got through for four runs, while it still answered. Run
  again, it passed.
- **A Tasmota that stopped.** Once, the interim Tasmota, testing the Wi-Fi
  details sent on its setup network, said "Connect failed with AP timeout"
  with the router on, then printed nothing and answered nothing for a quarter
  of an hour - not even restarting when its setup network's three minutes
  were up. Run again, it passed; not yet explained.
- **Real plugs.** The faults have been tried on the emulator only.

### What it has found

Each fixed, with a test, and the scenario run again. On 22 October:

- A request the plug or the network dropped part way through a step - a
  connection reset in an outage, a reply cut off by a power cut, none in time
  - was put as "something unexpected went wrong", though the step was then
  tried again as for any network trouble: 32 scenarios, 23 of them at the
  final check (H1), the rest during the backup (C3) or F4. It now says the
  connection to the plug was lost.
- A plug D1 had just restarted onto the Wi-Fi was taken, as it started up,
  for one that had lost its settings: the network a Tasmota shows for a
  moment at start-up was taken for its setup network, and the run waited 15
  minutes for it to be joined, then crashed: 6 scenarios. The plug is now
  given a minute first, and that look's errors stop only its step.
- Against the real eWeLink, a send of the installer that failed before the
  plug was told to fetch it - eWeLink dropping the tool's connection -
  was put down to the computer's firewall. It now says what happened.
- Wi-Fi drops just before a phase began, rather than during it: G1's check
  that the plug runs official Tasmota stopped the step as if it had failed;
  the command that starts a phase, lost before it reached the plug, left the
  run waiting on a plug that never began; and reading the Wi-Fi details off
  the plug, timed out once, had the person asked to type them. Each is now
  asked or sent again once the plug answers.

In the October 18-19 regression:

- The installer waited for ever on an upload its computer stopped sending -
  a laptop closed, say - answering nothing until it was switched off and on.
  It now gives up after a minute, and the run tries again.
- A power cut while the official startup firmware was written back (G1) left
  one sector half written, and every later run refused to go on: stuck for
  good, with the plug on Tasmota. That sector is now written again.
- A run stopped as the commit ran, whose plug finished the commit by itself,
  carried on by checking the plug's network on the startup firmware - which
  cannot say it - and was sent back to the start with no way forward.
- Tasmota sent through the installer's own network joined your Wi-Fi by
  itself, with the settings an earlier, cut-off try had left, while the run
  waited a quarter of an hour for a setup network it never raised.
- After the installer could not be sent again (the plug offline in eWeLink),
  the run asked for the installer's own network, never raised.
- A Tasmota plug too busy to say its template in time was taken for one not
  yet set up as an S60, and sent down a route that never finished.
- ESPHome's page arriving with a byte changed stopped the run as "something
  unexpected", where a dropped connection is asked again.
- A plug switched on as the run starts was asked about at once; it is now
  waited for a minute. An upload to the installer that stalls is noticed after
  two minutes, not five.

In the rounds before it, from 16 October:

- ESPHome given your Wi-Fi gave it up, and saved the empty one, if its first
  join took longer than half a minute; it now keeps it.
- A power cut just after F4's reset left official Tasmota with no Wi-Fi, on
  its own network, while the run waited for a restart it could not see; it is
  now set up through its own network.
- Just after a restart Tasmota could not yet say which slot it runs from
  (D4), was taken for one in the wrong slot, and sent another upload.
- An upload Tasmota refused went unnoticed; X2's plug back on the mover or
  ESPHome, and official Tasmota not running yet, were missed - each now seen
  and sent again.
- A run stopped as the firewall rule was added left the rule behind, and the
  next took it for one the computer had before.
- With two plugs converted at once, one run put back a safety lock the other
  had lifted.
- A commit the plug finished after its run was stopped was not seen by a run
  started again from the beginning.

Before that:

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
- With the tool stopped part way through sending the installer, the
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
