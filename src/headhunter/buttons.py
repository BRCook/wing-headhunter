"""Headhunter's custom control buttons: GPIO states on Console A.

Usage (test): python -m headhunter.buttons <display name> <gpio 1-4> <on|off> [--wait N]

Flashes a GPIO button with the phantom power confirmation pattern, then puts
it back the way it was, printing everything the console reports meanwhile.

A GPIO's state drives the GPIO jack on the back of the console, so only flash
GPIOs that have nothing connected to them.
"""

import argparse
import asyncio
import contextlib

from headhunter.native import NativeClient, wing_hash
from headhunter.probe import find_ip

# (seconds dark, seconds lit) for each flash. Each flash starts dark, because
# the button is already lit under the user's finger; the pattern always ends dark.
FLASH_PATTERNS = {
    "on": [(0.18, 0.18)] * 5,  # Five quick flashes: phantom power is now on
    "off": [(0.4, 0.7)] * 3,  # Three slow flashes: phantom power is now off
}


def gpio_path(gpio: int) -> str:
    if not 1 <= gpio <= 4:
        raise ValueError("GPIO must be between 1 and 4")
    return f"$ctl.gpio.{gpio}.gpstate"


async def flash(client: NativeClient, gpio: int, pattern: str) -> None:
    """Flash a momentary GPIO button, leaving it dark.

    The button is usually still held while it flashes, so its state at the
    start says nothing about its state at the end. A momentary button rests
    dark, and the console still reports the release when the finger lifts, so
    ending dark is always safe; restoring the starting state could leave it
    stuck lit if the button was released mid-flash.
    """
    path = gpio_path(gpio)
    try:
        for dark, lit in FLASH_PATTERNS[pattern]:
            client.set(path, 0)
            await asyncio.sleep(dark)
            client.set(path, 1)
            await asyncio.sleep(lit)
    finally:
        client.set(path, 0)


async def run(display_name: str, gpio: int, pattern: str, wait: float) -> None:
    ip = await find_ip(display_name)
    path = gpio_path(gpio)

    async with NativeClient(ip) as client:
        if await client.get("$syscfg.tcplock"):
            raise SystemExit(
                f"{display_name} has TCP remote lock on; it will refuse changes.")
        print(f"{path} before: {await client.get(path)}")

        async def report() -> None:
            loop = asyncio.get_running_loop()
            start = loop.time()
            async for event in client.events():
                if event.hash == wing_hash(path):
                    print(
                        f"  {loop.time() - start:5.2f}s  console reports {path} = {event.value}")

        listener = asyncio.create_task(report())
        if wait > 0:
            print(
                f"Press and hold the button now; flashing in {wait:g} seconds. Keep holding until it finishes.")
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
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("display_name",
                        help="A paired console's display name, e.g. COMPACT")
    parser.add_argument("gpio", type=int, help="GPIO number, 1 to 4 (A to D)")
    parser.add_argument("pattern", choices=FLASH_PATTERNS,
                        help="Which confirmation to flash")
    parser.add_argument("--wait", type=float, default=0,
                        help="Seconds to wait before flashing, so you can press and hold the button first")
    args = parser.parse_args()
    asyncio.run(run(args.display_name, args.gpio, args.pattern, args.wait))


if __name__ == "__main__":
    main()