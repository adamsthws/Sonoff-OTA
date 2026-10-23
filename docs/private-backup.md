# The maintainer's sealed Sonoff backup

This page is for the project's maintainer. Nobody converting a plug needs it,
and nothing here is part of a conversion.

The emulator ([emulator.md](emulator.md)) boots a real plug's Sonoff backup:
Sonoff's firmware, with that plug's eWeLink keys and the Wi-Fi it was set up on.
The project has exactly one, and can never make another - every plug it had is
converted. `tools/private_backup.py` keeps a copy of it off the maintainer's
computer without it ever being public: sealed on the maintainer's side, then
kept in a **private** package on GitHub's container registry. It is not in the
repository - not its history, a clone, a fork or a release - so
[legal.md](legal.md)'s "no Sonoff firmware is in the repository" stays true.

## What is sealed, and how

The backup directory as a conversion filed it,
`captures/plugs/<MAC>/sonoff-backup/` - `flash.bin`, `efuse.bin`, `report.json`,
`README.txt` - in a tar with a manifest of each file's size and SHA-256,
compressed with xz (4.2 MB to 1.0 MB: a flash image is mostly empty) and then
encrypted with AES-256-GCM. Encryption comes last because encrypted data does
not compress. The sealed file is one line, `AUTOFLASH-SEALED-BACKUP 1`, a
12-byte nonce, and the ciphertext with its tag; the line is the associated
data, so any AES-GCM implementation can open it, and any change to any byte is
found. Sealing proves the result opens to the backup before writing it;
opening checks every file against the manifest sealed with it.

Only the sealed file ever leaves this computer: `upload` refuses anything else.

The firmware in `artifacts/` is not compressed, nor needs to be: Git already
stores every file compressed, and the tool and its tests use those files as
they are, pinned byte for byte.

## The key

A random 256-bit key, made once by `tools/private_backup.py key`, which writes
it into `.env` as `AUTOFLASH_BACKUP_KEY` and never shows it. Without it the
sealed backup cannot be opened, by anyone - so keep a copy **off this
computer**, in a password manager: a `.env` lost with the laptop takes the
original backup and the only key with it. A new key is never written over an
old one, which would leave the sealed copy unopenable.

`tools/private_backup.py secret` sets it as the repository's Actions secret of
the same name, for a workflow that one day opens the backup on GitHub's side
(see below). GitHub never shows a secret back, so that copy is not one to
recover it from.

`.env` holding the key still loads for a conversion (`tools/autoflash/credentials.py`
knows the name, and keeps the value out of the log), and the emulator never
hands it to a run (`tools/emulator/unattended-test.sh`).

## Where it is kept

`ghcr.io/adamsthws/autoflash-private:sonoff-backup`, a private package owned by
the maintainer's account, uploaded with ORAS - which keeps any file in a
container registry - run from its own image, pinned by digest, so nothing is
installed but Docker. A package's visibility is its own, whatever its
repository's, and this one is not linked to the repository: a package made
public cannot be made private again, so it is never changed in its settings.

Uploading and fetching use the GitHub CLI's login, which needs the package
scopes once:

```sh
gh auth refresh -h github.com -s write:packages,read:packages
```

## Commands

```sh
B=captures/plugs/<MAC>/sonoff-backup
python3 tools/private_backup.py seal "$B" sonoff-backup.sealed     # the sealed copy
python3 tools/private_backup.py verify sonoff-backup.sealed "$B"   # opens to exactly it? writes nothing
python3 tools/private_backup.py upload sonoff-backup.sealed        # into the package, then checks it is private
python3 tools/private_backup.py check-private                      # at any time
python3 tools/private_backup.py fetch sonoff-backup.sealed         # back out of it
python3 tools/private_backup.py unseal sonoff-backup.sealed OUT    # the backup, into a new directory OUT
```

Nothing writes over a file or directory that is there already, and `unseal`
leaves each file read-only, as the backup is kept.

### Shown private

`check-private` asks three things, and calls the package private only if all
three say so:

1. The registry, asked for the package as nobody - with no login, as anyone on
   the internet would - must not hand it over. So that this is a real test, it
   asks the same for a public package first (ORAS's own), which it must hand
   over: a check that cannot tell the two apart says so and fails.
2. GitHub's own setting for the package, read through the maintainer's
   account, must be `private`.
3. The package must be there, with its `sonoff-backup` tag, as the maintainer
   sees it - so that the first answer is "not for you", not "no such thing".

`upload` runs it straight after uploading.

### If this computer is lost

On any computer with this repository, Docker and the GitHub CLI logged in as
the maintainer (with `read:packages`): put the key from the password manager in
`.env` as `AUTOFLASH_BACKUP_KEY`, then `fetch` and `unseal` into
`captures/plugs/<MAC>/sonoff-backup/`, where the emulator looks for it. Its
`flash.bin` must have SHA-256 `4b6e3393…9f9bcc40`, as every copy has.

## On GitHub's side, one day

Nothing opens the backup on GitHub's side today. It is needed only to make the
public starting plugs again ([emulator.md](emulator.md#public-plugs)) and for
the whole fault suite, which run on the maintainer's machines. A workflow that
does - on runners of the maintainer's own, say - needs:

- the package to give the repository access (its settings, "Manage Actions
  access": read), and the workflow `permissions: packages: read`, so that
  `fetch` reaches it with the workflow's `GITHUB_TOKEN`;
- the `AUTOFLASH_BACKUP_KEY` secret (`tools/private_backup.py secret`);
- to run only on a schedule or by hand (`workflow_dispatch`), from `main`:
  never on `pull_request_target`, which would run a contributor's code with
  the repository's secrets and could open the backup. A pull request from a
  fork never gets the repository's secrets: once the package gives the
  repository access, a workflow such a request adds could fetch the sealed
  file, but never the key to open it. Keep "require approval" for outside
  contributors' workflows on (the repository's Actions settings). Forks get
  no packages at all.

The workflow unseals into the runner's own `captures/` and nowhere else, and
publishes nothing made from it but what `sanitise_plug.py` has shown public.
