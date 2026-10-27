"""Talk to a Wing console using Behringer's native binary protocol (TCP port 2222).

Reference: Patrick-Gilles Maillot, "WING Remote Protocols" (Behringer), chapter
"WING native / binary data interface", and the java-wapi project's implementation.

The pieces, from the bottom up:

Framing. The TCP stream is split into numbered channels. 0xDF is an escape byte:
0xDF followed by 0xD0..0xDD switches channel, and 0xDF 0xDE stands for a literal
0xDF. Parameters live on channel 1, the audio engine.

Tokens. On channel 1, data is a stream of tokens. The ones we care about:
    0xD7 <hash> <value>   a parameter value (an event, or the reply to a request)
    0xD7 <hash> 0xDC      request a parameter's value
    0xD7 <hash> 0xDD      request a parameter's definition (name, type, range)
    0xDE                  end of a reply
    0xDF <len> ...        a definition

Hashes. Every parameter is addressed by a 32-bit hash of its dotted path, such as
"ch.1.mute" or "io.in.LCL.1.g"; see wing_hash().

Nothing in this module writes to a console unless set() is called explicitly.
"""

import asyncio
import contextlib
import logging
import struct
from collections.abc import AsyncIterator
from dataclasses import dataclass

log = logging.getLogger(__name__)

NATIVE_PORT = 2222
AUDIO_ENGINE_CHANNEL = 1
KEEPALIVE_SECONDS = 6.0  # The console drops idle connections after 10 seconds
KEEPALIVE_PATH = "$syscfg.consolename"

ESCAPE = 0xDF
CHANNEL_BASE = 0xD0
NUM_CHANNELS = 14

TOKEN_EMPTY_STRING = 0xD0
TOKEN_STRING_256 = 0xD1
TOKEN_NODE_INDEX_65536 = 0xD2
TOKEN_INT16 = 0xD3
TOKEN_INT32 = 0xD4
TOKEN_FLOAT32 = 0xD5
TOKEN_RAW_FLOAT32 = 0xD6
TOKEN_HASH = 0xD7
TOKEN_CLICK = 0xD8
TOKEN_STEP = 0xD9
TOKEN_ROOT = 0xDA
TOKEN_PARENT = 0xDB
TOKEN_DATA_REQUEST = 0xDC
TOKEN_DEFINITION_REQUEST = 0xDD
TOKEN_END = 0xDE
TOKEN_DEFINITION = 0xDF

Value = int | float | str | None


# --- Hashing -------------------------------------------------------------------


def _rotl(value: int, bits: int) -> int:
    return ((value << bits) | (value >> (32 - bits))) & 0xFFFFFFFF


def _mix(hash_: int, block: int) -> int:
    block = (block * 0xCC9E2D51) & 0xFFFFFFFF
    block = _rotl(block, 15)
    block = (block * 0x1B873593) & 0xFFFFFFFF
    hash_ ^= block
    return (_rotl(hash_, 13) * 5 + 0xE6546B64) & 0xFFFFFFFF


def wing_hash(path: str) -> int:
    """The native address of a parameter, from its dotted path.

    Lowercase ASCII letters are folded to uppercase, so the hash is not
    case-sensitive. Checked against hashes published in the protocol document,
    e.g. "ch.1.mute" is 0xF50F69F8.
    """
    if not path:
        return 0
    data = [ord(c) - 32 if "a" <= c <= "z" else ord(c) for c in path]
    hash_ = 0xCB21C6BE
    offset = 0
    while len(data) - offset >= 4:
        a, b, c, d = data[offset: offset + 4]
        hash_ = _mix(hash_, (a << 24) | (b << 16) | (c << 8) | d)
        offset += 4
    if offset < len(data):
        tail = 0
        for byte in data[offset:]:
            tail = (tail << 8) | byte
        hash_ = _mix(hash_, tail)
    return hash_ ^ len(path)


