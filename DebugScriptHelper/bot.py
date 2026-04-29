#!/usr/bin/env python3
"""
Layer Vote Bot — Main bot file.

Handles slash commands, interactive views (buttons, dropdowns),
background tasks for scheduled events, and layer cache management.
"""

import asyncio
import json
import logging
import re
import sys
import uuid
from datetime import datetime, timedelta
from typing import Optional

import aiohttp
import discord
from discord import app_commands, ui
from discord.ext import commands

import database as db
from config import TOKEN, ADMIN_IDS, EVENT_CHECK_INTERVAL, EVENT_CHECK_INTERVAL_FAST, EVENT_CRITICAL_WINDOW, LAYERS_JSON_SOURCES, DEBUG_MODE, is_excluded_layer
from i18n import t
from utils import (
    has_organizer_role, is_guild_admin,
    format_layer_short, format_layer_poll_option, suggestion_matches,
    build_event_embed, build_settings_embed,
    set_log_channel, send_to_log_channel,
)

logger = logging.getLogger("layer_vote")

if DEBUG_MODE:
    logging.getLogger().setLevel(logging.DEBUG)

# ---------------------------------------------------------------------------
# Token check
# ---------------------------------------------------------------------------
if not TOKEN:
    logger.critical("DISCORD_BOT_TOKEN not set. Exiting.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Duration parsing ("60" -> 3600s, "2h" -> 7200s, "1d" -> 86400s)
# ---------------------------------------------------------------------------

def parse_duration_to_seconds(value: str) -> Optional[int]:
    """Parse a duration string. Bare numbers are treated as minutes.

    Returns seconds (clamped to [60, 30*86400]) or None when unparseable/empty.
    """
    if not value:
        return None
    v = value.strip().lower()
    if not v:
        return None
    mult = 60  # default: minutes
    if v.endswith("m"):
        v, mult = v[:-1], 60
    elif v.endswith("h"):
        v, mult = v[:-1], 3600
    elif v.endswith("d"):
        v, mult = v[:-1], 86400
    elif v.endswith("w"):
        v, mult = v[:-1], 7 * 86400
    try:
        seconds = int(float(v) * mult)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return max(60, min(seconds, 30 * 86400))


# Max voting duration: two weeks, in hours.
MAX_VOTING_DURATION_HOURS = 2 * 7 * 24  # 336h


def parse_voting_duration_to_hours(value: str) -> Optional[int]:
    """Parse a voting-duration string into hours.

    Bare numbers are treated as hours (e.g. "24" = 24h). Suffixes h/d/w are
    supported. Result is clamped to [1, MAX_VOTING_DURATION_HOURS]. Returns
    None if the input is empty or unparseable.
    """
    if value is None:
        return None
    v = str(value).strip().lower()
    if not v:
        return None
    mult_hours = 1  # default: hours
    if v.endswith("h"):
        v = v[:-1]
    elif v.endswith("d"):
        v, mult_hours = v[:-1], 24
    elif v.endswith("w"):
        v, mult_hours = v[:-1], 7 * 24
    try:
        hours = int(float(v) * mult_hours)
    except (TypeError, ValueError):
        return None
    if hours <= 0:
        return None
    return max(1, min(hours, MAX_VOTING_DURATION_HOURS))

# ---------------------------------------------------------------------------
# Map name overrides (shorten long names at import time)
# ---------------------------------------------------------------------------
_MAP_NAME_OVERRIDES = {
    "Kamdesh Highlands": "Kamdesh",
    "Pacific Proving Grounds": "Pacific",
    "Tallil Outskirts": "Tallil",
    "Sanxian Islands": "Sanxian",
    "Lashkar Valley": "Lashkar",
    "Logar Valley": "Logar",
    "Sumari Bala": "Sumari"
}

# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
intents.members = True


class LayerVoteBot(commands.Bot):
    async def setup_hook(self):
        # Each active event owns its own message-bound view whose buttons carry
        # the event's db_id. Re-bind one view per active event on startup so
        # button clicks survive bot restarts.
        try:
            for record in db.get_all_active_events_global():
                msg_id = record["event"].get("event_message_id")
                if not msg_id:
                    continue
                lang = db.get_guild_language(record["guild_id"])
                self.add_view(
                    EventActionView(record["db_id"], lang),
                    message_id=msg_id,
                )
        except Exception as e:
            logger.warning(f"Failed to re-attach event views on startup: {e}")
        await self.tree.sync()
        logger.info("Slash commands synced and event views re-attached.")


bot = LayerVoteBot(command_prefix="!", intents=intents)

# Per-guild locks for concurrency safety
_guild_locks: dict[int, asyncio.Lock] = {}


def _get_guild_lock(guild_id: int) -> asyncio.Lock:
    if guild_id not in _guild_locks:
        _guild_locks[guild_id] = asyncio.Lock()
    return _guild_locks[guild_id]


# ---------------------------------------------------------------------------
# Helpers — precondition checks
# ---------------------------------------------------------------------------

async def check_guild_configured(interaction: discord.Interaction) -> Optional[dict]:
    """Check guild is configured, respond with error if not. Returns settings or None."""
    settings = db.get_guild_settings(interaction.guild_id)
    if settings is None:
        lang = db.get_guild_language(interaction.guild_id)
        await interaction.response.send_message(t("general.guild_not_configured", lang), ephemeral=True)
        return None
    return settings


async def check_organizer(interaction: discord.Interaction, settings: dict) -> bool:
    """Check user has organizer role. Responds with error if not. Returns True if OK."""
    lang = settings.get("language", "en")
    if not has_organizer_role(interaction.user, settings.get("organizer_role_id", 0)):
        await interaction.response.send_message(t("general.requires_organizer", lang), ephemeral=True)
        return False
    return True


async def _resolve_channel_event(interaction: discord.Interaction,
                                 lang: str) -> Optional[int]:
    """For slash commands acting on "the event in this channel": return the
    db_id when exactly one active event lives here. Replies with an error and
    returns None when there are zero (or multiple) — multi-event channels
    must be addressed via the embed buttons, which carry db_id explicitly.
    """
    events = db.get_active_events_in_channel(interaction.guild_id, interaction.channel_id)
    if not events:
        await interaction.response.send_message(t("event.no_event", lang), ephemeral=True)
        return None
    if len(events) > 1:
        await interaction.response.send_message(t("event.multiple_in_channel", lang), ephemeral=True)
        return None
    return events[0]["db_id"]


async def check_admin(interaction: discord.Interaction) -> bool:
    """Check user is a Discord admin. Responds with error if not."""
    if not is_guild_admin(interaction.user):
        lang = db.get_guild_language(interaction.guild_id)
        await interaction.response.send_message(t("general.requires_admin", lang), ephemeral=True)
        return False
    return True


# ═══════════════════════════════════════════════════════════════════════════
# LAYER CACHE — fetch & parse layers.json
# ═══════════════════════════════════════════════════════════════════════════

async def fetch_and_cache_layers() -> int:
    """Fetch layers.json from each configured source URL and populate layer_cache.

    Each source is stored independently in the cache, tagged with its derived
    name (the path segment immediately before /layers.json — e.g. "main",
    "supermod"). Sources that fail (network error, non-200, malformed JSON) are
    logged and skipped. Within a single source, layers with duplicate rawName
    are deduped (last wins).

    Returns the total number of cached layer rows across all sources.
    Raises if no source returned data — leaves the existing cache untouched.
    """
    fetched: list[tuple[str, str, object]] = []  # (source_name, url, payload)
    async with aiohttp.ClientSession() as session:
        for source_name, url in LAYERS_JSON_SOURCES:
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "layers.json HTTP %s from source '%s' (%s) — skipping",
                            resp.status, source_name, url,
                        )
                        continue
                    fetched.append((source_name, url, await resp.json(content_type=None)))
            except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError, ValueError) as e:
                logger.warning(
                    "layers.json fetch failed from source '%s' (%s): %s — skipping",
                    source_name, url, e,
                )

    if not fetched:
        raise RuntimeError("No layer sources returned data — cache not refreshed")

    db.clear_layer_cache()
    count = 0

    for source_name, source_url, data in fetched:
        layers_list = data.get("Maps", data) if isinstance(data, dict) else data
        if not isinstance(layers_list, list):
            logger.warning(
                "layers.json from source '%s' (%s) did not contain a list — skipping",
                source_name, source_url,
            )
            continue

        # Build factionID → {alliance, factionName} from the source's Units block —
        # this is the source of truth and covers SuperMod factions (SU_*) the
        # hardcoded ALLIANCE_FACTIONS map doesn't know about.
        faction_meta = _build_faction_meta_map(data)

        # Within a single source, dedupe by rawName (last wins).
        unique: dict[str, dict] = {}
        for layer in layers_list:
            if not isinstance(layer, dict):
                continue
            raw_name = layer.get("rawName") or layer.get("Name", "")
            if raw_name:
                unique[raw_name] = layer

        count += await _cache_source_layers(source_name, unique.values(), faction_meta)

    return count


def _build_faction_meta_map(data: object) -> dict[str, dict]:
    """Extract {factionID: {"alliance", "factionName"}} from the source's Units block."""
    if not isinstance(data, dict):
        return {}
    units = data.get("Units")
    if not isinstance(units, dict):
        return {}
    result: dict[str, dict] = {}
    for unit in units.values():
        if not isinstance(unit, dict):
            continue
        fid = unit.get("factionID")
        if not fid:
            continue
        entry = result.setdefault(fid, {"alliance": "", "factionName": ""})
        if not entry["alliance"]:
            alliance = unit.get("alliance") or ""
            if alliance:
                entry["alliance"] = alliance
        if not entry["factionName"]:
            faction_name = unit.get("factionName") or ""
            if faction_name:
                entry["factionName"] = faction_name
    return result


_MAP_SIZE_RE = re.compile(r"(-?[\d.]+)\s*x\s*(-?[\d.]+)", re.IGNORECASE)


def _parse_map_size_km(raw: str) -> Optional[float]:
    """Parse '4.0x4.0 km' / '1.2x1.2 km' → max(width, height) in km.

    Returns None for unparseable, zero, or otherwise unusable values. Negative
    components (one source has '-4.1x4.1 km') are treated as their absolute
    value — same magnitude, just a sign typo upstream.
    """
    if not raw:
        return None
    m = _MAP_SIZE_RE.search(raw)
    if not m:
        return None
    try:
        w, h = abs(float(m.group(1))), abs(float(m.group(2)))
    except ValueError:
        return None
    if w == 0 or h == 0:
        return None
    return max(w, h)


async def _cache_source_layers(source_name: str, layers,
                               faction_meta: dict[str, dict] = None) -> int:
    """Parse and upsert each layer for a single source. Returns count cached."""
    faction_meta = faction_meta or {}
    count = 0
    for layer in layers:
        raw_name = layer.get("rawName") or layer.get("Name", "")
        map_name = _MAP_NAME_OVERRIDES.get(
            layer.get("mapName") or layer.get("Map", ""),
            layer.get("mapName") or layer.get("Map", ""),
        )
        map_id = layer.get("mapId") or ""
        gamemode = layer.get("gamemode") or ""
        layer_version = layer.get("layerVersion") or None
        # Parse version from rawName when layerVersion is missing (e.g. AlBasrah_AAS_v3_CL)
        if not layer_version and raw_name:
            m = re.search(r"_v(\d+)", raw_name)
            if m:
                layer_version = f"v{m.group(1)}"

        if not raw_name or not map_name or not gamemode:
            continue

        if is_excluded_layer(map_id, map_name, gamemode):
            continue

        # Extract factions with their unit types, default unit, and team availability.
        # Entries are kept verbatim (no dedup) because the same factionId can appear
        # once per team with a different defaultUnit (e.g. ADF_LO_* for team1,
        # ADF_LD_* for team2 on Invasion layers).
        factions_data = []
        raw_factions = layer.get("factions") or []
        for fac in raw_factions:
            if isinstance(fac, dict):
                fac_id = fac.get("factionId", "")
                default_unit = fac.get("defaultUnit", "") or ""
                available_on_teams = fac.get("availableOnTeams") or []
                unit_types = []
                # Prepend the default unit (e.g. "CombinedArms") — it's never
                # listed in `types` but is always a valid selection.
                default_type = _extract_default_unit_type(default_unit, fac_id)
                if default_type:
                    unit_types.append({"type": default_type, "name": default_type})
                for ut in fac.get("types", []):
                    if isinstance(ut, str):
                        if ut != default_type:
                            unit_types.append({"type": ut, "name": ut})
                    elif isinstance(ut, dict):
                        ut_type = ut.get("type", "")
                        if ut_type != default_type:
                            unit_types.append({
                                "type": ut_type,
                                "name": ut.get("name", ut_type),
                            })
                if fac_id:
                    meta = faction_meta.get(fac_id, {})
                    factions_data.append({
                        "factionId": fac_id,
                        "factionName": meta.get("factionName", ""),
                        "defaultUnit": default_unit,
                        "availableOnTeams": available_on_teams,
                        "unitTypes": unit_types,
                        "alliance": meta.get("alliance", ""),
                    })
            elif isinstance(fac, str):
                meta = faction_meta.get(fac, {})
                factions_data.append({
                    "factionId": fac,
                    "factionName": meta.get("factionName", ""),
                    "defaultUnit": "",
                    "availableOnTeams": [],
                    "unitTypes": [],
                    "alliance": meta.get("alliance", ""),
                })

        # Extract team alliance restrictions
        team_configs = layer.get("teamConfigs", {})
        t1_alliances = []
        t2_alliances = []
        if isinstance(team_configs, dict):
            t1 = team_configs.get("team1") or team_configs.get("Team1") or {}
            t2 = team_configs.get("team2") or team_configs.get("Team2") or {}
            if isinstance(t1, dict):
                t1_alliances = t1.get("allowedAlliances", [])
            if isinstance(t2, dict):
                t2_alliances = t2.get("allowedAlliances", [])

        db.upsert_layer(
            raw_name=raw_name,
            source=source_name,
            map_name=map_name,
            map_id=map_id,
            gamemode=gamemode,
            layer_version=layer_version,
            factions=factions_data,
            team1_alliances=t1_alliances,
            team2_alliances=t2_alliances,
            map_size_km=_parse_map_size_km(layer.get("mapSize", "")),
        )
        count += 1

    return count


def get_factions_for_team(layer_data: dict, team: int,
                          blacklisted_factions: list[str] = None,
                          blacklisted_units: list[str] = None,
                          exclude_faction: str = None) -> list[dict]:
    """Get available factions for a team, respecting alliance restrictions and blacklists.

    Returns list of dicts: {factionId, unitTypes: [{type, name}]}
    """
    alliances_key = f"team{team}_allowed_alliances"
    allowed_alliances = layer_data.get(alliances_key, [])
    allowed_alliance_set = set(allowed_alliances) if allowed_alliances else set()

    # Fallback alliance → faction mapping for cached rows that predate the
    # per-faction alliance field. New caches store `alliance` directly on each
    # faction (sourced from the JSON's Units block), which covers SuperMod
    # (SU_*) and any other modded factions this map doesn't list.
    ALLIANCE_FACTIONS = {
        "BLUFOR": {"USA", "USMC", "BAF", "CAF", "ADF"},
        "REDFOR": {"RGF", "VDV", "PLA", "PLANMC", "PLAAGF"},
        "INDEPENDENT": {"IMF", "MEI", "TLF", "CRF", "GFI"},
        "PAC": {"PLA", "PLANMC", "PLAAGF"},
    }

    fallback_faction_ids = set()
    if allowed_alliances:
        for alliance in allowed_alliances:
            fallback_faction_ids |= ALLIANCE_FACTIONS.get(alliance, set())

    factions = layer_data.get("factions", [])
    seen_ids = set()
    result = []
    for fac in factions:
        fac_id = fac.get("factionId", "") if isinstance(fac, dict) else fac
        if not fac_id:
            continue
        # Filter by availableOnTeams when present — on layers like Invasion the
        # same factionId appears twice, once per team, with different defaultUnits.
        if isinstance(fac, dict):
            available = fac.get("availableOnTeams") or []
            if available and team not in available:
                continue
        if fac_id in seen_ids:
            continue
        if allowed_alliances:
            fac_alliance = fac.get("alliance", "") if isinstance(fac, dict) else ""
            if fac_alliance:
                if fac_alliance not in allowed_alliance_set:
                    continue
            elif fac_id not in fallback_faction_ids:
                continue
        if blacklisted_factions and fac_id in blacklisted_factions:
            continue
        if exclude_faction and fac_id == exclude_faction:
            continue

        seen_ids.add(fac_id)
        unit_types = []
        default_unit = ""
        faction_name = ""
        if isinstance(fac, dict):
            default_unit = fac.get("defaultUnit", "") or ""
            faction_name = fac.get("factionName", "") or ""
            for ut in fac.get("unitTypes", fac.get("types", [])):
                ut_type = ut.get("type", "") if isinstance(ut, dict) else ut
                if blacklisted_units and ut_type in blacklisted_units:
                    continue
                if ut_type:
                    unit_types.append(ut if isinstance(ut, dict) else {"type": ut, "name": ut})

        result.append({
            "factionId": fac_id,
            "factionName": faction_name,
            "defaultUnit": default_unit,
            "unitTypes": unit_types,
        })
    return result


