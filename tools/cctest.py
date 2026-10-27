"""Read, and optionally set up, one custom control button on a console.

Usage: python tools/cctest.py <display name>
       python tools/cctest.py <display name> --tree PATH
       python tools/cctest.py <display name> --find PATH
       python tools/cctest.py <display name> [--page N] --button N [--lower]
       python tools/cctest.py <display name> [--page N] --button N [--lower] --set C [--push] [--name TEXT]

With just a console's name, every custom control page and button is searched, and
every button with a function is listed; buttons set to a GPIO are shown in full.
Nothing is changed.

With --tree, everything under a path (such as $ctl.user.5) is listed, a few levels
deep, with values. Nothing is changed.

With --find, everything under a path is searched, however deep, and only settings
that are not blank (OFF, an empty name, or 0) are listed. Nothing is changed.

With --button, every setting of that one button is listed, as the console reports
it. --page defaults to the custom control page showing right now.

With --set, the button is given the GPIO function on that GPIO (A to D), as a toggle,
or momentary with --push, and optionally a name; a leading | shows the name inverted.
Everything is read back, and you are asked whether to keep the change; if not, the
original settings are put back.
"""

import argparse
import asyncio

from headhunter.consoles import find_ip
from headhunter.native import NativeClient, wing_hash


async def maybe(client: NativeClient, path: str):
    try:
        return await client.get(path, timeout=1.0)
    except TimeoutError:
        return None


async def addresses(client: NativeClient, base: str) -> dict[str, int]:
    """Each setting of a button and the console's address for it. A button's function settings
    (such as gpio) are only reachable at these addresses, not ones calculated from their names."""
    try:
        return {child.key: child.hash for child in await client.children(base)}
    except TimeoutError:
        return {}


async def show(client: NativeClient, base: str) -> dict:
    """List every setting of a button, and return their values."""
    values = {}
    try:
        children = await client.children(base)
    except TimeoutError:
        children = []
    if not children:
        print(f"  {base} reports no settings; check the page and button numbers")
        return values
    for child in children:
        try:
            values[child.key] = await client.get_hash(child.hash, timeout=1.0)
        except TimeoutError:
            values[child.key] = None
        print(f"  {child.key:<10} {child.long_name:<26} {values[child.key]!r:<16} {child.describe()}")
    return values


# Every group of custom control buttons in the protocol document: each group has buttons 1-4,
# each with an upper (bu) and lower (bd) row
BUTTON_GROUPS = [*(str(page) for page in range(1, 17)), "gpio", "user", "daw1", "daw2", "daw3", "daw4"]


async def survey(client: NativeClient) -> None:
    """List every custom control button with a function, in full for GPIO buttons."""
    blank = None
    found = 0
    for group in BUTTON_GROUPS:
        for button in range(1, 5):
            for row, row_name in (("bu", "upper"), ("bd", "lower")):
                base = f"$ctl.user.{group}.{button}.{row}"
                mode = await maybe(client, f"{base}.mode")
                if mode in (None, "OFF"):
                    if mode == "OFF":
                        blank = blank or base
                    continue
                found += 1
                name = await maybe(client, f"{base}.name")
                print(f"{base}: {mode} {name!r}")
                if mode == "GPIO":
                    await show(client, base)
    if not found:
        print("No custom control buttons have a function assigned.")
    if blank:
        print(f"\nA blank button, for comparison ({blank}):")
        await show(client, blank)


async def tree(client: NativeClient, path: str, depth: int = 0, max_depth: int = 3) -> None:
    """List everything under a path, with values, down to max_depth levels."""
    try:
        children = await client.children(path)
    except TimeoutError:
        print(f"{'  ' * depth}{path}: no answer")
        return
    for child in children:
        child_path = f"{path}.{child.key}"
        if child.type == "node":
            print(f"{'  ' * depth}{child.key}/  {child.long_name}")
            if depth + 1 < max_depth:
                await tree(client, child_path, depth + 1, max_depth)
        else:
            value = await maybe(client, child_path)
            print(f"{'  ' * depth}{child.key} = {value!r}  ({child.long_name})")


