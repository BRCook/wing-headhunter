"""Following a channel back to its remote preamp."""

import json
from pathlib import Path

import pytest

from headhunter.dante import parse_subscriptions
from headhunter.gain import Strip, resolve, strip_from_selidx
from headhunter.store import FOH, STAGE, Pairing

FIXTURES = Path(__file__).parent / "fixtures"
RACK = Pairing("rack-mac", "My-WingRCK", "wing-rack", "S1", "rack-dante", "WingRack", "RACK", STAGE)
COMPACT = Pairing("compact-mac", "My-WingCCT", "wing-compact", "S2", "compact-dante", "WingCompact", "COMPACT", FOH)


@pytest.fixture
def subscriptions():
    return parse_subscriptions(
        json.loads((FIXTURES / "subscriptions.json").read_text()),
        json.loads((FIXTURES / "channels.json").read_text()),
    )


@pytest.mark.parametrize(
    "selidx, expected",
    [(1, Strip("ch", 1)), (40, Strip("ch", 40)), (41, Strip("aux", 1)), (48, Strip("aux", 8)), (49, None), (None, None)],
)
def test_selected_strips(selidx, expected):
    assert strip_from_selidx(selidx) == expected


def states(compact=None, rack=None):
    rack_state = {
        "io.out.MOD.5.grp": "LCL", "io.out.MOD.5.in": 24, "io.in.LCL.24.g": 0.0,
        "io.out.MOD.6.grp": "A", "io.out.MOD.6.in": 3, "io.in.A.3.g": 0.5, "$stat.A.stat": "OK",
        "io.out.MOD.7.grp": "BUS", "io.out.MOD.7.in": 1,
        "io.out.MOD.8.grp": "B", "io.out.MOD.8.in": 1, "io.in.B.1.g": 0.0, "$stat.B.stat": "-",
        **(rack or {}),
    }
    compact_state = {
        "ch.1.in.conn.grp": "MOD", "ch.1.in.conn.in": 5,
        "ch.2.in.conn.grp": "MOD", "ch.2.in.conn.in": 5, "ch.2.in.conn.altgrp": "MOD",
        "ch.2.in.conn.altin": 6, "ch.2.in.set.altsrc": 1,
        "ch.3.in.conn.grp": "USB", "ch.3.in.conn.in": 3,
        "ch.4.in.conn.grp": "MOD", "ch.4.in.conn.in": 7,
        "ch.5.in.conn.grp": "MOD", "ch.5.in.conn.in": 8,
        **(compact or {}),
    }
    return {"compact-mac": compact_state, "rack-mac": rack_state}


def target(number, subscriptions, all_states=None):
    return resolve(Strip("ch", number), COMPACT, [RACK, COMPACT], subscriptions, all_states or states())


def test_a_local_preamp(subscriptions):
    assert target(1, subscriptions).preamp.label == "RACK L24"


def test_the_alt_source_is_followed_when_switched_to(subscriptions):
    assert target(2, subscriptions).preamp.label == "RACK A3"


@pytest.mark.parametrize(
    "number, reason",
    [
        (3, "source is USB 3"),
        (4, "RACK out 7 is BUS 1L"),
        (5, "RACK AES50 B empty"),
    ],
)
def test_reasons_trim_is_just_trim(subscriptions, number, reason):
    result = target(number, subscriptions)
    assert result.preamp is None
    assert result.reason == reason


def test_a_disconnected_stage_console(subscriptions):
    only_compact = {"compact-mac": states()["compact-mac"]}
    assert target(1, subscriptions, only_compact).reason == "RACK offline"


def test_an_unsubscribed_dante_input(subscriptions):
    unsubscribed = [s for s in subscriptions if not (s.rx_device == "WingCompact" and s.rx_channel == 5)]
    assert target(1, unsubscribed).reason == "Dante in 5 unsubscribed"


def test_a_preamp_the_console_does_not_have(subscriptions):
    no_local_24 = states()
    del no_local_24["rack-mac"]["io.in.LCL.24.g"]
    assert target(1, subscriptions, no_local_24).reason == "RACK has no L24"


