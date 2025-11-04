"""Asynchronous JSON persistence helpers for giveaway state."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

from .models import BotState


class StateStorage:
    """Simple async wrapper around JSON state persistence."""

    def __init__(self, path: Path) -> None:
        """Create a storage helper bound to the specified JSON path."""
        self.path = path
        self._lock = asyncio.Lock()

    async def load(self) -> BotState:
        """Load bot state from disk, returning an empty state when missing."""
        async with self._lock:
            if not self.path.exists():
                return BotState()
            data = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
            payload = json.loads(data)
            return BotState.from_payload(payload)

    async def save(self, state: BotState) -> None:
        """Persist the provided bot state to disk atomically."""
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = state.to_payload()
            data = json.dumps(payload, indent=2, sort_keys=True)
            await asyncio.to_thread(self.path.write_text, data, "utf-8")

    async def overwrite(self, state: BotState) -> None:
        """Alias for save, provided for semantic clarity when replacing state."""
        await self.save(state)
