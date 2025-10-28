from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime, timedelta
import re
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .config import Config, ConfigError, load_config
from .giveaway_manager import GiveawayManager
from .storage import StateStorage


CHANNEL_MENTION_RE = re.compile(r"^<#?(\d+)>?$")


class ChannelResolutionError(RuntimeError):
    """Raised when a provided channel value cannot be resolved."""


PERMISSION_LOG = logging.getLogger("giveaway.permissions")


async def _resolve_text_channel(
    bot: discord.Client,
    guild: discord.Guild,
    value: str,
    *,
    resolved: Optional[dict[str, dict]] = None,
) -> discord.TextChannel:
    if not value:
        raise ChannelResolutionError(
            "No channel value was provided. Use a mention, ID, or exact name."
        )

    value = value.strip()

    resolved_map = resolved or {}
    if resolved_map:
        possible_keys = (
            value,
            value.lstrip("#"),
        )
        resolved_channel = None
        for key in possible_keys:
            resolved_channel = resolved_map.get(key)
            if resolved_channel:
                break
        if not resolved_channel:
            lookup_name = value.lstrip("#").lower()
            for candidate in resolved_map.values():
                if candidate.get("name", "").lower() == lookup_name:
                    resolved_channel = candidate
                    break
        if resolved_channel:
            value = str(resolved_channel.get("id", value))

    channel_id: Optional[int] = None
    mention_match = CHANNEL_MENTION_RE.match(value)
    if mention_match:
        channel_id = int(mention_match.group(1))
    elif value.isdigit():
        channel_id = int(value)

    def _validate_channel(obj: Optional[discord.abc.GuildChannel]) -> Optional[discord.TextChannel]:
        if isinstance(obj, discord.TextChannel) and obj.guild.id == guild.id:
            return obj
        return None

    if channel_id is not None:
        cached_channel = _validate_channel(guild.get_channel(channel_id))
        if cached_channel:
            return cached_channel
        cached_channel = _validate_channel(bot.get_channel(channel_id))
        if cached_channel:
            return cached_channel
        try:
            fetched = await guild.fetch_channel(channel_id)
        except discord.Forbidden as exc:
            raise ChannelResolutionError(
                "I do not have permission to access that channel. Update the channel permissions or choose a different one."
            ) from exc
        except (discord.NotFound, discord.HTTPException) as exc:
            fetched = None
        validated = _validate_channel(fetched) if fetched else None
        if validated:
            return validated

    lookup = value.lstrip("#").lower()
    for text_channel in guild.text_channels:
        if text_channel.name.lower() == lookup:
            return text_channel

    if resolved_map:
        for candidate in resolved_map.values():
            if candidate.get("name", "").lower() == lookup:
                try:
                    candidate_id = int(candidate["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                cached_channel = _validate_channel(guild.get_channel(candidate_id))
                if cached_channel:
                    return cached_channel
                cached_channel = _validate_channel(bot.get_channel(candidate_id))
                if cached_channel:
                    return cached_channel
                try:
                    fetched = await guild.fetch_channel(candidate_id)
                except discord.Forbidden as exc:
                    raise ChannelResolutionError(
                        "I do not have permission to access that channel. Update the channel permissions or choose a different one."
                    ) from exc
                except (discord.NotFound, discord.HTTPException):
                    continue
                validated = _validate_channel(fetched) if fetched else None
                if validated:
                    return validated
    raise ChannelResolutionError(
        "Could not resolve the provided channel. Use a mention, ID, or exact name."
    )

def configure_logging(level: str) -> None:
    console_level = getattr(logging, level.upper(), logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    root_logger.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_dir / "log.txt", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)


class GiveawayBot(commands.Bot):
    def __init__(self, config: Config, storage: StateStorage) -> None:
        intents = discord.Intents.default()

        super().__init__(
            command_prefix=commands.when_mentioned,
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


async def admin_required(
    interaction: discord.Interaction, manager: GiveawayManager
) -> Optional[str]:
    command_name = getattr(getattr(interaction, "command", None), "name", "unknown")
    user = interaction.user
    user_id = getattr(user, "id", "unknown")

    guild = interaction.guild
    if guild is None:
        PERMISSION_LOG.debug(
            "Denied command %s for user %s: non-guild context.",
            command_name,
            user_id,
        )
        return "This command can only be used inside a guild."

    member: Optional[discord.Member]
    if isinstance(user, discord.Member):
        member = user
    else:
        user_id_value: Optional[int]
        if isinstance(user_id, int):
            user_id_value = user_id
        else:
            try:
                user_id_value = int(user_id)
            except (TypeError, ValueError):
                user_id_value = None

        member = guild.get_member(user_id_value) if user_id_value is not None else None
        if member is None and user_id_value is not None:
            try:
                member = await guild.fetch_member(user_id_value)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                member = None

    if member is None:
        PERMISSION_LOG.warning(
            "Denied command %s for user %s: unable to resolve guild member.",
            command_name,
            user_id,
        )
        return "You do not have permission to manage giveaways."

    if not manager.is_admin(
        member,
        guild_owner_id=getattr(guild, "owner_id", None),
        base_permissions=getattr(interaction, "permissions", None),
        role_ids=getattr(member, "_roles", None),
    ):
        member_role_ids = [role.id for role in member.roles]
        if not member_role_ids:
            raw_roles = getattr(member, "_roles", None)
            if raw_roles:
                member_role_ids = []
                for role_id in raw_roles:
                    try:
                        member_role_ids.append(int(role_id))
                    except (TypeError, ValueError):
                        continue
        PERMISSION_LOG.warning(
            "Denied command %s for user %s: missing giveaway admin rights (roles=%s).",
            command_name,
            member.id,
            member_role_ids,
        )
        return "You do not have permission to manage giveaways."

    PERMISSION_LOG.debug(
        "Authorized command %s for user %s.", command_name, member.id
    )
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
        channel="Channel where the giveaway embed should be posted (mention, ID, or name).",
        winners="Number of winners to draw.",
        title="Title for the giveaway embed.",
        description="Description for the giveaway.",
    )
    async def giveaway_start(
        interaction: discord.Interaction,
        channel: str,
        winners: app_commands.Range[int, 1, 100],
        title: str,
        description: str,
    ) -> None:
        error = await admin_required(interaction, manager)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a guild.", ephemeral=True
            )
            return

        resolved_channels = None
        try:
            resolved_channels = (
                interaction.data.get("resolved", {}).get("channels", {})  # type: ignore[assignment]
                if isinstance(interaction.data, dict)
                else None
            )
        except AttributeError:
            resolved_channels = None

        try:
            target_channel = await _resolve_text_channel(
                bot,
                interaction.guild,
                channel,
                resolved=resolved_channels,
            )
        except ChannelResolutionError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        end_time = datetime.now(tz=UTC) + timedelta(
            minutes=manager.config.manual_defaults.duration_minutes
        )

        giveaway = await manager.start_giveaway(
            guild=interaction.guild,  # type: ignore[arg-type]
            channel=target_channel,
            winners=winners,
            title=title,
            description=description,
            end_time=end_time,
        )

        await interaction.followup.send(
            f"Giveaway `{giveaway.id}` created in {target_channel.mention} and ending at "
            f"{giveaway.end_time.astimezone(manager.timezone):%Y-%m-%d %H:%M %Z}.",
            ephemeral=True,
        )

    @bot.tree.command(name="giveaway-end", description="End a giveaway immediately.")
    @app_commands.describe(giveaway_id="Identifier of the giveaway to end.")
    async def giveaway_end(interaction: discord.Interaction, giveaway_id: str) -> None:
        error = await admin_required(interaction, manager)
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
        error = await admin_required(interaction, manager)
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
        error = await admin_required(interaction, manager)
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
        error = await admin_required(interaction, manager)
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
    @app_commands.describe(
        channel="Channel where log messages should be posted (mention, ID, or name)."
    )
    async def giveaway_logger(
        interaction: discord.Interaction, channel: str
    ) -> None:
        error = await admin_required(interaction, manager)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a guild.", ephemeral=True
            )
            return

        resolved_channels = None
        try:
            resolved_channels = (
                interaction.data.get("resolved", {}).get("channels", {})  # type: ignore[assignment]
                if isinstance(interaction.data, dict)
                else None
            )
        except AttributeError:
            resolved_channels = None

        try:
            target_channel = await _resolve_text_channel(
                bot,
                interaction.guild,
                channel,
                resolved=resolved_channels,
            )
        except ChannelResolutionError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await manager.set_logger_channel(target_channel.id)
        await interaction.followup.send(
            f"Logger channel set to {target_channel.mention}.", ephemeral=True
        )

    @bot.tree.command(
        name="giveaway-add-admin-role",
        description="Allow a role to manage giveaways.",
    )
    @app_commands.describe(role="Role to grant giveaway management permissions.")
    async def giveaway_add_admin_role(
        interaction: discord.Interaction, role: discord.Role
    ) -> None:
        error = await admin_required(interaction, manager)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        added = await manager.add_admin_role(role.id)
        if not added:
            await interaction.followup.send(
                f"{role.mention} already has giveaway admin permissions.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Granted giveaway admin permissions to {role.mention}.",
            ephemeral=True,
        )

    @bot.tree.command(
        name="giveaway-remove-admin-role",
        description="Revoke giveaway management permissions from a role.",
    )
    @app_commands.describe(role="Role to revoke giveaway management permissions from.")
    async def giveaway_remove_admin_role(
        interaction: discord.Interaction, role: discord.Role
    ) -> None:
        error = await admin_required(interaction, manager)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        removed = await manager.remove_admin_role(role.id)
        if not removed:
            await interaction.followup.send(
                f"{role.mention} does not have giveaway admin permissions.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Removed giveaway admin permissions from {role.mention}.",
            ephemeral=True,
        )

    @bot.tree.command(
        name="giveaway-list-admin-roles",
        description="List all roles allowed to manage giveaways.",
    )
    async def giveaway_list_admin_roles(interaction: discord.Interaction) -> None:
        error = await admin_required(interaction, manager)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a guild.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        role_ids = await manager.list_admin_roles()
        if not role_ids:
            await interaction.followup.send(
                "No extra giveaway admin roles are configured.", ephemeral=True
            )
            return

        lines = []
        for role_id in role_ids:
            role = interaction.guild.get_role(role_id)
            if role is not None:
                lines.append(f"- {role.mention} ({role_id})")
            else:
                lines.append(f"- Unknown role ID `{role_id}`")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @bot.tree.command(
        name="giveaway-enable", description="Enable automatic scheduled giveaways."
    )
    async def giveaway_enable(interaction: discord.Interaction) -> None:
        error = await admin_required(interaction, manager)
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
        error = await admin_required(interaction, manager)
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
