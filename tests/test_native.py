"""The native protocol: hashing, framing and values, checked against the protocol document."""

import random
import struct

import pytest

from headhunter.native import (
    DefinitionRecord,
    EndRecord,
    FrameDecoder,
    RecordDecoder,
    ValueRecord,
    encode_frame,
    encode_value,
    wing_hash,
)


@pytest.mark.parametrize(
    "path, expected",
    [
        ("ch.1.mute", 0xF50F69F8),
        ("$ctl.daw.$on", 0xBEEFAEAB),
        ("$ctl.daw", 0xDF17C242),
        ("ch", 0x70B10139),
        ("$stat", 0x97A00439),
    ],
)
def test_hashes_match_the_protocol_document(path, expected):
    assert wing_hash(path) == expected


def test_hash_ignores_case():
    assert wing_hash("io.in.LCL.1.g") == wing_hash("io.in.lcl.1.g")


def test_frames_from_the_protocol_document():
    assert encode_frame(0, bytes.fromhex("d702dfaf0e02")) == bytes.fromhex("dfd0d702dfaf0e02")
    assert encode_frame(0, bytes.fromhex("d702dfd10e02")) == bytes.fromhex("dfd0d702dfded10e02")

    decoder = FrameDecoder()
    decoder.channel = 1
    assert decoder.feed(bytes.fromhex("D7DFDED4ABCF08")) == [(1, bytes.fromhex("D7DFD4ABCF08"))]


def test_framing_round_trips_awkward_payloads_split_anywhere():
    rng = random.Random(1)
    awkward = [0xDF, 0xDE, 0xD0, 0xD1, 0xDD, 0x00, 0xFF]
    for _ in range(2000):
        payload = bytes(rng.choice(awkward + [rng.randrange(256)]) for _ in range(rng.randrange(1, 30)))
        framed = encode_frame(1, payload)
        decoder = FrameDecoder()
        decoded = b""
        position = 0
        while position < len(framed):
            end = position + rng.randrange(1, 5)
            for _, piece in decoder.feed(framed[position:end]):
                decoded += piece
            position = end
        assert decoded == payload


def test_value_reply_from_the_protocol_document():
    assert RecordDecoder().feed(bytes.fromhex("d7beefaeab01de")) == [ValueRecord(0xBEEFAEAB, 1), EndRecord()]


def test_definition_from_the_protocol_document():
    records = RecordDecoder().feed(
        bytes.fromhex("df001fdf17c242beefaeab000003246f6e06444157204f4e00400000000000000001de")
    )
    definition = records[0]
    assert isinstance(definition, DefinitionRecord)
    assert (definition.parent, definition.hash) == (0xDF17C242, 0xBEEFAEAB)
    assert (definition.name, definition.long_name) == ("$on", "DAW ON")
    assert definition.type == "integer"
    assert (definition.minimum, definition.maximum) == (0, 1)


def test_linear_float_definition():
    body = struct.pack(">IIH", 1, 2, 0) + b"\x01g\x04GAIN" + struct.pack(">H", 0x11) + struct.pack(">ffi", -2, 45.5, 20)
    definition = RecordDecoder().feed(b"\xdf" + struct.pack(">H", len(body)) + body)[0]
    assert definition.describe() == "linear float -2..45.5 dB, 20 steps"


@pytest.mark.parametrize("value", [0, 1, 63, 64, -5, 100000, 32.5, "RACK-L24-HH", "", "x" * 100])
def test_values_round_trip(value):
    payload = b"\xd7" + struct.pack(">I", 1) + encode_value(value)
    record = RecordDecoder().feed(payload)[0]
    if isinstance(value, float):
        assert record.value == pytest.approx(value)
    else:
        assert record.value == value


def test_records_split_across_chunks():
    payload = b"".join(
        b"\xd7" + struct.pack(">I", wing_hash(path)) + encode_value(value)
        for path, value in [("io.in.LCL.1.g", 32.5), ("io.in.MOD.1.name", "RACK-L24-HH"), ("ch.1.mute", 1)]
    )
    decoder = RecordDecoder()
    records = []
    for position in range(0, len(payload), 3):
        records += decoder.feed(payload[position : position + 3])
    assert [r.value for r in records] == [32.5, "RACK-L24-HH", 1]


def test_numbered_entries_are_named_by_their_number():
    body = struct.pack(">IIH", 1, 2, 3) + b"\x00" + b"\x00" + struct.pack(">H", 0x00)
    definition = RecordDecoder().feed(b"\xdf" + struct.pack(">H", len(body)) + body)[0]
    assert (definition.name, definition.index, definition.key) == ("", 3, "3")
