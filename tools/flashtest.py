"""Flash a GPIO button with a phantom power confirmation pattern.

Usage: python tools/flashtest.py <display name> <gpio 1-4> <on|off> [--wait N]

Prints everything the console reports for that GPIO meanwhile. Use --wait to
get your finger on the button before it starts flashing.

A GPIO's state drives the GPIO jack on the back of the console, so only flash
GPIOs that have nothing connected to them.
"""

import argparse
import asyncio
import contextlib

from headhunter.buttons import FLASH_PATTERNS, flash, gpio_path
from headhunter.consoles import find_ip
from headhunter.native import NativeClient, wing_hash


async def run(display_name: str, gpio: int, pattern: str, wait: float) -> None:
    ip = await find_ip(display_name)
    path = gpio_path(gpio)

    async with NativeClient(ip) as client:
        if await client.get("$syscfg.tcplock"):
            raise SystemExit(f"{display_name} has TCP remote lock on; it will refuse changes.")
        print(f"{path} before: {await client.get(path)}")

        async def report() -> None:
            loop = asyncio.get_running_loop()
            start = loop.time()
            async for event in client.events():
                if event.hash == wing_hash(path):
                    print(f"  {loop.time() - start:5.2f}s  console reports {path} = {event.value}")

        listener = asyncio.create_task(report())
        if wait > 0:
            print(f"Press and hold the button now; flashing in {wait:g} seconds. Keep holding until it finishes.")
            await asyncio.sleep(wait)
        print(f"Flashing GPIO {gpio} with the '{pattern}' pattern...")
        await flash(client, gpio, pattern)
        print("Done flashing; release the button now if you are holding it.")
        await asyncio.sleep(3)  # Catch the release, and any late reports
        listener.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener
        print(f"{path} after:  {await client.get(path)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("display_name", help="A paired console's display name, e.g. COMPACT")
    parser.add_argument("gpio", type=int, help="GPIO number, 1 to 4 (A to D)")
    parser.add_argument("pattern", choices=FLASH_PATTERNS, help="Which confirmation to flash")
    parser.add_argument("--wait", type=float, default=0, help="Seconds to wait before flashing")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.display_name, args.gpio, args.pattern, args.wait))
    except LookupError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
