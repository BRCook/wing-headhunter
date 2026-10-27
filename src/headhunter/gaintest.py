"""Test remote preamp gain: change one input's gain in small steps and put it back.

Usage: python -m headhunter.gaintest <display name> <group> <input>

For example, "python -m headhunter.gaintest RACK A 24" tests AES50 A input 24.

Shows the preamp's type and range, then sets the gain to a few offsets from
its current value, reading it back each time to see what the console actually
accepted. Gain is always put back afterwards, even if something goes wrong.

This changes real preamp gain, so use an input with nothing plugged into it.
Phantom power is never touched.
"""

import argparse
import asyncio

from headhunter.native import NativeClient, wing_hash
from headhunter.probe import find_ip

# Offsets from the current gain, in dB. 0.3 is deliberately not a whole step,
# to see whether the console rounds it.
OFFSETS = [0.5, 1.0, 0.3, 2.5]
GROUPS = ["LCL", "A", "B", "C"]


async def run(display_name: str, group: str, number: int) -> None:
    ip = await find_ip(display_name)
    path = f"io.in.{group}.{number}.g"

    async with NativeClient(ip) as client:
        if await client.get("$syscfg.tcplock"):
            raise SystemExit(
                f"{display_name} has TCP remote lock on; it will refuse changes.")

        definition = await client.define(wing_hash(path))
        original = await client.get(path)
        name = await client.get(f"io.in.{group}.{number}.name")
        preamp_type = await client.get(f"io.in.{group}.{number}.$ha")
        phantom = await client.get(f"io.in.{group}.{number}.vph")
        print(
            f"{display_name} {group}{number}: name {name!r}, preamp type {preamp_type}, phantom {'on' if phantom else 'off'}")
        print(f"  gain {original:.2f} dB; range {definition.describe()}\n")

        answer = input(
            f"This will change the gain of {group}{number} on {display_name}. Is nothing plugged in? [y/N] ")
        if answer.strip().lower() != "y":
            raise SystemExit("Cancelled; nothing was changed.")

        loop = asyncio.get_running_loop()
        start = loop.time()

        async def watch() -> None:
            async for event in client.events():
                if event.hash == wing_hash(path):
                    print(
                        f"  {loop.time() - start:5.2f}s  console reports {event.value:.2f}")

        watcher = asyncio.create_task(watch())
        try:
            for offset in OFFSETS:
                # Go down instead of up if up would leave the preamp's range
                target = original + offset
                if target > definition.maximum:
                    target = original - offset
                target = round(target, 2)
                client.set(path, float(target))
                await asyncio.sleep(0.5)
                print(
                    f"  {loop.time() - start:5.2f}s  set {target:6.2f}, reads back {await client.get(path):6.2f}")
        finally:
            client.set(path, float(original))
            await asyncio.sleep(0.5)
            final = await client.get(path)
            watcher.cancel()

        status = "restored" if abs(
            final - original) < 0.01 else "NOT RESTORED, please check the console"
        print(f"\nGain {status} ({final:.2f} dB).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("display_name",
                        help="A paired console's display name, e.g. RACK")
    parser.add_argument("group", choices=GROUPS,
                        help="Preamp group: LCL, A, B or C")
    parser.add_argument("input", type=int, help="Input number")
    args = parser.parse_args()
    asyncio.run(run(args.display_name, args.group, args.input))


if __name__ == "__main__":
    main()