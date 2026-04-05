# Squad Layer Vote Bot

A Discord bot for collecting Squad layer suggestions from users and running votes on them using Discord's native poll feature.

## Features

- **Layer Suggestions**: Users suggest layers via interactive dropdown menus (Map > Mode > Factions > Units)
- **Layer Data**: Pulls from [SquadLayerList](https://github.com/fantinodavide/SquadLayerList) and caches locally
- **Admin Blacklists**: Block maps, factions, unit types, or gamemodes
- **Configurable Gamemodes**: Admin selects which gamemodes are available (AAS, RAAS, Invasion, TC, Destruction, Insurgency)
- **Discord Native Polls**: Voting uses Discord's built-in poll system (max 10 options)
- **History Blocking**: Prevents re-suggesting layers from recent events
- **Multi-Language**: English and German (i18n)
- **Persistent Embeds**: Buttons survive bot restarts

## Event Cycle

1. Admin creates event (`/create_layer_suggestion`)
2. Suggestion phase opens (scheduled or manual via `/open_suggestions`)
3. Users suggest layers by clicking "Suggest Layer" button
4. Admin closes suggestions (`/close_suggestions`)
5. Admin selects or randomizes layers for voting (`/select_for_vote`)
6. Voting starts (`/start_vote`) — Discord native poll
7. Poll ends — winner saved to history
8. Repeat

## Commands

### Setup (Discord Admin)

| Command | Description |
|---------|-------------|
| `/setup` | Initial server setup (organizer role, log channel, language) |
| `/set_organizer_role` | Change the organizer role |
| `/set_language` | Change bot language (en/de) |
| `/set_log_channel` | Change the log channel |
| `/settings` | View all current settings |
| `/sync` | Force sync slash commands |

### Configuration (Organizer)

| Command | Description |
|---------|-------------|
| `/config_gamemodes` | Select which gamemodes are available for suggestions |
| `/config_blacklist` | Manage blacklists (maps, gamemodes, factions, unit types) |
| `/config_suggestions` | Set max suggestions per user/total, history lookback |
| `/refresh_layers` | Re-fetch layer data from GitHub |

### Event Management (Organizer)

| Command | Description |
|---------|-------------|
| `/create_layer_suggestion` | Create a new layer vote event in the channel |
| `/open_suggestions` | Manually open the suggestion phase |
| `/close_suggestions` | Close the suggestion phase |
| `/select_for_vote` | Select layers for voting (manual or random) |
| `/start_vote` | Start the Discord poll |
| `/end_vote` | End voting early and determine the winner |
| `/delete_event` | Delete the current event |

### User

| Command | Description |
|---------|-------------|
| `/history` | View past winning layers |

### Interactive Buttons (on event embed)

| Button | Description |
|--------|-------------|
| Suggest Layer | Start the layer suggestion dropdown flow |
| Info | View your suggestions and event info |
| Admin | Open admin panel (organizer only) |

## Installation

### Docker (Recommended)

```bash
cp .env.dist .env
# Edit .env and set DISCORD_BOT_TOKEN
docker-compose up -d
```

### Manual

```bash
pip install discord.py python-dotenv aiohttp pynacl
cp .env.dist .env
# Edit .env and set DISCORD_BOT_TOKEN
cd DebugScriptHelper
python bot.py
```

## First-Time Setup (in Discord)

1. Run `/setup` with organizer role, log channel, and language
2. Run `/refresh_layers` to populate the layer cache (auto-fetched on first start)
3. Optionally configure blacklists with `/config_blacklist`
4. Optionally configure allowed gamemodes with `/config_gamemodes`
5. Create an event with `/create_layer_suggestion`

## Configuration

### Environment Variables (.env)

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_BOT_TOKEN` | Yes | Discord bot token |
| `ADMIN_IDS` | No | Comma-separated bot superadmin user IDs |
| `DEBUG_MODE` | No | Enable debug logging (true/false) |
| `LAYERS_JSON_URL` | No | Custom URL for layer data |
| `PUID` | No | Docker user ID (default: 1000) |
| `PGID` | No | Docker group ID (default: 1000) |

### Guild Settings (via commands)

| Setting | Default | Description |
|---------|---------|-------------|
| Organizer Role | — | Required during `/setup` |
| Log Channel | — | Required during `/setup` |
| Language | en | en or de |
| Allowed Gamemodes | AAS, RAAS, Invasion, TC, Destruction, Insurgency | Which modes appear in suggestions |
| Blacklisted Maps | — | Jensen's Range, Tutorial, and Training maps are excluded at import |
| Blacklisted Factions | — | Factions excluded from suggestions |
| Blacklisted Units | — | Unit types excluded from suggestions |
| Max Suggestions/User | 2 | 1-10 |
| Max Total Suggestions | 25 | 1-25 (hard cap due to Discord dropdown limit) |
| History Lookback | 3 | Block layers from last N events |

## Data Structure

### Suggestion Object

```json
{
  "id": "abc12345",
  "user_id": "123456789",
  "user_name": "PlayerName",
  "map_name": "Al Basrah",
  "gamemode": "AAS",
  "layer_version": "v1",
  "team1_faction": "USMC",
  "team1_unit": "CombinedArms",
  "team2_faction": "RGF",
  "team2_unit": "Mechanized",
  "raw_name": "AlBasrah_AAS_v1",
  "suggested_at": "2026-03-30T12:00:00"
}
```

## Project Structure

```
squad-event-map-layer-vote/
├── DebugScriptHelper/
│   ├── bot.py              # Main bot: commands, views, background tasks
│   ├── config.py           # .env loading, constants
│   ├── database.py         # SQLite with JSON blobs
│   ├── i18n.py             # Translation strings (de/en)
│   └── utils.py            # Embed builders, formatting helpers
├── data/                   # SQLite DB (Docker volume)
├── Dockerfile
├── docker-compose.yml
├── .env.dist
├── .gitignore
├── README.md
└── USER_GUIDE.md
```
