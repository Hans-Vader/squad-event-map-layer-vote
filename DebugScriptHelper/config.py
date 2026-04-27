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
#   • a single URL                       → LAYERS_JSON_URL=https://a.com/layers.json
#   • a comma-separated list             → LAYERS_JSON_URL=https://a.com/x.json,https://b.com/y.json
#   • a JSON array (for URLs with commas) → LAYERS_JSON_URL=["https://a.com/x.json","https://b.com/y.json"]
# When the same rawName appears in multiple sources, the source listed later wins.
_DEFAULT_LAYERS_JSON_URL = (
    "https://raw.githubusercontent.com/fantinodavide/SquadLayerList/refs/heads/main/layers.json"
)


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


LAYERS_JSON_URLS = _parse_layers_json_urls(os.getenv("LAYERS_JSON_URL", ""))

# ── SquadCalc link in embeds ──────────────────────────────────────────────
SQUADCALC_BASE_URL = os.getenv("SQUADCALC_BASE_URL", "").rstrip("/")

# ── Debug mode ────────────────────────────────────────────────────────────
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
