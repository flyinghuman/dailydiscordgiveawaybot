"""Per-guild SQLite persistence helpers for giveaway state."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from .models import (
    BotState,
    Giveaway,
    GuildState,
    PendingGiveaway,
    RecentWinner,
    RecurringGiveaway,
)

LOGGER = logging.getLogger(__name__)


class StateStorage:
    """Async wrapper around per-guild SQLite databases for bot state."""

    def __init__(self, base_dir: Path) -> None:
        """Initialise the storage helper with the base data directory."""
        self.base_dir = base_dir
        self.guilds_dir = self.base_dir / "guilds"
        self.legacy_path = self.base_dir / "state.json"
        self._lock = asyncio.Lock()

    async def load(self) -> BotState:
        """Load bot state from per-guild SQLite databases (migrating legacy JSON)."""
        async with self._lock:
            self.guilds_dir.mkdir(parents=True, exist_ok=True)

            if self.legacy_path.exists():
                LOGGER.info("Migrating legacy JSON state to per-guild SQLite databases.")
                legacy_data = await asyncio.to_thread(self.legacy_path.read_text, encoding="utf-8")
                payload = json.loads(legacy_data)
                state = BotState.from_payload(payload)
                await asyncio.to_thread(self._write_all_guilds, state)
                backup = self.legacy_path.with_suffix(".json.bak")
                self.legacy_path.replace(backup)
                LOGGER.info("Legacy state migrated; backup saved to %s", backup)
                return state

            state = BotState()
            for db_path in self.guilds_dir.glob("guild_*.sqlite"):
                guild_id = self._guild_id_from_path(db_path)
                if guild_id is None:
                    continue
                guild_state = await asyncio.to_thread(self._read_guild_db, db_path, guild_id)
                state.guilds[guild_id] = guild_state
            return state

    async def save(self, state: BotState) -> None:
        """Persist the provided bot state to the respective guild databases."""
        async with self._lock:
            self.guilds_dir.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(self._write_all_guilds, state)

    async def overwrite(self, state: BotState) -> None:
        """Alias for save, provided for semantic clarity when replacing state."""
        await self.save(state)

    # --- Internal helpers -------------------------------------------------

    def _guild_path(self, guild_id: int) -> Path:
        return self.guilds_dir / f"guild_{guild_id}.sqlite"

    @staticmethod
    def _guild_id_from_path(path: Path) -> int | None:
        match = re.match(r"guild_(\d+)\.sqlite$", path.name)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    def _write_all_guilds(self, state: BotState) -> None:
        guild_items = list(state.iter_guild_states())
        expected_paths = {self._guild_path(guild_id).resolve() for guild_id, _ in guild_items}
        for guild_id, guild_state in guild_items:
            self._write_guild_db(self._guild_path(guild_id), guild_id, guild_state)

        existing_paths = {path.resolve() for path in self.guilds_dir.glob("guild_*.sqlite")}
        for stale_path in existing_paths - expected_paths:
            try:
                Path(stale_path).unlink()
            except OSError as exc:
                LOGGER.warning("Unable to remove stale guild database %s: %s", stale_path, exc)

    def _write_guild_db(self, path: Path, guild_id: int, guild_state: GuildState) -> None:
        conn = sqlite3.connect(path)
        try:
            self._ensure_schema(conn)
            conn.execute("BEGIN")

            conn.execute("DELETE FROM metadata")
            conn.execute("DELETE FROM schedule_runs")
            conn.execute("DELETE FROM admin_roles")
            conn.execute("DELETE FROM giveaways")
            conn.execute("DELETE FROM pending_giveaways")
            conn.execute("DELETE FROM recurring_giveaways")
            conn.execute("DELETE FROM recent_winners")

            meta_entries = [
                ("auto_enabled", json.dumps(guild_state.auto_enabled)),
                ("timezone", guild_state.timezone),
                ("logger_channel_id", json.dumps(guild_state.logger_channel_id)),
                ("recent_winner_cooldown_enabled", json.dumps(guild_state.recent_winner_cooldown_enabled)),
                ("recent_winner_cooldown_days", json.dumps(guild_state.recent_winner_cooldown_days)),
            ]
            conn.executemany("INSERT INTO metadata(key, value) VALUES (?, ?)", meta_entries)

            if guild_state.schedule_runs:
                conn.executemany(
                    "INSERT INTO schedule_runs(schedule_id, last_run) VALUES (?, ?)",
                    [(schedule_id, last_run) for schedule_id, last_run in guild_state.schedule_runs.items()],
                )

            if guild_state.admin_roles:
                conn.executemany(
                    "INSERT INTO admin_roles(role_id) VALUES (?)",
                    [(int(role_id),) for role_id in guild_state.admin_roles],
                )

            if guild_state.giveaways:
                conn.executemany(
                    """
                    INSERT INTO giveaways(
                        id,
                        channel_id,
                        message_id,
                        winners,
                        title,
                        description,
                        end_time,
                        created_at,
                        participants,
                        scheduled_id,
                        is_active,
                        last_announced_winners
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            giveaway.id,
                            giveaway.channel_id,
                            giveaway.message_id,
                            giveaway.winners,
                            giveaway.title,
                            giveaway.description,
                            giveaway.end_time.isoformat(),
                            giveaway.created_at.isoformat(),
                            json.dumps(list(map(int, giveaway.participants))),
                            giveaway.scheduled_id,
                            1 if giveaway.is_active else 0,
                            json.dumps(list(map(int, giveaway.last_announced_winners))),
                        )
                        for giveaway in guild_state.giveaways
                    ],
                )

            if guild_state.pending_giveaways:
                conn.executemany(
                    """
                    INSERT INTO pending_giveaways(
                        id,
                        channel_id,
                        winners,
                        title,
                        description,
                        start_time,
                        end_time
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            pending.id,
                            pending.channel_id,
                            pending.winners,
                            pending.title,
                            pending.description,
                            pending.start_time.isoformat(),
                            pending.end_time.isoformat(),
                        )
                        for pending in guild_state.pending_giveaways
                    ],
                )

            if guild_state.recurring_giveaways:
                conn.executemany(
                    """
                    INSERT INTO recurring_giveaways(
                        id,
                        channel_id,
                        winners,
                        title,
                        description,
                        start_time,
                        end_time,
                        next_start,
                        next_end,
                        enabled
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            recurring.id,
                            recurring.channel_id,
                            recurring.winners,
                            recurring.title,
                            recurring.description,
                            recurring.start_time.strftime("%H:%M"),
                            recurring.end_time.strftime("%H:%M"),
                            recurring.next_start.isoformat(),
                            recurring.next_end.isoformat(),
                            1 if recurring.enabled else 0,
                        )
                        for recurring in guild_state.recurring_giveaways
                    ],
                )

            if guild_state.recent_winners:
                conn.executemany(
                    "INSERT INTO recent_winners(user_id, giveaway_id, won_at) VALUES (?, ?, ?)",
                    [
                        (
                            winner.user_id,
                            winner.giveaway_id,
                            winner.won_at.isoformat(),
                        )
                        for winner in guild_state.recent_winners
                    ],
                )

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _read_guild_db(self, path: Path, guild_id: int) -> GuildState:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            self._ensure_schema(conn)
            guild_state = GuildState()

            metadata = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM metadata")}
            guild_state.auto_enabled = bool(json.loads(metadata.get("auto_enabled", "true")))
            guild_state.timezone = metadata.get("timezone", "Europe/Berlin")
            guild_state.logger_channel_id = json.loads(metadata.get("logger_channel_id", "null"))
            guild_state.recent_winner_cooldown_enabled = bool(
                json.loads(metadata.get("recent_winner_cooldown_enabled", "false"))
            )
            guild_state.recent_winner_cooldown_days = int(json.loads(metadata.get("recent_winner_cooldown_days", "0")))

            guild_state.schedule_runs = {
                row["schedule_id"]: row["last_run"]
                for row in conn.execute("SELECT schedule_id, last_run FROM schedule_runs")
            }
            guild_state.admin_roles = [int(row["role_id"]) for row in conn.execute("SELECT role_id FROM admin_roles")]

            for row in conn.execute("SELECT * FROM giveaways"):
                participants = json.loads(row["participants"]) if row["participants"] else []
                last_winners = json.loads(row["last_announced_winners"]) if row["last_announced_winners"] else []
                giveaway = Giveaway(
                    id=row["id"],
                    guild_id=guild_id,
                    channel_id=row["channel_id"],
                    message_id=row["message_id"],
                    winners=row["winners"],
                    title=row["title"],
                    description=row["description"],
                    end_time=datetime.fromisoformat(row["end_time"]),
                    created_at=datetime.fromisoformat(row["created_at"]),
                    participants=[int(value) for value in participants],
                    scheduled_id=row["scheduled_id"],
                    is_active=bool(row["is_active"]),
                    last_announced_winners=[int(value) for value in last_winners],
                )
                guild_state.giveaways.append(giveaway)

            for row in conn.execute("SELECT * FROM pending_giveaways"):
                pending = PendingGiveaway(
                    id=row["id"],
                    guild_id=guild_id,
                    channel_id=row["channel_id"],
                    winners=row["winners"],
                    title=row["title"],
                    description=row["description"],
                    start_time=datetime.fromisoformat(row["start_time"]),
                    end_time=datetime.fromisoformat(row["end_time"]),
                )
                guild_state.pending_giveaways.append(pending)

            for row in conn.execute("SELECT * FROM recurring_giveaways"):
                start_time_obj = datetime.strptime(row["start_time"], "%H:%M").time()
                end_time_obj = datetime.strptime(row["end_time"], "%H:%M").time()
                recurring = RecurringGiveaway(
                    id=row["id"],
                    guild_id=guild_id,
                    channel_id=row["channel_id"],
                    winners=row["winners"],
                    title=row["title"],
                    description=row["description"],
                    start_time=start_time_obj,
                    end_time=end_time_obj,
                    next_start=datetime.fromisoformat(row["next_start"]),
                    next_end=datetime.fromisoformat(row["next_end"]),
                    enabled=bool(row["enabled"]),
                )
                guild_state.recurring_giveaways.append(recurring)

            for row in conn.execute(
                "SELECT user_id, giveaway_id, won_at FROM recent_winners ORDER BY won_at"
            ):
                guild_state.recent_winners.append(
                    RecentWinner(
                        user_id=int(row["user_id"]),
                        giveaway_id=row["giveaway_id"],
                        won_at=datetime.fromisoformat(row["won_at"]),
                    )
                )

            return guild_state
        finally:
            conn.close()

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schedule_runs (
                schedule_id TEXT PRIMARY KEY,
                last_run TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_roles (
                role_id INTEGER PRIMARY KEY
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS giveaways (
                id TEXT PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                winners INTEGER NOT NULL,
                title TEXT,
                description TEXT,
                end_time TEXT NOT NULL,
                created_at TEXT NOT NULL,
                participants TEXT,
                scheduled_id TEXT,
                is_active INTEGER NOT NULL,
                last_announced_winners TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_giveaways (
                id TEXT PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                winners INTEGER NOT NULL,
                title TEXT,
                description TEXT,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recurring_giveaways (
                id TEXT PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                winners INTEGER NOT NULL,
                title TEXT,
                description TEXT,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                next_start TEXT NOT NULL,
                next_end TEXT NOT NULL,
                enabled INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recent_winners (
                user_id INTEGER NOT NULL,
                giveaway_id TEXT NOT NULL,
                won_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_recent_winners_won_at
            ON recent_winners(won_at)
            """
        )
