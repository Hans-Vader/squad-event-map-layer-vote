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
    check_role_gate,
    format_layer_short, format_layer_poll_option, suggestion_matches,
    build_event_embed,
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

_DEFAULT_DURATION_MIN_SECONDS = 60
_DEFAULT_DURATION_MAX_SECONDS = 30 * 86400  # 30 days

# Standard placeholder/example string used wherever a duration is entered.
# Keep in sync with parse_duration_to_seconds — bare number = minutes,
# suffixes m/h/d/w supported.
DURATION_HINT = "60m, 2h, 1d, 1w"


def parse_duration_to_seconds(value: str,
                              min_seconds: int = _DEFAULT_DURATION_MIN_SECONDS,
                              max_seconds: int = _DEFAULT_DURATION_MAX_SECONDS,
                              ) -> Optional[int]:
    """Parse a duration string into seconds. Bare numbers are minutes.

    Suffixes: m (minutes), h (hours), d (days), w (weeks). Result clamped
    to [min_seconds, max_seconds]. Returns None for empty/unparseable
    input or non-positive values.

    Single source of truth for duration input across the bot — every
    slash command, modal field, and edit-dialog validator routes here so
    that "60" / "2h" / "1d" all mean the same thing wherever they appear.
    """
    if value is None:
        return None
    v = str(value).strip().lower()
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
    return max(min_seconds, min(seconds, max_seconds))


# Max voting duration: two weeks, in hours.
MAX_VOTING_DURATION_HOURS = 2 * 7 * 24  # 336h
_VOTING_MIN_SECONDS = 3600  # 1h — voting field stores hours, smaller values would round to 0
_VOTING_MAX_SECONDS = MAX_VOTING_DURATION_HOURS * 3600


def parse_voting_duration_input(value: str) -> Optional[int]:
    """Parse a voting-duration input into hours, using the unified duration parser.

    Bare numbers are minutes (matching every other duration field in the bot).
    Returns hours (the storage unit for `voting_duration_hours`), clamped to
    [1, MAX_VOTING_DURATION_HOURS]. Inputs below 1h round up to 1h.
    """
    seconds = parse_duration_to_seconds(value, min_seconds=_VOTING_MIN_SECONDS,
                                        max_seconds=_VOTING_MAX_SECONDS)
    if seconds is None:
        return None
    # round to nearest hour, min 1
    return max(1, round(seconds / 3600))

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
                phase = record["event"].get("phase", "created")
                view = _view_for_phase(record["db_id"], phase, lang)
                if view is None:
                    continue
                self.add_view(view, message_id=msg_id)
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


# Loadout-prefix tokens that appear between the faction marker and the unit
# type in a defaultUnit string (e.g. "ADF_LO_CombinedArms"). Discovered by
# scanning both layers.json and spm_layers.json — same set in both, the SPM
# variant just adds extra suffixes (-Boats, _SuperMod, …) after the type.
_UNIT_PREFIX_MARKERS = ("LO", "LD", "MO", "MD", "S", "Seed")

# Canonical unit-type names as the user expects to see them. The SPM source
# decorates the type with extra qualifiers ("CombinedArms-Boats_SuperMod",
# "CombinedArms_2BB_Boats", …) — we collapse those back to the canonical name
# so the dropdown stays clean and dedup against the `types` array works.
_KNOWN_UNIT_TYPES = (
    "CombinedArms", "AirAssault", "Mechanized", "Armored",
    "Motorized", "LightInfantry", "Support", "SpecialForces",
    "AntiTank", "AmphibiousAssault",
)


def extract_unit_prefix(default_unit: str, faction_id: Optional[str] = None) -> Optional[str]:
    """Extract the loadout-prefix token (LO, LD, MO, MD, S, Seed) from a defaultUnit.

    Scans for the marker directly so it doesn't matter whether factionId is
    a prefix of defaultUnit — handles the SPM cases where factionId is
    "SU_ADF" but defaultUnit is "ADF_LO_..." or even "PLAGF_2010_LO_..." with
    an era qualifier between the faction marker and the loadout token.

    `faction_id` is accepted for backward compatibility but is no longer used.
    """
    if not default_unit:
        return None
    for part in default_unit.split("_"):
        if part in _UNIT_PREFIX_MARKERS:
            return part
    return None


def _extract_default_unit_type(default_unit: str, faction_id: Optional[str] = None) -> Optional[str]:
    """Extract the canonical unit-type name (e.g. ``CombinedArms``) from a defaultUnit.

    Generic across both layers.json and spm_layers.json:

      ``ADF_LO_CombinedArms``                -> ``CombinedArms``
      ``ADF_LO_CombinedArms-Boats_SuperMod`` -> ``CombinedArms``  (SPM suffix)
      ``PLAGF_2010_LO_CombinedArms-Boats``   -> ``CombinedArms``  (era qualifier)
      ``UKSF_LO_SpecialForces_Boats``        -> ``SpecialForces``
      ``ADF_S_CombinedArms_Seed``            -> ``CombinedArms``
      ``FSTemplate_IMF``                     -> None  (no marker)

    Strategy: locate the loadout-prefix marker, then match the remainder
    against the canonical type list. `faction_id` is accepted for backward
    compatibility but not used — the SPM source has factionIds like
    ``SU_ADF`` that are not prefixes of ``ADF_LO_...``, which broke the old
    string-prefix approach.
    """
    if not default_unit:
        return None
    parts = default_unit.split("_")
    for i, part in enumerate(parts):
        if part not in _UNIT_PREFIX_MARKERS or i + 1 >= len(parts):
            continue
        tail = "_".join(parts[i + 1:])
        for known in _KNOWN_UNIT_TYPES:
            if tail == known:
                return known
            # Boundary check: the char after the canonical name must be a
            # separator so we don't match "Combined" inside a longer token.
            if tail.startswith(known) and not tail[len(known)].isalpha():
                return known
        # No canonical match — return the first sub-token (split on _ or -).
        return re.split(r"[-_]", tail, maxsplit=1)[0] or None
    return None


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


