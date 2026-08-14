"""Claude Agent SDK lane for the opt-in Wiki worker harness.

The SDK is imported only when a wk-claude run starts.  The normal Wiki boot
path stays independent from this optional provider dependency.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

from .wk_core import (
    WkDisposition,
    WkEventEnvelope,
    WkEventPhase,
    WkEventSequencer,
    WkLoop,
    WkMutationClass,
    WkRunMetadata,
    WkToolRegistry,
    WkToolRequest,
    WkToolResult,
    mutation_input_hash,
    redact_payload,
)
from .wk_feature import wk_enabled


WK_CLAUDE_TOOL_NAMES = (
    "wk.read",
    "wk.write",
    "wk.edit",
    "wk.bash",
    "wk.gate",
    "wk.status",
)
WK_CLAUDE_MCP_SERVER = "wiki"
WK_CLAUDE_MCP_TOOL_NAMES = tuple(
    f"mcp__{WK_CLAUDE_MCP_SERVER}__{name.removeprefix('wk.')}"
    for name in WK_CLAUDE_TOOL_NAMES
)
_API_AUTH_ENV = frozenset(
    {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"}
)


class WkClaudeError(RuntimeError):
    """Base error for the Claude lane."""


class WkClaudeDisabled(WkClaudeError):
    """The wk feature flag is off."""


class WkClaudePlanAuthError(WkClaudeError):
    """The lane would use an API credential instead of plan auth."""


class WkLedgerError(WkClaudeError):
    """The tool ledger cannot prove a complete operation."""


class ClaudeSdkClient(Protocol):
    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def query(self, prompt: str) -> None: ...

    async def interrupt(self) -> None: ...

    def receive_messages(self) -> AsyncIterator[object]: ...


def plan_auth_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return an environment that can use Claude Code plan login only.

    The SDK launches Claude Code.  It must use the account login stored by
    Claude Code, not an Anthropic API credential or an API endpoint override.
    """

    source = dict(os.environ if environment is None else environment)
    configured = sorted(name for name in _API_AUTH_ENV if source.get(name))
    if configured:
        raise WkClaudePlanAuthError(
            "Claude plan auth cannot use API environment: " + ", ".join(configured)
        )
    return {name: value for name, value in source.items() if name not in _API_AUTH_ENV}


def _sdk_imports() -> tuple[Any, Any, Any, Any, Any]:
    try:
        from claude_agent_sdk import (  # type: ignore[import-not-found]
            ClaudeAgentOptions,
            ClaudeSDKClient,
            PermissionResultAllow,
            PermissionResultDeny,
            create_sdk_mcp_server,
        )
    except ImportError as exc:
        raise WkClaudeError(
            "claude-agent-sdk is required for wk-claude; install the backend dependencies"
        ) from exc
    return (
        ClaudeAgentOptions,
        ClaudeSDKClient,
        PermissionResultAllow,
        PermissionResultDeny,
        create_sdk_mcp_server,
    )


def _message_dict(message: object) -> dict[str, Any]:
    """Convert an SDK message to its JSON-shaped wire record."""

    if isinstance(message, Mapping):
        return cast(dict[str, Any], dict(message))
    if is_dataclass(message):
        value = cast(dict[str, Any], asdict(message))
        raw_data = value.get("data")
        if isinstance(raw_data, Mapping):
            return cast(dict[str, Any], dict(raw_data))
        type_by_class = {
            "AssistantMessage": "assistant",
            "ResultMessage": "result",
            "StreamEvent": "stream_event",
            "UserMessage": "user",
        }
        raw_type = type_by_class.get(type(message).__name__)
        if raw_type is None:
            return value
        value["type"] = raw_type
        if raw_type in {"assistant", "user"}:
            content = value.pop("content", [])
            nested: dict[str, Any] = {"content": content}
            if raw_type == "assistant":
                for field_name in ("model", "usage", "id", "stop_reason"):
                    field_value = value.pop(field_name, None)
                    if field_value is not None:
                        nested[field_name if field_name != "id" else "id"] = field_value
            value["message"] = nested
        return value
    if hasattr(message, "model_dump"):
        dumped = message.model_dump()  # type: ignore[union-attr]
        if isinstance(dumped, Mapping):
            return cast(dict[str, Any], dict(dumped))
    value = getattr(message, "__dict__", None)
    if isinstance(value, Mapping):
        return cast(dict[str, Any], dict(value))
    raise TypeError(f"unsupported Claude SDK message: {type(message).__name__}")


