# When it goes wrong

Every problem listed here actually happened while this was being built. The
screen only shows a line per stage, so when one stops, the full story is in
the run's log, `captures/plugs/<MAC>/runs/<date>/run.log` - the tool prints its path, and it is
safe to share: what identifies you, your network or your plug is written as
`<email>`, `<wifi-name>`, `<device-id>` and so on. Running
again with `--verbose` shows the same detail as it happens. The run's own
record is in `autoflash-state.json` in the same directory, with the phase evidence
beside it.

First thing to try, always: start it again - `start.cmd` on Windows, `sh
start.sh` on macOS or Linux (or `python3 tools/s60_autoflash.py run`; the
examples below use that form, and the start scripts take the same options).

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

On Windows the tool's rule names Python itself, so Windows Security does not
ask whether Python may use networks. If it was asked once before and "Cancel"
was chosen, that left a rule blocking Python - and Windows applies blocking
rules before allowing ones. The tool finds such rules, turns them off while it
runs, and turns them back on afterwards, all within the one permission
question. Running the terminal as administrator does not help and is not
needed: the tool asks for administrator rights itself, only for its firewall
rule.

What the tool cannot change, it says, before it starts and again if the plug
cannot connect:

- **Other security software** (Norton, McAfee, Bitdefender and the like)
  often has a firewall of its own, which can block the plug whatever Windows'
  firewall allows. Allow Python in there, or pause it for the conversion.
- **A computer managed by an organisation** can have its firewall set, by
  Group Policy, to ignore rules added on the computer itself. Ask whoever
  manages it, or use another computer. A work computer may not allow
  administrator rights at all, which the rule needs.
- **A network or software that inspects encrypted connections** - common at
  work or school - makes eWeLink's and GitHub's certificates untrusted. The
  tool says so; use another network, or pause that software.

