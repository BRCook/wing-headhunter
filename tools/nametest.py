"""Try different padding characters in a channel name, to see what the scribble strip does with them.

Usage: python tools/nametest.py <display name> <channel> [name ...] [--seconds N]

Writes each name given (or, without any, a built-in list of padding
candidates) to the channel's name in turn, pausing so you can look at
the scribble strip, and reads each one back to see what the console stored.
The channel's real name is put back at the end.
"""

import argparse
import asyncio

from headhunter.consoles import find_ip
from headhunter.native import NativeClient

CANDIDATES = [
    ("normal space", " 2.5dB"),
    ("non-breaking space (U+00A0)", "\u00a02.5dB"),
    ("figure space (U+2007)", "\u20072.5dB"),
    ("narrow non-breaking space (U+202F)", "\u202f2.5dB"),
    ("leading zero", "02.5dB"),
    ("plus sign", "+2.5dB"),
    ("underscore", "_2.5dB"),
    ("for comparison", "27.5dB"),
]


async def run(display_name: str, channel: int, seconds: float, names: list[str] | None = None) -> None:
    candidates = [("as given", name) for name in names] if names else CANDIDATES
    ip = await find_ip(display_name)
    path = f"ch.{channel}.name"
    async with NativeClient(ip) as client:
        if await client.get("$syscfg.tcplock"):
            raise SystemExit(f"{display_name} has TCP remote lock on; it will refuse changes.")
        original = await client.get(path)
        print(f"Channel {channel} is named {original!r}; trying {len(candidates)} names, {seconds:g} seconds each.\n")
        try:
            for number, (description, text) in enumerate(candidates, start=1):
                client.set(path, text)
                await asyncio.sleep(0.3)
                stored = await client.get(path)
                verdict = "kept" if stored == text else f"STORED AS {stored!r}"
                print(f"  {number}. {description:<36} {text!r:<16} {verdict}")
                await asyncio.sleep(seconds)
        finally:
            client.set(path, original)
            await client.flush()
        print(f"\nChannel {channel} is named {original!r} again.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("display_name", help="A paired console's display name, e.g. COMPACT")
    parser.add_argument("channel", type=int, help="Channel number, 1 to 40")
    parser.add_argument("names", nargs="*", help="Names to try; without any, a built-in list of padding candidates")
    parser.add_argument("--seconds", type=float, default=4, help="How long to show each name (default 4)")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.display_name, args.channel, args.seconds, args.names))
    except LookupError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()