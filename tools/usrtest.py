"""List a console's user signals and patches, and which Dante outputs carry them. Read-only.

Usage: python tools/usrtest.py <display name>
"""

import argparse
import asyncio

from headhunter.consoles import find_ip
from headhunter.native import NativeClient


async def maybe(client: NativeClient, path: str):
    try:
        return await client.get(path, timeout=1.0)
    except TimeoutError:
        return None


async def run(display_name: str) -> None:
    ip = await find_ip(display_name)
    async with NativeClient(ip) as client:
        print("Entry  Kind    Mode  Source        Name")
        for n in range(1, 57):
            base = f"io.in.USR.{n}"
            mode, grp, number, name = await asyncio.gather(
                maybe(client, f"{base}.mode"), maybe(client, f"{base}.user.grp"),
                maybe(client, f"{base}.user.in"), maybe(client, f"{base}.name"),
            )
            if grp in (None, "OFF") and not name:
                continue  # Unused
            kind = "signal" if n <= 24 else "patch"
            print(f"{n:5}  {kind:6}  {mode!s:4}  {grp!s:>4} {number!s:<8} {name!r}")

        print("\nDante outputs carrying user signals or patches:")
        for n in range(1, 65):
            grp, number = await asyncio.gather(maybe(client, f"io.out.MOD.{n}.grp"), maybe(client, f"io.out.MOD.{n}.in"))
            if grp == "USR":
                print(f"  Dante output {n:2}: USR {number}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("display_name", help="A paired console's display name, e.g. RACK")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.display_name))
    except LookupError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
