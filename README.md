# wing-headhunter

Remote preamp control for Behringer Wing consoles connected over Dante.

> **Status:** early development. Source naming, gain mode, phantom power and the gain display all work, and have been tested on my Rack and Compact (see [Tested Hardware](#tested-hardware)). There is no web interface yet, so everything must be done from the command line.

> [!NOTE]
> This project is AI-assisted. 

## Why This Exists

Mixing monitors from front-of-house is commonplace these days, especially among us "weekend warriors". The Wing supports remote preamp control over AES50, but not over Dante. If your monitor console feeds your FOH console over Dante, there is no way to adjust stage preamps from FOH; you either walk to the stage, or you have a monitor engineer who does it for you.

I have neither the patience for the former nor the budget for the latter.

Headhunter fills this gap. It sits on the network between the two consoles, figures out where the sources are for each Dante patch, and lets you adjust the gain of the original preamp – local, AES50, or otherwise – from the FOH console using the trim knob.

> [!WARNING]
> Headhunter is an independent project, and is not affiliated with, endorsed by or supported by Audinate (who make Dante) or Behringer. Headhunter *changes settings on live mixing consoles*; use it at your own risk. I am not responsible if you break anything – including your show.

## Topology

Throughout this document we will refer to two consoles:

- **Console A** is the FOH console. This is where you mix, and where all control happens.
- **Console B** is the stage console. It has direct access to the preamps, and sends its inputs to Console A over Dante.

The setup this was built for looks like this:

```
Stage box ──AES50── Wing Rack (B) ──Dante── Wing Compact (A)
```

Every paired console is marked as either FOH or stage (see [Discovery and Pairing](#discovery-and-pairing)).

Console B already controls the stage box preamps over AES50, so Headhunter only needs to talk to Console B, and Console B passes the changes down the chain on its own. Any preamp Console B can control, Headhunter can control.

Note that this isn't built for situations where there is a "Console C". It would certainly be a bad idea to give preamp control to multiple consoles; however, we may eventually add the ability to *control* multiple consoles from Console A. Personally, I have no use case for this right now, but if there is demand, it can be done.

### Tested Hardware

Everything in this document has been tested with a Wing Compact as Console A and a Wing Rack as Console B, using their internal Dante modules, an SD8 stage box on AES50, and firmware 3.1.1 and 3.1.2.

Using a full-size Wing or a Wing Rack as Console A should work, but I don't own a full-size Wing, and I don't plan on using the Rack as the primary console, so... good luck! The only part that differs between models is the [strip light flashes](#strip-lights), which need to know how many strips each model shows at once: 12 on the Compact, 4 on the Rack, and 12, 8 and 4 across the full-size Wing's three fader banks, each of which can be on its own layer.

## Talking to the Consoles

Headhunter talks to both consoles using Behringer's native protocol – the same one Wing Edit and Mixing Station use – rather than OSC. The Wing only allows one client at a time to subscribe to OSC updates, so an OSC-based Headhunter would fight with anything else listening over OSC, such as Bitfocus Companion. The native protocol has no such limit: every connection receives every change the console makes, as it happens, without subscribing to anything.

Every parameter on a Wing has a path (such as `io.in.LCL.24.g` for the gain of local input 24) and the native protocol addresses parameters by a hash of that path. Headhunter calculates these hashes itself, so it never has to explore the console's parameter tree. The parameters it relies on are listed in [Console Parameters](#console-parameters).

Dante subscriptions are read using `netaudio`.

## Discovery and Pairing

You should never need to know a console's IP address. Headhunter finds everything on its own:

- **Consoles** are found by broadcasting the Wing's discovery message (`WING?`, UDP port 2222). Every Wing on the network replies with its IP, name, model, serial number and firmware version.
- **Dante cards** are found over mDNS. Wing Dante cards identify themselves with the model `wing64 B`, so Headhunter can tell them apart from any other Dante device on the network.

Unfortunately, a Wing knows nothing about its own Dante card, and nothing on the network links the two; they have different MAC addresses, different names, and nothing else in common. Headhunter therefore asks you to pair each console with its Dante card once, by picking both from the lists of what it has discovered. If the names line up, Headhunter will suggest the obvious pairing, but you must always confirm it.

Pairings are permanent, and are kept in a small SQLite database at `~/.local/share/headhunter/headhunter.db`. Each paired console has one entry:

| Field            | Example             | Source                     |
|------------------|---------------------|----------------------------|
| `console_mac`    | `XX:XX:XX:XX:XX:XX` | ARP table, after discovery |
| `console_name`   | `My-WingRCK`        | Discovery reply            |
| `console_model`  | `wing-rack`         | Discovery reply            |
| `console_serial` | `MYSERIALNUMBER`    | Discovery reply            |
| `dante_mac`      | `YY:YY:YY:YY:YY:YY` | mDNS                       |
| `dante_name`     | `WingRack`          | mDNS                       |
| `display_name`   | `RACK`              | You (7 characters max)     |
| `role`           | `STAGE`             | You (`FOH` or `STAGE`)     |
| `gain_gpio`      | `3`                 | You (FOH consoles only)    |
| `phantom_gpio`   | `4`                 | You (FOH consoles only)    |

A few rules apply:

- MAC addresses are the identity. Both MACs are permanent, while names and IPs can change at any time.
- Names are observed, not trusted. Every time Headhunter discovers a console, it refreshes the console and Dante names from what the network reports; if you rename a console on its own screen, Headhunter simply updates its records. The only exception is the display name, which only changes when you change it.
- IP addresses are never stored. They are rediscovered every time, so DHCP is no problem.
- Display names must be unique, since two consoles both called `RACK` would make source names ambiguous.
- Roles can be changed at any time. Headhunter only ever renames sources on a console marked FOH.

### Network Requirements

All of this relies on Headhunter, the consoles and the Dante cards sharing a single network. Broadcast discovery and mDNS do not cross subnets or VLANs, and Headhunter reads console MAC addresses from its ARP table, which only works on the local subnet.

With the Wing's internal Dante module, Dante must be set to **switched** mode in Dante Controller, so that console control and Dante share the same ethernet ports and VLAN. In redundant mode, Headhunter will not find your Dante cards.

With a Dante *expansion card*, Dante has its own ports, separate from the console's; the machine running Headhunter must then be able to reach both networks.

Headhunter cannot see either setting from the console. If it cannot find a paired console's Dante card, that console's Dante inputs simply are not named or controlled; `headhunter discover` is the quickest way to check what Headhunter can see.

If the machine running Headhunter has a firewall, it must allow UDP traffic *from* port 2222 on the console network; Wing consoles reply to discovery from that port. With `ufw`, for example:

```sh
sudo ufw allow proto udp from <your subnet> port 2222 comment 'Wing discovery replies'
```

mDNS (UDP port 5353) must also be allowed, which it usually already is if Dante Controller works on the same machine, but it's worth checking.

## Getting Started

Headhunter needs Python 3.13 or newer. Install it into a virtual environment:

```sh
git clone https://github.com/BRCook/wing-headhunter.git
cd wing-headhunter
python -m venv .venv
source .venv/bin/activate    # .venv/bin/activate.fish in fish
pip install -e .
```

Then set it up, once:

```sh
headhunter discover                               # Check that every console and Dante card can be seen
headhunter pair                                   # Pair each console with its Dante card
headhunter role COMPACT FOH                       # Mark your FOH console
headhunter buttons COMPACT --gain C --phantom D   # Choose its gain mode and phantom power GPIOs
```

`headhunter dante` shows what each paired console receives over Dante, and `headhunter names` shows the source names Headhunter would write, without writing them.

Finally, run it:

```sh
headhunter run --write-names --write-gain
```

Headhunter is careful by default. You must set `headhunter settings write_names on` and `headhunter settings write_gain on` before Headhunter will manipulate anything on your console. These changes are permanent; to only allow changes for a single run, use `headhunter run --write-names --write-gain`.

A few more options:

- `--restore-after SECONDS` changes how long trim must be still before it is restored (see [Gain Mode](#2-gain-mode)).
- `--stats MINUTES` logs Headhunter's memory and resource use every few minutes.

## How It Works

Headhunter does three jobs.

### 1. Source Naming

On Console B, inputs (local, AES50, StageConnect, whatever) are routed to the Dante module outputs. On Console A, the Dante module inputs are mapped to channels. Headhunter reads the Dante subscriptions from the network using `netaudio`, follows each of Console A's Dante inputs back to the Console B output it is subscribed to, and then back to whatever Console B has routed to that output.

It then renames the source on Console A to match. For example, if Console B (named `RACK` in Headhunter) routes local input 24 to Dante output 1, and Console A's Dante input 1 is subscribed to that output, then `MOD 1` on Console A is renamed to:

```
RACK-L24-HH
```

Names are monitored continuously and rebuilt whenever routing changes on either console, or whenever a Dante subscription changes. The consoles report routing changes the moment they happen; Dante subscriptions are checked every few seconds. The `HH` tag on the source name indicates it is managed by Headhunter.

> [!IMPORTANT]
> On Console A, turn off **Link customization to source** on every channel fed by Headhunter. Headhunter will not rename a source while any channel using it has the link turned on.

#### Name Format

Source names on the Wing are limited to 16 characters. Every name follows the format

```
<DEVICE>-<TYPE><NN><SIDE>-HH
```

where `<DEVICE>` is the name of the remote console in Headhunter, `<TYPE>` is one or two letters from the table below, and `<NN>` is the two-digit channel number.

Buses, mains, matrices, FX sends and monitors are stereo, and a Wing routes each side separately; input 1 of the bus group is bus 1 left, input 2 is bus 1 right, and so on. For these, `<NN>` is the actual bus (or main, or matrix…) number, and `<SIDE>` is `L` or `R`; so bus 1 right is `RACK-BS01R-HH`, not `RACK-BS02-HH`. Everything else is mono, and has no `<SIDE>`.

The longest possible suffix (a stereo source, such as `-BS16R-HH`) is 9 characters, so device names are limited to **7 characters**. Headhunter will truncate longer names when you choose them, so be careful!

#### Abbreviations

Every abbreviation is unique to ensure there is no confusion about what's on the other end.

| Source group on Console B | Wing name | Abbreviation | Example          | Remote gain? |
|---------------------------|-----------|--------------|------------------|--------------|
| Local input               | `LCL`     | `L`          | `RACK-L24-HH`    | Yes          |
| AES50 port A              | `A`       | `A`          | `RACK-A03-HH`    | Yes          |
| AES50 port B              | `B`       | `B`          | `RACK-B12-HH`    | Yes          |
| AES50 port C              | `C`       | `C`          | `RACK-C01-HH`    | Yes          |
| Aux input                 | `AUX`     | `X`          | `RACK-X04-HH`    | No           |
| StageConnect              | `SC`      | `S`          | `RACK-S08-HH`    | TBD          |
| USB                       | `USB`     | `U`          | `RACK-U02-HH`    | No           |
| Expansion card            | `CRD`     | `K`          | `RACK-K17-HH`    | No           |
| Internal module           | `MOD`     | `M`          | `RACK-M05-HH`    | No           |
| USB player                | `PLAY`    | `P`          | `RACK-P01-HH`    | No           |
| AES/EBU                   | `AES`     | `E`          | `RACK-E01-HH`    | No           |
| User signal or patch      | `USR`     | `US`         | `RACK-US12-HH`   | No           |
| Oscillator                | `OSC`     | `OS`         | `RACK-OS01-HH`   | No           |
| Bus (stereo)              | `BUS`     | `BS`         | `RACK-BS16L-HH`  | No           |
| Main (stereo)             | `MAIN`    | `MN`         | `RACK-MN01R-HH`  | No           |
| Matrix (stereo)           | `MTX`     | `MX`         | `RACK-MX04L-HH`  | No           |
| FX send (stereo)          | `SEND`    | `SN`         | `RACK-SN03R-HH`  | No           |
| Monitor (stereo)          | `MON`     | `MO`         | `RACK-MO01L-HH`  | No           |
| Not patched               | `OFF`     | –            | `RACK-OFF-HH`    | No           |
| Anything else             | –         | `??`         | `RACK-??01-HH`   | No           |

These are the source groups a Wing can route to a Dante output, as reported by the console itself. Note that a Wing cannot route a channel directly to an output; that is done through user signals, which is why channels do not appear in this table.

Behringer is releasing some StageConnect devices soon, and it is not clear if they will have controllable gain. We shall see.

If a Dante input on Console A is subscribed to a device Headhunter does not know about, its name is left alone and it is excluded from gain control.

#### Renaming a Device

Headhunter has three names for each console:

1. The console's "real" name, which you set in the UI under Setup>General.
2. The console's Dante name, which is how it is identified by `netaudio`.
3. The console's Headhunter name, which is how Headhunter identifies it when naming sources.

Headhunter will eventually be able to change a remote console's Headhunter name *and* its Dante name. This is not built yet. Note that Dante subscriptions are stored based on the Dante name, so changing the Dante name would break any subscription pointing at it; when this is built, Headhunter will update every console's subscriptions to the new Dante name as part of the rename.

Headhunter does not change a console's real name. It could, but the real name is how the console identifies itself to everything else on the network, so we leave it to you to change on the console itself.

### 2. Gain Mode

A custom control button on Console A toggles gain mode (see [Buttons](#buttons)). While gain mode is off, Console A functions normally; trim is trim. With gain mode turned on, the trim knob adjusts the gain of the remote preamp feeding the selected channel.

Every time gain mode turns on or off, the "colour LEDs" on Console A's selected fader layer flash white (see [Strip Lights](#strip-lights)) to alert you that gain mode has changed. Whenever you see this flash, **check the GPIO button** to ensure you haven't accidentally enabled or disabled gain mode!

When gain mode is enabled, Headhunter takes a snapshot of every channel's trim on Console A. Nothing changes on the console; your trim settings stay exactly where they are, and all adjustments are made relative to them. Then, when you turn the trim knob on the selected channel (any input channel or aux channel; the console reports which one is selected the moment you press its Select button):

1. Headhunter follows the channel's active source (main or alt, whichever is in use) back to the remote preamp, as described above.
2. It reads the preamp's current gain.
3. Trim movement is accumulated 1:1 with gain; every time the accumulated trim reaches one full gain step for that preamp, that step is applied to the preamp, and the remainder is carried.
4. Once the trim knob has been still for two seconds (change this with `--restore-after`), Headhunter restores the channel's trim to its snapshot value. Any remainder smaller than one gain step is discarded. For example, suppose a channel's trim is at +10 dB when you enable gain mode. You turn the knob up to +12.5 dB; the console shows +12.5 dB, and Headhunter adds 2.5 dB of gain on Console B. Two seconds after you stop, trim returns to +10 dB, and you are left with exactly 2.5 dB more than you started with -- at the preamp!

Trim ranges from −18 to +18 dB in 0.1 dB increments, while preamps move in larger steps. A preamp's range and step size depend on the hardware, so Headhunter asks the console for them rather than assuming anything. For example, on current firmware:

| Preamp                    | Range          | Step   |
|---------------------------|----------------|--------|
| Wing local inputs         | −2.5 to +45 dB | 2.5 dB |
| SD/DLX stage box on AES50 | −2 to +45.5 dB | 2.5 dB |

Steps count from the bottom of the range, not from zero; an SD8 can be at −2, +0.5 or +3 dB, but not 0 dB. The console rounds anything else to the nearest valid value.

Since the ratio is 1:1, turning the trim knob by 2.5 dB adds exactly 2.5 dB of gain on either of the preamps above; turning it by 1.0 dB does nothing.

Headhunter only adjusts a stage box preamp when the console reports that the stage box is actually connected. When a stage box connects or disconnects, the console changes that port's range and step size, so Headhunter reads them again.

Trim cannot go past ±18 dB, so if a channel's trim is already at +18 dB, the knob cannot turn any further and you cannot add gain. If it is close to the limit, you can still add gain, just a little at a time; trim is restored after every adjustment, so you can simply repeat it. Similarly for −18 dB and reducing gain.

A few rules keep this safe:

- Only the selected channel counts. Trim changes on any other channel – snapshot and scene recalls, Mixing Station, anything else – are treated as real trim changes: Headhunter does not act on them, and remembers them as that channel's new trim, so a later restore never undoes them. This means you should never be adjusting gain on a channel with Headhunter while also making changes to that channel from another connection!
- Channels without remote gain are ignored. If the selected channel's source has no preamp (see the table above), or is not a Dante input from a known device, adjusting trim does exactly what it normally does.

An adjustment is **cancelled**, and trim restored immediately, if you select another channel, turn gain mode off, or Console B drops off the network. Gain steps already sent to Console B stay applied; only the remainder is discarded.

> **Don't use your ears for this, use your eyes.** While you are adjusting gain, the trim you dialled in hasn't been restored yet, so you hear the trim change and the gain change stacked on top of each other – roughly twice what you asked for. Once the knob has been still for two seconds (or however long you set), trim snaps back and the volume drops to its real level. Watch the gain on the scribble strip, and wait for trim to be restored before judging the volume by ear. Since each step is effectively doubled before a reset, the input meter on Console A may read high during an adjustment, so it is not a reliable warning.

### 3. Phantom Power

A second custom control button on Console A controls +48 V on the remote preamp. This button must be momentary (see [Buttons](#buttons)), and only works while gain mode is on, so it cannot be triggered by accident during normal mixing.

To toggle phantom power on the selected channel's remote preamp, press and hold the button for two seconds, matching the console's own behaviour. Releasing it sooner, or selecting another channel while holding it, does nothing. When Headhunter has changed phantom power, it confirms by flashing the button:

- **Five quick flashes:** phantom power is now **on**.
- **Three slow flashes:** phantom power is now **off**.

At the same time, the channel strip's "colour LED" flashes red (see [Strip Lights](#strip-lights)), whichever way phantom power went. No flash means nothing changed. The current phantom power state is also shown on the scribble strip, alongside the gain.

The button can be released mid-flash; you do not need to hold it down after the initial two seconds.

### Buttons

Headhunter's two buttons are custom control buttons assigned to the console's GPIOs. Headhunter reads each GPIO's state over the network, so any custom control button assigned to a GPIO will do. Tell Headhunter which GPIOs you chose with `headhunter buttons` (see [Getting Started](#getting-started)).

- **Gain mode** uses a toggle button.
- **Phantom power** uses a momentary button, so that Headhunter can tell how long it is held.

> **Be careful which GPIOs you use.** A GPIO's state is not just a light on the console; it drives the GPIO jack on the back. Anything wired to that GPIO will switch every time you toggle gain mode, and every time Headhunter flashes the phantom power button. Use GPIOs that have nothing connected to them. On the Compact, only two of the four GPIOs have physical connections, so the other two are ideal.

### Strip Lights

Headhunter flashes the channel strip "colour LED" on Console A to confirm what it has done:

- **White,** on every channel on the selected layer, whenever gain mode turns on or off.
- **Red,** on the selected channel strip only, whenever phantom power turns on or off.

Channels, aux channels, buses, mains, matrices, DCAs and sends on fader all flash, including on user layers. Channels with nothing input assigned have no light to flash and will remain dark.

### Gain Display

Since trim is always restored, Console A never shows the actual preamp gain. To fix this, whenever Headhunter changes a preamp's gain or phantom power, it temporarily writes them into the channel's name, and hides the channel's icon so the text has the whole scribble strip:

```
27.5dB
27.5dB ! 48V !
```

The exclamation marks make phantom power hard to miss. Three seconds after the last change, the channel's real name and icon come back. If you rename the channel or change its icon while the gain is showing, your change is kept.

Channels with **Link customization to source** turned on show their source's name rather than their own, so they get no gain display; gain mode still works on them.

The display is taken down immediately if you turn gain mode off, if Console B drops off the network, or when Headhunter is stopped. If Headhunter crashes, or a snapshot is saved while the gain is showing, the gain display would stick. Since Headhunter never restores anything from a previous session (see [Startup](#startup)), there is no way to prevent this; you will need to fix the name by hand.

### Startup

Headhunter always starts with a blank slate. It remembers its configuration – which consoles are paired with which Dante cards, what to call them, and your settings – because you set those deliberately (see [Discovery and Pairing](#discovery-and-pairing)). Everything else is session state: trim snapshots, gain mode, gain displays, and adjustments in progress. Session state is never stored, and never restored from a previous session; what if it was still holding on to something from a show two weeks ago?

When it starts, Headhunter assumes gain mode is off, regardless of what Console A shows. Gain mode only turns on when Headhunter sees the gain mode button go from off to on. If the button was left on – say, because Headhunter crashed mid-show – Headhunter logs that it is ignoring it, and you will need to press it twice: once to turn it off, which does nothing at all, and once more to turn gain mode on. This may be inconvenient, but Headhunter should never *intrude* on a console.

Anything in progress when Headhunter stopped is gone. Gain steps that were already sent to Console B stay applied; trim movement that had not yet added up to a full step is lost, and will never be applied later.

**The only exception is naming!** Headhunter, by its very nature, will reach into Console A and change the Dante source names to match the inputs it finds downstream. That's the whole point. Don't be upset if your source names are overwritten by software you installed for the explicit purpose of changing source names.

## Console Parameters

For reference, these are the console parameters Headhunter depends on. `N` is a channel or input number; aux channels use `aux.N` in place of `ch.N`.

| Parameter                       | Console | Meaning                                               |
|---------------------------------|---------|-------------------------------------------------------|
| `$ctl.$stat.selidx`             | A       | The selected strip: 1–40 are channels, 41–48 aux channels, 49 and up buses, mains and matrices |
| `ch.N.in.set.trim`              | A       | Channel trim                                          |
| `ch.N.in.set.altsrc`            | A       | Whether the channel is using its alt source           |
| `ch.N.in.conn.grp`, `.in`       | A       | The channel's main source                             |
| `ch.N.in.conn.altgrp`, `.altin` | A       | The channel's alt source                              |
| `ch.N.name`, `.icon`            | A       | Channel name and icon, used for the gain display      |
| `ch.N.$name`                    | A       | The name the channel actually displays                |
| `ch.N.clink`                    | A       | Link customization to source                          |
| `ch.N.col`, `.led`              | A       | Strip colour and light; buses, mains, matrices and DCAs have the same, for the strip light flashes |
| `$ctl.layer.<section>.sel`      | A       | The layer on each fader section, e.g. `CMPCT` on the Compact |
| `$ctl.layer.<section>.<layer>.<position>.type`, `.i` | A | What is on each fader of a layer |
| `io.in.MOD.N.name`              | A       | Dante source names, set by Headhunter                 |
| `$ctl.gpio.N.gpstate`           | A       | Gain mode and phantom power buttons                   |
| `io.out.MOD.N.grp`, `.in`       | B       | What Console B sends to each Dante output             |
| `io.in.<group>.N.g`             | B       | Preamp gain                                           |
| `io.in.<group>.N.vph`           | B       | Phantom power                                         |
| `$stat.<port>.stat`, `.dev`     | B       | Whether an AES50 port is connected, and to what       |
| `$syscfg.opt_mod`               | Both    | Installed module: `NONE`, `DANTE`, `WSG` or `ACOM`    |
| `$syscfg.tcplock`               | Both    | When on, the console refuses changes over the network |

If `tcplock` is on, Headhunter cannot change anything.

## Failure Behaviour

Headhunter should never leave either console in a strange state. In particular:

- If Console B or the Dante network becomes unreachable, gain mode stops acting immediately; trims are restored, and channel names are restored. Headhunter keeps looking for the missing console, and picks up where it left off when it comes back, at whatever IP address it turns up at.
- If Headhunter itself crashes, the trim knob already behaves normally, since gain mode only exists inside Headhunter. At most one adjustment's worth of trim may be left unrestored, and any gain display or strip light flash in progress will stick. When Headhunter is started again, it follows the rules in [Startup](#startup).
- Headhunter never applies a change it cannot confirm. Every change to gain, phantom power or trim is read back from the console; if it did not take, the change is dropped, not retried later.
- If Console A drops off the network, Headhunter starts over when it comes back, just as if it had been restarted (see [Startup](#startup)). Gain mode turns off, and anything in progress is abandoned.

## Requirements

- Two Behringer Wing consoles, each with a Dante module, on the same network, with Dante in switched mode (see [Network Requirements](#network-requirements)).
- A computer on that network to run Headhunter (we will be planning for Alpine and Debian/Ubuntu).
- Python 3.13 or newer. `pip` installs everything else, including `netaudio` and `zeroconf`.