def get_unit_types_for_faction(factions: list[dict], faction_id: str,
                               blacklisted_units: list[str] = None,
                               team: int = None) -> list[dict]:
    """Get available unit types for a specific faction.

    When `team` is given, prefers the faction entry whose availableOnTeams
    matches — Invasion-style layers keep separate entries per team and they
    can expose different unit types.
    """
    fallback = None
    for fac in factions:
        fac_id = fac.get("factionId", "") if isinstance(fac, dict) else fac
        if fac_id != faction_id:
            continue
        units = (fac.get("unitTypes", fac.get("types", []))) if isinstance(fac, dict) else []
        if blacklisted_units:
            units = [u for u in units if (u.get("type", "") if isinstance(u, dict) else u) not in blacklisted_units]
        if team is not None and isinstance(fac, dict):
            available = fac.get("availableOnTeams") or []
            if available and team not in available:
                if fallback is None:
                    fallback = units
                continue
        return units
    return fallback or []


def get_faction_entry_for_team(factions: list[dict], faction_id: str,
                               team: int) -> Optional[dict]:
    """Return the faction entry for (factionId, team), or None.

    Prefers an entry listing the team in availableOnTeams; falls back to the
    first matching factionId when no team info is stored (older cache rows).
    """
    fallback = None
    for fac in factions:
        if not isinstance(fac, dict) or fac.get("factionId") != faction_id:
            continue
        available = fac.get("availableOnTeams") or []
        if not available:
            fallback = fallback or fac
            continue
        if team in available:
            return fac
    return fallback


def _resolve_unit_prefix(layer_data: dict, faction_id: str, team: int) -> Optional[str]:
    """Look up the unit prefix (LO, LD, MO, S, …) for a faction on a team."""
    if not layer_data or not faction_id:
        return None
    entry = get_faction_entry_for_team(layer_data.get("factions", []), faction_id, team)
    if not entry:
        return None
    return extract_unit_prefix(entry.get("defaultUnit", ""), faction_id)


def _resolve_faction_name(layer_data: dict, faction_id: str, team: int) -> str:
    """Look up the human-readable factionName for a faction on a team.

    Falls back to "" when the layer data has no entry for that faction.
    """
    if not layer_data or not faction_id:
        return ""
    entry = get_faction_entry_for_team(layer_data.get("factions", []), faction_id, team)
    if not entry:
        return ""
    return entry.get("factionName", "") or ""


def _faction_select_options(factions: list[dict]) -> list[discord.SelectOption]:
    """Build dropdown options for a list of factions (label=factionId,
    description=factionName). Caps at Discord's 25-option limit; description
    is truncated to the 100-char limit and omitted when empty."""
    return [
        discord.SelectOption(
            label=f["factionId"],
            value=f["factionId"],
            description=(f.get("factionName") or "")[:100] or None,
        )
        for f in factions[:25]
    ]


def extract_unit_prefix(default_unit: str, faction_id: str) -> Optional[str]:
    """Extract the middle token from a defaultUnit string.

    `ADF_LO_CombinedArms`  -> `LO`
    `ADF_LD_CombinedArms`  -> `LD`
    `ADF_S_CombinedArms_Seed` -> `S`
    Returns None if the string doesn't match the expected pattern.
    """
    if not default_unit or not faction_id:
        return None
    prefix = f"{faction_id}_"
    if not default_unit.startswith(prefix):
        return None
    remainder = default_unit[len(prefix):]
    token, _, _ = remainder.partition("_")
    return token or None


def _extract_default_unit_type(default_unit: str, faction_id: str) -> Optional[str]:
    """Extract the unit type suffix from a defaultUnit string.

    `ADF_LO_CombinedArms`       -> `CombinedArms`
    `ADF_S_CombinedArms_Seed`   -> `CombinedArms_Seed`
    """
    if not default_unit or not faction_id:
        return None
    prefix = f"{faction_id}_"
    if not default_unit.startswith(prefix):
        return None
    remainder = default_unit[len(prefix):]
    _, _, rest = remainder.partition("_")
    return rest or None


# ═══════════════════════════════════════════════════════════════════════════
# PERSISTENT VIEW — Event embed buttons
# ═══════════════════════════════════════════════════════════════════════════

class EventActionView(ui.View):
    """View attached to a specific event's embed. Buttons: Suggest, Info, Admin.

    Each instance is bound to a single event via db_id, so multiple events
    can coexist in the same channel without their buttons colliding. Views
    are re-attached on bot startup (see LayerVoteBot.setup_hook) by walking
    all active events and binding each view to its event_message_id.
    """

    def __init__(self, db_id: int, lang: str = "en"):
        super().__init__(timeout=None)
        self.db_id = db_id

        suggest = ui.Button(
            label=t("button.suggest", lang),
            style=discord.ButtonStyle.primary,
            custom_id=f"event_action:suggest:{db_id}",
            emoji="🗺️",
        )
        suggest.callback = self._suggest
        self.add_item(suggest)

        info = ui.Button(
            label=t("button.info", lang),
            style=discord.ButtonStyle.secondary,
            custom_id=f"event_action:info:{db_id}",
            emoji="ℹ️",
        )
        info.callback = self._info
        self.add_item(info)

        admin = ui.Button(
            label=t("button.admin", lang),
            style=discord.ButtonStyle.danger,
            custom_id=f"event_action:admin:{db_id}",
            emoji="⚙️",
        )
        admin.callback = self._admin
        self.add_item(admin)

    async def _suggest(self, interaction: discord.Interaction):
        await handle_suggest_start(interaction, self.db_id)

    async def _info(self, interaction: discord.Interaction):
        await handle_info(interaction, self.db_id)

    async def _admin(self, interaction: discord.Interaction):
        await handle_admin_panel(interaction, self.db_id)


# ═══════════════════════════════════════════════════════════════════════════
# SUGGESTION FLOW — Sequential dropdowns in ephemeral messages
# ═══════════════════════════════════════════════════════════════════════════

class SuggestState:
    """Tracks the state of a suggestion flow for a user."""
    __slots__ = ("guild_id", "channel_id", "db_id", "source", "map_name",
                 "mode_raw_name", "gamemode", "layer_version",
                 "team1_faction", "team1_unit", "team2_faction", "team2_unit",
                 "layer_data", "flow")

    def __init__(self, guild_id: int, channel_id: int, flow: str = "suggest",
                 db_id: int = 0):
        self.guild_id = guild_id
        self.channel_id = channel_id
        # db_id of the event this suggestion targets. 0 for the history_add
        # flow, which doesn't bind to an event.
        self.db_id = db_id
        # The layer source the user is suggesting from (e.g. "main", "supermod").
        # Empty string acts as "no source filter" — used for legacy events that
        # predate per-source caching.
        self.source = ""
        self.map_name = None
        self.mode_raw_name = None
        self.gamemode = None
        self.layer_version = None
        self.team1_faction = None
        self.team1_unit = None
        self.team2_faction = None
        self.team2_unit = None
        self.layer_data = None
        # "suggest" = normal event suggestion; "history_add" = manual
        # insertion into voting_history via /history_add.
        self.flow = flow


# Active suggestion sessions: user_id -> SuggestState
_suggest_sessions: dict[int, SuggestState] = {}


def _event_settings(event: dict, settings: dict) -> dict:
    """Merge per-event config with guild settings.

    Per-event keys (see db.EVENT_CONFIG_KEYS) win over guild values, so admins
    editing an event via the DM dialog only affect that event. Falls back to
    guild settings for any key the event doesn't carry (legacy events that
    predate Phase 2, or settings outside the snapshot list like language and
    organizer_role_id).
    """
    merged = dict(settings or {})
    cfg = (event or {}).get("config") or {}
    for key, value in cfg.items():
        merged[key] = value
    return merged


def _state_event_settings(state: "SuggestState") -> dict:
    """Per-suggest-flow settings: merged event config + guild fallbacks.

    SuggestState's mid-flow callbacks don't keep the event in scope, so each
    one re-merges from a fresh DB read. This keeps suggest filters in sync
    with concurrent DM edits to the event's config and avoids stale snapshots.
    """
    settings = db.get_guild_settings(state.guild_id) or {}
    if not state.db_id:
        return settings
    record = db.get_event_by_db_id(state.guild_id, state.db_id)
    return _event_settings(record["event"] if record else {}, settings)


def _resolve_event_sources(event: dict, settings: dict) -> list[str]:
    """Return the list of source names a user may pick from for this event.

    The event's stored `allowed_sources` (chosen by the admin at creation time)
    is the starting point. The guild's `allowed_sources` setting is then
    applied as a live cap — so changes to /config_layer_sources take effect
    immediately for already-active events, instead of being frozen at the
    moment the event was created.

    Falls back to all distinct sources currently in the cache when the event
    has no explicit selection (legacy events that predate this feature).
    """
    explicit = event.get("allowed_sources") or []
    candidate = list(explicit) if explicit else db.get_unique_sources()

    guild_allowed = settings.get("allowed_sources") or []
    if guild_allowed:
        candidate = [s for s in candidate if s in guild_allowed]

    return candidate


async def handle_suggest_start(interaction: discord.Interaction, db_id: int):
    """Start the suggestion flow when user clicks the Suggest button on a
    specific event's embed. The button carries db_id so the right event is
    targeted even when multiple events live in the channel."""
    settings = db.get_guild_settings(interaction.guild_id)
    if not settings:
        await interaction.response.send_message(
            t("general.guild_not_configured", "en"), ephemeral=True)
        return

    lang = settings.get("language", "en")

    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        await interaction.response.send_message(t("event.no_event", lang), ephemeral=True)
        return

    event = record["event"]
    if event.get("phase") != "suggestions_open":
        await interaction.response.send_message(t("suggest.not_open", lang), ephemeral=True)
        return

    # Check max suggestions — read from per-event config first, falling back
    # to guild defaults for legacy events created before Phase 2.
    event_settings = _event_settings(event, settings)
    max_suggestions = event_settings.get("max_suggestions_per_user", 2)
    user_suggestions = [s for s in event.get("suggestions", [])
                        if str(s.get("user_id")) == str(interaction.user.id)]
    if len(user_suggestions) >= max_suggestions:
        await interaction.response.send_message(
            t("suggest.max_reached", lang, max=max_suggestions), ephemeral=True)
        return

    # Check layer cache
    if db.get_layer_cache_count() == 0:
        await interaction.response.send_message(t("cache.empty", lang), ephemeral=True)
        return

    # Start suggestion flow
    state = SuggestState(interaction.guild_id, interaction.channel_id, db_id=db_id)
    _suggest_sessions[interaction.user.id] = state

    sources = _resolve_event_sources(event, settings)
    if len(sources) > 1:
        # Show source picker first; the map step runs after the user picks one.
        options = [discord.SelectOption(label=s, value=s) for s in sources[:25]]
        view = SourceSelectView(options, lang)
        embed = discord.Embed(
            title=t("suggest.phase_title", lang),
            description=t("suggest.select_source", lang),
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        return

    # Single source (or none recorded → no filter): skip the picker.
    state.source = sources[0] if sources else ""
    await _suggest_show_map_step(interaction, state, settings, lang, edit=False)


# Map-size buckets in km (max layer size per map, since skirmish layers reuse
# the mapId with a smaller area). Thresholds chosen so each bucket stays under
# Discord's 25-option Select cap for both layers.json and spm_layers.json.
_SIZE_BUCKETS: tuple[tuple[str, float], ...] = (
    ("small", 3.0),    # < 3.0 km — skirmish / CQB
    ("medium", 4.5),   # 3.0 ≤ size < 4.5 km — standard AAS
    ("large", float("inf")),  # ≥ 4.5 km — full RAAS / big maps
)
_SIZE_BUCKET_KEYS = {
    "small": "suggest.size_small",
    "medium": "suggest.size_medium",
    "large": "suggest.size_large",
}


def _bucket_for_size(size_km: Optional[float]) -> str:
    """Return the bucket key ('small'/'medium'/'large') for a map size in km.
    Maps without a size fall into 'medium' as a safe default."""
    if size_km is None:
        return "medium"
    for key, upper in _SIZE_BUCKETS:
        if size_km < upper:
            return key
    return "large"


def _group_maps_by_size(maps: list[str], sizes: "dict[str, float]") -> "dict[str, list[str]]":
    """Group map names by size bucket. Insertion order is small → medium → large
    so the dropdowns appear in size order regardless of dict iteration."""
    groups: dict[str, list[str]] = {key: [] for key, _ in _SIZE_BUCKETS}
    for m in maps:
        groups[_bucket_for_size(sizes.get(m))].append(m)
    return groups


def _build_map_picker_view(maps: list[str], lang: str,
                           sizes: "dict[str, float]") -> ui.View:
    """Build the map-select view: always split into Small/Medium/Large
    dropdowns by canonical (largest-layer) size, with map counts in every
    placeholder. Falls back to a single flat dropdown only when grouping
    collapses to a single non-empty bucket (e.g. tiny custom sources).
    """
    groups = _group_maps_by_size(maps, sizes)
    non_empty = [(k, v) for k, v in groups.items() if v]
    if len(non_empty) <= 1:
        options = [discord.SelectOption(label=m, value=m) for m in maps]
        placeholder = f"{t('suggest.select_map', lang).rstrip('.')} ({len(maps)})"
        return MapSelectView(options, lang, placeholder=placeholder)
    return GroupedMapSelectView(groups, lang)


async def _suggest_show_map_step(interaction: discord.Interaction, state: SuggestState,
                                 settings: dict, lang: str, edit: bool):
    """Render the map-select dropdown. Used after source pick or when only one source exists."""
    event_settings = _state_event_settings(state) if state.db_id else (settings or {})
    blacklisted_maps = event_settings.get("blacklisted_maps", [])
    source_filter = [state.source] if state.source else None
    maps = db.get_unique_maps(excluded_maps=blacklisted_maps, allowed_sources=source_filter)

    if not maps:
        msg = t("general.error", lang, error="No maps available")
        if edit:
            await interaction.response.edit_message(
                embed=discord.Embed(description=msg, color=discord.Color.red()),
                view=None,
            )
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return

    sizes = db.get_map_sizes(allowed_sources=source_filter)
    view = _build_map_picker_view(maps, lang, sizes)
    desc = t("suggest.select_map", lang)
    if state.source:
        desc = f"**{t('suggest.source_label', lang)}:** {state.source}\n{desc}"
    embed = discord.Embed(
        title=t("suggest.phase_title", lang),
        description=desc,
        color=discord.Color.green(),
    )
    if edit:
        await interaction.response.edit_message(embed=embed, view=view)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class SourceSelectView(ui.View):
    def __init__(self, options: list[discord.SelectOption], lang: str):
        super().__init__(timeout=120)
        self.add_item(SourceSelect(options, lang))


class SourceSelect(ui.Select):
    def __init__(self, options: list[discord.SelectOption], lang: str):
        super().__init__(placeholder=t("suggest.select_source", lang),
                         options=options, min_values=1, max_values=1)
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        state = _suggest_sessions.get(interaction.user.id)
        if not state:
            await interaction.response.send_message(t("general.timeout", self.lang), ephemeral=True)
            return

        state.source = self.values[0]
        settings = db.get_guild_settings(state.guild_id)
        lang = settings.get("language", "en") if settings else "en"
        await _suggest_show_map_step(interaction, state, settings or {}, lang, edit=True)


class MapSelectView(ui.View):
    def __init__(self, options: list[discord.SelectOption], lang: str,
                 placeholder: Optional[str] = None):
        super().__init__(timeout=120)
        self.lang = lang
        select = MapSelect(options, lang, placeholder=placeholder)
        self.add_item(select)


class GroupedMapSelectView(ui.View):
    """Map picker with one MapSelect per size bucket. Used when the map list
    exceeds Discord's 25-option-per-Select cap (typical for the supermod source,
    which has 43+ playable maps).

    Buckets are always Small/Medium/Large (3 dropdowns), comfortably under the
    5-component View cap.
    """

    def __init__(self, groups: "dict[str, list[str]]", lang: str):
        super().__init__(timeout=120)
        self.lang = lang
        for bucket_key, group_maps in groups.items():
            if not group_maps:
                continue
            label = t(_SIZE_BUCKET_KEYS[bucket_key], lang)
            if len(group_maps) > 25:
                logger.warning(
                    "Map size bucket '%s' has %d maps; truncating to 25 (Discord Select limit).",
                    bucket_key, len(group_maps),
                )
            options = [discord.SelectOption(label=m, value=m) for m in group_maps[:25]]
            self.add_item(MapSelect(options, lang, placeholder=f"{label} ({len(group_maps)})"))


class MapSelect(ui.Select):
    def __init__(self, options: list[discord.SelectOption], lang: str,
                 placeholder: Optional[str] = None):
        super().__init__(placeholder=placeholder or t("suggest.select_map", lang),
                         options=options, min_values=1, max_values=1)
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        state = _suggest_sessions.get(interaction.user.id)
        if not state:
            await interaction.response.send_message(t("general.timeout", self.lang), ephemeral=True)
            return

        state.map_name = self.values[0]
        settings = db.get_guild_settings(state.guild_id)
        lang = settings.get("language", "en") if settings else "en"
        event_settings = _state_event_settings(state) if state.db_id else (settings or {})
        source_filter = [state.source] if state.source else None

        # Get available modes for this map (within the chosen source, if any)
        modes = db.get_modes_for_map(
            state.map_name,
            allowed_gamemodes=event_settings.get("allowed_gamemodes", []),
            allowed_sources=source_filter,
        )

        if not modes:
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title=t("suggest.phase_title", lang),
                    description=t("general.error", lang, error="No modes available for this map"),
                    color=discord.Color.red(),
                ),
                view=None,
            )
            return

        options = [
            discord.SelectOption(label=m["display"], value=m["raw_name"])
            for m in modes[:25]
        ]

        view = ModeSelectView(options, lang)
        embed = discord.Embed(
            title=t("suggest.phase_title", lang),
            description=f"**Map:** {state.map_name}\n{t('suggest.select_mode', lang)}",
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=view)


