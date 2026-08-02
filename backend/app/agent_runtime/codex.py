from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..wiki_artifacts import artifact_server_command, artifact_server_environment
from .process import (
    ProviderProcessIdentity,
    command_tuple,
    resolve_provider_identity,
    terminate_process_group,
)
from .provider import (
    AdapterStatus,
    ProviderAdapter,
    ProviderBusy,
    ProviderEvent,
    ProviderProcessError,
    ProviderProtocolError,
    StartRequest,
)
from .types import LifecycleState, ProviderKind, RunRecord


IdentityResolver = Callable[..., Awaitable[ProviderProcessIdentity | None]]
_IDENTITY_VERIFY_DELAYS_SECONDS = (0.1, 0.2, 0.4, 0.8, 1.0, 1.0, 1.0, 1.0, 1.0)


@dataclass
class _PendingRequest:
    future: asyncio.Future[dict[str, Any]]
    generation: int
    method: str


@dataclass(frozen=True)
class _ServerRequest:
    raw_id: str | int
    generation: int
    method: str


@dataclass(frozen=True)
class _StreamEnd:
    generation: int


_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "item/tool/requestUserInput",
    "mcpServer/elicitation/request",
    "execCommandApproval",
    "applyPatchApproval",
}


def _request_key(request_id: str | int) -> tuple[str, str | int]:
    return ("int", request_id) if isinstance(request_id, int) else ("str", request_id)


