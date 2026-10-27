"""Read-only exploration of a paired console over the native protocol.

Usage: python tools/probe.py <display name> [--seconds N]

Reads a handful of parameters Headhunter will depend on, then prints every
change the console reports for N seconds (default 60). Parameters we already
know are shown by path; anything else is looked up by asking the console for
its definition. This tool never writes anything to the console.
"""

import argparse
import asyncio
import contextlib
import time

from headhunter.consoles import find_ip
from headhunter.native import NativeClient, Value, wing_hash

STARTUP_READS = [
    "$stat.A.stat",
    "$stat.A.dev",
    "$stat.B.stat",
    "$stat.B.dev",
    "$stat.C.stat",
    "$stat.C.dev",
    "io.in.A.8.g",
    "io.in.A.8.vph",
    "io.in.A.8.$ha",
    "$syscfg.consolename",
    "$syscfg.$cnsmdl",
    "$syscfg.$firmware",
    "$syscfg.eth_cfg",
    "$syscfg.opt_mod",
    "$syscfg.tcplock",
    "io.in.LCL.1.g",
    "io.in.LCL.1.vph",
    "io.in.LCL.1.$ha",
    "io.in.LCL.1.name",
    "io.in.A.1.g",
    "io.in.A.1.vph",
    "io.in.A.1.$ha",
    "io.in.A.1.name",
    "io.out.MOD.1.grp",
    "io.out.MOD.1.in",
    "io.out.MOD.2.grp",
    "io.out.MOD.2.in",
    "io.in.MOD.1.name",
    "ch.1.in.set.trim",
    "ch.1.in.set.altsrc",
    "ch.1.in.conn.grp",
    "ch.1.in.conn.in",
    "ch.1.clink",
    "ch.1.name",
    "$ctl.$stat.selidx",
    "$ctl.gpio.1.mode",
    "$ctl.gpio.2.mode",
    "$ctl.gpio.3.mode",
    "$ctl.gpio.4.mode",
    "$ctl.gpio.1.gpstate",
    "$ctl.gpio.2.gpstate",
    "$ctl.gpio.3.gpstate",
    "$ctl.gpio.4.gpstate",
]

# Parameters whose type and range we want to see
DEFINITIONS = [
    "ch.1.clink",
    "ch.1.in.set.trim",
    "io.in.LCL.1.g",
    "io.in.A.1.g",
    "io.in.B.1.g",
    "io.in.C.1.g",
    "io.out.MOD.1.grp",
    "$ctl.gpio.1.mode",
]

# Nodes whose children we want to list
CHILDREN = ["$syscfg"]


def known_paths() -> dict[int, str]:
    """Hashes of parameters we expect to see, so events can be shown by path."""
    paths = list(STARTUP_READS)
    for n in range(1, 41):
        for leaf in ("in.set.trim", "in.set.altsrc", "in.conn.grp", "in.conn.in",
                     "in.conn.altgrp", "in.conn.altin", "name", "mute", "fdr", "$fdr", "$mute",
                     "clink", "col", "icon"):
            paths.append(f"ch.{n}.{leaf}")
    for group, count in (("LCL", 24), ("A", 48), ("B", 48), ("C", 48)):
        for n in range(1, count + 1):
            for leaf in ("g", "vph", "name", "mute"):
                paths.append(f"io.in.{group}.{n}.{leaf}")
    for n in range(1, 65):
        paths += [f"io.in.MOD.{n}.name", f"io.out.MOD.{n}.grp", f"io.out.MOD.{n}.in"]
    return {wing_hash(path): path for path in paths}


def show(value: Value) -> str:
    return f"{value:.2f}" if isinstance(value, float) else repr(value)


async def run(display_name: str, seconds: float) -> None:
    ip = await find_ip(display_name)
    print(f"Connecting to {display_name} at {ip}...\n")
    names = known_paths()
    looked_up: dict[int, str] = {}

    async with NativeClient(ip) as client:
        width = max(len(path) for path in STARTUP_READS)
        for path in STARTUP_READS:
            try:
                value = show(await client.get(path))
            except TimeoutError:
                value = "(no reply)"
            print(f"  {path:<{width}}  {value}")

        print("\nDefinitions:")
        for path in DEFINITIONS:
            try:
                description = (await client.define(wing_hash(path))).describe()
            except TimeoutError:
                description = "(no reply)"
            print(f"  {path:<{width}}  {description}")

        for path in CHILDREN:
            print(f"\nChildren of {path}:")
            try:
                for child in await client.children(path):
                    print(f"  {child.name:<16} {child.long_name:<24} {child.describe()}")
            except TimeoutError:
                print("  (no reply)")

        if seconds <= 0:
            return
        print(f"\nListening for {seconds:.0f} seconds; make changes on the console now.\n")
        start = time.monotonic()

        async def listen() -> None:
            async for event in client.events():
                name = names.get(event.hash) or looked_up.get(event.hash)
                if name is None:
                    try:
                        definition = await client.define(event.hash)
                        name = (f"#{event.hash:08x} {definition.name} "
                                f"({definition.long_name}), parent #{definition.parent:08x}")
                    except TimeoutError:
                        name = f"#{event.hash:08x} (unknown)"
                    looked_up[event.hash] = name
                print(f"  {time.monotonic() - start:6.2f}s  {name} = {show(event.value)}")

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(listen(), seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("display_name", help="A paired console's display name, e.g. RACK")
    parser.add_argument("--seconds", type=float, default=60, help="How long to listen (0 to skip)")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.display_name, args.seconds))
    except LookupError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
