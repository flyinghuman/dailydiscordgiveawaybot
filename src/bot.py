from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .config import Config, ConfigError, load_config
from .giveaway_manager import GiveawayManager
from .storage import StateStorage


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


class GiveawayBot(commands.Bot):
    def __init__(self, config: Config, storage: StateStorage) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            application_id=config.application_id,
        )
        self.config = config
        self.manager = GiveawayManager(self, config, storage)
        self.scheduled_task = self._scheduled_checker

    async def setup_hook(self) -> None:
        await self.manager.load()
        await self.manager.handle_scheduled()
        self._scheduled_checker.start()
        await self.tree.sync()

    @tasks.loop(minutes=1)
    async def _scheduled_checker(self) -> None:
        await self.manager.handle_scheduled()

    async def on_ready(self) -> None:
        logging.getLogger(__name__).info(
            "Logged in as %s (%s)", self.user, self.user.id
        )  # type: ignore[attr-defined]


def admin_required(
    interaction: discord.Interaction, manager: GiveawayManager
) -> Optional[str]:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return "This command can only be used inside a guild."
    if not manager.is_admin(interaction.user):
        return "You do not have permission to manage giveaways."
    return None


def build_bot(config_path: Path) -> GiveawayBot:
    config = load_config(config_path)
    configure_logging(config.logging.level)
    storage = StateStorage(Path("data") / "state.json")
    try:
        return GiveawayBot(config, storage)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"Invalid timezone configured: {config.default_timezone}") from exc


def _parse_end_time(value: str, manager: GiveawayManager) -> datetime:
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            naive = datetime.strptime(value, fmt)
            return naive.replace(tzinfo=manager.timezone).astimezone(UTC)
        except ValueError:
            continue
    raise ValueError("Time must be in 'YYYY-MM-DD HH:MM' (24h) format.")


