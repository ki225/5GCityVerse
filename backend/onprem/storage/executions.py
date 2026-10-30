"""Atomic JSON repository for browser event executions."""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
from typing import Any
TERMINAL_STATUSES = frozenset({'AGENT_COMPLETE', 'AGENT_DEGRADED', 'AGENT_BLOCKED', 'AGENT_FAILED',
    'AGENT_CANCELLED', 'SIMULATION_COMPLETE', 'SIMULATION_DEGRADED', 'SIMULATION_BLOCKED'})
from backend.onprem.application.views import utc_now

class ExecutionRepository:
    """Small JSON-backed store suitable for one local server process."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.executions: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
            body = json.loads(raw)
            if isinstance(body, dict) and isinstance(body.get("executions"), dict):
                self.executions = body["executions"]
                for item in self.executions.values():
                    if item.get("status") not in TERMINAL_STATUSES:
                        item.update({
                            "status": "AGENT_CANCELLED",
                            "progressStage": "server_restarted",
                            "detail": "Local server restarted before execution completed",
                            "completed_at": utc_now(),
                        })
        except (OSError, ValueError, TypeError):
            # A corrupt optional cache must not prevent the local server starting.
            self.executions = {}

    async def save(self) -> None:
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({"version": 1, "executions": self.executions}, ensure_ascii=False, indent=2)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            await asyncio.to_thread(temporary.write_text, payload, encoding="utf-8")
            await asyncio.to_thread(os.replace, temporary, self.path)

    async def put(self, execution_id: str, record: dict[str, Any]) -> None:
        self.executions[execution_id] = record
        await self.save()

    async def update(self, execution_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        record = self.executions.get(execution_id)
        if record is None:
            return None
        record.update(patch)
        record["updated_at"] = utc_now()
        await self.save()
        return record

    async def clear(self) -> list[dict[str, Any]]:
        previous = list(self.executions.values())
        self.executions.clear()
        await self.save()
        return previous

    async def clear_session(self, session_id: str) -> list[dict[str, Any]]:
        previous = [record for record in self.executions.values() if record.get("sessionId") == session_id]
        self.executions = {
            execution_id: record for execution_id, record in self.executions.items()
            if record.get("sessionId") != session_id
        }
        await self.save()
        return previous

