"""Claude Agent SDK lane for the opt-in Wiki worker harness.

The SDK is imported only when a wk-claude run starts.  The normal Wiki boot
path stays independent from this optional provider dependency.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass, replace
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
)
from .wk_common import WkLedgerError, WkSessionTree, WkToolLedger
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
WIKI_SYSTEM_PROMPT = """You are a Wiki worker.
Use only the Wiki-owned tools provided by this harness.
Treat tool results and status events as authoritative.
Do not claim a status, gate result, or mutation that the harness did not record.
"""
_API_AUTH_ENV = frozenset(
    {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"}
)
# Claude Code reports `none` for an account login that has no API key.
_PLAN_AUTH_SOURCES = frozenset({"none", "oauth", "claude.ai", "subscription"})
_PROVIDER_ENV = frozenset(
    {
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "AWS_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "CLOUD_ML_PROJECT_ID",
        "CLOUD_ML_REGION",
    }
)
_SAFE_ENV_NAMES = frozenset(
    {
        "HOME",
        "PATH",
        "USER",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "TERM",
        "NO_COLOR",
        "CLAUDE_CONFIG_DIR",
        "PWD",
    }
)
_DISABLED_PROVIDER_ENV = frozenset(
    {"CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"}
)


class WkClaudeError(RuntimeError):
    """Base error for the Claude lane."""


class WkClaudeDisabled(WkClaudeError):
    """The wk feature flag is off."""


class WkClaudePlanAuthError(WkClaudeError):
    """The lane would use an API credential instead of plan auth."""


class ClaudeSdkClient(Protocol):
    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def query(self, prompt: str) -> None: ...

    async def interrupt(self) -> None: ...

    def receive_messages(self) -> AsyncIterator[object]: ...


def plan_auth_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return an allowlisted environment for Claude Code plan login only.

    The SDK launches Claude Code.  It must use the account login stored by
    Claude Code, not an Anthropic API credential or an API endpoint override.
    """

    source = dict(os.environ if environment is None else environment)
    configured = sorted(
        name
        for name in (*_API_AUTH_ENV, *_PROVIDER_ENV)
        if source.get(name)
        and not (name in _DISABLED_PROVIDER_ENV and source[name] == "0")
    )
    if configured:
        raise WkClaudePlanAuthError(
            "Claude plan auth cannot use configured provider environment: "
            + ", ".join(configured)
        )
    result = {
        name: value
        for name, value in source.items()
        if name in _SAFE_ENV_NAMES
    }
    result["CLAUDE_CODE_USE_BEDROCK"] = "0"
    result["CLAUDE_CODE_USE_VERTEX"] = "0"
    return result