class ModeSelectView(ui.View):
    def __init__(self, options: list[discord.SelectOption], lang: str):
        super().__init__(timeout=120)
        self.add_item(ModeSelect(options, lang))


class ModeSelect(ui.Select):
    def __init__(self, options: list[discord.SelectOption], lang: str):
        super().__init__(placeholder=t("suggest.select_mode", lang),
                         options=options, min_values=1, max_values=1)
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        state = _suggest_sessions.get(interaction.user.id)
        if not state:
            await interaction.response.send_message(t("general.timeout", self.lang), ephemeral=True)
            return

        raw_name = self.values[0]
        source_filter = [state.source] if state.source else None
        layer_data = db.get_layer_by_raw_name(raw_name, allowed_sources=source_filter)
        if not layer_data:
            await interaction.response.edit_message(
                embed=discord.Embed(description="Layer not found.", color=discord.Color.red()),
                view=None,
            )
            return

        state.mode_raw_name = raw_name
        state.gamemode = layer_data["gamemode"]
        state.layer_version = layer_data["layer_version"]
        state.layer_data = layer_data

        settings = db.get_guild_settings(state.guild_id)
        lang = settings.get("language", "en") if settings else "en"
        event_settings = _state_event_settings(state) if state.db_id else (settings or {})
        bl_factions = event_settings.get("blacklisted_factions", [])
        bl_units = event_settings.get("blacklisted_units", [])

        # Get factions for team 1
        factions = get_factions_for_team(layer_data, 1, bl_factions, bl_units)
        if not factions:
            await interaction.response.edit_message(
                embed=discord.Embed(description="No factions available.", color=discord.Color.red()),
                view=None,
            )
            return

        options = _faction_select_options(factions)

        mode_str = f"{state.gamemode} {state.layer_version}".strip() if state.layer_version else state.gamemode
        view = Team1FactionSelectView(options, lang)
        embed = discord.Embed(
            title=t("suggest.phase_title", lang),
            description=(
                f"**Map:** {state.map_name}\n"
                f"**Mode:** {mode_str}\n"
                f"{t('suggest.select_team1_faction', lang)}"
            ),
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=view)


class Team1FactionSelectView(ui.View):
    def __init__(self, options: list[discord.SelectOption], lang: str):
        super().__init__(timeout=120)
        self.add_item(Team1FactionSelect(options, lang))


class Team1FactionSelect(ui.Select):
    def __init__(self, options: list[discord.SelectOption], lang: str):
        super().__init__(placeholder=t("suggest.select_team1_faction", lang),
                         options=options, min_values=1, max_values=1)
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        state = _suggest_sessions.get(interaction.user.id)
        if not state:
            await interaction.response.send_message(t("general.timeout", self.lang), ephemeral=True)
            return

        state.team1_faction = self.values[0]
        settings = db.get_guild_settings(state.guild_id)
        lang = settings.get("language", "en") if settings else "en"
        event_settings = _state_event_settings(state) if state.db_id else (settings or {})
        bl_units = event_settings.get("blacklisted_units", [])

        # Get unit types for team 1 faction
        units = get_unit_types_for_faction(
            state.layer_data.get("factions", []), state.team1_faction, bl_units, team=1)

        if not units:
            # No unit types — skip to team 2
            state.team1_unit = "Default"
            await _show_team2_faction_select(interaction, state, settings)
            return

        options = [
            discord.SelectOption(label=u.get("type", "?"), value=u.get("type", "?"))
            for u in units[:25]
        ]

        mode_str = f"{state.gamemode} {state.layer_version}".strip() if state.layer_version else state.gamemode
        view = Team1UnitSelectView(options, lang)
        embed = discord.Embed(
            title=t("suggest.phase_title", lang),
            description=(
                f"**Map:** {state.map_name}\n"
                f"**Mode:** {mode_str}\n"
                f"**Team 1:** {state.team1_faction}\n"
                f"{t('suggest.select_team1_unit', lang)}"
            ),
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=view)


class Team1UnitSelectView(ui.View):
    def __init__(self, options: list[discord.SelectOption], lang: str):
        super().__init__(timeout=120)
        self.add_item(Team1UnitSelect(options, lang))


class Team1UnitSelect(ui.Select):
    def __init__(self, options: list[discord.SelectOption], lang: str):
        super().__init__(placeholder=t("suggest.select_team1_unit", lang),
                         options=options, min_values=1, max_values=1)
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        state = _suggest_sessions.get(interaction.user.id)
        if not state:
            await interaction.response.send_message(t("general.timeout", self.lang), ephemeral=True)
            return

        state.team1_unit = self.values[0]
        settings = db.get_guild_settings(state.guild_id)
        await _show_team2_faction_select(interaction, state, settings)


async def _show_team2_faction_select(interaction: discord.Interaction,
                                     state: SuggestState, settings: dict):
    """Show team 2 faction dropdown."""
    lang = settings.get("language", "en") if settings else "en"
    event_settings = _state_event_settings(state) if state.db_id else (settings or {})
    bl_factions = event_settings.get("blacklisted_factions", [])
    bl_units = event_settings.get("blacklisted_units", [])

    factions = get_factions_for_team(
        state.layer_data, 2, bl_factions, bl_units,
        exclude_faction=state.team1_faction)

    if not factions:
        await interaction.response.edit_message(
            embed=discord.Embed(description="No factions available for Team 2.", color=discord.Color.red()),
            view=None,
        )
        return

    options = _faction_select_options(factions)

    mode_str = f"{state.gamemode} {state.layer_version}".strip() if state.layer_version else state.gamemode
    view = Team2FactionSelectView(options, lang)
    embed = discord.Embed(
        title=t("suggest.phase_title", lang),
        description=(
            f"**Map:** {state.map_name}\n"
            f"**Mode:** {mode_str}\n"
            f"**Team 1:** {state.team1_faction} / {state.team1_unit}\n"
            f"{t('suggest.select_team2_faction', lang)}"
        ),
        color=discord.Color.green(),
    )
    await interaction.response.edit_message(embed=embed, view=view)


class Team2FactionSelectView(ui.View):
    def __init__(self, options: list[discord.SelectOption], lang: str):
        super().__init__(timeout=120)
        self.add_item(Team2FactionSelect(options, lang))


class Team2FactionSelect(ui.Select):
    def __init__(self, options: list[discord.SelectOption], lang: str):
        super().__init__(placeholder=t("suggest.select_team2_faction", lang),
                         options=options, min_values=1, max_values=1)
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        state = _suggest_sessions.get(interaction.user.id)
        if not state:
            await interaction.response.send_message(t("general.timeout", self.lang), ephemeral=True)
            return

        state.team2_faction = self.values[0]
        settings = db.get_guild_settings(state.guild_id)
        lang = settings.get("language", "en") if settings else "en"
        event_settings = _state_event_settings(state) if state.db_id else (settings or {})
        bl_units = event_settings.get("blacklisted_units", [])

        # Get unit types for team 2 faction
        units = get_unit_types_for_faction(
            state.layer_data.get("factions", []), state.team2_faction, bl_units, team=2)

        if not units:
            state.team2_unit = "Default"
            await _show_confirm(interaction, state, settings)
            return

        options = [
            discord.SelectOption(label=u.get("type", "?"), value=u.get("type", "?"))
            for u in units[:25]
        ]

        mode_str = f"{state.gamemode} {state.layer_version}".strip() if state.layer_version else state.gamemode
        view = Team2UnitSelectView(options, lang)
        embed = discord.Embed(
            title=t("suggest.phase_title", lang),
            description=(
                f"**Map:** {state.map_name}\n"
                f"**Mode:** {mode_str}\n"
                f"**Team 1:** {state.team1_faction} / {state.team1_unit}\n"
                f"**Team 2:** {state.team2_faction}\n"
                f"{t('suggest.select_team2_unit', lang)}"
            ),
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=view)


class Team2UnitSelectView(ui.View):
    def __init__(self, options: list[discord.SelectOption], lang: str):
        super().__init__(timeout=120)
        self.add_item(Team2UnitSelect(options, lang))


class Team2UnitSelect(ui.Select):
    def __init__(self, options: list[discord.SelectOption], lang: str):
        super().__init__(placeholder=t("suggest.select_team2_unit", lang),
                         options=options, min_values=1, max_values=1)
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        state = _suggest_sessions.get(interaction.user.id)
        if not state:
            await interaction.response.send_message(t("general.timeout", self.lang), ephemeral=True)
            return

        state.team2_unit = self.values[0]
        settings = db.get_guild_settings(state.guild_id)
        await _show_confirm(interaction, state, settings)


async def _show_confirm(interaction: discord.Interaction, state: SuggestState, settings: dict):
    """Show the confirmation step with Submit/Cancel buttons."""
    lang = settings.get("language", "en") if settings else "en"
    mode_str = f"{state.gamemode} {state.layer_version}".strip() if state.layer_version else state.gamemode

    view = ConfirmSuggestionView(lang)
    embed = discord.Embed(
        title=t("suggest.confirm_title", lang),
        description=(
            f"**Map:** {state.map_name}\n"
            f"**Mode:** {mode_str}\n"
            f"**Team 1:** {state.team1_faction} / {state.team1_unit}\n"
            f"**Team 2:** {state.team2_faction} / {state.team2_unit}"
        ),
        color=discord.Color.gold(),
    )
    await interaction.response.edit_message(embed=embed, view=view)


class ConfirmSuggestionView(ui.View):
    def __init__(self, lang: str):
        super().__init__(timeout=60)
        self.lang = lang

    @ui.button(label="Submit", style=discord.ButtonStyle.success, emoji="✅")
    async def submit_button(self, interaction: discord.Interaction, button: ui.Button):
        await handle_suggest_submit(interaction, self.lang)

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel_button(self, interaction: discord.Interaction, button: ui.Button):
        _suggest_sessions.pop(interaction.user.id, None)
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("general.cancelled", self.lang), color=discord.Color.greyple()),
            view=None,
        )


async def handle_suggest_submit(interaction: discord.Interaction, lang: str):
    """Process the final suggestion submission."""
    state = _suggest_sessions.pop(interaction.user.id, None)
    if not state:
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("general.timeout", lang), color=discord.Color.red()),
            view=None,
        )
        return

    if state.flow == "history_add":
        await _handle_history_add_submit(interaction, state, lang)
        return

    lock = _get_guild_lock(state.guild_id)
    async with lock:
        record = db.get_event_by_db_id(state.guild_id, state.db_id)
        if not record:
            await interaction.response.edit_message(
                embed=discord.Embed(description=t("event.no_event", lang), color=discord.Color.red()),
                view=None,
            )
            return

        event = record["event"]
        settings = db.get_guild_settings(state.guild_id)
        lang = settings.get("language", "en") if settings else lang
        event_settings = _event_settings(event, settings or {})

        if event.get("phase") != "suggestions_open":
            await interaction.response.edit_message(
                embed=discord.Embed(description=t("suggest.not_open", lang), color=discord.Color.red()),
                view=None,
            )
            return

        # Check total suggestion limit (hard cap 25 due to Discord select menu limit)
        max_total = min(event_settings.get("max_total_suggestions", 25), 25)
        if len(event.get("suggestions", [])) >= max_total:
            await interaction.response.edit_message(
                embed=discord.Embed(
                    description=t("suggest.max_total_reached", lang, max=max_total),
                    color=discord.Color.red(),
                ),
                view=None,
            )
            return

        # Build suggestion dict
        suggestion = {
            "id": str(uuid.uuid4())[:8],
            "user_id": str(interaction.user.id),
            "user_name": interaction.user.display_name,
            "map_name": state.map_name,
            "gamemode": state.gamemode,
            "layer_version": state.layer_version,
            "team1_faction": state.team1_faction,
            "team1_faction_name": _resolve_faction_name(state.layer_data, state.team1_faction, 1),
            "team1_unit": state.team1_unit,
            "team2_faction": state.team2_faction,
            "team2_faction_name": _resolve_faction_name(state.layer_data, state.team2_faction, 2),
            "team2_unit": state.team2_unit,
            "team1_unit_prefix": _resolve_unit_prefix(state.layer_data, state.team1_faction, 1),
            "team2_unit_prefix": _resolve_unit_prefix(state.layer_data, state.team2_faction, 2),
            "raw_name": state.mode_raw_name,
            "source": state.source,
            "suggested_at": datetime.now().isoformat(),
        }

        # Check duplicate in current event
        for existing in event.get("suggestions", []):
            if suggestion_matches(suggestion, existing):
                await interaction.response.edit_message(
                    embed=discord.Embed(description=t("suggest.duplicate", lang), color=discord.Color.red()),
                    view=None,
                )
                return

        # Check history blocking
        lookback = event_settings.get("history_lookback_events", 3)
        if lookback > 0:
            blocked = db.get_blocked_suggestions(state.guild_id, state.channel_id, lookback)
            for bl in blocked:
                if suggestion_matches(suggestion, bl):
                    await interaction.response.edit_message(
                        embed=discord.Embed(
                            description=t("suggest.blocked_history", lang, count=lookback),
                            color=discord.Color.red(),
                        ),
                        view=None,
                    )
                    return

        # Add suggestion
        event.setdefault("suggestions", []).append(suggestion)
        db.save_event(record["db_id"], event)

    # Confirm to user
    await interaction.response.edit_message(
        embed=discord.Embed(
            description=f"✅ {t('suggest.submitted', lang)}\n{format_layer_short(suggestion)}",
            color=discord.Color.green(),
        ),
        view=None,
    )

    # Update the main event embed
    await _update_event_embed(state.db_id)

    await send_to_log_channel(
        f"New suggestion by {interaction.user.display_name}: {format_layer_short(suggestion)}",
        guild_id=state.guild_id,
    )


# ═══════════════════════════════════════════════════════════════════════════
# INFO BUTTON handler
# ═══════════════════════════════════════════════════════════════════════════

