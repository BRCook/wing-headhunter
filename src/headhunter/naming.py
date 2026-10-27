"""Source naming: name each Dante input after the source at the other end.

Without writing, this is a dry run: it works out every name and prints it
beside the current one, but writes nothing to any console. When writing, it
writes every name that needs changing, then reads each one back to confirm it.

Only consoles marked FOH are renamed. On each one, every Dante input that is subscribed to another
paired console is followed back to that console's Dante output, and from
there to whatever is routed to it, e.g. RACK-L24-HH for local input 24. The
HH tag marks a name as managed by Headhunter.

A Dante input is never renamed while any channel using it (as its main or alt
source) has "link customization to source" turned on, since the channel would
take on the new name; those inputs are reported instead.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

from headhunter.dante import Subscription, read_subscriptions
from headhunter.consoles import locate
from headhunter.native import NativeClient, Value
from headhunter.store import Pairing, Store

# Wing source group -> abbreviation, as documented in the README
ABBREVIATIONS = {
    "LCL": "L",
    "A": "A",
    "B": "B",
    "C": "C",
    "AUX": "X",
    "SC": "S",
    "USB": "U",
    "CRD": "K",
    "MOD": "M",
    "PLAY": "P",
    "AES": "E",
    "USR": "US",
    "OSC": "OS",
    "BUS": "BS",
    "MAIN": "MN",
    "MTX": "MX",
    "SEND": "SN",
    "MON": "MO",
}
# Groups whose inputs are the left and right halves of stereo signals: input 1
# is bus 1 left, input 2 is bus 1 right, input 3 is bus 2 left, and so on
STEREO_GROUPS = {"BUS", "MAIN", "MTX", "SEND", "MON"}
NAME_MAX = 16
TAG = "HH"

# Strips that have sources, and how many of each
STRIPS = {"ch": 40, "aux": 8}


def source_name(display_name: str, group: str, number: int) -> str:
    """The name for a Dante input fed by `group` `number` on another console."""
    if group == "OFF":
        name = f"{display_name}-OFF-{TAG}"
    else:
        abbreviation = ABBREVIATIONS.get(group, "??")
        if group in STEREO_GROUPS:
            side = "L" if number % 2 else "R"
            name = f"{display_name}-{abbreviation}{(number + 1) // 2:02d}{side}-{TAG}"
        else:
            name = f"{display_name}-{abbreviation}{number:02d}-{TAG}"
    if len(name) > NAME_MAX:
        raise ValueError(f"Source name {name!r} is longer than {NAME_MAX} characters")
    return name


@dataclass(frozen=True)
class PlannedName:
    """A name Headhunter would give one Dante input."""

    console: Pairing  # The console receiving the Dante input
    dante_input: int
    current: str
    planned: str
    linked: tuple[str, ...] = ()  # Channels with customization linked to this input

    @property
    def needs_writing(self) -> bool:
        return not self.linked and self.current != self.planned


DANTE_INPUTS = 64
State = Mapping[str, Value]


def foh_paths() -> list[str]:
    """Every parameter naming needs from a FOH console."""
    paths = []
    for kind, count in STRIPS.items():
        for n in range(1, count + 1):
            paths += [f"{kind}.{n}.clink"] + [f"{kind}.{n}.in.conn.{leaf}" for leaf in ("grp", "in", "altgrp", "altin")]
    paths += [f"io.in.MOD.{n}.name" for n in range(1, DANTE_INPUTS + 1)]
    return paths


def stage_paths() -> list[str]:
    """Every parameter naming needs from a stage console."""
    return [f"io.out.MOD.{n}.{leaf}" for n in range(1, DANTE_INPUTS + 1) for leaf in ("grp", "in")]


def paths_for(pairing: Pairing) -> list[str]:
    """Every parameter naming needs from a console. Any console can send Dante,
    so every console's output routing is needed; FOH consoles need more."""
    return (foh_paths() if pairing.is_foh else []) + stage_paths()


def linked_from_state(state: State) -> dict[int, list[str]]:
    """Map each Dante input to the channels that use it with customization linked."""
    linked: dict[int, list[str]] = {}
    for kind, count in STRIPS.items():
        for n in range(1, count + 1):
            base = f"{kind}.{n}"
            if not state.get(f"{base}.clink"):
                continue
            label = f"{kind.upper()} {n}"
            for group_leaf, number_leaf in (("grp", "in"), ("altgrp", "altin")):
                if state.get(f"{base}.in.conn.{group_leaf}") == "MOD":
                    source = int(state.get(f"{base}.in.conn.{number_leaf}") or 0)
                    if label not in linked.setdefault(source, []):
                        linked[source].append(label)
    return linked