class VotingPhaseView(ui.View):
    """View attached to a gated event's embed during the voting phase.

    Suggest Layer is gone (suggestion phase ended) and Info is gone
    (suggestions are visible as poll options inside the thread). Replaced
    by Join Voting, which runs the role gate and adds the user to the
    private voting thread on success — also covers the late-joiner case
    where someone gets the allowed role *after* the thread was created.
    """

    def __init__(self, db_id: int, lang: str = "en"):
        super().__init__(timeout=None)
        self.db_id = db_id

        join = ui.Button(
            label=t("button.join_vote", lang),
            style=discord.ButtonStyle.success,
            custom_id=f"event_action:join_vote:{db_id}",
            emoji="🗳️",
        )
        join.callback = self._join
        self.add_item(join)

        admin = ui.Button(
            label=t("button.admin", lang),
            style=discord.ButtonStyle.danger,
            custom_id=f"event_action:admin:{db_id}",
            emoji="⚙️",
        )
        admin.callback = self._admin
        self.add_item(admin)

    async def _join(self, interaction: discord.Interaction):
        await handle_join_vote(interaction, self.db_id)

    async def _admin(self, interaction: discord.Interaction):
        await handle_admin_panel(interaction, self.db_id)


class CompletedPhaseView(ui.View):
    """View attached to a completed event's embed.

    Only the Admin button — vote ended, the user-facing actions are all
    behind us. The button still routes to the standard admin panel so the
    organizer can edit metadata or delete the event from the embed
    without touching slash commands.
    """

    def __init__(self, db_id: int, lang: str = "en"):
        super().__init__(timeout=None)
        self.db_id = db_id

        admin = ui.Button(
            label=t("button.admin", lang),
            style=discord.ButtonStyle.danger,
            custom_id=f"event_action:admin:{db_id}",
            emoji="⚙️",
        )
        admin.callback = self._admin
        self.add_item(admin)

    async def _admin(self, interaction: discord.Interaction):
        await handle_admin_panel(interaction, self.db_id)


def _view_for_phase(db_id: int, phase: str, lang: str) -> Optional[ui.View]:
    """Return the persistent View for an event in the given phase."""
    if phase == "completed":
        return CompletedPhaseView(db_id, lang)
    if phase == "voting":
        return VotingPhaseView(db_id, lang)
    return EventActionView(db_id, lang)


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

    # Per-event role/user gate. Same allow-list controls both suggesting
    # and voting — empty list = open.
    if not check_role_gate(event, interaction.user):
        await interaction.response.send_message(t("gate.denied", lang), ephemeral=True)
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
            # Open with whatever duration the wizard stored on the event.
            # To override, edit the event's suggestion_duration_seconds via
            # the Admin → Edit DM dialog before clicking Open.
            await admin_open_suggestions(interaction, db_id)
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
    """Open the suggestion phase for a specific event.

    `auto_close_seconds` is an explicit override. When None, fall back to
    the value the wizard stored at event-creation time — so the Admin →
    Open Suggestions button respects what the admin already chose, instead
    of resetting it.
    """
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

        if auto_close_seconds is None:
            auto_close_seconds = event.get("suggestion_duration_seconds")

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


# ═══════════════════════════════════════════════════════════════════════════
# VOTING THREAD — private thread for role-gated events
#
# Discord native polls can't be filtered per-user — anyone who can see the
# message can vote. For events with a non-empty allow-list, we sidestep that
# by posting the poll inside a *private thread* and letting Discord enforce
# membership at the platform level.
#
# Order on /start_vote (gated event):
#   1. Create private thread off the event's channel
#   2. Send a welcome message that mentions the allowed role(s) — Discord
#      auto-invites members of mentioned roles to private threads (much
#      faster than iterating add_user across a large role)
#   3. Add explicit users from allowed_user_ids
#   4. Post the poll inside the thread
#   5. Lock the thread (members can still vote — voting is an interaction,
#      not a message send — but they can't chat, only vote)
#
# Late joiners (someone gets the role after the thread was created) click
# the "Join Voting" button on the public event embed; the role gate runs
# again and they get added.
# ═══════════════════════════════════════════════════════════════════════════

async def _resolve_thread(guild: discord.Guild, thread_id: int) -> Optional[discord.Thread]:
    """Best-effort lookup of a thread by id, falling back to fetch_channel
    so archived private threads are still reachable."""
    if not thread_id:
        return None
    thread = guild.get_thread(thread_id)
    if thread is not None:
        return thread
    try:
        fetched = await bot.fetch_channel(thread_id)
        if isinstance(fetched, discord.Thread):
            return fetched
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None
    return None


async def _resolve_poll_target(channel: discord.abc.Messageable, event: dict) -> discord.abc.Messageable:
    """Return the messageable that holds this event's poll message.

    Gated events post their poll into a private thread (event.vote_thread_id);
    open events post directly in the parent channel. Falls back to `channel`
    if the thread is unreachable so callers don't have to special-case errors.
    """
    thread_id = event.get("vote_thread_id")
    if not thread_id:
        return channel
    guild = getattr(channel, "guild", None)
    if guild is None:
        return channel
    thread = await _resolve_thread(guild, thread_id)
    return thread or channel


_MONTH_NAMES_BY_LANG: dict[str, list[str]] = {
    "en": ["", "January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"],
    "de": ["", "Januar", "Februar", "März", "April", "Mai", "Juni",
           "Juli", "August", "September", "Oktober", "November", "Dezember"],
}


