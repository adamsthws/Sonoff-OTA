# Adopting a plug into Home Assistant

This guide is for any plug the tool has converted to ESPHome. Home Assistant's
ESPHome builder finds the plug on your network and offers to adopt it. If you
accept, the builder writes a short config for your plug, with its name, a new
API key and your Wi-Fi details. Everything else comes from this project's config
for your kind of plug:

```yaml
packages:
  adamsthws.sonoff-s60: github://adamsthws/firmware-liberator/devices/sonoff-s60/esphome.yaml@v2.3.0
```

From then on you update the plug from the builder. The tool does not update an
adopted plug, because the builder holds its API key
([Changing firmware later](what-happens.md#changing-firmware-later)).

## Config versions are pinned

The `@v2.3.0` is the release of the config your plug uses: the one its firmware
came from. A published release cannot change, so your plug gets the same config
every time you build it, whatever changes in this project later. "Update all" in
the builder rebuilds that same config with the newer ESPHome it is offering.

## Taking a fix

Read what changed in [CHANGELOG.md](../CHANGELOG.md), change the `@v2.3.0` to
the newer release, and install.

A config can run code on the computer that builds it, so choosing a newer release
means trusting it with that computer as well as your plug.

## Going back to this project's firmware

To stop using the builder for a plug, run the tool with `--replace-own-build`. It
installs this project's ESPHome over your build, after telling you what you lose
and asking you to confirm.

You lose any changes in your config, and the plug's API key. If the plug has a
key, the tool asks for it, or you can give it with `--esphome-api-key`. Afterwards
remove the plug from Home Assistant and add it again, and delete its config from
the builder. The tool can then update the plug again.

If you renamed the relay, it goes back to how it was before you renamed it, because
ESPHome saves a relay's state under its name. That may switch whatever is plugged
in, so unplug anything that should not change.

## Using your own config instead

Download your plug's `esphome.yaml` from the release you are on (it is in
`devices/<plug>/`). Save it in a folder beside your plug's config, such as
`common/`, and point the `packages:` line at your copy:

```yaml
packages:
  adamsthws.sonoff-s60: !include common/sonoff-s60.yaml
```

The builder then downloads nothing from this project, and the copy is yours to
change.

## Customising the config

The config the builder wrote for your plug is yours to edit. Anything you add
to it replaces the same setting in this project's config, and everything else
stays as it was. This example is for an S60, with three changes added at the
end:

```yaml
substitutions:
  name: esphome-abcdef
  friendly_name: esphome abcdef
packages:
  adamsthws.sonoff-s60: github://adamsthws/firmware-liberator/devices/sonoff-s60/esphome.yaml@v2.3.0
esphome:
  name: ${name}
  name_add_mac_suffix: false
  friendly_name: ${friendly_name}
api:
  encryption:
    key: "your plug's own key"
wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password
  # Added: a fixed address
  manual_ip:
    static_ip: 192.168.1.60
    gateway: 192.168.1.1
    subnet: 255.255.255.0
# Added: fewer log lines
logger:
  level: WARN
# Added: the relay's name, and off after a power cut
switch:
  - id: !extend relay
    name: "Lamp"
    restore_mode: ALWAYS_OFF
```

- **In a section**, such as `wifi:` or `logger:`, write only the settings you
  want to change.
- **In a list**, such as `switch:` or `sensor:`, use `!extend` with the `id` of
  the item you want to change. You will find the `id` in your plug's
  `esphome.yaml`. Without `!extend`, ESPHome treats it as a new item and shows
  an error.
- **`substitutions:`** only sets your plug's name in this example. The project's
  config does not use substitutions, so you cannot change its settings with them.

Your changes stay when you move to a newer release. If one of them refers to
something that release no longer has, the builder shows an error.

A plug adopted before 2.3.0 has a full copy of the config, which stays as it is
until you change it. To switch to the short config above, keep the name and key
from your copy, use your plug's folder under [devices/](../devices/) in the
`packages:` line, and add only the settings you changed in your copy.
