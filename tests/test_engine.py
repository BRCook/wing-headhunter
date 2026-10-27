"""The long-running program, against fake consoles."""

import asyncio
import contextlib
import json
from pathlib import Path

import pytest

from fakewing import FakeWing
from headhunter import engine
from headhunter.dante import parse_subscriptions
from headhunter.native import NativeClient
from headhunter.store import FOH, STAGE, Pairing

FIXTURES = Path(__file__).parent / "fixtures"
RACK = Pairing("rack-mac", "My-WingRCK", "wing-rack", "S1", "rack-dante", "WingRack", "RACK", STAGE)
COMPACT = Pairing("compact-mac", "My-WingCCT", "wing-compact", "S2", "compact-dante", "WingCompact", "COMPACT", FOH)


def defaults(pairing: Pairing) -> dict:
    """Every parameter the engine mirrors, set to an unrouted, unnamed console."""
    values = {}
    for path in engine.Engine._paths(pairing):
        leaf = path.rsplit(".", 1)[1]
        values[path] = {
            "clink": 0, "grp": "OFF", "altgrp": "OFF", "in": 1, "altin": 1, "g": 0.5, "vph": 0,
            "trim": 0.0, "altsrc": 0, "gpstate": 0, "selidx": 1, "stat": "-", "dev": "", "icon": 0,
            "col": 1, "led": 1,
        }.get(leaf, "")
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


def test_gain_mode_follows_its_button_and_the_selected_channel(network, caplog):
    """Read-only following: which preamp would the trim knob control?"""
    caplog.set_level("INFO", logger="headhunter.engine")
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3, "phantom_gpio": 4})

    async def scenario():
        rack = FakeWing(
            {
                **defaults(RACK),
                "io.out.MOD.1.grp": "A", "io.out.MOD.1.in": 8,
                "io.out.MOD.2.grp": "USB", "io.out.MOD.2.in": 1,
                "$stat.A.stat": "OK", "$stat.A.dev": "SD8",
            },
            ranges={"io.in.A.8.g": (-2.0, 45.5, 20)},
        )
        compact = FakeWing(
            {
                **defaults(foh),
                "$ctl.gpio.3.gpstate": 1,  # Left on, e.g. from before a crash
                "ch.13.in.conn.grp": "MOD", "ch.13.in.conn.in": 1,
                "ch.14.in.conn.grp": "MOD", "ch.14.in.conn.in": 2,
            }
        )
        await rack.start()
        await compact.start()
        running = engine.Engine([RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port})
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: running.links["compact-mac"].connected and running.links["rack-mac"].connected)
            await asyncio.sleep(0.3)
            # A button already on at startup does not turn gain mode on
            assert not running.gain_mode["compact-mac"]

            # Off, then on again: now it counts
            compact.push("$ctl.gpio.3.gpstate", 0)
            compact.push("$ctl.gpio.3.gpstate", 1)
            await wait_for(lambda: running.gain_mode["compact-mac"])

            # Selecting a channel fed by a stage box input follows it to the preamp
            compact.push("$ctl.$stat.selidx", 13)
            await wait_for(lambda: (t := running.targets.get("compact-mac")) and t.preamp is not None)
            assert running.targets["compact-mac"].preamp.label == "RACK A8"
            await wait_for(lambda: "-2 to +45.5, 2.5 dB steps" in caplog.text)
            assert "COMPACT CH 13 -> RACK A8: +0.5 dB, 48V off (SD8, -2 to +45.5, 2.5 dB steps)" in caplog.text

            # A channel fed by something with no preamp says why
            compact.push("$ctl.$stat.selidx", 14)
            await wait_for(lambda: (t := running.targets.get("compact-mac")) and t.strip.number == 14)
            assert running.targets["compact-mac"].reason == "RACK out 2 is USB 1"

            # Unplugging the stage box makes its preamps unreachable
            compact.push("$ctl.$stat.selidx", 13)
            rack.push("$stat.A.stat", "-")
            await wait_for(lambda: (t := running.targets.get("compact-mac")) and t.strip.number == 13 and t.preamp is None)
            assert running.targets["compact-mac"].reason == "RACK AES50 A empty"

            # Nothing has been written anywhere
            assert rack.writes == []
            assert all(path.startswith("io.in.MOD.") for path, _ in compact.writes)

            compact.push("$ctl.gpio.3.gpstate", 0)
            await wait_for(lambda: not running.gain_mode["compact-mac"])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("write_gain", [True, False])
def test_trim_becomes_gain_in_gain_mode(network, caplog, write_gain):
    caplog.set_level("INFO", logger="headhunter.engine")
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3})

    async def scenario():
        rack = FakeWing(
            {**defaults(RACK), "io.out.MOD.1.grp": "A", "io.out.MOD.1.in": 8, "$stat.A.stat": "OK", "$stat.A.dev": "SD8"},
            ranges={"io.in.A.8.g": (-2.0, 45.5, 20)},
        )
        compact = FakeWing({**defaults(foh), "ch.13.in.conn.grp": "MOD", "ch.13.in.conn.in": 1, "$ctl.$stat.selidx": 13})
        await rack.start()
        await compact.start()
        running = engine.Engine(
            [RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port}, write_gain=write_gain,
            restore_after=30,  # Restoring is tested separately
        )
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: (t := running.targets.get("compact-mac")) and t.preamp is not None)

            # Trim does nothing while gain mode is off
            compact.push("ch.13.in.set.trim", 3.0)
            await asyncio.sleep(0.3)
            assert rack.writes == []

            compact.push("$ctl.gpio.3.gpstate", 1)
            await wait_for(lambda: running.gain_mode["compact-mac"])

            # Trim on another channel does nothing
            compact.push("ch.1.in.set.trim", 5.0)
            await asyncio.sleep(0.3)
            assert rack.writes == []

            # 2.6 dB of trim on the selected channel, in small pieces: one 2.5 dB step
            for trim in (3.4, 4.1, 4.9, 5.6):
                compact.push("ch.13.in.set.trim", trim)
            if write_gain:
                await wait_for(lambda: rack.values["io.in.A.8.g"] == 3.0)
                # Back down by 5.2 dB in one go: two steps (0.1 dB was carried over)
                compact.push("ch.13.in.set.trim", 0.4)
                await wait_for(lambda: rack.values["io.in.A.8.g"] == -2.0)
                assert [value for _, value in rack.writes] == [3.0, -2.0]
                assert "ch.13.in.set.trim" not in dict(compact.writes)  # Not restored yet
            else:
                await wait_for(lambda: "COMPACT CH 13: would set RACK A8 +0.5 -> +3.0 dB" in caplog.text)
                assert rack.writes == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def layer(number: int, first: int) -> dict:
    """A Compact fader layer showing twelve channels from `first`, like its CH13-24 layer."""
    values = {"$ctl.layer.CMPCT.sel": number, f"$ctl.layer.CMPCT.{number}.ofs": 0}
    for position in range(1, 25):
        in_use = position <= 12
        values[f"$ctl.layer.CMPCT.{number}.{position}.type"] = "CH" if in_use else "OFF"
        values[f"$ctl.layer.CMPCT.{number}.{position}.i"] = first + position - 1 if in_use else 0
    return values