# --- Framing -------------------------------------------------------------------


def encode_frame(channel: int, payload: bytes) -> bytes:
    """Select a channel and escape a payload for sending."""
    out = bytearray([ESCAPE, CHANNEL_BASE + channel])
    escaped = False
    for byte in payload:
        if escaped and CHANNEL_BASE <= byte <= ESCAPE - 1:
            out.append(ESCAPE - 1)
        out.append(byte)
        escaped = byte == ESCAPE
    if escaped:
        out.append(ESCAPE - 1)
    return bytes(out)


class FrameDecoder:
    """Undo channel framing on a byte stream, keeping state between chunks."""

    def __init__(self) -> None:
        self.channel = -1
        self._escape_pending = False

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        """Return (channel, payload) pieces decoded from a chunk of the stream."""
        pieces: list[tuple[int, bytes]] = []
        payload = bytearray()

        def flush() -> None:
            if payload and self.channel >= 0:
                pieces.append((self.channel, bytes(payload)))
            payload.clear()

        for byte in data:
            if self._escape_pending:
                self._escape_pending = False
                if byte == ESCAPE - 1:
                    payload.append(ESCAPE)
                    continue
                if CHANNEL_BASE <= byte < CHANNEL_BASE + NUM_CHANNELS:
                    flush()
                    self.channel = byte - CHANNEL_BASE
                    continue
                payload.append(ESCAPE)
            if byte == ESCAPE:
                self._escape_pending = True
            else:
                payload.append(byte)
        flush()
        return pieces


# --- Values --------------------------------------------------------------------


def encode_value(value: Value) -> bytes:
    """Encode a value to send to the console."""
    if value is None or value == "":
        return bytes([TOKEN_EMPTY_STRING])
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int):
        if 0 <= value <= 0x3F:
            return bytes([value])
        if -0x8000 <= value <= 0x7FFF:
            return bytes([TOKEN_INT16]) + struct.pack(">h", value)
        return bytes([TOKEN_INT32]) + struct.pack(">i", value)
    if isinstance(value, float):
        return bytes([TOKEN_FLOAT32]) + struct.pack(">f", value)
    if isinstance(value, str):
        raw = value.encode("utf-8")
        if len(raw) <= 64:
            return bytes([0x80 + len(raw) - 1]) + raw
        if len(raw) <= 256:
            return bytes([TOKEN_STRING_256, len(raw) - 1]) + raw
        raise ValueError("Strings longer than 256 bytes are not supported")
    raise TypeError(f"Cannot encode {type(value).__name__}")


def _value_length(data: bytes, offset: int) -> int:
    """Length of the value token at offset, or -1 if it is not complete yet."""
    token = data[offset]
    if token <= 0x7F or token == TOKEN_EMPTY_STRING:
        return 1
    if token <= 0xBF:
        return token - 0x80 + 2
    if 0xC0 <= token <= 0xCF:
        return token - 0xC0 + 2
    if token == TOKEN_STRING_256:
        return -1 if len(data) - offset < 2 else data[offset + 1] + 3
    if token in (TOKEN_NODE_INDEX_65536, TOKEN_INT16):
        return 3
    if token in (TOKEN_INT32, TOKEN_FLOAT32, TOKEN_RAW_FLOAT32, TOKEN_HASH):
        return 5
    if token in (TOKEN_CLICK, TOKEN_ROOT, TOKEN_PARENT, TOKEN_DATA_REQUEST,
                 TOKEN_DEFINITION_REQUEST, TOKEN_END):
        return 1
    if token == TOKEN_STEP:
        return 2
    raise ValueError(f"Unknown token 0x{token:02X}")


