from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from unittest import mock
from uuid import uuid4

from backend.app import accounts
from backend.app import provider_health
from backend.app.agent_runtime.client import (
    SupervisorClient,
    SupervisorRemoteError,
    SupervisorUnavailable,
)
from backend.app.agent_runtime.codex import CodexAppServerAdapter
from backend.app.agent_runtime.fake import CodexFixtureAdapter, FixtureAdapterFactory
from backend.app.agent_runtime.process import (
    ProviderProcessIdentity,
    ProviderProcessStatus,
    provider_parent_pid,
    provider_pid_is_orphan,
    provider_process_status,
)
from backend.app.agent_runtime.protocol import UnixSupervisorServer
from backend.app.agent_runtime.provider import (
    AdapterStatus,
    ProviderEvent,
    StartRequest,
)
from backend.app.agent_runtime.store import RunStore, RuntimePaths, StoreConflict
from backend.app.agent_runtime.supervisor import Supervisor, resolve_safe_worktree
from backend.app.agent_runtime.types import (
    MAX_MESSAGE_DEDUPE_KEYS,
    LifecycleState,
    ProviderKind,
    RunRecord,
)
from backend.app.agent_runtime.version import RUNTIME_FINGERPRINT


FIXTURES = Path(__file__).parent / "fixtures" / "agent_runtime"


class StartFailureAdapter(CodexFixtureAdapter):
    async def start(self, request: StartRequest) -> AdapterStatus:
        await self._events.put(  # noqa: SLF001 - failure-drain fixture
            ProviderEvent(
                ProviderKind.CODEX,
                {
                    "method": "error",
                    "params": {"message": "fixture start failure", "willRetry": False},
                },
                generation=self.snapshot().generation + 1,
            )
        )
        raise RuntimeError("fixture start failure")


class ResumeFailureAdapter(CodexFixtureAdapter):
    def __init__(self, *args, attempts: list[str], **kwargs):
        super().__init__(*args, **kwargs)
        self.attempts = attempts

    async def resume(self, session_id: str) -> AdapterStatus:
        self.attempts.append(session_id)
        await self._events.put(  # noqa: SLF001 - failure-drain fixture
            ProviderEvent(
                ProviderKind.CODEX,
                {
                    "method": "error",
                    "params": {"message": "fixture resume failure", "willRetry": False},
                },
                generation=self.snapshot().generation + 1,
            )
        )
        raise RuntimeError("fixture resume failure")


def _paths(root: Path) -> RuntimePaths:
    return RuntimePaths(
        runtime_dir=root / "runtime",
        socket_path=root / "runtime" / "supervisor.sock",
        registry_path=root / "isolated-registry.json",
        archive_dir=root / "archive",
        status_dir=root / "status",
    )


async def _wait_for_events(store: RunStore, run_id: str, minimum: int) -> RunRecord:
    for _ in range(200):
        record = store.get(run_id)
        if record.raw_event_count >= minimum:
            return record
        await asyncio.sleep(0.01)
    raise AssertionError(f"run {run_id} did not reach {minimum} raw events")