class CodexAppServerAdapter(ProviderAdapter):
    """One stdio Codex App Server transport bound to one durable Wiki run."""

    provider = ProviderKind.CODEX

    def __init__(
        self,
        record: RunRecord,
        *,
        command: Sequence[str] = ("codex", "app-server", "--stdio"),
        env: Mapping[str, str] | None = None,
        request_timeout: float = 30.0,
        identity_resolver: IdentityResolver = resolve_provider_identity,
    ):
        child_env = dict(os.environ if env is None else env)
        child_env.pop("TMUX", None)
        child_env.pop("TMUX_PANE", None)
        child_env.setdefault(
            "WIKI_AGENT_RUNTIME_DIR",
            str(Path(child_env.get("HOME") or Path.home()) / ".wiki" / "agent-runtime"),
        )
        self.env = child_env
        self.base_command = command_tuple(command)
        self._restart_for_runtime_change = False
        self._configure_runtime(record)
        self.request_timeout = request_timeout
        self.identity_resolver = identity_resolver
        self.worktree = record.worktree
        self.model = record.model
        self.effort = record.effort
        self._resume_state = record.recovery_from_state or record.state

        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._events: asyncio.Queue[ProviderEvent | _StreamEnd] = asyncio.Queue()
        self._pending: dict[str, _PendingRequest] = {}
        self._server_request_ids: dict[tuple[str, str | int], _ServerRequest] = {}
        self._server_request_revision = 0
        self._thread_generations: dict[str, int] = {}
        self._suppress_stream_end: set[int] = set()
        self._request_id = 0
        self._generation = record.provider_generation
        self._state = record.state
        self._state_revision = 0
        self._session_id = record.provider_session_id
        self._active_turn_id = record.active_turn_id
        self._transcript_path = record.transcript_path
        self._provider_pid: int | None = None
        self._process_created_callback: Callable[[int], None] | None = None
        self._detail: str | None = None
        self._request: StartRequest | None = None
        self._write_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._turn_tasks: set[asyncio.Task[None]] = set()
        self._turn_start_pending = False

    def _configure_runtime(self, record: RunRecord) -> None:
        self.env["WIKI_RUN_ID"] = record.run_id
        self.env["WIKI_AGENT_ID"] = record.agent_id
        self.env["WIKI_AGENT_ROLE"] = record.role
        if record.backend_base_url:
            self.env["WIKI_BACKEND_URL"] = record.backend_base_url
        server_command = artifact_server_command()
        server_env = artifact_server_environment(self.env, record.run_id)
        toml_env = "{ " + ", ".join(
            f"{key} = {json.dumps(value)}" for key, value in server_env.items()
        ) + " }"
        self.command = command_tuple(
            (
                *self.base_command,
                "-c",
                f"mcp_servers.wiki_artifacts.command={json.dumps(server_command[0])}",
                "-c",
                f"mcp_servers.wiki_artifacts.args={json.dumps(list(server_command[1:]))}",
                "-c",
                f"mcp_servers.wiki_artifacts.env={toml_env}",
            )
        )
        self.run_id = record.run_id
        self.agent_id = record.agent_id

    def prepare_replacement(self, record: RunRecord) -> None:
        self._configure_runtime(record)
        self._restart_for_runtime_change = True

    def set_process_created_callback(self, callback: Callable[[int], None]) -> None:
        self._process_created_callback = callback

    def _status(self) -> AdapterStatus:
        return AdapterStatus(
            state=self._state,
            session_id=self._session_id,
            pid=self._provider_pid,
            generation=self._generation,
            active_turn_id=self._active_turn_id,
            transcript_path=self._transcript_path,
            detail=self._detail,
        )

    def _set_event_state(
        self,
        state: LifecycleState,
        *,
        active_turn_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        self._state = state
        self._active_turn_id = active_turn_id
        self._detail = detail
        self._state_revision += 1

    def _process_is_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def _emit(
        self, payload: dict[str, Any], *, direction: str, generation: int
    ) -> None:
        await self._events.put(
            ProviderEvent(
                provider=self.provider,
                payload=payload,
                direction=direction,
                generation=generation,
            )
        )

    async def _spawn(self, generation: int) -> None:
        if self._process_is_alive():
            raise ProviderProcessError("Codex App Server is already running")
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=self.worktree,
                env=self.env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=4 * 1024 * 1024,
                start_new_session=True,
            )
        except OSError as exc:
            raise ProviderProcessError(
                f"could not start Codex App Server: {exc}"
            ) from exc
        self._process = process
        if self._process_created_callback is not None:
            self._process_created_callback(process.pid)
        self._generation = generation
        self._provider_pid = None
        self._state = LifecycleState.STARTING
        self._detail = None
        self._reader_task = asyncio.create_task(
            self._reader_loop(process, generation),
            name=f"codex-reader-{self.run_id}",
        )
        self._stderr_task = asyncio.create_task(
            self._stderr_loop(process),
            name=f"codex-stderr-{self.run_id}",
        )
        try:
            await self._rpc(
                "initialize",
                {
                    "clientInfo": {
                        "name": "wiki-agent-supervisor",
                        "title": "Wiki agent supervisor",
                        "version": "1",
                    },
                    "capabilities": {
                        "experimentalApi": True,
                        "requestAttestation": False,
                    },
                },
                generation=generation,
            )
            await self._notify("initialized", {}, generation=generation)
        except Exception:
            await terminate_process_group(process)
            await asyncio.gather(
                *(
                    task
                    for task in (self._reader_task, self._stderr_task)
                    if task is not None
                ),
                return_exceptions=True,
            )
            raise

    async def _send_json(
        self,
        message: dict[str, Any],
        *,
        generation: int,
    ) -> None:
        process = self._process
        if process is None or process.returncode is not None or process.stdin is None:
            raise ProviderProcessError("Codex App Server stdin is unavailable")
        await self._emit(message, direction="client", generation=generation)
        encoded = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        async with self._write_lock:
            process.stdin.write(encoded)
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise ProviderProcessError("Codex App Server closed stdin") from exc

    async def _rpc(
        self,
        method: str,
        params: dict[str, Any],
        *,
        generation: int | None = None,
    ) -> dict[str, Any]:
        event_generation = generation or self._generation
        self._request_id += 1
        request_id = self._request_id
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        key = str(request_id)
        self._pending[key] = _PendingRequest(future, event_generation, method)
        server_request_revision = self._server_request_revision
        try:
            await self._send_json(
                {"id": request_id, "method": method, "params": params},
                generation=event_generation,
            )
            while True:
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(future),
                        timeout=self.request_timeout,
                    )
                except TimeoutError:
                    if future.done():
                        return await future
                    has_pending_server_request = any(
                        request.generation == event_generation
                        for request in self._server_request_ids.values()
                    )
                    if (
                        has_pending_server_request
                        or self._server_request_revision != server_request_revision
                    ):
                        # App Server may deliberately hold turn/start until a
                        # human answers requestUserInput/approval. The control
                        # stream is live, so a transport timeout must not kill
                        # the provider while that server request is pending.
                        server_request_revision = self._server_request_revision
                        continue
                    raise
        except TimeoutError as exc:
            raise ProviderProtocolError(f"Codex request timed out: {method}") from exc
        finally:
            self._pending.pop(key, None)

    async def _notify(
        self,
        method: str,
        params: dict[str, Any],
        *,
        generation: int | None = None,
    ) -> None:
        await self._send_json(
            {"method": method, "params": params},
            generation=generation or self._generation,
        )

    def _message_generation(self, message: dict[str, Any]) -> int:
        message_id = message.get("id")
        if "method" not in message and isinstance(message_id, (str, int)):
            pending = self._pending.get(str(message_id))
            if pending is not None:
                return pending.generation
        params = message.get("params")
        if isinstance(params, dict):
            thread_id = params.get("threadId")
            if isinstance(thread_id, str) and thread_id in self._thread_generations:
                return self._thread_generations[thread_id]
        return self._generation

    def _has_pending_approval(self, generation: int) -> bool:
        return any(
            request.generation == generation and request.method in _APPROVAL_METHODS
            for request in self._server_request_ids.values()
        )

    def _apply_message_state(self, message: dict[str, Any], generation: int) -> None:
        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        # App Server multiplexes threads on one transport. Notifications for
        # the archived thread can arrive after replacement has advanced the
        # generation; persist them, but never let them mutate the live thread.
        if generation != self._generation:
            return
        if isinstance(method, str) and message.get("id") is not None:
            raw_id = message["id"]
            if isinstance(raw_id, (str, int)):
                self._server_request_ids[_request_key(raw_id)] = _ServerRequest(
                    raw_id,
                    generation,
                    method,
                )
                self._server_request_revision += 1
        if method == "serverRequest/resolved":
            resolved_id = params.get("requestId")
            if isinstance(resolved_id, (str, int)):
                resolved = self._server_request_ids.pop(
                    _request_key(resolved_id),
                    None,
                )
                if resolved is not None:
                    self._server_request_revision += 1
                if (
                    resolved is not None
                    and resolved.method in _APPROVAL_METHODS
                    and self._state is LifecycleState.WAITING_APPROVAL
                    and not self._has_pending_approval(generation)
                ):
                    self._set_event_state(
                        LifecycleState.WORKING,
                        active_turn_id=self._active_turn_id,
                    )
        if method in _APPROVAL_METHODS:
            self._set_event_state(
                LifecycleState.WAITING_APPROVAL,
                active_turn_id=params.get("turnId") or self._active_turn_id,
            )
        elif method == "turn/started":
            turn = params.get("turn") or {}
            self._set_event_state(
                LifecycleState.WORKING,
                active_turn_id=turn.get("id") or params.get("turnId"),
            )
        elif method == "thread/status/changed":
            status = params.get("status") or {}
            status_type = status.get("type")
            active_flags = set(status.get("activeFlags") or [])
            if active_flags & {"waitingOnApproval", "waitingOnUserInput"}:
                self._set_event_state(
                    LifecycleState.WAITING_APPROVAL,
                    active_turn_id=self._active_turn_id,
                )
            elif status_type == "active":
                self._set_event_state(
                    LifecycleState.WORKING,
                    active_turn_id=self._active_turn_id,
                )
            elif status_type == "idle" and self._state is not LifecycleState.COMPLETED:
                self._set_event_state(LifecycleState.IDLE)
            elif status_type == "systemError":
                self._set_event_state(
                    LifecycleState.BLOCKED,
                    detail="Codex thread entered systemError",
                )
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            status = turn.get("status")
            state = {
                "completed": LifecycleState.IDLE,
                "interrupted": LifecycleState.INTERRUPTED,
                "failed": LifecycleState.BLOCKED,
            }.get(status)
            if state is not None:
                self._set_event_state(
                    state, detail=str(turn.get("error") or "") or None
                )
        elif method in {"thread/archived", "thread/closed"}:
            self._set_event_state(LifecycleState.COMPLETED)
        elif method == "error" and not params.get("willRetry"):
            error = (
                params.get("error") or params.get("message") or "Codex provider error"
            )
            self._set_event_state(LifecycleState.BLOCKED, detail=str(error))
        elif method == "thread/started":
            thread = params.get("thread") or {}
            thread_id = thread.get("id")
            if isinstance(thread_id, str):
                self._session_id = thread_id
                self._thread_generations[thread_id] = generation
            path = thread.get("path")
            if isinstance(path, str):
                self._transcript_path = path

    async def _reader_loop(
        self,
        process: asyncio.subprocess.Process,
        process_generation: int,
    ) -> None:
        assert process.stdout is not None
        reader_error: Exception | None = None
        try:
            while line := await process.stdout.readline():
                try:
                    value = json.loads(line)
                except ValueError:
                    value = {
                        "method": "provider/protocolError",
                        "params": {
                            "line": line.decode("utf-8", errors="replace").rstrip("\n")
                        },
                    }
                if not isinstance(value, dict):
                    value = {
                        "method": "provider/protocolError",
                        "params": {"value": value},
                    }
                generation = self._message_generation(value)
                self._apply_message_state(value, generation)
                await self._emit(value, direction="server", generation=generation)

                message_id = value.get("id")
                if "method" in value or not isinstance(message_id, (str, int)):
                    if value.get("method") == "currentTime/read" and isinstance(
                        message_id,
                        (str, int),
                    ):
                        await self.respond(
                            message_id, {"currentTimeAt": int(time.time())}
                        )
                    continue
                pending = self._pending.get(str(message_id))
                if pending is None or pending.future.done():
                    continue
                error = value.get("error")
                if error is not None:
                    pending.future.set_exception(
                        ProviderProtocolError(
                            f"Codex {pending.method} failed: {json.dumps(error, sort_keys=True)}"
                        )
                    )
                    continue
                result = value.get("result")
                pending.future.set_result(result if isinstance(result, dict) else {})
        except asyncio.CancelledError:
            await terminate_process_group(process)
            raise
        except Exception as exc:
            reader_error = exc
            await self._emit(
                {
                    "method": "provider/protocolError",
                    "params": {"error": f"{type(exc).__name__}: {exc}"},
                },
                direction="process",
                generation=process_generation,
            )
            await terminate_process_group(process)
        finally:
            # EOF is also a transport failure if the child stays alive. Never
            # await a provider that has closed its only control/output stream.
            if process.returncode is None:
                await terminate_process_group(process)
            returncode = await process.wait()
            if self._state is not LifecycleState.COMPLETED:
                self._state = LifecycleState.DEAD
                self._active_turn_id = None
                suffix = (
                    f" after reader failure: {reader_error}" if reader_error else ""
                )
                self._detail = (
                    f"Codex App Server exited with status {returncode}{suffix}"
                )
            await self._emit(
                {
                    "method": "provider/processExited",
                    "params": {"returncode": returncode},
                },
                direction="process",
                generation=process_generation,
            )
            error = ProviderProcessError(self._detail or "Codex App Server exited")
            for pending in self._pending.values():
                if not pending.future.done():
                    pending.future.set_exception(error)
            await self._events.put(_StreamEnd(process_generation))

    async def _stderr_loop(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        try:
            while line := await process.stderr.readline():
                await self._emit(
                    {
                        "method": "provider/stderr",
                        "params": {
                            "text": line.decode("utf-8", errors="replace").rstrip("\n")
                        },
                    },
                    direction="stderr",
                    generation=self._generation,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit(
                {
                    "method": "provider/protocolError",
                    "params": {
                        "stream": "stderr",
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                },
                direction="process",
                generation=self._generation,
            )
            await terminate_process_group(process)

    @staticmethod
    def _thread_from_result(result: dict[str, Any]) -> dict[str, Any]:
        thread = result.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise ProviderProtocolError("Codex response did not include a thread id")
        return thread

    def _remember_thread(self, thread: dict[str, Any], generation: int) -> None:
        self._session_id = str(thread["id"])
        self._thread_generations[self._session_id] = generation
        path = thread.get("path")
        if isinstance(path, str):
            self._transcript_path = path
        status = thread.get("status") or {}
        status_type = status.get("type") if isinstance(status, dict) else None
        active_flags = (
            set(status.get("activeFlags") or []) if isinstance(status, dict) else set()
        )
        if active_flags & {"waitingOnApproval", "waitingOnUserInput"}:
            self._state = LifecycleState.WAITING_APPROVAL
        elif (
            status_type == "active"
            and self._state is not LifecycleState.WAITING_APPROVAL
        ):
            self._state = LifecycleState.WORKING
        elif status_type == "idle" and self._state not in {
            LifecycleState.INTERRUPTED,
            LifecycleState.BLOCKED,
            LifecycleState.COMPLETED,
        }:
            self._state = LifecycleState.IDLE

    async def _refresh_identity(self, *, required: bool = False) -> None:
        wrapper_pid = (
            self._process.pid if self._process_is_alive() and self._process else None
        )
        attempts = len(_IDENTITY_VERIFY_DELAYS_SECONDS) + 1 if required else 1
        for attempt in range(attempts):
            identity = await self.identity_resolver(
                wrapper_pid,
                self.provider,
                self._session_id,
                reported_path=self._transcript_path,
            )
            if identity is not None:
                self._provider_pid = identity.pid
                self._transcript_path = identity.transcript_path
                return
            if attempt + 1 < attempts:
                await asyncio.sleep(_IDENTITY_VERIFY_DELAYS_SECONDS[attempt])
        if required:
            raise ProviderProcessError(
                "Codex provider PID/transcript could not be verified from open handles"
            )

    @staticmethod
    def _text_input(text: str) -> list[dict[str, Any]]:
        return [{"type": "text", "text": text, "text_elements": []}]

    async def _complete_turn_start(
        self,
        text: str,
        revision: int,
        generation: int,
    ) -> None:
        try:
            params: dict[str, Any] = {
                "threadId": self._session_id,
                "input": self._text_input(text),
                "approvalPolicy": "never",
            }
            if self.effort:
                params["effort"] = self.effort
            result = await self._rpc("turn/start", params, generation=generation)
            turn = result.get("turn") or {}
            turn_id = turn.get("id")
            if self._state_revision == revision or (
                self._state is LifecycleState.WORKING and self._active_turn_id is None
            ):
                self._state = LifecycleState.WORKING
                self._active_turn_id = turn_id if isinstance(turn_id, str) else None
                self._detail = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._state not in {LifecycleState.DEAD, LifecycleState.COMPLETED}:
                self._set_event_state(
                    LifecycleState.BLOCKED,
                    detail=f"Codex turn/start failed: {exc}",
                )
            await self._emit(
                {
                    "method": "error",
                    "params": {
                        "operation": "turn/start",
                        "message": str(exc),
                        "willRetry": False,
                        "source": "adapter",
                    },
                },
                direction="server",
                generation=generation,
            )
        finally:
            self._turn_start_pending = False

    async def _start_turn(self, text: str) -> None:
        if not self._session_id:
            raise ProviderProtocolError("Codex thread is not initialized")
        if self._turn_start_pending:
            raise ProviderBusy(
                "Codex turn/start is already awaiting provider acceptance"
            )
        revision = self._state_revision
        generation = self._generation
        self._state = LifecycleState.WORKING
        self._active_turn_id = None
        self._detail = None
        self._turn_start_pending = True
        task = asyncio.create_task(
            self._complete_turn_start(text, revision, generation),
            name=f"codex-turn-start-{self.run_id}-{generation}",
        )
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)
        # Let the task enqueue/write the request before returning control to
        # the supervisor. The response may legitimately wait on a human.
        await asyncio.sleep(0)

    async def _cancel_turn_tasks(self) -> None:
        tasks = list(self._turn_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._turn_tasks.clear()
        self._turn_start_pending = False

    async def _start_thread(self, prompt: str, generation: int) -> None:
        result = await self._rpc(
            "thread/start",
            {
                "cwd": self.worktree,
                "runtimeWorkspaceRoots": [self.worktree],
                "model": self.model,
                "approvalPolicy": "never",
                "sandbox": "danger-full-access",
                "experimentalRawEvents": True,
                "historyMode": "legacy",
            },
            generation=generation,
        )
        self._remember_thread(self._thread_from_result(result), generation)
        await self._start_turn(prompt)
        await self._refresh_identity(required=True)

    async def start(self, request: StartRequest) -> AdapterStatus:
        async with self._operation_lock:
            self._request = request
            self.worktree = request.worktree
            self.model = request.model
            self.effort = request.effort
            generation = self._generation + 1
            await self._spawn(generation)
            try:
                await self._start_thread(request.prompt, generation)
            except BaseException:
                await asyncio.shield(terminate_process_group(self._process))
                raise
            return self._status()

    async def resume(self, session_id: str) -> AdapterStatus:
        async with self._operation_lock:
            generation = self._generation + 1
            await self._spawn(generation)
            self._session_id = session_id
            self._thread_generations[session_id] = generation
            try:
                result = await self._rpc(
                    "thread/resume",
                    {
                        "threadId": session_id,
                        "cwd": self.worktree,
                        "model": self.model,
                        "approvalPolicy": "never",
                        "sandbox": "danger-full-access",
                        "excludeTurns": True,
                    },
                    generation=generation,
                )
                self._remember_thread(self._thread_from_result(result), generation)
                if self._resume_state is LifecycleState.WORKING:
                    status_dir = Path(
                        self.env.get(
                            "WIKI_AGENT_STATUS_DIR",
                            "/tmp/agent-status",
                        )
                    )
                    await self._start_turn(
                        f"You are worker for ticket {self.agent_id}. Your provider session was "
                        f"interrupted. Re-read {status_dir / f'{self.agent_id}.json'} and resume "
                        "from your current step."
                    )
                await self._refresh_identity(required=True)
            except BaseException:
                await asyncio.shield(terminate_process_group(self._process))
                raise
            return self._status()

    async def send_now(self, message: str) -> AdapterStatus:
        async with self._operation_lock:
            if not self._process_is_alive() or not self._session_id:
                raise ProviderProcessError("Codex provider is not attached")
            if self._turn_start_pending:
                raise ProviderBusy("Codex turn/start is awaiting provider acceptance")
            if self._state is LifecycleState.WORKING and self._active_turn_id:
                try:
                    await self._rpc(
                        "turn/steer",
                        {
                            "threadId": self._session_id,
                            "expectedTurnId": self._active_turn_id,
                            "input": self._text_input(message),
                        },
                    )
                    return self._status()
                except ProviderProtocolError:
                    await self._refresh_status()
                    if self._state is not LifecycleState.IDLE:
                        raise
            await self._start_turn(message)
            return self._status()

    async def send_on_idle(self, message: str) -> AdapterStatus:
        await self.status()
        if self._state is not LifecycleState.IDLE:
            raise ProviderBusy(f"Codex run is {self._state.value}, not idle")
        return await self.send_now(message)

    async def interrupt(self) -> AdapterStatus:
        async with self._operation_lock:
            if not self._session_id or not self._active_turn_id:
                return self._status()
            await self._rpc(
                "turn/interrupt",
                {"threadId": self._session_id, "turnId": self._active_turn_id},
            )
            if self._state not in {
                LifecycleState.BLOCKED,
                LifecycleState.DEAD,
                LifecycleState.COMPLETED,
            }:
                self._state = LifecycleState.INTERRUPTED
                self._active_turn_id = None
            return self._status()

    async def _shutdown(self, final_state: LifecycleState) -> AdapterStatus:
        await self._cancel_turn_tasks()
        self._state = final_state
        self._active_turn_id = None
        self._server_request_ids.clear()
        await terminate_process_group(self._process)
        self._provider_pid = None
        if self._stderr_task is not None:
            await asyncio.gather(self._stderr_task, return_exceptions=True)
        if self._reader_task is not None:
            await asyncio.gather(self._reader_task, return_exceptions=True)
        return self._status()

    async def stop(self) -> AdapterStatus:
        async with self._operation_lock:
            if self._active_turn_id and self._process_is_alive():
                try:
                    await self._rpc(
                        "turn/interrupt",
                        {"threadId": self._session_id, "turnId": self._active_turn_id},
                    )
                except (ProviderProcessError, ProviderProtocolError):
                    pass
            return await self._shutdown(LifecycleState.DEAD)

    async def replace(
        self,
        new_prompt: str,
        model: str | None = None,
        effort: str | None = None,
    ) -> AdapterStatus:
        async with self._operation_lock:
            self.model = model or self.model
            self.effort = effort or self.effort
            if not self._process_is_alive() or not self._session_id:
                # Supervisor replacement quiesces this adapter before
                # publishing the new current run. Restart the same transport
                # only after that publication, so no provider can write
                # status under the old run.
                self._session_id = None
                self._active_turn_id = None
                self._state = LifecycleState.STARTING
                self._restart_for_runtime_change = False
                generation = self._generation + 1
                await self._spawn(generation)
                try:
                    await self._start_thread(new_prompt, generation)
                except BaseException:
                    await asyncio.shield(terminate_process_group(self._process))
                    raise
                return self._status()
            if self._active_turn_id:
                try:
                    await self._rpc(
                        "turn/interrupt",
                        {"threadId": self._session_id, "turnId": self._active_turn_id},
                    )
                except ProviderProtocolError:
                    pass
            await self._cancel_turn_tasks()
            self._server_request_ids.clear()
            await self._rpc("thread/archive", {"threadId": self._session_id})
            generation = self._generation + 1
            if self._restart_for_runtime_change:
                self._suppress_stream_end.add(self._generation)
                await self._shutdown(LifecycleState.DEAD)
                await self._spawn(generation)
                self._restart_for_runtime_change = False
            else:
                self._generation = generation
            self._session_id = None
            self._active_turn_id = None
            self._state = LifecycleState.STARTING
            await self._start_thread(new_prompt, generation)
            return self._status()

    async def _refresh_status(self) -> AdapterStatus:
        if not self._process_is_alive():
            if self._state is not LifecycleState.COMPLETED:
                self._state = LifecycleState.DEAD
                self._provider_pid = None
            return self._status()
        if self._session_id:
            try:
                result = await self._rpc(
                    "thread/read",
                    {"threadId": self._session_id, "includeTurns": False},
                )
                self._remember_thread(
                    self._thread_from_result(result), self._generation
                )
            except ProviderProtocolError as exc:
                self._detail = str(exc)
        await self._refresh_identity()
        return self._status()

    async def status(self) -> AdapterStatus:
        async with self._operation_lock:
            return await self._refresh_status()

    def snapshot(self) -> AdapterStatus:
        return self._status()

    def events(self) -> AsyncIterator[ProviderEvent]:
        return self._event_stream()

    async def drain_events(self) -> list[ProviderEvent]:
        events: list[ProviderEvent] = []
        while True:
            try:
                event = self._events.get_nowait()
            except asyncio.QueueEmpty:
                return events
            if isinstance(event, _StreamEnd):
                self._suppress_stream_end.discard(event.generation)
                continue
            events.append(event)

    async def _event_stream(self) -> AsyncIterator[ProviderEvent]:
        while True:
            event = await self._events.get()
            if isinstance(event, _StreamEnd):
                if (
                    event.generation in self._suppress_stream_end
                    or event.generation < self._generation
                ):
                    self._suppress_stream_end.discard(event.generation)
                    continue
                return
            yield event

    async def archive(self) -> AdapterStatus:
        async with self._operation_lock:
            if self._process_is_alive() and self._session_id:
                if self._active_turn_id:
                    await self._rpc(
                        "turn/interrupt",
                        {"threadId": self._session_id, "turnId": self._active_turn_id},
                    )
                await self._cancel_turn_tasks()
                self._server_request_ids.clear()
                await self._rpc("thread/archive", {"threadId": self._session_id})
            return await self._shutdown(LifecycleState.COMPLETED)

    async def respond(
        self,
        request_id: str | int,
        response: dict[str, Any],
    ) -> AdapterStatus:
        key = _request_key(request_id)
        pending = self._server_request_ids.pop(key, None)
        if pending is None or pending.generation != self._generation:
            raise ProviderProtocolError(
                f"unknown, resolved, or stale Codex server request id: {request_id!r}"
            )
        self._server_request_revision += 1
        try:
            await self._send_json(
                {"id": pending.raw_id, "result": response},
                generation=pending.generation,
            )
        except Exception:
            self._server_request_ids[key] = pending
            self._server_request_revision += 1
            raise
        if (
            pending.method in _APPROVAL_METHODS
            and self._state is LifecycleState.WAITING_APPROVAL
            and not self._has_pending_approval(pending.generation)
        ):
            self._state = LifecycleState.WORKING
        return self._status()

    async def close(self) -> None:
        # Preserve the durable working/idle state in RunStore. The replacement
        # daemon will exact-ID resume after this owned process group exits.
        await self._cancel_turn_tasks()
        await terminate_process_group(self._process)
        if self._stderr_task is not None:
            await asyncio.gather(self._stderr_task, return_exceptions=True)
        if self._reader_task is not None:
            await asyncio.gather(self._reader_task, return_exceptions=True)
