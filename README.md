# Competitive Pokémon Tournament Discord Bot

A Discord tournament administrator with persistent teams, weekly schedules, battle
reporting, standings, role pings, and automatic Monday/Friday announcements.

## Setup

1. Install Python 3.11 or newer.
2. Create a bot in the Discord Developer Portal.
3. Enable **Server Members Intent** and **Message Content Intent**.
4. Invite it with the `bot` and `applications.commands` scopes. Give it permission
   to view channels, send messages, and mention the Pokémon role.
5. Install and run:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:DISCORD_TOKEN="your-token"
$env:DISCORD_TEST_GUILD_ID="your-server-id"
python bot.py
```

Leave `DISCORD_TEST_GUILD_ID` unset in production. Global slash commands can take
up to an hour to appear. The default scheduler timezone is `America/Chicago`;
change `TOURNAMENT_TIMEZONE` to another IANA timezone if needed.

## Commands

- `/add_player player:@member` — asks for and stores the player's team name.
- `/remove_team` — displays a selectable list and removes the chosen team.
- `/set_pokemon_role role:@role` — sets the announcement role.
- `/configure_channel channel:#channel mode:...` — controls where commands and
  announcements are allowed; text channels and threads are supported.
- `/begin_tournament` — asks for player count, week count, and every matchup.
- `/record_battle` — records a scheduled result and updates both teams.
- `/change_battle` — corrects a result recorded in the current week and repairs
  both teams' statistics.
- `/skip_week` — advances a week immediately and announces its schedule.
- `/team_stats team_name:name` — shows wins, losses, and differential.
- `/standings` — posts the league table ranked by wins, then differential.

League commands are available to all server members. Interactive buttons can only
be used by the member who started that command.
Starting a tournament resets every registered team's wins, losses, and point
differential to zero.
During schedule entry, use exact registered team names:

```text
Team Rocket vs Cerulean Gym, Pallet Town vs Pewter Gym
bye: Viridian Gym
done
```

The bot stores all league data in SQLite, so it survives restarts. Keep the bot
running at midnight in the configured timezone for scheduled announcements.