def test_steps_count_from_the_bottom_of_the_range():
    from headhunter.gain import Steps

    sd8 = Steps(-2.0, 45.5, 20)
    assert sd8.step == 2.5
    assert sd8.move(0.5, 1) == 3.0
    assert sd8.move(0.5, -1) == -2.0
    assert sd8.move(0.6, 1) == 3.0  # Off-grid values are taken as the nearest valid one
    assert sd8.move(-2.0, -3) == -2.0  # Never below the range
    assert sd8.move(43.0, 5) == 45.5  # Never above it


def test_trim_is_turned_into_whole_steps():
    from headhunter.gain import Accumulator

    accumulator = Accumulator()
    assert [accumulator.add(delta, 2.5) for delta in (0.3, 0.6, 0.5, 0.4)] == [0, 0, 0, 0]
    assert accumulator.add(0.8, 2.5) == 1  # 2.6 dB in total: one step, 0.1 dB carried
    assert accumulator.total == pytest.approx(0.1)
    assert accumulator.add(-5.2, 2.5) == -2  # -5.1 dB: two steps down, -0.1 dB carried
    assert accumulator.add(7.6, 2.5) == 3


@pytest.mark.parametrize(
    "selidx, label",
    [(3, "CH 3"), (41, "AUX 1"), (48, "AUX 8"), (49, "BUS 1"), (64, "BUS 16"), (65, "MAIN 1"), (69, "MTX 1"), (76, "MTX 8"), (99, "strip 99")],
)
def test_strip_labels(selidx, label):
    from headhunter.gain import strip_label

    assert strip_label(selidx) == label


def test_buses_have_no_source():
    from headhunter.gain import no_source_reason

    assert no_source_reason(49) == "no source"


@pytest.mark.parametrize(
    "level, phantom, text",
    [
        (27.5, 0, "27.5dB"),
        (2.5, 0, "2.5dB"),
        (-2.0, 1, "-2.0dB ! 48V !"),
        (0.5, 1, "0.5dB ! 48V !"),
        (45.5, 1, "45.5dB ! 48V !"),
    ],
)
def test_display_text(level, phantom, text):
    from headhunter.gain import display_text

    assert display_text(level, phantom) == text
    assert len(text) <= 16
    assert not text.startswith(" ")  # A normal space would start a new line on the scribble strip


@pytest.mark.parametrize(
    "kind, index, expected",
    [("CH", 13, "ch.13"), ("CH", 41, "aux.1"), ("BUS", 16, "bus.16"), ("DCA", 3, "dca.3"),
     ("BUS", 17, "main.1"), ("BUS", 20, "main.4"), ("BUS", 21, "mtx.1"), ("BUS", 28, "mtx.8"), ("BUS", 29, None),
     ("SENDS", 7, "ch.7"), ("SENDS", 42, "aux.2"),
     ("OFF", 0, None), ("MIDI", 5, None), ("CH", None, None)],
)
def test_layer_strips(kind, index, expected):
    from headhunter.gain import layer_strip

    assert layer_strip(kind, index) == expected


def test_fader_sections_by_model():
    from headhunter.gain import layer_sections

    def sections(model):
        return layer_sections(Pairing("m", "n", model, "s", "d", "D", "X", STAGE))

    assert sections("wing-compact") == [("CMPCT", 12)]
    assert sections("wing-rack") == [("RCK", 4)]
    assert sections("ngc-full") == [("L", 12), ("C", 8), ("R", 4)]


@pytest.mark.parametrize("name, number", [("A", 1), ("b", 2), (" C ", 3), ("D", 4)])
def test_gpio_names(name, number):
    from headhunter.buttons import gpio_name, gpio_number

    assert gpio_number(name) == number
    assert gpio_name(number) == name.strip().upper()


@pytest.mark.parametrize("name", ["E", "3", "", "AB"])
def test_bad_gpio_names(name):
    from headhunter.buttons import gpio_number

    with pytest.raises(ValueError):
        gpio_number(name)