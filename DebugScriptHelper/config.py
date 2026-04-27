#!/usr/bin/env python3
"""
Minimal configuration for the Layer Vote Bot.

Only the Discord bot token is read from environment variables.
All server-specific settings (organizer role, blacklists, language, etc.)
are stored per-guild in the database and managed via /setup and /config_* commands.
"""

import json
import os
import logging
import re
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("config")

# ── The only environment variable ─────────────────────────────────────────
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    logger.warning("DISCORD_BOT_TOKEN not found in environment variables")

# ── Optional: admin user IDs for bot-level superadmin (bypass all checks) ─
_admin_ids_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [aid.strip() for aid in _admin_ids_raw.split(",") if aid.strip()]

# ── Background task interval (seconds) ────────────────────────────────────
EVENT_CHECK_INTERVAL = 10
EVENT_CHECK_INTERVAL_FAST = 1
EVENT_CRITICAL_WINDOW = 60

if EVENT_CRITICAL_WINDOW <= EVENT_CHECK_INTERVAL:
    raise ValueError(
        f"EVENT_CRITICAL_WINDOW ({EVENT_CRITICAL_WINDOW}s) must be greater than "
        f"EVENT_CHECK_INTERVAL ({EVENT_CHECK_INTERVAL}s)."
    )

# ── Layer data source(s) ──────────────────────────────────────────────────
# LAYERS_JSON_URL accepts:
#   • a single URL                        → LAYERS_JSON_URL=https://a.com/layers.json
#   • a comma-separated list              → LAYERS_JSON_URL=https://a.com/x.json,https://b.com/y.json
#   • a JSON array (for URLs with commas) → LAYERS_JSON_URL=["https://a.com/x.json","https://b.com/y.json"]
# Each source is given a short name derived from the path segment immediately
# before "/layers.json" — e.g. ".../refs/heads/main/layers.json" → "main",
# ".../mods/supermod/layers.json" → "supermod". Two URLs that resolve to the
# same name will raise a fatal error at startup.
_DEFAULT_LAYERS_JSON_URL = (
    "https://raw.githubusercontent.com/fantinodavide/SquadLayerList/refs/heads/main/layers.json"
)
_SOURCE_NAME_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _parse_layers_json_urls(raw: str) -> list[str]:
    raw = (raw or "").strip()
    if not raw:
        return [_DEFAULT_LAYERS_JSON_URL]
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("LAYERS_JSON_URL looks like JSON but failed to parse; falling back to comma-split")
        else:
            if isinstance(parsed, list):
                urls = [str(u).strip() for u in parsed if str(u).strip()]
                if urls:
                    return urls
    return [u.strip() for u in raw.split(",") if u.strip()]


def derive_source_name(url: str) -> str:
    """Derive a short source name from a layers.json URL.

    Takes the path segment immediately before "/layers.json". If the URL does
    not end in "/layers.json", falls back to the last non-empty path segment
    (with any ".json" extension stripped). Non-alphanumeric characters are
    sanitized to "_". Returns "default" if nothing usable is found.
    """
    path = urlparse(url).path
    parts = [p for p in path.split("/") if p]
    name = ""
    if parts and parts[-1].lower() == "layers.json" and len(parts) >= 2:
        name = parts[-2]
    elif parts:
        # Fallback for URLs that don't end in /layers.json — strip trailing extension
        name = parts[-1]
        if "." in name:
            name = name.rsplit(".", 1)[0]
    name = _SOURCE_NAME_SANITIZE_RE.sub("_", name).strip("_")
    return name or "default"


def _build_layers_json_sources(urls: list[str]) -> list[tuple[str, str]]:
    """Pair each URL with its derived source name. Raises on name collisions."""
    sources: list[tuple[str, str]] = []
    seen: dict[str, str] = {}
    for url in urls:
        name = derive_source_name(url)
        if name in seen:
            raise ValueError(
                f"LAYERS_JSON_URL: source name '{name}' is derived from multiple URLs "
                f"({seen[name]!r} and {url!r}). Each layers.json URL must produce a "
                f"unique name (derived from the path segment before '/layers.json')."
            )
        seen[name] = url
        sources.append((name, url))
    return sources


LAYERS_JSON_URLS = _parse_layers_json_urls(os.getenv("LAYERS_JSON_URL", ""))
LAYERS_JSON_SOURCES = _build_layers_json_sources(LAYERS_JSON_URLS)

# ── Layer exclusions — never cache these maps / gamemodes ────────────────
# mapId substrings: any layer whose mapId contains one of these is excluded.
EXCLUDED_MAP_ID_SUBSTRINGS: tuple[str, ...] = (
    "JensensRange",
    "JesensRange",           # typo variant found in source data
    "Supermod_JensensRange",
)

# mapName prefixes: any layer whose resolved mapName starts with one of these is excluded.
EXCLUDED_MAP_NAME_PREFIXES: tuple[str, ...] = (
    "Jensen",
    "Tutorial",
)

# Exact gamemode values to exclude.
EXCLUDED_GAMEMODES: frozenset[str] = frozenset({"Training"})


def is_excluded_layer(map_id: str, map_name: str, gamemode: str) -> bool:
    if any(s in map_id for s in EXCLUDED_MAP_ID_SUBSTRINGS):
        return True
    if any(map_name.startswith(p) for p in EXCLUDED_MAP_NAME_PREFIXES):
        return True
    if gamemode in EXCLUDED_GAMEMODES:
        return True
    return False


# ── SquadCalc link in embeds ──────────────────────────────────────────────
SQUADCALC_BASE_URL = os.getenv("SQUADCALC_BASE_URL", "").rstrip("/")

# ── Debug mode ────────────────────────────────────────────────────────────
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
