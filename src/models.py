from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Sequence


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
class BotState:
    auto_enabled: bool = True
    logger_channel_id: Optional[int] = None
    schedule_runs: dict = field(default_factory=dict)
    giveaways: List[Giveaway] = field(default_factory=list)
    admin_roles: List[int] = field(default_factory=list)
    pending_giveaways: List[PendingGiveaway] = field(default_factory=list)

    def to_payload(self) -> dict:
        return {
            "auto_enabled": self.auto_enabled,
            "logger_channel_id": self.logger_channel_id,
            "schedule_runs": self.schedule_runs,
            "giveaways": [g.to_payload() for g in self.giveaways],
            "admin_roles": self.admin_roles,
            "pending_giveaways": [p.to_payload() for p in self.pending_giveaways],
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "BotState":
        giveaways_payload = payload.get("giveaways", [])
        giveaways = [Giveaway.from_payload(g) for g in giveaways_payload]
        pending_payload = payload.get("pending_giveaways", [])
        pending = [PendingGiveaway.from_payload(p) for p in pending_payload]
        state = cls(
            auto_enabled=bool(payload.get("auto_enabled", True)),
            logger_channel_id=payload.get("logger_channel_id"),
            schedule_runs=dict(payload.get("schedule_runs", {})),
            giveaways=giveaways,
            admin_roles=[int(r) for r in payload.get("admin_roles", [])],
            pending_giveaways=pending,
        )
        return state

    def upsert_giveaway(self, giveaway: Giveaway) -> None:
        for idx, item in enumerate(self.giveaways):
            if item.id == giveaway.id:
                self.giveaways[idx] = giveaway
                return
        self.giveaways.append(giveaway)

    def remove_giveaway(self, giveaway_id: str) -> Optional[Giveaway]:
        for idx, item in enumerate(self.giveaways):
            if item.id == giveaway_id:
                return self.giveaways.pop(idx)
        return None

    def get_giveaway(self, giveaway_id: str) -> Optional[Giveaway]:
        for item in self.giveaways:
            if item.id == giveaway_id:
                return item
        return None

    def list_active(self) -> List[Giveaway]:
        return [g for g in self.giveaways if g.is_active]

    def list_all(self) -> Sequence[Giveaway]:
        return tuple(self.giveaways)

    def get_pending(self, pending_id: str) -> Optional[PendingGiveaway]:
        for item in self.pending_giveaways:
            if item.id == pending_id:
                return item
        return None

    def upsert_pending(self, pending: PendingGiveaway) -> None:
        for idx, item in enumerate(self.pending_giveaways):
            if item.id == pending.id:
                self.pending_giveaways[idx] = pending
                return
        self.pending_giveaways.append(pending)

    def remove_pending(self, pending_id: str) -> Optional[PendingGiveaway]:
        for idx, item in enumerate(self.pending_giveaways):
            if item.id == pending_id:
                return self.pending_giveaways.pop(idx)
        return None

    def list_pending(self) -> Sequence[PendingGiveaway]:
        return tuple(self.pending_giveaways)
