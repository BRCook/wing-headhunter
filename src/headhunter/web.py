"""The web page, served by headhunter run.

The page loads once, then a WebSocket keeps it up to date: the status whenever
it changes, and every log line as it happens. Everything is served from here,
so the page works on a show network with no internet.
"""

import asyncio
import collections
import logging
from importlib import resources

from aiohttp import WSCloseCode, WSMsgType, web

log = logging.getLogger(__name__)

STATUS_CHECK_SECONDS = 0.25
LOG_LINES_KEPT = 300


class LogBuffer(logging.Handler):
    """Keeps recent log lines, and passes new ones to every open page."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: collections.deque[dict] = collections.deque(maxlen=LOG_LINES_KEPT)
        self.listeners: set[asyncio.Queue] = set()
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        line = {
            "time": self.formatter.formatTime(record, "%H:%M:%S") if self.formatter else "",
            "level": record.levelname.lower(),
            "message": self.format(record).splitlines()[0],
        }
        self.lines.append(line)
        for queue in self.listeners:
            queue.put_nowait(line)


class WebServer:
    def __init__(self, engine, port: int, host: str = "0.0.0.0", logs: LogBuffer | None = None) -> None:
        self.engine = engine
        self.port = port
        self.host = host
        self.logs = logs or LogBuffer()
        self._runner: web.AppRunner | None = None
        self._sockets: set[web.WebSocketResponse] = set()  # Open pages

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._page)
        app.router.add_get("/api/status", self._status)
        app.router.add_get("/ws", self._websocket)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        if self.port == 0:  # Chosen by the system, as in tests
            self.port = site._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        # Open pages never close on their own, so close them; otherwise stopping waits for them
        for socket in list(self._sockets):
            await socket.close(code=WSCloseCode.GOING_AWAY, message=b"Headhunter stopped")
        if self._runner is not None:
            await self._runner.cleanup()

    async def _page(self, request: web.Request) -> web.Response:
        html = resources.files("headhunter").joinpath("web/index.html").read_text()
        return web.Response(text=html, content_type="text/html")

    async def _status(self, request: web.Request) -> web.Response:
        return web.json_response(self.engine.status())

    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse(heartbeat=20)
        await socket.prepare(request)
        self._sockets.add(socket)
        queue: asyncio.Queue = asyncio.Queue()
        self.logs.listeners.add(queue)
        try:
            await socket.send_json({"type": "log", "lines": list(self.logs.lines)})
            last = None
            reader = asyncio.create_task(self._drain(socket))
            while not socket.closed and not reader.done():
                status = self.engine.status()
                if status != last:
                    await socket.send_json({"type": "status", "status": status})
                    last = status
                lines = []
                try:
                    lines.append(await asyncio.wait_for(queue.get(), STATUS_CHECK_SECONDS))
                    while not queue.empty():
                        lines.append(queue.get_nowait())
                except TimeoutError:
                    pass
                if lines:
                    await socket.send_json({"type": "log", "lines": lines})
            reader.cancel()
        except (ConnectionResetError, RuntimeError):
            pass  # The page was closed
        finally:
            self.logs.listeners.discard(queue)
            self._sockets.discard(socket)
        return socket

    @staticmethod
    async def _drain(socket: web.WebSocketResponse) -> None:
        """Read (and ignore) anything the page sends, so a closed page is noticed."""
        async for message in socket:
            if message.type == WSMsgType.ERROR:
                break
