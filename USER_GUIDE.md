# User Guide — Squad Layer Vote Bot

## For Players

### Suggesting a Layer

1. Find the channel with the active Layer Vote event
2. Click the **"Suggest Layer"** button on the event embed
3. Follow the dropdown steps:
   - **Step 1**: Select a map
   - **Step 2**: Select a game mode (e.g., AAS v1, RAAS v2, Invasion)
   - **Step 3**: Select Team 1 faction and unit type
   - **Step 4**: Select Team 2 faction and unit type
   - **Step 5**: Confirm your suggestion
4. Your suggestion appears in the event embed (if visible)

### Viewing Your Suggestions

Click the **"Info"** button on the event embed to see:
- Current event phase
- How many suggestions you've used
- Your submitted suggestions

### Voting

When the admin starts a vote, a Discord poll appears in the channel. Simply vote for your preferred layer.

### Viewing Past Winners

Use `/history` to see the winners of previous events.

---

## For Organizers

### Initial Setup

1. Run `/setup` and select:
   - **Organizer Role**: The role that can manage events
   - **Log Channel**: Where bot logs are sent
   - **Language**: English or German
2. Run `/refresh_layers` to load layer data (happens automatically on first start)

### Configuring the Bot

| Command | What it does |
|---------|-------------|
| `/config_gamemodes` | Choose which game modes are available (AAS, RAAS, etc.) |
| `/config_blacklist maps` | Block maps from being suggested (min. 2 required) |
| `/config_blacklist factions` | Block factions from being suggested |
| `/config_blacklist units` | Block unit types from being suggested |
| `/config_suggestions` | Set max suggestions per user, history blocking, visibility |
| `/config_roles suggest` | Set which roles can suggest layers |
| `/config_roles vote` | Set which roles can vote |

### Running an Event

**Step 1: Create**
```
/create_event
  suggestion_start: 05.04.2026 18:00    (optional — auto-opens at this time)
  voting_duration_hours: 24              (optional — how long the poll runs)
```

**Step 2: Open Suggestions**
- Wait for the scheduled time, or
- Use `/open_suggestions` to open immediately, or
- Click Admin > "Open Suggestions" on the event embed

**Step 3: Collect Suggestions**
Users click "Suggest Layer" and submit their picks.

**Step 4: Close Suggestions**
- Use `/close_suggestions` or Admin > "Close Suggestions"

**Step 5: Select Layers for Voting**
- Use `/select_for_vote` or Admin > "Select for Vote"
- Pick specific layers from the dropdown, or
- Click "Random" to select random layers
- Confirm your selection

**Step 6: Start Voting**
- Use `/start_vote` to create the Discord poll
- The poll runs for the configured duration

**Step 7: End & Results**
- Wait for the poll to expire naturally, or
- Use `/end_vote` to end early
- The winner is saved to history automatically

### Admin Panel

Click the **"Admin"** button on the event embed for quick actions:
- Open/Close suggestions
- Select layers for voting
- End voting
- Delete the event

### Settings

Use `/settings` to view all current configuration at a glance.

---

## FAQ

**Q: How many layers can be in the vote?**
A: Maximum 10 (Discord poll limit).

**Q: What if no factions are available for a map?**
A: Check your blacklist settings — you may have blocked too many factions.

**Q: Can I have multiple events in one server?**
A: Yes, one event per channel.

**Q: How does history blocking work?**
A: If a layer was suggested in one of the last N events (configurable), it cannot be suggested again. The exact combination (map + mode + factions + units) must match.

**Q: What maps are blacklisted by default?**
A: PacificProvingGrounds and JensensRange. You must always have at least 2 maps on the blacklist.