def _current_month_year_label(lang: str) -> str:
    """Localized "<MonthName> <Year>" for the current date.

    Used in voting-thread names. Avoids strftime("%B") because that
    depends on the host system's LC_TIME, which we don't control.
    """
    now = datetime.now()
    months = _MONTH_NAMES_BY_LANG.get(lang) or _MONTH_NAMES_BY_LANG["en"]
    return f"{months[now.month]} {now.year}"


async def _create_voting_thread(channel: discord.TextChannel, event: dict,
                                 lang: str) -> Optional[discord.Thread]:
    """Create the private voting thread and pre-populate its members.

    Returns None when the event has no allow-list (caller falls back to
    posting the poll directly in `channel`). Errors during the optional
    role-mention auto-invite are non-fatal — eligible users can always
    use the Join Voting button to opt in afterwards.
    """
    role_ids = event.get("allowed_role_ids") or []
    user_ids = event.get("allowed_user_ids") or []
    if not role_ids and not user_ids:
        return None

    try:
        thread = await channel.create_thread(
            name=t("thread.voting_name", lang, period=_current_month_year_label(lang)),
            type=discord.ChannelType.private_thread,
            invitable=False,  # only the bot/mods can add others
            auto_archive_duration=10080,  # 7 days
        )
    except discord.HTTPException as e:
        logger.error(f"Failed to create voting thread in #{channel.id}: {e}")
        return None

    # Welcome message + role pings → Discord auto-adds role members to the
    # private thread. allowed_mentions is set so the pings actually fire.
    parts = [t("thread.voting_welcome", lang)]
    if role_ids:
        parts.append(" ".join(f"<@&{rid}>" for rid in role_ids))
    if user_ids:
        parts.append(" ".join(f"<@{uid}>" for uid in user_ids))
    try:
        await thread.send(
            "\n".join(parts),
            allowed_mentions=discord.AllowedMentions(roles=True, users=True),
        )
    except discord.HTTPException as e:
        logger.warning(f"Failed to send welcome ping in voting thread {thread.id}: {e}")

    # Belt-and-suspenders: explicit add_user for the user-id allow-list.
    # (Role mention covers role members; explicit users may not be in any
    # mentioned role.)
    for uid in user_ids:
        try:
            user = await bot.fetch_user(int(uid))
            await thread.add_user(user)
        except (ValueError, discord.HTTPException) as e:
            logger.warning(f"Could not add user {uid} to voting thread: {e}")

    return thread


async def handle_join_vote(interaction: discord.Interaction, db_id: int):
    """Join Voting button — gate-check the user and add them to the thread.

    Also handles the late-joiner case: someone who gets the allowed role
    after the thread was created can opt in here without an organizer
    having to add them manually.
    """
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
    if event.get("phase") != "voting":
        await interaction.response.send_message(t("vote.not_in_voting_phase", lang), ephemeral=True)
        return

    if not check_role_gate(event, interaction.user):
        await interaction.response.send_message(t("gate.denied", lang), ephemeral=True)
        return

    thread_id = event.get("vote_thread_id")
    if not thread_id:
        # Open event — poll lives directly in the parent channel; nothing to join.
        await interaction.response.send_message(t("gate.no_thread", lang), ephemeral=True)
        return

    thread = await _resolve_thread(interaction.guild, thread_id)
    if thread is None:
        await interaction.response.send_message(t("gate.thread_missing", lang), ephemeral=True)
        return

    try:
        await thread.add_user(interaction.user)
    except discord.HTTPException as e:
        logger.warning(f"Failed to add {interaction.user.id} to voting thread {thread_id}: {e}")

    await interaction.response.send_message(
        t("gate.joined", lang, thread=thread.mention), ephemeral=True)


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

    # Gated events post the poll inside a private thread so Discord enforces
    # the allow-list. Open events keep the legacy in-channel behavior.
    voting_thread = await _create_voting_thread(interaction.channel, event, lang)
    target = voting_thread if voting_thread is not None else interaction.channel
    poll_message = await target.send(poll=poll)

    if voting_thread is not None:
        try:
            await voting_thread.edit(locked=True)
        except discord.HTTPException as e:
            logger.warning(f"Failed to lock voting thread {voting_thread.id}: {e}")

    lock = _get_guild_lock(interaction.guild_id)
    async with lock:
        record = db.get_event_by_db_id(interaction.guild_id, db_id)
        if record:
            event = record["event"]
            event["poll_message_id"] = poll_message.id
            if voting_thread is not None:
                event["vote_thread_id"] = voting_thread.id
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

    voting_thread = await _create_voting_thread(channel, event, lang)
    target = voting_thread if voting_thread is not None else channel

    try:
        poll_message = await target.send(poll=poll)
    except Exception as e:
        logger.error(f"Failed to send auto-poll: {e}")
        return False

    if voting_thread is not None:
        try:
            await voting_thread.edit(locked=True)
        except discord.HTTPException as e:
            logger.warning(f"Failed to lock voting thread {voting_thread.id}: {e}")

    lock = _get_guild_lock(guild_id)
    async with lock:
        rec = db.get_active_event_unsafe(db_id)
        if rec:
            rec["event"]["poll_message_id"] = poll_message.id
            if voting_thread is not None:
                rec["event"]["vote_thread_id"] = voting_thread.id
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

    target = await _resolve_poll_target(channel, event)

    try:
        message = await target.fetch_message(poll_msg_id)
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

        # Clean up the poll only when it's directly in the parent channel.
        # Gated events vote inside a private thread, which we leave intact
        # as a permanent record of the vote — deleting just the poll there
        # would leave an empty thread with only the welcome message.
        poll_msg_id = event.get("poll_message_id")
        thread_id = event.get("vote_thread_id")
        if poll_msg_id and not thread_id:
            try:
                async for msg in interaction.channel.history(
                    after=discord.Object(id=poll_msg_id), limit=15
                ):
                    if msg.type.value == 46:  # MessageType.poll_result
                        await msg.delete()
                        break
            except Exception:
                pass

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
    if kind == "vote_duration":
        if not value:
            return "—"
        # value is stored as hours; reuse the seconds formatter for consistency.
        return _format_duration_seconds(int(value) * 3600)
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
    {"key": "voting_duration_hours",     "label_key": "edit.prop.voting_duration",       "kind": "vote_duration", "target": "event"},
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


