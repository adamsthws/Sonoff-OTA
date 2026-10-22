# Changelog

The conversion tool's own versions (`x.y.z`), not any firmware's. A patch
release fixes something; a minor one adds something; a major one is for a
change that stops a run started on an earlier version from being resumed, or
removes an option.

The version is set in one place, `tools/autoflash/__init__.py`. Merging a new
one to `main` tags it and publishes a release with its notes from here, so add
the section below before changing the number.

## 2.0.2

- The plug or the network dropping a request part way through a step - a
  connection reset in a Wi-Fi outage, say - was put as "something unexpected
  went wrong", though the step was tried again as for any other network
  trouble. It now says the connection to the plug was lost.

## 2.0.1

- A plug brought back from Tasmota's own safeboot finished its conversion,
  then the run stopped with "something unexpected went wrong" instead of its
  report, which could not name where the plug started. A Tasmota there with
  a web password is now asked for it, where the run stopped, and the rest of
  the run treats the plug as the Tasmota it was.
- The look round the network for a converted plug, 5 to 10 seconds - longer
  when one this computer converted may be restarting - showed nothing on
  screen, so the run seemed to wait for a key; one pressed then was taken as
  the next question's answer, an Enter as its default. The look now says so
  while it looks, then leaves the screen, and a key pressed while nothing is
  asking is dropped.
- The firmware question gives the firmwares by name alone.

## 2.0.0

ESPHome, as well as Tasmota: the run asks at the start which the plug should
end up running. And a plug can move between them later, or back to Sonoff's
own firmware.

A major release because the tool is now Autoflash, and the names that called
it after the S60 have gone:

- `tools/s60_autoflash.py` is `tools/autoflash_cli.py`. `start.sh`,
  `start.ps1` and `start.cmd` run it as before; a command that named the
  old file needs the new name.
- The settings it reads start `AUTOFLASH_`. In `.env`, `S60_WIFI_SSID`,
  `S60_WIFI_PASSWORD` and `S60_SUDO_PASSWORD` are still read, and the run asks
  for them to be renamed; `S60_PYTHON_HOME` is not, and is now
  `AUTOFLASH_PYTHON_HOME`.

- A run looks for the plug first. One already on Tasmota or ESPHome is offered
  the other two: Tasmota, ESPHome, or Sonoff's own firmware and settings put
  back from the backup taken when it was first converted (only where that
  backup is on this computer). One left part way through a conversion is
  recognised, and can go on to any of the three.
- Every way round is over the air and checked as the first conversion is:
  every block read back and compared byte for byte, and the plug switched
  only once all of it is.
- The bootloader becomes ESPHome's on every route, once: it starts all three
  firmwares, and rolls back an update that fails to start. The final check of
  a Tasmota plug now covers it too.

- Choosing ESPHome needs nothing built or installed: the run installs a
  ready-made ESPHome for the S60 (`ESPHome/sonoff_s60.yaml`), with the plug's Wi-Fi
  written in, in memory only. It joins your Wi-Fi by itself, has no password
  to start with, as Tasmota has none, and Home Assistant finds it and can
  adopt it.
- Every block of ESPHome is read back and checked before the plug switches to
  it. The plug's memory layout stays as Sonoff made it; its bootloader becomes
  ESPHome's own, so ESPHome can roll back an update that fails to start.
- The answer is kept with the run, so carrying on an unfinished run carries
  on the same way. `--firmware tasmota` or `--firmware esphome` answers it in
  advance, as an unattended run must.
- The report says which firmware the plug ended up with, and the relay and
  meter are checked through ESPHome on its route.
- The report calls Windows 11 so where Python 3.10 or 3.11, not the tool's
  portable one, called it Windows 10.
- The report's answers are shorter: "(portable)" for the tool's own Python,
  "Failed" for a backup that failed, "Switches (meter confirms)" for the relay.
- The plug's own network is looked for by this plug's names only - Tasmota's,
  and the `ESP_` name it sometimes shows instead, which was missed before - so
  a neighbour's Tasmota is never mistaken for it. Wherever you are asked to
  join it, its exact name is given.
- That `ESP_` name is only the one Tasmota's network has for a moment, as it
  turns it on and before it names it - which a computer can still list for a
  minute after, perhaps first. The network is now taken by its real name
  wherever that is listed, and caught in that moment it is looked at again,
  where before you could be asked to join one already gone.
