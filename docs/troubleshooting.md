# When it goes wrong

Every problem listed here actually happened while this was being built. The
screen only shows a line per stage, so when one stops, the full story is in
the run's log, `captures/<name>/run.log` - the tool prints its path. Running
again with `--verbose` shows the same detail as it happens. The run's own
record is in `captures/<name>/autoflash-state.json`, with the phase evidence
beside it.

First thing to try, always:

```sh
python3 tools/s60_autoflash.py run
```

It picks up where it stopped. Re-running is safe - every step checks the plug
before writing anything.

---

## This computer

**"the plug did not fetch the ... instructions from this computer"**

The plug could not reach this computer. The tool adds a firewall rule itself
when it can see a firewall (ufw or firewalld on Linux, Windows Defender
Firewall, the macOS firewall), so this usually means a firewall it cannot see -
security software, or rules managed some other way - or that the addition was
declined. The message shows the command for the firewall it found. Nothing
was written; run again once the plug can get through.

On Windows, the tool's rule can be overridden by an earlier choice. The first
time Python listens for the plug, Windows Security may ask whether Python may
use networks; choosing Cancel there can add a rule that blocks Python, and
Windows applies blocking rules before allowing ones. Open Windows Defender
Firewall, then Advanced settings, then Inbound Rules, and look for a rule for
Python whose action is Block. Delete it, or set it to Allow, and run again.
Running the terminal as administrator does not help and is not needed: the
tool asks for administrator rights itself, only for its firewall rule.

