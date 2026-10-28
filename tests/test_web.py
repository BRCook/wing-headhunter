"""The status page."""

import asyncio
import logging

import aiohttp

from headhunter.web import LogBuffer, WebServer

STATUS = {
    "consoles": [
        {"name": "COMPACT", "role": "FOH", "connected": True, "ip": "10.0.0.3", "gain_mode": True,
         "selected": {"strip": "CH 13", "preamp": "RACK L1", "gain": 12.5, "phantom": True}},
        {"name": "RACK", "role": "STAGE", "connected": False, "ip": None},
    ],
    "writing": {"names": True, "gain": False},
}


class StubEngine:
    def __init__(self):
        self.current = STATUS

    def status(self):
        return self.current


async def started():
    logs = LogBuffer()
    server = WebServer(StubEngine(), port=0, host="127.0.0.1", logs=logs)
    await server.start()
    return server, logs, f"http://127.0.0.1:{server.port}"


def test_the_page_and_status_are_served():
    async def scenario():
        server, _, url = await started()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url + "/") as response:
                    assert response.status == 200
                    assert "<title>Headhunter</title>" in await response.text()
                async with session.get(url + "/api/status") as response:
                    assert await response.json() == STATUS
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_the_page_is_kept_up_to_date():
    async def scenario():
        server, logs, url = await started()
        logger = logging.getLogger("headhunter.test")
        logger.addHandler(logs)
        logger.setLevel(logging.INFO)
        try:
            logger.info("Before the page opened")
            async with aiohttp.ClientSession() as session, session.ws_connect(url + "/ws") as socket:
                backlog = await socket.receive_json()
                assert backlog["type"] == "log"
                assert backlog["lines"][-1]["message"] == "Before the page opened"
                assert (await socket.receive_json()) == {"type": "status", "status": STATUS}

                logger.warning("Something happened")
                line = await asyncio.wait_for(socket.receive_json(), 2)
                assert line["lines"][0]["message"] == "Something happened"
                assert line["lines"][0]["level"] == "warning"

                server.engine.current = {**STATUS, "writing": {"names": True, "gain": True}}
                update = await asyncio.wait_for(socket.receive_json(), 2)
                assert update["status"]["writing"]["gain"] is True
        finally:
            logger.removeHandler(logs)
            await server.stop()

    asyncio.run(scenario())


def test_stopping_does_not_wait_for_open_pages():
    async def scenario():
        server, _, url = await started()
        async with aiohttp.ClientSession() as session, session.ws_connect(url + "/ws") as socket:
            await socket.receive_json()
            await asyncio.wait_for(server.stop(), 2)
            async def until_closed():
                while (message := await socket.receive()).type not in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                    pass  # Updates queued before the close

            await asyncio.wait_for(until_closed(), 2)

    asyncio.run(scenario())


class SetupEngine(StubEngine):
    def __init__(self):
        super().__init__()
        self.assigned = []

    async def button_setup(self, name):
        if name != "COMPACT":
            raise ValueError(f"No FOH console called {name!r}")
        return {"console": "COMPACT", "buttons": []}

    async def assign_button(self, name, role, button, gpio):
        if role not in ("gain", "phantom"):
            raise ValueError("The role must be gain or phantom")
        self.assigned.append((name, role, button.path, gpio))

    async def clear_button(self, name, button):
        self.assigned.append((name, "clear", button.path, None))


def test_the_setup_endpoints():
    async def scenario():
        engine = SetupEngine()
        server = WebServer(engine, port=0, host="127.0.0.1")
        await server.start()
        url = f"http://127.0.0.1:{server.port}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url + "/api/buttons/COMPACT") as response:
                    assert (await response.json())["console"] == "COMPACT"
                async with session.get(url + "/api/buttons/NOPE") as response:
                    assert response.status == 400 and "NOPE" in (await response.json())["error"]
                button = {"page": "U2", "number": 3, "row": "bu"}
                async with session.post(url + "/api/buttons/COMPACT/assign", json={**button, "role": "phantom", "gpio": "d"}) as response:
                    assert response.status == 200
                async with session.post(url + "/api/buttons/COMPACT/assign", json={**button, "role": "phantom", "gpio": "E"}) as response:
                    assert response.status == 400
                async with session.post(url + "/api/buttons/COMPACT/clear", json=button) as response:
                    assert response.status == 200
            assert engine.assigned == [("COMPACT", "phantom", "$ctl.user.U2.3.bu", 4), ("COMPACT", "clear", "$ctl.user.U2.3.bu", None)]
        finally:
            await server.stop()

    asyncio.run(scenario())