async def handle_info(interaction: discord.Interaction, db_id: int):
    """Show info about the user's suggestions in this event."""
    settings = db.get_guild_settings(interaction.guild_id)
    lang = settings.get("language", "en") if settings else "en"

    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        await interaction.response.send_message(t("event.no_event", lang), ephemeral=True)
        return

    event = record["event"]
    user_suggestions = [s for s in event.get("suggestions", [])
                        if str(s.get("user_id")) == str(interaction.user.id)]

    event_settings = _event_settings(event, settings or {})
    max_suggestions = event_settings.get("max_suggestions_per_user", 2)
    embed = discord.Embed(
        title=t("button.info", lang),
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name=t("admin.phase", lang, phase=event.get("phase", "?")),
        value=f"{len(user_suggestions)}/{max_suggestions} suggestions used",
        inline=False,
    )

    if user_suggestions:
        lines = [f"• {format_layer_short(s)}" for s in user_suggestions]
        embed.add_field(name="Your Suggestions", value="\n".join(lines), inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
# ADMIN PANEL
# ═══════════════════════════════════════════════════════════════════════════

async def handle_admin_panel(interaction: discord.Interaction, db_id: int):
    """Show admin action buttons for a specific event."""
    settings = db.get_guild_settings(interaction.guild_id)
    if not settings:
        await interaction.response.send_message(
            t("general.guild_not_configured", "en"), ephemeral=True)
        return

    lang = settings.get("language", "en")
    if not has_organizer_role(interaction.user, settings.get("organizer_role_id", 0)):
        await interaction.response.send_message(t("general.requires_organizer", lang), ephemeral=True)
        return

    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        await interaction.response.send_message(t("event.no_event", lang), ephemeral=True)
        return

    event = record["event"]
    phase = event.get("phase", "created")

    embed = discord.Embed(
        title=t("button.admin", lang),
        description=t("admin.phase", lang, phase=phase) + "\n" +
                    t("admin.suggestions_count", lang, count=len(event.get("suggestions", []))),
        color=discord.Color.dark_red(),
    )

    suggestion_count = len(event.get("suggestions", []))
    view = AdminPanelView(phase, lang, record["db_id"], suggestion_count)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class AdminPanelView(ui.View):
    def __init__(self, phase: str, lang: str, db_id: int, suggestion_count: int = 0):
        super().__init__(timeout=120)
        self.lang = lang
        self.db_id = db_id

        if phase == "created":
            self.add_item(AdminButton("open_suggestions", t("admin.open_suggestions", lang), discord.ButtonStyle.success, "▶️"))
        elif phase == "suggestions_open":
            self.add_item(AdminButton("close_suggestions", t("admin.close_suggestions", lang), discord.ButtonStyle.secondary, "⏹️"))
        elif phase == "suggestions_closed":
            self.add_item(AdminButton("select_for_vote", t("admin.select_for_vote", lang), discord.ButtonStyle.primary, "🗳️"))
        elif phase == "voting":
            self.add_item(AdminButton("end_vote", t("admin.end_vote", lang), discord.ButtonStyle.danger, "🏁"))

        # Removing a suggestion only makes sense before the poll is live.
        if phase in ("suggestions_open", "suggestions_closed") and suggestion_count > 0:
            self.add_item(AdminButton("remove_suggestion",
                                      t("admin.remove_suggestion", lang),
                                      discord.ButtonStyle.secondary, "✂️"))

        # Edit opens a DM dialog for changing this event's per-event config
        # (gamemodes, blacklists, limits, voting params) without touching the
        # guild defaults. Available at any phase.
        self.add_item(AdminButton("edit_event", t("admin.edit_event", lang),
                                  discord.ButtonStyle.primary, "✏️"))

        self.add_item(AdminButton("delete_event", t("admin.delete_event", lang), discord.ButtonStyle.danger, "🗑️"))


class AdminButton(ui.Button):
    def __init__(self, action: str, label: str, style: discord.ButtonStyle, emoji: str):
        # Custom_ids are scoped to the action only; the per-event db_id lives
        # on the view (AdminPanelView), which is non-persistent (panel is
        # ephemeral, reopens via the Admin button each time).
        super().__init__(label=label, style=style, emoji=emoji, custom_id=f"admin:{action}")
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        db_id = self.view.db_id
        if self.action == "open_suggestions":
            settings = db.get_guild_settings(interaction.guild_id)
            lang = settings.get("language", "en") if settings else "en"
            await interaction.response.send_modal(OpenSuggestionsModal(lang, db_id))
        elif self.action == "close_suggestions":
            await admin_close_suggestions(interaction, db_id)
        elif self.action == "select_for_vote":
            await admin_select_for_vote(interaction, db_id)
        elif self.action == "end_vote":
            await admin_end_vote(interaction, db_id)
        elif self.action == "remove_suggestion":
            await admin_remove_suggestion(interaction, db_id)
        elif self.action == "edit_event":
            await admin_edit_event(interaction, db_id)
        elif self.action == "delete_event":
            await admin_delete_event(interaction, db_id)


class ConfirmActionView(ui.View):
    """Generic confirmation dialog with Confirm and Cancel buttons.

    The confirm_callback is invoked with (interaction, db_id) so admin flows
    can route the action back to the originating event without relying on
    channel-scoped lookups.
    """

    def __init__(self, lang: str, confirm_callback, db_id: int = 0):
        super().__init__(timeout=60)
        self.lang = lang
        self.db_id = db_id
        self._confirm_callback = confirm_callback
        self.confirm_button.label = t("general.confirm", lang)
        self.cancel_button.label = t("general.cancel", lang)

    @ui.button(label="Confirm", style=discord.ButtonStyle.danger, emoji="✅")
    async def confirm_button(self, interaction: discord.Interaction, button: ui.Button):
        await self._confirm_callback(interaction, self.db_id)

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(
            embed=discord.Embed(
                description=t("general.cancelled", self.lang),
                color=discord.Color.greyple()),
            view=None,
        )


async def admin_open_suggestions(interaction: discord.Interaction, db_id: int,
                                 auto_close_seconds: Optional[int] = None):
    """Open the suggestion phase for a specific event."""
    lock = _get_guild_lock(interaction.guild_id)
    end_time = None
    async with lock:
        record = db.get_event_by_db_id(interaction.guild_id, db_id)
        if not record:
            return
        event = record["event"]
        settings = db.get_guild_settings(interaction.guild_id)
        lang = settings.get("language", "en") if settings else "en"

        if event.get("phase") not in ("created",):
            await interaction.response.send_message(
                embed=discord.Embed(description=t("phase.already_open", lang), color=discord.Color.orange()),
                ephemeral=True,
            )
            return

        event["phase"] = "suggestions_open"
        end_time = (datetime.now() + timedelta(seconds=auto_close_seconds)) if auto_close_seconds else None
        event["suggestion_end_time"] = end_time
        event["suggestion_duration_seconds"] = auto_close_seconds
        db.save_event(record["db_id"], event)

    if end_time:
        ts = int(end_time.timestamp())
        ack_text = t("phase.suggestions_opened_until", lang, ts=ts)
    else:
        ack_text = t("phase.suggestions_opened", lang)

    await interaction.response.send_message(
        embed=discord.Embed(description=f"✅ {ack_text}", color=discord.Color.green()),
        ephemeral=True,
    )
    await _update_event_embed(db_id)
    await send_to_log_channel(ack_text, guild_id=interaction.guild_id)


class OpenSuggestionsModal(ui.Modal):
    """Prompts the organizer for an optional suggestion-phase duration."""

    def __init__(self, lang: str, db_id: int):
        super().__init__(title=t("phase.duration_modal_title", lang))
        self.lang = lang
        self.db_id = db_id
        self.duration_input = ui.TextInput(
            label=t("phase.duration_label", lang),
            placeholder=t("phase.duration_placeholder", lang),
            required=False,
            max_length=20,
        )
        self.add_item(self.duration_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = (self.duration_input.value or "").strip()
        seconds = parse_duration_to_seconds(raw) if raw else None
        if raw and seconds is None:
            await interaction.response.send_message(
                t("phase.invalid_duration", self.lang, value=raw),
                ephemeral=True,
            )
            return
        await admin_open_suggestions(interaction, self.db_id, auto_close_seconds=seconds)


async def admin_close_suggestions(interaction: discord.Interaction, db_id: int):
    """Show confirmation before closing the suggestion phase."""
    settings = db.get_guild_settings(interaction.guild_id)
    lang = settings.get("language", "en") if settings else "en"

    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        return
    event = record["event"]
    if event.get("phase") != "suggestions_open":
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("phase.not_open", lang), color=discord.Color.orange()),
            view=None,
        )
        return

    view = ConfirmActionView(lang, _do_close_suggestions, db_id=db_id)
    await interaction.response.edit_message(
        embed=discord.Embed(description=t("confirm.close_suggestions", lang), color=discord.Color.orange()),
        view=view,
    )


async def _do_close_suggestions(interaction: discord.Interaction, db_id: int):
    """Actually close the suggestion phase after confirmation."""
    lock = _get_guild_lock(interaction.guild_id)
    async with lock:
        record = db.get_event_by_db_id(interaction.guild_id, db_id)
        if not record:
            return
        event = record["event"]
        settings = db.get_guild_settings(interaction.guild_id)
        lang = settings.get("language", "en") if settings else "en"

        if event.get("phase") != "suggestions_open":
            await interaction.response.edit_message(
                embed=discord.Embed(description=t("phase.not_open", lang), color=discord.Color.orange()),
                view=None,
            )
            return

        event["phase"] = "suggestions_closed"
        db.save_event(record["db_id"], event)

    count = len(event.get("suggestions", []))
    await interaction.response.edit_message(
        embed=discord.Embed(
            description=f"✅ {t('phase.suggestions_closed', lang, count=count)}",
            color=discord.Color.green(),
        ),
        view=None,
    )
    await _update_event_embed(db_id)
    await send_to_log_channel(f"Suggestion phase closed. {count} suggestions.", guild_id=interaction.guild_id)


async def admin_select_for_vote(interaction: discord.Interaction, db_id: int):
    """Show layer selection view for admin to pick layers for voting."""
    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        return

    event = record["event"]
    settings = db.get_guild_settings(interaction.guild_id)
    lang = settings.get("language", "en") if settings else "en"
    suggestions = event.get("suggestions", [])

    if not suggestions:
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("vote.no_suggestions", lang), color=discord.Color.orange()),
            view=None,
        )
        return

    max_voting = min(event.get("max_voting_layers", 10), 10)

    # Build selection options
    options = []
    for s in suggestions[:25]:
        label = format_layer_poll_option(s)
        options.append(discord.SelectOption(label=label, value=s["id"]))

    view = VoteSelectionView(options, max_voting, lang, record["db_id"])
    embed = discord.Embed(
        title=t("admin.select_for_vote", lang),
        description=t("vote.select_layers", lang, max=max_voting),
        color=discord.Color.blue(),
    )
    await interaction.response.edit_message(embed=embed, view=view)


class VoteSelectionView(ui.View):
    def __init__(self, options: list[discord.SelectOption], max_values: int,
                 lang: str, db_id: int):
        super().__init__(timeout=120)
        self.lang = lang
        self.db_id = db_id
        self.max_values = max_values

        select = VoteLayerSelect(options, max_values, lang)
        self.add_item(select)
        self.add_item(RandomButton(min(len(options), max_values), lang))
        self.add_item(ConfirmVoteButton(lang))

    selected_ids: list[str] = []


class VoteLayerSelect(ui.Select):
    def __init__(self, options: list[discord.SelectOption], max_values: int, lang: str):
        super().__init__(
            placeholder=t("vote.select_layers", lang, max=max_values),
            options=options,
            min_values=1,
            max_values=min(max_values, len(options)),
        )
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_ids = self.values
        await interaction.response.defer()


class RandomButton(ui.Button):
    def __init__(self, count: int, lang: str):
        super().__init__(
            label=t("button.random", lang, count=count),
            style=discord.ButtonStyle.secondary,
            emoji="🎲",
        )
        self.count = count
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        import random
        record = db.get_event_by_db_id(interaction.guild_id, self.view.db_id)
        if not record:
            return

        suggestions = record["event"].get("suggestions", [])
        count = min(self.count, len(suggestions))
        selected = random.sample(suggestions, count)
        self.view.selected_ids = [s["id"] for s in selected]

        names = [format_layer_poll_option(s) for s in selected]
        await interaction.response.edit_message(
            embed=discord.Embed(
                title=t("admin.select_for_vote", self.lang),
                description="**Selected (random):**\n" + "\n".join(f"• {n}" for n in names),
                color=discord.Color.blue(),
            ),
        )


class ConfirmVoteButton(ui.Button):
    def __init__(self, lang: str):
        super().__init__(
            label=t("button.confirm_selection", lang),
            style=discord.ButtonStyle.success,
            emoji="✅",
        )
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        selected_ids = self.view.selected_ids
        if not selected_ids:
            await interaction.response.edit_message(
                embed=discord.Embed(
                    description=t("vote.no_layers_selected", self.lang),
                    color=discord.Color.orange(),
                ),
            )
            return

        captured_ids = list(selected_ids)
        lang = self.lang
        db_id = self.view.db_id

        async def _do_start_vote(confirm_interaction: discord.Interaction, _db_id: int):
            lock = _get_guild_lock(confirm_interaction.guild_id)
            async with lock:
                record = db.get_event_by_db_id(confirm_interaction.guild_id, _db_id)
                if not record:
                    return
                event = record["event"]
                event["selected_for_vote"] = captured_ids
                event["phase"] = "voting"
                db.save_event(record["db_id"], event)
            await _start_poll(confirm_interaction, _db_id, captured_ids)

        view = ConfirmActionView(lang, _do_start_vote, db_id=db_id)
        await interaction.response.edit_message(
            embed=discord.Embed(
                description=t("confirm.start_vote", lang),
                color=discord.Color.orange()),
            view=view,
        )


async def _start_poll(interaction: discord.Interaction, db_id: int,
                      selected_ids: list[str]):
    """Create a Discord native poll for the selected layers."""
    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        return

    event = record["event"]
    settings = db.get_guild_settings(interaction.guild_id)
    lang = settings.get("language", "en") if settings else "en"
    suggestions = event.get("suggestions", [])
    duration_hours = event.get("voting_duration_hours", 24)

    # Get selected suggestions
    selected = [s for s in suggestions if s.get("id") in selected_ids]
    if not selected:
        return

    # Build poll
    poll = discord.Poll(
        question=t("vote.poll_question", lang),
        duration=timedelta(hours=duration_hours),
        multiple=bool(event.get("allow_multiple_votes", False)),
    )
    for s in selected[:10]:
        poll.add_answer(text=format_layer_poll_option(s))

    channel = interaction.channel
    poll_message = await channel.send(poll=poll)

    lock = _get_guild_lock(interaction.guild_id)
    async with lock:
        record = db.get_event_by_db_id(interaction.guild_id, db_id)
        if record:
            event = record["event"]
            event["poll_message_id"] = poll_message.id
            db.save_event(record["db_id"], event)

    await interaction.response.edit_message(
        embed=discord.Embed(
            description=f"✅ {t('vote.started', lang, hours=duration_hours)}",
            color=discord.Color.green(),
        ),
        view=None,
    )
    await _update_event_embed(db_id)
    await send_to_log_channel(
        f"Voting started with {len(selected)} layers for {duration_hours}h",
        guild_id=interaction.guild_id,
    )


async def _auto_start_poll(db_id: int, selected_ids: list[str]) -> bool:
    """Background variant of _start_poll — creates the poll without an
    interaction. Returns True on success.

    Assumes the event phase has already been set to "voting" under lock."""
    record = db.get_active_event_unsafe(db_id)
    if not record:
        return False
    guild_id = record["guild_id"]
    channel_id = record["channel_id"]
    event = record["event"]

    guild = bot.get_guild(guild_id)
    if not guild:
        return False
    channel = guild.get_channel(channel_id)
    if not channel:
        return False

    settings = db.get_guild_settings(guild_id)
    lang = settings.get("language", "en") if settings else "en"
    suggestions = event.get("suggestions", [])
    duration_hours = event.get("voting_duration_hours", 24)

    selected = [s for s in suggestions if s.get("id") in selected_ids]
    if not selected:
        return False

    poll = discord.Poll(
        question=t("vote.poll_question", lang),
        duration=timedelta(hours=duration_hours),
        multiple=bool(event.get("allow_multiple_votes", False)),
    )
    for s in selected[:10]:
        poll.add_answer(text=format_layer_poll_option(s))

    try:
        poll_message = await channel.send(poll=poll)
    except Exception as e:
        logger.error(f"Failed to send auto-poll: {e}")
        return False

    lock = _get_guild_lock(guild_id)
    async with lock:
        rec = db.get_active_event_unsafe(db_id)
        if rec:
            rec["event"]["poll_message_id"] = poll_message.id
            db.save_event(rec["db_id"], rec["event"])

    await _update_event_embed(db_id)
    return True


async def admin_end_vote(interaction: discord.Interaction, db_id: int):
    """End the voting phase and determine the winner."""
    settings = db.get_guild_settings(interaction.guild_id)
    lang = settings.get("language", "en") if settings else "en"

    lock = _get_guild_lock(interaction.guild_id)
    async with lock:
        record = db.get_event_by_db_id(interaction.guild_id, db_id)
        if not record:
            return

        event = record["event"]
        if event.get("phase") != "voting":
            await interaction.response.edit_message(
                embed=discord.Embed(description=t("vote.not_in_voting_phase", lang), color=discord.Color.orange()),
                view=None,
            )
            return

        # Try to end the poll and get results
        winner = await _resolve_poll_winner(interaction.channel, event)

        event["phase"] = "completed"
        event["winning_layer"] = winner
        db.save_event(record["db_id"], event)

        # Only record events that actually produced a winner.
        if winner:
            db.save_voting_history(
                interaction.guild_id,
                interaction.channel_id,
                event.get("suggestions", []),
                winner,
            )

    if winner:
        desc = f"✅ {t('vote.ended', lang)}\n{t('vote.winner', lang, layer=format_layer_short(winner))}"
    else:
        desc = f"✅ {t('vote.ended', lang)}\n{t('vote.no_winner', lang)}"

    await interaction.response.edit_message(
        embed=discord.Embed(description=desc, color=discord.Color.gold()),
        view=None,
    )
    await _update_event_embed(db_id)
    await send_to_log_channel(
        f"Voting ended. Winner: {format_layer_short(winner) if winner else 'None'}",
        guild_id=interaction.guild_id,
    )


async def _resolve_poll_winner(channel: discord.TextChannel, event: dict) -> Optional[dict]:
    """Try to fetch poll results and determine the winning layer."""
    poll_msg_id = event.get("poll_message_id")
    if not poll_msg_id:
        return None

    try:
        message = await channel.fetch_message(poll_msg_id)
        if not message.poll:
            return None

        # Try to end the poll if it's still active
        try:
            message = await message.end_poll()
        except discord.HTTPException:
            pass

        # Find the answer with most votes
        best_answer = None
        best_votes = -1
        for answer in message.poll.answers:
            if answer.vote_count > best_votes:
                best_votes = answer.vote_count
                best_answer = answer

        if best_answer and best_votes > 0:
            # Match back to suggestion by poll answer text
            selected_ids = event.get("selected_for_vote", [])
            suggestions = event.get("suggestions", [])
            selected = [s for s in suggestions if s.get("id") in selected_ids]

            answer_text = best_answer.text
            for s in selected:
                if format_layer_poll_option(s) == answer_text:
                    return s

            # Fallback: return first selected if exact match fails
            if selected:
                return selected[0]
    except discord.NotFound:
        logger.warning(f"Poll message {poll_msg_id} not found")
    except Exception as e:
        logger.error(f"Error resolving poll winner: {e}")

    return None