def _message_type(value: Mapping[str, Any]) -> str:
    raw_type = value.get("type")
    return str(raw_type) if raw_type else "unknown"


def _message_phase(value: Mapping[str, Any]) -> WkEventPhase:
    raw_type = _message_type(value)
    subtype = str(value.get("subtype") or "").casefold()
    if "compact" in subtype:
        return WkEventPhase.COMPACTION
    if raw_type == "assistant" or raw_type == "stream_event":
        return WkEventPhase.ASSISTANT
    if raw_type == "result":
        return WkEventPhase.TURN
    if raw_type == "user":
        content = (value.get("message") or {}).get("content")
        if isinstance(content, Sequence) and any(
            isinstance(block, Mapping) and block.get("type") == "tool_result"
            for block in content
        ):
            return WkEventPhase.TOOL
        return WkEventPhase.TURN
    if raw_type == "system" and subtype in {"init", "startup"}:
        return WkEventPhase.RUN
    if raw_type == "system":
        return WkEventPhase.STATUS
    return WkEventPhase.STATUS


def _event_timestamp(value: Mapping[str, Any], fallback: str) -> str:
    timestamp = value.get("timestamp")
    return str(timestamp) if timestamp else fallback


@dataclass
class WkSessionTree:
    """Append-only parent-linked session entries."""

    entries: list[dict[str, object]]
    current_id: str | None = None

    def __init__(self) -> None:
        self.entries = []
        self.current_id = None

    def append(self, entry: Mapping[str, object]) -> str:
        entry_id = str(entry.get("id") or uuid4())
        value = dict(entry)
        value["id"] = entry_id
        value["parent_id"] = value.get("parent_id", self.current_id)
        self.entries.append(value)
        self.current_id = entry_id
        return entry_id

    def record_event(self, event: WkEventEnvelope) -> str:
        return self.append(
            {
                "kind": event.kind,
                "phase": event.phase.value,
                "source_event_id": event.source_event_id,
                "payload": cast(dict[str, object], redact_payload(event.payload)),
            }
        )


class WkClaudeEventTranslator:
    """Retain raw SDK records, then publish normalized Wiki envelopes."""

    def __init__(
        self,
        *,
        metadata: WkRunMetadata,
        run_id: str,
        agent_id: str,
        sequencer: WkEventSequencer | None = None,
        timestamp: Callable[[], str] | None = None,
    ) -> None:
        self.metadata = metadata
        self.run_id = run_id
        self.agent_id = agent_id
        self.sequencer = sequencer or WkEventSequencer()
        self.timestamp = timestamp or _utc_timestamp
        self.raw_events: list[dict[str, Any]] = []
        self.session = WkSessionTree()
        self._plan_auth_verified = False

    def translate(self, message: object) -> WkEventEnvelope:
        raw = _message_dict(message)
        self.raw_events.append(json.loads(json.dumps(raw, default=str)))
        if _message_type(raw) == "system" and str(raw.get("subtype")) == "init":
            self.verify_plan_auth_event(raw)
        event = self.sequencer.emit(
            run_id=self.run_id,
            agent_id=self.agent_id,
            kind=f"claude.{_message_type(raw)}",
            phase=_message_phase(raw),
            provider="claude",
            lane="wk-claude",
            disposition=WkDisposition.RENDERED,
            ts=_event_timestamp(raw, self.timestamp()),
            payload=raw,
            parent_source_event_id=(
                self.sequencer.events[-1].source_event_id
                if self.sequencer.events
                else None
            ),
        )
        self.session.record_event(event)
        return event

    def verify_plan_auth_event(self, raw: Mapping[str, Any]) -> None:
        data = raw.get("data")
        source = data if isinstance(data, Mapping) else raw
        auth_source = source.get("apiKeySource") or source.get("api_key_source")
        if isinstance(auth_source, str) and auth_source.casefold() in {
            "anthropic_api_key",
            "api_key",
            "api-key",
            "auth_token",
            "auth-token",
        }:
            raise WkClaudePlanAuthError(
                f"Claude SDK resolved API auth source {auth_source!r}, not plan auth"
            )
        self._plan_auth_verified = True


@dataclass(frozen=True)
class _LedgerOperation:
    request: WkToolRequest
    started: WkEventEnvelope
    result: WkToolResult | None = None


