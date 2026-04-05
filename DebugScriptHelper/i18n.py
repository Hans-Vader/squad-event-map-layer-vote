#!/usr/bin/env python3
"""
Internationalization module for the Layer Vote Bot.
Supports German (de) and English (en). Default language: English.
"""

from typing import Optional

SUPPORTED_LANGUAGES = {"de", "en"}
DEFAULT_LANGUAGE = "en"

# ---------------------------------------------------------------------------
# Translation strings keyed by dotted path
# ---------------------------------------------------------------------------
_STRINGS: dict[str, dict[str, str]] = {
    # ── General ───────────────────────────────────────────────────────────
    "general.no_active_event": {
        "de": "Es gibt derzeit kein aktives Event in diesem Kanal.",
        "en": "There is no active event in this channel.",
    },
    "general.no_permission": {
        "de": "Du hast keine Berechtigung für diese Aktion.",
        "en": "You do not have permission for this action.",
    },
    "general.requires_organizer": {
        "de": "Nur Mitglieder mit der Organisator-Rolle können diese Aktion ausführen.",
        "en": "Only members with the organizer role can perform this action.",
    },
    "general.requires_admin": {
        "de": "Nur Server-Administratoren können diese Aktion ausführen.",
        "en": "Only server administrators can perform this action.",
    },
    "general.cancelled": {
        "de": "Abgebrochen.",
        "en": "Cancelled.",
    },
    "general.timeout": {
        "de": "Zeitüberschreitung. Bitte starte den Vorgang neu.",
        "en": "Timeout. Please start the process again.",
    },
    "general.error": {
        "de": "Ein Fehler ist aufgetreten: {error}",
        "en": "An error occurred: {error}",
    },
    "general.success": {
        "de": "Erfolgreich!",
        "en": "Success!",
    },
    "general.confirm": {
        "de": "Bestätigen",
        "en": "Confirm",
    },
    "general.cancel": {
        "de": "Abbrechen",
        "en": "Cancel",
    },
    "general.guild_not_configured": {
        "de": "Dieser Server ist noch nicht eingerichtet. Nutze `/setup` zuerst.",
        "en": "This server is not configured yet. Use `/setup` first.",
    },

    # ── Setup ─────────────────────────────────────────────────────────────
    "setup.welcome": {
        "de": "Server-Einrichtung abgeschlossen! Organisator-Rolle: {role}, Log-Kanal: {channel}, Sprache: {language}",
        "en": "Server setup complete! Organizer role: {role}, Log channel: {channel}, Language: {language}",
    },
    "setup.already_configured": {
        "de": "Server ist bereits eingerichtet. Einstellungen wurden aktualisiert.",
        "en": "Server is already configured. Settings have been updated.",
    },
    "setup.organizer_role_updated": {
        "de": "Organisator-Rolle aktualisiert auf: {role}",
        "en": "Organizer role updated to: {role}",
    },
    "setup.language_updated": {
        "de": "Sprache aktualisiert auf: {language}",
        "en": "Language updated to: {language}",
    },
    "setup.log_channel_updated": {
        "de": "Log-Kanal aktualisiert auf: {channel}",
        "en": "Log channel updated to: {channel}",
    },

    # ── Config ────────────────────────────────────────────────────────────
    "config.gamemodes_updated": {
        "de": "Erlaubte Spielmodi aktualisiert: {modes}",
        "en": "Allowed gamemodes updated: {modes}",
    },
    "config.blacklist_updated": {
        "de": "Blacklist aktualisiert ({type}): {items}",
        "en": "Blacklist updated ({type}): {items}",
    },
    "config.suggestions_updated": {
        "de": "Vorschlags-Einstellungen aktualisiert.",
        "en": "Suggestion settings updated.",
    },
    "config.roles_updated": {
        "de": "Rollen-Einstellungen aktualisiert ({type}).",
        "en": "Role settings updated ({type}).",
    },

    # ── Layer cache ───────────────────────────────────────────────────────
    "cache.refreshing": {
        "de": "Layer-Daten werden aktualisiert...",
        "en": "Refreshing layer data...",
    },
    "cache.refreshed": {
        "de": "{count} Layers erfolgreich zwischengespeichert.",
        "en": "Successfully cached {count} layers.",
    },
    "cache.error": {
        "de": "Fehler beim Laden der Layer-Daten: {error}",
        "en": "Error loading layer data: {error}",
    },
    "cache.empty": {
        "de": "Layer-Datenbank ist leer. Nutze `/refresh_layers` zum Laden.",
        "en": "Layer cache is empty. Use `/refresh_layers` to load data.",
    },

    # ── Event management ──────────────────────────────────────────────────
    "event.created": {
        "de": "Layer-Vote Event erstellt!",
        "en": "Layer vote event created!",
    },
    "event.already_exists": {
        "de": "In diesem Kanal gibt es bereits ein aktives Event.",
        "en": "There is already an active event in this channel.",
    },
    "event.deleted": {
        "de": "Event gelöscht.",
        "en": "Event deleted.",
    },
    "event.no_event": {
        "de": "Kein aktives Event in diesem Kanal.",
        "en": "No active event in this channel.",
    },

    # ── Suggestions ───────────────────────────────────────────────────────
    "suggest.phase_title": {
        "de": "Layer-Vorschlag",
        "en": "Layer Suggestion",
    },
    "suggest.select_map": {
        "de": "Wähle eine Map aus.",
        "en": "Select a map.",
    },
    "suggest.select_mode": {
        "de": "Wähle einen Spielmodus.",
        "en": "Select a game mode.",
    },
    "suggest.select_team1_faction": {
        "de": "Wähle die Fraktion für Team 1.",
        "en": "Select the faction for Team 1.",
    },
    "suggest.select_team1_unit": {
        "de": "Wähle den Einheitstyp für Team 1.",
        "en": "Select the unit type for Team 1.",
    },
    "suggest.select_team2_faction": {
        "de": "Wähle die Fraktion für Team 2.",
        "en": "Select the faction for Team 2.",
    },
    "suggest.select_team2_unit": {
        "de": "Wähle den Einheitstyp für Team 2.",
        "en": "Select the unit type for Team 2.",
    },
    "suggest.confirm_title": {
        "de": "Vorschlag bestätigen",
        "en": "Confirm Suggestion",
    },
    "suggest.submitted": {
        "de": "Dein Vorschlag wurde eingereicht!",
        "en": "Your suggestion has been submitted!",
    },
    "suggest.max_reached": {
        "de": "Du hast bereits die maximale Anzahl an Vorschlägen ({max}) erreicht.",
        "en": "You have already reached the maximum number of suggestions ({max}).",
    },
    "suggest.max_total_reached": {
        "de": "Die maximale Anzahl an Vorschlägen ({max}) wurde erreicht.",
        "en": "The maximum number of suggestions ({max}) has been reached.",
    },
    "suggest.not_open": {
        "de": "Die Vorschlagsphase ist derzeit nicht geöffnet.",
        "en": "The suggestion phase is not currently open.",
    },
    "suggest.blocked_history": {
        "de": "Dieser Layer wurde bereits in einem der letzten {count} Events vorgeschlagen.",
        "en": "This layer was already suggested in one of the last {count} events.",
    },
    "suggest.no_role": {
        "de": "Du hast nicht die erforderliche Rolle zum Vorschlagen.",
        "en": "You do not have the required role to suggest layers.",
    },
    "suggest.duplicate": {
        "de": "Dieser Layer wurde bereits in diesem Event vorgeschlagen.",
        "en": "This layer has already been suggested in this event.",
    },

    # ── Suggestion phase ──────────────────────────────────────────────────
    "phase.suggestions_opened": {
        "de": "Vorschlagsphase eröffnet!",
        "en": "Suggestion phase opened!",
    },
    "phase.suggestions_closed": {
        "de": "Vorschlagsphase geschlossen. {count} Vorschläge eingegangen.",
        "en": "Suggestion phase closed. {count} suggestions received.",
    },
    "phase.already_open": {
        "de": "Die Vorschlagsphase ist bereits geöffnet.",
        "en": "The suggestion phase is already open.",
    },
    "phase.not_open": {
        "de": "Die Vorschlagsphase ist nicht geöffnet.",
        "en": "The suggestion phase is not open.",
    },

    # ── Confirmation ─────────────────────────────────────────────────────
    "confirm.close_suggestions": {
        "de": "Möchtest du die Vorschlagsphase wirklich schließen?",
        "en": "Are you sure you want to close the suggestion phase?",
    },
    "confirm.delete_event": {
        "de": "Möchtest du das Event wirklich löschen? Dies kann nicht rückgängig gemacht werden.",
        "en": "Are you sure you want to delete the event? This cannot be undone.",
    },
    "confirm.start_vote": {
        "de": "Möchtest du die Abstimmung wirklich starten?",
        "en": "Are you sure you want to start the vote?",
    },

    # ── Voting ────────────────────────────────────────────────────────────
    "vote.select_layers": {
        "de": "Wähle Layers für die Abstimmung (max {max}):",
        "en": "Select layers for voting (max {max}):",
    },
    "vote.random_button": {
        "de": "Zufällig auswählen",
        "en": "Random selection",
    },
    "vote.no_suggestions": {
        "de": "Es gibt keine Vorschläge für die Abstimmung.",
        "en": "There are no suggestions to vote on.",
    },
    "vote.started": {
        "de": "Abstimmung gestartet! Dauer: {hours} Stunden.",
        "en": "Voting started! Duration: {hours} hours.",
    },
    "vote.ended": {
        "de": "Abstimmung beendet!",
        "en": "Voting ended!",
    },
    "vote.winner": {
        "de": "Gewinner: {layer}",
        "en": "Winner: {layer}",
    },
    "vote.no_winner": {
        "de": "Kein eindeutiger Gewinner.",
        "en": "No clear winner.",
    },
    "vote.poll_question": {
        "de": "Welcher Layer soll gespielt werden?",
        "en": "Which layer should be played?",
    },
    "vote.not_in_voting_phase": {
        "de": "Das Event ist nicht in der Abstimmungsphase.",
        "en": "The event is not in the voting phase.",
    },
    "vote.no_layers_selected": {
        "de": "Es wurden keine Layers für die Abstimmung ausgewählt.",
        "en": "No layers have been selected for voting.",
    },

    # ── Embed ─────────────────────────────────────────────────────────────
    "embed.title_suggestion": {
        "de": "Layer-Vote — Vorschlagsphase",
        "en": "Layer Vote — Suggestion Phase",
    },
    "embed.title_voting": {
        "de": "Layer-Vote — Abstimmung",
        "en": "Layer Vote — Voting",
    },
    "embed.title_completed": {
        "de": "Layer-Vote — Abgeschlossen",
        "en": "Layer Vote — Completed",
    },
    "embed.title_created": {
        "de": "Layer-Vote — Erstellt",
        "en": "Layer Vote — Created",
    },
    "embed.status": {
        "de": "Status",
        "en": "Status",
    },
    "embed.status_created": {
        "de": "Erstellt — Vorschläge öffnen um <t:{ts}:f>" ,
        "en": "Created — Suggestions open at <t:{ts}:f>",
    },
    "embed.status_created_manual": {
        "de": "Erstellt — Wartet auf manuelle Eröffnung",
        "en": "Created — Waiting for manual opening",
    },
    "embed.status_suggestions_open": {
        "de": "Vorschläge offen",
        "en": "Suggestions open",
    },
    "embed.status_suggestions_closed": {
        "de": "Vorschläge geschlossen — {count} eingegangen",
        "en": "Suggestions closed — {count} received",
    },
    "embed.status_voting": {
        "de": "Abstimmung läuft",
        "en": "Voting in progress",
    },
    "embed.status_completed": {
        "de": "Abgeschlossen",
        "en": "Completed",
    },
    "embed.suggestions_header": {
        "de": "Vorschläge",
        "en": "Suggestions",
    },
    "embed.suggestions_count": {
        "de": "{count} Vorschläge eingereicht",
        "en": "{count} suggestions submitted",
    },
    "embed.suggestions_hidden": {
        "de": "{count} Vorschläge eingereicht (nicht sichtbar)",
        "en": "{count} suggestions submitted (hidden)",
    },
    "embed.no_suggestions": {
        "de": "Noch keine Vorschläge.",
        "en": "No suggestions yet.",
    },
    "embed.winner_header": {
        "de": "Gewinner",
        "en": "Winner",
    },
    "embed.footer": {
        "de": "Layer Vote Bot",
        "en": "Layer Vote Bot",
    },

    # ── Buttons ───────────────────────────────────────────────────────────
    "button.suggest": {
        "de": "Layer vorschlagen",
        "en": "Suggest Layer",
    },
    "button.info": {
        "de": "Info",
        "en": "Info",
    },
    "button.admin": {
        "de": "Admin",
        "en": "Admin",
    },
    "button.submit": {
        "de": "Absenden",
        "en": "Submit",
    },
    "button.cancel": {
        "de": "Abbrechen",
        "en": "Cancel",
    },
    "button.random": {
        "de": "Zufällig ({count})",
        "en": "Random ({count})",
    },
    "button.confirm_selection": {
        "de": "Auswahl bestätigen",
        "en": "Confirm Selection",
    },

    # ── Settings display ──────────────────────────────────────────────────
    "settings.title": {
        "de": "Server-Einstellungen",
        "en": "Server Settings",
    },
    "settings.organizer_role": {
        "de": "Organisator-Rolle",
        "en": "Organizer Role",
    },
    "settings.log_channel": {
        "de": "Log-Kanal",
        "en": "Log Channel",
    },
    "settings.language": {
        "de": "Sprache",
        "en": "Language",
    },
    "settings.allowed_gamemodes": {
        "de": "Erlaubte Spielmodi",
        "en": "Allowed Gamemodes",
    },
    "settings.blacklisted_maps": {
        "de": "Gesperrte Maps",
        "en": "Blacklisted Maps",
    },
    "settings.blacklisted_factions": {
        "de": "Gesperrte Fraktionen",
        "en": "Blacklisted Factions",
    },
    "settings.blacklisted_units": {
        "de": "Gesperrte Einheitstypen",
        "en": "Blacklisted Unit Types",
    },
    "settings.max_suggestions": {
        "de": "Max. Vorschläge pro User",
        "en": "Max Suggestions per User",
    },
    "settings.history_lookback": {
        "de": "History-Lookback (Events)",
        "en": "History Lookback (Events)",
    },
    "settings.suggestions_visible": {
        "de": "Vorschläge sichtbar",
        "en": "Suggestions Visible",
    },
    "settings.suggest_roles": {
        "de": "Vorschlags-Rollen",
        "en": "Suggest Roles",
    },
    "settings.vote_roles": {
        "de": "Abstimmungs-Rollen",
        "en": "Vote Roles",
    },
    "settings.layer_cache": {
        "de": "Gecachte Layers",
        "en": "Cached Layers",
    },

    # ── History ───────────────────────────────────────────────────────────
    "history.title": {
        "de": "Vergangene Gewinner",
        "en": "Past Winners",
    },
    "history.empty": {
        "de": "Keine vergangenen Abstimmungen gefunden.",
        "en": "No past votes found.",
    },
    "history.entry": {
        "de": "**{layer}** — {date}",
        "en": "**{layer}** — {date}",
    },

    # ── Admin info ────────────────────────────────────────────────────────
    "admin.suggestions_count": {
        "de": "Vorschläge: {count}",
        "en": "Suggestions: {count}",
    },
    "admin.phase": {
        "de": "Phase: {phase}",
        "en": "Phase: {phase}",
    },
    "admin.open_suggestions": {
        "de": "Vorschläge öffnen",
        "en": "Open Suggestions",
    },
    "admin.close_suggestions": {
        "de": "Vorschläge schließen",
        "en": "Close Suggestions",
    },
    "admin.select_for_vote": {
        "de": "Für Abstimmung auswählen",
        "en": "Select for Vote",
    },
    "admin.start_vote": {
        "de": "Abstimmung starten",
        "en": "Start Vote",
    },
    "admin.end_vote": {
        "de": "Abstimmung beenden",
        "en": "End Vote",
    },
    "admin.delete_event": {
        "de": "Event löschen",
        "en": "Delete Event",
    },
    "admin.random_count_label": {
        "de": "Anzahl zufälliger Layers",
        "en": "Number of random layers",
    },
}


# ---------------------------------------------------------------------------
# Lookup function
# ---------------------------------------------------------------------------

def t(key: str, lang: str = "en", **kwargs) -> str:
    """Look up a translation string by key and language.

    Falls back to English if the key is missing for the requested language.
    Interpolates {placeholder} values from kwargs.
    """
    entry = _STRINGS.get(key)
    if entry is None:
        return f"[{key}]"
    text = entry.get(lang) or entry.get("en") or f"[{key}]"
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError):
            pass
    return text
