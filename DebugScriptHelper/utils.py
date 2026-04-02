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


def check_suggest_permission(user, settings: dict) -> bool:
    """Check if user is allowed to suggest layers based on role/user gates.

    If no gates are configured, anyone can suggest.
    """
    role_ids = settings.get("suggest_role_ids", [])
    user_ids = settings.get("suggest_user_ids", [])

    if not role_ids and not user_ids:
        return True

    if str(user.id) in ADMIN_IDS:
        return True
    if str(user.id) in [str(uid) for uid in user_ids]:
        return True
    if hasattr(user, "roles"):
        user_role_ids = {role.id for role in user.roles}
        if user_role_ids & set(role_ids):
            return True
    return False


def check_vote_permission(user, settings: dict) -> bool:
    """Check if user is allowed to vote based on role/user gates.

    If no gates are configured, anyone can vote.
    """
    role_ids = settings.get("vote_role_ids", [])
    user_ids = settings.get("vote_user_ids", [])

    if not role_ids and not user_ids:
        return True

    if str(user.id) in ADMIN_IDS:
        return True
    if str(user.id) in [str(uid) for uid in user_ids]:
        return True
    if hasattr(user, "roles"):
        user_role_ids = {role.id for role in user.roles}
        if user_role_ids & set(role_ids):
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


def format_layer_poll_option(suggestion: dict) -> str:
    """Format a layer for use in a Discord poll option (max 55 chars for poll answers)."""
    map_name = suggestion.get("map_name", "?")
    gamemode = suggestion.get("gamemode", "?")
    version = suggestion.get("layer_version", "")
    t1_faction = suggestion.get("team1_faction", "?")
    t1_unit = suggestion.get("team1_unit", "?")
    t2_faction = suggestion.get("team2_faction", "?")
    t2_unit = suggestion.get("team2_unit", "?")

    mode_str = f"{gamemode} {version}".strip() if version else gamemode
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

def build_event_embed(event: dict, settings: dict, user_is_admin: bool = False) -> Embed:
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
    visible = settings.get("suggestions_visible", True)

    if phase in ("suggestions_open", "suggestions_closed", "voting"):
        if suggestions:
            if visible or user_is_admin:
                lines = []
                for i, s in enumerate(suggestions, 1):
                    user_name = s.get("user_name", "?")
                    layer_str = format_layer_short(s)
                    lines.append(f"{i}. {layer_str} (by {user_name})")
                value = "\n".join(lines)
                if len(value) > 1024:
                    value = "\n".join(lines[:15]) + f"\n... and {len(lines) - 15} more"
                embed.add_field(
                    name=f"{t('embed.suggestions_header', lang)} ({len(suggestions)})",
                    value=value,
                    inline=False,
                )
            else:
                embed.add_field(
                    name=t("embed.suggestions_header", lang),
                    value=t("embed.suggestions_hidden", lang, count=len(suggestions)),
                    inline=False,
                )
        else:
            embed.add_field(
                name=t("embed.suggestions_header", lang),
                value=t("embed.no_suggestions", lang),
                inline=False,
            )

    # Winner (completed phase)
    if phase == "completed":
        winner = event.get("winning_layer")
        if winner:
            embed.add_field(
                name=t("embed.winner_header", lang),
                value=format_layer_short(winner),
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
        name=t("settings.suggestions_visible", lang),
        value="✅" if settings.get("suggestions_visible", True) else "❌",
        inline=True,
    )

    # Suggest roles
    sr_ids = settings.get("suggest_role_ids", [])
    su_ids = settings.get("suggest_user_ids", [])
    sr_text = ", ".join(f"<@&{r}>" for r in sr_ids) if sr_ids else ""
    su_text = ", ".join(f"<@{u}>" for u in su_ids) if su_ids else ""
    combined = ", ".join(filter(None, [sr_text, su_text])) or "Everyone"
    embed.add_field(name=t("settings.suggest_roles", lang), value=combined, inline=False)

    # Vote roles
    vr_ids = settings.get("vote_role_ids", [])
    vu_ids = settings.get("vote_user_ids", [])
    vr_text = ", ".join(f"<@&{r}>" for r in vr_ids) if vr_ids else ""
    vu_text = ", ".join(f"<@{u}>" for u in vu_ids) if vu_ids else ""
    combined_v = ", ".join(filter(None, [vr_text, vu_text])) or "Everyone"
    embed.add_field(name=t("settings.vote_roles", lang), value=combined_v, inline=False)

    embed.add_field(
        name=t("settings.layer_cache", lang),
        value=str(layer_count),
        inline=True,
    )

    return embed
