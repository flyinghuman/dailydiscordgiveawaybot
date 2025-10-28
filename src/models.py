from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Dict, Iterable, List, Optional, Sequence


@dataclass(slots=True)
class Giveaway:
    id: str
    guild_id: int
    channel_id: int
    message_id: int
    winners: int
    title: str
    description: str
    end_time: datetime
    created_at: datetime
    participants: List[int] = field(default_factory=list)
    scheduled_id: Optional[str] = None
    is_active: bool = True
    last_announced_winners: List[int] = field(default_factory=list)

    def add_participant(self, user_id: int) -> bool:
        if user_id in self.participants:
            return False
        self.participants.append(user_id)
        return True

    def remove_participant(self, user_id: int) -> bool:
        if user_id not in self.participants:
            return False
        self.participants.remove(user_id)
        return True

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "winners": self.winners,
            "title": self.title,
            "description": self.description,
            "end_time": self.end_time.isoformat(),
            "created_at": self.created_at.isoformat(),
            "participants": self.participants,
            "scheduled_id": self.scheduled_id,
            "is_active": self.is_active,
            "last_announced_winners": self.last_announced_winners,
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "Giveaway":
        return cls(
            id=str(payload["id"]),
            guild_id=int(payload["guild_id"]),
            channel_id=int(payload["channel_id"]),
            message_id=int(payload["message_id"]),
            winners=int(payload["winners"]),
            title=str(payload["title"]),
            description=str(payload["description"]),
            end_time=datetime.fromisoformat(payload["end_time"]),
            created_at=datetime.fromisoformat(payload["created_at"]),
            participants=list(map(int, payload.get("participants", []))),
            scheduled_id=payload.get("scheduled_id"),
            is_active=bool(payload.get("is_active", True)),
            last_announced_winners=list(
                map(int, payload.get("last_announced_winners", []))
            ),
        )


@dataclass(slots=True)
class PendingGiveaway:
    id: str
    guild_id: int
    channel_id: int
    winners: int
    title: str
    description: str
    start_time: datetime
    end_time: datetime

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "winners": self.winners,
            "title": self.title,
            "description": self.description,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "PendingGiveaway":
        return cls(
            id=str(payload["id"]),
            guild_id=int(payload["guild_id"]),
            channel_id=int(payload["channel_id"]),
            winners=int(payload["winners"]),
            title=str(payload["title"]),
            description=str(payload["description"]),
            start_time=datetime.fromisoformat(payload["start_time"]),
            end_time=datetime.fromisoformat(payload["end_time"]),
        )


