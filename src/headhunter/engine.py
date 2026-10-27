"""The long-running Headhunter program.

Keeps one connection to every paired console, each with a mirror of the
parameters Headhunter depends on. Mirrors are read in full when a console
connects, then kept current from the console's own change reports. Dante
subscriptions are polled through netaudio. Whenever anything relevant changes,
source names are recalculated; they are only written if asked to.

In gain mode, trim movement on the selected channel is turned into whole gain
steps on the remote preamp feeding it, and once the trim knob has been still
for a few seconds, trim is put back where it was. Holding the phantom power
button toggles 48V on the same preamp. Gain, trim and phantom power are only
changed if asked to; otherwise Headhunter reports what it would do.

If a console disappears, its connection is retried until it comes back, at
whatever IP address it turns up at.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable

from headhunter.consoles import locate
from headhunter import gain
from headhunter.buttons import flash
from headhunter.dante import NetaudioError, Subscription, read_subscriptions
from headhunter.naming import PlannedName, apply_names, paths_for, plan_from_state
from headhunter.native import NATIVE_PORT, DefinitionRecord, NativeClient, Value, wing_hash
from headhunter.store import Pairing, Settings, Store

log = logging.getLogger(__name__)

RETRY_SECONDS = 5.0  # Between attempts to find or reconnect to a console
READ_BATCH = 100  # Parameters read at once when a console connects
READ_TIMEOUT, SLOW_READ_TIMEOUT = 2.0, 5.0  # A second, slower try for anything that did not answer
CONFIG_POLL_SECONDS = 2.0  # How often to check the database for configuration changes
RENAMES_LOGGED_SEPARATELY = 4  # More renames than this at once are summed up in one line
DANTE_POLL_SECONDS = 5.0
SETTLE_SECONDS = 0.5  # Wait for a burst of changes to finish before recalculating
FOLLOW_SECONDS = 0.1  # Same, for following the selected channel
RESTORE_SECONDS = 2.0  # How long the trim knob must be still before trim is restored
PHANTOM_HOLD_SECONDS = 2.0  # How long the phantom power button must be held, as on the console itself
DISPLAY_SECONDS = 3.0  # How long gain stays on the scribble strip after the last change
LIGHT_FLASHES, LIGHT_FLASH_SECONDS = 3, 0.25  # Strip lights flashing for gain mode and phantom power

OnChange = Callable[["ConsoleLink", str | None, Value, Value], None]


_MISSING = object()


class ConsoleLink:
    """A self-healing connection to one paired console, with a mirror of its state."""

    def __init__(
        self,
        pairing: Pairing,
        paths: list[str],
        on_change: OnChange,
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
                state = await self._read_all(client)
            except (OSError, TimeoutError, ConnectionError) as error:
                log.warning("Could not connect to %s at %s: %s", self.name, ip, error or type(error).__name__)
                await client.close()
                await asyncio.sleep(RETRY_SECONDS)
                continue

            self.state = state
            self.client = client
            log.info("Connected to %s at %s", self.name, ip)
            self._notify(None, None, None)
            try:
                async for event in client.events():
                    path = self._by_hash.get(event.hash)
                    if path is not None and self.state.get(path) != event.value:
                        old = self.state.get(path)
                        self.state[path] = event.value
                        self._notify(path, old, event.value)
            finally:
                self.client = None
                await client.close()
            log.warning("Lost connection to %s; retrying", self.name)
            self._notify(None, None, None)
            await asyncio.sleep(RETRY_SECONDS)

    def _notify(self, path: str | None, old: Value, new: Value) -> None:
        """Report a change; a problem handling one change must never stop the connection."""
        try:
            self.on_change(self, path, old, new)
        except Exception:
            log.exception("Error handling a change to %s on %s", path, self.name)

    async def _read_all(self, client: NativeClient) -> dict[str, Value]:
        """Read every mirrored parameter, in batches. Anything that does not answer gets a
        second, slower try; anything still missing is logged and left out of the mirror."""
        state: dict[str, Value] = {}
        missing: list[str] = []
        for timeout, paths in ((READ_TIMEOUT, self.paths), (SLOW_READ_TIMEOUT, None)):
            paths = paths if paths is not None else missing
            missing = []
            for start in range(0, len(paths), READ_BATCH):
                batch = paths[start : start + READ_BATCH]
                values = await asyncio.gather(*(self._read(client, path, timeout) for path in batch))
                for path, value in zip(batch, values):
                    if value is _MISSING:
                        missing.append(path)
                    else:
                        state[path] = value
            if not missing:
                break
        if missing:
            log.warning("%s did not report %d parameters, e.g. %s", self.name, len(missing), ", ".join(missing[:3]))
        return state

    @staticmethod
    async def _read(client: NativeClient, path: str, timeout: float) -> Value:
        try:
            return await client.get(path, timeout)
        except TimeoutError:
            return _MISSING

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
        write_gain: bool = False,
        restore_after: float | None = None,
        overrides: set[str] | None = None,
        store_path=None,
    ) -> None:
        """overrides names settings given on the command line, which configuration changes leave alone.
        With store_path, the database there is watched, and configuration changes applied while running."""
        self.pairings = list(pairings)
        self.write_names = write_names
        self.write_gain = write_gain
        self.restore_after = RESTORE_SECONDS if restore_after is None else restore_after
        self._overrides = overrides or set()
        self._store_path = store_path
        self._ports = ports or {}
        self._running = False
        self.links: dict[str, ConsoleLink] = {}
        self._link_tasks: dict[str, asyncio.Task] = {}
        self.subscriptions: list[Subscription] = []
        self._dirty = asyncio.Event()
        self._reported: dict[tuple[str, int], tuple[str, str, tuple[str, ...]]] = {}
        self._summaries: dict[str, str] = {}
        # Gain mode, per FOH console. Always off at startup (see the README's Startup section)
        self.gain_mode: dict[str, bool] = {}
        self._connected_before: set[str] = set()
        self.targets: dict[str, gain.Target | None] = {}
        self._follow_dirty = asyncio.Event()
        self._described: dict[str, str] = {}
        self._definitions: dict[tuple, DefinitionRecord] = {}
        self._accumulators: dict[str, gain.Accumulator] = {}
        # Work for the gain worker, in order: ("trim", foh, strip, delta) or ("restore", foh, strip, None)
        self._work: asyncio.Queue[tuple[str, Pairing, gain.Strip, float | None]] = asyncio.Queue()
        self._dry_gains: dict[tuple[str, str], float] = {}  # What gain would be, in dry-run mode
        # Trim snapshots, per FOH console: what each strip's trim is put back to
        self._snapshots: dict[str, dict[str, float]] = {}
        # The strip being adjusted on each FOH console, and its restore timer
        self._adjusting: dict[str, gain.Strip] = {}
        self._restore_timers: dict[str, asyncio.TimerHandle] = {}
        # Phantom power button holds in progress, and flashes being shown
        self._phantom_holds: dict[str, asyncio.TimerHandle] = {}
        self._flashes: set[asyncio.Task] = set()
        # Strip light flashes on a console take turns, so one never saves another's colours as the real ones
        self._light_locks: dict[str, asyncio.Lock] = {}

        # Gain displays on scribble strips: per FOH console, name path -> (real name, text shown, real icon)
        self._shown: dict[str, dict[str, tuple[Value, str, Value]]] = {}
        self._display_timers: dict[tuple[str, str], asyncio.TimerHandle] = {}
        for pairing in self.pairings:
            self._add(pairing)

    # --- Configuration --------------------------------------------------------------------------

    def _add(self, pairing: Pairing) -> None:
        """Start looking after a console."""
        mac = pairing.console_mac
        self.links[mac] = ConsoleLink(pairing, self._paths(pairing), self._changed, self._ports.get(mac, NATIVE_PORT))
        if pairing.is_foh:
            self.gain_mode.setdefault(mac, False)
            self._accumulators.setdefault(mac, gain.Accumulator())
            self._snapshots.setdefault(mac, {})
            self._light_locks.setdefault(mac, asyncio.Lock())
            self._shown.setdefault(mac, {})
        if self._running:
            self._link_tasks[mac] = asyncio.create_task(self.links[mac].run(), name=f"link {pairing.display_name}")

    async def _drop(self, mac: str) -> None:
        """Stop looking after a console."""
        task = self._link_tasks.pop(mac, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.links.pop(mac, None)

    async def reconfigure(self, pairings: list[Pairing], settings: Settings | None = None) -> None:
        """Apply new configuration while running.

        A console whose configuration changes is treated like gain mode being turned off:
        anything in progress on it is stopped. A console that changes role is reconnected,
        since Headhunter watches different things on FOH and stage consoles.
        """
        old = {p.console_mac: p for p in self.pairings}
        new = {p.console_mac: p for p in pairings}
        for mac, before in old.items():
            after = new.get(mac)
            if after == before:
                continue
            if before.is_foh and self.links.get(mac) is not None:
                self._stop_gain_mode(before)
            if after is None:
                await self._drop(mac)
                log.info("%s is no longer paired", before.display_name)
            elif (after.role, after.console_model) != (before.role, before.console_model):
                await self._drop(mac)
                self._add(after)
                log.info("%s is now %s; reconnecting", after.display_name, after.role)
            else:
                self.links[mac].pairing = after
                log.info("%s: configuration changed", after.display_name)
        for mac, after in new.items():
            if mac not in old:
                self._add(after)
                log.info("%s paired", after.display_name)
        self.pairings = list(pairings)
        if settings is not None:
            self._apply_settings(settings)
        self._changed()

    def _apply_settings(self, settings: Settings) -> None:
        for name in ("write_names", "write_gain", "restore_after"):
            value = getattr(settings, name)
            if name in self._overrides or getattr(self, name) == value:
                continue
            setattr(self, name, value)
            log.info("Setting %s is now %s", name, value)
            if name == "write_gain":
                for pairing in self.pairings:
                    if pairing.is_foh:
                        self._stop_gain_mode(pairing)

    def _stop_gain_mode(self, foh: Pairing) -> None:
        """Turn gain mode off on a console, as if its button had been pressed."""
        mac = foh.console_mac
        if not self.gain_mode.get(mac):
            return
        self.gain_mode[mac] = False
        self._accumulators[mac].reset()
        self._restore_now(foh)
        self._cancel_phantom_hold(foh)
        self._unshow_all(foh)
        log.info("%s: gain mode off", foh.display_name)

    async def _watch_config(self) -> None:
        """Apply configuration changes made elsewhere, such as from another headhunter command."""
        with Store(self._store_path) as store:
            version = store.data_version()
            while True:
                await asyncio.sleep(CONFIG_POLL_SECONDS)
                try:
                    current = store.data_version()
                    if current != version:
                        version = current
                        await self.reconfigure(store.pairings(), store.settings())
                except Exception:
                    log.exception("Error applying configuration changes")

    @staticmethod
    def _paths(pairing: Pairing) -> list[str]:
        paths = paths_for(pairing) + gain.preamp_paths(pairing)
        if pairing.is_foh:
            paths += gain.foh_paths(pairing)
        return list(dict.fromkeys(paths))  # Without duplicates, in order

    def _changed(
        self, link: ConsoleLink | None = None, path: str | None = None, old: Value = None, new: Value = None
    ) -> None:
        self._dirty.set()
        self._follow_dirty.set()
        if link is not None and path is None and link.pairing.is_foh:
            if link.connected:
                self._fresh_connection(link)
            else:
                self._forget(link.pairing)
        if link is not None and path is not None and link.pairing.is_foh:
            self._button_changed(link.pairing, path, old, new)
            self._trim_changed(link, path, old, new)
        if link is not None and path is None and not link.connected:
            # A console dropped out; stop any adjustment that depends on it, and put names back
            for foh_mac in list(self._adjusting):
                self._restore_now(self.links[foh_mac].pairing)
            for foh_mac, shown in self._shown.items():
                if shown and foh_mac != link.pairing.console_mac:
                    self._unshow_all(self.links[foh_mac].pairing)

    def _trim_changed(self, link: ConsoleLink, path: str, old: Value, new: Value) -> None:
        """In gain mode, turn trim movement on the selected strip into gain; any other
        trim change is a real trim change, so it updates the snapshot instead."""
        foh = link.pairing
        if path == "$ctl.$stat.selidx":
            self._accumulators[foh.console_mac].reset()  # Never carry movement over to another channel
            self._restore_now(foh)
            self._cancel_phantom_hold(foh)  # A hold applies to the channel it started on
            return
        if not self.gain_mode[foh.console_mac] or not path.endswith(".in.set.trim"):
            return
        if not isinstance(new, (int, float)):
            return
        snapshot = self._snapshots[foh.console_mac]
        strip = gain.strip_from_selidx(link.state.get("$ctl.$stat.selidx"))
        adjusting = (
            strip is not None
            and path == f"{strip.kind}.{strip.number}.in.set.trim"
            and isinstance(old, (int, float))
            and self._resolve(foh, strip).preamp is not None
        )
        if not adjusting:
            snapshot[path] = float(new)  # A real trim change; never undo it
            return
        snapshot.setdefault(path, float(old))
        self._adjusting[foh.console_mac] = strip
        self._work.put_nowait(("trim", foh, strip, float(new) - float(old)))
        self._schedule_restore(foh, strip)

    def _resolve(self, foh: Pairing, strip: gain.Strip) -> gain.Target:
        states = {mac: l.state for mac, l in self.links.items() if l.connected}
        if foh.console_mac not in states:
            return gain.Target(strip, None, f"{foh.display_name} is not connected")
        return gain.resolve(strip, foh, self.pairings, self.subscriptions, states)

    def _schedule_restore(self, foh: Pairing, strip: gain.Strip) -> None:
        """(Re)start the countdown to restoring a strip's trim."""
        timer = self._restore_timers.pop(foh.console_mac, None)
        if timer is not None:
            timer.cancel()
        self._restore_timers[foh.console_mac] = asyncio.get_running_loop().call_later(
            self.restore_after, self._work.put_nowait, ("restore", foh, strip, None)
        )

    def _restore_now(self, foh: Pairing) -> None:
        """Restore the strip being adjusted straight away, if there is one."""
        strip = self._adjusting.get(foh.console_mac)
        if strip is None:
            return
        timer = self._restore_timers.pop(foh.console_mac, None)
        if timer is not None:
            timer.cancel()
        self._work.put_nowait(("restore", foh, strip, None))

    def _fresh_connection(self, link: ConsoleLink) -> None:
        """Every connection to a FOH console starts from a blank slate, as at startup:
        a gain mode button that is already on is noted, and ignored."""
        foh = link.pairing
        when = "reconnect" if foh.console_mac in self._connected_before else "boot"
        self._connected_before.add(foh.console_mac)
        if foh.gain_gpio is not None and link.state.get(f"$ctl.gpio.{foh.gain_gpio}.gpstate"):
            log.info("%s: gain mode toggle ON at %s; ignoring", foh.display_name, when)

    def _forget(self, foh: Pairing) -> None:
        """A FOH console dropped off the network: abandon everything in progress on it.
        Nothing is finished or restored when it comes back."""
        mac = foh.console_mac
        if self.gain_mode[mac]:
            log.warning("%s: gain mode off; everything in progress is abandoned", foh.display_name)
        self.gain_mode[mac] = False
        self._accumulators[mac].reset()
        self._adjusting.pop(mac, None)
        self._snapshots[mac] = {}
        self._shown[mac] = {}
        self._cancel_phantom_hold(foh)
        for timers in (self._restore_timers, self._phantom_holds):
            timer = timers.pop(mac, None)
            if timer is not None:
                timer.cancel()
        for key in [key for key in self._display_timers if key[0] == mac]:
            self._display_timers.pop(key).cancel()

    def _button_changed(self, foh: Pairing, path: str, old: Value, new: Value) -> None:
        """Turn gain mode on or off when its button changes, and time holds of the phantom
        power button. Only a change counts, never a button's state at startup, so a
        gain mode button left on needs pressing twice."""
        if foh.phantom_gpio is not None and path == f"$ctl.gpio.{foh.phantom_gpio}.gpstate":
            self._phantom_button(foh, old, new)
            return
        if foh.gain_gpio is None or path != f"$ctl.gpio.{foh.gain_gpio}.gpstate":
            return
        link = self.links[foh.console_mac]
        if not old and new:
            self.gain_mode[foh.console_mac] = True
            self._accumulators[foh.console_mac].reset()
            # Snapshot every trim; adjustments are made relative to these, and they are restored to them
            self._snapshots[foh.console_mac] = {
                path: float(value)
                for path, value in link.state.items()
                if path.endswith(".in.set.trim") and isinstance(value, (int, float))
            }
            log.info("%s: gain mode on", foh.display_name)
            if self.write_gain:
                self._start_flash(self._flash_layer(foh))
        elif old and not new:
            if not self.gain_mode[foh.console_mac]:
                return  # The button was on at startup, so gain mode was never on: nothing has changed
            self._stop_gain_mode(foh)  # An adjustment in progress is cancelled, and trim restored
            if self.write_gain:
                self._start_flash(self._flash_layer(foh))

    def _phantom_button(self, foh: Pairing, old: Value, new: Value) -> None:
        """Pressing starts the countdown to toggling phantom power; releasing early cancels it."""
        if not old and new:
            if not self.gain_mode[foh.console_mac]:
                return  # The button does nothing outside gain mode
            strip = gain.strip_from_selidx(self.links[foh.console_mac].state.get("$ctl.$stat.selidx"))
            if strip is None:
                return
            self._cancel_phantom_hold(foh)
            self._phantom_holds[foh.console_mac] = asyncio.get_running_loop().call_later(
                PHANTOM_HOLD_SECONDS, self._phantom_held, foh, strip
            )
        elif old and not new:
            self._cancel_phantom_hold(foh)

    def _phantom_held(self, foh: Pairing, strip: gain.Strip) -> None:
        self._phantom_holds.pop(foh.console_mac, None)
        self._work.put_nowait(("phantom", foh, strip, None))

    def _cancel_phantom_hold(self, foh: Pairing) -> None:
        timer = self._phantom_holds.pop(foh.console_mac, None)
        if timer is not None:
            timer.cancel()

    async def run(self) -> None:
        if not any(p.is_foh for p in self.pairings):
            log.warning("No console is marked FOH, so no sources will be named")
        self._running = True
        for mac, link in self.links.items():
            self._link_tasks[mac] = asyncio.create_task(link.run(), name=f"link {link.name}")
        tasks = []
        for pairing in self.pairings:
            if pairing.is_foh and pairing.gain_gpio is None:
                log.warning("%s has no gain mode button; choose one with: headhunter buttons %s --gain <A-D>",
                            pairing.display_name, pairing.display_name)
        tasks += [
            asyncio.create_task(self._poll_dante(), name="dante"),
            asyncio.create_task(self._naming(), name="naming"),
            asyncio.create_task(self._following(), name="following"),
            asyncio.create_task(self._gain_worker(), name="gain"),
        ]
        if self._store_path is not None:
            tasks.append(asyncio.create_task(self._watch_config(), name="configuration"))
        try:
            # Waiting this way, rather than with gather(), means stopping Headhunter does not
            # cancel the console connections before the finally block has used them
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                task.result()  # Re-raise whatever stopped it
        finally:
            # Put real channel names back before disconnecting, so a clean exit leaves nothing behind
            for pairing in self.pairings:
                if pairing.is_foh:
                    with contextlib.suppress(ConnectionError, TimeoutError):
                        await self._unshow_now(pairing)
            for task in [*tasks, *self._link_tasks.values()]:
                task.cancel()
            self._running = False
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _poll_dante(self) -> None:
        while True:
            try:
                subscriptions = await read_subscriptions(self.pairings)
            except NetaudioError as error:
                log.warning("Could not read Dante subscriptions: %s", error)
            except Exception:
                log.exception("Error reading Dante subscriptions")
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
            try:
                await self._update_names()
            except Exception:
                log.exception("Error updating source names")

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
        written = [plan for plan in to_write if plan not in failed]
        for plan in written:
            # The console never reports our own writes back, so update the mirror ourselves
            self.links[plan.console.console_mac].state[f"io.in.MOD.{plan.dante_input}.name"] = plan.planned
        for console in {plan.console.console_mac: plan.console for plan in written}.values():
            mine = [plan for plan in written if plan.console is console]
            if len(mine) > RENAMES_LOGGED_SEPARATELY:
                log.info("%s: renamed %d Dante inputs", console.display_name, len(mine))
                continue
            for plan in mine:
                log.info("%s MOD %d: %r -> %r", console.display_name, plan.dante_input, plan.current, plan.planned)
        for plan in failed:
            log.warning("%s MOD %d: wanted %r, reads back differently", plan.console.display_name, plan.dante_input, plan.planned)
        self._changed()  # Recalculate, so the summary reflects what is now on the console

    async def _gain_worker(self) -> None:
        """Do queued gain changes and trim restores one at a time, in order, so they never overlap."""
        while True:
            kind, foh, strip, delta = await self._work.get()
            try:
                if kind == "trim":
                    await self._apply_trim(foh, strip, delta)
                elif kind == "restore":
                    await self._restore_trim(foh, strip)
                elif kind == "unshow":
                    await self._unshow(foh, strip)
                else:
                    await self._toggle_phantom(foh, strip)
            except (ConnectionError, TimeoutError) as error:
                self._accumulators[foh.console_mac].reset()
                action = {"trim": "change gain", "restore": "restore trim", "phantom": "change 48V",
                          "unshow": "restore a channel name"}[kind]
                log.warning("Could not %s: %s", action, error or type(error).__name__)
            except Exception:
                self._accumulators[foh.console_mac].reset()
                log.exception("Error handling %s on %s", kind, foh.display_name)

    async def _toggle_phantom(self, foh: Pairing, strip: gain.Strip) -> None:
        """Toggle phantom power on a strip's remote preamp, and confirm it by flashing the button."""
        if not self.gain_mode[foh.console_mac]:
            return
        target = self._resolve(foh, strip)
        if target.preamp is None:
            log.info("%s %s: no 48V: %s", foh.display_name, strip.label, target.reason)
            return
        preamp = target.preamp
        link = self.links[preamp.console.console_mac]
        wanted = 0 if link.state.get(f"{preamp.path}.vph") else 1
        if not self.write_gain:
            log.info("%s %s: would turn %s 48V %s", foh.display_name, strip.label, preamp.label, "on" if wanted else "off")
            return
        if link.client is None:
            raise ConnectionError(f"Not connected to {preamp.console.display_name}")
        link.client.set(f"{preamp.path}.vph", wanted)
        actual = await link.client.get(f"{preamp.path}.vph")
        link.state[f"{preamp.path}.vph"] = actual  # Never reported back to us
        self._described[foh.console_mac] = await self._description(foh)
        if actual != wanted:
            log.warning("%s: 48V did not turn %s", preamp.label, "on" if wanted else "off")
            return  # No flash: nothing changed
        log.info("%s %s: %s 48V %s", foh.display_name, strip.label, preamp.label, "on" if actual else "off")
        await self._show(foh, strip, preamp)
        foh_client = self.links[foh.console_mac].client
        if foh_client is not None and foh.phantom_gpio is not None:
            self._start_flash(self._flash(foh_client, foh.phantom_gpio, "on" if actual else "off"))
        self._start_flash(self._flash_lights(foh, [f"{strip.kind}.{strip.number}"], gain.RED))

    def _start_flash(self, coroutine) -> None:
        """Run a flash alongside everything else, keeping hold of it until it finishes."""
        task = asyncio.create_task(coroutine)
        self._flashes.add(task)
        task.add_done_callback(self._flashes.discard)

    async def _layer_strips(self, foh: Pairing) -> list[str]:
        """The strips on the faders right now, across every fader section, as path prefixes like ch.13."""
        client = self.links[foh.console_mac].client
        strips = []
        for section, faders in gain.layer_sections(foh):
            layer = await self._maybe(client, f"$ctl.layer.{section}.sel")
            if layer is None:
                continue
            offset = await self._maybe(client, f"$ctl.layer.{section}.{layer}.ofs") or 0
            for position in range(offset + 1, offset + faders + 1):
                base = f"$ctl.layer.{section}.{layer}.{position}"
                strip = gain.layer_strip(await self._maybe(client, f"{base}.type"), await self._maybe(client, f"{base}.i"))
                if strip is not None and strip not in strips:
                    strips.append(strip)
        return strips

    @staticmethod
    async def _maybe(client: NativeClient | None, path: str) -> Value:
        """Read a parameter that may not exist on every console; None if it does not answer."""
        if client is None:
            return None
        try:
            return await client.get(path, timeout=1.0)
        except TimeoutError:
            return None

    async def _flash_layer(self, foh: Pairing) -> None:
        """Flash every channel on the faders white, to show gain mode turning on or off."""
        try:
            strips = await self._layer_strips(foh)
        except ConnectionError:
            return
        await self._flash_lights(foh, strips, gain.WHITE)

    async def _flash_lights(self, foh: Pairing, strips: list[str], colour: int) -> None:
        """Flash strip lights in one colour: save each strip's colour and light, turn the lights
        on in that colour, flash them off and on three times, ending off, then put everything back.

        strips are path prefixes such as "ch.13". A colour or light someone changes meanwhile is kept:
        Headhunter's own flash writes never reach the mirror, but other changes do, so each light is
        put back to what the mirror says, which is the original unless someone changed it.
        """
        if not strips:
            return
        link = self.links[foh.console_mac]
        async with self._light_locks[foh.console_mac]:
            client = link.client
            if client is None:
                return
            try:
                original = {}
                for base in strips:
                    for setting in ("col", "led"):
                        path = f"{base}.{setting}"
                        value = await self._maybe(client, path)
                        if value is not None:
                            original[path] = value
                lights = [path for path in original if path.endswith(".led")]
                try:
                    for path in original:
                        client.set(path, colour if path.endswith(".col") else 1)
                    for _ in range(LIGHT_FLASHES):
                        await asyncio.sleep(LIGHT_FLASH_SECONDS)
                        self._set_lights(client, lights, 0)
                        await asyncio.sleep(LIGHT_FLASH_SECONDS)
                        self._set_lights(client, lights, 1)
                    await asyncio.sleep(LIGHT_FLASH_SECONDS)
                    self._set_lights(client, lights, 0)  # End dark
                    await asyncio.sleep(LIGHT_FLASH_SECONDS)
                finally:
                    for path, value in original.items():
                        client.set(path, link.state.get(path, value))
                    await client.flush()
            except ConnectionError:
                pass  # The console went away; nothing is restored when it comes back

    @staticmethod
    def _set_lights(client: NativeClient, lights: list[str], on: int) -> None:
        for path in lights:
            client.set(path, on)

    @staticmethod
    async def _flash(client: NativeClient, gpio: int, pattern: str) -> None:
        try:
            await flash(client, gpio, pattern)
        except ConnectionError:
            pass  # The console went away; nothing to confirm to

    async def _restore_trim(self, foh: Pairing, strip: gain.Strip) -> None:
        """Put a strip's trim back to its snapshot, and discard any leftover movement."""
        if self._adjusting.get(foh.console_mac) == strip:
            del self._adjusting[foh.console_mac]
        self._accumulators[foh.console_mac].reset()
        path = f"{strip.kind}.{strip.number}.in.set.trim"
        snapshot = self._snapshots[foh.console_mac].get(path)
        link = self.links[foh.console_mac]
        current = link.state.get(path)
        if snapshot is None or not isinstance(current, (int, float)) or abs(current - snapshot) < 0.05:
            return
        if not self.write_gain:
            log.info("%s %s: would put trim back %+.1f -> %+.1f dB", foh.display_name, strip.label, current, snapshot)
            return
        if link.client is None:
            raise ConnectionError(f"Not connected to {foh.display_name}")
        link.client.set(path, float(snapshot))
        actual = await link.client.get(path)
        link.state[path] = actual  # Never reported back to us, so update the mirror ourselves
        if not isinstance(actual, (int, float)) or abs(actual - snapshot) > 0.05:
            log.warning("%s %s: trim not back to %+.1f dB, reads %s", foh.display_name, strip.label, snapshot, actual)
            return
        log.info("%s %s: trim back to %+.1f dB", foh.display_name, strip.label, actual)

    async def _apply_trim(self, foh: Pairing, strip: gain.Strip, delta: float) -> None:
        if not self.gain_mode[foh.console_mac] or self._adjusting.get(foh.console_mac) != strip:
            return  # Turned off, or interrupted, since this was queued
        accumulator = self._accumulators[foh.console_mac]
        target = self._resolve(foh, strip)
        if target.preamp is None:
            return  # Trim is just trim on this channel
        preamp = target.preamp
        definition = await self._definition(preamp)
        if definition is None or not definition.steps or definition.steps < 2:
            log.warning("%s: range unknown, not changing", preamp.label)
            return
        steps = gain.Steps(definition.minimum, definition.maximum, definition.steps)
        moved = accumulator.add(delta, steps.step)
        if moved == 0:
            return

        link = self.links[preamp.console.console_mac]
        key = (preamp.console.console_mac, preamp.path)
        current = self._dry_gains.get(key, link.state.get(f"{preamp.path}.g")) if not self.write_gain \
            else link.state.get(f"{preamp.path}.g")
        if not isinstance(current, (int, float)):
            return
        wanted = steps.move(current, moved)
        if wanted == current:
            accumulator.reset()  # At the end of the range; nothing more to do in this direction
            log.info("%s %s: %s at %+.1f dB, end of range", foh.display_name, strip.label, preamp.label, current)
            return

        if not self.write_gain:
            self._dry_gains[key] = wanted
            log.info("%s %s: would set %s %+.1f -> %+.1f dB", foh.display_name, strip.label, preamp.label, current, wanted)
            return

        if link.client is None:
            raise ConnectionError(f"Not connected to {preamp.console.display_name}")
        link.client.set(f"{preamp.path}.g", float(wanted))
        actual = await link.client.get(f"{preamp.path}.g")
        # The console never reports our own changes back, so update the mirror ourselves
        link.state[f"{preamp.path}.g"] = actual
        # ...and the follow line too, so the change is not logged twice
        self._described[foh.console_mac] = await self._description(foh)
        if not isinstance(actual, (int, float)) or abs(actual - wanted) > 0.01:
            accumulator.reset()  # Never build on a change we could not confirm
            log.warning("%s: wanted %+.1f dB, reads %s", preamp.label, wanted, actual)
            return
        log.info("%s %s: %s %+.1f -> %+.1f dB", foh.display_name, strip.label, preamp.label, current, actual)
        await self._show(foh, strip, preamp)

    async def _show(self, foh: Pairing, strip: gain.Strip, preamp: gain.Preamp) -> None:
        """Show a preamp's gain and phantom power on a strip's scribble strip, for a few seconds.

        The channel's icon is hidden meanwhile, so the text has the whole scribble strip.
        """
        link = self.links[foh.console_mac]
        base = f"{strip.kind}.{strip.number}"
        if link.state.get(f"{base}.clink") or link.client is None:
            return  # A linked channel shows its source's name, so the display would never be seen
        stage = self.links[preamp.console.console_mac].state
        text = gain.display_text(stage.get(f"{preamp.path}.g"), stage.get(f"{preamp.path}.vph"))
        name_path, icon_path = f"{base}.name", f"{base}.icon"
        shown = self._shown[foh.console_mac]
        if name_path in shown:
            real_name, _, real_icon = shown[name_path]
        else:
            real_name, real_icon = link.state.get(name_path, ""), link.state.get(icon_path, gain.NO_ICON)
        link.client.set(name_path, text)
        link.state[name_path] = text  # Never reported back to us
        if link.state.get(icon_path, gain.NO_ICON) != gain.NO_ICON:
            link.client.set(icon_path, gain.NO_ICON)
            link.state[icon_path] = gain.NO_ICON
        shown[name_path] = (real_name, text, real_icon)
        key = (foh.console_mac, name_path)
        timer = self._display_timers.pop(key, None)
        if timer is not None:
            timer.cancel()
        self._display_timers[key] = asyncio.get_running_loop().call_later(
            DISPLAY_SECONDS, self._work.put_nowait, ("unshow", foh, strip, None)
        )

    async def _unshow(self, foh: Pairing, strip: gain.Strip) -> None:
        """Put a strip's real name and icon back, unless someone has changed them in the meantime."""
        name_path = f"{strip.kind}.{strip.number}.name"
        icon_path = f"{strip.kind}.{strip.number}.icon"
        timer = self._display_timers.pop((foh.console_mac, name_path), None)
        if timer is not None:
            timer.cancel()
        entry = self._shown[foh.console_mac].get(name_path)
        if entry is None:
            return
        real_name, text, real_icon = entry
        link = self.links[foh.console_mac]
        if not link.connected:
            return  # Forgotten when the connection drops; nothing is restored when it comes back
        # Each is only put back if it is still as Headhunter left it; a change made meanwhile stays
        if link.state.get(name_path) == text:
            link.client.set(name_path, real_name)
            link.state[name_path] = real_name
        if real_icon != gain.NO_ICON and link.state.get(icon_path) == gain.NO_ICON:
            link.client.set(icon_path, real_icon)
            link.state[icon_path] = real_icon
        self._shown[foh.console_mac].pop(name_path, None)

    def _unshow_all(self, foh: Pairing) -> None:
        """Queue every gain display on a console to be taken down straight away."""
        for name_path in list(self._shown[foh.console_mac]):
            kind, number = name_path.split(".")[:2]
            self._work.put_nowait(("unshow", foh, gain.Strip(kind, int(number)), None))

    async def _unshow_now(self, foh: Pairing) -> None:
        """Take down every gain display on a console immediately, bypassing the queue."""
        for name_path in list(self._shown[foh.console_mac]):
            kind, number = name_path.split(".")[:2]
            await self._unshow(foh, gain.Strip(kind, int(number)))
        client = self.links[foh.console_mac].client
        if client is not None:
            await client.flush()

    async def _following(self) -> None:
        while True:
            await self._follow_dirty.wait()
            await asyncio.sleep(FOLLOW_SECONDS)
            self._follow_dirty.clear()
            for pairing in self.pairings:
                if pairing.is_foh:
                    try:
                        await self._follow(pairing)
                    except (ConnectionError, TimeoutError):
                        pass  # Followed again when the console reconnects
                    except Exception:
                        log.exception("Error following %s", pairing.display_name)

    async def _follow(self, foh: Pairing) -> None:
        """Work out what the trim knob on the selected channel would control, and report it if that changed."""
        if not self.links[foh.console_mac].connected:
            self.targets[foh.console_mac] = None
            return
        description = await self._description(foh)
        if self._described.get(foh.console_mac) != description:
            self._described[foh.console_mac] = description
            log.info("%s", description)

    async def _description(self, foh: Pairing) -> str:
        """What the trim knob on a FOH console's selected strip controls, in words; updates self.targets."""
        selidx = self.links[foh.console_mac].state.get("$ctl.$stat.selidx")
        strip = gain.strip_from_selidx(selidx)
        if strip is None:
            self.targets[foh.console_mac] = None
            return f"{foh.display_name} {gain.strip_label(selidx)}: trim only: {gain.no_source_reason(selidx)}"
        target = self._resolve(foh, strip)
        self.targets[foh.console_mac] = target
        if target.preamp is None:
            return f"{foh.display_name} {strip.label}: trim only: {target.reason}"
        return f"{foh.display_name} {strip.label} -> {await self._describe(target.preamp)}"

    async def _describe(self, preamp: gain.Preamp) -> str:
        """A preamp's gain, phantom power and range, e.g. for the log."""
        link = self.links[preamp.console.console_mac]
        level = link.state.get(f"{preamp.path}.g")
        phantom = "on" if link.state.get(f"{preamp.path}.vph") else "off"
        text = f"{preamp.label}: {level:+.1f} dB, 48V {phantom}"
        definition = await self._definition(preamp)
        if definition is not None and definition.steps and definition.steps > 1:
            step = (definition.maximum - definition.minimum) / (definition.steps - 1)
            device = link.state.get(f"$stat.{preamp.group}.dev") if preamp.group in gain.AES50_PORTS else ""
            text += f" ({device + ', ' if device else ''}{definition.minimum:+g} to {definition.maximum:+g}, {step:g} dB steps)"
        return text

    async def _definition(self, preamp: gain.Preamp) -> DefinitionRecord | None:
        """A preamp's range and step size. These depend on what is connected to an AES50
        port, so they are cached per connected device and reread when it changes."""
        link = self.links[preamp.console.console_mac]
        device = link.state.get(f"$stat.{preamp.group}.dev") if preamp.group in gain.AES50_PORTS else None
        key = (preamp.console.console_mac, preamp.group, device)
        if key not in self._definitions:
            if link.client is None:
                return None
            try:
                self._definitions[key] = await link.client.define(wing_hash(f"{preamp.path}.g"))
            except (TimeoutError, ConnectionError):
                return None
        return self._definitions[key]

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
                log.info("%s: %r skipped, linked on %s", where, plan.planned, ", ".join(plan.linked))
            elif plan.current != plan.planned and not self.write_names:
                log.info("%s: would rename %r -> %r", where, plan.current, plan.planned)


