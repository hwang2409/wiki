from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .types import EventDisposition, LifecycleState, ProviderKind


@dataclass(frozen=True)
class NormalizedProviderEvent:
    disposition: EventDisposition
    kind: str
    payload: dict[str, Any]
    lifecycle_state: LifecycleState | None = None


_CODEX_RENDERED_METHODS = {
    "thread/started",
    "turn/started",
    "turn/completed",
    "item/started",
    "item/completed",
    "item/fileChange/outputDelta",
    "item/commandExecution/outputDelta",
    "error",
    "account/rateLimits/updated",
    "context/compacted",
    "thread/archived",
    "thread/closed",
}
_CODEX_SUMMARIZED_METHODS = {
    "item/agentMessage/delta",
    "item/reasoning/summaryTextDelta",
    "thread/tokenUsage/updated",
    "thread/status/changed",
}
_CODEX_IGNORED_METHODS = {
    "rawResponseItem/completed",
    "mcpServer/startupStatus/updated",
    "remoteControl/status/changed",
    "thread/settings/updated",
}
_CODEX_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/tool/requestUserInput",
}


def _codex_state(payload: dict[str, Any]) -> LifecycleState | None:
    method = payload.get("method")
    params = payload.get("params") or {}
    if method in _CODEX_APPROVAL_METHODS:
        return LifecycleState.WAITING_APPROVAL
    if method == "turn/started":
        return LifecycleState.WORKING
    if method == "thread/status/changed":
        status = (params.get("status") or {}).get("type")
        return {
            "active": LifecycleState.WORKING,
            "idle": LifecycleState.IDLE,
        }.get(status)
    if method == "turn/completed":
        status = (params.get("turn") or {}).get("status")
        return {
            "completed": LifecycleState.IDLE,
            "interrupted": LifecycleState.INTERRUPTED,
            "failed": LifecycleState.BLOCKED,
        }.get(status)
    if method in {"thread/archived", "thread/closed"}:
        return LifecycleState.COMPLETED
    if method == "error" and not params.get("willRetry"):
        return LifecycleState.BLOCKED
    return None


def _normalize_codex(payload: dict[str, Any]) -> NormalizedProviderEvent:
    method = payload.get("method")
    lifecycle = _codex_state(payload)
    if method in _CODEX_APPROVAL_METHODS:
        disposition = EventDisposition.RENDERED
        kind = "approval"
    elif method in _CODEX_RENDERED_METHODS:
        disposition = EventDisposition.RENDERED
        kind = str(method).replace("/", "_")
    elif method in _CODEX_SUMMARIZED_METHODS:
        disposition = EventDisposition.SUMMARIZED
        kind = str(method).replace("/", "_")
    elif method in _CODEX_IGNORED_METHODS:
        disposition = EventDisposition.IGNORED
        kind = str(method).replace("/", "_")
    elif method is None and "id" in payload:
        disposition = EventDisposition.IGNORED
        kind = "rpc_response"
    else:
        disposition = EventDisposition.UNKNOWN
        kind = str(method or "unknown")
    return NormalizedProviderEvent(disposition, kind, payload, lifecycle)


_CLAUDE_RENDERED_TYPES = {
    "assistant",
    "user",
    "result",
    "task_notification",
    "stream_event",
}
_CLAUDE_SUMMARIZED_SYSTEM_SUBTYPES = {
    "status",
    "hook_started",
    "hook_progress",
    "hook_response",
    "task_started",
    "task_progress",
}


def _normalize_claude(payload: dict[str, Any]) -> NormalizedProviderEvent:
    event_type = payload.get("type")
    subtype = payload.get("subtype")
    state = None
    if event_type == "control_request":
        state = LifecycleState.WAITING_APPROVAL
        return NormalizedProviderEvent(EventDisposition.RENDERED, "approval", payload, state)
    if event_type == "result":
        if subtype in {"interrupted", "interrupt"}:
            state = LifecycleState.INTERRUPTED
        else:
            state = LifecycleState.BLOCKED if payload.get("is_error") else LifecycleState.IDLE
    elif event_type == "system" and subtype == "status":
        status = payload.get("status")
        state = {
            "requesting": LifecycleState.WORKING,
            "running": LifecycleState.WORKING,
            "idle": LifecycleState.IDLE,
        }.get(status)
    if event_type in _CLAUDE_RENDERED_TYPES:
        return NormalizedProviderEvent(
            EventDisposition.RENDERED,
            f"claude_{event_type}",
            payload,
            state,
        )
    if event_type == "system" and subtype in _CLAUDE_SUMMARIZED_SYSTEM_SUBTYPES:
        return NormalizedProviderEvent(
            EventDisposition.SUMMARIZED,
            f"claude_{subtype}",
            payload,
            state,
        )
    if event_type in {"control_response", "keep_alive"}:
        return NormalizedProviderEvent(EventDisposition.IGNORED, f"claude_{event_type}", payload, state)
    return NormalizedProviderEvent(
        EventDisposition.UNKNOWN,
        f"claude_{event_type or 'unknown'}",
        payload,
        state,
    )


def normalize_provider_event(
    provider: ProviderKind,
    payload: dict[str, Any],
) -> NormalizedProviderEvent:
    if provider is ProviderKind.CODEX:
        return _normalize_codex(payload)
    return _normalize_claude(payload)