@dataclass(slots=True)
class GuildState:
    auto_enabled: bool = True
    timezone: str = "Europe/Berlin"
    logger_channel_id: Optional[int] = None
    schedule_runs: Dict[str, str] = field(default_factory=dict)
    giveaways: List[Giveaway] = field(default_factory=list)
    pending_giveaways: List[PendingGiveaway] = field(default_factory=list)
    recurring_giveaways: List["RecurringGiveaway"] = field(default_factory=list)
    admin_roles: List[int] = field(default_factory=list)

    def to_payload(self) -> dict:
        return {
            "auto_enabled": self.auto_enabled,
            "timezone": self.timezone,
            "logger_channel_id": self.logger_channel_id,
            "schedule_runs": self.schedule_runs,
            "giveaways": [g.to_payload() for g in self.giveaways],
            "pending_giveaways": [p.to_payload() for p in self.pending_giveaways],
            "recurring_giveaways": [r.to_payload() for r in self.recurring_giveaways],
            "admin_roles": self.admin_roles,
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "GuildState":
        giveaways_payload = payload.get("giveaways", [])
        giveaways = [Giveaway.from_payload(g) for g in giveaways_payload]
        pending_payload = payload.get("pending_giveaways", [])
        pending = [PendingGiveaway.from_payload(p) for p in pending_payload]
        recurring_payload = payload.get("recurring_giveaways", [])
        recurring = [RecurringGiveaway.from_payload(r) for r in recurring_payload]
        return cls(
            auto_enabled=bool(payload.get("auto_enabled", True)),
            timezone=payload.get("timezone", "Europe/Berlin"),
            logger_channel_id=payload.get("logger_channel_id"),
            schedule_runs=dict(payload.get("schedule_runs", {})),
            giveaways=giveaways,
            pending_giveaways=pending,
            recurring_giveaways=recurring,
            admin_roles=[int(r) for r in payload.get("admin_roles", [])],
        )


@dataclass(slots=True)
class BotState:
    guilds: Dict[int, GuildState] = field(default_factory=dict)

    def to_payload(self) -> dict:
        return {
            "guilds": {
                str(guild_id): guild_state.to_payload()
                for guild_id, guild_state in self.guilds.items()
            }
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "BotState":
        if "guilds" in payload:
            guilds_payload = payload.get("guilds", {})
            guilds: Dict[int, GuildState] = {}
            for guild_id_str, guild_payload in guilds_payload.items():
                try:
                    guild_id = int(guild_id_str)
                except (TypeError, ValueError):
                    continue
                guilds[guild_id] = GuildState.from_payload(guild_payload or {})
            return cls(guilds=guilds)

        # Legacy flat payload migration
        legacy_state = GuildState(
            auto_enabled=bool(payload.get("auto_enabled", True)),
            timezone=payload.get("timezone", "Europe/Berlin"),
            logger_channel_id=payload.get("logger_channel_id"),
            schedule_runs=dict(payload.get("schedule_runs", {})),
            giveaways=[Giveaway.from_payload(g) for g in payload.get("giveaways", [])],
            pending_giveaways=[
                PendingGiveaway.from_payload(p)
                for p in payload.get("pending_giveaways", [])
            ],
            recurring_giveaways=[
                RecurringGiveaway.from_payload(r)
                for r in payload.get("recurring_giveaways", [])
            ],
            admin_roles=[int(r) for r in payload.get("admin_roles", [])],
        )

        guilds: Dict[int, GuildState] = {}
        for giveaway in legacy_state.giveaways:
            guilds.setdefault(giveaway.guild_id, GuildState()).giveaways.append(
                giveaway
            )
        for pending in legacy_state.pending_giveaways:
            guilds.setdefault(pending.guild_id, GuildState()).pending_giveaways.append(
                pending
            )
        for recurring in legacy_state.recurring_giveaways:
            guilds.setdefault(recurring.guild_id, GuildState()).recurring_giveaways.append(
                recurring
            )

        if not guilds:
            # No giveaway data; keep a default guild-less state
            guilds[0] = legacy_state
        else:
            for guild_state in guilds.values():
                guild_state.admin_roles = list(legacy_state.admin_roles)
                guild_state.auto_enabled = legacy_state.auto_enabled
                guild_state.timezone = legacy_state.timezone
                guild_state.logger_channel_id = legacy_state.logger_channel_id
                guild_state.schedule_runs = dict(legacy_state.schedule_runs)

        return cls(guilds=guilds)

    def ensure_guild_state(
        self, guild_id: int, *, default_admin_roles: Optional[Iterable[int]] = None
    ) -> GuildState:
        state = self.guilds.get(guild_id)
        if state is None:
            roles: List[int] = []
            if default_admin_roles:
                seen: set[int] = set()
                for role in default_admin_roles:
                    try:
                        role_id = int(role)
                    except (TypeError, ValueError):
                        continue
                    if role_id not in seen:
                        seen.add(role_id)
                        roles.append(role_id)
            state = GuildState(admin_roles=roles)
            self.guilds[guild_id] = state
        return state

    def get_guild_state(self, guild_id: int) -> Optional[GuildState]:
        return self.guilds.get(guild_id)

    def iter_guild_states(self) -> Iterable[tuple[int, GuildState]]:
        return tuple(self.guilds.items())

    def upsert_giveaway(self, giveaway: Giveaway) -> None:
        state = self.ensure_guild_state(giveaway.guild_id)
        for idx, item in enumerate(state.giveaways):
            if item.id == giveaway.id:
                state.giveaways[idx] = giveaway
                return
        state.giveaways.append(giveaway)

    def remove_giveaway(self, guild_id: int, giveaway_id: str) -> Optional[Giveaway]:
        state = self.get_guild_state(guild_id)
        if not state:
            return None
        for idx, item in enumerate(state.giveaways):
            if item.id == giveaway_id:
                return state.giveaways.pop(idx)
        return None

    def get_giveaway(self, guild_id: int, giveaway_id: str) -> Optional[Giveaway]:
        state = self.get_guild_state(guild_id)
        if not state:
            return None
        for item in state.giveaways:
            if item.id == giveaway_id:
                return item
        return None

    def list_active(self, guild_id: int) -> List[Giveaway]:
        state = self.get_guild_state(guild_id)
        if not state:
            return []
        return [g for g in state.giveaways if g.is_active]

    def list_all(self, guild_id: int) -> Sequence[Giveaway]:
        state = self.get_guild_state(guild_id)
        if not state:
            return ()
        return tuple(state.giveaways)

    def get_pending(self, guild_id: int, pending_id: str) -> Optional[PendingGiveaway]:
        state = self.get_guild_state(guild_id)
        if not state:
            return None
        for item in state.pending_giveaways:
            if item.id == pending_id:
                return item
        return None

    def upsert_pending(self, guild_id: int, pending: PendingGiveaway) -> None:
        state = self.ensure_guild_state(guild_id)
        for idx, item in enumerate(state.pending_giveaways):
            if item.id == pending.id:
                state.pending_giveaways[idx] = pending
                return
        state.pending_giveaways.append(pending)

    def remove_pending(self, guild_id: int, pending_id: str) -> Optional[PendingGiveaway]:
        state = self.get_guild_state(guild_id)
        if not state:
            return None
        for idx, item in enumerate(state.pending_giveaways):
            if item.id == pending_id:
                return state.pending_giveaways.pop(idx)
        return None

    def list_pending(self, guild_id: int) -> Sequence[PendingGiveaway]:
        state = self.get_guild_state(guild_id)
        if not state:
            return ()
        return tuple(state.pending_giveaways)

    def get_recurring(self, guild_id: int, schedule_id: str) -> Optional["RecurringGiveaway"]:
        state = self.get_guild_state(guild_id)
        if not state:
            return None
        for item in state.recurring_giveaways:
            if item.id == schedule_id:
                return item
        return None

    def upsert_recurring(self, guild_id: int, recurring: "RecurringGiveaway") -> None:
        state = self.ensure_guild_state(guild_id)
        for idx, item in enumerate(state.recurring_giveaways):
            if item.id == recurring.id:
                state.recurring_giveaways[idx] = recurring
                return
        state.recurring_giveaways.append(recurring)

    def remove_recurring(self, guild_id: int, schedule_id: str) -> Optional["RecurringGiveaway"]:
        state = self.get_guild_state(guild_id)
        if not state:
            return None
        for idx, item in enumerate(state.recurring_giveaways):
            if item.id == schedule_id:
                return state.recurring_giveaways.pop(idx)
        return None

    def list_recurring(self, guild_id: int) -> Sequence["RecurringGiveaway"]:
        state = self.get_guild_state(guild_id)
        if not state:
            return ()
        return tuple(state.recurring_giveaways)


@dataclass(slots=True)
class RecurringGiveaway:
    id: str
    guild_id: int
    channel_id: int
    winners: int
    title: str
    description: str
    start_time: time
    end_time: time
    next_start: datetime
    next_end: datetime
    enabled: bool = True

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "winners": self.winners,
            "title": self.title,
            "description": self.description,
            "start_time": self.start_time.strftime("%H:%M"),
            "end_time": self.end_time.strftime("%H:%M"),
            "next_start": self.next_start.isoformat(),
            "next_end": self.next_end.isoformat(),
            "enabled": self.enabled,
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "RecurringGiveaway":
        start_time_value = payload.get("start_time", "00:00")
        end_time_value = payload.get("end_time", "00:00")
        start_time_obj = datetime.strptime(start_time_value, "%H:%M").time()
        end_time_obj = datetime.strptime(end_time_value, "%H:%M").time()
        return cls(
            id=str(payload["id"]),
            guild_id=int(payload["guild_id"]),
            channel_id=int(payload["channel_id"]),
            winners=int(payload["winners"]),
            title=str(payload["title"]),
            description=str(payload["description"]),
            start_time=start_time_obj,
            end_time=end_time_obj,
            next_start=datetime.fromisoformat(payload["next_start"]),
            next_end=datetime.fromisoformat(payload["next_end"]),
            enabled=bool(payload.get("enabled", True)),
        )
