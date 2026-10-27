"""Pair discovered consoles with their Dante cards, interactively.

Lists existing pairings, then walks through every unpaired console and asks
which Dante card belongs to it. Headhunter suggests a card when exactly one
card's name matches the console's model, but you always confirm.

set_role() marks an existing pairing as the FOH console or a stage console.
Headhunter only ever renames sources on FOH consoles.
"""

import asyncio
import re

from headhunter.discovery import DiscoveredConsole, DiscoveredDanteDevice, discover_all
from headhunter.store import DISPLAY_NAME_MAX, FOH, ROLES, STAGE, Store

# Default display names for known console models
MODEL_DISPLAY_NAMES = {
    "wing-rack": "RACK",
    "wing-compact": "COMPACT",
    "ngc-full": "WING",
    "wing": "WING",
    "wing-bk": "WING",
}


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def suggest_card(
    console: DiscoveredConsole, cards: list[DiscoveredDanteDevice]
) -> DiscoveredDanteDevice | None:
    """Suggest a Dante card whose name contains the console's model.

    Only suggests when exactly one card matches; otherwise it is a guess.
    """
    model = _normalise(console.model)
    matches = [card for card in cards if model and model in _normalise(card.name)]
    return matches[0] if len(matches) == 1 else None


def default_display_name(console: DiscoveredConsole) -> str:
    return MODEL_DISPLAY_NAMES.get(console.model, console.name.upper())[:DISPLAY_NAME_MAX]


def _choose_card(
    console: DiscoveredConsole, cards: list[DiscoveredDanteDevice]
) -> DiscoveredDanteDevice | None:
    suggestion = suggest_card(console, cards)
    for number, card in enumerate(cards, start=1):
        marker = "  (suggested)" if card is suggestion else ""
        print(f"  {number}. {card.name}  {card.ip}  {card.mac}{marker}")
    print("  0. Skip this console")

    default = cards.index(suggestion) + 1 if suggestion else None
    prompt = f"Dante card [{default}]: " if default else "Dante card: "
    while True:
        answer = input(prompt).strip()
        if not answer and default:
            return suggestion
        if answer.isdigit() and 0 <= int(answer) <= len(cards):
            return cards[int(answer) - 1] if int(answer) else None
        print("Please enter one of the numbers above.")


def _choose_display_name(console: DiscoveredConsole, store: Store) -> str:
    default = default_display_name(console)
    taken = {pairing.display_name for pairing in store.pairings()}
    while True:
        answer = input(f"Display name, {DISPLAY_NAME_MAX} characters max [{default}]: ")
        name = (answer.strip() or default)[:DISPLAY_NAME_MAX]
        if name in taken:
            print(f"{name!r} is already in use.")
        elif name:
            return name


def run(store: Store) -> None:
    print("Discovering consoles and Dante cards...")
    consoles, cards = asyncio.run(discover_all())
    store.refresh(consoles, cards)

    pairings = store.pairings()
    print("\nExisting pairings:")
    if not pairings:
        print("  (none)")
    for pairing in pairings:
        print(f"  {pairing.display_name} ({pairing.role}): {pairing.console_name} <-> {pairing.dante_name}")

    paired_consoles = {pairing.console_mac for pairing in pairings}
    paired_cards = {pairing.dante_mac for pairing in pairings}
    unpaired_cards = [card for card in cards if card.mac and card.mac not in paired_cards]

    for console in consoles:
        if console.mac in paired_consoles:
            continue
        print(f"\nUnpaired console: {console.name} ({console.model}, {console.ip})")
        if not console.mac:
            print("  Skipping: its MAC address is unknown.")
            continue
        if not unpaired_cards:
            print("  No unpaired Dante cards left.")
            break

        card = _choose_card(console, unpaired_cards)
        if card is None:
            continue
        display_name = _choose_display_name(console, store)
        is_foh = input("Is this the FOH console, where you mix? [y/N] ").strip().lower() == "y"
        pairing = store.add(console, card, display_name, FOH if is_foh else STAGE)
        unpaired_cards.remove(card)
        print(f"  Paired {pairing.console_name} with {pairing.dante_name} as {pairing.display_name} ({pairing.role}).")

    print("\nDone.")


def set_role(store: Store, display_name: str, role: str) -> None:
    role = role.upper()
    if role not in ROLES:
        raise SystemExit(f"Role must be one of {', '.join(ROLES)}")
    pairing = store.by_display_name(display_name)
    if pairing is None:
        raise SystemExit(f"No paired console called {display_name!r}")
    store.set_role(pairing.console_mac, role)
    print(f"{pairing.display_name} is now marked {role}.")
