from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import UTC, datetime, timedelta, time
from typing import Dict, Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from .config import Config, ScheduledGiveawayConfig
from .models import (
    BotState,
    Giveaway,
    GuildState,
    PendingGiveaway,
    RecentWinner,
    RecurringGiveaway,
)
from .storage import StateStorage
from .views import GiveawayView

log = logging.getLogger(__name__)


class GiveawayManager:
    """Coordinates giveaway lifecycle, persistence, and Discord interactions."""

    def __init__(
        self, bot: discord.Client, config: Config, storage: StateStorage
    ) -> None:
        self.bot = bot
        self.config = config
        self.storage = storage
        self._default_timezone = config.default_timezone
        self.state = BotState()
        self._finish_tasks: Dict[str, asyncio.Task] = {}
        self._pending_tasks: Dict[str, asyncio.Task] = {}
        self._recurring_tasks: Dict[str, asyncio.Task] = {}
        self._state_lock = asyncio.Lock()

    async def load(self) -> None:
        try:
            self.state = await self.storage.load()
        except Exception as exc:
            log.exception(
                "Failed to load persisted state, starting with defaults: %s", exc
            )
            self.state = BotState()

        default_admin_roles = list(self.config.permissions.admin_roles or [])
        updated = False
        for _, guild_state in self.state.iter_guild_states():
            auto_enabled = (
                self.config.scheduling.auto_enabled and guild_state.auto_enabled
            )
            if guild_state.auto_enabled != auto_enabled:
                guild_state.auto_enabled = auto_enabled
                updated = True
            if not guild_state.admin_roles and default_admin_roles:
                guild_state.admin_roles = list(default_admin_roles)
                updated = True
            tz_name = guild_state.timezone or self._default_timezone
            try:
                ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                tz_name = self._default_timezone
                updated = True
            if guild_state.timezone != tz_name:
                guild_state.timezone = tz_name
                updated = True
            if guild_state.recent_winner_cooldown_days < 0:
                guild_state.recent_winner_cooldown_days = 0
                updated = True
            if guild_state.recent_winners:
                retention_days = max(guild_state.recent_winner_cooldown_days, 30)
                cutoff = datetime.now(tz=UTC) - timedelta(days=retention_days)
                filtered_recent = [
                    entry for entry in guild_state.recent_winners if entry.won_at >= cutoff
                ]
                if len(filtered_recent) != len(guild_state.recent_winners):
                    guild_state.recent_winners = filtered_recent
                    updated = True

        if updated:
            await self.save_state()

        await self._restore_pending_giveaways()
        await self._restore_active_giveaways()
        await self._restore_recurring_giveaways()

    async def _restore_pending_giveaways(self) -> None:
        async with self._state_lock:
            pending_items = [
                pending
                for _, guild_state in self.state.iter_guild_states()
                for pending in guild_state.pending_giveaways
            ]
        for pending in pending_items:
            await self._schedule_start(pending, reschedule=False)

    async def _restore_active_giveaways(self) -> None:
        async with self._state_lock:
            active_items = [
                giveaway
                for _, guild_state in self.state.iter_guild_states()
                for giveaway in guild_state.giveaways
                if giveaway.is_active
            ]
        for giveaway in active_items:
            await self._register_view(giveaway)
            await self._schedule_finish(giveaway)

    async def _restore_recurring_giveaways(self) -> None:
        async with self._state_lock:
            recurring_items = [
                recurring
                for _, guild_state in self.state.iter_guild_states()
                for recurring in guild_state.recurring_giveaways
                if recurring.enabled
            ]
        for recurring in recurring_items:
            await self._schedule_recurring(recurring, reschedule=False)

    async def save_state(self) -> None:
        await self.storage.save(self.state)

    def _ensure_guild_state(self, guild_id: int) -> GuildState:
        state = self.state.ensure_guild_state(
            guild_id, default_admin_roles=self.config.permissions.admin_roles
        )
        if not state.timezone:
            state.timezone = self._default_timezone
        return state

    def get_timezone(self, guild_id: int) -> ZoneInfo:
        state = self._ensure_guild_state(guild_id)
        tz_name = state.timezone or self._default_timezone or "Europe/Berlin"
        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            state.timezone = "Europe/Berlin"
            return ZoneInfo("Europe/Berlin")

    async def _get_recent_winner_blocklist(self, guild_id: int) -> set[int]:
        blocked: set[int] = set()
        needs_save = False
        async with self._state_lock:
            guild_state = self._ensure_guild_state(guild_id)
            cooldown_enabled = guild_state.recent_winner_cooldown_enabled
            cooldown_days = max(guild_state.recent_winner_cooldown_days, 0)
            now = datetime.now(tz=UTC)
            retention_days = max(cooldown_days, 30)
            retention_cutoff = now - timedelta(days=retention_days)

            retained: list[RecentWinner] = []
            if cooldown_enabled and cooldown_days > 0:
                cooldown_cutoff = now - timedelta(days=cooldown_days)
            else:
                cooldown_cutoff = None

            for entry in guild_state.recent_winners:
                if entry.won_at >= retention_cutoff:
                    retained.append(entry)
                    if cooldown_cutoff and entry.won_at >= cooldown_cutoff:
                        blocked.add(entry.user_id)
                else:
                    needs_save = True

            if len(retained) != len(guild_state.recent_winners):
                guild_state.recent_winners = retained
                needs_save = True

            if not (cooldown_enabled and cooldown_days > 0):
                blocked.clear()

        if needs_save:
            await self.save_state()

        return blocked

    async def _record_recent_winners(
        self, guild_id: int, winners: Iterable[int], giveaway_id: str
    ) -> None:
        winners_list = [int(winner) for winner in winners if winner is not None]
        if not winners_list:
            return

        needs_save = False
        now = datetime.now(tz=UTC)
        async with self._state_lock:
            guild_state = self._ensure_guild_state(guild_id)
            retention_days = max(guild_state.recent_winner_cooldown_days, 30)
            retention_cutoff = now - timedelta(days=retention_days)

            retained = [
                entry for entry in guild_state.recent_winners if entry.won_at >= retention_cutoff
            ]
            if len(retained) != len(guild_state.recent_winners):
                needs_save = True
            for winner_id in winners_list:
                retained.append(
                    RecentWinner(
                        user_id=winner_id,
                        giveaway_id=giveaway_id,
                        won_at=now,
                    )
                )
                needs_save = True
            guild_state.recent_winners = retained

        if needs_save:
            await self.save_state()

    def is_admin(
        self,
        member: discord.Member,
        *,
        guild_owner_id: Optional[int] = None,
        base_permissions: Optional[discord.Permissions] = None,
        role_ids: Optional[Iterable[int]] = None,
    ) -> bool:
        owner_id = guild_owner_id
        if owner_id is None:
            guild = getattr(member, "guild", None)
            if guild is not None:
                owner_id = getattr(guild, "owner_id", None) or getattr(
                    guild, "_owner_id", None
                )
        if owner_id is not None and owner_id == member.id:
            log.debug("Member %s is guild owner; treating as giveaway admin.", member.id)
            return True

        permissions_obj = base_permissions
        if permissions_obj is None:
            try:
                permissions_obj = member.guild_permissions
            except AttributeError:
                permissions_obj = None
        if permissions_obj is None:
            raw_permissions = getattr(member, "_permissions", None)
            if raw_permissions is not None:
                try:
                    permissions_obj = discord.Permissions(int(raw_permissions))
                except (TypeError, ValueError):
                    permissions_obj = None
        if permissions_obj and (
            permissions_obj.administrator or permissions_obj.manage_guild
        ):
            log.debug(
                "Member %s has administrative permissions; treating as giveaway admin.",
                member.id,
            )
            return True

        effective_role_ids: set[int] = set()
        if role_ids is not None:
            for role_id in role_ids:
                try:
                    effective_role_ids.add(int(role_id))
                except (TypeError, ValueError):
                    continue

        if not effective_role_ids:
            try:
                for role in member.roles:
                    effective_role_ids.add(role.id)
            except AttributeError:
                pass
            if not effective_role_ids:
                raw_roles = getattr(member, "_roles", None)
                if raw_roles:
                    for role_id in raw_roles:
                        try:
                            effective_role_ids.add(int(role_id))
                        except (TypeError, ValueError):
                            continue

        guild_context = getattr(member, "guild", None)
        admin_roles: set[int] = set()
        if guild_context is not None:
            guild_state = self.state.get_guild_state(guild_context.id)
            if guild_state and guild_state.admin_roles:
                admin_roles.update(int(r) for r in guild_state.admin_roles)
        if not admin_roles and self.config.permissions.admin_roles:
            admin_roles.update(int(r) for r in self.config.permissions.admin_roles)
        if not admin_roles:
            log.debug("No giveaway admin roles configured; denying member %s.", member.id)
            return False

        matching_roles = sorted(admin_roles.intersection(effective_role_ids))
        if matching_roles:
            log.debug(
                "Member %s matched giveaway admin role(s) %s.",
                member.id,
                matching_roles,
            )
            return True

        log.debug(
            "Member %s lacks required giveaway admin roles %s (has %s).",
            member.id,
            sorted(admin_roles),
            sorted(effective_role_ids),
        )
        return False

    async def schedule_manual_giveaway(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        *,
        winners: int,
        title: str,
        description: str,
        start_time: datetime,
        end_time: datetime,
    ) -> PendingGiveaway:
        if winners <= 0:
            raise ValueError("winners must be greater than zero")
        if end_time <= start_time:
            raise ValueError("end_time must be after start_time")

        self._ensure_guild_state(guild.id)

        pending = PendingGiveaway(
            id=self._generate_giveaway_id(),
            guild_id=guild.id,
            channel_id=channel.id,
            winners=winners,
            title=title,
            description=description,
            start_time=start_time.astimezone(UTC),
            end_time=end_time.astimezone(UTC),
        )
        async with self._state_lock:
            self._ensure_guild_state(pending.guild_id)
            self.state.upsert_pending(pending.guild_id, pending)
            await self.save_state()

        await self._schedule_start(pending)
        await self._notify_logger(
            f"Giveaway **{pending.title}** scheduled for <#{pending.channel_id}> at "
            f"{pending.start_time.astimezone(self.get_timezone(pending.guild_id)):%Y-%m-%d %H:%M %Z}.",
            guild_id=pending.guild_id,
        )
        return pending

    async def schedule_recurring_giveaway(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        *,
        winners: int,
        title: str,
        description: str,
        start_local: datetime,
        end_local: datetime,
        immediate_started: bool,
    ) -> "RecurringGiveaway":
        schedule_id = f"R{self._generate_giveaway_id()}"
        self._ensure_guild_state(guild.id)
        tz = self.get_timezone(guild.id)
        base_start_time = start_local.astimezone(tz).time()
        base_end_time = end_local.astimezone(tz).time()

        if immediate_started:
            next_start_local, next_end_local = self._compute_next_window(
                guild.id, base_start_time, base_end_time, reference=start_local
            )
        else:
            if start_local <= datetime.now(tz=tz):
                next_start_local, next_end_local = self._compute_next_window(
                    guild.id, base_start_time, base_end_time, reference=None
                )
            else:
                next_start_local = start_local.astimezone(UTC)
                next_end_local = end_local.astimezone(UTC)

        recurring = RecurringGiveaway(
            id=schedule_id,
            guild_id=guild.id,
            channel_id=channel.id,
            winners=winners,
            title=title,
            description=description,
            start_time=base_start_time,
            end_time=base_end_time,
            next_start=next_start_local,
            next_end=next_end_local,
            enabled=True,
        )

        async with self._state_lock:
            self._ensure_guild_state(guild.id)
            self.state.upsert_recurring(guild.id, recurring)
            await self.save_state()

        await self._schedule_recurring(recurring)
        await self._notify_logger(
            f"Recurring giveaway `{recurring.id}` configured for <#{recurring.channel_id}>.",
            guild_id=guild.id,
        )
        return recurring

    async def start_giveaway(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        *,
        winners: int,
        title: str,
        description: str,
        end_time: datetime,
        scheduled_id: Optional[str] = None,
    ) -> Giveaway:
        if winners <= 0:
            raise ValueError("winners must be greater than zero")

        giveaway_id = self._generate_giveaway_id()
        view = self._build_view(giveaway_id)
        tz = self.get_timezone(guild.id)
        embed = self._build_embed(
            giveaway=None,
            title=title,
            description=description,
            winners=winners,
            participants=0,
            end_time=end_time,
            status="Active",
            tz=tz,
        )
        message = await channel.send(embed=embed, view=view)
        self.bot.add_view(view, message_id=message.id)

        giveaway = Giveaway(
            id=giveaway_id,
            guild_id=guild.id,
            channel_id=channel.id,
            message_id=message.id,
            winners=winners,
            title=title,
            description=description,
            end_time=end_time.astimezone(UTC),
            created_at=datetime.now(tz=UTC),
            scheduled_id=scheduled_id,
        )

        async with self._state_lock:
            self._ensure_guild_state(giveaway.guild_id)
            self.state.upsert_giveaway(giveaway)
            await self.save_state()

        await self._schedule_finish(giveaway)

        await message.edit(embed=self._embed_from_giveaway(giveaway), view=view)

        await self._notify_logger(
            f"Giveaway **{giveaway.title}** (`{giveaway.id}`) started in <#{giveaway.channel_id}>.",
            guild_id=giveaway.guild_id,
        )
        return giveaway

    async def end_giveaway(
        self, guild_id: int, giveaway_id: str, *, notify: bool = True
    ) -> Optional[Giveaway]:
        async with self._state_lock:
            giveaway = self.state.get_giveaway(guild_id, giveaway_id)
            if not giveaway:
                return None
            if not giveaway.is_active:
                return giveaway
            giveaway.is_active = False
            await self.save_state()

        task = self._finish_tasks.pop(giveaway_id, None)
        current = asyncio.current_task()
        if task and task is not current:
            task.cancel()
        await self._finalize_giveaway(giveaway, notify=notify)
        return giveaway

    async def _finalize_giveaway(self, giveaway: Giveaway, *, notify: bool) -> None:
        channel = await self._fetch_text_channel(giveaway.channel_id)
        if not channel:
            log.warning(
                "Unable to locate channel %s for giveaway %s",
                giveaway.channel_id,
                giveaway.id,
            )
            return

        winners = await self._choose_winners(giveaway)
        giveaway.last_announced_winners = winners

        embed = self._embed_from_giveaway(giveaway, status="Finished", winners=winners)
        message = await self._fetch_message(channel, giveaway.message_id)
        if message:
            await message.edit(embed=embed, view=None)

        if winners and notify:
            mentions = " ".join(f"<@{winner_id}>" for winner_id in winners)
            await channel.send(
                f"🎉 Giveaway **{giveaway.title}** has ended! Congratulations to {mentions}!"
            )
        else:
            if notify:
                await channel.send(
                    f"Giveaway **{giveaway.title}** ended without enough participants."
                )
            giveaway.participants.clear()

        await self._record_recent_winners(giveaway.guild_id, winners, giveaway.id)
        await self.save_state()
        if winners:
            winner_mentions = ", ".join(f"<@{winner_id}>" for winner_id in winners)
            log_message = (
                f"Giveaway **{giveaway.title}** (`{giveaway.id}`) finished with "
                f"{len(winners)} winner(s): {winner_mentions}."
            )
        else:
            log_message = (
                f"Giveaway **{giveaway.title}** (`{giveaway.id}`) finished with no winners."
            )
        await self._notify_logger(log_message, guild_id=giveaway.guild_id)

    async def add_participant(
        self, guild_id: int, giveaway_id: str, user: discord.Member
    ) -> str:
        async with self._state_lock:
            giveaway = self.state.get_giveaway(guild_id, giveaway_id)
            if not giveaway:
                return "This giveaway is no longer available."
            if not giveaway.is_active:
                return "This giveaway has already finished."
            if user.id in giveaway.participants:
                return "You have already joined this giveaway."
            giveaway.participants.append(user.id)
            await self.save_state()

        await self._update_embed(giveaway)
        await self._notify_logger(
            f"{user.mention} joined giveaway `{giveaway.id}`.", guild_id=guild_id
        )
        return "You're in! Good luck!"

    async def remove_participant(
        self, guild_id: int, giveaway_id: str, user: discord.Member
    ) -> str:
        async with self._state_lock:
            giveaway = self.state.get_giveaway(guild_id, giveaway_id)
            if not giveaway:
                return "This giveaway is no longer available."
            if not giveaway.is_active:
                return "This giveaway has already finished."
            if user.id not in giveaway.participants:
                return "You are not part of this giveaway."
            giveaway.participants.remove(user.id)
            await self.save_state()

        await self._update_embed(giveaway)
        await self._notify_logger(
            f"{user.mention} left giveaway `{giveaway.id}`.", guild_id=guild_id
        )
        return "You've left the giveaway."

    async def list_giveaways(self, guild_id: int) -> Iterable[Giveaway]:
        async with self._state_lock:
            return list(self.state.list_all(guild_id))

    async def get_pending_giveaway(
        self, guild_id: int, pending_id: str
    ) -> Optional[PendingGiveaway]:
        async with self._state_lock:
            return self.state.get_pending(guild_id, pending_id)

    async def cleanup_finished(self, guild_id: int) -> int:
        async with self._state_lock:
            guild_state = self._ensure_guild_state(guild_id)
            cooldown_days = max(guild_state.recent_winner_cooldown_days, 0)
            cutoff = (
                datetime.now(tz=UTC) - timedelta(days=cooldown_days)
                if cooldown_days > 0
                else None
            )
            remaining: list[Giveaway] = []
            removed: list[Giveaway] = []
            for giveaway in guild_state.giveaways:
                if giveaway.is_active:
                    remaining.append(giveaway)
                    continue
                if cutoff is not None and giveaway.end_time >= cutoff:
                    # Keep recently finished giveaways so recent winner cooldown can reference them.
                    remaining.append(giveaway)
                    continue
                removed.append(giveaway)
            if not removed:
                return 0
            guild_state.giveaways = remaining
            await self.save_state()

        for giveaway in removed:
            finish_task = self._finish_tasks.pop(giveaway.id, None)
            if finish_task:
                finish_task.cancel()

        await self._notify_logger(
            f"Cleaned up {len(removed)} finished giveaway(s) older than {cooldown_days} day(s).",
            guild_id=guild_id,
        )
        return len(removed)

    async def get_text_channel(self, channel_id: int) -> Optional[discord.TextChannel]:
        return await self._fetch_text_channel(channel_id)

    async def set_logger_channel(
        self, guild_id: int, channel_id: Optional[int]
    ) -> None:
        async with self._state_lock:
            guild_state = self._ensure_guild_state(guild_id)
            guild_state.logger_channel_id = channel_id
            await self.save_state()

    async def toggle_auto(self, guild_id: int, enabled: bool) -> bool:
        async with self._state_lock:
            guild_state = self._ensure_guild_state(guild_id)
            guild_state.auto_enabled = enabled
            await self.save_state()
            return guild_state.auto_enabled

    async def add_admin_role(self, guild_id: int, role_id: int) -> bool:
        async with self._state_lock:
            guild_state = self._ensure_guild_state(guild_id)
            if role_id in guild_state.admin_roles:
                return False
            guild_state.admin_roles.append(role_id)
            await self.save_state()
        await self._notify_logger(
            f"Role <@&{role_id}> added to giveaway administrators.", guild_id=guild_id
        )
        return True

    async def remove_admin_role(self, guild_id: int, role_id: int) -> bool:
        async with self._state_lock:
            guild_state = self._ensure_guild_state(guild_id)
            if role_id not in guild_state.admin_roles:
                return False
            guild_state.admin_roles.remove(role_id)
            await self.save_state()
        await self._notify_logger(
            f"Role <@&{role_id}> removed from giveaway administrators.",
            guild_id=guild_id,
        )
        return True

    async def list_admin_roles(self, guild_id: int) -> list[int]:
        async with self._state_lock:
            guild_state = self.state.get_guild_state(guild_id)
            if not guild_state:
                return []
            return list(guild_state.admin_roles)

    async def update_giveaway(
        self,
        guild_id: int,
        giveaway_id: str,
        *,
        winners: Optional[int] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        end_time: Optional[datetime] = None,
    ) -> Optional[Giveaway]:
        async with self._state_lock:
            giveaway = self.state.get_giveaway(guild_id, giveaway_id)
            if not giveaway:
                return None

            if winners is not None:
                if winners <= 0:
                    raise ValueError("winners must be greater than zero")
                giveaway.winners = winners
            if title is not None:
                giveaway.title = title
            if description is not None:
                giveaway.description = description
            if end_time is not None:
                giveaway.end_time = end_time.astimezone(UTC)

            await self.save_state()

        if end_time is not None:
            await self._schedule_finish(giveaway, reschedule=True)

        await self._update_embed(giveaway)
        await self._notify_logger(
            f"Giveaway `{giveaway.id}` updated.", guild_id=guild_id
        )
        return giveaway

    async def reroll(
        self, guild_id: int, giveaway_id: str
    ) -> Optional[Iterable[int]]:
        async with self._state_lock:
            giveaway = self.state.get_giveaway(guild_id, giveaway_id)
            if not giveaway:
                return None
            if giveaway.is_active:
                raise RuntimeError("Cannot reroll an active giveaway.")

        winners = await self._choose_winners(giveaway, reroll=True)
        await self._record_recent_winners(guild_id, winners, giveaway.id)

        async with self._state_lock:
            giveaway = self.state.get_giveaway(guild_id, giveaway_id)
            if not giveaway:
                return None
            giveaway.last_announced_winners = list(winners)
            await self.save_state()

        if winners:
            winner_mentions = ", ".join(f"<@{winner_id}>" for winner_id in winners)
            await self._notify_logger(
                f"Giveaway `{giveaway.id}` rerolled. Winners: {winner_mentions}.",
                guild_id=guild_id,
            )
        else:
            await self._notify_logger(
                f"Giveaway `{giveaway.id}` rerolled but produced no winners.",  # pragma: no cover
                guild_id=guild_id,
            )
        return winners

    async def get_giveaway(
        self, guild_id: int, giveaway_id: str
    ) -> Optional[Giveaway]:
        async with self._state_lock:
            return self.state.get_giveaway(guild_id, giveaway_id)

    async def handle_scheduled(self) -> None:
        if not self.config.scheduling.auto_enabled:
            return

        now_utc = datetime.now(tz=UTC)

        for schedule in self.config.scheduling.giveaways:
            if not schedule.enabled:
                continue
            channel_id = schedule.channel_id
            if not channel_id:
                log.debug(
                    "Scheduled giveaway %s skipped because no channel is configured.",
                    schedule.id,
                )
                continue
            channel = await self._fetch_text_channel(channel_id)
            guild = channel.guild if channel else None
            if not channel or not guild:
                log.info(
                    "Scheduled giveaway %s channel %s not found; skipping run.",
                    schedule.id,
                    schedule.channel_id,
                )
                continue

            tz = self.get_timezone(guild.id)
            now_local = now_utc.astimezone(tz)
            today_iso = now_local.date().isoformat()

            async with self._state_lock:
                guild_state = self._ensure_guild_state(guild.id)
                guild_auto_enabled = guild_state.auto_enabled
                schedule_runs_snapshot = dict(guild_state.schedule_runs)

            if not guild_auto_enabled:
                continue

            await self._maybe_start_schedule(
                schedule,
                guild,
                channel,
                tz,
                now_local,
                today_iso,
                schedule_runs_snapshot,
            )

    async def _maybe_start_schedule(
        self,
        schedule: ScheduledGiveawayConfig,
        guild: discord.Guild,
        channel: discord.TextChannel,
        tz: ZoneInfo,
        now_local: datetime,
        today_iso: str,
        schedule_runs_snapshot: dict,
    ) -> None:
        last_run = schedule_runs_snapshot.get(schedule.id)
        if last_run == today_iso:
            return

        start_dt = datetime.combine(now_local.date(), schedule.start_time, tzinfo=tz)
        end_dt = datetime.combine(now_local.date(), schedule.end_time, tzinfo=tz)
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
        if now_local < start_dt:
            return

        async with self._state_lock:
            active_existing = [
                g
                for g in self.state.list_active(guild.id)
                if g.scheduled_id == schedule.id and g.is_active
            ]
            if active_existing:
                return

        giveaway = await self.start_giveaway(
            guild,
            channel,
            winners=schedule.winners,
            title=schedule.title,
            description=schedule.description,
            end_time=end_dt.astimezone(UTC),
            scheduled_id=schedule.id,
        )

        async with self._state_lock:
            guild_state = self._ensure_guild_state(guild.id)
            guild_state.schedule_runs[schedule.id] = today_iso
            await self.save_state()

        await self._notify_logger(
            f"Scheduled giveaway `{schedule.id}` triggered as `{giveaway.id}`.",
            guild_id=guild.id,
        )

    async def _update_embed(self, giveaway: Giveaway) -> None:
        channel = await self._fetch_text_channel(giveaway.channel_id)
        if not channel:
            return
        message = await self._fetch_message(channel, giveaway.message_id)
        if not message:
            return
        await message.edit(embed=self._embed_from_giveaway(giveaway))

    async def _choose_winners(
        self, giveaway: Giveaway, reroll: bool = False
    ) -> list[int]:
        if len(giveaway.participants) == 0:
            return []
        winners_count = min(giveaway.winners, len(giveaway.participants))
        population = list(giveaway.participants)
        if reroll and giveaway.last_announced_winners:
            # Allow reroll to avoid previous winners when possible
            population = [
                p for p in population if p not in giveaway.last_announced_winners
            ] or population
        blocklist = await self._get_recent_winner_blocklist(giveaway.guild_id)
        if blocklist:
            filtered_population = [p for p in population if p not in blocklist]
            if filtered_population:
                population = filtered_population
            else:
                log.info(
                    "All participants eligible for giveaway %s are within the recent winner cooldown.",
                    giveaway.id,
                )
                await self._notify_logger(
                    f"All participants eligible for giveaway `{giveaway.id}` are within the recent winner cooldown.",
                    guild_id=giveaway.guild_id,
                )
                population = filtered_population
        if winners_count > len(population):
            winners_count = len(population)
        if winners_count == 0:
            return []
        rng = secrets.SystemRandom()
        return rng.sample(population, winners_count)

    async def _register_view(self, giveaway: Giveaway) -> None:
        view = self._build_view(giveaway.id)
        self.bot.add_view(view, message_id=giveaway.message_id)

    async def _schedule_start(
        self, pending: PendingGiveaway, *, reschedule: bool = True
    ) -> None:
        if reschedule and pending.id in self._pending_tasks:
            self._pending_tasks[pending.id].cancel()

        async def runner() -> None:
            try:
                now = datetime.now(tz=UTC)
                delay = (pending.start_time - now).total_seconds()
                if delay > 0:
                    await asyncio.sleep(delay)
                await self._start_pending_giveaway(pending.guild_id, pending.id)
            except asyncio.CancelledError:
                log.debug("Start task for pending giveaway %s cancelled", pending.id)
                raise
            finally:
                self._pending_tasks.pop(pending.id, None)

        task = asyncio.create_task(runner())
        self._pending_tasks[pending.id] = task

    def _compute_next_window(
        self,
        guild_id: int,
        start_time_value: time,
        end_time_value: time,
        reference: Optional[datetime] = None,
    ) -> tuple[datetime, datetime]:
        tz = self.get_timezone(guild_id)
        if reference is None:
            reference_local = datetime.now(tz=tz)
        else:
            reference_local = reference.astimezone(tz)
        start_local = datetime.combine(reference_local.date(), start_time_value, tzinfo=tz)
        if start_local <= reference_local:
            start_local += timedelta(days=1)
        end_local = datetime.combine(start_local.date(), end_time_value, tzinfo=tz)
        if end_local <= start_local:
            end_local += timedelta(days=1)
        return start_local.astimezone(UTC), end_local.astimezone(UTC)

    async def _schedule_recurring(
        self, recurring: "RecurringGiveaway", *, reschedule: bool = True
    ) -> None:
        if reschedule:
            task = self._recurring_tasks.pop(recurring.id, None)
            if task:
                task.cancel()
        if not recurring.enabled:
            return

        delay = (recurring.next_start - datetime.now(tz=UTC)).total_seconds()
        if delay < 0:
            delay = 0

        async def runner() -> None:
            try:
                if delay > 0:
                    await asyncio.sleep(delay)
                await self._run_recurring(recurring.guild_id, recurring.id)
            except asyncio.CancelledError:
                log.debug("Recurring task for %s cancelled", recurring.id)
                raise
            finally:
                self._recurring_tasks.pop(recurring.id, None)

        self._recurring_tasks[recurring.id] = asyncio.create_task(runner())

    async def _run_recurring(self, guild_id: int, schedule_id: str) -> None:
        async with self._state_lock:
            recurring = self.state.get_recurring(guild_id, schedule_id)
        if not recurring or not recurring.enabled:
            return

        channel = await self._fetch_text_channel(recurring.channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            log.warning(
                "Recurring giveaway %s channel %s unavailable; disabling schedule.",
                schedule_id,
                recurring.channel_id,
            )
            await self.disable_recurring(guild_id, schedule_id)
            return

        guild = channel.guild
        try:
            giveaway = await self.start_giveaway(
                guild,
                channel,
                winners=recurring.winners,
                title=recurring.title,
                description=recurring.description,
                end_time=recurring.next_end,
                scheduled_id=schedule_id,
            )
        except Exception:
            log.exception("Failed to start recurring giveaway %s", schedule_id)
            next_start, next_end = self._compute_next_window(
                guild_id, recurring.start_time, recurring.end_time
            )
            async with self._state_lock:
                latest = self.state.get_recurring(guild_id, schedule_id)
                if latest:
                    latest.next_start = next_start
                    latest.next_end = next_end
                    self.state.upsert_recurring(guild_id, latest)
                    await self.save_state()
                    recurring = latest
            if recurring.enabled:
                await self._schedule_recurring(recurring, reschedule=True)
            return

        tz = self.get_timezone(guild_id)
        next_start_utc, next_end_utc = self._compute_next_window(
            guild_id, recurring.start_time, recurring.end_time, reference=datetime.now(tz=tz)
        )

        async with self._state_lock:
            latest = self.state.get_recurring(guild_id, schedule_id)
            if not latest:
                return
            latest.next_start = next_start_utc
            latest.next_end = next_end_utc
            self.state.upsert_recurring(guild_id, latest)
            await self.save_state()

        await self._schedule_recurring(latest, reschedule=True)
        await self._notify_logger(
            f"Scheduled giveaway `{schedule_id}` started as `{giveaway.id}`.",
            guild_id=guild_id,
        )

    async def audit_overdue(self) -> None:
        now = datetime.now(tz=UTC)
        to_end: list[tuple[int, str]] = []
        to_finalize: list[tuple[int, str]] = []

        async with self._state_lock:
            for guild_id, guild_state in self.state.iter_guild_states():
                for giveaway in guild_state.giveaways:
                    if giveaway.end_time <= now:
                        if giveaway.is_active:
                            to_end.append((guild_id, giveaway.id))
                        elif (
                            not giveaway.last_announced_winners
                            and len(giveaway.participants) > 0
                        ):
                            to_finalize.append((guild_id, giveaway.id))

        for guild_id, giveaway_id in to_end:
            await self.end_giveaway(guild_id, giveaway_id)

        for guild_id, giveaway_id in to_finalize:
            async with self._state_lock:
                giveaway = self.state.get_giveaway(guild_id, giveaway_id)
                if not giveaway or giveaway.last_announced_winners:
                    continue
            await self._finalize_giveaway(giveaway, notify=True)

    async def set_timezone(self, guild_id: int, timezone_name: str) -> None:
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Invalid timezone: {timezone_name}") from exc

        async with self._state_lock:
            guild_state = self._ensure_guild_state(guild_id)
            if guild_state.timezone == timezone_name:
                return
            guild_state.timezone = timezone_name
            recurring_items = list(guild_state.recurring_giveaways)
            for recurring in recurring_items:
                next_start, next_end = self._compute_next_window(
                    guild_id, recurring.start_time, recurring.end_time
                )
                recurring.next_start = next_start
                recurring.next_end = next_end
                task = self._recurring_tasks.pop(recurring.id, None)
                if task:
                    task.cancel()
            await self.save_state()

        for recurring in recurring_items:
            if recurring.enabled:
                await self._schedule_recurring(recurring, reschedule=False)

    async def set_recent_winner_cooldown_days(self, guild_id: int, days: int) -> None:
        if days < 0:
            raise ValueError("Cooldown days must be zero or greater.")
        async with self._state_lock:
            guild_state = self._ensure_guild_state(guild_id)
            if guild_state.recent_winner_cooldown_days == days:
                return
            guild_state.recent_winner_cooldown_days = days
            await self.save_state()

    async def set_recent_winner_cooldown_enabled(
        self, guild_id: int, enabled: bool
    ) -> bool:
        async with self._state_lock:
            guild_state = self._ensure_guild_state(guild_id)
            if guild_state.recent_winner_cooldown_enabled == enabled:
                return False
            guild_state.recent_winner_cooldown_enabled = enabled
            await self.save_state()
            return True

    async def get_settings_snapshot(self, guild_id: int) -> dict:
        async with self._state_lock:
            guild_state = self._ensure_guild_state(guild_id)
            return {
                "timezone": guild_state.timezone,
                "auto_enabled": guild_state.auto_enabled,
                "recent_winner_cooldown_days": guild_state.recent_winner_cooldown_days,
                "recent_winner_cooldown_enabled": guild_state.recent_winner_cooldown_enabled,
            }

    async def get_recurring_giveaway(
        self, guild_id: int, schedule_id: str
    ) -> Optional[RecurringGiveaway]:
        async with self._state_lock:
            return self.state.get_recurring(guild_id, schedule_id)

    async def disable_recurring(self, guild_id: int, schedule_id: str) -> str:
        async with self._state_lock:
            recurring = self.state.get_recurring(guild_id, schedule_id)
            if not recurring:
                return "not_found"
            if not recurring.enabled:
                return "already_disabled"
            recurring.enabled = False
            self.state.upsert_recurring(guild_id, recurring)
            task = self._recurring_tasks.pop(schedule_id, None)
            if task:
                task.cancel()
            await self.save_state()
        await self._notify_logger(
            f"Scheduled giveaway `{schedule_id}` disabled.", guild_id=guild_id
        )
        return "disabled"

    async def enable_recurring(self, guild_id: int, schedule_id: str) -> str:
        async with self._state_lock:
            recurring = self.state.get_recurring(guild_id, schedule_id)
            if not recurring:
                return "not_found"
            if recurring.enabled:
                return "already_enabled"
            recurring.enabled = True
            next_start, next_end = self._compute_next_window(
                guild_id, recurring.start_time, recurring.end_time
            )
            recurring.next_start = next_start
            recurring.next_end = next_end
            self.state.upsert_recurring(guild_id, recurring)
            await self.save_state()

        await self._schedule_recurring(recurring, reschedule=True)
        await self._notify_logger(
            f"Scheduled giveaway `{schedule_id}` enabled.", guild_id=guild_id
        )
        return "enabled"

    async def _start_pending_giveaway(self, guild_id: int, pending_id: str) -> None:
        async with self._state_lock:
            pending = self.state.get_pending(guild_id, pending_id)
        if not pending:
            return

        channel = await self._fetch_text_channel(pending.channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            log.warning(
                "Pending giveaway %s channel %s unavailable; removing from state.",
                pending_id,
                pending.channel_id,
            )
            async with self._state_lock:
                removed = self.state.remove_pending(guild_id, pending_id)
                if removed:
                    await self.save_state()
            await self._notify_logger(
                f"Scheduled giveaway `{pending_id}` could not start because channel <#{pending.channel_id}> is unavailable.",
                guild_id=guild_id,
            )
            return

        guild = channel.guild
        try:
            giveaway = await self.start_giveaway(
                guild,
                channel,
                winners=pending.winners,
                title=pending.title,
                description=pending.description,
                end_time=pending.end_time,
            )
        except Exception:
            log.exception("Failed to start pending giveaway %s", pending_id)
            async with self._state_lock:
                removed = self.state.remove_pending(guild_id, pending_id)
                if removed:
                    await self.save_state()
            await self._notify_logger(
                f"Scheduled giveaway `{pending_id}` failed to start. Check logs for details.",
                guild_id=guild_id,
            )
            return

        async with self._state_lock:
            removed = self.state.remove_pending(guild_id, pending_id)
            if removed:
                await self.save_state()

        await self._notify_logger(
            f"Scheduled giveaway `{pending_id}` started as `{giveaway.id}`.",
            guild_id=guild_id,
        )

    async def _schedule_finish(
        self, giveaway: Giveaway, *, reschedule: bool = False
    ) -> None:
        if reschedule and giveaway.id in self._finish_tasks:
            self._finish_tasks[giveaway.id].cancel()

        if not giveaway.is_active:
            return

        now = datetime.now(tz=UTC)
        delay = (giveaway.end_time - now).total_seconds()
        if delay <= 0:
            asyncio.create_task(self.end_giveaway(giveaway.guild_id, giveaway.id))
            return

        async def waiter():
            try:
                await asyncio.sleep(delay)
                await self.end_giveaway(giveaway.guild_id, giveaway.id)
            except asyncio.CancelledError:
                log.debug("Finish task for giveaway %s cancelled", giveaway.id)

        task = asyncio.create_task(waiter())
        self._finish_tasks[giveaway.id] = task

    async def _notify_logger(
        self, message: str, *, guild_id: Optional[int] = None
    ) -> None:
        channel_id: Optional[int] = None
        if guild_id is not None:
            guild_state = self.state.get_guild_state(guild_id)
            if guild_state and guild_state.logger_channel_id:
                channel_id = guild_state.logger_channel_id
        if not channel_id:
            channel_id = self.config.logging.logger_channel_id
        if not channel_id:
            return
        channel = await self._fetch_text_channel(channel_id)
        if channel:
            try:
                await channel.send(f"[Giveaway] {message}")
            except discord.HTTPException as exc:
                log.warning("Failed to send log message to %s: %s", channel_id, exc)

    async def _fetch_text_channel(
        self, channel_id: int
    ) -> Optional[discord.TextChannel]:
        channel = self.bot.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        try:
            fetched = await self.bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return fetched if isinstance(fetched, discord.TextChannel) else None

    async def _fetch_message(
        self, channel: discord.TextChannel, message_id: int
    ) -> Optional[discord.Message]:
        try:
            return await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    def _build_view(self, giveaway_id: str) -> GiveawayView:
        return GiveawayView(self, giveaway_id)

    def _build_embed(
        self,
        *,
        giveaway: Optional[Giveaway],
        title: str,
        description: str,
        winners: int,
        participants: int,
        end_time: datetime,
        status: str,
        winner_mentions: Optional[str] = None,
        tz: ZoneInfo,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.blue()
            if status == "Active"
            else discord.Color.dark_gray(),
        )
        embed.add_field(name="Winners", value=str(winners), inline=True)
        embed.add_field(name="Participants", value=str(participants), inline=True)
        end_local = end_time.astimezone(tz)
        embed.add_field(
            name="Ends At", value=end_local.strftime("%Y-%m-%d %H:%M %Z"), inline=False
        )
        embed.add_field(name="Status", value=status, inline=True)
        if giveaway:
            embed.set_footer(text=f"Giveaway ID: {giveaway.id}")
        if winner_mentions:
            embed.add_field(name="Winner(s)", value=winner_mentions, inline=False)
        return embed

    def _embed_from_giveaway(
        self,
        giveaway: Giveaway,
        *,
        status: Optional[str] = None,
        winners: Optional[Iterable[int]] = None,
    ) -> discord.Embed:
        resolved_status = status or ("Active" if giveaway.is_active else "Finished")
        winner_mentions = None
        if winners:
            winner_mentions = " ".join(f"<@{winner_id}>" for winner_id in winners)
        tz = self.get_timezone(giveaway.guild_id)
        return self._build_embed(
            giveaway=giveaway,
            title=giveaway.title,
            description=giveaway.description,
            winners=giveaway.winners,
            participants=len(giveaway.participants),
            end_time=giveaway.end_time,
            status=resolved_status,
            winner_mentions=winner_mentions,
            tz=tz,
        )

    def _generate_giveaway_id(self) -> str:
        return datetime.utcnow().strftime("%Y%m%d%H%M%S%f")[-10:]
