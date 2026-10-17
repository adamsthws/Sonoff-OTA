# Changelog

The conversion tool's own versions (`x.y.z`), not any firmware's. A patch
release fixes something; a minor one adds something; a major one is for a
change that stops a run started on an earlier version from being resumed, or
removes an option.

The version is set in one place, `tools/autoflash/__init__.py`. Merging a new
one to `main` tags it and publishes a release with its notes from here, so add
the section below before changing the number.

## 2.0.0

ESPHome, as well as Tasmota: the run asks at the start which the plug should
end up running. And a plug can move between them later, or back to Sonoff's
own firmware.

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
- The plug's own network is looked for by this plug's names only - Tasmota's,
  and the `ESP_` name it sometimes shows instead, which was missed before - so
  a neighbour's Tasmota is never mistaken for it. Wherever you are asked to
  join it, its exact name is given.
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
- A run started again (`--new`) after the memory layout was rebuilt for
  Tasmota, before official Tasmota was set up, took the plug for one on its
  way from ESPHome and sent it round steps that need Sonoff's layout, which
  never finished. This computer's record of the plug now tells the two
  apart, and the run goes on from official Tasmota.
- A plug offline in eWeLink when the installer is to be sent - after a send
  cut off part way, say - is waited for for up to three minutes, then the
  run says to switch it off and on, where before it was refused at once
  and the firewall blamed.
- An upload of official Tasmota to the startup firmware that stalls is
  noticed after two minutes, not ten, and tried again from a restart - up
  to three times - before it is left to be done by hand.
- A plug that cannot be found can be set up through its own network by
  choice, for a computer that cannot see Wi-Fi networks. While waiting for a
  plug whose Wi-Fi went, the run says it will rejoin by itself, and that it has
  no network of its own at that stage.

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