Windows and macOS have not been through a real conversion yet, so a problem
there may be new. Please post it in the
[Test reports](https://github.com/adamsthws/Sonoff-OTA/discussions/1) thread - see
[Reporting how it went](../README.md#reporting-how-it-went) - and the fix
will help everyone after you.

On Linux, blocked connections show in the kernel log:

```sh
journalctl -k | grep "SRC=192.168.1.50"
```

**The computer's address changed mid-run**

A cable unplugged, or a move from wired to Wi-Fi. The tool works out this
computer's address afresh each time and says when it has changed, so nothing
needs doing. If it chooses the wrong connection, name the address yourself:

```sh
python3 tools/s60_autoflash.py run --listen-ip 192.168.1.20
```

Stay on one connection from the rebuild onwards, ideally wired.

**Joining the plug's network: "waiting for this computer to join"**

The tool carries on as soon as the plug answers at 192.168.4.1, so it waits
until the computer has actually joined. If the computer keeps dropping off the
plug's network, close any setup page the system opened by itself: on Tasmota's
setup network every page view makes the plug scan for networks, which briefly
takes its own network away.

If your home network itself uses 192.168.4.x addresses and the computer also
has a wired connection, the two clash; unplug the cable while joined to the
plug's network.

**The bridge password is not on the clipboard**

Copying needs `xclip`, `xsel` or `wl-copy` on Linux (`pbcopy` and `clip` are
built into macOS and Windows). Without one, type it: `s60-ota-bridge`.

## The router rule (no longer used)

The tool no longer touches your router. The plug downloads our firmware straight
from your computer, so there is nothing to redirect - whether or not Sonoff is
offering the plug an update. If you are following an older guide that told you to
add a destination-NAT rule, you can skip that step entirely.
[router-rules.md](router-rules.md) keeps the old instructions, says why the rule
was dropped, and covers a simpler rule that stops a stock plug installing Sonoff
updates.

## A plug from another region

Every plug converted so far is a UK S60TPG. The European S60TPF is expected to
work the same way (see [Regional variants](../README.md#regional-variants-of-the-s60-plugs)), and the
tool checks the plug at two points rather than assuming:

- **"... is a ..., not a Sonoff S60 (SN-ESP32C3-S60-01)"** - the plug reports a
  different firmware family. The run stops here and nothing has been written;
  the plug is still on its Sonoff firmware.
- **The read-only check before the rewrite (step E1) stops the run** - the
  plug's memory layout is not the one the S60TPG has. By then the plug runs
  Tasmota and keeps working; only the move to the official layout is refused.
  Leave it as it is and report it rather than forcing past the check.

Either way, or if it converts cleanly, please post in the
[Test reports](https://github.com/adamsthws/Sonoff-OTA/discussions/1) thread - see
[Reporting how it went](../README.md#reporting-how-it-went) for what to
include. Reports from other regions are very welcome.

## The plug can't be found

**"did not answer within 180s"**

- Check it is actually powered on. This has caught everyone at least once.
- Its address may have changed. The tool records the plug's hardware address
  at the start and finds it again on a new address by itself; it only asks
  when the plug cannot be found at all. Your router's device list shows it
  under a name starting `tasmota-`.

## The first install

**"Device is offline; refusing to send" straight after a complete download**

That means it worked. The installer firmware does not talk to eWeLink, so the
cloud reports the plug offline, and the tool refuses to send the update twice.
The tool checks for the installer's `S60-OTA-Bridge-XXXX` network and, when it
can see it, carries on by itself. If this computer's Wi-Fi is off it cannot
look; turn it on and run again.

**"Device model is ..., not the S60's"**

Another Sonoff on the same account answered. The tool converts only the S60
and stops before sending anything. Run again and choose the S60 from the list.

**"Empty reply from server" when you try `curl` yourself**

Tasmota refuses commands that arrive without a `Referer` header, as a security
measure, and refuses by closing the connection - which looks exactly like the
device being switched off. Add the header:

```sh
curl -H "Referer: http://192.168.1.50/" "http://192.168.1.50/cm?cmnd=Status%2011"
```

The tool already does this.

**Tasmota never appears after the installer gave it your Wi-Fi**

With the bridge and a captured settings template, Tasmota starts with the Wi-Fi
details the installer had just joined with, and joins by itself. If the details
stop working (say the router restarted onto another channel and refuses the
plug), Tasmota keeps retrying them and never opens its setup network.

Switch the plug off and on 7 times in a row, leaving it on for only 2-3
seconds each time, then leave it on. Tasmota counts quick restarts like that,
clears its settings on the seventh, and opens `tasmota-XXXXXX-NNNN` as on a
first install. The tool, still waiting, carries on with the setup network.

## Uploading firmware

**The upload finishes suspiciously fast and nothing changes**

A firmware upload to this plug takes tens of seconds. If it "succeeds" in under
ten, it did not write anything. Tasmota needs the file's size passed in the
request, and silently commits nothing without it. The tool sends it; if you are
uploading by hand, use the plug's own web page rather than a bare `curl`.

**"the plug rejected the firmware"**

The message includes what the plug said. Usually it means the file is wrong -
check `sha256sum -c SHA256SUMS` passes and that you are uploading the file the
tool named, not the `.factory.bin`.

## Slots and restarts

**"the plug is still running from slot 0 after 2 uploads"**

Each upload writes the *other* half of memory and restarts into it, so the slot
alternates every time. The tool handles one swap-back automatically. If it
reports this, uploads are not being committed at all - see the "suspiciously
fast" entry above.

**A step says it was interrupted**

The tool marks a step before it starts writing, so an interrupted run is
visible rather than silent. It will check the plug and tell you whether the
step got far enough, then let you choose. Take the recommendation unless you
know better.

## After the rebuild

**The plug is broadcasting its own Wi-Fi network**

Normally Tasmota joins your Wi-Fi by itself after the rebuild: the temporary
startup firmware saves your details where it reads them. If it could not (it
saves nothing when its own settings write fails, and the plug then behaves as
before), Tasmota starts up asking to be configured. The tool names the
`tasmota-XXXXXX-NNNN` network; join it (open, no password) and the tool sends
your Wi-Fi details itself.
If the plug cannot connect with them it says so and leaves the plug waiting -
nothing is saved - and you can set the Wi-Fi up at `http://192.168.4.1`.

**Safeboot is showing instead of Tasmota**

Safeboot is the small recovery firmware. Seeing it after the rebuild is normal;
seeing it after the whole run has finished is not. It means the full Tasmota
was never installed - re-run and let the tool redo that step.

Note that Safeboot has no Wi-Fi setup of its own. It relies on settings already
saved on the plug. That is why the conversion uses a temporary version with
Wi-Fi details built in, and swaps it for the official one at the very end.

## The worst case

**Power was lost while the memory layout was being rewritten**

If the plug does not come back at all after that step, Wi-Fi recovery is not
possible. The plug would need opening and a USB-serial adapter connected to the
chip inside. See
[reference/recovery-1.1.1.md](../archive/docs/reference/recovery-1.1.1.md).

This is the risk you accepted at the start, and it is the only failure in this
process that cannot be fixed by running the tool again.

## Checking a finished plug at any time

```sh
python3 tools/s60_autoflash.py step H1 --plug-ip 192.168.1.50
```

Reads three areas of the plug's memory and compares them against the official
Tasmota files. Read-only - safe to run whenever you want reassurance.
