"""Resource monitoring, for spotting leaks in long runs.

Every few minutes, logs what the process is using: memory as the operating
system sees it, Python's own heap, and the internal things that would grow if
something were leaking (tasks, queued work, mirrors). If the heap has grown
since startup, the lines of code that grew the most are logged too.
"""

import asyncio
import logging
import resource
import tracemalloc
from pathlib import Path

from headhunter import dante

log = logging.getLogger(__name__)

GROWTH_WORTH_REPORTING = 100 * 1024  # Bytes
TOP_GROWTH = 3


def _process_memory() -> tuple[float, float]:
    """Resident memory and its peak, in MB, from /proc (Linux only)."""
    current = peak = 0.0
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                current = int(line.split()[1]) / 1024
            elif line.startswith("VmHWM:"):
                peak = int(line.split()[1]) / 1024
    except OSError:
        pass
    return current, peak


async def report(engine, minutes: float) -> None:
    """Log resource use every `minutes` until cancelled. Starts tracing Python memory allocations."""
    tracemalloc.start()
    baseline = tracemalloc.take_snapshot()
    while True:
        await asyncio.sleep(minutes * 60)
        current, peak = _process_memory()
        heap, _ = tracemalloc.get_traced_memory()
        children = resource.getrusage(resource.RUSAGE_CHILDREN)
        mirrors = "+".join(str(len(link.state)) for link in engine.links.values())
        log.info(
            "stats: memory %.1f MB (peak %.1f), Python heap %.1f MB, %d tasks, %d queued, "
            "mirrors %s parameters, netaudio %d runs (%.0f s CPU)",
            current, peak, heap / 1024 / 1024, len(asyncio.all_tasks()), engine._work.qsize(),
            mirrors, dante.RUNS, children.ru_utime + children.ru_stime,
        )
        growth = tracemalloc.take_snapshot().compare_to(baseline, "lineno")
        for stat in growth[:TOP_GROWTH]:
            if stat.size_diff >= GROWTH_WORTH_REPORTING:
                frame = stat.traceback[0]
                log.info("stats:   %+.0f KB since startup at %s:%d", stat.size_diff / 1024, frame.filename, frame.lineno)
