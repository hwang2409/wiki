from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid5, NAMESPACE_URL

from .provider import AdapterStatus, ProviderAdapter, ProviderEvent, StartRequest
from .types import LifecycleState, ProviderKind, RunRecord


class WireFixture:
    """Captured App Server exchange grouped by the client method that caused it."""

    def __init__(self, path: Path):
        self.path = path
        self.segments: dict[str, list[dict[str, Any]]] = {}
        active = "bootstrap"
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            row = json.loads(line)
            message = row["message"]
            if row["direction"] == "client":
                method = message.get("method")
                if isinstance(method, str):
                    active = method
                continue
            self.segments.setdefault(active, []).append(message)

    def server_messages(self, method: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self.segments.get(method, [])]

    def response_result(self, method: str) -> dict[str, Any] | None:
        for message in self.segments.get(method, []):
            result = message.get("result")
            if isinstance(result, dict):
                return result
        return None


class CodexFixtureAdapter(ProviderAdapter):
    """Protocol fake replaying sanitized wire messages captured from Codex 0.144.0."""

    provider = ProviderKind.CODEX

    def __init__(
        self,
        success_fixture: Path,
        control_fixture: Path,
        *,
        pid: int | None = None,
        generation: int = 0,
    ):
        self.success = WireFixture(success_fixture)
        self.control = WireFixture(control_fixture)
        self.pid = os.getpid() if pid is None else pid
        self._events: asyncio.Queue[ProviderEvent | None] = asyncio.Queue()
        self._status = AdapterStatus(
            LifecycleState.STARTING,
            None,
            self.pid,
            generation=generation,
        )
        self._request: StartRequest | None = None
        self._queued: list[str] = []
        self.replayed_methods: list[str] = []
        self.closed = False

    async def _emit(
        self,
        fixture: WireFixture,
        method: str,
        *,
        generation: int | None = None,
    ) -> None:
        self.replayed_methods.append(method)
        event_generation = generation or max(1, self._status.generation)
        for message in fixture.server_messages(method):
            await self._events.put(
                ProviderEvent(
                    self.provider,
                    message,
                    direction="server",
                    generation=event_generation,
                )
            )

    @staticmethod
    def _session_id(fixture: WireFixture, method: str) -> str | None:
        result = fixture.response_result(method) or {}
        thread = result.get("thread") or {}
        value = thread.get("id") or thread.get("sessionId")
        return value if isinstance(value, str) else None

    @staticmethod
    def _turn_id(fixture: WireFixture, method: str) -> str | None:
        result = fixture.response_result(method) or {}
        turn = result.get("turn") or {}
        value = turn.get("id") or result.get("turnId")
        return value if isinstance(value, str) else None

    async def start(self, request: StartRequest) -> AdapterStatus:
        self._request = request
        generation = self._status.generation + 1
        await self._emit(self.success, "initialize", generation=generation)
        await self._emit(self.success, "thread/start", generation=generation)
        await self._emit(self.success, "turn/start", generation=generation)
        session_id = self._session_id(self.success, "thread/start") or str(
            uuid5(NAMESPACE_URL, f"codex:{request.run_id}")
        )
        self._status = AdapterStatus(
            LifecycleState.IDLE,
            session_id,
            self.pid,
            generation=generation,
        )
        return self._status

    async def resume(self, session_id: str) -> AdapterStatus:
        generation = self._status.generation + 1
        await self._emit(self.control, "thread/resume", generation=generation)
        self._status = AdapterStatus(
            LifecycleState.IDLE,
            session_id,
            self.pid,
            generation=generation,
        )
        return self._status

    async def send_now(self, message: str) -> AdapterStatus:
        if self._status.state is LifecycleState.WORKING and self._status.active_turn_id:
            await self._emit(self.control, "turn/steer")
            return self._status
        # The captured control turn intentionally remains in progress so a
        # second send exercises App Server's real turn/steer contract.
        await self._emit(self.control, "turn/start")
        turn_id = self._turn_id(self.control, "turn/start")
        self._status = AdapterStatus(
            LifecycleState.WORKING,
            self._status.session_id,
            self.pid,
            generation=max(1, self._status.generation),
            active_turn_id=turn_id,
        )
        return self._status

    async def send_on_idle(self, message: str) -> AdapterStatus:
        if self._status.state is LifecycleState.IDLE:
            return await self.send_now(message)
        self._queued.append(message)
        return self._status

    async def interrupt(self) -> AdapterStatus:
        await self._emit(self.control, "turn/interrupt")
        self._status = AdapterStatus(
            LifecycleState.INTERRUPTED,
            self._status.session_id,
            self.pid,
            generation=max(1, self._status.generation),
        )
        return self._status

    async def stop(self) -> AdapterStatus:
        self._status = AdapterStatus(
            LifecycleState.DEAD,
            self._status.session_id,
            None,
            generation=max(1, self._status.generation),
            detail="stopped",
        )
        return self._status

    async def replace(self, new_prompt: str, model: str | None = None) -> AdapterStatus:
        if self._request is None:
            raise RuntimeError("adapter has not started")
        request = StartRequest(
            prompt=new_prompt,
            model=model or self._request.model,
            effort=self._request.effort,
            worktree=self._request.worktree,
            run_id=self._request.run_id,
            agent_id=self._request.agent_id,
        )
        await self.stop()
        return await self.start(request)

    async def status(self) -> AdapterStatus:
        return self._status

    def snapshot(self) -> AdapterStatus:
        return self._status

    async def _event_stream(self) -> AsyncIterator[ProviderEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    def events(self) -> AsyncIterator[ProviderEvent]:
        return self._event_stream()

    async def archive(self) -> AdapterStatus:
        await self._emit(self.success, "thread/archive")
        self._status = AdapterStatus(
            LifecycleState.COMPLETED,
            self._status.session_id,
            None,
            generation=max(1, self._status.generation),
        )
        return self._status

    async def respond(
        self, request_id: str | int, response: dict[str, Any]
    ) -> AdapterStatus:
        await self._events.put(
            ProviderEvent(
                self.provider,
                {"id": request_id, "result": response},
                direction="client",
                generation=max(1, self._status.generation),
            )
        )
        return self._status

    async def close(self) -> None:
        self.closed = True
        await self._events.put(None)


class ClaudeFixtureAdapter(ProviderAdapter):
    """Stream-json fake replaying sanitized Claude transcript rows."""

    provider = ProviderKind.CLAUDE

    def __init__(self, fixture: Path, *, pid: int | None = None, generation: int = 0):
        self.fixture = fixture
        self.pid = os.getpid() if pid is None else pid
        self._rows = [
            json.loads(line)
            for line in fixture.read_text(encoding="utf-8").splitlines()
            if line
        ]
        self._events: asyncio.Queue[ProviderEvent | None] = asyncio.Queue()
        self._status = AdapterStatus(
            LifecycleState.STARTING,
            None,
            self.pid,
            generation=generation,
        )
        self._request: StartRequest | None = None
        self._queued: list[str] = []
        self.closed = False

    async def _replay(self, *, generation: int | None = None) -> None:
        event_generation = generation or max(1, self._status.generation)
        for row in self._rows:
            await self._events.put(
                ProviderEvent(
                    self.provider,
                    dict(row),
                    direction="stdout",
                    generation=event_generation,
                )
            )

    async def start(self, request: StartRequest) -> AdapterStatus:
        self._request = request
        generation = self._status.generation + 1
        await self._replay(generation=generation)
        session_id = str(uuid5(NAMESPACE_URL, f"claude:{request.run_id}"))
        self._status = AdapterStatus(
            LifecycleState.IDLE,
            session_id,
            self.pid,
            generation=generation,
        )
        return self._status

    async def resume(self, session_id: str) -> AdapterStatus:
        generation = self._status.generation + 1
        await self._replay(generation=generation)
        self._status = AdapterStatus(
            LifecycleState.IDLE,
            session_id,
            self.pid,
            generation=generation,
        )
        return self._status

    async def send_now(self, message: str) -> AdapterStatus:
        await self._replay()
        self._status = AdapterStatus(
            LifecycleState.IDLE,
            self._status.session_id,
            self.pid,
            generation=max(1, self._status.generation),
        )
        return self._status

    async def send_on_idle(self, message: str) -> AdapterStatus:
        if self._status.state is LifecycleState.IDLE:
            return await self.send_now(message)
        self._queued.append(message)
        return self._status

    async def interrupt(self) -> AdapterStatus:
        await self._events.put(
            ProviderEvent(
                self.provider,
                {
                    "type": "result",
                    "subtype": "interrupted",
                    "is_error": True,
                    "result": "interrupted by fixture client",
                },
                direction="stdout",
                generation=max(1, self._status.generation),
            )
        )
        self._status = AdapterStatus(
            LifecycleState.INTERRUPTED,
            self._status.session_id,
            self.pid,
            generation=max(1, self._status.generation),
        )
        return self._status

    async def stop(self) -> AdapterStatus:
        self._status = AdapterStatus(
            LifecycleState.DEAD,
            self._status.session_id,
            None,
            generation=max(1, self._status.generation),
            detail="stopped",
        )
        return self._status

    async def replace(self, new_prompt: str, model: str | None = None) -> AdapterStatus:
        if self._request is None:
            raise RuntimeError("adapter has not started")
        request = StartRequest(
            prompt=new_prompt,
            model=model or self._request.model,
            effort=self._request.effort,
            worktree=self._request.worktree,
            run_id=self._request.run_id,
            agent_id=self._request.agent_id,
        )
        await self.stop()
        return await self.start(request)

    async def status(self) -> AdapterStatus:
        return self._status

    def snapshot(self) -> AdapterStatus:
        return self._status

    async def _event_stream(self) -> AsyncIterator[ProviderEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    def events(self) -> AsyncIterator[ProviderEvent]:
        return self._event_stream()

    async def archive(self) -> AdapterStatus:
        self._status = AdapterStatus(
            LifecycleState.COMPLETED,
            self._status.session_id,
            None,
            generation=max(1, self._status.generation),
        )
        return self._status

    async def respond(
        self, request_id: str | int, response: dict[str, Any]
    ) -> AdapterStatus:
        await self._events.put(
            ProviderEvent(
                self.provider,
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": request_id,
                        "response": response,
                    },
                },
                direction="stdin",
                generation=max(1, self._status.generation),
            )
        )
        return self._status

    async def close(self) -> None:
        self.closed = True
        await self._events.put(None)


class FixtureAdapterFactory:
    def __init__(self, fixture_dir: Path, *, pid: int | None = None):
        self.fixture_dir = fixture_dir
        self.pid = pid

    def __call__(self, record: RunRecord) -> ProviderAdapter:
        provider = record.provider
        if provider is ProviderKind.CODEX:
            return CodexFixtureAdapter(
                self.fixture_dir / "codex_app_server_success.jsonl",
                self.fixture_dir / "codex_app_server_control.jsonl",
                pid=self.pid,
                generation=record.provider_generation,
            )
        return ClaudeFixtureAdapter(
            self.fixture_dir / "claude_stream_native_surfaces.jsonl",
            pid=self.pid,
            generation=record.provider_generation,
        )
