"""Gain mode: working out which remote preamp the trim knob controls.

For the selected channel on a FOH console, Headhunter follows its active
source (main or alt) to a Dante input, the Dante input to the paired console
it is subscribed to, that console's routing to whatever feeds the Dante
output, and finally to a preamp. Every way that chain can break has a reason,
so Headhunter can always say why trim is not controlling gain.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from headhunter.dante import Subscription
from headhunter.native import Value
from headhunter.store import GPIO_COUNT, Pairing

State = Mapping[str, Value]

# Source groups with preamps Headhunter can control
PREAMP_GROUPS = ("LCL", "A", "B", "C")
AES50_PORTS = ("A", "B", "C")
AES50_INPUTS = 48

# Local inputs by console model; the full-size Wing has 8, the Rack and Compact 24
LOCAL_INPUTS = {"wing-rack": 24, "wing-compact": 24}
LOCAL_INPUTS_DEFAULT = 8

# Short preamp names, matching the abbreviations in source names: RACK L1, RACK A8
PREAMP_ABBREVIATIONS = {"LCL": "L", "A": "A", "B": "B", "C": "C"}
STEREO_GROUPS = {"BUS", "MAIN", "MTX", "SEND", "MON"}


# Full names, for the status page
GROUP_FULL_NAMES = {
    "LCL": "Local", "A": "AES50 A", "B": "AES50 B", "C": "AES50 C", "AUX": "Aux",
    "SC": "StageConnect", "USB": "USB", "CRD": "Card", "MOD": "Dante", "PLAY": "USB player",
    "AES": "AES/EBU", "USR": "User signal", "OSC": "Oscillator", "BUS": "Bus", "MAIN": "Main",
    "MTX": "Matrix", "SEND": "FX send", "MON": "Monitor",
}
STRIP_FULL_NAMES = {"CH": "Channel", "AUX": "Aux", "BUS": "Bus", "MAIN": "Main", "MTX": "Matrix"}


USER_SIGNALS = 24  # USR entries 1-24 are user signals; 25-56 are user patches
USER_ENTRIES = 56


def user_entry(number: int, mode: Value) -> tuple[str, int, str]:
    """How the console names a user signal or patch entry: ("US" or "UP", number, side).

    Patches are numbered from 1 (entry 25 is patch 1). Each entry is mono, but an odd
    entry and the next one can be linked as a stereo pair, which takes the odd entry's
    number, with L and R; side is "" for a mono entry.
    """
    patch = number > USER_SIGNALS
    index = number - USER_SIGNALS if patch else number
    kind = "UP" if patch else "US"
    if mode in ("ST", "M/S"):
        return kind, index if index % 2 else index - 1, "L" if index % 2 else "R"
    return kind, index, ""


def user_full_label(number: int, mode: Value) -> str:
    kind, index, side = user_entry(number, mode)
    name = "User patch" if kind == "UP" else "User signal"
    return f"{name} {index}" + {"L": " left", "R": " right", "": ""}[side]


# Groups that can be switched to mono, and the node holding each one's switch
MONO_SWITCHES = {"BUS": "bus", "MAIN": "main", "MTX": "mtx"}
MONO_SWITCH_COUNTS = {"BUS": 16, "MAIN": 4, "MTX": 8}


def is_mono(group: Value, number: Value, state: State | None) -> bool:
    """Whether a bus, main or matrix source is mono. Its two source entries then carry the
    same signal, and are named without a side."""
    if group not in MONO_SWITCHES or not isinstance(number, int) or state is None:
        return False
    return bool(state.get(f"{MONO_SWITCHES[group]}.{(number + 1) // 2}.busmono"))


def mono_switch_paths() -> list[str]:
    return [f"{node}.{n}.busmono" for group, node in MONO_SWITCHES.items() for n in range(1, MONO_SWITCH_COUNTS[group] + 1)]


def source_full_label(group: Value, number: Value, state: State | None = None) -> str:
    """A source's full name, with the side for stereo halves: USB 3, Bus 1 right, or Bus 1 when mono."""
    name = GROUP_FULL_NAMES.get(group, str(group))
    if is_mono(group, number, state):
        return f"{name} {(number + 1) // 2}"
    if group in STEREO_GROUPS and isinstance(number, int):
        return f"{name} {(number + 1) // 2} {'left' if number % 2 else 'right'}"
    return f"{name} {number}"


