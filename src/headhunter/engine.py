"""The long-running Headhunter program.

Keeps one connection to every paired console, each with a mirror of the
parameters Headhunter depends on. Mirrors are read in full when a console
connects, then kept current from the console's own change reports. Dante
subscriptions are polled through netaudio. Whenever anything relevant changes,
source names are recalculated; they are only written if asked to.

If a console disappears, its connection is retried until it comes back, at
whatever IP address it turns up at.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable

from headhunter.consoles import locate
from headhunter.dante import NetaudioError, Subscription, read_subscriptions
from headhunter.naming import PlannedName, apply_names, paths_for, plan_from_state
from headhunter.native import NATIVE_PORT, NativeClient, Value, wing_hash
from headhunter.store import Pairing, Store

log = logging.getLogger(__name__)

RETRY_SECONDS = 5.0  # Between attempts to find or reconnect to a console
DANTE_POLL_SECONDS = 5.0
SETTLE_SECONDS = 0.5  # Wait for a burst of changes to finish before recalculating


class ConsoleLink:
    """A self-healing connection to one paired console, with a mirror of its state."""

    def __init__(
        self,
        pairing: Pairing,
        paths: list[str],
        on_change: Callable[["ConsoleLink"], None],
        port: int = NATIVE_PORT,
    ) -> None:
        self.pairing = pairing
        self.port = port
        self.paths = paths
        self.on_change = on_change
        self.state: dict[str, Value] = {}
        self.client: NativeClient | None = None
        self._by_hash = {wing_hash(path): path for path in paths}

    @property
    def name(self) -> str:
        return self.pairing.display_name

    @property
    def connected(self) -> bool:
        return self.client is not None and not self.client.closed.is_set()

    async def run(self) -> None:
        """Connect, mirror, and reconnect whenever the connection drops. Runs until cancelled."""
        while True:
            ip = await self._find()
            client = NativeClient(ip, self.port, label=self.name)
            try:
                await client.connect()
                values = await asyncio.gather(*(client.get(path) for path in self.paths))
            except (OSError, TimeoutError, ConnectionError) as error:
                log.warning("Could not connect to %s at %s: %s", self.name, ip, error or type(error).__name__)
                await client.close()
                await asyncio.sleep(RETRY_SECONDS)
                continue

            self.state = dict(zip(self.paths, values))
            self.client = client
            log.info("Connected to %s at %s", self.name, ip)
            self.on_change(self)
            try:
                async for event in client.events():
                    path = self._by_hash.get(event.hash)
                    if path is not None and self.state.get(path) != event.value:
                        self.state[path] = event.value
                        self.on_change(self)
            finally:
                self.client = None
                await client.close()
            log.warning("Lost connection to %s; retrying", self.name)
            self.on_change(self)
            await asyncio.sleep(RETRY_SECONDS)

    async def _find(self) -> str:
        """Discover the console's current IP, retrying until it answers."""
        while True:
            ips = await locate([self.pairing])
            if self.pairing.console_mac in ips:
                return ips[self.pairing.console_mac]
            log.warning("%s did not answer discovery; retrying", self.name)
            await asyncio.sleep(RETRY_SECONDS)