def _event_message_url(guild_id: int, db_id: int) -> Optional[str]:
    """Build a Discord deep-link to the event's public message, or None.

    Returns None if the event is gone or its embed message hasn't been
    posted yet.
    """
    record = db.get_event_by_db_id(guild_id, db_id)
    if not record:
        return None
    msg_id = record["event"].get("event_message_id")
    if not msg_id:
        return None
    return f"https://discord.com/channels/{guild_id}/{record['channel_id']}/{msg_id}"


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
        text = t("edit.finished", self.lang)
        url = _event_message_url(self.guild_id, self.db_id)
        if url:
            text = f"{text} [{t('edit.event_link', self.lang)}]({url})"
        try:
            await interaction.channel.send(text)
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
        # Maps and factions blacklists are scoped through the same source →
        # category flow used by the suggest dialog, otherwise a single flat
        # dropdown becomes unusable on sources with many maps/factions.
        if prop["key"] in ("blacklisted_maps", "blacklisted_factions"):
            await _show_scoped_blacklist_source_picker(
                interaction, user_id, db_id, guild_id, lang, prop, event)
            return

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

    else:  # int / duration / vote_duration — all go through a Modal
        view = EditScalarView(user_id, db_id, guild_id, lang, prop)
        _set_active_view(user_id, view)
        if prop["kind"] == "int":
            desc = t("edit.int_prompt", lang,
                     current=_format_property_value(current, "int"),
                     min=prop.get("min", "—"), max=prop.get("max", "—"))
        else:
            desc = t("edit.duration_prompt", lang,
                     current=_format_property_value(current, prop["kind"]))
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
            placeholder = DURATION_HINT
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
        elif self.prop["kind"] == "vote_duration":
            value = parse_voting_duration_input(raw)
            if value is None:
                await interaction.response.send_message(
                    t("phase.invalid_duration", self.lang, value=raw), ephemeral=True)
                return
        else:  # duration
            value = parse_duration_to_seconds(raw)
            if value is None:
                await interaction.response.send_message(
                    t("phase.invalid_duration", self.lang, value=raw), ephemeral=True)
                return

        await _apply_edit(interaction, self.user_id, self.db_id, self.guild_id,
                          self.lang, self.prop, value, via_modal=True)


# ───────────────────────────────────────────────────────────────────────────
# Scoped blacklist editors (blacklisted_maps, blacklisted_factions)
#
# A flat multi-select is unusable when a source has dozens of maps/factions.
# Mirroring the suggest flow, the user first picks a source, then sees a
# bucketed picker (Small/Medium/Large for maps, single list for factions).
# Saves are scope-aware: only entries within the visible bucket(s) are
# replaced, so blacklisted items from other sources/buckets stay intact.
# ───────────────────────────────────────────────────────────────────────────

async def _bounce_to_main(interaction: discord.Interaction, user_id: int,
                          db_id: int, guild_id: int, lang: str,
                          message: str) -> None:
    """Show a brief notice and reattach the main edit view."""
    fallback_view = EditMainView(user_id, db_id, guild_id, lang)
    _set_active_view(user_id, fallback_view)
    await interaction.response.edit_message(
        embed=discord.Embed(description=message, color=discord.Color.orange()),
        view=fallback_view,
    )


async def _show_scoped_blacklist_source_picker(
        interaction: discord.Interaction, user_id: int, db_id: int,
        guild_id: int, lang: str, prop: dict, event: dict) -> None:
    """Step 1 of the scoped blacklist edit: pick a source.

    Skips straight to the editor when only one source is available, matching
    the suggest flow's behaviour.
    """
    settings = db.get_guild_settings(guild_id) or {}
    sources = _resolve_event_sources(event, settings)

    if not sources:
        await _bounce_to_main(interaction, user_id, db_id, guild_id, lang,
                              t("cache.empty", lang))
        return

    if len(sources) == 1:
        await _show_scoped_blacklist_editor(
            interaction, user_id, db_id, guild_id, lang, prop, sources[0])
        return

    view = ScopedBlacklistSourceView(user_id, db_id, guild_id, lang, prop, sources)
    _set_active_view(user_id, view)
    embed = discord.Embed(
        title=t(prop["label_key"], lang),
        description=t("suggest.select_source", lang),
        color=discord.Color.blurple(),
    )
    await interaction.response.edit_message(embed=embed, view=view)


async def _show_scoped_blacklist_editor(
        interaction: discord.Interaction, user_id: int, db_id: int,
        guild_id: int, lang: str, prop: dict, source: str) -> None:
    """Step 2: render the bucketed multi-select picker for the chosen source.

    Builds the per-Select buckets that each save will scope itself to:
    Small/Medium/Large groups for maps (mirroring the suggest flow), a
    single bucket for factions. Either way, the picker is a
    `ScopedBlacklistView`.
    """
    record = db.get_event_by_db_id(guild_id, db_id)
    if not record:
        await _notify_event_gone(interaction, user_id, lang)
        return
    event = record["event"]
    blacklist = _read_event_property(event, prop["key"], prop["target"]) or []
    source_filter = [source] if source else None

    if prop["key"] == "blacklisted_maps":
        maps = db.get_unique_maps(allowed_sources=source_filter)
        if not maps:
            await _bounce_to_main(interaction, user_id, db_id, guild_id, lang,
                                  t("cache.empty", lang))
            return
        sizes = db.get_map_sizes(allowed_sources=source_filter)
        groups = _group_maps_by_size(maps, sizes)
        buckets = [
            (f"{t(_SIZE_BUCKET_KEYS[k], lang)} ({len(items)})", items)
            for k, items in groups.items() if items
        ]
    else:  # blacklisted_factions
        factions = db.get_unique_factions(allowed_sources=source_filter)
        if not factions:
            await _bounce_to_main(interaction, user_id, db_id, guild_id, lang,
                                  t("cache.empty", lang))
            return
        buckets = [(t("edit.list_placeholder", lang), factions)]

    view = ScopedBlacklistView(user_id, db_id, guild_id, lang, prop, source,
                               buckets, blacklist)
    _set_active_view(user_id, view)
    desc = (f"**{t('suggest.source_label', lang)}:** {source}\n"
            f"{t('edit.list_prompt', lang)}") if source else t("edit.list_prompt", lang)
    embed = discord.Embed(
        title=t(prop["label_key"], lang),
        description=desc,
        color=discord.Color.blurple(),
    )
    await interaction.response.edit_message(embed=embed, view=view)