- A power cut while the memory layout is rebuilt for Tasmota no longer costs
  Tasmota its settings. The rebuild wrote over where they were kept, so the
  plug came back waiting on its setup network, and the next run then stopped
  for good on the plug having changed. Now the settings are moved aside
  first, with an interim layout pointing at them, and the run itself waits
  for the plug, puts them back and tries again - up to three times in all,
  for a power cut, a read-back that differed, or the plug changing since its
  checks. A plug that did lose them is found on its setup network.
- A plug switched off and on before the installer has done its work goes
  back to Sonoff's firmware - by design, so nothing is lost - but the run then
  waited a quarter of an hour for an installer that was gone. It now sees
  Sonoff's firmware and sends the installer again, up to three times.
- A backup cut off by the home Wi-Fi going - the installer then raises its
  own network - is read again wherever the installer is once you have joined
  that, where before both tries went to its address on the home network and
  timed out, and an unattended run carried on without a backup.
- Tasmota sent through the installer whose upload took, though its answer
  was lost on the way back (a weak signal), joined your Wi-Fi by itself -
  and the try again waited a quarter of an hour for the installer, gone. It
  now finds Tasmota there, on your network or its own, and carries on.
- A plug whose Tasmota lost its settings part way through - switched off and
  on 7 times, say - waited on its own network while the run kept looking for
  it on yours, and once past setting Tasmota up (F4), the run asked for Wi-Fi
  details it could not use. It is now found on its own network and set up
  through it again, and Tasmota set up again where its template went too.
- Tasmota given the Wi-Fi on its setup network that took the details and
  restarted onto your Wi-Fi before saying so was sent them again, and the run
  then waited a quarter of an hour for you to join its network - gone. A
  network gone after no word is now taken as the plug on your Wi-Fi, and it
  is looked for there.
- An ESPHome plug whose API is encrypted, asked for its Wi-Fi without the
  key, could be taken for one without the tool's Wi-Fi action, where it
  answered and hung up before the tool's next write; the run then asked for
  the Wi-Fi password rather than for the key. Its answer is now read.
- A run started again (`--new`) after the memory layout was rebuilt for
  Tasmota, before official Tasmota was set up, took the plug for one on its
  way from ESPHome and sent it round steps that need Sonoff's layout, which
  never finished. This computer's record of the plug now tells the two
  apart, and the run goes on from official Tasmota.
- A plug offline in eWeLink when the installer is to be sent - after a send
  cut off part way, say - is waited for for up to three minutes, then the
  run says to switch it off and on, where before it was refused at once
  and the firewall blamed.
- A run stopped as the memory layout was committed - its window closed, say -
  whose plug then finished the commit by itself, carried on by checking the
  plug's network on the startup firmware, which cannot say it, and went back
  to the start of the conversion with no way forward. It now sees the commit
  done and carries on from there.
- After the installer could not be sent again (the plug offline in
  eWeLink), a plug slow to answer made the run ask you to join the
  installer's own network, which it had never raised. The next try now
  follows.
- ESPHome's page arriving damaged - a byte changed on its way - stopped the
  run with "something unexpected went wrong". It is now asked for again, as
  when the connection drops.
- A power cut while the official startup firmware was written back (G1)
  left the sector being written half done, and every later run refused to
  carry on - the conversion stuck for good, though the plug kept running
  Tasmota. That one sector is now written again.
- The installer gives up an upload that has gone silent for a minute. A
  computer going to sleep part way through sending it Tasmota (D1) - its lid
  closed, say - left the installer waiting for good: it answered nothing
  more, neither on your network nor on its own, until it was switched off
  and on.
- An upload of Tasmota to the installer cut off by the network is noticed
  after two minutes, not five, and tried again - through the installer's
  own network, if it has raised that and you have joined it.
- Tasmota sent through the installer's own network, after an earlier try
  cut off by the network had the installer save your Wi-Fi for it, joined
  your Wi-Fi by itself - while the run waited for a setup network it never
  raised, and asked you to join it. The run now sends you back to your usual
  Wi-Fi and finds it there; any wait for Tasmota's setup network also looks
  for Tasmota on your network.
- A Tasmota plug too busy to say its template in time - just started, or
  answering the run's look round the network - was taken for a Tasmota not
  yet set up as an S60, and sent down the wrong route, which never finished.
  It is asked again, and left to the next look rather than guessed at.
