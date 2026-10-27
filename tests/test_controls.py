"""Custom control buttons, against a fake console that behaves like the real one: a button's
GPIO flag only exists while its function is GPIO, at an address not calculated from its name."""

import asyncio

import pytest

from fakewing import FakeWing
from headhunter import controls
from headhunter.native import NativeClient
from headhunter.store import FOH, Pairing

COMPACT = Pairing("compact-mac", "My-WingCCT", "wing-compact", "S2", "compact-dante", "WingCompact", "COMPACT", FOH)
GAIN = controls.Button("U2", 1, "bu")
PHANTOM = controls.Button("U2", 2, "bu")
SPARE = controls.Button("U2", 3, "bu")


def console():
    """Ben's Compact: page U2 has GPIO C (toggle) and GPIO D (push); everything else is blank."""
    wing = FakeWing({})
    for button in controls.all_buttons(COMPACT):
        wing.add_button(button.path)
    wing.add_button(GAIN.path, "GPIO", "", "C")
    wing.add_button(PHANTOM.path, "GPIO", "", "D-P")
    return wing


async def connected(wing):
    await wing.start()
    client = NativeClient("127.0.0.1", wing.port)
    await client.connect()
    return client


def test_pages_by_model():
    assert controls.pages(COMPACT)[:4] == ["U1", "U2", "U3", "U4"]
    assert len(controls.all_buttons(COMPACT)) == 9 * 4  # One row of buttons on the Compact
    assert all(button.row == "bu" for button in controls.all_buttons(COMPACT))
    assert GAIN.label == "page U2, button 1"


@pytest.mark.parametrize("value, parsed", [("C", (3, False)), ("D-P", (4, True)), ("2S", None), ("E", None), (None, None)])
def test_flags(value, parsed):
    assert controls.parse_flag(value) == parsed


def test_reading_a_button():
    async def scenario():
        wing = console()
        client = await connected(wing)
        try:
            assert (await controls.read(client, PHANTOM)).gpio == (4, True)
            assert (await controls.read(client, SPARE)) == controls.Setup("OFF", "", None)
        finally:
            await client.close()
            await wing.stop()

    asyncio.run(scenario())


def test_assigning_a_button():
    async def scenario():
        wing = console()
        client = await connected(wing)
        try:
            result = await controls.assign(client, SPARE, gpio=2, push=True, name="|HH TEST")
            assert (result.mode, result.flag, result.name) == ("GPIO", "B-P", "|HH TEST")
            assert wing.flags[SPARE.path] == "B-P"
            # The function is set first: the flag cannot be written before it exists
            order = [path for path, _ in wing.writes]
            assert order.index(f"{SPARE.path}.mode") < order.index(f"{SPARE.path}.gpio")
        finally:
            await client.close()
            await wing.stop()

    asyncio.run(scenario())


def test_finding_the_buttons_on_a_gpio():
    async def scenario():
        wing = console()
        client = await connected(wing)
        try:
            assert await controls.find(client, COMPACT, 4) == [(PHANTOM, True)]
            assert await controls.find(client, COMPACT, 3) == [(GAIN, False)]
            assert await controls.find(client, COMPACT, 1) == []
        finally:
            await client.close()
            await wing.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "gain_mode, phantoms, label",
    [
        (False, [True], "|HH DISABLED"),
        (True, None, "|HH NO 48V"),
        (True, [False], "|HH 48V ON"),
        (True, [True], "|HH 48V OFF"),
        (True, [True, False], "|HH 48V ON"),  # A stereo channel with 48V on one side: pressing turns it on
        (True, [True, True], "|HH 48V OFF"),
    ],
)
def test_phantom_labels(gain_mode, phantoms, label):
    assert controls.phantom_label(gain_mode, phantoms) == label


def test_gain_labels():
    assert (controls.gain_label(True), controls.gain_label(False)) == ("|HH GAIN ON", "|HH GAIN OFF")
