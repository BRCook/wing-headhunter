"""Advertising the status page."""

import asyncio

from zeroconf import NonUniqueNameException

from headhunter import mdns


def test_local_addresses_leave_out_loopback(monkeypatch):
    class IP:
        def __init__(self, ip):
            self.ip = ip

    class Adapter:
        def __init__(self, *ips):
            self.ips = [IP(ip) for ip in ips]

    monkeypatch.setattr(mdns.ifaddr, "get_adapters", lambda: [
        Adapter("127.0.0.1", ("::1", 0, 0)), Adapter("10.10.100.101", "169.254.3.4"), Adapter("192.168.1.5"),
    ])
    assert mdns.local_addresses() == ["10.10.100.101", "192.168.1.5"]


def test_a_taken_name_falls_back_to_the_next(monkeypatch):
    registered = []

    class FakeZeroconf:
        def __init__(self, **kwargs):
            pass

        async def async_register_service(self, info):
            if info.server == "headhunter.local.":
                raise NonUniqueNameException
            registered.append(info)

        async def async_unregister_service(self, info):
            registered.remove(info)

        async def async_close(self):
            pass

    monkeypatch.setattr(mdns, "AsyncZeroconf", FakeZeroconf)
    monkeypatch.setattr(mdns, "local_addresses", lambda: ["10.10.100.101"])

    async def scenario():
        advertiser = mdns.Advertiser("headhunter", 8048)
        await advertiser.start()
        assert advertiser.host == "headhunter-2.local"
        assert registered[0].port == 8048
        await advertiser.stop()
        assert registered == []

    asyncio.run(scenario())
