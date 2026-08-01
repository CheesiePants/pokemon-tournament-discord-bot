import asyncio
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks


DB_PATH = os.getenv("DATABASE_PATH", "pokemon_league.db")
TIMEZONE = ZoneInfo(os.getenv("TOURNAMENT_TIMEZONE", "America/Chicago"))


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def initialize_database() -> None:
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                announcement_channel_id INTEGER,
                pokemon_role_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS teams (
                guild_id INTEGER NOT NULL,
                member_id INTEGER NOT NULL,
                name TEXT NOT NULL COLLATE NOCASE,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                differential INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, member_id),
                UNIQUE (guild_id, name)
            );

            CREATE TABLE IF NOT EXISTS allowed_channels (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                allow_commands INTEGER NOT NULL DEFAULT 0,
                allow_announcements INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, channel_id)
            );

            CREATE TABLE IF NOT EXISTS tournaments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                total_weeks INTEGER NOT NULL,
                current_week INTEGER NOT NULL DEFAULT 1,
                started_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                last_monday TEXT,
                last_friday TEXT
            );

            CREATE UNIQUE INDEX IF NOT EXISTS one_active_tournament
            ON tournaments(guild_id) WHERE active = 1;

            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
                week INTEGER NOT NULL,
                team1_member_id INTEGER,
                team2_member_id INTEGER,
                bye_member_id INTEGER,
                winner_member_id INTEGER,
                loser_member_id INTEGER,
                differential INTEGER,
                reported INTEGER NOT NULL DEFAULT 0,
                CHECK (
                    (bye_member_id IS NOT NULL AND team1_member_id IS NULL AND team2_member_id IS NULL)
                    OR
                    (bye_member_id IS NULL AND team1_member_id IS NOT NULL AND team2_member_id IS NOT NULL)
                )
            );
            """
        )


class DifferentialView(discord.ui.View):
    def __init__(self, user_id: int) -> None:
        super().__init__(timeout=180)
        self.user_id = user_id
        self.value: int | None = None
        for value in range(1, 7):
            button = discord.ui.Button(
                label=str(value),
                style=discord.ButtonStyle.primary,
            )

            async def select_value(
                button_interaction: discord.Interaction,
                selected: int = value,
            ) -> None:
                self.value = selected
                self.stop()
                await button_interaction.response.edit_message(
                    content=f"Point differential selected: **{selected}**",
                    view=None,
                )

            button.callback = select_value
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "Only the administrator recording this battle can choose the differential.",
            ephemeral=True,
        )
        return False


class MatchSelectionView(discord.ui.View):
    def __init__(
        self, user_id: int, matches: list[sqlite3.Row], allow_manual: bool = True
    ) -> None:
        super().__init__(timeout=180)
        self.user_id = user_id
        self.selected_match: sqlite3.Row | None = None
        self.manual = False

        # Discord allows at most 25 buttons in five rows. Reserve the final
        # slot for manual entry when it is enabled.
        match_limit = 24 if allow_manual else 25
        for index, match in enumerate(matches[:match_limit]):
            label = f"{match['team1_name']} vs {match['team2_name']}"
            button = discord.ui.Button(
                label=label[:80],
                style=discord.ButtonStyle.secondary,
                row=index // 5,
            )

            async def select_match(
                button_interaction: discord.Interaction,
                selected: sqlite3.Row = match,
            ) -> None:
                self.selected_match = selected
                self.stop()
                await button_interaction.response.edit_message(
                    content=(
                        f"Battle selected: **{selected['team1_name']}** vs "
                        f"**{selected['team2_name']}**"
                    ),
                    view=None,
                )

            button.callback = select_match
            self.add_item(button)

        if allow_manual:
            manual_index = min(len(matches), 24)
            manual_button = discord.ui.Button(
                label="Manual fight input",
                style=discord.ButtonStyle.primary,
                row=manual_index // 5,
            )

            async def select_manual(button_interaction: discord.Interaction) -> None:
                self.manual = True
                self.stop()
                await button_interaction.response.edit_message(
                    content="Manual fight input selected.",
                    view=None,
                )

            manual_button.callback = select_manual
            self.add_item(manual_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "Only the administrator recording this battle can choose the match.",
            ephemeral=True,
        )
        return False


class WinnerSelectionView(discord.ui.View):
    def __init__(
        self, user_id: int, first: sqlite3.Row, second: sqlite3.Row
    ) -> None:
        super().__init__(timeout=180)
        self.user_id = user_id
        self.winner: sqlite3.Row | None = None

        for team in (first, second):
            button = discord.ui.Button(
                label=team["name"][:80],
                style=discord.ButtonStyle.success,
            )

            async def select_winner(
                button_interaction: discord.Interaction,
                selected: sqlite3.Row = team,
            ) -> None:
                self.winner = selected
                self.stop()
                await button_interaction.response.edit_message(
                    content=f"Winner selected: **{selected['name']}**",
                    view=None,
                )

            button.callback = select_winner
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "Only the administrator recording this battle can choose the winner.",
            ephemeral=True,
        )
        return False


class TeamRemovalView(discord.ui.View):
    def __init__(self, user_id: int, teams: list[sqlite3.Row]) -> None:
        super().__init__(timeout=180)
        self.user_id = user_id
        self.teams = teams
        self.page = 0
        self.selected_member_id: int | None = None
        self._build_page()

    def _build_page(self) -> None:
        self.clear_items()
        start = self.page * 25
        page_teams = self.teams[start:start + 25]
        select = discord.ui.Select(
            placeholder=f"Choose a team to remove (page {self.page + 1})",
            options=[
                discord.SelectOption(
                    label=team["name"][:100],
                    value=str(team["member_id"]),
                    description=f"{team['wins']}W-{team['losses']}L, Diff {team['differential']:+d}"[:100],
                )
                for team in page_teams
            ],
            row=0,
        )

        async def select_team(select_interaction: discord.Interaction) -> None:
            self.selected_member_id = int(select.values[0])
            selected = next(
                team for team in self.teams
                if team["member_id"] == self.selected_member_id
            )
            self.stop()
            await select_interaction.response.edit_message(
                content=f"Team selected for removal: **{selected['name']}**",
                view=None,
            )

        select.callback = select_team
        self.add_item(select)

        if self.page > 0:
            previous = discord.ui.Button(label="Previous", row=1)

            async def previous_page(button_interaction: discord.Interaction) -> None:
                self.page -= 1
                self._build_page()
                await button_interaction.response.edit_message(view=self)

            previous.callback = previous_page
            self.add_item(previous)

        if start + 25 < len(self.teams):
            following = discord.ui.Button(label="Next", row=1)

            async def next_page(button_interaction: discord.Interaction) -> None:
                self.page += 1
                self._build_page()
                await button_interaction.response.edit_message(view=self)

            following.callback = next_page
            self.add_item(following)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "Only the administrator removing a team can use this list.",
            ephemeral=True,
        )
        return False


@dataclass
class Prompt:
    bot: commands.Bot
    interaction: discord.Interaction

    async def ask(self, question: str, timeout: float = 180) -> discord.Message:
        await self.interaction.followup.send(question)

        def check(message: discord.Message) -> bool:
            return (
                message.author.id == self.interaction.user.id
                and message.channel.id == self.interaction.channel_id
            )

        try:
            return await self.bot.wait_for("message", check=check, timeout=timeout)
        except asyncio.TimeoutError:
            raise ValueError("The setup timed out. Run the command again when you are ready.")

    async def integer(self, question: str, minimum: int = 1) -> int:
        while True:
            message = await self.ask(question)
            try:
                value = int(message.content.strip())
                if value >= minimum:
                    return value
            except ValueError:
                pass
            await self.interaction.followup.send(f"Please enter a whole number of at least {minimum}.")

    async def differential(self) -> int:
        view = DifferentialView(self.interaction.user.id)
        await self.interaction.followup.send(
            "What was the positive point differential? Choose 1 through 6:",
            view=view,
            wait=True,
        )
        timed_out = await view.wait()
        if timed_out or view.value is None:
            raise ValueError("The differential selection timed out. Run the command again.")
        return view.value

    async def match_selection(
        self, matches: list[sqlite3.Row]
    ) -> tuple[sqlite3.Row | None, bool]:
        view = MatchSelectionView(self.interaction.user.id, matches)
        note = ""
        if len(matches) > 24:
            note = "\nOnly the first 24 matches fit; use manual input for the others."
        await self.interaction.followup.send(
            "Choose this week's battle, or use manual fight input:" + note,
            view=view,
            wait=True,
        )
        timed_out = await view.wait()
        if timed_out or (view.selected_match is None and not view.manual):
            raise ValueError("The battle selection timed out. Run the command again.")
        return view.selected_match, view.manual

    async def winner(
        self, first: sqlite3.Row, second: sqlite3.Row
    ) -> sqlite3.Row:
        view = WinnerSelectionView(self.interaction.user.id, first, second)
        await self.interaction.followup.send(
            "Who won this battle?",
            view=view,
            wait=True,
        )
        timed_out = await view.wait()
        if timed_out or view.winner is None:
            raise ValueError("The winner selection timed out. Run the command again.")
        return view.winner

    async def team_to_remove(self, teams: list[sqlite3.Row]) -> int:
        view = TeamRemovalView(self.interaction.user.id, teams)
        await self.interaction.followup.send(
            "Select a team from the list to remove it:",
            view=view,
            wait=True,
        )
        timed_out = await view.wait()
        if timed_out or view.selected_member_id is None:
            raise ValueError("The team selection timed out. Run the command again.")
        return view.selected_member_id


class LeagueCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.command and interaction.command.name == "configure_channel":
            return True
        if not interaction.guild_id or not interaction.channel_id:
            return True
        with connect() as db:
            configured = db.execute(
                "SELECT COUNT(*) FROM allowed_channels WHERE guild_id=? AND allow_commands=1",
                (interaction.guild_id,),
            ).fetchone()[0]
            if not configured:
                return True
            channel_ids = [interaction.channel_id]
            if isinstance(interaction.channel, discord.Thread):
                channel_ids.append(interaction.channel.parent_id)
            placeholders = ",".join("?" for _ in channel_ids)
            allowed = db.execute(
                f"""
                SELECT 1 FROM allowed_channels
                WHERE guild_id=? AND allow_commands=1
                  AND channel_id IN ({placeholders})
                """,
                (interaction.guild_id, *channel_ids),
            ).fetchone()
        if allowed:
            return True
        await interaction.response.send_message(
            "Commands are not enabled in this channel or thread."
        )
        return False


class PokemonLeagueBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents, tree_cls=LeagueCommandTree)

    async def setup_hook(self) -> None:
        initialize_database()
        scheduler.start()
        test_guild = os.getenv("DISCORD_TEST_GUILD_ID", "765285366461628436")
        if test_guild:
            guild = discord.Object(id=int(test_guild))
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
                # Guild commands appear immediately during development. Remove
                # previously registered global copies so Discord does not show
                # every slash command twice in the test server.
                self.tree.clear_commands(guild=None)
                await self.tree.sync()
                print(f"Slash commands synced to test server {test_guild}.")
            except discord.Forbidden:
                print(
                    f"WARNING: The bot cannot access test server {test_guild}. "
                    "Falling back to global slash commands."
                )
                await self.tree.sync()
        else:
            await self.tree.sync()


bot = PokemonLeagueBot()


async def require_admin(interaction: discord.Interaction) -> bool:
    # League commands are intentionally available to every server member.
    # Keeping this helper makes the command flow explicit and allows a future
    # permission system to be introduced without rewriting every command.
    return True


def active_tournament(guild_id: int) -> sqlite3.Row | None:
    with connect() as db:
        return db.execute(
            "SELECT * FROM tournaments WHERE guild_id = ? AND active = 1", (guild_id,)
        ).fetchone()


def team_for_name(guild_id: int, name: str) -> sqlite3.Row | None:
    with connect() as db:
        return db.execute(
            "SELECT * FROM teams WHERE guild_id = ? AND name = ? COLLATE NOCASE",
            (guild_id, name.strip()),
        ).fetchone()


def role_ping(guild_id: int) -> str:
    with connect() as db:
        row = db.execute(
            "SELECT pokemon_role_id FROM guild_settings WHERE guild_id = ?", (guild_id,)
        ).fetchone()
    return f"<@&{row['pokemon_role_id']}>" if row and row["pokemon_role_id"] else ""


def announcement_destinations(
    guild: discord.Guild, fallback_channel_id: int
) -> list[discord.TextChannel | discord.Thread]:
    with connect() as db:
        rows = db.execute(
            """
            SELECT channel_id FROM allowed_channels
            WHERE guild_id=? AND allow_announcements=1
            ORDER BY channel_id
            """,
            (guild.id,),
        ).fetchall()
    destinations = []
    for row in rows:
        channel = guild.get_channel_or_thread(row["channel_id"])
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            destinations.append(channel)
    if not destinations:
        fallback = guild.get_channel_or_thread(fallback_channel_id)
        if isinstance(fallback, (discord.TextChannel, discord.Thread)):
            destinations.append(fallback)
    return destinations


def schedule_text(tournament_id: int, week: int) -> str:
    with connect() as db:
        rows = db.execute(
            """
            SELECT m.*, t1.name AS team1_name, t2.name AS team2_name, b.name AS bye_name
            FROM matches m
            LEFT JOIN teams t1 ON t1.member_id=m.team1_member_id
              AND t1.guild_id=(SELECT guild_id FROM tournaments WHERE id=m.tournament_id)
            LEFT JOIN teams t2 ON t2.member_id=m.team2_member_id
              AND t2.guild_id=(SELECT guild_id FROM tournaments WHERE id=m.tournament_id)
            LEFT JOIN teams b ON b.member_id=m.bye_member_id
              AND b.guild_id=(SELECT guild_id FROM tournaments WHERE id=m.tournament_id)
            WHERE m.tournament_id=? AND m.week=?
            ORDER BY m.id
            """,
            (tournament_id, week),
        ).fetchall()
    lines = []
    for row in rows:
        if row["bye_member_id"]:
            lines.append(f"• **{row['bye_name']}** has a bye")
        else:
            lines.append(f"• **{row['team1_name']}** vs **{row['team2_name']}**")
    return "\n".join(lines) or "No matches are scheduled."


def standings_text(guild_id: int) -> str:
    with connect() as db:
        teams = db.execute(
            """
            SELECT * FROM teams
            WHERE guild_id=?
            ORDER BY wins DESC, differential DESC, name COLLATE NOCASE
            """,
            (guild_id,),
        ).fetchall()
    if not teams:
        return "There are no registered teams in this league."

    lines = []
    displayed_rank = 0
    previous_record: tuple[int, int] | None = None
    for position, team in enumerate(teams, 1):
        record = (team["wins"], team["differential"])
        if record != previous_record:
            displayed_rank = position
            previous_record = record
        lines.append(
            f"**{displayed_rank}. {team['name']}** — "
            f"{team['wins']}W-{team['losses']}L · Diff {team['differential']:+d}"
        )
    return "\n".join(lines)


def week_results_text(tournament_id: int, week: int) -> str:
    with connect() as db:
        matches = db.execute(
            """
            SELECT m.*,
                   t1.name AS team1_name,
                   t2.name AS team2_name,
                   b.name AS bye_name,
                   w.name AS winner_name,
                   l.name AS loser_name
            FROM matches m
            JOIN tournaments tournament ON tournament.id=m.tournament_id
            LEFT JOIN teams t1 ON t1.guild_id=tournament.guild_id
              AND t1.member_id=m.team1_member_id
            LEFT JOIN teams t2 ON t2.guild_id=tournament.guild_id
              AND t2.member_id=m.team2_member_id
            LEFT JOIN teams b ON b.guild_id=tournament.guild_id
              AND b.member_id=m.bye_member_id
            LEFT JOIN teams w ON w.guild_id=tournament.guild_id
              AND w.member_id=m.winner_member_id
            LEFT JOIN teams l ON l.guild_id=tournament.guild_id
              AND l.member_id=m.loser_member_id
            WHERE m.tournament_id=? AND m.week=?
            ORDER BY m.id
            """,
            (tournament_id, week),
        ).fetchall()

    lines = []
    for match in matches:
        if match["bye_member_id"]:
            lines.append(f"• **{match['bye_name']}** had a bye")
        elif match["reported"]:
            lines.append(
                f"• **{match['winner_name']}** defeated **{match['loser_name']}** "
                f"by {match['differential']} points"
            )
        else:
            lines.append(
                f"• **{match['team1_name']}** vs **{match['team2_name']}** — "
                "result not reported"
            )
    return "\n".join(lines) or "No matches were scheduled."


async def post_week_results(
    channel: discord.TextChannel | discord.Thread, tournament_id: int, week: int
) -> None:
    await channel.send(
        f"## Week {week} Results\n{week_results_text(tournament_id, week)}"
    )


async def post_standings(
    channel: discord.TextChannel | discord.Thread, guild_id: int, heading: str
) -> None:
    await channel.send(f"## {heading}\n{standings_text(guild_id)}")


async def announce_week(guild: discord.Guild, tournament: sqlite3.Row, heading: str) -> None:
    ping = role_ping(guild.id)
    for channel in announcement_destinations(guild, tournament["channel_id"]):
        await channel.send(
            f"{ping}\n## {heading}\n"
            f"{schedule_text(tournament['id'], tournament['current_week'])}",
            allowed_mentions=discord.AllowedMentions(roles=True),
        )


async def finish_tournament(guild: discord.Guild, tournament: sqlite3.Row) -> None:
    with connect() as db:
        standings = db.execute(
            """
            SELECT * FROM teams WHERE guild_id=?
            ORDER BY wins DESC, differential DESC, name COLLATE NOCASE
            """,
            (guild.id,),
        ).fetchall()
        db.execute("UPDATE tournaments SET active=0 WHERE id=?", (tournament["id"],))
    lines = [
        f"{index}. **{team['name']}** — {team['wins']}W-{team['losses']}L "
        f"(Diff: {team['differential']:+d})"
        for index, team in enumerate(standings, 1)
    ]
    tie_note = ""
    if len(standings) > 1 and (
        standings[0]["wins"], standings[0]["differential"]
    ) == (standings[1]["wins"], standings[1]["differential"]):
        tie_note = "\n\n⚠️ The top teams are still tied. An administrator must calculate the tiebreaker."
    for channel in announcement_destinations(guild, tournament["channel_id"]):
        await channel.send(
            f"{role_ping(guild.id)}\n## Final standings\n" + "\n".join(lines) + tie_note
            + "\n\nThe tournament is now complete.",
            allowed_mentions=discord.AllowedMentions(roles=True),
        )


@bot.tree.command(name="add_player", description="Add or update a league player and team.")
@app_commands.describe(player="The Discord member who owns the team")
async def add_player(interaction: discord.Interaction, player: discord.Member) -> None:
    if not interaction.guild or not await require_admin(interaction):
        return
    await interaction.response.defer()
    prompt = Prompt(bot, interaction)
    try:
        answer = await prompt.ask(f"What is {player.mention}'s team name?")
    except ValueError as error:
        await interaction.followup.send(str(error))
        return
    name = answer.content.strip()
    if not name or len(name) > 80:
        await interaction.followup.send("Team names must be 1–80 characters.")
        return
    try:
        with connect() as db:
            db.execute(
                """
                INSERT INTO teams(guild_id, member_id, name) VALUES(?,?,?)
                ON CONFLICT(guild_id, member_id) DO UPDATE SET name=excluded.name
                """,
                (interaction.guild_id, player.id, name),
            )
    except sqlite3.IntegrityError:
        await interaction.followup.send("Another player already uses that team name.")
        return
    await interaction.followup.send(f"Added {player.mention} as **{name}**.")


@bot.tree.command(name="remove_team", description="Choose and remove a registered league team.")
async def remove_team(interaction: discord.Interaction) -> None:
    if not interaction.guild or not await require_admin(interaction):
        return
    if active_tournament(interaction.guild_id):
        await interaction.response.send_message(
            "Teams cannot be removed during an active tournament. End the tournament first."
        )
        return
    with connect() as db:
        teams = db.execute(
            "SELECT * FROM teams WHERE guild_id=? ORDER BY name COLLATE NOCASE",
            (interaction.guild_id,),
        ).fetchall()
    if not teams:
        await interaction.response.send_message("There are no teams to remove.")
        return

    await interaction.response.defer()
    prompt = Prompt(bot, interaction)
    try:
        member_id = await prompt.team_to_remove(teams)
    except ValueError as error:
        await interaction.followup.send(str(error))
        return

    selected = next(team for team in teams if team["member_id"] == member_id)
    with connect() as db:
        db.execute(
            "DELETE FROM teams WHERE guild_id=? AND member_id=?",
            (interaction.guild_id, member_id),
        )
    await interaction.followup.send(f"Removed **{selected['name']}** from the league.")


@bot.tree.command(name="set_pokemon_role", description="Choose the role pinged by announcements.")
async def set_pokemon_role(interaction: discord.Interaction, role: discord.Role) -> None:
    if not interaction.guild or not await require_admin(interaction):
        return
    with connect() as db:
        db.execute(
            """
            INSERT INTO guild_settings(guild_id, pokemon_role_id) VALUES(?,?)
            ON CONFLICT(guild_id) DO UPDATE SET pokemon_role_id=excluded.pokemon_role_id
            """,
            (interaction.guild_id, role.id),
        )
    await interaction.response.send_message(
        f"Tournament announcements will mention {role.mention}."
    )


@bot.tree.command(
    name="configure_channel",
    description="Control where league commands and announcements are allowed.",
)
@app_commands.describe(
    channel="The Discord text channel or thread to configure",
    mode="What the bot is allowed to do in this location",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="Commands", value="commands"),
        app_commands.Choice(name="Announcements", value="announcements"),
        app_commands.Choice(name="Commands and announcements", value="both"),
        app_commands.Choice(name="Disabled", value="disabled"),
    ]
)
async def configure_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel | discord.Thread,
    mode: app_commands.Choice[str],
) -> None:
    if not interaction.guild or not await require_admin(interaction):
        return
    allow_commands = mode.value in ("commands", "both")
    allow_announcements = mode.value in ("announcements", "both")
    with connect() as db:
        if mode.value == "disabled":
            db.execute(
                "DELETE FROM allowed_channels WHERE guild_id=? AND channel_id=?",
                (interaction.guild_id, channel.id),
            )
        else:
            db.execute(
                """
                INSERT INTO allowed_channels(
                    guild_id,channel_id,allow_commands,allow_announcements
                ) VALUES(?,?,?,?)
                ON CONFLICT(guild_id,channel_id) DO UPDATE SET
                    allow_commands=excluded.allow_commands,
                    allow_announcements=excluded.allow_announcements
                """,
                (
                    interaction.guild_id, channel.id,
                    int(allow_commands), int(allow_announcements),
                ),
            )
    await interaction.response.send_message(
        f"{channel.mention} is now configured for **{mode.name.lower()}**."
    )


@bot.tree.command(name="begin_tournament", description="Interactively create a tournament schedule.")
async def begin_tournament(interaction: discord.Interaction) -> None:
    if not interaction.guild or not await require_admin(interaction):
        return
    if active_tournament(interaction.guild_id):
        await interaction.response.send_message("A tournament is already active.")
        return
    await interaction.response.defer()
    with connect() as db:
        teams = db.execute(
            "SELECT * FROM teams WHERE guild_id=? ORDER BY name", (interaction.guild_id,)
        ).fetchall()
    if len(teams) < 2:
        await interaction.followup.send("Add at least two players first.")
        return
    prompt = Prompt(bot, interaction)
    try:
        player_count = await prompt.integer(
            f"How many players are competing? ({len(teams)} registered)", 2
        )
        if player_count > len(teams):
            raise ValueError("There are not enough registered players.")
        weeks = await prompt.integer("How many weeks will the tournament run?", 1)
        name_map = {team["name"].casefold(): team for team in teams}
        schedule: list[tuple[int, int | None, int | None, int | None]] = []
        for week in range(1, weeks + 1):
            await interaction.followup.send(
                f"**Week {week} setup.** Enter matches as `Team A vs Team B`. "
                "Separate multiple fights with commas, such as "
                "`Team 1 vs Team 2, Team 3 vs Team 4`. "
                "For a bye, enter `bye: Team Name`. Enter `done` when this week is complete."
            )
            used: set[int] = set()
            while True:
                message = await prompt.ask(
                    f"Week {week}: enter one or more matchups, a bye, or `done`."
                )
                text = message.content.strip()
                if text.casefold() == "done":
                    if len(used) != player_count:
                        await interaction.followup.send(
                            f"This week includes {len(used)} of {player_count} players."
                        )
                        continue
                    break

                entries = [entry.strip() for entry in text.split(",") if entry.strip()]
                pending: list[tuple[int, int | None, int | None, int | None]] = []
                pending_used = set(used)
                error_message: str | None = None
                for entry in entries:
                    if entry.casefold().startswith("bye:"):
                        team = name_map.get(entry.split(":", 1)[1].strip().casefold())
                        if not team or team["member_id"] in pending_used:
                            error_message = "Unknown or already scheduled bye team."
                            break
                        pending_used.add(team["member_id"])
                        pending.append((week, None, None, team["member_id"]))
                        continue

                    separator = entry.casefold().find(" vs ")
                    if separator == -1:
                        error_message = (
                            "Use `Team A vs Team B`, separating multiple fights with commas."
                        )
                        break
                    first = name_map.get(entry[:separator].strip().casefold())
                    second = name_map.get(entry[separator + 4:].strip().casefold())
                    if (
                        not first or not second
                        or first["member_id"] == second["member_id"]
                        or first["member_id"] in pending_used
                        or second["member_id"] in pending_used
                    ):
                        error_message = "Unknown, duplicate, or already scheduled team."
                        break
                    pending_used.update((first["member_id"], second["member_id"]))
                    pending.append(
                        (week, first["member_id"], second["member_id"], None)
                    )

                if len(pending_used) > player_count:
                    error_message = f"This week can only include {player_count} players."
                if error_message:
                    await interaction.followup.send(error_message)
                    continue
                used = pending_used
                schedule.extend(pending)
    except ValueError as error:
        await interaction.followup.send(str(error))
        return

    now = datetime.now(TIMEZONE)
    with connect() as db:
        db.execute(
            """
            UPDATE teams
            SET wins=0, losses=0, differential=0
            WHERE guild_id=?
            """,
            (interaction.guild_id,),
        )
        cursor = db.execute(
            """
            INSERT INTO tournaments(guild_id,channel_id,total_weeks,started_at)
            VALUES(?,?,?,?)
            """,
            (interaction.guild_id, interaction.channel_id, weeks, now.isoformat()),
        )
        tournament_id = cursor.lastrowid
        db.executemany(
            """
            INSERT INTO matches(tournament_id,week,team1_member_id,team2_member_id,bye_member_id)
            VALUES(?,?,?,?,?)
            """,
            [(tournament_id, *row) for row in schedule],
        )
    await interaction.followup.send(
        f"Tournament created with {player_count} players over {weeks} weeks."
    )
    tournament = active_tournament(interaction.guild_id)
    await announce_week(interaction.guild, tournament, "Tournament begins — Week 1")


@bot.tree.command(name="record_battle", description="Record a completed tournament battle.")
async def record_battle(interaction: discord.Interaction) -> None:
    if not interaction.guild or not await require_admin(interaction):
        return
    tournament = active_tournament(interaction.guild_id)
    if not tournament:
        await interaction.response.send_message("There is no active tournament.")
        return
    await interaction.response.defer()
    prompt = Prompt(bot, interaction)
    try:
        with connect() as db:
            current_matches = db.execute(
                """
                SELECT m.*, t1.name AS team1_name, t2.name AS team2_name
                FROM matches m
                JOIN teams t1 ON t1.guild_id=? AND t1.member_id=m.team1_member_id
                JOIN teams t2 ON t2.guild_id=? AND t2.member_id=m.team2_member_id
                WHERE m.tournament_id=? AND m.week=? AND m.reported=0
                  AND m.bye_member_id IS NULL
                ORDER BY m.id
                """,
                (
                    interaction.guild_id, interaction.guild_id,
                    tournament["id"], tournament["current_week"],
                ),
            ).fetchall()

        match, manual = await prompt.match_selection(current_matches)
        if manual:
            matchup = (
                await prompt.ask("Which match? Enter `Team A vs Team B`.")
            ).content.strip()
            parts = matchup.split(" vs ")
            if len(parts) != 2:
                raise ValueError("Use `Team A vs Team B`.")
            first = team_for_name(interaction.guild_id, parts[0])
            second = team_for_name(interaction.guild_id, parts[1])
            if not first or not second:
                raise ValueError("One or both team names were not found.")
            with connect() as db:
                match = db.execute(
                    """
                    SELECT * FROM matches
                    WHERE tournament_id=? AND week=? AND reported=0
                      AND ((team1_member_id=? AND team2_member_id=?)
                        OR (team1_member_id=? AND team2_member_id=?))
                    LIMIT 1
                    """,
                    (
                        tournament["id"], tournament["current_week"],
                        first["member_id"], second["member_id"],
                        second["member_id"], first["member_id"],
                    ),
                ).fetchone()
        else:
            first = team_for_name(interaction.guild_id, match["team1_name"])
            second = team_for_name(interaction.guild_id, match["team2_name"])

        override = False
        if not match:
            override_answer = await prompt.ask(
                "Is this a override input (Use this for extension or for any reason "
                "you need to input stats of a match (Yes/No)"
            )
            answer = override_answer.content.strip().casefold()
            if answer not in ("yes", "y"):
                await interaction.followup.send("Battle input cancelled.")
                return
            override = True

        winner = await prompt.winner(first, second)
        loser = second if winner["member_id"] == first["member_id"] else first
        differential = await prompt.differential()
    except ValueError as error:
        await interaction.followup.send(str(error))
        return
    with connect() as db:
        db.execute(
            "UPDATE teams SET wins=wins+1,differential=differential+? WHERE guild_id=? AND member_id=?",
            (differential, interaction.guild_id, winner["member_id"]),
        )
        db.execute(
            "UPDATE teams SET losses=losses+1,differential=differential-? WHERE guild_id=? AND member_id=?",
            (differential, interaction.guild_id, loser["member_id"]),
        )
        if match:
            db.execute(
                """
                UPDATE matches
                SET winner_member_id=?,loser_member_id=?,differential=?,reported=1
                WHERE id=?
                """,
                (winner["member_id"], loser["member_id"], differential, match["id"]),
            )
        else:
            db.execute(
                """
                INSERT INTO matches(
                    tournament_id,week,team1_member_id,team2_member_id,
                    winner_member_id,loser_member_id,differential,reported
                ) VALUES(?,?,?,?,?,?,?,1)
                """,
                (
                    tournament["id"], tournament["current_week"],
                    first["member_id"], second["member_id"],
                    winner["member_id"], loser["member_id"], differential,
                ),
            )
    await interaction.followup.send(
        f"Recorded **{winner['name']}** over **{loser['name']}**, "
        f"differential {differential:+d}"
        + (" (override)." if override else ".")
    )


@bot.tree.command(
    name="change_battle",
    description="Correct a battle result recorded during the current week.",
)
async def change_battle(interaction: discord.Interaction) -> None:
    if not interaction.guild or not await require_admin(interaction):
        return
    tournament = active_tournament(interaction.guild_id)
    if not tournament:
        await interaction.response.send_message("There is no active tournament.")
        return

    with connect() as db:
        completed_matches = db.execute(
            """
            SELECT m.*, t1.name AS team1_name, t2.name AS team2_name
            FROM matches m
            JOIN teams t1 ON t1.guild_id=? AND t1.member_id=m.team1_member_id
            JOIN teams t2 ON t2.guild_id=? AND t2.member_id=m.team2_member_id
            WHERE m.tournament_id=? AND m.week=? AND m.reported=1
              AND m.bye_member_id IS NULL
            ORDER BY m.id
            """,
            (
                interaction.guild_id, interaction.guild_id,
                tournament["id"], tournament["current_week"],
            ),
        ).fetchall()
    if not completed_matches:
        await interaction.response.send_message(
            "No battles have been recorded for the current week."
        )
        return

    await interaction.response.defer()
    view = MatchSelectionView(
        interaction.user.id, completed_matches, allow_manual=False
    )
    note = ""
    if len(completed_matches) > 25:
        note = "\nOnly the first 25 recorded matches can be displayed."
    await interaction.followup.send(
        "Choose the recorded battle you want to change:" + note,
        view=view,
        wait=True,
    )
    timed_out = await view.wait()
    if timed_out or view.selected_match is None:
        await interaction.followup.send(
            "The battle selection timed out. Run the command again."
        )
        return

    match = view.selected_match
    first = team_for_name(interaction.guild_id, match["team1_name"])
    second = team_for_name(interaction.guild_id, match["team2_name"])
    if not first or not second:
        await interaction.followup.send("One of the teams no longer exists.")
        return

    prompt = Prompt(bot, interaction)
    try:
        winner = await prompt.winner(first, second)
        loser = second if winner["member_id"] == first["member_id"] else first
        differential = await prompt.differential()
    except ValueError as error:
        await interaction.followup.send(str(error))
        return

    old_winner = team_for_name(
        interaction.guild_id,
        match["team1_name"]
        if match["winner_member_id"] == first["member_id"]
        else match["team2_name"],
    )
    old_loser = second if old_winner["member_id"] == first["member_id"] else first
    old_differential = match["differential"]

    with connect() as db:
        db.execute(
            """
            UPDATE teams SET wins=wins-1, differential=differential-?
            WHERE guild_id=? AND member_id=?
            """,
            (old_differential, interaction.guild_id, old_winner["member_id"]),
        )
        db.execute(
            """
            UPDATE teams SET losses=losses-1, differential=differential+?
            WHERE guild_id=? AND member_id=?
            """,
            (old_differential, interaction.guild_id, old_loser["member_id"]),
        )
        db.execute(
            """
            UPDATE teams SET wins=wins+1, differential=differential+?
            WHERE guild_id=? AND member_id=?
            """,
            (differential, interaction.guild_id, winner["member_id"]),
        )
        db.execute(
            """
            UPDATE teams SET losses=losses+1, differential=differential-?
            WHERE guild_id=? AND member_id=?
            """,
            (differential, interaction.guild_id, loser["member_id"]),
        )
        db.execute(
            """
            UPDATE matches
            SET winner_member_id=?, loser_member_id=?, differential=?
            WHERE id=?
            """,
            (winner["member_id"], loser["member_id"], differential, match["id"]),
        )

    await interaction.followup.send(
        f"Changed the result: **{winner['name']}** defeated **{loser['name']}** "
        f"by {differential} points."
    )


@bot.tree.command(name="team_stats", description="View a team's current league record.")
async def team_stats(interaction: discord.Interaction, team_name: str) -> None:
    if not interaction.guild:
        return
    team = team_for_name(interaction.guild_id, team_name)
    if not team:
        await interaction.response.send_message("Team not found.")
        return
    await interaction.response.send_message(
        f"**{team['name']}** — {team['wins']} wins, {team['losses']} losses, "
        f"differential {team['differential']:+d}"
    )


@bot.tree.command(name="standings", description="Post the current Pokémon league standings.")
async def standings(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        return
    await interaction.response.send_message(
        "## Pokémon League Standings\n" + standings_text(interaction.guild_id)
    )


@bot.tree.command(name="skip_week", description="Advance immediately to the next tournament week.")
async def skip_week(interaction: discord.Interaction) -> None:
    if not interaction.guild or not await require_admin(interaction):
        return
    tournament = active_tournament(interaction.guild_id)
    if not tournament:
        await interaction.response.send_message("There is no active tournament.")
        return
    if tournament["current_week"] >= tournament["total_weeks"]:
        await interaction.response.send_message(
            "Final week completed. Posting the results and final standings."
        )
        for channel in announcement_destinations(
            interaction.guild, tournament["channel_id"]
        ):
            await post_week_results(
                channel, tournament["id"], tournament["current_week"]
            )
        await finish_tournament(interaction.guild, tournament)
        return
    for channel in announcement_destinations(
        interaction.guild, tournament["channel_id"]
    ):
        await post_week_results(
            channel, tournament["id"], tournament["current_week"]
        )
        await post_standings(
            channel,
            interaction.guild_id,
            f"Standings after Week {tournament['current_week']}",
        )
    with connect() as db:
        db.execute(
            "UPDATE tournaments SET current_week=current_week+1,last_monday=? WHERE id=?",
            (datetime.now(TIMEZONE).date().isoformat(), tournament["id"]),
        )
    await interaction.response.send_message("Week advanced.")
    await announce_week(
        interaction.guild, active_tournament(interaction.guild_id),
        f"Schedule advanced — Week {tournament['current_week'] + 1}",
    )


@tasks.loop(minutes=1)
async def scheduler() -> None:
    now = datetime.now(TIMEZONE)
    if now.hour != 0 or now.minute > 1 or now.weekday() not in (0, 4):
        return
    date_key = now.date().isoformat()
    with connect() as db:
        tournaments = db.execute("SELECT * FROM tournaments WHERE active=1").fetchall()
    for tournament in tournaments:
        guild = bot.get_guild(tournament["guild_id"])
        if not guild:
            continue
        if now.weekday() == 4 and tournament["last_friday"] != date_key:
            for channel in announcement_destinations(
                guild, tournament["channel_id"]
            ):
                await channel.send(
                    f"{role_ping(guild.id)}\n⏰ **Battle reminder:** complete your Week "
                    f"{tournament['current_week']} match before Monday!",
                    allowed_mentions=discord.AllowedMentions(roles=True),
                )
            with connect() as db:
                db.execute(
                    "UPDATE tournaments SET last_friday=? WHERE id=?",
                    (date_key, tournament["id"]),
                )
        elif now.weekday() == 0 and tournament["last_monday"] != date_key:
            started_date = datetime.fromisoformat(tournament["started_at"]).date()
            if tournament["last_monday"] is None and now.date() == started_date:
                new_week = tournament["current_week"]
            else:
                new_week = tournament["current_week"] + 1
            if new_week > tournament["total_weeks"]:
                for channel in announcement_destinations(
                    guild, tournament["channel_id"]
                ):
                    await post_week_results(
                        channel, tournament["id"], tournament["current_week"]
                    )
                await finish_tournament(guild, tournament)
            else:
                if new_week > tournament["current_week"]:
                    for channel in announcement_destinations(
                        guild, tournament["channel_id"]
                    ):
                        await post_week_results(
                            channel, tournament["id"], tournament["current_week"]
                        )
                        await post_standings(
                            channel,
                            guild.id,
                            f"Standings after Week {tournament['current_week']}",
                        )
                with connect() as db:
                    db.execute(
                        "UPDATE tournaments SET current_week=?,last_monday=? WHERE id=?",
                        (new_week, date_key, tournament["id"]),
                    )
                await announce_week(
                    guild, active_tournament(guild.id), f"Tournament Week {new_week}"
                )


@scheduler.before_loop
async def before_scheduler() -> None:
    await bot.wait_until_ready()


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} ({bot.user.id if bot.user else 'unknown'})")
    if bot.guilds:
        print("Servers visible to the bot:")
        for guild in bot.guilds:
            print(f"  - {guild.name}: {guild.id}")
    else:
        print(
            "WARNING: The bot cannot see any servers. Invite it using both the "
            "'bot' and 'applications.commands' OAuth2 scopes."
        )


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Set the DISCORD_TOKEN environment variable.")
    bot.run(token)


if __name__ == "__main__":
    main()
