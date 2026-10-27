"""Resource monitoring."""

import asyncio
import logging
from types import SimpleNamespace

from headhunter import stats


def test_stats_are_logged(caplog):
    caplog.set_level(logging.INFO, logger="headhunter.stats")
    engine = SimpleNamespace(links={"a": SimpleNamespace(state={"x": 1, "y": 2})}, _work=asyncio.Queue())

    async def scenario():
        task = asyncio.create_task(stats.report(engine, minutes=0.001))
        await asyncio.sleep(0.2)
        task.cancel()

    asyncio.run(scenario())
    assert "stats: memory" in caplog.text
    assert "mirrors 2 parameters" in caplog.text