- A plug switched on as the run starts - offline in eWeLink, and not yet
  answering on the network - is waited for for up to a minute while it joins
  the Wi-Fi and the cloud, where before the run stopped at once to ask.
- An upload of official Tasmota to the startup firmware that stalls is
  noticed after two minutes, not ten, and tried again from a restart - up
  to three times - before it is left to be done by hand.
- The installer is now 4.4.3. It starts even when an upload of Tasmota into
  Sonoff's slot (D1) was cut off part way - by a power cut, or the computer
  stopping - where 4.4.2 refused to start, leaving a plug with no network at
  all that only a serial cable could recover.
- A run started again after D1 was cut off finds the plug on the installer
  and carries on from the backup, instead of looking for it in eWeLink -
  which it cannot answer - and being told to switch it off and on. An upload
  to the installer that stops is tried again by itself, three times in all.
- Ctrl-C no longer stops a run part way by itself. The first asks, at the
  end of the stage under way, whether to stop there - carrying on by itself
  after 30 seconds; a second within ten seconds stops at once, as before. At
  a question, or waiting on you, it stops at once. Closing the window stops
  the run, and the next one says that is why it stopped.
- Two runs on the same plug at once are refused, the second told which run
  has it: each would write over what the other was doing. The one refused
  leaves the other's log, report and firewall rule as they are, and
  `cleanup` waits until no run is converting a plug.
- Two plugs converted at once, in two windows, no longer trip over this
  computer's safety locks: a run starting put back a lock the other had
  lifted for its installer or its memory-layout change, and that one then
  failed putting it back itself. Each run that lifts one now says so, and the
  lock goes back when the last of them is done.
