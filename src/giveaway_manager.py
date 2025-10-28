from __future__ import annotations

import asyncio
import logging
import random
from datetime import UTC, datetime, timedelta
from typing import Dict, Iterable, Optional
from zoneinfo import ZoneInfo

import discord

from .config import Config, ScheduledGiveawayConfig
from .models import BotState, Giveaway
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
        self.timezone = ZoneInfo(config.default_timezone)
        self.state = BotState(auto_enabled=config.scheduling.auto_enabled)
        self._finish_tasks: Dict[str, asyncio.Task] = {}
        self._state_lock = asyncio.Lock()

    async def load(self) -> None:
        try:
            self.state = await self.storage.load()
        except Exception as exc:
            log.exception(
                "Failed to load persisted state, starting with defaults: %s", exc
            )
            self.state = BotState(auto_enabled=self.config.scheduling.auto_enabled)

        # If config changed to disable auto scheduling ensure state reflects it
        self.state.auto_enabled = (
            self.config.scheduling.auto_enabled and self.state.auto_enabled
        )

        await self._restore_active_giveaways()

    async def _restore_active_giveaways(self) -> None:
        for giveaway in self.state.list_all():
            if giveaway.is_active:
                await self._register_view(giveaway)
                await self._schedule_finish(giveaway)

    async def save_state(self) -> None:
        await self.storage.save(self.state)

    def is_admin(self, member: discord.Member) -> bool:
        if member.guild_permissions.manage_guild:
            return True
        admin_roles = set(self.config.permissions.admin_roles)
        if not admin_roles:
            return False
        return any(role.id in admin_roles for role in member.roles)

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
        embed = self._build_embed(
            giveaway=None,
            title=title,
            description=description,
            winners=winners,
            participants=0,
            end_time=end_time,
            status="Active",
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
            self.state.upsert_giveaway(giveaway)
            await self.save_state()

        await self._schedule_finish(giveaway)

        await message.edit(embed=self._embed_from_giveaway(giveaway), view=view)

        await self._notify_logger(
            f"Giveaway **{giveaway.title}** (`{giveaway.id}`) started in <#{giveaway.channel_id}>."
        )
        return giveaway

    async def end_giveaway(
        self, giveaway_id: str, *, notify: bool = True
    ) -> Optional[Giveaway]:
        async with self._state_lock:
            giveaway = self.state.get_giveaway(giveaway_id)
            if not giveaway:
                return None
            if not giveaway.is_active:
                return giveaway
            giveaway.is_active = False
            await self.save_state()

        task = self._finish_tasks.pop(giveaway_id, None)
        if task:
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
        elif notify:
            await channel.send(
                f"Giveaway **{giveaway.title}** ended without enough participants."
            )

        await self.save_state()
        await self._notify_logger(
            f"Giveaway **{giveaway.title}** (`{giveaway.id}`) finished with {len(winners)} winner(s)."
        )

    async def add_participant(self, giveaway_id: str, user: discord.Member) -> str:
        async with self._state_lock:
            giveaway = self.state.get_giveaway(giveaway_id)
            if not giveaway:
                return "This giveaway is no longer available."
            if not giveaway.is_active:
                return "This giveaway has already finished."
            if user.id in giveaway.participants:
                return "You have already joined this giveaway."
            giveaway.participants.append(user.id)
            await self.save_state()

        await self._update_embed(giveaway)
        await self._notify_logger(f"{user.mention} joined giveaway `{giveaway.id}`.")
        return "You're in! Good luck!"

    async def remove_participant(self, giveaway_id: str, user: discord.Member) -> str:
        async with self._state_lock:
            giveaway = self.state.get_giveaway(giveaway_id)
            if not giveaway:
                return "This giveaway is no longer available."
            if not giveaway.is_active:
                return "This giveaway has already finished."
            if user.id not in giveaway.participants:
                return "You are not part of this giveaway."
            giveaway.participants.remove(user.id)
            await self.save_state()

        await self._update_embed(giveaway)
        await self._notify_logger(f"{user.mention} left giveaway `{giveaway.id}`.")
        return "You've left the giveaway."

    async def list_giveaways(self) -> Iterable[Giveaway]:
        async with self._state_lock:
            return list(self.state.list_all())

    async def get_text_channel(self, channel_id: int) -> Optional[discord.TextChannel]:
        return await self._fetch_text_channel(channel_id)

    async def set_logger_channel(self, channel_id: Optional[int]) -> None:
        async with self._state_lock:
            self.state.logger_channel_id = channel_id
            await self.save_state()

    async def toggle_auto(self, enabled: bool) -> bool:
        async with self._state_lock:
            self.state.auto_enabled = enabled
            await self.save_state()
            return self.state.auto_enabled

    async def update_giveaway(
        self,
        giveaway_id: str,
        *,
        winners: Optional[int] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        end_time: Optional[datetime] = None,
    ) -> Optional[Giveaway]:
        async with self._state_lock:
            giveaway = self.state.get_giveaway(giveaway_id)
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
        await self._notify_logger(f"Giveaway `{giveaway.id}` updated.")
        return giveaway

    async def reroll(self, giveaway_id: str) -> Optional[Iterable[int]]:
        async with self._state_lock:
            giveaway = self.state.get_giveaway(giveaway_id)
            if not giveaway:
                return None
            if giveaway.is_active:
                raise RuntimeError("Cannot reroll an active giveaway.")
            winners = await self._choose_winners(giveaway, reroll=True)
            giveaway.last_announced_winners = list(winners)
            await self.save_state()

        await self._notify_logger(f"Giveaway `{giveaway.id}` rerolled.")
        return winners

    async def get_giveaway(self, giveaway_id: str) -> Optional[Giveaway]:
        async with self._state_lock:
            return self.state.get_giveaway(giveaway_id)

    async def handle_scheduled(self) -> None:
        if not self.state.auto_enabled or not self.config.scheduling.auto_enabled:
            return

        now_utc = datetime.now(tz=UTC)
        now_local = now_utc.astimezone(self.timezone)
        today = now_local.date()
        today_iso = today.isoformat()

        async with self._state_lock:
            schedule_runs = dict(self.state.schedule_runs)

        for schedule in self.config.scheduling.giveaways:
            if not schedule.enabled:
                continue
            await self._maybe_start_schedule(
                schedule, now_local, today_iso, schedule_runs
            )

    async def _maybe_start_schedule(
        self,
        schedule: ScheduledGiveawayConfig,
        now_local: datetime,
        today_iso: str,
        schedule_runs_snapshot: dict,
    ) -> None:
        last_run = schedule_runs_snapshot.get(schedule.id)
        if last_run == today_iso:
            return

        start_dt = datetime.combine(
            now_local.date(), schedule.start_time, tzinfo=self.timezone
        )
        end_dt = datetime.combine(
            now_local.date(), schedule.end_time, tzinfo=self.timezone
        )
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
        if now_local < start_dt:
            return

        async with self._state_lock:
            # Check if there is an active giveaway already tied to this schedule
            active_existing = [
                g for g in self.state.list_active() if g.scheduled_id == schedule.id
            ]
            if active_existing:
                return

        channel = await self._fetch_text_channel(schedule.channel_id)
        guild = channel.guild if channel else None
        if not channel or not guild:
            log.warning(
                "Scheduled giveaway %s channel %s not found",
                schedule.id,
                schedule.channel_id,
            )
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
            self.state.schedule_runs[schedule.id] = today_iso
            await self.save_state()

        await self._notify_logger(
            f"Scheduled giveaway `{schedule.id}` triggered as `{giveaway.id}`."
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
        return random.sample(population, winners_count)

    async def _register_view(self, giveaway: Giveaway) -> None:
        view = self._build_view(giveaway.id)
        self.bot.add_view(view, message_id=giveaway.message_id)

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
            asyncio.create_task(self.end_giveaway(giveaway.id))
            return

        async def waiter():
            try:
                await asyncio.sleep(delay)
                await self.end_giveaway(giveaway.id)
            except asyncio.CancelledError:
                log.debug("Finish task for giveaway %s cancelled", giveaway.id)

        task = asyncio.create_task(waiter())
        self._finish_tasks[giveaway.id] = task

    async def _notify_logger(self, message: str) -> None:
        channel_id = (
            self.state.logger_channel_id or self.config.logging.logger_channel_id
        )
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
        end_local = end_time.astimezone(self.timezone)
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
        return self._build_embed(
            giveaway=giveaway,
            title=giveaway.title,
            description=giveaway.description,
            winners=giveaway.winners,
            participants=len(giveaway.participants),
            end_time=giveaway.end_time,
            status=resolved_status,
            winner_mentions=winner_mentions,
        )

    def _generate_giveaway_id(self) -> str:
        return datetime.utcnow().strftime("%Y%m%d%H%M%S%f")[-10:]
