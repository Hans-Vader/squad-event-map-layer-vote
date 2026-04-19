#!/usr/bin/env python3
"""
Database layer for the Layer Vote Bot.

Uses SQLite with tables:
- guild_settings: per-guild configuration
- layer_cache: cached layer data from layers.json
- events: per-channel suggestion/voting cycles
- voting_history: past winning layers

Every public function accepts a guild_id to ensure full multi-guild isolation.
"""

import json
import os
import sqlite3
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger("layer_vote.db")

DB_FILE = os.path.join("data", "layer_vote.db")

# ---------------------------------------------------------------------------
# JSON helpers for datetime round-tripping
# ---------------------------------------------------------------------------

class _DateTimeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return {"__datetime__": obj.isoformat()}
        return super().default(obj)


def _datetime_hook(obj):
    if "__datetime__" in obj:
        return datetime.fromisoformat(obj["__datetime__"])
    return obj


def _dumps(obj) -> str:
    return json.dumps(obj, cls=_DateTimeEncoder)


def _loads(raw: str):
    return json.loads(raw, object_hook=_datetime_hook)


# ---------------------------------------------------------------------------
# Default guild settings
# ---------------------------------------------------------------------------

DEFAULT_GUILD_SETTINGS = {
    "organizer_role_id": 0,
    "log_channel_id": None,
    "language": "en",
    "allowed_gamemodes": ["AAS", "RAAS", "Invasion", "TerritoryControl", "Destruction", "Insurgency"],
    "blacklisted_maps": [],
    "blacklisted_gamemodes": [],
    "blacklisted_factions": [],
    "blacklisted_units": [],
    "max_suggestions_per_user": 2,
    "max_total_suggestions": 25,
    "history_lookback_events": 3,
    # Defaults used by /create_layer_suggestion when the corresponding
    # parameter is omitted. suggestion_start/duration are stored as duration
    # strings (e.g. "1h", "30m"); start is applied as an offset from now.
    "default_suggestion_start": None,
    "default_suggestion_duration": None,
    "default_voting_duration_hours": 24,
    "default_allow_multiple_votes": False,
}


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id     INTEGER PRIMARY KEY,
            settings     TEXT    NOT NULL,
            created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at   TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS layer_cache (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            raw_name                TEXT    UNIQUE NOT NULL,
            map_name                TEXT    NOT NULL,
            map_id                  TEXT    NOT NULL,
            gamemode                TEXT    NOT NULL,
            layer_version           TEXT,
            factions_json           TEXT    NOT NULL DEFAULT '[]',
            team1_allowed_alliances TEXT    NOT NULL DEFAULT '[]',
            team2_allowed_alliances TEXT    NOT NULL DEFAULT '[]',
            cached_at               TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_layer_cache_map
            ON layer_cache(map_name, gamemode);

        CREATE TABLE IF NOT EXISTS events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id     INTEGER NOT NULL,
            channel_id   INTEGER NOT NULL,
            event_data   TEXT    NOT NULL,
            status       TEXT    NOT NULL DEFAULT 'active',
            created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at   TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_events_guild
            ON events(guild_id, status);
        CREATE INDEX IF NOT EXISTS idx_events_channel
            ON events(guild_id, channel_id, status);

        CREATE TABLE IF NOT EXISTS voting_history (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id         INTEGER NOT NULL,
            channel_id       INTEGER NOT NULL,
            all_suggestions  TEXT    NOT NULL DEFAULT '[]',
            winning_layer    TEXT,
            completed_at     TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_history_guild
            ON voting_history(guild_id, channel_id, completed_at);
    """)
    conn.commit()
    conn.close()
    logger.info(f"Database initialised: {DB_FILE}")


# ---------------------------------------------------------------------------
# Guild settings
# ---------------------------------------------------------------------------

def get_guild_settings(guild_id: int) -> Optional[dict]:
    """Return settings dict for a guild, or None if not configured."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT settings FROM guild_settings WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    settings = _loads(row[0])
    merged = {**DEFAULT_GUILD_SETTINGS, **settings}
    return merged


def save_guild_settings(guild_id: int, settings: dict):
    """Upsert guild settings."""
    conn = _get_conn()
    with conn:
        conn.execute(
            """INSERT INTO guild_settings (guild_id, settings, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(guild_id) DO UPDATE SET settings=excluded.settings, updated_at=excluded.updated_at""",
            (guild_id, _dumps(settings)),
        )
    conn.close()


def get_guild_language(guild_id: int) -> str:
    """Shortcut: return language code for a guild."""
    settings = get_guild_settings(guild_id)
    if settings is None:
        return DEFAULT_GUILD_SETTINGS["language"]
    return settings.get("language", DEFAULT_GUILD_SETTINGS["language"])


def guild_is_configured(guild_id: int) -> bool:
    """Check if a guild has run /setup."""
    return get_guild_settings(guild_id) is not None


# ---------------------------------------------------------------------------
# Layer cache
# ---------------------------------------------------------------------------

def clear_layer_cache():
    """Delete all cached layers."""
    conn = _get_conn()
    with conn:
        conn.execute("DELETE FROM layer_cache")
    conn.close()


def upsert_layer(raw_name: str, map_name: str, map_id: str, gamemode: str,
                 layer_version: Optional[str], factions: list,
                 team1_alliances: list, team2_alliances: list):
    """Insert or update a single layer in the cache."""
    conn = _get_conn()
    with conn:
        conn.execute(
            """INSERT INTO layer_cache
               (raw_name, map_name, map_id, gamemode, layer_version,
                factions_json, team1_allowed_alliances, team2_allowed_alliances, cached_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(raw_name) DO UPDATE SET
                 map_name=excluded.map_name, map_id=excluded.map_id,
                 gamemode=excluded.gamemode, layer_version=excluded.layer_version,
                 factions_json=excluded.factions_json,
                 team1_allowed_alliances=excluded.team1_allowed_alliances,
                 team2_allowed_alliances=excluded.team2_allowed_alliances,
                 cached_at=excluded.cached_at""",
            (raw_name, map_name, map_id, gamemode, layer_version,
             json.dumps(factions), json.dumps(team1_alliances), json.dumps(team2_alliances)),
        )
    conn.close()


def get_layer_cache_count() -> int:
    """Return number of cached layers."""
    conn = _get_conn()
    row = conn.execute("SELECT COUNT(*) FROM layer_cache").fetchone()
    conn.close()
    return row[0] if row else 0


def get_unique_maps(excluded_maps: list[str] = None) -> list[str]:
    """Return sorted list of unique map names, optionally excluding some."""
    conn = _get_conn()
    rows = conn.execute("SELECT DISTINCT map_name FROM layer_cache ORDER BY map_name").fetchall()
    conn.close()
    maps = [r[0] for r in rows]
    if excluded_maps:
        excluded_lower = {m.lower() for m in excluded_maps}
        maps = [m for m in maps if not any(m.lower().startswith(e.lower()) for e in excluded_maps)]
    return maps


def get_modes_for_map(map_name: str, allowed_gamemodes: list[str] = None,
                      blacklisted_gamemodes: list[str] = None) -> list[dict]:
    """Return available mode+version combos for a map.

    Returns list of dicts with keys: gamemode, layer_version, raw_name, display
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT gamemode, layer_version, raw_name FROM layer_cache WHERE map_name = ? ORDER BY gamemode, layer_version",
        (map_name,),
    ).fetchall()
    conn.close()

    results = []
    for gamemode, version, raw_name in rows:
        if allowed_gamemodes and gamemode not in allowed_gamemodes:
            continue
        if blacklisted_gamemodes and gamemode in blacklisted_gamemodes:
            continue
        display = gamemode
        if version:
            display = f"{gamemode} {version}"
        results.append({
            "gamemode": gamemode,
            "layer_version": version,
            "raw_name": raw_name,
            "display": display,
        })
    return results


def get_layer_by_raw_name(raw_name: str) -> Optional[dict]:
    """Return full layer data by raw_name."""
    conn = _get_conn()
    row = conn.execute(
        """SELECT raw_name, map_name, map_id, gamemode, layer_version,
                  factions_json, team1_allowed_alliances, team2_allowed_alliances
           FROM layer_cache WHERE raw_name = ?""",
        (raw_name,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return {
        "raw_name": row[0],
        "map_name": row[1],
        "map_id": row[2],
        "gamemode": row[3],
        "layer_version": row[4],
        "factions": json.loads(row[5]),
        "team1_allowed_alliances": json.loads(row[6]),
        "team2_allowed_alliances": json.loads(row[7]),
    }


def get_unique_factions() -> list[str]:
    """Return sorted list of all unique faction IDs from cached layers."""
    conn = _get_conn()
    rows = conn.execute("SELECT factions_json FROM layer_cache").fetchall()
    conn.close()
    factions = set()
    for (fj,) in rows:
        for f in json.loads(fj):
            if isinstance(f, dict) and "factionId" in f:
                factions.add(f["factionId"])
            elif isinstance(f, str):
                factions.add(f)
    return sorted(factions)


def get_unique_unit_types() -> list[str]:
    """Return sorted list of all unique unit type names from cached layers."""
    conn = _get_conn()
    rows = conn.execute("SELECT factions_json FROM layer_cache").fetchall()
    conn.close()
    units = set()
    for (fj,) in rows:
        for f in json.loads(fj):
            if isinstance(f, dict):
                for ut in f.get("unitTypes", []):
                    if isinstance(ut, dict):
                        units.add(ut.get("type", ""))
                    elif isinstance(ut, str):
                        units.add(ut)
    units.discard("")
    return sorted(units)


def get_unique_gamemodes() -> list[str]:
    """Return sorted list of all unique gamemodes from cached layers."""
    conn = _get_conn()
    rows = conn.execute("SELECT DISTINCT gamemode FROM layer_cache ORDER BY gamemode").fetchall()
    conn.close()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Events — one active event per (guild, channel)
# ---------------------------------------------------------------------------

def get_event_by_channel(guild_id: int, channel_id: int) -> Optional[dict]:
    """Return the active event for a channel, or None."""
    conn = _get_conn()
    row = conn.execute(
        """SELECT id, event_data
           FROM events
           WHERE guild_id = ? AND channel_id = ? AND status = 'active'
           LIMIT 1""",
        (guild_id, channel_id),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    event_data = _loads(row[1])
    return {"db_id": row[0], "event": event_data}


def get_all_active_events_global() -> list[dict]:
    """Return all active events across all guilds (for background tasks)."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT id, guild_id, channel_id, event_data
           FROM events WHERE status = 'active'"""
    ).fetchall()
    conn.close()
    return [
        {
            "db_id": row[0],
            "guild_id": row[1],
            "channel_id": row[2],
            "event": _loads(row[3]),
        }
        for row in rows
    ]


def save_event(db_id: int, event_data: dict):
    """Update an existing event row."""
    conn = _get_conn()
    with conn:
        conn.execute(
            """UPDATE events
               SET event_data = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (_dumps(event_data), db_id),
        )
    conn.close()


def create_event(guild_id: int, channel_id: int, event_data: dict) -> int:
    """Insert a new active event. Returns the new row id."""
    conn = _get_conn()
    with conn:
        cur = conn.execute(
            """INSERT INTO events (guild_id, channel_id, event_data, status)
               VALUES (?, ?, ?, 'active')""",
            (guild_id, channel_id, _dumps(event_data)),
        )
        new_id = cur.lastrowid
    conn.close()
    logger.info(f"Event created: db_id={new_id}, guild={guild_id}, channel={channel_id}")
    return new_id


def complete_event(db_id: int):
    """Mark an event as completed (soft-delete)."""
    conn = _get_conn()
    with conn:
        conn.execute(
            "UPDATE events SET status = 'completed_' || id, updated_at = datetime('now') WHERE id = ?",
            (db_id,),
        )
    conn.close()
    logger.info(f"Event completed: db_id={db_id}")


def delete_event(db_id: int):
    """Mark an event as deleted (soft-delete)."""
    conn = _get_conn()
    with conn:
        conn.execute(
            "UPDATE events SET status = 'deleted_' || id, updated_at = datetime('now') WHERE id = ?",
            (db_id,),
        )
    conn.close()
    logger.info(f"Event deleted: db_id={db_id}")


def channel_has_active_event(guild_id: int, channel_id: int) -> bool:
    """Check if a channel already has an active event."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT 1 FROM events WHERE guild_id = ? AND channel_id = ? AND status = 'active' LIMIT 1",
        (guild_id, channel_id),
    ).fetchone()
    conn.close()
    return row is not None


# ---------------------------------------------------------------------------
# Voting history
# ---------------------------------------------------------------------------

def save_voting_history(guild_id: int, channel_id: int, all_suggestions: list,
                        winning_layer: Optional[dict]):
    """Save completed event to voting history."""
    conn = _get_conn()
    with conn:
        conn.execute(
            """INSERT INTO voting_history (guild_id, channel_id, all_suggestions, winning_layer)
               VALUES (?, ?, ?, ?)""",
            (guild_id, channel_id, _dumps(all_suggestions),
             _dumps(winning_layer) if winning_layer else None),
        )
    conn.close()


def get_recent_history(guild_id: int, channel_id: int, limit: int = 10) -> list[dict]:
    """Return recent voting history entries."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT id, all_suggestions, winning_layer, completed_at
           FROM voting_history
           WHERE guild_id = ? AND channel_id = ?
           ORDER BY completed_at DESC LIMIT ?""",
        (guild_id, channel_id, limit),
    ).fetchall()
    conn.close()
    return [
        {
            "id": row[0],
            "all_suggestions": _loads(row[1]),
            "winning_layer": _loads(row[2]) if row[2] else None,
            "completed_at": row[3],
        }
        for row in rows
    ]


def delete_voting_history_entry(entry_id: int) -> bool:
    """Delete a single voting_history row. Returns True if a row was removed."""
    conn = _get_conn()
    with conn:
        cur = conn.execute("DELETE FROM voting_history WHERE id = ?", (entry_id,))
        removed = cur.rowcount > 0
    conn.close()
    if removed:
        logger.info(f"Voting history entry deleted: id={entry_id}")
    return removed


def get_blocked_suggestions(guild_id: int, channel_id: int, lookback: int) -> list[dict]:
    """Return all suggestions from the last `lookback` events for blocking.

    Includes both the full list of suggestions made during each event AND the
    recorded winning_layer, so that manual history entries that only carry a
    winner still get blocked.
    """
    history = get_recent_history(guild_id, channel_id, limit=lookback)
    blocked = []
    for entry in history:
        for suggestion in entry.get("all_suggestions", []):
            blocked.append(suggestion)
        winner = entry.get("winning_layer")
        if winner:
            blocked.append(winner)
    return blocked


def build_default_event(suggestion_start_time=None) -> dict:
    """Build a fresh event data dict."""
    return {
        "phase": "created",
        "event_message_id": None,
        "suggestion_start_time": suggestion_start_time,
        "suggestion_end_time": None,
        "suggestion_duration_seconds": None,
        "voting_start_time": None,
        "voting_duration_hours": 24,
        "poll_message_id": None,
        "max_voting_layers": 10,
        "selected_for_vote": [],
        "winning_layer": None,
        "allow_multiple_votes": False,
        "suggestions": [],
    }
