from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

from .models import BotState


class StateStorage:
    """Simple async wrapper around JSON state persistence."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def load(self) -> BotState:
        async with self._lock:
            if not self.path.exists():
                return BotState()
            data = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
            payload = json.loads(data)
            return BotState.from_payload(payload)

    async def save(self, state: BotState) -> None:
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = state.to_payload()
            data = json.dumps(payload, indent=2, sort_keys=True)
            await asyncio.to_thread(self.path.write_text, data, "utf-8")

    async def overwrite(self, state: BotState) -> None:
        await self.save(state)
