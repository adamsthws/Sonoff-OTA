# Staying on the right side of copyright

This project lets you back up the Sonoff firmware on a plug you own, and put
it back later. Sonoff's firmware is Sonoff's copyright, so this page sets out
what the project does, and deliberately does not do, to keep that use lawful.
It is not legal advice.

## The short version

- The backup is of **your plug**, is kept **on your computer**, and is only
  ever written back to **the same plug**.
- The project **never distributes Sonoff's firmware**, in whole or in part.
- The tool updates through the plug's **ordinary update mechanism**, started
  from **your own eWeLink account**. It breaks no encryption, signature or other
  protection.

## What the law allows

Copyright law in most places lets the lawful user of a program make a backup
copy of it, and study how it works so that other software can work with it:

- **UK:** the Copyright, Designs and Patents Act 1988, s.50A (backup copies),
  s.50BA (observing, studying and testing) and s.50B (decompiling for
  interoperability).
- **EU:** the Software Directive 2009/24/EC, Art. 5(2), 5(3) and 6. The backup
  right cannot be removed by contract.
- **US:** 17 U.S.C. §117, which lets the owner of a copy of a program make an
  archival copy. Firmware that comes with a device you bought is generally
  treated as a copy you own.

None of these rights cover giving the copy to anyone else, and that is where
the line is drawn below.

Right-to-repair laws mostly require manufacturers to provide parts, tools and
information. They support what this project does, but they do not grant
copyright permissions, so the project relies on the copyright exceptions above.

## What the project does

### The backup stays with you and your plug

- The backup is read from the plug during a conversion and saved under
  `captures/plugs/<MAC>/sonoff-backup/`
  ([Your Sonoff backup](technical-details.md#your-sonoff-backup)).
  `captures/` is Git-ignored, so a backup cannot be committed by accident.
- Each backup is filed under the MAC address read from the chip's own eFuses,
  not one reported over the network.
- **Back to Sonoff's firmware** only writes a plug's own backup to that plug, and
  is only offered when that backup is on the computer doing it. It is never
  used to put one plug's firmware onto another. Each backup also holds that
  plug's eWeLink device id and keys, which belong to that plug alone.
- The backup is written once and kept read-only. Converting a plug again keeps
  its first backup.

### No Sonoff firmware is distributed

- No Sonoff firmware image, and none of Sonoff's code, is in the repository
  or its history. The firmware files committed are this project's own builds,
  builds of Tasmota and ESPHome, and two data files
  ([THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) lists their sources and
  licences).
- One of those data files, `artifacts/sonoff-s60-partition-table.bin`, was read
  from a stock plug. It is the standard ESP-IDF partition table every S60
  ships with: a short, fixed-format list of partition names, offsets and
  sizes, with no code or settings in it. The tool needs it to check that a
  plug's layout is the one it expects, and to put that layout back when a plug
  returns to Sonoff's firmware.
- The docs describe the firmware format in prose and tables. They do not list
  Sonoff's code or data.
- The emulator ([emulator.md](emulator.md)) boots **your own** backup, and its
  image does not contain one. `tools/emulator/patch_stock.py` changes a copy
  of your backup so it runs under QEMU. It checks the bytes it replaces are as
  expected, but carries none of Sonoff's code beyond those few instructions,
  and never changes the backup itself.

### Nothing is circumvented

The tool does not 'get around' technological
protection measures.

- It sends the plug the same kind of upgrade command Sonoff's servers send,
  from the plug owner's own eWeLink account. The plug runs every check it
  normally does, including the SHA-256 check on the firmware it downloads
  ([How it works](what-happens.md)).
- The plug's firmware is not encrypted and does not check signatures, and the
  tool does not disable or work around any such feature.
- The optional router rules ([router-rules.md](router-rules.md)) keep a stock
  plug as it is by stopping it from installing updates. They do not defeat
  anything.


### Signing in to eWeLink

- The tool signs in to eWeLink as you, with your own account, to talk to your
  own plugs. It does not keep your password or the access token
  ([How your eWeLink account is handled](technical-details.md#how-your-ewelink-account-is-handled)).
- The application id it signs in with is the public one used by the
  open-source SonoffLAN integration. It is not taken from Sonoff's own app.

### Names

Sonoff and eWeLink are trademarks of their owners. The project names them only
to say which devices and services it works with, and states that it is
independent and not an official installer
([THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)).

## What we ask of you

- **Do not share your backup.** It is Sonoff's firmware, and it also holds your
  Wi-Fi password and your plug's eWeLink keys in plain text.
- **Do not attach a Sonoff backup, or any part of one, to an issue or pull request.**
  Describe what you see instead, for example the offsets and sizes involved,
  the Sonoff version and the log lines. A pull request that adds Sonoff
  firmware or code taken from it will not be accepted.
- **Use a backup only on the plug it came from.**
