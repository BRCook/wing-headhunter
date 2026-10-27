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


def test_buttons(store):
    store.add(COMPACT, COMPACT_CARD, "COMPACT", FOH)
    store.set_buttons(COMPACT.mac, 3, 4)
    pairing = store.by_display_name("COMPACT")
    assert (pairing.gain_gpio, pairing.phantom_gpio) == (3, 4)
    for gain, phantom in [(5, None), (3, 3), (0, 1)]:
        with pytest.raises(ValueError):
            store.set_buttons(COMPACT.mac, gain, phantom)


def test_version_2_databases_are_upgraded(tmp_path):
    path = tmp_path / "v2.db"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE consoles (console_mac TEXT PRIMARY KEY, console_name TEXT NOT NULL,
            console_model TEXT NOT NULL, console_serial TEXT NOT NULL, dante_mac TEXT NOT NULL UNIQUE,
            dante_name TEXT NOT NULL, display_name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'STAGE');
        INSERT INTO consoles VALUES ('m', 'n', 'wing-compact', 's', 'd', 'WingCompact', 'COMPACT', 't', 't', 'FOH');
        PRAGMA user_version = 2;
        """
    )
    db.commit()
    db.close()
    with Store(path) as store:
        pairing = store.pairings()[0]
        assert (pairing.role, pairing.gain_gpio, pairing.phantom_gpio) == (FOH, None, None)
        store.set_buttons("m", 3, 4)


def test_display_names_are_unique_ignoring_case(store):
    store.add(RACK, RACK_CARD, "RACK")
    with pytest.raises(ValueError):
        store.add(COMPACT, COMPACT_CARD, "rack")


def test_version_3_databases_are_upgraded(tmp_path):
    path = tmp_path / "v3.db"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE consoles (console_mac TEXT PRIMARY KEY, console_name TEXT NOT NULL,
            console_model TEXT NOT NULL, console_serial TEXT NOT NULL, dante_mac TEXT NOT NULL UNIQUE,
            dante_name TEXT NOT NULL, display_name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'STAGE', gain_gpio INTEGER, phantom_gpio INTEGER);
        INSERT INTO consoles VALUES ('m', 'n', 'wing-rack', 's', 'd', 'WingRack', 'RACK', 't', 't', 'STAGE', NULL, NULL);
        PRAGMA user_version = 3;
        """
    )
    db.commit()
    db.close()
    with Store(path) as store:
        assert store.by_display_name("rack").display_name == "RACK"
        with pytest.raises(ValueError):
            store.add(COMPACT, COMPACT_CARD, "Rack")


def test_settings_default_and_change(store):
    from headhunter.store import Settings

    assert store.settings() == Settings()
    store.set_setting("write_gain", "on")
    store.set_setting("restore_after", "1.5")
    store.set_setting("web_port", "9000")
    settings = store.settings()
    assert (settings.write_gain, settings.restore_after, settings.web_port) == (True, 1.5, 9000)


@pytest.mark.parametrize(
    "key, text",
    [("write_gain", "maybe"), ("restore_after", "0"), ("restore_after", "abc"), ("web_port", "80"), ("colour", "red")],
)
def test_bad_settings_are_refused(store, key, text):
    with pytest.raises(ValueError):
        store.set_setting(key, text)


def test_changes_from_elsewhere_are_noticed(tmp_path):
    path = tmp_path / "headhunter.db"
    with Store(path) as watcher, Store(path) as other:
        before = watcher.data_version()
        other.set_setting("write_names", "on")
        assert watcher.data_version() != before
        assert watcher.settings().write_names
