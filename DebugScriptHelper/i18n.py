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
    "config.create_suggestion_updated": {
        "de": "Standardwerte für `/create_layer_suggestion` aktualisiert:",
        "en": "Defaults for `/create_layer_suggestion` updated:",
    },
    "config.roles_updated": {
        "de": "Rollen-Einstellungen aktualisiert ({type}).",
        "en": "Role settings updated ({type}).",
    },
    "config.sources_prompt": {
        "de": "Wähle die Layer-Quellen aus, die beim Erstellen von Events angeboten werden sollen ({count} verfügbar).",
        "en": "Select which layer sources are offered when creating events ({count} available).",
    },
    "config.sources_placeholder": {
        "de": "Layer-Quellen auswählen",
        "en": "Select layer sources",
    },
    "config.sources_updated": {
        "de": "Erlaubte Layer-Quellen aktualisiert: {sources}",
        "en": "Allowed layer sources updated: {sources}",
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
    "event.multiple_in_channel": {
        "de": "Mehrere aktive Events in diesem Kanal — bitte den Admin-Button auf dem jeweiligen Event-Embed verwenden.",
        "en": "Multiple active events in this channel — please use the Admin button on the specific event embed.",
    },
    "event.select_sources_title": {
        "de": "Layer-Quellen auswählen",
        "en": "Select layer sources",
    },
    "event.select_sources_desc": {
        "de": "Wähle, welche Layer-Quellen die Nutzer für dieses Event vorschlagen dürfen, dann bestätige.",
        "en": "Select which layer sources users may suggest from for this event, then confirm.",
    },
    "event.select_sources_placeholder": {
        "de": "Quellen für dieses Event auswählen",
        "en": "Select sources for this event",
    },
    "event.select_sources_required": {
        "de": "Bitte wähle mindestens eine Layer-Quelle aus.",
        "en": "Please select at least one layer source.",
    },

    # ── Suggestions ───────────────────────────────────────────────────────
    "suggest.phase_title": {
        "de": "Layer-Vorschlag",
        "en": "Layer Suggestion",
    },
    "suggest.select_source": {
        "de": "Wähle eine Layer-Quelle.",
        "en": "Select a layer source.",
    },
    "suggest.source_label": {
        "de": "Quelle",
        "en": "Source",
    },
    "suggest.select_map": {
        "de": "Wähle eine Map aus.",
        "en": "Select a map.",
    },
    "suggest.size_small": {
        "de": "Klein",
        "en": "Small",
    },
    "suggest.size_medium": {
        "de": "Mittel",
        "en": "Medium",
    },
    "suggest.size_large": {
        "de": "Groß",
        "en": "Large",
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
    "phase.invalid_duration": {
        "de": "Ungültige Dauer: `{value}`. Erwartet z.B. `60`, `2h`, `1d`.",
        "en": "Invalid duration: `{value}`. Expected e.g. `60`, `2h`, `1d`.",
    },
    "phase.suggestions_opened_until": {
        "de": "Vorschlagsphase eröffnet bis <t:{ts}:f> (<t:{ts}:R>).",
        "en": "Suggestion phase opened until <t:{ts}:f> (<t:{ts}:R>).",
    },
    "phase.auto_vote_started": {
        "de": "Vorschlagszeit abgelaufen — Abstimmung automatisch mit {count} Layern gestartet.",
        "en": "Suggestion time expired — voting auto-started with {count} layers.",
    },
    "phase.selection_needed": {
        "de": "{mention} Vorschlagszeit abgelaufen in <#{channel_id}> — {count} Vorschläge, manuelle Auswahl erforderlich (max {max}).",
        "en": "{mention} Suggestion time expired in <#{channel_id}> — {count} suggestions, manual selection required (max {max}).",
    },
    "embed.status_suggestions_open_until": {
        "de": "Vorschläge offen bis <t:{ts}:f> (<t:{ts}:R>)",
        "en": "Suggestions open until <t:{ts}:f> (<t:{ts}:R>)",
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
        "de": "Erstellt — Vorschläge öffnen um <t:{ts}:f> (<t:{ts}:R>)",
        "en": "Created — Suggestions open at <t:{ts}:f> (<t:{ts}:R>)",
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
        "de": "Klick auf die Karte --> 🗺 um SquadCalc zu öffnen",
        "en": "Click on the map --> 🗺 to open SquadCalc",
    },
    "embed.footer_legend_supermod": {
        "de": "SPM/SU = SuperMod | GoingDark = SuperMod Nacht",
        "en": "SPM/SU = SuperMod | GoingDark = SuperMod Night",
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
    "button.join_vote": {
        "de": "Zur Abstimmung",
        "en": "Join Voting",
    },

    # ── Role gate (per-event role/user allow-list) ────────────────────────
    "gate.denied": {
        "de": "Du bist nicht berechtigt, an diesem Event teilzunehmen.",
        "en": "You are not eligible to participate in this event.",
    },
    "gate.no_thread": {
        "de": "Dieses Event ist offen — die Abstimmung läuft direkt in diesem Kanal.",
        "en": "This event is open — voting takes place directly in this channel.",
    },
    "gate.thread_missing": {
        "de": "Der Abstimmungs-Thread wurde nicht gefunden.",
        "en": "The voting thread could not be found.",
    },
    "gate.joined": {
        "de": "Du wurdest zum Abstimmungs-Thread hinzugefügt: {thread}",
        "en": "You've been added to the voting thread: {thread}",
    },

    # ── Voting thread (private thread created at /start_vote) ─────────────
    "thread.voting_name": {
        "de": "Abstimmung — {period}",
        "en": "Voting — {period}",
    },
    "thread.voting_welcome": {
        "de": "🗳️ Berechtigte Mitglieder können hier abstimmen.",
        "en": "🗳️ Eligible members can vote here.",
    },

    # ── /set_event_roles & /clear_event_roles ─────────────────────────────
    "roles.no_args": {
        "de": "Gib mindestens eine Rolle oder einen Nutzer an.",
        "en": "Please provide at least one role or user.",
    },
    "roles.added": {
        "de": "Allow-Liste aktualisiert:\n{changes}",
        "en": "Allow-list updated:\n{changes}",
    },
    "roles.no_changes": {
        "de": "Keine Änderungen — die angegebenen Einträge sind bereits in der Allow-Liste.",
        "en": "No changes — the entries you provided are already on the allow-list.",
    },
    "roles.cleared": {
        "de": "Allow-Liste geleert. Das Event ist jetzt offen für alle.",
        "en": "Allow-list cleared. The event is now open to everyone.",
    },
    "roles.already_empty": {
        "de": "Die Allow-Liste ist bereits leer.",
        "en": "The allow-list is already empty.",
    },

    # ── Event creation wizard (Modal + Confirm view) ──────────────────────
    "event.wizard_title": {
        "de": "Neues Layer-Vote-Event",
        "en": "New layer vote event",
    },
    "event.wizard_start_label": {
        "de": "Start (DD.MM.YYYY HH:MM) — leer = manuell",
        "en": "Start (DD.MM.YYYY HH:MM) — empty = manual",
    },
    "event.wizard_suggestion_duration_label": {
        "de": "Vorschlagsphase-Dauer — leer = manuell",
        "en": "Suggestion phase duration — empty = manual",
    },
    "event.wizard_vote_duration_label": {
        "de": "Abstimmungs-Dauer (max. 14 Tage)",
        "en": "Voting duration (max 14 days)",
    },
    "event.wizard_invalid_date_time": {
        "de": "Ungültiges Datum/Uhrzeit: `{value}`. Format: `DD.MM.YYYY HH:MM`.",
        "en": "Invalid date/time: `{value}`. Format: `DD.MM.YYYY HH:MM`.",
    },
    "event.wizard_confirm_title": {
        "de": "Event-Konfiguration bestätigen",
        "en": "Confirm event configuration",
    },
    "event.wizard_confirm_desc": {
        "de": "Wähle Rolle/Nutzer, die teilnehmen dürfen (optional), die Layer-Quellen und ob mehrere Stimmen erlaubt sind. Dann **Bestätigen**.",
        "en": "Pick the role/user allowed to participate (optional), the layer sources, and whether multiple votes are allowed. Then **Confirm**.",
    },
    "event.wizard_gate_placeholder": {
        "de": "Rolle/Nutzer, die teilnehmen dürfen (optional)",
        "en": "Role/user allowed to participate (optional)",
    },
    "event.wizard_multi_on": {
        "de": "Mehrfachstimmen: AN",
        "en": "Multiple votes: ON",
    },
    "event.wizard_multi_off": {
        "de": "Mehrfachstimmen: AUS",
        "en": "Multiple votes: OFF",
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
    "settings.create_suggestion_defaults": {
        "de": "Standardwerte für /create_layer_suggestion",
        "en": "Defaults for /create_layer_suggestion",
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
    "history.add_title": {
        "de": "Layer zur History hinzufügen",
        "en": "Add Layer to History",
    },
    "history.added": {
        "de": "Layer zur History hinzugefügt.",
        "en": "Layer added to history.",
    },
    "history.remove_title": {
        "de": "Layer aus History entfernen",
        "en": "Remove Layer from History",
    },
    "history.remove_prompt": {
        "de": "Wähle einen History-Eintrag zum Entfernen:",
        "en": "Select a history entry to remove:",
    },
    "history.remove_placeholder": {
        "de": "History-Eintrag auswählen",
        "en": "Select history entry",
    },
    "history.removed": {
        "de": "History-Eintrag entfernt.",
        "en": "History entry removed.",
    },
    "history.remove_not_found": {
        "de": "History-Eintrag nicht gefunden.",
        "en": "History entry not found.",
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
    "admin.edit_event": {
        "de": "Event bearbeiten",
        "en": "Edit Event",
    },
    "admin.random_count_label": {
        "de": "Anzahl zufälliger Layers",
        "en": "Number of random layers",
    },
    "admin.remove_suggestion": {
        "de": "Vorschlag entfernen",
        "en": "Remove Suggestion",
    },
    "admin.remove_select": {
        "de": "Zu entfernenden Vorschlag auswählen",
        "en": "Select a suggestion to remove",
    },
    "admin.remove_select_chunk": {
        "de": "Zu entfernenden Vorschlag auswählen ({current}/{total})",
        "en": "Select a suggestion to remove ({current}/{total})",
    },
    "admin.remove_prompt": {
        "de": "Wähle einen der **{count}** Vorschläge zum Entfernen aus.",
        "en": "Pick one of the **{count}** suggestions to remove.",
    },
    "admin.no_suggestions": {
        "de": "Keine Vorschläge vorhanden.",
        "en": "There are no suggestions to remove.",
    },
    "admin.suggestion_removed": {
        "de": "Vorschlag entfernt: {layer}",
        "en": "Suggestion removed: {layer}",
    },
    "admin.remove_not_found": {
        "de": "Vorschlag nicht gefunden (möglicherweise bereits entfernt).",
        "en": "Suggestion not found (it may have already been removed).",
    },

    # ── Event edit DM dialog ─────────────────────────────────────────────
    "edit.title": {
        "de": "Event-Konfiguration bearbeiten",
        "en": "Edit Event Configuration",
    },
    "edit.select_property": {
        "de": "Wähle eine Eigenschaft zum Bearbeiten — Änderungen gelten nur für dieses Event.",
        "en": "Pick a property to edit — changes apply to this event only.",
    },
    "edit.pick_property_placeholder": {
        "de": "Eigenschaft auswählen",
        "en": "Select a property",
    },
    "edit.dm_sent": {
        "de": "Bearbeitungsdialog wurde dir per DM geschickt.",
        "en": "Edit dialog sent to your DMs.",
    },
    "edit.dm_blocked": {
        "de": "Konnte keine DM senden — bitte erlaube DMs von Server-Mitgliedern.",
        "en": "Couldn't open a DM — please allow DMs from server members.",
    },
    "edit.session_active": {
        "de": "Du hast bereits eine offene Bearbeitungssitzung. Schließe diese zuerst.",
        "en": "You already have an active edit session — close it first.",
    },
    "edit.done": {
        "de": "Fertig",
        "en": "Done",
    },
    "edit.finished": {
        "de": "Bearbeitung abgeschlossen.",
        "en": "Edit session closed.",
    },
    "edit.event_link": {
        "de": "Zum Event",
        "en": "Go to event",
    },
    "edit.timeout": {
        "de": "Ich bin mir nicht sicher, wohin du gegangen bist. Wir können es später erneut versuchen.",
        "en": "I'm not sure where you went. We can try again later.",
    },
    "edit.list_prompt": {
        "de": "Wähle die gewünschten Werte aus und bestätige (Auswahl speichert sofort).",
        "en": "Select the desired values — your choice saves immediately.",
    },
    "edit.list_placeholder": {
        "de": "Werte auswählen",
        "en": "Select values",
    },
    "edit.bool_prompt": {
        "de": "Aktueller Wert: {value}. Wähle einen neuen Wert.",
        "en": "Current value: {value}. Pick a new value.",
    },
    "edit.bool_yes": {
        "de": "Ja",
        "en": "Yes",
    },
    "edit.bool_no": {
        "de": "Nein",
        "en": "No",
    },
    "edit.int_prompt": {
        "de": "Aktuell: `{current}` (Bereich {min}–{max}). Klicke ⌨️, um einen neuen Wert einzugeben.",
        "en": "Current: `{current}` (range {min}–{max}). Click ⌨️ to enter a new value.",
    },
    "edit.duration_prompt": {
        "de": "Aktuell: `{current}`. Klicke ⌨️, um einen neuen Wert einzugeben (z.B. `60`, `2h`, `1d`).",
        "en": "Current: `{current}`. Click ⌨️ to enter a new value (e.g. `60`, `2h`, `1d`).",
    },
    "edit.open_input": {
        "de": "Wert eingeben",
        "en": "Enter value",
    },
    "edit.input_label": {
        "de": "Neuer Wert",
        "en": "New value",
    },
    "edit.invalid_int": {
        "de": "Ungültige Zahl: `{value}`.",
        "en": "Invalid number: `{value}`.",
    },
    "edit.out_of_range": {
        "de": "Wert {value} außerhalb des erlaubten Bereichs ({min}–{max}).",
        "en": "Value {value} is outside the allowed range ({min}–{max}).",
    },
    "edit.updated_inline": {
        "de": "**{prop}** aktualisiert.",
        "en": "**{prop}** updated.",
    },

    # Edit dialog property labels
    "edit.prop.allowed_gamemodes": {
        "de": "Erlaubte Spielmodi",
        "en": "Allowed Gamemodes",
    },
    "edit.prop.blacklisted_maps": {
        "de": "Gesperrte Maps",
        "en": "Blacklisted Maps",
    },
    "edit.prop.blacklisted_factions": {
        "de": "Gesperrte Fraktionen",
        "en": "Blacklisted Factions",
    },
    "edit.prop.blacklisted_units": {
        "de": "Gesperrte Einheitstypen",
        "en": "Blacklisted Unit Types",
    },
    "edit.prop.max_per_user": {
        "de": "Max. Vorschläge pro Nutzer",
        "en": "Max Suggestions per User",
    },
    "edit.prop.max_total": {
        "de": "Max. Vorschläge insgesamt",
        "en": "Max Total Suggestions",
    },
    "edit.prop.history_lookback": {
        "de": "History-Lookback (Events)",
        "en": "History Lookback (events)",
    },
    "edit.prop.allowed_sources": {
        "de": "Erlaubte Layer-Quellen",
        "en": "Allowed Layer Sources",
    },
    "edit.prop.voting_duration": {
        "de": "Abstimmungsdauer (Stunden)",
        "en": "Voting Duration (hours)",
    },
    "edit.prop.max_voting_layers": {
        "de": "Max. Layers in Abstimmung",
        "en": "Max Voting Layers",
    },
    "edit.prop.allow_multiple_votes": {
        "de": "Mehrfachstimmen erlauben",
        "en": "Allow Multiple Votes",
    },
    "edit.prop.suggestion_duration": {
        "de": "Vorschlagsphasen-Dauer",
        "en": "Suggestion Phase Duration",
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