- This computer is kept awake while a run works, with what each system has
  already (systemd-inhibit, caffeinate, Windows' own setting) - nothing
  installed, and let go however the run ends.
- A plug that cannot be found can be set up through its own network by
  choice, for a computer that cannot see Wi-Fi networks. While waiting for a
  plug whose Wi-Fi went, the run says it will rejoin by itself, and that it has
  no network of its own at that stage.
- The mover, which takes a plug from Tasmota to ESPHome, raises its own
  network when it cannot join yours - two minutes after, if your Wi-Fi's name
  or password changed since the plug last joined - and the run sets it up
  through that, then gives ESPHome the same details.
- An upload Tasmota refuses - an image damaged on the way, say - is noticed
  from the page it answers with, and sent again, up to three times. Before,
  it was taken for one that worked, and the run waited five minutes for a
  restart that never came, then stopped blaming the network.
- A power cut in the second after official Tasmota's settings are reset
  (F4) leaves it without its Wi-Fi, on its own setup network. The run now
  sets it up through that there and then, where it waited three minutes and
  stopped saying the plug had not restarted.
- Stopping a run with Ctrl-C just as it added its firewall rule could leave
  the rule behind for good: the next run took it for one this computer had
  before, and left it. A stop there now waits for the rule to be recorded, so
  it is removed as usual, and a rule an earlier run left (ufw keeps the
  tool's comment on it) is recognised and removed too.
- Official Tasmota is taken as running only once it says so. A plug
  restarting back onto the startup firmware - the upload not taken - was
  taken for Tasmota for not answering for a moment, and the next stage then
  failed every time. Now the upload is sent again, up to three times, and a
  run carried on that finds the startup firmware at that stage sends it too.
- A plug switched off and on just as Berry Tasmota began to go on, from
  ESPHome or the mover (X2), starts the firmware it ran before - the startup
  firmware not settled in yet - which the run took for Tasmota, then waited
  for Tasmota in vain at every run after. It now sees which firmware is
  running, and sends the startup firmware again first.
- A run stopped a second into the commit (E3), which the plug then finished
  by itself, left a plug a new run (`--new`) took for one on its way from
  ESPHome: its steps need Sonoff's layout, and failed every time. The new
  run now sees the plug fetched the new layout and runs the startup
  firmware, and goes on from official Tasmota.
- Just after its restart into the high slot (D4), Tasmota could not yet say
  which slot it ran from; that was taken for the wrong slot, and a second
  upload sent into a plug still starting, which failed as "something
  unexpected". The run now waits until the plug reports which slot it runs
  from.
- Every firmware the plug runs part way through a conversion - the
  installer, the mover, the startup firmware, and Tasmota until it is set up
  - now goes by one name, `autoflash-recovery-` and the last four characters
  of the plug's hardware address (`autoflash-recovery-CDEF`): in your router's
  list, and for the installer's and the mover's own networks - and for
  Tasmota's, installed through the installer's own network, where it was
  Tasmota's default (`tasmota-ABCDEF-1234`). Every such network is open: the
  installer's had a password to type (`S60-OTA-Bridge-XXXX`).
- A converted plug is named for its firmware and the last six characters of
  its hardware address - `tasmota-abcdef` or `esphome-abcdef` - whatever kind
  of plug it is, and its own network has that name too. Tasmota was
  `tasmota-ABCDEF-1234`, and ESPHome `sonoff-s60-abcdef` with its network
  `Sonoff-Setup-CDEF`; the run still knows those. The names a person sees
  still say what plug it is: Tasmota's template (Sonoff S60TPG) and ESPHome's
  friendly name (Sonoff S60). ESPHome is 1.2.0 for it.
- Setting Tasmota up at the end (F4) restarts the plug once, where it
  restarted it twice: the name and the template go in as the plug saves the
  defaults its reset rebuilt, so it comes back converted, and never as
  Tasmota's own `tasmota-ABCDEF-1234` for a moment in between. Should that
  not take, they go in after the reset, as before.
- A Tasmota whose settings were erased part way - switched off and on 7
  times, as the run can advise - comes back under the run's name for it
  (`autoflash-recovery-CDEF`) once it is given the Wi-Fi on its setup
  network, where it kept Tasmota's own until the next firmware.
- The tool calls itself Autoflash, not the S60 conversion tool - in
  `--version`, and to the system it keeps awake - as it is to convert more
  than one kind of plug. Its firewall rule is called `Autoflash` (it was
  `S60 autoflash`); one a run of an earlier version left is still found and
  removed.
- What is the S60's own - its names in eWeLink and its stock firmware, its
  wiring for Tasmota, what its ESPHome calls itself - is kept in one place
  (`tools/autoflash/kinds.py`), and a run keeps which kind of plug it
  converts, ready for more kinds. Official Tasmota is still named after the
  plug's own model (Sonoff S60TPG), and the messages that only meant "the
  plug" now say so.
- ESPHome that cannot join your Wi-Fi at the end (I2) is set up through its
  own network by the run, as the mover already was, where before the run
  stopped and left it to you - and both carry on by themselves when you have
  joined that network already. The details the run has go first (right after
  a router outage, say); new ones are asked for only if the plug does not join
  with them. Every send of Wi-Fi details through a plug's own network, Tasmota's
  too, is tried up to three times, and rejected ones can be typed again.
- ESPHome, and the mover, no longer lose the Wi-Fi details the run wrote in
  when their first join takes over 30 seconds - a router restarting just then,
  say. ESPHome's `wifi.configure` put back what was there before - nothing, on
  a first boot - and saved that, leaving a plug with no network until it was
  switched off and on. They now start with the details, and keep trying.
- A run begun with 1.2.0 and stopped after its checks of the plug (E1), then
  carried on with this version, stopped at the next step every time: this
  version makes its own set of files for the plug, which those checks were
  not made with. The checks are now made again first, as the memory layout's
  rewrite already did.
- A run begun with 1.2.0 and stopped once the memory layout was being
  rewritten (E3) or after, then carried on with this version, stopped every
  time at a step 1.2.0 did not have: putting ESPHome's bootloader on (D5),
  which can only come before that rewrite. Such a run now leaves it out: the
  plug keeps Sonoff's bootloader, as 1.2.0 left every plug, and the final
  check does not look for ESPHome's. It goes on when the plug next changes
  firmware. Swapping 1.2.0's temporary startup firmware for the official one
  (G1) then asked for the Wi-Fi details the plug was converted with, to rebuild
  and check it - which no details could, as that firmware is not this
  version's. For such a run it is now written over without that check, once
  the plug shows the official memory layout and runs official Tasmota, which
  never starts from there; the final check reads the official one back.
- A converted plug left on Tasmota's own safeboot - after another firmware's
  whole-flash image (an ESPHome `factory.bin`, say) was uploaded through
  Tasmota's page, which writes it over Tasmota, so the plug starts the
  safeboot from then on; or just switched to it - was taken for one part way
  through a conversion, and sent down steps that need Sonoff's layout, which
  stopped at D4. The run now tells Tasmota's own safeboot from its own startup
  firmware, starts the plug's Tasmota again if it is there, else puts official
  Tasmota back, with its settings either way, and goes on from there to the
  firmware chosen - or stops there, for Tasmota. It looks for such a plug as
  for one it converted: one this computer converted, or the one `--plug-ip`
  names.

## 1.2.0

The report at the end now comes from every run that converted or failed, and
says how long each stage took.

- A run that failed - a stage that stopped, or something unexpected - offers
  its report too, saying where it stopped and why, with nothing private in
  it. A run you stopped yourself, at a question or with Ctrl-C, offers none.
- The report gives each stage's time, how long each stage that runs on the
  plug kept it busy, which stages needed retries, and the plug's Wi-Fi
  signal.
- Python has a line of its own in the report, saying where it came from:
  the tool's portable one, one it installed, a virtual environment, or one
  already on the computer.
- Fixes a plug that had just restarted, after the memory-layout rewrite say,
  being taken for one not answering: a request it could never answer was
  waited on for up to 20 minutes.

## 1.1.1

Fixes the ways a plug could be left answering nothing, not even its web
page, until it was switched off and on.

- A download that stops part way, as a router restarting mid-transfer leaves
  it, is given up after five seconds and fetched again. The plug used to wait
  on it for ever.
- Never gives up on a request the plug has not answered yet: a plug that
  found one given up on stopped answering altogether.
- No stage keeps the plug busy for more than about a minute. Busy for much
  longer, a real plug stops answering (its energy meter's readings pile up),
  so the plug now spends at most 20 seconds retrying, and this computer waits
  out a router restart instead - up to five minutes - and runs the stage again
  by itself.
- Notices a stage that has stopped sooner; no stage waits more than five
  minutes.

## 1.1.0

Keeps going through a router restart or a dropped network, says why when it
cannot, and backs up each plug's Sonoff firmware before it is replaced.

- Backs up the plug's whole flash - Sonoff firmware, settings and eWeLink
  identity - as a stage of its own before Tasmota is written, so it can go
  back to Sonoff over USB-serial one day (`--no-backup` keeps none). Uses the
  installer firmware 4.4.2.
- Keeps each plug's records under its own MAC address.
- Rides out a router restart: the plug waits up to five minutes for its
  Wi-Fi to come back and carries on, and makes no request while it is down.
- When a stage on the plug stops, it says why straight away, in the plug's
  own words, instead of waiting half an hour; and if the plug could not tell
  this computer, its console is read for the reason.
- Notices a plug that has stopped within seconds; no stage waits more than
  eight minutes.
- Tries again by itself, up to three times, a stage that failed for a
  network reason and is safe to repeat.
- Picks up where it left off when the memory-layout rewrite's report was
  lost, or the official startup firmware's install was cut off part way -
  both used to leave a plug that could not be finished.
- Says what to do with a plug that answers nothing - switch it off and on,
  which is safe at that point - and waits for one still finishing a stage
  that was stopped.
- Waiting for you, to join a network say, no longer times out.
- Says when the installer download stopped part way, rather than blaming a
  firewall.
- Windows: sets up Python without progress bars, and no longer crashes
  looking for an installed Python that is not there.
- The emulator: runs several plugs side by side (`--instance N`), and
  restarts the router faithfully (`plug router off/on`).

## 1.0.0

The first numbered release: a stock Sonoff S60 to the official Tasmota, over
the air, with nothing opened or soldered.

- One command from start to finish (`start.sh`, or `start.cmd` on Windows),
  which also sets up the Python it needs.
- The UK S60TPG and the European S60TPF, on stock firmware 1.1.1 and 1.2.0.
- Ends byte for byte identical to a plug flashed over USB with the official
  Tasmota release, and checks that by reading the flash back.
- Resumable at any point, with every step checking the plug before it writes.
- Takes the plug's Wi-Fi from the plug itself; the password is never written
  to this computer's disk.
- Adds one temporary firewall rule for the plug when needed, and removes it
  again however the run ends.
- Retries Safeboot block transfers for up to a minute when the network drops.
- An unattended mode for testing (`--unattended`), and an emulated plug to
  test against (`tools/emulator/`).
- The tool's version in its log, in the test report, and from `--version`.
