"""Codex App Server lane for the opt-in Wiki worker harness."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tomllib
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from .codex import CodexAppServerAdapter
from .normalizer import normalize_provider_event
from .provider import StartRequest
from .types import ProviderKind, RunRecord, utc_now
from .wk_common import WkLedgerError, WkSessionTree, WkSteeringQueue, WkToolBridge, WkToolLedger
from .wk_tools import register_default_wk_tools
from .wk_core import (
    WkDisposition,
    WkEventEnvelope,
    WkEventPhase,
    WkEventSequencer,
    WkLoop,
    WkRunMetadata,
    WkToolRegistry,
)
from .wk_feature import wk_enabled


WK_CODEX_TOOL_NAMES = (
    "wk.read",
    "wk.write",
    "wk.edit",
    "wk.bash",
    "wk.gate",
    "wk.status",
)
WK_CODEX_MCP_TOOL_NAMES = tuple(
    f"mcp__wiki__{name.removeprefix('wk.')}" for name in WK_CODEX_TOOL_NAMES
)
WK_CODEX_DYNAMIC_TOOLS = tuple(
    {
        "name": name.removeprefix("wk."),
        "description": f"Wiki-owned {name} tool",
        "inputSchema": {"type": "object", "additionalProperties": True},
    }
    for name in WK_CODEX_TOOL_NAMES
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
        "CODEX_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
    }
)
_CREDENTIAL_ENV_NAMES = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
        "CODEX_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "OPENAI_BEARER_TOKEN",
        "CODEX_BEARER_TOKEN",
        "HTTP_AUTHORIZATION",
    }
)


class WkCodexError(RuntimeError):
    """Base error for the Codex lane."""


class WkCodexDisabled(WkCodexError):
    """The wk feature flag is off."""


class WkCodexPlanAuthError(WkCodexError):
    """The lane cannot prove plan authentication."""


class WkCodexPolicyError(WkCodexError):
    """The App Server cannot prove Wiki-owned tool execution."""


def codex_plan_auth_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = dict(os.environ if environment is None else environment)
    configured = sorted(name for name in _CREDENTIAL_ENV_NAMES if source.get(name))
    if configured:
        raise WkCodexPlanAuthError(
            "Codex plan auth cannot use configured provider environment: "
            + ", ".join(configured)
        )
    result = {name: value for name, value in source.items() if name in _SAFE_ENV_NAMES}
    result.setdefault("CODEX_HOME", str(Path(result.get("HOME") or Path.home()) / ".codex"))
    return result


_FORBIDDEN_CONFIG_KEYS = frozenset(
    {
        "api_key",
        "apiKey",
        "bearer_token",
        "bearerToken",
        "bearer",
        "experimental_bearer_token",
        "oauth_token",
        "authorization",
        "http_headers",
        "httpHeaders",
        "headers",
        "static_headers",
        "env_key",
        "envKey",
        "auth_command",
        "authCommand",
        "command_auth",
        "commandAuth",
    }
)


def _reject_auth_sources(paths: Sequence[Path]) -> None:
    def walk(value: object, path: str) -> None:
        if not isinstance(value, Mapping):
            return
        for key, child in value.items():
            key_text = str(key)
            if key_text in _FORBIDDEN_CONFIG_KEYS:
                raise WkCodexPlanAuthError(
                    f"Codex plan auth rejects credential config source: {path}.{key_text}"
                )
            walk(child, f"{path}.{key_text}")

    for path in paths:
        try:
            with path.open("rb") as handle:
                walk(tomllib.load(handle), str(path))
        except FileNotFoundError:
            continue
        except tomllib.TOMLDecodeError as exc:
            raise WkCodexPlanAuthError(
                f"Codex settings are not valid TOML: {path}"
            ) from exc


def _settings_paths(worktree: Path, environment: Mapping[str, str]) -> tuple[Path, ...]:
    roots = {worktree / ".codex"}
    codex_home = environment.get("CODEX_HOME")
    if codex_home:
        roots.add(Path(codex_home))
    home = environment.get("HOME")
    if home:
        roots.add(Path(home) / ".codex")
    return tuple(sorted(root / "config.toml" for root in roots))


class _SettingsGuard:
    def __init__(self, paths: Sequence[Path]):
        self.paths = tuple(paths)
        self._fingerprint = self._read()

    def _read(self) -> tuple[tuple[str, bool, str], ...]:
        rows: list[tuple[str, bool, str]] = []
        for path in self.paths:
            try:
                rows.append((str(path), True, hashlib.sha256(path.read_bytes()).hexdigest()))
            except FileNotFoundError:
                rows.append((str(path), False, ""))
            except OSError as exc:
                raise WkCodexError(f"cannot inspect Codex settings source: {type(exc).__name__}") from exc
        return tuple(rows)

    def verify_unchanged(self) -> None:
        if self._read() != self._fingerprint:
            raise WkCodexError("Codex settings source changed during the run")


class WkCodexEventTranslator:
    """Retain raw App Server frames before shared envelope translation."""

    provider = "codex"
    lane = "wk-codex"

    def __init__(
        self,
        *,
        run_id: str,
        agent_id: str,
        sequencer: WkEventSequencer | None = None,
    ) -> None:
        self.run_id = run_id
        self.agent_id = agent_id
        self.sequencer = sequencer or WkEventSequencer()
        self.timestamp = utc_now
        self.raw_events: list[dict[str, Any]] = []
        self.session = WkSessionTree()

    @staticmethod
    def _phase(raw: Mapping[str, Any]) -> WkEventPhase:
        method = str(raw.get("method") or "")
        params = raw.get("params")
        item = params.get("item") if isinstance(params, Mapping) else None
        item_type = item.get("type") if isinstance(item, Mapping) else None
        if method == "thread/started":
            return WkEventPhase.RUN
        if method == "context/compacted":
            return WkEventPhase.COMPACTION
        if method in {"thread/archived", "thread/closed"}:
            return WkEventPhase.ARCHIVE
        if method == "item/tool/call":
            return WkEventPhase.TOOL
        if method.startswith("item/") and item_type in {
            "commandExecution",
            "fileChange",
            "mcpToolCall",
            "dynamicToolCall",
        }:
            return WkEventPhase.TOOL
        if method.startswith("item/"):
            return WkEventPhase.ASSISTANT
        if method.startswith("turn/"):
            return WkEventPhase.TURN
        return WkEventPhase.STATUS

    def error_event(self, error: BaseException, *, kind: str = "codex.provider_error") -> WkEventEnvelope:
        raw = {
            "method": kind,
            "params": {"error_class": type(error).__name__, "error": str(error)},
        }
        self.raw_events.append(raw)
        event = self.sequencer.emit(
            run_id=self.run_id,
            agent_id=self.agent_id,
            kind=kind,
            phase=WkEventPhase.STATUS,
            provider=self.provider,
            lane=self.lane,
            disposition=WkDisposition.RENDERED,
            ts=self.timestamp(),
            payload=raw,
            parent_source_event_id=(
                self.sequencer.events[-1].source_event_id if self.sequencer.events else None
            ),
        )
        self.session.record_event(event)
        return event

    def translate(self, raw: Mapping[str, Any], *, direction: str = "provider") -> WkEventEnvelope:
        value = json.loads(json.dumps(dict(raw), default=str))
        self.raw_events.append(value)
        normalized = normalize_provider_event(
            ProviderKind.CODEX,
            dict(value),
            direction=direction,
        )
        method = str(value.get("method") or "")
        params = value.get("params")
        failed_turn = (
            method == "turn/completed"
            and isinstance(params, Mapping)
            and isinstance(params.get("turn"), Mapping)
            and params["turn"].get("status") == "failed"
        )
        provider_error = method in {"error", "provider/protocolError", "provider/processExited"} or failed_turn
        kind = "codex.provider_error" if provider_error else f"codex.{normalized.kind}"
        disposition = {
            "rendered": WkDisposition.RENDERED,
            "summarized": WkDisposition.SUMMARIZED,
            "ignored": WkDisposition.INTENTIONALLY_IGNORED,
            "unknown": WkDisposition.UNKNOWN,
        }[normalized.disposition.value]
        event = self.sequencer.emit(
            run_id=self.run_id,
            agent_id=self.agent_id,
            kind=kind,
            phase=self._phase(value),
            provider=self.provider,
            lane=self.lane,
            disposition=disposition,
            ts=str(value.get("timestamp") or self.timestamp()),
            payload=value,
            parent_source_event_id=(
                self.sequencer.events[-1].source_event_id if self.sequencer.events else None
            ),
        )
        self.session.record_event(event)
        return event


class WkCodexLane:
    """Thin plan-auth App Server driver over the existing Codex adapter."""

    def __init__(
        self,
        *,
        metadata: WkRunMetadata,
        run_id: str,
        agent_id: str,
        worktree: Path,
        model: str,
        loop: WkLoop,
        command: Sequence[str] = ("codex", "app-server", "--stdio"),
        auth_command: Sequence[str] | None = None,
        environment: Mapping[str, str] | None = None,
        steering_path: Path | None = None,
        request_timeout: float = 30.0,
        wiki_command: Sequence[str] = ("wiki",),
    ) -> None:
        if not wk_enabled():
            raise WkCodexDisabled("WIKI_ENABLE_WK=1 is required for wk-codex")
        if metadata.kind != "wk-codex":
            raise ValueError("WkCodexLane requires kind=wk-codex")
        self.metadata = metadata
        self.run_id = run_id
        self.agent_id = agent_id
        self.worktree = worktree
        self.model = model
        self.loop = loop
        self.environment = codex_plan_auth_environment(environment)
        del auth_command
        self.command = tuple(command)
        self._settings_guard = _SettingsGuard(_settings_paths(worktree, self.environment))
        _reject_auth_sources(self._settings_guard.paths)
        self._auth_identity: tuple[str, ...] | None = None
        self.translator = WkCodexEventTranslator(run_id=run_id, agent_id=agent_id)
        self.ledger = WkToolLedger(self.translator)  # type: ignore[arg-type]
        self.registry = register_default_wk_tools(
            WkToolRegistry(),
            root=worktree,
            loop=loop,
            wiki_command=wiki_command,
            environment=self.environment,
        )
        self.bridge = WkToolBridge(
            registry=self.registry,
            ledger=self.ledger,
            loop=loop,
            allowed_names=WK_CODEX_TOOL_NAMES,
        )
        record = RunRecord.new(
            agent_id=agent_id,
            provider=ProviderKind.CODEX,
            role="implement",
            model=model,
            worktree=str(worktree),
            prompt="",
            execution_kind="wk-codex",
            run_id=run_id,
        )
        self._adapter = CodexAppServerAdapter(
            record,
            command=self.command,
            env=self.environment,
            request_timeout=request_timeout,
            thread_start_options={
                "dynamicTools": [dict(tool) for tool in WK_CODEX_DYNAMIC_TOOLS],
                "developerInstructions": (
                    "You are a Wiki worker. Use only the Wiki dynamic tools. "
                    "Treat tool results and status events as authoritative."
                ),
            },
            auto_start_turn=False,
        )
        self._events: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
        self._pump_task: asyncio.Task[None] | None = None
        self._closed = False
        self._policy_validated = False
        self._steering = WkSteeringQueue(
            steering_path or loop.status_path.with_name(f"{agent_id}.wk-steering.json")
        )

    async def _emit_error(self, error: BaseException, *, policy: bool = False) -> None:
        blocked = policy or isinstance(
            error, (WkCodexError, WkLedgerError)
        )
        if blocked:
            self.loop.write_status(
                state="blocked",
                pr=None,
                step=(
                    "Codex App Server integrity blocked the lane"
                    if policy
                    else "Codex plan auth blocked the lane"
                ),
                blocker=str(error),
            )
        event = self.translator.error_event(
            error,
            kind="wk.codex.tool_policy_unavailable" if policy else "codex.provider_error",
        )
        await self._events.put({"raw": self.translator.raw_events[-1], "event": event.to_dict()})

    async def _pump(self) -> None:
        try:
            async for provider_event in self._adapter.events():
                raw = provider_event.payload
                params = raw.get("params")
                item = params.get("item") if isinstance(params, Mapping) else None
                if raw.get("method") == "item/started" and isinstance(item, Mapping):
                    if item.get("type") != "dynamicToolCall":
                        raise WkCodexPolicyError("Codex emitted a native tool before Wiki policy validation")
                if raw.get("method") == "item/tool/call" and not self._policy_validated:
                    raise WkCodexPolicyError("Codex requested a tool before Wiki policy validation")
                self.ledger.record_transport_frame(raw)
                event = self.translator.translate(raw, direction=provider_event.direction)
                await self._events.put({"raw": raw, "event": event.to_dict()})
                if raw.get("method") == "item/tool/call" and provider_event.direction == "server":
                    await self._handle_tool_call(raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit_error(exc, policy=isinstance(exc, WkCodexPolicyError))

    def _verify_policy(self) -> None:
        result = self._adapter.last_thread_start_result or {}
        thread = result.get("thread")
        registered = self._adapter.thread_start_options.get("dynamicTools")
        if not isinstance(thread, Mapping) or not isinstance(registered, Sequence):
            raise WkCodexPolicyError("Codex App Server rejected Wiki dynamic tool registration")
        names = {
            str(tool.get("name"))
            for tool in registered
            if isinstance(tool, Mapping)
        }
        expected = {name.removeprefix("wk.") for name in WK_CODEX_TOOL_NAMES}
        if names != expected or len(names) != len(registered):
            raise WkCodexPolicyError("Codex App Server dynamic tools do not prove Wiki ownership")

    @staticmethod
    def _plan_identity(value: Mapping[str, Any]) -> tuple[str, ...]:
        account = value.get("account")
        if not isinstance(account, Mapping):
            raise WkCodexPlanAuthError("Codex App Server account/read returned no account")
        if account.get("type") != "chatgpt" or not isinstance(account.get("planType"), str):
            raise WkCodexPlanAuthError("Codex App Server account/read did not prove plan auth")
        identity = tuple(
            str(account.get(key) or "")
            for key in ("type", "planType", "email", "id", "accountId", "organizationId")
        )
        if not any(identity[2:]):
            raise WkCodexPlanAuthError("Codex App Server account/read returned no stable identity")
        return identity

    async def _verify_account(self) -> None:
        current = self._plan_identity(await self._adapter.account_read())
        if self._auth_identity is None:
            self._auth_identity = current
        elif current != self._auth_identity:
            raise WkCodexPlanAuthError("Codex plan auth identity changed during the run")

    async def _handle_tool_call(self, raw: Mapping[str, Any]) -> None:
        request_id = raw.get("id")
        params = raw.get("params")
        if not isinstance(request_id, (str, int)) or not isinstance(params, Mapping):
            raise WkCodexPolicyError("Codex dynamic tool request is malformed")
        if params.get("namespace") != "wiki":
            raise WkCodexPolicyError("Codex requested a non-Wiki tool namespace")
        tool = params.get("tool")
        call_id = params.get("callId")
        arguments = params.get("arguments")
        if not isinstance(tool, str) or not isinstance(call_id, str) or not isinstance(arguments, Mapping):
            raise WkCodexPolicyError("Codex dynamic tool request is incomplete")
        result = await self.bridge.invoke(f"wk.{tool}", arguments, call_id=call_id)
        for event in self.ledger.events[-2:]:
            await self._events.put({"raw": {"method": event.kind}, "event": event.to_dict()})
        await self._adapter.respond(
            request_id,
            {
                "contentItems": [{"type": "inputText", "text": json.dumps(result.to_dict(), sort_keys=True)}],
                "success": result.success,
            },
        )

    async def _verify_turn_boundary(self, *, require_policy: bool = True) -> None:
        self._settings_guard.verify_unchanged()
        _reject_auth_sources(self._settings_guard.paths)
        await self._verify_account()
        if require_policy:
            await self._adapter.status()
            self._verify_policy()

    async def _block(self, error: BaseException, *, policy: bool = False) -> None:
        await self._emit_error(error, policy=policy)
        await self._adapter.close()
        raise WkCodexError(str(error)) from error

    async def start(self, prompt: str) -> None:
        try:
            self._settings_guard.verify_unchanged()
            _reject_auth_sources(self._settings_guard.paths)
            self._pump_task = asyncio.create_task(self._pump())
            await self._adapter.start(
                StartRequest(
                    prompt=prompt,
                    model=self.model,
                    effort=None,
                    worktree=str(self.worktree),
                    run_id=self.run_id,
                    agent_id=self.agent_id,
                )
            )
            await self._verify_turn_boundary()
            self._verify_policy()
            self._policy_validated = True
            await self._adapter.start_turn(prompt)
        except WkCodexPolicyError as exc:
            await self._block(exc, policy=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._block(exc, policy=True)

    async def resume(self, session_id: str) -> None:
        try:
            self._settings_guard.verify_unchanged()
            _reject_auth_sources(self._settings_guard.paths)
            if self._pump_task is None:
                self._pump_task = asyncio.create_task(self._pump())
            await self._adapter.resume(session_id)
            await self._verify_turn_boundary()
            self._verify_policy()
            self._policy_validated = True
            if self._adapter.snapshot().state.value == "working":
                await self._adapter.start_turn(
                    f"resume Wiki worker {self.agent_id} from the durable session"
                )
        except WkCodexPolicyError as exc:
            await self._block(exc, policy=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._block(exc, policy=True)

    async def _deliver(self, item_id: str, message: str) -> None:
        await self._verify_turn_boundary()
        await self._adapter.send_now(message)
        self._steering.acknowledge(item_id)

    async def send_now(self, message: str) -> None:
        item = self._steering.enqueue(message, mode="now")
        try:
            await self._deliver(item.message_id, message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit_error(exc)
            raise WkCodexError(str(exc)) from exc

    async def send_on_idle(self, message: str) -> None:
        item = self._steering.enqueue(message, mode="on-idle")
        try:
            if self._adapter.snapshot().state.value == "idle":
                await self._deliver(item.message_id, message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit_error(exc)
            raise WkCodexError(str(exc)) from exc

    async def interrupt(self) -> None:
        try:
            await self._verify_turn_boundary()
            await self._adapter.interrupt()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit_error(exc)
            raise WkCodexError(str(exc)) from exc

    async def replace(self, prompt: str) -> None:
        try:
            await self._verify_turn_boundary()
            await self._adapter.replace(prompt)
            self._verify_policy()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit_error(exc)
            raise WkCodexError(str(exc)) from exc

    async def respond(self, request_id: str | int, response: dict[str, Any]) -> None:
        try:
            await self._adapter.respond(request_id, response)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit_error(exc)
            raise WkCodexError(str(exc)) from exc

    async def close(self) -> None:
        self._closed = True
        await self._adapter.close()
        if self._pump_task is not None:
            await asyncio.gather(self._pump_task, return_exceptions=True)
        try:
            self.ledger.reconcile_transport()
            self.ledger.reconcile()
        except WkLedgerError as exc:
            await self._emit_error(exc, policy=True)
            raise
        await self._events.put(None)

    async def archive(self) -> None:
        try:
            await self._verify_turn_boundary()
            await self._adapter.archive()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit_error(exc)
            raise WkCodexError(str(exc)) from exc

    async def events(self) -> AsyncIterator[Mapping[str, object]]:
        while True:
            item = await self._events.get()
            if item is None:
                return
            yield item


__all__ = [
    "WK_CODEX_MCP_TOOL_NAMES",
    "WK_CODEX_TOOL_NAMES",
    "WkCodexDisabled",
    "WkCodexError",
    "WkCodexEventTranslator",
    "WkCodexLane",
    "WkCodexPlanAuthError",
    "WkCodexPolicyError",
    "codex_plan_auth_environment",
]