async def admin_delete_event(interaction: discord.Interaction, db_id: int):
    """Show confirmation before deleting a specific event."""
    settings = db.get_guild_settings(interaction.guild_id)
    lang = settings.get("language", "en") if settings else "en"

    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("event.no_event", lang), color=discord.Color.red()),
            view=None,
        )
        return

    view = ConfirmActionView(lang, _do_delete_event, db_id=db_id)
    await interaction.response.edit_message(
        embed=discord.Embed(description=t("confirm.delete_event", lang), color=discord.Color.orange()),
        view=view,
    )


async def _do_delete_event(interaction: discord.Interaction, db_id: int):
    """Actually delete the event after confirmation."""
    settings = db.get_guild_settings(interaction.guild_id)
    lang = settings.get("language", "en") if settings else "en"

    lock = _get_guild_lock(interaction.guild_id)
    async with lock:
        record = db.get_event_by_db_id(interaction.guild_id, db_id)
        if not record:
            await interaction.response.edit_message(
                embed=discord.Embed(description=t("event.no_event", lang), color=discord.Color.red()),
                view=None,
            )
            return

        event = record["event"]

        # Delete the event embed message
        msg_id = event.get("event_message_id")
        if msg_id:
            try:
                msg = await interaction.channel.fetch_message(msg_id)
                await msg.delete()
            except discord.NotFound:
                pass

        # Delete poll message and its result message if they exist
        poll_msg_id = event.get("poll_message_id")
        if poll_msg_id:
            # Try to delete the Discord-generated poll result message first
            try:
                async for msg in interaction.channel.history(
                    after=discord.Object(id=poll_msg_id), limit=15
                ):
                    if msg.type.value == 46:  # MessageType.poll_result
                        await msg.delete()
                        break
            except Exception:
                pass

            # Delete the poll message itself
            try:
                msg = await interaction.channel.fetch_message(poll_msg_id)
                await msg.delete()
            except discord.NotFound:
                pass

        db.delete_event(record["db_id"])

    await interaction.response.edit_message(
        embed=discord.Embed(description=f"✅ {t('event.deleted', lang)}", color=discord.Color.green()),
        view=None,
    )
    await send_to_log_channel("Event deleted", guild_id=interaction.guild_id)


# ═══════════════════════════════════════════════════════════════════════════
# ADMIN: Remove a single suggestion
# ═══════════════════════════════════════════════════════════════════════════

# Discord caps each Select at 25 options and a View at 5 action rows, so a
# single picker view holds up to 5 × 25 = 125 suggestions.
_REMOVE_PICKER_OPTIONS_PER_SELECT = 25
_REMOVE_PICKER_MAX_SUGGESTIONS = 5 * _REMOVE_PICKER_OPTIONS_PER_SELECT


def _remove_option_label(s: dict) -> str:
    """Build a Discord-safe (≤100 char) option label for a suggestion."""
    user = s.get("user_name", "?") or "?"
    layer = format_layer_short(s)
    label = f"{user} — {layer}"
    if len(label) > 100:
        label = label[:97] + "..."
    return label


class RemoveSuggestionView(ui.View):
    """Picker view that chunks suggestions across multiple Select dropdowns.

    Discord caps each Select at 25 options, so we split the suggestion list
    into 25-sized chunks. Each chunk becomes its own Select on its own row.
    """

    def __init__(self, suggestions: list[dict], lang: str, db_id: int):
        super().__init__(timeout=120)
        self.db_id = db_id
        chunks = [
            suggestions[i:i + _REMOVE_PICKER_OPTIONS_PER_SELECT]
            for i in range(0, len(suggestions), _REMOVE_PICKER_OPTIONS_PER_SELECT)
        ]
        for idx, chunk in enumerate(chunks[:5]):
            self.add_item(RemoveSuggestionSelect(chunk, lang, idx, len(chunks)))


class RemoveSuggestionSelect(ui.Select):
    def __init__(self, chunk: list[dict], lang: str, idx: int, total: int):
        if total > 1:
            placeholder = t("admin.remove_select_chunk", lang,
                            current=idx + 1, total=total)
        else:
            placeholder = t("admin.remove_select", lang)

        options = [
            discord.SelectOption(
                label=_remove_option_label(s),
                value=s["id"],
                description=(s.get("user_name") or "")[:100] or None,
            )
            for s in chunk if s.get("id")
        ]
        super().__init__(placeholder=placeholder, options=options,
                         min_values=1, max_values=1)
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        await admin_do_remove_suggestion(interaction, self.view.db_id, self.values[0])


async def admin_remove_suggestion(interaction: discord.Interaction, db_id: int):
    """Render the picker view for choosing a suggestion to remove."""
    settings = db.get_guild_settings(interaction.guild_id)
    lang = settings.get("language", "en") if settings else "en"

    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("event.no_event", lang),
                                color=discord.Color.red()),
            view=None,
        )
        return

    suggestions = record["event"].get("suggestions", [])
    if not suggestions:
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("admin.no_suggestions", lang),
                                color=discord.Color.orange()),
            view=None,
        )
        return

    visible = suggestions[:_REMOVE_PICKER_MAX_SUGGESTIONS]
    embed = discord.Embed(
        title=t("admin.remove_suggestion", lang),
        description=t("admin.remove_prompt", lang, count=len(visible)),
        color=discord.Color.dark_red(),
    )
    await interaction.response.edit_message(
        embed=embed,
        view=RemoveSuggestionView(visible, lang, db_id),
    )


async def admin_do_remove_suggestion(interaction: discord.Interaction,
                                     db_id: int, suggestion_id: str):
    """Remove the chosen suggestion, refresh the event embed, and re-render
    the picker so the admin can remove more without reopening the panel.
    """
    settings = db.get_guild_settings(interaction.guild_id)
    lang = settings.get("language", "en") if settings else "en"

    removed: Optional[dict] = None
    remaining: list[dict] = []
    lock = _get_guild_lock(interaction.guild_id)
    async with lock:
        record = db.get_event_by_db_id(interaction.guild_id, db_id)
        if not record:
            await interaction.response.edit_message(
                embed=discord.Embed(description=t("event.no_event", lang),
                                    color=discord.Color.red()),
                view=None,
            )
            return

        event = record["event"]
        new_list: list[dict] = []
        for s in event.get("suggestions", []):
            if removed is None and s.get("id") == suggestion_id:
                removed = s
                continue
            new_list.append(s)

        if removed is None:
            await interaction.response.edit_message(
                embed=discord.Embed(description=t("admin.remove_not_found", lang),
                                    color=discord.Color.orange()),
                view=None,
            )
            return

        event["suggestions"] = new_list
        remaining = new_list
        db.save_event(record["db_id"], event)

    # Refresh the public event embed. The per-user suggestion limit is
    # computed live from event["suggestions"], so removal automatically frees
    # the slot for the original suggester.
    await _update_event_embed(db_id)

    await send_to_log_channel(
        f"Suggestion removed by {interaction.user.display_name}: "
        f"{format_layer_short(removed)} (originally by {removed.get('user_name', '?')})",
        guild_id=interaction.guild_id,
    )

    removed_line = t("admin.suggestion_removed", lang,
                     layer=format_layer_short(removed))
    if remaining:
        visible = remaining[:_REMOVE_PICKER_MAX_SUGGESTIONS]
        embed = discord.Embed(
            title=t("admin.remove_suggestion", lang),
            description=(
                f"✅ {removed_line}\n\n"
                f"{t('admin.remove_prompt', lang, count=len(visible))}"
            ),
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(
            embed=embed,
            view=RemoveSuggestionView(visible, lang, db_id),
        )
    else:
        await interaction.response.edit_message(
            embed=discord.Embed(description=f"✅ {removed_line}",
                                color=discord.Color.green()),
            view=None,
        )


# ═══════════════════════════════════════════════════════════════════════════
# EVENT EDIT DIALOG (DM)
# ═══════════════════════════════════════════════════════════════════════════
#
# Admin → Edit on the event embed opens a private DM dialog where the
# organizer can change this event's per-event configuration (gamemodes,
# blacklists, suggestion limits, voting params) without affecting any other
# event or the guild defaults. Replaces the old /config_gamemodes,
# /config_layer_sources, /config_blacklist, /config_suggestions slash
# commands; their picker UIs are reborn as views inside this dialog and
# write to event["config"] / event[...] instead of guild_settings.
#
# Session model: a single DM message (kept on the in-memory session dict) is
# re-rendered with different views for the property selector, list pickers,
# bool toggles, and scalar modals. Component interactions edit that message
# in place; modal submits edit it via the stored Message reference.

# user_id -> {db_id, guild_id, lang, dm_message}
_active_edit_sessions: dict[int, dict] = {}


def _format_duration_seconds(seconds: int) -> str:
    """Render seconds as '60', '2h', '1d' style — round-trip via parse_duration_to_seconds."""
    s = int(seconds)
    if s <= 0:
        return "0"
    if s % 86400 == 0:
        return f"{s // 86400}d"
    if s % 3600 == 0:
        return f"{s // 3600}h"
    if s % 60 == 0:
        return f"{s // 60}m"
    return f"{s}s"


def _format_property_value(value, kind: str) -> str:
    """Compact one-line display of a property's current value."""
    if kind == "list":
        if not value:
            return "—"
        joined = ", ".join(value[:5])
        return joined + ("…" if len(value) > 5 else "")
    if kind == "bool":
        return "✅" if value else "❌"
    if kind == "duration":
        if not value:
            return "—"
        return _format_duration_seconds(int(value))
    if value is None:
        return "—"
    return str(value)


def _read_event_property(event: dict, key: str, target: str):
    """Read a property's current value from event[key] or event["config"][key]."""
    if target == "config":
        return (event.get("config") or {}).get(key)
    return event.get(key)


def _write_event_property(event: dict, key: str, target: str, value) -> None:
    """Write a property's value into event[key] or event["config"][key]."""
    if target == "config":
        event.setdefault("config", {})[key] = value
    else:
        event[key] = value


# Properties exposed by the DM edit dialog. Keys live on either event["config"]
# (the per-event snapshot of guild settings — Phase 2) or directly on the
# event (sources, voting params, suggestion timer). `source` is a callable
# returning the available choices for "list" kinds.
_EDIT_PROPERTIES: list[dict] = [
    {"key": "allowed_gamemodes",         "label_key": "edit.prop.allowed_gamemodes",     "kind": "list",     "target": "config", "source": db.get_unique_gamemodes},
    {"key": "blacklisted_maps",          "label_key": "edit.prop.blacklisted_maps",      "kind": "list",     "target": "config", "source": db.get_unique_maps},
    {"key": "blacklisted_factions",      "label_key": "edit.prop.blacklisted_factions",  "kind": "list",     "target": "config", "source": db.get_unique_factions},
    {"key": "blacklisted_units",         "label_key": "edit.prop.blacklisted_units",     "kind": "list",     "target": "config", "source": db.get_unique_unit_types},
    {"key": "max_suggestions_per_user",  "label_key": "edit.prop.max_per_user",          "kind": "int",      "target": "config", "min": 1,  "max": 10},
    {"key": "max_total_suggestions",     "label_key": "edit.prop.max_total",             "kind": "int",      "target": "config", "min": 1,  "max": 25},
    {"key": "history_lookback_events",   "label_key": "edit.prop.history_lookback",      "kind": "int",      "target": "config", "min": 0,  "max": 50},
    {"key": "allowed_sources",           "label_key": "edit.prop.allowed_sources",       "kind": "list",     "target": "event",  "source": db.get_unique_sources},
    {"key": "voting_duration_hours",     "label_key": "edit.prop.voting_duration",       "kind": "int",      "target": "event",  "min": 1,  "max": MAX_VOTING_DURATION_HOURS},
    {"key": "max_voting_layers",         "label_key": "edit.prop.max_voting_layers",     "kind": "int",      "target": "event",  "min": 1,  "max": 10},
    {"key": "allow_multiple_votes",      "label_key": "edit.prop.allow_multiple_votes",  "kind": "bool",     "target": "event"},
    {"key": "suggestion_duration_seconds", "label_key": "edit.prop.suggestion_duration", "kind": "duration", "target": "event"},
]


def _find_edit_property(key: str) -> Optional[dict]:
    return next((p for p in _EDIT_PROPERTIES if p["key"] == key), None)


def _build_edit_main_embed(event: dict, lang: str,
                           updated_label: Optional[str] = None) -> discord.Embed:
    """Property overview embed shown at the top of every DM dialog state."""
    embed = discord.Embed(
        title=t("edit.title", lang),
        description=t("edit.select_property", lang),
        color=discord.Color.blurple(),
    )
    for prop in _EDIT_PROPERTIES:
        value = _read_event_property(event, prop["key"], prop["target"])
        formatted = _format_property_value(value, prop["kind"])
        embed.add_field(
            name=t(prop["label_key"], lang),
            value=f"`{formatted}`",
            inline=True,
        )
    if updated_label:
        embed.add_field(
            name="​",
            value=f"✅ {t('edit.updated_inline', lang, prop=updated_label)}",
            inline=False,
        )
    return embed


async def admin_edit_event(interaction: discord.Interaction, db_id: int):
    """Kick off a DM edit session for this event. Triggered by Admin → Edit."""
    user = interaction.user
    settings = db.get_guild_settings(interaction.guild_id) or {}
    lang = settings.get("language", "en")

    if user.id in _active_edit_sessions:
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("edit.session_active", lang), color=discord.Color.orange()),
            view=None,
        )
        return

    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("event.no_event", lang), color=discord.Color.red()),
            view=None,
        )
        return

    try:
        dm = await user.create_dm()
    except discord.Forbidden:
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("edit.dm_blocked", lang), color=discord.Color.red()),
            view=None,
        )
        return

    # Reserve the session BEFORE the DM send so the view's callbacks can find it.
    session: dict = {
        "db_id": db_id,
        "guild_id": interaction.guild_id,
        "lang": lang,
        "dm_message": None,
        "active_view": None,
    }
    _active_edit_sessions[user.id] = session

    embed = _build_edit_main_embed(record["event"], lang)
    view = EditMainView(user.id, db_id, interaction.guild_id, lang)
    try:
        dm_msg = await dm.send(embed=embed, view=view)
    except discord.Forbidden:
        _active_edit_sessions.pop(user.id, None)
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("edit.dm_blocked", lang), color=discord.Color.red()),
            view=None,
        )
        return
    session["dm_message"] = dm_msg
    session["active_view"] = view

    await interaction.response.edit_message(
        embed=discord.Embed(
            description=f"📨 {t('edit.dm_sent', lang)}",
            color=discord.Color.green(),
        ),
        view=None,
    )


def _close_session(user_id: int) -> None:
    _active_edit_sessions.pop(user_id, None)


def _set_active_view(user_id: int, view: ui.View) -> None:
    """Mark `view` as the currently displayed dialog view for this session.

    Used by _handle_edit_timeout to ignore stale on_timeout callbacks from
    views the user has already navigated away from.
    """
    session = _active_edit_sessions.get(user_id)
    if session is not None:
        session["active_view"] = view


async def _handle_edit_timeout(view: ui.View, user_id: int) -> None:
    """Inform the user we gave up waiting and disable the dialog.

    Called from the on_timeout of any edit view. No-op if the session was
    already closed (e.g. the user pressed Done before the timer fired) or
    if `view` is a stale view the user has already navigated away from —
    each navigation creates a fresh view whose timer supersedes the old one.
    """
    session = _active_edit_sessions.get(user_id)
    if not session or session.get("active_view") is not view:
        return
    _active_edit_sessions.pop(user_id, None)
    lang = session.get("lang", "en")
    dm_msg = session.get("dm_message")
    if dm_msg is None:
        return
    try:
        await dm_msg.edit(view=None)
    except discord.HTTPException:
        pass
    try:
        await dm_msg.channel.send(t("edit.timeout", lang))
    except discord.HTTPException:
        pass


async def _refresh_main_view(interaction: discord.Interaction, user_id: int,
                             db_id: int, guild_id: int, lang: str,
                             updated_label: Optional[str] = None,
                             via_modal: bool = False) -> None:
    """Re-render the property selector after an edit or cancel.

    `via_modal=True` means the originating interaction is a Modal submission,
    in which case we ack the modal and edit the stored DM message directly
    (interaction.response.edit_message doesn't apply to modal submits).
    """
    record = db.get_event_by_db_id(guild_id, db_id)
    if not record:
        _close_session(user_id)
        if via_modal:
            try:
                await interaction.response.send_message(
                    t("event.no_event", lang), ephemeral=True)
            except discord.InteractionResponded:
                pass
        else:
            await interaction.response.edit_message(
                embed=discord.Embed(description=t("event.no_event", lang), color=discord.Color.red()),
                view=None,
            )
        return

    embed = _build_edit_main_embed(record["event"], lang, updated_label=updated_label)
    view = EditMainView(user_id, db_id, guild_id, lang)
    _set_active_view(user_id, view)

    if via_modal:
        # Acknowledge the modal silently, then edit the stored DM message.
        try:
            await interaction.response.defer()
        except discord.InteractionResponded:
            pass
        session = _active_edit_sessions.get(user_id)
        dm_msg = session.get("dm_message") if session else None
        if dm_msg is not None:
            await dm_msg.edit(embed=embed, view=view)
    else:
        await interaction.response.edit_message(embed=embed, view=view)


