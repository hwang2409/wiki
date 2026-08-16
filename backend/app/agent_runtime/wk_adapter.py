"""Supervisor adapter for the opt-in wk provider lanes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from .provider import AdapterStatus, ProviderAdapter, ProviderEvent, ProviderProtocolError, StartRequest
from .types import LifecycleState, ProviderKind, RunRecord
from .wk_core import WkLoop, WkRunMetadata


class WkProviderAdapter(ProviderAdapter):
    """Adapt a WkProvider to the supervisor's durable provider contract."""

    def __init__(
        self,
        record: RunRecord,
        *,
        status_path: Path,
        command: Sequence[str],
        env: Mapping[str, str] | None,
    ) -> None:
        if record.execution_kind not in {"wk-claude", "wk-codex"}:
            raise ValueError("WkProviderAdapter requires a wk execution kind")
        self.provider = record.provider
        self.record = record
        self.loop = WkLoop(status_path=status_path)
        self._state = record.state
        self._session_id = record.provider_session_id
        self._pid: int | None = record.provider_pid
        self._generation = max(1, record.provider_generation)
        self._active_turn_id = record.active_turn_id
        self._core_phase: str | None = record.core_phase or "run"
        self._detail: str | None = None
        self._lane: Any = self._build_lane(record, command=command, env=env)
        self._process_created_callback: Any = None
        self._closed = False

        underlying = getattr(self._lane, "_adapter", None)
        if underlying is not None:
            underlying.set_process_created_callback(self._process_created)

    def _build_lane(
        self,
        record: RunRecord,
        *,
        command: Sequence[str],
        env: Mapping[str, str] | None,
    ) -> Any:
        metadata = WkRunMetadata.from_kind(record.execution_kind or "")
        kwargs = {
            "metadata": metadata,
            "run_id": record.run_id,
            "agent_id": record.agent_id,
            "worktree": Path(record.worktree),
            "model": record.model,
            "loop": self.loop,
        }
        if record.execution_kind == "wk-codex":
            from .wk_codex import WkCodexLane

            return WkCodexLane(
                **kwargs,
                command=command,
                environment=env,
            )
        from .wk_claude import WkClaudeLane

        return WkClaudeLane(
            **kwargs,
            environment=env,
            cli_path=command[0] if command else None,
        )

    def _process_created(self, pid: int) -> None:
        self._pid = pid
        if self._process_created_callback is not None:
            self._process_created_callback(pid)

    def set_process_created_callback(self, callback: Any) -> None:
        self._process_created_callback = callback

    def _status_from_lane(self) -> AdapterStatus:
        underlying = getattr(self._lane, "_adapter", None)
        if underlying is not None:
            status = underlying.snapshot()
            self._state = status.state
            self._session_id = status.session_id
            self._pid = status.pid
            self._generation = status.generation
            self._active_turn_id = status.active_turn_id
            self._detail = status.detail
        return AdapterStatus(
            state=self._state,
            session_id=self._session_id,
            pid=self._pid,
            generation=self._generation,
            active_turn_id=self._active_turn_id,
            transcript_path=self.record.transcript_path,
            detail=self._detail,
            core_phase=self._core_phase,
        )

    @staticmethod
    def _state_for_raw(raw: Mapping[str, Any], current: LifecycleState) -> LifecycleState:
        method = str(raw.get("method") or "")
        event_type = str(raw.get("type") or "")
        subtype = str(raw.get("subtype") or "")
        if "approval" in method or event_type == "control_request":
            return LifecycleState.WAITING_APPROVAL
        if method in {"turn/started"} or event_type in {"assistant", "stream_event"}:
            return LifecycleState.WORKING
        if method == "turn/completed" or event_type == "result":
            return LifecycleState.BLOCKED if raw.get("is_error") else LifecycleState.IDLE
        if method in {"thread/archived", "thread/closed"}:
            return LifecycleState.COMPLETED
        if event_type == "system" and subtype == "status":
            return {
                "requesting": LifecycleState.WORKING,
                "running": LifecycleState.WORKING,
                "idle": LifecycleState.IDLE,
            }.get(str(raw.get("status")), current)
        if event_type == "provider_error" or method == "error":
            return LifecycleState.BLOCKED
        return current

    async def start(self, request: StartRequest) -> AdapterStatus:
        self._state = LifecycleState.STARTING
        await self._lane.start(request.prompt)
        self._state = LifecycleState.WORKING
        self.loop.write_status(
            state="working",
            pr=None,
            step="wk provider turn is active",
            blocker=None,
        )
        return self._status_from_lane()

    async def resume(self, session_id: str) -> AdapterStatus:
        await self._lane.resume(session_id)
        self._state = LifecycleState.WORKING
        return self._status_from_lane()

    async def send_now(self, message: str) -> AdapterStatus:
        await self._lane.send_now(message)
        self._state = LifecycleState.WORKING
        return self._status_from_lane()

    async def send_on_idle(self, message: str) -> AdapterStatus:
        await self._lane.send_on_idle(message)
        return self._status_from_lane()

    async def interrupt(self) -> AdapterStatus:
        await self._lane.interrupt()
        self._state = LifecycleState.INTERRUPTED
        return self._status_from_lane()

    async def stop(self) -> AdapterStatus:
        if not self._closed:
            await self._lane.close()
            self._closed = True
        self._state = LifecycleState.DEAD
        return self._status_from_lane()

    async def replace(
        self,
        new_prompt: str,
        model: str | None = None,
        effort: str | None = None,
    ) -> AdapterStatus:
        del model, effort
        await self._lane.replace(new_prompt)
        self._state = LifecycleState.WORKING
        return self._status_from_lane()

    async def status(self) -> AdapterStatus:
        return self._status_from_lane()

    def snapshot(self) -> AdapterStatus:
        return self._status_from_lane()

    async def archive(self) -> AdapterStatus:
        await self._lane.archive()
        self._state = LifecycleState.COMPLETED
        return self._status_from_lane()

    async def respond(self, request_id: str | int, response: dict[str, Any]) -> AdapterStatus:
        responder = getattr(self._lane, "respond", None)
        if responder is None:
            raise ProviderProtocolError("wk Claude lane does not support provider responses")
        await responder(request_id, response)
        return self._status_from_lane()

    def events(self) -> AsyncIterator[ProviderEvent]:
        return self._event_stream()

    async def _event_stream(self) -> AsyncIterator[ProviderEvent]:
        async for item in self._lane.events():
            raw = item.get("raw") if isinstance(item, Mapping) else None
            envelope = item.get("event") if isinstance(item, Mapping) else None
            if not isinstance(raw, Mapping) or not isinstance(envelope, Mapping):
                raise ProviderProtocolError("wk lane emitted an incomplete event")
            payload = dict(raw)
            payload["_wk_event"] = dict(envelope)
            self._state = self._state_for_raw(raw, self._state)
            if isinstance(envelope.get("phase"), str):
                self._core_phase = envelope["phase"]
            yield ProviderEvent(
                provider=self.provider,
                payload=payload,
                direction="provider",
                generation=self._generation,
            )

    async def close(self) -> None:
        await self.stop()


__all__ = ["WkProviderAdapter"]
