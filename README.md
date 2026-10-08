# Sonoff S60 → Tasmota, without opening the plug

Turns a stock Sonoff S60 smart plug into one running **Tasmota** using
OTA - no soldering, no taking the plug apart.

There's one script with checks at every stage. Resumable and can be 
restarted at any point

```sh
python3 tools/s60_autoflash.py run
```

> ### Where this is up to
>
> **The conversion works.** Five plugs have gone from stock 1.1.1 and 1.2.0
> to official Tasmota. The last two entirely by the script, and the
> last one from stock 1.2.0 using only the published files in this
> repository - no compiling, no flash dump, no USB.
>
> It is still early: every plug converted so far is a UK S60TPG. The European
> S60TPF looks like the same hardware and should work too, but nobody has
> tried yet - if you have one, please do, and [tell us how it went](#telling-us-how-it-went).
> Read the warnings below and expect to answer a few questions along the way.

---

## Read this before you start

**This is a mains-voltage device and one step cannot be undone.**

Partway through, the tool rewrites the small area of memory that tells the plug
where its firmware lives. It takes about a minute. If the power is cut during
that minute, the plug will not start again, and no amount of Wi-Fi cleverness
will bring it back - it would need opening and a USB-serial adapter.

That step has worked every time it has been run, and everything before it is
reversible. But please only do this with a plug you can afford to lose.

One thing reduces the risk to nearly nothing: plug it into a socket nobody is
going to switch off until the run finishes.

**This is not official Sonoff or Tasmota software.** Converting the plug ends
any support or warranty you had, and the eWeLink app will no longer control it.

---

## What you need

| | |
|---|---|
| **The plug** | A Wi-Fi Sonoff S60 (ESP32-C3 inside): the UK **S60TPG** or the European **S60TPF**. Tested on the S60TPG on stock firmware 1.1.1 and 1.2.0; the S60TPF is expected to work but has not been converted yet - see [Other regions](#other-regions). Not the Zigbee S60 (S60ZB...), which has a different chip. |
| **An eWeLink account** | Free. A throwaway one is fine. The plug has to be added to it first. |
| **A computer** | Linux, macOS or Windows, Python 3.10 or newer, with Wi-Fi. A Wi-Fi-only laptop is fine: normally the whole run happens over your own network. Only if a step cannot be done that way does it ask you to join the plug's own network, and it tells you when to switch back. Linux is the tested path; macOS and Windows are written for but not yet proven - if you use one, please [try it and tell us](#windows-and-macos). |
| **Administrator rights** | Not to start it: run it in an ordinary terminal, not as administrator or with sudo. If the computer's firewall is on, the tool adds one temporary rule letting only the plug in, asking through the system's own prompt (sudo, or the Windows permission dialog), and removes it when the run finishes. On Windows, if Windows Security asks whether Python may use networks, choose Allow. |
| **Your router's admin page** | Not needed. The plug downloads our firmware straight from your computer, so nothing is redirected. Optional insurance, not needed today: a router rule, added before a new plug first joins your Wi-Fi, stops it installing a future Sonoff firmware that might close the way in. See [docs/router-rules.md](docs/router-rules.md). |

Your Wi-Fi must be **2.4 GHz** - the plug cannot use 5 GHz.

## What it has been run on

| Plug | Model | Stock firmware | What was done |
|---|---|---|---|
| 1 | UK S60TPG | 1.2.0 | Converted with wrapped bridge v2, then v3, then Tasmota |
| 2 | UK S60TPG | 1.1.1 | Converted with wrapped bridge v3 directly, then Tasmota |
| 3 | UK S60TPG | 1.1.1 | Converted, then migrated to the official partition layout by hand |
| 4 | UK S60TPG | 1.1.1 | The whole thing by the automated script - conversion and layout |
| 5 | UK S60TPG | 1.2.0 | The whole thing by the automated script, from a bundle built only from the published files |
| 6 | UK S60TPG | 1.2.0 | The whole thing by the automated script with no network switching and no Wi-Fi typed: bridge v5 and the settings-saving startup firmware |

All six are UK Type G plugs from the `SN-ESP32C3-S60-01` firmware family.
Plugs 3 to 6 end up byte-for-byte identical to a plug flashed over USB with
the official Tasmota release, verified by reading their flash back.

**Reported by others.** [mati1988r](https://github.com/HG0161/SonoffS60OTA/issues/1)
ran the read-only checks - mDNS discovery and the encrypted LAN connection - on
a **European** S60 on stock **1.2.0**, and reported them working. No European
plug has been converted yet.

### Other regions

Testing on other regional versions is very welcome, and we'd encourage it: it
is the only way this table grows. There is good reason to expect the European
**S60TPF** (Type E/F) to work exactly like the UK S60TPG:

- **Same chip and same wiring.** Both are ESP32-C3 plugs, and the published
  pin assignments for the S60TPF - relay on GPIO4, status LED on GPIO5, power
  meter on GPIO7, button on GPIO9 - are the ones this tool sets on the S60TPG.
- **Same way in.** The tool only goes ahead if the plug reports the S60's
  firmware family (`SN-ESP32C3-S60-01`), and a European plug on stock 1.2.0
  has already passed the read-only checks.
- **It checks rather than assumes.** If the plug reports a different firmware
  family, the run stops before anything is sent. If its memory layout differs
  from the S60TPG's, it stops before rewriting the layout - by then the plug
  is already running Tasmota, so it keeps working, just not on the official
  layout.

Things worth checking on another region, and mentioning in a report:

- the power readings against a known load: the S60TPF's meter is listed as a
  CSE7759B, which speaks the same protocol as the S60TPG's CSE7766, but its
  calibration may differ;
- that the relay, button and LED behave as described in
  [What you end up with](#what-you-end-up-with);
- the name on Tasmota's page: the tool names the template after the model
  eWeLink reports, so it should read "Sonoff S60TPF". If eWeLink reports
  something unexpected it reads "Sonoff S60" instead - worth mentioning, since
  it means the model field looks different on your plug.

Whatever happens - converted, stopped partway, or anything odd - please
[tell us how it went](#telling-us-how-it-went).

### Windows and macOS

Every conversion so far was run from Linux. The tool is written for Windows
and macOS too - finding the plug, the firewall rule, Wi-Fi and the clipboard
each have their own code for those systems - but none of it has been through a
real conversion yet. If you are on Windows or a Mac, please give it a go and
let us know how it went. A report from a first run is what moves Windows or
macOS from "written for" to tested, so the next person can follow the
instructions knowing they have worked for someone already, and anything that
trips you up gets fixed for everyone after you.

Whether it worked first time or stopped partway, please
[tell us how it went](#telling-us-how-it-went) - including any prompts from
the system along the way, such as the Windows permission dialog, Windows
Security asking about Python, or the macOS firewall, and what you chose.

### Telling us how it went

Every report goes in one place: the pinned
[Test reports](https://github.com/adamsthws/Sonoff-OTA/discussions/1) thread in the
repository's Discussions. The tool also offers the link, with this run's
details ready to paste, when a conversion finishes.
A clean conversion, a run that stopped partway, or only the read-only checks -
all of it is useful. Reading the thread is the quickest way to see what has
worked for others, and each report is how a plug or a computer gets added to
the table above.

Please include:

- **the plug:** the model printed on it (S60TPG, S60TPF, ...), its plug type
  or region, and the stock firmware version the eWeLink app showed before
  converting;
- **the computer:** Windows, macOS or Linux and its version, and
  `python --version`;
- **the run:** how far it got and roughly how long it took, and whether it
  asked you to type the Wi-Fi or join the plug's setup network;
- **the result:** the name Tasmota's page shows, and whether the relay,
  button, LED and power readings behave;
- **if it stopped:** the on-screen messages around where it stopped.

If something clearly needs fixing in the code, an
[issue](https://github.com/HG0161/SonoffS60OTA/issues) is welcome too - link
it from your report so the thread stays complete.

The run's log, `captures/<run>/run.log`, has the full detail if you are asked
for more. Read it before sharing any of it: it holds your Wi-Fi network's
name, your eWeLink email and your devices' addresses (never passwords), and
the other files in `captures/` hold more than that, so don't attach those.

## Getting set up

```sh
git clone https://github.com/HG0161/SonoffS60OTA.git
cd SonoffS60OTA
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
```

On Windows the environment's Python is `.venv\Scripts\python` instead of
`.venv/bin/python`. The commands below use `python3` for short; run them with
the environment's Python so nothing is installed outside the repository.

The tests should all pass. They check the tool against itself and take a few
seconds; nothing touches the plug.

## The firmware bundle

The first run prepares a bundle: it downloads the official Tasmota release
files, checks every one against a pinned SHA-256, and writes the bundle into
`captures/`, which git ignores. It asks nothing: the bundle is built without
your Wi-Fi details, which are added later in the run. To rebuild it on its own:

```sh
python3 tools/prepare_imprinted_bundle.py --without-wifi
```

Partway through, the plug runs a small temporary startup firmware that has to
join your Wi-Fi by itself, so your network name and password are written into
it. You are not asked for them: by that point the plug is running Tasmota on
your network, and the tool reads them from the plug's own configuration into
memory. They are proven, because the plug is connected with them at that
moment, and the tool checks the name against the network the plug reports
being on. Nothing is typed, and nothing is written to disk.

Earlier still, the installer reads the Wi-Fi the Sonoff firmware saved, joins
that network, and gives the first Tasmota its settings with the details in, so
it joins too. The temporary startup firmware later saves them where official
Tasmota reads them, so the finished plug joins by itself as well.

The plug therefore keeps the Wi-Fi it was set up with in the eWeLink app, so
set it up there on the 2.4 GHz network it should stay on. If the details cannot
be read or given automatically, the tool says why and asks for them - only
then, and only once. To put the plug on a different network, pass `--wifi` and
they are asked for instead - typed twice, since Tasmota never reports its
password for checking.

Your Wi-Fi password is never written to disk. It is held in memory for the run
only; the temporary startup firmware built from it is built in memory and
handed to the migration server through a pipe, never as a file. The plug loses
the password at the end of the run, when the official startup firmware
replaces the temporary one.

Other secrets are not kept either: the plug's eWeLink LAN key is no longer
fetched, and the update links, the plug's settings memory and staged firmware
are deleted or reduced to hashes as soon as they have been checked - however
the run ends. `python3 tools/s60_autoflash.py cleanup` removes anything an older
version of the tool left behind.

### What is in it

Partway through the conversion the plug needs two Tasmota images that are not
part of the ordinary release download:

- [`artifacts/tasmota32c3-bluetooth.bin`](artifacts/tasmota32c3-bluetooth.bin)
  is the Berry-capable ESP32-C3 build used while the old partition map is still
  active. It is pinned byte-for-byte and includes its upstream source revision
  in [the artifact notes](artifacts/README.md).
- [`artifacts/s60-recovery-safeboot-imprintable.bin`](artifacts/s60-recovery-safeboot-imprintable.bin)
  is the small temporary startup firmware. It contains placeholders, not a
  real network name or password, so the published copy is safe to share. It
  also saves the Wi-Fi where Tasmota reads it, so the official Tasmota
  installed after it joins your network by itself.

Never flash the placeholder image directly. Each run writes your Wi-Fi
details into an in-memory copy, repairs both ESP image integrity fields,
reads the details back from where the firmware will read them, and proves the
copy differs from the published image in nothing else.

The bundle pins that copy by SHA-256 without storing it, and holds the
Bluetooth image, the official Tasmota release files and the partition table
taken from them, all pinned by hash so nothing can be swapped underneath you. It needs no flash dump: the tool reads the plug's
partition table and settings live, and checks them, before anything is written.

The published images remove the need to compile Tasmota locally. They do not
remove the safety checks: every image and every live flash readback still has
to match its pinned provenance.

## Doing it

```sh
python3 tools/s60_autoflash.py run
```

That is the whole interface. It picks up an unfinished run, and starts a new
one once the last plug is done.

While it runs, the screen shows one line per stage - for example
`Stage 6 of 14 - Installing Tasmota - underway 42s`, which changes to
`complete` when the stage is done - plus anything it needs you to read or
answer. Everything else goes to a log, `captures/<run>/run.log`. To watch all
of it as it happens, add `--verbose`.

It will:

1. **List what has to be in place first** - the eWeLink app and account, the
   plug added with LAN Control on - and wait for a key press.
2. **Prepare the firmware bundle**, on the first run only - see
   [The firmware bundle](#the-firmware-bundle).
3. **Find the plug on your network.** It must already be in the eWeLink app
   with LAN Control on. If several Sonoff devices answer, it asks which.
4. **Ask the plug what firmware it has** and where it downloads updates from.
   It stops here, before anything is written, unless the device is an S60.
5. **Replace the Sonoff firmware** with a small installer of ours. *This is the
   point of no return,* and the one place it asks you to confirm: after a
   short warning you type PROCEED, which covers the whole conversion,
   including the rewrite in
   step 7. If the computer's firewall is on, it adds a temporary rule for the
   plug first.
6. **Install a first Tasmota through the installer**, which has joined your
   Wi-Fi by itself, and give it your Wi-Fi details so it joins too. If the
   installer could not join, or Tasmota does not appear, it asks you to join
   the plug's own network instead, names it, does the rest itself, and tells
   you when to switch back.
7. **Rebuild the plug's memory layout** to match the official Tasmota one. It
   first checks the plug is on the Wi-Fi network the bundle is set up for, then
   counts down ten seconds - Ctrl-C stops it - before the minute in which the
   power must stay on.
8. **Install official Tasmota**, which joins your Wi-Fi by itself, reset its
   settings to Tasmota's defaults (keeping the Wi-Fi), apply the S60 settings,
   and check the result. Only if it does not appear on your network are you
   asked to join its setup network.

At the end it verifies the finished plug against the official Tasmota release,
region by region, tells you whether every byte matches, and removes the
temporary firewall rule - which needs administrator rights again, so it says
why before the system asks.

Apart from that one confirmation, it asks for your eWeLink password (not
stored; the email is remembered). Your Wi-Fi details are only asked for if they
cannot be read off the plug or given to it automatically, and the tool says
which and why.

### If you have to stop

Close the window, lose your Wi-Fi, run out of time - it doesn't matter. Run the
same command again:

```sh
python3 tools/s60_autoflash.py run
```

It remembers which plug it was working on and what it had done. Where a step
was interrupted, it checks the plug to work out whether that step finished,
and only asks you if the plug cannot show it.

To give up on a plug part-way and remove the temporary firewall rule:

```sh
python3 tools/s60_autoflash.py cleanup
```

## What you end up with

Official Tasmota on the official partition layout - the same arrangement you
would get from flashing the plug over USB:

| Area | What's in it | Size |
|---|---|---|
| `nvs` | Settings | 20 KiB |
| `otadata` | Which firmware to start | 8 KiB |
| `safeboot` | Recovery firmware for future updates | 832 KiB |
| `app0` | Tasmota itself | 2,880 KiB |
| `spiffs` | File storage | 320 KiB |

Which means normal Tasmota upgrades from then on, a filesystem the plug never
had, and 2,880 KiB of room for firmware instead of the 1,984 KiB the Sonoff
layout allowed.

After it finishes, check the physical side yourself: the relay clicks, the
button toggles it, the LED follows, and with a load plugged in the voltage,
current and power readings look sensible.

## When something goes wrong

Start with **[docs/troubleshooting.md](docs/troubleshooting.md)** - it covers
the problems that actually came up while this was being built, including what
each one looked like on screen.

The most common causes are a firewall on your computer blocking the plug, and
the computer's address changing partway through.

## How it works, roughly

The stock firmware will accept an update if the command comes from the account
that owns the plug. The tool uses that: signed in as you, it sends the plug an
update command whose download address is your computer, and answers with our
firmware rather than Sonoff's.

From there the plug is ours, and the rest is careful housekeeping - get a full
Tasmota on, move it out of the way, rebuild the memory layout underneath it,
then put the real thing back.

**[docs/what-happens.md](docs/what-happens.md)** explains it properly, in
English, including why the risky step is risky.

## Safety, and why you can believe the checks

- **Nothing is trusted, everything is hashed.** Every firmware file is checked
  against a known SHA-256 before use, and the finished plug is read back and
  compared against the official release.
- **The destructive steps are locked by default.** Files in this repository
  have to be deliberately renamed before the tool will run them, and the
  conversion only starts after you type PROCEED - not a y/n - below a warning
  that spells out the risk, before the first write that cannot be undone.
- **The plug checks too.** Every stage that writes refuses to run unless the
  plug is in exactly the state it expects, and says so rather than guessing.
- **Everything is recorded.** Each run keeps its evidence in `captures/`, which
  git ignores, so nothing private ends up published.

## For developers

The user-facing tool sits on top of a set of smaller, single-purpose scripts in
`tools/`, each of which can be run on its own. `archive/` holds the research
tools, the bridge firmware source and the custom Tasmota build that got the
early plugs converted - none of it is needed now, and none of it is deleted. The research behind all of it -
how the update mechanism was worked out, the partition analysis, the reviewed
migration plan and the manual procedure - is in
**[archive/docs/reference/](archive/docs/reference/)**.

To build the temporary startup firmware yourself instead of using the
published one, the local-build route is still there:

```sh
python3 tools/configure_recovery_safeboot.py        # asks for your Wi-Fi, privately
tools/build_recovery_safeboot.sh /tmp/tasmota-src   # builds the image
python3 tools/prepare_recovery_safeboot_migration.py
python3 tools/safeboot_migration_status.py          # says whether the bundle is good
```

It needs a full flash dump of a plug for `prepare_safeboot_migration.py`. The
procedure, including which Tasmota commit to check out, is in
[archive/docs/reference/part-2-safeboot-recovery-contingency.md](archive/docs/reference/part-2-safeboot-recovery-contingency.md).

[archive/docs/reference/automatic-wifi.md](archive/docs/reference/automatic-wifi.md) records how the network switches and Wi-Fi typing were
removed, what was ruled out, and why.
[archive/docs/reference/lessons-learned.md](archive/docs/reference/lessons-learned.md)
collects the pitfalls met along the way.

Start with [archive/docs/reference/manual-procedure.md](archive/docs/reference/manual-procedure.md)
if you want to do it by hand, and
[archive/docs/reference/ota-findings.md](archive/docs/reference/ota-findings.md) for how the
firmware format was decoded.

## Thanks

Worked out with the Tasmota community's discussion of this device, and built on
Tasmota itself. Tasmota is GPL-3.0-only; so is this.

See [`LICENSE`](LICENSE) and [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
