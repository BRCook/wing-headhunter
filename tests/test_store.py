"""The pairing database."""

import sqlite3

import pytest

from headhunter.discovery import DiscoveredConsole, DiscoveredDanteDevice
from headhunter.store import FOH, STAGE, Store

RACK = DiscoveredConsole("10.0.0.2", "My-WingRCK", "wing-rack", "S1", "3.1.1", "00:00:00:00:00:01")
COMPACT = DiscoveredConsole("10.0.0.3", "My-WingCCT", "wing-compact", "S2", "3.1.2", "00:00:00:00:00:02")
RACK_CARD = DiscoveredDanteDevice("WingRack", "10.0.0.12", "00:1d:c1:00:00:01", "wing64 B")
COMPACT_CARD = DiscoveredDanteDevice("WingCompact", "10.0.0.13", "00:1d:c1:00:00:02", "wing64 B")


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "headhunter.db") as store:
        yield store


def test_pairings_are_stored(store):
    store.add(RACK, RACK_CARD, "RACK")
    store.add(COMPACT, COMPACT_CARD, "COMPACT", FOH)
    assert [(p.display_name, p.role) for p in store.pairings()] == [("COMPACT", FOH), ("RACK", STAGE)]


def test_long_display_names_are_truncated(store):
    assert store.add(RACK, RACK_CARD, "MONITORS").display_name == "MONITOR"


@pytest.mark.parametrize(
    "console, card, name",
    [
        (COMPACT, RACK_CARD, "OTHER"),  # Card already paired
        (RACK, COMPACT_CARD, "OTHER"),  # Console already paired
        (COMPACT, COMPACT_CARD, "RACK"),  # Display name taken
    ],
)
def test_duplicates_are_refused(store, console, card, name):
    store.add(RACK, RACK_CARD, "RACK")
    with pytest.raises(ValueError):
        store.add(console, card, name)


def test_names_are_refreshed_but_display_names_are_kept(store):
    store.add(RACK, RACK_CARD, "RACK")
    renamed_console = DiscoveredConsole("x", "Renamed-Rack", "wing-rack", "S1", "3.1.2", RACK.mac)
    renamed_card = DiscoveredDanteDevice("NewDanteName", "x", RACK_CARD.mac, "wing64 B")
    store.refresh([renamed_console], [renamed_card])
    pairing = store.by_console_mac(RACK.mac)
    assert (pairing.console_name, pairing.dante_name, pairing.display_name) == ("Renamed-Rack", "NewDanteName", "RACK")


def test_roles(store):
    store.add(COMPACT, COMPACT_CARD, "COMPACT")
    store.set_role(COMPACT.mac, FOH)
    assert store.by_display_name("compact").is_foh
    with pytest.raises(ValueError):
        store.set_role(COMPACT.mac, "BOSS")


def test_version_1_databases_are_upgraded(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE consoles (console_mac TEXT PRIMARY KEY, console_name TEXT NOT NULL,
            console_model TEXT NOT NULL, console_serial TEXT NOT NULL, dante_mac TEXT NOT NULL UNIQUE,
            dante_name TEXT NOT NULL, display_name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL);
        INSERT INTO consoles VALUES ('m', 'n', 'wing-rack', 's', 'd', 'WingRack', 'RACK', 't', 't');
        PRAGMA user_version = 1;
        """
    )
    db.commit()
    db.close()
    with Store(path) as store:
        assert [(p.display_name, p.role) for p in store.pairings()] == [("RACK", STAGE)]


def test_newer_databases_are_refused(tmp_path):
    path = tmp_path / "future.db"
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version = 99")
    db.commit()
    db.close()
    with pytest.raises(RuntimeError):
        Store(path)