def stage_box_setup(foh):
    """A stage console with an SD8 on Dante output 1, and a FOH console with channels 13 and 14 fed by it."""
    rack = FakeWing(
        {**defaults(RACK), "io.out.MOD.1.grp": "A", "io.out.MOD.1.in": 8, "$stat.A.stat": "OK", "$stat.A.dev": "SD8"},
        ranges={"io.in.A.8.g": (-2.0, 45.5, 20)},
    )
    compact = FakeWing(
        {
            **defaults(foh),
            "ch.13.in.conn.grp": "MOD", "ch.13.in.conn.in": 1, "ch.13.in.set.trim": 2.0,
            "ch.14.in.conn.grp": "MOD", "ch.14.in.conn.in": 1,
            "$ctl.$stat.selidx": 13,
            **layer(2, 13),
        }
    )
    return rack, compact


def trim_writes(wing, path):
    return [value for written, value in wing.writes if written == path]


def test_trim_is_restored_once_the_knob_is_still(network):
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3})

    async def scenario():
        rack, compact = stage_box_setup(foh)
        await rack.start()
        await compact.start()
        running = engine.Engine(
            [RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port}, write_gain=True, restore_after=0.4
        )
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: (t := running.targets.get("compact-mac")) and t.preamp is not None)
            compact.push("$ctl.gpio.3.gpstate", 1)
            await wait_for(lambda: running.gain_mode["compact-mac"])

            # A trim change on another channel is a real trim change: it becomes that channel's snapshot
            compact.push("ch.14.in.set.trim", 6.0)

            # Adjust channel 13 by 2.6 dB: one step of gain, then trim goes back to +2 dB
            compact.push("ch.13.in.set.trim", 4.6)
            await wait_for(lambda: rack.values["io.in.A.8.g"] == 3.0)
            assert compact.values["ch.13.in.set.trim"] == 4.6  # Not while the knob might still be moving
            await wait_for(lambda: compact.values["ch.13.in.set.trim"] == 2.0)

            # Now adjust channel 14; it goes back to +6 dB, its trim from after gain mode was turned on
            compact.push("$ctl.$stat.selidx", 14)
            await asyncio.sleep(0.2)
            compact.push("ch.14.in.set.trim", 8.6)
            await wait_for(lambda: rack.values["io.in.A.8.g"] == 5.5)
            await wait_for(lambda: compact.values["ch.14.in.set.trim"] == 6.0)
            assert trim_writes(compact, "ch.14.in.set.trim") == [6.0]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("interruption", ["select another channel", "turn gain mode off", "lose the stage console"])
def test_an_interrupted_adjustment_is_restored_straight_away(network, interruption):
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3})

    async def scenario():
        rack, compact = stage_box_setup(foh)
        await rack.start()
        await compact.start()
        running = engine.Engine(
            [RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port}, write_gain=True, restore_after=30
        )
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: (t := running.targets.get("compact-mac")) and t.preamp is not None)
            compact.push("$ctl.gpio.3.gpstate", 1)
            await wait_for(lambda: running.gain_mode["compact-mac"])

            compact.push("ch.13.in.set.trim", 3.0)  # Less than a step: nothing applied yet
            await wait_for(lambda: "compact-mac" in running._adjusting)

            if interruption == "select another channel":
                compact.push("$ctl.$stat.selidx", 14)
            elif interruption == "turn gain mode off":
                compact.push("$ctl.gpio.3.gpstate", 0)
            else:
                await rack.stop()

            # Long before the 30 second restore delay
            await wait_for(lambda: compact.values["ch.13.in.set.trim"] == 2.0, timeout=3)
            assert "compact-mac" not in running._adjusting
            assert all(path != "io.in.A.8.g" for path, _ in rack.writes)  # The leftover 1 dB was discarded
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            with contextlib.suppress(Exception):
                await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def test_the_dry_run_only_says_it_would_restore(network, caplog):
    caplog.set_level("INFO", logger="headhunter.engine")
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3})

    async def scenario():
        rack, compact = stage_box_setup(foh)
        await rack.start()
        await compact.start()
        running = engine.Engine(
            [RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port}, write_gain=False, restore_after=0.3
        )
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: (t := running.targets.get("compact-mac")) and t.preamp is not None)
            compact.push("$ctl.gpio.3.gpstate", 1)
            await wait_for(lambda: running.gain_mode["compact-mac"])
            compact.push("ch.13.in.set.trim", 4.6)
            await wait_for(lambda: "COMPACT CH 13: would put trim back +4.6 -> +2.0 dB" in caplog.text)
            assert rack.writes == []
            assert trim_writes(compact, "ch.13.in.set.trim") == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


