"""Renaming a console's Dante device."""

import asyncio
import json
from pathlib import Path

import pytest

from headhunter import dante, renaming
from headhunter.dante import parse_subscriptions
from headhunter.store import STAGE, Pairing

FIXTURES = Path(__file__).parent / "fixtures"
RACK = Pairing("rack-mac", "My-WingRCK", "wing-rack", "S1", "rack-dante", "WingRack", "RACK", STAGE)


@pytest.fixture
def network(monkeypatch):
    """A fake Dante network with Ben's real subscriptions, which records changes."""
    state = {
        "subscriptions": parse_subscriptions(
            json.loads((FIXTURES / "subscriptions.json").read_text()),
            json.loads((FIXTURES / "channels.json").read_text()),
        ),
        "renamed": [],
        "subscribed": [],
    }

    async def read_all_subscriptions():
        return state["subscriptions"]

    async def rename_device(mac, name):
        state["renamed"].append((mac, name))

    async def subscribe(tx_device, tx_channel, rx_device, rx_channel):
        state["subscribed"].append((tx_device, tx_channel, rx_device, rx_channel))
        state["subscriptions"] = [
            dante.Subscription(s.rx_device, s.rx_channel, tx_device, tx_channel, True)
            if (s.rx_device, s.rx_channel) == (rx_device, rx_channel) else s
            for s in state["subscriptions"]
        ]

    monkeypatch.setattr(dante, "read_all_subscriptions", read_all_subscriptions)
    monkeypatch.setattr(dante, "rename_device", rename_device)
    monkeypatch.setattr(dante, "subscribe", subscribe)
    monkeypatch.setattr(renaming, "RETRY_SECONDS", 0.01)
    return state


@pytest.mark.parametrize("name", ["", "-Stage", "Stage-", "Has Space", "x" * 32, "Wing_Rack"])
def test_invalid_names_are_refused(name):
    with pytest.raises(ValueError):
        renaming.check_name(name, set())


def test_names_in_use_are_refused(network):
    with pytest.raises(ValueError, match="already in use"):
        asyncio.run(renaming.plan(RACK, "wingcompact"))


def test_the_plan_finds_every_subscription_to_the_device(network):
    plan = asyncio.run(renaming.plan(RACK, "WingStage"))
    assert len(plan.affected) == 64
    assert {s.rx_device for s in plan.affected} == {"WingCompact"}
    assert network["renamed"] == []  # Planning changes nothing


def test_renaming_moves_every_subscription(network):
    plan = asyncio.run(renaming.plan(RACK, "WingStage"))
    failed = asyncio.run(renaming.execute(plan))
    assert failed == []
    assert network["renamed"] == [("rack-dante", "WingStage")]
    assert sorted(network["subscribed"]) == sorted(("WingStage", n, "WingCompact", n) for n in range(1, 65))


def test_subscriptions_that_never_take_are_reported(network, monkeypatch):
    monkeypatch.setattr(renaming, "RECONNECT_SECONDS", 0.1)

    async def failing_subscribe(tx_device, tx_channel, rx_device, rx_channel):
        if rx_channel == 7:
            raise dante.NetaudioError("no such device")
        network["subscribed"].append((tx_device, tx_channel, rx_device, rx_channel))

    monkeypatch.setattr(dante, "subscribe", failing_subscribe)
    plan = asyncio.run(renaming.plan(RACK, "WingStage"))
    failed = asyncio.run(renaming.execute(plan))
    assert [s.rx_channel for s in failed] == [7]
