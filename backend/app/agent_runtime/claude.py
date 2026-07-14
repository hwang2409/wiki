from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

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


# Claude Code accepts aliases or full API IDs, not versioned catalog shortnames.
_CLAUDE_CLI_MODEL_OVERRIDES = {
    "opus-4.7": "claude-opus-4-7",
    "sonnet-4.6": "claude-sonnet-4-6",
    "haiku-4.5": "claude-haiku-4-5",
}


@dataclass
class _PendingControl:
    future: asyncio.Future[dict[str, Any]]
    generation: int
    subtype: str


@dataclass(frozen=True)
class _ServerControl:
    raw_id: str
    generation: int
    subtype: str
    tool_use_id: str | None = None
    input_payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class _StreamEnd:
    generation: int


def _message_tool_result_ids(value: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    message = value.get("message")
    if not isinstance(message, dict):
        return ids
    for block in message.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        tool_use_id = block.get("tool_use_id")
        if isinstance(tool_use_id, str) and tool_use_id:
            ids.add(tool_use_id)
    return ids


def _ask_user_question_answer(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if not isinstance(value, list) or not value:
        return None
    selected: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return None
        selected.append(item.strip())
    # Claude Code's AskUserQuestion input contract stores multi-select answers
    # as comma-separated strings in updatedInput.answers.
    return ", ".join(selected)


def _ask_user_question_answers(response: dict[str, Any]) -> dict[str, str] | None:
    answers = response.get("answers")
    if isinstance(answers, dict) and answers:
        normalized: dict[str, str] = {}
        for prompt, answer in answers.items():
            if not isinstance(prompt, str) or not prompt.strip():
                return None
            normalized_answer = _ask_user_question_answer(answer)
            if normalized_answer is None:
                return None
            normalized[prompt.strip()] = normalized_answer
        return normalized or None
    if not isinstance(answers, list) or not answers:
        return None
    normalized = {}
    for item in answers:
        if not isinstance(item, dict):
            return None
        prompt = item.get("prompt") or item.get("question")
        answer = item.get("answer")
        if not isinstance(prompt, str) or not prompt.strip():
            return None
        normalized_answer = _ask_user_question_answer(answer)
        if normalized_answer is None:
            return None
        normalized[prompt.strip()] = normalized_answer
    return normalized or None


def _ask_user_question_allow_response(
    input_payload: dict[str, Any] | None,
    answers: dict[str, str],
) -> dict[str, Any]:
    updated_input = dict(input_payload or {})
    updated_input["answers"] = dict(answers)
    return {
        "behavior": "allow",
        "updatedInput": updated_input,
    }


class ClaudeStreamAdapter(ProviderAdapter):
    """Persistent bidirectional Claude stream-json subprocess adapter."""

    provider = ProviderKind.CLAUDE

    def __init__(
        self,
        record: RunRecord,
        *,
        command: Sequence[str] = ("claude",),
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
        self.command = command_tuple(command)
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
        self._pending: dict[str, _PendingControl] = {}
        self._server_request_ids: dict[str, _ServerControl] = {}
        self._pending_question_ids: set[str] = set()
        self._deferred_question_answers: dict[str, dict[str, Any]] = {}
        self._session_generations: dict[str, int] = {}
        self._suppress_stream_end: set[int] = set()
        self._request_id = 0
        self._generation = record.provider_generation
        self._state = record.state
        self._session_id = record.provider_session_id
        self._provider_pid: int | None = None
        self._transcript_path = record.transcript_path
        self._detail: str | None = None
        self._request: StartRequest | None = None
        self._write_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()

    def _configure_runtime(self, record: RunRecord) -> None:
        self.env["WIKI_RUN_ID"] = record.run_id
        self.env["WIKI_AGENT_ID"] = record.agent_id
        self.env["WIKI_AGENT_ROLE"] = record.role
        if record.backend_base_url:
            self.env["WIKI_BACKEND_URL"] = record.backend_base_url
        server_command = artifact_server_command()
        server_env = artifact_server_environment(self.env, record.run_id)
        self.artifact_mcp_config = json.dumps(
            {
                "mcpServers": {
                    "wiki-artifacts": {
                        "command": server_command[0],
                        "args": list(server_command[1:]),
                        "env": server_env,
                    }
                }
            },
            separators=(",", ":"),
        )
        self.run_id = record.run_id
        self.agent_id = record.agent_id

    def prepare_replacement(self, record: RunRecord) -> None:
        self._configure_runtime(record)

    def _status(self) -> AdapterStatus:
        return AdapterStatus(
            state=self._state,
            session_id=self._session_id,
            pid=self._provider_pid,
            generation=self._generation,
            transcript_path=self._transcript_path,
            detail=self._detail,
        )

    def _process_is_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def _command_for(self, session_id: str, *, resume: bool) -> tuple[str, ...]:
        args = [
            *self.command,
            "-p",
            "--output-format",
            "stream-json",
            "--input-format",
            "stream-json",
            "--verbose",
            "--replay-user-messages",
            "--permission-prompt-tool",
            "stdio",
            "--include-partial-messages",
            "--include-hook-events",
            "--mcp-config",
            self.artifact_mcp_config,
            # Parity with the codex adapter's approvalPolicy "never" +
            # danger-full-access: unattended workers, same trust model as the
            # legacy tmux flow's --dangerously-skip-permissions. The stdio
            # permission-prompt-tool stays wired so anything that still asks
            # (e.g. a policy-forced prompt) surfaces in the app instead of
            # killing the run.
            "--permission-mode",
            "bypassPermissions",
            "--model",
            _CLAUDE_CLI_MODEL_OVERRIDES.get(self.model, self.model),
        ]
        if self.effort:
            args.extend(("--effort", self.effort))
        args.extend(("--resume" if resume else "--session-id", session_id))
        return tuple(args)

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

    async def _spawn(self, session_id: str, *, resume: bool, generation: int) -> None:
        if self._process_is_alive():
            raise ProviderProcessError("Claude stream process is already running")
        try:
            process = await asyncio.create_subprocess_exec(
                *self._command_for(session_id, resume=resume),
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
                f"could not start Claude stream process: {exc}"
            ) from exc
        self._process = process
        self._generation = generation
        self._session_id = session_id
        self._session_generations[session_id] = generation
        self._provider_pid = process.pid
        self._state = LifecycleState.STARTING
        self._detail = None
        self._reader_task = asyncio.create_task(
            self._reader_loop(process, generation),
            name=f"claude-reader-{self.run_id}-{generation}",
        )
        self._stderr_task = asyncio.create_task(
            self._stderr_loop(process, generation),
            name=f"claude-stderr-{self.run_id}-{generation}",
        )
        try:
            await self._control("initialize", {}, generation=generation)
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

    async def _send_json(self, message: dict[str, Any], *, generation: int) -> None:
        process = self._process
        if process is None or process.returncode is not None or process.stdin is None:
            raise ProviderProcessError("Claude stream stdin is unavailable")
        await self._emit(message, direction="stdin", generation=generation)
        encoded = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        async with self._write_lock:
            process.stdin.write(encoded)
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise ProviderProcessError(
                    "Claude stream process closed stdin"
                ) from exc

    async def _control(
        self,
        subtype: str,
        request: dict[str, Any],
        *,
        generation: int | None = None,
    ) -> dict[str, Any]:
        event_generation = generation or self._generation
        self._request_id += 1
        request_id = f"wiki-{self._request_id}"
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = _PendingControl(future, event_generation, subtype)
        try:
            await self._send_json(
                {
                    "type": "control_request",
                    "request_id": request_id,
                    "request": {"subtype": subtype, **request},
                },
                generation=event_generation,
            )
            return await asyncio.wait_for(future, timeout=self.request_timeout)
        except TimeoutError as exc:
            raise ProviderProtocolError(
                f"Claude control request timed out: {subtype}"
            ) from exc
        finally:
            self._pending.pop(request_id, None)

    async def _send_user(self, text: str) -> None:
        await self._send_user_message([{"type": "text", "text": text}])
        self._state = LifecycleState.WORKING
        self._detail = None

    async def _send_user_message(self, content: list[dict[str, Any]]) -> None:
        await self._send_json(
            {
                "type": "user",
                "session_id": "",
                "message": {
                    "role": "user",
                    "content": content,
                },
                "parent_tool_use_id": None,
            },
            generation=self._generation,
        )

    def _message_generation(self, value: dict[str, Any], fallback: int) -> int:
        session_id = value.get("session_id")
        if isinstance(session_id, str) and session_id in self._session_generations:
            return self._session_generations[session_id]
        if value.get("type") == "control_response":
            response = value.get("response") or {}
            request_id = (
                response.get("request_id") if isinstance(response, dict) else None
            )
            pending = self._pending.get(str(request_id))
            if pending is not None:
                return pending.generation
        return fallback

    def _has_pending_approval(self, generation: int) -> bool:
        return any(
            request.generation == generation and request.subtype == "can_use_tool"
            for request in self._server_request_ids.values()
        )

    def _drop_pending_questions(self, tool_use_ids: set[str]) -> None:
        if not tool_use_ids:
            return
        self._pending_question_ids.difference_update(tool_use_ids)
        for tool_use_id in tool_use_ids:
            self._deferred_question_answers.pop(tool_use_id, None)

    def _drop_pending_question(self, tool_use_id: str | None) -> None:
        if not isinstance(tool_use_id, str) or not tool_use_id:
            return
        self._pending_question_ids.discard(tool_use_id)
        self._deferred_question_answers.pop(tool_use_id, None)

    def _matching_question_control(
        self, tool_use_id: str
    ) -> tuple[str, _ServerControl] | None:
        for control_id, control in self._server_request_ids.items():
            if (
                control.tool_use_id == tool_use_id
                and control.subtype == "can_use_tool"
                and control.generation == self._generation
            ):
                return control_id, control
        return None

    async def _send_question_answer(
        self,
        control_id: str,
        control: _ServerControl,
        response: dict[str, Any],
    ) -> None:
        answers = _ask_user_question_answers(response)
        if answers is None:
            self._server_request_ids[control_id] = control
            raise ProviderProtocolError(
                "Claude AskUserQuestion responses require a non-empty answers map"
            )
        try:
            await self._send_json(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": control.raw_id,
                        "response": _ask_user_question_allow_response(
                            control.input_payload,
                            answers,
                        ),
                    },
                },
                generation=self._generation,
            )
            await asyncio.sleep(0)
        except Exception:
            self._server_request_ids[control_id] = control
            raise
        self._drop_pending_question(control.tool_use_id)
        self._state = LifecycleState.WORKING
        self._detail = None

    async def _flush_deferred_question_answer(
        self,
        control_id: str,
        control: _ServerControl,
    ) -> None:
        tool_use_id = control.tool_use_id
        if not isinstance(tool_use_id, str) or not tool_use_id:
            return
        response = self._deferred_question_answers.get(tool_use_id)
        if response is None:
            return
        pending = self._server_request_ids.pop(control_id, None)
        if pending is None:
            return
        await self._send_question_answer(control_id, pending, response)

    def _apply_message_state(
        self, value: dict[str, Any], generation: int
    ) -> tuple[str, _ServerControl] | None:
        event_type = value.get("type")
        subtype = value.get("subtype")
        session_id = value.get("session_id")
        deferred_question_control: tuple[str, _ServerControl] | None = None
        if isinstance(session_id, str) and session_id:
            self._session_id = session_id
            self._session_generations[session_id] = self._generation
        self._drop_pending_questions(_message_tool_result_ids(value))
        if event_type == "stream_event":
            event = value.get("event")
            if isinstance(event, dict) and event.get("type") == "content_block_start":
                block = event.get("content_block")
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") == "AskUserQuestion"
                ):
                    tool_use_id = block.get("id") or block.get("tool_use_id")
                    if isinstance(tool_use_id, str) and tool_use_id:
                        self._pending_question_ids.add(tool_use_id)
        if event_type == "control_request":
            request_id = value.get("request_id")
            request = value.get("request") or {}
            if isinstance(request_id, str):
                request_subtype = (
                    str(request.get("subtype")) if isinstance(request, dict) else ""
                )
                tool_use_id = (
                    request.get("tool_use_id") if isinstance(request, dict) else None
                )
                input_payload = request.get("input") if isinstance(request, dict) else None
                self._server_request_ids[request_id] = _ServerControl(
                    request_id,
                    generation,
                    request_subtype,
                    tool_use_id if isinstance(tool_use_id, str) else None,
                    dict(input_payload) if isinstance(input_payload, dict) else None,
                )
                deferred_question_control = (
                    request_id,
                    self._server_request_ids[request_id],
                )
            if isinstance(request, dict) and request.get("subtype") == "can_use_tool":
                self._state = LifecycleState.WAITING_APPROVAL
        elif event_type == "control_cancel_request":
            request_id = value.get("request_id")
            cancelled: _ServerControl | None = None
            if isinstance(request_id, str):
                cancelled = self._server_request_ids.pop(request_id, None)
            if (
                cancelled is not None
                and cancelled.subtype == "can_use_tool"
                and self._state is LifecycleState.WAITING_APPROVAL
                and not self._has_pending_approval(generation)
            ):
                self._state = LifecycleState.WORKING
        elif event_type in {
            "assistant",
            "stream_event",
        } and not self._has_pending_approval(generation):
            self._state = LifecycleState.WORKING
        elif event_type == "system" and subtype == "session_state_changed":
            state = value.get("state") or value.get("session_state")
            mapped = {
                "running": LifecycleState.WORKING,
                "idle": LifecycleState.IDLE,
                "requires_action": LifecycleState.WAITING_APPROVAL,
            }.get(state)
            if mapped is not None:
                if not (
                    self._has_pending_approval(generation)
                    and mapped in {LifecycleState.WORKING, LifecycleState.IDLE}
                ):
                    self._state = mapped
        elif event_type == "system" and subtype == "status":
            mapped = {
                "requesting": LifecycleState.WORKING,
                "running": LifecycleState.WORKING,
                "idle": LifecycleState.IDLE,
            }.get(value.get("status"))
            if mapped is not None:
                if not (
                    self._has_pending_approval(generation)
                    and mapped in {LifecycleState.WORKING, LifecycleState.IDLE}
                ):
                    self._state = mapped
        elif event_type == "result":
            self._server_request_ids.clear()
            if subtype in {"interrupted", "interrupt"}:
                self._state = LifecycleState.INTERRUPTED
            elif value.get("is_error"):
                self._state = LifecycleState.BLOCKED
                self._detail = str(value.get("result") or "Claude provider error")
            else:
                self._state = LifecycleState.IDLE
        return deferred_question_control

    async def _reader_loop(
        self,
        process: asyncio.subprocess.Process,
        generation: int,
    ) -> None:
        assert process.stdout is not None
        reader_error: Exception | None = None
        try:
            while line := await process.stdout.readline():
                try:
                    value = json.loads(line)
                except ValueError:
                    value = {
                        "type": "provider_protocol_error",
                        "line": line.decode("utf-8", errors="replace").rstrip("\n"),
                    }
                if not isinstance(value, dict):
                    value = {"type": "provider_protocol_error", "value": value}
                event_generation = self._message_generation(value, generation)
                deferred_question_control = self._apply_message_state(
                    value, event_generation
                )
                if deferred_question_control is not None:
                    await self._flush_deferred_question_answer(
                        *deferred_question_control
                    )
                await self._emit(value, direction="stdout", generation=event_generation)
                if value.get("type") != "control_response":
                    continue
                response = value.get("response")
                if not isinstance(response, dict):
                    continue
                request_id = response.get("request_id")
                pending = self._pending.get(str(request_id))
                if pending is None or pending.future.done():
                    continue
                if response.get("subtype") == "success":
                    result = response.get("response")
                    pending.future.set_result(
                        result if isinstance(result, dict) else {}
                    )
                else:
                    pending.future.set_exception(
                        ProviderProtocolError(
                            f"Claude {pending.subtype} failed: "
                            f"{json.dumps(response, sort_keys=True)}"
                        )
                    )
        except asyncio.CancelledError:
            await terminate_process_group(process)
            raise
        except Exception as exc:
            reader_error = exc
            await self._emit(
                {
                    "type": "provider_protocol_error",
                    "error": f"{type(exc).__name__}: {exc}",
                },
                direction="process",
                generation=generation,
            )
            await terminate_process_group(process)
        finally:
            # EOF is also a transport failure if the child stays alive. Never
            # await a provider that has closed its only control/output stream.
            if process.returncode is None:
                await terminate_process_group(process)
            returncode = await process.wait()
            exit_payload: dict[str, Any] = {
                "type": "provider_process_exit",
                "returncode": returncode,
            }
            if reader_error is not None:
                exit_payload["reader_error"] = (
                    f"{type(reader_error).__name__}: {reader_error}"
                )
            await self._emit(
                exit_payload,
                direction="process",
                generation=generation,
            )
            detail = f"Claude process exited with status {returncode}"
            if reader_error is not None:
                detail += f" after reader failure: {reader_error}"
            error = ProviderProcessError(detail)
            for pending in self._pending.values():
                if pending.generation == generation and not pending.future.done():
                    pending.future.set_exception(error)
            await self._events.put(_StreamEnd(generation))

    async def _stderr_loop(
        self,
        process: asyncio.subprocess.Process,
        generation: int,
    ) -> None:
        assert process.stderr is not None
        try:
            while line := await process.stderr.readline():
                await self._emit(
                    {
                        "type": "provider_stderr",
                        "text": line.decode("utf-8", errors="replace").rstrip("\n"),
                    },
                    direction="stderr",
                    generation=generation,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit(
                {
                    "type": "provider_protocol_error",
                    "stream": "stderr",
                    "error": f"{type(exc).__name__}: {exc}",
                },
                direction="process",
                generation=generation,
            )
            await terminate_process_group(process)

    async def _refresh_transcript(self, *, wait_for_file: bool = False) -> None:
        process_pid = (
            self._process.pid if self._process_is_alive() and self._process else None
        )
        identity = await self.identity_resolver(
            process_pid,
            self.provider,
            self._session_id,
            reported_path=self._transcript_path,
        )
        if identity is not None:
            self._provider_pid = identity.pid
            self._transcript_path = identity.transcript_path
            return
        self._provider_pid = process_pid
        if not self._session_id:
            return
        config_dir = Path(
            self.env.get("CLAUDE_CONFIG_DIR")
            or Path(self.env.get("HOME") or Path.home()) / ".claude"
        )
        attempts = 20 if wait_for_file else 1
        for attempt in range(attempts):
            candidates = list(config_dir.glob(f"projects/**/{self._session_id}.jsonl"))
            if candidates:
                self._transcript_path = str(
                    max(candidates, key=lambda path: path.stat().st_mtime)
                )
                return
            if attempt + 1 < attempts:
                await asyncio.sleep(0.1)

    async def start(self, request: StartRequest) -> AdapterStatus:
        async with self._operation_lock:
            self._request = request
            self.worktree = request.worktree
            self.model = request.model
            self.effort = request.effort
            generation = self._generation + 1
            session_id = str(uuid4())
            await self._spawn(session_id, resume=False, generation=generation)
            try:
                await self._send_user(request.prompt)
                await self._refresh_transcript(wait_for_file=True)
            except Exception:
                await terminate_process_group(self._process)
                raise
            return self._status()

    async def resume(self, session_id: str) -> AdapterStatus:
        async with self._operation_lock:
            generation = self._generation + 1
            try:
                await self._spawn(session_id, resume=True, generation=generation)
                self._state = LifecycleState.IDLE
                if self._resume_state is LifecycleState.WORKING:
                    status_dir = Path(
                        self.env.get("WIKI_AGENT_STATUS_DIR") or "/tmp/agent-status"
                    )
                    await self._send_user(
                        f"You are worker for ticket {self.agent_id}. Your provider session was "
                        f"interrupted. Re-read {status_dir / f'{self.agent_id}.json'} and resume "
                        "from your current step."
                    )
                await self._refresh_transcript()
            except Exception:
                await terminate_process_group(self._process)
                raise
            return self._status()

    async def send_now(self, message: str) -> AdapterStatus:
        async with self._operation_lock:
            if not self._process_is_alive():
                raise ProviderProcessError("Claude provider is not attached")
            await self._send_user(message)
            return self._status()

    async def send_on_idle(self, message: str) -> AdapterStatus:
        if self._state is not LifecycleState.IDLE:
            raise ProviderBusy(f"Claude run is {self._state.value}, not idle")
        return await self.send_now(message)

    async def interrupt(self) -> AdapterStatus:
        async with self._operation_lock:
            if not self._process_is_alive():
                raise ProviderProcessError("Claude provider is not attached")
            await self._control("interrupt", {})
            self._state = LifecycleState.INTERRUPTED
            return self._status()

    async def _wait_for_exit(self, process: asyncio.subprocess.Process) -> None:
        try:
            await asyncio.wait_for(process.wait(), timeout=3.0)
        except TimeoutError:
            await terminate_process_group(process)

    async def _end_session(
        self,
        reason: str,
        *,
        suppress_stream_end: bool,
    ) -> None:
        process = self._process
        if process is None:
            self._server_request_ids.clear()
            self._pending_question_ids.clear()
            self._deferred_question_answers.clear()
            return
        generation = self._generation
        if suppress_stream_end:
            self._suppress_stream_end.add(generation)
        if process.returncode is None:
            try:
                await self._control("end_session", {"reason": reason})
            except (ProviderProcessError, ProviderProtocolError):
                pass
            if process.stdin is not None:
                process.stdin.close()
            await self._wait_for_exit(process)
        if self._stderr_task is not None:
            await asyncio.gather(self._stderr_task, return_exceptions=True)
        if self._reader_task is not None:
            await asyncio.gather(self._reader_task, return_exceptions=True)
        self._server_request_ids.clear()
        self._pending_question_ids.clear()
        self._deferred_question_answers.clear()

    async def stop(self) -> AdapterStatus:
        async with self._operation_lock:
            self._state = LifecycleState.DEAD
            await self._end_session("wiki-stop", suppress_stream_end=False)
            self._provider_pid = None
            return self._status()

    async def replace(
        self,
        new_prompt: str,
        model: str | None = None,
        effort: str | None = None,
    ) -> AdapterStatus:
        async with self._operation_lock:
            if not self._process_is_alive():
                raise ProviderProcessError(
                    "Claude replacement requires an attached provider"
                )
            await self._end_session("wiki-replace", suppress_stream_end=True)
            self.model = model or self.model
            self.effort = effort
            generation = self._generation + 1
            session_id = str(uuid4())
            try:
                await self._spawn(session_id, resume=False, generation=generation)
                await self._send_user(new_prompt)
                await self._refresh_transcript(wait_for_file=True)
            except Exception:
                await terminate_process_group(self._process)
                raise
            return self._status()

    async def status(self) -> AdapterStatus:
        async with self._operation_lock:
            if not self._process_is_alive():
                if self._state is not LifecycleState.COMPLETED:
                    self._state = LifecycleState.DEAD
                self._provider_pid = None
                return self._status()
            await self._refresh_transcript()
            return self._status()

    def snapshot(self) -> AdapterStatus:
        return self._status()

    def events(self) -> AsyncIterator[ProviderEvent]:
        return self._event_stream()

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
            self._state = LifecycleState.COMPLETED
            await self._end_session("wiki-archive", suppress_stream_end=False)
            self._provider_pid = None
            return self._status()

    async def respond(
        self,
        request_id: str | int,
        response: dict[str, Any],
    ) -> AdapterStatus:
        if not isinstance(request_id, str):
            raise ProviderProtocolError(
                f"unknown, canceled, or stale Claude server request id: {request_id!r}"
            )
        pending = self._server_request_ids.pop(request_id, None)
        if pending is None:
            if request_id not in self._pending_question_ids:
                raise ProviderProtocolError(
                    f"unknown, canceled, or stale Claude server request id: {request_id!r}"
                )
            if _ask_user_question_answers(response) is None:
                raise ProviderProtocolError(
                    "Claude AskUserQuestion responses require a non-empty answers map"
                )
            matching_question_control = self._matching_question_control(request_id)
            if matching_question_control is None:
                self._deferred_question_answers[request_id] = dict(response)
                return self._status()
            matching_control_id, _matching_control = matching_question_control
            matching_control = self._server_request_ids.pop(matching_control_id, None)
            if matching_control is None:
                self._deferred_question_answers[request_id] = dict(response)
                return self._status()
            await self._send_question_answer(
                matching_control_id,
                matching_control,
                response,
            )
            return self._status()
        if pending.generation != self._generation:
            self._server_request_ids[request_id] = pending
            raise ProviderProtocolError(
                f"unknown, canceled, or stale Claude server request id: {request_id!r}"
            )
        try:
            await self._send_json(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": pending.raw_id,
                        "response": response,
                    },
                },
                generation=self._generation,
            )
            await asyncio.sleep(0)
        except Exception:
            self._server_request_ids[request_id] = pending
            raise
        if (
            pending.subtype == "can_use_tool"
            and self._state is LifecycleState.WAITING_APPROVAL
            and not self._has_pending_approval(pending.generation)
        ):
            self._state = LifecycleState.WORKING
        return self._status()

    async def close(self) -> None:
        await terminate_process_group(self._process)
        if self._stderr_task is not None:
            await asyncio.gather(self._stderr_task, return_exceptions=True)
        if self._reader_task is not None:
            await asyncio.gather(self._reader_task, return_exceptions=True)
        self._server_request_ids.clear()
        self._pending_question_ids.clear()
        self._deferred_question_answers.clear()
