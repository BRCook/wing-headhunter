# wing-headhunter

Remote preamp control for Behringer Wing consoles connected over Dante.

> **Status:** design only. Nothing in this document has been implemented yet, and several of the console behaviours it depends on have not been verified; see [Open Questions](#open-questions).

## Why This Exists

Mixing monitors from front-of-house is commonplace these days, especially among us "weekend warriors". The Wing supports remote preamp control over AES50, but not over Dante. If your monitor console feeds your FOH console over Dante, there is no way to adjust stage preamps from FOH; you either walk to the stage, or you have a monitor engineer who does it for you.

I have neither the patience for the former nor the budget for the latter.

Headhunter fills this gap. It sits on the network between the two consoles, figures out where the sources are for each Dante patch, and lets you adjust the gain of the original preamp – local, AES50, or otherwise – from the FOH console using the trim knob.

## Topology

Throughout this document we will refer to two consoles:

- **Console A** is the FOH console. This is where you mix, and where all control happens.
- **Console B** is the stage console. It has direct access to the preamps, and sends its inputs to Console A over Dante.

The setup this was built for looks like this:

```
Stage box ──AES50── Wing Rack (B) ──Dante── Wing Compact (A)
```

Console B already controls the stage box preamps over AES50, so Headhunter only needs to talk to Console B, and Console B passes the changes down the chain on its own. Any preamp Console B can control, Headhunter can control.

Note that this isn't built for situations where there is a "Console C". It would certainly be a bad idea to give preamp control to multiple consoles; however, we may eventually add the ability to
*control* multiple consoles from Console A. Personally, I have no use case for this right now, but if there is demand, it can be done.

## Talking to the Consoles

Headhunter talks to both consoles using Behringer's native protocol – the same one Wing Edit and Mixing Station use – rather than OSC. The Wing only allows one client at a time to subscribe to OSC updates, so an OSC-based Headhunter would fight with anything else listening over OSC, such as Bitfocus Companion. The native protocol has no such limit.

Dante subscriptions are read (and, when renaming a device, changed) using `netaudio`.

## How It Works

Headhunter does three jobs.

### 1. Source Naming

On Console B, inputs (local, AES50, StageConnect, whatever) are routed to the Dante module outputs. On Console A, the Dante module inputs are mapped to channels. Headhunter reads the Dante subscriptions from the network using `netaudio`, follows each of Console A's Dante inputs back to the Console B output it is subscribed to, and then back to whatever Console B has routed to that output.

It then renames the source on Console A to match. For example, if Console B (named `RACK` in Headhunter) routes local input 24 to Dante output 1, and Console A's Dante input 1 is subscribed to that output, then `MOD 1` on Console A is renamed to:

```
RACK-L24-(D)
```

Names are monitored continuously and rebuilt whenever routing changes on either console, or whenever a Dante subscription changes. The `(D)` tag on the source name indicates it is a Dante patch controlled by Headhunter.

#### Name Format

Source names on the Wing are limited to 16 characters. Every name follows the format

```
<DEVICE>-<TYPE><NN>-(D)
```

where `<DEVICE>` is the name of the remote console in Headhunter, `<TYPE>` is one or two letters from the table below, and `<NN>` is the two-digit channel number. The longest possible suffix (a two-letter type) is 9 characters, so device names are limited to **7 characters**. This is enforced when the configuration is loaded; Headhunter will truncate names if they are longer than this, so be careful!

#### Abbreviations

Every abbreviation is unique to ensure there is no confusion about what's on the other end.

| Type            | Abbreviation | Example         | Remote gain? |
|-----------------|--------------|-----------------|--------------|
| Local input     | `L`          | `RACK-L24-(D)`  | Yes          |
| AES50 port A    | `A`          | `RACK-A03-(D)`  | Yes          |
| AES50 port B    | `B`          | `RACK-B12-(D)`  | Yes          |
| AES50 port C    | `C`          | `RACK-C01-(D)`  | Yes          |
| Aux input       | `X`          | `RACK-X04-(D)`  | No           |
| StageConnect    | `S`          | `RACK-S08-(D)`  | TBD          |
| USB             | `U`          | `RACK-U02-(D)`  | No           |
| Expansion card  | `K`          | `RACK-K17-(D)`  | No           |
| Internal module | `M`          | `RACK-M05-(D)`  | No           |
| AES/EBU         | `E`          | `RACK-E01-(D)`  | No           |
| Channel         | `CH`         | `RACK-CH12-(D)` | No           |
| Aux channel     | `AX`         | `RACK-AX03-(D)` | No           |
| Bus             | `BS`         | `RACK-BS16-(D)` | No           |
| Main            | `MN`         | `RACK-MN01-(D)` | No           |
| Matrix          | `MX`         | `RACK-MX04-(D)` | No           |
| Anything else   | `??`         | `RACK-??01-(D)` | No           |

This table is a draft; the full list of Wing source and output types still needs to be checked against the current firmware. Behringer is releasing some StageConnect devices soon, and it is not clear if they will have controllable gain. We shall see.

If a Dante input on Console A is subscribed to a device Headhunter does not know about, its name is left alone and it is excluded from gain control.

#### Renaming a Device

Headhunter has three names for each console:

1. The console's "real" name, which you set in the UI under Setup>General.
2. The console's Dante name, which is how it is identified by `netaudio`.
3. The console's Headhunter name, which is how Headhunter identifies it when naming sources.

Headhunter can change a remote console's Headhunter name *and* its Dante name. Note that Dante subscriptions are stored based on the Dante name; changing the Dante name would break any subscription pointing at it. Headhunter will automatically update any console's subscriptions to the new Dante name.

Headhunter cannot change a console's real name.

### 2. Gain Mode

A custom control button on Console A toggles gain mode. While gain mode is off, Console A functions normally; trim is trim. With gain mode turned on, the trim knob adjusts the gain of the remote preamp feeding the selected channel.

When gain mode is enabled, Headhunter takes a snapshot of every channel's trim on Console A. Nothing changes on the console; your trim settings stay exactly where they are, and all adjustments are made relative to them. Then, when you turn the trim knob on the selected channel:

1. Headhunter follows the channel's active source (main or alt, whichever is in use) back to the remote preamp, as described above.
2. It reads the preamp's current gain.
3. Trim movement is accumulated 1:1 with gain; every time the accumulated trim reaches one full gain step for that preamp, that step is applied to the preamp, and the remainder is carried.
4. Once the trim knob has been still for three seconds (the exact timing can be changed in Headhunter's settings), Headhunter restores the channel's trim to its snapshot value. Any remainder smaller than one gain step is discarded.

For example, suppose a channel's trim is at +10 dB when you enable gain mode. You turn the knob up to +12.5 dB; the console shows +12.5 dB, and Headhunter adds 2.5 dB of gain on Console B. Three seconds after you stop, trim returns to +10 dB, and you are left with exactly 2.5 dB more than you started with -- at the preamp!

The Wing adjusts trim in 0.1 dB increments, while preamps typically move in much larger steps (typically 2.5 dB, to be verified per source type). Since the ratio is 1:1, turning the trim knob by 2.5 dB adds exactly 2.5 dB of gain; turning it by 1.0 dB does nothing.

A few rules keep this safe:

- Only the selected channel counts. Trim changes on any other channel – snapshot and scene recalls, Mixing Station, anything else – are ignored. However, Headhunter does *not* interfere with any trim changes made outside its scope, so if you make a change in, say, Mixing Station, Headhunter will not reset the trim for you. This means you should never be adjusting gain on a channel with Headhunter while also making changes to that channel from another connection!
- Headhunter ignores its own changes. Every trim restore generates a change notification from the console; Headhunter must recognise its own writes and never act on them.
- Channels without remote gain are ignored. If the selected channel's source has no preamp (see the table above), or is not a Dante input from a known device, adjusting trim does exactly what it normally does.

Disabling gain mode returns the trim knob to normal behaviour. If an adjustment is still in progress, it is **cancelled** and trim is restored immediately. Gain steps already sent to Console B stay applied; only the remainder is discarded.

> **Don't use your ears for this, use your eyes.** While you are adjusting gain, the trim you dialled in hasn't been restored yet, so you hear the trim change and the gain change stacked on top of each other – roughly twice what you asked for. Once the knob has been still for three seconds (or however long you set), trim snaps back and the volume drops to its real level. Watch the gain on the scribble strip, and wait for trim to be restored before judging the volume by ear. Since each step is effectively doubled before a reset, the input meter on Console A may read high during an adjustment, so it is not a reliable warning.

### 3. Phantom Power

A second custom control button on Console A controls +48 V on the remote preamp. When you select a channel, Headhunter sets the button to match the remote preamp's phantom power: lit if +48 V is on, unlit if it is off. Pressing and holding the button (for two seconds, matching the console's own behaviour) toggles phantom power on the remote preamp.

The button only works while gain mode is on, so it cannot be triggered by accident during normal mixing. As with trim, Headhunter ignores its own changes to the button; setting the button on channel select must never be mistaken for a press.

### Gain Display

Since trim is always restored, Console A never shows the actual preamp gain. To fix this, Headhunter temporarily writes the current gain into the selected channel's name on the scribble strip whenever it changes, and restores the original name after a few seconds.

If Headhunter crashes or a snapshot is saved while a temporary name is showing, the temporary name would stick. Since Headhunter never restores anything from a previous session (see [Startup](#startup)), there is no way to prevent this; you will need to fix the name by hand.

### Startup

Headhunter always starts with a blank slate. It stores nothing between runs, and never restores anything from a previous session; what if it was still holding on to something from a show two weeks ago?

When it starts, Headhunter assumes gain mode is off, regardless of what Console A shows. Gain mode only turns on when Headhunter sees the gain mode button go from off to on. If the button was left on – say, because Headhunter crashed mid-show – you will need to press it twice: once to turn it off, and once more to turn it back on. This may be inconvenient, but Headhunter should never *intrude* on a console.

Anything in progress when Headhunter stopped is gone. Gain steps that were already sent to Console B stay applied; trim movement that had not yet added up to a full step is lost, and will never be applied later.

**The only exception is naming!** Headhunter, by its very nature, will reach into Console A and change the Dante source names to match the inputs it finds downstream. That's the whole point. Don't be upset if your source names are overwritten by software you installed for the explicit purpose of changing source names.

## Failure Behaviour

Headhunter should never leave either console in a strange state. In particular:

- If Console B or the Dante network becomes unreachable, gain mode stops acting immediately; trims are restored, and channel names are restored.
- If Headhunter itself crashes, the trim knob already behaves normally, since gain mode only exists inside Headhunter. At most one adjustment's worth of trim may be left unrestored, and any temporary channel name will stick. When Headhunter is started again, it follows the rules in [Startup](#startup).
- Headhunter never applies a gain change it cannot confirm. If a write to Console B fails, the change is dropped, not retried later.

## Requirements

- Two Behringer Wing consoles, each with a Dante module, on the same network.
- A computer on that network to run Headhunter (we will be planning for Alpine and Debian/Ubuntu).
- Python 3.13, with `netaudio`.

## Open Questions

None of the following have been verified, and the design may change depending on the answers:

1. Can custom control buttons on Console A be read **and** set over the native protocol? If not, the fallback is to assign the buttons to an unused mute group, which the bridge can read and set. That mute group must have no members, or it will mute real channels.
2. What are the gain step sizes and ranges for each preamp type (Wing local, SD/DL8/16/32, StageConnect devices)?
3. Does StageConnect carry remote preamp control at all?
4. What are the exact native protocol parameters for trim, gain, phantom power, source names, channel names, and main/alt source selection on the current firmware?
5. How does Console A report the selected channel, and how quickly?
6. How long is "three seconds" before trim is restored? It could be excruciatingly long. This needs to be tuned by ear (ironically).
7. Can this be used with the X32 and M32 series of consoles as well? They do not speak the Wing's native protocol, but they allow up to 16 OSC subscribers, so OSC may be good enough there.
8. Is a web UI feasible, and worth the effort?
9. How complete is Behringer's documentation of the native protocol, and is there an existing Python implementation worth using rather than writing our own?
10. Is the input meter on Console A pre- or post-trim? This determines how misleading it is during an adjustment.