def _verify_cli_plan_auth(
    cli_path: str | Path | None, environment: Mapping[str, str]
) -> dict[str, str]:
    if not cli_path:
        raise WkClaudePlanAuthError("Claude CLI path is required to prove plan auth")
    try:
        completed = subprocess.run(
            [str(cli_path), "auth", "status", "--json"],
            cwd=None,
            env=plan_auth_environment(environment),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WkClaudePlanAuthError(
            f"Claude CLI auth status could not prove plan auth: {type(exc).__name__}"
        ) from exc
    if completed.returncode != 0:
        raise WkClaudePlanAuthError(
            f"Claude CLI auth status failed with exit code {completed.returncode}"
        )
    try:
        status = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise WkClaudePlanAuthError("Claude CLI auth status was not JSON") from exc
    if not isinstance(status, Mapping):
        raise WkClaudePlanAuthError("Claude CLI auth status was not an object")
    if status.get("loggedIn") is not True:
        raise WkClaudePlanAuthError("Claude CLI is not logged in with plan auth")
    auth_method = str(status.get("authMethod") or "").casefold()
    api_provider = str(status.get("apiProvider") or "").casefold()
    subscription = status.get("subscriptionType")
    account_id = status.get("accountId") or status.get("account_id") or status.get("email")
    organization_id = (
        status.get("organizationId")
        or status.get("organization_id")
        or status.get("orgId")
    )
    if auth_method not in {"claude.ai", "oauth"}:
        raise WkClaudePlanAuthError("Claude CLI auth method is not subscription auth")
    if api_provider not in {"firstparty", "first_party"}:
        raise WkClaudePlanAuthError("Claude CLI auth provider is not first-party")
    if not isinstance(subscription, str) or not subscription.strip():
        raise WkClaudePlanAuthError("Claude CLI did not report a subscription identity")
    if not isinstance(account_id, str) or not account_id.strip():
        raise WkClaudePlanAuthError("Claude CLI did not report an account identity")
    if not isinstance(organization_id, str) or not organization_id.strip():
        raise WkClaudePlanAuthError("Claude CLI did not report an organization identity")
    return {
        "auth_method": auth_method,
        "api_provider": api_provider,
        "subscription_type": subscription,
        "account_id": account_id,
        "organization_id": organization_id,
    }


def _sanitized_transport_class() -> type[Any]:
    """Return an SDK transport that replaces, rather than merges, the env."""

    import anyio
    from anyio.streams.text import TextReceiveStream, TextSendStream
    from claude_agent_sdk._internal._task_compat import spawn_detached
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        _ACTIVE_CHILDREN,
        SubprocessCLITransport,
    )
    from claude_agent_sdk._version import __version__
    from subprocess import PIPE

    class SanitizedSubprocessCLITransport(SubprocessCLITransport):
        async def connect(self) -> None:
            if self._process:
                return
            if self._cli_path is None:
                self._cli_path = await anyio.to_thread.run_sync(self._find_cli)
            self._reject_windows_batch_cli(self._cli_path)
            cmd = self._build_command()
            process_env = dict(self._options.env)
            unexpected = set(process_env) - _SAFE_ENV_NAMES - _DISABLED_PROVIDER_ENV
            if unexpected:
                raise WkClaudeError(
                    "sanitized Claude SDK transport received unsupported environment: "
                    + ", ".join(sorted(unexpected))
                )
            process_env["CLAUDE_CODE_ENTRYPOINT"] = "sdk-py"
            process_env["CLAUDE_AGENT_SDK_VERSION"] = __version__
            if self._cwd:
                process_env["PWD"] = self._cwd
            try:
                self._process = await anyio.open_process(
                    cmd,
                    stdin=PIPE,
                    stdout=PIPE,
                    stderr=PIPE if self._options.stderr is not None else None,
                    cwd=self._cwd,
                    env=process_env,
                    user=self._options.user,
                )
                _ACTIVE_CHILDREN.add(self._process)
                if self._process.stdout:
                    self._stdout_stream = TextReceiveStream(self._process.stdout)
                if self._process.stderr:
                    self._stderr_stream = TextReceiveStream(self._process.stderr)
                    self._stderr_task = spawn_detached(self._handle_stderr())
                if self._process.stdin:
                    self._stdin_stream = TextSendStream(self._process.stdin)
                self._ready = True
            except Exception as exc:
                raise WkClaudeError(
                    f"sanitized Claude SDK transport failed to start: {exc}"
                ) from exc

    return SanitizedSubprocessCLITransport


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
            if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
                normalized_content: list[object] = []
                for block in content:
                    if isinstance(block, Mapping) and "type" not in block:
                        block = dict(block)
                        if "tool_use_id" in block:
                            block["type"] = "tool_result"
                        elif "id" in block and "name" in block:
                            block["type"] = "tool_use"
                        elif "text" in block:
                            block["type"] = "text"
                        elif "thinking" in block:
                            block["type"] = "thinking"
                    normalized_content.append(block)
                content = normalized_content
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
        self._effective_tools: frozenset[str] | None = None
        self._effective_hooks: object = None
        self._effective_settings_sources: object = None

    @property
    def plan_auth_verified(self) -> bool:
        return self._plan_auth_verified

    @property
    def provider(self) -> str:
        return "claude"

    @property
    def lane(self) -> str:
        return "wk-claude"

    def error_event(
        self, error: BaseException, *, raw: Mapping[str, Any] | None = None
    ) -> WkEventEnvelope:
        value = dict(raw) if raw is not None else {
            "type": "provider_error",
            "error_class": type(error).__name__,
            "error": str(error),
        }
        if not self.raw_events or self.raw_events[-1] != value:
            self.raw_events.append(value)
        event = self.sequencer.emit(
            run_id=self.run_id,
            agent_id=self.agent_id,
            kind="claude.provider_error",
            phase=WkEventPhase.STATUS,
            provider="claude",
            lane="wk-claude",
            disposition=WkDisposition.RENDERED,
            ts=self.timestamp(),
            payload=value,
        )
        self.session.record_event(event)
        return event

    def _verify_startup_event(self, raw: Mapping[str, Any]) -> None:
        self.verify_plan_auth_event(raw)
        source = raw.get("data")
        init = source if isinstance(source, Mapping) else raw
        tools = init.get("tools")
        if not isinstance(tools, Sequence) or isinstance(tools, (str, bytes)):
            raise WkClaudePlanAuthError(
                "Claude SDK init did not expose its effective tool list"
            )
        effective_tools = {str(tool) for tool in tools}
        expected_tools = set(WK_CLAUDE_MCP_TOOL_NAMES)
        if effective_tools != expected_tools:
            missing = expected_tools - effective_tools
            extra = effective_tools - expected_tools
            detail = []
            if missing:
                detail.append("missing Wiki tools: " + ", ".join(sorted(missing)))
            if extra:
                detail.append("non-Wiki tools: " + ", ".join(sorted(extra)))
            raise WkClaudePlanAuthError("Claude SDK effective tool policy failed (" + "; ".join(detail) + ")")
        hooks = init.get("hooks") or init.get("active_hooks")
        if hooks:
            raise WkClaudePlanAuthError("Claude SDK exposed active hooks")
        settings_sources = init.get("setting_sources") or init.get("settingSources")
        if settings_sources:
            raise WkClaudePlanAuthError("Claude SDK loaded filesystem settings")
        self._effective_tools = frozenset(effective_tools)
        self._effective_hooks = hooks
        self._effective_settings_sources = settings_sources

    def verify_runtime_policy(self) -> None:
        if self._effective_tools != frozenset(WK_CLAUDE_MCP_TOOL_NAMES):
            raise WkClaudePlanAuthError("Claude SDK effective tool policy drifted")
        if self._effective_hooks or self._effective_settings_sources:
            raise WkClaudePlanAuthError("Claude SDK hooks or settings became active")

    def events_for_kind(self, kind: str) -> tuple[WkEventEnvelope, ...]:
        return tuple(event for event in self.sequencer.events if event.kind == kind)

    def translate(self, message: object) -> WkEventEnvelope:
        raw = _message_dict(message)
        self.raw_events.append(json.loads(json.dumps(raw, default=str)))
        if _message_type(raw) == "system" and str(raw.get("subtype")) == "init":
            self._verify_startup_event(raw)
        provider_error = raw.get("error")
        if isinstance(provider_error, str) and provider_error.casefold() in {
            "authentication_failed",
            "auth_error",
            "authentication_error",
        }:
            return self.error_event(WkClaudeError(provider_error), raw=raw)
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
        if not isinstance(auth_source, str) or auth_source.casefold() not in _PLAN_AUTH_SOURCES:
            raise WkClaudePlanAuthError(
                f"Claude SDK auth source is not proven plan auth: {auth_source!r}"
            )
        self._plan_auth_verified = True