@pytest.fixture
def quick_flashes(monkeypatch):
    from headhunter import buttons

    monkeypatch.setattr(engine, "PHANTOM_HOLD_SECONDS", 0.3)
    monkeypatch.setattr(buttons, "FLASH_PATTERNS", {"on": [(0.01, 0.01)] * 5, "off": [(0.01, 0.01)] * 3})


@pytest.mark.parametrize("write_gain", [True, False])
def test_holding_the_phantom_button_toggles_48v(network, quick_flashes, caplog, write_gain):
    caplog.set_level("INFO", logger="headhunter.engine")
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3, "phantom_gpio": 4})

    async def scenario():
        rack, compact = stage_box_setup(foh)
        await rack.start()
        await compact.start()
        running = engine.Engine(
            [RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port}, write_gain=write_gain
        )
        task = asyncio.create_task(running.run())
        flashes = lambda: trim_writes(compact, "$ctl.gpio.4.gpstate")
        try:
            await wait_for(lambda: (t := running.targets.get("compact-mac")) and t.preamp is not None)

            # Outside gain mode, holding the button does nothing
            compact.push("$ctl.gpio.4.gpstate", 1)
            await asyncio.sleep(0.5)
            compact.push("$ctl.gpio.4.gpstate", 0)
            assert rack.writes == []

            compact.push("$ctl.gpio.3.gpstate", 1)
            await wait_for(lambda: running.gain_mode["compact-mac"])

            # A short press does nothing
            compact.push("$ctl.gpio.4.gpstate", 1)
            await asyncio.sleep(0.1)
            compact.push("$ctl.gpio.4.gpstate", 0)
            await asyncio.sleep(0.5)
            assert rack.writes == []

            # Holding it turns 48V on, then five flashes confirm it, ending dark
            compact.push("$ctl.gpio.4.gpstate", 1)
            if write_gain:
                await wait_for(lambda: rack.values["io.in.A.8.vph"] == 1)
                await wait_for(lambda: len(flashes()) == 11)
                assert flashes() == [0, 1] * 5 + [0]
                compact.push("$ctl.gpio.4.gpstate", 0)  # Let go

                # Holding it again turns 48V off, with three flashes
                compact.push("$ctl.gpio.4.gpstate", 1)
                await wait_for(lambda: rack.values["io.in.A.8.vph"] == 0)
                await wait_for(lambda: len(flashes()) == 11 + 7)
                assert "COMPACT CH 13: RACK A8 48V off" in caplog.text
            else:
                await wait_for(lambda: "COMPACT CH 13: would turn RACK A8 48V on" in caplog.text)
                await asyncio.sleep(0.3)
                assert rack.writes == [] and flashes() == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def test_changing_channel_mid_hold_cancels_it(network, quick_flashes):
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3, "phantom_gpio": 4})

    async def scenario():
        rack, compact = stage_box_setup(foh)
        await rack.start()
        await compact.start()
        running = engine.Engine([RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port}, write_gain=True)
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: (t := running.targets.get("compact-mac")) and t.preamp is not None)
            compact.push("$ctl.gpio.3.gpstate", 1)
            await wait_for(lambda: running.gain_mode["compact-mac"])
            compact.push("$ctl.gpio.4.gpstate", 1)
            await asyncio.sleep(0.1)
            compact.push("$ctl.$stat.selidx", 14)
            await asyncio.sleep(0.6)
            assert rack.writes == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


@pytest.fixture
def quick_display(monkeypatch):
    monkeypatch.setattr(engine, "DISPLAY_SECONDS", 0.6)


def test_gain_is_shown_on_the_scribble_strip(network, quick_flashes, quick_display):
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3, "phantom_gpio": 4})

    async def scenario():
        rack, compact = stage_box_setup(foh)
        compact.values["ch.13.name"] = "KICK IN"
        compact.values["ch.13.icon"] = 23
        await rack.start()
        await compact.start()
        running = engine.Engine(
            [RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port}, write_gain=True, restore_after=0.3
        )
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: (t := running.targets.get("compact-mac")) and t.preamp is not None)
            compact.push("$ctl.gpio.3.gpstate", 1)
            await wait_for(lambda: running.gain_mode["compact-mac"])

            # A gain change shows the new gain without the icon, then the real name and icon come back
            compact.push("ch.13.in.set.trim", 4.6)
            await wait_for(lambda: compact.values["ch.13.name"] == "3.0dB")
            assert compact.values["ch.13.icon"] == 0
            await wait_for(lambda: compact.values["ch.13.name"] == "KICK IN")
            assert compact.values["ch.13.icon"] == 23

            # Turning 48V on shows it too
            compact.push("$ctl.gpio.4.gpstate", 1)
            await wait_for(lambda: compact.values["ch.13.name"] == "3.0dB ! 48V !")
            compact.push("$ctl.gpio.4.gpstate", 0)
            await wait_for(lambda: compact.values["ch.13.name"] == "KICK IN")

            # Renaming the channel while the gain is showing: the new name stays, the icon still comes back
            compact.push("ch.13.in.set.trim", 4.6)
            await wait_for(lambda: compact.values["ch.13.name"] == "5.5dB ! 48V !")
            compact.push("ch.13.name", "KICK OUT")
            await asyncio.sleep(1.0)
            assert compact.values["ch.13.name"] == "KICK OUT"
            assert compact.values["ch.13.icon"] == 23

            # Changing the icon while the gain is showing: the new icon stays, the name still comes back
            compact.push("ch.13.in.set.trim", -0.6)  # 2.6 dB down from the restored +2 dB: one step
            await wait_for(lambda: compact.values["ch.13.name"] == "3.0dB ! 48V !")
            compact.push("ch.13.icon", 42)
            await wait_for(lambda: compact.values["ch.13.name"] == "KICK OUT")
            assert compact.values["ch.13.icon"] == 42

            # Turning gain mode off takes the display down straight away
            compact.push("ch.13.in.set.trim", 9.0)
            await wait_for(lambda: compact.values["ch.13.name"].endswith("48V !"))
            compact.push("$ctl.gpio.3.gpstate", 0)
            await wait_for(lambda: compact.values["ch.13.name"] == "KICK OUT", timeout=0.5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def test_shutting_down_puts_names_back(network, quick_flashes, monkeypatch):
    monkeypatch.setattr(engine, "DISPLAY_SECONDS", 60)
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3})

    async def scenario():
        rack, compact = stage_box_setup(foh)
        compact.values["ch.13.name"] = "KICK IN"
        await rack.start()
        await compact.start()
        running = engine.Engine([RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port}, write_gain=True)
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: (t := running.targets.get("compact-mac")) and t.preamp is not None)
            compact.push("$ctl.gpio.3.gpstate", 1)
            await wait_for(lambda: running.gain_mode["compact-mac"])
            compact.push("ch.13.in.set.trim", 4.6)
            await wait_for(lambda: compact.values["ch.13.name"] == "3.0dB")
            task.cancel()  # As Ctrl+C does
            await asyncio.gather(task, return_exceptions=True)
            await wait_for(lambda: compact.values["ch.13.name"] == "KICK IN", timeout=1)
        finally:
            task.cancel()
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def test_linked_channels_get_no_display(network, quick_display):
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3})

    async def scenario():
        rack, compact = stage_box_setup(foh)
        compact.values["ch.13.name"] = "KICK IN"
        compact.values["ch.13.clink"] = 1
        await rack.start()
        await compact.start()
        running = engine.Engine([RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port}, write_gain=True)
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: (t := running.targets.get("compact-mac")) and t.preamp is not None)
            compact.push("$ctl.gpio.3.gpstate", 1)
            await wait_for(lambda: running.gain_mode["compact-mac"])
            compact.push("ch.13.in.set.trim", 4.6)
            await wait_for(lambda: rack.values["io.in.A.8.g"] == 3.0)  # Gain still works
            await asyncio.sleep(0.2)
            assert "ch.13.name" not in dict(compact.writes)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


