from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..wiki_artifacts import artifact_from_codex_mcp_tool_result
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
    "item/mcpToolCall/outputDelta",
    "item/dynamicToolCall/outputDelta",
    "item/commandExecution/terminalInteraction",
    "item/delta",
    "turn/diff/updated",
    "warning",
    "skills/changed",
    "turn/plan/updated",
    "error",
    "account/rateLimits/updated",
    "context/compacted",
    "account/chatgptAuthTokens/refresh",
    "serverRequest/resolved",
    "thread/archived",
    "thread/closed",
}
_CODEX_SUMMARIZED_METHODS = {
    "item/agentMessage/delta",
    "item/reasoning/summaryTextDelta",
    "item/reasoning/summaryPartAdded",
    "hook/started",
    "hook/completed",
    "thread/tokenUsage/updated",
    "thread/status/changed",
}
_CODEX_IGNORED_METHODS = {
    "rawResponseItem/completed",
    "rawResponse/completed",
    "mcpServer/startupStatus/updated",
    "remoteControl/status/changed",
    "thread/settings/updated",
    "thread/goal/cleared",
    "turn/moderationMetadata",
}
_CODEX_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "item/tool/requestUserInput",
    "mcpServer/elicitation/request",
    "execCommandApproval",
    "applyPatchApproval",
}


def _codex_state(payload: dict[str, Any]) -> LifecycleState | None:
    method = payload.get("method")
    params = payload.get("params") or {}
    if method in _CODEX_APPROVAL_METHODS:
        return LifecycleState.WAITING_APPROVAL
    if method == "turn/started":
        return LifecycleState.WORKING
    if method == "thread/status/changed":
        status_value = params.get("status") or {}
        active_flags = set(status_value.get("activeFlags") or [])
        if active_flags & {"waitingOnApproval", "waitingOnUserInput"}:
            return LifecycleState.WAITING_APPROVAL
        status = status_value.get("type")
        return {
            "active": LifecycleState.WORKING,
            "idle": LifecycleState.IDLE,
            "systemError": LifecycleState.BLOCKED,
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
    if method == "account/chatgptAuthTokens/refresh":
        return LifecycleState.BLOCKED
    return None


def _codex_moderation_flags(params: object) -> list[str]:
    """Extract active flags from Codex's nested moderation metadata payloads."""

    if not isinstance(params, dict):
        return []
    flags: list[str] = []

    def walk(value: object, *, in_flag_map: bool = False) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "is_blocked" and child is True:
                    flags.append("blocked")
                    continue
                is_flag_map = in_flag_map or key in {"flag", "flags", "category_flags", "labels"}
                if is_flag_map and isinstance(child, bool):
                    if child and key.lower() != "safe":
                        flags.append(key)
                elif is_flag_map and isinstance(child, str) and child.lower() != "safe":
                    flags.append(child)
                elif isinstance(child, (dict, list)):
                    walk(child, in_flag_map=is_flag_map)
            return
        if isinstance(value, list):
            for child in value:
                if isinstance(child, dict):
                    name = child.get("flag") or child.get("name") or child.get("type")
                    if isinstance(name, str) and name and name.lower() != "safe":
                        flags.append(name)
                    walk(child, in_flag_map=in_flag_map)
                elif isinstance(child, str) and child.lower() != "safe":
                    flags.append(child)

    walk(params)
    return list(dict.fromkeys(flags))


def _codex_moderation_is_warning(params: object) -> bool:
    return bool(_codex_moderation_flags(params))


def _normalize_codex(payload: dict[str, Any]) -> NormalizedProviderEvent:
    method = payload.get("method")
    lifecycle = _codex_state(payload)
    params = payload.get("params")
    item = params.get("item") if isinstance(params, dict) else None
    artifact = (
        artifact_from_codex_mcp_tool_result(item)
        if method == "item/completed"
        else None
    )
    if artifact is not None:
        return NormalizedProviderEvent(EventDisposition.RENDERED, "artifact", artifact)
    if method in _CODEX_APPROVAL_METHODS:
        disposition = EventDisposition.RENDERED
        kind = "approval"
    elif method == "serverRequest/resolved":
        disposition = EventDisposition.RENDERED
        kind = "approval_resolved"
    elif method == "turn/moderationMetadata" and _codex_moderation_is_warning(params):
        disposition = EventDisposition.RENDERED
        kind = "turn_moderationMetadata_warning"
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
    "permission-mode",
    "progress",
    "pr-link",
}
_CLAUDE_SUMMARIZED_TYPES = {
    "custom-title",
    "agent-name",
}
_CLAUDE_IGNORED_TYPES = {
    "ai-title",
    "file-history-snapshot",
    "last-prompt",
    "mode",
    "queue-operation",
    "started",
}
_CLAUDE_RENDERED_SYSTEM_SUBTYPES = {
    "api_error",
    "api_retry",
    "compact_boundary",
    "init",
    "scheduled_task_fire",
    "stop_hook_summary",
    "task_notification",
    "task_updated",
    "turn_duration",
    "informational",
    "local_command",
    "away_summary",
}
_CLAUDE_SUMMARIZED_SYSTEM_SUBTYPES = {
    "status",
    "hook_started",
    "hook_progress",
    "hook_response",
    "task_started",
    "task_progress",
    "thinking_tokens",
}
_CLAUDE_RATE_LIMIT_FIELDS = {
    "status",
    "rateLimitType",
    "isUsingOverage",
    "overageStatus",
    "overageDisabledReason",
    "resetsAt",
}


