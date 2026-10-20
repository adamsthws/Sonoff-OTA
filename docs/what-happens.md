# What actually happens to your plug

Written for someone who wants to understand what they are about to do, without
needing to know anything about embedded firmware. Nothing here is required
reading - the tool works without it - but the one irreversible step is easier
to consent to if you know why it is irreversible.

## The problem

A Sonoff S60 arrives running Sonoff's own firmware, which talks to Sonoff's
servers. Replacing it normally means opening the case and connecting a wire to
the chip inside. That is fiddly, voids everything, and on a mains plug it is
genuinely dangerous if you get it wrong.

There is another way in. The plug already knows how to replace its own
firmware - that is how Sonoff ships updates. It will do it when the owner of
the plug asks. You are the owner.

## Getting in

The plug already knows how to replace its own firmware, and it does it when the
account that owns the plug tells it to. When Sonoff ships an update, their
servers send the plug an *upgrade command*: a message that names where to
download the new firmware and the exact fingerprint - a SHA-256 hash - the
download must match. The plug fetches that address, checks the hash, and
installs what it gets.

Two things about that command are the whole way in. It only has to come from the
account that owns the plug - your eWeLink account - and the download address and
hash inside it are not tied to Sonoff. So the tool, signed in as you, sends the
plug its own upgrade command: one that names *your computer* as the download
address, and our firmware's hash. The plug fetches our firmware straight from
your computer and installs it, exactly as it would a genuine update.

Nothing is faked or bypassed. The plug does every check it normally does - it
confirms the command came from its owner, and it verifies the firmware it
downloaded against the hash in the command. It simply has no reason to care that
the address is your computer and the firmware is Tasmota rather than Sonoff's.

Because the plug downloads straight from your computer, **no router rule is
involved at all.** The tool points the plug at your computer and serves it our
firmware; it never touches, redirects or depends on Sonoff's own download
address. This holds whether or not Sonoff has an update waiting for the plug -
the address and hash in our command decide what the plug installs, not whatever
Sonoff happens to be offering.

> **A note on history.** Earlier versions added one rule to your router
> whenever Sonoff was offering the plug an update. It was never how our firmware
> was delivered - that has always come straight from your computer - only a
> guard against a second, unlikely possibility: that the plug might *also* fetch
> and install Sonoff's genuine update on its own, in the same short window, from
> a one-time signed address only the plug can produce. That guard was dropped to
> keep the process simple, since the owner's command already decides what is
> installed. If a future plug ever turns out to race its own update during the
> conversion, the guard can be put back: [router-rules.md](router-rules.md)
> has the rule, how to test it and where the old step lives - and how a
> simpler router rule can stop a stock plug installing Sonoff updates at all.

## Why it takes several stages

You cannot replace everything at once, because the plug is running from the
memory you want to change.

Think of the plug's memory as a shelf with two large slots for firmware. The
plug runs from one and can write to the other. So:

1. Sonoff's firmware is in the first slot. We put a small installer of ours in
   the second, and restart into it.
2. The installer puts a full Tasmota back into the first slot.
3. Tasmota then copies itself into the second slot and restarts, so the entire
   first half of memory is now free to be rebuilt.

Every stage runs from somewhere that is not about to be overwritten. That is
the whole trick, and it is why the process looks like more steps than it
should.

## The step that cannot be undone

At the very start of the plug's memory is a small table, a few thousand bytes
long, listing where everything lives. The bootloader reads it every time the plug
powers on. Without it, the plug does not know where its firmware is and will
not start at all.

The Sonoff layout divides memory into two firmware slots and no file storage.
The official Tasmota layout divides it differently: a small recovery firmware,
one much larger area for Tasmota, and space for files. To get the official
layout, that table has to be replaced.

There is only one copy of it. No spare, no backup the bootloader can fall back
on. Replacing it means erasing it and writing the new one - and for the moment
in between, the plug has no valid table. That moment is about a second, inside
a step that takes a minute, and if the power fails during it, the plug is
finished as far as Wi-Fi is concerned.

Everything the tool does around that second is designed to make it as brief and
as certain as possible: the new firmware is already in place and verified before
the table is touched, the replacement table is checked byte for byte in memory
first, and the plug refuses the whole operation unless it is running from the
far end of memory, well away from everything being changed.

## How your Wi-Fi carries across

The plug keeps the Wi-Fi network it was set up with in the eWeLink app. The installer reads and joins the saved WiFi network automatically.

The small temporary firmware that runs after the rebuild saves your Wi-Fi again, where official Tasmota reads it, and Tasmota joins your network by itself.