@pytest.fixture
def quick_lights(monkeypatch):
    monkeypatch.setattr(engine, "LIGHT_FLASH_SECONDS", 0.02)


def writes_to(wing, path):
    return [value for written, value in wing.writes if written == path]


# Light on, then off and on three times, ending off, then the original put back
FLASHED_LIGHT = [1, 0, 1, 0, 1, 0, 1, 0]


@pytest.mark.parametrize("turning", ["on", "off"])
def test_gain_mode_flashes_the_layer_white(network, quick_lights, turning):
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3})

    async def scenario():
        rack, compact = stage_box_setup(foh)
        compact.values["ch.13.col"] = 4
        compact.values["ch.20.led"] = 0  # A light normally off still flashes, and ends off
        await rack.start()
        await compact.start()
        running = engine.Engine([RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port}, write_gain=True)
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: running.links["compact-mac"].connected)
            compact.push("$ctl.gpio.3.gpstate", 1)
            if turning == "off":
                await wait_for(lambda: len(writes_to(compact, "ch.24.led")) == len(FLASHED_LIGHT) + 1)
                compact.writes.clear()
                compact.push("$ctl.gpio.3.gpstate", 0)
            await wait_for(lambda: len(writes_to(compact, "ch.24.led")) == len(FLASHED_LIGHT) + 1)
            assert writes_to(compact, "ch.13.col") == [18, 4]
            assert writes_to(compact, "ch.13.led") == FLASHED_LIGHT + [1]
            assert writes_to(compact, "ch.20.led") == FLASHED_LIGHT + [0]
            assert compact.values["ch.13.col"] == 4 and compact.values["ch.20.led"] == 0
            assert writes_to(compact, "ch.12.led") == [] and writes_to(compact, "ch.25.led") == []  # Not on the faders
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def test_quick_toggles_never_leave_strips_white(network, quick_lights):
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3})

    async def scenario():
        rack, compact = stage_box_setup(foh)
        compact.values["ch.13.col"] = 4
        await rack.start()
        await compact.start()
        running = engine.Engine([RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port}, write_gain=True)
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: running.links["compact-mac"].connected)
            for state in (1, 0, 1):
                compact.push("$ctl.gpio.3.gpstate", state)
            await wait_for(lambda: len(writes_to(compact, "ch.13.col")) == 6)
            assert writes_to(compact, "ch.13.col") == [18, 4] * 3
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def test_a_colour_changed_during_the_flash_is_kept(network, quick_lights, monkeypatch):
    monkeypatch.setattr(engine, "LIGHT_FLASH_SECONDS", 0.1)
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3})

    async def scenario():
        rack, compact = stage_box_setup(foh)
        compact.values["ch.13.col"] = 4
        await rack.start()
        await compact.start()
        running = engine.Engine([RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port}, write_gain=True)
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: running.links["compact-mac"].connected)
            compact.push("$ctl.gpio.3.gpstate", 1)
            await wait_for(lambda: writes_to(compact, "ch.13.col") == [18])
            compact.push("ch.13.col", 7)  # Someone picks a new colour mid-flash
            await wait_for(lambda: len(writes_to(compact, "ch.13.col")) == 2)
            assert compact.values["ch.13.col"] == 7
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def test_the_layer_does_not_flash_in_a_dry_run(network, quick_lights):
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3})

    async def scenario():
        rack, compact = stage_box_setup(foh)
        await rack.start()
        await compact.start()
        running = engine.Engine([RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port}, write_gain=False)
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: running.links["compact-mac"].connected)
            compact.push("$ctl.gpio.3.gpstate", 1)
            await wait_for(lambda: running.gain_mode["compact-mac"])
            await asyncio.sleep(0.3)
            assert writes_to(compact, "ch.13.led") == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def test_phantom_power_flashes_the_channel_red_both_ways(network, quick_flashes, quick_lights):
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3, "phantom_gpio": 4})

    async def scenario():
        rack, compact = stage_box_setup(foh)
        compact.values["ch.13.col"] = 4
        await rack.start()
        await compact.start()
        running = engine.Engine([RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port}, write_gain=True)
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: (t := running.targets.get("compact-mac")) and t.preamp is not None)
            compact.push("$ctl.gpio.3.gpstate", 1)
            await wait_for(lambda: running.gain_mode["compact-mac"])
            await wait_for(lambda: len(writes_to(compact, "ch.13.col")) == 2)  # The layer flash

            for vph in (1, 0):
                before = len(writes_to(compact, "ch.13.col"))
                compact.push("$ctl.gpio.4.gpstate", 1)
                await wait_for(lambda: rack.values["io.in.A.8.vph"] == vph)
                await wait_for(lambda: len(writes_to(compact, "ch.13.col")) == before + 2)
                assert writes_to(compact, "ch.13.col")[before:] == [9, 4]
                compact.push("$ctl.gpio.4.gpstate", 0)
            assert compact.values["ch.13.col"] == 4
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def test_a_gain_mode_button_on_at_boot_is_ignored(network, quick_lights, caplog):
    caplog.set_level("INFO", logger="headhunter.engine")
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3})

    async def scenario():
        rack, compact = stage_box_setup(foh)
        compact.values["$ctl.gpio.3.gpstate"] = 1
        await rack.start()
        await compact.start()
        running = engine.Engine([RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port}, write_gain=True)
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: "COMPACT: gain mode toggle ON at boot; ignoring" in caplog.text)

            # Turning it off changes nothing: no log line, no flash
            compact.push("$ctl.gpio.3.gpstate", 0)
            await asyncio.sleep(0.4)
            assert "gain mode off" not in caplog.text
            assert writes_to(compact, "ch.13.led") == []

            # Turning it on again is a real change
            compact.push("$ctl.gpio.3.gpstate", 1)
            await wait_for(lambda: "COMPACT: gain mode on" in caplog.text)
            await wait_for(lambda: len(writes_to(compact, "ch.13.led")) == len(FLASHED_LIGHT) + 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def test_bus_layers_flash_the_buses_on_the_faders(network, quick_lights):
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3})

    async def scenario():
        rack, compact = stage_box_setup(foh)
        compact.values.update({"$ctl.layer.CMPCT.sel": 5, "$ctl.layer.CMPCT.5.ofs": 4})  # Scrolled to buses 5-16
        for position in range(1, 25):
            compact.values[f"$ctl.layer.CMPCT.5.{position}.type"] = "BUS" if position <= 16 else "OFF"
            compact.values[f"$ctl.layer.CMPCT.5.{position}.i"] = position if position <= 16 else 0
        for n in range(1, 17):
            compact.values[f"bus.{n}.col"] = 2
            compact.values[f"bus.{n}.led"] = 1
        await rack.start()
        await compact.start()
        running = engine.Engine([RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port}, write_gain=True)
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: running.links["compact-mac"].connected)
            compact.push("$ctl.gpio.3.gpstate", 1)
            await wait_for(lambda: len(writes_to(compact, "bus.16.led")) == len(FLASHED_LIGHT) + 1)
            assert writes_to(compact, "bus.5.col") == [18, 2]
            assert writes_to(compact, "bus.4.led") == []  # Scrolled off the faders
            assert writes_to(compact, "ch.13.led") == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