def plan_from_state(
    pairings: list[Pairing],
    subscriptions: list[Subscription],
    states: Mapping[str, State],
) -> list[PlannedName]:
    """Work out the name for every Dante input on FOH consoles fed by another paired console.

    states maps console MAC to that console's current parameter values, for
    the parameters listed by paths_for().
    Consoles missing from states are treated as unreachable and skipped.
    """
    by_dante_name = {p.dante_name: p for p in pairings}
    plans = []
    for receiver in pairings:
        if not receiver.is_foh or receiver.console_mac not in states:
            continue
        rx_state = states[receiver.console_mac]
        linked = linked_from_state(rx_state)
        for sub in subscriptions:
            if sub.rx_device != receiver.dante_name or not sub.connected:
                continue
            sender = by_dante_name.get(sub.tx_device)
            if sender is None or sender is receiver or sender.console_mac not in states:
                continue  # Not from a reachable paired console; leave its name alone
            tx_state = states[sender.console_mac]
            group = tx_state.get(f"io.out.MOD.{sub.tx_channel}.grp")
            number = tx_state.get(f"io.out.MOD.{sub.tx_channel}.in")
            if group is None or number is None:
                continue
            plans.append(
                PlannedName(
                    receiver,
                    sub.rx_channel,
                    str(rx_state.get(f"io.in.MOD.{sub.rx_channel}.name", "")),
                    source_name(sender.display_name, str(group), int(number)),
                    tuple(linked.get(sub.rx_channel, ())),
                )
            )
    return plans


async def read_state(client: NativeClient, paths: list[str]) -> dict[str, Value]:
    """Read many parameters at once."""
    values = await asyncio.gather(*(client.get(path) for path in paths))
    return dict(zip(paths, values))


async def plan_names(
    pairings: list[Pairing],
    subscriptions: list[Subscription],
    clients: Mapping[str, NativeClient],
) -> list[PlannedName]:
    """Read every console's current state and work out the names, in one go.

    clients maps console MAC to a connected client for every reachable console.
    """
    states = {}
    for pairing in pairings:
        client = clients.get(pairing.console_mac)
        if client is not None:
            states[pairing.console_mac] = await read_state(client, paths_for(pairing))
    return plan_from_state(pairings, subscriptions, states)


async def apply_names(plans: list[PlannedName], clients: Mapping[str, NativeClient]) -> list[PlannedName]:
    """Write every planned name that differs from the current one.

    Returns the plans whose names did not read back as written.
    """
    changes = [p for p in plans if p.needs_writing]
    for plan in changes:
        clients[plan.console.console_mac].set(f"io.in.MOD.{plan.dante_input}.name", plan.planned)
    await asyncio.sleep(0.5)  # Give the console a moment before reading back
    readbacks = await asyncio.gather(
        *(clients[p.console.console_mac].get(f"io.in.MOD.{p.dante_input}.name") for p in changes)
    )
    return [plan for plan, value in zip(changes, readbacks) if value != plan.planned]


async def run(write: bool) -> None:
    with Store() as store:
        pairings = store.pairings()
    if not any(p.is_foh for p in pairings):
        raise SystemExit(
            "No console is marked FOH, so there is nothing to rename. Mark one with:\n"
            "  headhunter role <display name> FOH"
        )
    ips = await locate(pairings)
    for pairing in pairings:
        if pairing.console_mac not in ips:
            print(f"{pairing.display_name} did not answer discovery; skipping it.")

    subscriptions = await read_subscriptions(pairings)
    clients = {mac: NativeClient(ip) for mac, ip in ips.items()}
    await asyncio.gather(*(client.connect() for client in clients.values()))
    failed: list[PlannedName] = []
    try:
        plans = await plan_names(pairings, subscriptions, clients)
        if write:
            for receiver in {p.console.console_mac: p.console for p in plans}.values():
                if await clients[receiver.console_mac].get("$syscfg.tcplock"):
                    raise SystemExit(f"{receiver.display_name} has TCP remote lock on; it will refuse changes.")
            failed = await apply_names(plans, clients)
    finally:
        await asyncio.gather(*(client.close() for client in clients.values()))

    print("Names written and read back.\n" if write else "Dry run: nothing has been written.\n")
    for receiver in (p for p in pairings if p.is_foh):
        rows = sorted((p for p in plans if p.console is receiver), key=lambda p: p.dante_input)
        changes = sum(p.needs_writing for p in rows)
        skipped = sum(bool(p.linked) for p in rows)
        verb = "changed" if write else "would change"
        print(f"{receiver.display_name}: {len(rows)} Dante inputs from paired consoles, "
              f"{changes} {verb}, {skipped} skipped")
        for plan in rows:
            if plan.linked:
                note = f"  skipped: customization linked on {', '.join(plan.linked)}"
                marker = "  "
            else:
                note = "  FAILED: did not read back as written" if plan in failed else ""
                marker = "->" if plan.current != plan.planned else "  "
            print(f"  MOD {plan.dante_input:2}  {plan.current!r:20} {marker} {plan.planned}{note}")
        print()
    if any(p.linked for p in plans):
        print("To rename skipped inputs, turn off \"link customization to source\" on the channels listed.\n")
    if failed:
        raise SystemExit(f"{len(failed)} names did not read back as written.")
