"""Headhunter's permanent configuration: which console is paired with which Dante card.

Pairings live in a small SQLite database. MAC addresses are the identity; the
console and Dante names are refreshed from the network on every discovery, and
the display name only changes when the user changes it. IP addresses are never
stored. Session state (trim snapshots, gain mode, and so on) never goes here.
"""

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from headhunter.discovery import DiscoveredConsole, DiscoveredDanteDevice

DISPLAY_NAME_MAX = 7
SCHEMA_VERSION = 2

FOH = "FOH"
STAGE = "STAGE"
ROLES = (FOH, STAGE)

SCHEMA = f"""
CREATE TABLE consoles (
    console_mac    TEXT PRIMARY KEY,
    console_name   TEXT NOT NULL,
    console_model  TEXT NOT NULL,
    console_serial TEXT NOT NULL,
    dante_mac      TEXT NOT NULL UNIQUE,
    dante_name     TEXT NOT NULL,
    display_name   TEXT NOT NULL UNIQUE
        CHECK (length(display_name) BETWEEN 1 AND {DISPLAY_NAME_MAX}),
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    role           TEXT NOT NULL DEFAULT '{STAGE}' CHECK (role IN ('{FOH}', '{STAGE}'))
);
"""

# Upgrades from each older schema version to the next
MIGRATIONS = {
    1: f"ALTER TABLE consoles ADD COLUMN role TEXT NOT NULL DEFAULT '{STAGE}' "
       f"CHECK (role IN ('{FOH}', '{STAGE}'))",
}


def default_path() -> Path:
    """The database location, following the XDG base directory convention."""
    data_home = os.environ.get(
        "XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(data_home) / "headhunter" / "headhunter.db"


def clean_display_name(name: str) -> str:
    """Strip whitespace and truncate to the maximum display name length."""
    cleaned = name.strip()[:DISPLAY_NAME_MAX]
    if not cleaned:
        raise ValueError("Display name cannot be empty")
    return cleaned


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Pairing:
    """A console paired with its Dante card."""

    console_mac: str
    console_name: str
    console_model: str
    console_serial: str
    dante_mac: str
    dante_name: str
    display_name: str
    role: str = STAGE

    @property
    def is_foh(self) -> bool:
        return self.role == FOH


class Store:
    """The pairing database."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._migrate()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _migrate(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            with self._db:
                self._db.executescript(SCHEMA)
                self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"{self.path} has schema version {version}, which is newer than "
                f"this Headhunter ({SCHEMA_VERSION}); please update Headhunter"
            )
        while version < SCHEMA_VERSION:
            with self._db:
                self._db.execute(MIGRATIONS[version])
                version += 1
                self._db.execute(f"PRAGMA user_version = {version}")

    def pairings(self) -> list[Pairing]:
        rows = self._db.execute(
            "SELECT * FROM consoles ORDER BY display_name"
        ).fetchall()
        return [_to_pairing(row) for row in rows]

    def by_console_mac(self, mac: str) -> Pairing | None:
        row = self._db.execute(
            "SELECT * FROM consoles WHERE console_mac = ?", (mac,)
        ).fetchone()
        return _to_pairing(row) if row else None

    def by_dante_mac(self, mac: str) -> Pairing | None:
        row = self._db.execute(
            "SELECT * FROM consoles WHERE dante_mac = ?", (mac,)
        ).fetchone()
        return _to_pairing(row) if row else None

    def add(
            self,
            console: DiscoveredConsole,
            dante: DiscoveredDanteDevice,
            display_name: str,
            role: str = STAGE,
    ) -> Pairing:
        """Pair a console with a Dante card.

        Raises ValueError if either is missing a MAC address, either is already
        paired, or the display name is already taken.
        """
        if not console.mac or not dante.mac:
            raise ValueError(
                "Both the console and the Dante card need a MAC address")
        display_name = clean_display_name(display_name)
        if role not in ROLES:
            raise ValueError(f"Role must be one of {', '.join(ROLES)}")
        now = _now()
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO consoles (console_mac, console_name, console_model, "
                    "console_serial, dante_mac, dante_name, display_name, created_at, "
                    "updated_at, role) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        console.mac,
                        console.name,
                        console.model,
                        console.serial,
                        dante.mac,
                        dante.name,
                        display_name,
                        now,
                        now,
                        role,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError(f"Could not pair: {error}") from error
        return self.by_console_mac(console.mac)

    def remove(self, console_mac: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM consoles WHERE console_mac = ?",
                             (console_mac,))

    def rename(self, console_mac: str, display_name: str) -> None:
        """Change a pairing's display name."""
        display_name = clean_display_name(display_name)
        try:
            with self._db:
                self._db.execute(
                    "UPDATE consoles SET display_name = ?, updated_at = ? "
                    "WHERE console_mac = ?",
                    (display_name, _now(), console_mac),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError(
                f"Display name {display_name!r} is already taken") from error

    def set_role(self, console_mac: str, role: str) -> None:
        """Mark a console as FOH or stage."""
        if role not in ROLES:
            raise ValueError(f"Role must be one of {', '.join(ROLES)}")
        with self._db:
            self._db.execute(
                "UPDATE consoles SET role = ?, updated_at = ? WHERE console_mac = ?",
                (role, _now(), console_mac),
            )

    def by_display_name(self, display_name: str) -> Pairing | None:
        """Find a pairing by display name, ignoring case."""
        return next(
            (p for p in self.pairings() if
             p.display_name.lower() == display_name.lower()),
            None,
        )

    def refresh(
            self,
            consoles: list[DiscoveredConsole],
            dante_devices: list[DiscoveredDanteDevice],
    ) -> None:
        """Update stored names from what the network currently reports.

        Only rows whose names have actually changed are touched.
        """
        now = _now()
        with self._db:
            for console in consoles:
                if console.mac:
                    self._db.execute(
                        "UPDATE consoles SET console_name = ?, console_model = ?, "
                        "console_serial = ?, updated_at = ? WHERE console_mac = ? "
                        "AND (console_name, console_model, console_serial) "
                        "IS NOT (?, ?, ?)",
                        (
                            console.name,
                            console.model,
                            console.serial,
                            now,
                            console.mac,
                            console.name,
                            console.model,
                            console.serial,
                        ),
                    )
            for device in dante_devices:
                if device.mac:
                    self._db.execute(
                        "UPDATE consoles SET dante_name = ?, updated_at = ? "
                        "WHERE dante_mac = ? AND dante_name IS NOT ?",
                        (device.name, now, device.mac, device.name),
                    )


def _to_pairing(row: sqlite3.Row) -> Pairing:
    return Pairing(
        console_mac=row["console_mac"],
        console_name=row["console_name"],
        console_model=row["console_model"],
        console_serial=row["console_serial"],
        dante_mac=row["dante_mac"],
        dante_name=row["dante_name"],
        display_name=row["display_name"],
        role=row["role"],
    )