class Engine:
    """Everything Headhunter does while running."""

    def __init__(
        self,
        pairings: list[Pairing],
        write_names: bool = False,
        ports: dict[str, int] | None = None,
    ) -> None:
        self.pairings = pairings
        self.write_names = write_names
        ports = ports or {}
        self.links = {
            p.console_mac: ConsoleLink(p, paths_for(p), self._changed, ports.get(p.console_mac, NATIVE_PORT))
            for p in pairings
        }
        self.subscriptions: list[Subscription] = []
        self._dirty = asyncio.Event()
        self._reported: dict[tuple[str, int], tuple[str, str, tuple[str, ...]]] = {}
        self._summaries: dict[str, str] = {}

    def _changed(self, link: ConsoleLink | None = None) -> None:
        self._dirty.set()

    async def run(self) -> None:
        if not any(p.is_foh for p in self.pairings):
            log.warning("No console is marked FOH, so no sources will be named")
        tasks = [asyncio.create_task(link.run(), name=f"link {link.name}") for link in self.links.values()]
        tasks += [
            asyncio.create_task(self._poll_dante(), name="dante"),
            asyncio.create_task(self._naming(), name="naming"),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _poll_dante(self) -> None:
        while True:
            try:
                subscriptions = await read_subscriptions(self.pairings)
            except NetaudioError as error:
                log.warning("Could not read Dante subscriptions: %s", error)
            else:
                if subscriptions != self.subscriptions:
                    if self.subscriptions:
                        log.info("Dante subscriptions changed")
                    self.subscriptions = subscriptions
                    self._changed()
            await asyncio.sleep(DANTE_POLL_SECONDS)

    async def _naming(self) -> None:
        while True:
            await self._dirty.wait()
            await asyncio.sleep(SETTLE_SECONDS)
            self._dirty.clear()
            await self._update_names()

    def plans(self) -> list[PlannedName]:
        """The names Headhunter wants right now, from connected consoles only."""
        states = {mac: link.state for mac, link in self.links.items() if link.connected}
        return plan_from_state(self.pairings, self.subscriptions, states)

    async def _update_names(self) -> None:
        plans = self.plans()
        self._report(plans)
        if not self.write_names:
            return
        to_write = [p for p in plans if p.needs_writing]
        if not to_write:
            return
        clients = {mac: link.client for mac, link in self.links.items() if link.client is not None}
        try:
            failed = await apply_names(to_write, clients)
        except (ConnectionError, TimeoutError) as error:
            log.warning("Could not write names: %s", error or type(error).__name__)
            return
        for plan in to_write:
            if plan not in failed:
                # The console never reports our own writes back, so update the mirror ourselves
                self.links[plan.console.console_mac].state[f"io.in.MOD.{plan.dante_input}.name"] = plan.planned
                log.info("%s MOD %d renamed %r -> %r", plan.console.display_name, plan.dante_input, plan.current, plan.planned)
            else:
                log.warning("%s MOD %d did not read back as %r", plan.console.display_name, plan.dante_input, plan.planned)
        self._changed()  # Recalculate, so the summary reflects what is now on the console

    def _waiting(self, receiver: Pairing) -> dict[str, int]:
        """Dante inputs on a console fed by paired consoles that are not connected, by sender."""
        by_dante_name = {p.dante_name: p for p in self.pairings}
        waiting: dict[str, int] = {}
        for sub in self.subscriptions:
            if sub.rx_device != receiver.dante_name or not sub.connected:
                continue
            sender = by_dante_name.get(sub.tx_device)
            if sender is not None and sender is not receiver and not self.links[sender.console_mac].connected:
                waiting[sender.display_name] = waiting.get(sender.display_name, 0) + 1
        return waiting

    def _report(self, plans: list[PlannedName]) -> None:
        """Log what each Dante input should be called, but only when that changes."""
        for pairing in self.pairings:
            if not pairing.is_foh or not self.links[pairing.console_mac].connected:
                continue
            mine = [p for p in plans if p.console is pairing]
            summary = (
                f"{pairing.display_name}: {len(mine) + sum(self._waiting(pairing).values())} Dante inputs "
                f"from paired consoles; {sum(not p.linked and p.current == p.planned for p in mine)} named "
                f"correctly, {sum(p.needs_writing for p in mine)} to rename, {sum(bool(p.linked) for p in mine)} skipped"
            )
            for sender, count in sorted(self._waiting(pairing).items()):
                summary += f", {count} waiting for {sender}"
            if self._summaries.get(pairing.console_mac) != summary:
                self._summaries[pairing.console_mac] = summary
                log.info("%s", summary)
        for plan in plans:
            key = (plan.console.console_mac, plan.dante_input)
            summary = (plan.current, plan.planned, plan.linked)
            if self._reported.get(key) == summary:
                continue
            self._reported[key] = summary
            where = f"{plan.console.display_name} MOD {plan.dante_input}"
            if plan.linked:
                log.info("%s: would be %r, but customization is linked on %s", where, plan.planned, ", ".join(plan.linked))
            elif plan.current != plan.planned and not self.write_names:
                log.info("%s: would rename %r -> %r", where, plan.current, plan.planned)


async def run(write_names: bool) -> None:
    with Store() as store:
        pairings = store.pairings()
    if not pairings:
        raise SystemExit("No consoles are paired yet; run 'headhunter pair' first.")
    log.info("Starting with %d paired consoles; names will %sbe written",
             len(pairings), "" if write_names else "not ")
    await Engine(pairings, write_names).run()