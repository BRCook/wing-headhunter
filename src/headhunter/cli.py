"""The headhunter command.

    headhunter discover               List every Wing console and Wing Dante card found
    headhunter pair                   Pair consoles with their Dante cards
    headhunter role <name> <FOH|STAGE>  Mark a paired console as FOH or stage
    headhunter dante                  Show what each paired console receives over Dante
    headhunter names [--write]        Name Dante sources on FOH consoles (dry run without --write)
    headhunter buttons <name> --gain N [--phantom N]  Choose a FOH console's GPIO buttons
    headhunter rename <name> [--name NEW] [--dante NEW]  Change a console's Headhunter or Dante name
    headhunter run [--write-names] [--write-gain]  Keep running: keep names up to date, and run gain mode
"""

import argparse
import asyncio
import contextlib
import logging

from headhunter import __version__
from headhunter.dante import NetaudioError, read_subscriptions
from headhunter.discovery import discover_all
from headhunter.engine import run as run_engine
from headhunter.naming import run as run_naming
from headhunter.pairing import run as run_pairing
from headhunter.pairing import set_role
from headhunter.store import Store


def discover(args: argparse.Namespace) -> None:
    consoles, dante_devices = asyncio.run(discover_all())

    print("Consoles:")
    if not consoles:
        print("  (none found)")
    for console in consoles:
        print(f"  {console.name}")
        print(f"    ip:       {console.ip}")
        print(f"    mac:      {console.mac or '(unknown)'}")
        print(f"    model:    {console.model}")
        print(f"    serial:   {console.serial}")
        print(f"    firmware: {console.firmware}")

    print("\nWing Dante cards:")
    if not dante_devices:
        print("  (none found)")
    for device in dante_devices:
        print(f"  {device.name}")
        print(f"    ip:       {device.ip or '(unknown)'}")
        print(f"    mac:      {device.mac or '(unknown)'}")
        print(f"    model:    {device.model}")


def pair(args: argparse.Namespace) -> None:
    with Store() as store:
        run_pairing(store)


def role(args: argparse.Namespace) -> None:
    with Store() as store:
        set_role(store, args.display_name, args.role)


def dante(args: argparse.Namespace) -> None:
    with Store() as store:
        pairings = store.pairings()
    by_dante_name = {pairing.dante_name: pairing.display_name for pairing in pairings}
    subscriptions = asyncio.run(read_subscriptions(pairings))

    for pairing in pairings:
        print(f"{pairing.display_name} receives:")
        rows = [s for s in subscriptions if s.rx_device == pairing.dante_name and s.tx_device]
        if not rows:
            print("  (nothing)")
        for sub in sorted(rows, key=lambda s: s.rx_channel):
            source = by_dante_name.get(sub.tx_device, f"{sub.tx_device} (not paired)")
            state = "" if sub.connected else "  (not connected)"
            tx = sub.tx_channel if sub.tx_channel is not None else "?"
            print(f"  Dante in {sub.rx_channel:2}  <-  {source} Dante out {tx}{state}")


def buttons(args: argparse.Namespace) -> None:
    with Store() as store:
        pairing = store.by_display_name(args.display_name)
        if pairing is None:
            raise SystemExit(f"No paired console called {args.display_name!r}")
        try:
            store.set_buttons(pairing.console_mac, args.gain, args.phantom)
        except ValueError as error:
            raise SystemExit(str(error)) from None
    phantom = f", phantom power on GPIO {args.phantom}" if args.phantom else ""
    print(f"{pairing.display_name}: gain mode on GPIO {args.gain}{phantom}.")
    if not pairing.is_foh:
        print(f"Note that {pairing.display_name} is not marked FOH, so its buttons will not be used.")


