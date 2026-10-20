# Technical details

This page covers what the [README](../README.md) leaves out. You don't need it
to convert a plug. It covers what the firmware bundle holds, how your Wi-Fi
details and eWeLink account are handled, the finished memory layout, and where
the developer and research material lives.

For a plain-English account of what happens to the plug, read
[what-happens.md](what-happens.md) instead.

## The firmware bundle

The first run prepares a bundle: it downloads the official Tasmota release
files, checks every one against a pinned SHA-256, and writes the bundle into
`captures/`, which git ignores. It asks nothing: the bundle is built without
your Wi-Fi details, which are added later in the run. To rebuild it on its own:

```sh
python3 tools/prepare_imprinted_bundle.py --without-wifi
```

### What is in it

Partway through the conversion the plug needs two Tasmota images that are not
part of the ordinary release download:

- [`artifacts/tasmota32c3-bluetooth.bin`](../artifacts/tasmota32c3-bluetooth.bin)
  is the Berry-capable ESP32-C3 build used while the old partition map is still
  active. It is pinned byte-for-byte and includes its upstream source revision
  in [the artifact notes](../artifacts/README.md).
- [`artifacts/s60-recovery-safeboot-imprintable.bin`](../artifacts/s60-recovery-safeboot-imprintable.bin)
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
taken from them, all pinned by hash so nothing can be swapped underneath you.
It needs no flash dump: the tool reads the plug's partition table and settings
live, and checks them, before anything is written.

The published images remove the need to compile Tasmota locally. They do not
remove the safety checks: every image and every live flash readback still has
to match its pinned provenance.

## How your Wi-Fi details are handled

Partway through, the plug runs a small temporary startup firmware that has to
join your Wi-Fi by itself, so your network name and password are written into
it. You are not asked for them: by that point the plug is running Tasmota on
your network, and the tool reads them from the plug's own configuration into
memory. The plug is connected with them at that moment, which proves they
work, and the tool checks the name against the network the plug reports
being on. Nothing is typed, and nothing is written to disk.

Earlier still, the installer reads the Wi-Fi the Sonoff firmware saved, joins
that network, and gives the first Tasmota its settings with the details in, so
it joins too. The temporary startup firmware later saves them where official
Tasmota reads them, so the finished plug joins by itself as well.

If the details cannot be read or given automatically, the tool says why and
asks for them - only then, and only once. With `--wifi` they are asked for
instead, typed twice, since Tasmota never reports its password for checking.

Apart from the Sonoff backup (below), your Wi-Fi password is never written
to disk. It is held in memory for the run only; the temporary startup firmware built from it is built in memory and
handed to the migration server through a pipe, never as a file. The plug loses
the password at the end of the run, when the official startup firmware
replaces the temporary one.

Other secrets are not kept either, outside the backup: the plug's eWeLink
LAN key is never saved, and the update links, the plug's settings memory and staged firmware
are deleted or reduced to hashes as soon as they have been checked - however
the run ends. `python3 tools/autoflash_cli.py cleanup` deletes any such file
still on disk, such as after the computer lost power mid-run. It leaves the
backups alone.

### Your Sonoff backup

Stage 6 of the run, "Backing up the Sonoff firmware", comes straight after
the installer is sent and before anything is written over the Sonoff
firmware. It is covered by the PROCEED typed at stage 5, whose warning says
the backup will be taken (or that the plug has one already); nothing reaches
the plug before PROCEED. The stage reads the plug's whole 4 MB flash through the installer (bridge 4.4.2's read-only
`GET /flash` and `GET /efuse`), checks it byte for byte against the plug -
the installer works out the SHA-256 of its flash on the plug itself
(`GET /flash-sha256`), and a backup whose hash differs is refused and read
again - and keeps it with the plug's other records,
under its MAC address - which no other plug has:

```text
captures/plugs/58-cf-79-01-02-03/
    plug.json              the plug's eWeLink name and id, model, Sonoff version
    sonoff-backup/         the backup
    runs/20261013-031500/  each conversion of it: its state and its log
```

A run starts in `captures/plugs/unidentified/runs/` and moves under the MAC as
soon as the plug is found, before anything is written to it. Runs from before
this layout, kept straight under `captures/`, are moved there the next time
the tool starts if they know their plug's MAC; the rest stay put and still
work. The MAC is taken from the chip's own eFuses for the backup, so it is
filed under the plug it came from even if the network reported another.

