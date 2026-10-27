"""The long-running program, against fake consoles."""

import asyncio
import json
from pathlib import Path

import pytest

from fakewing import FakeWing
from headhunter import engine
from headhunter.dante import parse_subscriptions
from headhunter.naming import paths_for
from headhunter.native import NativeClient
from headhunter.store import FOH, STAGE, Pairing

FIXTURES = Path(__file__).parent / "fixtures"
RACK = Pairing("rack-mac", "My-WingRCK", "wing-rack", "S1", "rack-dante", "WingRack", "RACK", STAGE)
COMPACT = Pairing("compact-mac", "My-WingCCT", "wing-compact", "S2", "compact-dante", "WingCompact", "COMPACT", FOH)


def defaults(pairing: Pairing) -> dict:
    """Every parameter the engine mirrors, set to an unrouted, unnamed console."""
    values = {}
    for path in paths_for(pairing):
        leaf = path.rsplit(".", 1)[1]
        values[path] = {"clink": 0, "grp": "OFF", "altgrp": "OFF", "in": 1, "altin": 1}.get(leaf, "")
    return values


async def wait_for(condition, timeout: float = 5.0) -> None:
    async def poll():
        while not condition():
            await asyncio.sleep(0.05)

    await asyncio.wait_for(poll(), timeout)


@pytest.fixture(autouse=True)
def fast_timing(monkeypatch):
    monkeypatch.setattr(engine, "RETRY_SECONDS", 0.2)
    monkeypatch.setattr(engine, "SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(engine, "DANTE_POLL_SECONDS", 0.2)


@pytest.fixture
def network(monkeypatch):
    """Pretend every paired console is on 127.0.0.1, with real subscriptions from netaudio."""
    subscriptions = parse_subscriptions(
        json.loads((FIXTURES / "subscriptions.json").read_text()),
        json.loads((FIXTURES / "channels.json").read_text()),
    )

    async def locate(pairings):
        return {p.console_mac: "127.0.0.1" for p in pairings}

    async def read_subscriptions(pairings):
        return subscriptions

    monkeypatch.setattr(engine, "locate", locate)
    monkeypatch.setattr(engine, "read_subscriptions", read_subscriptions)


def test_a_dropped_connection_ends_cleanly():
    async def scenario():
        wing = FakeWing({})
        await wing.start()
        client = NativeClient("127.0.0.1", wing.port)
        await client.connect()
        assert await client.get("$syscfg.consolename") == "FakeWing"
        await wing.stop()
        await asyncio.wait_for(client.closed.wait(), 2)
        assert [event async for event in client.events()] == []
        with pytest.raises(ConnectionError):
            await client.get("$syscfg.consolename")
        await client.close()

    asyncio.run(scenario())


def test_names_are_written_kept_up_to_date_and_survive_a_reboot(network):
    async def scenario():
        rack = FakeWing({**defaults(RACK), "io.out.MOD.1.grp": "LCL", "io.out.MOD.1.in": 24})
        compact = FakeWing(defaults(COMPACT))
        await rack.start()
        await compact.start()
        running = engine.Engine([RACK, COMPACT], write_names=True, ports={"rack-mac": rack.port, "compact-mac": compact.port})
        task = asyncio.create_task(running.run())
        try:
            # Names are written when everything connects
            await wait_for(lambda: compact.values["io.in.MOD.1.name"] == "RACK-L24-HH")
            assert compact.values["io.in.MOD.2.name"] == "RACK-OFF-HH"
            assert all(path.startswith("io.in.MOD.") for path, _ in compact.writes)
            assert rack.writes == []  # The stage console is never written to

            # A routing change on the stage console is followed straight away
            rack.push("io.out.MOD.2.grp", "BUS")
            rack.push("io.out.MOD.2.in", 3)
            await wait_for(lambda: compact.values["io.in.MOD.2.name"] == "RACK-BS02L-HH")

            # Turning on a channel's customization link protects its source
            compact.push("ch.7.in.conn.grp", "MOD")
            compact.push("ch.7.in.conn.in", 3)
            compact.push("ch.7.clink", 1)
            rack.push("io.out.MOD.3.grp", "LCL")
            rack.push("io.out.MOD.3.in", 5)
            await asyncio.sleep(0.5)
            assert compact.values["io.in.MOD.3.name"] == "RACK-OFF-HH"

            # The stage console reboots with different routing; Headhunter reconnects and catches up
            await rack.stop()
            await wait_for(lambda: not running.links["rack-mac"].connected)
            rack.values["io.out.MOD.1.grp"] = "A"
            rack.values["io.out.MOD.1.in"] = 8
            await rack.start()
            await wait_for(lambda: compact.values["io.in.MOD.1.name"] == "RACK-A08-HH")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def test_nothing_is_written_without_permission(network):
    async def scenario():
        rack = FakeWing({**defaults(RACK), "io.out.MOD.1.grp": "LCL", "io.out.MOD.1.in": 24})
        compact = FakeWing(defaults(COMPACT))
        await rack.start()
        await compact.start()
        running = engine.Engine([RACK, COMPACT], write_names=False, ports={"rack-mac": rack.port, "compact-mac": compact.port})
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: any(p.planned == "RACK-L24-HH" for p in running.plans()))
            await asyncio.sleep(0.3)
            assert compact.writes == [] and rack.writes == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())
