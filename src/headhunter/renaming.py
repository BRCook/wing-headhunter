"""Renaming a console's Dante device, and re-pointing every subscription to it.

Dante subscriptions refer to the transmitting device by name, so renaming a
device breaks every subscription to it, on every device on the network. This
finds them all first, renames the device, then subscribes each one again under
the new name.
"""

import asyncio
import re
from dataclasses import dataclass

from headhunter import dante
from headhunter.dante import Subscription
from headhunter.store import Pairing

# Dante device names: letters, digits and hyphens, not starting or ending with a hyphen
DANTE_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,29}[A-Za-z0-9])?$")
RECONNECT_SECONDS = 20.0
RETRY_SECONDS = 2.0


@dataclass(frozen=True)
class RenamePlan:
    pairing: Pairing
    new_name: str
    affected: list[Subscription]  # Every subscription to the device, which must be re-pointed


def check_name(new_name: str, taken: set[str]) -> None:
    """Raise ValueError if a name is not a valid, unused Dante device name."""
    if not DANTE_NAME.match(new_name):
        raise ValueError(
            "Dante names are 1 to 31 letters, digits and hyphens, and cannot start or end with a hyphen"
        )
    if new_name.lower() in {name.lower() for name in taken}:
        raise ValueError(f"{new_name} is already in use on the network")


async def plan(pairing: Pairing, new_name: str) -> RenamePlan:
    """Work out what renaming a console's Dante device would affect, without changing anything."""
    subscriptions = await dante.read_all_subscriptions()
    taken = {s.rx_device for s in subscriptions} | {s.tx_device for s in subscriptions if s.tx_device}
    taken.discard(pairing.dante_name)
    check_name(new_name, taken)
    affected = [s for s in subscriptions if s.tx_device == pairing.dante_name and s.tx_channel is not None]
    return RenamePlan(pairing, new_name, affected)


async def execute(rename: RenamePlan) -> list[Subscription]:
    """Rename the device, re-point every affected subscription, and wait for them to reconnect.

    Returns the subscriptions that did not reconnect in time.
    """
    await dante.rename_device(rename.pairing.dante_mac, rename.new_name)

    # The new name can take a moment to appear on the network, so retry each subscription for a while
    pending = list(rename.affected)
    deadline = asyncio.get_running_loop().time() + RECONNECT_SECONDS
    while pending and asyncio.get_running_loop().time() < deadline:
        still_pending = []
        for sub in pending:
            try:
                await dante.subscribe(rename.new_name, sub.tx_channel, sub.rx_device, sub.rx_channel)
            except dante.NetaudioError:
                still_pending.append(sub)
        pending = still_pending
        if pending:
            await asyncio.sleep(RETRY_SECONDS)
    if pending:
        return pending

    # Check they all reconnected
    while asyncio.get_running_loop().time() < deadline:
        current = {(s.rx_device, s.rx_channel): s for s in await dante.read_all_subscriptions()}
        unconnected = [
            sub for sub in rename.affected
            if not ((now := current.get((sub.rx_device, sub.rx_channel)))
                    and now.tx_device == rename.new_name and now.connected)
        ]
        if not unconnected:
            return []
        await asyncio.sleep(RETRY_SECONDS)
    return unconnected
