# Router rules for a stock plug (optional)

None of this is needed to convert a plug. The tool delivers its firmware
straight from your computer over your own network, and never relies on your
router. This page is here for two things a router *can* do for a plug that is
still running Sonoff's firmware:

- **stop it installing Sonoff updates**, so a future Sonoff firmware can never
  close the way this tool gets in before you have converted the plug;
- **send its update download to your computer instead**, which is what earlier
  versions of the tool required whenever Sonoff was offering the plug an update.

Both are written for someone comfortable in their router's admin pages. Neither
is a normal "port forward" (that sends traffic arriving from the internet to a
device at home; these deal with traffic leaving a device at home).

## How a Sonoff update reaches the plug

What is known, from the reverse engineering in
[ota-findings.md](../archive/docs/reference/ota-findings.md) and a watch of a
stock plug on 1.2.0 with `tools/watch_sonoff_updates.py` (October 2026):

- The plug keeps one encrypted connection open to eWeLink's servers and sends a
  tiny heartbeat on it every 145 seconds. In the hours watched, nothing else
  happened unless someone used the plug or had its page open in the app.
- When it connects (at power-on, or after losing Wi-Fi) it reports its firmware
  version. Whether to offer an update is decided by Sonoff's cloud, not the
  plug. A power cycle showed no download and no plain-HTTP traffic at all.
- The plug only downloads firmware when it is sent an **upgrade command**, which
  names the download address. Sonoff's own downloads have always been **plain
  HTTP on port 8088**.

What is **not** known yet: whether Sonoff ever sends that command by itself,
without anyone tapping Update in the app. The watched plug had no update on
offer, so it could not show this. Watching a plug that Sonoff *is* offering an
update to would.

## Blocking Sonoff updates

> **Not needed today.** Every Sonoff firmware tested so far, up to and including
> 1.2.0, can be converted, and the tool works whether or not Sonoff is offering
> the plug an update. This step is insurance. If Sonoff ever releases a firmware
> that closes the way in - for example by checking a signature on downloads, or
> by refusing an upgrade command that names another address - a plug that
> installs it could no longer be converted by this tool. A plug that never
> receives it stays convertible. If such a firmware appears, this step becomes
> necessary for new plugs.

The rule: **reject TCP from every device on the network the plug will join, to
port 8088 anywhere outside your home network.** It covers the whole network,
not one device, because a new plug's address and MAC address are not known
until it has already joined - and by then it may already have been offered an
update. So the rule goes in **before the plug is unboxed**: before it ever joins
your Wi-Fi or is added to the eWeLink app.

For a new plug, the order is:

1. Add the rule (below).
2. Unbox the plug, power it up and pair it in the eWeLink app as usual.
3. Convert it with the tool.
4. Once no plug on your network is still on Sonoff's firmware, remove the rule,
   or narrow it to a stock plug you are keeping (its address is known by then -
   reserve it in DHCP and use that address as the source).

Why this is expected to work: in the research, the plug's route to Sonoff's
download server was blocked at the router and a genuine upgrade command was
sent. The plug could not download the firmware and stayed on the version it
had. One side effect was seen: afterwards it showed as offline in eWeLink until
it was power-cycled.

It does not get in the way of converting the plug: our firmware comes from your
computer on your own network, and that traffic never passes through the
router's firewall. Nor does it affect a converted plug: Tasmota's own updates
do not use port 8088.

What it costs: while the rule is in place, no device on that network can
download from port 8088 on the internet. Very little uses that port, but check
nothing of yours does. If you have a separate network for smart-home devices,
put the rule there instead and nothing else is touched.

What it does **not** cover: it only stops downloads on port 8088, which is what
Sonoff's downloads have always used. If Sonoff ever moved them to another port,
or to HTTPS on 443, this rule would not see them. The only certain block is
cutting the plug off from the internet altogether, which also ends control
through the eWeLink cloud - and the pairing that the tool relies on.

### On OPNsense

1. **Firewall → Rules → LAN** (or the interface of the network the plug's Wi-Fi
   is on), then **Add**.
2. Set:

   | Field | Value |
   |---|---|
   | Action | Reject (the plug gives up straight away, instead of waiting) |
   | Interface | LAN |
   | Direction | in |
   | Protocol | TCP |
   | Source | LAN net - every device, since the plug's address isn't known yet |
   | Destination | LAN net, with **Invert** ticked - meaning "anywhere except my network" |
   | Destination port | 8088 |
   | Log | ticked, so the firewall log shows every blocked attempt |
   | Description | Block Sonoff firmware downloads |

3. Move it **above** the "allow LAN to any" rule. Rules are checked from the top
   and the first match wins.
4. **Apply**.

On other routers the same rule is usually under *Firewall → Traffic Rules*
(OpenWrt) or *Firewall → Rules → LAN* (pfSense): a reject or drop rule for
outbound TCP from the home network to port 8088 outside it.

To check it: the firewall log shows a rejected connection to some address on
port 8088 whenever a device tries to download from that port, and for a plug
the update watcher shows `plug opened a connection to the firmware port` at the
same moment. To remove it, delete the rule and apply.

## Redirecting the download to your computer

This was a step in earlier versions of the tool, used only when Sonoff was
offering the plug an update. It was never how our firmware was delivered - that
has always come straight from your computer - but a guard against the plug
fetching and installing Sonoff's genuine update in the same few seconds. It was
dropped because the owner's upgrade command already decides what is installed,
and because many routers cannot do it. To block updates, use the rule above
instead: it is simpler, and needs none of the following.

The rule is a **destination NAT** (DNAT) rule for traffic *from* the plug. Its
values came from the update information the tool reads at step A2:

| Field | Value |
|---|---|
| Protocol | TCP |
| Source address | the plug's address |
| Destination address | the Sonoff download address in the update information |
| Destination port | its port (8088 so far) |
| Redirect to address | your computer's address |
| Redirect to port | the port the tool serves the bridge on (8088, or 8090–8099 if 8088 was busy) |

Because the plug and your computer are on the same network, the reply must also
go back through the router, or the plug sees it arrive from the wrong address
and drops the connection. Turn on **NAT reflection** / **hairpin NAT** /
**masquerading**, or add an outbound NAT rule for traffic from the plug to your
computer on that port that translates to the router's own address.

Where it lives on common routers:

- **OPNsense / pfSense:** Firewall → NAT → Port Forward, on the **LAN** interface.
- **OpenWrt:** Network → Firewall → Port Forwards, with the source zone set to
  LAN.
- **UniFi:** the graphical "Destination NAT" policy only applies to traffic
  arriving from the internet. The research added the two rules with `iptables`
  over SSH instead; the exact commands are in
  [ota-findings.md](../archive/docs/reference/ota-findings.md).

Common mistakes: source and destination the wrong way round; a plain port
forward instead of DNAT; no NAT reflection; and the plug's address changing
after the rule was made (reserve it in DHCP).

### Testing it, and putting it back into the tool

The old step tested the rule before anything was written to the plug. It
borrowed the plug's address on your computer for a few seconds (so the plug had
to be unplugged), then checked that a connection from that address to Sonoff's
download server was redirected, and one from your computer's own address was
not. That test still exists and is still covered by the test suite:

- `tools/autoflash/redirect_test.py` - the test itself, with the
  address-borrowing helpers in `tools/autoflash/hostsys.py`;
- `tools/test_ota_firewall.sh` - the research's command-line version.

The step that asked for the rule and ran the test was step **B2**,
`step_intercept`, in `tools/s60_autoflash.py`; git history has it. Putting it
back means restoring that step and passing the Sonoff address from step A2 to
it.
