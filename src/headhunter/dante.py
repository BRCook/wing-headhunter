"""Read Dante subscriptions between paired consoles, using netaudio.

We use netaudio's command line with --json output rather than its Python
internals, which are more likely to change between releases. Devices are
selected by MAC address, so renaming a Dante device never breaks anything.

Subscriptions name their transmit channel rather than numbering it, so the
channel list is used to turn each name back into a channel number; that keeps
working even if someone renames Dante channels in Dante Controller.
"""

import asyncio
import json
import sys
from dataclasses import dataclass

from headhunter.store import Pairing

NETAUDIO_TIMEOUT = 30.0
RUNS = 0  # How many times netaudio has been run, for the stats


class NetaudioError(RuntimeError):
    """netaudio failed, or returned something we could not understand."""


@dataclass(frozen=True)
class Subscription:
    """One Dante receive channel and what it is subscribed to."""

    rx_device: str
    rx_channel: int
    tx_device: str | None
    tx_channel: int | None
    connected: bool


async def run_netaudio(macs: list[str], *command: str) -> object:
    """Run a netaudio command against the given devices (all devices, if none are given) and return its JSON."""
    return await _netaudio(macs, command, as_json=True)


async def run_netaudio_text(macs: list[str], *command: str) -> str:
    """Run a netaudio command that changes something, and return what it printed."""
    return await _netaudio(macs, command, as_json=False)


async def _netaudio(macs: list[str], command: tuple[str, ...], as_json: bool) -> object:
    global RUNS
    RUNS += 1
    args = [sys.executable, "-m", "netaudio"] + (["--json"] if as_json else [])
    for mac in macs:
        args += ["-m", mac]
    process = await asyncio.create_subprocess_exec(
        *args, *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), NETAUDIO_TIMEOUT)
    except TimeoutError:
        process.kill()
        raise NetaudioError(f"netaudio {' '.join(command)} timed out") from None
    except asyncio.CancelledError:
        process.kill()  # Headhunter is stopping; do not leave netaudio running
        raise
    if process.returncode != 0:
        message = (stderr or stdout).decode(errors="replace").strip()
        raise NetaudioError(f"netaudio {' '.join(command)} failed: {message}")
    if not as_json:
        return stdout.decode(errors="replace").strip()
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as error:
        raise NetaudioError(f"netaudio {' '.join(command)} returned invalid JSON") from error


def transmit_channel_numbers(channels: dict) -> dict[str, dict[str, int]]:
    """Map device name -> transmit channel name -> channel number."""
    numbers: dict[str, dict[str, int]] = {}
    for device in channels.values():
        names = numbers.setdefault(device["name"], {})
        for key, channel in device.get("tx_channels", {}).items():
            # Accept any of the channel's names, in case a subscription uses another one
            for name in {key, channel.get("name"), channel.get("friendly_name"), channel.get("factory_name")}:
                if name:
                    names[name] = channel["number"]
    return numbers


def parse_subscriptions(subscriptions: list, channels: dict) -> list[Subscription]:
    """Combine netaudio's subscription list and channel list."""
    numbers = transmit_channel_numbers(channels)
    parsed = []
    for entry in subscriptions:
        tx_device = entry.get("tx_device")
        tx_name = entry.get("tx_channel")
        tx_channel = numbers.get(tx_device, {}).get(tx_name) if tx_device and tx_name else None
        status = entry.get("status") or {}
        parsed.append(
            Subscription(
                rx_device=entry["rx_device"],
                rx_channel=entry["rx_channel_number"],
                tx_device=tx_device,
                tx_channel=tx_channel,
                connected=status.get("state") == "connected" and tx_channel is not None,
            )
        )
    return parsed


async def read_all_subscriptions() -> list[Subscription]:
    """Read every subscription on every Dante device on the network."""
    subscriptions, channels = await asyncio.gather(
        run_netaudio([], "subscription", "list"),
        run_netaudio([], "channel", "list"),
    )
    if not isinstance(subscriptions, list) or not isinstance(channels, dict):
        raise NetaudioError("netaudio returned data in an unexpected shape")
    return parse_subscriptions(subscriptions, channels)


async def rename_device(dante_mac: str, new_name: str) -> None:
    """Rename a Dante device; netaudio reads the name back and fails if it did not take."""
    await run_netaudio_text([dante_mac], "device", "name", new_name)


async def subscribe(tx_device: str, tx_channel: int, rx_device: str, rx_channel: int) -> None:
    """Subscribe a receive channel to a transmit channel, both by number."""
    await run_netaudio_text([], "subscription", "add", "--tx", f"tx:{tx_channel}@{tx_device}", "--rx", f"rx:{rx_channel}@{rx_device}")


async def read_subscriptions(pairings: list[Pairing]) -> list[Subscription]:
    """Read every subscription on the paired consoles' Dante cards."""
    macs = [pairing.dante_mac for pairing in pairings]
    if not macs:
        return []
    subscriptions, channels = await asyncio.gather(
        run_netaudio(macs, "subscription", "list"),
        run_netaudio(macs, "channel", "list"),
    )
    if not isinstance(subscriptions, list) or not isinstance(channels, dict):
        raise NetaudioError("netaudio returned data in an unexpected shape")
    return parse_subscriptions(subscriptions, channels)