BLANK = (None, "", "OFF", 0)


async def find(client: NativeClient, path: str) -> int:
    """Print every setting under a path that is not blank, however deep. Returns how many."""
    try:
        children = await client.children(path)
    except TimeoutError:
        return 0
    found = 0
    for child in children:
        child_path = f"{path}.{child.key}"
        if child.type == "node":
            found += await find(client, child_path)
            continue
        value = await maybe(client, child_path)
        if value not in BLANK:
            print(f"{child_path} = {value!r}  ({child.long_name})")
            found += 1
    return found


async def run(args: argparse.Namespace) -> None:
    ip = await find_ip(args.display_name)
    async with NativeClient(ip) as client:
        if args.find:
            print(f"Searching everything under {args.find}...")
            found = await find(client, args.find)
            print(f"{found} settings that are not blank.")
            return
        if args.tree:
            print(f"{args.tree}:")
            await tree(client, args.tree, 1, args.depth + 1)
            return
        if args.button is None:
            await survey(client)
            return
        page = args.page
        if page is None:
            selected = await maybe(client, "$ctl.user.sel")
            page = selected + 1 if isinstance(selected, int) else 1  # Reported as 0-15
        row = "bd" if args.lower else "bu"
        base = f"$ctl.user.{page}.{args.button}.{row}"
        print(f"Custom control page {page}, button {args.button}, {'lower' if args.lower else 'upper'} row ({base}):")
        original = await show(client, base)
        if not args.set:
            return

        if await client.get("$syscfg.tcplock"):
            raise SystemExit(f"{args.display_name} has TCP remote lock on; it will refuse changes.")
        gpio = args.set.upper() + ("-P" if args.push else "")
        changes = {"mode": "GPIO", "gpio": gpio}
        if args.name is not None:
            changes["name"] = args.name
        if input(f"\nSet this button to {changes}? [y/N] ").strip().lower() != "y":
            return
        # The function first: the button's other settings depend on it
        client.set(f"{base}.mode", "GPIO")
        await asyncio.sleep(0.5)
        reachable = await addresses(client, base)
        if "gpio" not in reachable:
            raise SystemExit("The button has no gpio setting after choosing GPIO; nothing else was changed.")
        client.set_hash(reachable["gpio"], gpio)
        if args.name is not None:
            client.set(f"{base}.name", args.name)
        await asyncio.sleep(0.5)
        print("\nAfter the change:")
        await show(client, base)

        if input("\nKeep this? [Y/n] ").strip().lower() == "n":
            # The function first, then whatever settings it has
            client.set(f"{base}.mode", original.get("mode", "OFF"))
            await asyncio.sleep(0.5)
            reachable = await addresses(client, base)
            for name, value in original.items():
                if name not in ("mode",) and not name.startswith("$") and value is not None and name in reachable:
                    client.set_hash(reachable[name], value)
            await asyncio.sleep(0.5)
            print("\nPut back:")
            await show(client, base)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("display_name", help="A paired console's display name, e.g. COMPACT")
    parser.add_argument("--find", metavar="PATH", help="List every setting under PATH that is not blank, e.g. $ctl.user")
    parser.add_argument("--tree", metavar="PATH", help="List everything under PATH, e.g. $ctl.user.5")
    parser.add_argument("--depth", type=int, default=3, help="With --tree: how many levels to list (default 3)")
    parser.add_argument("--page", help="Custom control page: U1 to U4 on the Compact, 1 to 16 on the full-size Wing (default: the one showing)")
    parser.add_argument("--button", type=int, help="Button, 1 to 4 (without it, every button is searched)")
    parser.add_argument("--lower", action="store_true", help="The lower row of buttons, rather than the upper")
    parser.add_argument("--set", metavar="GPIO", help="Give the button the GPIO function on this GPIO, A to D")
    parser.add_argument("--push", action="store_true", help="With --set: momentary rather than toggle")
    parser.add_argument("--name", help="With --set: the button's name")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except LookupError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
