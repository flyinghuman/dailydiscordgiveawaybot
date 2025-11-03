from __future__ import annotations

import argparse
import asyncio
import enum
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
ENV_PATH = Path(".env")


class SettingsSetKey(enum.Enum):
    TIMEZONE = "timezone"
    RECENT_WINNER_DAYS = "recent_winner_days"


class SettingsToggleKey(enum.Enum):
    RECENT_WINNER_COOLDOWN = "recent_winner_cooldown"
    AUTO_DAILY = "auto_daily"


def _load_env_file(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if value and ((value[0] == value[-1]) and value.startswith(("'", '"'))):
            value = value[1:-1]
        os.environ.setdefault(key, value)


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
        await self.manager.audit_overdue()
        self._scheduled_checker.start()
        await self.tree.sync()
        dev_guild_id = self.config.permissions.development_guild_id
        if dev_guild_id:
            guild = discord.Object(dev_guild_id)
            await self.tree.sync(guild=guild)

    @tasks.loop(minutes=1)
    async def _scheduled_checker(self) -> None:
        await self.manager.handle_scheduled()
        await self.manager.audit_overdue()

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
    _load_env_file()
    config = load_config(config_path)
    configure_logging(config.logging.level)
    storage = StateStorage(Path("data") / "state.json")
    try:
        return GiveawayBot(config, storage)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"Invalid timezone configured: {config.default_timezone}") from exc


def _parse_end_time(value: str, tz: ZoneInfo) -> datetime:
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            naive = datetime.strptime(value, fmt)
            return naive.replace(tzinfo=tz).astimezone(UTC)
        except ValueError:
            continue
    raise ValueError("Time must be in 'HH:MM' (24h) format.")