def rename(args: argparse.Namespace) -> None:
    from headhunter import renaming

    if not args.name and not args.dante:
        raise SystemExit("Give --name, --dante, or both.")
    with Store() as store:
        pairing = store.by_display_name(args.display_name)
    if pairing is None:
        raise SystemExit(f"No paired console called {args.display_name!r}")

    if args.dante:
        try:
            plan = asyncio.run(renaming.plan(pairing, args.dante))
        except ValueError as error:
            raise SystemExit(str(error)) from None
        print(f"Renaming Dante device {pairing.dante_name} to {plan.new_name}.")
        if plan.affected:
            print(f"These {len(plan.affected)} subscriptions will be moved to the new name:")
            for device in sorted({s.rx_device for s in plan.affected}):
                channels = sorted(s.rx_channel for s in plan.affected if s.rx_device == device)
                print(f"  {device}: {len(channels)} channels ({_ranges(channels)})")
            print("Audio on these channels will drop out for a few seconds.")
        if input("Go ahead? [y/N] ").strip().lower() != "y":
            raise SystemExit("Cancelled; nothing was changed.")
        failed = asyncio.run(renaming.execute(plan))
        with Store() as store:
            store.set_dante_name(pairing.console_mac, plan.new_name)
        if failed:
            print(f"Renamed, but {len(failed)} subscriptions did not reconnect; check them in Dante Controller:")
            for sub in failed:
                print(f"  {sub.rx_device} channel {sub.rx_channel} <- {plan.new_name} channel {sub.tx_channel}")
            raise SystemExit(1)
        print(f"Renamed to {plan.new_name}; every subscription reconnected.")

    if args.name:
        with Store() as store:
            try:
                store.rename(pairing.console_mac, args.name)
            except ValueError as error:
                raise SystemExit(str(error)) from None
            new = store.by_console_mac(pairing.console_mac).display_name
        print(f"{pairing.display_name} is now called {new} in Headhunter. Restart 'headhunter run' to use the new name.")


def _ranges(numbers: list[int]) -> str:
    """1, 2, 3, 5, 7, 8 -> 1-3, 5, 7-8"""
    parts, start = [], None
    for i, n in enumerate(numbers):
        if start is None:
            start = n
        if i + 1 == len(numbers) or numbers[i + 1] != n + 1:
            parts.append(f"{start}-{n}" if n != start else str(n))
            start = None
    return ", ".join(parts)


def names(args: argparse.Namespace) -> None:
    asyncio.run(run_naming(args.write))


def run(args: argparse.Namespace) -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_engine(args.write_names, args.write_gain, args.restore_after, args.stats))


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="headhunter", description="Remote preamp control for Behringer Wing consoles connected over Dante."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show debugging messages")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    commands.add_parser("discover", help="List every Wing console and Wing Dante card found").set_defaults(func=discover)
    commands.add_parser("pair", help="Pair consoles with their Dante cards").set_defaults(func=pair)

    role_parser = commands.add_parser("role", help="Mark a paired console as FOH or stage")
    role_parser.add_argument("display_name", help="The console's display name, e.g. COMPACT")
    role_parser.add_argument("role", choices=["FOH", "STAGE", "foh", "stage"], help="FOH or STAGE")
    role_parser.set_defaults(func=role)

    commands.add_parser("dante", help="Show what each paired console receives over Dante").set_defaults(func=dante)

    names_parser = commands.add_parser("names", help="Name Dante sources on FOH consoles")
    names_parser.add_argument("--write", action="store_true", help="Write the names (without this, it is a dry run)")
    names_parser.set_defaults(func=names)

    buttons_parser = commands.add_parser("buttons", help="Choose a FOH console's gain mode and phantom power GPIOs")
    buttons_parser.add_argument("display_name", help="The FOH console's display name, e.g. COMPACT")
    buttons_parser.add_argument("--gain", type=int, required=True, metavar="GPIO", help="GPIO for the gain mode button, 1 to 4")
    buttons_parser.add_argument("--phantom", type=int, metavar="GPIO", help="GPIO for the phantom power button, 1 to 4")
    buttons_parser.set_defaults(func=buttons)

    rename_parser = commands.add_parser("rename", help="Change a console's Headhunter name or Dante name")
    rename_parser.add_argument("display_name", help="The console's current Headhunter name, e.g. RACK")
    rename_parser.add_argument("--name", metavar="NEW", help="New Headhunter name, 7 characters max")
    rename_parser.add_argument("--dante", metavar="NEW", help="New Dante device name; subscriptions to it are moved over")
    rename_parser.set_defaults(func=rename)

    run_parser = commands.add_parser("run", help="Keep running, and keep names up to date as things change")
    run_parser.add_argument("--write-names", action="store_true", help="Write names (without this, only report them)")
    run_parser.add_argument("--write-gain", action="store_true", help="Change preamp gain and restore trim in gain mode (without this, only report it)")
    run_parser.add_argument("--stats", type=float, metavar="MINUTES", help="Log memory and resource use every few minutes, to spot leaks")
    run_parser.add_argument("--restore-after", type=float, metavar="SECONDS", help="How long trim must be still before it is restored (default 2)")
    run_parser.set_defaults(func=run)

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO if args.command == "run" else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(message)s" if args.command == "run" else "%(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        args.func(args)
    except (LookupError, NetaudioError) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()