async def drop_and_return(running, wing, mac, away=0.6):
    """Take a console off the network for a while, as a pulled cable would, then bring it back."""
    await wing.stop()
    await wait_for(lambda: not running.links[mac].connected)
    await asyncio.sleep(away)
    await wing.start()
    await wait_for(lambda: running.links[mac].connected)


def test_losing_the_foh_console_starts_over(network, quick_display, caplog):
    """A dropped FOH console means nothing in progress can be trusted: gain mode turns off, and
    nothing is finished or restored when it comes back, just as after a restart."""
    caplog.set_level("INFO", logger="headhunter.engine")
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3})

    async def scenario():
        rack, compact = stage_box_setup(foh)
        compact.values["ch.13.name"] = "KICK IN"
        await rack.start()
        await compact.start()
        running = engine.Engine(
            [RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port}, write_gain=True, restore_after=0.3
        )
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: (t := running.targets.get("compact-mac")) and t.preamp is not None)
            compact.push("$ctl.gpio.3.gpstate", 1)
            await wait_for(lambda: running.gain_mode["compact-mac"])
            compact.push("ch.13.in.set.trim", 4.6)
            await wait_for(lambda: compact.values["ch.13.name"] == "3.0dB")

            await drop_and_return(running, compact, "compact-mac", away=1.0)
            assert not running.gain_mode["compact-mac"]
            assert "gain mode toggle ON at reconnect; ignoring" in caplog.text
            await asyncio.sleep(1.0)
            # Left exactly as they were when the connection dropped
            assert compact.values["ch.13.name"] == "3.0dB"
            assert compact.values["ch.13.in.set.trim"] == 4.6

            # The button needs turning off and on again
            compact.push("$ctl.gpio.3.gpstate", 0)
            compact.push("$ctl.gpio.3.gpstate", 1)
            await wait_for(lambda: running.gain_mode["compact-mac"])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def test_a_value_headhunter_does_not_understand_does_not_stop_it(network):
    async def scenario():
        rack = FakeWing({**defaults(RACK), "io.out.MOD.1.grp": "LCL", "io.out.MOD.1.in": 24})
        compact = FakeWing(defaults(COMPACT))
        await rack.start()
        await compact.start()
        running = engine.Engine([RACK, COMPACT], write_names=True, ports={"rack-mac": rack.port, "compact-mac": compact.port})
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: compact.values["io.in.MOD.1.name"] == "RACK-L24-HH")
            rack.push("io.out.MOD.2.grp", "LCL")
            rack.push("io.out.MOD.2.in", "nonsense")
            rack.push("io.out.MOD.3.grp", "LCL")
            rack.push("io.out.MOD.3.in", 7)
            await wait_for(lambda: compact.values["io.in.MOD.3.name"] == "RACK-L07-HH")
            assert not task.done()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def test_consoles_can_be_paired_and_unpaired_while_running(network):
    async def scenario():
        rack = FakeWing({**defaults(RACK), "io.out.MOD.1.grp": "LCL", "io.out.MOD.1.in": 24})
        compact = FakeWing(defaults(COMPACT))
        await rack.start()
        await compact.start()
        ports = {"rack-mac": rack.port, "compact-mac": compact.port}
        running = engine.Engine([COMPACT], write_names=True, ports=ports)  # The Rack is not paired yet
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: running.links["compact-mac"].connected)
            await running.reconfigure([RACK, COMPACT])
            await wait_for(lambda: compact.values["io.in.MOD.1.name"] == "RACK-L24-HH")

            await running.reconfigure([COMPACT])
            assert "rack-mac" not in running.links
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def test_changing_a_foh_consoles_buttons_turns_gain_mode_off(network):
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3})

    async def scenario():
        rack, compact = stage_box_setup(foh)
        await rack.start()
        await compact.start()
        running = engine.Engine(
            [RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port}, write_gain=True, restore_after=30
        )
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: (t := running.targets.get("compact-mac")) and t.preamp is not None)
            compact.push("$ctl.gpio.3.gpstate", 1)
            await wait_for(lambda: running.gain_mode["compact-mac"])
            compact.push("ch.13.in.set.trim", 3.0)  # An adjustment in progress
            await wait_for(lambda: "compact-mac" in running._adjusting)

            moved = Pairing(**{**foh.__dict__, "gain_gpio": 1})
            await running.reconfigure([RACK, moved])
            assert not running.gain_mode["compact-mac"]
            await wait_for(lambda: compact.values["ch.13.in.set.trim"] == 2.0)  # Restored straight away

            # The new button works; the old one does nothing
            compact.push("$ctl.gpio.3.gpstate", 0)
            compact.push("$ctl.gpio.3.gpstate", 1)
            await asyncio.sleep(0.3)
            assert not running.gain_mode["compact-mac"]
            compact.push("$ctl.gpio.1.gpstate", 1)
            await wait_for(lambda: running.gain_mode["compact-mac"])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def test_changing_role_reconnects_and_takes_effect(network):
    async def scenario():
        rack = FakeWing({**defaults(RACK), "io.out.MOD.1.grp": "LCL", "io.out.MOD.1.in": 24})
        compact = FakeWing(defaults(COMPACT))
        await rack.start()
        await compact.start()
        as_stage = Pairing(**{**COMPACT.__dict__, "role": STAGE})
        running = engine.Engine([RACK, as_stage], write_names=True, ports={"rack-mac": rack.port, "compact-mac": compact.port})
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: running.links["compact-mac"].connected)
            await asyncio.sleep(0.3)
            assert compact.writes == []  # No FOH console, so nothing is named

            await running.reconfigure([RACK, COMPACT])
            await wait_for(lambda: compact.values["io.in.MOD.1.name"] == "RACK-L24-HH")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def test_changes_in_the_database_are_applied_while_running(network, monkeypatch, tmp_path):
    from headhunter.store import Store

    monkeypatch.setattr(engine, "CONFIG_POLL_SECONDS", 0.1)
    path = tmp_path / "headhunter.db"
    with Store(path) as store:
        store._db.execute(
            "INSERT INTO consoles VALUES (?, ?, ?, ?, ?, ?, ?, 't', 't', ?, NULL, NULL)",
            (RACK.console_mac, RACK.console_name, RACK.console_model, RACK.console_serial,
             RACK.dante_mac, RACK.dante_name, RACK.display_name, STAGE),
        )
        store._db.execute(
            "INSERT INTO consoles VALUES (?, ?, ?, ?, ?, ?, ?, 't', 't', ?, NULL, NULL)",
            (COMPACT.console_mac, COMPACT.console_name, COMPACT.console_model, COMPACT.console_serial,
             COMPACT.dante_mac, COMPACT.dante_name, COMPACT.display_name, FOH),
        )
        store._db.commit()

    async def scenario():
        rack = FakeWing({**defaults(RACK), "io.out.MOD.1.grp": "LCL", "io.out.MOD.1.in": 24})
        compact = FakeWing(defaults(COMPACT))
        await rack.start()
        await compact.start()
        with Store(path) as store:
            pairings = store.pairings()
        running = engine.Engine(pairings, ports={"rack-mac": rack.port, "compact-mac": compact.port}, store_path=path)
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: running.links["compact-mac"].connected)
            await asyncio.sleep(0.3)
            assert compact.writes == []  # Not writing names yet

            with Store(path) as store:  # As another headhunter command would
                store.set_setting("write_names", "on")
                store.rename(RACK.console_mac, "STAGE")
            await wait_for(lambda: compact.values["io.in.MOD.1.name"] == "STAGE-L24-HH")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def test_renaming_a_whole_console_is_summed_up_in_one_line(network, caplog):
    caplog.set_level("INFO", logger="headhunter.engine")

    async def scenario():
        rack = FakeWing({**defaults(RACK), **{f"io.out.MOD.{n}.grp": "LCL" for n in range(1, 4)},
                         **{f"io.out.MOD.{n}.in": n for n in range(1, 4)}})
        compact = FakeWing(defaults(COMPACT))
        await rack.start()
        await compact.start()
        running = engine.Engine([RACK, COMPACT], write_names=True, ports={"rack-mac": rack.port, "compact-mac": compact.port})
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: "COMPACT: renamed 64 Dante inputs" in caplog.text)
            assert "COMPACT MOD 1:" not in caplog.text

            rack.push("io.out.MOD.2.in", 9)  # A single change is logged on its own
            await wait_for(lambda: "COMPACT MOD 2: 'RACK-L02-HH' -> 'RACK-L09-HH'" in caplog.text)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def test_status_describes_what_the_page_shows(network):
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3, "phantom_gpio": 4})

    async def scenario():
        rack, compact = stage_box_setup(foh)
        rack.values["io.in.A.8.vph"] = 1
        await rack.start()
        await compact.start()
        running = engine.Engine([RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port})
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: (t := running.targets.get("compact-mac")) and t.preamp is not None)
            status = running.status()
            compact_status = next(c for c in status["consoles"] if c["name"] == "COMPACT")
            assert compact_status["connected"] and compact_status["ip"] == "127.0.0.1"
            assert (compact_status["gain_button"], compact_status["phantom_button"]) == ("C", "D")
            assert compact_status["gain_mode"] is False
            assert compact_status["selected"] == {
                "strip": "Channel 13", "stereo": False,
                "sides": [{"side": "", "source": "RACK AES50 A 8", "through": None, "gain": 0.5, "phantom": True}],
            }
            assert status["writing"] == {"names": False, "gain": False}

            compact.push("$ctl.$stat.selidx", 5)  # No source
            await wait_for(lambda: running.status()["consoles"][1]["selected"].get("reason") == "It has no source.")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def stereo_setup(foh):
    """Channel 13 on a stereo pair of Dante inputs (37 and 38), fed by a local input with 2.5 dB
    steps on the left and an AES50 input with 0.5 dB steps on the right."""
    rack = FakeWing(
        {
            **defaults(RACK),
            "io.out.MOD.37.grp": "LCL", "io.out.MOD.37.in": 1, "io.in.LCL.1.g": 10.0,
            "io.out.MOD.38.grp": "A", "io.out.MOD.38.in": 8, "io.in.A.8.g": 20.0,
            "$stat.A.stat": "OK", "$stat.A.dev": "DL16",
        },
        ranges={"io.in.LCL.1.g": (-2.5, 45.0, 20), "io.in.A.8.g": (-3.0, 45.5, 98)},
    )
    compact = FakeWing({
        **defaults(foh), "ch.13.in.conn.grp": "MOD", "ch.13.in.conn.in": 37, "ch.13.in.set.trim": 0.0,
        "io.in.MOD.37.mode": "ST", "$ctl.$stat.selidx": 13, "ch.13.name": "OH",
    })
    return rack, compact