def _parse_value(data: bytes, offset: int) -> Value:
    token = data[offset]
    if token <= 0x3F:
        return token
    if token <= 0x7F:
        return token - 0x40 + 1  # Node index
    if token <= 0xBF:
        return data[offset + 1: offset + token - 0x80 + 2].decode("utf-8",
                                                                  "replace")
    if 0xC0 <= token <= 0xCF:
        return data[offset + 1: offset + token - 0xC0 + 2].decode("utf-8",
                                                                  "replace")
    if token == TOKEN_EMPTY_STRING:
        return ""
    if token == TOKEN_STRING_256:
        length = data[offset + 1] + 1
        return data[offset + 2: offset + 2 + length].decode("utf-8", "replace")
    if token == TOKEN_NODE_INDEX_65536:
        return struct.unpack_from(">H", data, offset + 1)[0] + 1
    if token == TOKEN_INT16:
        return struct.unpack_from(">h", data, offset + 1)[0]
    if token == TOKEN_INT32:
        return struct.unpack_from(">i", data, offset + 1)[0]
    if token in (TOKEN_FLOAT32, TOKEN_RAW_FLOAT32):
        return struct.unpack_from(">f", data, offset + 1)[0]
    return None


# --- Records -------------------------------------------------------------------


@dataclass(frozen=True)
class ValueRecord:
    """A parameter value: an event, or the reply to a request."""

    hash: int
    value: Value


TYPE_NAMES = {
    0: "node",
    1: "linear float",
    2: "log float",
    3: "fader level",
    4: "integer",
    5: "string enum",
    6: "float enum",
    7: "string",
}
UNIT_NAMES = {0: "", 1: "dB", 2: "%", 3: "ms", 4: "Hz", 5: "m", 6: "s",
              7: "oct"}


@dataclass(frozen=True)
class DefinitionRecord:
    """A parameter or node definition."""

    parent: int
    hash: int
    name: str
    long_name: str
    flags: int
    minimum: float | int | None = None
    maximum: float | int | None = None
    steps: int | None = None
    items: tuple[str, ...] = ()
    max_length: int | None = None

    @property
    def type(self) -> str:
        return TYPE_NAMES.get((self.flags >> 4) & 0xF, "unknown")

    @property
    def unit(self) -> str:
        return UNIT_NAMES.get(self.flags & 0xF, "")

    @property
    def read_only(self) -> bool:
        return bool(self.flags & 0x200)

    def describe(self) -> str:
        """A one-line summary, like 'linear float -3..45.5 dB, 98 steps'."""
        text = self.type
        if self.minimum is not None:
            text += f" {self.minimum:g}..{self.maximum:g}"
            if self.unit:
                text += f" {self.unit}"
        if self.steps:
            text += f", {self.steps} steps"
        if self.items:
            text += f" [{', '.join(self.items)}]"
        if self.max_length is not None:
            text += f", {self.max_length} chars max"
        if self.read_only:
            text += " (read-only)"
        return text


@dataclass(frozen=True)
class EndRecord:
    """The end of a reply."""


Record = ValueRecord | DefinitionRecord | EndRecord


