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

    def __init__(self, values: dict, ranges: dict | None = None) -> None:
        """ranges maps parameter paths to (minimum, maximum, steps), for definition requests."""
        self.values = {"$syscfg.consolename": "FakeWing", **values}
        self.ranges = ranges or {}
        self.buttons: set[str] = set()  # Custom control buttons, by path, e.g. $ctl.user.U2.1.bu
        self.flags: dict[str, str] = {}  # Each GPIO button's flag, e.g. "C" or "D-P", by button path
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

    def _definition(self, hash_: int, path: str) -> bytes:
        """A linear float definition in dB, followed by the end of the reply."""
        minimum, maximum, steps = self.ranges[path]
        name = path.rsplit(".", 1)[1].encode()
        body = (
            struct.pack(">IIH", 0, hash_, 0)
            + bytes([len(name)]) + name + bytes([4]) + b"GAIN"
            + struct.pack(">H", 0x11)
            + struct.pack(">ffi", minimum, maximum, steps)
        )
        return b"\xdf" + struct.pack(">H", len(body)) + body + bytes([TOKEN_END])

    # --- Custom control buttons, which have a gpio setting only while their function is GPIO, at
    # an address the console makes up rather than one calculated from its name

    def add_button(self, base: str, mode: str = "OFF", name: str = "", flag: str | None = None) -> None:
        self.buttons.add(base)
        for leaf, value in (("mode", mode), ("name", name)):
            self.values[f"{base}.{leaf}"] = value
            self._paths[wing_hash(f"{base}.{leaf}")] = f"{base}.{leaf}"
        if flag is not None:
            self.flags[base] = flag

    def push_flag(self, base: str, value: str) -> None:
        """Change a button's GPIO flag as if someone changed it on the console, and report it."""
        self.flags[base] = value
        payload = bytes([TOKEN_HASH]) + struct.pack(">I", self.flag_hash(base)) + encode_value(value)
        for writer in self._writers:
            writer.write(encode_frame(AUDIO_ENGINE_CHANNEL, payload))

    @staticmethod
    def flag_hash(base: str) -> int:
        return wing_hash(f"{base}.gpio") ^ 0x5A5A5A5A

    def _button_for_flag(self, hash_: int) -> str | None:
        for base in self.buttons:
            if self.values.get(f"{base}.mode") == "GPIO" and self.flag_hash(base) == hash_:
                return base
        return None

    def _children(self, base: str) -> bytes:
        parent = wing_hash(base)
        records = [
            self._child(parent, wing_hash(f"{base}.mode"), "mode", "MODE", 0x50, items=("OFF", "MUTE", "GPIO")),
            self._child(parent, wing_hash(f"{base}.name"), "name", "NAME", 0x70, max_length=16),
        ]
        if self.values.get(f"{base}.mode") == "GPIO":
            flags = ("A", "B", "C", "D", "A-P", "B-P", "C-P", "D-P")
            records.append(self._child(parent, self.flag_hash(base), "gpio", "GPIO FLAG", 0x50, items=flags))
        return b"".join(records) + bytes([TOKEN_END])

    @staticmethod
    def _child(parent: int, hash_: int, name: str, long_name: str, flags: int, items=(), max_length=0) -> bytes:
        body = struct.pack(">IIH", parent, hash_, 0)
        body += bytes([len(name)]) + name.encode() + bytes([len(long_name)]) + long_name.encode()
        body += struct.pack(">H", flags)
        if flags >> 4 == 5:
            body += struct.pack(">H", len(items))
            for item in items:
                body += bytes([len(item)]) + item.encode() + bytes([0])
        elif flags >> 4 == 7:
            body += struct.pack(">H", max_length)
        return b"\xdf" + struct.pack(">H", len(body)) + body

    def _process(self, data: bytes, writer: asyncio.StreamWriter) -> None:
        offset = 0
        while offset < len(data):
            if data[offset] != TOKEN_HASH:
                offset += 1
                continue
            hash_ = struct.unpack_from(">I", data, offset + 1)[0]
            token = data[offset + 5]
            if hash_ not in self._paths:  # Values may have been added since the console was created
                self._paths = {wing_hash(path): path for path in self.values}
            path = self._paths.get(hash_)
            flag_button = self._button_for_flag(hash_) if path is None else None
            if token == TOKEN_DATA_REQUEST and flag_button is not None:
                reply = bytes([TOKEN_HASH]) + struct.pack(">I", hash_) + encode_value(self.flags.get(flag_button, "A")) + bytes([TOKEN_END])
                writer.write(encode_frame(AUDIO_ENGINE_CHANNEL, reply))
                offset += 6
                continue
            if token == TOKEN_DATA_REQUEST:
                if path is not None:
                    reply = bytes([TOKEN_HASH]) + struct.pack(">I", hash_) + encode_value(self.values[path]) + bytes([TOKEN_END])
                    writer.write(encode_frame(AUDIO_ENGINE_CHANNEL, reply))
                offset += 6
            elif token == TOKEN_DEFINITION_REQUEST:
                button = next((b for b in self.buttons if wing_hash(b) == hash_), None)
                if button is not None:
                    writer.write(encode_frame(AUDIO_ENGINE_CHANNEL, self._children(button)))
                elif path in self.ranges:
                    writer.write(encode_frame(AUDIO_ENGINE_CHANNEL, self._definition(hash_, path)))
                offset += 6
            else:
                length = _value_length(data, offset + 5)
                value = _parse_value(data, offset + 5)
                if flag_button is not None:
                    self.flags[flag_button] = value
                    self.writes.append((f"{flag_button}.gpio", value))
                if path is not None:
                    self.values[path] = value
                    self.writes.append((path, value))
                offset += 5 + length