def test_a_stereo_channel_adjusts_both_preamps(network, quick_flashes, quick_display):
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3, "phantom_gpio": 4})

    async def scenario():
        rack, compact = stereo_setup(foh)
        await rack.start()
        await compact.start()
        running = engine.Engine(
            [RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port}, write_gain=True, restore_after=30
        )
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: (t := running.targets.get("compact-mac")) and len(t.preamps) == 2)
            compact.push("$ctl.gpio.3.gpstate", 1)
            await wait_for(lambda: running.gain_mode["compact-mac"])

            # 2.6 dB of trim: one 2.5 dB step on the left, five 0.5 dB steps on the right
            compact.push("ch.13.in.set.trim", 2.6)
            await wait_for(lambda: rack.values["io.in.LCL.1.g"] == 12.5 and rack.values["io.in.A.8.g"] == 22.5)
            await wait_for(lambda: compact.values["ch.13.name"] == "12.5/22.5dB")

            # 1.4 dB more: not a step on the left, but two and a half on the right
            compact.push("ch.13.in.set.trim", 4.0)
            await wait_for(lambda: rack.values["io.in.A.8.g"] == 24.0)
            assert rack.values["io.in.LCL.1.g"] == 12.5

            # Phantom power, off on both: one hold turns both on
            compact.push("$ctl.gpio.4.gpstate", 1)
            await wait_for(lambda: rack.values["io.in.LCL.1.vph"] == 1 and rack.values["io.in.A.8.vph"] == 1)

            status = next(c for c in running.status()["consoles"] if c["name"] == "COMPACT")["selected"]
            assert status["stereo"] and [s["side"] for s in status["sides"]] == ["Left", "Right"]
            assert [s["source"] for s in status["sides"]] == ["RACK Local 1", "RACK AES50 A 8"]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def test_phantom_on_one_side_of_a_stereo_channel_turns_both_on(network, quick_flashes):
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3, "phantom_gpio": 4})

    async def scenario():
        rack, compact = stereo_setup(foh)
        rack.values["io.in.A.8.vph"] = 1  # Already on, on the right only
        await rack.start()
        await compact.start()
        running = engine.Engine([RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port}, write_gain=True)
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: (t := running.targets.get("compact-mac")) and len(t.preamps) == 2)
            compact.push("$ctl.gpio.3.gpstate", 1)
            await wait_for(lambda: running.gain_mode["compact-mac"])
            compact.push("$ctl.gpio.4.gpstate", 1)
            await wait_for(lambda: rack.values["io.in.LCL.1.vph"] == 1)
            assert rack.values["io.in.A.8.vph"] == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def test_phantom_power_can_be_switched_back_during_its_flash(network, monkeypatch):
    """A slip of the finger must be undoable at once: pressing the button stops its flash."""
    from headhunter import buttons

    monkeypatch.setattr(engine, "PHANTOM_HOLD_SECONDS", 0.3)
    monkeypatch.setattr(buttons, "FLASH_PATTERNS", {"on": [(0.3, 0.3)] * 5, "off": [(0.3, 0.3)] * 3})  # Three seconds
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3, "phantom_gpio": 4})

    async def scenario():
        rack, compact = stage_box_setup(foh)
        await rack.start()
        await compact.start()
        running = engine.Engine([RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port}, write_gain=True)
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: (t := running.targets.get("compact-mac")) and t.preamp is not None)
            compact.push("$ctl.gpio.3.gpstate", 1)
            await wait_for(lambda: running.gain_mode["compact-mac"])

            compact.push("$ctl.gpio.4.gpstate", 1)  # Oops: hold, 48V on, and the flash starts
            await wait_for(lambda: rack.values["io.in.A.8.vph"] == 1)
            compact.push("$ctl.gpio.4.gpstate", 0)
            await asyncio.sleep(0.2)

            compact.push("$ctl.gpio.4.gpstate", 1)  # Straight back, while it is still flashing
            await wait_for(lambda: rack.values["io.in.A.8.vph"] == 0, timeout=1.5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def test_a_press_after_an_unreported_release_still_counts(network, quick_flashes):
    """While the button flashes under a finger, the console may never report it being let go.
    The next press must still count, even though Headhunter thinks it is already pressed."""
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3, "phantom_gpio": 4})

    async def scenario():
        rack, compact = stage_box_setup(foh)
        await rack.start()
        await compact.start()
        running = engine.Engine([RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port}, write_gain=True)
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: (t := running.targets.get("compact-mac")) and t.preamp is not None)
            compact.push("$ctl.gpio.3.gpstate", 1)
            await wait_for(lambda: running.gain_mode["compact-mac"])

            compact.push("$ctl.gpio.4.gpstate", 1)
            await wait_for(lambda: rack.values["io.in.A.8.vph"] == 1)
            await asyncio.sleep(0.3)  # Let go during the flash; the console never says so

            compact.push("$ctl.gpio.4.gpstate", 1)  # The next press
            await wait_for(lambda: rack.values["io.in.A.8.vph"] == 0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def with_buttons(wing, phantom_flag="D-P"):
    """Give a fake Compact Ben's custom control buttons: gain mode on GPIO C, phantom power on GPIO D."""
    from headhunter import controls

    for button in controls.all_buttons(COMPACT):
        wing.add_button(button.path)
    wing.add_button("$ctl.user.U2.1.bu", "GPIO", "", "C")
    wing.add_button("$ctl.user.U2.2.bu", "GPIO", "", phantom_flag)
    return wing


def test_the_phantom_button_label_says_what_it_will_do(network, quick_flashes):
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3, "phantom_gpio": 4})

    async def scenario():
        rack, compact = stage_box_setup(foh)
        with_buttons(compact)

        def label():
            return compact.values["$ctl.user.U2.2.bu.name"]

        await rack.start()
        await compact.start()
        running = engine.Engine([RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port}, write_gain=True)
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: label() == "|HH DISABLED")
            assert compact.values["$ctl.user.U2.1.bu.name"] == "|HH GAIN OFF"
            compact.push("$ctl.gpio.3.gpstate", 1)
            await wait_for(lambda: label() == "|HH 48V ON")  # Channel 13: 48V off
            assert compact.values["$ctl.user.U2.1.bu.name"] == "|HH GAIN ON"
            compact.push("$ctl.gpio.4.gpstate", 1)
            await wait_for(lambda: label() == "|HH 48V OFF")  # After turning it on
            compact.push("$ctl.gpio.4.gpstate", 0)
            compact.push("$ctl.$stat.selidx", 5)  # A channel with no source
            await wait_for(lambda: label() == "|HH NO 48V")
            compact.push("$ctl.gpio.3.gpstate", 0)
            await wait_for(lambda: label() == "|HH DISABLED")
            assert compact.values["$ctl.user.U2.1.bu.name"] == "|HH GAIN OFF"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def test_a_toggle_phantom_button_is_refused(network, quick_flashes, caplog):
    caplog.set_level("INFO", logger="headhunter.engine")
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3, "phantom_gpio": 4})

    async def scenario():
        rack, compact = stage_box_setup(foh)
        with_buttons(compact, phantom_flag="D")  # A toggle: one press would leave it "held"
        await rack.start()
        await compact.start()
        running = engine.Engine([RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port}, write_gain=True)
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: "phantom power is off until it is a push button" in caplog.text)
            compact.push("$ctl.gpio.3.gpstate", 1)
            await wait_for(lambda: running.gain_mode["compact-mac"])
            compact.push("$ctl.gpio.4.gpstate", 1)
            await asyncio.sleep(0.6)
            assert rack.values["io.in.A.8.vph"] == 0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())