class WkToolLedger:
    """Record and reconcile every Wiki tool call and result."""

    def __init__(self, translator: WkClaudeEventTranslator):
        self.translator = translator
        self.operations: dict[str, _LedgerOperation] = {}

    @property
    def events(self) -> list[WkEventEnvelope]:
        return self.translator.sequencer.events

    def record_started(self, request: WkToolRequest) -> WkEventEnvelope:
        if request.call_id in self.operations:
            raise WkLedgerError(f"duplicate tool call: {request.call_id}")
        event = self.translator.sequencer.emit(
            run_id=self.translator.run_id,
            agent_id=self.translator.agent_id,
            kind="tool.started",
            phase=WkEventPhase.TOOL,
            provider="claude",
            lane="wk-claude",
            disposition=WkDisposition.RENDERED,
            ts=self.translator.timestamp(),
            payload={
                "call_id": request.call_id,
                "name": request.name,
                "arguments": dict(request.arguments),
                "mutation": request.mutation.value,
            },
            integrity={"input_hash": mutation_input_hash(request)},
        )
        self.operations[request.call_id] = _LedgerOperation(request, event)
        self.translator.session.record_event(event)
        return event

    def record_result(
        self, request: WkToolRequest, result: WkToolResult
    ) -> WkEventEnvelope:
        operation = self.operations.get(request.call_id)
        if operation is None:
            raise WkLedgerError(f"tool result has no start: {request.call_id}")
        if operation.result is not None:
            raise WkLedgerError(f"duplicate tool result: {request.call_id}")
        if mutation_input_hash(operation.request) != mutation_input_hash(request):
            raise WkLedgerError(f"tool request changed: {request.call_id}")
        event = self.translator.sequencer.emit(
            run_id=self.translator.run_id,
            agent_id=self.translator.agent_id,
            kind="tool.completed" if result.success else "tool.failed",
            phase=WkEventPhase.TOOL,
            provider="claude",
            lane="wk-claude",
            disposition=WkDisposition.RENDERED,
            ts=self.translator.timestamp(),
            payload={"call_id": request.call_id, "result": result.to_dict()},
            integrity={
                "input_hash": mutation_input_hash(request),
                "result_hash": _hash_result(result),
            },
        )
        self.operations[request.call_id] = _LedgerOperation(request, event, result)
        self.translator.session.record_event(event)
        return event

    def reconcile(self, events: Sequence[WkEventEnvelope] | None = None) -> None:
        source = list(self.events if events is None else events)
        started: dict[str, WkEventEnvelope] = {}
        completed: dict[str, WkEventEnvelope] = {}
        for event in source:
            if event.kind == "tool.started":
                call_id = _event_call_id(event)
                if call_id is not None:
                    started[call_id] = event
            elif event.kind in {"tool.completed", "tool.failed"}:
                call_id = _event_call_id(event)
                if call_id is not None:
                    completed[call_id] = event
        missing = sorted(set(started) - set(completed))
        if missing:
            raise WkLedgerError("missing tool results: " + ", ".join(missing))
        unknown = sorted(set(completed) - set(started))
        if unknown:
            raise WkLedgerError("tool results without starts: " + ", ".join(unknown))
        for call_id, event in started.items():
            if event.payload.get("name") != "wk.gate":
                continue
            result_event = completed[call_id]
            started_hash = event.integrity.get("input_hash") if event.integrity else None
            result_hash = (
                result_event.integrity.get("input_hash")
                if result_event.integrity
                else None
            )
            if started_hash != result_hash:
                raise WkLedgerError(f"tool input hash changed: {call_id}")
            result_value = result_event.payload.get("result")
            if not isinstance(result_value, Mapping):
                raise WkLedgerError(f"gate result is not typed: {call_id}")
            if not isinstance(result_value.get("exit_code"), int):
                raise WkLedgerError(f"gate result has no real exit code: {call_id}")
            if result_value.get("success") is not True:
                raise WkLedgerError(f"gate did not succeed: {call_id}")


def _event_call_id(event: WkEventEnvelope) -> str | None:
    value = event.payload.get("call_id")
    return str(value) if value else None


def _hash_result(result: WkToolResult) -> str:
    encoded = json.dumps(result.to_dict(), sort_keys=True, separators=(",", ":")).encode()
    import hashlib

    return hashlib.sha256(encoded).hexdigest()


def _utc_timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


ApprovalRequest = Callable[[str, Mapping[str, object]], Awaitable[bool]]


