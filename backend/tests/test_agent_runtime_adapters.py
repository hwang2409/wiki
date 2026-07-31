from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from backend.app.agent_models import MODEL_OPTIONS
from backend.app.agent_runtime.claude import ClaudeStreamAdapter
from backend.app.agent_runtime.codex import CodexAppServerAdapter
from backend.app.agent_runtime.process import (
    ProviderProcessIdentity,
    select_transcript_identity,
    terminate_process_group,
)
from backend.app.agent_runtime.provider import (
    ProviderAdapter,
    ProviderEvent,
    ProviderProcessError,
    ProviderProtocolError,
    StartRequest,
)
from backend.app.agent_runtime.types import LifecycleState, ProviderKind, RunRecord
from backend.app.wiki_artifacts import artifact_server_command


FIXTURES = Path(__file__).parent / "fixtures" / "agent_runtime"


def _record(root: Path, provider: ProviderKind, *, state: LifecycleState) -> RunRecord:
    worktree = root / "worktree"
    worktree.mkdir(exist_ok=True)
    record = RunRecord.new(
        agent_id="WIKI-42",
        provider=provider,
        role="implement",
        model="fixture-model",
        effort="high" if provider is ProviderKind.CODEX else None,
        worktree=str(worktree),
        prompt="fixture prompt",
    )
    record.state = state
    return record


def _start_request(record: RunRecord, prompt: str = "initial prompt") -> StartRequest:
    return StartRequest(
        prompt=prompt,
        model=record.model,
        effort=record.effort,
        worktree=record.worktree,
        run_id=record.run_id,
        agent_id=record.agent_id,
    )