class EditMainView(ui.View):
    """Top-level DM view: a property dropdown + a Done button."""

    def __init__(self, user_id: int, db_id: int, guild_id: int, lang: str):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.db_id = db_id
        self.guild_id = guild_id
        self.lang = lang

        options = [
            discord.SelectOption(
                label=t(prop["label_key"], lang)[:100],
                value=prop["key"],
            )
            for prop in _EDIT_PROPERTIES
        ]
        select = ui.Select(
            placeholder=t("edit.pick_property_placeholder", lang),
            options=options,
            min_values=1, max_values=1,
        )
        select.callback = self._on_select
        self.add_item(select)

        done = ui.Button(
            label=t("edit.done", lang),
            style=discord.ButtonStyle.secondary,
            emoji="🛑",
        )
        done.callback = self._on_done
        self.add_item(done)

    async def _on_select(self, interaction: discord.Interaction):
        prop = _find_edit_property(interaction.data["values"][0])
        if not prop:
            return
        await _show_property_editor(interaction, self.user_id, self.db_id,
                                    self.guild_id, self.lang, prop)

    async def _on_done(self, interaction: discord.Interaction):
        _close_session(self.user_id)
        # Keep the overview embed visible; just strip the dropdown + Done
        # button, then append the closing line as a new DM message below.
        try:
            await interaction.response.edit_message(view=None)
        except discord.HTTPException:
            pass
        try:
            await interaction.channel.send(t("edit.finished", self.lang))
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        await _handle_edit_timeout(self, self.user_id)


async def _show_property_editor(interaction: discord.Interaction, user_id: int,
                                db_id: int, guild_id: int, lang: str,
                                prop: dict) -> None:
    """Render the editor UI for a specific property, replacing the main view."""
    record = db.get_event_by_db_id(guild_id, db_id)
    if not record:
        _close_session(user_id)
        await interaction.response.edit_message(
            embed=discord.Embed(description=t("event.no_event", lang), color=discord.Color.red()),
            view=None,
        )
        return

    event = record["event"]
    current = _read_event_property(event, prop["key"], prop["target"])
    label = t(prop["label_key"], lang)

    if prop["kind"] == "list":
        choices = prop["source"]() if prop.get("source") else []
        if not choices:
            fallback_view = EditMainView(user_id, db_id, guild_id, lang)
            _set_active_view(user_id, fallback_view)
            await interaction.response.edit_message(
                embed=discord.Embed(description=t("cache.empty", lang), color=discord.Color.orange()),
                view=fallback_view,
            )
            return
        current_set = set(current or [])
        # An empty `allowed_sources` means "all sources allowed" — preselect
        # everything so the admin sees the effective state, mirroring the old
        # /config_layer_sources behaviour.
        if prop["key"] == "allowed_sources" and not current_set:
            current_set = set(choices)
        options = [
            discord.SelectOption(label=c[:100], value=c, default=(c in current_set))
            for c in choices[:25]
        ]
        view = EditListView(user_id, db_id, guild_id, lang, prop, options)
        _set_active_view(user_id, view)
        embed = discord.Embed(
            title=label,
            description=t("edit.list_prompt", lang),
            color=discord.Color.blurple(),
        )
        await interaction.response.edit_message(embed=embed, view=view)

    elif prop["kind"] == "bool":
        view = EditBoolView(user_id, db_id, guild_id, lang, prop, bool(current))
        _set_active_view(user_id, view)
        embed = discord.Embed(
            title=label,
            description=t("edit.bool_prompt", lang,
                          value=_format_property_value(current, "bool")),
            color=discord.Color.blurple(),
        )
        await interaction.response.edit_message(embed=embed, view=view)

    else:  # int / duration — both go through a Modal triggered by a button
        view = EditScalarView(user_id, db_id, guild_id, lang, prop)
        _set_active_view(user_id, view)
        if prop["kind"] == "int":
            desc = t("edit.int_prompt", lang,
                     current=_format_property_value(current, "int"),
                     min=prop.get("min", "—"), max=prop.get("max", "—"))
        else:
            desc = t("edit.duration_prompt", lang,
                     current=_format_property_value(current, "duration"))
        embed = discord.Embed(title=label, description=desc, color=discord.Color.blurple())
        await interaction.response.edit_message(embed=embed, view=view)


class EditListView(ui.View):
    """Multi-select editor for list-typed properties."""

    def __init__(self, user_id: int, db_id: int, guild_id: int, lang: str,
                 prop: dict, options: list[discord.SelectOption]):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.db_id = db_id
        self.guild_id = guild_id
        self.lang = lang
        self.prop = prop

        select = ui.Select(
            placeholder=t("edit.list_placeholder", lang),
            options=options,
            min_values=0, max_values=len(options),
        )
        select.callback = self._on_save
        self.add_item(select)

        cancel = ui.Button(
            label=t("general.cancel", lang),
            style=discord.ButtonStyle.secondary, emoji="↩️",
        )
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    async def _on_save(self, interaction: discord.Interaction):
        new_values = list(interaction.data.get("values", []))
        await _apply_edit(interaction, self.user_id, self.db_id, self.guild_id,
                          self.lang, self.prop, new_values)

    async def _on_cancel(self, interaction: discord.Interaction):
        await _refresh_main_view(interaction, self.user_id, self.db_id,
                                 self.guild_id, self.lang)

    async def on_timeout(self):
        await _handle_edit_timeout(self, self.user_id)


class EditBoolView(ui.View):
    """Two-button toggle for bool properties."""

    def __init__(self, user_id: int, db_id: int, guild_id: int, lang: str,
                 prop: dict, current_value: bool):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.db_id = db_id
        self.guild_id = guild_id
        self.lang = lang
        self.prop = prop

        yes = ui.Button(
            label=t("edit.bool_yes", lang),
            style=discord.ButtonStyle.success if current_value else discord.ButtonStyle.secondary,
            emoji="✅",
        )
        yes.callback = self._make_setter(True)
        self.add_item(yes)

        no = ui.Button(
            label=t("edit.bool_no", lang),
            style=discord.ButtonStyle.danger if not current_value else discord.ButtonStyle.secondary,
            emoji="❌",
        )
        no.callback = self._make_setter(False)
        self.add_item(no)

        cancel = ui.Button(
            label=t("general.cancel", lang),
            style=discord.ButtonStyle.secondary, emoji="↩️",
        )
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    def _make_setter(self, value: bool):
        async def cb(interaction: discord.Interaction):
            await _apply_edit(interaction, self.user_id, self.db_id, self.guild_id,
                              self.lang, self.prop, value)
        return cb

    async def _on_cancel(self, interaction: discord.Interaction):
        await _refresh_main_view(interaction, self.user_id, self.db_id,
                                 self.guild_id, self.lang)

    async def on_timeout(self):
        await _handle_edit_timeout(self, self.user_id)


class EditScalarView(ui.View):
    """Wrapper for int/duration properties — opens a Modal on click.

    Modals can only be opened from a component interaction, so we have to
    chain Component → Modal rather than putting the TextInput in the view.
    """

    def __init__(self, user_id: int, db_id: int, guild_id: int, lang: str,
                 prop: dict):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.db_id = db_id
        self.guild_id = guild_id
        self.lang = lang
        self.prop = prop

        edit = ui.Button(
            label=t("edit.open_input", lang),
            style=discord.ButtonStyle.primary, emoji="⌨️",
        )
        edit.callback = self._on_edit
        self.add_item(edit)

        cancel = ui.Button(
            label=t("general.cancel", lang),
            style=discord.ButtonStyle.secondary, emoji="↩️",
        )
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    async def _on_edit(self, interaction: discord.Interaction):
        modal = EditScalarModal(self.user_id, self.db_id, self.guild_id,
                                self.lang, self.prop)
        await interaction.response.send_modal(modal)

    async def _on_cancel(self, interaction: discord.Interaction):
        await _refresh_main_view(interaction, self.user_id, self.db_id,
                                 self.guild_id, self.lang)

    async def on_timeout(self):
        await _handle_edit_timeout(self, self.user_id)