def _parse_definition(data: bytes, offset: int,
                      length: int) -> DefinitionRecord | None:
    body = data[offset:length]
    try:
        position = 3 if struct.unpack_from(">H", body, 1)[0] else 7
        parent, hash_ = struct.unpack_from(">II", body, position)
        position += 10  # parent, hash, index
        name_length = body[position]
        name = body[position + 1: position + 1 + name_length].decode("utf-8",
                                                                     "replace")
        position += 1 + name_length
        long_length = body[position]
        long_name = body[position + 1: position + 1 + long_length].decode(
            "utf-8", "replace")
        position += 1 + long_length
        flags = struct.unpack_from(">H", body, position)[0]
        position += 2
        kind = (flags >> 4) & 0xF
        details: dict = {}
        if kind in (1, 2):
            minimum, maximum, steps = struct.unpack_from(">ffi", body, position)
            details = {"minimum": minimum, "maximum": maximum, "steps": steps}
        elif kind == 4:
            minimum, maximum = struct.unpack_from(">ii", body, position)
            details = {"minimum": minimum, "maximum": maximum}
        elif kind == 5:
            count = struct.unpack_from(">H", body, position)[0]
            position += 2
            items = []
            for _ in range(count):
                length = body[position]
                items.append(
                    body[position + 1: position + 1 + length].decode("utf-8",
                                                                     "replace"))
                position += 1 + length
                position += 1 + body[position]  # Skip the long item name
            details = {"items": tuple(items)}
        elif kind == 6:
            count = struct.unpack_from(">H", body, position)[0]
            position += 2
            items = []
            for _ in range(count):
                value = struct.unpack_from(">f", body, position)[0]
                position += 4
                items.append(f"{value:g}")
                position += 1 + body[position]
            details = {"items": tuple(items)}
        elif kind == 7:
            details = {
                "max_length": struct.unpack_from(">H", body, position)[0]}
    except (struct.error, IndexError):
        return None
    return DefinitionRecord(parent, hash_, name, long_name, flags, **details)


class RecordDecoder:
    """Turn channel 1 payloads into records, keeping partial data between chunks."""

    def __init__(self) -> None:
        self._buffer = b""

    def feed(self, payload: bytes) -> list[Record]:
        data = self._buffer + payload
        records: list[Record] = []
        offset = 0
        while offset < len(data):
            token = data[offset]
            if token == TOKEN_HASH:
                if len(data) - offset < 6:
                    break
                value_length = _value_length(data, offset + 5)
                if value_length < 0 or len(data) - offset - 5 < value_length:
                    break
                hash_ = struct.unpack_from(">I", data, offset + 1)[0]
                value_token = data[offset + 5]
                if value_token not in (TOKEN_DATA_REQUEST,
                                       TOKEN_DEFINITION_REQUEST):
                    records.append(
                        ValueRecord(hash_, _parse_value(data, offset + 5)))
                offset += 5 + value_length
            elif token == TOKEN_DEFINITION:
                if len(data) - offset < 3:
                    break
                short_length = struct.unpack_from(">H", data, offset + 1)[0]
                if short_length:
                    total = 3 + short_length
                else:
                    if len(data) - offset < 7:
                        break
                    total = 7 + struct.unpack_from(">I", data, offset + 3)[0]
                if len(data) - offset < total:
                    break
                definition = _parse_definition(data, offset, offset + total)
                if definition:
                    records.append(definition)
                offset += total
            elif token == TOKEN_END:
                records.append(EndRecord())
                offset += 1
            else:
                try:
                    length = _value_length(data, offset)
                except ValueError:
                    log.warning("Skipping unknown token 0x%02X", token)
                    length = 1
                if length < 0 or len(data) - offset < length:
                    break
                offset += length
        self._buffer = data[offset:]
        return records


# --- Client --------------------------------------------------------------------


