# Security

## Reporting a problem

Please report a security problem privately, through GitHub:
**[Report a vulnerability](https://github.com/adamsthws/Sonoff-OTA/security/advisories/new)**
- not as an issue or in the discussions, where it would be public before it
can be fixed. Say what you found, how to see it, and what it lets someone do.

A report is answered as soon as it can be, saying what is done about it and
when.

## What is in scope

- **The conversion tool** - everything in this repository that runs on your
  computer or is sent to the plug.
- **The firmware files it installs** from `artifacts/`, as built here.
- **The repository's workflows**, which update and release it.

Some of what crosses your local network during a conversion is sent in
plain HTTP, and is known to be:
[docs/technical-details.md](docs/technical-details.md#what-crosses-the-network)
says what, when and why. A problem in Tasmota or ESPHome themselves belongs with
[Tasmota](https://github.com/arendst/Tasmota/security) or
[ESPHome](https://github.com/esphome/esphome/security).

## Versions

Fixes go into the next release only, not into earlier ones.
