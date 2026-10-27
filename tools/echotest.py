"""Find out whether the console reports Headhunter's own trim changes back to it.

Usage: python tools/echotest.py <display name> <channel> [--delta DB]

Opens two connections to the console: a writer, which nudges the channel's
trim by --delta dB (default 1.0) and then puts it back, and an observer, which
only listens, like a second app would. Prints every trim report each connection
receives, and reads the trim back after each change to confirm it took effect.

This changes the channel's trim for about a second, so use a channel with
nothing on it.
"""

import argparse
import asyncio
import contextlib

from headhunter.native import NativeClient, wing_hash
from headhunter.consoles import find_ip

TRIM_MIN, TRIM_MAX = -18.0, 18.0


async def run(display_name: str, channel: int, delta: float) -> None:
    if not 1 <= channel <= 40:
        raise SystemExit("Channel must be between 1 and 40")
    ip = await find_ip(display_name)
    path = f"ch.{channel}.in.set.trim"
    target_hash = wing_hash(path)
    loop = asyncio.get_running_loop()
    start = loop.time()

    async with NativeClient(ip) as writer, NativeClient(ip) as observer:
        if await writer.get("$syscfg.tcplock"):
            raise SystemExit(f"{display_name} has TCP remote lock on; it will refuse changes.")

        async def watch(name: str, client: NativeClient) -> None:
            async for event in client.events():
                if event.hash == target_hash:
                    print(f"  {loop.time() - start:5.2f}s  {name} received {path} = {event.value:.2f}")

        watchers = [
            asyncio.create_task(watch("writer  ", writer)),
            asyncio.create_task(watch("observer", observer)),
        ]

        # A connection may only start receiving reports once it has sent the
        # console something, so have the observer read the trim once first
        await observer.get(path)

        original = await writer.get(path)
        target = original + delta if original + delta <= TRIM_MAX else original - delta
        target = round(max(TRIM_MIN, min(TRIM_MAX, target)), 1)
        print(f"{path} is {original:.2f}; changing it to {target:.2f} and back.\n")

        print(f"  {loop.time() - start:5.2f}s  writer sets {target:.2f}")
        writer.set(path, float(target))
        await asyncio.sleep(1)
        print(f"  {loop.time() - start:5.2f}s  writer reads back {await writer.get(path):.2f}")

        print(f"  {loop.time() - start:5.2f}s  writer sets {original:.2f}")
        writer.set(path, float(original))
        await asyncio.sleep(1)
        final = await writer.get(path)
        print(f"  {loop.time() - start:5.2f}s  writer reads back {final:.2f}")

        for task in watchers:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    status = "restored" if abs(final - original) < 0.05 else "NOT RESTORED, please check the console"
    print(f"\nTrim {status}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("display_name", help="A paired console's display name, e.g. COMPACT")
    parser.add_argument("channel", type=int, help="Channel number, 1 to 40; use one with nothing on it")
    parser.add_argument("--delta", type=float, default=1.0, help="How far to nudge the trim, in dB")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.display_name, args.channel, args.delta))
    except LookupError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