class ScopedBlacklistSourceView(ui.View):
    """Source picker for blacklisted_maps / blacklisted_factions edits."""

    def __init__(self, user_id: int, db_id: int, guild_id: int, lang: str,
                 prop: dict, sources: list[str]):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.db_id = db_id
        self.guild_id = guild_id
        self.lang = lang
        self.prop = prop

        options = [discord.SelectOption(label=s[:100], value=s) for s in sources[:25]]
        select = ui.Select(
            placeholder=t("suggest.select_source", lang),
            options=options, min_values=1, max_values=1,
        )
        select.callback = self._on_select
        self.add_item(select)

        cancel = ui.Button(
            label=t("general.cancel", lang),
            style=discord.ButtonStyle.secondary, emoji="↩️",
        )
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    async def _on_select(self, interaction: discord.Interaction):
        source = interaction.data["values"][0]
        await _show_scoped_blacklist_editor(
            interaction, self.user_id, self.db_id, self.guild_id, self.lang,
            self.prop, source)

    async def _on_cancel(self, interaction: discord.Interaction):
        await _refresh_main_view(interaction, self.user_id, self.db_id,
                                 self.guild_id, self.lang)

    async def on_timeout(self):
        await _handle_edit_timeout(self, self.user_id)


class ScopedBlacklistView(ui.View):
    """Scoped multi-select editor for blacklisted_maps / blacklisted_factions.

    `buckets` is a list of `(placeholder, items)` tuples — one Select per
    non-empty bucket. Maps pass 1-3 size-grouped buckets; factions pass a
    single bucket. Saves are scope-aware: only items in the touched Select's
    bucket are replaced in the global blacklist, leaving entries from other
    buckets/sources intact.
    """

    def __init__(self, user_id: int, db_id: int, guild_id: int, lang: str,
                 prop: dict, source: str,
                 buckets: list, blacklist: list[str]):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.db_id = db_id
        self.guild_id = guild_id
        self.lang = lang
        self.prop = prop
        self.source = source

        bl_set = set(blacklist or [])
        for placeholder, items in buckets:
            if not items:
                continue
            visible = items[:25]
            if len(items) > 25:
                logger.warning(
                    "scoped blacklist '%s' bucket %r in source '%s' has %d items; truncating to 25.",
                    prop["key"], placeholder, source, len(items))
            options = [
                discord.SelectOption(label=i[:100], value=i, default=(i in bl_set))
                for i in visible
            ]
            select = ui.Select(
                placeholder=placeholder,
                options=options,
                min_values=0, max_values=len(options),
            )
            select.callback = self._make_callback(visible)
            self.add_item(select)

        # Each Select auto-saves on change, so both buttons just navigate
        # back to the main overview. Cancel and Fertig are kept separate so
        # the user has a clear "I'm done" affordance distinct from "back".
        done = ui.Button(
            label=t("edit.done", lang),
            style=discord.ButtonStyle.success, emoji="✅",
        )
        done.callback = self._on_done
        self.add_item(done)

        cancel = ui.Button(
            label=t("general.cancel", lang),
            style=discord.ButtonStyle.secondary, emoji="↩️",
        )
        cancel.callback = self._on_done
        self.add_item(cancel)

    def _make_callback(self, scope_items: list[str]):
        scope = set(scope_items)

        async def cb(interaction: discord.Interaction):
            selected = set(interaction.data.get("values", []))

            def transform(current):
                return sorted((set(current or []) - scope) | selected)

            ok = await _persist_property_value(
                self.guild_id, self.db_id, self.prop, transform)
            if not ok:
                await _notify_event_gone(interaction, self.user_id, self.lang)
                return
            await _show_scoped_blacklist_editor(
                interaction, self.user_id, self.db_id, self.guild_id,
                self.lang, self.prop, self.source)
        return cb

    async def _on_done(self, interaction: discord.Interaction):
        await _refresh_main_view(interaction, self.user_id, self.db_id,
                                 self.guild_id, self.lang)

    async def on_timeout(self):
        await _handle_edit_timeout(self, self.user_id)


async def _persist_property_value(guild_id: int, db_id: int, prop: dict,
                                   value_or_transform) -> bool:
    """Persist a property value under the guild lock, then refresh the embed.

    `value_or_transform` may be a value, or a callable receiving the current
    value and returning the new one — invoked inside the lock so the
    read-modify-write is atomic against concurrent edits. Returns False when
    the event has been deleted under us; the caller is responsible for
    surfacing that to the user.
    """
    lock = _get_guild_lock(guild_id)
    async with lock:
        record = db.get_event_by_db_id(guild_id, db_id)
        if not record:
            return False
        event = record["event"]
        if callable(value_or_transform):
            current = _read_event_property(event, prop["key"], prop["target"])
            value = value_or_transform(current)
        else:
            value = value_or_transform
        _write_event_property(event, prop["key"], prop["target"], value)
        db.save_event(record["db_id"], event)
    await _update_event_embed(db_id)
    return True


