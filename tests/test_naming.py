"""Source names, and which Dante inputs get renamed."""

import asyncio
import json
from pathlib import Path

import pytest

from headhunter.dante import parse_subscriptions
from headhunter.naming import apply_names, plan_names, source_name
from headhunter.store import FOH, STAGE, Pairing

FIXTURES = Path(__file__).parent / "fixtures"
RACK = Pairing("rack-mac", "My-WingRCK", "wing-rack", "S1", "rack-dante", "WingRack", "RACK", STAGE)
COMPACT = Pairing("compact-mac", "My-WingCCT", "wing-compact", "S2", "compact-dante", "WingCompact", "COMPACT", FOH)


@pytest.mark.parametrize(
    "group, number, expected",
    [
        ("LCL", 24, "RACK-L24-HH"),
        ("A", 3, "RACK-A03-HH"),
        ("OSC", 1, "RACK-OS01-HH"),
        ("USR", 12, "RACK-US12-HH"),
        ("BUS", 1, "RACK-BS01L-HH"),
        ("BUS", 2, "RACK-BS01R-HH"),
        ("MAIN", 2, "RACK-MN01R-HH"),
        ("MTX", 7, "RACK-MX04L-HH"),
        ("SEND", 6, "RACK-SN03R-HH"),
        ("MON", 1, "RACK-MO01L-HH"),
        ("OFF", 1, "RACK-OFF-HH"),
        ("NEWTHING", 1, "RACK-??01-HH"),
    ],
)
def test_source_names(group, number, expected):
    assert source_name("RACK", group, number) == expected


def test_longest_names_fit_in_sixteen_characters():
    for group, number in [("BUS", 32), ("SEND", 32), ("USR", 56)]:
        assert len(source_name("COMPACT", group, number)) <= 16


def test_names_that_are_too_long_are_refused():
    with pytest.raises(ValueError):
        source_name("TOOLONGX", "BUS", 32)


class FakeConsole:
    """Answers reads from a dictionary, with sensible defaults, and records writes."""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.writes = []

    async def get(self, path):
        if path in self.values:
            return self.values[path]
        if path.endswith("clink"):
            return 0
        if path.endswith(("grp", "altgrp")):
            return "OFF"
        if path.endswith((".in", "altin")):
            return 1
        return ""

    def set(self, path, value):
        self.writes.append(path)
        self.values[path] = value


@pytest.fixture
def subscriptions():
    return parse_subscriptions(
        json.loads((FIXTURES / "subscriptions.json").read_text()),
        json.loads((FIXTURES / "channels.json").read_text()),
    )


def test_only_foh_consoles_are_renamed(subscriptions):
    clients = {"rack-mac": FakeConsole(), "compact-mac": FakeConsole()}
    plans = asyncio.run(plan_names([RACK, COMPACT], subscriptions, clients))
    assert len(plans) == 64
    assert {p.console.display_name for p in plans} == {"COMPACT"}


def test_names_follow_console_b_routing(subscriptions):
    rack = FakeConsole({"io.out.MOD.1.grp": "LCL", "io.out.MOD.1.in": 24, "io.out.MOD.2.grp": "BUS", "io.out.MOD.2.in": 2})
    plans = asyncio.run(plan_names([RACK, COMPACT], subscriptions, {"rack-mac": rack, "compact-mac": FakeConsole()}))
    assert [p.planned for p in plans[:3]] == ["RACK-L24-HH", "RACK-BS01R-HH", "RACK-OFF-HH"]


def test_inputs_used_by_linked_channels_are_skipped(subscriptions):
    compact = FakeConsole(
        {
            "ch.1.clink": 1, "ch.1.in.conn.grp": "MOD", "ch.1.in.conn.in": 1,
            "ch.5.clink": 1, "ch.5.in.conn.altgrp": "MOD", "ch.5.in.conn.altin": 3,
            "aux.2.clink": 1, "aux.2.in.conn.grp": "MOD", "aux.2.in.conn.in": 3,
            "ch.9.clink": 0, "ch.9.in.conn.grp": "MOD", "ch.9.in.conn.in": 4,
        }
    )
    clients = {"rack-mac": FakeConsole(), "compact-mac": compact}
    plans = asyncio.run(plan_names([RACK, COMPACT], subscriptions, clients))
    linked = {p.dante_input: p.linked for p in plans if p.linked}
    assert linked == {1: ("CH 1",), 3: ("CH 5", "AUX 2")}

    asyncio.run(apply_names(plans, clients))
    assert "io.in.MOD.1.name" not in compact.writes
    assert "io.in.MOD.3.name" not in compact.writes
    assert "io.in.MOD.4.name" in compact.writes


