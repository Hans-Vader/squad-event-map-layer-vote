#!/usr/bin/env python3
"""
Utility functions for the Layer Vote Bot.

Permission checks, embed builders, layer formatting helpers.
"""

import logging
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode

import discord
from discord import Embed

from i18n import t
from config import ADMIN_IDS, LAYERS_JSON_SOURCES, SQUADCALC_BASE_URL

logger = logging.getLogger("layer_vote")

# Layer source whose factionIds and map names map cleanly to SquadCalc params.
# Layers from any other source still get a clickable map icon, but the URL
# points at SquadCalc's homepage (since the params would 404) and the
# layer-specific info is surfaced via a hover tooltip on the link instead.
SQUADCALC_COMPATIBLE_SOURCE = "main"

# ---------------------------------------------------------------------------
# Log channel — set per guild at runtime
# ---------------------------------------------------------------------------

_log_channels: dict[int, discord.TextChannel] = {}


def set_log_channel(guild_id: int, channel: discord.TextChannel):
    _log_channels[guild_id] = channel


def get_log_channel(guild_id: int) -> Optional[discord.TextChannel]:
    return _log_channels.get(guild_id)


async def send_to_log_channel(message: str, guild: discord.Guild = None,
                              guild_id: int = None, level: str = "INFO",
                              mention_role_id: int = 0):
    """Send a formatted message to the guild's log channel.

    When `mention_role_id` is given and non-zero, the role is pinged via
    `AllowedMentions(roles=True)` — used when organizers need to act (e.g.
    too many suggestions to auto-start voting).
    """
    gid = guild_id or (guild.id if guild else None)
    if not gid:
        return False

    getattr(logger, level.lower(), logger.info)(message)

    channel = _log_channels.get(gid)
    if not channel:
        return False

    icons = {"INFO": "ℹ️", "WARNING": "⚠️", "ERROR": "❌", "CRITICAL": "🚨"}
    labels = {"INFO": "INFO", "WARNING": "WARNING", "ERROR": "ERROR", "CRITICAL": "CRITICAL"}
    icon = icons.get(level, "ℹ️")
    label = labels.get(level, "INFO")
    formatted = f"{icon} **{label}**: {message}"

    try:
        if mention_role_id:
            await channel.send(
                formatted,
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
        else:
            await channel.send(formatted)
        return True
    except Exception as e:
        logger.error(f"Failed to send to log channel: {e}")
        return False


# ---------------------------------------------------------------------------
# Role / permission checks
# ---------------------------------------------------------------------------

def has_organizer_role(user, organizer_role_id: int) -> bool:
    """Check if user has the guild's organizer role or is a bot-level admin."""
    if hasattr(user, "id") and str(user.id) in ADMIN_IDS:
        return True
    if not hasattr(user, "roles"):
        return False
    if organizer_role_id == 0:
        return False
    return any(role.id == organizer_role_id for role in user.roles)


def is_guild_admin(user) -> bool:
    """Check if user has Discord administrator permission or is bot-level admin."""
    if hasattr(user, "id") and str(user.id) in ADMIN_IDS:
        return True
    if hasattr(user, "guild_permissions"):
        return user.guild_permissions.administrator
    return False


def check_role_gate(event: dict, user) -> bool:
    """Check if a user is allowed to participate in a gated event.

    The event's `allowed_role_ids` and `allowed_user_ids` form the allow-list.
    Both empty = no gate (anyone allowed). Bot-level admins always pass so
    operators can test restricted events without being on the allow-list.
    Organizers do NOT auto-bypass: the gate is about participation, not
    moderation, and an organizer running the event isn't automatically on
    the team that's supposed to vote.
    """
    if hasattr(user, "id") and str(user.id) in ADMIN_IDS:
        return True
    role_ids = event.get("allowed_role_ids") or []
    user_ids = event.get("allowed_user_ids") or []
    if not role_ids and not user_ids:
        return True
    if hasattr(user, "id") and str(user.id) in [str(uid) for uid in user_ids]:
        return True
    if hasattr(user, "roles"):
        user_role_ids = {r.id for r in user.roles}
        if any(rid in user_role_ids for rid in role_ids):
            return True
    return False


# ---------------------------------------------------------------------------
# Layer formatting
# ---------------------------------------------------------------------------

def format_layer_short(suggestion: dict) -> str:
    """Format a layer suggestion as a short one-line string.

    Example: "Al Basrah AAS v1 — USMC/CombinedArms vs RGF/Mechanized"
    """
    map_name = suggestion.get("map_name", "?")
    gamemode = suggestion.get("gamemode", "?")
    version = suggestion.get("layer_version", "")
    t1_faction = suggestion.get("team1_faction", "?")
    t1_unit = suggestion.get("team1_unit", "?")
    t2_faction = suggestion.get("team2_faction", "?")
    t2_unit = suggestion.get("team2_unit", "?")

    mode_str = f"{gamemode} {version}".strip() if version else gamemode
    return f"{map_name} {mode_str} — {t1_faction}/{t1_unit} vs {t2_faction}/{t2_unit}"


def build_squadcalc_url(suggestion: dict) -> Optional[str]:
    """Build a SquadCalc URL for the given suggestion, or None if disabled.

    Only returns a parameterized URL for layers from the SquadCalc-compatible
    source ("main"); SPM/SU layers don't round-trip into SquadCalc params, so
    callers should handle non-main sources via build_map_icon_markdown.
    """
    if not SQUADCALC_BASE_URL:
        return None

    source = suggestion.get("source") or ""
    if source and source != SQUADCALC_COMPATIBLE_SOURCE:
        return None

    map_name = suggestion.get("map_name", "")
    if not map_name:
        return None

    sc_map = map_name.replace(" ", "").replace("'", "")

    gamemode = suggestion.get("gamemode", "")
    version = suggestion.get("layer_version", "")
    sc_layer = f"{gamemode}{version}" if version else gamemode

    params = {"map": sc_map, "layer": sc_layer}

    t1 = suggestion.get("team1_faction")
    t2 = suggestion.get("team2_faction")
    if t1:
        params["team1"] = t1
    if t2:
        params["team2"] = t2

    t1u = suggestion.get("team1_unit")
    t2u = suggestion.get("team2_unit")
    # Prefix (LO, LD, MO, S, …) is layer + team dependent. Stored on the
    # suggestion at submit time; fall back to LO for legacy rows.
    t1_prefix = suggestion.get("team1_unit_prefix") or "LO"
    t2_prefix = suggestion.get("team2_unit_prefix") or "LO"
    if t1u and t1u != "Default":
        params["team1unit"] = f"{t1}_{t1_prefix}_{t1u}"
    if t2u and t2u != "Default":
        params["team2unit"] = f"{t2}_{t2_prefix}_{t2u}"

    return f"{SQUADCALC_BASE_URL}/?{urlencode(params)}"


def _build_layer_tooltip(suggestion: dict) -> str:
    """One-line tooltip text: map + mode + full faction names."""
    map_name = suggestion.get("map_name", "?")
    gamemode = suggestion.get("gamemode", "?")
    version = suggestion.get("layer_version", "")
    mode_str = f"{gamemode} {version}".strip() if version else gamemode

    t1_name = suggestion.get("team1_faction_name") or suggestion.get("team1_faction") or "?"
    t2_name = suggestion.get("team2_faction_name") or suggestion.get("team2_faction") or "?"

    text = f"{map_name} {mode_str} — {t1_name} vs {t2_name}"
    # Markdown link titles are quoted with `"`; replace any embedded quotes so
    # the link doesn't break (e.g. SU_IRGC's name contains "Saberin Unit").
    return text.replace('"', "'")


# No-op masked-link target used to attach a hover tooltip to the 🗺 icon for
# non-main-source layers — Discord requires a URL on masked links, but we
# don't want to send users to SquadCalc since it doesn't recognize SPM/SU
# maps or factions. discord.com keeps the click inside the user's Discord.
_TOOLTIP_NOOP_URL = SQUADCALC_BASE_URL


def build_map_icon_markdown(suggestion: dict) -> str:
    """Render the 🗺️ map icon for embeds.

    Both main and SPM/SU layers go through the same link template; only the
    URL target differs (SquadCalc when usable, a no-op Discord URL otherwise).
    The hover tooltip — map + version + full faction names — is identical
    across sources. Falls back to a plain emoji when no URL is available
    (e.g. SquadCalc disabled and main source).
    """
    url = build_squadcalc_url(suggestion) or _fallback_icon_url(suggestion)
    if not url:
        return "🗺️"
    return f'[🗺️]({url} "{_build_layer_tooltip(suggestion)}")'


def _fallback_icon_url(suggestion: dict) -> Optional[str]:
    """No-op masked-link target for non-main sources, or None if main."""
    source = suggestion.get("source") or ""
    if source and source != SQUADCALC_COMPATIBLE_SOURCE:
        return _TOOLTIP_NOOP_URL
    return None


def format_suggestion_entry(index: int, suggestion: dict) -> str:
    """Format a suggestion as a single-line embed entry.

    Example: 🗺️ **1. Al Basrah** — AAS v1 ⚔️ USMC/CombinedArms vs RGF/Mechanized • UserName
    """
    gamemode = suggestion.get("gamemode", "?")
    gm_short = _GAMEMODE_ABBREV.get(gamemode, gamemode)

    map_name = _MAP_NAME_ABBREV.get(suggestion.get("map_name", "?"), suggestion.get("map_name", "?"))
    t1_faction = suggestion.get("team1_faction", "?")
    t1_unit = _UNIT_ABBREV.get(suggestion.get("team1_unit", "?"), suggestion.get("team1_unit", "?"))
    t2_faction = suggestion.get("team2_faction", "?")
    t2_unit = _UNIT_ABBREV.get(suggestion.get("team2_unit", "?"), suggestion.get("team2_unit", "?"))

    version = suggestion.get("layer_version", "")
    user_name = suggestion.get("user_name", "?")

    mode_str = f"{gm_short} {version}".strip() if version else gm_short

    map_icon = build_map_icon_markdown(suggestion)

    return (
        f"{map_icon} **{index}. {map_name}**: {mode_str} "
        f"⚔️ {t1_faction}/{t1_unit} vs {t2_faction}/{t2_unit} • {user_name}"
    )


_GAMEMODE_ABBREV = {
    "TerritoryControl": "TC",
    "Invasion": "INV",
}

_MAP_NAME_ABBREV = {
    "Kamdesh Highlands": "Kamdesh",
    "Pacific Proving Grounds": "Pacific",
}

_UNIT_ABBREV = {
    "LightInfantry": "LightInf",
    "CombinedArms": "CombArms"
}


def format_layer_poll_option(suggestion: dict) -> str:
    """Format a layer for use in a Discord poll option (max 55 chars for poll answers)."""
    map_name = _MAP_NAME_ABBREV.get(suggestion.get("map_name", "?"), suggestion.get("map_name", "?"))
    gamemode = suggestion.get("gamemode", "?")
    version = suggestion.get("layer_version", "")
    t1_faction = suggestion.get("team1_faction", "?")
    t1_unit = _UNIT_ABBREV.get(suggestion.get("team1_unit", "?"), suggestion.get("team1_unit", "?"))
    t2_faction = suggestion.get("team2_faction", "?")
    t2_unit = _UNIT_ABBREV.get(suggestion.get("team2_unit", "?"), suggestion.get("team2_unit", "?"))

    gm_short = _GAMEMODE_ABBREV.get(gamemode, gamemode)
    mode_str = f"{gm_short}{version}".strip() if version else gm_short
    text = f"{map_name} {mode_str} {t1_faction}({t1_unit}) vs {t2_faction}({t2_unit})"
    if len(text) > 55:
        text = text[:52] + "..."
    return text


def suggestion_matches(s1: dict, s2: dict) -> bool:
    """Check if two suggestions represent the exact same layer combination."""
    keys = ("map_name", "gamemode", "layer_version",
            "team1_faction", "team1_unit", "team2_faction", "team2_unit")
    return all(s1.get(k) == s2.get(k) for k in keys)


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------

_SUPERMOD_SOURCE = "supermod"


def _event_uses_supermod(event: dict, settings: dict) -> bool:
    """Whether the supermod layer source is among this event's active sources.

    Mirrors the precedence used by bot._resolve_event_sources without needing
    a database lookup: explicit event sources first, then the guild cap, then
    the full configured source list as a legacy fallback.
    """
    explicit = event.get("allowed_sources") or []
    guild_allowed = settings.get("allowed_sources") or []

    if explicit:
        candidate = list(explicit)
    elif guild_allowed:
        candidate = list(guild_allowed)
    else:
        candidate = [name for name, _ in LAYERS_JSON_SOURCES]

    if explicit and guild_allowed:
        candidate = [s for s in candidate if s in guild_allowed]

    return _SUPERMOD_SOURCE in candidate


def _embed_total_chars(embed: Embed) -> int:
    """Return total character count of an embed (Discord limit: 6000)."""
    total = len(embed.title or "") + len(embed.description or "")
    total += len(embed.footer.text) if embed.footer else 0
    total += len(embed.author.name) if embed.author else 0
    for field in embed.fields:
        total += len(field.name or "") + len(field.value or "")
    return total


def build_event_embed(event: dict, settings: dict,
                      vote_counts: Optional[dict] = None) -> Embed:
    """Build the main event embed displayed in the channel.

    During the voting phase, callers can pass `vote_counts` (mapping
    suggestion id → live vote_count from the poll) to render per-layer
    counts inline. Suggestions not in `selected_for_vote` aren't part of
    the poll and stay countless.
    """
    phase = event.get("phase", "created")
    lang = settings.get("language", "en")

    title_keys = {
        "created": "embed.title_created",
        "suggestions_open": "embed.title_suggestion",
        "suggestions_closed": "embed.title_suggestion",
        "voting": "embed.title_voting",
        "completed": "embed.title_completed",
    }
    title = t(title_keys.get(phase, "embed.title_created"), lang)

    color_map = {
        "created": discord.Color.greyple(),
        "suggestions_open": discord.Color.green(),
        "suggestions_closed": discord.Color.orange(),
        "voting": discord.Color.blue(),
        "completed": discord.Color.gold(),
    }
    color = color_map.get(phase, discord.Color.greyple())

    embed = Embed(title=title, color=color)

    # Status field
    if phase == "created":
        sst = event.get("suggestion_start_time")
        if sst and isinstance(sst, datetime):
            ts = int(sst.timestamp())
            status_text = t("embed.status_created", lang, ts=ts)
        else:
            status_text = t("embed.status_created_manual", lang)
    elif phase == "suggestions_open":
        end_time = event.get("suggestion_end_time")
        if end_time and isinstance(end_time, datetime):
            ts = int(end_time.timestamp())
            status_text = t("embed.status_suggestions_open_until", lang, ts=ts)
        else:
            status_text = t("embed.status_suggestions_open", lang)
    elif phase == "suggestions_closed":
        count = len(event.get("suggestions", []))
        status_text = t("embed.status_suggestions_closed", lang, count=count)
    elif phase == "voting":
        status_text = t("embed.status_voting", lang)
    elif phase == "completed":
        status_text = t("embed.status_completed", lang)
    else:
        status_text = phase

    embed.add_field(name=t("embed.status", lang), value=status_text, inline=False)

    # Suggestions
    suggestions = event.get("suggestions", [])

    if phase in ("suggestions_open", "suggestions_closed", "voting"):
        header = f"📋 {t('embed.suggestions_header', lang)} ({len(suggestions)})"
        if suggestions:
            selected_ids = set(event.get("selected_for_vote") or [])
            show_counts = phase == "voting" and vote_counts is not None

            def _entry_with_count(idx: int, sug: dict) -> str:
                base = format_suggestion_entry(idx, sug)
                if show_counts and sug.get("id") in selected_ids:
                    count = vote_counts.get(sug.get("id"), 0)
                    base += f" · 🗳️ **{count}**"
                return base

            entries = [_entry_with_count(i, s) for i, s in enumerate(suggestions, 1)]

            # Split entries across multiple fields (each ≤1024 chars)
            fields: list[str] = []
            current_chunk: list[str] = []
            current_len = 0
            for entry in entries:
                line_len = len(entry) + (1 if current_chunk else 0)  # +1 for \n
                if current_chunk and current_len + line_len > 1024:
                    fields.append("\n".join(current_chunk))
                    current_chunk = [entry]
                    current_len = len(entry)
                else:
                    current_chunk.append(entry)
                    current_len += line_len
            if current_chunk:
                fields.append("\n".join(current_chunk))

            # Add fields — first gets the header, continuations use zero-width space
            for idx, field_value in enumerate(fields):
                name = header if idx == 0 else "\u200b"
                embed.add_field(name=name, value=field_value, inline=False)

            # Trim entries only if total embed exceeds 6000 chars
            while _embed_total_chars(embed) > 6000 and len(entries) > 1:
                entries.pop()
                remaining = len(suggestions) - len(entries)

                # Rebuild fields from trimmed entries
                fields = []
                current_chunk = []
                current_len = 0
                for entry in entries:
                    line_len = len(entry) + (1 if current_chunk else 0)
                    if current_chunk and current_len + line_len > 1024:
                        fields.append("\n".join(current_chunk))
                        current_chunk = [entry]
                        current_len = len(entry)
                    else:
                        current_chunk.append(entry)
                        current_len += line_len
                if current_chunk:
                    last = "\n".join(current_chunk)
                    last += f"\n... and {remaining} more"
                    fields.append(last)

                # Replace suggestion fields in embed
                embed.clear_fields()
                embed.add_field(name=t("embed.status", lang), value=status_text, inline=False)
                for idx, field_value in enumerate(fields):
                    name = header if idx == 0 else "\u200b"
                    embed.add_field(name=name, value=field_value, inline=False)
        else:
            embed.add_field(
                name=f"📋 {t('embed.suggestions_header', lang)}",
                value=t("embed.no_suggestions", lang),
                inline=False,
            )

    # Winner (completed phase)
    if phase == "completed":
        winner = event.get("winning_layer")
        if winner:
            map_name = winner.get("map_name", "?")
            gamemode = winner.get("gamemode", "?")
            version = winner.get("layer_version", "")
            t1_id = winner.get("team1_faction", "?")
            t2_id = winner.get("team2_faction", "?")
            # Prefer the human-readable factionName captured at submit time;
            # fall back to the factionId for legacy history rows.
            t1 = winner.get("team1_faction_name") or t1_id
            t2 = winner.get("team2_faction_name") or t2_id
            t1u = winner.get("team1_unit", "?")
            t2u = winner.get("team2_unit", "?")

            mode_str = f"{gamemode} {version}".strip() if version else gamemode

            map_icon = build_map_icon_markdown(winner)

            winner_text = (
                f"{map_icon} **{map_name}** — {mode_str}\n"
                f"⚔️ {t1}/{t1u} vs {t2}/{t2u}"
            )

            embed.add_field(
                name=f"🏆 {t('embed.winner_header', lang)}",
                value=winner_text,
                inline=False,
            )

    # Footer: when the supermod source is active, the legend takes the slot
    # so users can decode SPM/SU and GoingDark prefixes (the SquadCalc hint
    # is suppressed since SquadCalc has no supermod map data anyway).
    # Otherwise we fall back to the SquadCalc hint, which only makes sense
    # when clickable map icons are visible (during/after suggestions, or on
    # a completed winner).
    if _event_uses_supermod(event, settings):
        embed.set_footer(text=t("embed.footer_legend_supermod", lang))
    else:
        show_squadcalc_hint = (
            phase in ("suggestions_open", "suggestions_closed", "voting")
            and event.get("suggestions")
        ) or (phase == "completed" and event.get("winning_layer"))
        if show_squadcalc_hint:
            embed.set_footer(text=t("embed.footer", lang))
    return embed


def build_settings_embed(settings: dict, guild: discord.Guild, layer_count: int) -> Embed:
    """Build an embed showing all current guild settings."""
    lang = settings.get("language", "en")
    embed = Embed(title=t("settings.title", lang), color=discord.Color.blurple())

    orga_role_id = settings.get("organizer_role_id", 0)
    orga_role = guild.get_role(orga_role_id)
    embed.add_field(
        name=t("settings.organizer_role", lang),
        value=orga_role.mention if orga_role else f"ID: {orga_role_id}",
        inline=True,
    )

    log_ch_id = settings.get("log_channel_id")
    embed.add_field(
        name=t("settings.log_channel", lang),
        value=f"<#{log_ch_id}>" if log_ch_id else "—",
        inline=True,
    )

    embed.add_field(name=t("settings.language", lang), value=lang.upper(), inline=True)

    embed.add_field(
        name=t("settings.allowed_gamemodes", lang),
        value=", ".join(settings.get("allowed_gamemodes", [])) or "—",
        inline=False,
    )

    embed.add_field(
        name=t("settings.blacklisted_maps", lang),
        value=", ".join(settings.get("blacklisted_maps", [])) or "—",
        inline=False,
    )

    bl_factions = settings.get("blacklisted_factions", [])
    if bl_factions:
        embed.add_field(
            name=t("settings.blacklisted_factions", lang),
            value=", ".join(bl_factions),
            inline=False,
        )

    bl_units = settings.get("blacklisted_units", [])
    if bl_units:
        embed.add_field(
            name=t("settings.blacklisted_units", lang),
            value=", ".join(bl_units),
            inline=False,
        )

    embed.add_field(
        name=t("settings.max_suggestions", lang),
        value=str(settings.get("max_suggestions_per_user", 2)),
        inline=True,
    )
    embed.add_field(
        name=t("settings.history_lookback", lang),
        value=str(settings.get("history_lookback_events", 3)),
        inline=True,
    )
    embed.add_field(
        name=t("settings.layer_cache", lang),
        value=str(layer_count),
        inline=True,
    )

    defaults_value = (
        f"`suggestion_start`: {settings.get('default_suggestion_start') or '—'}\n"
        f"`suggestion_duration`: {settings.get('default_suggestion_duration') or '—'}\n"
        f"`voting_duration_hours`: {settings.get('default_voting_duration_hours', 24)}\n"
        f"`allow_multiple_votes`: {settings.get('default_allow_multiple_votes', False)}"
    )
    embed.add_field(
        name=t("settings.create_suggestion_defaults", lang),
        value=defaults_value,
        inline=False,
    )

    return embed
