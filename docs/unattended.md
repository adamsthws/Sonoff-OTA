# Unattended runs

For testing: a whole conversion with nobody at the keyboard, and the screen
it drew kept afterwards for review, colours and all.

## Give the answers in advance

Copy `.env.example` to `.env` (git ignores it) and fill it in:

```sh
cp .env.example .env
chmod 600 .env
```

| Name | What |
|---|---|
| `EWELINK_EMAIL` | the eWeLink account the plug is in |
| `EWELINK_PASSWORD` | its password |
| `S60_SUDO_PASSWORD` | Linux and macOS: this computer's password, only if sudo asks for one (for the temporary firewall rule) |
| `S60_WIFI_SSID`, `S60_WIFI_PASSWORD` | only for a plug on an ESPHome that cannot say its Wi-Fi password (one older than this project's 1.1.0, or whose API has an encryption key), when this computer has no Sonoff backup of the plug to take it from: the network it is on, and its password (none for an open network) |
| `S60_TASMOTA_PASSWORD` | only for a plug on Tasmota with a web password: that password. Without it, the run stops when it finds the plug |
| `S60_ESPHOME_OTA_PASSWORD` | only for a plug on ESPHome with an update password (`password:` under `ota:` in its config): that password. Without it, the run stops before the go-ahead to leave ESPHome |

The same names set in the environment win over the file, and
`--env-file PATH` reads another file instead. A misspelt name is refused,
not ignored. The values stay in the tool's memory: they are not passed on to
the helper tools' environment, and the passwords are kept out of the log.

The file is also read on an ordinary run, where it saves typing the account
details. If eWeLink refuses the password, it is asked for instead. The
emulator is the exception: its copy of the repository leaves `.env` out, so a
run you sit through there is asked everything, as a real one is, while
`tools/emulator/unattended-test.sh` hands `.env` to its run alone.

## Run it

```sh
python3 tools/unattended_run.py                       # recorded, as below
python3 tools/unattended_run.py -- --plug-ip 192.168.1.50 --new
sh start.sh run --unattended                          # the same, unrecorded
```

Anything after `--` goes to the run itself. `--timeout MINUTES` stops a run
that is still going after that long, as Ctrl-C would.

With `--unattended`:

- the "Before you start" list and the window-size note are not waited on;
- which firmware to install is not asked: say it with `--firmware tasmota` or
  `--firmware esphome`, or the run stops there;
- PROCEED, before the point of no return, is taken as typed;
- an unfinished run is carried on, as the menu's first option would (`--new`
  starts afresh instead), and an interrupted step is done again;
- a yes/no question takes its default, the cautious answer: a weak Wi-Fi
  signal, for one, stops the run;
- any other question stops the run, saying which question it was. Two plugs
  answering, say, or the Wi-Fi details not readable off the plug.

Each answer it gives is shown on the screen, marked `(unattended)`.

## The recording

`unattended_run.py` runs the conversion on a terminal of its own (a
pseudo-terminal), so it draws exactly what a person would see. On a
terminal, the run is shown live as well. When it ends, the screen as it was
left is rebuilt from the recording: it is printed when the output is not a
terminal (a CI log, say), and is kept in `captures/unattended/<time>/`
(`--save DIR` to choose):

| File | What |
|---|---|
| `screen.ansi` | the final screen, with its colours: `cat` or `less -R` it |
| `screen.txt` | the same without colours, to read or compare anywhere |
| `screen.raw` | every byte as written: `cat` replays it in a terminal |

The terminal is the same size as the one running it, or 100x40 with none.
`--size 120x50` sets it. Exit codes are the run's: 0 finished, 130 stopped
at a question, 143 stopped by `--timeout`, anything else failed. The run's
full log is in the run's `run.log`, under `captures/plugs/<MAC>/runs/`, as usual.

## In the emulator

`tools/emulator/unattended-test.sh --fresh` starts the emulator, converts the
emulated plug this way, prints its final screen and stops the emulator again.
See "Unattended, for testing" in [emulator.md](emulator.md#unattended-for-testing).

## Windows

The recorder is for Linux and macOS only. On Windows,
`start.cmd run --unattended` runs unattended but unrecorded, and the firewall
rule's UAC window still needs a click.