async def _wait_for_published(
    queue: asyncio.Queue[dict[str, Any]],
    event_type: str,
    *,
    timeout: float = 2,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"did not publish {event_type}")
        event = await asyncio.wait_for(queue.get(), timeout=remaining)
        if event.get("type") == event_type:
            return event


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.paths = _paths(self.root)
        self.store = RunStore(self.paths)
        self.supervisor = Supervisor(
            self.store,
            FixtureAdapterFactory(FIXTURES, pid=os.getpid()),
        )

    async def asyncTearDown(self) -> None:
        await self.supervisor.close()
        self.tmp.cleanup()

    async def _spawn_orphan_process(self, *, ignore_sigterm: bool = False) -> int:
        child_code = (
            "import os, signal, sys, time\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            "    os.setsid()\n"
            f"    {'signal.signal(signal.SIGTERM, signal.SIG_IGN)' if ignore_sigterm else 'pass'}\n"
            "    os.close(sys.stdout.fileno())\n"
            "    time.sleep(30)\n"
            "    os._exit(0)\n"
            "print(pid, flush=True)\n"
            "os._exit(0)\n"
        )
        helper = subprocess.Popen(
            [sys.executable, "-c", child_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        stdout, _ = helper.communicate(timeout=2)
        child_pid = int(stdout.strip())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if await provider_parent_pid(child_pid) == 1:
                return child_pid
            await asyncio.sleep(0.05)
        self.fail(f"child {child_pid} was not reparented to init")

    async def _cleanup_process(self, pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if await provider_process_status(pid) is None:
                return
            await asyncio.sleep(0.05)
        self.fail(f"child {pid} did not exit during cleanup")

    def _codex_process_factory(self, env: dict[str, str]):
        async def identity(
            pid: int | None,
            _provider: ProviderKind,
            _session_id: str | None,
            *,
            reported_path: str | None = None,
        ) -> ProviderProcessIdentity | None:
            if pid is None or reported_path is None:
                return None
            return ProviderProcessIdentity(pid, reported_path)

        def factory(record: RunRecord):
            return CodexAppServerAdapter(
                record,
                command=(
                    sys.executable,
                    "-u",
                    str(FIXTURES / "fake_codex_app_server.py"),
                ),
                env=env,
                request_timeout=2,
                identity_resolver=identity,
            )

        return factory

    def _seed_accounts(self) -> dict[str, str]:
        auth = self.root / "codex" / "auth.json"
        account_dir = self.root / "codex-accounts"
        for name, token in (("alpha", "alpha-stored"), ("beta", "beta")):
            path = account_dir / name
            path.mkdir(parents=True)
            (path / "auth.json").write_text(
                json.dumps({"tokens": token}), encoding="utf-8"
            )
        auth.parent.mkdir(parents=True)
        auth.write_text('{"tokens":"alpha-refreshed"}', encoding="utf-8")
        env = {
            "WIKI_CODEX_AUTH_PATH": str(auth),
            "WIKI_CODEX_ACCOUNTS_DIR": str(account_dir),
            "WIKI_ROTATION_LOG_PATH": str(account_dir / "rotation.log"),
            "WIKI_CODEX_SESSIONS_DIR": str(self.root / "codex" / "sessions"),
            "WIKI_ACCOUNT_HOME_OVERRIDE": str(self.root / "account-home"),
            "WIKI_CLI_PATH": str(self.root / "missing-wiki"),
            "TMUX": "",
            "TMUX_PANE": "",
        }
        with mock.patch.dict(os.environ, env):
            accounts.write_state(
                accounts.AccountState(
                    active="alpha",
                    accounts={
                        "alpha": {"limit_reset_at": None},
                        "beta": {"limit_reset_at": None},
                    },
                )
            )
        return env

    async def test_runtime_card_covers_each_role_and_provider_variant(self) -> None:
        backend_url = "http://127.0.0.1:43111"
        for provider in (ProviderKind.CODEX, ProviderKind.CLAUDE):
            for role in ("implement", "orchestrator"):
                with self.subTest(provider=provider.value, role=role):
                    agent_id = f"WIKI-CARD-{provider.value.upper()}-{role.upper()}"
                    with mock.patch(
                        "backend.app.agent_runtime.runtime_card.worktree_branch",
                        return_value="codex/fixture-runtime-card",
                    ):
                        record = await self.supervisor.start_run(
                            agent_id=agent_id,
                            provider=provider,
                            role=role,
                            model=f"fixture-{provider.value}",
                            effort="high" if provider is ProviderKind.CODEX else None,
                            worktree=str(self.worktree),
                            prompt=f"Original prompt for {agent_id}",
                            orchestrator_id=None if role == "orchestrator" else "wiki",
                            backend_base_url=backend_url,
                        )
                    prompt = self.store.get(record.run_id).initial_prompt or ""
                    self.assertTrue(prompt.startswith("<WIKI_RUNTIME_CARD v=1>"))
                    self.assertIn(f"run_id={record.run_id}", prompt)
                    self.assertIn(f"role={role}", prompt)
                    self.assertIn(f"kind={provider.legacy_kind}", prompt)
                    self.assertIn(f"backend: {backend_url}", prompt)
                    self.assertIn(f"worktree: {self.worktree.resolve()}", prompt)
                    self.assertIn("branch: codex/fixture-runtime-card", prompt)
                    self.assertTrue(prompt.endswith(f"Original prompt for {agent_id}"))
                    self.assertLess(len(prompt.encode("utf-8")), 100_000)
                    if role == "orchestrator":
                        self.assertIn("ORCHESTRATOR controls", prompt)
                        self.assertIn("wiki agent spawn <ticket>", prompt)
                        self.assertIn("wiki gate <pr> --expect-sha <sha>", prompt)
                        self.assertIn("wiki agent archive <id>", prompt)
                    else:
                        self.assertIn("WORKER contract", prompt)
                        self.assertIn(
                            "You do not have fleet spawn/steer/archive authority",
                            prompt,
                        )
                        self.assertNotIn("wiki agent spawn <ticket>", prompt)

    async def test_mixed_fake_fleet_persists_raw_before_normalized(self) -> None:
        codex = await self.supervisor.start_run(
            agent_id="WIKI-CODEX",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-CODEX",
            orchestrator_id="wiki-dev",
        )
        claude = await self.supervisor.start_run(
            agent_id="WIKI-CLAUDE",
            provider=ProviderKind.CLAUDE,
            role="review",
            model="fixture-claude",
            effort=None,
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-CLAUDE",
            orchestrator_id="wiki-dev",
        )
        codex = await _wait_for_events(self.store, codex.run_id, 10)
        claude = await _wait_for_events(self.store, claude.run_id, 10)

        self.assertGreater(codex.normalized_event_count, 0)
        self.assertEqual(codex.raw_event_count, codex.normalized_event_count)
        self.assertGreater(claude.normalized_event_count, 0)
        self.assertEqual(claude.raw_event_count, claude.normalized_event_count)
        self.assertGreater(codex.disposition_counts["rendered"], 0)
        self.assertGreater(codex.disposition_counts["ignored"], 0)
        self.assertGreater(claude.disposition_counts["rendered"], 0)

    async def test_message_contract_and_sse_event_shape_remain_stable(self) -> None:
        queue = self.supervisor.subscribe()
        record = await self.supervisor.start_run(
            agent_id="WIKI-42",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-42",
        )
        event = await asyncio.wait_for(queue.get(), timeout=2)
        self.assertEqual(
            event, {"type": "agents", "tickets": ["WIKI-42"], "surface": "agents"}
        )

        sent = await self.supervisor.send_now(record.run_id, "steer now")
        self.assertEqual(sent, {"status": "sent"})
        queued = await self.supervisor.send_on_idle(record.run_id, "send after idle")
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["position"], 1)
        self.assertEqual(queued["messages"][0]["text"], "send after idle")

        session_events = []
        for _ in range(40):
            event = await asyncio.wait_for(queue.get(), timeout=2)
            session_events.append(event)
            if {item.get("surface") for item in session_events} >= {"session", "queue"}:
                break
        self.assertIn(
            {"type": "session", "ticket": "WIKI-42", "surface": "session"},
            session_events,
        )
        self.assertIn(
            {"type": "session", "ticket": "WIKI-42", "surface": "queue"},
            session_events,
        )
        self.supervisor.unsubscribe(queue)

    async def test_codex_success_event_carries_turn_credential_fingerprint(self) -> None:
        queue = self.supervisor.subscribe()
        with mock.patch.object(
            provider_health, "credential_fingerprint", return_value="fp-old"
        ):
            await self.supervisor.start_run(
                agent_id="WIKI-AUTH-FINGERPRINT",
                provider=ProviderKind.CODEX,
                role="implement",
                model="fixture-codex",
                effort="high",
                worktree=str(self.worktree),
                prompt="Work on ticket WIKI-AUTH-FINGERPRINT",
            )
            verified = await _wait_for_published(queue, "codex_auth_verified")
        self.assertEqual(verified["credential_fingerprint"], "fp-old")
        self.supervisor.unsubscribe(queue)

    async def test_artifact_failure_message_deduplicates_durably(self) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-ARTIFACT-DEDUPE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Work on artifact feedback.",
        )
        await self.supervisor.send_now(record.run_id, "begin work")
        dedupe_key = "artifact-render:artifact-123:mermaid-render"
        first = await self.supervisor.send_on_idle(
            record.run_id,
            "normalized artifact failure",
            dedupe_key=dedupe_key,
        )
        second = await self.supervisor.send_on_idle(
            record.run_id,
            "different duplicate payload",
            dedupe_key=dedupe_key,
        )

        self.assertEqual(first["status"], "queued")
        self.assertEqual(second["status"], "deduplicated")
        self.assertEqual(len(self.store.queued_messages(record.run_id)), 1)
        self.assertEqual(
            self.store.get(record.run_id).message_dedupe_keys,
            [dedupe_key],
        )

        reloaded = RunStore(self.paths)
        self.assertEqual(reloaded.get(record.run_id).message_dedupe_keys, [dedupe_key])

        for index in range(MAX_MESSAGE_DEDUPE_KEYS + 1):
            self.store.claim_message_dedupe_key(record.run_id, f"artifact-render:key-{index}:svg-render")
        keys = self.store.get(record.run_id).message_dedupe_keys
        self.assertEqual(len(keys), MAX_MESSAGE_DEDUPE_KEYS)
        self.assertNotIn("artifact-render:key-0:svg-render", keys)
        self.assertIn(
            f"artifact-render:key-{MAX_MESSAGE_DEDUPE_KEYS}:svg-render",
            keys,
        )

    async def test_dispatch_idempotently_replays_start_and_message_once(self) -> None:
        start_params = {
            "agent_id": "WIKI-IDEMPOTENT",
            "provider": "codex",
            "role": "implement",
            "model": "fixture-codex",
            "effort": "high",
            "worktree": str(self.worktree),
            "prompt": "Work on ticket WIKI-IDEMPOTENT",
            "request_id": "spawn-retry-1",
        }
        first_start, replayed_start = await asyncio.gather(
            self.supervisor.dispatch("run/start", start_params),
            self.supervisor.dispatch("run/start", dict(start_params)),
        )
        self.assertEqual(first_start, replayed_start)
        matching_runs = [
            record
            for record in self.store.list_runs()
            if record.agent_id == "WIKI-IDEMPOTENT"
        ]
        self.assertEqual(len(matching_runs), 1)

        adapter = self.supervisor.adapters[matching_runs[0].run_id]
        starts_before_message = adapter.replayed_methods.count("turn/start")
        message_params = {
            "agent_id": "WIKI-IDEMPOTENT",
            "text": "Retry-safe steer",
            "request_id": "message-retry-1",
        }
        first_message, replayed_message = await asyncio.gather(
            self.supervisor.dispatch("run/send_now", message_params),
            self.supervisor.dispatch("run/send_now", dict(message_params)),
        )
        self.assertEqual(first_message, {"status": "sent"})
        self.assertEqual(replayed_message, first_message)
        self.assertEqual(
            adapter.replayed_methods.count("turn/start"),
            starts_before_message + 1,
        )
        await asyncio.sleep(0)
        self.assertEqual(self.supervisor.idempotency_tasks, {})

    async def test_dispatch_allows_integer_codex_approval_request_id(self) -> None:
        started = await self.supervisor.dispatch(
            "run/start",
            {
                "agent_id": "WIKI-APPROVAL",
                "provider": "codex",
                "role": "implement",
                "model": "fixture-codex",
                "effort": "high",
                "worktree": str(self.worktree),
                "prompt": "Handle a Codex approval.",
            },
        )

        responded = await self.supervisor.dispatch(
            "run/respond",
            {
                "run_id": started["run_id"],
                "request_id": 3,
                "response": {"approved": True},
            },
        )

        self.assertEqual(responded["run_id"], started["run_id"])

    async def test_idempotency_cache_evicts_oldest_result(self) -> None:
        self.supervisor.idempotency_cache_size = 2
        started = await self.supervisor.dispatch(
            "run/start",
            {
                "agent_id": "WIKI-IDEMPOTENCY-LRU",
                "provider": "codex",
                "role": "implement",
                "model": "fixture-codex",
                "effort": "high",
                "worktree": str(self.worktree),
                "prompt": "Work on ticket WIKI-IDEMPOTENCY-LRU",
                "request_id": "spawn-retry-1",
            },
        )
        for request_id in ("message-retry-1", "message-retry-2"):
            await self.supervisor.dispatch(
                "run/send_now",
                {
                    "run_id": started["run_id"],
                    "text": request_id,
                    "request_id": request_id,
                },
            )

        self.assertEqual(len(self.supervisor.idempotency_results), 2)
        self.assertNotIn(("run/start", "spawn-retry-1"), self.supervisor.idempotency_results)

    async def test_start_warns_when_active_workers_reach_soft_cap(self) -> None:
        self.supervisor.worker_soft_cap = 1
        warned_at_cap = await self.supervisor.dispatch(
            "run/start",
            {
                "agent_id": "WIKI-SOFT-CAP-ONE",
                "provider": "codex",
                "role": "implement",
                "model": "fixture-codex",
                "effort": "high",
                "worktree": str(self.worktree),
                "prompt": "Work on ticket WIKI-SOFT-CAP-ONE",
            },
        )
        warned = await self.supervisor.dispatch(
            "run/start",
            {
                "agent_id": "WIKI-SOFT-CAP-TWO",
                "provider": "codex",
                "role": "review",
                "model": "fixture-codex",
                "effort": "high",
                "worktree": str(self.worktree),
                "prompt": "Work on ticket WIKI-SOFT-CAP-TWO",
            },
        )

        self.assertEqual(
            warned_at_cap["warning"],
            "1 active workers; soft cap 1 — expect provider timeouts under load",
        )
        self.assertEqual(
            warned["warning"],
            "2 active workers; soft cap 1 — expect provider timeouts under load",
        )

    async def test_malformed_worker_soft_cap_uses_default_with_warning(self) -> None:
        with (
            mock.patch.dict(os.environ, {"WIKI_WORKER_SOFT_CAP": "not-a-number"}),
            warnings.catch_warnings(record=True) as caught,
        ):
            warnings.simplefilter("always")
            supervisor = Supervisor(self.store, FixtureAdapterFactory(FIXTURES))
        try:
            self.assertEqual(supervisor.worker_soft_cap, 5)
            self.assertTrue(
                any("WIKI_WORKER_SOFT_CAP" in str(item.message) for item in caught)
            )
        finally:
            await supervisor.close()

    async def test_pending_id_round_trips_through_claude_provider_echo(self) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-96",
            provider=ProviderKind.CLAUDE,
            role="implement",
            model="fixture-claude",
            effort=None,
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-96",
        )
        await _wait_for_events(self.store, record.run_id, 5)
        pending_id = str(uuid4())

        sent = await self.supervisor.send_now(
            record.run_id,
            "Test test test",
            pending_id,
        )
        self.assertEqual(sent["status"], "sent")
        self.assertEqual(sent["pending_id"], pending_id)
        self.assertNotIn("messages", sent)

        adapter = self.supervisor.adapters[record.run_id]
        await self.supervisor._handle_provider_event(  # noqa: SLF001
            record.run_id,
            adapter,
            ProviderEvent(
                ProviderKind.CLAUDE,
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Test test test\n<system-reminder>hook</system-reminder>",
                            }
                        ],
                    },
                },
            ),
        )

        for _ in range(100):
            composer_messages = self.store.get(record.run_id).composer_messages
            if composer_messages:
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("Claude provider echo never acknowledged pending_id")

        self.assertEqual(composer_messages[0]["pending_id"], pending_id)
        self.assertEqual(composer_messages[0]["text"], "Test test test")
        self.assertEqual(self.store.get(record.run_id).pending_user_messages, [])

    async def test_queued_message_keeps_pending_id_until_delivery(self) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-96-QUEUE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-96-QUEUE",
        )
        await self.supervisor.send_now(record.run_id, "begin a long turn")
        pending_id = str(uuid4())

        queued = await self.supervisor.send_on_idle(
            record.run_id,
            "deliver later",
            pending_id,
        )

        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["messages"][0]["pending_id"], pending_id)
        self.assertEqual(
            self.store.get(record.run_id).queued_messages[0]["pending_id"],
            pending_id,
        )

    async def test_delivered_pending_id_survives_replace_until_late_echo(self) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-96-REPLACE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-96-REPLACE",
        )
        await self.supervisor.send_now(record.run_id, "begin a long turn")
        pending_id = str(uuid4())
        await self.supervisor.send_on_idle(
            record.run_id,
            "survive replacement",
            pending_id,
        )
        adapter = self.supervisor.adapters[record.run_id]

        # The provider has accepted the queued message, but its user echo has
        # not arrived yet: it has moved from queued to pending reconciliation.
        await self.supervisor._deliver_next_queued_locked(  # noqa: SLF001
            record.run_id,
            adapter,
        )
        delivered = self.store.get(record.run_id)
        self.assertEqual(delivered.queued_messages, [])
        self.assertEqual(
            [message["pending_id"] for message in delivered.pending_user_messages],
            [pending_id],
        )

        replacement = await self.supervisor.replace(
            record.run_id,
            "Continue ticket WIKI-96-REPLACE after revival",
        )
        self.assertEqual(
            [
                message["pending_id"]
                for message in self.store.get(replacement.run_id).pending_user_messages
            ],
            [pending_id],
        )

        replacement_adapter = self.supervisor.adapters[replacement.run_id]
        await self.supervisor._handle_provider_event(  # noqa: SLF001
            replacement.run_id,
            replacement_adapter,
            ProviderEvent(
                ProviderKind.CODEX,
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "userMessage",
                            "content": [
                                {"type": "text", "text": "survive replacement"}
                            ],
                        }
                    },
                },
                generation=replacement.provider_generation,
            ),
        )

        reconciled = self.store.get(replacement.run_id)
        self.assertEqual(reconciled.pending_user_messages, [])
        self.assertEqual(
            [message["pending_id"] for message in reconciled.composer_messages],
            [pending_id],
        )

    async def test_codex_question_before_turn_response_can_be_answered(self) -> None:
        await self.supervisor.close()
        protocol_log = self.root / "live-order-protocol.jsonl"
        transcript_dir = self.root / "live-order-transcripts"
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.root / "home"),
                "CODEX_HOME": str(self.root / "codex-home"),
                "FAKE_PROTOCOL_LOG": str(protocol_log),
                "FAKE_CODEX_TRANSCRIPT_DIR": str(transcript_dir),
                "FAKE_CODEX_APPROVAL": "1",
                "TMUX": "must-not-leak",
                "TMUX_PANE": "%9999",
            }
        )

        self.supervisor = Supervisor(self.store, self._codex_process_factory(env))
        events = self.supervisor.subscribe()
        start_task = asyncio.create_task(
            self.supervisor.start_run(
                agent_id="WIKI-QUESTION",
                provider=ProviderKind.CODEX,
                role="implement",
                model="fixture-codex",
                effort="high",
                worktree=str(self.worktree),
                prompt="Ask the fixture question",
            )
        )
        run_id: str | None = None
        for _ in range(200):
            run_id = self.store.current_run_id("WIKI-QUESTION")
            if run_id and self.store.get(run_id).pending_requests:
                break
            await asyncio.sleep(0.01)
        self.assertIsNotNone(run_id)
        assert run_id is not None
        started = await asyncio.wait_for(start_task, timeout=2)
        self.assertEqual(started.provider_session_id, "thread-1")
        pending = self.store.get(run_id).pending_requests
        self.assertEqual(pending["int:0"]["request_id"], 0)
        self.assertEqual(
            pending["int:0"]["request_kind"],
            "item/tool/requestUserInput",
        )

        await self.supervisor.respond(
            run_id,
            0,
            {"answers": {"wiki_surface": {"answers": ["Agents page"]}}},
        )
        self.assertEqual(self.store.get(run_id).pending_requests, {})
        self.assertGreater(started.raw_event_count, 0)
        surfaces: set[str] = set()
        for _ in range(8):
            event = await asyncio.wait_for(events.get(), timeout=2)
            surface = event.get("surface")
            if isinstance(surface, str):
                surfaces.add(surface)
            if "session" in surfaces:
                break
        self.assertIn("session", surfaces)
        self.supervisor.unsubscribe(events)

    async def test_background_codex_turn_rejection_is_durably_blocked(self) -> None:
        await self.supervisor.close()
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.root / "failure-home"),
                "CODEX_HOME": str(self.root / "failure-codex-home"),
                "FAKE_PROTOCOL_LOG": str(self.root / "failure-protocol.jsonl"),
                "FAKE_CODEX_TRANSCRIPT_DIR": str(self.root / "failure-transcripts"),
                "FAKE_TURN_START_ERROR": "1",
                "TMUX": "must-not-leak",
                "TMUX_PANE": "%9999",
            }
        )
        self.supervisor = Supervisor(self.store, self._codex_process_factory(env))
        started = await self.supervisor.start_run(
            agent_id="WIKI-TURN-FAIL",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Reject this turn after accepting the session",
        )
        for _ in range(200):
            failed = self.store.get(started.run_id)
            if failed.state is LifecycleState.BLOCKED:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(failed.state, LifecycleState.BLOCKED)
        self.assertTrue(
            any(
                event["payload"].get("params", {}).get("operation") == "turn/start"
                for event in self.store.read_raw_events(started.run_id)
            )
        )

    async def test_event_driven_delivery_failure_keeps_message_and_provider(
        self,
    ) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-42",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-42",
        )
        adapter = self.supervisor.adapters[record.run_id]
        self.store.queue_message(record.run_id, "do not lose this")
        with mock.patch.object(
            adapter,
            "send_on_idle",
            side_effect=RuntimeError("fixture delivery failed"),
        ):
            await self.supervisor._handle_provider_event(  # noqa: SLF001
                record.run_id,
                adapter,
                ProviderEvent(
                    ProviderKind.CODEX,
                    {
                        "method": "thread/status/changed",
                        "params": {"status": {"type": "idle"}},
                    },
                ),
            )
            for _ in range(200):
                persisted = self.store.get(record.run_id)
                if persisted.state_reason == "queued message delivery failed: fixture delivery failed":
                    break
                await asyncio.sleep(0.01)
        queued = persisted.queued_messages
        self.assertEqual([item["text"] for item in queued], ["do not lose this"])
        self.assertEqual(
            persisted.state_reason,
            "queued message delivery failed: fixture delivery failed",
        )
        self.assertIs(self.supervisor.adapters[record.run_id], adapter)
        self.assertFalse(getattr(adapter, "closed", False))
        self.assertNotIn(record.run_id, self.supervisor.pipeline_failures)

    async def test_normalization_failure_keeps_raw_event_and_run_alive(self) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-42",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-42",
        )
        record = await _wait_for_events(self.store, record.run_id, 10)
        before_raw = record.raw_event_count
        before_normalized = record.normalized_event_count
        adapter = self.supervisor.adapters[record.run_id]
        with mock.patch(
            "backend.app.agent_runtime.supervisor.normalize_provider_event",
            side_effect=RuntimeError("fixture normalizer failure"),
        ):
            await self.supervisor._handle_provider_event(  # noqa: SLF001 - ordering contract test
                record.run_id,
                adapter,
                ProviderEvent(ProviderKind.CODEX, {"method": "fixture/unknown"}),
            )
        record = self.store.get(record.run_id)
        self.assertEqual(record.raw_event_count, before_raw + 1)
        self.assertEqual(record.normalized_event_count, before_normalized + 1)
        self.assertEqual(record.state, LifecycleState.IDLE)
        self.assertEqual(
            self.store.read_normalized_events(record.run_id)[-1]["disposition"],
            "unknown",
        )

    async def test_start_failure_events_are_drained_before_run_is_marked_dead(
        self,
    ) -> None:
        await self.supervisor.close()

        def factory(_record: RunRecord):
            return StartFailureAdapter(
                FIXTURES / "codex_app_server_success.jsonl",
                FIXTURES / "codex_app_server_control.jsonl",
                pid=987_654,
            )

        self.supervisor = Supervisor(self.store, factory, pid_alive=lambda _pid: False)
        params = {
            "agent_id": "WIKI-START-FAIL",
            "provider": "codex",
            "role": "implement",
            "model": "fixture-codex",
            "effort": "high",
            "worktree": str(self.worktree),
            "prompt": "Fail after emitting a provider event",
            "request_id": "failed-spawn-retry",
        }
        with self.assertRaisesRegex(RuntimeError, "fixture start failure"):
            await self.supervisor.dispatch("run/start", params)
        with self.assertRaisesRegex(RuntimeError, "fixture start failure"):
            await self.supervisor.dispatch("run/start", dict(params))
        await asyncio.sleep(0)
        self.assertNotIn(
            ("run/start", "failed-spawn-retry"), self.supervisor.idempotency_tasks
        )
        self.assertIn(
            ("run/start", "failed-spawn-retry"), self.supervisor.idempotency_results
        )
        run_id = self.store.current_run_id("WIKI-START-FAIL")
        assert run_id is not None
        failed = self.store.get(run_id)
        self.assertEqual(failed.state, LifecycleState.DEAD)
        self.assertTrue(
            any(
                event["payload"].get("method") == "error"
                for event in self.store.read_raw_events(run_id)
            )
        )

    async def test_replace_creates_new_run_and_stale_run_never_recovers(self) -> None:
        old = await self.supervisor.start_run(
            agent_id="WIKI-42",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Original ticket WIKI-42 prompt",
        )
        adapter = self.supervisor.adapters[old.run_id]
        with mock.patch.object(
            adapter, "replace", wraps=adapter.replace
        ) as provider_replace:
            replacement = await self.supervisor.replace(
                old.run_id,
                "Replacement ticket WIKI-42 prompt",
            )
        provider_replace.assert_awaited_once()
        replacement_prompt = provider_replace.await_args.args[0]
        self.assertIn("<WIKI_RUNTIME_CARD v=1>", replacement_prompt)
        self.assertIn("Replacement ticket WIKI-42 prompt", replacement_prompt)
        self.assertIn(f"run_id={replacement.run_id}", replacement_prompt)
        self.assertEqual(provider_replace.await_args.args[1:], (None, "high"))
        self.assertNotEqual(replacement.run_id, old.run_id)
        old = self.store.get(old.run_id)
        self.assertEqual(old.replaced_by_run_id, replacement.run_id)
        self.assertEqual(old.outcome, "handoff")
        self.assertNotIn(old.run_id, self.supervisor.adapters)
        self.assertNotIn(old.run_id, self.supervisor.event_tasks)
        self.assertIs(self.supervisor.adapters[replacement.run_id], adapter)
        await _wait_for_events(self.store, old.run_id, 10)
        await _wait_for_events(self.store, replacement.run_id, 10)
        old_methods = [
            event["payload"].get("method")
            for event in self.store.read_raw_events(old.run_id)
        ]
        replacement_methods = [
            event["payload"].get("method")
            for event in self.store.read_raw_events(replacement.run_id)
        ]
        self.assertEqual(
            {event["generation"] for event in self.store.read_raw_events(old.run_id)},
            {1},
        )
        self.assertEqual(
            {
                event["generation"]
                for event in self.store.read_raw_events(replacement.run_id)
            },
            {2},
        )
        self.assertEqual(old_methods.count("thread/started"), 1)
        self.assertEqual(replacement_methods.count("thread/started"), 1)

        recovery = await self.supervisor.recover_on_start()
        old_result = next(item for item in recovery if item["run_id"] == old.run_id)
        self.assertEqual(old_result["action"], "skip")

    async def test_replace_terminates_verified_orphan_before_fresh_launch(self) -> None:
        old = RunRecord.new(
            agent_id="WIKI-SWAP",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Original swap prompt",
        )
        old.state = LifecycleState.IDLE
        old.provider_session_id = "old-swap-session"
        old.provider_pid = 424_242
        self.store.create(old)
        orphan = ProviderProcessStatus(
            pid=424_242,
            parent_pid=1,
            created_at=1_783_718_400.125,
            process_group_id=424_242,
        )

        with (
            mock.patch.object(self.supervisor, "pid_alive", return_value=True),
            mock.patch.object(
                self.supervisor,
                "_orphaned_provider_process",
                new=mock.AsyncMock(return_value=orphan),
            ) as inspect_orphan,
            mock.patch.object(
                self.supervisor,
                "_terminate_orphan_provider_pid",
                new=mock.AsyncMock(return_value=True),
            ) as terminate_orphan,
        ):
            replacement = await self.supervisor.replace(
                old.run_id,
                "Recover after supervisor swap",
            )

        inspect_orphan.assert_awaited_once_with(424_242)
        terminate_orphan.assert_awaited_once_with(orphan)
        archived_old = self.store.get(old.run_id)
        self.assertIsNone(archived_old.provider_pid)
        self.assertEqual(archived_old.replaced_by_run_id, replacement.run_id)
        self.assertIn(replacement.run_id, self.supervisor.adapters)
        self.assertEqual(replacement.state, LifecycleState.IDLE)

    async def test_cross_provider_orchestrator_replace_stops_old_pid_both_directions(
        self,
    ) -> None:
        claude = await self.supervisor.start_run(
            agent_id="wiki",
            provider=ProviderKind.CLAUDE,
            role="orchestrator",
            model="fixture-claude",
            effort=None,
            worktree=str(self.worktree),
            prompt="Coordinate the fixture fleet",
        )
        claude_adapter = self.supervisor.adapters[claude.run_id]
        published = self.supervisor.subscribe()

        codex = await self.supervisor.replace(
            claude.run_id,
            "Continue as Codex",
            model="fixture-codex",
            provider=ProviderKind.CODEX,
            effort="high",
        )

        self.assertEqual(codex.provider, ProviderKind.CODEX)
        self.assertEqual(codex.model, "fixture-codex")
        self.assertEqual(codex.effort, "high")
        self.assertTrue(claude_adapter.closed)
        self.assertIsNone(claude_adapter.snapshot().pid)
        self.assertNotIn(claude.run_id, self.supervisor.adapters)
        self.assertIs(
            self.supervisor.adapters[codex.run_id].provider,
            ProviderKind.CODEX,
        )
        model_event = await _wait_for_published(published, "model_changed")
        self.assertEqual(model_event["from_model"], "fixture-claude")
        self.assertEqual(model_event["to_model"], "fixture-codex")
        normalized = self.store.read_normalized_events(codex.run_id)
        self.assertEqual(normalized[-1]["kind"], "model_changed")
        self.assertEqual(normalized[-1]["payload"]["trigger"], "replace")

        codex_adapter = self.supervisor.adapters[codex.run_id]
        claude_again = await self.supervisor.replace(
            codex.run_id,
            "Continue as Claude",
            model="fixture-claude-next",
            provider=ProviderKind.CLAUDE,
            effort=None,
        )

        self.assertEqual(claude_again.provider, ProviderKind.CLAUDE)
        self.assertEqual(claude_again.model, "fixture-claude-next")
        self.assertIsNone(claude_again.effort)
        self.assertTrue(codex_adapter.closed)
        self.assertIsNone(codex_adapter.snapshot().pid)
        self.assertNotIn(codex.run_id, self.supervisor.adapters)
        self.assertIs(
            self.supervisor.adapters[claude_again.run_id].provider,
            ProviderKind.CLAUDE,
        )
        self.supervisor.unsubscribe(published)

    async def test_queue_model_change_persists_then_applies_at_idle_boundary(
        self,
    ) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-56",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Original ticket WIKI-56 prompt",
        )
        await self.supervisor.send_now(record.run_id, "start a turn")
        queued = await self.supervisor.queue_model_change(record.run_id, "fixture-codex-next")
        self.assertEqual(queued, {"status": "queued", "desired_model": "fixture-codex-next"})
        self.assertEqual(self.store.get(record.run_id).desired_model, "fixture-codex-next")

        published = self.supervisor.subscribe()
        adapter = self.supervisor.adapters[record.run_id]
        status = adapter.snapshot()
        adapter._status = AdapterStatus(  # noqa: SLF001 - fixture state sync
            LifecycleState.IDLE,
            status.session_id,
            status.pid,
            generation=status.generation,
        )
        await adapter._events.put(  # noqa: SLF001 - fixture event injection
            ProviderEvent(
                ProviderKind.CODEX,
                {
                    "method": "turn/completed",
                    "params": {"turn": {"status": "completed"}},
                },
                generation=status.generation,
            )
        )

        model_event = await _wait_for_published(published, "model_changed")
        self.assertEqual(model_event["from_model"], "fixture-codex")
        self.assertEqual(model_event["to_model"], "fixture-codex-next")
        replacement = self.store.get(str(model_event["run_id"]))
        self.assertEqual(replacement.model, "fixture-codex-next")
        self.assertIsNone(replacement.desired_model)
        normalized = self.store.read_normalized_events(replacement.run_id)
        self.assertTrue(
            any(event["kind"] == "model_changed" for event in normalized),
            normalized,
        )
        self.supervisor.unsubscribe(published)

    async def test_cancel_model_change_before_idle_prevents_apply(self) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-56-CANCEL",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Original ticket WIKI-56 prompt",
        )
        await self.supervisor.send_now(record.run_id, "start a turn")
        await self.supervisor.queue_model_change(record.run_id, "fixture-codex-next")
        canceled = await self.supervisor.cancel_model_change(record.run_id)
        self.assertEqual(canceled, {"status": "canceled", "desired_model": None})

        adapter = self.supervisor.adapters[record.run_id]
        status = adapter.snapshot()
        adapter._status = AdapterStatus(  # noqa: SLF001 - fixture state sync
            LifecycleState.IDLE,
            status.session_id,
            status.pid,
            generation=status.generation,
        )
        await adapter._events.put(  # noqa: SLF001 - fixture event injection
            ProviderEvent(
                ProviderKind.CODEX,
                {
                    "method": "turn/completed",
                    "params": {"turn": {"status": "completed"}},
                },
                generation=status.generation,
            )
        )
        await asyncio.sleep(0.05)
        current = self.store.get(record.run_id)
        self.assertEqual(current.model, "fixture-codex")
        self.assertIsNone(current.desired_model)
        self.assertIn(record.run_id, self.supervisor.adapters)

    async def test_recovery_resumes_only_current_working_and_idle_with_dead_pid(
        self,
    ) -> None:
        await self.supervisor.close()
        store = RunStore(self.paths)
        records: dict[str, RunRecord] = {}
        for index, state in enumerate(
            (
                LifecycleState.WORKING,
                LifecycleState.IDLE,
                LifecycleState.STARTING,
                LifecycleState.WAITING_APPROVAL,
                LifecycleState.DEAD,
                LifecycleState.COMPLETED,
            ),
            start=1,
        ):
            worktree = self.root / f"recovery-{index}"
            worktree.mkdir()
            record = RunRecord.new(
                agent_id=f"WIKI-R{index}",
                provider=ProviderKind.CODEX if index % 2 else ProviderKind.CLAUDE,
                role="implement",
                model="fixture",
                worktree=str(worktree),
                prompt="fixture",
            )
            record.state = state
            record.provider_session_id = f"session-{index}"
            record.provider_pid = 999_000 + index
            store.create(record)
            records[state.value] = record

        supervisor = Supervisor(
            store,
            FixtureAdapterFactory(FIXTURES, pid=os.getpid()),
            pid_alive=lambda _pid: False,
        )
        try:
            results = await supervisor.recover_on_start()
            by_id = {item["run_id"]: item for item in results}
            self.assertEqual(by_id[records["working"].run_id]["action"], "resume")
            self.assertEqual(by_id[records["idle"].run_id]["action"], "resume")
            self.assertEqual(by_id[records["starting"].run_id]["action"], "block")
            self.assertEqual(
                by_id[records["waiting-approval"].run_id]["action"], "block"
            )
            self.assertEqual(by_id[records["dead"].run_id]["action"], "skip")
            self.assertEqual(by_id[records["completed"].run_id]["action"], "skip")
            self.assertEqual(
                store.get(records["working"].run_id).state, LifecycleState.IDLE
            )
            self.assertEqual(
                store.get(records["idle"].run_id).state, LifecycleState.IDLE
            )
            self.assertEqual(
                store.get(records["waiting-approval"].run_id).state,
                LifecycleState.BLOCKED,
            )
            self.assertEqual(
                store.get(records["starting"].run_id).state,
                LifecycleState.BLOCKED,
            )
        finally:
            await supervisor.close()
        # Keep tearDown from closing the already-closed original twice.
        self.supervisor = Supervisor(self.store, FixtureAdapterFactory(FIXTURES))

    async def test_recovery_rechecks_live_orphan_then_resumes_exact_session(
        self,
    ) -> None:
        await self.supervisor.close()
        record = RunRecord.new(
            agent_id="WIKI-ORPHAN",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        record.state = LifecycleState.WORKING
        record.provider_session_id = "session-exact"
        record.provider_pid = 424_242
        self.store.create(record)
        alive = {"value": True}
        supervisor = Supervisor(
            self.store,
            FixtureAdapterFactory(FIXTURES, pid=os.getpid()),
            pid_alive=lambda _pid: alive["value"],
        )
        try:
            with self.assertRaisesRegex(StoreConflict, "refusing duplicate resume"):
                await supervisor.resume_run(record.run_id)
            with self.assertRaisesRegex(StoreConflict, "refusing false stop"):
                await supervisor.stop(record.run_id)
            first = await supervisor.recover_on_start()
            self.assertEqual(first[0]["action"], "block")
            blocked = self.store.get(record.run_id)
            self.assertEqual(blocked.state, LifecycleState.BLOCKED)
            self.assertEqual(blocked.recovery_from_state, LifecycleState.WORKING)

            alive["value"] = False
            second = await supervisor.recover_on_start()
            self.assertEqual(second[0]["action"], "resume")
            resumed = self.store.get(record.run_id)
            self.assertEqual(resumed.state, LifecycleState.IDLE)
            self.assertEqual(resumed.provider_session_id, "session-exact")
            self.assertIsNone(resumed.recovery_from_state)
            self.assertIn(record.run_id, supervisor.adapters)
        finally:
            await supervisor.close()
        self.supervisor = Supervisor(self.store, FixtureAdapterFactory(FIXTURES))

    async def test_failed_automatic_resume_does_not_loop_and_manual_retry_works(
        self,
    ) -> None:
        await self.supervisor.close()
        record = RunRecord.new(
            agent_id="WIKI-RESUME-FAIL",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        record.state = LifecycleState.WORKING
        record.provider_session_id = "session-retry"
        record.provider_pid = 987_654
        self.store.create(record)
        attempts: list[str] = []

        def factory(_record: RunRecord):
            if not attempts:
                return ResumeFailureAdapter(
                    FIXTURES / "codex_app_server_success.jsonl",
                    FIXTURES / "codex_app_server_control.jsonl",
                    pid=987_654,
                    attempts=attempts,
                )
            return CodexFixtureAdapter(
                FIXTURES / "codex_app_server_success.jsonl",
                FIXTURES / "codex_app_server_control.jsonl",
                pid=987_654,
            )

        self.supervisor = Supervisor(self.store, factory, pid_alive=lambda _pid: False)
        first = await self.supervisor.recover_on_start()
        self.assertEqual(first[0]["action"], "block")
        blocked = self.store.get(record.run_id)
        self.assertEqual(blocked.recovery_from_state, LifecycleState.WORKING)
        self.assertTrue(blocked.automatic_resume_suppressed)
        self.assertEqual(attempts, ["session-retry"])
        self.assertTrue(
            any(
                event["payload"].get("method") == "error"
                for event in self.store.read_raw_events(record.run_id)
            )
        )

        second = await self.supervisor.recover_on_start()
        self.assertEqual(second[0]["action"], "block")
        self.assertEqual(attempts, ["session-retry"])

        resumed = await self.supervisor.resume_run(record.run_id)
        self.assertEqual(resumed.state, LifecycleState.IDLE)
        self.assertFalse(resumed.automatic_resume_suppressed)

    async def test_stable_automatic_resume_clears_crash_loop_guard(self) -> None:
        await self.supervisor.close()
        record = RunRecord.new(
            agent_id="WIKI-RESUME-STABLE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        record.state = LifecycleState.IDLE
        record.provider_session_id = "session-stable"
        record.provider_pid = 987_654
        self.store.create(record)
        self.supervisor = Supervisor(
            self.store,
            FixtureAdapterFactory(FIXTURES, pid=os.getpid()),
            pid_alive=lambda _pid: False,
            recovery_stability_seconds=0,
        )

        first = await self.supervisor.recover_on_start()
        self.assertEqual(first[0]["action"], "resume")
        self.assertTrue(self.store.get(record.run_id).automatic_resume_suppressed)

        second = await self.supervisor.recover_on_start()
        self.assertEqual(second[0]["action"], "retain")
        stable = self.store.get(record.run_id)
        self.assertFalse(stable.automatic_resume_suppressed)
        self.assertIsNone(stable.automatic_resume_guarded_at)

    async def test_reaper_completes_stale_adapterless_run(self) -> None:
        await self.supervisor.close()
        record = RunRecord.new(
            agent_id="WIKI-REAPER",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        record.state = LifecycleState.WORKING
        self.store.create(record)

        with mock.patch.dict(
            os.environ,
            {
                "WIKI_REAPER_INTERVAL_SECONDS": "0",
                "WIKI_REAPER_GRACE_SECONDS": "0.01",
            },
        ):
            self.supervisor = Supervisor(
                self.store,
                FixtureAdapterFactory(FIXTURES, pid=os.getpid()),
                pid_alive=lambda _pid: False,
            )
            first = await self.supervisor.recover_on_start()
            self.assertEqual(first[0]["action"], "block")
            self.assertEqual(
                self.store.get(record.run_id).state,
                LifecycleState.BLOCKED,
            )

            await asyncio.sleep(0.02)

            second = await self.supervisor.recover_on_start()
            self.assertEqual(second[0]["action"], "reap")

        reaped = self.store.get(record.run_id)
        self.assertEqual(reaped.state, LifecycleState.COMPLETED)
        self.assertEqual(reaped.state_reason, "adapter_lost")

    async def test_archive_succeeds_after_reaper_completes_run(self) -> None:
        await self.supervisor.close()
        record = RunRecord.new(
            agent_id="WIKI-REAPER-ARCHIVE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        record.state = LifecycleState.WORKING
        self.store.create(record)

        with mock.patch.dict(
            os.environ,
            {
                "WIKI_REAPER_INTERVAL_SECONDS": "0",
                "WIKI_REAPER_GRACE_SECONDS": "0.01",
            },
        ):
            self.supervisor = Supervisor(
                self.store,
                FixtureAdapterFactory(FIXTURES, pid=os.getpid()),
                pid_alive=lambda _pid: False,
            )
            await self.supervisor.recover_on_start()
            await asyncio.sleep(0.02)
            await self.supervisor.recover_on_start()

        archived = await self.supervisor.archive(record.run_id)

        self.assertEqual(archived.state, LifecycleState.COMPLETED)
        self.assertFalse(self.store.run_dir(record.run_id).exists())
        self.assertFalse(
            (self.paths.registry_path.exists())
            and json.loads(self.paths.registry_path.read_text()).get(
                "WIKI-REAPER-ARCHIVE"
            )
        )

    async def test_reaper_skips_active_quiesce_runs(self) -> None:
        await self.supervisor.close()
        record = RunRecord.new(
            agent_id="WIKI-REAPER-QUIESCE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        record.state = LifecycleState.BLOCKED
        record.provider_session_id = "session-quiesce"
        record.quiesce_operation_id = "00000000-0000-4000-8000-000000000088"
        record.quiesce_resume_state = LifecycleState.WORKING
        record.automatic_resume_suppressed = True
        self.store.create(record)

        with mock.patch.dict(
            os.environ,
            {
                "WIKI_REAPER_INTERVAL_SECONDS": "0",
                "WIKI_REAPER_GRACE_SECONDS": "0",
            },
        ):
            self.supervisor = Supervisor(
                self.store,
                FixtureAdapterFactory(FIXTURES, pid=os.getpid()),
                pid_alive=lambda _pid: False,
            )
            await self.supervisor.recover_on_start()

        guarded = self.store.get(record.run_id)
        self.assertEqual(guarded.state, LifecycleState.BLOCKED)
        self.assertEqual(guarded.quiesce_operation_id, record.quiesce_operation_id)

    async def test_recovery_retains_healthy_attachment_despite_stale_pid(self) -> None:
        await self.supervisor.close()
        self.supervisor = Supervisor(
            self.store,
            FixtureAdapterFactory(FIXTURES, pid=987_654),
            pid_alive=lambda _pid: False,
        )
        record = await self.supervisor.start_run(
            agent_id="WIKI-ATTACHED",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-ATTACHED",
        )
        adapter = self.supervisor.adapters[record.run_id]

        recovery = await self.supervisor.recover_on_start()

        result = next(item for item in recovery if item["run_id"] == record.run_id)
        self.assertEqual(result["action"], "retain")
        self.assertIs(self.supervisor.adapters[record.run_id], adapter)
        with self.assertRaisesRegex(StoreConflict, "already has an attached"):
            await self.supervisor.resume_run(record.run_id)

    async def test_daemon_owned_rotation_quiesces_only_codex_and_resumes_exact_session(
        self,
    ) -> None:
        await self.supervisor.close()
        self.supervisor = Supervisor(
            self.store,
            FixtureAdapterFactory(FIXTURES, pid=987_654),
            pid_alive=lambda _pid: False,
        )
        env = self._seed_accounts()
        codex = await self.supervisor.start_run(
            agent_id="WIKI-CODEX-ROTATE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        claude = await self.supervisor.start_run(
            agent_id="WIKI-CLAUDE-ROTATE",
            provider=ProviderKind.CLAUDE,
            role="review",
            model="fixture-claude",
            effort=None,
            worktree=str(self.worktree),
            prompt="fixture",
        )
        codex_adapter = self.supervisor.adapters[codex.run_id]
        claude_adapter = self.supervisor.adapters[claude.run_id]
        operation_id = "00000000-0000-4000-8000-000000000099"
        tmux_called = AssertionError("headless account rotation touched tmux")

        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(accounts, "codex_login_status", return_value=True),
            mock.patch.object(accounts, "tmux_live_windows", side_effect=tmux_called),
            mock.patch.object(accounts, "tmux_kill_window", side_effect=tmux_called),
        ):
            result = await self.supervisor.request_codex_rotation(
                operation_id=operation_id,
                force_target="beta",
            )

        self.assertEqual(
            result,
            {
                "from": "alpha",
                "to": "beta",
                "revived": ["WIKI-CODEX-ROTATE"],
                "failed": [],
                "failed_reasons": {},
            },
        )
        resumed = self.store.get(codex.run_id)
        self.assertEqual(resumed.provider_session_id, codex.provider_session_id)
        self.assertEqual(resumed.state, LifecycleState.IDLE)
        self.assertIsNone(resumed.quiesce_operation_id)
        self.assertIsNot(self.supervisor.adapters[codex.run_id], codex_adapter)
        assert isinstance(codex_adapter, CodexFixtureAdapter)
        self.assertTrue(codex_adapter.closed)
        self.assertIs(self.supervisor.adapters[claude.run_id], claude_adapter)
        self.assertEqual(
            self.store.get(claude.run_id).provider_session_id,
            claude.provider_session_id,
        )
        with mock.patch.dict(os.environ, env):
            self.assertEqual(accounts.read_state().active, "beta")
            self.assertEqual(
                json.loads(Path(env["WIKI_CODEX_AUTH_PATH"]).read_text())["tokens"],
                "beta",
            )
        journal = self.store.read_codex_rotation_journal()
        assert journal is not None
        self.assertEqual(journal["phase"], "complete")
        self.assertEqual(journal["status"], "succeeded")
        self.assertNotIn("tokens", json.dumps(journal))

    async def test_rate_limit_event_rotates_codex_fleet_and_pins_reset(
        self,
    ) -> None:
        await self.supervisor.close()
        self.supervisor = Supervisor(
            self.store,
            FixtureAdapterFactory(FIXTURES, pid=987_654),
            pid_alive=lambda _pid: False,
        )
        env = self._seed_accounts()
        queue = self.supervisor.subscribe()
        codex = await self.supervisor.start_run(
            agent_id="WIKI-RATE-LIMIT",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        claude = await self.supervisor.start_run(
            agent_id="WIKI-CLAUDE-STABLE",
            provider=ProviderKind.CLAUDE,
            role="review",
            model="fixture-claude",
            effort=None,
            worktree=str(self.worktree),
            prompt="fixture",
        )
        old_adapter = self.supervisor.adapters[codex.run_id]
        payload = {
            "method": "account/rateLimits/updated",
            "params": {
                "rateLimits": {
                    "primary": {"resetsAt": 1_750_001_234},
                    "secondary": {"resetsAt": 1_760_000_000},
                    "rateLimitReachedType": "rate_limit_reached",
                }
            },
        }

        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(accounts, "codex_login_status", return_value=True),
        ):
            await self.supervisor._handle_provider_event(  # noqa: SLF001
                codex.run_id,
                old_adapter,
                ProviderEvent(ProviderKind.CODEX, payload),
            )
            published = await _wait_for_published(queue, "codex_rotation")
            for _ in range(200):
                current = self.store.get(codex.run_id)
                new_adapter = self.supervisor.adapters.get(codex.run_id)
                if (
                    current.state is LifecycleState.IDLE
                    and new_adapter is not None
                    and new_adapter is not old_adapter
                ):
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(accounts.read_state().active, "beta")
            self.assertEqual(
                accounts.read_state().accounts["alpha"]["limit_reset_at"],
                datetime.fromtimestamp(1_750_001_234, tz=timezone.utc).isoformat(),
            )

        self.assertEqual(
            published,
            {
                "type": "codex_rotation",
                "from": "alpha",
                "to": "beta",
                "revived": ["WIKI-RATE-LIMIT"],
                "failed": [],
                "failed_reasons": {},
                "ts": published["ts"],
            },
        )
        self.assertIsNot(self.supervisor.adapters[codex.run_id], old_adapter)
        self.assertIn(claude.run_id, self.supervisor.adapters)
        self.supervisor.unsubscribe(queue)

    async def test_rate_limit_event_without_eligible_account_emits_banner(
        self,
    ) -> None:
        await self.supervisor.close()
        self.supervisor = Supervisor(
            self.store,
            FixtureAdapterFactory(FIXTURES, pid=987_654),
            pid_alive=lambda _pid: False,
        )
        env = self._seed_accounts()
        queue = self.supervisor.subscribe()
        record = await self.supervisor.start_run(
            agent_id="WIKI-NO-ELIGIBLE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        adapter = self.supervisor.adapters[record.run_id]
        payload = {
            "method": "account/rateLimits/updated",
            "params": {
                "rateLimits": {
                    "primary": {"resetsAt": 1_750_009_999},
                    "rateLimitReachedType": "rate_limit_reached",
                }
            },
        }

        with mock.patch.dict(os.environ, env):
            state = accounts.read_state()
            state.accounts.setdefault("beta", {})["limit_reset_at"] = (
                "2099-01-02T00:00:00+00:00"
            )
            accounts.write_state(state)
            await self.supervisor._handle_provider_event(  # noqa: SLF001
                record.run_id,
                adapter,
                ProviderEvent(ProviderKind.CODEX, payload),
            )
            published = await _wait_for_published(queue, "codex_limit_no_eligible")
            self.assertEqual(accounts.read_state().active, "alpha")

        self.assertEqual(published["tickets"], ["WIKI-NO-ELIGIBLE"])
        self.assertEqual(
            published["reset_at"],
            datetime.fromtimestamp(1_750_009_999, tz=timezone.utc).isoformat(),
        )
        self.supervisor.unsubscribe(queue)

    async def test_auth_dead_event_resumes_exact_session_without_rotation(
        self,
    ) -> None:
        queue = self.supervisor.subscribe()
        record = await self.supervisor.start_run(
            agent_id="WIKI-AUTH-DEAD",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        old_adapter = self.supervisor.adapters[record.run_id]

        await self.supervisor._handle_provider_event(  # noqa: SLF001
            record.run_id,
            old_adapter,
            ProviderEvent(
                ProviderKind.CODEX,
                {
                    "method": "error",
                    "params": {
                        "message": (
                            "Your access token could not be refreshed because you have "
                            "since logged out or signed in to another account. "
                            "Please sign in again."
                        ),
                        "willRetry": False,
                    },
                },
            ),
        )
        published = await _wait_for_published(queue, "codex_auth_dead_revival")
        for _ in range(200):
            current = self.store.get(record.run_id)
            new_adapter = self.supervisor.adapters.get(record.run_id)
            if (
                current.state is LifecycleState.IDLE
                and new_adapter is not None
                and new_adapter is not old_adapter
            ):
                break
            await asyncio.sleep(0.01)

        current = self.store.get(record.run_id)
        self.assertEqual(current.provider_session_id, record.provider_session_id)
        self.assertEqual(current.state, LifecycleState.IDLE)
        self.assertIsNot(self.supervisor.adapters[record.run_id], old_adapter)
        self.assertIsInstance(old_adapter, CodexFixtureAdapter)
        old_codex_adapter = cast(CodexFixtureAdapter, old_adapter)
        self.assertTrue(old_codex_adapter.closed)
        self.assertEqual(published["revived"], ["WIKI-AUTH-DEAD"])
        self.assertEqual(published["failed"], [])
        self.supervisor.unsubscribe(queue)

    async def test_auth_dead_exhaustion_alerts_without_restarting_run(self) -> None:
        queue = self.supervisor.subscribe()
        record = await self.supervisor.start_run(
            agent_id="WIKI-AUTH-CAP",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        adapter = self.supervisor.adapters[record.run_id]
        now = time.monotonic()
        self.supervisor.auth_dead_attempts[record.agent_id] = [
            now - 400,
            now - 500,
            now - 600,
        ]

        await self.supervisor._handle_provider_event(  # noqa: SLF001
            record.run_id,
            adapter,
            ProviderEvent(
                ProviderKind.CODEX,
                {
                    "method": "error",
                    "params": {
                        "message": (
                            "Your access token could not be refreshed because you have "
                            "since logged out or signed in to another account. "
                            "Please sign in again."
                        ),
                        "willRetry": False,
                    },
                },
            ),
        )
        published = await _wait_for_published(queue, "codex_auth_dead_exhausted")
        await asyncio.sleep(0.05)

        self.assertEqual(published["tickets"], ["WIKI-AUTH-CAP"])
        self.assertIsInstance(adapter, CodexFixtureAdapter)
        codex_adapter = cast(CodexFixtureAdapter, adapter)
        self.assertIs(self.supervisor.adapters[record.run_id], adapter)
        self.assertFalse(codex_adapter.closed)
        self.supervisor.unsubscribe(queue)

    async def test_claude_limit_event_alerts_once_per_hour(self) -> None:
        queue = self.supervisor.subscribe()
        record = await self.supervisor.start_run(
            agent_id="WIKI-CLAUDE-LIMIT",
            provider=ProviderKind.CLAUDE,
            role="review",
            model="fixture-claude",
            effort=None,
            worktree=str(self.worktree),
            prompt="fixture",
        )
        adapter = self.supervisor.adapters[record.run_id]
        payload = {
            "type": "result",
            "subtype": "error",
            "is_error": True,
            "result": "Claude usage limit reached. Try again at 4pm.",
        }

        await self.supervisor._handle_provider_event(  # noqa: SLF001
            record.run_id,
            adapter,
            ProviderEvent(ProviderKind.CLAUDE, payload),
        )
        first = await _wait_for_published(queue, "claude_limit_hit")
        await self.supervisor._handle_provider_event(  # noqa: SLF001
            record.run_id,
            adapter,
            ProviderEvent(ProviderKind.CLAUDE, payload),
        )

        self.assertEqual(first["ticket"], "WIKI-CLAUDE-LIMIT")
        self.assertEqual(first["window"], "")
        with self.assertRaises(TimeoutError):
            await _wait_for_published(queue, "claude_limit_hit", timeout=0.1)
        self.supervisor.unsubscribe(queue)

    async def test_rotation_failure_restores_auth_and_resumes_quiesced_run(
        self,
    ) -> None:
        await self.supervisor.close()
        self.supervisor = Supervisor(
            self.store,
            FixtureAdapterFactory(FIXTURES, pid=987_654),
            pid_alive=lambda _pid: False,
        )
        env = self._seed_accounts()
        record = await self.supervisor.start_run(
            agent_id="WIKI-ROTATE-ROLLBACK",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        old_adapter = self.supervisor.adapters[record.run_id]
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(accounts, "codex_login_status", return_value=False),
            self.assertRaisesRegex(
                accounts.RotationError,
                "codex login status failed",
            ),
        ):
            await self.supervisor.request_codex_rotation(
                operation_id="00000000-0000-4000-8000-000000000098",
                force_target="beta",
            )

        recovered = self.store.get(record.run_id)
        self.assertEqual(recovered.provider_session_id, record.provider_session_id)
        self.assertEqual(recovered.state, LifecycleState.IDLE)
        self.assertIsNone(recovered.quiesce_operation_id)
        assert isinstance(old_adapter, CodexFixtureAdapter)
        self.assertTrue(old_adapter.closed)
        self.assertIn(record.run_id, self.supervisor.adapters)
        with mock.patch.dict(os.environ, env):
            self.assertEqual(accounts.read_state().active, "alpha")
            self.assertEqual(
                json.loads(Path(env["WIKI_CODEX_AUTH_PATH"]).read_text())["tokens"],
                "alpha-refreshed",
            )
        journal = self.store.read_codex_rotation_journal()
        assert journal is not None
        self.assertEqual(journal["phase"], "complete")
        self.assertEqual(journal["status"], "failed")

    async def test_rotation_journal_rolls_back_and_resumes_after_daemon_restart(
        self,
    ) -> None:
        await self.supervisor.close()
        self.supervisor = Supervisor(
            self.store,
            FixtureAdapterFactory(FIXTURES, pid=987_654),
            pid_alive=lambda _pid: False,
        )
        env = self._seed_accounts()
        record = await self.supervisor.start_run(
            agent_id="WIKI-ROTATE-RESTART",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        operation_id = "00000000-0000-4000-8000-000000000096"
        with mock.patch.dict(os.environ, env):
            previous = accounts.read_state()
            accounts.snapshot_active_auth("alpha")
        journal = {
            "schema_version": 1,
            "operation_id": operation_id,
            "phase": "installing",
            "status": "working",
            "started_at": "2026-07-09T12:00:00+00:00",
            "updated_at": "2026-07-09T12:00:00+00:00",
            "outgoing": "alpha",
            "incoming": "beta",
            "previous_state": previous.to_dict(),
            "committed_state": None,
            "rollback_requires_auth_restore": True,
            "runs": [
                {
                    "run_id": record.run_id,
                    "agent_id": record.agent_id,
                    "session_id": record.provider_session_id,
                    "resume_state": record.state.value,
                    "detached": True,
                    "resumed": False,
                    "skipped": False,
                    "failed_reason": None,
                }
            ],
            "close_only_run_ids": [],
            "result": None,
            "error": None,
        }
        self.store.write_codex_rotation_journal(journal)
        self.store.mark_quiesce_intent(
            record.run_id,
            operation_id,
            record.provider_session_id or "",
        )
        adapter = self.supervisor.adapters[record.run_id]
        async with self.supervisor._run_lock(record.run_id):  # noqa: SLF001
            await self.supervisor._close_and_drain_adapter(  # noqa: SLF001
                record.run_id,
                adapter,
            )
            self.store.finish_provider_detached(
                record.run_id,
                operation_id,
                reason="fixture daemon crash during auth install",
            )
        with mock.patch.dict(os.environ, env):
            self.assertTrue(accounts.install_incoming_auth("beta"))
            accounts.write_state(
                accounts.AccountState(
                    active="beta",
                    accounts={
                        "alpha": {"limit_reset_at": None},
                        "beta": {"limit_reset_at": None},
                    },
                )
            )

        await self.supervisor.close()
        self.supervisor = Supervisor(
            self.store,
            FixtureAdapterFactory(FIXTURES, pid=987_655),
            pid_alive=lambda _pid: False,
        )
        with mock.patch.dict(os.environ, env):
            await self.supervisor.recover_on_start()

        recovered = self.store.get(record.run_id)
        self.assertEqual(recovered.provider_session_id, record.provider_session_id)
        self.assertEqual(recovered.state, LifecycleState.IDLE)
        self.assertIsNone(recovered.quiesce_operation_id)
        self.assertIn(record.run_id, self.supervisor.adapters)
        with mock.patch.dict(os.environ, env):
            self.assertEqual(accounts.read_state().active, "alpha")
            self.assertEqual(
                json.loads(Path(env["WIKI_CODEX_AUTH_PATH"]).read_text())["tokens"],
                "alpha-refreshed",
            )
        recovered_journal = self.store.read_codex_rotation_journal()
        assert recovered_journal is not None
        self.assertEqual(recovered_journal["phase"], "complete")
        self.assertEqual(recovered_journal["status"], "failed")

    async def test_rotation_preflight_blocks_before_mutation_on_approval_or_stale_pid(
        self,
    ) -> None:
        await self.supervisor.close()
        self.supervisor = Supervisor(
            self.store,
            FixtureAdapterFactory(FIXTURES, pid=987_654),
            pid_alive=lambda pid: pid == 777_777,
        )
        env = self._seed_accounts()
        healthy = await self.supervisor.start_run(
            agent_id="WIKI-ROTATE-HEALTHY",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        approval = await self.supervisor.start_run(
            agent_id="WIKI-ROTATE-APPROVAL",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        self.store.transition(approval.run_id, LifecycleState.WAITING_APPROVAL)
        stale = RunRecord.new(
            agent_id="WIKI-ROTATE-STALE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        stale.state = LifecycleState.DEAD
        stale.provider_session_id = "stale-session"
        stale.provider_pid = 777_777
        self.store.create(stale)
        healthy_adapter = self.supervisor.adapters[healthy.run_id]

        with (
            mock.patch.dict(os.environ, env),
            self.assertRaisesRegex(StoreConflict, "not safe to rotate"),
        ):
            await self.supervisor.request_codex_rotation(
                operation_id="00000000-0000-4000-8000-000000000097",
                force_target="beta",
            )

        self.assertIs(self.supervisor.adapters[healthy.run_id], healthy_adapter)
        assert isinstance(healthy_adapter, CodexFixtureAdapter)
        self.assertFalse(healthy_adapter.closed)
        self.assertIsNone(self.store.get(healthy.run_id).quiesce_operation_id)
        self.assertIsNone(self.store.read_codex_rotation_journal())
        with mock.patch.dict(os.environ, env):
            self.assertEqual(accounts.read_state().active, "alpha")

    async def test_raw_persistence_failure_closes_and_blocks_without_recovery(
        self,
    ) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-PERSIST",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-PERSIST",
        )
        await _wait_for_events(self.store, record.run_id, 10)
        adapter = self.supervisor.adapters[record.run_id]
        self.assertIsInstance(adapter, CodexFixtureAdapter)
        assert isinstance(adapter, CodexFixtureAdapter)

        with mock.patch.object(
            self.store,
            "append_raw",
            side_effect=OSError("fixture fsync failed"),
        ):
            await adapter.respond("fixture-request", {"answer": "fixture"})
            for _ in range(200):
                if record.run_id in self.supervisor.pipeline_failures:
                    break
                await asyncio.sleep(0.01)

        self.assertTrue(adapter.closed)
        self.assertNotIn(record.run_id, self.supervisor.adapters)
        self.assertFalse(
            any(key[0] == id(adapter) for key in self.supervisor.event_routes)
        )
        blocked = self.store.get(record.run_id)
        self.assertEqual(blocked.state, LifecycleState.BLOCKED)
        self.assertEqual(
            blocked.state_reason,
            "provider event persistence failed: fixture fsync failed",
        )

        recovery = await self.supervisor.recover_on_start()
        result = next(item for item in recovery if item["run_id"] == record.run_id)
        self.assertEqual(result["action"], "block")
        self.assertNotIn(record.run_id, self.supervisor.adapters)

    async def test_replace_serializes_send_and_interrupt_against_old_run(self) -> None:
        old = await self.supervisor.start_run(
            agent_id="WIKI-RACE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Original WIKI-RACE prompt",
        )
        adapter = self.supervisor.adapters[old.run_id]
        agent_lock = self.supervisor._run_lock(old.run_id)  # noqa: SLF001
        replace_started = asyncio.Event()
        allow_replace = asyncio.Event()
        original_replace = adapter.replace

        async def delayed_replace(
            prompt: str,
            model: str | None = None,
            effort: str | None = None,
        ):
            replace_started.set()
            await allow_replace.wait()
            return await original_replace(prompt, model, effort)

        with (
            mock.patch.object(adapter, "replace", side_effect=delayed_replace),
            mock.patch.object(
                adapter, "send_now", wraps=adapter.send_now
            ) as provider_send,
            mock.patch.object(
                adapter,
                "send_on_idle",
                wraps=adapter.send_on_idle,
            ) as provider_send_on_idle,
            mock.patch.object(
                adapter, "interrupt", wraps=adapter.interrupt
            ) as provider_interrupt,
        ):
            replace_task = asyncio.create_task(
                self.supervisor.replace(old.run_id, "Replacement WIKI-RACE prompt")
            )
            await asyncio.wait_for(replace_started.wait(), timeout=2)
            self.store.queue_message(
                old.run_id,
                "must not reach replacement from idle queue",
            )
            send_task = asyncio.create_task(
                self.supervisor.send_now(old.run_id, "must not reach replacement")
            )
            interrupt_task = asyncio.create_task(self.supervisor.interrupt(old.run_id))
            queued_delivery_task = asyncio.create_task(
                self.supervisor._deliver_next_queued(old.run_id, adapter)  # noqa: SLF001
            )
            await asyncio.sleep(0)
            self.assertFalse(send_task.done())
            self.assertFalse(interrupt_task.done())
            self.assertFalse(queued_delivery_task.done())

            allow_replace.set()
            replacement = await asyncio.wait_for(replace_task, timeout=2)
            control_results = await asyncio.gather(
                send_task,
                interrupt_task,
                return_exceptions=True,
            )
            await queued_delivery_task

        self.assertTrue(
            all(isinstance(item, StoreConflict) for item in control_results)
        )
        provider_send.assert_not_awaited()
        provider_send_on_idle.assert_not_awaited()
        provider_interrupt.assert_not_awaited()
        self.assertNotEqual(replacement.run_id, old.run_id)
        self.assertIs(self.supervisor.adapters[replacement.run_id], adapter)
        self.assertIs(
            self.supervisor._run_lock(replacement.run_id),  # noqa: SLF001
            agent_lock,
        )
        self.assertEqual(len(self.store.get(old.run_id).queued_messages), 1)

    async def test_slow_provider_control_does_not_block_other_runs(self) -> None:
        first = await self.supervisor.start_run(
            agent_id="WIKI-SLOW",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-SLOW",
        )
        second = await self.supervisor.start_run(
            agent_id="WIKI-FAST",
            provider=ProviderKind.CLAUDE,
            role="review",
            model="fixture-claude",
            effort=None,
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-FAST",
        )
        first_adapter = self.supervisor.adapters[first.run_id]
        first_send = first_adapter.send_now
        slow_started = asyncio.Event()
        release_slow = asyncio.Event()

        async def delayed_send(message: str):
            slow_started.set()
            await release_slow.wait()
            return await first_send(message)

        with mock.patch.object(first_adapter, "send_now", side_effect=delayed_send):
            slow_task = asyncio.create_task(
                self.supervisor.send_now(first.run_id, "slow control")
            )
            await asyncio.wait_for(slow_started.wait(), timeout=2)
            fast_result = await asyncio.wait_for(
                self.supervisor.send_now(second.run_id, "independent control"),
                timeout=0.5,
            )
            release_slow.set()
            slow_result = await asyncio.wait_for(slow_task, timeout=2)

        self.assertEqual(fast_result, {"status": "sent"})
        self.assertEqual(slow_result, {"status": "sent"})

    async def test_store_replace_failure_blocks_old_run_and_clears_live_routes(
        self,
    ) -> None:
        old = await self.supervisor.start_run(
            agent_id="WIKI-STORE-FAIL",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Original WIKI-STORE-FAIL prompt",
        )
        adapter = self.supervisor.adapters[old.run_id]
        attempted: dict[str, RunRecord] = {}

        def fail_registry_commit(_old_run_id: str, replacement: RunRecord):
            attempted["replacement"] = replacement
            raise OSError("fixture registry commit failed")

        with mock.patch.object(
            self.store,
            "replace",
            side_effect=fail_registry_commit,
        ):
            with self.assertRaisesRegex(OSError, "registry commit failed"):
                await self.supervisor.replace(
                    old.run_id,
                    "Replacement WIKI-STORE-FAIL prompt",
                )

        blocked = self.store.get(old.run_id)
        self.assertEqual(blocked.state, LifecycleState.DEAD)
        self.assertEqual(
            blocked.state_reason,
            "replacement metadata commit failed: fixture registry commit failed",
        )
        self.assertIsNone(blocked.provider_pid)
        self.assertIsNone(blocked.active_turn_id)
        self.assertEqual(self.store.current_run_id(old.agent_id), old.run_id)
        self.assertNotIn(old.run_id, self.supervisor.adapters)
        self.assertNotIn(old.run_id, self.supervisor.event_tasks)
        self.assertFalse(
            any(key[0] == id(adapter) for key in self.supervisor.event_routes)
        )
        self.assertEqual((await adapter.status()).state, LifecycleState.DEAD)
        replacement = attempted["replacement"]
        self.assertNotEqual(replacement.state, LifecycleState.STARTING)
        self.assertIsNotNone(replacement.provider_session_id)
        self.assertGreater(replacement.provider_generation, 0)
        self.assertIsNotNone(replacement.provider_pid)

    async def test_provider_replace_failure_drains_event_and_keeps_old_identity(
        self,
    ) -> None:
        old = await self.supervisor.start_run(
            agent_id="WIKI-PROVIDER-REPLACE-FAIL",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="original prompt",
        )
        adapter = self.supervisor.adapters[old.run_id]
        self.assertIsInstance(adapter, CodexFixtureAdapter)
        assert isinstance(adapter, CodexFixtureAdapter)

        async def fail_replace(
            _prompt: str,
            _model: str | None = None,
            _effort: str | None = None,
        ):
            await adapter._events.put(  # noqa: SLF001 - failure-drain fixture
                ProviderEvent(
                    ProviderKind.CODEX,
                    {
                        "method": "error",
                        "params": {
                            "message": "fixture replacement failure",
                            "willRetry": False,
                        },
                    },
                    generation=old.provider_generation,
                )
            )
            raise RuntimeError("fixture replacement failure")

        with mock.patch.object(adapter, "replace", side_effect=fail_replace):
            with self.assertRaisesRegex(RuntimeError, "replacement failure"):
                await self.supervisor.replace(old.run_id, "replacement prompt")

        failed = self.store.get(old.run_id)
        self.assertEqual(failed.state, LifecycleState.BLOCKED)
        self.assertEqual(failed.provider_session_id, old.provider_session_id)
        self.assertEqual(failed.transcript_path, old.transcript_path)
        raw_events = self.store.read_raw_events(old.run_id)
        self.assertTrue(
            any(event["payload"].get("method") == "error" for event in raw_events),
            raw_events,
        )
        self.assertNotIn(old.run_id, self.supervisor.adapters)

    async def test_dead_current_run_can_be_replaced_without_resurrecting_it(
        self,
    ) -> None:
        await self.supervisor.close()
        dead = RunRecord.new(
            agent_id="WIKI-DEAD-REPLACE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="failed original",
        )
        dead.state = LifecycleState.DEAD
        dead.provider_session_id = "dead-session"
        self.store.create(dead)
        self.supervisor = Supervisor(
            self.store,
            FixtureAdapterFactory(FIXTURES, pid=987_654),
            pid_alive=lambda _pid: False,
        )

        replacement = await self.supervisor.replace(
            dead.run_id,
            "fresh replacement prompt",
        )

        archived = self.store.get(dead.run_id)
        self.assertEqual(archived.state, LifecycleState.DEAD)
        self.assertEqual(archived.replaced_by_run_id, replacement.run_id)
        self.assertNotEqual(replacement.provider_session_id, "dead-session")
        self.assertEqual(
            self.store.current_run_id(dead.agent_id),
            replacement.run_id,
        )

    async def test_explicit_stop_detaches_then_allows_fresh_replacement(self) -> None:
        original = await self.supervisor.start_run(
            agent_id="WIKI-STOP-REPLACE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="original prompt",
        )
        stopped = await self.supervisor.stop(original.run_id)
        self.assertEqual(stopped.state, LifecycleState.DEAD)
        self.assertNotIn(original.run_id, self.supervisor.adapters)

        replacement = await self.supervisor.replace(
            original.run_id,
            "replacement after stop",
        )
        self.assertNotEqual(replacement.run_id, original.run_id)
        self.assertIn(replacement.run_id, self.supervisor.adapters)

    async def test_failed_stop_control_remains_terminal_and_never_revives(self) -> None:
        original = await self.supervisor.start_run(
            agent_id="WIKI-STOP-FAIL",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        adapter = self.supervisor.adapters[original.run_id]

        with (
            mock.patch.object(
                adapter,
                "stop",
                side_effect=RuntimeError("fixture stop rejection"),
            ),
            self.assertRaisesRegex(RuntimeError, "fixture stop rejection"),
        ):
            await self.supervisor.stop(original.run_id)

        stopped = self.store.get(original.run_id)
        self.assertEqual(stopped.state, LifecycleState.DEAD)
        self.assertEqual(
            stopped.state_reason,
            "provider stop failed: fixture stop rejection",
        )
        self.assertNotIn(original.run_id, self.supervisor.adapters)
        recovery = await self.supervisor.recover_on_start()
        result = next(item for item in recovery if item["run_id"] == original.run_id)
        self.assertEqual(result["action"], "skip")

    async def test_failed_archive_control_remains_completed(self) -> None:
        original = await self.supervisor.start_run(
            agent_id="WIKI-ARCHIVE-FAIL",
            provider=ProviderKind.CLAUDE,
            role="implement",
            model="fixture-claude",
            effort=None,
            worktree=str(self.worktree),
            prompt="fixture",
        )
        adapter = self.supervisor.adapters[original.run_id]

        with (
            mock.patch.object(
                adapter,
                "archive",
                side_effect=RuntimeError("fixture archive rejection"),
            ),
            self.assertRaisesRegex(RuntimeError, "fixture archive rejection"),
        ):
            await self.supervisor.archive(original.run_id)

        archived = self.store.get(original.run_id)
        self.assertEqual(archived.state, LifecycleState.COMPLETED)
        self.assertEqual(
            archived.state_reason,
            "provider archive failed: fixture archive rejection",
        )
        self.assertNotIn(original.run_id, self.supervisor.adapters)

    async def test_archive_finalizes_current_run_into_archive_dir(self) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-ARCHIVE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="fixture prompt",
        )
        self.paths.status_dir.mkdir(parents=True, exist_ok=True)
        (self.paths.status_dir / "WIKI-ARCHIVE.json").write_text(
            json.dumps({"state": "merge-ready", "step": "waiting for merge"}),
            encoding="utf-8",
        )
        self.supervisor.detached_at_monotonic[record.run_id] = time.monotonic()

        archived = await self.supervisor.archive(record.run_id)

        self.assertEqual(archived.state, LifecycleState.COMPLETED)
        self.assertFalse(self.store.run_dir(record.run_id).exists())
        self.assertNotIn("WIKI-ARCHIVE", json.loads(self.paths.registry_path.read_text()))
        session_dirs = sorted((self.paths.archive_dir / "WIKI-ARCHIVE").iterdir())
        self.assertEqual(len(session_dirs), 1)
        session_dir = session_dirs[0]
        self.assertTrue((session_dir / "run.json").is_file())
        self.assertTrue((session_dir / "raw.jsonl").is_file())
        self.assertTrue((session_dir / "events.jsonl").is_file())
        self.assertTrue((session_dir / "cdx-WIKI-ARCHIVE.log").is_file())
        archived_prompt = (session_dir / "cdx-WIKI-ARCHIVE-prompt.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"run_id={record.run_id}", archived_prompt)
        self.assertTrue(archived_prompt.endswith("fixture prompt"))
        meta = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["worker"]["run_id"], record.run_id)
        final_status = json.loads(
            (session_dir / "final-status.json").read_text(encoding="utf-8")
        )
        self.assertEqual(final_status["state"], "merge-ready")
        self.assertNotIn(record.run_id, self.supervisor.adapters)
        self.assertNotIn(record.run_id, self.supervisor.detached_at_monotonic)

    async def test_archive_allows_detached_dead_run(self) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-ARCHIVE-DEAD",
            provider=ProviderKind.CLAUDE,
            role="implement",
            model="fixture-claude",
            effort=None,
            worktree=str(self.worktree),
            prompt="fixture",
        )
        await self.supervisor.stop(record.run_id)
        self.supervisor.detached_at_monotonic[record.run_id] = time.monotonic()

        archived = await self.supervisor.archive(record.run_id)

        self.assertEqual(archived.state, LifecycleState.DEAD)
        self.assertFalse(self.store.run_dir(record.run_id).exists())
        self.assertNotIn(record.run_id, self.supervisor.detached_at_monotonic)
        self.assertFalse(
            (self.paths.registry_path.exists())
            and json.loads(self.paths.registry_path.read_text()).get("WIKI-ARCHIVE-DEAD")
        )

    async def test_provider_parent_pid_reports_real_orphan(self) -> None:
        child_pid = await self._spawn_orphan_process()
        try:
            self.assertEqual(await provider_parent_pid(child_pid), 1)
            self.assertTrue(await provider_pid_is_orphan(child_pid))
        finally:
            await self._cleanup_process(child_pid)

    async def test_archive_allows_detached_live_orphan_run(self) -> None:
        await self.supervisor.close()
        self.supervisor = Supervisor(
            self.store,
            FixtureAdapterFactory(FIXTURES),
        )
        child_pid = await self._spawn_orphan_process(ignore_sigterm=True)
        record = RunRecord.new(
            agent_id="WIKI-ARCHIVE-ORPHAN",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        record.state = LifecycleState.BLOCKED
        record.state_reason = "provider PID is live but its control channel is not attached"
        record.provider_session_id = "session-orphan"
        record.provider_pid = child_pid
        self.store.create(record)
        try:
            archived = await self.supervisor.archive(record.run_id)
        finally:
            if await provider_process_status(child_pid) is not None:
                await self._cleanup_process(child_pid)

        self.assertEqual(archived.state, LifecycleState.COMPLETED)
        self.assertIsNone(archived.provider_pid)
        self.assertFalse(self.store.run_dir(record.run_id).exists())
        self.assertFalse(
            (self.paths.registry_path.exists())
            and json.loads(self.paths.registry_path.read_text()).get(
                "WIKI-ARCHIVE-ORPHAN"
            )
        )
        self.assertIsNone(await provider_process_status(child_pid))

    async def test_archive_refuses_detached_live_non_orphan_run(self) -> None:
        await self.supervisor.close()
        self.supervisor = Supervisor(
            self.store,
            FixtureAdapterFactory(FIXTURES),
        )
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        record = RunRecord.new(
            agent_id="WIKI-ARCHIVE-LIVE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        record.state = LifecycleState.BLOCKED
        record.state_reason = "provider PID is live but its control channel is not attached"
        record.provider_session_id = "session-live"
        record.provider_pid = process.pid
        self.store.create(record)

        try:
            with self.assertRaisesRegex(
                StoreConflict,
                "provider PID is live without attached control; refusing false archive",
            ):
                await self.supervisor.archive(record.run_id)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)

        blocked = self.store.get(record.run_id)
        self.assertEqual(blocked.state, LifecycleState.BLOCKED)
        self.assertEqual(blocked.provider_pid, process.pid)

    async def test_archive_marks_orphan_kill_failed_and_clears_pid(self) -> None:
        await self.supervisor.close()
        self.supervisor = Supervisor(
            self.store,
            FixtureAdapterFactory(FIXTURES),
            pid_alive=lambda _pid: True,
        )
        record = RunRecord.new(
            agent_id="WIKI-ARCHIVE-KILL-FAIL",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        record.state = LifecycleState.BLOCKED
        record.state_reason = "provider PID is live but its control channel is not attached"
        record.provider_session_id = "session-kill-fail"
        record.provider_pid = 424_244
        self.store.create(record)

        orphan = ProviderProcessStatus(
            pid=424_244,
            parent_pid=1,
            created_at=1_783_718_400.125,
            process_group_id=424_244,
        )
        with (
            mock.patch.object(
                self.supervisor,
                "_orphaned_provider_process",
                new=mock.AsyncMock(return_value=orphan),
            ),
            mock.patch.object(
                self.supervisor,
                "_terminate_orphan_provider_pid",
                new=mock.AsyncMock(return_value=False),
            ),
            self.assertRaisesRegex(
                StoreConflict,
                "orphan provider PID remained live after forced archive cleanup",
            ),
        ):
            await self.supervisor.archive(record.run_id)

        blocked = self.store.get(record.run_id)
        self.assertEqual(blocked.state, LifecycleState.BLOCKED)
        self.assertEqual(blocked.state_reason, "orphan_kill_failed")
        self.assertIsNone(blocked.provider_pid)

    async def test_codex_fake_exercises_start_steer_and_targeted_interrupt(
        self,
    ) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-42",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-42",
        )
        adapter = self.supervisor.adapters[record.run_id]
        self.assertIsInstance(adapter, CodexFixtureAdapter)
        assert isinstance(adapter, CodexFixtureAdapter)
        started = await self.supervisor.send_now(record.run_id, "begin a long turn")
        self.assertEqual(started, {"status": "sent"})
        status = await adapter.status()
        self.assertEqual(status.state, LifecycleState.WORKING)
        self.assertIsNotNone(status.active_turn_id)

        await self.supervisor.send_now(record.run_id, "steer the active turn")
        await self.supervisor.interrupt(record.run_id)
        self.assertEqual(
            adapter.replayed_methods[-3:],
            ["turn/start", "turn/steer", "turn/interrupt"],
        )

    async def test_claude_interruption_result_stays_interrupted(self) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-CLAUDE",
            provider=ProviderKind.CLAUDE,
            role="implement",
            model="fixture-claude",
            effort=None,
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-CLAUDE",
        )
        record = await _wait_for_events(self.store, record.run_id, 10)
        before = record.raw_event_count
        await self.supervisor.interrupt(record.run_id)
        await _wait_for_events(self.store, record.run_id, before + 1)
        self.assertEqual(
            self.store.get(record.run_id).state, LifecycleState.INTERRUPTED
        )

    async def test_unexpected_stream_end_preserves_state_for_exact_resume(self) -> None:
        await self.supervisor.close()
        self.supervisor = Supervisor(
            self.store,
            FixtureAdapterFactory(FIXTURES, pid=987_654),
            pid_alive=lambda _pid: False,
            adapter_detach_grace_seconds=0.1,
        )
        record = await self.supervisor.start_run(
            agent_id="WIKI-STREAM",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="Work on ticket WIKI-STREAM",
        )
        await _wait_for_events(self.store, record.run_id, 10)
        registry = json.loads(self.paths.registry_path.read_text(encoding="utf-8"))
        self.assertTrue(registry["WIKI-STREAM"]["current"]["control_attached"])
        adapter = self.supervisor.adapters[record.run_id]
        await adapter.close()
        for _ in range(200):
            if record.run_id not in self.supervisor.adapters:
                break
            await asyncio.sleep(0.01)
        self.assertNotIn(record.run_id, self.supervisor.adapters)
        self.assertIn(record.run_id, self.supervisor.detached_at_monotonic)
        registry = json.loads(self.paths.registry_path.read_text(encoding="utf-8"))
        self.assertFalse(registry["WIKI-STREAM"]["current"]["control_attached"])
        interrupted = self.store.get(record.run_id)
        self.assertEqual(interrupted.state, LifecycleState.IDLE)
        self.assertEqual(interrupted.state_reason, "provider event stream ended")

        delayed_recovery = await self.supervisor.recover_on_start()
        delayed = next(
            item for item in delayed_recovery if item["run_id"] == record.run_id
        )
        self.assertEqual(delayed["action"], "block")
        self.assertEqual(delayed["reason"], "provider control channel recently detached")
        await asyncio.sleep(0.11)
        recovery = await self.supervisor.recover_on_start()
        result = next(item for item in recovery if item["run_id"] == record.run_id)
        self.assertEqual(result["action"], "resume")
        self.assertIn(record.run_id, self.supervisor.adapters)
        guarded = self.store.get(record.run_id)
        self.assertTrue(guarded.automatic_resume_suppressed)
        self.assertIsNotNone(guarded.automatic_resume_guarded_at)

        resumed_adapter = self.supervisor.adapters[record.run_id]
        await resumed_adapter.close()
        for _ in range(200):
            if record.run_id not in self.supervisor.adapters:
                break
            await asyncio.sleep(0.01)
        crash_loop_check = await self.supervisor.recover_on_start()
        retry = next(
            item for item in crash_loop_check if item["run_id"] == record.run_id
        )
        self.assertEqual(retry["action"], "block")
        self.assertNotIn(record.run_id, self.supervisor.adapters)

    def test_safe_worktree_refuses_home_relative_missing(self) -> None:
        self.assertEqual(
            resolve_safe_worktree(str(self.worktree)), self.worktree.resolve()
        )
        with self.assertRaisesRegex(ValueError, "absolute"):
            resolve_safe_worktree("relative/path")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            resolve_safe_worktree(str(self.root / "missing"))
        with self.assertRaisesRegex(ValueError, r"\$HOME"):
            resolve_safe_worktree(str(Path.home()))


class UnixClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.paths = _paths(self.root)
        self.store = RunStore(self.paths)
        self.supervisor = Supervisor(self.store, FixtureAdapterFactory(FIXTURES))
        self.server = UnixSupervisorServer(self.supervisor, self.paths.socket_path)
        await self.server.start()
        self.client = SupervisorClient(self.paths, timeout=2)

    async def asyncTearDown(self) -> None:
        await self.server.close()
        await self.supervisor.close()
        self.tmp.cleanup()

    async def test_client_round_trip_message_contract_and_event_subscription(
        self,
    ) -> None:
        ping = await asyncio.to_thread(self.client.ping)
        self.assertEqual(ping["status"], "ok")
        self.assertIsInstance(ping["runtime_fingerprint"], str)
        self.assertEqual(self.paths.socket_path.stat().st_mode & 0o777, 0o600)

        stream = self.client.subscribe_events()
        next_event = asyncio.ensure_future(anext(stream))
        for _ in range(100):
            if self.supervisor.subscribers:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.supervisor.subscribers)
        started = await asyncio.to_thread(
            self.client.request,
            "run/start",
            {
                "agent_id": "WIKI-42",
                "provider": "codex",
                "role": "implement",
                "model": "fixture-codex",
                "effort": "high",
                "worktree": str(self.worktree),
                "prompt": "Work on ticket WIKI-42",
            },
        )
        event = await asyncio.wait_for(next_event, timeout=3)
        self.assertEqual(
            event, {"type": "agents", "tickets": ["WIKI-42"], "surface": "agents"}
        )
        self.assertNotIn("initial_prompt", started)

        sent = await asyncio.to_thread(
            self.client.send_message, "WIKI-42", "now", "now"
        )
        self.assertEqual(sent, {"status": "sent"})
        queued = await asyncio.to_thread(
            self.client.send_message,
            "WIKI-42",
            "later",
            "on-idle",
        )
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["messages"][0]["text"], "later")
        listed_queue = await asyncio.to_thread(self.client.queue, "WIKI-42")
        self.assertEqual(listed_queue, {"messages": queued["messages"]})
        emptied = await asyncio.to_thread(
            self.client.delete_queued,
            "WIKI-42",
            0,
        )
        self.assertEqual(emptied, {"messages": []})
        with self.assertRaises(SupervisorRemoteError) as missing:
            await asyncio.to_thread(self.client.delete_queued, "WIKI-42", 0)
        self.assertEqual(missing.exception.error_type, "RunNotFound")

        runs = await asyncio.to_thread(self.client.request, "run/list")
        current = next(
            run for run in runs["runs"] if run["run_id"] == started["run_id"]
        )
        self.assertTrue(current["control_attached"])
        self.assertTrue(current["provider_alive"])
        await stream.aclose()

    def test_default_client_timeout_is_lane_aware_and_retry_safe(self) -> None:
        client = SupervisorClient(self.paths)
        for method in {
            "run/start",
            "run/replace",
            "run/stop",
            "run/resume",
            "run/interrupt",
            "run/archive",
            "run/respond",
        }:
            self.assertEqual(client._timeout_for(method), 30.0)  # noqa: SLF001
        for method in {"ping", "run/list", "run/status", "run/queue", "events/read"}:
            self.assertEqual(client._timeout_for(method), 1.0)  # noqa: SLF001
        connection = mock.Mock()
        connection.connect.side_effect = socket.timeout()
        with mock.patch(
            "backend.app.agent_runtime.client.socket.socket",
            return_value=connection,
        ):
            with self.assertRaises(SupervisorUnavailable) as timed_out:
                client.request("run/start", {"agent_id": "WIKI-TIMEOUT"})

        self.assertEqual(connection.settimeout.call_args.args, (30.0,))
        self.assertIn("may have succeeded", str(timed_out.exception))
        self.assertIn("Check GET /api/agents", str(timed_out.exception))

        read_connection = mock.Mock()
        read_connection.connect.side_effect = socket.timeout()
        with mock.patch(
            "backend.app.agent_runtime.client.socket.socket",
            return_value=read_connection,
        ):
            with self.assertRaises(SupervisorUnavailable):
                client.request("run/status", {"agent_id": "WIKI-TIMEOUT"})
        self.assertEqual(read_connection.settimeout.call_args.args, (1.0,))

    async def test_server_accepts_existing_prompt_contract_above_default_reader_limit(
        self,
    ) -> None:
        started = await asyncio.to_thread(
            self.client.request,
            "run/start",
            {
                "agent_id": "WIKI-LARGE",
                "provider": "codex",
                "role": "implement",
                "model": "fixture-codex",
                "effort": "high",
                "worktree": str(self.worktree),
                "prompt": "x" * 70_000,
            },
        )
        self.assertEqual(started["agent_id"], "WIKI-LARGE")

    async def test_socket_refuses_to_replace_regular_file(self) -> None:
        await self.server.close()
        self.paths.socket_path.write_text("not a socket", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "non-socket"):
            await self.server.start()

    async def test_second_server_refuses_to_unlink_active_socket(self) -> None:
        second = UnixSupervisorServer(self.supervisor, self.paths.socket_path)
        with self.assertRaisesRegex(RuntimeError, "already active"):
            await second.start()
        self.assertTrue(self.paths.socket_path.exists())

    def test_client_replaces_a_supervisor_from_an_older_runtime(self) -> None:
        client = SupervisorClient(
            self.paths,
            timeout=0.1,
            runtime_fingerprint="current-runtime",
        )
        stale = {"status": "ok", "pid": 424_242}
        current = {
            "status": "ok",
            "pid": 424_243,
            "runtime_fingerprint": "current-runtime",
        }
        with (
            mock.patch.object(
                client,
                "ping",
                side_effect=[stale, SupervisorUnavailable("stopped"), current],
            ),
            mock.patch.object(
                client,
                "request",
                return_value={"status": "ok", "runs": []},
            ) as request,
            mock.patch.object(client, "_spawn_detached") as spawn,
            mock.patch("backend.app.agent_runtime.client.os.kill") as kill,
        ):
            self.assertEqual(client.ensure_running(timeout=0.1), current)
        request.assert_called_once_with("run/list")
        kill.assert_called_once_with(424_242, signal.SIGTERM)
        spawn.assert_called_once_with()

    def test_client_migrates_idle_and_working_runs_across_runtime_swap(self) -> None:
        client = SupervisorClient(
            self.paths,
            timeout=0.1,
            runtime_fingerprint="current-runtime",
            swap_drain_seconds=0,
        )
        idle = {
            "agent_id": "WIKI-IDLE",
            "run_id": "idle-run",
            "role": "implement",
            "state": "idle",
            "provider_session_id": "idle-session",
            "provider_pid": 424_250,
            "control_attached": True,
            "transcript_path": "/isolated/idle.jsonl",
        }
        working = {
            "agent_id": "wiki",
            "run_id": "working-run",
            "role": "orchestrator",
            "state": "working",
            "provider_session_id": "working-session",
            "provider_pid": 424_251,
            "active_turn_id": "turn-1",
            "control_attached": True,
            "transcript_path": "/isolated/working.jsonl",
        }
        calls: list[tuple[str, dict[str, Any]]] = []

        def request(method: str, params: dict[str, Any] | None = None) -> Any:
            values = dict(params or {})
            calls.append((method, values))
            if method == "run/list":
                return {"runs": [idle, working]}
            if method == "run/status":
                if values.get("agent_id") == "WIKI-IDLE":
                    return idle
                if values.get("agent_id") == "wiki":
                    return working
                return {**working, "state": "interrupted", "active_turn_id": None}
            if method in {"run/interrupt", "run/stop", "run/replace"}:
                return {"status": "ok"}
            raise AssertionError(method)

        stale = {
            "status": "ok",
            "pid": 424_242,
            "runtime_fingerprint": "old-runtime",
        }
        current = {
            "status": "ok",
            "pid": 424_243,
            "runtime_fingerprint": "current-runtime",
        }
        with (
            mock.patch.object(
                client,
                "ping",
                side_effect=[stale, SupervisorUnavailable("stopped"), current],
            ),
            mock.patch.object(client, "request", side_effect=request),
            mock.patch.object(client, "_spawn_detached") as spawn,
            mock.patch("backend.app.agent_runtime.client.os.kill"),
        ):
            self.assertEqual(client.ensure_running(timeout=0.1), current)

        spawn.assert_called_once_with()
        self.assertEqual(
            [values["run_id"] for method, values in calls if method == "run/stop"],
            ["idle-run", "working-run"],
        )
        self.assertEqual(
            [values["run_id"] for method, values in calls if method == "run/interrupt"],
            ["working-run"],
        )
        replacements = [values for method, values in calls if method == "run/replace"]
        self.assertEqual(
            [values["run_id"] for values in replacements],
            ["idle-run", "working-run"],
        )
        self.assertIn("idle-session", replacements[0]["prompt"])
        self.assertIn("working-session", replacements[1]["prompt"])
        self.assertIn(str(self.paths.status_dir), replacements[0]["prompt"])

    def test_client_matching_fingerprint_does_not_inspect_or_replace_runs(self) -> None:
        client = SupervisorClient(
            self.paths,
            runtime_fingerprint="current-runtime",
        )
        current = {
            "status": "ok",
            "pid": 424_243,
            "runtime_fingerprint": "current-runtime",
        }
        with (
            mock.patch.object(client, "ping", return_value=current),
            mock.patch.object(client, "request") as request,
            mock.patch.object(client, "_spawn_detached") as spawn,
        ):
            self.assertEqual(client.ensure_running(), current)
        request.assert_not_called()
        spawn.assert_not_called()

    def test_client_does_not_replace_an_old_runtime_when_autostart_is_off(
        self,
    ) -> None:
        client = SupervisorClient(
            self.paths,
            timeout=0.1,
            runtime_fingerprint="current-runtime",
        )
        with (
            mock.patch.object(
                client,
                "ping",
                return_value={
                    "pid": 424_242,
                    "runtime_fingerprint": "old-runtime",
                },
            ),
            mock.patch.object(client, "_spawn_detached") as spawn,
            mock.patch("backend.app.agent_runtime.client.os.kill") as kill,
            mock.patch.dict(os.environ, {"WIKI_SUPERVISOR_AUTOSTART": "off"}),
            self.assertRaisesRegex(SupervisorUnavailable, "does not match"),
        ):
            client.ensure_running(timeout=0.1)
        kill.assert_not_called()
        spawn.assert_not_called()

    def test_client_accepts_an_unversioned_external_supervisor(self) -> None:
        client = SupervisorClient(
            self.paths,
            timeout=0.1,
            runtime_fingerprint="current-runtime",
        )
        health = {"status": "ok", "pid": 424_242}
        with (
            mock.patch.object(client, "ping", return_value=health),
            mock.patch.dict(os.environ, {"WIKI_SUPERVISOR_AUTOSTART": "off"}),
        ):
            self.assertEqual(client.ensure_running(timeout=0.1), health)


class DaemonProcessTests(unittest.TestCase):
    def test_daemon_process_owns_fake_run_without_tmux(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            worktree = root / "worktree"
            worktree.mkdir()
            env = os.environ.copy()
            env["TMUX"] = ""
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "backend.app.agent_runtime.daemon",
                    "--runtime-dir",
                    str(paths.runtime_dir),
                    "--socket",
                    str(paths.socket_path),
                    "--registry",
                    str(paths.registry_path),
                    "--fake-fixture-dir",
                    str(FIXTURES),
                ],
                cwd=Path(__file__).resolve().parents[2],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            client = SupervisorClient(paths, timeout=1)
            try:
                deadline = time.monotonic() + 5
                while True:
                    try:
                        self.assertEqual(client.ping()["status"], "ok")
                        break
                    except SupervisorUnavailable:
                        if time.monotonic() >= deadline:
                            stderr = (
                                process.stderr.read()
                                if process.poll() is not None and process.stderr
                                else "process still running without a socket"
                            )
                            self.fail(f"daemon did not start: {stderr}")
                        time.sleep(0.05)
                started = client.request(
                    "run/start",
                    {
                        "agent_id": "WIKI-42",
                        "provider": "codex",
                        "role": "implement",
                        "model": "fixture-codex",
                        "effort": "high",
                        "worktree": str(worktree),
                        "prompt": "Work on ticket WIKI-42",
                    },
                )
                self.assertEqual(started["state"], "idle")
                self.assertTrue(paths.registry_path.is_file())
                registry = json.loads(paths.registry_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    registry["WIKI-42"]["current"]["run_id"], started["run_id"]
                )
            finally:
                process.terminate()
                process.wait(timeout=5)
                if process.stderr:
                    process.stderr.close()
            self.assertFalse(paths.socket_path.exists())
            self.assertFalse(paths.pid_path.exists())

    def test_fingerprint_swap_replaces_live_runs_under_fresh_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            worktree = root / "worktree"
            worktree.mkdir()
            old_checkout = root / "old-checkout"
            shutil.copytree(
                Path(__file__).resolve().parents[1],
                old_checkout / "backend",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            old_version = old_checkout / "backend/app/agent_runtime/version.py"
            old_version.write_text(
                old_version.read_text(encoding="utf-8") + "\n# old-runtime-fixture\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            env.update(
                {
                    "TMUX": "",
                    "WIKI_AGENT_ARCHIVE_DIR": str(paths.archive_dir),
                    "WIKI_AGENT_STATUS_DIR": str(paths.status_dir),
                    "WIKI_SUPERVISOR_FAKE_FIXTURE_DIR": str(FIXTURES),
                }
            )
            old_process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "backend.app.agent_runtime.daemon",
                    "--runtime-dir",
                    str(paths.runtime_dir),
                    "--socket",
                    str(paths.socket_path),
                    "--registry",
                    str(paths.registry_path),
                    "--fake-fixture-dir",
                    str(FIXTURES),
                ],
                cwd=old_checkout,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            client = SupervisorClient(
                paths,
                timeout=1,
                runtime_fingerprint=RUNTIME_FINGERPRINT,
                swap_drain_seconds=0.1,
            )
            new_pid: int | None = None
            try:
                deadline = time.monotonic() + 5
                while True:
                    try:
                        old_health = client.ping()
                        break
                    except SupervisorUnavailable:
                        if time.monotonic() >= deadline:
                            stderr = (
                                old_process.stderr.read()
                                if old_process.poll() is not None and old_process.stderr
                                else "old daemon still starting"
                            )
                            self.fail(f"old daemon did not start: {stderr}")
                        time.sleep(0.05)
                self.assertNotEqual(
                    old_health["runtime_fingerprint"],
                    RUNTIME_FINGERPRINT,
                )
                old_runs = []
                for agent_id in ("WIKI-IDLE", "WIKI-WORKING"):
                    old_runs.append(
                        client.request(
                            "run/start",
                            {
                                "agent_id": agent_id,
                                "provider": "codex",
                                "role": "implement",
                                "model": "fixture-codex",
                                "effort": "high",
                                "worktree": str(worktree),
                                "prompt": f"Work on {agent_id}",
                            },
                        )
                    )
                client.request(
                    "run/send_now",
                    {"agent_id": "WIKI-WORKING", "text": "keep working"},
                )
                self.assertEqual(
                    client.request("run/status", {"agent_id": "WIKI-WORKING"})[
                        "state"
                    ],
                    "working",
                )

                with (
                    mock.patch.dict(os.environ, env, clear=False),
                    warnings.catch_warnings(),
                ):
                    warnings.simplefilter("ignore", ResourceWarning)
                    current = client.ensure_running(timeout=5)
                new_pid = cast(int, current["pid"])
                self.assertNotEqual(new_pid, old_process.pid)
                old_process.wait(timeout=5)

                listed = client.request("run/list")["runs"]
                replacements = {
                    run["replaces_run_id"]: run
                    for run in listed
                    if run.get("replaces_run_id")
                }
                self.assertEqual(
                    set(replacements),
                    {run["run_id"] for run in old_runs},
                )
                for replacement in replacements.values():
                    self.assertTrue(replacement["control_attached"])
                    self.assertEqual(replacement["provider_pid"], new_pid)
                    client.request(
                        "run/send_now",
                        {
                            "agent_id": replacement["agent_id"],
                            "text": "post-swap steering",
                        },
                    )
                    post_swap = client.request(
                        "run/status", {"agent_id": replacement["agent_id"]}
                    )
                    self.assertTrue(post_swap["control_attached"])
                    self.assertIn(post_swap["state"], {"idle", "working"})
            finally:
                if new_pid is not None:
                    try:
                        os.kill(new_pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    deadline = time.monotonic() + 5
                    while paths.socket_path.exists() and time.monotonic() < deadline:
                        time.sleep(0.05)
                if old_process.poll() is None:
                    old_process.terminate()
                    old_process.wait(timeout=5)
                if old_process.stderr:
                    old_process.stderr.close()


if __name__ == "__main__":
    unittest.main()