class WkClaudeToolBridge:
    """Expose only Wiki tools to the SDK MCP server."""

    def __init__(
        self,
        *,
        registry: WkToolRegistry,
        ledger: WkToolLedger,
        loop: WkLoop,
    ) -> None:
        self.registry = registry
        self.ledger = ledger
        self.loop = loop

    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        call_id: str | None = None,
    ) -> WkToolResult:
        if name not in WK_CLAUDE_TOOL_NAMES:
            raise WkClaudeError(f"unsupported Claude Wiki tool: {name!r}")
        mutation = (
            WkMutationClass.PROCESS
            if name in {"wk.bash", "wk.gate"}
            else WkMutationClass.FILE
            if name in {"wk.write", "wk.edit"}
            else WkMutationClass.NONE
        )
        request = WkToolRequest(
            call_id=call_id or str(uuid4()),
            name=name,
            arguments=dict(arguments),
            mutation=mutation,
        )
        self.ledger.record_started(request)
        try:
            if name == "wk.status":
                result = await self._status(arguments)
            else:
                result = await self.registry.execute(request)
        except Exception as exc:
            result = WkToolResult(
                success=False,
                exit_code=None,
                error_class=type(exc).__name__,
                error_detail=str(exc),
                mutation=request.mutation,
            )
        self.ledger.record_result(request, result)
        return result

    async def _status(self, arguments: Mapping[str, object]) -> WkToolResult:
        state = str(arguments.get("state") or "")
        step = str(arguments.get("step") or "")
        blocker_value = arguments.get("blocker")
        blocker = str(blocker_value) if blocker_value is not None else None
        pr_value = arguments.get("pr")
        pr = str(pr_value) if pr_value is not None else None
        sequence = self.loop.write_status(
            state=state,
            pr=pr,
            step=step,
            blocker=blocker,
        )
        return WkToolResult(
            success=True,
            exit_code=0,
            stdout=json.dumps({"status_write_seq": sequence}, sort_keys=True),
        )


def _sdk_tool_schema() -> dict[str, object]:
    return {"type": "object", "additionalProperties": True}