Windows and macOS have not been through a real conversion yet, so a problem
there may be new. Please post it in the
[Test reports](https://github.com/adamsthws/Sonoff-OTA/discussions/1) thread - see
[Reporting how it went](../README.md#reporting-how-it-went) - and the fix
will help everyone after you. To ask for help, start a
[discussion](https://github.com/adamsthws/Sonoff-OTA/discussions)
of your own or open an [issue](https://github.com/adamsthws/Sonoff-OTA/issues)
rather than asking in the thread.

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

**An earlier installer's password is not on the clipboard**

Only an installer from an earlier release has a password on its network. Copying
it needs `xclip`, `xsel` or `wl-copy` on Linux (`pbcopy` and `clip` are built
into macOS and Windows). Without one, type it: `s60-ota-bridge`.

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

**"Your S60 did not answer on this network"**

The tool lists each S60 in your eWeLink account with what it found:

- **"eWeLink shows it offline"** - the plug is switched off at the wall, or
  not on Wi-Fi. Switch it on and wait for the app to show it online.
- **"answers at ..., but eWeLink shows it offline"** (under *"Your S60 is
  on this network but cannot reach the internet"* when it is the only
  problem) - the plug is on your network but cannot reach the internet,
  usually because the router keeps it LAN-only. The conversion is sent to
  the plug through eWeLink, so allow it internet access, wait for the app to
  show it online, then choose *Look again*. You can block it again once it
  runs Tasmota.
- **"is at ... but does not answer on the network"** - the plug is on your
  network but LAN Control is off. Turn it on in the plug's settings in the
  eWeLink app, then choose *Look again*.
- **"is online in eWeLink but was not seen on this computer's network"** -
  this computer is on a different network from the plug, such as the 5 GHz
  or guest Wi-Fi, a different router, or a VPN. The message names the network
  the plug is on.

**"No Sonoff S60 in the eWeLink account ..."**

The plug is in a different eWeLink account. The message lists what the
account does have; choose *Use a different eWeLink account*.

**"Could not reach eWeLink"**

The tool could not sign in to see your account, so it lists every Sonoff plug
that answers on the network instead. The next stage needs eWeLink too, and
checks there that the plug you chose is an S60, so check this computer's
internet connection first.

**"did not answer within 180s"**

- Check it is actually powered on. This has caught everyone at least once.
- Its address may have changed. The tool records the plug's hardware address
  at the start and finds it again on a new address by itself; it only asks
  when the plug cannot be found at all. Your router's device list shows it
  under a name starting `tasmota-`.
- Your Wi-Fi may have gone, a router restarting, say. Part way through, the
  plug has no network of its own to fall back on: it keeps trying yours and
  rejoins within seconds of it coming back, and the tool waits for it.

**The plug's own network**

Wherever the tool asks you to join the plug's own network, it names it. The
installer, the mover and ESPHome all call theirs `Sonoff-Setup-` and the last
four characters of the plug's hardware address, such as `Sonoff-Setup-CDEF`.
Official Tasmota names its own after the plug, `tasmota-` and six characters,
such as `tasmota-ABCDEF-1234`; sometimes it shows up instead as `ESP_` and six
characters one higher, such as `ESP_ABCDF0` - the same network, and the tool
looks for both. All of them are open, with no password, and the plug's page on
them is at `http://192.168.4.1`. (An installer from an earlier release calls
its network `S60-OTA-Bridge-XXXX`, with the password `s60-ota-bridge`; the
tool says so when it sees one.)

## The first install

**"Device is offline; refusing to send" straight after a complete download**

That means it worked. The installer firmware does not talk to eWeLink, so the
cloud reports the plug offline, and the tool refuses to send the update twice.
The tool checks for the installer's `Sonoff-Setup-XXXX` network and, when it
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
clears its settings on the seventh, and opens its setup network as on a
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

## When a migration stage stops

The stages that check, stage and rewrite the plug's memory run on the plug
itself, and report back to this computer. When one stops, the plug says why,
and the tool shows it:

**"... did not pass: the plug says: ..."**

The plug's own reason. Two kinds:

- **One of its checks refused** - "NVS changed since preflight", "source table
  changed", and the like. Nothing was written. Something changed on the plug
  between stages; run again, and the earlier checks are done again first. If
  the same refusal comes back, report it rather than forcing past it.
- **"... returned HTTP -1 after N attempts"** - the plug could not reach this
  computer. Usually security software or a firewall that changed mid-run, the
  computer going to sleep, or a weak Wi-Fi signal (the tool warns when it is
  below -75 dBm). These stages are safe to repeat, so the tool tries them
  again by itself, up to three times, before stopping.

**"the plug stopped the ... without reaching this computer"** or **"the plug
has sent nothing for 2 minutes"**

The plug lost its connection to this computer, so its reason could not arrive.
The tool then reads the plug's own console and shows the last lines, marked
`The plug's console:`, and gives the plug's reason from there ("its console
says: ..."). Then as above.

**How long it waits**

A stage keeps the plug busy for under a minute, and while it does the plug
answers nothing else. Kept busy for much longer, a real plug stops answering
until it is switched off and on (its energy meter's readings pile up), so the
plug never spends more than 20 seconds in all trying a request again. If its
Wi-Fi drops - a router restarting, say - the stage stops, and the tool waits up
to 5 minutes for the plug to answer again, then runs it again by itself (three
tries in all). The tool checks on the plug whenever it goes quiet, so a plug
that has stopped is noticed within about 20 seconds; no stage is waited on for
more than 5 minutes in all. Waiting for you - to join a network, say - has no
time limit.

**"The plug is not answering at all"**

A plug can be left answering nothing - not even its web page - though it
still answers ping: kept busy too long, or left waiting on this computer when
the tool was stopped mid-stage. Give it two minutes; if its web page still does
not answer, switch it off and on. At every point this can happen that is safe: staging
writes only the spare half of memory, the rewrite downloads what it needs
before it writes anything, and installing the official startup firmware
carries on where it stopped. Then run this again.

**"The plug is on the network but not answering yet"**

A stage stopped part way (Ctrl-C, say) keeps the plug busy until it gives up,
for up to a minute - and while busy, the plug answers nothing else. Run again straight away and the tool waits for it, then
carries on.

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
before), Tasmota starts up asking to be configured. The tool names its setup
network; join it (open, no password) and the tool sends your Wi-Fi details
itself. If this computer cannot see Wi-Fi networks, the tool cannot spot it
either: when it asks what to do about a plug it cannot find, choose *It is
showing its own Wi-Fi network*.
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

## A converted plug that has lost your Wi-Fi

After a new router or a changed Wi-Fi password, the plug cannot join any more.

- **ESPHome** opens its own network by itself after a minute and a half
  without your Wi-Fi (`Sonoff-Setup-` and four characters). Join it, and its page
  asks for your Wi-Fi. It keeps trying your Wi-Fi meanwhile, so after a router
  restart it simply rejoins.
- **Tasmota** keeps trying your Wi-Fi and never opens a network by itself, so
  a router restart cannot leave it waiting on one. Press its button 6 times
  quickly: it opens its setup network for 3 minutes (`tasmota-` and the plug's
  name, or `ESP_` and six characters). Join it, browse to `http://192.168.4.1`
  and choose your Wi-Fi.

## Checking a finished plug at any time

```sh
python3 tools/s60_autoflash.py step H1 --plug-ip 192.168.1.50
```

Reads four areas of the plug's memory - the bootloader, the memory map, Safeboot
and Tasmota - and compares them against the pinned files. Read-only - safe to
run whenever you want reassurance.
