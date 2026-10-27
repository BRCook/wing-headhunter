"""A fake Wing console that speaks enough of the native protocol for testing."""

import asyncio
import struct

from headhunter.native import (
    AUDIO_ENGINE_CHANNEL,
    TOKEN_DATA_REQUEST,
    TOKEN_DEFINITION_REQUEST,
    TOKEN_END,
    TOKEN_HASH,
    FrameDecoder,
    _parse_value,
    _value_length,
    encode_frame,
    encode_value,
    wing_hash,
)


class FakeWing:
    """Answers reads, accepts writes, and can push changes and drop connections."""

    def __init__(self, values: dict) -> None:
        self.values = {"$syscfg.consolename": "FakeWing", **values}
        self._paths = {wing_hash(path): path for path in self.values}
        self._writers: list[asyncio.StreamWriter] = []
        self._server: asyncio.Server | None = None
        self.port = 0
        self.writes: list[tuple[str, object]] = []

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        """Drop every connection and stop listening, like a console being switched off."""
        self._server.close()
        for writer in self._writers:
            writer.close()
        self._writers.clear()
        await self._server.wait_closed()

    def push(self, path: str, value) -> None:
        """Change a value as if someone changed it on the console, and report it to every connection."""
        self.values[path] = value
        self._paths[wing_hash(path)] = path
        payload = bytes([TOKEN_HASH]) + struct.pack(">I", wing_hash(path)) + encode_value(value)
        for writer in self._writers:
            writer.write(encode_frame(AUDIO_ENGINE_CHANNEL, payload))

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        frames = FrameDecoder()
        try:
            while chunk := await reader.read(65536):
                for _, payload in frames.feed(chunk):
                    self._process(payload, writer)
        except ConnectionError:
            pass

    def _process(self, data: bytes, writer: asyncio.StreamWriter) -> None:
        offset = 0
        while offset < len(data):
            if data[offset] != TOKEN_HASH:
                offset += 1
                continue
            hash_ = struct.unpack_from(">I", data, offset + 1)[0]
            token = data[offset + 5]
            path = self._paths.get(hash_)
            if token == TOKEN_DATA_REQUEST:
                if path is not None:
                    reply = bytes([TOKEN_HASH]) + struct.pack(">I", hash_) + encode_value(self.values[path]) + bytes([TOKEN_END])
                    writer.write(encode_frame(AUDIO_ENGINE_CHANNEL, reply))
                offset += 6
            elif token == TOKEN_DEFINITION_REQUEST:
                offset += 6
            else:
                length = _value_length(data, offset + 5)
                value = _parse_value(data, offset + 5)
                if path is not None:
                    self.values[path] = value
                    self.writes.append((path, value))
                offset += 5 + length
