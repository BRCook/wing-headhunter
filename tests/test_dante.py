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


def test_netaudio_is_not_left_running_when_headhunter_stops(monkeypatch):
    import asyncio
    import sys

    from headhunter import dante

    started = {}
    real_exec = asyncio.create_subprocess_exec

    async def slow_netaudio(*args, **kwargs):
        process = await real_exec(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
        started["process"] = process
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_netaudio)

    async def scenario():
        task = asyncio.create_task(dante.run_netaudio([], "subscription", "list"))
        await asyncio.sleep(0.5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(started["process"].wait(), 2)  # Killed, not left sleeping

    asyncio.run(scenario())
