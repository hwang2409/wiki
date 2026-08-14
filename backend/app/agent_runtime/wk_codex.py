"""Codex App Server lane for the opt-in Wiki worker harness."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from .codex import CodexAppServerAdapter
from .normalizer import normalize_provider_event
from .provider import StartRequest
from .types import ProviderKind, RunRecord, utc_now
from .wk_claude import (
    WkClaudeToolBridge,
    WkSessionTree,
    WkSteeringQueue,
    WkToolLedger,
    register_default_wk_tools,
)
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


def _file_fingerprint(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _verify_codex_plan_auth(
    command: Sequence[str], environment: Mapping[str, str]
) -> dict[str, str | None]:
    if not command:
        raise WkCodexPlanAuthError("Codex CLI path is required to prove plan auth")
    try:
        completed = subprocess.run(
            [*command, "login", "status"],
            env=codex_plan_auth_environment(environment),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WkCodexPlanAuthError(
            f"Codex login status could not prove plan auth: {type(exc).__name__}"
        ) from exc
    output = (completed.stdout or "").strip()
    if completed.returncode != 0 or "logged in using chatgpt" not in output.casefold():
        raise WkCodexPlanAuthError("Codex login status did not prove ChatGPT plan auth")
    identity = output.split("account=", 1)[-1] if "account=" in output else ""
    account_id, _, organization_id = identity.partition(" organization=")
    auth_path = Path(codex_plan_auth_environment(environment)["CODEX_HOME"]) / "auth.json"
    return {
        "auth_status_hash": hashlib.sha256(output.encode()).hexdigest(),
        "account_id": account_id or None,
        "organization_id": organization_id or None,
        "credential_fingerprint": _file_fingerprint(auth_path),
    }


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
        self.auth_command = tuple(auth_command or (command[0],))
        self.command = tuple(command)
        self._settings_guard = _SettingsGuard(_settings_paths(worktree, self.environment))
        self._auth_identity: dict[str, str | None] | None = None
        self.translator = WkCodexEventTranslator(run_id=run_id, agent_id=agent_id)
        self.ledger = WkToolLedger(self.translator)  # type: ignore[arg-type]
        self.registry = register_default_wk_tools(
            WkToolRegistry(),
            root=worktree,
            loop=loop,
            wiki_command=wiki_command,
            environment=self.environment,
        )
        self.bridge = WkClaudeToolBridge(registry=self.registry, ledger=self.ledger, loop=loop)
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
        policy = {
            "owner": "wiki",
            "tools": list(WK_CODEX_MCP_TOOL_NAMES),
            "nativeToolsDisabled": True,
        }
        self._adapter = CodexAppServerAdapter(
            record,
            command=self.command,
            env=self.environment,
            request_timeout=request_timeout,
            thread_start_options={"toolPolicy": policy},
        )
        self._events: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
        self._pump_task: asyncio.Task[None] | None = None
        self._closed = False
        self._steering = WkSteeringQueue(
            steering_path or loop.status_path.with_name(f"{agent_id}.wk-steering.json")
        )

    async def _emit_error(self, error: BaseException, *, policy: bool = False) -> None:
        blocked = policy or isinstance(error, WkCodexPlanAuthError)
        if blocked:
            self.loop.write_status(
                state="blocked",
                pr=None,
                step=(
                    "Codex App Server tool policy blocked the lane"
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
                self.ledger.record_transport_frame(raw)
                event = self.translator.translate(raw, direction=provider_event.direction)
                await self._events.put({"raw": raw, "event": event.to_dict()})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit_error(exc)

    def _verify_policy(self) -> None:
        result = self._adapter.last_thread_start_result or {}
        policy = result.get("toolPolicy")
        if not isinstance(policy, Mapping):
            raise WkCodexPolicyError("Codex App Server did not return a tool policy proof")
        tools = policy.get("tools")
        if not isinstance(tools, Sequence) or isinstance(tools, (str, bytes)):
            raise WkCodexPolicyError("Codex App Server tool policy does not prove Wiki ownership")
        if (
            policy.get("owner") != "wiki"
            or {str(tool) for tool in tools} != set(WK_CODEX_MCP_TOOL_NAMES)
            or policy.get("nativeToolsDisabled") is not True
        ):
            raise WkCodexPolicyError("Codex App Server tool policy does not prove Wiki ownership")

    async def _verify_turn_boundary(self, *, require_policy: bool = True) -> None:
        self._settings_guard.verify_unchanged()
        current = _verify_codex_plan_auth(self.auth_command, self.environment)
        if self._auth_identity is None:
            self._auth_identity = current
        elif current != self._auth_identity:
            raise WkCodexPlanAuthError("Codex plan auth identity changed during the run")
        if require_policy:
            await self._adapter.status()
            self._verify_policy()

    async def _block(self, error: BaseException, *, policy: bool = False) -> None:
        await self._emit_error(error, policy=policy)
        await self._adapter.close()
        raise WkCodexError(str(error)) from error

    async def start(self, prompt: str) -> None:
        try:
            await self._verify_turn_boundary(require_policy=False)
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
            self._verify_policy()
        except WkCodexPolicyError as exc:
            await self._block(exc, policy=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit_error(exc)
            raise WkCodexError(str(exc)) from exc

    async def resume(self, session_id: str) -> None:
        try:
            await self._verify_turn_boundary(require_policy=False)
            if self._pump_task is None:
                self._pump_task = asyncio.create_task(self._pump())
            await self._adapter.resume(session_id)
            self._verify_policy()
        except WkCodexPolicyError as exc:
            await self._block(exc, policy=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit_error(exc)
            raise WkCodexError(str(exc)) from exc

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
        self.ledger.reconcile_transport()
        self.ledger.reconcile()
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