class NativeClient:
    """A connection to one console over the native protocol.

    Values the console pushes on its own (events) are delivered by events().

    The console never reports a connection's own changes back to it, but does
    report them to every other connection. Headhunter should therefore use a
    single connection per console, and keep track of what it has written itself.
    """

    def __init__(self, host: str, port: int = NATIVE_PORT) -> None:
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._tasks: list[asyncio.Task] = []
        self._events: asyncio.Queue[ValueRecord] = asyncio.Queue()
        self._pending_values: dict[int, list[asyncio.Future[Value]]] = {}
        self._pending_definitions: dict[
            int, list[asyncio.Future[DefinitionRecord]]] = {}
        self._child_requests: dict[
            int, tuple[list[DefinitionRecord], asyncio.Future]] = {}

    async def connect(self, timeout: float = 3.0) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout
        )
        self._tasks = [
            asyncio.create_task(self._read_loop()),
            asyncio.create_task(self._keepalive_loop()),
        ]
        # The console only starts reporting changes to a connection once that
        # connection has sent it something, so greet it straight away
        await self.get(KEEPALIVE_PATH, timeout)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._writer:
            self._writer.close()
            with contextlib.suppress(OSError):
                await self._writer.wait_closed()

    async def __aenter__(self) -> "NativeClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _send(self, payload: bytes) -> None:
        assert self._writer is not None, "Not connected"
        self._writer.write(encode_frame(AUDIO_ENGINE_CHANNEL, payload))

    async def get(self, path: str, timeout: float = 2.0) -> Value:
        """Read a parameter's current value."""
        hash_ = wing_hash(path)
        future: asyncio.Future[
            Value] = asyncio.get_running_loop().create_future()
        self._pending_values.setdefault(hash_, []).append(future)
        self._send(bytes([TOKEN_HASH]) + struct.pack(">I", hash_) + bytes(
            [TOKEN_DATA_REQUEST]))
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            with contextlib.suppress(ValueError):
                self._pending_values.get(hash_, []).remove(future)

    async def define(self, hash_: int,
                     timeout: float = 2.0) -> DefinitionRecord:
        """Read a parameter's definition (name, long name, type) by hash."""
        future: asyncio.Future[
            DefinitionRecord] = asyncio.get_running_loop().create_future()
        self._pending_definitions.setdefault(hash_, []).append(future)
        self._send(bytes([TOKEN_HASH]) + struct.pack(">I", hash_) + bytes(
            [TOKEN_DEFINITION_REQUEST]))
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            with contextlib.suppress(ValueError):
                self._pending_definitions.get(hash_, []).remove(future)

    async def children(self, path: str, timeout: float = 3.0) -> list[
        DefinitionRecord]:
        """Read the definitions of every child of a node."""
        hash_ = wing_hash(path)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        found: list[DefinitionRecord] = []
        self._child_requests[hash_] = (found, future)
        self._send(bytes([TOKEN_HASH]) + struct.pack(">I", hash_) + bytes(
            [TOKEN_DEFINITION_REQUEST]))
        try:
            await asyncio.wait_for(future, timeout)
        finally:
            self._child_requests.pop(hash_, None)
        return found

    def set(self, path: str, value: Value) -> None:
        """Write a parameter. This changes the console; use with care."""
        self._send(bytes([TOKEN_HASH]) + struct.pack(">I", wing_hash(
            path)) + encode_value(value))

    async def events(self) -> AsyncIterator[ValueRecord]:
        """Values the console sends on its own, as they arrive."""
        while True:
            yield await self._events.get()

    async def _read_loop(self) -> None:
        assert self._reader is not None
        frames = FrameDecoder()
        records = RecordDecoder()
        while True:
            chunk = await self._reader.read(65536)
            if not chunk:
                log.warning("Console %s closed the connection", self.host)
                return
            for channel, payload in frames.feed(chunk):
                if channel != AUDIO_ENGINE_CHANNEL:
                    continue
                for record in records.feed(payload):
                    self._dispatch(record)

    def _dispatch(self, record: Record) -> None:
        if isinstance(record, ValueRecord):
            waiters = self._pending_values.pop(record.hash, [])
            for future in waiters:
                if not future.done():
                    future.set_result(record.value)
            if not waiters:
                self._events.put_nowait(record)
        elif isinstance(record, DefinitionRecord):
            for future in self._pending_definitions.pop(record.hash, []):
                if not future.done():
                    future.set_result(record)
            if record.parent in self._child_requests:
                self._child_requests[record.parent][0].append(record)
        elif isinstance(record, EndRecord):
            # Children requests are made one at a time, so an end of data
            # completes whichever ones have received something
            for found, future in self._child_requests.values():
                if found and not future.done():
                    future.set_result(None)

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_SECONDS)
            try:
                await self.get(KEEPALIVE_PATH)
            except TimeoutError:
                log.warning("Console %s did not answer the keep-alive",
                            self.host)