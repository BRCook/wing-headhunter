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


def source_label(group: Value, number: Value) -> str:
    """A short name for a source, with the side for stereo halves: USB 3, BUS 1R."""
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
    reason: str | None = None


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


def display_text(level: Value, phantom: Value) -> str:
    """What the scribble strip shows while gain is being adjusted: 27.5dB, or 27.5dB ! 48V !.

    Scribble strips are narrow, so positive gains have no plus sign. There is
    no padding: a space starts a new line on a scribble strip, and the console
    draws other space characters with no width. Phantom power is surrounded by
    exclamation marks so it stands out.
    """
    text = f"{level:.1f}dB" if isinstance(level, (int, float)) else "?dB"
    return f"{text} ! 48V !" if phantom else text


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


def resolve(
    strip: Strip,
    foh: Pairing,
    pairings: list[Pairing],
    subscriptions: list[Subscription],
    states: Mapping[str, State],
) -> Target:
    """Follow a strip on a FOH console back to the remote preamp feeding it.

    states maps console MAC to the mirrored state of every connected console.
    """
    group, number = active_source(strip, states[foh.console_mac])
    if group in (None, "OFF"):
        return Target(strip, None, "no source")
    if group != "MOD":
        return Target(strip, None, f"source is {source_label(group, number)}")

    sub = next(
        (s for s in subscriptions if s.rx_device == foh.dante_name and s.rx_channel == number and s.connected),
        None,
    )
    if sub is None:
        return Target(strip, None, f"Dante in {number} unsubscribed")
    sender = next((p for p in pairings if p.dante_name == sub.tx_device), None)
    if sender is None or sender is foh:
        return Target(strip, None, f"Dante in {number} is from {sub.tx_device}, not paired")
    if sender.console_mac not in states:
        return Target(strip, None, f"{sender.display_name} offline")

    tx_state = states[sender.console_mac]
    tx_group = tx_state.get(f"io.out.MOD.{sub.tx_channel}.grp")
    tx_number = tx_state.get(f"io.out.MOD.{sub.tx_channel}.in")
    where = f"{sender.display_name} out {sub.tx_channel}"
    if tx_group in (None, "OFF"):
        return Target(strip, None, f"{where} not patched")
    if tx_group not in PREAMP_GROUPS:
        return Target(strip, None, f"{where} is {source_label(tx_group, tx_number)}")
    if not isinstance(tx_number, int):
        return Target(strip, None, f"{where} is {tx_group} {tx_number!r}, not understood")
    if tx_group in AES50_PORTS and tx_state.get(f"$stat.{tx_group}.stat") != "OK":
        return Target(strip, None, f"{sender.display_name} AES50 {tx_group} empty")
    preamp = Preamp(sender, str(tx_group), int(tx_number))
    if f"{preamp.path}.g" not in tx_state:
        return Target(strip, None, f"{sender.display_name} has no {preamp.label.split()[-1]}")
    return Target(strip, preamp)


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