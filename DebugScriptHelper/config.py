#!/usr/bin/env python3
"""
Minimal configuration for the Layer Vote Bot.

Only the Discord bot token is read from environment variables.
All server-specific settings (organizer role, blacklists, language, etc.)
are stored per-guild in the database and managed via /setup and /config_* commands.
"""

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

# ── Layer data source ─────────────────────────────────────────────────────
LAYERS_JSON_URL = os.getenv(
    "LAYERS_JSON_URL",
    "https://raw.githubusercontent.com/fantinodavide/SquadLayerList/refs/heads/main/layers.json",
)

# ── SquadCalc link in embeds ──────────────────────────────────────────────
SQUADCALC_BASE_URL = os.getenv("SQUADCALC_BASE_URL", "").rstrip("/")

# ── Debug mode ────────────────────────────────────────────────────────────
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
