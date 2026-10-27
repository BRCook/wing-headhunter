"""Finding paired consoles on the network."""

from headhunter.discovery import discover_consoles
from headhunter.store import Pairing, Store


async def locate(pairings: list[Pairing]) -> dict[str, str]:
    """Find the current IP of every paired console that answers, keyed by console MAC."""
    found = {console.mac: console.ip for console in await discover_consoles() if console.mac}
    return {p.console_mac: found[p.console_mac] for p in pairings if p.console_mac in found}


async def find_ip(display_name: str) -> str:
    """Find the current IP of one paired console, by display name.

    Raises LookupError if no console has that name, or it does not answer.
    """
    with Store() as store:
        pairing = store.by_display_name(display_name)
    if pairing is None:
        raise LookupError(f"No paired console called {display_name!r}")
    ips = await locate([pairing])
    if pairing.console_mac not in ips:
        raise LookupError(f"{pairing.display_name} ({pairing.console_name}) did not answer discovery")
    return ips[pairing.console_mac]