async def _notify_event_gone(interaction: discord.Interaction, user_id: int,
                              lang: str, *, via_modal: bool = False) -> None:
    """Close the session and tell the user their event no longer exists."""
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


async def _apply_edit(interaction: discord.Interaction, user_id: int,
                      db_id: int, guild_id: int, lang: str,
                      prop: dict, value, via_modal: bool = False) -> None:
    """Persist an edit, refresh the public event embed, return to main view."""
    if not await _persist_property_value(guild_id, db_id, prop, value):
        await _notify_event_gone(interaction, user_id, lang, via_modal=via_modal)
        return
    label = t(prop["label_key"], lang)
    await _refresh_main_view(interaction, user_id, db_id, guild_id, lang,
                             updated_label=label, via_modal=via_modal)


# ═══════════════════════════════════════════════════════════════════════════
# EVENT EMBED UPDATE
# ═══════════════════════════════════════════════════════════════════════════

# Keyed by event db_id rather than (guild,channel) since multiple events may
# share a channel; debouncing must be per-event.
_display_update_tasks: dict[int, asyncio.Task] = {}

# Live vote-count refresh cadence. Short enough that the embed feels live
# during a 24h vote; long enough to stay well clear of Discord's per-channel
# message-edit rate limits even with multiple concurrent voting events.
LIVE_VOTE_REFRESH_SECONDS = 60

# db_id -> datetime of last live-vote embed refresh; throttles the periodic
# refresh in check_events_loop so we don't burn API calls every loop tick.
_last_vote_embed_refresh: dict[int, datetime] = {}


async def _update_event_embed(db_id: int):
    """Debounced update of a specific event's embed message."""
    task = _display_update_tasks.get(db_id)
    if task and not task.done():
        task.cancel()
    _display_update_tasks[db_id] = asyncio.create_task(_do_update_embed(db_id))


async def _fetch_vote_counts(target: discord.abc.Messageable, event: dict) -> dict:
    """Read live per-suggestion vote counts from the poll message.

    Returns {suggestion_id: vote_count} for layers in the poll. Empty dict
    on any error — callers should treat that as "live counts unavailable"
    and fall back to a count-less embed.
    """
    poll_msg_id = event.get("poll_message_id")
    if not poll_msg_id:
        return {}
    try:
        msg = await target.fetch_message(poll_msg_id)
    except (discord.NotFound, discord.HTTPException):
        return {}
    if not getattr(msg, "poll", None):
        return {}

    text_to_count = {a.text: a.vote_count for a in msg.poll.answers}
    selected_ids = set(event.get("selected_for_vote") or [])
    counts: dict = {}
    for s in event.get("suggestions", []):
        sid = s.get("id")
        if sid not in selected_ids:
            continue
        text = format_layer_poll_option(s)
        if text in text_to_count:
            counts[sid] = text_to_count[text]
    return counts


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

        # Fetch live vote counts during voting so the embed shows running
        # totals next to each polled layer. Failures degrade gracefully to
        # a count-less embed.
        vote_counts = None
        if event.get("phase") == "voting":
            target = await _resolve_poll_target(channel, event)
            vote_counts = await _fetch_vote_counts(target, event)

        embed = build_event_embed(event, settings, vote_counts=vote_counts)
        message = await channel.fetch_message(msg_id)

        lang = settings.get("language", "en")
        phase = event.get("phase", "created")
        view = _view_for_phase(db_id, phase, lang)
        await message.edit(embed=embed, view=view)
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

@bot.tree.command(name="create_layer_suggestion",
                  description="Create a new layer vote event in this channel")
async def cmd_create_event(interaction: discord.Interaction):
    """Open the event-creation wizard (modal → confirm view).

    All previous parameters moved into a wizard for a streamlined UX
    that mirrors the squad-event-discord-bot's create_event flow.
    """
    settings = await check_guild_configured(interaction)
    if not settings:
        return
    if not await check_organizer(interaction, settings):
        return

    lang = settings.get("language", "en")

    if db.get_layer_cache_count() == 0:
        await interaction.response.send_message(t("cache.empty", lang), ephemeral=True)
        return

    # Resolve which sources will be offered. Same logic as before — the
    # universe of cache sources, intersected with the guild's allowed list.
    offered = _resolve_offered_sources(settings)
    if not offered:
        await interaction.response.send_message(t("cache.empty", lang), ephemeral=True)
        return

    await interaction.response.send_modal(EventScheduleModal(settings, lang, offered))


def _resolve_offered_sources(settings: dict) -> list[str]:
    """Sources to expose to event creators: cache ∩ guild default (or all if no default)."""
    cache_sources = db.get_unique_sources()
    guild_default = settings.get("allowed_sources") or []
    if guild_default:
        return [s for s in cache_sources if s in guild_default]
    return list(cache_sources)