def strip_full_label(label: str) -> str:
    """CH 13 -> Channel 13, AUX 2 -> Aux 2, and so on."""
    kind, _, rest = label.partition(" ")
    return f"{STRIP_FULL_NAMES.get(kind, kind)} {rest}".strip()


def source_label(group: Value, number: Value, state: State | None = None) -> str:
    """A short name for a source, with the side for stereo halves: USB 3, BUS 1R, or BUS 1 when mono."""
    if is_mono(group, number, state):
        return f"{group} {(number + 1) // 2}"
    if group in STEREO_GROUPS and isinstance(number, int):
        return f"{group} {(number + 1) // 2}{'L' if number % 2 else 'R'}"
    return f"{group} {number}"


@dataclass(frozen=True)
class Strip:
    """A channel strip with a source: an input channel or an aux channel."""

    kind: str  # "ch" or "aux"
    number: int

    @property
    def label(self) -> str:
        return f"{self.kind.upper()} {self.number}"


def strip_from_selidx(selidx: Value) -> Strip | None:
    """The strip for a $ctl.$stat.selidx value: 1-40 are channels, 41-48 aux channels.

    Buses, mains and so on have no source, so they have no strip here.
    """
    if not isinstance(selidx, int):
        return None
    if 1 <= selidx <= 40:
        return Strip("ch", selidx)
    if 41 <= selidx <= 48:
        return Strip("aux", selidx - 40)
    return None


# What the selected strip index means beyond input and aux channels
OTHER_STRIPS = [("BUS", 49, 16), ("MAIN", 65, 4), ("MTX", 69, 8)]


def strip_label(selidx: Value) -> str:
    """A label for any selected strip, such as CH 3, AUX 1 or BUS 16."""
    strip = strip_from_selidx(selidx)
    if strip is not None:
        return strip.label
    if isinstance(selidx, int):
        for kind, first, count in OTHER_STRIPS:
            if first <= selidx < first + count:
                return f"{kind} {selidx - first + 1}"
    return f"strip {selidx}"


def no_source_reason(selidx: Value) -> str:
    """Why a selected strip that is not an input or aux channel has no remote gain:
    buses, mains and matrices have no source."""
    return "no source"


@dataclass(frozen=True)
class Preamp:
    """A preamp on a paired console."""

    console: Pairing
    group: str
    number: int

    @property
    def path(self) -> str:
        return f"io.in.{self.group}.{self.number}"

    @property
    def label(self) -> str:
        return f"{self.console.display_name} {PREAMP_ABBREVIATIONS[self.group]}{self.number}"

    @property
    def full_label(self) -> str:
        return f"{self.console.display_name} {GROUP_FULL_NAMES[self.group]} {self.number}"


@dataclass(frozen=True)
class Steps:
    """A preamp's range: valid gains are minimum, minimum + step, ... up to maximum."""

    minimum: float
    maximum: float
    count: int

    @property
    def step(self) -> float:
        return (self.maximum - self.minimum) / (self.count - 1)

    def move(self, gain: float, steps: int) -> float:
        """The gain `steps` whole steps away from `gain`, kept within the range.

        Steps count from the bottom of the range, not from zero, so the
        result is always a value the preamp can really be set to.
        """
        index = round((gain - self.minimum) / self.step)
        index = max(0, min(self.count - 1, index + steps))
        return round(self.minimum + index * self.step, 3)


@dataclass(frozen=True)
class Target:
    """What the trim knob on a strip would control: a preamp, or the reason there is none."""

    strip: Strip
    preamp: Preamp | None
    reason: str | None = None  # Short, for the log
    explanation: str | None = None  # In full, for the status page
    through: str | None = None  # The user patch in between, if any, e.g. "user patch 1 left"
    halves: tuple = ()  # For each Dante input the strip takes: one, or two for a stereo strip

    @property
    def preamps(self) -> list["Preamp"]:
        """Every preamp the trim knob controls: one, or two on a stereo strip."""
        if self.halves:
            return [half.preamp for half in self.halves if half.preamp is not None]
        return [self.preamp] if self.preamp is not None else []