class OrchestratorMcpConfigTests(unittest.TestCase):
    def test_both_providers_attach_run_isolated_mcp_to_orchestrators(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "worktree"
            worktree.mkdir()
            env = {
                **os.environ,
                "WIKI_AGENT_RUNTIME_DIR": str(root / "runtime"),
            }
            for provider in (ProviderKind.CODEX, ProviderKind.CLAUDE):
                with self.subTest(provider=provider.value):
                    record = RunRecord.new(
                        agent_id="wiki",
                        provider=provider,
                        role="orchestrator",
                        model="fixture-model",
                        effort="high" if provider is ProviderKind.CODEX else None,
                        worktree=str(worktree),
                        prompt="coordinate",
                        backend_base_url="http://127.0.0.1:43113",
                    )
                    if provider is ProviderKind.CODEX:
                        adapter = CodexAppServerAdapter(record, env=env)
                        server_env = next(
                            argument.removeprefix("mcp_servers.wiki_artifacts.env=")
                            for argument in adapter.command
                            if argument.startswith("mcp_servers.wiki_artifacts.env=")
                        )
                        self.assertIn(f'WIKI_RUN_ID = "{record.run_id}"', server_env)
                        self.assertIn('WIKI_AGENT_ROLE = "orchestrator"', server_env)
                        self.assertIn(
                            'WIKI_BACKEND_URL = "http://127.0.0.1:43113"',
                            server_env,
                        )
                    else:
                        adapter = ClaudeStreamAdapter(record, env=env)
                        config = json.loads(adapter.artifact_mcp_config)
                        server_env = config["mcpServers"]["wiki-artifacts"]["env"]
                        self.assertEqual(server_env["WIKI_RUN_ID"], record.run_id)
                        self.assertEqual(server_env["WIKI_AGENT_ROLE"], "orchestrator")
                        self.assertEqual(
                            server_env["WIKI_BACKEND_URL"],
                            "http://127.0.0.1:43113",
                        )


async def _wait_event(
    adapter: ProviderAdapter,
    predicate: Callable[[ProviderEvent], bool],
    *,
    timeout: float = 3.0,
) -> ProviderEvent:
    async def find() -> ProviderEvent:
        async for event in adapter.events():
            if predicate(event):
                return event
        raise AssertionError("provider event stream ended before expected event")

    return await asyncio.wait_for(find(), timeout=timeout)


async def _wait_protocol_row(
    path: Path,
    predicate: Callable[[dict], bool],
    *,
    timeout: float = 3.0,
) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        for row in _protocol_rows(path):
            if predicate(row):
                return row
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("protocol log never contained the expected row")
        await asyncio.sleep(0.01)


def _protocol_rows(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


class ProcessIdentityTests(unittest.TestCase):
    def test_exact_reported_path_then_session_and_never_lexical_guess(self) -> None:
        candidates = [
            (100, "/sessions/rollout-root-session.jsonl"),
            (101, "/sessions/rollout-subagent-a.jsonl"),
            (101, "/sessions/rollout-subagent-b.jsonl"),
        ]
        exact_path = select_transcript_identity(
            candidates,
            [99, 100, 101],
            "wrong-session",
            reported_path="/sessions/rollout-root-session.jsonl",
        )
        self.assertEqual(
            exact_path,
            ProviderProcessIdentity(100, "/sessions/rollout-root-session.jsonl"),
        )
        exact_session = select_transcript_identity(
            candidates,
            [99, 100, 101],
            "subagent-a",
        )
        self.assertEqual(
            exact_session,
            ProviderProcessIdentity(101, "/sessions/rollout-subagent-a.jsonl"),
        )
        self.assertIsNone(
            select_transcript_identity(candidates, [99, 100, 101], "missing")
        )


class ProcessGroupTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_kills_group_after_wrapper_exit_and_term_resistant_child(
        self,
    ) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        ready_path = root / "child-ready"
        child_code = (
            "import pathlib,signal,time;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            f"pathlib.Path({str(ready_path)!r}).touch();"
            "time.sleep(30)"
        )
        wrapper_code = (
            "import pathlib,subprocess,sys,time;"
            f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}],"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
            f"ready=pathlib.Path({str(ready_path)!r});"
            "deadline=time.monotonic()+3;"
            "exec('while not ready.exists() and time.monotonic() < deadline: time.sleep(0.01)');"
            "print(p.pid,flush=True)"
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            wrapper_code,
            stdout=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None
        child_pid = int((await process.stdout.readline()).decode().strip())
        await process.wait()
        os.kill(child_pid, 0)
        await terminate_process_group(process, timeout=0.2)
        with self.assertRaises(ProcessLookupError):
            os.killpg(process.pid, 0)
        self.assertIsNone(
            select_transcript_identity(
                [(101, "/sessions/rollout-unrelated.jsonl")],
                [99, 101],
                "root-session",
            )
        )


class CodexAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log = self.root / "codex-protocol.jsonl"
        self.transcripts = self.root / "codex-sessions"
        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(self.root / "home"),
                "CODEX_HOME": str(self.root / "codex-home"),
                "FAKE_PROTOCOL_LOG": str(self.log),
                "FAKE_CODEX_TRANSCRIPT_DIR": str(self.transcripts),
                "WIKI_AGENT_STATUS_DIR": str(self.root / "status"),
                "WIKI_AGENT_RUNTIME_DIR": str(self.root / "runtime"),
                "TMUX": "must-not-leak",
                "TMUX_PANE": "%9999",
            }
        )
        self.adapters: list[CodexAppServerAdapter] = []

    async def asyncTearDown(self) -> None:
        await asyncio.gather(*(adapter.close() for adapter in self.adapters))
        self.tmp.cleanup()

    async def _identity(
        self,
        wrapper_pid: int | None,
        _provider: ProviderKind,
        _session_id: str | None,
        *,
        reported_path: str | None = None,
    ) -> ProviderProcessIdentity | None:
        if wrapper_pid is None or reported_path is None:
            return None
        return ProviderProcessIdentity(wrapper_pid, reported_path)

    def _adapter(
        self,
        record: RunRecord,
        *,
        approval: bool = False,
        request_timeout: float = 2,
    ) -> CodexAppServerAdapter:
        env = dict(self.env)
        if approval:
            env["FAKE_CODEX_APPROVAL"] = "1"
        adapter = CodexAppServerAdapter(
            record,
            command=(sys.executable, "-u", str(FIXTURES / "fake_codex_app_server.py")),
            env=env,
            request_timeout=request_timeout,
            identity_resolver=self._identity,
        )
        self.adapters.append(adapter)
        return adapter

    async def test_start_steer_interrupt_replace_and_archive(self) -> None:
        record = _record(self.root, ProviderKind.CODEX, state=LifecycleState.STARTING)
        adapter = self._adapter(record)
        started = await adapter.start(_start_request(record))
        self.assertEqual(started.state, LifecycleState.WORKING)
        self.assertEqual(started.session_id, "thread-1")
        self.assertEqual(started.generation, 1)
        process = adapter._process  # noqa: SLF001 - PID identity contract
        assert process is not None
        self.assertEqual(started.pid, process.pid)
        self.assertTrue(str(started.transcript_path).endswith("rollout-thread-1.jsonl"))
        self.assertTrue(
            any(
                str(argument).startswith("mcp_servers.wiki_artifacts.command=")
                for argument in adapter.command
            )
        )
        server_command = artifact_server_command()
        self.assertIn(
            f"mcp_servers.wiki_artifacts.command={json.dumps(server_command[0])}",
            adapter.command,
        )
        self.assertIn(
            f"mcp_servers.wiki_artifacts.args={json.dumps(list(server_command[1:]))}",
            adapter.command,
        )
        server_env = next(
            argument.removeprefix("mcp_servers.wiki_artifacts.env=")
            for argument in adapter.command
            if argument.startswith("mcp_servers.wiki_artifacts.env=")
        )
        self.assertIn(f'WIKI_RUN_ID = "{record.run_id}"', server_env)
        self.assertIn(
            f'WIKI_AGENT_RUNTIME_DIR = "{self.root / "runtime"}"',
            server_env,
        )
        self.assertEqual(adapter.env["WIKI_RUN_ID"], record.run_id)
        await _wait_event(
            adapter,
            lambda event: (
                event.payload.get("method") == "turn/started" and event.generation == 1
            ),
        )
        for _ in range(100):
            if not adapter._turn_start_pending:  # noqa: SLF001
                break
            await asyncio.sleep(0.01)

        steered = await adapter.send_now("steer active turn")
        self.assertEqual(steered.state, LifecycleState.WORKING)
        interrupted = await adapter.interrupt()
        self.assertEqual(interrupted.state, LifecycleState.INTERRUPTED)
        await _wait_event(
            adapter,
            lambda event: event.payload.get("method") == "turn/completed",
        )
        self.assertEqual((await adapter.status()).state, LifecycleState.INTERRUPTED)

        replacement = await adapter.replace("replacement prompt", "fixture-model-2")
        self.assertEqual(replacement.session_id, "thread-2")
        self.assertEqual(replacement.generation, 2)
        old_archived = await _wait_event(
            adapter,
            lambda event: event.payload.get("method") == "thread/archived",
        )
        new_started = await _wait_event(
            adapter,
            lambda event: (
                event.payload.get("method") == "turn/started" and event.generation == 2
            ),
        )
        self.assertEqual(old_archived.generation, 1)
        self.assertEqual(new_started.generation, 2)

        adapter._apply_message_state(  # noqa: SLF001 - generation regression
            {
                "method": "thread/archived",
                "params": {"threadId": "thread-1"},
            },
            1,
        )
        self.assertEqual(adapter.snapshot().state, LifecycleState.WORKING)
        self.assertEqual(adapter.snapshot().session_id, "thread-2")

        archived = await adapter.archive()
        self.assertEqual(archived.state, LifecycleState.COMPLETED)
        self.assertIsNone(archived.pid)
        methods = [row.get("method") for row in _protocol_rows(self.log)]
        self.assertIn("turn/steer", methods)
        self.assertEqual(methods.count("thread/start"), 2)
        first = _protocol_rows(self.log)[0]
        self.assertIsNone(first["tmux"])
        self.assertIsNone(first["tmux_pane"])

    async def test_start_survives_slow_open_handle_verification(self) -> None:
        record = _record(self.root, ProviderKind.CODEX, state=LifecycleState.STARTING)
        adapter = self._adapter(record)
        attempts = 0

        async def delayed_identity(
            wrapper_pid: int | None,
            _provider: ProviderKind,
            _session_id: str | None,
            *,
            reported_path: str | None = None,
        ) -> ProviderProcessIdentity | None:
            nonlocal attempts
            attempts += 1
            if attempts < 4 or wrapper_pid is None or reported_path is None:
                return None
            return ProviderProcessIdentity(wrapper_pid, reported_path)

        adapter.identity_resolver = delayed_identity
        started = await adapter.start(_start_request(record))

        self.assertEqual(started.state, LifecycleState.WORKING)
        self.assertEqual(attempts, 4)

    async def test_replacement_restarts_transport_with_new_run_mcp_identity(self) -> None:
        record = _record(self.root, ProviderKind.CODEX, state=LifecycleState.STARTING)
        adapter = self._adapter(record)
        started = await adapter.start(_start_request(record))
        old_process = adapter._process  # noqa: SLF001 - replacement transport contract
        assert old_process is not None

        replacement = RunRecord.new(
            agent_id=record.agent_id,
            provider=record.provider,
            role=record.role,
            model=record.model,
            effort=record.effort,
            worktree=record.worktree,
            prompt="replacement prompt",
            replaces_run_id=record.run_id,
            backend_base_url="http://127.0.0.1:43114",
        )
        adapter.prepare_replacement(replacement)
        status = await adapter.replace("replacement prompt")

        new_process = adapter._process  # noqa: SLF001
        assert new_process is not None
        self.assertNotEqual(new_process.pid, old_process.pid)
        self.assertEqual(status.generation, started.generation + 1)
        server_env = next(
            argument.removeprefix("mcp_servers.wiki_artifacts.env=")
            for argument in adapter.command
            if argument.startswith("mcp_servers.wiki_artifacts.env=")
        )
        self.assertIn(f'WIKI_RUN_ID = "{replacement.run_id}"', server_env)
        self.assertIn('WIKI_BACKEND_URL = "http://127.0.0.1:43114"', server_env)
        replacement_event = await _wait_event(
            adapter,
            lambda event: (
                event.payload.get("method") == "turn/started"
                and event.generation == status.generation
            ),
        )
        self.assertEqual(replacement_event.generation, status.generation)

    async def test_numeric_approval_response_keeps_original_id_type(self) -> None:
        record = _record(self.root, ProviderKind.CODEX, state=LifecycleState.STARTING)
        adapter = self._adapter(record, approval=True, request_timeout=0.5)
        start_task = asyncio.create_task(adapter.start(_start_request(record)))
        request = await _wait_event(
            adapter,
            lambda event: event.payload.get("method") == "item/tool/requestUserInput",
        )
        started = await asyncio.wait_for(start_task, timeout=2)
        self.assertEqual(started.session_id, "thread-1")
        self.assertEqual(request.payload["id"], 0)
        self.assertEqual(adapter.snapshot().state, LifecycleState.WAITING_APPROVAL)
        await asyncio.sleep(0.12)
        self.assertTrue(adapter._turn_start_pending)  # noqa: SLF001
        with (
            mock.patch.object(
                adapter,
                "_send_json",
                new=mock.AsyncMock(side_effect=ProviderProcessError("fixture write failed")),
            ),
            self.assertRaisesRegex(ProviderProcessError, "fixture write failed"),
        ):
            await adapter.respond(
                0,
                {"answers": {"wiki_surface": {"answers": ["Agents page"]}}},
            )
        self.assertIn(("int", 0), adapter._server_request_ids)  # noqa: SLF001
        await adapter.respond(
            0,
            {"answers": {"wiki_surface": {"answers": ["Agents page"]}}},
        )
        await _wait_event(
            adapter,
            lambda event: event.payload.get("method") == "serverRequest/resolved",
        )
        for _ in range(100):
            if not adapter._turn_start_pending:  # noqa: SLF001
                break
            await asyncio.sleep(0.01)
        self.assertFalse(adapter._turn_start_pending)  # noqa: SLF001
        response = next(
            row
            for row in reversed(_protocol_rows(self.log))
            if row.get("id") == 0 and "result" in row
        )
        self.assertIsInstance(response["id"], int)
        self.assertEqual(
            response["result"],
            {"answers": {"wiki_surface": {"answers": ["Agents page"]}}},
        )
        with self.assertRaises(ProviderProtocolError):
            await adapter.respond(
                0,
                {"answers": {"wiki_surface": {"answers": ["Session page"]}}},
            )

    async def test_working_resume_reuses_exact_id_and_reissues_continuation(
        self,
    ) -> None:
        record = _record(self.root, ProviderKind.CODEX, state=LifecycleState.WORKING)
        record.provider_session_id = "thread-exact"
        record.provider_generation = 4
        adapter = self._adapter(record)
        resumed = await adapter.resume("thread-exact")
        self.assertEqual(resumed.session_id, "thread-exact")
        self.assertEqual(resumed.generation, 5)
        self.assertEqual(resumed.state, LifecycleState.WORKING)
        rows = _protocol_rows(self.log)
        methods = [row.get("method") for row in rows]
        self.assertIn("thread/resume", methods)
        self.assertIn("turn/start", methods)
        continuation = next(row for row in rows if row.get("method") == "turn/start")
        text = continuation["params"]["input"][0]["text"]
        self.assertIn(str(self.root / "status" / "WIKI-42.json"), text)

    async def test_resolved_clock_request_does_not_clear_pending_approval(self) -> None:
        record = _record(self.root, ProviderKind.CODEX, state=LifecycleState.WORKING)
        adapter = self._adapter(record)
        adapter._generation = 1  # noqa: SLF001 - protocol state regression
        adapter._apply_message_state(  # noqa: SLF001 - protocol state regression
            {
                "id": 0,
                "method": "item/tool/requestUserInput",
                "params": {"turnId": "turn-1"},
            },
            1,
        )
        adapter._apply_message_state(  # noqa: SLF001 - id type collision regression
            {"id": "0", "method": "currentTime/read", "params": {}},
            1,
        )
        self.assertEqual(len(adapter._server_request_ids), 2)  # noqa: SLF001
        adapter._apply_message_state(  # noqa: SLF001 - protocol state regression
            {
                "method": "serverRequest/resolved",
                "params": {"requestId": "0"},
            },
            1,
        )
        self.assertEqual(adapter.snapshot().state, LifecycleState.WAITING_APPROVAL)
        adapter._apply_message_state(  # noqa: SLF001 - protocol state regression
            {
                "method": "serverRequest/resolved",
                "params": {"requestId": 0},
            },
            1,
        )
        self.assertEqual(adapter.snapshot().state, LifecycleState.WORKING)

    async def test_background_turn_rejection_transitions_to_blocked(self) -> None:
        record = _record(self.root, ProviderKind.CODEX, state=LifecycleState.STARTING)
        env = dict(self.env)
        env["FAKE_TURN_START_ERROR"] = "1"
        adapter = CodexAppServerAdapter(
            record,
            command=(
                sys.executable,
                "-u",
                str(FIXTURES / "fake_codex_app_server.py"),
            ),
            env=env,
            request_timeout=0.2,
            identity_resolver=self._identity,
        )
        self.adapters.append(adapter)
        started = await adapter.start(_start_request(record))
        self.assertEqual(started.state, LifecycleState.WORKING)
        failure = await _wait_event(
            adapter,
            lambda event: (
                event.payload.get("method") == "error"
                and event.payload.get("params", {}).get("source") == "adapter"
            ),
        )
        self.assertEqual(failure.payload["params"]["operation"], "turn/start")
        self.assertEqual(adapter.snapshot().state, LifecycleState.BLOCKED)

    async def test_broken_output_streams_terminate_provider_instead_of_hanging(
        self,
    ) -> None:
        for flag in ("FAKE_OVERSIZED", "FAKE_OVERSIZED_STDERR", "FAKE_EOF_LIVE"):
            with self.subTest(flag=flag):
                record = _record(
                    self.root,
                    ProviderKind.CODEX,
                    state=LifecycleState.STARTING,
                )
                env = dict(self.env)
                env[flag] = "1"
                adapter = CodexAppServerAdapter(
                    record,
                    command=(
                        sys.executable,
                        "-u",
                        str(FIXTURES / "fake_codex_app_server.py"),
                    ),
                    env=env,
                    request_timeout=2,
                    identity_resolver=self._identity,
                )
                self.adapters.append(adapter)
                with self.assertRaises(ProviderProcessError):
                    await asyncio.wait_for(
                        adapter.start(_start_request(record)), timeout=5
                    )
                process = adapter._process  # noqa: SLF001 - process leak regression
                assert process is not None
                self.assertIsNotNone(process.returncode)


class ClaudeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log = self.root / "claude-protocol.jsonl"
        self.config = self.root / "claude-config"
        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(self.root / "home"),
                "CLAUDE_CONFIG_DIR": str(self.config),
                "FAKE_PROTOCOL_LOG": str(self.log),
                "WIKI_AGENT_RUNTIME_DIR": str(self.root / "runtime"),
                "TMUX": "must-not-leak",
                "TMUX_PANE": "%9999",
            }
        )
        self.adapters: list[ClaudeStreamAdapter] = []

    async def asyncTearDown(self) -> None:
        await asyncio.gather(*(adapter.close() for adapter in self.adapters))
        self.tmp.cleanup()

    async def _no_open_handle(
        self,
        _pid: int | None,
        _provider: ProviderKind,
        _session_id: str | None,
        *,
        reported_path: str | None = None,
    ) -> ProviderProcessIdentity | None:
        return None

    def _adapter(self, record: RunRecord) -> ClaudeStreamAdapter:
        adapter = ClaudeStreamAdapter(
            record,
            command=(sys.executable, "-u", str(FIXTURES / "fake_claude_stream.py")),
            env=self.env,
            request_timeout=2,
            identity_resolver=self._no_open_handle,
        )
        self.adapters.append(adapter)
        return adapter

    def test_catalog_models_use_cli_compatible_model_names(self) -> None:
        expected_cli_models = {
            "claude-fable-5": "claude-fable-5",
            "opus-4.7": "claude-opus-4-7",
            "opus": "opus",
            "sonnet": "sonnet",
            "sonnet-4.6": "claude-sonnet-4-6",
            "haiku": "haiku",
            "haiku-4.5": "claude-haiku-4-5",
        }
        catalog_models = [
            option.id for option in MODEL_OPTIONS if option.provider == "claude"
        ]
        self.assertEqual(catalog_models, list(expected_cli_models))

        for model, expected_cli_model in expected_cli_models.items():
            with self.subTest(model=model):
                record = _record(
                    self.root,
                    ProviderKind.CLAUDE,
                    state=LifecycleState.STARTING,
                )
                record.model = model
                adapter = self._adapter(record)
                command = adapter._command_for(  # noqa: SLF001 - argv contract
                    "00000000-0000-4000-8000-000000000040",
                    resume=False,
                )
                self.assertEqual(
                    command[command.index("--model") + 1],
                    expected_cli_model,
                )

    async def _apply_message(
        self,
        adapter: ClaudeStreamAdapter,
        value: dict[str, object],
        generation: int,
    ) -> None:
        deferred = adapter._apply_message_state(value, generation)  # noqa: SLF001
        if deferred is not None:
            await adapter._flush_deferred_question_answer(*deferred)  # noqa: SLF001

    async def test_persistent_turns_permission_interrupt_and_no_tmux(self) -> None:
        record = _record(self.root, ProviderKind.CLAUDE, state=LifecycleState.STARTING)
        adapter = self._adapter(record)
        started = await adapter.start(_start_request(record))
        self.assertEqual(started.state, LifecycleState.WORKING)
        self.assertEqual(started.generation, 1)
        session_id = started.session_id
        self.assertIsNotNone(session_id)
        await _wait_event(
            adapter,
            lambda event: (
                event.payload.get("type") == "result"
                and event.payload.get("subtype") == "success"
            ),
        )
        idle = await adapter.status()
        self.assertEqual(idle.state, LifecycleState.IDLE)
        self.assertEqual(
            idle.transcript_path,
            str(self.config / "projects" / "isolated-worktree" / f"{session_id}.jsonl"),
        )

        await adapter.send_now("approval required")
        approval = await _wait_event(
            adapter,
            lambda event: event.payload.get("type") == "control_request",
        )
        self.assertEqual(approval.payload["request"]["subtype"], "can_use_tool")
        self.assertEqual(
            (await adapter.status()).state, LifecycleState.WAITING_APPROVAL
        )
        with (
            mock.patch.object(
                adapter,
                "_send_json",
                new=mock.AsyncMock(side_effect=ProviderProcessError("fixture write failed")),
            ),
            self.assertRaisesRegex(ProviderProcessError, "fixture write failed"),
        ):
            await adapter.respond(
                "permission-1",
                {
                    "behavior": "deny",
                    "message": "Denied by test",
                    "interrupt": False,
                    "toolUseID": "toolu_fixture",
                },
            )
        self.assertIn("permission-1", adapter._server_request_ids)  # noqa: SLF001
        await adapter.respond(
            "permission-1",
            {
                "behavior": "deny",
                "message": "Denied by test",
                "interrupt": False,
                "toolUseID": "toolu_fixture",
            },
        )
        with self.assertRaises(ProviderProtocolError):
            await adapter.respond("permission-1", {"behavior": "deny"})
        await _wait_event(
            adapter,
            lambda event: event.payload.get("result") == "permission handled",
        )
        self.assertEqual((await adapter.status()).state, LifecycleState.IDLE)

        generation = adapter.snapshot().generation
        adapter._apply_message_state(  # noqa: SLF001 - canceled approval regression
            {
                "type": "control_request",
                "request_id": "permission-canceled",
                "request": {"subtype": "can_use_tool"},
            },
            generation,
        )
        adapter._apply_message_state(  # noqa: SLF001 - canceled approval regression
            {"type": "control_cancel_request", "request_id": "permission-canceled"},
            generation,
        )
        with self.assertRaises(ProviderProtocolError):
            await adapter.respond("permission-canceled", {"behavior": "allow"})

        interrupted = await adapter.interrupt()
        self.assertEqual(interrupted.state, LifecycleState.INTERRUPTED)
        stopped = await adapter.stop()
        self.assertEqual(stopped.state, LifecycleState.DEAD)

        first = _protocol_rows(self.log)[0]
        argv = first["argv"]
        self.assertIn("--permission-prompt-tool", argv)
        self.assertIn("--mcp-config", argv)
        mcp_config = json.loads(argv[argv.index("--mcp-config") + 1])
        artifact_config = mcp_config["mcpServers"]["wiki-artifacts"]
        server_command = artifact_server_command()
        self.assertEqual(artifact_config["command"], server_command[0])
        self.assertEqual(artifact_config["args"], list(server_command[1:]))
        self.assertEqual(artifact_config["env"]["WIKI_RUN_ID"], record.run_id)
        self.assertEqual(
            artifact_config["env"]["WIKI_AGENT_RUNTIME_DIR"],
            str(self.root / "runtime"),
        )
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("--tmux", argv)
        self.assertIsNone(first["tmux"])
        self.assertIsNone(first["tmux_pane"])

    async def test_resume_exact_id_then_replace_uses_fresh_session_and_generation(
        self,
    ) -> None:
        record = _record(self.root, ProviderKind.CLAUDE, state=LifecycleState.IDLE)
        record.provider_session_id = "00000000-0000-4000-8000-000000000042"
        record.provider_generation = 3
        adapter = self._adapter(record)
        resumed = await adapter.resume(record.provider_session_id)
        self.assertEqual(resumed.session_id, record.provider_session_id)
        self.assertEqual(resumed.generation, 4)
        self.assertEqual(resumed.state, LifecycleState.IDLE)

        replacement_record = RunRecord.new(
            agent_id=record.agent_id,
            provider=record.provider,
            role=record.role,
            model="fixture-model-2",
            worktree=record.worktree,
            prompt="replacement prompt",
            replaces_run_id=record.run_id,
            backend_base_url="http://127.0.0.1:43115",
        )
        adapter.prepare_replacement(replacement_record)
        replacement = await adapter.replace("replacement prompt", "fixture-model-2")
        self.assertNotEqual(replacement.session_id, resumed.session_id)
        self.assertEqual(replacement.generation, 5)
        result = await _wait_event(
            adapter,
            lambda event: (
                event.generation == 5 and event.payload.get("type") == "result"
            ),
        )
        self.assertEqual(result.generation, 5)
        rows = _protocol_rows(self.log)
        invocations = [row for row in rows if "argv" in row]
        self.assertEqual(len(invocations), 2)
        self.assertIn("--resume", invocations[0]["argv"])
        self.assertIn("--session-id", invocations[1]["argv"])
        replacement_config = json.loads(
            invocations[1]["argv"][invocations[1]["argv"].index("--mcp-config") + 1]
        )
        replacement_env = replacement_config["mcpServers"]["wiki-artifacts"]["env"]
        self.assertEqual(replacement_env["WIKI_RUN_ID"], replacement_record.run_id)
        self.assertEqual(
            replacement_env["WIKI_BACKEND_URL"],
            "http://127.0.0.1:43115",
        )
        user_rows = [row for row in rows if row.get("type") == "user"]
        self.assertEqual(len(user_rows), 1)

        archived = await adapter.archive()
        self.assertEqual(archived.state, LifecycleState.COMPLETED)

    async def test_pending_question_response_waits_for_control_request(self) -> None:
        record = _record(self.root, ProviderKind.CLAUDE, state=LifecycleState.STARTING)
        adapter = self._adapter(record)
        started = await adapter.start(_start_request(record))
        self.assertEqual(started.state, LifecycleState.WORKING)
        await _wait_event(
            adapter,
            lambda event: (
                event.payload.get("type") == "result"
                and event.payload.get("subtype") == "success"
            ),
        )
        self.assertEqual((await adapter.status()).state, LifecycleState.IDLE)

        generation = adapter.snapshot().generation
        await self._apply_message(
            adapter,
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "content_block": {
                        "type": "tool_use",
                        "name": "AskUserQuestion",
                        "id": "toolu_pending_fixture",
                    },
                },
            },
            generation,
        )
        response = {
            "answers": {
                "Which path should I take?": "Ship it",
            }
        }
        responded = await adapter.respond("toolu_pending_fixture", response)
        self.assertEqual(responded.state, LifecycleState.WORKING)
        self.assertEqual(  # noqa: SLF001 - deferred race path regression
            adapter._deferred_question_answers["toolu_pending_fixture"],
            response,
        )
        self.assertIn("toolu_pending_fixture", adapter._pending_question_ids)  # noqa: SLF001
        self.assertFalse(
            any(
                row.get("type") == "control_response"
                and row.get("response", {}).get("request_id") == "permission-ask-user"
                for row in _protocol_rows(self.log)
            )
        )

        await self._apply_message(
            adapter,
            {
                "type": "control_request",
                "request_id": "permission-ask-user",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "AskUserQuestion",
                    "tool_use_id": "toolu_pending_fixture",
                    "input": {"questions": [{"question": "Which path should I take?"}]},
                },
            },
            generation,
        )

        approval_row = await _wait_protocol_row(
            self.log,
            lambda row: (
                row.get("type") == "control_response"
                and row.get("response", {}).get("request_id") == "permission-ask-user"
            ),
        )
        self.assertEqual(
            approval_row["response"]["response"]["updatedInput"],
            {
                "questions": [{"question": "Which path should I take?"}],
                "answers": {"Which path should I take?": "Ship it"},
            },
        )
        self.assertNotIn("toolu_pending_fixture", adapter._pending_question_ids)  # noqa: SLF001
        self.assertNotIn("toolu_pending_fixture", adapter._deferred_question_answers)  # noqa: SLF001

    async def test_pending_question_response_updates_control_input(self) -> None:
        record = _record(self.root, ProviderKind.CLAUDE, state=LifecycleState.STARTING)
        adapter = self._adapter(record)
        started = await adapter.start(_start_request(record))
        self.assertEqual(started.state, LifecycleState.WORKING)
        await _wait_event(
            adapter,
            lambda event: (
                event.payload.get("type") == "result"
                and event.payload.get("subtype") == "success"
            ),
        )
        self.assertEqual((await adapter.status()).state, LifecycleState.IDLE)

        generation = adapter.snapshot().generation
        adapter._apply_message_state(  # noqa: SLF001 - pair question answer with control request approval
            {
                "type": "control_request",
                "request_id": "permission-ask-user",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "AskUserQuestion",
                    "tool_use_id": "toolu_pending_fixture",
                    "input": {"questions": [{"question": "Which path should I take?"}]},
                },
            },
            generation,
        )
        adapter._pending_question_ids.add("toolu_pending_fixture")  # noqa: SLF001 - question response path
        responded = await adapter.respond(
            "toolu_pending_fixture",
            {
                "answers": {
                    "Which path should I take?": "Ship it",
                }
            },
        )
        self.assertEqual(responded.state, LifecycleState.WORKING)
        self.assertNotIn("toolu_pending_fixture", adapter._pending_question_ids)  # noqa: SLF001
        self.assertEqual(adapter._deferred_question_answers, {})  # noqa: SLF001

        approval_row = await _wait_protocol_row(
            self.log,
            lambda row: (
                row.get("type") == "control_response"
                and row.get("response", {}).get("request_id") == "permission-ask-user"
            ),
        )
        self.assertEqual(
            approval_row["response"]["response"]["updatedInput"],
            {
                "questions": [{"question": "Which path should I take?"}],
                "answers": {"Which path should I take?": "Ship it"},
            },
        )
        self.assertEqual(
            approval_row["response"]["response"]["behavior"],
            "allow",
        )
        self.assertFalse(
            any(
                row.get("type") == "user"
                and any(
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and block.get("tool_use_id") == "toolu_pending_fixture"
                    for block in row.get("message", {}).get("content") or []
                )
                for row in _protocol_rows(self.log)
            )
        )

    async def test_multi_select_response_uses_claude_comma_separated_answer(self) -> None:
        record = _record(self.root, ProviderKind.CLAUDE, state=LifecycleState.STARTING)
        adapter = self._adapter(record)
        await adapter.start(_start_request(record))
        await _wait_event(
            adapter,
            lambda event: (
                event.payload.get("type") == "result"
                and event.payload.get("subtype") == "success"
            ),
        )

        generation = adapter.snapshot().generation
        input_payload = {
            "questions": [
                {
                    "question": "Which features?",
                    "multiSelect": True,
                }
            ]
        }
        adapter._apply_message_state(  # noqa: SLF001 - pair answer with Claude control request
            {
                "type": "control_request",
                "request_id": "permission-multi-select",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "AskUserQuestion",
                    "tool_use_id": "toolu_multi_select",
                    "input": input_payload,
                },
            },
            generation,
        )
        adapter._pending_question_ids.add("toolu_multi_select")  # noqa: SLF001 - fixture setup

        await adapter.respond(
            "toolu_multi_select",
            {"answers": {"Which features?": ["Search", "Vim mode"]}},
        )

        approval_row = await _wait_protocol_row(
            self.log,
            lambda row: (
                row.get("type") == "control_response"
                and row.get("response", {}).get("request_id") == "permission-multi-select"
            ),
        )
        self.assertEqual(
            approval_row["response"]["response"]["updatedInput"],
            {
                **input_payload,
                "answers": {"Which features?": "Search, Vim mode"},
            },
        )

    async def test_pending_question_response_cleanup_drops_deferred_answer(self) -> None:
        record = _record(self.root, ProviderKind.CLAUDE, state=LifecycleState.STARTING)
        adapter = self._adapter(record)
        started = await adapter.start(_start_request(record))
        self.assertEqual(started.state, LifecycleState.WORKING)
        await _wait_event(
            adapter,
            lambda event: (
                event.payload.get("type") == "result"
                and event.payload.get("subtype") == "success"
            ),
        )
        self.assertEqual((await adapter.status()).state, LifecycleState.IDLE)

        generation = adapter.snapshot().generation
        await self._apply_message(
            adapter,
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "content_block": {
                        "type": "tool_use",
                        "name": "AskUserQuestion",
                        "id": "toolu_pending_fixture",
                    },
                },
            },
            generation,
        )
        await adapter.respond(
            "toolu_pending_fixture",
            {
                "answers": {
                    "Which path should I take?": "Ship it",
                }
            },
        )
        self.assertIn("toolu_pending_fixture", adapter._deferred_question_answers)  # noqa: SLF001

        await self._apply_message(
            adapter,
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_pending_fixture",
                        }
                    ]
                },
            },
            generation,
        )
        self.assertNotIn("toolu_pending_fixture", adapter._pending_question_ids)  # noqa: SLF001
        self.assertNotIn("toolu_pending_fixture", adapter._deferred_question_answers)  # noqa: SLF001

        await self._apply_message(
            adapter,
            {
                "type": "control_request",
                "request_id": "permission-ask-user",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "AskUserQuestion",
                    "tool_use_id": "toolu_pending_fixture",
                    "input": {"questions": [{"question": "Which path should I take?"}]},
                },
            },
            generation,
        )
        self.assertFalse(
            any(
                row.get("type") == "control_response"
                and row.get("response", {}).get("request_id") == "permission-ask-user"
                for row in _protocol_rows(self.log)
            )
        )

    async def test_pending_question_response_keeps_latest_queued_answer(self) -> None:
        record = _record(self.root, ProviderKind.CLAUDE, state=LifecycleState.STARTING)
        adapter = self._adapter(record)
        started = await adapter.start(_start_request(record))
        self.assertEqual(started.state, LifecycleState.WORKING)
        await _wait_event(
            adapter,
            lambda event: (
                event.payload.get("type") == "result"
                and event.payload.get("subtype") == "success"
            ),
        )
        self.assertEqual((await adapter.status()).state, LifecycleState.IDLE)

        generation = adapter.snapshot().generation
        await self._apply_message(
            adapter,
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "content_block": {
                        "type": "tool_use",
                        "name": "AskUserQuestion",
                        "id": "toolu_pending_fixture",
                    },
                },
            },
            generation,
        )
        await adapter.respond(
            "toolu_pending_fixture",
            {
                "answers": {
                    "Which path should I take?": "Take path A",
                }
            },
        )
        await adapter.respond(
            "toolu_pending_fixture",
            {
                "answers": {
                    "Which path should I take?": "Take path B",
                }
            },
        )
        self.assertEqual(  # noqa: SLF001 - deferred overwrite regression
            adapter._deferred_question_answers["toolu_pending_fixture"],
            {
                "answers": {
                    "Which path should I take?": "Take path B",
                }
            },
        )

        await self._apply_message(
            adapter,
            {
                "type": "control_request",
                "request_id": "permission-ask-user",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "AskUserQuestion",
                    "tool_use_id": "toolu_pending_fixture",
                    "input": {"questions": [{"question": "Which path should I take?"}]},
                },
            },
            generation,
        )

        control_row = await _wait_protocol_row(
            self.log,
            lambda row: (
                row.get("type") == "control_response"
                and row.get("response", {}).get("request_id") == "permission-ask-user"
            ),
        )
        self.assertEqual(
            control_row["response"]["response"]["updatedInput"]["answers"],
            {"Which path should I take?": "Take path B"},
        )

    async def test_working_resume_reissues_continuation_but_idle_does_not(self) -> None:
        self.env["WIKI_AGENT_STATUS_DIR"] = str(self.root / "status")
        working = _record(self.root, ProviderKind.CLAUDE, state=LifecycleState.WORKING)
        working.provider_session_id = "00000000-0000-4000-8000-000000000043"
        adapter = self._adapter(working)
        resumed = await adapter.resume(working.provider_session_id)
        self.assertEqual(resumed.state, LifecycleState.WORKING)
        user = await _wait_protocol_row(
            self.log,
            lambda row: row.get("type") == "user",
        )
        self.assertIn(
            str(self.root / "status" / "WIKI-42.json"),
            user["message"]["content"][0]["text"],
        )
        await adapter.stop()

        self.log.unlink()
        idle = _record(self.root, ProviderKind.CLAUDE, state=LifecycleState.IDLE)
        idle.provider_session_id = "00000000-0000-4000-8000-000000000044"
        idle_adapter = self._adapter(idle)
        idle_status = await idle_adapter.resume(idle.provider_session_id)
        self.assertEqual(idle_status.state, LifecycleState.IDLE)
        self.assertFalse(
            any(row.get("type") == "user" for row in _protocol_rows(self.log))
        )

    async def test_broken_output_streams_terminate_provider_instead_of_hanging(
        self,
    ) -> None:
        for flag in ("FAKE_OVERSIZED", "FAKE_OVERSIZED_STDERR", "FAKE_EOF_LIVE"):
            with self.subTest(flag=flag):
                record = _record(
                    self.root,
                    ProviderKind.CLAUDE,
                    state=LifecycleState.STARTING,
                )
                env = dict(self.env)
                env[flag] = "1"
                adapter = ClaudeStreamAdapter(
                    record,
                    command=(
                        sys.executable,
                        "-u",
                        str(FIXTURES / "fake_claude_stream.py"),
                    ),
                    env=env,
                    request_timeout=2,
                    identity_resolver=self._no_open_handle,
                )
                self.adapters.append(adapter)
                with self.assertRaises(ProviderProcessError):
                    await asyncio.wait_for(
                        adapter.start(_start_request(record)), timeout=5
                    )
                process = adapter._process  # noqa: SLF001 - process leak regression
                assert process is not None
                self.assertIsNotNone(process.returncode)


if __name__ == "__main__":
    unittest.main()
