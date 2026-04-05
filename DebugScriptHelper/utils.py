#!/usr/bin/env python3
"""
Utility functions for the Layer Vote Bot.

Permission checks, embed builders, layer formatting helpers.
"""

import logging
from datetime import datetime
from typing import Optional

import discord
from discord import Embed

from i18n import t
from config import ADMIN_IDS

logger = logging.getLogger("layer_vote")

# ---------------------------------------------------------------------------
# Log channel — set per guild at runtime
# ---------------------------------------------------------------------------

_log_channels: dict[int, discord.TextChannel] = {}


def set_log_channel(guild_id: int, channel: discord.TextChannel):
    _log_channels[guild_id] = channel


def get_log_channel(guild_id: int) -> Optional[discord.TextChannel]:
    return _log_channels.get(guild_id)


async def send_to_log_channel(message: str, guild: discord.Guild = None,
                              guild_id: int = None, level: str = "INFO"):
    """Send a formatted message to the guild's log channel."""
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


def format_suggestion_entry(index: int, suggestion: dict) -> str:
    """Format a suggestion as a single-line embed entry.

    Example: 🗺️ **1. Al Basrah** — AAS v1 ⚔️ USMC/CombinedArms vs RGF/Mechanized • UserName
    """
    map_name = suggestion.get("map_name", "?")
    gamemode = suggestion.get("gamemode", "?")
    version = suggestion.get("layer_version", "")
    t1_faction = suggestion.get("team1_faction", "?")
    t1_unit = suggestion.get("team1_unit", "?")
    t2_faction = suggestion.get("team2_faction", "?")
    t2_unit = suggestion.get("team2_unit", "?")
    user_name = suggestion.get("user_name", "?")

    mode_str = f"{gamemode} {version}".strip() if version else gamemode
    return (
        f"🗺️ **{index}. {map_name}**: {mode_str} "
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
    mode_str = f"{gm_short} {version}".strip() if version else gm_short
    text = f"{map_name} {mode_str} — {t1_faction} ({t1_unit}) vs {t2_faction} ({t2_unit})"
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

def _embed_total_chars(embed: Embed) -> int:
    """Return total character count of an embed (Discord limit: 6000)."""
    total = len(embed.title or "") + len(embed.description or "")
    total += len(embed.footer.text) if embed.footer else 0
    total += len(embed.author.name) if embed.author else 0
    for field in embed.fields:
        total += len(field.name or "") + len(field.value or "")
    return total


def build_event_embed(event: dict, settings: dict) -> Embed:
    """Build the main event embed displayed in the channel."""
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
            entries = [format_suggestion_entry(i, s) for i, s in enumerate(suggestions, 1)]

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
            t1 = winner.get("team1_faction", "?")
            t1u = winner.get("team1_unit", "?")
            t2 = winner.get("team2_faction", "?")
            t2u = winner.get("team2_unit", "?")

            mode_str = f"{gamemode} {version}".strip() if version else gamemode
            winner_text = (
                f"🗺️ **{map_name}** — {mode_str}\n"
                f"⚔️ {t1}/{t1u} vs {t2}/{t2u}"
            )

            embed.add_field(
                name=f"🏆 {t('embed.winner_header', lang)}",
                value=winner_text,
                inline=False,
            )

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

    return embed