def build_claude_sdk_options(
    *,
    prompt: str,
    model: str,
    worktree: Path,
    bridge: WkClaudeToolBridge,
    approval: ApprovalRequest | None = None,
    resume: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> Any:
    """Build SDK options with plan auth and no built-in provider tools."""

    del prompt
    ClaudeAgentOptions, _client, Allow, Deny, create_server = _sdk_imports()
    tool_declarations: list[Any] = []
    for public_name, sdk_name in zip(WK_CLAUDE_TOOL_NAMES, WK_CLAUDE_MCP_TOOL_NAMES):
        short_name = sdk_name.rsplit("__", 1)[-1]

        async def handler(
            arguments: Mapping[str, object],
            *,
            _public_name: str = public_name,
        ) -> dict[str, object]:
            result = await bridge.invoke(_public_name, arguments)
            return {
                "content": [{"type": "text", "text": json.dumps(result.to_dict())}],
                "is_error": not result.success,
            }

        from claude_agent_sdk import tool  # type: ignore[import-not-found]

        tool_declarations.append(tool(short_name, f"Wiki-owned {public_name} tool", _sdk_tool_schema())(handler))

    mcp_server = create_server(
        name=WK_CLAUDE_MCP_SERVER,
        version="0.1.0",
        tools=tool_declarations,
    )

    async def can_use_tool(
        tool_name: str,
        input_value: dict[str, Any],
        _context: Any,
    ) -> Any:
        if tool_name not in WK_CLAUDE_MCP_TOOL_NAMES:
            return Deny(behavior="deny", message="only Wiki-owned tools are permitted")
        if approval is None or await approval(tool_name, input_value):
            return Allow(behavior="allow", updated_input=input_value)
        return Deny(behavior="deny", message="Wiki tool approval denied")

    sdk_environment = plan_auth_environment(environment)
    options_kwargs: dict[str, Any] = {
        "tools": [],
        "allowed_tools": [],
        "disallowed_tools": [
            "Read",
            "Write",
            "Edit",
            "Bash",
            "Glob",
            "Grep",
            "Task",
            "WebFetch",
            "WebSearch",
        ],
        "mcp_servers": {WK_CLAUDE_MCP_SERVER: mcp_server},
        "strict_mcp_config": True,
        "permission_mode": "default",
        "can_use_tool": can_use_tool,
        "cwd": str(worktree),
        "model": model,
        "env": sdk_environment,
        "setting_sources": ["user", "project", "local"],
    }
    if resume is not None:
        options_kwargs["resume"] = resume
    cli_path = shutil.which("claude")
    if cli_path:
        options_kwargs["cli_path"] = cli_path
    return ClaudeAgentOptions(**options_kwargs)


class WkClaudeLane:
    """Minimal turn and steering loop over ClaudeSDKClient."""

    def __init__(
        self,
        *,
        metadata: WkRunMetadata,
        run_id: str,
        agent_id: str,
        worktree: Path,
        model: str,
        registry: WkToolRegistry,
        loop: WkLoop,
        client_factory: Callable[[Any], ClaudeSdkClient] | None = None,
        approval: ApprovalRequest | None = None,
    ) -> None:
        if not wk_enabled():
            raise WkClaudeDisabled("WIKI_ENABLE_WK=1 is required for wk-claude")
        if metadata.kind != "wk-claude":
            raise ValueError("WkClaudeLane requires kind=wk-claude")
        self.metadata = metadata
        self.run_id = run_id
        self.agent_id = agent_id
        self.worktree = worktree
        self.model = model
        self.loop = loop
        self.translator = WkClaudeEventTranslator(
            metadata=metadata,
            run_id=run_id,
            agent_id=agent_id,
        )
        self.ledger = WkToolLedger(self.translator)
        self.bridge = WkClaudeToolBridge(registry=registry, ledger=self.ledger, loop=loop)
        self.approval = approval
        self._client_factory = client_factory
        self._client: ClaudeSdkClient | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._events: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
        self._closed = False

    def _new_client(self, *, resume: str | None = None) -> ClaudeSdkClient:
        options = build_claude_sdk_options(
            prompt="",
            model=self.model,
            worktree=self.worktree,
            bridge=self.bridge,
            approval=self.approval,
            resume=resume,
        )
        if self._client_factory is not None:
            return self._client_factory(options)
        _options, client_type, _allow, _deny, _server = _sdk_imports()
        return client_type(options=options)

    async def _receive(self) -> None:
        assert self._client is not None
        try:
            async for message in self._client.receive_messages():
                event = self.translator.translate(message)
                await self._events.put(
                    {"raw": self.translator.raw_events[-1], "event": event.to_dict()}
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._events.put(
                {
                    "raw": {"type": "provider_error", "error": str(exc)},
                    "event": {
                        "phase": WkEventPhase.STATUS.value,
                        "kind": "claude.error",
                        "error": str(exc),
                    },
                }
            )
        finally:
            if self._closed:
                await self._events.put(None)

    async def _ensure_client(self, *, resume: str | None = None) -> None:
        if self._client is not None:
            return
        self._client = self._new_client(resume=resume)
        await self._client.connect()
        self._receive_task = asyncio.create_task(self._receive())

    async def start(self, prompt: str) -> None:
        await self._ensure_client()
        assert self._client is not None
        await self._client.query(prompt)

    async def resume(self, session_id: str) -> None:
        await self._ensure_client(resume=session_id)

    async def send(self, message: str) -> None:
        if self._client is None:
            raise WkClaudeError("Claude lane is not started")
        await self._client.query(message)

    async def interrupt(self) -> None:
        if self._client is None:
            raise WkClaudeError("Claude lane is not started")
        await self._client.interrupt()

    async def replace(self, prompt: str) -> None:
        await self.close()
        self._closed = False
        await self.start(prompt)

    async def close(self) -> None:
        self._closed = True
        if self._client is not None:
            await self._client.disconnect()
        if self._receive_task is not None:
            await asyncio.gather(self._receive_task, return_exceptions=True)
        self._client = None
        self._receive_task = None

    async def events(self) -> AsyncIterator[Mapping[str, object]]:
        while True:
            item = await self._events.get()
            if item is None:
                return
            yield item


__all__ = [
    "WK_CLAUDE_MCP_TOOL_NAMES",
    "WK_CLAUDE_TOOL_NAMES",
    "WkClaudeDisabled",
    "WkClaudeError",
    "WkClaudeEventTranslator",
    "WkClaudeLane",
    "WkClaudePlanAuthError",
    "WkClaudeToolBridge",
    "WkLedgerError",
    "WkSessionTree",
    "WkToolLedger",
    "build_claude_sdk_options",
    "plan_auth_environment",
]