def local_inputs(pairing: Pairing) -> int:
    return LOCAL_INPUTS.get(pairing.console_model, LOCAL_INPUTS_DEFAULT)


def foh_paths(pairing: Pairing) -> list[str]:
    """Everything gain mode needs from a FOH console, beyond what naming already mirrors."""
    paths = ["$ctl.$stat.selidx"] + [f"$ctl.gpio.{n}.gpstate" for n in range(1, GPIO_COUNT + 1)]
    for kind, count in (("ch", 40), ("aux", 8)):
        for n in range(1, count + 1):
            paths += [
                f"{kind}.{n}.in.set.trim", f"{kind}.{n}.in.set.altsrc",
                f"{kind}.{n}.name", f"{kind}.{n}.icon", f"{kind}.{n}.col", f"{kind}.{n}.led",
            ]
    return paths


NO_ICON = 0
RED = 9  # From the colour chart in Behringer's protocol document
WHITE = 18  # The nearest to white in the same chart

# Fader sections by console model, and how many strips each shows at once. Each
# section can be on its own layer; the console reports what is on each layer, so
# this is all Headhunter needs to know about a model.
LAYER_SECTIONS = {
    "wing-compact": [("CMPCT", 12)],
    "wing-rack": [("RCK", 4)],
}
LAYER_SECTIONS_DEFAULT = [("L", 12), ("C", 8), ("R", 4)]  # The full-size Wing's three fader banks


def layer_sections(pairing: Pairing) -> list[tuple[str, int]]:
    return LAYER_SECTIONS.get(pairing.console_model, LAYER_SECTIONS_DEFAULT)


def layer_strip(kind: Value, index: Value) -> str | None:
    """The strip at a layer position, as a path prefix such as ch.13.

    CH positions are numbered like the selected channel (41-48 are aux channels).
    SENDS positions are sends on fader, numbered the same way; each fader's
    scribble strip shows the channel it sends from. BUS positions run through
    buses 1-16, then mains 1-4 (17-20) and matrices 1-8 (21-28). DCA positions
    are DCAs 1-16.
    """
    if not isinstance(index, int):
        return None
    if kind in ("CH", "SENDS"):
        strip = strip_from_selidx(index)
        return f"{strip.kind}.{strip.number}" if strip else None
    if kind == "BUS":
        if 1 <= index <= 16:
            return f"bus.{index}"
        if 17 <= index <= 20:
            return f"main.{index - 16}"
        if 21 <= index <= 28:
            return f"mtx.{index - 20}"
        return None
    if kind == "DCA" and 1 <= index <= 16:
        return f"dca.{index}"
    return None


NAME_MAX = 16


def display_text(levels, phantoms) -> str:
    """What the scribble strip shows while gain is being adjusted: 27.5dB, or 27.5dB ! 48V !.

    levels and phantoms are lists, one for each preamp on the strip (two on a stereo strip),
    or single values. Scribble strips are narrow, so positive gains have no plus sign. There
    is no padding: a space starts a new line on a scribble strip, and the console draws other
    space characters with no width. Phantom power is surrounded by exclamation marks so it
    stands out; on a stereo strip with phantom on one side only, the side is named.
    """
    if not isinstance(levels, (list, tuple)):
        levels, phantoms = [levels], [phantoms]
    shown = [f"{level:.1f}" if isinstance(level, (int, float)) else "?" for level in levels]
    numbers = shown[0] if len(set(shown)) == 1 else "/".join(shown)
    on = [bool(phantom) for phantom in phantoms]
    if all(on):
        marker = "48V"
    elif any(on):
        marker = "48V " + ("L" if on[0] else "R")
    else:
        return f"{numbers}dB"
    tight = marker.replace(" ", "")
    for text in (f"{numbers}dB ! {marker} !", f"{numbers}dB !{marker}!", f"{numbers} !{marker}!", f"{numbers} !{tight}!"):
        if len(text) <= NAME_MAX:
            return text
    return f"{numbers}!{marker.replace(' ', '')}"[:NAME_MAX]