def register_commands(bot: GiveawayBot) -> None:
    manager = bot.manager

    settings_group = app_commands.Group(
        name="giveaway-settings",
        description="Manage giveaway-wide settings.",
    )

    @settings_group.command(name="set", description="Set a giveaway configuration value.")
    @app_commands.describe(
        setting="Which setting to update.",
        value="New value for the chosen setting. For cooldown days provide an integer. For Time zone provide a valid IANA timezone string (e.g., 'Europe/Berlin').",
    )
    async def settings_set(
        interaction: discord.Interaction, setting: SettingsSetKey, value: str
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

        if setting is SettingsSetKey.TIMEZONE:
            timezone_value = value.strip()
            try:
                await manager.set_timezone(interaction.guild.id, timezone_value)
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            tz = manager.get_timezone(interaction.guild.id)
            current_local = datetime.now(tz=tz)
            await interaction.response.send_message(
                f"Timezone updated to `{timezone_value}`. Local time is now {current_local:%Y-%m-%d %H:%M %Z}.",
                ephemeral=True,
            )
            return

        if setting is SettingsSetKey.RECENT_WINNER_DAYS:
            try:
                days = int(value.strip())
            except ValueError:
                await interaction.response.send_message(
                    "Cooldown days must be a whole number.", ephemeral=True
                )
                return
            if days < 0:
                await interaction.response.send_message(
                    "Cooldown days must be zero or greater.", ephemeral=True
                )
                return
            await manager.set_recent_winner_cooldown_days(interaction.guild.id, days)
            snapshot = await manager.get_settings_snapshot(interaction.guild.id)
            status = (
                "enabled"
                if snapshot["recent_winner_cooldown_enabled"]
                else "disabled"
            )
            await interaction.response.send_message(
                f"Recent winner cooldown threshold set to {days} day(s). "
                f"The feature is currently {status}.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Unsupported setting selected.", ephemeral=True
        )

    @settings_group.command(name="get", description="Show current giveaway settings.")
    async def settings_get(interaction: discord.Interaction) -> None:
        error = await admin_required(interaction, manager)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a guild.", ephemeral=True
            )
            return
        snapshot = await manager.get_settings_snapshot(interaction.guild.id)
        cooldown_state = (
            f"Enabled ({snapshot['recent_winner_cooldown_days']} day cooldown)"
            if snapshot["recent_winner_cooldown_enabled"]
            else f"Disabled ({snapshot['recent_winner_cooldown_days']} day threshold)"
        )
        message = (
            "Current giveaway settings:\n"
            f"- Timezone: `{snapshot['timezone']}`\n"
            f"- Daily automation: {'Enabled' if snapshot['auto_enabled'] else 'Disabled'}\n"
            f"- Recent winner cooldown: {cooldown_state}"
        )
        await interaction.response.send_message(message, ephemeral=True)

    @settings_group.command(
        name="enable", description="Enable a giveaway feature toggle."
    )
    @app_commands.describe(feature="Feature to enable.")
    async def settings_enable(
        interaction: discord.Interaction, feature: SettingsToggleKey
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

        snapshot_before = await manager.get_settings_snapshot(interaction.guild.id)

        if feature is SettingsToggleKey.RECENT_WINNER_COOLDOWN:
            changed = await manager.set_recent_winner_cooldown_enabled(
                interaction.guild.id, True
            )
            if changed:
                snapshot = await manager.get_settings_snapshot(interaction.guild.id)
                await interaction.response.send_message(
                    f"Recent winner cooldown enabled ({snapshot['recent_winner_cooldown_days']} day threshold).",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "Recent winner cooldown is already enabled.", ephemeral=True
                )
            return

        if feature is SettingsToggleKey.AUTO_DAILY:
            if snapshot_before["auto_enabled"]:
                await interaction.response.send_message(
                    "Daily automation is already enabled.", ephemeral=True
                )
                return
            await manager.toggle_auto(interaction.guild.id, True)
            await interaction.response.send_message(
                "Daily automation has been enabled.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Unsupported feature selected.", ephemeral=True
        )

    @settings_group.command(
        name="disable", description="Disable a giveaway feature toggle."
    )
    @app_commands.describe(feature="Feature to disable.")
    async def settings_disable(
        interaction: discord.Interaction, feature: SettingsToggleKey
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

        snapshot_before = await manager.get_settings_snapshot(interaction.guild.id)

        if feature is SettingsToggleKey.RECENT_WINNER_COOLDOWN:
            changed = await manager.set_recent_winner_cooldown_enabled(
                interaction.guild.id, False
            )
            if changed:
                await interaction.response.send_message(
                    "Recent winner cooldown disabled.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "Recent winner cooldown is already disabled.", ephemeral=True
                )
            return

        if feature is SettingsToggleKey.AUTO_DAILY:
            if not snapshot_before["auto_enabled"]:
                await interaction.response.send_message(
                    "Daily automation is already disabled.", ephemeral=True
                )
                return
            await manager.toggle_auto(interaction.guild.id, False)
            await interaction.response.send_message(
                "Daily automation has been disabled.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Unsupported feature selected.", ephemeral=True
        )

    bot.tree.add_command(settings_group)

    @bot.tree.command(name="giveaway-start", description="Start a new giveaway.")
    @app_commands.describe(
        channel="Channel where the giveaway embed should be posted (mention, ID, or name).",
        winners="Number of winners to draw.",
        title="Title for the giveaway embed.",
        description="Description for the giveaway.",
        start="Start time in HH:MM (24h) to begin the giveaway.",
        end="End time in HH:MM (24h) to finish the giveaway.",
        run_daily="Automatically repeat this giveaway every day at the provided times.",
    )
    async def giveaway_start(
        interaction: discord.Interaction,
        channel: str,
        winners: app_commands.Range[int, 1, 100],
        title: str,
        description: str,
        start: str,
        end: str,
        run_daily: bool = False,
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

        try:
            start_hhmm = datetime.strptime(start.strip(), "%H:%M").time()
            end_hhmm = datetime.strptime(end.strip(), "%H:%M").time()
        except ValueError:
            await interaction.response.send_message(
                "Start and end times must follow HH:MM (24h) format.", ephemeral=True
            )
            return

        tz = manager.get_timezone(interaction.guild.id)
        now_local = datetime.now(tz=tz)
        tolerance = timedelta(minutes=1)
        scheduled_start_local = datetime.combine(
            now_local.date(), start_hhmm, tzinfo=tz
        )
        if scheduled_start_local + tolerance < now_local:
            scheduled_start_local += timedelta(days=1)
        immediate = (scheduled_start_local - now_local) <= tolerance
        effective_start_local = now_local if immediate else scheduled_start_local

        scheduled_end_local = datetime.combine(
            scheduled_start_local.date(), end_hhmm, tzinfo=tz
        )
        if scheduled_end_local <= scheduled_start_local:
            scheduled_end_local += timedelta(days=1)
        if immediate and scheduled_end_local <= effective_start_local:
            scheduled_end_local += timedelta(days=1)

        await interaction.response.defer(ephemeral=True)

        if run_daily:
            started_now = None
            if immediate:
                started_now = await manager.start_giveaway(
                    guild=interaction.guild,  # type: ignore[arg-type]
                    channel=target_channel,
                    winners=winners,
                    title=title,
                    description=description,
                    end_time=scheduled_end_local.astimezone(UTC),
                )
            recurring = await manager.schedule_recurring_giveaway(
                interaction.guild,  # type: ignore[arg-type]
                target_channel,
                winners=winners,
                title=title,
                description=description,
                start_local=scheduled_start_local,
                end_local=scheduled_end_local,
                immediate_started=immediate,
            )

            lines = []
            if started_now:
                lines.append(
                    f"Giveaway `{started_now.id}` started immediately in {target_channel.mention} "
                    f"and will end at {started_now.end_time.astimezone(tz):%Y-%m-%d %H:%M %Z}."
                )
            else:
                lines.append(
                    f"Recurring giveaway scheduled for {scheduled_start_local:%Y-%m-%d %H:%M %Z} in {target_channel.mention}."
                )
            next_run = recurring.next_start.astimezone(tz)
            lines.append(
                f"Recurring schedule ID: `{recurring.id}`. Next run: {next_run:%Y-%m-%d %H:%M %Z}."
            )
            lines.append(
                "Use /giveaway-disable with the schedule ID to pause daily runs."
            )
            await interaction.followup.send("\n".join(lines), ephemeral=True)
            return

        if immediate:
            giveaway = await manager.start_giveaway(
                guild=interaction.guild,  # type: ignore[arg-type]
                channel=target_channel,
                winners=winners,
                title=title,
                description=description,
                end_time=scheduled_end_local.astimezone(UTC),
            )
            await interaction.followup.send(
                f"Giveaway `{giveaway.id}` started immediately in {target_channel.mention} "
                f"and will end at {giveaway.end_time.astimezone(tz):%Y-%m-%d %H:%M %Z}.",
                ephemeral=True,
            )
        else:
            pending = await manager.schedule_manual_giveaway(
                interaction.guild,  # type: ignore[arg-type]
                target_channel,
                winners=winners,
                title=title,
                description=description,
                start_time=scheduled_start_local,
                end_time=scheduled_end_local,
            )
            await interaction.followup.send(
                f"Giveaway **{title}** scheduled for {scheduled_start_local:%Y-%m-%d %H:%M %Z} "
                f"in {target_channel.mention} and set to end at "
                f"{scheduled_end_local:%Y-%m-%d %H:%M %Z}. (Schedule ID: {pending.id})",
                ephemeral=True,
            )

    @bot.tree.command(name="giveaway-end", description="End a giveaway immediately.")
    @app_commands.describe(giveaway_id="Identifier of the giveaway to end.")
    async def giveaway_end(interaction: discord.Interaction, giveaway_id: str) -> None:
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
        giveaway = await manager.end_giveaway(interaction.guild.id, giveaway_id)
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

        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a guild.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        tz = manager.get_timezone(interaction.guild.id)
        new_end_time = None
        if end_time:
            try:
                new_end_time = _parse_end_time(end_time, tz)
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
                interaction.guild.id,
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
        giveaways = await manager.list_giveaways(interaction.guild.id)
        if not giveaways:
            await interaction.followup.send(
                "No giveaways have been created yet.", ephemeral=True
            )
            return
        tz = manager.get_timezone(interaction.guild.id)
        lines = []
        for giveaway in giveaways:
            status = "Active" if giveaway.is_active else "Finished"
            end_time = giveaway.end_time.astimezone(tz).strftime(
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
        if not interaction.guild:
            await interaction.followup.send(
                "This command can only be used in a guild.", ephemeral=True
            )
            return
        giveaway = await manager.get_giveaway(interaction.guild.id, giveaway_id)
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

        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a guild.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        giveaway = await manager.get_giveaway(interaction.guild.id, giveaway_id)
        if not giveaway:
            await interaction.followup.send("Giveaway not found.", ephemeral=True)
            return
        if giveaway.is_active:
            await interaction.followup.send(
                "End the giveaway before rerolling winners.", ephemeral=True
            )
            return

        winners = await manager.reroll(interaction.guild.id, giveaway_id)
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
        await manager.set_logger_channel(interaction.guild.id, target_channel.id)
        await interaction.followup.send(
            f"Logger channel set to {target_channel.mention}.", ephemeral=True
        )

    @bot.tree.command(
        name="giveaway-cleanup",
        description="Remove finished giveaways from the bot history.",
    )
    async def giveaway_cleanup(interaction: discord.Interaction) -> None:
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
        removed = await manager.cleanup_finished(interaction.guild.id)
        if removed == 0:
            await interaction.followup.send(
                "No finished giveaways found to remove.", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"Removed {removed} finished giveaway(s) from history.", ephemeral=True
            )

    @bot.tree.command(
        name="giveaway-show", description="Display details about a giveaway."
    )
    @app_commands.describe(giveaway_id="Identifier of the giveaway to display.")
    async def giveaway_show(
        interaction: discord.Interaction, giveaway_id: str
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

        await interaction.response.defer(ephemeral=True)

        giveaway = await manager.get_giveaway(interaction.guild.id, giveaway_id)
        if giveaway:
            status_text = "Active" if giveaway.is_active else "Finished"
            color = discord.Color.blue() if giveaway.is_active else discord.Color.dark_gray()
            description_text = giveaway.description or "No description provided."
            embed = discord.Embed(
                title=giveaway.title,
                description=description_text,
                color=color,
            )
            embed.add_field(name="Status", value=status_text, inline=True)
            embed.add_field(name="Channel", value=f"<#{giveaway.channel_id}>", inline=True)
            embed.add_field(name="Planned Winners", value=str(giveaway.winners), inline=True)
            embed.add_field(
                name="Participant Count", value=str(len(giveaway.participants)), inline=True
            )

            tz = manager.get_timezone(interaction.guild.id)
            created_local = giveaway.created_at.astimezone(tz)
            embed.add_field(
                name="Created At",
                value=created_local.strftime("%Y-%m-%d %H:%M %Z"),
                inline=False,
            )
            end_local = giveaway.end_time.astimezone(tz)
            embed.add_field(
                name="Ends At", value=end_local.strftime("%Y-%m-%d %H:%M %Z"), inline=False
            )

            if giveaway.scheduled_id:
                embed.add_field(
                    name="Scheduled Source", value=giveaway.scheduled_id, inline=False
                )

            if not giveaway.is_active:
                if giveaway.last_announced_winners:
                    winners_text = " ".join(
                        f"<@{winner_id}>" for winner_id in giveaway.last_announced_winners
                    )
                else:
                    winners_text = "No winners were selected."
                embed.add_field(name="Winner(s)", value=winners_text, inline=False)

            max_preview = 20
            if giveaway.participants:
                preview_lines = [
                    f"- <@{user_id}>" for user_id in giveaway.participants[:max_preview]
                ]
                remaining = len(giveaway.participants) - max_preview
                if remaining > 0:
                    preview_lines.append(f"... and {remaining} more.")
                participants_value = "\n".join(preview_lines)
            else:
                participants_value = "No participants yet."

            embed.add_field(
                name="Participants",
                value=participants_value,
                inline=False,
            )

            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        pending = await manager.get_pending_giveaway(interaction.guild.id, giveaway_id)
        if pending:
            embed = discord.Embed(
                title=pending.title,
                description=pending.description or "No description provided.",
                color=discord.Color.orange(),
            )
            embed.add_field(name="Status", value="Scheduled", inline=True)
            embed.add_field(name="Channel", value=f"<#{pending.channel_id}>", inline=True)
            embed.add_field(name="Planned Winners", value=str(pending.winners), inline=True)

            tz = manager.get_timezone(interaction.guild.id)
            start_local = pending.start_time.astimezone(tz)
            end_local = pending.end_time.astimezone(tz)
            embed.add_field(
                name="Starts At", value=start_local.strftime("%Y-%m-%d %H:%M %Z"), inline=False
            )
            embed.add_field(
                name="Ends At", value=end_local.strftime("%Y-%m-%d %H:%M %Z"), inline=False
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        recurring = await manager.get_recurring_giveaway(
            interaction.guild.id, giveaway_id
        )
        if recurring:
            tz = manager.get_timezone(interaction.guild.id)
            start_local = datetime.combine(
                datetime.now(tz=tz).date(), recurring.start_time, tzinfo=tz
            )
            end_local = datetime.combine(
                start_local.date(), recurring.end_time, tzinfo=tz
            )
            if end_local <= start_local:
                end_local += timedelta(days=1)
            next_run = recurring.next_start.astimezone(tz)
            next_end = recurring.next_end.astimezone(tz)
            embed = discord.Embed(
                title=recurring.title,
                description=recurring.description or "No description provided.",
                color=discord.Color.green() if recurring.enabled else discord.Color.dark_gray(),
            )
            embed.add_field(name="Status", value="Enabled" if recurring.enabled else "Disabled", inline=True)
            embed.add_field(name="Channel", value=f"<#{recurring.channel_id}>", inline=True)
            embed.add_field(name="Planned Winners", value=str(recurring.winners), inline=True)
            embed.add_field(
                name="Daily Window",
                value=f"Starts {recurring.start_time.strftime('%H:%M')} - Ends {recurring.end_time.strftime('%H:%M')} ({getattr(tz, 'key', str(tz))})",
                inline=False,
            )
            embed.add_field(
                name="Next Run",
                value=f"{next_run:%Y-%m-%d %H:%M %Z} → {next_end:%Y-%m-%d %H:%M %Z}",
                inline=False,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        await interaction.followup.send("Giveaway not found.", ephemeral=True)

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
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a guild.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        added = await manager.add_admin_role(interaction.guild.id, role.id)
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
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a guild.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        removed = await manager.remove_admin_role(interaction.guild.id, role.id)
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
        role_ids = await manager.list_admin_roles(interaction.guild.id)
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
        name="giveaway-enable",
        description="Enable a recurring giveaway schedule.",
    )
    @app_commands.describe(
        schedule_id="Identifier of the recurring giveaway schedule to enable."
    )
    async def giveaway_enable(
        interaction: discord.Interaction, schedule_id: str
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

        await interaction.response.defer(ephemeral=True)
        result = await manager.enable_recurring(interaction.guild.id, schedule_id.strip())
        if result == "not_found":
            await interaction.followup.send(
                "No recurring giveaway with that ID was found for this server.",
                ephemeral=True,
            )
            return
        if result == "already_enabled":
            await interaction.followup.send(
                "That recurring giveaway is already enabled.", ephemeral=True
            )
            return

        recurring = await manager.get_recurring_giveaway(
            interaction.guild.id, schedule_id.strip()
        )
        tz = manager.get_timezone(interaction.guild.id)
        next_run = (
            recurring.next_start.astimezone(tz)
            if recurring
            else None
        )
        message = "Recurring giveaway enabled."
        if next_run:
            message += f" Next run: {next_run:%Y-%m-%d %H:%M %Z}."
        await interaction.followup.send(message, ephemeral=True)

    @bot.tree.command(
        name="giveaway-disable",
        description="Disable a recurring giveaway schedule.",
    )
    @app_commands.describe(
        schedule_id="Identifier of the recurring giveaway schedule to disable."
    )
    async def giveaway_disable(
        interaction: discord.Interaction, schedule_id: str
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

        await interaction.response.defer(ephemeral=True)
        result = await manager.disable_recurring(
            interaction.guild.id, schedule_id.strip()
        )
        if result == "not_found":
            await interaction.followup.send(
                "No recurring giveaway with that ID was found for this server.",
                ephemeral=True,
            )
            return
        if result == "already_disabled":
            await interaction.followup.send(
                "That recurring giveaway is already disabled.", ephemeral=True
            )
            return
        await interaction.followup.send(
            "Recurring giveaway disabled. Use /giveaway-enable to resume it.",
            ephemeral=True,
        )


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
