"""Pairing suggestions."""

import pytest

from headhunter.discovery import DiscoveredConsole, DiscoveredDanteDevice
from headhunter.pairing import default_display_name, suggest_card


def console(model, name="My-Console"):
    return DiscoveredConsole("10.0.0.2", name, model, "S1", "3.1.1", "00:00:00:00:00:01")


def card(name):
    return DiscoveredDanteDevice(name, "10.0.0.12", "00:1d:c1:00:00:01", "wing64 B")


CARDS = [card("WingCompact"), card("WingRack")]


@pytest.mark.parametrize("model, expected", [("wing-rack", "WingRack"), ("wing-compact", "WingCompact")])
def test_the_matching_card_is_suggested(model, expected):
    assert suggest_card(console(model), CARDS).name == expected


def test_nothing_is_suggested_when_the_match_is_ambiguous():
    assert suggest_card(console("wing-rack"), [card("WingRack"), card("WingRack-2")]) is None


def test_nothing_is_suggested_when_nothing_matches():
    assert suggest_card(console("ngc-full"), CARDS) is None


@pytest.mark.parametrize(
    "model, name, expected",
    [("wing-rack", "x", "RACK"), ("wing-compact", "x", "COMPACT"), ("ngc-full", "x", "WING"), ("mystery", "Front of house", "FRONT O")],
)
def test_default_display_names(model, name, expected):
    assert default_display_name(console(model, name)) == expected