def preamp_paths(pairing: Pairing) -> list[str]:
    """Every preamp's gain and phantom power on a console, and its AES50 ports' status."""
    paths = []
    for group, count in (("LCL", local_inputs(pairing)), *((port, AES50_INPUTS) for port in AES50_PORTS)):
        for n in range(1, count + 1):
            paths += [f"io.in.{group}.{n}.g", f"io.in.{group}.{n}.vph"]
    for port in AES50_PORTS:
        paths += [f"$stat.{port}.stat", f"$stat.{port}.dev"]
    return paths


def active_source(strip: Strip, state: State) -> tuple[Value, Value]:
    """The (group, number) a strip is currently listening to: its alt source if switched to it."""
    base = f"{strip.kind}.{strip.number}.in"
    if state.get(f"{base}.set.altsrc"):
        return state.get(f"{base}.conn.altgrp"), state.get(f"{base}.conn.altin")
    return state.get(f"{base}.conn.grp"), state.get(f"{base}.conn.in")


STEREO_MODES = ("ST", "M/S")


@dataclass(frozen=True)
class Half:
    """One Dante input of a strip's source, followed back to its preamp if it has one."""

    dante_input: int
    preamp: Preamp | None
    reason: str | None = None
    explanation: str | None = None
    through: str | None = None
    source: str | None = None  # What is on the other end, in full, e.g. "RACK Aux 1"
    problem: str | None = None  # Why there is no preamp, in a word or two, e.g. "no preamp"


def strip_inputs(number: int, foh_state: State) -> list[int]:
    """The Dante inputs a strip with source MOD `number` takes: two if that input is half of a
    stereo pair (an odd input and the next), otherwise one."""
    if number % 2 and foh_state.get(f"io.in.MOD.{number}.mode") in STEREO_MODES:
        return [number, number + 1]
    if not number % 2 and foh_state.get(f"io.in.MOD.{number - 1}.mode") in STEREO_MODES:
        return [number - 1, number]
    return [number]


def resolve(
    strip: Strip,
    foh: Pairing,
    pairings: list[Pairing],
    subscriptions: list[Subscription],
    states: Mapping[str, State],
) -> Target:
    """Follow a strip on a FOH console back to the remote preamp, or preamps, feeding it.

    A stereo strip takes two Dante inputs, and each is followed on its own.
    states maps console MAC to the mirrored state of every connected console.
    """
    group, number = active_source(strip, states[foh.console_mac])
    if group in (None, "OFF"):
        return Target(strip, None, "no source", "It has no source.")
    if group != "MOD":
        return Target(strip, None, f"source is {source_label(group, number)}",
                      f"Its source is {source_full_label(group, number)}, not a Dante input.")
    if not isinstance(number, int):
        return Target(strip, None, "source not understood", "Its source is not understood.")

    inputs = strip_inputs(number, states[foh.console_mac])
    halves = tuple(_follow(n, foh, pairings, subscriptions, states) for n in inputs)
    found = [half for half in halves if half.preamp is not None]
    if len(halves) == 1:
        half = halves[0]
        return Target(strip, half.preamp, half.reason, half.explanation, half.through, halves)
    if not found:
        return Target(
            strip, None,
            f"L: {halves[0].reason}; R: {halves[1].reason}",
            f"Left: {halves[0].explanation} Right: {halves[1].explanation}",
            halves=halves,
        )
    return Target(strip, found[0].preamp, through=found[0].through, halves=halves)