class EventScheduleModal(ui.Modal):
    """Wizard step 1: collect schedule + duration text inputs.

    Modals only support TextInputs, so role/user gate, source picking,
    and the multi-vote toggle live on the follow-up EventCreateConfirmView.
    Suggestion start is strictly absolute (DD.MM.YYYY + HH:MM); the legacy
    "+offset" syntax was removed. If the guild has a default offset, we
    pre-fill the date/time fields with `now + offset` so admins can accept
    or edit the resolved time instead of writing it from scratch.
    """

    def __init__(self, settings: dict, lang: str, offered_sources: list[str]):
        super().__init__(title=t("event.wizard_title", lang), timeout=600)
        self.settings = settings
        self.lang = lang
        self.offered_sources = list(offered_sources)

        # Pre-fill start datetime from guild default offset, if any.
        prefill_start = ""
        default_offset = settings.get("default_suggestion_start") or ""
        if default_offset:
            offset_secs = parse_duration_to_seconds(default_offset)
            if offset_secs is not None:
                target = datetime.now() + timedelta(seconds=offset_secs)
                prefill_start = target.strftime("%d.%m.%Y %H:%M")

        sug_default = settings.get("default_suggestion_duration") or ""
        vote_hours = int(settings.get("default_voting_duration_hours", 24) or 24)
        vote_default = f"{vote_hours}h"

        self.start = ui.TextInput(
            label=t("event.wizard_start_label", lang),
            placeholder="DD.MM.YYYY HH:MM",
            required=False, max_length=16, default=prefill_start,
        )
        self.sug_duration = ui.TextInput(
            label=t("event.wizard_suggestion_duration_label", lang),
            placeholder=DURATION_HINT,
            required=False, max_length=10, default=sug_default,
        )
        self.vote_duration = ui.TextInput(
            label=t("event.wizard_vote_duration_label", lang),
            placeholder=DURATION_HINT,
            required=True, max_length=10, default=vote_default,
        )
        self.add_item(self.start)
        self.add_item(self.sug_duration)
        self.add_item(self.vote_duration)

    async def on_submit(self, interaction: discord.Interaction):
        lang = self.lang

        # Single combined "DD.MM.YYYY HH:MM" field; empty = manual phase.
        start_raw = self.start.value.strip()
        sst: Optional[datetime] = None
        if start_raw:
            try:
                sst = datetime.strptime(start_raw, "%d.%m.%Y %H:%M")
            except ValueError:
                await interaction.response.send_message(
                    t("event.wizard_invalid_date_time", lang, value=start_raw),
                    ephemeral=True)
                return

        suggestion_duration_seconds = None
        if self.sug_duration.value.strip():
            suggestion_duration_seconds = parse_duration_to_seconds(self.sug_duration.value)
            if suggestion_duration_seconds is None:
                await interaction.response.send_message(
                    t("phase.invalid_duration", lang, value=self.sug_duration.value),
                    ephemeral=True)
                return

        voting_duration_hours = parse_voting_duration_input(self.vote_duration.value)
        if voting_duration_hours is None:
            await interaction.response.send_message(
                t("phase.invalid_duration", lang, value=self.vote_duration.value),
                ephemeral=True)
            return

        view = EventCreateConfirmView(
            lang=lang,
            sst=sst,
            suggestion_duration_seconds=suggestion_duration_seconds,
            voting_duration_hours=voting_duration_hours,
            offered_sources=self.offered_sources,
            allow_multiple_votes=bool(self.settings.get("default_allow_multiple_votes", False)),
        )
        embed = discord.Embed(
            title=t("event.wizard_confirm_title", lang),
            description=t("event.wizard_confirm_desc", lang),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class EventCreateConfirmView(ui.View):
    """Wizard step 2: gate selection, source picker, multi-vote toggle, Confirm.

    The schedule fields collected by EventScheduleModal are stashed on
    this view; the admin uses the visual selectors here for the things
    a modal can't host (Role/User pickers, multi-selects, toggles).
    """

    def __init__(self, lang, sst, suggestion_duration_seconds, voting_duration_hours,
                 offered_sources, allow_multiple_votes):
        super().__init__(timeout=300)
        self.lang = lang
        self.sst = sst
        self.suggestion_duration_seconds = suggestion_duration_seconds
        self.voting_duration_hours = voting_duration_hours
        self.offered_sources = list(offered_sources)
        self.allow_multiple_votes = bool(allow_multiple_votes)
        self.selected_role_ids: list[int] = []
        self.selected_user_ids: list[int] = []
        self.selected_sources: list[str] = list(offered_sources)

        # Row 0 — gate (mentionable: roles + members in one picker; optional)
        self.gate_select = ui.MentionableSelect(
            placeholder=t("event.wizard_gate_placeholder", lang),
            min_values=0,
            max_values=10,
            row=0,
        )
        self.gate_select.callback = self._gate_changed
        self.add_item(self.gate_select)

        # Row 1 — source select, only when there's a real choice
        self.source_select: Optional[ui.Select] = None
        if len(offered_sources) > 1:
            options = [
                discord.SelectOption(label=s, value=s, default=True)
                for s in offered_sources[:25]
            ]
            self.source_select = ui.Select(
                placeholder=t("event.select_sources_placeholder", lang),
                options=options,
                min_values=1,
                max_values=len(options),
                row=1,
            )
            self.source_select.callback = self._sources_changed
            self.add_item(self.source_select)

        # Row 2 — multi-vote toggle + Confirm
        self.multi_button = ui.Button(
            label=self._multi_label(),
            style=self._multi_style(),
            row=2,
        )
        self.multi_button.callback = self._multi_toggled
        self.add_item(self.multi_button)

        self.confirm_button = ui.Button(
            label=t("button.confirm_selection", lang),
            style=discord.ButtonStyle.success,
            emoji="✅",
            row=2,
        )
        self.confirm_button.callback = self._confirm
        self.add_item(self.confirm_button)

    def _multi_label(self) -> str:
        key = "event.wizard_multi_on" if self.allow_multiple_votes else "event.wizard_multi_off"
        return t(key, self.lang)

    def _multi_style(self) -> discord.ButtonStyle:
        return discord.ButtonStyle.success if self.allow_multiple_votes else discord.ButtonStyle.secondary

    async def _gate_changed(self, interaction: discord.Interaction):
        roles: list[int] = []
        users: list[int] = []
        for v in self.gate_select.values:
            if isinstance(v, discord.Role):
                roles.append(v.id)
            else:
                users.append(v.id)
        self.selected_role_ids = roles
        self.selected_user_ids = users
        await interaction.response.defer()

    async def _sources_changed(self, interaction: discord.Interaction):
        self.selected_sources = list(self.source_select.values)
        await interaction.response.defer()

    async def _multi_toggled(self, interaction: discord.Interaction):
        self.allow_multiple_votes = not self.allow_multiple_votes
        self.multi_button.label = self._multi_label()
        self.multi_button.style = self._multi_style()
        await interaction.response.edit_message(view=self)

    async def _confirm(self, interaction: discord.Interaction):
        if not self.selected_sources:
            await interaction.response.send_message(
                t("event.select_sources_required", self.lang), ephemeral=True)
            return
        settings = db.get_guild_settings(interaction.guild_id) or {}
        await _finalize_event_creation(
            interaction, settings, self.lang,
            allowed_sources=self.selected_sources,
            sst=self.sst,
            suggestion_duration_seconds=self.suggestion_duration_seconds,
            voting_duration_hours=self.voting_duration_hours,
            allow_multiple_votes=self.allow_multiple_votes,
            allowed_role_ids=self.selected_role_ids,
            allowed_user_ids=self.selected_user_ids,
            ack_via_followup=True,
        )


async def _finalize_event_creation(interaction: discord.Interaction, settings: dict, lang: str,
                                   *, allowed_sources: list[str],
                                   sst, suggestion_duration_seconds,
                                   voting_duration_hours, allow_multiple_votes,
                                   allowed_role_ids: list[int],
                                   allowed_user_ids: list[int],
                                   ack_via_followup: bool):
    """Create the event row and post its embed.

    Sole call site is the EventCreateConfirmView confirm button — the
    wizard now always routes through that view, so there's no separate
    "single-source fast path" anymore (the view just hides the source
    select when there's nothing to pick).
    """
    event_data = db.build_default_event(suggestion_start_time=sst, settings=settings)
    event_data["voting_duration_hours"] = max(1, min(MAX_VOTING_DURATION_HOURS, voting_duration_hours))
    event_data["suggestion_duration_seconds"] = suggestion_duration_seconds
    event_data["allow_multiple_votes"] = bool(allow_multiple_votes)
    event_data["allowed_sources"] = list(allowed_sources)
    event_data["allowed_role_ids"] = list(allowed_role_ids)
    event_data["allowed_user_ids"] = list(allowed_user_ids)

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


@bot.tree.command(
    name="set_event_roles",
    description="Restrict suggesting + voting to specific roles/users for the event in this channel",
)
@app_commands.describe(
    role="Role allowed to participate (suggest + vote). Can be combined with user.",
    user="User allowed to participate (suggest + vote). Can be combined with role.",
)
async def cmd_set_event_roles(interaction: discord.Interaction,
                              role: discord.Role = None,
                              user: discord.Member = None):
    settings = await check_guild_configured(interaction)
    if not settings:
        return
    if not await check_organizer(interaction, settings):
        return

    lang = settings.get("language", "en")

    if role is None and user is None:
        await interaction.response.send_message(t("roles.no_args", lang), ephemeral=True)
        return

    db_id = await _resolve_channel_event(interaction, lang)
    if db_id is None:
        return

    changes: list[str] = []
    lock = _get_guild_lock(interaction.guild_id)
    async with lock:
        record = db.get_event_by_db_id(interaction.guild_id, db_id)
        if not record:
            await interaction.response.send_message(t("event.no_event", lang), ephemeral=True)
            return
        event = record["event"]
        role_ids = list(event.get("allowed_role_ids") or [])
        user_ids = list(event.get("allowed_user_ids") or [])

        if role is not None and role.id not in role_ids:
            role_ids.append(role.id)
            changes.append(f"+ role {role.mention}")
        if user is not None and user.id not in user_ids:
            user_ids.append(user.id)
            changes.append(f"+ user {user.mention}")

        if not changes:
            await interaction.response.send_message(t("roles.no_changes", lang), ephemeral=True)
            return

        event["allowed_role_ids"] = role_ids
        event["allowed_user_ids"] = user_ids
        db.save_event(record["db_id"], event)

    await interaction.response.send_message(
        t("roles.added", lang, changes="\n".join(changes)),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    await send_to_log_channel(
        f"Allow-list updated by {interaction.user.display_name}: {', '.join(changes)}",
        guild_id=interaction.guild_id,
    )


@bot.tree.command(
    name="clear_event_roles",
    description="Clear the role/user allow-list (event becomes open to everyone)",
)
async def cmd_clear_event_roles(interaction: discord.Interaction):
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
        if not (event.get("allowed_role_ids") or event.get("allowed_user_ids")):
            await interaction.response.send_message(t("roles.already_empty", lang), ephemeral=True)
            return
        event["allowed_role_ids"] = []
        event["allowed_user_ids"] = []
        db.save_event(record["db_id"], event)

    await interaction.response.send_message(t("roles.cleared", lang), ephemeral=True)
    await send_to_log_channel(
        f"Event allow-list cleared by {interaction.user.display_name}",
        guild_id=interaction.guild_id,
    )


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

                # Refresh the embed periodically so live vote counts stay
                # current while the poll is still running. Throttled to
                # LIVE_VOTE_REFRESH_SECONDS per event.
                if phase == "voting":
                    last_refresh = _last_vote_embed_refresh.get(db_id)
                    if (last_refresh is None or
                            (now - last_refresh).total_seconds() >= LIVE_VOTE_REFRESH_SECONDS):
                        _last_vote_embed_refresh[db_id] = now
                        await _update_event_embed(db_id)

                # Check if poll has ended (voting phase)
                if phase == "voting":
                    poll_msg_id = event.get("poll_message_id")
                    if poll_msg_id:
                        try:
                            guild = bot.get_guild(guild_id)
                            if guild:
                                channel = guild.get_channel(channel_id)
                                if channel:
                                    target = await _resolve_poll_target(channel, event)
                                    message = await target.fetch_message(poll_msg_id)
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