def _normalize_claude(payload: dict[str, Any]) -> NormalizedProviderEvent:
    event_type = payload.get("type")
    subtype = payload.get("subtype")
    state = None
    if event_type == "control_request":
        state = LifecycleState.WAITING_APPROVAL
        return NormalizedProviderEvent(
            EventDisposition.RENDERED, "approval", payload, state
        )
    if event_type == "control_cancel_request":
        return NormalizedProviderEvent(
            EventDisposition.RENDERED,
            "approval_cancelled",
            payload,
            LifecycleState.WORKING,
        )
    if event_type == "result":
        if subtype in {"interrupted", "interrupt"}:
            state = LifecycleState.INTERRUPTED
        else:
            state = (
                LifecycleState.BLOCKED
                if payload.get("is_error")
                else LifecycleState.IDLE
            )
    elif event_type == "system" and subtype == "status":
        status = payload.get("status")
        state = {
            "requesting": LifecycleState.WORKING,
            "running": LifecycleState.WORKING,
            "idle": LifecycleState.IDLE,
        }.get(status)
    elif event_type == "system" and subtype == "session_state_changed":
        status = payload.get("state") or payload.get("session_state")
        state = {
            "running": LifecycleState.WORKING,
            "idle": LifecycleState.IDLE,
            "requires_action": LifecycleState.WAITING_APPROVAL,
        }.get(status)
    elif event_type == "provider_process_exit":
        return NormalizedProviderEvent(
            EventDisposition.RENDERED,
            "provider_process_exit",
            payload,
        )
    elif event_type == "provider_stderr":
        return NormalizedProviderEvent(
            EventDisposition.SUMMARIZED,
            "provider_stderr",
            payload,
        )
    if event_type in _CLAUDE_IGNORED_TYPES:
        return NormalizedProviderEvent(
            EventDisposition.IGNORED,
            f"claude_{event_type}",
            payload,
            state,
        )
    if event_type in _CLAUDE_SUMMARIZED_TYPES:
        return NormalizedProviderEvent(
            EventDisposition.SUMMARIZED,
            f"claude_{event_type}",
            payload,
            state,
        )
    if event_type == "system" and subtype in _CLAUDE_RENDERED_SYSTEM_SUBTYPES:
        return NormalizedProviderEvent(
            EventDisposition.RENDERED,
            f"claude_{subtype}",
            payload,
            state,
        )
    if event_type == "attachment":
        attachment = payload.get("attachment") or {}
        disposition = (
            EventDisposition.RENDERED
            if isinstance(attachment, dict)
            and attachment.get("type") == "task_reminder"
            else EventDisposition.UNKNOWN
        )
        return NormalizedProviderEvent(
            disposition,
            "claude_attachment",
            payload,
            state,
        )
    if event_type == "rate_limit_event":
        rate_limit_info = payload.get("rate_limit_info")
        rate_limit_info = rate_limit_info if isinstance(rate_limit_info, dict) else {}
        normalized_payload = dict(payload)
        for field in _CLAUDE_RATE_LIMIT_FIELDS:
            if field in rate_limit_info:
                normalized_payload[field] = rate_limit_info[field]
        return NormalizedProviderEvent(
            EventDisposition.RENDERED,
            "claude_rate_limit_event",
            normalized_payload,
            state,
        )
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
        return NormalizedProviderEvent(
            EventDisposition.IGNORED, f"claude_{event_type}", payload, state
        )
    return NormalizedProviderEvent(
        EventDisposition.UNKNOWN,
        f"claude_{event_type or 'unknown'}",
        payload,
        state,
    )


def normalize_provider_event(
    provider: ProviderKind,
    payload: dict[str, Any],
    *,
    direction: str = "provider",
) -> NormalizedProviderEvent:
    wk_event = payload.get("_wk_event")
    if isinstance(wk_event, dict):
        provider_payload = {
            key: value for key, value in payload.items() if key != "_wk_event"
        }
        normalized = normalize_provider_event(
            provider,
            provider_payload,
            direction=direction,
        )
        return NormalizedProviderEvent(
            normalized.disposition,
            normalized.kind,
            {**normalized.payload, "_wk_event": wk_event},
            normalized.lifecycle_state,
        )
    if direction in {"client", "stdin"}:
        is_approval_response = (
            provider is ProviderKind.CODEX
            and payload.get("id") is not None
            and "result" in payload
        ) or (
            provider is ProviderKind.CLAUDE
            and payload.get("type") == "control_response"
        )
        return NormalizedProviderEvent(
            EventDisposition.IGNORED,
            "approval_response"
            if is_approval_response
            else f"{provider.value}_client_message",
            payload,
        )
    if direction == "stderr":
        return NormalizedProviderEvent(
            EventDisposition.SUMMARIZED,
            f"{provider.value}_stderr",
            payload,
        )
    if direction == "process":
        is_exit = (
            payload.get("method") == "provider/processExited"
            or payload.get("type") == "provider_process_exit"
        )
        return NormalizedProviderEvent(
            EventDisposition.RENDERED,
            "provider_process_exit" if is_exit else "provider_protocol_error",
            payload,
        )
    if provider is ProviderKind.CODEX:
        return _normalize_codex(payload)
    return _normalize_claude(payload)