The backup holds:

- `flash.bin` - the whole chip: bootloader, partition table, the Sonoff
  firmware, its settings and its eWeLink pairing;
- `efuse.bin` - the chip's eFuse registers, which only the emulator needs;
- `report.json` - what the backup holds, checked when it was taken;
- `README.txt` - how to use it.

It is the original: written once, read-only, never replaced. A plug that is
converted again keeps its first backup, and the run says so in the warning
before PROCEED rather than reading the plug again. Like every stage, the
backup shows as one line on screen; what it found - the partitions, the
Sonoff version, where it was kept - is in the run's log, and on screen with
`--verbose`. Only a failure is shown: after two failed reads the run says
the conversion can go on without a backup, though that is not recommended,
and offers to try again, to carry on without one, or to quit - which leaves
the plug on the installer, to carry on from there next time or go back to
Sonoff with a power cycle. Carrying on marks the stage "Skipped", in green:
the conversion is not affected. An unattended run carries on without one.
The test report says how the backup went - completed, failed, or not taken -
and never where it is or which plug it came from. `python3 tools/autoflash_cli.py list` shows
every backed-up plug.

It is for two things. **Going back to Sonoff:** writing `flash.bin` back
over USB-serial (`esptool.py --chip esp32c3 write_flash 0x0 flash.bin`)
returns the plug to the Sonoff version, settings and pairing it had - but
that means opening the plug, which is what the over-the-air route avoids.
One of the two app slots holds the installer rather than an older Sonoff
version; the plug boots the other. Restoring it over the air, from Tasmota,
is not built yet. **The emulator:** `tools/emulator/emu.sh` offers every
backed-up plug by its MAC and runs a copy of the one you choose
([docs/emulator.md](emulator.md)).

**It holds secrets in plain text:** your Wi-Fi password, as the Sonoff
firmware saved it, and the plug's eWeLink keys. The directory and files can
be read by your user only, `cleanup` keeps them, and they never belong in
an issue or a discussion. Delete them if you do not want them, or convert
with `--no-backup` to keep none. Reading the flash takes under a minute.

### What crosses the network

Not written to disk is not the same as never exposed. The password crosses
your local network up to three times during a run, in plain HTTP:

- inside the Sonoff backup, as the installer sends the plug's flash to the
  tool (not with `--no-backup`). While it runs, the installer answers anyone
  on your network - it also takes any firmware it is sent, which could read
  the same memory, so reading it adds no new way in;
- when the tool reads it, in the plug's configuration backup (skipped if you
  typed the details instead). This is the same `/dl` download as
  Configuration → Backup Configuration in Tasmota's web UI. The file is only
  lightly obfuscated: each byte is XORed with a fixed key, by a published
  method that tools such as Tasmota's `decode-config.py` reverse, and the
  password inside is plain text;
- when the temporary startup firmware is uploaded, with the password written
  into it as plain bytes.

Tasmota's own web password, if one is set, also travels unencrypted - from
the tool just as from a browser logging in.

That risk is accepted on purpose. Tasmota's web UI has no HTTPS, so every
settings backup taken from a browser exposes the password the same way;
the run adds nothing an ordinary backup does not. And the password only gets
someone onto the network the plug is already on - which anyone able to watch
this traffic is on already.

Keeping the password on the plug throughout was considered: Berry could
write it into the startup firmware on the plug, or leave it in flash for that
firmware to read. That is not worth it to close two brief exposures in a
process that is no more exposed than everyday Tasmota use. If traffic on your
local network is a concern, the answer is to separate it, for example with an
IoT VLAN, not to change this tool.

## How your eWeLink account is handled

The run signs in to your eWeLink account twice: once to find the plug, by
matching the S60s in the account against what answers on your network, and
once to send the plug the cloud update command that installs the bridge. Both
go straight to eWeLink's own servers over HTTPS, and the update command over
an encrypted WebSocket. Nothing about the account passes through anyone else.

Your eWeLink password is asked once per run and held in memory only. It is
never written to disk, to the run state or to the log, and never put on a
command line: the delivery tool, `serve_tasmota_ota.py`, receives it through
the `EWELINK_PASSWORD` environment variable and clears its copy as soon as it
has signed in. The access token eWeLink hands back is not kept either.

