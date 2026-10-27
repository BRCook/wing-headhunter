"""Headhunter's custom control buttons: GPIO states on Console A.

A GPIO's state drives the GPIO jack on the back of the console, so only use
GPIOs that have nothing connected to them.
"""

import asyncio

from headhunter.native import NativeClient

# GPIOs are numbered 1-4 in the protocol, but named A-D on the console
GPIO_NAMES = "ABCD"


def gpio_name(number: int) -> str:
    return GPIO_NAMES[number - 1]


def gpio_number(name: str) -> int:
    """A GPIO's number from its console name, A to D (case does not matter)."""
    text = name.strip().upper()
    if len(text) != 1 or text not in GPIO_NAMES:
        raise ValueError(f"GPIOs are named {', '.join(GPIO_NAMES)}")
    return GPIO_NAMES.index(text) + 1


# (seconds dark, seconds lit) for each flash. Each flash starts dark, because
# the button is already lit under the user's finger; the pattern always ends dark.
FLASH_PATTERNS = {
    "on": [(0.18, 0.18)] * 5,  # Five quick flashes: phantom power is now on
    "off": [(0.4, 0.7)] * 3,  # Three slow flashes: phantom power is now off
}


def gpio_path(gpio: int) -> str:
    if not 1 <= gpio <= 4:
        raise ValueError("GPIO must be between 1 and 4")
    return f"$ctl.gpio.{gpio}.gpstate"


async def flash(client: NativeClient, gpio: int, pattern: str) -> None:
    """Flash a momentary GPIO button, leaving it dark.

    The button is usually still held while it flashes, so its state at the
    start says nothing about its state at the end. A momentary button rests
    dark, and the console still reports the release when the finger lifts, so
    ending dark is always safe; restoring the starting state could leave it
    stuck lit if the button was released mid-flash.
    """
    path = gpio_path(gpio)
    try:
        for dark, lit in FLASH_PATTERNS[pattern]:
            client.set(path, 0)
            await asyncio.sleep(dark)
            client.set(path, 1)
            await asyncio.sleep(lit)
    finally:
        client.set(path, 0)