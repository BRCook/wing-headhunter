"""Custom control buttons: finding them, reading them, and setting them up for Headhunter.

Headhunter's buttons are custom control buttons with the GPIO function. Their GPIO
setting (the "GPIO flag": A to D for a toggle, A-P to D-P for a push button) only exists
while the function is GPIO, and the console gives it its own address rather than one
calculated from its name, so it is always looked up first.
"""

import asyncio
from dataclasses import dataclass

from headhunter.buttons import GPIO_NAMES, gpio_name
from headhunter.native import NativeClient, Value
from headhunter.store import Pairing

# Custom control pages by console model, and the rows of buttons each has. The Compact has
# one button per column; the full-size Wing's numbered pages have an upper and lower row
# (from the protocol document; not checked on a real console).
PAGES = {
    "wing-compact": ["U1", "U2", "U3", "U4", "MM", "D1", "D2", "D3", "D4"],
    "wing-rack": [],
}
PAGES_DEFAULT = [str(n) for n in range(1, 17)]
MODEL_ROWS = {"wing-compact": ("bu",)}
ROWS_DEFAULT = ("bu", "bd")
BUTTONS_PER_PAGE = 4
ROWS = {"bu": "upper", "bd": "lower"}

READ_TIMEOUT = 1.0
FLAG_APPEARS_SECONDS = 2.0  # How long to wait for the GPIO flag to appear after choosing GPIO

# Button labels: HH inverted on the first line, the rest on the second. The gain mode button
# shows whether gain mode is on; the phantom power button, what pressing it will do.
LABEL_GAIN_ON = "|HH GAIN ON"
LABEL_GAIN_OFF = "|HH GAIN OFF"
LABEL_DISABLED = "|HH DISABLED"
LABEL_NO_48V = "|HH NO 48V"
LABEL_48V_ON = "|HH 48V ON"
LABEL_48V_OFF = "|HH 48V OFF"


def pages(pairing: Pairing) -> list[str]:
    return PAGES.get(pairing.console_model, PAGES_DEFAULT)


@dataclass(frozen=True)
class Button:
    page: str
    number: int  # 1 to 4
    row: str  # "bu" or "bd"

    @property
    def path(self) -> str:
        return f"$ctl.user.{self.page}.{self.number}.{self.row}"

    @property
    def label(self) -> str:
        if self.row == "bu" and not self.page.isdigit():
            return f"page {self.page}, button {self.number}"  # The Compact has just one row
        return f"page {self.page}, button {self.number}, {ROWS[self.row]} row"


def rows(pairing: Pairing) -> tuple[str, ...]:
    return MODEL_ROWS.get(pairing.console_model, ROWS_DEFAULT)


def all_buttons(pairing: Pairing) -> list[Button]:
    return [
        Button(page, number, row)
        for page in pages(pairing)
        for number in range(1, BUTTONS_PER_PAGE + 1)
        for row in rows(pairing)
    ]


def mode_paths(pairing: Pairing) -> list[str]:
    """Every button's function, for watching: a change means the buttons must be looked at again."""
    return [f"{button.path}.mode" for button in all_buttons(pairing)]


def flag(gpio: int, push: bool) -> str:
    """The GPIO flag for a GPIO (1-4) as a toggle or push button: C, or C-P."""
    return gpio_name(gpio) + ("-P" if push else "")


def parse_flag(value: Value) -> tuple[int, bool] | None:
    """A GPIO flag's GPIO (1-4) and whether it is a push button; None for anything else."""
    if not isinstance(value, str):
        return None
    letter, _, push = value.partition("-")
    if len(letter) != 1 or letter not in GPIO_NAMES or push not in ("", "P"):
        return None
    return GPIO_NAMES.index(letter) + 1, push == "P"


@dataclass(frozen=True)
class Setup:
    """How a button is set up."""

    mode: Value
    name: Value
    flag: Value = None  # Only for GPIO buttons
    flag_address: int | None = None  # The console's address for the flag, for watching it

    @property
    def gpio(self) -> tuple[int, bool] | None:
        return parse_flag(self.flag) if self.mode == "GPIO" else None


async def _flag_address(client: NativeClient, button: Button) -> int | None:
    """The console's address for a button's GPIO flag, if it has one."""
    try:
        children = await client.children(button.path, timeout=READ_TIMEOUT * 3)
    except TimeoutError:
        return None
    return next((child.hash for child in children if child.key == "gpio"), None)


async def _maybe(client: NativeClient, path: str) -> Value:
    try:
        return await client.get(path, READ_TIMEOUT)
    except TimeoutError:
        return None


async def read(client: NativeClient, button: Button) -> Setup:
    mode = await _maybe(client, f"{button.path}.mode")
    name = await _maybe(client, f"{button.path}.name")
    value = address = None
    if mode == "GPIO":
        address = await _flag_address(client, button)
        if address is not None:
            try:
                value = await client.get_hash(address, READ_TIMEOUT)
            except TimeoutError:
                pass
    return Setup(mode, name, value, address)


async def assign(client: NativeClient, button: Button, gpio: int, push: bool, name: str | None) -> Setup:
    """Make a button a GPIO button, and optionally name it. Raises RuntimeError if the console
    does not end up with exactly that setup."""
    wanted = flag(gpio, push)
    client.set(f"{button.path}.mode", "GPIO")
    # The GPIO flag only appears once the function is GPIO
    address = None
    deadline = asyncio.get_running_loop().time() + FLAG_APPEARS_SECONDS
    while address is None and asyncio.get_running_loop().time() < deadline:
        address = await _flag_address(client, button)
        if address is None:
            await asyncio.sleep(0.1)
    if address is None:
        raise RuntimeError(f"{button.label} has no GPIO setting after choosing the GPIO function")
    client.set_hash(address, wanted)
    if name is not None:
        client.set(f"{button.path}.name", name)
    result = await read(client, button)
    if result.mode != "GPIO" or result.flag != wanted or (name is not None and result.name != name):
        raise RuntimeError(f"{button.label} did not take the new setup; it reads {result}")
    return result


async def gpio_buttons(client: NativeClient, pairing: Pairing) -> list[tuple[Button, Setup]]:
    """Every button with the GPIO function, and how each is set up."""
    found = []
    for button in all_buttons(pairing):
        if await _maybe(client, f"{button.path}.mode") == "GPIO":
            found.append((button, await read(client, button)))
    return found


async def find(client: NativeClient, pairing: Pairing, gpio: int) -> list[tuple[Button, bool]]:
    """Every button set to a GPIO, with whether each is a push button."""
    return [
        (button, setup.gpio[1])
        for button, setup in await gpio_buttons(client, pairing)
        if setup.gpio is not None and setup.gpio[0] == gpio
    ]


def gain_label(gain_mode: bool) -> str:
    return LABEL_GAIN_ON if gain_mode else LABEL_GAIN_OFF


def phantom_label(gain_mode: bool, phantoms: list[bool] | None) -> str:
    """What the phantom power button's label says it will do for the selected channel.

    phantoms is each preamp's phantom power on the selected channel (two on a stereo
    channel), or None when it has no preamp.
    """
    if not gain_mode:
        return LABEL_DISABLED
    if not phantoms:
        return LABEL_NO_48V
    return LABEL_48V_OFF if all(phantoms) else LABEL_48V_ON