What is kept is the account's email, so the next run can offer it first. It
is in `captures/settings.json` and the run's state file, both inside
`captures/`, which git ignores. In the run's log it is written as `<email>`,
along with the plug's device id, name and hardware address.

The device list eWeLink returns includes each device's LAN key (`devicekey`):
not the device id, and not an account login, but the secret its LAN Control
mode encrypts local traffic with. eWeLink sends it whether asked or not. The
run does not use it: it is never logged, and is left out of everything the
run records about the plug. It stays in memory only while the run needs the
account, and is gone when the run's processes exit. It stops mattering once
the plug runs Tasmota.

For an unattended run (testing, below), the email and password can be given
in advance instead, in a `.env` file at the top of the checkout. Git ignores
it, and so do the AI coding tools the repository has settings for (Claude
Code, Copilot, Cursor, Windsurf, Aider, Gemini CLI). What it holds is read
into the tool's memory only: it is not passed on to the helper tools'
environment, and the passwords are kept out of the log. A password given
this way is tried once; if eWeLink refuses it, it is asked for instead.

A throwaway eWeLink account is fine, and limits what a mistake could expose:
once the plug is converted, the account is no longer needed.

## Locks on the destructive steps

Besides the PROCEED confirmation, the destructive steps are locked by default:
the `RECOVERY_LOCK` and `REPARTITION_LOCK` files in this repository have to be
deliberately renamed before the underlying tools will run them on their own.

## The finished memory layout

Official Tasmota on the official partition layout - the same arrangement you
would get from flashing the plug over USB:

| Area | What's in it | Size |
|---|---|---|
| `nvs` | Settings | 20 KiB |
| `otadata` | Which firmware to start | 8 KiB |
| `safeboot` | Recovery firmware for future updates | 832 KiB |
| `app0` | Tasmota itself | 2,880 KiB |
| `spiffs` | File storage | 320 KiB |

That gives 2,880 KiB of room for firmware instead of the 1,984 KiB the Sonoff
layout allowed, plus a filesystem the plug never had.

## For developers

The user-facing tool sits on top of a set of smaller, single-purpose scripts in
`tools/`, each of which can be run on its own. `archive/` holds the research
tools, the bridge firmware source and the custom Tasmota build that got the
early plugs converted. None of it is needed now, and none of it has been
deleted. The research behind all of it - how the update mechanism was worked
out, the partition analysis, the reviewed migration plan and the manual
procedure - is in **[archive/docs/reference/](../archive/docs/reference/)**.

The tests check the tool against itself; nothing touches the plug:

```sh
python3 -m unittest discover -s tests
```

To try a change against a plug without converting a real one, the emulator in
`tools/emulator/` runs the whole conversion against a virtual S60 booting a
real stock flash dump, in Docker. It has taken a UK S60TPG on stock 1.2.0
through every stage to the official layout. On a Linux computer with KVM it
also runs the tool on Windows beside the plug, in a Windows that starts clean
every run. What it needs, how to set it up, use it and start over, how it
works, and what had to be changed in the emulator to get there are in
**[emulator.md](emulator.md)**.

For automated testing, `run --unattended` runs a conversion with nobody at
the keyboard. The eWeLink account comes from `.env`, PROCEED is taken as
typed, yes/no questions take their cautious default, and any other question
stops the run, naming it. `tools/unattended_run.py` runs it on a
pseudo-terminal, records everything it draws, and rebuilds the final screen,
colours included, for review. `tools/emulator/unattended-test.sh` does all of
that against the emulated plug in one command. How each piece works is in
**[unattended.md](unattended.md)**.

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
[part-2-safeboot-recovery-contingency.md](../archive/docs/reference/part-2-safeboot-recovery-contingency.md).

[automatic-wifi.md](../archive/docs/reference/automatic-wifi.md) records how
the network switches and Wi-Fi typing were removed, what was ruled out, and
why. [lessons-learned.md](../archive/docs/reference/lessons-learned.md)
collects the pitfalls met along the way.

Start with [manual-procedure.md](../archive/docs/reference/manual-procedure.md)
if you want to do it by hand, and
[ota-findings.md](../archive/docs/reference/ota-findings.md) for how the
firmware format was decoded.
