"""Find the strips on the Compact's current layer, and try flashing their lights.

Usage: python tools/striptest.py <display name> [--channel N]

1. Reads which layer is showing, and what is on it.
2. Flashes the scribble light of every channel on that layer three times.
3. Flashes one channel's colour red three times (--channel, default the selected one).

Everything is put back as it was afterwards.
"""

import argparse
import asyncio

from headhunter.consoles import find_ip
from headhunter.gain import strip_from_selidx
from headhunter.native import NativeClient

RED = 9  # From the colour chart in Behringer's protocol document
POSITIONS = 24  # Positions per layer, according to the protocol document


async def maybe(client: NativeClient, path: str):
    try:
        return await client.get(path, timeout=1.0)
    except TimeoutError:
        return None


async def run(display_name: str, channel: int | None) -> None:
    ip = await find_ip(display_name)
    async with NativeClient(ip) as client:
        if await client.get("$syscfg.tcplock"):
            raise SystemExit(f"{display_name} has TCP remote lock on; it will refuse changes.")

        layer = await client.get("$ctl.layer.CMPCT.sel")
        base = f"$ctl.layer.CMPCT.{layer}"
        offset = await maybe(client, f"{base}.ofs")
        name = await maybe(client, f"{base}.name")
        print(f"Layer {layer} ({name!r}), offset {offset}:")
        strips = []
        for position in range(1, POSITIONS + 1):
            kind = await maybe(client, f"{base}.{position}.type")
            index = await maybe(client, f"{base}.{position}.i")
            print(f"  position {position:2}: {kind} {index}")
            if kind == "CH" and isinstance(index, int):
                strip = strip_from_selidx(index)
                if strip is not None:
                    strips.append(strip)

        print(f"\nFlashing the scribble lights of {', '.join(s.label for s in strips) or 'nothing'}...")
        led_paths = [f"{s.kind}.{s.number}.led" for s in strips]
        original_leds = {path: await client.get(path) for path in led_paths}
        try:
            for _ in range(3):
                for path in led_paths:
                    client.set(path, 0)
                await asyncio.sleep(0.25)
                for path in led_paths:
                    client.set(path, 1)
                await asyncio.sleep(0.25)
        finally:
            for path, value in original_leds.items():
                client.set(path, value)
            await client.flush()

        if channel is None:
            selected = strip_from_selidx(await client.get("$ctl.$stat.selidx"))
            target = f"{selected.kind}.{selected.number}" if selected else "ch.1"
        else:
            target = f"ch.{channel}"
        colour_path = f"{target}.col"
        original_colour = await client.get(colour_path)
        print(f"Flashing {target} red three times (its colour is {original_colour})...")
        try:
            for _ in range(3):
                client.set(colour_path, RED)
                await asyncio.sleep(0.3)
                client.set(colour_path, original_colour)
                await asyncio.sleep(0.3)
        finally:
            client.set(colour_path, original_colour)
            await client.flush()
        print("Done; everything is back as it was.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("display_name", help="A paired console's display name, e.g. COMPACT")
    parser.add_argument("--channel", type=int, help="Channel to flash red (default: the selected one)")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.display_name, args.channel))
    except LookupError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()