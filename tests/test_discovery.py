"""Parsing what consoles and Dante cards report about themselves."""

import pytest

from headhunter.discovery import decode_dante_model, mac_from_dante_id, parse_reply


def test_a_discovery_reply():
    console = parse_reply(b"WING,10.0.0.2,My-WingRCK,wing-rack,S1,3.1.1-0-g054c7f4b:release", "10.0.0.2")
    assert (console.name, console.model, console.serial, console.firmware) == (
        "My-WingRCK", "wing-rack", "S1", "3.1.1-0-g054c7f4b:release",
    )


def test_a_console_name_with_commas():
    assert parse_reply(b"WING,10.0.0.2,Stage, left, rack,wing-rack,S1,3.1.1", "10.0.0.2").name == "Stage, left, rack"


def test_the_address_the_reply_came_from_wins():
    assert parse_reply(b"WING,192.168.1.99,N,wing-rack,S1,3.1.1", "10.0.0.2").ip == "10.0.0.2"


@pytest.mark.parametrize("reply", [b"", b"WING?", b"NOTWING,1,2,3,4,5", b"WING,too,short", b"\xff\xfe"])
def test_anything_else_is_ignored(reply):
    assert parse_reply(reply, "10.0.0.2") is None


@pytest.mark.parametrize(
    "dante_id, mac",
    [
        (b"001dc1fffe9f77fa", "00:1d:c1:9f:77:fa"),
        (b"001DC1FFFE8D5D66", "00:1d:c1:8d:5d:66"),
        (b"001dc1aaaa9f77fa", None),  # No fffe in the middle
        (b"001dc1fffe9f77", None),  # Too short
        (b"zzzzzzfffezzzzzz", None),  # Not hexadecimal
        (None, None),
    ],
)
def test_macs_from_dante_ids(dante_id, mac):
    assert mac_from_dante_id(dante_id) == mac


@pytest.mark.parametrize(
    "raw, model",
    [(b"_77696e6736342042", "wing64 B"), (b"DAI2", "DAI2"), (b"_nothex", "_nothex"), (None, None)],
)
def test_dante_models(raw, model):
    assert decode_dante_model(raw) == model