def test_changing_the_phantom_button_to_a_toggle_while_running_is_noticed(network, quick_flashes, caplog):
    caplog.set_level("INFO", logger="headhunter.engine")
    foh = Pairing(**{**COMPACT.__dict__, "gain_gpio": 3, "phantom_gpio": 4})

    async def scenario():
        rack, compact = stage_box_setup(foh)
        with_buttons(compact)  # A push button, to start with
        await rack.start()
        await compact.start()
        running = engine.Engine([RACK, foh], ports={"rack-mac": rack.port, "compact-mac": compact.port}, write_gain=True)
        task = asyncio.create_task(running.run())
        try:
            await wait_for(lambda: running._phantom_usable.get("compact-mac") is True)
            compact.push_flag("$ctl.user.U2.2.bu", "D")  # Someone makes it a toggle on the console
            await wait_for(lambda: "phantom power is off until it is a push button" in caplog.text)
            compact.push("$ctl.gpio.3.gpstate", 1)
            await wait_for(lambda: running.gain_mode["compact-mac"])
            compact.push("$ctl.gpio.4.gpstate", 1)
            await asyncio.sleep(0.6)
            assert rack.values["io.in.A.8.vph"] == 0

            compact.push("$ctl.gpio.4.gpstate", 0)
            compact.push_flag("$ctl.user.U2.2.bu", "D-P")  # And back
            await wait_for(lambda: "phantom power is back on" in caplog.text)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rack.stop()
            await compact.stop()

    asyncio.run(scenario())