If any of that does not work, the tool falls back to asking you to
join the plug's own setup network.

Each time the plug restarts into another firmware, that firmware has to join
your Wi-Fi on its own, and a weak signal makes that less certain. So the tool
reads the plug's Wi-Fi signal as early in the run as the plug can say it, and
again at later stages. If one reading is weak (below -75 dBm) it suggests
moving the plug nearer your router first, and asks whether to carry on - once
a run. The run's report gives the lowest reading.

## The run, step by step

This is what `python3 tools/s60_autoflash.py run` does, in order, for a plug
still on Sonoff's firmware (one already converted is in
[Changing firmware later](#changing-firmware-later)). First it asks which
firmware you would like, Tasmota or ESPHome (`--firmware` answers in
advance); steps 1-7 are the same for both.

1. **List what has to be in place first** - the eWeLink app and account, the
   plug added with LAN Control on - and wait for a key press.
2. **Download the firmware it needs**, on the first run only, and check every
   file against a known fingerprint.
3. **Find the plug on your network.** It signs in to your eWeLink account
   (your email and password are asked here, once for the whole run) and
   looks on your network for the S60s in it, matched by their eWeLink id, so
   no other device can be mistaken for yours. With one S60 it carries on by
   itself; with several it asks which. A plug that answers here while
   eWeLink still shows it offline - one that has only just been switched on -
   is given up to a minute to reach eWeLink before anything is
   asked. If yours does not answer, it says why: offline in eWeLink, LAN
   Control off, or on a different network.
4. **Ask the plug what firmware it has** and where it downloads updates from.
   It stops here, before anything is written, unless the device is an S60.
   It also reads the plug's Wi-Fi signal, from what Sonoff's firmware
   announces on your network (encrypted with the plug's own key, which your
   eWeLink account holds): a weak one is best fixed now, by moving the plug,
   while nothing has been written.
5. **Replace the Sonoff firmware** with a small installer of ours. *This is the
   point of no return,* and the one place it asks you to confirm: after a
   short warning - which also says the Sonoff firmware is backed up first,
   or already was - you type PROCEED, which covers the whole conversion,
   including the backup in step 6 and the rewrite in step 8. Nothing is sent
   to the plug before that. If the computer's firewall is on, it adds a
   temporary rule for the plug first.
6. **Back up the Sonoff firmware.** The installer, which has joined your
   Wi-Fi by itself, hands over a copy of the plug's whole memory - Sonoff
   firmware, settings and all - checked byte for byte against the plug, and
   kept under the plug's MAC address in `captures/plugs/` (see
   [Your Sonoff backup](technical-details.md#your-sonoff-backup)). A plug
   backed up before keeps that backup and is not read again; `--no-backup`
   skips it.
7. **Install a first Tasmota through the installer**, and give it your Wi-Fi
   details so it joins too. If the installer could not join, or Tasmota does
   not appear, it asks you to join the plug's own network instead, names it,
   does the rest itself, and tells you when to switch back.
8. **Update the bootloader**, the small program that starts the plug's
   firmware, to ESPHome's, which starts Tasmota, ESPHome and Sonoff's firmware
   alike and rolls back an update that fails to start. There is only one, so
   it is fetched and checked whole first, then written in under a second and
   read back; if it does not read back right, the old one is put back. A
   power cut in that second would leave a plug that has to be opened to
   repair. Then **rebuild the plug's memory layout** to match the official Tasmota one. It
   counts down ten seconds first - Ctrl-C stops it. Before writing over
   anything, it copies the plug's settings, and the note that says which half
   to start, to where the rebuild does not reach, and points the plug at the
   copy: a power cut during the rebuild leaves it starting the same Tasmota,
   settings and all. The run waits for it to come back, puts its settings
   back where they were and tries again by itself - up to three times in
   all, for a power cut, a read-back that differed, or the plug changing
   since its checks. Only the moments the layout table itself is written - a
   few milliseconds, twice each time - would leave a plug that has to be
   opened.
9. **Install official Tasmota**, which joins your Wi-Fi by itself, apply the
   S60 settings, and check the result. Only if it does not appear on your
   network are you asked to join its setup network.

At the end it checks the finished plug against the official Tasmota release,
tells you whether every byte matches, and removes the temporary firewall rule
- which needs administrator rights again, so it says why before the system
asks.

For **ESPHome**, after the bootloader, steps 8 and 9 are instead:

8. **Install ESPHome** into the half of the plug's memory Tasmota is not
   running from - the published ESPHome for the S60, with your Wi-Fi written
   in, in memory only. Every block is read back and compared with the image,
   byte for byte. Only then does the plug switch to ESPHome, by clearing the
   note that says which half to start, and restart. Until that moment it
   still starts Tasmota. The memory layout itself is not changed.
9. **Check ESPHome is running** and has joined your Wi-Fi. If it cannot, it
   puts up its own network, named like `Sonoff-Setup-XXXX`, where its page
   asks for your Wi-Fi.

It then switches the relay off and on again through ESPHome to check it, and
gives you the plug's ESPHome page.

## At the end

The tool reads back four parts of the plug's memory, calculates a fingerprint
of each, and compares them against the files they came from:

- the bootloader
- the memory layout table
- the recovery firmware
- Tasmota itself

If all four match, the plug's Tasmota is byte-for-byte identical to a plug
flashed over USB with the official release - not equivalent, identical - with
ESPHome's bootloader starting it.

From then on it is an ordinary Tasmota device: normal updates through its own
web page, normal integrations, and none of it touching Sonoff's servers.


## Changing firmware later

A plug this tool converted can move to either of the other two, or back to
Sonoff's firmware. The run finds it on the network by what it answers as,
says what it runs, and offers the other two (Sonoff's only where the plug's
backup is on the computer). Every route has the same checks as above.

Most routes pass through the same place the first conversion does: Sonoff's
memory layout, with a full Tasmota running from its second half. From there
the steps above finish the job for Tasmota or ESPHome, and going back to
Sonoff writes the plug's own firmware and settings from its backup into the
first half, checks them, and switches to them.

- **From ESPHome**, the startup firmware is sent to ESPHome as an ordinary
  ESPHome update, with your Wi-Fi written in. This project's ESPHome gives
  its Wi-Fi name and password from 1.1.0, through its native API, while the
  API has no encryption key. From an ESPHome that does not, they come from
  the plug's Sonoff backup on this computer - the Wi-Fi it was set up with -
  when that is shown to be the plug's network still: the firmware bundle on
  this computer was set up for exactly those details, or ESPHome says it is
  on that network. Otherwise they are asked for. The startup firmware joins
  your Wi-Fi, saves it for Tasmota, and is sent the full Tasmota.
- **From Tasmota**, the plug is on Tasmota's own layout, where Tasmota sits
  across both halves of Sonoff's. A small ESPHome, the *mover*, fits in front
  of it: it is written there, the area Sonoff's layout keeps settings in is
  cleared, and Sonoff's layout table is written last - the one moment a power
  cut would leave a plug needing to be opened. The plug restarts into the
  mover, which takes ESPHome itself as an update, or the startup firmware
  as on the way from ESPHome.
- **A plug left part way** - running the mover, the startup firmware or the
  full Tasmota on Sonoff's layout - is recognised by a new run, and can go on
  to any of the three from where it is.

A plug whose firmware has a password - a Tasmota web password, or an ESPHome
update password, set in Home Assistant, say - is asked for it: Tasmota's when
the run finds the plug, ESPHome's before the go-ahead to leave it. The
question has a second choice, for once you have removed the password on the
plug yourself: try again without one. Each try is checked on the plug, before
anything is written. A password typed is held in memory for the run only, and
sent only to a plug that has asked for one; `S60_TASMOTA_PASSWORD` and
`S60_ESPHOME_OTA_PASSWORD`, in `.env` or the environment, give them in advance
(for Tasmota, `--web-password` too). An ESPHome set to take only encrypted
updates (`encryption:` under `ota:`, keyed by its API key) is not one this
tool can update: the run says so, and waits for it to be removed.

The Wi-Fi signal is read first from Tasmota, before anything is changed. From
ESPHome it is read before anything is written too: from its Wi-Fi signal
sensor, which this project's ESPHome has from 1.1.0, or else from what ESPHome
prints of its configuration when asked through its native API - which an
ESPHome with an API encryption key will not answer. From one that gives
neither, it is read once the full Tasmota is running, as the startup firmware
cannot say either.

## Safety, and why you can believe the checks

- **Nothing is trusted, everything is checked.** Every firmware file is
  checked against a known fingerprint before use, and the finished plug is
  read back and compared against the official release.
- **The risky steps need you to say yes.** The conversion only starts after
  you type PROCEED - not a y/n - below a warning that spells out the risk.
- **The plug checks too.** Every stage that writes refuses to run unless the
  plug is in exactly the state it expects, and says so rather than guessing.
- **Nothing private is published.** Each run keeps its records in `captures/`,
  which git ignores.