class EditScalarModal(ui.Modal):
    """Text-input modal for int / duration properties."""

    def __init__(self, user_id: int, db_id: int, guild_id: int, lang: str,
                 prop: dict):
        super().__init__(title=t(prop["label_key"], lang)[:45])
        self.user_id = user_id
        self.db_id = db_id
        self.guild_id = guild_id
        self.lang = lang
        self.prop = prop

        if prop["kind"] == "int":
            placeholder = f"{prop.get('min', 0)}–{prop.get('max', '?')}"
        else:
            placeholder = "60, 2h, 1d, 1w"
        self.value_input = ui.TextInput(
            label=t("edit.input_label", lang)[:45],
            placeholder=placeholder,
            required=True,
            max_length=20,
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = (self.value_input.value or "").strip()
        if self.prop["kind"] == "int":
            try:
                value = int(raw)
            except ValueError:
                await interaction.response.send_message(
                    t("edit.invalid_int", self.lang, value=raw), ephemeral=True)
                return
            mn = self.prop.get("min")
            mx = self.prop.get("max")
            if (mn is not None and value < mn) or (mx is not None and value > mx):
                await interaction.response.send_message(
                    t("edit.out_of_range", self.lang, value=value, min=mn, max=mx),
                    ephemeral=True,
                )
                return
        else:  # duration
            value = parse_duration_to_seconds(raw)
            if value is None:
                await interaction.response.send_message(
                    t("phase.invalid_duration", self.lang, value=raw), ephemeral=True)
                return

        await _apply_edit(interaction, self.user_id, self.db_id, self.guild_id,
                          self.lang, self.prop, value, via_modal=True)


async def _apply_edit(interaction: discord.Interaction, user_id: int,
                      db_id: int, guild_id: int, lang: str,
                      prop: dict, value, via_modal: bool = False) -> None:
    """Persist an edit, refresh the public event embed, return to main view."""
    lock = _get_guild_lock(guild_id)
    async with lock:
        record = db.get_event_by_db_id(guild_id, db_id)
        if not record:
            _close_session(user_id)
            if via_modal:
                try:
                    await interaction.response.send_message(
                        t("event.no_event", lang), ephemeral=True)
                except discord.InteractionResponded:
                    pass
            else:
                await interaction.response.edit_message(
                    embed=discord.Embed(description=t("event.no_event", lang),
                                        color=discord.Color.red()),
                    view=None,
                )
            return
        event = record["event"]
        _write_event_property(event, prop["key"], prop["target"], value)
        db.save_event(record["db_id"], event)

    # Refresh the in-channel event embed so users see the new config.
    await _update_event_embed(db_id)

    label = t(prop["label_key"], lang)
    await _refresh_main_view(interaction, user_id, db_id, guild_id, lang,
                             updated_label=label, via_modal=via_modal)


# ═══════════════════════════════════════════════════════════════════════════
# EVENT EMBED UPDATE
# ═══════════════════════════════════════════════════════════════════════════

# Keyed by event db_id rather than (guild,channel) since multiple events may
# share a channel; debouncing must be per-event.
_display_update_tasks: dict[int, asyncio.Task] = {}


async def _update_event_embed(db_id: int):
    """Debounced update of a specific event's embed message."""
    task = _display_update_tasks.get(db_id)
    if task and not task.done():
        task.cancel()
    _display_update_tasks[db_id] = asyncio.create_task(_do_update_embed(db_id))


async def _do_update_embed(db_id: int):
    """Actually update the event embed after a short delay."""
    await asyncio.sleep(2)

    record = db.get_active_event_unsafe(db_id)
    if not record:
        return
    guild_id = record["guild_id"]
    channel_id = record["channel_id"]
    event = record["event"]

    settings = db.get_guild_settings(guild_id)
    if not settings:
        return

    embed = build_event_embed(event, settings)
    msg_id = event.get("event_message_id")
    if not msg_id:
        return

    try:
        guild = bot.get_guild(guild_id)
        if not guild:
            return
        channel = guild.get_channel(channel_id)
        if not channel:
            return
        message = await channel.fetch_message(msg_id)

        lang = settings.get("language", "en")
        phase = event.get("phase", "created")
        if phase == "completed":
            await message.edit(embed=embed, view=None)
        else:
            await message.edit(embed=embed, view=EventActionView(db_id, lang))
    except discord.NotFound:
        logger.warning(f"Event message {msg_id} not found in {channel_id}")
    except Exception as e:
        logger.error(f"Error updating event embed: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# SLASH COMMANDS — Setup & Config
# ═══════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="setup", description="Initial server setup for the Layer Vote Bot")
@app_commands.describe(
    organizer_role="The role that can manage events",
    log_channel="Channel for bot log messages",
    language="Bot language",
)
@app_commands.choices(language=[
    app_commands.Choice(name="English", value="en"),
    app_commands.Choice(name="Deutsch", value="de"),
])
async def cmd_setup(interaction: discord.Interaction, organizer_role: discord.Role,
                    log_channel: discord.TextChannel,
                    language: app_commands.Choice[str] = None):
    if not await check_admin(interaction):
        return

    lang_value = language.value if language else "en"

    settings = db.get_guild_settings(interaction.guild_id) or dict(db.DEFAULT_GUILD_SETTINGS)
    settings["organizer_role_id"] = organizer_role.id
    settings["log_channel_id"] = log_channel.id
    settings["language"] = lang_value
    db.save_guild_settings(interaction.guild_id, settings)

    set_log_channel(interaction.guild_id, log_channel)

    msg = t("setup.welcome", lang_value, role=organizer_role.mention,
            channel=log_channel.mention, language=lang_value.upper())
    await interaction.response.send_message(msg, ephemeral=True)
    await send_to_log_channel(f"Server setup by {interaction.user.display_name}", guild_id=interaction.guild_id)


@bot.tree.command(name="set_organizer_role", description="Change the organizer role")
@app_commands.describe(role="The new organizer role")
async def cmd_set_organizer_role(interaction: discord.Interaction, role: discord.Role):
    if not await check_admin(interaction):
        return
    settings = await check_guild_configured(interaction)
    if not settings:
        return

    settings["organizer_role_id"] = role.id
    db.save_guild_settings(interaction.guild_id, settings)
    lang = settings.get("language", "en")
    await interaction.response.send_message(
        t("setup.organizer_role_updated", lang, role=role.mention), ephemeral=True)


@bot.tree.command(name="set_language", description="Change the bot language")
@app_commands.describe(language="Language (en/de)")
@app_commands.choices(language=[
    app_commands.Choice(name="English", value="en"),
    app_commands.Choice(name="Deutsch", value="de"),
])
async def cmd_set_language(interaction: discord.Interaction, language: app_commands.Choice[str]):
    if not await check_admin(interaction):
        return
    settings = await check_guild_configured(interaction)
    if not settings:
        return

    settings["language"] = language.value
    db.save_guild_settings(interaction.guild_id, settings)
    await interaction.response.send_message(
        t("setup.language_updated", language.value, language=language.value.upper()), ephemeral=True)

    # Refresh all active event embeds in this guild so the language change takes effect
    for ev in db.get_all_active_events_global():
        if ev["guild_id"] == interaction.guild_id:
            await _update_event_embed(ev["db_id"])


@bot.tree.command(name="set_log_channel", description="Change the log channel")
@app_commands.describe(channel="The new log channel")
async def cmd_set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await check_admin(interaction):
        return
    settings = await check_guild_configured(interaction)
    if not settings:
        return

    settings["log_channel_id"] = channel.id
    db.save_guild_settings(interaction.guild_id, settings)
    set_log_channel(interaction.guild_id, channel)
    lang = settings.get("language", "en")
    await interaction.response.send_message(
        t("setup.log_channel_updated", lang, channel=channel.mention), ephemeral=True)


@bot.tree.command(name="settings", description="View current server settings")
async def cmd_settings(interaction: discord.Interaction):
    settings = await check_guild_configured(interaction)
    if not settings:
        return
    if not await check_organizer(interaction, settings):
        return

    layer_count = db.get_layer_cache_count()
    embed = build_settings_embed(settings, interaction.guild, layer_count)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="sync", description="Force sync slash commands")
async def cmd_sync(interaction: discord.Interaction):
    if not await check_admin(interaction):
        return
    await bot.tree.sync()
    await interaction.response.send_message("Commands synced.", ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
# Per-event configuration is edited via the Admin → Edit DM dialog (see the
# `EVENT EDIT DIALOG` section). The previous /config_gamemodes,
# /config_layer_sources, /config_blacklist, and /config_suggestions slash
# commands wrote to guild-wide settings, which leaked across every event in
# the channel — they were removed in Phase 3 along with their picker views
# (BlacklistConfigView / GamemodeConfigView / SourceConfigView), whose logic
# now lives inside the DM dialog and writes to event["config"] instead.
# ═══════════════════════════════════════════════════════════════════════════


@bot.tree.command(name="config_create_suggestion",
                  description="Set defaults for /create_layer_suggestion parameters")
@app_commands.describe(
    suggestion_start="Default offset to auto-open suggestions after command run (e.g. '1h', '30m'). Empty string clears.",
    suggestion_duration="Default suggestion window length (e.g. '60', '2h'). Empty string clears.",
    voting_duration_hours="Default vote length: bare number = hours, or '24h' / '2d' / '1w'. Max '2w' (14 days).",
    allow_multiple_votes="Default for allowing multiple poll votes per user",
)
async def cmd_config_create_suggestion(interaction: discord.Interaction,
                                       suggestion_start: str = None,
                                       suggestion_duration: str = None,
                                       voting_duration_hours: str = None,
                                       allow_multiple_votes: bool = None):
    settings = await check_guild_configured(interaction)
    if not settings:
        return
    if not await check_organizer(interaction, settings):
        return

    lang = settings.get("language", "en")

    if suggestion_start is not None:
        stripped = suggestion_start.strip()
        if stripped == "":
            settings["default_suggestion_start"] = None
        else:
            if parse_duration_to_seconds(stripped) is None:
                await interaction.response.send_message(
                    t("phase.invalid_duration", lang, value=suggestion_start),
                    ephemeral=True,
                )
                return
            settings["default_suggestion_start"] = stripped

    if suggestion_duration is not None:
        stripped = suggestion_duration.strip()
        if stripped == "":
            settings["default_suggestion_duration"] = None
        else:
            if parse_duration_to_seconds(stripped) is None:
                await interaction.response.send_message(
                    t("phase.invalid_duration", lang, value=suggestion_duration),
                    ephemeral=True,
                )
                return
            settings["default_suggestion_duration"] = stripped

    if voting_duration_hours is not None:
        parsed_voting_hours = parse_voting_duration_to_hours(voting_duration_hours)
        if parsed_voting_hours is None:
            await interaction.response.send_message(
                t("phase.invalid_duration", lang, value=voting_duration_hours),
                ephemeral=True,
            )
            return
        settings["default_voting_duration_hours"] = parsed_voting_hours

    if allow_multiple_votes is not None:
        settings["default_allow_multiple_votes"] = bool(allow_multiple_votes)

    db.save_guild_settings(interaction.guild_id, settings)

    summary = (
        f"• suggestion_start: `{settings.get('default_suggestion_start') or '—'}`\n"
        f"• suggestion_duration: `{settings.get('default_suggestion_duration') or '—'}`\n"
        f"• voting_duration_hours: `{settings.get('default_voting_duration_hours', 24)}`\n"
        f"• allow_multiple_votes: `{settings.get('default_allow_multiple_votes', False)}`"
    )
    await interaction.response.send_message(
        f"{t('config.create_suggestion_updated', lang)}\n{summary}",
        ephemeral=True,
    )


@bot.tree.command(name="refresh_layers", description="Re-fetch layer data from GitHub")
async def cmd_refresh_layers(interaction: discord.Interaction):
    settings = await check_guild_configured(interaction)
    if not settings:
        return
    if not await check_organizer(interaction, settings):
        return

    lang = settings.get("language", "en")
    await interaction.response.defer(ephemeral=True)

    try:
        count = await fetch_and_cache_layers()
        await interaction.followup.send(t("cache.refreshed", lang, count=count), ephemeral=True)
        await send_to_log_channel(f"Layer cache refreshed: {count} layers", guild_id=interaction.guild_id)
    except Exception as e:
        logger.error(f"Error refreshing layers: {e}")
        await interaction.followup.send(t("cache.error", lang, error=str(e)), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
# SLASH COMMANDS — Event Management
# ═══════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="create_layer_suggestion", description="Create a new layer vote event in this channel")
@app_commands.describe(
    suggestion_start="When to auto-open suggestions (DD.MM.YYYY HH:MM) or leave empty for manual",
    suggestion_duration="Suggestion window length, e.g. '60' (mins), '2h', '1d'. Empty = manual close.",
    voting_duration_hours="Vote length: bare number = hours, or '24h' / '2d' / '1w'. Max '2w' (14 days).",
    allow_multiple_votes="Allow each voter to pick multiple layers in the poll",
)
async def cmd_create_event(interaction: discord.Interaction,
                           suggestion_start: str = None,
                           suggestion_duration: str = None,
                           voting_duration_hours: str = None,
                           allow_multiple_votes: bool = None):
    settings = await check_guild_configured(interaction)
    if not settings:
        return
    if not await check_organizer(interaction, settings):
        return

    lang = settings.get("language", "en")

    if db.get_layer_cache_count() == 0:
        await interaction.response.send_message(t("cache.empty", lang), ephemeral=True)
        return

    # Apply guild-configured defaults when params are omitted
    if suggestion_start is None:
        suggestion_start = settings.get("default_suggestion_start")
    if suggestion_duration is None:
        suggestion_duration = settings.get("default_suggestion_duration")
    if allow_multiple_votes is None:
        allow_multiple_votes = settings.get("default_allow_multiple_votes", False)

    if voting_duration_hours is not None:
        parsed_voting_hours = parse_voting_duration_to_hours(voting_duration_hours)
        if parsed_voting_hours is None:
            await interaction.response.send_message(
                t("phase.invalid_duration", lang, value=voting_duration_hours),
                ephemeral=True,
            )
            return
        voting_duration_hours = parsed_voting_hours
    else:
        voting_duration_hours = int(settings.get("default_voting_duration_hours", 24))

    # Parse suggestion start time. Accept absolute timestamps OR a duration
    # offset from now (e.g. "1h" = start in one hour) — the latter is what
    # gets stored by /config_create_suggestion.
    sst = None
    if suggestion_start:
        for fmt in ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M", "%d.%m %H:%M"):
            try:
                sst = datetime.strptime(suggestion_start, fmt)
                if fmt == "%d.%m %H:%M":
                    sst = sst.replace(year=datetime.now().year)
                break
            except ValueError:
                continue
        if sst is None:
            offset_seconds = parse_duration_to_seconds(suggestion_start)
            if offset_seconds is not None:
                sst = datetime.now() + timedelta(seconds=offset_seconds)

    suggestion_duration_seconds = None
    if suggestion_duration:
        suggestion_duration_seconds = parse_duration_to_seconds(suggestion_duration)
        if suggestion_duration_seconds is None:
            await interaction.response.send_message(
                t("phase.invalid_duration", lang, value=suggestion_duration),
                ephemeral=True,
            )
            return

    # Resolve which sources should be allowed for this event:
    #   - Take all sources currently in the cache as the universe of options.
    #   - Intersect with the guild's `allowed_sources` default, if set.
    cache_sources = db.get_unique_sources()
    guild_default = settings.get("allowed_sources") or []
    if guild_default:
        offered = [s for s in cache_sources if s in guild_default]
    else:
        offered = list(cache_sources)

    if not offered:
        await interaction.response.send_message(t("cache.empty", lang), ephemeral=True)
        return

    # If only one source is available, skip the picker entirely.
    if len(offered) == 1:
        await _finalize_event_creation(
            interaction, settings, lang,
            allowed_sources=offered,
            sst=sst,
            suggestion_duration_seconds=suggestion_duration_seconds,
            voting_duration_hours=voting_duration_hours,
            allow_multiple_votes=allow_multiple_votes,
            ack_via_followup=False,
        )
        return

    # Multiple sources → ask the admin which to expose to users.
    options = [
        discord.SelectOption(label=s, value=s, default=True)
        for s in offered[:25]
    ]
    view = EventSourceSelectView(
        options=options,
        lang=lang,
        sst=sst,
        suggestion_duration_seconds=suggestion_duration_seconds,
        voting_duration_hours=voting_duration_hours,
        allow_multiple_votes=allow_multiple_votes,
    )
    embed = discord.Embed(
        title=t("event.select_sources_title", lang),
        description=t("event.select_sources_desc", lang),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def _finalize_event_creation(interaction: discord.Interaction, settings: dict, lang: str,
                                   *, allowed_sources: list[str],
                                   sst, suggestion_duration_seconds,
                                   voting_duration_hours, allow_multiple_votes,
                                   ack_via_followup: bool):
    """Create the event row and post its embed. Used by both the no-picker path
    (single source) and the EventSourceSelectView confirm callback."""
    event_data = db.build_default_event(suggestion_start_time=sst, settings=settings)
    event_data["voting_duration_hours"] = max(1, min(MAX_VOTING_DURATION_HOURS, voting_duration_hours))
    event_data["suggestion_duration_seconds"] = suggestion_duration_seconds
    event_data["allow_multiple_votes"] = bool(allow_multiple_votes)
    event_data["allowed_sources"] = list(allowed_sources)

    # Create event in DB first to get its db_id; the EventActionView and the
    # follow-up update both need it baked into their button custom_ids.
    db_id = db.create_event(interaction.guild_id, interaction.channel_id, event_data)

    embed = build_event_embed(event_data, settings)
    view = EventActionView(db_id, lang)
    msg = await interaction.channel.send(embed=embed, view=view)

    # Save message ID
    lock = _get_guild_lock(interaction.guild_id)
    async with lock:
        record = db.get_event_by_db_id(interaction.guild_id, db_id)
        if record:
            event = record["event"]
            event["event_message_id"] = msg.id
            db.save_event(record["db_id"], event)

    ack_text = f"✅ {t('event.created', lang)}"
    if ack_via_followup:
        await interaction.response.edit_message(content=ack_text, embed=None, view=None)
    else:
        await interaction.response.send_message(ack_text, ephemeral=True)
    await send_to_log_channel(
        f"Event created in <#{interaction.channel_id}> by {interaction.user.display_name} "
        f"(sources: {', '.join(allowed_sources)})",
        guild_id=interaction.guild_id,
    )


class EventSourceSelectView(ui.View):
    """Per-event source picker shown when /create_layer_suggestion is run with
    more than one source available. Confirms with the admin's selection and
    then proceeds to event creation."""

    def __init__(self, options, lang, sst, suggestion_duration_seconds,
                 voting_duration_hours, allow_multiple_votes):
        super().__init__(timeout=180)
        self.lang = lang
        self.sst = sst
        self.suggestion_duration_seconds = suggestion_duration_seconds
        self.voting_duration_hours = voting_duration_hours
        self.allow_multiple_votes = allow_multiple_votes
        # Discord's Select.values is empty until the user interacts with the
        # dropdown — even when options have default=True. Capture the defaults
        # so a no-interaction click on Confirm uses what the admin saw selected.
        self._default_values = [o.value for o in options if o.default]
        self.select = ui.Select(
            placeholder=t("event.select_sources_placeholder", lang),
            options=options,
            min_values=1,
            max_values=len(options),
        )
        self.add_item(self.select)

    @ui.button(label="Confirm", style=discord.ButtonStyle.success, row=1)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        chosen = list(self.select.values) if self.select.values else list(self._default_values)
        if not chosen:
            await interaction.response.send_message(
                t("event.select_sources_required", self.lang), ephemeral=True)
            return
        settings = db.get_guild_settings(interaction.guild_id) or {}
        await _finalize_event_creation(
            interaction, settings, self.lang,
            allowed_sources=chosen,
            sst=self.sst,
            suggestion_duration_seconds=self.suggestion_duration_seconds,
            voting_duration_hours=self.voting_duration_hours,
            allow_multiple_votes=self.allow_multiple_votes,
            ack_via_followup=True,
        )


@bot.tree.command(name="open_suggestions", description="Manually open the suggestion phase")
@app_commands.describe(
    duration="Optional — e.g. '60' (mins), '2h', '1d'. Empty = manual close.",
)
async def cmd_open_suggestions(interaction: discord.Interaction, duration: str = None):
    settings = await check_guild_configured(interaction)
    if not settings:
        return
    if not await check_organizer(interaction, settings):
        return

    lang = settings.get("language", "en")

    seconds = None
    if duration:
        seconds = parse_duration_to_seconds(duration)
        if seconds is None:
            await interaction.response.send_message(
                t("phase.invalid_duration", lang, value=duration), ephemeral=True)
            return

    db_id = await _resolve_channel_event(interaction, lang)
    if db_id is None:
        return

    lock = _get_guild_lock(interaction.guild_id)
    end_time = None
    async with lock:
        record = db.get_event_by_db_id(interaction.guild_id, db_id)
        if not record:
            await interaction.response.send_message(t("event.no_event", lang), ephemeral=True)
            return

        event = record["event"]
        if event.get("phase") not in ("created",):
            await interaction.response.send_message(t("phase.already_open", lang), ephemeral=True)
            return

        event["phase"] = "suggestions_open"
        end_time = (datetime.now() + timedelta(seconds=seconds)) if seconds else None
        event["suggestion_end_time"] = end_time
        event["suggestion_duration_seconds"] = seconds
        db.save_event(record["db_id"], event)

    if end_time:
        ts = int(end_time.timestamp())
        ack_text = t("phase.suggestions_opened_until", lang, ts=ts)
    else:
        ack_text = t("phase.suggestions_opened", lang)

    await interaction.response.send_message(f"✅ {ack_text}", ephemeral=True)
    await _update_event_embed(db_id)
    await send_to_log_channel(ack_text, guild_id=interaction.guild_id)


@bot.tree.command(name="close_suggestions", description="Close the suggestion phase")
async def cmd_close_suggestions(interaction: discord.Interaction):
    settings = await check_guild_configured(interaction)
    if not settings:
        return
    if not await check_organizer(interaction, settings):
        return

    lang = settings.get("language", "en")

    db_id = await _resolve_channel_event(interaction, lang)
    if db_id is None:
        return

    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        await interaction.response.send_message(t("event.no_event", lang), ephemeral=True)
        return

    event = record["event"]
    if event.get("phase") != "suggestions_open":
        await interaction.response.send_message(t("phase.not_open", lang), ephemeral=True)
        return

    view = ConfirmActionView(lang, _do_close_suggestions, db_id=db_id)
    await interaction.response.send_message(
        embed=discord.Embed(description=t("confirm.close_suggestions", lang), color=discord.Color.orange()),
        view=view,
        ephemeral=True,
    )


@bot.tree.command(name="start_vote", description="Start voting with selected layers")
@app_commands.describe(duration_hours="Vote length: bare number = hours, or '24h' / '2d' / '1w'. Max '2w' (14 days).")
async def cmd_start_vote(interaction: discord.Interaction, duration_hours: str = None):
    settings = await check_guild_configured(interaction)
    if not settings:
        return
    if not await check_organizer(interaction, settings):
        return

    lang = settings.get("language", "en")

    db_id = await _resolve_channel_event(interaction, lang)
    if db_id is None:
        return

    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        await interaction.response.send_message(t("event.no_event", lang), ephemeral=True)
        return

    event = record["event"]
    if event.get("phase") != "suggestions_closed":
        await interaction.response.send_message(
            "Close suggestions first, then use the Admin panel to select layers.", ephemeral=True)
        return

    selected = event.get("selected_for_vote", [])
    if not selected:
        await interaction.response.send_message(t("vote.no_layers_selected", lang), ephemeral=True)
        return

    if duration_hours:
        parsed_hours = parse_voting_duration_to_hours(duration_hours)
        if parsed_hours is None:
            await interaction.response.send_message(
                t("phase.invalid_duration", lang, value=duration_hours), ephemeral=True)
            return
        lock = _get_guild_lock(interaction.guild_id)
        async with lock:
            record = db.get_event_by_db_id(interaction.guild_id, db_id)
            if record:
                event = record["event"]
                event["voting_duration_hours"] = parsed_hours
                db.save_event(record["db_id"], event)

    captured_ids = list(selected)

    async def _do_start(confirm_interaction: discord.Interaction, _db_id: int):
        lock = _get_guild_lock(confirm_interaction.guild_id)
        async with lock:
            rec = db.get_event_by_db_id(confirm_interaction.guild_id, _db_id)
            if not rec:
                return
            ev = rec["event"]
            ev["phase"] = "voting"
            db.save_event(rec["db_id"], ev)
        await _start_poll(confirm_interaction, _db_id, captured_ids)

    view = ConfirmActionView(lang, _do_start, db_id=db_id)
    await interaction.response.send_message(
        embed=discord.Embed(description=t("confirm.start_vote", lang), color=discord.Color.orange()),
        view=view,
        ephemeral=True,
    )


@bot.tree.command(name="end_vote", description="End voting early and determine the winner")
async def cmd_end_vote(interaction: discord.Interaction):
    settings = await check_guild_configured(interaction)
    if not settings:
        return
    if not await check_organizer(interaction, settings):
        return

    lang = settings.get("language", "en")

    db_id = await _resolve_channel_event(interaction, lang)
    if db_id is None:
        return

    lock = _get_guild_lock(interaction.guild_id)
    async with lock:
        record = db.get_event_by_db_id(interaction.guild_id, db_id)
        if not record:
            await interaction.response.send_message(t("event.no_event", lang), ephemeral=True)
            return

        event = record["event"]
        if event.get("phase") != "voting":
            await interaction.response.send_message(t("vote.not_in_voting_phase", lang), ephemeral=True)
            return

        winner = await _resolve_poll_winner(interaction.channel, event)
        event["phase"] = "completed"
        event["winning_layer"] = winner
        db.save_event(record["db_id"], event)

        if winner:
            db.save_voting_history(
                interaction.guild_id,
                interaction.channel_id,
                event.get("suggestions", []),
                winner,
            )

    if winner:
        desc = f"✅ {t('vote.ended', lang)}\n{t('vote.winner', lang, layer=format_layer_short(winner))}"
    else:
        desc = f"✅ {t('vote.ended', lang)}\n{t('vote.no_winner', lang)}"

    await interaction.response.send_message(desc, ephemeral=True)
    await _update_event_embed(db_id)
    await send_to_log_channel(
        f"Voting ended. Winner: {format_layer_short(winner) if winner else 'None'}",
        guild_id=interaction.guild_id,
    )


@bot.tree.command(name="delete_event", description="Delete the current event in this channel")
async def cmd_delete_event(interaction: discord.Interaction):
    settings = await check_guild_configured(interaction)
    if not settings:
        return
    if not await check_organizer(interaction, settings):
        return

    lang = settings.get("language", "en")

    db_id = await _resolve_channel_event(interaction, lang)
    if db_id is None:
        return

    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        await interaction.response.send_message(t("event.no_event", lang), ephemeral=True)
        return

    view = ConfirmActionView(lang, _do_delete_event, db_id=db_id)
    await interaction.response.send_message(
        embed=discord.Embed(description=t("confirm.delete_event", lang), color=discord.Color.orange()),
        view=view,
        ephemeral=True,
    )


@bot.tree.command(name="select_for_vote", description="Select layers for voting")
async def cmd_select_for_vote(interaction: discord.Interaction):
    settings = await check_guild_configured(interaction)
    if not settings:
        return
    if not await check_organizer(interaction, settings):
        return

    lang = settings.get("language", "en")

    db_id = await _resolve_channel_event(interaction, lang)
    if db_id is None:
        return

    record = db.get_event_by_db_id(interaction.guild_id, db_id)
    if not record:
        await interaction.response.send_message(t("event.no_event", lang), ephemeral=True)
        return

    event = record["event"]
    if event.get("phase") != "suggestions_closed":
        await interaction.response.send_message(
            "The suggestion phase must be closed first.", ephemeral=True)
        return

    suggestions = event.get("suggestions", [])
    if not suggestions:
        await interaction.response.send_message(t("vote.no_suggestions", lang), ephemeral=True)
        return

    max_voting = min(event.get("max_voting_layers", 10), 10)
    options = [
        discord.SelectOption(
            label=format_layer_poll_option(s),
            value=s["id"],
        )
        for s in suggestions[:25]
    ]

    view = VoteSelectionView(options, max_voting, lang, record["db_id"])
    embed = discord.Embed(
        title=t("admin.select_for_vote", lang),
        description=t("vote.select_layers", lang, max=max_voting),
        color=discord.Color.blue(),
    )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
# SLASH COMMANDS — User
# ═══════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="history", description="View past winning layers")
@app_commands.describe(count="Number of past events to show (default 5)")
async def cmd_history(interaction: discord.Interaction, count: int = 5):
    settings = await check_guild_configured(interaction)
    if not settings:
        return

    lang = settings.get("language", "en")
    history = db.get_recent_history(interaction.guild_id, interaction.channel_id,
                                    limit=min(count, 25))

    if not history:
        await interaction.response.send_message(t("history.empty", lang), ephemeral=True)
        return

    embed = discord.Embed(title=t("history.title", lang), color=discord.Color.gold())

    for entry in history:
        winner = entry.get("winning_layer")
        if not winner:
            continue
        embed.add_field(
            name=format_layer_short(winner),
            value=entry.get("completed_at", "?"),
            inline=False,
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
# SLASH COMMANDS — History editing
# ═══════════════════════════════════════════════════════════════════════════

async def _handle_history_add_submit(interaction: discord.Interaction,
                                     state: SuggestState, lang: str):
    """Save the picked layer as a standalone voting_history entry."""
    settings = db.get_guild_settings(state.guild_id)
    lang = settings.get("language", "en") if settings else lang

    layer = {
        "id": str(uuid.uuid4())[:8],
        "user_id": str(interaction.user.id),
        "user_name": interaction.user.display_name,
        "map_name": state.map_name,
        "gamemode": state.gamemode,
        "layer_version": state.layer_version,
        "team1_faction": state.team1_faction,
        "team1_faction_name": _resolve_faction_name(state.layer_data, state.team1_faction, 1),
        "team1_unit": state.team1_unit,
        "team2_faction": state.team2_faction,
        "team2_faction_name": _resolve_faction_name(state.layer_data, state.team2_faction, 2),
        "team2_unit": state.team2_unit,
        "team1_unit_prefix": _resolve_unit_prefix(state.layer_data, state.team1_faction, 1),
        "team2_unit_prefix": _resolve_unit_prefix(state.layer_data, state.team2_faction, 2),
        "raw_name": state.mode_raw_name,
        "source": state.source,
        "suggested_at": datetime.now().isoformat(),
    }

    db.save_voting_history(state.guild_id, state.channel_id, [layer], layer)

    await interaction.response.edit_message(
        embed=discord.Embed(
            description=f"✅ {t('history.added', lang)}\n{format_layer_short(layer)}",
            color=discord.Color.green(),
        ),
        view=None,
    )
    await send_to_log_channel(
        f"History entry added by {interaction.user.display_name}: {format_layer_short(layer)}",
        guild_id=state.guild_id,
    )


@bot.tree.command(name="history_add",
                  description="Manually add a previously played layer to the history")
async def cmd_history_add(interaction: discord.Interaction):
    settings = await check_guild_configured(interaction)
    if not settings:
        return
    if not await check_organizer(interaction, settings):
        return

    lang = settings.get("language", "en")

    if db.get_layer_cache_count() == 0:
        await interaction.response.send_message(t("cache.empty", lang), ephemeral=True)
        return

    state = SuggestState(interaction.guild_id, interaction.channel_id, flow="history_add")
    _suggest_sessions[interaction.user.id] = state

    blacklisted_maps = settings.get("blacklisted_maps", [])
    maps = db.get_unique_maps(excluded_maps=blacklisted_maps)
    if not maps:
        await interaction.response.send_message(
            t("general.error", lang, error="No maps available"), ephemeral=True)
        return

    sizes = db.get_map_sizes()
    view = _build_map_picker_view(maps, lang, sizes)
    embed = discord.Embed(
        title=t("history.add_title", lang),
        description=t("suggest.select_map", lang),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class HistoryRemoveView(ui.View):
    def __init__(self, options: list[discord.SelectOption], lang: str):
        super().__init__(timeout=120)
        self.add_item(HistoryRemoveSelect(options, lang))


class HistoryRemoveSelect(ui.Select):
    def __init__(self, options: list[discord.SelectOption], lang: str):
        super().__init__(
            placeholder=t("history.remove_placeholder", lang),
            options=options, min_values=1, max_values=1,
        )
        self.lang = lang

    async def callback(self, interaction: discord.Interaction):
        try:
            entry_id = int(self.values[0])
        except (TypeError, ValueError):
            await interaction.response.edit_message(
                embed=discord.Embed(description=t("general.error", self.lang, error="bad id"),
                                    color=discord.Color.red()),
                view=None,
            )
            return

        removed = db.delete_voting_history_entry(entry_id)
        if not removed:
            await interaction.response.edit_message(
                embed=discord.Embed(description=t("history.remove_not_found", self.lang),
                                    color=discord.Color.red()),
                view=None,
            )
            return

        await interaction.response.edit_message(
            embed=discord.Embed(
                description=f"✅ {t('history.removed', self.lang)}",
                color=discord.Color.green(),
            ),
            view=None,
        )
        await send_to_log_channel(
            f"History entry {entry_id} removed by {interaction.user.display_name}",
            guild_id=interaction.guild_id,
        )


@bot.tree.command(name="history_remove",
                  description="Remove an entry from the voting history")
async def cmd_history_remove(interaction: discord.Interaction):
    settings = await check_guild_configured(interaction)
    if not settings:
        return
    if not await check_organizer(interaction, settings):
        return

    lang = settings.get("language", "en")
    history = db.get_recent_history(interaction.guild_id, interaction.channel_id, limit=25)
    if not history:
        await interaction.response.send_message(t("history.empty", lang), ephemeral=True)
        return

    options = []
    for entry in history:
        winner = entry.get("winning_layer")
        if not winner:
            continue
        label = format_layer_poll_option(winner)
        date = str(entry.get("completed_at", ""))[:16]
        options.append(discord.SelectOption(
            label=label[:100],
            value=str(entry["id"]),
            description=date[:100] or None,
        ))

    if not options:
        await interaction.response.send_message(t("history.empty", lang), ephemeral=True)
        return

    view = HistoryRemoveView(options, lang)
    await interaction.response.send_message(
        t("history.remove_prompt", lang), view=view, ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
# BACKGROUND TASK — Check events loop
# ═══════════════════════════════════════════════════════════════════════════

async def _handle_suggestion_timeout(db_id: int, guild_id: int, channel_id: int):
    """Fire when a suggestion phase's auto-close timer expires.

    - If suggestions count fits in max_voting_layers: phase -> voting and
      auto-start the poll with every suggestion.
    - Otherwise: phase -> suggestions_closed and ping the organizer role in
      the log channel so they can run manual selection.
    """
    settings = db.get_guild_settings(guild_id) or {}
    lang = settings.get("language", "en")
    organizer_role_id = settings.get("organizer_role_id", 0) or 0

    auto_started_ids: Optional[list[str]] = None
    needs_selection = False
    suggestion_count = 0
    max_voting = 10

    lock = _get_guild_lock(guild_id)
    async with lock:
        rec = db.get_event_by_db_id(guild_id, db_id)
        if not rec:
            return
        event = rec["event"]
        if event.get("phase") != "suggestions_open":
            return

        suggestions = event.get("suggestions", [])
        suggestion_count = len(suggestions)
        max_voting = min(int(event.get("max_voting_layers", 10) or 10), 10)
        # Clear the timer so we don't fire twice.
        event["suggestion_end_time"] = None

        if suggestion_count == 0 or suggestion_count <= max_voting:
            # Auto-start voting with every suggestion (or transition to a
            # no-suggestion completed state if there are none).
            if suggestion_count == 0:
                event["phase"] = "suggestions_closed"
                needs_selection = False
            else:
                selected_ids = [s["id"] for s in suggestions]
                event["selected_for_vote"] = selected_ids
                event["phase"] = "voting"
                auto_started_ids = selected_ids
        else:
            event["phase"] = "suggestions_closed"
            needs_selection = True

        db.save_event(rec["db_id"], event)

    if auto_started_ids:
        ok = await _auto_start_poll(db_id, auto_started_ids)
        if ok:
            await send_to_log_channel(
                t("phase.auto_vote_started", lang, count=len(auto_started_ids)),
                guild_id=guild_id,
            )
            return
        # Poll creation failed — fall through to manual-selection path so
        # the organizer can still act.
        lock2 = _get_guild_lock(guild_id)
        async with lock2:
            rec = db.get_event_by_db_id(guild_id, db_id)
            if rec and rec["event"].get("phase") == "voting":
                rec["event"]["phase"] = "suggestions_closed"
                rec["event"]["selected_for_vote"] = []
                db.save_event(rec["db_id"], rec["event"])
        needs_selection = True

    await _update_event_embed(db_id)

    if needs_selection:
        mention = f"<@&{organizer_role_id}>" if organizer_role_id else ""
        msg = t(
            "phase.selection_needed", lang,
            mention=mention,
            channel_id=channel_id,
            count=suggestion_count,
            max=max_voting,
        )
        await send_to_log_channel(
            msg,
            guild_id=guild_id,
            level="WARNING",
            mention_role_id=organizer_role_id,
        )
    elif suggestion_count == 0:
        await send_to_log_channel(
            f"Suggestion phase auto-closed with 0 suggestions in <#{channel_id}>",
            guild_id=guild_id,
            level="WARNING",
        )


async def check_events_loop():
    """Background loop that checks for scheduled events."""
    await bot.wait_until_ready()
    logger.info("Background event check loop started.")

    while not bot.is_closed():
        sleep_time = EVENT_CHECK_INTERVAL

        try:
            events = db.get_all_active_events_global()
            now = datetime.now()

            for record in events:
                event = record["event"]
                guild_id = record["guild_id"]
                channel_id = record["channel_id"]
                db_id = record["db_id"]
                phase = event.get("phase", "created")

                # Auto-open suggestions
                if phase == "created":
                    sst = event.get("suggestion_start_time")
                    if sst and isinstance(sst, datetime):
                        seconds_until = (sst - now).total_seconds()
                        if seconds_until <= 0:
                            lock = _get_guild_lock(guild_id)
                            async with lock:
                                rec = db.get_event_by_db_id(guild_id, db_id)
                                if rec and rec["event"].get("phase") == "created":
                                    rec["event"]["phase"] = "suggestions_open"
                                    # Propagate the optional auto-close window
                                    # configured at event-creation time.
                                    dur = rec["event"].get("suggestion_duration_seconds")
                                    if dur:
                                        rec["event"]["suggestion_end_time"] = (
                                            now + timedelta(seconds=int(dur))
                                        )
                                    db.save_event(rec["db_id"], rec["event"])
                            await _update_event_embed(db_id)
                            await send_to_log_channel(
                                f"Suggestion phase auto-opened in <#{channel_id}>",
                                guild_id=guild_id,
                            )
                        elif seconds_until < EVENT_CRITICAL_WINDOW:
                            sleep_time = EVENT_CHECK_INTERVAL_FAST

                # Auto-close suggestions when their timer expires
                if phase == "suggestions_open":
                    set_end = event.get("suggestion_end_time")
                    if set_end and isinstance(set_end, datetime):
                        seconds_until = (set_end - now).total_seconds()
                        if seconds_until <= 0:
                            await _handle_suggestion_timeout(db_id, guild_id, channel_id)
                        elif seconds_until < EVENT_CRITICAL_WINDOW:
                            sleep_time = EVENT_CHECK_INTERVAL_FAST

                # Check if poll has ended (voting phase)
                if phase == "voting":
                    poll_msg_id = event.get("poll_message_id")
                    if poll_msg_id:
                        try:
                            guild = bot.get_guild(guild_id)
                            if guild:
                                channel = guild.get_channel(channel_id)
                                if channel:
                                    message = await channel.fetch_message(poll_msg_id)
                                    if message.poll and message.poll.is_finalised():
                                        lock = _get_guild_lock(guild_id)
                                        async with lock:
                                            rec = db.get_event_by_db_id(guild_id, db_id)
                                            if rec and rec["event"].get("phase") == "voting":
                                                winner = await _resolve_poll_winner(channel, rec["event"])
                                                rec["event"]["phase"] = "completed"
                                                rec["event"]["winning_layer"] = winner
                                                db.save_event(rec["db_id"], rec["event"])
                                                if winner:
                                                    db.save_voting_history(
                                                        guild_id, channel_id,
                                                        rec["event"].get("suggestions", []),
                                                        winner,
                                                    )
                                        await _update_event_embed(db_id)
                                        winner_str = format_layer_short(winner) if winner else "None"
                                        await send_to_log_channel(
                                            f"Poll ended in <#{channel_id}>. Winner: {winner_str}",
                                            guild_id=guild_id,
                                        )
                        except discord.NotFound:
                            pass
                        except Exception as e:
                            logger.error(f"Error checking poll {poll_msg_id}: {e}")

        except Exception as e:
            logger.error(f"Error in background loop: {e}")

        await asyncio.sleep(sleep_time)


# ═══════════════════════════════════════════════════════════════════════════
# BOT EVENTS
# ═══════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

    # Initialize log channels from saved settings
    for guild in bot.guilds:
        settings = db.get_guild_settings(guild.id)
        if settings and settings.get("log_channel_id"):
            channel = guild.get_channel(settings["log_channel_id"])
            if channel:
                set_log_channel(guild.id, channel)

    # Auto-fetch layers if cache is empty
    if db.get_layer_cache_count() == 0:
        logger.info("Layer cache is empty, fetching...")
        try:
            count = await fetch_and_cache_layers()
            logger.info(f"Cached {count} layers on startup")
        except Exception as e:
            logger.error(f"Failed to fetch layers on startup: {e}")

    # Notify all configured log channels that the bot is online
    for guild in bot.guilds:
        await send_to_log_channel(f"Layer Vote Bot connected as {bot.user}", guild_id=guild.id)

    # Start background loop (only once, even if on_ready fires again on reconnect)
    if not getattr(bot, "_background_loop_started", False):
        bot._background_loop_started = True
        bot.loop.create_task(check_events_loop())


# ═══════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    db.init_db()
    bot.run(TOKEN)