def _utc_timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


ApprovalRequest = Callable[[str, Mapping[str, object]], Awaitable[bool]]


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class _WkPathTool:
    def __init__(self, *, root: Path, name: str, mutation: WkMutationClass):
        self.root = root.resolve()
        self.name = name
        self.mutation = mutation

    def _path(self, value: object) -> Path:
        if not isinstance(value, str) or not value:
            raise ValueError("path must be a non-empty string")
        path = (self.root / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError("path is outside the wk worktree")
        return path


class WkReadTool(_WkPathTool):
    def __init__(self, *, root: Path, max_bytes: int = 1_000_000):
        super().__init__(root=root, name="wk.read", mutation=WkMutationClass.NONE)
        self.max_bytes = max_bytes

    def validate(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        path = self._path(arguments.get("path"))
        return {"path": str(path), "max_bytes": int(arguments.get("max_bytes", self.max_bytes))}

    async def execute(self, request: WkToolRequest) -> WkToolResult:
        path = self._path(request.arguments["path"])
        max_bytes = min(max(1, int(request.arguments.get("max_bytes", self.max_bytes))), self.max_bytes)
        try:
            if path.is_dir():
                output = "\n".join(sorted(item.name for item in path.iterdir()))
                data = output.encode()
            else:
                data = path.read_bytes()
                output = data[:max_bytes].decode("utf-8", errors="replace")
            return WkToolResult(
                success=True,
                exit_code=0,
                stdout=output,
                mutation=self.mutation,
                mutation_receipt={"path": str(path), "byte_count": len(data)},
            )
        except OSError as exc:
            return WkToolResult(
                success=False,
                exit_code=1,
                error_class=type(exc).__name__,
                error_detail=str(exc),
                mutation=self.mutation,
            )


class WkWriteTool(_WkPathTool):
    def __init__(self, *, root: Path):
        super().__init__(root=root, name="wk.write", mutation=WkMutationClass.FILE)

    def validate(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        path = self._path(arguments.get("path"))
        content = arguments.get("content", arguments.get("contents"))
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        return {"path": str(path), "content": content}

    async def execute(self, request: WkToolRequest) -> WkToolResult:
        path = self._path(request.arguments["path"])
        content = str(request.arguments["content"]).encode()
        before = _hash_bytes(path.read_bytes()) if path.exists() else None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            return WkToolResult(
                success=True,
                exit_code=0,
                mutation=WkMutationClass.FILE,
                mutation_receipt={
                    "path": str(path),
                    "before": before,
                    "after": _hash_bytes(content),
                    "byte_count": len(content),
                },
            )
        except OSError as exc:
            return WkToolResult(
                success=False,
                exit_code=1,
                error_class=type(exc).__name__,
                error_detail=str(exc),
                mutation=WkMutationClass.FILE,
            )


class WkEditTool(_WkPathTool):
    def __init__(self, *, root: Path):
        super().__init__(root=root, name="wk.edit", mutation=WkMutationClass.FILE)

    def validate(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        path = self._path(arguments.get("path"))
        old = arguments.get("old")
        new = arguments.get("new")
        if not isinstance(old, str) or not isinstance(new, str):
            raise ValueError("old and new must be strings")
        return {"path": str(path), "old": old, "new": new, "replace_all": bool(arguments.get("replace_all", False))}

    async def execute(self, request: WkToolRequest) -> WkToolResult:
        path = self._path(request.arguments["path"])
        try:
            before_bytes = path.read_bytes()
            before = before_bytes.decode("utf-8")
            old = str(request.arguments["old"])
            new = str(request.arguments["new"])
            replace_all = bool(request.arguments.get("replace_all", False))
            matches = before.count(old)
            if matches == 0 or (matches > 1 and not replace_all):
                raise ValueError(f"edit matched {matches} ranges")
            after = before.replace(old, new, -1 if replace_all else 1)
            path.write_text(after, encoding="utf-8")
            return WkToolResult(
                success=True,
                exit_code=0,
                mutation=WkMutationClass.FILE,
                mutation_receipt={
                    "path": str(path),
                    "before": _hash_bytes(before_bytes),
                    "after": _hash_bytes(after.encode()),
                    "matched_ranges": matches,
                },
            )
        except (OSError, UnicodeError, ValueError) as exc:
            return WkToolResult(
                success=False,
                exit_code=1,
                error_class=type(exc).__name__,
                error_detail=str(exc),
                mutation=WkMutationClass.FILE,
            )


async def _run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_ms: int,
    env: Mapping[str, str] | None = None,
) -> WkToolResult:
    started = asyncio.get_running_loop().time()
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=dict(env or os.environ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=max(timeout_ms, 1) / 1000
            )
        except TimeoutError:
            timed_out = True
            process.kill()
            stdout, stderr = await process.communicate()
        duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        exit_code = process.returncode
        return WkToolResult(
            success=not timed_out and exit_code == 0,
            exit_code=exit_code,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            error_class="timeout" if timed_out else ("process_failed" if exit_code else None),
            timed_out=timed_out,
            duration_ms=duration_ms,
            mutation=WkMutationClass.PROCESS,
            mutation_receipt={
                "pid": process.pid,
                "stdout_sha256": _hash_bytes(stdout),
                "stderr_sha256": _hash_bytes(stderr),
            },
        )
    except OSError as exc:
        return WkToolResult(
            success=False,
            exit_code=127,
            error_class=type(exc).__name__,
            error_detail=str(exc),
            mutation=WkMutationClass.PROCESS,
        )


class WkBashTool:
    name = "wk.bash"

    def __init__(
        self,
        *,
        root: Path,
        timeout_ms: int = 120_000,
        environment: Mapping[str, str] | None = None,
    ):
        self.root = root.resolve()
        self.timeout_ms = timeout_ms
        self.environment = dict(environment) if environment is not None else None

    def validate(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        timeout_ms = min(max(1, int(arguments.get("timeout_ms", self.timeout_ms))), 600_000)
        return {"command": command, "timeout_ms": timeout_ms}

    async def execute(self, request: WkToolRequest) -> WkToolResult:
        return await _run_process(
            ("/bin/sh", "-lc", str(request.arguments["command"])),
            cwd=self.root,
            timeout_ms=int(request.arguments.get("timeout_ms", self.timeout_ms)),
            env=(
                self.environment
                if self.environment is not None
                else plan_auth_environment()
            ),
        )


class WkGateTool(WkBashTool):
    name = "wk.gate"

    def __init__(
        self,
        *,
        root: Path,
        wiki_command: Sequence[str] = ("wiki",),
        environment: Mapping[str, str] | None = None,
    ):
        super().__init__(root=root, environment=environment)
        self.wiki_command = tuple(wiki_command)

    def validate(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        pr = arguments.get("pr")
        if not isinstance(pr, str) or not pr:
            raise ValueError("pr must be a non-empty string")
        value: dict[str, object] = {"pr": pr, "timeout_ms": int(arguments.get("timeout_ms", self.timeout_ms))}
        return value

    async def execute(self, request: WkToolRequest) -> WkToolResult:
        argv = [*self.wiki_command, "gate", str(request.arguments["pr"]), "--json"]
        result = await _run_process(
            argv,
            cwd=self.root,
            timeout_ms=int(request.arguments.get("timeout_ms", self.timeout_ms)),
            env=(
                self.environment
                if self.environment is not None
                else plan_auth_environment()
            ),
        )
        if not result.stdout:
            return result
        try:
            verdict = json.loads(result.stdout)
        except json.JSONDecodeError:
            return result
        if not isinstance(verdict, Mapping):
            return result
        receipt = dict(result.mutation_receipt or {})
        receipt["verdict"] = dict(verdict)
        return replace(result, mutation_receipt=receipt)


class WkStatusTool:
    name = "wk.status"

    def __init__(self, *, loop: WkLoop):
        self.loop = loop

    def validate(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        state = arguments.get("state")
        step = arguments.get("step")
        if not isinstance(state, str) or not isinstance(step, str):
            raise ValueError("state and step are required strings")
        return {
            "state": state,
            "step": step,
            "pr": arguments.get("pr"),
            "blocker": arguments.get("blocker"),
        }

    async def execute(self, request: WkToolRequest) -> WkToolResult:
        sequence = self.loop.write_status(
            state=str(request.arguments["state"]),
            pr=str(request.arguments["pr"]) if request.arguments.get("pr") else None,
            step=str(request.arguments["step"]),
            blocker=(
                str(request.arguments["blocker"])
                if request.arguments.get("blocker")
                else None
            ),
        )
        return WkToolResult(
            success=True,
            exit_code=0,
            stdout=json.dumps({"status_write_seq": sequence}, sort_keys=True),
        )


def register_default_wk_tools(
    registry: WkToolRegistry,
    *,
    root: Path,
    loop: WkLoop,
    wiki_command: Sequence[str] = ("wiki",),
    environment: Mapping[str, str] | None = None,
) -> WkToolRegistry:
    tools: tuple[object, ...] = (
        WkReadTool(root=root),
        WkWriteTool(root=root),
        WkEditTool(root=root),
        WkBashTool(root=root, environment=environment),
        WkGateTool(root=root, wiki_command=wiki_command, environment=environment),
        WkStatusTool(loop=loop),
    )
    for tool in tools:
        if tool.name not in registry.names:  # type: ignore[attr-defined]
            registry.register(cast(Any, tool))
    return registry


@dataclass(frozen=True)
class _SteeringItem:
    message_id: str
    message: str
    mode: str


class WkSteeringQueue:
    """Durable steering messages that survive turn boundaries and replacement."""

    def __init__(self, path: Path):
        self.path = path
        self._items: list[_SteeringItem] = []
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, list):
                raise WkClaudeError("wk steering queue is not a list")
            self._items = [
                _SteeringItem(str(item["id"]), str(item["message"]), str(item["mode"]))
                for item in value
            ]

    @property
    def pending(self) -> tuple[_SteeringItem, ...]:
        return tuple(self._items)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        tmp = Path(raw_tmp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    [
                        {"id": item.message_id, "message": item.message, "mode": item.mode}
                        for item in self._items
                    ],
                    handle,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def enqueue(self, message: str, *, mode: str) -> _SteeringItem:
        item = _SteeringItem(str(uuid4()), message, mode)
        self._items.append(item)
        self._save()
        return item

    def acknowledge(self, message_id: str) -> None:
        self._items = [item for item in self._items if item.message_id != message_id]
        self._save()


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
        self.ledger.reconcile()
        return result


from .wk_common import (
    WkSteeringQueue,
    WkToolBridge as WkClaudeToolBridge,
)
from .wk_tools import register_default_wk_tools


def _sdk_tool_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {},
        "additionalProperties": True,
    }


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
        "setting_sources": [],
        "hooks": None,
        "settings": None,
        "system_prompt": {
            "type": "preset",
            "preset": "claude_code",
            "append": WIKI_SYSTEM_PROMPT,
        },
    }
    if resume is not None:
        options_kwargs["resume"] = resume
    cli_path = shutil.which("claude")
    if cli_path:
        options_kwargs["cli_path"] = cli_path
    return ClaudeAgentOptions(**options_kwargs)


def _settings_paths(worktree: Path, environment: Mapping[str, str]) -> tuple[Path, ...]:
    roots = {worktree / ".claude", Path("/etc/claude-code"), Path("/Library/Application Support/ClaudeCode")}
    for name in ("CLAUDE_CONFIG_DIR", "HOME"):
        value = environment.get(name)
        if value:
            roots.add(Path(value) / ".claude" if name == "HOME" else Path(value))
    names = ("settings.json", "settings.local.json", "managed-settings.json")
    return tuple(sorted({root / name for root in roots if str(root) != "." for name in names}))


class _SettingsGuard:
    def __init__(self, paths: Sequence[Path]):
        self.paths = tuple(paths)
        self._fingerprint = self._read()

    def _read(self) -> tuple[tuple[str, bool, str], ...]:
        rows: list[tuple[str, bool, str]] = []
        for path in self.paths:
            try:
                content = path.read_bytes()
            except FileNotFoundError:
                rows.append((str(path), False, ""))
            except OSError as exc:
                raise WkClaudeError(
                    f"cannot inspect Claude settings source {path}: {type(exc).__name__}"
                ) from exc
            else:
                rows.append((str(path), True, hashlib.sha256(content).hexdigest()))
        return tuple(rows)

    def verify_unchanged(self) -> None:
        current = self._read()
        if current != self._fingerprint:
            raise WkClaudeError("Claude settings source changed during the run")


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
        loop: WkLoop,
        role: str = "review",
        registry: WkToolRegistry | None = None,
        client_factory: Callable[[Any], ClaudeSdkClient] | None = None,
        options_factory: Callable[..., Any] | None = None,
        approval: ApprovalRequest | None = None,
        steering_path: Path | None = None,
        wiki_command: Sequence[str] = ("wiki",),
        environment: Mapping[str, str] | None = None,
        cli_path: str | Path | None = None,
        sequencer: WkEventSequencer | None = None,
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
        self.role = role
        self.loop = loop
        self._events: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
        self._environment = plan_auth_environment(environment)
        self.registry = register_default_wk_tools(
            registry or WkToolRegistry(),
            root=worktree,
            loop=loop,
            wiki_command=wiki_command,
            environment=self._environment,
        )
        self.translator = WkClaudeEventTranslator(
            metadata=metadata,
            run_id=run_id,
            agent_id=agent_id,
            sequencer=sequencer,
        )
        self.ledger = WkToolLedger(self.translator)
        self.loop.bind_integrity(self.ledger, role=role)
        self.bridge = WkClaudeToolBridge(
            registry=self.registry, ledger=self.ledger, loop=loop
        )
        self.bridge.event_publisher = self._publish_ledger_events
        self.approval = approval
        self.cli_path = cli_path
        self._client_factory = client_factory
        self._options_factory = options_factory
        self._steering = WkSteeringQueue(
            steering_path or loop.status_path.with_name(f"{agent_id}.wk-steering.json")
        )
        self._client: ClaudeSdkClient | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._closed = False
        self._bootstrap_policy_verified = False
        self._startup_ready = asyncio.Event()
        self._startup_error: BaseException | None = None
        self._auth_identity: dict[str, str] | None = None
        self._sdk_environment: Mapping[str, str] = {}
        self._client_options_cli_path: str | Path | None = None
        self._settings_guard = _SettingsGuard(
            _settings_paths(worktree, self._environment)
        )

    async def _publish_ledger_events(self, events: Sequence[WkEventEnvelope]) -> None:
        for event in events:
            raw = {"type": "wk_ledger", "event_id": event.source_event_id}
            await self._events.put({"raw": raw, "event": event.to_dict()})

    def _new_client(self, *, resume: str | None = None) -> ClaudeSdkClient:
        if self._options_factory is None:
            options = build_claude_sdk_options(
                prompt="",
                model=self.model,
                worktree=self.worktree,
                bridge=self.bridge,
                approval=self.approval,
                resume=resume,
                environment=self._environment,
            )
            if self.cli_path is not None and is_dataclass(options):
                options = replace(options, cli_path=str(self.cli_path))
        else:
            options = self._options_factory(resume=resume)
        cli_path = getattr(options, "cli_path", None)
        sdk_environment = getattr(options, "env", None)
        if cli_path is not None and isinstance(sdk_environment, Mapping):
            self._client_options_cli_path = cli_path
            self._sdk_environment = dict(sdk_environment)
            self._auth_identity = _verify_cli_plan_auth(cli_path, sdk_environment)
        if self._client_factory is not None:
            return self._client_factory(options)
        _options, client_type, _allow, _deny, _server = _sdk_imports()
        transport_type = _sanitized_transport_class()
        # claude_agent_sdk/client.py v0.2.128 replaces options with
        # permission_prompt_tool_name="stdio" before it creates its transport.
        transport_options = options
        if getattr(options, "can_use_tool", None):
            transport_options = replace(options, permission_prompt_tool_name="stdio")
        return client_type(
            options=options,
            transport=transport_type(prompt=None, options=transport_options),
        )

    def _verify_turn_boundary(self, *, require_policy: bool = True) -> None:
        self._settings_guard.verify_unchanged()
        if self._auth_identity is None and (
            self._client_factory is None and self._options_factory is None
        ):
            raise WkClaudePlanAuthError("Claude plan auth identity was not verified")
        if self._auth_identity is not None:
            current = _verify_cli_plan_auth(
                self._client_options_cli_path, self._sdk_environment
            )
            if current != self._auth_identity:
                raise WkClaudePlanAuthError("Claude plan auth identity changed during the run")
        if require_policy:
            self.translator.verify_runtime_policy()

    async def _receive(self) -> None:
        assert self._client is not None
        try:
            async for message in self._client.receive_messages():
                event = self.translator.translate(message)
                raw = self.translator.raw_events[-1]
                self.ledger.record_transport_frame(raw)
                if raw.get("type") == "system" and raw.get("subtype") == "init":
                    self._startup_ready.set()
                await self._events.put(
                    {"raw": raw, "event": event.to_dict()}
                )
                if raw.get("type") == "result":
                    await self._deliver_pending(include_idle=True)
            self.ledger.reconcile()
            self.ledger.reconcile_transport()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._startup_error = exc
            self._startup_ready.set()
            event = self.translator.error_event(exc)
            await self._events.put(
                {
                    "raw": self.translator.raw_events[-1],
                    "event": event.to_dict(),
                }
            )

    async def _emit_provider_error(self, error: BaseException) -> None:
        event = self.translator.error_event(error)
        await self._events.put(
            {"raw": self.translator.raw_events[-1], "event": event.to_dict()}
        )

    async def _query_provider(self, prompt: str, *, require_policy: bool = True) -> None:
        if self._client is None:
            raise WkClaudeError("Claude lane is not started")
        try:
            self._verify_turn_boundary(
                require_policy=require_policy or self._bootstrap_policy_verified
            )
            await self._client.query(prompt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit_provider_error(exc)
            raise WkClaudeError(str(exc)) from exc

    async def _verify_boundary(self) -> None:
        try:
            self._verify_turn_boundary()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit_provider_error(exc)
            raise WkClaudeError(str(exc)) from exc

    async def _ensure_client(self, *, resume: str | None = None) -> None:
        if self._client is not None:
            return
        self._client = self._new_client(resume=resume)
        try:
            await self._client.connect()
        except Exception as exc:
            self._startup_error = exc
            self._startup_ready.set()
            event = self.translator.error_event(exc)
            await self._events.put(
                {"raw": self.translator.raw_events[-1], "event": event.to_dict()}
            )
            self._client = None
            raise WkClaudeError(str(exc)) from exc
        self._receive_task = asyncio.create_task(self._receive())

    async def _await_startup(self) -> None:
        try:
            await asyncio.wait_for(self._startup_ready.wait(), timeout=30)
        except TimeoutError as exc:
            self._startup_error = WkClaudePlanAuthError(
                "Claude SDK startup did not prove plan auth and tool policy"
            )
            event = self.translator.error_event(self._startup_error)
            await self._events.put({"raw": self.translator.raw_events[-1], "event": event.to_dict()})
            raise self._startup_error from exc
        if self._startup_error is not None:
            raise WkClaudeError(str(self._startup_error)) from self._startup_error

    async def _deliver_pending(self, *, include_idle: bool = False) -> None:
        if self._client is None:
            return
        items = tuple(
            item
            for item in self._steering.pending
            if item.mode == "now" or include_idle
        )
        for item in items:
            await self._query_provider(item.message)
            self._steering.acknowledge(item.message_id)

    async def start(self, prompt: str) -> None:
        try:
            await self._ensure_client()
            # Claude Code emits its system/init record only after the first
            # user message, so the first query must go out before the lane
            # can verify the init-derived tool policy.  Plan auth is already
            # proven pre-connect; init verification still fails the run when
            # the record arrives with a bad auth source or tool set.
            await self._query_provider(
                prompt, require_policy=self._bootstrap_policy_verified
            )
            await self._await_startup()
            self.translator.verify_runtime_policy()
            self._bootstrap_policy_verified = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.translator.events_for_kind("claude.provider_error"):
                await self._emit_provider_error(exc)
            raise
        await self._deliver_pending()

    async def resume(self, session_id: str) -> None:
        try:
            await self._ensure_client(resume=session_id)
            await self._await_startup()
            await self._verify_boundary()
            self._bootstrap_policy_verified = True
            await self._deliver_pending()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.translator.events_for_kind("claude.provider_error"):
                await self._emit_provider_error(exc)
            raise

    async def send(self, message: str) -> None:
        await self.send_now(message)

    async def send_now(self, message: str) -> None:
        if self._client is None:
            raise WkClaudeError("Claude lane is not started")
        self._steering.enqueue(message, mode="now")
        await self._deliver_pending()

    async def send_on_idle(self, message: str) -> None:
        self._steering.enqueue(message, mode="on-idle")

    async def interrupt(self) -> None:
        if self._client is None:
            raise WkClaudeError("Claude lane is not started")
        try:
            await self._client.interrupt()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit_provider_error(exc)
            raise WkClaudeError(str(exc)) from exc

    async def replace(self, prompt: str) -> None:
        await self._close_transport()
        self._closed = False
        self._startup_ready = asyncio.Event()
        self._startup_error = None
        await self.start(prompt)

    async def archive(self) -> None:
        try:
            self._verify_turn_boundary()
            self.ledger.reconcile_transport()
            self.ledger.reconcile()
            raw = {"type": "archive", "session_id": self.run_id}
            self.translator.raw_events.append(raw)
            event = self.translator.sequencer.emit(
                run_id=self.run_id,
                agent_id=self.agent_id,
                kind="claude.archive",
                phase=WkEventPhase.ARCHIVE,
                provider="claude",
                lane="wk-claude",
                disposition=WkDisposition.RENDERED,
                ts=self.translator.timestamp(),
                payload=raw,
            )
            self.translator.session.record_event(event)
            await self._events.put({"raw": raw, "event": event.to_dict()})
            await self._close_transport()
            await self._events.put(None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit_provider_error(exc)
            raise WkClaudeError(str(exc)) from exc

    async def _close_transport(self) -> None:
        self._closed = True
        if self._client is not None:
            try:
                await self._client.disconnect()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._emit_provider_error(exc)
        if self._receive_task is not None:
            await asyncio.gather(self._receive_task, return_exceptions=True)
        self._client = None
        self._receive_task = None

    async def close(self) -> None:
        await self._close_transport()
        await self._events.put(None)

    async def events(self) -> AsyncIterator[Mapping[str, object]]:
        while True:
            item = await self._events.get()
            if item is None:
                return
            yield item


__all__ = [
    "WK_CLAUDE_MCP_TOOL_NAMES",
    "WK_CLAUDE_TOOL_NAMES",
    "WIKI_SYSTEM_PROMPT",
    "WkBashTool",
    "WkClaudeDisabled",
    "WkClaudeError",
    "WkClaudeEventTranslator",
    "WkClaudeLane",
    "WkClaudePlanAuthError",
    "WkClaudeToolBridge",
    "WkEditTool",
    "WkGateTool",
    "WkLedgerError",
    "WkReadTool",
    "WkSessionTree",
    "WkStatusTool",
    "WkSteeringQueue",
    "WkToolLedger",
    "WkWriteTool",
    "build_claude_sdk_options",
    "plan_auth_environment",
    "register_default_wk_tools",
]
