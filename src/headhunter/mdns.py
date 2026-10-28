"""Advertising Headhunter on the network, so its page can be found at headhunter.local."""

import ipaddress
import logging

import ifaddr
from zeroconf import IPVersion, NonUniqueNameException
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

log = logging.getLogger(__name__)

SERVICE_TYPE = "_http._tcp.local."
ATTEMPTS = 9  # headhunter, then headhunter-2 to headhunter-9


def local_addresses() -> list[str]:
    """This machine's IPv4 addresses, other than loopback and link-local ones."""
    found = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if not isinstance(ip.ip, str):
                continue  # IPv6
            address = ipaddress.ip_address(ip.ip)
            if not (address.is_loopback or address.is_link_local) and ip.ip not in found:
                found.append(ip.ip)
    return found


def candidate_names(name: str) -> list[str]:
    return [name] + [f"{name}-{n}" for n in range(2, ATTEMPTS + 1)]


class Advertiser:
    """Announces a host name and a web service for Headhunter's page."""

    def __init__(self, name: str, port: int) -> None:
        self.name = name
        self.port = port
        self.host: str | None = None  # The name actually in use, e.g. headhunter.local
        self._zeroconf: AsyncZeroconf | None = None
        self._info: AsyncServiceInfo | None = None

    async def start(self) -> None:
        addresses = local_addresses()
        if not addresses:
            log.warning("No network address to advertise the status page on")
            return
        self._zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only)
        for name in candidate_names(self.name):
            info = AsyncServiceInfo(
                SERVICE_TYPE,
                f"Headhunter ({name}).{SERVICE_TYPE}",
                port=self.port,
                server=f"{name}.local.",
                parsed_addresses=addresses,
                properties={"path": "/"},
            )
            try:
                await self._zeroconf.async_register_service(info)
            except NonUniqueNameException:
                continue  # Someone else has this name; try the next
            self._info = info
            self.host = f"{name}.local"
            return
        log.warning("Could not advertise the status page; every name from %s to %s is taken",
                    self.name, candidate_names(self.name)[-1])

    async def stop(self) -> None:
        if self._zeroconf is None:
            return
        if self._info is not None:
            await self._zeroconf.async_unregister_service(self._info)
        await self._zeroconf.async_close()