def register_commands(bot: GiveawayBot) -> None:
    manager = bot.manager

    @bot.tree.command(name="giveaway-start", description="Start a new giveaway.")
    @app_commands.describe(
        channel="Channel where the giveaway embed should be posted.",
        winners="Number of winners to draw.",
        title="Title for the giveaway embed.",
        description="Description for the giveaway.",
    )
    async def giveaway_start(
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        winners: app_commands.Range[int, 1, 100],
        title: str,
        description: str,
    ) -> None:
        error = admin_required(interaction, manager)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        end_time = datetime.now(tz=UTC) + timedelta(
            minutes=manager.config.manual_defaults.duration_minutes
        )

        giveaway = await manager.start_giveaway(
            guild=interaction.guild,  # type: ignore[arg-type]
            channel=channel,
            winners=winners,
            title=title,
            description=description,
            end_time=end_time,
        )

        await interaction.followup.send(
            f"Giveaway `{giveaway.id}` created in {channel.mention} and ending at "
            f"{giveaway.end_time.astimezone(manager.timezone):%Y-%m-%d %H:%M %Z}.",
            ephemeral=True,
        )

    @bot.tree.command(name="giveaway-end", description="End a giveaway immediately.")
    @app_commands.describe(giveaway_id="Identifier of the giveaway to end.")
    async def giveaway_end(interaction: discord.Interaction, giveaway_id: str) -> None:
        error = admin_required(interaction, manager)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        giveaway = await manager.end_giveaway(giveaway_id)
        if not giveaway:
            await interaction.followup.send("Giveaway not found.", ephemeral=True)
            return
        await interaction.followup.send(
            f"Giveaway `{giveaway_id}` ended.", ephemeral=True
        )

    @bot.tree.command(name="giveaway-edit", description="Edit giveaway details.")
    @app_commands.describe(
        giveaway_id="Identifier of the giveaway to edit.",
        winners="New number of winners.",
        title="Updated title.",
        description="Updated description.",
        end_time="New end time in 'YYYY-MM-DD HH:MM' (24h) format.",
    )
    async def giveaway_edit(
        interaction: discord.Interaction,
        giveaway_id: str,
        winners: Optional[int] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> None:
        error = admin_required(interaction, manager)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        new_end_time = None
        if end_time:
            try:
                new_end_time = _parse_end_time(end_time, manager)
            except ValueError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return

        winners_value = None
        if winners is not None:
            if winners <= 0:
                await interaction.followup.send("Winners must be greater than zero.", ephemeral=True)
                return
            winners_value = int(winners)
        try:
            giveaway = await manager.update_giveaway(
                giveaway_id,
                winners=winners_value,
                title=title,
                description=description,
                end_time=new_end_time,
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        if not giveaway:
            await interaction.followup.send("Giveaway not found.", ephemeral=True)
            return
        await interaction.followup.send(
            f"Giveaway `{giveaway_id}` updated.", ephemeral=True
        )

    @bot.tree.command(
        name="giveaway-list", description="List all configured giveaways."
    )
    async def giveaway_list(interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command is guild-only.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        giveaways = await manager.list_giveaways()
        if not giveaways:
            await interaction.followup.send(
                "No giveaways have been created yet.", ephemeral=True
            )
            return
        lines = []
        for giveaway in giveaways:
            status = "Active" if giveaway.is_active else "Finished"
            end_time = giveaway.end_time.astimezone(manager.timezone).strftime(
                "%Y-%m-%d %H:%M %Z"
            )
            lines.append(
                f"- `{giveaway.id}` • **{giveaway.title}** • {status} • ends {end_time} • "
                f"{len(giveaway.participants)} participant(s)"
            )
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @bot.tree.command(
        name="giveaway-show-participants",
        description="Show who has joined a giveaway.",
    )
    @app_commands.describe(giveaway_id="Identifier of the giveaway to inspect.")
    async def giveaway_show_participants(
        interaction: discord.Interaction, giveaway_id: str
    ) -> None:
        error = admin_required(interaction, manager)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        giveaway = await manager.get_giveaway(giveaway_id)
        if not giveaway:
            await interaction.followup.send("Giveaway not found.", ephemeral=True)
            return
        if not giveaway.participants:
            await interaction.followup.send("No participants yet.", ephemeral=True)
            return
        mentions = "\n".join(f"- <@{pid}>" for pid in giveaway.participants)
        await interaction.followup.send(
            f"Participants for **{giveaway.title}** (`{giveaway.id}`):\n{mentions}",
            ephemeral=True,
        )

    @bot.tree.command(
        name="giveaway-reroll", description="Reroll winners for a finished giveaway."
    )
    @app_commands.describe(giveaway_id="Identifier of the giveaway to reroll.")
    async def giveaway_reroll(
        interaction: discord.Interaction, giveaway_id: str
    ) -> None:
        error = admin_required(interaction, manager)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        giveaway = await manager.get_giveaway(giveaway_id)
        if not giveaway:
            await interaction.followup.send("Giveaway not found.", ephemeral=True)
            return
        if giveaway.is_active:
            await interaction.followup.send(
                "End the giveaway before rerolling winners.", ephemeral=True
            )
            return

        winners = await manager.reroll(giveaway_id)
        if not winners:
            await interaction.followup.send(
                "No participants available to reroll.", ephemeral=True
            )
            return
        mentions = " ".join(f"<@{wid}>" for wid in winners)
        channel = await manager.get_text_channel(giveaway.channel_id)
        if channel:
            await channel.send(
                f"🔁 Giveaway **{giveaway.title}** reroll result: {mentions or 'No winners'}"
            )
        await interaction.followup.send(
            f"Rerolled giveaway `{giveaway_id}`.", ephemeral=True
        )

    @bot.tree.command(
        name="giveaway-logger", description="Set the giveaway log channel."
    )
    @app_commands.describe(channel="Channel where log messages should be posted.")
    async def giveaway_logger(
        interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        error = admin_required(interaction, manager)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await manager.set_logger_channel(channel.id)
        await interaction.followup.send(
            f"Logger channel set to {channel.mention}.", ephemeral=True
        )

    @bot.tree.command(
        name="giveaway-enable", description="Enable automatic scheduled giveaways."
    )
    async def giveaway_enable(interaction: discord.Interaction) -> None:
        error = admin_required(interaction, manager)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await manager.toggle_auto(True)
        await interaction.followup.send("Automatic giveaways enabled.", ephemeral=True)

    @bot.tree.command(
        name="giveaway-disable", description="Disable automatic scheduled giveaways."
    )
    async def giveaway_disable(interaction: discord.Interaction) -> None:
        error = admin_required(interaction, manager)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await manager.toggle_auto(False)
        await interaction.followup.send("Automatic giveaways disabled.", ephemeral=True)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Discord Giveaway Bot")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config") / "config.yaml",
        help="Path to the bot configuration file.",
    )
    args = parser.parse_args()

    try:
        bot = build_bot(args.config)
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    register_commands(bot)

    async with bot:
        await bot.start(bot.config.token)


if __name__ == "__main__":
    asyncio.run(main())
