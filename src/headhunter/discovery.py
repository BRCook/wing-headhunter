"""Find Wing consoles and Wing Dante cards on the local network.

Consoles are found by broadcasting the Wing's discovery message ("WING?") to
UDP port 2222. Every Wing that hears it replies with

    WING,<ip>,<name>,<model>,<serial>,<firmware>

Console MAC addresses are read from the kernel's ARP table, which only works on
Linux, and only for consoles on the local subnet.

Dante cards are found over mDNS, using the _netaudio-cmc service that every
Dante device advertises. Its properties include the card's model (hex-encoded,
"wing64 B" for a Wing Dante card) and an "id" that is the card's MAC address
with "fffe" inserted in the middle.
"""

import asyncio
import logging
from dataclasses import dataclass, replace
from pathlib import Path

from zeroconf import IPVersion, ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

log = logging.getLogger(__name__)

WING_PORT = 2222
DISCOVERY_MESSAGE = b"WING?"
BROADCAST_ADDRESS = "255.255.255.255"
ARP_TABLE = Path("/proc/net/arp")

DANTE_SERVICE = "_netaudio-cmc._udp.local."
WING_DANTE_MODEL = "wing64 B"


@dataclass(frozen=True)
class DiscoveredConsole:
    """A Wing console that answered discovery."""

    ip: str
    name: str
    model: str
    serial: str
    firmware: str
    mac: str | None = None


@dataclass(frozen=True)
class DiscoveredDanteDevice:
    """A Wing Dante card advertised over mDNS."""

    name: str
    ip: str | None
    mac: str | None
    model: str


def parse_reply(data: bytes, source_ip: str) -> DiscoveredConsole | None:
    """Turn a discovery reply into a DiscoveredConsole, or None if it isn't one.

    The console's name is the only field that could contain a comma, so the
    fields after it are taken from the end of the reply.
    """
    try:
        fields = data.decode("ascii").split(",")
    except UnicodeDecodeError:
        return None
    if len(fields) < 6 or fields[0] != "WING":
        return None

    # We trust the address the reply came from over the address it reports
    return DiscoveredConsole(
        ip=source_ip,
        name=",".join(fields[2:-3]),
        model=fields[-3],
        serial=fields[-2],
        firmware=fields[-1],
    )


def read_arp_table() -> dict[str, str]:
    """Map IP addresses to MAC addresses using the kernel's ARP table."""
    table = {}
    try:
        lines = ARP_TABLE.read_text().splitlines()[1:]  # Skip the header
    except OSError:
        log.warning("Could not read %s; MAC addresses will be missing", ARP_TABLE)
        return table

    for line in lines:
        fields = line.split()
        if len(fields) >= 4 and fields[3] != "00:00:00:00:00:00":
            table[fields[0]] = fields[3]
    return table


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    """Collects discovery replies, keyed by IP address."""

    def __init__(self, replies: dict[str, DiscoveredConsole]) -> None:
        self.replies = replies

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        console = parse_reply(data, addr[0])
        if console is None:
            log.debug("Ignoring unexpected reply from %s: %r", addr[0], data)
        else:
            self.replies[console.ip] = console


async def discover_consoles(
    timeout: float = 2.0, attempts: int = 3
) -> list[DiscoveredConsole]:
    """Broadcast discovery and return every Wing console that answers.

    The broadcast is sent several times across the timeout, in case a packet
    goes missing.
    """
    loop = asyncio.get_running_loop()
    replies: dict[str, DiscoveredConsole] = {}

    transport, _ = await loop.create_datagram_endpoint(
        lambda: _DiscoveryProtocol(replies),
        local_addr=("0.0.0.0", 0),
        allow_broadcast=True,
    )
    try:
        for _ in range(attempts):
            transport.sendto(DISCOVERY_MESSAGE, (BROADCAST_ADDRESS, WING_PORT))
            await asyncio.sleep(timeout / attempts)

        # Linux does not learn a MAC address from a broadcast reply alone.
        # Sending each console one direct message makes the kernel resolve it.
        for ip in replies:
            transport.sendto(DISCOVERY_MESSAGE, (ip, WING_PORT))
        await asyncio.sleep(0.2)
    finally:
        transport.close()

    arp = read_arp_table()
    return [
        replace(console, mac=arp.get(console.ip))
        for console in sorted(replies.values(), key=lambda c: c.ip)
    ]


def decode_dante_model(raw: bytes | None) -> str | None:
    """Decode a Dante model property.

    Some devices hex-encode their model behind a leading underscore; a Wing
    Dante card reports "_77696e6736342042", which is "wing64 B".
    """
    if not raw:
        return None
    text = raw.decode("ascii", errors="replace")
    if text.startswith("_"):
        try:
            return bytes.fromhex(text[1:]).decode("ascii")
        except ValueError:
            pass
    return text


def mac_from_dante_id(raw: bytes | None) -> str | None:
    """Recover a MAC address from a Dante device id.

    The id is the MAC with "fffe" inserted in the middle; for example
    001dc1fffe9f77fa is the MAC 00:1d:c1:9f:77:fa.
    """
    if not raw:
        return None
    text = raw.decode("ascii", errors="replace").lower()
    if len(text) != 16 or text[6:10] != "fffe":
        return None
    digits = text[:6] + text[10:]
    try:
        int(digits, 16)
    except ValueError:
        return None
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


async def discover_dante_devices(timeout: float = 3.0) -> list[DiscoveredDanteDevice]:
    """Browse mDNS and return every Wing Dante card that answers.

    Other Dante devices are ignored.
    """
    names: set[str] = set()

    def on_change(
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        if state_change is not ServiceStateChange.Removed:
            names.add(name)

    aiozc = AsyncZeroconf()
    try:
        browser = AsyncServiceBrowser(aiozc.zeroconf, [DANTE_SERVICE], handlers=[on_change])
        await asyncio.sleep(timeout)
        await browser.async_cancel()

        infos = [AsyncServiceInfo(DANTE_SERVICE, name) for name in sorted(names)]
        await asyncio.gather(*(info.async_request(aiozc.zeroconf, 2000) for info in infos))
    finally:
        await aiozc.async_close()

    devices = []
    for info in infos:
        properties = info.properties or {}
        model = decode_dante_model(properties.get(b"model"))
        if model != WING_DANTE_MODEL:
            log.debug("Ignoring Dante device %s with model %r", info.name, model)
            continue

        addresses = info.parsed_addresses(IPVersion.V4Only)
        devices.append(
            DiscoveredDanteDevice(
                name=info.name.removesuffix("." + DANTE_SERVICE),
                ip=addresses[0] if addresses else None,
                mac=mac_from_dante_id(properties.get(b"id")),
                model=model,
            )
        )
    return devices


async def discover_all() -> tuple[list[DiscoveredConsole], list[DiscoveredDanteDevice]]:
    """Discover consoles and Dante cards at the same time."""
    consoles, dante_devices = await asyncio.gather(
        discover_consoles(), discover_dante_devices()
    )
    return consoles, dante_devices