async def run(
    write_names: bool | None = None,
    write_gain: bool | None = None,
    restore_after: float | None = None,
    stats_minutes: float | None = None,
) -> None:
    """Run Headhunter. Settings come from the database; any given here override them for this run."""
    with Store() as store:
        pairings = store.pairings()
        settings = store.settings()
        path = store.path
    if not pairings:
        raise SystemExit("No consoles are paired yet; run 'headhunter pair' first.")
    given = {"write_names": write_names, "write_gain": write_gain, "restore_after": restore_after,
             "stats_minutes": stats_minutes}
    overrides = {name for name, value in given.items() if value is not None}
    value = {name: given[name] if name in overrides else getattr(settings, name) for name in given}
    log.info("Starting with %d paired consoles; names will %sbe written; gain will %sbe changed",
             len(pairings), "" if value["write_names"] else "not ", "" if value["write_gain"] else "not ")
    engine = Engine(
        pairings, value["write_names"], write_gain=value["write_gain"], restore_after=value["restore_after"],
        overrides=overrides, store_path=path,
    )
    stats_minutes = value["stats_minutes"]
    if not stats_minutes:
        await engine.run()
        return
    from headhunter.stats import report

    reporter = asyncio.create_task(report(engine, stats_minutes))
    try:
        await engine.run()
    finally:
        reporter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reporter