def test_correct_names_are_not_rewritten_and_failures_are_reported(subscriptions):
    compact = FakeConsole({"io.in.MOD.2.name": "RACK-OFF-HH"})
    original_set = compact.set
    compact.set = lambda path, value: original_set(path, value[:5] if path == "io.in.MOD.3.name" else value)
    clients = {"rack-mac": FakeConsole(), "compact-mac": compact}
    plans = asyncio.run(plan_names([RACK, COMPACT], subscriptions, clients))
    failed = asyncio.run(apply_names(plans, clients))
    assert "io.in.MOD.2.name" not in compact.writes
    assert [p.dante_input for p in failed] == [3]


@pytest.mark.parametrize(
    "number, mode, expected",
    [
        (25, "ST", "RACK-UP01L-HH"),  # Entries 25 and 26 linked as a stereo pair
        (26, "ST", "RACK-UP01R-HH"),
        (27, "M", "RACK-UP03-HH"),  # A mono patch keeps its own number
        (56, "ST", "RACK-UP31R-HH"),
        (3, "M", "RACK-US03-HH"),
        (1, "ST", "RACK-US01L-HH"),
        (2, "ST", "RACK-US01R-HH"),
    ],
)
def test_user_signals_and_patches(number, mode, expected):
    assert source_name("RACK", "USR", number, mode) == expected


def test_the_longest_user_patch_name_fits():
    assert len(source_name("COMPACT", "USR", 56, "ST")) == 16


def test_the_right_half_of_a_stereo_pair_is_left_alone(subscriptions):
    """A stereo channel makes its Dante input stereo, and the pair shares one name; only the
    left input is named, from what the stage console sends to it."""
    rack = FakeConsole({"io.out.MOD.37.grp": "USR", "io.out.MOD.37.in": 25, "io.in.USR.25.mode": "M",
                        "io.out.MOD.38.grp": "USR", "io.out.MOD.38.in": 26, "io.in.USR.26.mode": "M"})
    compact = FakeConsole({"io.in.MOD.37.mode": "ST"})
    clients = {"rack-mac": rack, "compact-mac": compact}
    plans = {p.dante_input: p for p in asyncio.run(plan_names([RACK, COMPACT], subscriptions, clients))}
    assert plans[37].planned == "RACK-UP01S-HH" and plans[37].needs_writing
    assert plans[38].paired_with == 37 and not plans[38].needs_writing

    asyncio.run(apply_names(list(plans.values()), clients))
    assert "io.in.MOD.37.name" in compact.writes
    assert "io.in.MOD.38.name" not in compact.writes


def test_mono_inputs_are_all_named(subscriptions):
    compact = FakeConsole({"io.in.MOD.37.mode": "M"})
    plans = {p.dante_input: p for p in asyncio.run(plan_names([RACK, COMPACT], subscriptions, {"rack-mac": FakeConsole(), "compact-mac": compact}))}
    assert plans[38].paired_with is None


@pytest.mark.parametrize(
    "group, number, mode, expected",
    [
        ("USR", 25, "ST", "RACK-UP01S-HH"),
        ("LCL", 1, None, "RACK-L01S-HH"),
        ("BUS", 31, None, "RACK-BS16S-HH"),
        ("OFF", 1, None, "RACK-OFF-HH"),
    ],
)
def test_stereo_pairs_are_marked_s(group, number, mode, expected):
    assert source_name("RACK", group, number, mode, stereo=True) == expected


def test_the_longest_stereo_name_fits():
    assert len(source_name("COMPACT", "BUS", 31, stereo=True)) == 16


@pytest.mark.parametrize(
    "group, number, expected",
    [("BUS", 1, "RACK-BS01-HH"), ("BUS", 2, "RACK-BS01-HH"), ("MAIN", 3, "RACK-MN02-HH"), ("MTX", 16, "RACK-MX08-HH")],
)
def test_mono_buses_mains_and_matrices_have_no_side(group, number, expected):
    assert source_name("RACK", group, number, mono=True) == expected


def test_a_mono_bus_on_a_stereo_pair_is_marked_s():
    assert source_name("RACK", "BUS", 1, stereo=True, mono=True) == "RACK-BS01S-HH"


def test_names_follow_the_mono_switch(subscriptions):
    rack = FakeConsole({
        "io.out.MOD.1.grp": "BUS", "io.out.MOD.1.in": 1, "io.out.MOD.2.grp": "BUS", "io.out.MOD.2.in": 2,
        "io.out.MOD.3.grp": "BUS", "io.out.MOD.3.in": 3, "bus.1.busmono": 1, "bus.2.busmono": 0,
    })
    plans = asyncio.run(plan_names([RACK, COMPACT], subscriptions, {"rack-mac": rack, "compact-mac": FakeConsole()}))
    assert [p.planned for p in plans[:3]] == ["RACK-BS01-HH", "RACK-BS01-HH", "RACK-BS02L-HH"]
