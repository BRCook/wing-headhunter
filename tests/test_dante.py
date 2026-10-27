"""Dante subscriptions, parsed from real netaudio output."""

import copy
import json
from pathlib import Path

import pytest

from headhunter.dante import parse_subscriptions

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def netaudio_output():
    subscriptions = json.loads((FIXTURES / "subscriptions.json").read_text())
    channels = json.loads((FIXTURES / "channels.json").read_text())
    return subscriptions, channels


def test_every_subscription_is_parsed(netaudio_output):
    parsed = parse_subscriptions(*netaudio_output)
    assert len(parsed) == 128
    assert all(s.connected for s in parsed)
    assert all(s.tx_channel == s.rx_channel for s in parsed)


def test_renamed_transmit_channels_map_back_to_their_numbers(netaudio_output):
    subscriptions, channels = copy.deepcopy(netaudio_output)
    channels["WingRack.local."]["tx_channels"]["05"]["friendly_name"] = "Kick"
    subscriptions[4]["tx_channel"] = "Kick"
    assert parse_subscriptions(subscriptions, channels)[4].tx_channel == 5


def test_unknown_transmit_channels_are_not_connected(netaudio_output):
    subscriptions, channels = copy.deepcopy(netaudio_output)
    subscriptions[0]["tx_channel"] = "Nonexistent"
    first = parse_subscriptions(subscriptions, channels)[0]
    assert first.tx_channel is None
    assert not first.connected