def _follow(
    number: int,
    foh: Pairing,
    pairings: list[Pairing],
    subscriptions: list[Subscription],
    states: Mapping[str, State],
) -> Half:
    """Follow one Dante input on a FOH console back to the preamp feeding it, if any."""
    sub = next(
        (s for s in subscriptions if s.rx_device == foh.dante_name and s.rx_channel == number and s.connected),
        None,
    )
    dante = f"Dante input {number}"
    if sub is None:
        return Half(number, None, f"Dante in {number} unsubscribed", f"{dante} is not subscribed to anything.",
                    source=dante, problem="unsubscribed")
    sender = next((p for p in pairings if p.dante_name == sub.tx_device), None)
    if sender is None or sender is foh:
        return Half(number, None, f"Dante in {number} is from {sub.tx_device}, not paired",
                    f"{dante} comes from {sub.tx_device}, which is not paired.",
                    source=f"{sub.tx_device} Dante output {sub.tx_channel}", problem="not paired")
    if sender.console_mac not in states:
        return Half(number, None, f"{sender.display_name} offline", f"{sender.display_name} is not connected.",
                    source=f"{sender.display_name} Dante output {sub.tx_channel}", problem="offline")

    tx_state = states[sender.console_mac]
    tx_group = tx_state.get(f"io.out.MOD.{sub.tx_channel}.grp")
    tx_number = tx_state.get(f"io.out.MOD.{sub.tx_channel}.in")
    where = f"{sender.display_name} out {sub.tx_channel}"
    where_full = f"{sender.display_name} Dante output {sub.tx_channel}"
    if tx_group in (None, "OFF"):
        return Half(number, None, f"{where} not patched", f"Nothing is patched to {where_full}.",
                    source=where_full, problem="not patched")

    # A user patch carries one input; follow it there
    through = None
    if tx_group == "USR" and isinstance(tx_number, int) and USER_SIGNALS < tx_number <= USER_ENTRIES:
        through = user_full_label(tx_number, tx_state.get(f"io.in.USR.{tx_number}.mode"))
        where, where_full = f"{where} via {through.replace('User patch', 'UP')}", f"{where_full}, through {through.lower()},"
        tx_group = tx_state.get(f"io.in.USR.{tx_number}.user.grp")
        tx_number = tx_state.get(f"io.in.USR.{tx_number}.user.in")
        if tx_group in (None, "OFF"):
            return Half(number, None, f"{where}, empty", f"{through} has no source.",
                        source=f"{sender.display_name} {through}", problem="empty")
        through = through.lower()
    if tx_group not in PREAMP_GROUPS:
        if tx_group == "USR" and isinstance(tx_number, int):
            mode = tx_state.get(f"io.in.USR.{tx_number}.mode")
            kind, index, side = user_entry(tx_number, mode)
            full = user_full_label(tx_number, mode)
            return Half(number, None, f"{where} is {kind} {index}{side}",
                        f"{where_full} carries {full}, which has no preamp.",
                        source=f"{sender.display_name} {full}", through=through, problem="no preamp")
        full = source_full_label(tx_group, tx_number, tx_state)
        return Half(number, None, f"{where} is {source_label(tx_group, tx_number, tx_state)}",
                    f"{where_full} carries {full}, which has no preamp.",
                    source=f"{sender.display_name} {full}", through=through, problem="no preamp")
    if not isinstance(tx_number, int):
        return Half(number, None, f"{where} is {tx_group} {tx_number!r}, not understood",
                    f"{where_full} reports a source Headhunter does not understand.",
                    source=where_full, through=through, problem="not understood")
    preamp = Preamp(sender, str(tx_group), int(tx_number))
    if tx_group in AES50_PORTS and tx_state.get(f"$stat.{tx_group}.stat") != "OK":
        return Half(number, None, f"{sender.display_name} AES50 {tx_group} empty",
                    f"Nothing is connected to {sender.display_name} AES50 {tx_group}.",
                    source=preamp.full_label, through=through, problem="nothing connected")
    if f"{preamp.path}.g" not in tx_state:
        return Half(number, None, f"{sender.display_name} has no {preamp.label.split()[-1]}",
                    f"{sender.display_name} has no {GROUP_FULL_NAMES[preamp.group]} {preamp.number}.",
                    source=preamp.full_label, through=through, problem="does not exist")
    return Half(number, preamp, through=through, source=preamp.full_label)


class Accumulators:
    """One Accumulator per preamp, so the two sides of a stereo strip each move in their own steps."""

    def __init__(self) -> None:
        self._each: dict[tuple, Accumulator] = {}

    def add(self, key: tuple, delta: float, step: float) -> int:
        return self._each.setdefault(key, Accumulator()).add(delta, step)

    def reset(self) -> None:
        self._each.clear()


class Accumulator:
    """Turns trim movement into whole gain steps, 1:1 in dB, carrying the remainder."""

    def __init__(self) -> None:
        self.total = 0.0

    def add(self, delta: float, step: float) -> int:
        """Add trim movement; return how many whole steps it has made up (negative for down)."""
        self.total = round(self.total + delta, 3)
        steps = int(self.total / step)  # Towards zero, so 2.4 dB of a 2.5 dB step does nothing
        self.total = round(self.total - steps * step, 3)
        return steps

    def reset(self) -> None:
        self.total = 0.0
