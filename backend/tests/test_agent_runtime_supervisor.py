from __future__ import annotations

import asyncio
import fcntl
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
from backend.app.account_notices import AccountNoticeStore
from backend.app import provider_health
from backend.app.agent_runtime.client import (
    SupervisorClient,
    SupervisorRemoteError,
    SupervisorUnavailable,
)
from backend.app.agent_runtime.command_log import AgentCommand, CommandRetryable
from backend.app.agent_runtime import daemon as agent_daemon
from backend.app.agent_runtime import supervisor as supervisor_module
from backend.app.agent_runtime.claude import ClaudeStreamAdapter
from backend.app.agent_runtime.codex import CodexAppServerAdapter
from backend.app.agent_runtime.fake import (
    ClaudeFixtureAdapter,
    CodexFixtureAdapter,
    FixtureAdapterFactory,
)
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
    ProviderAdapter,
    ProviderBusy,
    ProviderEvent,
    ProviderProcessError,
    StartRequest,
)
from backend.app.agent_runtime.store import RunNotFound, RunStore, RuntimePaths, StoreConflict
from backend.app.agent_runtime.supervisor import Supervisor, resolve_safe_worktree
from backend.app.agent_runtime.types import (
    EventDisposition,
    MAX_MESSAGE_DEDUPE_KEYS,
    LifecycleState,
    ProviderKind,
    RunRecord,
    restart_recovery_decision,
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


class ApprovalRecoveryStallAdapter(CodexFixtureAdapter):
    """Accept the continuation but never emit a replacement approval."""


class ApprovalRecoveryAdapter(CodexFixtureAdapter):
    def __init__(self, *args, resumed_sessions: list[str], **kwargs):
        super().__init__(*args, **kwargs)
        self.resumed_sessions = resumed_sessions

    async def resume(self, session_id: str) -> AdapterStatus:
        self.resumed_sessions.append(session_id)
        return await super().resume(session_id)

    async def send_now(self, message: str) -> AdapterStatus:
        del message
        status = self.snapshot()
        self._status = AdapterStatus(  # noqa: SLF001 - approval recovery fixture
            LifecycleState.WAITING_APPROVAL,
            status.session_id,
            status.pid,
            generation=max(1, status.generation),
        )
        await self._events.put(  # noqa: SLF001 - approval recovery fixture
            ProviderEvent(
                ProviderKind.CODEX,
                {
                    "id": 8,
                    "method": "item/tool/requestUserInput",
                    "params": {
                        "questions": [{"id": "surface", "question": "Which surface?"}]
                    },
                },
                generation=self._status.generation,  # noqa: SLF001
            )
        )
        return self._status


class HandoverStopEventCodexAdapter(CodexFixtureAdapter):
    """Replay the Codex interrupt completion emitted while handover stops it."""

    stop_result = "interrupted"

    async def stop(self) -> AdapterStatus:
        status = self.snapshot()
        if status.state in {
            LifecycleState.WORKING,
            LifecycleState.WAITING_APPROVAL,
        }:
            if self.stop_result in {"approval", "approval_then_completed"}:
                await self.emit_approval()
                if self.stop_result == "approval_then_completed":
                    await self._events.put(  # noqa: SLF001 - stop-event fixture
                        ProviderEvent(
                            ProviderKind.CODEX,
                            {
                                "method": "turn/completed",
                                "params": {
                                    "turn": {
                                        "id": status.active_turn_id or "handover-turn",
                                        "status": "completed",
                                    }
                                },
                            },
                            generation=status.generation,
                        )
                    )
            else:
                await self._events.put(  # noqa: SLF001 - stop-event fixture
                    ProviderEvent(
                        ProviderKind.CODEX,
                        {
                            "method": "turn/completed",
                            "params": {
                                "turn": {
                                    "id": status.active_turn_id or "handover-turn",
                                    "status": self.stop_result,
                                }
                            },
                        },
                        generation=status.generation,
                    )
                )
        return await super().stop()

    async def emit_approval(self) -> None:
        status = self.snapshot()
        self._status = AdapterStatus(  # noqa: SLF001 - handover fixture state
            LifecycleState.WAITING_APPROVAL,
            status.session_id,
            status.pid,
            generation=max(1, status.generation),
            active_turn_id=status.active_turn_id or "approval-turn",
        )
        await self._events.put(  # noqa: SLF001 - approval handover fixture
            ProviderEvent(
                ProviderKind.CODEX,
                {
                    "id": "handover-approval",
                    "method": "item/tool/requestUserInput",
                    "params": {
                        "questions": [
                            {"id": "surface", "question": "Which surface?"}
                        ]
                    },
                },
                generation=self._status.generation,  # noqa: SLF001
            )
        )

    async def send_now(self, message: str) -> AdapterStatus:
        if "Recreate the exact approval question" in message:
            await self.emit_approval()
            return self.snapshot()
        return await super().send_now(message)


class HandoverCodexFactory(FixtureAdapterFactory):
    def __init__(
        self,
        fixture_dir: Path,
        *,
        pid: int | None = None,
        stop_result: str = "interrupted",
    ):
        super().__init__(fixture_dir, pid=pid)
        self.stop_result = stop_result

    def __call__(self, record: RunRecord) -> ProviderAdapter:
        if record.provider is ProviderKind.CODEX:
            adapter = HandoverStopEventCodexAdapter(
                self.fixture_dir / "codex_app_server_success.jsonl",
                self.fixture_dir / "codex_app_server_control.jsonl",
                pid=self.pid,
                generation=record.provider_generation,
            )
            adapter.stop_result = self.stop_result
            return adapter
        return super().__call__(record)


class HandoverCancelClaudeAdapter(ClaudeFixtureAdapter):
    recovery_prompt_count = 0

    async def start(self, request: StartRequest) -> AdapterStatus:
        self._request = request  # noqa: SLF001 - handover cancel fixture
        self._status = AdapterStatus(
            LifecycleState.IDLE,
            str(uuid4()),
            self.pid,
            generation=max(1, self.snapshot().generation + 1),
        )
        return self.snapshot()

    async def resume(self, session_id: str) -> AdapterStatus:
        self._status = AdapterStatus(  # noqa: SLF001 - handover cancel fixture
            LifecycleState.IDLE,
            session_id,
            self.pid,
            generation=max(1, self.snapshot().generation + 1),
        )
        return self.snapshot()

    async def stop(self) -> AdapterStatus:
        if self.snapshot().state is LifecycleState.WAITING_APPROVAL:
            await self._events.put(  # noqa: SLF001 - handover cancel fixture
                ProviderEvent(
                    ProviderKind.CLAUDE,
                    {"type": "control_cancel_request", "request_id": "old"},
                    direction="stdout",
                    generation=self.snapshot().generation,
                )
            )
            await self._events.put(  # noqa: SLF001 - end-session response fixture
                ProviderEvent(
                    ProviderKind.CLAUDE,
                    {
                        "type": "control_response",
                        "response": {"request_id": "end-session"},
                    },
                    direction="stdout",
                    generation=self.snapshot().generation,
                )
            )
        return await super().stop()

    async def send_now(self, message: str) -> AdapterStatus:
        if "Recreate the exact approval question" in message:
            self.recovery_prompt_count += 1
            await self._events.put(  # noqa: SLF001 - approval recovery fixture
                ProviderEvent(
                    ProviderKind.CLAUDE,
                    {
                        "type": "control_request",
                        "request_id": "old",
                        "request": {"subtype": "can_use_tool"},
                    },
                    direction="stdout",
                    generation=self.snapshot().generation,
                )
            )
            self._status = AdapterStatus(
                LifecycleState.WAITING_APPROVAL,
                self.snapshot().session_id,
                self.snapshot().pid,
                generation=self.snapshot().generation,
            )
            return self.snapshot()
        return await super().send_now(message)


class HandoverCancelFactory(FixtureAdapterFactory):
    def __call__(self, record: RunRecord) -> ProviderAdapter:
        if record.provider is ProviderKind.CLAUDE:
            return HandoverCancelClaudeAdapter(
                self.fixture_dir / "claude_stream_native_surfaces.jsonl",
                pid=self.pid,
                generation=record.provider_generation,
            )
        return super().__call__(record)


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
            record = await self.supervisor.start_run(
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
        self.assertEqual(verified["run_id"], record.run_id)
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
        stored_keys = [
            entry["key"]
            for entry in self.store.get(record.run_id).message_dedupe_keys
        ]
        self.assertEqual(stored_keys, [dedupe_key])

        reloaded = RunStore(self.paths)
        reloaded_keys = [
            entry["key"]
            for entry in reloaded.get(record.run_id).message_dedupe_keys
        ]
        self.assertEqual(reloaded_keys, [dedupe_key])

        # WIKI-232 REVIEW11 M3: cover the legacy on-disk schema too.
        # Snapshots that predate the owner-aware entry format store
        # ``message_dedupe_keys`` as a bare list of strings; a broken
        # migration would either drop them (redeliver messages) or
        # promote them with an unexpected owner (let unrelated retries
        # bypass the dedupe). Overwrite the JSON snapshot with the
        # legacy shape, reopen the store, and verify:
        #  (a) legacy strings are promoted to owner-less objects,
        #  (b) an ownerless duplicate is rejected,
        #  (c) an owned re-claim by the SAME owner replays cleanly,
        #  (d) an owned re-claim by a DIFFERENT owner still dedupes,
        #  (e) release removes the entry,
        #  (f) ``RunStore.replace`` copies the promoted entries to the
        #      replacement run.
        legacy_run_path = self.store.run_path(record.run_id)
        legacy_snapshot = json.loads(legacy_run_path.read_text(encoding="utf-8"))
        legacy_snapshot["message_dedupe_keys"] = [
            dedupe_key,
            "legacy-only:migration-key:v0",
        ]
        legacy_run_path.write_text(
            json.dumps(legacy_snapshot),
            encoding="utf-8",
        )

        legacy_store = RunStore(self.paths)
        promoted = legacy_store.get(record.run_id).message_dedupe_keys
        # (a) The migration promotes bare strings into owner-less objects.
        self.assertEqual(
            promoted,
            [
                {"key": dedupe_key},
                {"key": "legacy-only:migration-key:v0"},
            ],
        )

        # (b) An ownerless duplicate claim on a legacy key is rejected.
        _, claimed_ownerless = legacy_store.claim_message_dedupe_key(
            record.run_id, dedupe_key
        )
        self.assertFalse(
            claimed_ownerless,
            "legacy ownerless entry must reject a repeat unowned claim",
        )

        # (c) An owned re-claim uses the effect_id as owner. Because the
        # legacy entry has no owner, the first owned claim still rejects
        # (matches the current invariant that legacy claims cannot be
        # retried — the owner match check requires an existing owner).
        _, first_owned = legacy_store.claim_message_dedupe_key(
            record.run_id,
            dedupe_key,
            owner="run/send_on_idle:legacy-effect-1",
        )
        self.assertFalse(
            first_owned,
            "legacy ownerless entry does not silently gain an owner",
        )

        # Now claim a fresh key with an owner, then prove the same owner
        # replays cleanly and a different owner still dedupes.
        fresh_key = "artifact-render:fresh-owner:svg-render"
        _, fresh_claim = legacy_store.claim_message_dedupe_key(
            record.run_id,
            fresh_key,
            owner="run/send_on_idle:legacy-effect-2",
        )
        self.assertTrue(fresh_claim)
        _, replay_claim = legacy_store.claim_message_dedupe_key(
            record.run_id,
            fresh_key,
            owner="run/send_on_idle:legacy-effect-2",
        )
        self.assertTrue(
            replay_claim,
            "same-owner replay must succeed so a crash between the "
            "dedupe write and the provider send can retry",
        )
        _, cross_owner = legacy_store.claim_message_dedupe_key(
            record.run_id,
            fresh_key,
            owner="run/send_on_idle:different-effect",
        )
        self.assertFalse(
            cross_owner,
            "different-owner claim must dedupe against the prior owner",
        )

        # (e) Release drops the promoted legacy entry.
        legacy_store.release_message_dedupe_key(
            record.run_id, "legacy-only:migration-key:v0"
        )
        after_release = {
            entry["key"]
            for entry in legacy_store.get(record.run_id).message_dedupe_keys
        }
        self.assertNotIn("legacy-only:migration-key:v0", after_release)
        # And the released key is now claimable fresh (with any owner).
        _, reclaimed = legacy_store.claim_message_dedupe_key(
            record.run_id,
            "legacy-only:migration-key:v0",
            owner="run/send_on_idle:post-release",
        )
        self.assertTrue(reclaimed)

        # (f) Replacement copies the promoted entries so a mid-flight
        # composer retry stays idempotent across ``RunStore.replace``.
        old_record = legacy_store.get(record.run_id)
        replacement = RunRecord.new(
            agent_id=old_record.agent_id,
            provider=old_record.provider,
            role=old_record.role,
            model=old_record.model,
            worktree=old_record.worktree,
            prompt=old_record.initial_prompt or "legacy replacement",
        )
        _old_after, new_run = legacy_store.replace(record.run_id, replacement)
        # Every prior dedupe key survives to the replacement so a
        # replayed retry on the same key still dedupes.
        replaced_keys = {
            entry["key"] for entry in new_run.message_dedupe_keys
        }
        self.assertIn(dedupe_key, replaced_keys)
        _, replaced_ownerless = legacy_store.claim_message_dedupe_key(
            new_run.run_id, dedupe_key
        )
        self.assertFalse(
            replaced_ownerless,
            "replacement inherits the legacy dedupe entry so retries "
            "on the same key still dedupe",
        )

        # Reset the store to the current-schema snapshot for the rest of
        # the test — cap enforcement below assumes the full object list.
        current_snapshot = legacy_store.get(record.run_id).to_dict()
        # Restore the pre-legacy record so cap-eviction below runs on
        # the same starting point as the original assertion.
        current_snapshot["message_dedupe_keys"] = [
            {"key": entry["key"]}
            for entry in current_snapshot["message_dedupe_keys"]
            if entry["key"] == dedupe_key
        ]
        legacy_run_path.write_text(
            json.dumps(current_snapshot),
            encoding="utf-8",
        )
        self.store = RunStore(self.paths)

        for index in range(MAX_MESSAGE_DEDUPE_KEYS + 1):
            self.store.claim_message_dedupe_key(
                record.run_id, f"artifact-render:key-{index}:svg-render"
            )
        keys = [
            entry["key"]
            for entry in self.store.get(record.run_id).message_dedupe_keys
        ]
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

    async def test_implicit_start_id_is_reusable_after_archive(self) -> None:
        params = {
            "agent_id": "WIKI-IMPLICIT-ARCHIVE",
            "provider": "codex",
            "role": "implement",
            "model": "fixture-codex",
            "effort": "high",
            "worktree": str(self.worktree),
            "prompt": "Retry this omitted-id spawn after archive.",
            "request_id": "spawn-implicit-archive",
            "implicit_request_id": True,
        }
        first = await self.supervisor.dispatch("run/start", params)
        await asyncio.sleep(0)
        await self.supervisor.dispatch(
            "run/archive",
            {"run_id": first["run_id"], "outcome": "closed"},
        )

        second = await self.supervisor.dispatch("run/start", dict(params))

        self.assertNotEqual(second["run_id"], first["run_id"])

    async def test_implicit_start_failure_does_not_cache_error(self) -> None:
        await self.supervisor.close()
        attempts = 0

        def factory(record: RunRecord):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return StartFailureAdapter(
                    FIXTURES / "codex_app_server_success.jsonl",
                    FIXTURES / "codex_app_server_control.jsonl",
                    pid=987_654,
                )
            return CodexFixtureAdapter(
                FIXTURES / "codex_app_server_success.jsonl",
                FIXTURES / "codex_app_server_control.jsonl",
                pid=os.getpid(),
            )

        self.supervisor = Supervisor(self.store, factory, pid_alive=lambda _pid: False)
        params = {
            "agent_id": "WIKI-IMPLICIT-FAILURE",
            "provider": "codex",
            "role": "implement",
            "model": "fixture-codex",
            "effort": "high",
            "worktree": str(self.worktree),
            "prompt": "Retry this omitted-id spawn after failure.",
            "request_id": "spawn-implicit-failure",
            "implicit_request_id": True,
        }
        with self.assertRaisesRegex(ProviderProcessError, "fixture start failure"):
            await self.supervisor.dispatch("run/start", params)
        await asyncio.sleep(0)

        second = await self.supervisor.dispatch("run/start", dict(params))

        self.assertEqual(attempts, 2)
        self.assertEqual(second["agent_id"], "WIKI-IMPLICIT-FAILURE")

    async def test_factory_failure_rolls_back_fresh_start(self) -> None:
        def failing_factory(_record: RunRecord) -> ProviderAdapter:
            raise RuntimeError("factory failure")

        await self.supervisor.close()
        self.supervisor = Supervisor(self.store, failing_factory)
        with self.assertRaisesRegex(ProviderProcessError, "factory failure"):
            await self.supervisor.start_run(
                agent_id="WIKI-FACTORY-FAILURE",
                provider=ProviderKind.CODEX,
                role="implement",
                model="fixture-codex",
                effort="high",
                worktree=str(self.worktree),
                prompt="factory failure must roll back",
            )

        self.assertIsNone(self.store.current_run_id("WIKI-FACTORY-FAILURE"))
        self.assertEqual(self.store.list_runs(), [])
        self.assertFalse(
            self.store.status_path("WIKI-FACTORY-FAILURE").exists()
        )

    async def test_attachment_persistence_failure_closes_adapter_and_rolls_back(
        self,
    ) -> None:
        adapter: CodexFixtureAdapter | None = None
        original_factory = self.supervisor.adapter_factory

        def factory(record: RunRecord) -> ProviderAdapter:
            nonlocal adapter
            adapter = cast(CodexFixtureAdapter, original_factory(record))
            return adapter

        self.supervisor.adapter_factory = factory
        with mock.patch.object(
            self.store,
            "set_control_attached",
            side_effect=OSError("control-state persistence failure"),
        ):
            with self.assertRaisesRegex(
                ProviderProcessError, "control-state persistence failure"
            ):
                await self.supervisor.start_run(
                    agent_id="WIKI-ATTACH-FAILURE",
                    provider=ProviderKind.CODEX,
                    role="implement",
                    model="fixture-codex",
                    effort="high",
                    worktree=str(self.worktree),
                    prompt="attachment failure must roll back",
                )

        assert adapter is not None
        self.assertTrue(adapter.closed)
        self.assertIsNone(self.store.current_run_id("WIKI-ATTACH-FAILURE"))
        self.assertEqual(self.store.list_runs(), [])

    async def test_aborted_start_preserves_interleaved_committed_registry_entry(self) -> None:
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        allow_first = asyncio.Event()
        allow_second_failure = asyncio.Event()
        original_factory = self.supervisor.adapter_factory

        def factory(record: RunRecord):
            adapter = original_factory(record)
            if record.agent_id == "WIKI-ROLLBACK-A":
                assert isinstance(adapter, ClaudeFixtureAdapter)

                async def delayed_start(request: StartRequest) -> AdapterStatus:
                    first_started.set()
                    await allow_first.wait()
                    return await ClaudeFixtureAdapter.start(adapter, request)

                adapter.start = delayed_start  # type: ignore[method-assign]
            elif record.agent_id == "WIKI-ROLLBACK-B":
                assert isinstance(adapter, CodexFixtureAdapter)

                async def delayed_failure(request: StartRequest) -> AdapterStatus:
                    del request
                    second_started.set()
                    await allow_second_failure.wait()
                    raise RuntimeError("interleaved fixture failure")

                adapter.start = delayed_failure  # type: ignore[method-assign]
            return adapter

        self.supervisor.adapter_factory = factory
        first_task = asyncio.create_task(
            self.supervisor.start_run(
                agent_id="WIKI-ROLLBACK-A",
                provider=ProviderKind.CLAUDE,
                role="implement",
                model="fixture-claude",
                worktree=str(self.worktree),
                prompt="committed start must survive a later abort",
            )
        )
        await asyncio.wait_for(first_started.wait(), timeout=2)
        second_task = asyncio.create_task(
            self.supervisor.start_run(
                agent_id="WIKI-ROLLBACK-B",
                provider=ProviderKind.CODEX,
                role="implement",
                model="fixture-codex",
                effort="high",
                worktree=str(self.worktree),
                prompt="this start will abort",
            )
        )
        await asyncio.wait_for(second_started.wait(), timeout=2)
        allow_first.set()
        first = await first_task
        allow_second_failure.set()
        with self.assertRaisesRegex(ProviderProcessError, "interleaved fixture failure"):
            await second_task

        self.assertEqual(self.store.current_run_id("WIKI-ROLLBACK-A"), first.run_id)
        self.assertIsNone(self.store.current_run_id("WIKI-ROLLBACK-B"))
        registry = json.loads(self.paths.registry_path.read_text(encoding="utf-8"))
        self.assertEqual(registry["WIKI-ROLLBACK-A"]["current"]["run_id"], first.run_id)

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

    async def test_on_idle_crash_after_provider_acceptance_does_not_resend(self) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-ON-IDLE-CRASH",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="on-idle crash boundary",
        )
        adapter = self.supervisor.adapters[record.run_id]
        pending_id = str(uuid4())

        with mock.patch.object(
            adapter,
            "send_on_idle",
            wraps=adapter.send_on_idle,
        ) as provider_send:
            with mock.patch.object(
                self.store.command_log,
                "mark_steer_sent_for_pending",
                side_effect=asyncio.CancelledError,
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await self.supervisor.send_on_idle(
                        record.run_id,
                        "deliver once",
                        pending_id=pending_id,
                        effect_id="on-idle-crash",
                    )

            self.assertEqual(provider_send.await_count, 1)
            effect = self.store.command_log.steer_effect_for_pending(
                record.run_id, pending_id
            )
            self.assertIsNotNone(effect)
            self.assertEqual(effect["status"] if effect else None, "sending")

            # A restart has the queued message and the durable sending marker,
            # but no provider echo yet. Recovery must wait instead of sending.
            await self.supervisor._deliver_next_queued_locked(  # noqa: SLF001
                record.run_id,
                adapter,
            )
            self.assertEqual(provider_send.await_count, 1)

            pending = self.store.get(record.run_id).pending_user_messages[0]
            raw = self.store.append_raw(
                record.run_id,
                provider="codex",
                direction="server",
                payload={"method": "item/completed"},
            )
            self.store.append_normalized(
                record.run_id,
                raw_seq=int(raw["seq"]),
                disposition=EventDisposition.RENDERED,
                kind="user_message",
                payload={
                    "pending_id": pending_id,
                    "composer_text": pending["text"],
                    "composer_sent_at": pending["sent_at"],
                },
            )
            await self.supervisor._deliver_next_queued_locked(  # noqa: SLF001
                record.run_id,
                adapter,
            )

        self.assertEqual(provider_send.await_count, 1)
        self.assertEqual(self.store.get(record.run_id).queued_messages, [])
        self.assertEqual(
            self.store.command_log.steer_effect_for_pending(
                record.run_id, pending_id
            )["status"],
            "acknowledged",
        )

    async def test_recovery_after_sent_before_pop_drains_queue_tail(self) -> None:
        """WIKI-232 REVIEW5 H2: recovery must drain a tail after a sent
        effect survives a crash before its queue row is popped."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-REVIEW5-SENT",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="review 5 sent before pop",
        )
        adapter = self.supervisor.adapters[record.run_id]
        pid1 = str(uuid4())
        pid2 = str(uuid4())

        real_mark = self.store.command_log.mark_steer_sent_for_pending

        def mark_sent_then_crash(run_id: str, pending_id: str) -> None:
            real_mark(run_id, pending_id)
            raise asyncio.CancelledError

        with mock.patch.object(
            self.store.command_log,
            "mark_steer_sent_for_pending",
            side_effect=mark_sent_then_crash,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.supervisor.send_on_idle(
                    record.run_id,
                    "crashed-head",
                    pending_id=pid1,
                    effect_id="review5-sent-before-pop-1",
                )

        first = self.store.command_log.steer_effect_for_pending(
            record.run_id, pid1,
        )
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first["status"], "sent")
        self.assertEqual(
            [message["text"] for message in self.store.queued_messages(record.run_id)],
            ["crashed-head"],
        )

        # Keep the tail behind the stale sent head while the provider is
        # working, then make the recovery cleanup observe IDLE.
        snapshot = adapter.snapshot()
        working = AdapterStatus(
            LifecycleState.WORKING,
            snapshot.session_id,
            os.getpid(),
            generation=snapshot.generation,
        )
        adapter._status = working  # noqa: SLF001 - queue fixture
        self.store.update_adapter_status(record.run_id, working)
        queued = await self.supervisor.send_on_idle(
            record.run_id,
            "tail-message",
            pending_id=pid2,
            effect_id="review5-sent-before-pop-2",
        )
        self.assertEqual(queued["status"], "queued")

        idle = AdapterStatus(
            LifecycleState.IDLE,
            snapshot.session_id,
            os.getpid(),
            generation=snapshot.generation,
        )
        adapter._status = idle  # noqa: SLF001 - recovery fixture
        self.store.update_adapter_status(record.run_id, idle)

        original_send = adapter.send_on_idle
        provider_calls: list[str] = []

        async def tracked_send(message: str) -> AdapterStatus:
            provider_calls.append(message)
            return await original_send(message)

        adapter.send_on_idle = tracked_send  # type: ignore[method-assign]
        try:
            await self.supervisor._deliver_next_queued_locked(  # noqa: SLF001
                record.run_id, adapter,
            )
            for _ in range(200):
                if not self.store.queued_messages(record.run_id):
                    break
                await asyncio.sleep(0.01)
        finally:
            adapter.send_on_idle = original_send  # type: ignore[method-assign]

        self.assertEqual(provider_calls, ["tail-message"])
        self.assertEqual(self.store.queued_messages(record.run_id), [])
        second = self.store.command_log.steer_effect_for_pending(
            record.run_id, pid2,
        )
        self.assertIsNotNone(second)
        assert second is not None
        self.assertIn(second["status"], {"sent", "acknowledged"})

    async def test_reconcile_reload_preserves_live_sent_effect(self) -> None:
        """WIKI-232 REVIEW6 H1: a stale recovery snapshot must not
        overwrite a live drain that already marked its effect sent."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-REVIEW6-RECONCILE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="review 6 reconcile race",
        )
        adapter = self.supervisor.adapters[record.run_id]
        snapshot = adapter.snapshot()
        working = AdapterStatus(
            LifecycleState.WORKING,
            snapshot.session_id,
            os.getpid(),
            generation=snapshot.generation,
        )
        adapter._status = working  # noqa: SLF001 - queue fixture
        self.store.update_adapter_status(record.run_id, working)
        pending_id = str(uuid4())
        await self.supervisor.send_on_idle(
            record.run_id,
            "live-reconcile-send",
            pending_id=pending_id,
            effect_id="review6-reconcile-race",
        )

        idle = AdapterStatus(
            LifecycleState.IDLE,
            snapshot.session_id,
            os.getpid(),
            generation=snapshot.generation,
        )
        adapter._status = idle  # noqa: SLF001 - race fixture
        self.store.update_adapter_status(record.run_id, idle)

        send_started = asyncio.Event()
        release_send = asyncio.Event()
        original_send = adapter.send_on_idle

        async def paused_send(message: str) -> AdapterStatus:
            send_started.set()
            await release_send.wait()
            return await original_send(message)

        adapter.send_on_idle = paused_send  # type: ignore[method-assign]
        live_task = asyncio.create_task(
            self.supervisor._deliver_next_queued(  # noqa: SLF001
                record.run_id,
                adapter,
            )
        )
        try:
            await send_started.wait()
            sending = self.store.command_log.steer_effect_for_pending(
                record.run_id, pending_id,
            )
            self.assertIsNotNone(sending)
            assert sending is not None
            self.assertEqual(sending["status"], "sending")

            real_list = self.store.command_log.sending_steer_effects
            snapshot_seen = asyncio.Event()

            class SnapshotRows(list[dict[str, Any]]):
                def __iter__(self):
                    snapshot_seen.set()
                    return super().__iter__()

            def stale_snapshot() -> list[dict[str, Any]]:
                return SnapshotRows(real_list())

            self.supervisor._sending_effects_reconciled = False  # noqa: SLF001
            with mock.patch.object(
                self.store.command_log,
                "sending_steer_effects",
                side_effect=stale_snapshot,
            ):
                reconcile_task = asyncio.create_task(
                    self.supervisor._reconcile_sending_steer_effects()  # noqa: SLF001
                )
                await snapshot_seen.wait()
                # The live drain owns the run lock. Reconciliation has its
                # stale list, then waits for that live operation to finish.
                release_send.set()
                await live_task
                await reconcile_task
        finally:
            adapter.send_on_idle = original_send  # type: ignore[method-assign]
            if not live_task.done():
                live_task.cancel()
                await asyncio.gather(live_task, return_exceptions=True)

        final_effect = self.store.command_log.steer_effect_for_pending(
            record.run_id, pending_id,
        )
        self.assertIsNotNone(final_effect)
        assert final_effect is not None
        self.assertEqual(final_effect["status"], "sent")
        self.assertEqual(self.store.queued_messages(record.run_id), [])

    async def test_send_now_replay_after_dedupe_claim_delivers_once(self) -> None:
        """WIKI-232 H1: a crash between the dedupe-claim write and the
        provider send used to leave the effect at 'queued' with the dedupe
        key claimed. Replay saw its own key and returned deduplicated
        without ever calling the provider (provider_send_count=0). Bind the
        claim to the steer effect so the retry can resume."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-H1",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="dedupe claim replay",
        )
        adapter = self.supervisor.adapters[record.run_id]
        pending_id = str(uuid4())
        effect_id = "send-now-h1"
        dedupe_key = "wiki-232-h1:artifact-render:1"

        real_update = self.store.command_log.update_steer_effect

        def crash_on_sending(method, request_id, status, result=None):
            if status == "sending":
                # Simulate the daemon dying immediately after the dedupe
                # claim persisted but before the provider was invoked.
                raise asyncio.CancelledError()
            return real_update(method, request_id, status, result)

        with mock.patch.object(
            adapter, "send_now", wraps=adapter.send_now
        ) as provider_send:
            with mock.patch.object(
                self.store.command_log,
                "update_steer_effect",
                side_effect=crash_on_sending,
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await self.supervisor.send_now(
                        record.run_id,
                        "deliver exactly once",
                        pending_id=pending_id,
                        dedupe_key=dedupe_key,
                        effect_id=effect_id,
                    )

            # Nothing reached the provider; the dedupe claim is bound to
            # this effect so a replay can complete instead of being told
            # "deduplicated" by its own earlier claim.
            self.assertEqual(provider_send.await_count, 0)
            keys = self.store.get(record.run_id).message_dedupe_keys
            self.assertEqual(len(keys), 1)
            self.assertEqual(keys[0].get("key"), dedupe_key)
            # Owner is method-scoped so the same request_id can legally
            # appear once per supervisor method without cross-claim.
            self.assertEqual(keys[0].get("owner"), f"run/send_now:{effect_id}")
            queued_effect = self.store.command_log.steer_effect_for_pending(
                record.run_id, pending_id
            )
            self.assertIsNotNone(queued_effect)
            self.assertEqual(
                queued_effect["status"] if queued_effect else None, "queued"
            )

            result = await self.supervisor.send_now(
                record.run_id,
                "deliver exactly once",
                pending_id=pending_id,
                dedupe_key=dedupe_key,
                effect_id=effect_id,
            )

        self.assertEqual(result["status"], "sent")
        self.assertEqual(provider_send.await_count, 1)
        self.assertEqual(
            self.store.command_log.steer_effect_for_pending(
                record.run_id, pending_id
            )["status"],
            "sent",
        )

    async def test_send_now_echo_before_error_records_successful_receipt(
        self,
    ) -> None:
        """REVIEW15 H1: durable echo wins over a later transport error."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-R15-NOW",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="accepted then error send_now",
        )
        adapter = self.supervisor.adapters[record.run_id]
        original_send = adapter.send_now
        provider_calls: list[str] = []

        async def echo_then_raise(message: str) -> AdapterStatus:
            provider_calls.append(message)
            await self.supervisor._handle_provider_event(  # noqa: SLF001
                record.run_id,
                adapter,
                ProviderEvent(
                    ProviderKind.CODEX,
                    {
                        "method": "item/completed",
                        "params": {
                            "item": {
                                "type": "userMessage",
                                "content": [{"type": "text", "text": message}],
                            }
                        },
                    },
                ),
            )
            raise RuntimeError("response failed after provider acceptance")

        adapter.send_now = echo_then_raise  # type: ignore[method-assign]
        params = {
            "run_id": record.run_id,
            "text": "accepted exactly once",
            "dedupe_key": "review15-now-dedupe",
            "request_id": "review15-now-request",
        }
        try:
            result = await self.supervisor.dispatch("run/send_now", params)
            replay = await self.supervisor.dispatch("run/send_now", dict(params))
        finally:
            adapter.send_now = original_send  # type: ignore[method-assign]

        self.assertEqual(result["status"], "sent")
        self.assertEqual(replay, result)
        self.assertEqual(provider_calls, ["accepted exactly once"])
        receipt = self.store.command_log.receipt(
            "run/send_now", "review15-now-request"
        )
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertTrue(receipt.ok)
        self.assertEqual(receipt.result, result)

    async def test_send_now_next_turn_echo_after_error_records_success(
        self,
    ) -> None:
        """REVIEW16 H1: the event pump crosses the transport boundary."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-R16-NOW",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="next-turn echo after send_now error",
        )
        adapter = self.supervisor.adapters[record.run_id]
        original_send = adapter.send_now
        provider_calls: list[str] = []

        async def schedule_echo_then_raise(message: str) -> AdapterStatus:
            provider_calls.append(message)
            event = ProviderEvent(
                ProviderKind.CODEX,
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "userMessage",
                            "content": [{"type": "text", "text": message}],
                        }
                    },
                },
            )
            asyncio.get_running_loop().call_soon(
                adapter._events.put_nowait,  # noqa: SLF001 - pump boundary fixture
                event,
            )
            raise RuntimeError("response failed before scheduled echo drained")

        adapter.send_now = schedule_echo_then_raise  # type: ignore[method-assign]
        request_id = "review16-next-turn-request"
        dedupe_key = "review16-next-turn-dedupe"
        params = {
            "run_id": record.run_id,
            "text": "scheduled echo exactly once",
            "source": "fleet-monitor",
            "dedupe_key": dedupe_key,
            "request_id": request_id,
        }
        try:
            result = await self.supervisor.dispatch("run/send_now", params)
            replay = await self.supervisor.dispatch("run/send_now", dict(params))
        finally:
            adapter.send_now = original_send  # type: ignore[method-assign]

        self.assertEqual(provider_calls, ["scheduled echo exactly once"])
        self.assertEqual(result["status"], "sent")
        self.assertEqual(replay, result)
        pending_id = result.get("pending_id")
        self.assertIsInstance(pending_id, str)
        receipt = self.store.command_log.receipt("run/send_now", request_id)
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertTrue(receipt.ok)
        self.assertEqual(receipt.result, result)

        composer = self.store.get(record.run_id).composer_messages
        matching_composer = [
            message for message in composer if message.get("pending_id") == pending_id
        ]
        self.assertEqual(len(matching_composer), 1)
        self.assertEqual(matching_composer[0].get("source"), "fleet-monitor")
        matching_normalized = [
            event
            for event in self.store.read_normalized_events(record.run_id)
            if isinstance(event.get("payload"), dict)
            and event["payload"].get("pending_id") == pending_id
        ]
        self.assertEqual(len(matching_normalized), 1)
        claims = self.store.get(record.run_id).message_dedupe_keys
        self.assertEqual(
            claims,
            [
                {
                    "key": dedupe_key,
                    "owner": f"run/send_now:{request_id}",
                }
            ],
        )

    async def test_send_now_error_without_echo_stays_uncertain(
        self,
    ) -> None:
        """REVIEW16 H1 control: ambiguity keeps correlation and dedupe state."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-R15-NOW-FAIL",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="rejected send_now",
        )
        adapter = self.supervisor.adapters[record.run_id]
        original_send = adapter.send_now
        provider_calls: list[str] = []

        async def raise_without_echo(message: str) -> AdapterStatus:
            provider_calls.append(message)
            raise RuntimeError("provider never accepted")

        adapter.send_now = raise_without_echo  # type: ignore[method-assign]
        params = {
            "run_id": record.run_id,
            "text": "must stay uncertain",
            "source": "fleet-monitor",
            "dedupe_key": "review16-no-echo-dedupe",
            "request_id": "review15-now-failed-request",
        }
        try:
            result = await self.supervisor.dispatch("run/send_now", params)
            replay = await self.supervisor.dispatch("run/send_now", dict(params))
        finally:
            adapter.send_now = original_send  # type: ignore[method-assign]

        self.assertEqual(provider_calls, ["must stay uncertain"])
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(replay, result)
        receipt = self.store.command_log.receipt(
            "run/send_now", "review15-now-failed-request"
        )
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertTrue(receipt.ok)
        self.assertEqual(receipt.result, result)
        pending_id = result.get("pending_id")
        self.assertIsInstance(pending_id, str)
        current = self.store.get(record.run_id)
        self.assertEqual(
            [
                message.get("pending_id")
                for message in current.pending_user_messages
            ],
            [pending_id],
        )
        self.assertEqual(
            current.message_dedupe_keys,
            [
                {
                    "key": "review16-no-echo-dedupe",
                    "owner": "run/send_now:review15-now-failed-request",
                }
            ],
        )
        effect = self.store.command_log.steer_effect_for_pending(
            record.run_id, str(pending_id)
        )
        self.assertIsNotNone(effect)
        assert effect is not None
        self.assertEqual(effect["status"], "sending")
        self.assertEqual(effect["result"]["status"], "uncertain")

    async def test_send_now_equal_text_waits_for_older_unresolved_matcher(
        self,
    ) -> None:
        """REVIEW18 H2: equal text cannot create two unresolved matchers."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-R18-EQUAL",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="defer equal send text",
        )
        adapter = self.supervisor.adapters[record.run_id]
        original_send = adapter.send_now
        provider_calls: list[str] = []

        async def ambiguous_send(message: str) -> AdapterStatus:
            provider_calls.append(message)
            raise RuntimeError("ambiguous first delivery")

        adapter.send_now = ambiguous_send  # type: ignore[method-assign]
        first_params = {
            "run_id": record.run_id,
            "text": "same normalized text",
            "source": "first-source",
            "request_id": "review18-equal-first",
        }
        second_params = {
            "run_id": record.run_id,
            "text": "  same normalized text  ",
            "source": "second-source",
            "request_id": "review18-equal-second",
        }
        try:
            first = await self.supervisor.dispatch("run/send_now", first_params)
            with self.assertRaisesRegex(
                CommandRetryable, "identical message delivery"
            ):
                await self.supervisor.dispatch("run/send_now", second_params)
        finally:
            adapter.send_now = original_send  # type: ignore[method-assign]

        self.assertEqual(first["status"], "uncertain")
        self.assertEqual(provider_calls, ["same normalized text"])
        self.assertIsNone(
            self.store.command_log.receipt(
                "run/send_now", "review18-equal-second"
            )
        )
        pending_commands = self.store.command_log.pending()
        self.assertEqual(
            [command.request_id for command in pending_commands],
            ["review18-equal-second"],
        )
        pending_messages = self.store.get(record.run_id).pending_user_messages
        self.assertEqual(len(pending_messages), 1)
        self.assertEqual(pending_messages[0].get("source"), "first-source")
        second_effect = self.store.command_log.steer_effect_for_request(
            "run/send_now", "review18-equal-second"
        )
        self.assertIsNotNone(second_effect)
        assert second_effect is not None
        self.assertEqual(second_effect["status"], "queued")

    async def test_send_now_normalize_failure_recovers_sent_receipt(
        self,
    ) -> None:
        """REVIEW18 H1: raw echo recovery completes the original command."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-R18-NOW",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="recover send_now normalization",
        )
        record = await _wait_for_events(self.store, record.run_id, 10)
        adapter = self.supervisor.adapters[record.run_id]
        original_send = adapter.send_now
        provider_calls: list[str] = []

        async def schedule_echo_then_raise(message: str) -> AdapterStatus:
            provider_calls.append(message)
            event = ProviderEvent(
                ProviderKind.CODEX,
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "userMessage",
                            "content": [{"type": "text", "text": message}],
                        }
                    },
                },
            )
            asyncio.get_running_loop().call_soon(
                adapter._events.put_nowait,  # noqa: SLF001 - pump boundary fixture
                event,
            )
            raise RuntimeError("transport failed after durable raw echo")

        adapter.send_now = schedule_echo_then_raise  # type: ignore[method-assign]
        real_append = self.store.append_normalized
        failed_appends = 0

        def fail_first_append(*args, **kwargs):
            nonlocal failed_appends
            if failed_appends == 0:
                failed_appends += 1
                raise OSError("fixture normalized append failure")
            return real_append(*args, **kwargs)

        request_id = "review18-send-now-request"
        params = {
            "run_id": record.run_id,
            "text": "recover this send now echo",
            "source": "fleet-monitor",
            "dedupe_key": "review18-send-now-dedupe",
            "request_id": request_id,
        }
        try:
            with mock.patch.object(
                self.store,
                "append_normalized",
                side_effect=fail_first_append,
            ):
                with self.assertRaisesRegex(
                    CommandRetryable, "normalization did not commit"
                ):
                    await self.supervisor.dispatch("run/send_now", params)
        finally:
            adapter.send_now = original_send  # type: ignore[method-assign]

        self.assertEqual(failed_appends, 1)
        self.assertEqual(provider_calls, ["recover this send now echo"])
        self.assertIsNone(
            self.store.command_log.receipt("run/send_now", request_id)
        )
        self.assertEqual(
            [command.request_id for command in self.store.command_log.pending()],
            [request_id],
        )

        await self.supervisor.recover_on_start()
        replay = await self.supervisor.dispatch("run/send_now", dict(params))

        self.assertEqual(replay["status"], "sent")
        self.assertEqual(provider_calls, ["recover this send now echo"])
        receipt = self.store.command_log.receipt("run/send_now", request_id)
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertTrue(receipt.ok)
        self.assertEqual(receipt.result, replay)
        pending_id = replay.get("pending_id")
        self.assertIsInstance(pending_id, str)
        matching_normalized = [
            event
            for event in self.store.read_normalized_events(record.run_id)
            if isinstance(event.get("payload"), dict)
            and event["payload"].get("pending_id") == pending_id
        ]
        self.assertEqual(len(matching_normalized), 1)
        matching_composer = [
            message
            for message in self.store.get(record.run_id).composer_messages
            if message.get("pending_id") == pending_id
        ]
        self.assertEqual(len(matching_composer), 1)
        self.assertEqual(matching_composer[0].get("source"), "fleet-monitor")

    async def test_recover_on_start_reconciles_wedged_sending_effect(self) -> None:
        """WIKI-232 H2: an on-idle effect stuck at 'sending' after a daemon
        crash used to wedge the queue forever because no fresh transport
        could produce the missing echo. The recover_on_start sweep now
        promotes each such effect to a terminal 'uncertain' result and
        drops the head so later queued messages can drain."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-H2",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="sending recovery",
        )
        adapter = self.supervisor.adapters[record.run_id]
        pending_id = str(uuid4())

        with mock.patch.object(
            adapter, "send_on_idle", wraps=adapter.send_on_idle
        ) as provider_send:
            with mock.patch.object(
                self.store.command_log,
                "mark_steer_sent_for_pending",
                side_effect=asyncio.CancelledError,
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await self.supervisor.send_on_idle(
                        record.run_id,
                        "wedge me",
                        pending_id=pending_id,
                        effect_id="send-on-idle-h2",
                    )

            # Pre-sweep: exactly the wedge scenario the finding describes.
            wedged = self.store.command_log.steer_effect_for_pending(
                record.run_id, pending_id
            )
            self.assertEqual(wedged["status"] if wedged else None, "sending")
            self.assertEqual(len(self.store.queued_messages(record.run_id)), 1)
            self.assertEqual(provider_send.await_count, 1)

            # Force a boot so recover_on_start does the sweep.
            self.supervisor._sending_effects_reconciled = False  # noqa: SLF001
            await self.supervisor._reconcile_sending_steer_effects()  # noqa: SLF001

        # Post-sweep: effect is terminal, queue drained, no double-send.
        resolved = self.store.command_log.steer_effect_for_pending(
            record.run_id, pending_id
        )
        self.assertEqual(resolved["status"], "acknowledged")
        self.assertEqual(resolved["result"]["status"], "uncertain")
        self.assertEqual(self.store.queued_messages(record.run_id), [])
        self.assertEqual(self.store.get(record.run_id).pending_user_messages, [])

        # And a fresh queued message drains normally through the same run
        # once the provider returns to idle. Return the adapter to IDLE so
        # the drain proceeds instead of preserving the head for a still-busy
        # provider (WIKI-232 R3 gate).
        follow_up_adapter = self.supervisor.adapters[record.run_id]
        follow_up_snapshot = follow_up_adapter.snapshot()
        idle = AdapterStatus(
            LifecycleState.IDLE,
            follow_up_snapshot.session_id,
            os.getpid(),
            generation=follow_up_snapshot.generation,
        )
        follow_up_adapter._status = idle  # noqa: SLF001 - drain fixture
        self.store.update_adapter_status(record.run_id, idle)

        follow_up_pending = str(uuid4())
        follow_up = await self.supervisor.send_on_idle(
            record.run_id,
            "queue drains after wedge",
            pending_id=follow_up_pending,
            effect_id="send-on-idle-h2-followup",
        )
        self.assertIn(follow_up["status"], {"sent", "queued"})
        await self.supervisor._deliver_next_queued_locked(  # noqa: SLF001
            record.run_id,
            follow_up_adapter,
        )
        self.assertEqual(self.store.queued_messages(record.run_id), [])

    async def test_startup_reconcile_skips_missing_run_then_handles_live(
        self,
    ) -> None:
        """REVIEW15 H3: an archived effect cannot abort the startup scan."""

        stale = await self.supervisor.start_run(
            agent_id="WIKI-232-R15-STALE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="stale sending effect",
        )
        stale_pending = str(uuid4())
        self.store.command_log.steer_effect(
            method="run/send_now",
            request_id="review15-stale-effect",
            agent_id=stale.agent_id,
            command_hash="",
            run_id=stale.run_id,
            pending_id=stale_pending,
            message="stale",
            mode="now",
        )
        self.store.command_log.mark_steer_sending_for_pending(
            stale.run_id, stale_pending
        )
        await self.supervisor.archive(stale.run_id, outcome="closed")

        live = await self.supervisor.start_run(
            agent_id="WIKI-232-R15-LIVE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="live sending effect",
        )
        live_pending = str(uuid4())
        self.store.command_log.steer_effect(
            method="run/send_now",
            request_id="review15-live-effect",
            agent_id=live.agent_id,
            command_hash="",
            run_id=live.run_id,
            pending_id=live_pending,
            message="live",
            mode="now",
        )
        self.store.command_log.mark_steer_sending_for_pending(
            live.run_id, live_pending
        )

        self.supervisor._sending_effects_reconciled = False  # noqa: SLF001
        await self.supervisor._reconcile_sending_steer_effects()  # noqa: SLF001

        self.assertTrue(self.supervisor._sending_effects_reconciled)  # noqa: SLF001
        stale_effect = self.store.command_log.steer_effect_for_request(
            "run/send_now", "review15-stale-effect"
        )
        live_effect = self.store.command_log.steer_effect_for_request(
            "run/send_now", "review15-live-effect"
        )
        assert stale_effect is not None and live_effect is not None
        self.assertEqual(stale_effect["status"], "acknowledged")
        self.assertEqual(
            stale_effect["result"]["reason"],
            "run_missing_during_startup_reconcile",
        )
        self.assertEqual(live_effect["status"], "acknowledged")
        self.assertEqual(
            live_effect["result"]["reason"],
            "supervisor_restart_dropped_send",
        )

    async def test_recover_on_start_normalizes_orphan_raw_before_reconcile(
        self,
    ) -> None:
        """WIKI-232 REVIEW8 H1: a daemon stop between raw append and the
        deferred normalize flush leaves an orphan raw echo. Without
        recovery-side normalization, ``steer_delivery_observed`` misses
        the durable echo and the sending sweep marks the effect
        ``supervisor_restart_dropped_send`` even though the provider
        durably accepted the send."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-REVIEW8-ORPHAN",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="review8 orphan raw",
        )
        pending_id = str(uuid4())
        echoed_text = "orphan echo body"

        # Steer effect at ``sending`` and a pending user message match
        # what ``_deliver_next_queued`` writes before an inbound
        # composer echo would land.
        self.store.command_log.steer_effect(
            method="run/send_on_idle",
            request_id="review8-orphan-effect",
            agent_id=record.agent_id,
            command_hash="",
            run_id=record.run_id,
            pending_id=pending_id,
            message=echoed_text,
            mode="on_idle",
        )
        self.store.command_log.mark_steer_sending_for_pending(
            record.run_id, pending_id
        )
        self.store.track_pending_user_message(
            record.run_id, pending_id, echoed_text
        )

        # Simulate the crash: raw echo committed, deferred normalize
        # never flushed. Do NOT append a normalized row.
        self.store.append_raw(
            record.run_id,
            provider=ProviderKind.CODEX.value,
            direction="provider",
            payload={
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": echoed_text}],
                    }
                },
            },
            generation=1,
        )
        # Precondition: no normalized row references the raw echo, so
        # ``steer_delivery_observed`` returns False before the recovery.
        self.assertFalse(
            self.store.steer_delivery_observed(record.run_id, pending_id)
        )
        pre_reconcile = self.store.command_log.steer_effect_for_pending(
            record.run_id, pending_id
        )
        assert pre_reconcile is not None
        self.assertEqual(pre_reconcile["status"], "sending")

        # Boot-time recovery: normalize orphan raws, then reconcile
        # sending effects. Composer echo is now durable, so the sweep
        # promotes the effect to ``sent`` rather than dropping.
        self.supervisor._sending_effects_reconciled = False  # noqa: SLF001
        await self.supervisor._normalize_orphan_raw_events()  # noqa: SLF001
        await self.supervisor._reconcile_sending_steer_effects()  # noqa: SLF001

        normalized_rows = self.store.read_normalized_events(record.run_id)
        matching = [
            row
            for row in normalized_rows
            if isinstance(row.get("payload"), dict)
            and row["payload"].get("pending_id") == pending_id
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(int(matching[0]["raw_seq"]), 1)
        self.assertTrue(
            self.store.steer_delivery_observed(record.run_id, pending_id)
        )
        resolved = self.store.command_log.steer_effect_for_pending(
            record.run_id, pending_id
        )
        assert resolved is not None
        self.assertEqual(resolved["status"], "acknowledged")
        # Live path uses ``acknowledge_steer_for_pending`` which sets no
        # result payload. The regression we guard against is the sweep
        # writing an uncertain ``supervisor_restart_dropped_send`` here.
        result = resolved.get("result")
        if result is not None:
            self.assertNotEqual(
                result.get("reason"), "supervisor_restart_dropped_send"
            )
        recovered = self.store.get(record.run_id)
        self.assertEqual(recovered.pending_user_messages, [])
        self.assertTrue(
            any(
                message.get("pending_id") == pending_id
                for message in recovered.composer_messages
            )
        )

    async def test_orphan_scan_recovers_supervisor_model_change_once(
        self,
    ) -> None:
        """REVIEW18 H1: a synthetic raw row becomes one normalized row."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-R18-MODEL",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="recover synthetic model change",
        )
        record = await _wait_for_events(self.store, record.run_id, 10)
        before_raw = self.store.get(record.run_id).raw_event_count
        with mock.patch.object(
            self.store,
            "append_normalized",
            side_effect=OSError("model change normalize crash"),
        ):
            with self.assertRaisesRegex(OSError, "model change normalize crash"):
                self.supervisor._append_model_changed_event(  # noqa: SLF001
                    record,
                    old_model="fixture-codex",
                    new_model="fixture-codex-new",
                    trigger="review18-crash",
                )

        raw_rows = self.store.read_raw_events(record.run_id)
        self.assertEqual(len(raw_rows), before_raw + 1)
        orphan = raw_rows[-1]
        self.assertEqual(orphan["provider"], "supervisor")
        self.assertEqual(orphan["payload"]["type"], "model_changed")

        await self.supervisor.recover_on_start()
        normalized_path = self.store.normalized_events_path(record.run_id)
        run_path = self.store.run_path(record.run_id)
        matching = [
            row
            for row in self.store.read_normalized_events(record.run_id)
            if int(row.get("raw_seq", 0)) == int(orphan["seq"])
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["kind"], "model_changed")
        self.assertEqual(matching[0]["disposition"], "rendered")
        normalized_mtime = normalized_path.stat().st_mtime_ns
        run_mtime = run_path.stat().st_mtime_ns
        normalized_bytes = normalized_path.read_bytes()
        run_bytes = run_path.read_bytes()

        await asyncio.sleep(0.01)
        await self.supervisor.recover_on_start()

        self.assertEqual(normalized_path.read_bytes(), normalized_bytes)
        self.assertEqual(run_path.read_bytes(), run_bytes)
        self.assertEqual(normalized_path.stat().st_mtime_ns, normalized_mtime)
        self.assertEqual(run_path.stat().st_mtime_ns, run_mtime)

    async def test_orphan_scan_recovers_middle_gap_raw_row(self) -> None:
        """WIKI-232 REVIEW9 F2: a max-based cutoff skips a middle gap
        forever. Raw row N unnormalized, later raw row M > N normalized,
        so max(normalized.raw_seq) >= N. The sweep must recover every
        absent raw row, not just those past the max."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-R9-GAP",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="middle gap",
        )
        pending_id = str(uuid4())
        echoed_text = "middle gap echo body"

        self.store.command_log.steer_effect(
            method="run/send_on_idle",
            request_id="review9-gap-effect",
            agent_id=record.agent_id,
            command_hash="",
            run_id=record.run_id,
            pending_id=pending_id,
            message=echoed_text,
            mode="on_idle",
        )
        self.store.command_log.mark_steer_sending_for_pending(
            record.run_id, pending_id
        )
        self.store.track_pending_user_message(
            record.run_id, pending_id, echoed_text
        )

        # Raw seq 1 is the orphan (deferred normalize crashed). Raw seq 2
        # is a benign later event that DID normalize. max(normalized.raw_seq)
        # is 2, so the old max-based cutoff skips seq 1 forever.
        self.store.append_raw(
            record.run_id,
            provider=ProviderKind.CODEX.value,
            direction="provider",
            payload={
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": echoed_text}],
                    }
                },
            },
            generation=1,
        )
        self.store.append_raw(
            record.run_id,
            provider=ProviderKind.CODEX.value,
            direction="provider",
            payload={
                "method": "item/started",
                "params": {"item": {"type": "agentReasoning"}},
            },
            generation=1,
        )
        # Only seq 2 has a normalized row — mimics the later event landing
        # while seq 1's deferred normalize was dropped by a crash.
        self.store.append_normalized(
            record.run_id,
            raw_seq=2,
            disposition=EventDisposition.IGNORED,
            kind="agent_reasoning",
            payload={},
        )

        self.assertFalse(
            self.store.steer_delivery_observed(record.run_id, pending_id)
        )

        self.supervisor._sending_effects_reconciled = False  # noqa: SLF001
        await self.supervisor._normalize_orphan_raw_events()  # noqa: SLF001
        await self.supervisor._reconcile_sending_steer_effects()  # noqa: SLF001

        normalized_rows = self.store.read_normalized_events(record.run_id)
        matching = [
            row
            for row in normalized_rows
            if isinstance(row.get("payload"), dict)
            and row["payload"].get("pending_id") == pending_id
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(int(matching[0]["raw_seq"]), 1)
        self.assertTrue(
            self.store.steer_delivery_observed(record.run_id, pending_id)
        )
        resolved = self.store.command_log.steer_effect_for_pending(
            record.run_id, pending_id
        )
        assert resolved is not None
        self.assertEqual(resolved["status"], "acknowledged")
        result = resolved.get("result")
        if result is not None:
            self.assertNotEqual(
                result.get("reason"), "supervisor_restart_dropped_send"
            )

    async def test_orphan_scan_serializes_with_live_event_pump(self) -> None:
        """WIKI-232 REVIEW9 F2: the orphan sweep runs before recovery
        attaches live event pumps, and holds the per-run
        event-processing lock so a concurrent normalize path cannot
        write a second normalized row for the same raw seq."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-R9-RACE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="startup race",
        )
        pending_id = str(uuid4())
        echoed_text = "startup race echo body"

        self.store.command_log.steer_effect(
            method="run/send_on_idle",
            request_id="review9-race-effect",
            agent_id=record.agent_id,
            command_hash="",
            run_id=record.run_id,
            pending_id=pending_id,
            message=echoed_text,
            mode="on_idle",
        )
        self.store.command_log.mark_steer_sending_for_pending(
            record.run_id, pending_id
        )
        self.store.track_pending_user_message(
            record.run_id, pending_id, echoed_text
        )
        self.store.append_raw(
            record.run_id,
            provider=ProviderKind.CODEX.value,
            direction="provider",
            payload={
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": echoed_text}],
                    }
                },
            },
            generation=1,
        )

        run_id = record.run_id
        event_lock = self.supervisor.event_processing_locks.setdefault(
            run_id, asyncio.Lock()
        )
        # Simulate a live pump holding the lock at recovery time. The
        # sweep must wait for the pump to release. Once it does, the
        # sweep sees the normalize the pump wrote and does not double-
        # process the raw row.
        await event_lock.acquire()
        try:
            sweep = asyncio.create_task(
                self.supervisor._normalize_orphan_raw_events()  # noqa: SLF001
            )
            # Give the sweep a chance to reach the lock and block.
            await asyncio.sleep(0.05)
            self.assertFalse(sweep.done())
            self.store.append_normalized(
                run_id,
                raw_seq=1,
                disposition=EventDisposition.RENDERED,
                kind="agent_user_message",
                payload={
                    "pending_id": pending_id,
                    "composer_text": echoed_text,
                    "composer_sent_at": time.time(),
                },
            )
        finally:
            event_lock.release()
        await sweep

        normalized_rows = self.store.read_normalized_events(run_id)
        matching = [
            row
            for row in normalized_rows
            if int(row.get("raw_seq", 0)) == 1
        ]
        self.assertEqual(
            len(matching),
            1,
            f"sweep must not duplicate the pump's normalized row: {matching}",
        )

    async def test_orphan_scan_preserves_later_authoritative_state(self) -> None:
        """WIKI-232 REVIEW10 H1: an orphaned raw row whose raw_seq sits
        below an already-normalized lifecycle event must not regress the
        run state. The reproduction: raw seq 1 is turn/started (WORKING)
        with a dropped normalize; raw seq 2 is turn/completed (IDLE)
        already normalized authoritatively. Without the guard, the
        recovery-side ``append_normalized`` for seq 1 flips record.state
        back to WORKING because IDLE->WORKING is a legal transition,
        and later ``_reconcile_existing_runs`` walks the file-order
        normalized log and re-applies the same regression."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-R10-MIDGAP-IDLE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="middle-gap idle",
        )
        run_id = record.run_id

        # Raw seq 1: turn/started (would set lifecycle WORKING).
        self.store.append_raw(
            run_id,
            provider=ProviderKind.CODEX.value,
            direction="provider",
            payload={
                "method": "turn/started",
                "params": {"turn": {"turnId": "midgap-1"}},
            },
            generation=1,
        )
        # Raw seq 2: turn/completed (sets lifecycle IDLE) — already normalized.
        self.store.append_raw(
            run_id,
            provider=ProviderKind.CODEX.value,
            direction="provider",
            payload={
                "method": "turn/completed",
                "params": {"turn": {"turnId": "midgap-1", "status": "completed"}},
            },
            generation=1,
        )
        self.store.append_normalized(
            run_id,
            raw_seq=2,
            disposition=EventDisposition.RENDERED,
            kind="codex_turn_completed",
            payload={"status": "completed"},
            lifecycle_state=LifecycleState.IDLE,
        )
        # Reflect the authoritative live-path result: state has settled to IDLE.
        idle_record = self.store.get(run_id)
        self.assertEqual(idle_record.state, LifecycleState.IDLE)

        self.supervisor._sending_effects_reconciled = False  # noqa: SLF001
        await self.supervisor._normalize_orphan_raw_events()  # noqa: SLF001

        # State stays at IDLE — the stale orphan does not flip it back.
        post_scan = self.store.get(run_id)
        self.assertEqual(post_scan.state, LifecycleState.IDLE)

        normalized_rows = self.store.read_normalized_events(run_id)
        by_raw_seq = {int(row.get("raw_seq", 0)): row for row in normalized_rows}
        # Both raw seqs are now normalized (durable observability preserved),
        # and each keeps its original lifecycle_state so a later replay can
        # inspect the historical transition. The rebuild in raw_seq order is
        # what keeps ``record.state`` at IDLE.
        self.assertIn(1, by_raw_seq)
        self.assertIn(2, by_raw_seq)
        self.assertEqual(by_raw_seq[1].get("lifecycle_state"), "working")
        self.assertEqual(by_raw_seq[2].get("lifecycle_state"), "idle")

        # Walking normalized events in raw_seq order gives a monotonic
        # lifecycle progression that ends at the max-raw_seq authoritative
        # state.
        ordered_lifecycles = [
            row.get("lifecycle_state")
            for row in sorted(
                normalized_rows,
                key=lambda event: int(event.get("raw_seq", 0)),
            )
            if isinstance(row.get("lifecycle_state"), str)
        ]
        self.assertEqual(ordered_lifecycles, ["working", "idle"])

        # Simulate a fresh boot: a full store rebuild must not resurrect
        # the WORKING transition even though the recovered row is
        # physically at the end of the JSONL file.
        rebuilt = RunStore(self.paths)
        rebuilt_record = rebuilt.get(run_id)
        self.assertEqual(rebuilt_record.state, LifecycleState.IDLE)

    async def test_orphan_scan_preserves_later_blocking_state(self) -> None:
        """WIKI-232 REVIEW10 H1 variant: a blocking event (error) is more
        dangerous than IDLE because ``recover_on_start`` uses lifecycle
        state to decide whether to resume the provider. If the stale
        orphan re-applies WORKING over BLOCKED, the daemon would resume
        a run the provider already stopped."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-R10-MIDGAP-BLOCK",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="middle-gap blocked",
        )
        run_id = record.run_id

        # Raw seq 1: turn/started (would set lifecycle WORKING) — orphan.
        self.store.append_raw(
            run_id,
            provider=ProviderKind.CODEX.value,
            direction="provider",
            payload={
                "method": "turn/started",
                "params": {"turn": {"turnId": "midgap-blocked"}},
            },
            generation=1,
        )
        # Raw seq 2: fatal error (sets lifecycle BLOCKED) — already normalized.
        self.store.append_raw(
            run_id,
            provider=ProviderKind.CODEX.value,
            direction="provider",
            payload={
                "method": "error",
                "params": {"message": "provider crash", "willRetry": False},
            },
            generation=1,
        )
        self.store.append_normalized(
            run_id,
            raw_seq=2,
            disposition=EventDisposition.RENDERED,
            kind="codex_error",
            payload={"message": "provider crash"},
            lifecycle_state=LifecycleState.BLOCKED,
        )
        blocked_record = self.store.get(run_id)
        self.assertEqual(blocked_record.state, LifecycleState.BLOCKED)

        self.supervisor._sending_effects_reconciled = False  # noqa: SLF001
        await self.supervisor._normalize_orphan_raw_events()  # noqa: SLF001

        post_scan = self.store.get(run_id)
        self.assertEqual(post_scan.state, LifecycleState.BLOCKED)

        normalized_rows = self.store.read_normalized_events(run_id)
        by_raw_seq = {int(row.get("raw_seq", 0)): row for row in normalized_rows}
        self.assertIn(1, by_raw_seq)
        self.assertIn(2, by_raw_seq)
        self.assertEqual(by_raw_seq[1].get("lifecycle_state"), "working")
        self.assertEqual(by_raw_seq[2].get("lifecycle_state"), "blocked")

        # Fresh boot rebuild must not resurrect WORKING and mislead
        # ``recover_on_start`` into resuming a stopped provider.
        rebuilt = RunStore(self.paths)
        rebuilt_record = rebuilt.get(run_id)
        self.assertEqual(rebuilt_record.state, LifecycleState.BLOCKED)

    async def test_public_restart_path_recovers_wedged_send_end_to_end(
        self,
    ) -> None:
        """WIKI-232 REVIEW11 M2: exercise the full ``recover_on_start``
        contract. Prior boot-recovery coverage called private helpers
        with hand-authored effect / pending / raw rows — a regression
        in the public wiring (``run_daemon`` starting the recovery in
        the wrong order, ``command_queue.recover_pending`` failing to
        skip an already-acknowledged effect, a replaced ``run/start``
        wrapper losing the queued send_on_idle intent) would stay
        green. Drive the crash state through ``supervisor.dispatch``,
        tear down the current supervisor, spin up a fresh supervisor
        on the same ``RuntimePaths``, and call ``recover_on_start``
        once. Assert: (a) the orphan raw event is normalized exactly
        once, (b) the queue drains, (c) the steer effect ends
        ``acknowledged`` with a sent-shape result (not
        ``supervisor_restart_dropped_send``), (d) the durable send
        receipt is ok, (e) no second provider delivery fires."""

        # Start via public dispatch so the command log carries a
        # ``run/start`` receipt the restart path can replay.
        start_result = await self.supervisor.dispatch(
            "run/start",
            {
                "agent_id": "WIKI-232-R11-M2-PUBLIC",
                "provider": "codex",
                "role": "implement",
                "model": "fixture-codex",
                "effort": "high",
                "worktree": str(self.worktree),
                "prompt": "public restart integration",
                "request_id": "r11-m2-start",
            },
        )
        run_id = start_result["run_id"]
        record = self.store.get(run_id)
        adapter = self.supervisor.adapters[run_id]

        # Force adapter to WORKING so the public send_on_idle intent
        # queues instead of delivering immediately — matches the
        # production window in which a monitor steer arrives during a
        # provider turn.
        snapshot = adapter.snapshot()
        working = AdapterStatus(
            LifecycleState.WORKING,
            snapshot.session_id,
            os.getpid(),
            generation=snapshot.generation,
        )
        adapter._status = working  # noqa: SLF001 - queueing fixture
        self.store.update_adapter_status(run_id, working)

        pending_id = str(uuid4())
        effect_id = "r11-m2-send-effect"
        echoed_text = "public restart echo body"

        queued = await self.supervisor.dispatch(
            "run/send_on_idle",
            {
                "agent_id": record.agent_id,
                "run_id": run_id,
                "text": echoed_text,
                "pending_id": pending_id,
                "request_id": effect_id,
            },
        )
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(len(self.store.queued_messages(run_id)), 1)
        self.assertEqual(
            self.store.command_log.steer_effect_for_pending(run_id, pending_id)[
                "status"
            ],
            "queued",
        )

        # Simulate the crash between the drain's ``mark_sending`` step
        # and the composer echo landing normalized: the raw echo is
        # durable in JSONL but its normalized row never flushed.
        self.store.command_log.mark_steer_sending_for_pending(run_id, pending_id)
        self.store.track_pending_user_message(run_id, pending_id, echoed_text)
        raw_row = self.store.append_raw(
            run_id,
            provider=ProviderKind.CODEX.value,
            direction="provider",
            payload={
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": echoed_text}],
                    }
                },
            },
            generation=1,
        )
        pre_reconcile = self.store.command_log.steer_effect_for_pending(
            run_id, pending_id
        )
        assert pre_reconcile is not None
        self.assertEqual(pre_reconcile["status"], "sending")
        self.assertFalse(self.store.steer_delivery_observed(run_id, pending_id))

        # Snapshot pre-recovery totals so the "no second delivery"
        # invariant can be verified from the durable side (raw rows do
        # not grow, normalized log gains exactly one row for the orphan).
        pre_raw_count = self.store.get(run_id).raw_event_count
        pre_normalized_count = self.store.get(run_id).normalized_event_count

        # Simulate the daemon restart: tear down the current supervisor
        # and rebuild on the same on-disk paths.
        await self.supervisor.close()
        restarted_store = RunStore(self.paths)
        restarted = Supervisor(
            restarted_store,
            FixtureAdapterFactory(FIXTURES, pid=os.getpid()),
        )
        try:
            adapter_calls: list[str] = []

            # Wrap the adapter factory so we can watch send_on_idle on
            # the replacement adapter — recovery must not re-drive the
            # provider once the durable echo has been recovered.
            base_factory = restarted.adapter_factory

            def watching_factory(record_arg):
                fresh = base_factory(record_arg)
                original_send = fresh.send_on_idle

                async def tracked(msg: str):
                    adapter_calls.append(msg)
                    return await original_send(msg)

                fresh.send_on_idle = tracked  # type: ignore[method-assign]
                return fresh

            restarted.adapter_factory = watching_factory  # type: ignore[assignment]

            results = await restarted.recover_on_start()
            self.assertTrue(
                any(
                    result.get("run_id") == run_id
                    for result in results
                ),
                f"recover_on_start must report the recovered run: {results}",
            )

            # (a) Exactly one normalized row exists for the recovered
            # raw echo. No duplicates.
            normalized_rows = restarted_store.read_normalized_events(run_id)
            matching = [
                row
                for row in normalized_rows
                if isinstance(row.get("payload"), dict)
                and row["payload"].get("pending_id") == pending_id
            ]
            self.assertEqual(
                len(matching),
                1,
                f"exactly one normalized row for the recovered echo: {matching}",
            )
            self.assertEqual(int(matching[0]["raw_seq"]), int(raw_row["seq"]))

            # (b) Queue drained through the public path.
            self.assertEqual(restarted_store.queued_messages(run_id), [])

            # (c) Steer effect terminal with sent-shape result.
            resolved = restarted_store.command_log.steer_effect_for_pending(
                run_id, pending_id
            )
            assert resolved is not None
            self.assertEqual(resolved["status"], "acknowledged")
            result = resolved.get("result")
            if result is not None:
                self.assertNotEqual(
                    result.get("reason"),
                    "supervisor_restart_dropped_send",
                    "recovery must not drop a send whose echo was durable",
                )

            # (d) Durable receipt for the queued send_on_idle intent is ok.
            receipt = restarted_store.command_log.receipt(
                "run/send_on_idle", effect_id
            )
            self.assertIsNotNone(receipt)
            assert receipt is not None
            self.assertTrue(
                receipt.ok,
                f"send_on_idle receipt must be ok, got {receipt}",
            )

            # (e) No second provider delivery. raw_event_count is
            # unchanged (recovery only normalizes the existing orphan;
            # it must not append a fresh raw echo), and the replacement
            # adapter's send_on_idle was NOT invoked during recovery.
            post_raw_count = restarted_store.get(run_id).raw_event_count
            self.assertEqual(
                post_raw_count,
                pre_raw_count,
                "recovery must not append a duplicate raw echo",
            )
            self.assertEqual(
                adapter_calls,
                [],
                "recovery must not re-drive adapter.send_on_idle for a "
                "durably-observed echo",
            )
            post_normalized_count = restarted_store.get(run_id).normalized_event_count
            self.assertEqual(
                post_normalized_count,
                pre_normalized_count + 1,
                "recovery normalizes exactly the one orphan raw row",
            )
        finally:
            await restarted.close()

    async def test_orphan_scan_preserves_later_causal_projection(self) -> None:
        """WIKI-232 REVIEW11 H1: the suppression must cover every later
        causal event, not only lifecycle. Middle gap: raw seq 1 is an
        approval request (would add ``pending_requests[42]`` and set
        lifecycle WAITING_APPROVAL) with a dropped normalize; raw seq 2
        is the matching ``serverRequest/resolved`` (already normalized,
        no lifecycle_state). A lifecycle-only guard leaves the max
        applied lifecycle raw_seq at 0, so the recovered approval still
        runs its pending_request add and flips ``idle {}`` to
        ``waiting-approval [42]``. Recovering an unanswerable pending
        request wedges resume (``RESTART_RECOVERY_TABLE`` treats
        WAITING_APPROVAL as resumable) or, worse, resumes a run the
        provider already stopped."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-R11-MIDGAP-APPROVAL",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="middle-gap approval",
        )
        run_id = record.run_id

        # An earlier lifecycle event so ``record.state`` is a real IDLE
        # (matching the review's reproduction) and the marker reflects a
        # pre-approval baseline. Live path normally lands this before
        # any approval fires.
        self.store.append_raw(
            run_id,
            provider=ProviderKind.CODEX.value,
            direction="provider",
            payload={
                "method": "thread/status/changed",
                "params": {"status": {"type": "idle"}},
            },
            generation=1,
        )
        self.store.append_normalized(
            run_id,
            raw_seq=1,
            disposition=EventDisposition.RENDERED,
            kind="thread_status_changed",
            payload={"status": {"type": "idle"}},
            lifecycle_state=LifecycleState.IDLE,
        )

        # Raw seq 2: approval request (orphan). Live pump dropped this
        # normalize between raw fsync and normalized append.
        approval_request_id = 42
        approval_payload = {
            "method": "item/tool/requestUserInput",
            "id": approval_request_id,
            "params": {"questions": []},
        }
        self.store.append_raw(
            run_id,
            provider=ProviderKind.CODEX.value,
            direction="provider",
            payload=approval_payload,
            generation=1,
        )
        # Raw seq 3: serverRequest/resolved (already normalized). Carries
        # no lifecycle_state — the whole point of the finding is that a
        # lifecycle-only marker cannot see it.
        resolved_payload = {
            "method": "serverRequest/resolved",
            "params": {"requestId": approval_request_id, "result": "approve"},
        }
        self.store.append_raw(
            run_id,
            provider=ProviderKind.CODEX.value,
            direction="provider",
            payload=resolved_payload,
            generation=1,
        )
        self.store.append_normalized(
            run_id,
            raw_seq=3,
            disposition=EventDisposition.RENDERED,
            kind="approval_resolved",
            payload=resolved_payload,
        )

        pre_recovery = self.store.get(run_id)
        self.assertEqual(pre_recovery.state, LifecycleState.IDLE)
        self.assertEqual(pre_recovery.pending_requests, {})

        self.supervisor._sending_effects_reconciled = False  # noqa: SLF001
        await self.supervisor._normalize_orphan_raw_events()  # noqa: SLF001

        post_scan = self.store.get(run_id)
        self.assertEqual(
            post_scan.state,
            LifecycleState.IDLE,
            "recovered stale-order approval must not flip IDLE to "
            "WAITING_APPROVAL",
        )
        self.assertEqual(
            post_scan.pending_requests,
            {},
            "recovered stale-order approval must not resurrect a "
            "pending_request that raw_seq=3 serverRequest/resolved "
            "already cleared",
        )

        normalized_rows = self.store.read_normalized_events(run_id)
        by_raw_seq = {int(row.get("raw_seq", 0)): row for row in normalized_rows}
        # The orphan row is durably recovered so replay is complete.
        self.assertIn(2, by_raw_seq)
        self.assertEqual(by_raw_seq[2].get("kind"), "approval")
        self.assertEqual(by_raw_seq[2].get("lifecycle_state"), "waiting-approval")

        # Fresh boot rebuild must also preserve IDLE and empty pending.
        rebuilt = RunStore(self.paths)
        rebuilt_record = rebuilt.get(run_id)
        self.assertEqual(rebuilt_record.state, LifecycleState.IDLE)
        self.assertEqual(rebuilt_record.pending_requests, {})

    async def test_dedupe_owner_is_scoped_by_command_method(self) -> None:
        """WIKI-232 R2 H1: request IDs are legal once per supervisor
        method, so the dedupe owner must be method-scoped. A bare
        effect_id would let a run/send_on_idle call with the same
        request_id + dedupe_key reclaim a prior run/send_now claim and
        deliver a duplicate. The correct behavior is to surface the
        collision as deduplicated on the second call."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-XMODE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="cross-mode dedupe",
        )
        dedupe_key = "wiki-232-xmode:artifact-render:1"
        shared_request_id = "xmode-request-1"

        sent = await self.supervisor.send_now(
            record.run_id,
            "cross-mode message",
            dedupe_key=dedupe_key,
            effect_id=shared_request_id,
        )
        self.assertEqual(sent["status"], "sent")

        # Same request_id under the other method is a legal command-log
        # entry, but it carries the same dedupe_key so the second call
        # must be deduplicated instead of queuing a duplicate message.
        dupe = await self.supervisor.send_on_idle(
            record.run_id,
            "cross-mode message",
            dedupe_key=dedupe_key,
            effect_id=shared_request_id,
        )
        self.assertEqual(dupe["status"], "deduplicated")
        self.assertEqual(dupe["dedupe_key"], dedupe_key)

        entries = [
            entry
            for entry in self.store.get(record.run_id).message_dedupe_keys
            if entry.get("key") == dedupe_key
        ]
        self.assertEqual(len(entries), 1)
        self.assertEqual(
            entries[0].get("owner"),
            f"run/send_now:{shared_request_id}",
        )

    async def test_in_session_uncertain_head_drains_next_queued(self) -> None:
        """WIKI-232 R2 H2a: dropping an uncertain queue head from the
        in-session exception path must not strand later queued messages.
        Prior to the fix the drop returned without triggering another
        drain, so anything queued behind the failed head sat forever
        unless a fresh external idle event happened to fire."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-R2A",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="uncertain-head drain",
        )
        adapter = self.supervisor.adapters[record.run_id]
        snapshot = adapter.snapshot()

        # Force adapter non-idle so send_on_idle queues instead of delivering.
        working = AdapterStatus(
            LifecycleState.WORKING,
            snapshot.session_id,
            os.getpid(),
            generation=snapshot.generation,
        )
        adapter._status = working  # noqa: SLF001 - queueing fixture
        self.store.update_adapter_status(record.run_id, working)

        pid1 = str(uuid4())
        pid2 = str(uuid4())
        q1 = await self.supervisor.send_on_idle(
            record.run_id,
            "queued-1",
            pending_id=pid1,
            effect_id="r2a-1",
        )
        q2 = await self.supervisor.send_on_idle(
            record.run_id,
            "queued-2",
            pending_id=pid2,
            effect_id="r2a-2",
        )
        self.assertEqual(q1["status"], "queued")
        self.assertEqual(q2["status"], "queued")
        self.assertEqual(len(self.store.queued_messages(record.run_id)), 2)

        idle = AdapterStatus(
            LifecycleState.IDLE,
            snapshot.session_id,
            os.getpid(),
            generation=snapshot.generation,
        )
        adapter._status = idle  # noqa: SLF001 - drain fixture
        self.store.update_adapter_status(record.run_id, idle)

        original_send = adapter.send_on_idle
        call_log: list[str] = []

        async def flaky_send_on_idle(msg: str) -> AdapterStatus:
            call_log.append(msg)
            if msg == "queued-1":
                raise RuntimeError("simulated transient adapter failure")
            return await original_send(msg)

        adapter.send_on_idle = flaky_send_on_idle  # type: ignore[method-assign]
        try:
            await self.supervisor._deliver_next_queued_locked(  # noqa: SLF001
                record.run_id,
                adapter,
            )
            for _ in range(200):
                if not self.store.queued_messages(record.run_id):
                    break
                await asyncio.sleep(0.01)
        finally:
            adapter.send_on_idle = original_send  # type: ignore[method-assign]

        # Both attempts were made: the second WITHOUT any new
        # supervisor.send_on_idle call or synthetic idle event.
        self.assertEqual(call_log, ["queued-1", "queued-2"])
        self.assertEqual(self.store.queued_messages(record.run_id), [])

        first_effect = self.store.command_log.steer_effect_for_pending(
            record.run_id, pid1,
        )
        self.assertIsNotNone(first_effect)
        assert first_effect is not None
        self.assertEqual(first_effect["status"], "acknowledged")
        self.assertEqual(first_effect["result"]["status"], "uncertain")

        second_effect = self.store.command_log.steer_effect_for_pending(
            record.run_id, pid2,
        )
        self.assertIsNotNone(second_effect)
        assert second_effect is not None
        self.assertIn(second_effect["status"], {"sent", "acknowledged"})

    async def test_recover_on_start_uncertain_head_drains_next_queued(
        self,
    ) -> None:
        """WIKI-232 R2 H2b: same guarantee as the in-session path but
        for the restart sweep. After the sweep terminalizes a wedged
        head it must schedule delivery of the next queued message, so
        recovery does not leave a run stranded until the next external
        idle event."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-R2B",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="restart drain",
        )
        adapter = self.supervisor.adapters[record.run_id]
        pid1 = str(uuid4())

        # Wedge the first message in `sending` state — the CancelledError
        # trick used by the existing H2 test.
        with mock.patch.object(
            self.store.command_log,
            "mark_steer_sent_for_pending",
            side_effect=asyncio.CancelledError,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.supervisor.send_on_idle(
                    record.run_id,
                    "wedged-1",
                    pending_id=pid1,
                    effect_id="r2b-1",
                )

        wedged = self.store.command_log.steer_effect_for_pending(
            record.run_id, pid1,
        )
        self.assertIsNotNone(wedged)
        assert wedged is not None
        self.assertEqual(wedged["status"], "sending")
        self.assertEqual(len(self.store.queued_messages(record.run_id)), 1)

        # Adapter is WORKING after the wedge, so queue a second message
        # behind the stuck head via the normal send_on_idle path.
        pid2 = str(uuid4())
        q2 = await self.supervisor.send_on_idle(
            record.run_id,
            "queued-2",
            pending_id=pid2,
            effect_id="r2b-2",
        )
        self.assertEqual(q2["status"], "queued")
        self.assertEqual(len(self.store.queued_messages(record.run_id)), 2)

        # Return the adapter to IDLE so the post-sweep drain can deliver.
        snapshot = adapter.snapshot()
        idle = AdapterStatus(
            LifecycleState.IDLE,
            snapshot.session_id,
            os.getpid(),
            generation=snapshot.generation,
        )
        adapter._status = idle  # noqa: SLF001 - drain fixture
        self.store.update_adapter_status(record.run_id, idle)

        original_send = adapter.send_on_idle
        call_log: list[str] = []

        async def tracked_send(msg: str) -> AdapterStatus:
            call_log.append(msg)
            return await original_send(msg)

        adapter.send_on_idle = tracked_send  # type: ignore[method-assign]
        try:
            self.supervisor._sending_effects_reconciled = False  # noqa: SLF001
            await self.supervisor._reconcile_sending_steer_effects()  # noqa: SLF001
            for _ in range(200):
                if not self.store.queued_messages(record.run_id):
                    break
                await asyncio.sleep(0.01)
        finally:
            adapter.send_on_idle = original_send  # type: ignore[method-assign]

        # The second message drained without any new supervisor call
        # or synthetic idle event — only the sweep + its spawned drain.
        self.assertEqual(call_log, ["queued-2"])
        self.assertEqual(self.store.queued_messages(record.run_id), [])

        resolved = self.store.command_log.steer_effect_for_pending(
            record.run_id, pid1,
        )
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved["status"], "acknowledged")
        self.assertEqual(resolved["result"]["status"], "uncertain")

    async def test_r3_in_session_drain_preserves_queue_when_provider_busy(
        self,
    ) -> None:
        """WIKI-232 R3 H1: after an uncertain head drop the follow-up drain
        must NOT re-enter send_on_idle while the provider is still WORKING.
        Otherwise ProviderBusy re-enters the exception path, marks the next
        item uncertain, removes it, and cascades until the queue is empty.
        The queued items must be preserved until a real WORKING->IDLE
        transition drains them."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-R3A",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="r3 in-session drain",
        )
        adapter = self.supervisor.adapters[record.run_id]
        snapshot = adapter.snapshot()

        working = AdapterStatus(
            LifecycleState.WORKING,
            snapshot.session_id,
            os.getpid(),
            generation=snapshot.generation,
        )
        adapter._status = working  # noqa: SLF001 - queueing fixture
        self.store.update_adapter_status(record.run_id, working)

        pid1 = str(uuid4())
        pid2 = str(uuid4())
        pid3 = str(uuid4())
        for text, pid, eid in (
            ("queued-1", pid1, "r3a-1"),
            ("queued-2", pid2, "r3a-2"),
            ("queued-3", pid3, "r3a-3"),
        ):
            resp = await self.supervisor.send_on_idle(
                record.run_id, text, pending_id=pid, effect_id=eid,
            )
            self.assertEqual(resp["status"], "queued")
        self.assertEqual(len(self.store.queued_messages(record.run_id)), 3)

        # Enter the drain with the adapter IDLE so the first send is
        # attempted; the send fails, drops the head, and schedules a
        # follow-up drain. Between the head drop and the drain running
        # the adapter is switched to WORKING to simulate the provider
        # taking a new turn (or never returning to IDLE) — the drain
        # must observe WORKING and stop instead of stripping the queue.
        idle = AdapterStatus(
            LifecycleState.IDLE,
            snapshot.session_id,
            os.getpid(),
            generation=snapshot.generation,
        )
        adapter._status = idle  # noqa: SLF001 - drain fixture
        self.store.update_adapter_status(record.run_id, idle)

        original_send = adapter.send_on_idle
        call_log: list[str] = []

        async def flaky_send(msg: str) -> AdapterStatus:
            call_log.append(msg)
            if msg == "queued-1":
                # Fail the first send AND flip the adapter to WORKING so
                # the exception handler observes a busy provider by the
                # time the drain check runs.
                adapter._status = working  # noqa: SLF001 - simulate flip
                self.store.update_adapter_status(record.run_id, working)
                raise RuntimeError("simulated transient adapter failure")
            # Any subsequent send_on_idle from the drain must reject
            # because the provider is still working.
            raise ProviderBusy(
                f"provider is {adapter._status.state.value}, not idle"  # noqa: SLF001
            )

        adapter.send_on_idle = flaky_send  # type: ignore[method-assign]
        try:
            await self.supervisor._deliver_next_queued_locked(  # noqa: SLF001
                record.run_id, adapter,
            )
            # Give any scheduled drain task time to run (and to no-op).
            for _ in range(50):
                await asyncio.sleep(0.01)
        finally:
            adapter.send_on_idle = original_send  # type: ignore[method-assign]

        # Only the first send was attempted. The drain saw WORKING and
        # backed off — queued-2 and queued-3 stay in the durable queue.
        self.assertEqual(call_log, ["queued-1"])
        remaining = self.store.queued_messages(record.run_id)
        self.assertEqual([m["text"] for m in remaining], ["queued-2", "queued-3"])

        # queued-2 and queued-3 effects stay queued (not sending / not
        # acknowledged) so a later natural idle transition retries them.
        for pid in (pid2, pid3):
            effect = self.store.command_log.steer_effect_for_pending(
                record.run_id, pid,
            )
            self.assertIsNotNone(effect)
            assert effect is not None
            self.assertEqual(effect["status"], "queued")

        # Sanity: the dropped head IS terminated uncertain, matching R2.
        first = self.store.command_log.steer_effect_for_pending(
            record.run_id, pid1,
        )
        assert first is not None
        self.assertEqual(first["status"], "acknowledged")
        self.assertEqual(first["result"]["status"], "uncertain")

    async def test_r3_recover_on_start_drain_preserves_queue_when_provider_busy(
        self,
    ) -> None:
        """WIKI-232 R3 H1 restart path: same guarantee for the recovery
        sweep. After the sweep terminalizes a wedged head, the follow-up
        drain must gate on adapter IDLE. Otherwise ProviderBusy strips
        the entire queue one item at a time."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-R3B",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="r3 restart drain",
        )
        adapter = self.supervisor.adapters[record.run_id]
        pid1 = str(uuid4())

        # Wedge the first message in `sending` state (same trick as R2).
        with mock.patch.object(
            self.store.command_log,
            "mark_steer_sent_for_pending",
            side_effect=asyncio.CancelledError,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.supervisor.send_on_idle(
                    record.run_id,
                    "wedged-1",
                    pending_id=pid1,
                    effect_id="r3b-1",
                )

        wedged = self.store.command_log.steer_effect_for_pending(
            record.run_id, pid1,
        )
        assert wedged is not None
        self.assertEqual(wedged["status"], "sending")

        # Queue additional items behind the wedged head; adapter is WORKING
        # after the wedge so these all queue durably.
        pid2 = str(uuid4())
        pid3 = str(uuid4())
        for text, pid, eid in (
            ("queued-2", pid2, "r3b-2"),
            ("queued-3", pid3, "r3b-3"),
        ):
            resp = await self.supervisor.send_on_idle(
                record.run_id, text, pending_id=pid, effect_id=eid,
            )
            self.assertEqual(resp["status"], "queued")
        self.assertEqual(len(self.store.queued_messages(record.run_id)), 3)

        # Adapter stays WORKING through the recovery sweep — the drain
        # scheduled by the sweep must not re-enter send_on_idle.
        self.assertIs(adapter.snapshot().state, LifecycleState.WORKING)

        original_send = adapter.send_on_idle
        call_log: list[str] = []

        async def busy_send(msg: str) -> AdapterStatus:
            call_log.append(msg)
            raise ProviderBusy("provider still working")

        adapter.send_on_idle = busy_send  # type: ignore[method-assign]
        try:
            self.supervisor._sending_effects_reconciled = False  # noqa: SLF001
            await self.supervisor._reconcile_sending_steer_effects()  # noqa: SLF001
            for _ in range(50):
                await asyncio.sleep(0.01)
        finally:
            adapter.send_on_idle = original_send  # type: ignore[method-assign]

        # The sweep terminated the wedged head as uncertain and dropped
        # it, but did NOT attempt any further send_on_idle while the
        # adapter is still WORKING. Queue tail is preserved.
        self.assertEqual(call_log, [])
        remaining = self.store.queued_messages(record.run_id)
        self.assertEqual([m["text"] for m in remaining], ["queued-2", "queued-3"])

        # queued-2 and queued-3 effects stay queued for the natural
        # WORKING->IDLE transition to drain later.
        for pid in (pid2, pid3):
            effect = self.store.command_log.steer_effect_for_pending(
                record.run_id, pid,
            )
            assert effect is not None
            self.assertEqual(effect["status"], "queued")

        resolved = self.store.command_log.steer_effect_for_pending(
            record.run_id, pid1,
        )
        assert resolved is not None
        self.assertEqual(resolved["status"], "acknowledged")
        self.assertEqual(resolved["result"]["status"], "uncertain")

    async def test_r4_send_on_idle_reports_uncertain_when_adapter_rejects(
        self,
    ) -> None:
        """WIKI-232 R4 H1: dispatch-level surface. When the adapter is IDLE
        and the inline drain's ``send_on_idle`` raises, the queue head is
        removed as part of the uncertain-acknowledged termination in
        ``_deliver_next_queued_locked``. Prior to the fix, ``_send_on_idle``
        treated queue-slot removal as proof of provider acceptance and
        returned ``status=sent``. CommandQueue then recorded a successful
        sent receipt for a delivery that never touched the provider.
        The dispatch must return the terminal steer effect's result
        (``uncertain``) and the receipt must carry it forward."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-R4A",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="r4 uncertain on-idle",
        )
        adapter = self.supervisor.adapters[record.run_id]
        snapshot = adapter.snapshot()

        # Force the adapter IDLE so ``_send_on_idle`` takes the inline
        # drain branch. That's the only path that hits the buggy return
        # site — a WORKING adapter would return ``queued`` instead.
        idle = AdapterStatus(
            LifecycleState.IDLE,
            snapshot.session_id,
            os.getpid(),
            generation=snapshot.generation,
        )
        adapter._status = idle  # noqa: SLF001 - drain fixture
        self.store.update_adapter_status(record.run_id, idle)

        # Adapter rejects every inline delivery — mirrors a provider whose
        # transport dropped between IDLE observation and the actual send.
        original_send = adapter.send_on_idle
        provider_calls: list[str] = []

        async def rejecting_send(msg: str) -> AdapterStatus:
            provider_calls.append(msg)
            raise RuntimeError("simulated transport rejection")

        adapter.send_on_idle = rejecting_send  # type: ignore[method-assign]
        request_id = "r4-uncertain-1"
        try:
            result = await self.supervisor.dispatch(
                "run/send_on_idle",
                {
                    "run_id": record.run_id,
                    "text": "uncertain-me",
                    "request_id": request_id,
                },
            )
        finally:
            adapter.send_on_idle = original_send  # type: ignore[method-assign]

        # Dispatch return AND durable receipt both carry uncertain — proof
        # that the caller sees the terminal steer effect, not a spurious
        # ``sent``. The provider was called once (the failing attempt) and
        # never again.
        self.assertEqual(provider_calls, ["uncertain-me"])
        self.assertEqual(
            result.get("status"),
            "uncertain",
            f"dispatch must return uncertain when adapter rejects; got {result!r}",
        )
        receipt = self.store.command_log.receipt("run/send_on_idle", request_id)
        self.assertIsNotNone(receipt, "dispatch must persist a receipt")
        assert receipt is not None
        self.assertTrue(
            receipt.ok,
            "the command itself succeeded (no exception); only the delivery is uncertain",
        )
        self.assertIsInstance(receipt.result, dict)
        self.assertEqual(
            receipt.result.get("status"),
            "uncertain",
            f"receipt.result must carry uncertain, got {receipt.result!r}",
        )

        # The queue head was removed by the inline drain's
        # uncertain-acknowledged branch — that removal was the exact signal
        # the buggy return path misread as delivery success.
        self.assertEqual(self.store.queued_messages(record.run_id), [])

        # The terminal steer effect matches the returned result.
        terminal = self.store.command_log.steer_effect_for_pending(
            record.run_id, result.get("pending_id"),
        )
        self.assertIsNotNone(terminal)
        assert terminal is not None
        self.assertEqual(terminal["status"], "acknowledged")
        self.assertEqual(terminal["result"]["status"], "uncertain")

    async def test_send_on_idle_echo_before_error_records_sent(
        self,
    ) -> None:
        """REVIEW15 H2: deferred echo proves queued delivery before error."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-R15-IDLE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="accepted then error send_on_idle",
        )
        adapter = self.supervisor.adapters[record.run_id]
        snapshot = adapter.snapshot()
        idle = AdapterStatus(
            LifecycleState.IDLE,
            snapshot.session_id,
            os.getpid(),
            generation=snapshot.generation,
        )
        adapter._status = idle  # noqa: SLF001 - inline drain fixture
        self.store.update_adapter_status(record.run_id, idle)
        original_send = adapter.send_on_idle
        provider_calls: list[str] = []

        async def echo_then_raise(message: str) -> AdapterStatus:
            provider_calls.append(message)
            await self.supervisor._handle_provider_event(  # noqa: SLF001
                record.run_id,
                adapter,
                ProviderEvent(
                    ProviderKind.CODEX,
                    {
                        "method": "item/completed",
                        "params": {
                            "item": {
                                "type": "userMessage",
                                "content": [{"type": "text", "text": message}],
                            }
                        },
                    },
                ),
            )
            raise RuntimeError("response failed after queued acceptance")

        adapter.send_on_idle = echo_then_raise  # type: ignore[method-assign]
        request_id = "review15-idle-request"
        try:
            result = await self.supervisor.dispatch(
                "run/send_on_idle",
                {
                    "run_id": record.run_id,
                    "text": "queued accepted exactly once",
                    "source": "fleet-monitor",
                    "request_id": request_id,
                },
            )
        finally:
            adapter.send_on_idle = original_send  # type: ignore[method-assign]

        self.assertEqual(provider_calls, ["queued accepted exactly once"])
        self.assertEqual(result["status"], "sent")
        pending_id = result.get("pending_id")
        self.assertIsInstance(pending_id, str)
        self.assertEqual(self.store.queued_messages(record.run_id), [])
        composer = self.store.get(record.run_id).composer_messages
        matching_composer = [
            message for message in composer if message.get("pending_id") == pending_id
        ]
        self.assertEqual(len(matching_composer), 1)
        self.assertEqual(matching_composer[0].get("source"), "fleet-monitor")
        matching_normalized = [
            event
            for event in self.store.read_normalized_events(record.run_id)
            if isinstance(event.get("payload"), dict)
            and event["payload"].get("pending_id") == pending_id
        ]
        self.assertEqual(len(matching_normalized), 1)
        terminal = self.store.command_log.steer_effect_for_pending(
            record.run_id, str(pending_id)
        )
        self.assertIsNotNone(terminal)
        assert terminal is not None
        self.assertEqual(terminal["status"], "acknowledged")
        self.assertEqual(terminal["result"]["status"], "sent")
        receipt = self.store.command_log.receipt(
            "run/send_on_idle", request_id
        )
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertTrue(receipt.ok)
        self.assertEqual(receipt.result, result)

    async def test_send_on_idle_next_turn_echo_after_error_records_sent(
        self,
    ) -> None:
        """REVIEW17 H1: queued delivery waits for the event-pump echo."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-R17-IDLE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="next-turn echo after send_on_idle error",
        )
        adapter = self.supervisor.adapters[record.run_id]
        snapshot = adapter.snapshot()
        idle = AdapterStatus(
            LifecycleState.IDLE,
            snapshot.session_id,
            os.getpid(),
            generation=snapshot.generation,
        )
        adapter._status = idle  # noqa: SLF001 - inline drain fixture
        self.store.update_adapter_status(record.run_id, idle)
        original_send = adapter.send_on_idle
        provider_calls: list[str] = []

        async def schedule_echo_then_raise(message: str) -> AdapterStatus:
            provider_calls.append(message)
            event = ProviderEvent(
                ProviderKind.CODEX,
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "userMessage",
                            "content": [{"type": "text", "text": message}],
                        }
                    },
                },
            )
            asyncio.get_running_loop().call_soon(
                adapter._events.put_nowait,  # noqa: SLF001 - pump boundary fixture
                event,
            )
            raise RuntimeError("response failed before queued echo drained")

        adapter.send_on_idle = schedule_echo_then_raise  # type: ignore[method-assign]
        request_id = "review17-idle-next-turn-request"
        try:
            result = await self.supervisor.dispatch(
                "run/send_on_idle",
                {
                    "run_id": record.run_id,
                    "text": "queued echo on next turn",
                    "source": "fleet-monitor",
                    "request_id": request_id,
                },
            )
        finally:
            adapter.send_on_idle = original_send  # type: ignore[method-assign]

        self.assertEqual(provider_calls, ["queued echo on next turn"])
        self.assertEqual(result["status"], "sent")
        pending_id = result.get("pending_id")
        self.assertIsInstance(pending_id, str)
        receipt = self.store.command_log.receipt("run/send_on_idle", request_id)
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertTrue(receipt.ok)
        self.assertEqual(receipt.result, result)
        self.assertEqual(self.store.queued_messages(record.run_id), [])

        composer = self.store.get(record.run_id).composer_messages
        matching_composer = [
            message for message in composer if message.get("pending_id") == pending_id
        ]
        self.assertEqual(len(matching_composer), 1)
        self.assertEqual(matching_composer[0].get("source"), "fleet-monitor")
        matching_normalized = [
            event
            for event in self.store.read_normalized_events(record.run_id)
            if isinstance(event.get("payload"), dict)
            and event["payload"].get("pending_id") == pending_id
        ]
        self.assertEqual(len(matching_normalized), 1)
        self.assertEqual(
            matching_normalized[0]["payload"].get("source"), "fleet-monitor"
        )

    async def test_send_on_idle_normalize_failure_recovers_sent_receipt(
        self,
    ) -> None:
        """REVIEW18 H1: queued raw echo recovery completes its command."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-R18-IDLE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="recover send_on_idle normalization",
        )
        record = await _wait_for_events(self.store, record.run_id, 10)
        adapter = self.supervisor.adapters[record.run_id]
        snapshot = adapter.snapshot()
        idle = AdapterStatus(
            LifecycleState.IDLE,
            snapshot.session_id,
            os.getpid(),
            generation=snapshot.generation,
        )
        adapter._status = idle  # noqa: SLF001 - inline drain fixture
        self.store.update_adapter_status(record.run_id, idle)
        original_send = adapter.send_on_idle
        provider_calls: list[str] = []

        async def schedule_echo_then_raise(message: str) -> AdapterStatus:
            provider_calls.append(message)
            event = ProviderEvent(
                ProviderKind.CODEX,
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "userMessage",
                            "content": [{"type": "text", "text": message}],
                        }
                    },
                },
            )
            asyncio.get_running_loop().call_soon(
                adapter._events.put_nowait,  # noqa: SLF001 - pump boundary fixture
                event,
            )
            raise RuntimeError("transport failed after durable queued raw echo")

        adapter.send_on_idle = schedule_echo_then_raise  # type: ignore[method-assign]
        real_append = self.store.append_normalized
        failed_appends = 0

        def fail_first_append(*args, **kwargs):
            nonlocal failed_appends
            if failed_appends == 0:
                failed_appends += 1
                raise OSError("fixture queued normalized append failure")
            return real_append(*args, **kwargs)

        request_id = "review18-send-on-idle-request"
        params = {
            "run_id": record.run_id,
            "text": "recover this queued echo",
            "source": "fleet-monitor",
            "request_id": request_id,
        }
        try:
            with mock.patch.object(
                self.store,
                "append_normalized",
                side_effect=fail_first_append,
            ):
                with self.assertRaisesRegex(
                    CommandRetryable, "normalization did not commit"
                ):
                    await self.supervisor.dispatch("run/send_on_idle", params)
        finally:
            adapter.send_on_idle = original_send  # type: ignore[method-assign]

        self.assertEqual(failed_appends, 1)
        self.assertEqual(provider_calls, ["recover this queued echo"])
        self.assertIsNone(
            self.store.command_log.receipt("run/send_on_idle", request_id)
        )
        self.assertEqual(
            [command.request_id for command in self.store.command_log.pending()],
            [request_id],
        )

        await self.supervisor.recover_on_start()
        replay = await self.supervisor.dispatch("run/send_on_idle", dict(params))

        self.assertEqual(replay["status"], "sent")
        self.assertEqual(provider_calls, ["recover this queued echo"])
        receipt = self.store.command_log.receipt("run/send_on_idle", request_id)
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertTrue(receipt.ok)
        self.assertEqual(receipt.result, replay)
        self.assertEqual(self.store.queued_messages(record.run_id), [])
        pending_id = replay.get("pending_id")
        self.assertIsInstance(pending_id, str)
        matching_normalized = [
            event
            for event in self.store.read_normalized_events(record.run_id)
            if isinstance(event.get("payload"), dict)
            and event["payload"].get("pending_id") == pending_id
        ]
        self.assertEqual(len(matching_normalized), 1)
        matching_composer = [
            message
            for message in self.store.get(record.run_id).composer_messages
            if message.get("pending_id") == pending_id
        ]
        self.assertEqual(len(matching_composer), 1)
        self.assertEqual(matching_composer[0].get("source"), "fleet-monitor")

    async def test_r5_provider_busy_after_idle_snapshot_preserves_queue(
        self,
    ) -> None:
        """WIKI-232 R5 H1: the R3 idle-snapshot gate is TOCTOU. The
        provider can flip WORKING between ``snapshot() == IDLE`` and
        ``send_on_idle``'s authoritative state check, which then raises
        ``ProviderBusy``. Prior to the fix, that raise landed in the
        generic ``except Exception`` handler: the effect was terminalized
        ``uncertain`` and the queue head was popped even though the
        provider explicitly rejected the send. ``ProviderBusy`` must be
        treated as known non-acceptance — the queue entry stays and the
        steer effect returns to ``queued`` so the next real WORKING->IDLE
        transition retries it."""

        record = await self.supervisor.start_run(
            agent_id="WIKI-232-R5A",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="r5 provider-busy after idle snapshot",
        )
        adapter = self.supervisor.adapters[record.run_id]
        snapshot = adapter.snapshot()

        # Prime the durable queue with an effect-bound entry. Force the
        # adapter to WORKING first so ``send_on_idle`` queues instead of
        # inline-delivers.
        working = AdapterStatus(
            LifecycleState.WORKING,
            snapshot.session_id,
            os.getpid(),
            generation=snapshot.generation,
        )
        adapter._status = working  # noqa: SLF001 - queueing fixture
        self.store.update_adapter_status(record.run_id, working)

        pending_id = str(uuid4())
        resp = await self.supervisor.send_on_idle(
            record.run_id,
            "busy-race",
            pending_id=pending_id,
            effect_id="r5-busy-1",
        )
        self.assertEqual(resp["status"], "queued")

        # Flip the adapter to IDLE so both the drain's entry-snapshot gate
        # AND the fresh-snapshot recheck above ``mark_sending`` observe
        # IDLE. The race lives in the tiny window between that recheck
        # and the actual ``send_on_idle`` call.
        idle = AdapterStatus(
            LifecycleState.IDLE,
            snapshot.session_id,
            os.getpid(),
            generation=snapshot.generation,
        )
        adapter._status = idle  # noqa: SLF001 - drain fixture
        self.store.update_adapter_status(record.run_id, idle)

        original_send = adapter.send_on_idle
        provider_calls: list[str] = []
        later_event_tasks: list[asyncio.Task[None]] = []
        before_raw = self.store.get(record.run_id).raw_event_count
        before_normalized = self.store.get(record.run_id).normalized_event_count

        async def busy_after_snapshot(msg: str) -> AdapterStatus:
            provider_calls.append(msg)
            # Simulate the race: the provider raced back to WORKING
            # between our IDLE snapshot and this authoritative check.
            adapter._status = working  # noqa: SLF001 - simulate flip
            self.store.update_adapter_status(record.run_id, working)
            # The provider echo can arrive before the authoritative status
            # refresh raises ProviderBusy. It must wait for the attempt
            # outcome instead of acknowledging this rejected send.
            await self.supervisor._handle_provider_event(  # noqa: SLF001
                record.run_id,
                adapter,
                ProviderEvent(
                    ProviderKind.CODEX,
                    {
                        "method": "item/completed",
                        "params": {
                            "item": {
                                "type": "userMessage",
                                "content": [
                                    {"type": "text", "text": "busy-race"}
                                ],
                            }
                        },
                    },
                ),
            )
            later_event_tasks.append(
                asyncio.create_task(
                    self.supervisor._handle_provider_event(  # noqa: SLF001
                        record.run_id,
                        adapter,
                        ProviderEvent(
                            ProviderKind.CODEX,
                            {"method": "fixture/later-event"},
                        ),
                    )
                )
            )
            await asyncio.sleep(0)
            raise ProviderBusy("provider raced to WORKING after IDLE snapshot")

        adapter.send_on_idle = busy_after_snapshot  # type: ignore[method-assign]
        try:
            await self.supervisor._deliver_next_queued_locked(  # noqa: SLF001
                record.run_id, adapter,
            )
        finally:
            adapter.send_on_idle = original_send  # type: ignore[method-assign]
        await asyncio.gather(*later_event_tasks)

        def is_review7_event(event: dict[str, Any]) -> bool:
            payload = event["payload"]
            if payload.get("method") == "fixture/later-event":
                return True
            if payload.get("method") != "item/completed":
                return False
            params = payload.get("params")
            item = params.get("item") if isinstance(params, dict) else None
            content = item.get("content") if isinstance(item, dict) else None
            return bool(
                isinstance(content, list)
                and content
                and isinstance(content[0], dict)
                and content[0].get("text") == "busy-race"
            )

        raw_events = [
            event
            for event in self.store.read_raw_events(record.run_id)
            if event["seq"] > before_raw and is_review7_event(event)
        ]
        raw_seqs = {event["seq"] for event in raw_events}
        normalized_events = [
            event
            for event in self.store.read_normalized_events(record.run_id)
            if event["seq"] > before_normalized and event["raw_seq"] in raw_seqs
        ]
        self.assertEqual(len(raw_events), 2)
        self.assertEqual(len(normalized_events), 2)
        self.assertEqual(
            [event["payload"].get("method") for event in raw_events],
            ["item/completed", "fixture/later-event"],
        )
        self.assertEqual(
            [event["raw_seq"] for event in normalized_events],
            [event["seq"] for event in raw_events],
        )

        # ProviderBusy was raised once — the fix must NOT retry inside the
        # same drain call.
        self.assertEqual(provider_calls, ["busy-race"])

        # Queue entry preserved: the provider REJECTED, so the entry was
        # never delivered and must stay for a later drain to retry.
        remaining = self.store.queued_messages(record.run_id)
        self.assertEqual(
            [m["text"] for m in remaining],
            ["busy-race"],
            "known-rejected head must stay in the durable queue",
        )
        self.assertEqual(
            remaining[0].get("pending_id"),
            pending_id,
            "queue entry identity preserved for retry",
        )

        # Steer effect returned to ``queued`` — NOT acknowledged/uncertain.
        # The R3 comment explicitly notes that any ``sending`` effect
        # would otherwise be terminalized in the R2 uncertain-drop branch.
        effect = self.store.command_log.steer_effect_for_pending(
            record.run_id, pending_id,
        )
        self.assertIsNotNone(effect)
        assert effect is not None
        self.assertEqual(
            effect["status"],
            "queued",
            f"ProviderBusy must revert sending->queued, got {effect['status']!r}",
        )
        self.assertNotEqual(
            effect["status"],
            "acknowledged",
            "known non-acceptance must NOT terminalize the effect",
        )
        self.assertEqual(
            self.store.get(record.run_id).pending_user_messages,
            [],
            "known rejection must discard the pending echo matcher",
        )

        # Retry path: a natural WORKING->IDLE transition drains the same
        # entry successfully. Restore the real send_on_idle so the drain
        # actually delivers, flip to IDLE, and rerun the drain.
        adapter._status = idle  # noqa: SLF001 - retry fixture
        self.store.update_adapter_status(record.run_id, idle)
        retry_calls: list[str] = []

        async def tracked_retry(message: str) -> AdapterStatus:
            retry_calls.append(message)
            return await original_send(message)

        adapter.send_on_idle = tracked_retry  # type: ignore[method-assign]
        try:
            await self.supervisor._deliver_next_queued_locked(  # noqa: SLF001
                record.run_id, adapter,
            )
        finally:
            adapter.send_on_idle = original_send  # type: ignore[method-assign]
        self.assertEqual(retry_calls, ["busy-race"])
        self.assertEqual(
            self.store.queued_messages(record.run_id),
            [],
            "the retry drain must deliver the preserved head once the provider is really IDLE",
        )
        delivered = self.store.command_log.steer_effect_for_pending(
            record.run_id, pending_id,
        )
        assert delivered is not None
        self.assertIn(
            delivered["status"],
            {"sent", "acknowledged"},
            f"retry must terminalize the effect as sent/acknowledged, got {delivered['status']!r}",
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

    async def test_handover_reemits_and_answers_approval_through_real_adapters(self) -> None:
        await self.supervisor.close()
        env_root = self.root / "real-adapter-env"
        env_root.mkdir()
        codex_env = os.environ.copy()
        codex_env.update(
            {
                "HOME": str(env_root / "codex-home"),
                "CODEX_HOME": str(env_root / "codex"),
                "FAKE_PROTOCOL_LOG": str(env_root / "codex-protocol.jsonl"),
                "FAKE_CODEX_TRANSCRIPT_DIR": str(env_root / "codex-sessions"),
                "FAKE_CODEX_APPROVAL": "1",
            }
        )
        claude_env = os.environ.copy()
        claude_env.update(
            {
                "HOME": str(env_root / "claude-home"),
                "CLAUDE_CONFIG_DIR": str(env_root / "claude-config"),
                "FAKE_PROTOCOL_LOG": str(env_root / "claude-protocol.jsonl"),
            }
        )

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

        async def no_identity(
            _pid: int | None,
            _provider: ProviderKind,
            _session_id: str | None,
            *,
            reported_path: str | None = None,
        ) -> ProviderProcessIdentity | None:
            del reported_path
            return None

        def factory(record: RunRecord):
            if record.provider is ProviderKind.CODEX:
                return CodexAppServerAdapter(
                    record,
                    command=(
                        sys.executable,
                        "-u",
                        str(FIXTURES / "fake_codex_app_server.py"),
                    ),
                    env=codex_env,
                    request_timeout=1,
                    identity_resolver=identity,
                )
            return ClaudeStreamAdapter(
                record,
                command=(
                    sys.executable,
                    "-u",
                    str(FIXTURES / "fake_claude_stream.py"),
                ),
                env=claude_env,
                request_timeout=1,
                identity_resolver=no_identity,
            )

        self.supervisor = Supervisor(
            self.store,
            factory,
            pid_alive=lambda _pid: False,
        )
        cases = (
            (
                ProviderKind.CODEX,
                "WIKI-REAL-CODEX-APPROVAL",
                "thread-recovery",
                "int:old",
                {
                    "request_id": "old",
                    "request_kind": "item/tool/requestUserInput",
                    "payload": {
                        "method": "item/tool/requestUserInput",
                        "id": "old",
                        "params": {"questions": [{"question": "Which surface?"}]},
                    },
                },
                0,
                {"answers": {"wiki_surface": {"answers": ["Agents page"]}}},
            ),
            (
                ProviderKind.CLAUDE,
                "WIKI-REAL-CLAUDE-APPROVAL",
                "session-recovery",
                "str:old",
                {
                    "request_id": "old",
                    "request_kind": "can_use_tool",
                    "payload": {
                        "type": "control_request",
                        "request_id": "old",
                        "request": {"subtype": "can_use_tool"},
                    },
                },
                "permission-1",
                {
                    "behavior": "deny",
                    "message": "Denied by handover test",
                    "interrupt": False,
                    "toolUseID": "toolu_fixture",
                },
            ),
        )
        for (
            provider,
            agent_id,
            session_id,
            pending_key,
            pending,
            new_request_id,
            response,
        ) in cases:
            with self.subTest(provider=provider.value):
                record = RunRecord.new(
                    agent_id=agent_id,
                    provider=provider,
                    role="implement",
                    model="fixture-model",
                    effort="high" if provider is ProviderKind.CODEX else None,
                    worktree=str(self.worktree),
                    prompt="handover approval",
                )
                record.state = LifecycleState.WAITING_APPROVAL
                record.provider_session_id = session_id
                record.provider_pid = 999_000 + len(cases)
                record.provider_generation = 1
                record.pending_requests[pending_key] = pending
                self.store.create(record)

                results = await self.supervisor.recover_on_start()
                self.assertEqual(
                    next(item for item in results if item["run_id"] == record.run_id)[
                        "action"
                    ],
                    "resume",
                )
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    current = self.store.get(record.run_id)
                    if current.state is LifecycleState.WAITING_APPROVAL and current.pending_requests:
                        break
                    await asyncio.sleep(0.01)
                current = self.store.get(record.run_id)
                self.assertEqual(current.state, LifecycleState.WAITING_APPROVAL)
                self.assertTrue(current.pending_requests)
                await self.supervisor.respond(record.run_id, new_request_id, response)

                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    current = self.store.get(record.run_id)
                    if not current.pending_requests and current.state is not LifecycleState.WAITING_APPROVAL:
                        break
                    await asyncio.sleep(0.01)
                current = self.store.get(record.run_id)
                self.assertFalse(current.pending_requests)
                self.assertNotEqual(current.state, LifecycleState.WAITING_APPROVAL)

    async def test_failed_approval_recovery_drains_and_preserves_request(self) -> None:
        await self.supervisor.close()

        def factory(_record: RunRecord) -> ApprovalRecoveryStallAdapter:
            return ApprovalRecoveryStallAdapter(
                FIXTURES / "codex_app_server_success.jsonl",
                FIXTURES / "codex_app_server_control.jsonl",
                pid=os.getpid(),
            )

        self.supervisor = Supervisor(
            self.store,
            factory,
            pid_alive=lambda _pid: False,
            approval_recovery_timeout_seconds=0.05,
        )
        record = RunRecord.new(
            agent_id="WIKI-APPROVAL-RECOVERY-STALL",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-model",
            effort="high",
            worktree=str(self.worktree),
            prompt="handover approval",
        )
        record.state = LifecycleState.WAITING_APPROVAL
        record.provider_session_id = "thread-recovery-stall"
        record.provider_pid = 999_001
        record.provider_generation = 1
        record.pending_requests["int:7"] = {
            "request_id": 7,
            "request_kind": "item/tool/requestUserInput",
            "payload": {
                "method": "item/tool/requestUserInput",
                "id": 7,
                "params": {"questions": [{"question": "Which surface?"}]},
            },
        }
        original_pending = dict(record.pending_requests)
        self.store.create(record)

        recovery = await self.supervisor.recover_on_start()
        result = next(item for item in recovery if item["run_id"] == record.run_id)
        self.assertEqual(result["action"], "block")
        current = self.store.get(record.run_id)
        self.assertEqual(current.state, LifecycleState.BLOCKED)
        self.assertEqual(current.recovery_from_state, LifecycleState.WAITING_APPROVAL)
        self.assertTrue(current.automatic_resume_suppressed)
        self.assertEqual(current.pending_requests, original_pending)
        self.assertNotIn(record.run_id, self.supervisor.adapters)

        self.store.clear_automatic_resume_suppression(record.run_id)
        with self.assertRaises(StoreConflict):
            await self.supervisor.resume_run(record.run_id)
        current = self.store.get(record.run_id)
        self.assertEqual(current.pending_requests, original_pending)
        self.assertNotIn(record.run_id, self.supervisor.adapters)

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

    async def test_start_failure_rolls_back_the_registered_run(
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
        self.assertIsNone(self.store.current_run_id("WIKI-START-FAIL"))
        self.assertFalse(
            any(
                path.name == "run.json"
                for path in self.paths.runs_dir.glob("*/run.json")
            )
        )

    async def test_start_failure_removes_status_written_during_provider_start(self) -> None:
        status_path = self.store.status_path("WIKI-STATUS-ROLLBACK")
        original_factory = self.supervisor.adapter_factory

        def factory(record: RunRecord):
            adapter = original_factory(record)
            assert isinstance(adapter, CodexFixtureAdapter)

            async def write_status_then_fail(request: StartRequest) -> AdapterStatus:
                del request
                status_path.parent.mkdir(parents=True, exist_ok=True)
                status_path.write_text('{"state":"working"}\n', encoding="utf-8")
                raise RuntimeError("status-writing fixture failure")

            adapter.start = write_status_then_fail  # type: ignore[method-assign]
            return adapter

        self.supervisor.adapter_factory = factory
        with self.assertRaisesRegex(ProviderProcessError, "status-writing fixture failure"):
            await self.supervisor.start_run(
                agent_id="WIKI-STATUS-ROLLBACK",
                provider=ProviderKind.CODEX,
                role="implement",
                model="fixture-codex",
                effort="high",
                worktree=str(self.worktree),
                prompt="status must roll back with the failed start",
            )

        self.assertIsNone(self.store.current_run_id("WIKI-STATUS-ROLLBACK"))
        self.assertFalse(status_path.exists())

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

    async def test_replace_replay_waits_for_replacement_provider_control(self) -> None:
        old = await self.supervisor.start_run(
            agent_id="WIKI-REPLACE-OWNERSHIP",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="original ownership prompt",
        )
        replacement_id = str(uuid4())
        payload = {
            "run_id": old.run_id,
            "replacement_run_id": replacement_id,
            "prompt": "replacement ownership prompt",
        }
        command = AgentCommand.replace(
            agent_id=old.agent_id,
            request_id="replace-ownership",
            payload=payload,
        )
        replacement = await self.supervisor.replace(
            old.run_id,
            payload["prompt"],
            replacement_run_id=replacement_id,
            effect_id=command.request_id,
            command_hash=command.command_hash,
        )
        adapter = self.supervisor.adapters[replacement.run_id]
        await self.supervisor._detach_adapter(replacement.run_id)  # noqa: SLF001

        with self.assertRaises(CommandRetryable):
            await self.supervisor._dispatch(  # noqa: SLF001
                "run/replace",
                {
                    **payload,
                    "agent_id": old.agent_id,
                    "request_id": command.request_id,
                },
                command_hash=command.command_hash,
            )

        self.supervisor._attach_adapter(replacement.run_id, adapter)  # noqa: SLF001
        replayed = await self.supervisor._dispatch(  # noqa: SLF001
            "run/replace",
            {
                **payload,
                "agent_id": old.agent_id,
                "request_id": command.request_id,
            },
            command_hash=command.command_hash,
        )
        self.assertEqual(replayed["run_id"], replacement.run_id)
        self.assertEqual(
            self.store.command_log.replace_effect(
                "run/replace",
                command.request_id,
                agent_id=old.agent_id,
                command_hash=command.command_hash,
            )["status"],
            "completed",
        )

    async def test_start_replay_keeps_uncommitted_retained_run_pending_after_restart(
        self,
    ) -> None:
        request_id = "start-retained-uncommitted"
        record = RunRecord.new(
            agent_id="WIKI-START-RETAINED",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            worktree=str(self.worktree),
            prompt="retained uncommitted start",
            run_id=str(uuid4()),
            start_request_id=request_id,
        )
        command = AgentCommand.spawn(
            agent_id=record.agent_id,
            request_id=request_id,
            payload={
                "agent_id": record.agent_id,
                "provider": record.provider.value,
                "role": record.role,
                "model": record.model,
                "worktree": record.worktree,
                "prompt": record.initial_prompt or "retained uncommitted start",
                "run_id": record.run_id,
            },
        )
        self.store.command_log.append_intent(command, {record.agent_id: None})
        self.store.create(record, transactional_start=True)
        record = self.store.get(record.run_id)
        record.provider_pid = os.getpid()
        self.store._write_record(record)  # noqa: SLF001 - uncertain live provider fixture
        self.store.transition(
            record.run_id,
            LifecycleState.BLOCKED,
            reason="provider identity is uncertain after restart",
        )

        restarted_store = RunStore(self.paths)
        restarted = Supervisor(
            restarted_store,
            FixtureAdapterFactory(FIXTURES, pid=os.getpid()),
        )
        try:
            await restarted.command_queue.recover_pending()
            self.assertEqual(restarted_store.command_log.pending(), [command])
            self.assertIsNone(
                restarted_store.command_log.receipt("run/start", request_id)
            )
            retained = restarted_store.get(record.run_id)
            self.assertEqual(retained.state, LifecycleState.BLOCKED)
            self.assertIsNotNone(retained.start_transaction)
            self.assertIsNone(restarted_store.find_start_request(request_id))
        finally:
            await restarted.close()

    async def test_recovery_retries_uncommitted_start_cleanup_after_pid_exit(
        self,
    ) -> None:
        request_id = "start-retry-after-pid-exit"
        record = RunRecord.new(
            agent_id="WIKI-START-RETRY-AFTER-EXIT",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            worktree=str(self.worktree),
            prompt="retry uncommitted start after provider exit",
            run_id=str(uuid4()),
            start_request_id=request_id,
        )
        command = AgentCommand.spawn(
            agent_id=record.agent_id,
            request_id=request_id,
            payload={
                "agent_id": record.agent_id,
                "provider": record.provider.value,
                "role": record.role,
                "model": record.model,
                "worktree": record.worktree,
                "prompt": record.initial_prompt or "retry uncommitted start after provider exit",
                "run_id": record.run_id,
            },
        )
        self.store.command_log.append_intent(command, {record.agent_id: None})
        self.store.create(record, transactional_start=True)
        provider = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        restarted: Supervisor | None = None
        try:
            record = self.store.get(record.run_id)
            record.provider_pid = provider.pid
            self.store._write_record(record)  # noqa: SLF001 - uncertain live provider fixture
            self.store.transition(
                record.run_id,
                LifecycleState.BLOCKED,
                reason="provider identity is uncertain after restart",
            )

            restarted_store = RunStore(self.paths)
            restarted = Supervisor(
                restarted_store,
                FixtureAdapterFactory(FIXTURES, pid=os.getpid()),
            )
            first = await restarted.recover_on_start()
            self.assertEqual(first[0]["action"], "block")
            self.assertIsNotNone(restarted_store.get(record.run_id).start_transaction)
            self.assertEqual(restarted_store.command_log.pending(), [command])
            self.assertIsNone(
                restarted_store.command_log.receipt("run/start", request_id)
            )

            provider.terminate()
            provider.wait(timeout=5)
            second = await restarted.recover_on_start()

            self.assertEqual(restarted_store.list_runs(), [restarted_store.get(record.run_id)])
            current = restarted_store.get(record.run_id)
            self.assertEqual(current.run_id, record.run_id)
            self.assertIsNone(current.start_transaction)
            self.assertEqual(second, [])
            self.assertEqual(restarted_store.command_log.pending(), [])
            receipt = restarted_store.command_log.receipt("run/start", request_id)
            self.assertIsNotNone(receipt)
            assert receipt is not None
            self.assertTrue(receipt.ok)
        finally:
            if provider.poll() is None:
                provider.kill()
                provider.wait(timeout=5)
            if restarted is not None:
                await restarted.close()

    async def test_recovery_poll_skips_in_flight_local_start(self) -> None:
        release_start = asyncio.Event()
        start_entered = asyncio.Event()

        class PausedStartClaudeAdapter(ClaudeFixtureAdapter):
            async def start(self, request: StartRequest) -> AdapterStatus:
                start_entered.set()
                await release_start.wait()
                return await super().start(request)

        class PausedStartFactory(FixtureAdapterFactory):
            def __call__(self, record: RunRecord) -> ProviderAdapter:
                if record.provider is ProviderKind.CLAUDE:
                    return PausedStartClaudeAdapter(
                        self.fixture_dir / "claude_stream_native_surfaces.jsonl",
                        pid=self.pid,
                        generation=record.provider_generation,
                    )
                return super().__call__(record)

        await self.supervisor.close()
        self.supervisor = Supervisor(
            self.store,
            PausedStartFactory(FIXTURES, pid=os.getpid()),
        )

        start_task = asyncio.create_task(
            self.supervisor.start_run(
                agent_id="WIKI-PAUSED-START-INFLIGHT",
                provider=ProviderKind.CLAUDE,
                role="implement",
                model="fixture-claude",
                effort=None,
                worktree=str(self.worktree),
                prompt="paused start survives recovery poll",
            )
        )
        recovery_task: asyncio.Task[list[dict[str, str]]] | None = None
        try:
            await asyncio.wait_for(start_entered.wait(), timeout=2)

            runs = self.store.list_runs()
            self.assertEqual(len(runs), 1)
            in_flight = runs[0]
            self.assertIsNotNone(in_flight.start_transaction)
            self.assertIn(in_flight.run_id, self.supervisor.adapters)
            run_dir = self.store.run_dir(in_flight.run_id)
            self.assertTrue(run_dir.exists())

            abort_called = asyncio.Event()
            original_abort = self.store.abort_uncommitted_starts

            def instrumented_abort() -> list[str]:
                result = original_abort()
                abort_called.set()
                return result

            self.store.abort_uncommitted_starts = instrumented_abort  # type: ignore[method-assign]

            recovery_task = asyncio.create_task(self.supervisor.recover_on_start())
            await asyncio.wait_for(abort_called.wait(), timeout=2)

            # The periodic recovery must not race the launch it does not own.
            self.assertIn(in_flight.run_id, self.supervisor.adapters)
            self.assertTrue(run_dir.exists())
            persisted = self.store.get(in_flight.run_id)
            self.assertIsNotNone(persisted.start_transaction)
            self.assertEqual(
                self.store.current_run_id(in_flight.agent_id),
                in_flight.run_id,
            )
            self.assertIsNone(self.store.command_log.receipt("run/start", None))

            # Release the paused provider start; commit_start must still succeed.
            release_start.set()
            record = await asyncio.wait_for(start_task, timeout=5)
            await asyncio.wait_for(recovery_task, timeout=5)

            self.assertIsNone(self.store.get(record.run_id).start_transaction)
            self.assertIn(record.run_id, self.supervisor.adapters)
            self.assertTrue(self.store.run_dir(record.run_id).exists())
        finally:
            release_start.set()
            for task in (start_task, recovery_task):
                if task is None or task.done():
                    continue
                try:
                    await asyncio.wait_for(task, timeout=2)
                except Exception:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_recovery_aborts_uncommitted_start_when_pid_dies_mid_scan(
        self,
    ) -> None:
        """PID exit between abort scan and _recover_once must not RESUME."""

        request_id = "start-race-scan-then-exit"
        record = RunRecord.new(
            agent_id="WIKI-START-RACE-SCAN-EXIT",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            worktree=str(self.worktree),
            prompt="uncommitted start with post-scan PID exit",
            run_id=str(uuid4()),
            start_request_id=request_id,
        )
        command = AgentCommand.spawn(
            agent_id=record.agent_id,
            request_id=request_id,
            payload={
                "agent_id": record.agent_id,
                "provider": record.provider.value,
                "role": record.role,
                "model": record.model,
                "worktree": record.worktree,
                "prompt": record.initial_prompt or "uncommitted start with post-scan PID exit",
                "run_id": record.run_id,
            },
        )
        self.store.command_log.append_intent(command, {record.agent_id: None})
        self.store.create(record, transactional_start=True)
        provider = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        restarted: Supervisor | None = None
        try:
            record = self.store.get(record.run_id)
            record.provider_pid = provider.pid
            # Simulate a crash between adapter.start() and commit_start(): the
            # provider is durably attributed, state is IDLE, session id is
            # visible — every RESUME predicate is satisfied except the
            # uncommitted start_transaction we own here.
            record.provider_session_id = "codex-race-session"
            record.state = LifecycleState.IDLE
            self.store._write_record(record)  # noqa: SLF001 - restore crash-time snapshot

            restarted_store = RunStore(self.paths)
            restarted = Supervisor(
                restarted_store,
                FixtureAdapterFactory(FIXTURES, pid=os.getpid()),
            )

            original_abort = restarted_store.abort_uncommitted_starts

            def abort_then_kill() -> list[str]:
                aborted = original_abort()
                # Force the PID to exit strictly between the abort scan
                # (which sees live-unverifiable identity and skips) and
                # _recover_once (which now sees a dead PID).
                if provider.poll() is None:
                    provider.terminate()
                    provider.wait(timeout=5)
                return aborted

            restarted_store.abort_uncommitted_starts = abort_then_kill  # type: ignore[method-assign]

            results = await restarted.recover_on_start()

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["run_id"], record.run_id)
            self.assertEqual(results[0]["action"], "skip")
            self.assertEqual(results[0]["reason"], "uncommitted start aborted")
            # The command intent replays cleanly: the aborted record is
            # recreated as a fresh, committed start; no start_transaction
            # remains and the receipt is durable.
            self.assertEqual(restarted_store.command_log.pending(), [])
            receipt = restarted_store.command_log.receipt("run/start", request_id)
            self.assertIsNotNone(receipt)
            assert receipt is not None
            self.assertTrue(receipt.ok)
            current = restarted_store.get(record.run_id)
            self.assertEqual(current.run_id, record.run_id)
            self.assertIsNone(current.start_transaction)
            self.assertIn(record.run_id, restarted.adapters)
        finally:
            if provider.poll() is None:
                provider.kill()
                provider.wait(timeout=5)
            if restarted is not None:
                await restarted.close()

    async def _crash_replace_with_published_effect(
        self,
        *,
        agent_id: str,
        request_id: str,
        cross_provider: bool,
    ) -> tuple[RunRecord, RunRecord, AgentCommand, dict[str, Any]]:
        """Drive one fresh/cross-provider replace to a durably committed
        replacement, then rewind the effect to "published" to simulate a
        crash between _launch_record and the completed marker."""

        old = await self.supervisor.start_run(
            agent_id=agent_id,
            provider=ProviderKind.CLAUDE if cross_provider else ProviderKind.CODEX,
            role="implement",
            model="fixture-claude" if cross_provider else "fixture-codex",
            effort=None if cross_provider else "high",
            worktree=str(self.worktree),
            prompt="original prompt",
        )
        if not cross_provider:
            # Force the fresh replacement branch: detach and drop the old PID
            # so _replace_without_admission sees old_adapter is None with no
            # orphan handling required.
            old_adapter = self.supervisor.adapters[old.run_id]
            await self.supervisor._detach_adapter(old.run_id)  # noqa: SLF001
            await old_adapter.close()
            old = self.store.transition(
                old.run_id,
                LifecycleState.DEAD,
                reason="prepare fresh replace",
            )
            record = self.store.get(old.run_id)
            record.provider_pid = None
            self.store._write_record(record)  # noqa: SLF001 - clear PID for fresh path

        replacement_id = str(uuid4())
        target_provider = ProviderKind.CODEX if cross_provider else ProviderKind.CODEX
        payload: dict[str, Any] = {
            "agent_id": old.agent_id,
            "run_id": old.run_id,
            "replacement_run_id": replacement_id,
            "prompt": "replacement prompt",
        }
        if cross_provider:
            payload["provider"] = target_provider.value
            payload["model"] = "fixture-codex"
        command = AgentCommand.replace(
            agent_id=old.agent_id,
            request_id=request_id,
            payload=payload,
        )

        replacement = await self.supervisor.replace(
            old.run_id,
            payload["prompt"],
            provider=target_provider if cross_provider else None,
            replacement_run_id=replacement_id,
            effect_id=command.request_id,
            command_hash=command.command_hash,
        )
        self.assertIn(replacement.run_id, self.supervisor.adapters)
        self.assertIsNone(self.store.get(replacement.run_id).start_transaction)

        # Rewind the effect status to "published" — simulates a crash after
        # store.replace()+publish and inside _launch_record's window.
        self.store.command_log.update_replace_effect(
            "run/replace", command.request_id, "published"
        )
        effect = self.store.command_log.replace_effect(
            "run/replace",
            command.request_id,
            agent_id=old.agent_id,
            command_hash=command.command_hash,
        )
        self.assertIsNotNone(effect)
        assert effect is not None
        self.assertEqual(effect["status"], "published")
        return old, replacement, command, payload

    async def _replay_published_replace_and_assert_promoted(
        self,
        *,
        old: RunRecord,
        replacement: RunRecord,
        command: AgentCommand,
        payload: dict[str, Any],
    ) -> None:
        # The supervisor still owns the replacement adapter — the reviewer's
        # crash-recovery invariant is that a durably committed replacement
        # must be promoted, not killed. Replay the command via _dispatch to
        # exercise the exact code path recover_pending would drive.
        self.assertIn(replacement.run_id, self.supervisor.adapters)

        replayed = await self.supervisor._dispatch(  # noqa: SLF001
            "run/replace",
            {**payload, "request_id": command.request_id},
            command_hash=command.command_hash,
        )
        self.assertEqual(replayed["run_id"], replacement.run_id)
        self.assertIn(replacement.run_id, self.supervisor.adapters)
        current = self.store.get(replacement.run_id)
        self.assertNotEqual(current.state, LifecycleState.BLOCKED)
        self.assertEqual(
            self.store.current_run_id(old.agent_id), replacement.run_id
        )
        effect = self.store.command_log.replace_effect(
            "run/replace",
            command.request_id,
            agent_id=old.agent_id,
            command_hash=command.command_hash,
        )
        self.assertIsNotNone(effect)
        assert effect is not None
        self.assertEqual(effect["status"], "completed")
        self.assertTrue(self.store.run_dir(replacement.run_id).exists())

    async def test_replace_replay_promotes_published_effect_fresh_path(self) -> None:
        old, replacement, command, payload = await self._crash_replace_with_published_effect(
            agent_id="WIKI-PUBLISHED-FRESH-CRASH",
            request_id="published-fresh-crash",
            cross_provider=False,
        )
        await self._replay_published_replace_and_assert_promoted(
            old=old,
            replacement=replacement,
            command=command,
            payload=payload,
        )

    async def test_replace_replay_promotes_published_effect_cross_provider_path(
        self,
    ) -> None:
        old, replacement, command, payload = await self._crash_replace_with_published_effect(
            agent_id="WIKI-PUBLISHED-CROSS-CRASH",
            request_id="published-cross-crash",
            cross_provider=True,
        )
        await self._replay_published_replace_and_assert_promoted(
            old=old,
            replacement=replacement,
            command=command,
            payload=payload,
        )

    async def test_replace_cancellation_during_quiesce_terminalizes_old_run(self) -> None:
        old = await self.supervisor.start_run(
            agent_id="WIKI-REPLACE-CANCEL-STOP",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="original prompt",
        )
        adapter = self.supervisor.adapters[old.run_id]
        first_stop_started = asyncio.Event()
        release_first_stop = asyncio.Event()
        original_stop = adapter.stop
        stop_calls = 0

        async def cancellable_stop():
            nonlocal stop_calls
            stop_calls += 1
            if stop_calls == 1:
                first_stop_started.set()
                await release_first_stop.wait()
            return await original_stop()

        with mock.patch.object(adapter, "stop", side_effect=cancellable_stop):
            replacement_task = asyncio.create_task(
                self.supervisor.replace(old.run_id, "replacement prompt")
            )
            await asyncio.wait_for(first_stop_started.wait(), timeout=2)
            replacement_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await replacement_task

        current = self.store.get(old.run_id)
        self.assertEqual(current.state, LifecycleState.DEAD)
        self.assertIsNone(current.provider_pid)
        self.assertEqual(self.store.current_run_id(old.agent_id), old.run_id)
        self.assertNotIn(old.run_id, self.supervisor.adapters)
        self.assertNotIn(old.run_id, self.supervisor.event_tasks)
        self.assertEqual(stop_calls, 1)
        self.assertTrue(adapter.closed)

    async def test_replace_cancellation_after_publication_rolls_back_child(self) -> None:
        old = await self.supervisor.start_run(
            agent_id="WIKI-REPLACE-CANCEL-PUBLISH",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="original prompt",
        )
        adapter = self.supervisor.adapters[old.run_id]
        published = asyncio.Event()
        original_replace = adapter.replace

        async def paused_replace(
            prompt: str,
            model: str | None = None,
            effort: str | None = None,
        ) -> AdapterStatus:
            published.set()
            await asyncio.Event().wait()
            return await original_replace(prompt, model, effort)

        with mock.patch.object(adapter, "replace", side_effect=paused_replace):
            replacement_task = asyncio.create_task(
                self.supervisor.replace(old.run_id, "replacement prompt")
            )
            await asyncio.wait_for(published.wait(), timeout=2)
            replacement_id = self.store.current_run_id(old.agent_id)
            self.assertIsNotNone(replacement_id)
            self.assertNotEqual(replacement_id, old.run_id)
            replacement_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await replacement_task

        current = self.store.get(old.run_id)
        self.assertEqual(self.store.current_run_id(old.agent_id), old.run_id)
        self.assertEqual(current.state, LifecycleState.BLOCKED)
        self.assertIsNone(current.provider_pid)
        self.assertNotIn(old.run_id, self.supervisor.adapters)
        self.assertNotIn(old.run_id, self.supervisor.event_tasks)
        self.assertFalse(self.store.status_path(old.agent_id).exists())

    async def test_start_cancellation_terminalizes_spawned_run(self) -> None:
        started = asyncio.Event()
        allow_start = asyncio.Event()
        captured: list[CodexFixtureAdapter] = []
        original_factory = self.supervisor.adapter_factory

        def paused_factory(record: RunRecord):
            adapter = original_factory(record)
            assert isinstance(adapter, CodexFixtureAdapter)
            captured.append(adapter)

            async def paused_start(request: StartRequest) -> AdapterStatus:
                started.set()
                await allow_start.wait()
                return await CodexFixtureAdapter.start(adapter, request)

            adapter.start = paused_start  # type: ignore[method-assign]
            return adapter

        self.supervisor.adapter_factory = paused_factory
        start_task = asyncio.create_task(
            self.supervisor.start_run(
                agent_id="WIKI-REPLACE-CANCEL-SPAWN",
                provider=ProviderKind.CODEX,
                role="implement",
                model="fixture-codex",
                effort="high",
                worktree=str(self.worktree),
                prompt="spawn prompt",
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        run_id = self.store.current_run_id("WIKI-REPLACE-CANCEL-SPAWN")
        self.assertIsNotNone(run_id)
        start_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await start_task

        assert run_id is not None
        with self.assertRaises(RunNotFound):
            self.store.get(run_id)
        self.assertIsNone(self.store.current_run_id("WIKI-REPLACE-CANCEL-SPAWN"))
        self.assertFalse(
            self.store.status_path("WIKI-REPLACE-CANCEL-SPAWN").exists()
        )
        self.assertNotIn(run_id, self.supervisor.adapters)
        self.assertNotIn(run_id, self.supervisor.event_tasks)
        self.assertTrue(captured[0].closed)

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

    async def test_cross_provider_codex_to_claude_cancellation_cleans_old_run(self) -> None:
        await self._assert_cross_provider_replace_cancellation(
            ProviderKind.CODEX,
            ProviderKind.CLAUDE,
            "WIKI-CROSS-CANCEL-CODEX-CLAUDE",
        )

    async def test_cross_provider_claude_to_codex_cancellation_cleans_old_run(self) -> None:
        await self._assert_cross_provider_replace_cancellation(
            ProviderKind.CLAUDE,
            ProviderKind.CODEX,
            "WIKI-CROSS-CANCEL-CLAUDE-CODEX",
        )

    async def _assert_cross_provider_replace_cancellation(
        self,
        old_provider: ProviderKind,
        new_provider: ProviderKind,
        agent_id: str,
    ) -> None:
        old = await self.supervisor.start_run(
            agent_id=agent_id,
            provider=old_provider,
            role="implement",
            model="fixture-codex" if old_provider is ProviderKind.CODEX else "fixture-claude",
            effort="high" if old_provider is ProviderKind.CODEX else None,
            worktree=str(self.worktree),
            prompt="original cross-provider prompt",
        )
        adapter = self.supervisor.adapters[old.run_id]
        stop_started = asyncio.Event()

        async def paused_stop() -> AdapterStatus:
            stop_started.set()
            await asyncio.Event().wait()
            raise AssertionError("paused stop should only finish through cancellation")

        with mock.patch.object(adapter, "stop", side_effect=paused_stop):
            replacement_task = asyncio.create_task(
                self.supervisor.replace(
                    old.run_id,
                    "replacement cross-provider prompt",
                    model="fixture-codex" if new_provider is ProviderKind.CODEX else "fixture-claude",
                    provider=new_provider,
                    effort="high" if new_provider is ProviderKind.CODEX else None,
                )
            )
            await asyncio.wait_for(stop_started.wait(), timeout=2)
            replacement_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await replacement_task

        current = self.store.get(old.run_id)
        self.assertEqual(self.store.current_run_id(agent_id), old.run_id)
        self.assertEqual(current.state, LifecycleState.DEAD)
        self.assertIsNone(current.provider_pid)
        self.assertTrue(adapter.closed)
        self.assertNotIn(old.run_id, self.supervisor.adapters)
        self.assertNotIn(old.run_id, self.supervisor.event_tasks)
        decision = restart_recovery_decision(
            current,
            is_current=True,
            provider_pid_alive=False,
            provider_control_attached=False,
        )
        self.assertNotEqual(decision.action.value, "resume")

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

    async def test_recovery_resumes_current_sessions_with_dead_pid(
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
                by_id[records["waiting-approval"].run_id]["action"], "resume"
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
                LifecycleState.IDLE,
            )
            self.assertEqual(
                store.get(records["starting"].run_id).state,
                LifecycleState.BLOCKED,
            )
        finally:
            await supervisor.close()
        # Keep tearDown from closing the already-closed original twice.
        self.supervisor = Supervisor(self.store, FixtureAdapterFactory(FIXTURES))

    async def test_idle_recovery_delivers_queued_message_once_after_handover(self) -> None:
        await self.supervisor.close()
        store = RunStore(self.paths)
        worktree = self.root / "recovery-idle-queued"
        worktree.mkdir()
        record = RunRecord.new(
            agent_id="WIKI-IDLE-QUEUED-RECOVERY",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            worktree=str(worktree),
            prompt="idle queued recovery",
        )
        record.state = LifecycleState.IDLE
        record.provider_session_id = "idle-queued-session"
        record.provider_pid = 999_123
        store.create(record)
        store.queue_message(record.run_id, "deliver after recovery")
        supervisor = Supervisor(
            store,
            FixtureAdapterFactory(FIXTURES, pid=os.getpid()),
            pid_alive=lambda _pid: False,
        )
        try:
            results = await supervisor.recover_on_start()
            self.assertEqual(results[0]["action"], "resume")
            adapter = supervisor.adapters[record.run_id]
            self.assertIsInstance(adapter, CodexFixtureAdapter)
            assert isinstance(adapter, CodexFixtureAdapter)
            self.assertEqual(adapter.replayed_methods.count("turn/start"), 1)
            self.assertEqual(store.queued_messages(record.run_id), [])
        finally:
            await supervisor.close()
        self.supervisor = Supervisor(self.store, FixtureAdapterFactory(FIXTURES))

    async def test_handover_preserves_working_idle_waiting_runs_and_sessions(self) -> None:
        records: list[RunRecord] = []
        for index, state in enumerate(
            (
                LifecycleState.WORKING,
                LifecycleState.IDLE,
                LifecycleState.WAITING_APPROVAL,
            ),
            start=1,
        ):
            record = await self.supervisor.start_run(
                agent_id=f"WIKI-HANDOVER-{index}",
                provider=ProviderKind.CLAUDE,
                role="implement",
                model="fixture-claude",
                effort=None,
                worktree=str(self.worktree),
                prompt=f"handover-{index}",
            )
            session_id = f"handover-session-{index}"
            adapter_status = AdapterStatus(state, session_id, os.getpid(), generation=1)
            record = self.store.transition(
                record.run_id,
                state,
                adapter_status=adapter_status,
            )
            records.append(record)

        saved = [
            {
                "run_id": record.run_id,
                "agent_id": record.agent_id,
                "provider_session_id": record.provider_session_id,
            }
            for record in records
        ]
        result = await self.supervisor.prepare_handover([record.run_id for record in records])
        self.assertEqual(set(result["drained_run_ids"]), {record.run_id for record in records})
        retry = await self.supervisor.prepare_handover()
        self.assertEqual(retry, result)
        await self.supervisor.close()
        replacement = Supervisor(
            RunStore(self.paths),
            FixtureAdapterFactory(FIXTURES, pid=os.getpid()),
            pid_alive=lambda _pid: False,
        )
        try:
            await replacement.recover_on_start()
            for expected in saved:
                current = replacement.store.get(expected["run_id"])
                self.assertEqual(current.run_id, expected["run_id"])
                self.assertEqual(current.provider_session_id, expected["provider_session_id"])
                self.assertEqual(current.agent_id, expected["agent_id"])
        finally:
            await replacement.close()
        self.supervisor = Supervisor(self.store, FixtureAdapterFactory(FIXTURES))

    async def test_codex_handover_preserves_intent_through_stop_events(self) -> None:
        for state in (LifecycleState.WORKING, LifecycleState.WAITING_APPROVAL):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                worktree = root / "worktree"
                worktree.mkdir()
                paths = _paths(root)
                store = RunStore(paths)
                factory = HandoverCodexFactory(FIXTURES, pid=os.getpid())
                supervisor = Supervisor(
                    store,
                    factory,
                    pid_alive=lambda _pid: False,
                )
                record = await supervisor.start_run(
                    agent_id=f"WIKI-CODEX-HANDOVER-{state.value}",
                    provider=ProviderKind.CODEX,
                    role="implement",
                    model="fixture-codex",
                    effort="high",
                    worktree=str(worktree),
                    prompt="handover with stop event",
                )
                adapter = supervisor.adapters[record.run_id]
                assert isinstance(adapter, HandoverStopEventCodexAdapter)
                if state is LifecycleState.WORKING:
                    status = await adapter.send_now("continue the active turn")
                    store.update_adapter_status(record.run_id, status)
                    expected_pending: dict[str, dict[str, Any]] = {}
                else:
                    await adapter.emit_approval()
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        current = store.get(record.run_id)
                        if current.state is state and current.pending_requests:
                            break
                        await asyncio.sleep(0.01)
                    expected_pending = dict(store.get(record.run_id).pending_requests)
                    self.assertTrue(expected_pending)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if store.get(record.run_id).state is state:
                        break
                    await asyncio.sleep(0.01)
                before = store.get(record.run_id)
                self.assertEqual(before.state, state)
                session_id = before.provider_session_id
                self.assertIsNotNone(session_id)

                result = await supervisor.prepare_handover()
                self.assertEqual(result["drained_run_ids"], [record.run_id])
                detached = store.get(record.run_id)
                self.assertEqual(detached.state, state)
                self.assertIsNone(detached.provider_pid)
                self.assertEqual(detached.provider_session_id, session_id)
                self.assertEqual(detached.pending_requests, expected_pending)
                await supervisor.close()

                replacement = Supervisor(
                    RunStore(paths),
                    HandoverCodexFactory(FIXTURES, pid=os.getpid()),
                    pid_alive=lambda _pid: False,
                )
                try:
                    recovered = await replacement.recover_on_start()
                    self.assertEqual(recovered[0]["action"], "resume")
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        current = replacement.store.get(record.run_id)
                        if state is LifecycleState.WORKING:
                            if current.state is LifecycleState.IDLE:
                                break
                        elif current.state is state and current.pending_requests:
                            break
                        await asyncio.sleep(0.01)
                    current = replacement.store.get(record.run_id)
                    self.assertEqual(current.provider_session_id, session_id)
                    if state is LifecycleState.WAITING_APPROVAL:
                        self.assertEqual(current.state, state)
                        self.assertTrue(current.pending_requests)
                finally:
                    await replacement.close()

    async def test_codex_handover_classifies_natural_stop_results(self) -> None:
        for stop_result, expected_state in (
            ("completed", LifecycleState.IDLE),
            ("approval", LifecycleState.WAITING_APPROVAL),
            ("approval_then_completed", LifecycleState.IDLE),
        ):
            with self.subTest(stop_result=stop_result), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                worktree = root / "worktree"
                worktree.mkdir()
                paths = _paths(root)
                store = RunStore(paths)
                supervisor = Supervisor(
                    store,
                    HandoverCodexFactory(
                        FIXTURES,
                        pid=os.getpid(),
                        stop_result=stop_result,
                    ),
                    pid_alive=lambda _pid: False,
                )
                record = await supervisor.start_run(
                    agent_id=f"WIKI-CODEX-NATURAL-{stop_result}",
                    provider=ProviderKind.CODEX,
                    role="implement",
                    model="fixture-codex",
                    effort="high",
                    worktree=str(worktree),
                    prompt="natural handover result",
                )
                adapter = supervisor.adapters[record.run_id]
                assert isinstance(adapter, HandoverStopEventCodexAdapter)
                status = await adapter.send_now("start the active turn")
                store.update_adapter_status(record.run_id, status)
                before = store.get(record.run_id)
                session_id = before.provider_session_id
                self.assertIsNotNone(session_id)

                await supervisor.prepare_handover()
                detached = store.get(record.run_id)
                self.assertEqual(detached.state, expected_state)
                if expected_state is LifecycleState.WAITING_APPROVAL:
                    self.assertTrue(detached.pending_requests)
                else:
                    self.assertFalse(detached.pending_requests)
                await supervisor.close()

                replacement_adapter_factory = HandoverCodexFactory(
                    FIXTURES,
                    pid=os.getpid(),
                    stop_result="interrupted",
                )
                replacement = Supervisor(
                    RunStore(paths),
                    replacement_adapter_factory,
                    pid_alive=lambda _pid: False,
                )
                try:
                    recovered = await replacement.recover_on_start()
                    self.assertEqual(recovered[0]["action"], "resume")
                    current = replacement.store.get(record.run_id)
                    self.assertEqual(current.provider_session_id, session_id)
                    recovered_adapter = replacement.adapters[record.run_id]
                    assert isinstance(
                        recovered_adapter, HandoverStopEventCodexAdapter
                    )
                    self.assertNotIn("turn/start", recovered_adapter.replayed_methods)
                    if expected_state is LifecycleState.WAITING_APPROVAL:
                        self.assertEqual(current.state, expected_state)
                        self.assertTrue(current.pending_requests)
                        await replacement.respond(
                            record.run_id,
                            "handover-approval",
                            {"answers": [{"id": "surface", "answer": "Wiki"}]},
                        )
                        self.assertFalse(
                            replacement.store.get(record.run_id).pending_requests
                        )
                finally:
                    await replacement.close()

    def test_handover_finalization_matrix_covers_every_cell(self) -> None:
        captured_pending = {"int:old": {"payload": {"question": "old"}}}
        drained_pending = {"int:new": {"payload": {"question": "new"}}}
        events_by_outcome = {
            "supervisor-interrupted": [
                ProviderEvent(
                    ProviderKind.CODEX,
                    {
                        "method": "turn/completed",
                        "params": {"turn": {"status": "interrupted"}},
                    },
                )
            ],
            "natural-completed": [
                ProviderEvent(
                    ProviderKind.CODEX,
                    {
                        "method": "turn/completed",
                        "params": {"turn": {"status": "completed"}},
                    },
                )
            ],
            "new-approval": [
                ProviderEvent(
                    ProviderKind.CODEX,
                    {
                        "id": 1,
                        "method": "item/tool/requestUserInput",
                        "params": {"questions": [{"id": "new"}]},
                    },
                )
            ],
            "control-cancel-request": [
                ProviderEvent(
                    ProviderKind.CLAUDE,
                    {"type": "control_cancel_request", "request_id": "old"},
                )
            ],
            "user-response": [
                ProviderEvent(
                    ProviderKind.CODEX,
                    {"id": 1, "result": {"ok": True}},
                    direction="client",
                )
            ],
            "none": [],
        }
        expected = {
            LifecycleState.WORKING: {
                "supervisor-interrupted": (LifecycleState.WORKING, captured_pending),
                "natural-completed": (LifecycleState.IDLE, {}),
                "new-approval": (
                    LifecycleState.WAITING_APPROVAL,
                    {**captured_pending, **drained_pending},
                ),
                "control-cancel-request": (LifecycleState.WORKING, captured_pending),
                "user-response": (LifecycleState.WORKING, {}),
                "none": (LifecycleState.WORKING, captured_pending),
            },
            LifecycleState.WAITING_APPROVAL: {
                "supervisor-interrupted": (
                    LifecycleState.WAITING_APPROVAL,
                    captured_pending,
                ),
                "natural-completed": (LifecycleState.IDLE, {}),
                "new-approval": (
                    LifecycleState.WAITING_APPROVAL,
                    {**captured_pending, **drained_pending},
                ),
                "control-cancel-request": (
                    LifecycleState.WAITING_APPROVAL,
                    captured_pending,
                ),
                "user-response": (LifecycleState.WORKING, {}),
                "none": (LifecycleState.WAITING_APPROVAL, captured_pending),
            },
            LifecycleState.IDLE: {
                "supervisor-interrupted": (LifecycleState.IDLE, captured_pending),
                "natural-completed": (LifecycleState.IDLE, {}),
                "new-approval": (
                    LifecycleState.WAITING_APPROVAL,
                    {**captured_pending, **drained_pending},
                ),
                "control-cancel-request": (LifecycleState.IDLE, {}),
                "user-response": (LifecycleState.IDLE, {}),
                "none": (LifecycleState.IDLE, captured_pending),
            },
        }
        for captured_state, outcomes in expected.items():
            for outcome, expected_result in outcomes.items():
                with self.subTest(captured_state=captured_state, outcome=outcome):
                    result = supervisor_module.Supervisor._classify_handover_finalization(
                        captured_state,
                        [
                            (cast(ProviderAdapter, object()), event)
                            for event in events_by_outcome[outcome]
                        ],
                        captured_pending,
                        drained_pending,
                    )
                    self.assertEqual(result, expected_result)

    async def test_handover_control_cancel_preserves_answerable_approval(self) -> None:
        root = Path(tempfile.mkdtemp())
        worktree = root / "worktree"
        worktree.mkdir()
        paths = _paths(root)
        store = RunStore(paths)
        supervisor = Supervisor(
            store,
            HandoverCancelFactory(FIXTURES, pid=os.getpid()),
            pid_alive=lambda _pid: False,
        )
        record = await supervisor.start_run(
            agent_id="WIKI-HANDOVER-CANCEL-APPROVAL",
            provider=ProviderKind.CLAUDE,
            role="implement",
            model="fixture-claude",
            worktree=str(worktree),
            prompt="cancel during handover",
        )
        session_id = record.provider_session_id
        self.assertIsNotNone(session_id)
        adapter = supervisor.adapters[record.run_id]
        adapter._status = AdapterStatus(  # noqa: SLF001 - handover cancel fixture
            LifecycleState.WAITING_APPROVAL,
            session_id,
            os.getpid(),
            generation=record.provider_generation,
        )
        await supervisor._handle_provider_event_without_admission(  # noqa: SLF001
            record.run_id,
            adapter,
            ProviderEvent(
                ProviderKind.CLAUDE,
                {
                    "type": "control_request",
                    "request_id": "old",
                    "request": {"subtype": "can_use_tool"},
                },
                direction="stdout",
                generation=record.provider_generation,
            ),
        )
        store.transition(
            record.run_id,
            LifecycleState.WAITING_APPROVAL,
            adapter_status=AdapterStatus(
                LifecycleState.WAITING_APPROVAL,
                session_id,
                os.getpid(),
                generation=record.provider_generation,
            ),
        )
        try:
            await supervisor.prepare_handover()
            detached = store.get(record.run_id)
            self.assertEqual(detached.state, LifecycleState.WAITING_APPROVAL)
            self.assertIn("str:old", detached.pending_requests)
        finally:
            await supervisor.close()

        replacement = Supervisor(
            store,
            HandoverCancelFactory(FIXTURES, pid=os.getpid()),
            pid_alive=lambda _pid: False,
        )
        try:
            recovered = await replacement.recover_on_start()
            self.assertEqual(recovered[0]["action"], "resume")
            recovered_adapter = replacement.adapters[record.run_id]
            self.assertEqual(recovered_adapter.recovery_prompt_count, 1)
            for _ in range(100):
                if replacement.store.get(record.run_id).pending_requests:
                    break
                await asyncio.sleep(0.01)
            current = replacement.store.get(record.run_id)
            self.assertEqual(current.state, LifecycleState.WAITING_APPROVAL)
            await replacement.respond(record.run_id, "old", {"behavior": "allow"})
            self.assertFalse(replacement.store.get(record.run_id).pending_requests)
        finally:
            await replacement.close()
            shutil.rmtree(root, ignore_errors=True)

    async def test_handover_barrier_defers_start_until_after_snapshot(self) -> None:
        existing = await self.supervisor.start_run(
            agent_id="WIKI-HANDOVER-BARRIER-EXISTING",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="existing handover run",
        )
        snapshot_started = asyncio.Event()
        release_snapshot = asyncio.Event()
        original_quiesce = self.supervisor._quiesce_adapter_for_replacement

        async def paused_quiesce(run_id: str, adapter: Any) -> None:
            snapshot_started.set()
            await release_snapshot.wait()
            await original_quiesce(run_id, adapter)

        with mock.patch.object(
            self.supervisor,
            "_quiesce_adapter_for_replacement",
            new=paused_quiesce,
        ):
            handover_task = asyncio.create_task(self.supervisor.prepare_handover())
            await asyncio.wait_for(snapshot_started.wait(), timeout=2)
            start_task = asyncio.create_task(
                self.supervisor.start_run(
                    agent_id="WIKI-HANDOVER-BARRIER-NEW",
                    provider=ProviderKind.CODEX,
                    role="implement",
                    model="fixture-codex",
                    effort="high",
                    worktree=str(self.worktree),
                    prompt="must wait for handover",
                )
            )
            await asyncio.sleep(0.05)
            release_snapshot.set()
            result = await asyncio.wait_for(handover_task, timeout=2)
            with self.assertRaisesRegex(StoreConflict, "handover is in progress"):
                await start_task

        self.assertEqual(
            result["drained_run_ids"],
            [existing.run_id],
        )
        self.assertEqual(
            [run["run_id"] for run in result["runs"]],
            [existing.run_id],
        )
        self.assertIsNone(self.store.current_run_id("WIKI-HANDOVER-BARRIER-NEW"))

    async def test_handover_barrier_defers_send_and_respond_without_stale_writes(
        self,
    ) -> None:
        send_run = await self.supervisor.start_run(
            agent_id="WIKI-HANDOVER-SEND-RACE",
            provider=ProviderKind.CLAUDE,
            role="implement",
            model="fixture-claude",
            worktree=str(self.worktree),
            prompt="send race",
        )
        respond_run = await self.supervisor.start_run(
            agent_id="WIKI-HANDOVER-RESPOND-RACE",
            provider=ProviderKind.CLAUDE,
            role="implement",
            model="fixture-claude",
            worktree=str(self.worktree),
            prompt="respond race",
        )
        self.store.transition(
            respond_run.run_id,
            LifecycleState.WAITING_APPROVAL,
            adapter_status=AdapterStatus(
                LifecycleState.WAITING_APPROVAL,
                "respond-session",
                os.getpid(),
                generation=1,
            ),
        )
        snapshot_started = asyncio.Event()
        release_snapshot = asyncio.Event()
        original_quiesce = self.supervisor._quiesce_adapter_for_replacement

        async def paused_quiesce(run_id: str, adapter: Any) -> None:
            snapshot_started.set()
            await release_snapshot.wait()
            await original_quiesce(run_id, adapter)

        with mock.patch.object(
            self.supervisor,
            "_quiesce_adapter_for_replacement",
            new=paused_quiesce,
        ):
            handover_task = asyncio.create_task(self.supervisor.prepare_handover())
            await asyncio.wait_for(snapshot_started.wait(), timeout=2)
            send_task = asyncio.create_task(
                self.supervisor.send_now(send_run.run_id, "must wait")
            )
            respond_task = asyncio.create_task(
                self.supervisor.respond(respond_run.run_id, "old", {})
            )
            await asyncio.sleep(0.05)
            release_snapshot.set()
            await asyncio.wait_for(handover_task, timeout=2)
            with self.assertRaisesRegex(StoreConflict, "handover is in progress"):
                await send_task
            with self.assertRaisesRegex(StoreConflict, "handover is in progress"):
                await respond_task

        current = self.store.get(respond_run.run_id)
        self.assertEqual(current.state, LifecycleState.WAITING_APPROVAL)
        self.assertIsNone(current.provider_pid)

    async def test_handover_queues_late_events_before_second_snapshot(self) -> None:
        first = await self.supervisor.start_run(
            agent_id="WIKI-HANDOVER-EVENT-FIRST",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="first handover event run",
        )
        second = await self.supervisor.start_run(
            agent_id="WIKI-HANDOVER-EVENT-SECOND",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="second handover event run",
        )
        await asyncio.sleep(0.05)
        second_adapter = self.supervisor.adapters[second.run_id]
        before = self.store.get(second.run_id)
        first_drain_started = asyncio.Event()
        release_first_drain = asyncio.Event()
        original_quiesce = self.supervisor._quiesce_adapter_for_replacement

        async def paused_first_drain(run_id: str, adapter: Any) -> None:
            if run_id == first.run_id:
                first_drain_started.set()
                await release_first_drain.wait()
            await original_quiesce(run_id, adapter)

        async def ordered_preflight() -> list[str]:
            return [first.run_id, second.run_id]

        handover_task: asyncio.Task[Any] | None = None
        try:
            with mock.patch.object(
                self.supervisor,
                "_quiesce_adapter_for_replacement",
                new=paused_first_drain,
            ), mock.patch.object(
                self.supervisor,
                "_handover_preflight",
                new=ordered_preflight,
            ):
                handover_task = asyncio.create_task(self.supervisor.prepare_handover())
                await asyncio.wait_for(first_drain_started.wait(), timeout=5)

                second_adapter._status = AdapterStatus(  # noqa: SLF001
                    LifecycleState.WAITING_APPROVAL,
                    second.provider_session_id,
                    os.getpid(),
                    generation=second_adapter.snapshot().generation,
                )
                await second_adapter._events.put(  # noqa: SLF001
                    ProviderEvent(
                        ProviderKind.CODEX,
                        {
                            "method": "item/agentMessage/delta",
                            "params": {"delta": "late output"},
                        },
                        generation=second_adapter.snapshot().generation,
                    )
                )
                await second_adapter._events.put(  # noqa: SLF001
                    ProviderEvent(
                        ProviderKind.CODEX,
                        {
                            "id": 42,
                            "method": "item/tool/requestUserInput",
                            "params": {
                                "questions": [
                                    {"id": "surface", "question": "Which surface?"}
                                ]
                            },
                        },
                        generation=second_adapter.snapshot().generation,
                    )
                )

                async def late_events_queued() -> None:
                    while len(self.supervisor.handover_event_queue.get(second.run_id, [])) < 2:
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(late_events_queued(), timeout=5)
                release_first_drain.set()
                result = await asyncio.wait_for(handover_task, timeout=5)
        finally:
            release_first_drain.set()
            if handover_task is not None and not handover_task.done():
                handover_task.cancel()
            if handover_task is not None:
                await asyncio.gather(handover_task, return_exceptions=True)

        current = self.store.get(second.run_id)
        self.assertEqual(current.raw_event_count, before.raw_event_count + 2)
        self.assertEqual(current.normalized_event_count, before.normalized_event_count + 2)
        self.assertEqual(current.state, LifecycleState.WAITING_APPROVAL)
        self.assertIn("int:42", current.pending_requests)
        second_runs = [run for run in result["runs"] if run["run_id"] == second.run_id]
        self.assertEqual(len(second_runs), 1)
        self.assertTrue(second_runs[0]["pending_requests"])

    async def test_handover_drains_adapter_queue_after_provider_stop(self) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-HANDOVER-ADAPTER-QUEUE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="adapter queue handover",
        )
        await _wait_for_events(self.store, record.run_id, 10)
        before = self.store.get(record.run_id)
        adapter = self.supervisor.adapters[record.run_id]
        event = ProviderEvent(
            ProviderKind.CODEX,
            {
                "id": 91,
                "method": "item/tool/requestUserInput",
                "params": {
                    "questions": [{"id": "queue", "question": "Continue?"}]
                },
            },
            generation=adapter.snapshot().generation,
        )
        original_drain = adapter.drain_events

        async def inject_after_stop() -> list[ProviderEvent]:
            await adapter._events.put(event)  # noqa: SLF001 - queue boundary probe
            return await original_drain()

        with mock.patch.object(adapter, "drain_events", new=inject_after_stop):
            await self.supervisor.prepare_handover()

        current = self.store.get(record.run_id)
        self.assertEqual(current.raw_event_count, before.raw_event_count + 1)
        self.assertEqual(current.normalized_event_count, before.normalized_event_count + 1)
        self.assertIn("int:91", current.pending_requests)
        self.assertFalse(self.supervisor.handover_event_queue)

    async def test_handover_waits_for_pump_event_in_flight_during_drain(self) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-HANDOVER-PUMP-INFLIGHT",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="pump in-flight handover",
        )
        await _wait_for_events(self.store, record.run_id, 10)
        before = self.store.get(record.run_id)
        adapter = self.supervisor.adapters[record.run_id]
        event = ProviderEvent(
            ProviderKind.CODEX,
            {
                "id": 94,
                "method": "item/tool/requestUserInput",
                "params": {
                    "questions": [{"id": "inflight", "question": "Continue?"}]
                },
            },
            generation=adapter.snapshot().generation,
        )
        drain_started = asyncio.Event()
        release_drain = asyncio.Event()
        original_drain = adapter.drain_events

        async def blocked_drain() -> list[ProviderEvent]:
            drain_started.set()
            await release_drain.wait()
            return await original_drain()

        with mock.patch.object(adapter, "drain_events", new=blocked_drain):
            handover_task = asyncio.create_task(self.supervisor.prepare_handover())
            await asyncio.wait_for(drain_started.wait(), timeout=2)
            await adapter._events.put(event)  # noqa: SLF001 - in-flight probe

            async def pump_has_taken_event() -> None:
                while not self.supervisor.event_inflight_counts.get(record.run_id):
                    await asyncio.sleep(0)

            await asyncio.wait_for(pump_has_taken_event(), timeout=2)
            release_drain.set()
            await asyncio.wait_for(handover_task, timeout=2)

        current = self.store.get(record.run_id)
        self.assertEqual(current.raw_event_count, before.raw_event_count + 1)
        self.assertEqual(current.normalized_event_count, before.normalized_event_count + 1)
        self.assertIn("int:94", current.pending_requests)
        self.assertFalse(self.supervisor.handover_event_queue)

    async def test_handover_flushes_old_generation_route_after_replacement(self) -> None:
        old = await self.supervisor.start_run(
            agent_id="WIKI-HANDOVER-OLD-GENERATION",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="old generation handover",
        )
        await _wait_for_events(self.store, old.run_id, 10)
        replacement = await self.supervisor.replace(
            old.run_id, "replacement generation handover"
        )
        await _wait_for_events(self.store, replacement.run_id, 10)
        old_before = self.store.get(old.run_id)
        adapter = self.supervisor.adapters[replacement.run_id]
        original_drain = adapter.drain_events
        injected = False

        async def inject_old_generation() -> list[ProviderEvent]:
            nonlocal injected
            if not injected:
                injected = True
                await adapter._events.put(  # noqa: SLF001 - generation route probe
                    ProviderEvent(
                        ProviderKind.CODEX,
                        {
                            "method": "item/agentMessage/delta",
                            "params": {"delta": "late old generation output"},
                        },
                        generation=old.provider_generation,
                    )
                )
            return await original_drain()

        with mock.patch.object(
            adapter, "drain_events", new=inject_old_generation
        ):
            await self.supervisor.prepare_handover()

        old_after = self.store.get(old.run_id)
        self.assertEqual(old_after.raw_event_count, old_before.raw_event_count + 1)
        self.assertEqual(
            old_after.normalized_event_count,
            old_before.normalized_event_count + 1,
        )
        self.assertFalse(self.supervisor.handover_event_queue)

    async def test_failed_handover_preflight_replays_queued_events(self) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-HANDOVER-PREFLIGHT-FAIL",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="preflight failure",
        )
        await _wait_for_events(self.store, record.run_id, 10)
        before = self.store.get(record.run_id)
        adapter = self.supervisor.adapters[record.run_id]

        async def fail_preflight() -> list[str]:
            await adapter._events.put(  # noqa: SLF001 - barrier failure probe
                ProviderEvent(
                    ProviderKind.CODEX,
                    {
                        "id": 92,
                        "method": "item/tool/requestUserInput",
                        "params": {
                            "questions": [{"id": "preflight", "question": "Retry?"}]
                        },
                    },
                    generation=adapter.snapshot().generation,
                )
            )
            raise StoreConflict("fixture preflight failure")

        with mock.patch.object(
            self.supervisor, "_handover_preflight", new=fail_preflight
        ):
            with self.assertRaisesRegex(StoreConflict, "fixture preflight failure"):
                await self.supervisor.prepare_handover()

        current = self.store.get(record.run_id)
        self.assertEqual(current.raw_event_count, before.raw_event_count + 1)
        self.assertIn("int:92", current.pending_requests)
        self.assertFalse(self.supervisor.handover_event_queue)

    async def test_handover_preflight_rejects_live_detached_control_before_drain(
        self,
    ) -> None:
        healthy = await self.supervisor.start_run(
            agent_id="WIKI-HANDOVER-ATTACHED",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="attached handover run",
        )
        detached = await self.supervisor.start_run(
            agent_id="WIKI-HANDOVER-DETACHED",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="detached handover run",
        )
        await _wait_for_events(self.store, healthy.run_id, 10)
        await _wait_for_events(self.store, detached.run_id, 10)
        healthy_adapter = self.supervisor.adapters[healthy.run_id]
        await self.supervisor._detach_adapter(  # noqa: SLF001 - live control probe
            detached.run_id,
            preserve_event_routes=True,
        )
        detached_record = self.store.get(detached.run_id)
        self.assertFalse(self.supervisor._runtime_status(detached_record)["control_attached"])
        self.assertTrue(self.supervisor.pid_alive(detached_record.provider_pid))

        with mock.patch.object(
            healthy_adapter, "stop", wraps=healthy_adapter.stop
        ) as healthy_stop:
            with self.assertRaisesRegex(StoreConflict, "live provider control is detached"):
                await self.supervisor.prepare_handover()

        healthy_stop.assert_not_awaited()
        self.assertIs(self.supervisor.adapters.get(healthy.run_id), healthy_adapter)
        self.assertTrue(self.supervisor._runtime_status(self.store.get(healthy.run_id))["control_attached"])

    async def test_failed_handover_retries_queued_delivery_after_barrier(self) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-HANDOVER-QUEUE-RETRY",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="queued delivery retry",
        )
        await _wait_for_events(self.store, record.run_id, 10)
        adapter = self.supervisor.adapters[record.run_id]
        idle = AdapterStatus(
            LifecycleState.IDLE,
            adapter.snapshot().session_id,
            os.getpid(),
            generation=adapter.snapshot().generation,
        )
        adapter._status = idle  # noqa: SLF001 - queue retry fixture
        self.store.update_adapter_status(record.run_id, idle)
        await self.supervisor.send_on_idle(record.run_id, "deliver after retry")

        async def fail_preflight_with_delivery_task() -> list[str]:
            self.supervisor._spawn_monitor_task(  # noqa: SLF001 - barrier race fixture
                self.supervisor._deliver_next_queued(record.run_id, adapter),
                name="handover-queued-delivery-retry",
            )
            await asyncio.sleep(0)
            raise StoreConflict("fixture failed handover")

        with mock.patch.object(
            self.supervisor,
            "_handover_preflight",
            new=fail_preflight_with_delivery_task,
        ):
            with self.assertRaisesRegex(StoreConflict, "fixture failed handover"):
                await self.supervisor.prepare_handover()

        for _ in range(100):
            if not self.store.get(record.run_id).queued_messages:
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("queued message was not delivered after handover failure")
        self.assertEqual(adapter._queued, [])  # noqa: SLF001
        self.assertEqual(adapter.snapshot().state, LifecycleState.WORKING)

    async def test_failed_handover_drain_replays_queued_events(self) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-HANDOVER-DRAIN-FAIL",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="drain failure",
        )
        await _wait_for_events(self.store, record.run_id, 10)
        before = self.store.get(record.run_id)
        adapter = self.supervisor.adapters[record.run_id]

        async def fail_drain(run_id: str, candidate: Any) -> None:
            await adapter._events.put(  # noqa: SLF001 - barrier failure probe
                ProviderEvent(
                    ProviderKind.CODEX,
                    {
                        "id": 93,
                        "method": "item/tool/requestUserInput",
                        "params": {
                            "questions": [{"id": "drain", "question": "Retry?"}]
                        },
                    },
                    generation=adapter.snapshot().generation,
                )
            )
            raise StoreConflict(f"fixture drain failure: {run_id}")

        with mock.patch.object(
            self.supervisor,
            "_quiesce_adapter_for_replacement",
            new=fail_drain,
        ):
            with self.assertRaisesRegex(StoreConflict, "fixture drain failure"):
                await self.supervisor.prepare_handover()

        current = self.store.get(record.run_id)
        self.assertEqual(current.raw_event_count, before.raw_event_count + 1)
        self.assertIn("int:93", current.pending_requests)
        self.assertFalse(self.supervisor.handover_event_queue)

    async def test_mutation_admission_does_not_deadlock_nested_resume_replace(self) -> None:
        record = await self.supervisor.start_run(
            agent_id="WIKI-HANDOVER-ADMISSION-RACE",
            provider=ProviderKind.CLAUDE,
            role="implement",
            model="fixture-claude",
            worktree=str(self.worktree),
            prompt="admission race",
        )
        run_lock = self.supervisor._run_lock(record.run_id)
        await run_lock.acquire()
        try:
            resume_task = asyncio.create_task(self.supervisor.resume_run(record.run_id))
            replace_task = asyncio.create_task(
                self.supervisor.replace(record.run_id, "replacement after admission race")
            )
            await asyncio.sleep(0.05)
            self.assertFalse(resume_task.done())
            self.assertFalse(replace_task.done())

            handover_task = asyncio.create_task(self.supervisor.prepare_handover())
            await asyncio.sleep(0.05)
            run_lock.release()

            results = await asyncio.wait_for(
                asyncio.gather(
                    resume_task,
                    replace_task,
                    handover_task,
                    return_exceptions=True,
                ),
                timeout=2,
            )
        finally:
            if run_lock.locked():
                run_lock.release()

        self.assertFalse(any(isinstance(result, asyncio.TimeoutError) for result in results))

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

    async def test_waiting_approval_orphan_retries_exact_session_recovery(self) -> None:
        await self.supervisor.close()
        record = RunRecord.new(
            agent_id="WIKI-ORPHAN-APPROVAL",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        record.state = LifecycleState.WAITING_APPROVAL
        record.provider_session_id = "session-exact-approval"
        record.provider_pid = 424_243
        record.pending_requests["str:old"] = {
            "request_id": "old",
            "request_kind": "item/tool/requestUserInput",
            "payload": {
                "method": "item/tool/requestUserInput",
                "id": "old",
                "params": {"questions": [{"question": "Which surface?"}]},
            },
        }
        self.store.create(record)
        alive = {"value": True}
        resumed_sessions: list[str] = []

        def factory(_record: RunRecord) -> ApprovalRecoveryAdapter:
            return ApprovalRecoveryAdapter(
                FIXTURES / "codex_app_server_success.jsonl",
                FIXTURES / "codex_app_server_control.jsonl",
                pid=os.getpid(),
                resumed_sessions=resumed_sessions,
            )

        self.supervisor = Supervisor(
            self.store,
            factory,
            pid_alive=lambda _pid: alive["value"],
        )
        first = await self.supervisor.recover_on_start()
        self.assertEqual(first[0]["action"], "block")
        blocked = self.store.get(record.run_id)
        self.assertEqual(blocked.recovery_from_state, LifecycleState.WAITING_APPROVAL)
        self.assertEqual(blocked.pending_requests["str:old"]["request_id"], "old")

        alive["value"] = False
        second = await self.supervisor.recover_on_start()
        self.assertEqual(second[0]["action"], "resume")
        resumed = self.store.get(record.run_id)
        self.assertEqual(resumed.provider_session_id, "session-exact-approval")
        self.assertEqual(resumed.state, LifecycleState.WAITING_APPROVAL)
        self.assertEqual(resumed.pending_requests["int:8"]["request_id"], 8)
        self.assertEqual(resumed_sessions, ["session-exact-approval"])
        self.assertIn(record.run_id, self.supervisor.adapters)

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
                "failed_run_ids": {},
                "revived_run_ids": {"WIKI-CODEX-ROTATE": codex.run_id},
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
                "failed_run_ids": {},
                "revived_run_ids": {"WIKI-RATE-LIMIT": codex.run_id},
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
        self.assertEqual(published["run_ids"], {"WIKI-NO-ELIGIBLE": record.run_id})
        self.assertEqual(
            published["reset_at"],
            datetime.fromtimestamp(1_750_009_999, tz=timezone.utc).isoformat(),
        )
        self.supervisor.unsubscribe(queue)

    async def test_rate_limit_rotation_failures_publish_all_current_codex_run_ids(self) -> None:
        records = [
            await self.supervisor.start_run(
                agent_id=f"WIKI-ROTATION-FAIL-{index}",
                provider=ProviderKind.CODEX,
                role="implement",
                model="fixture-codex",
                effort="high",
                worktree=str(self.worktree),
                prompt="fixture",
            )
            for index in (1, 2)
        ]
        expected = {record.agent_id: record.run_id for record in records}
        queue = self.supervisor.subscribe()

        for error in (
            StoreConflict("fixture store conflict"),
            accounts.RotationError("fixture rotation error"),
        ):
            with mock.patch.object(
                self.supervisor,
                "request_codex_rotation",
                new=mock.AsyncMock(side_effect=error),
            ):
                await self.supervisor._handle_codex_rate_limit_event(  # noqa: SLF001
                    records[0].run_id,
                    outgoing_reset_at="2099-01-01T00:00:00+00:00",
                )
            published = await _wait_for_published(queue, "codex_rotation_failed")
            self.assertEqual(published["tickets"], sorted(expected))
            self.assertEqual(published["run_ids"], expected)

        self.supervisor.unsubscribe(queue)

    async def test_auth_dead_exact_session_failure_blocks_and_publishes_run_identity(self) -> None:
        queue = self.supervisor.subscribe()
        record = await self.supervisor.start_run(
            agent_id="WIKI-AUTH-RESUME-FAIL",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        adapter = self.supervisor.adapters[record.run_id]
        failure = RuntimeError("fixture exact-session resume failure")

        with mock.patch.object(
            self.supervisor,
            "_resume_run_without_admission",
            new=mock.AsyncMock(side_effect=failure),
        ):
            await self.supervisor._recover_codex_auth_dead(  # noqa: SLF001
                record.run_id,
                adapter,
                prior_state=LifecycleState.WORKING,
            )

        published = await _wait_for_published(queue, "codex_auth_dead_revival")
        current = self.store.get(record.run_id)
        self.assertEqual(current.state, LifecycleState.BLOCKED)
        self.assertEqual(published["revived"], [])
        self.assertEqual(published["revived_run_ids"], {})
        self.assertEqual(published["failed"], [record.agent_id])
        self.assertEqual(
            published["failed_reasons"],
            {record.agent_id: str(failure)},
        )
        self.assertEqual(
            published["failed_run_ids"], {record.agent_id: record.run_id}
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
        self.assertEqual(
            published["revived_run_ids"], {"WIKI-AUTH-DEAD": record.run_id}
        )
        self.supervisor.unsubscribe(queue)

    async def test_auth_dead_cap_survives_exact_session_resumes(self) -> None:
        queue = self.supervisor.subscribe()
        record = await self.supervisor.start_run(
            agent_id="WIKI-AUTH-SAME-RUN-CAP",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        session_id = record.provider_session_id
        prior_adapter: ProviderAdapter | None = None

        for attempt in range(accounts.AUTH_DEAD_MAX_ATTEMPTS):
            adapter = self.supervisor.adapters[record.run_id]
            self.assertIsNot(adapter, prior_adapter)
            await self.supervisor._recover_codex_auth_dead(  # noqa: SLF001
                record.run_id,
                adapter,
                prior_state=LifecycleState.WORKING,
            )
            published = await _wait_for_published(queue, "codex_auth_dead_revival")
            self.assertEqual(published["revived"], [record.agent_id])
            self.assertEqual(
                published["revived_run_ids"], {record.agent_id: record.run_id}
            )
            current = self.store.get(record.run_id)
            self.assertEqual(current.provider_session_id, session_id)
            self.assertEqual(
                len(self.supervisor.auth_dead_attempts[record.run_id]), attempt + 1
            )
            prior_adapter = adapter
            if attempt + 1 < accounts.AUTH_DEAD_MAX_ATTEMPTS:
                self.supervisor.auth_dead_attempts[record.run_id][-1] -= (
                    accounts.AUTH_DEAD_COOLDOWN_SECONDS + 1
                )

        adapter = self.supervisor.adapters[record.run_id]
        await self.supervisor._recover_codex_auth_dead(  # noqa: SLF001
            record.run_id,
            adapter,
            prior_state=LifecycleState.WORKING,
        )
        exhausted = await _wait_for_published(queue, "codex_auth_dead_exhausted")
        self.assertEqual(exhausted["tickets"], [record.agent_id])
        self.assertIs(self.supervisor.adapters[record.run_id], adapter)
        self.assertFalse(cast(CodexFixtureAdapter, adapter).closed)

        await self.supervisor._recover_codex_auth_dead(  # noqa: SLF001
            record.run_id,
            adapter,
            prior_state=LifecycleState.WORKING,
        )
        with self.assertRaises(asyncio.TimeoutError):
            await _wait_for_published(
                queue, "codex_auth_dead_exhausted", timeout=0.05
            )
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
        self.supervisor.auth_dead_attempts[record.run_id] = [
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

    async def test_auth_dead_recovery_state_is_fresh_after_replacement(self) -> None:
        queue = self.supervisor.subscribe()
        old = await self.supervisor.start_run(
            agent_id="WIKI-AUTH-REPLACE",
            provider=ProviderKind.CODEX,
            role="implement",
            model="fixture-codex",
            effort="high",
            worktree=str(self.worktree),
            prompt="fixture",
        )
        now = time.monotonic()
        self.supervisor.auth_dead_attempts[old.run_id] = [now - 10, now - 20, now - 30]
        self.supervisor.auth_dead_alert_at[old.run_id] = now

        replacement = await self.supervisor.replace(old.run_id, "replacement prompt")

        self.assertNotIn(old.run_id, self.supervisor.auth_dead_attempts)
        self.assertNotIn(old.run_id, self.supervisor.auth_dead_alert_at)
        self.assertNotIn(replacement.run_id, self.supervisor.auth_dead_attempts)
        self.assertNotIn(replacement.run_id, self.supervisor.auth_dead_alert_at)

        await self.supervisor._recover_codex_auth_dead(  # noqa: SLF001
            replacement.run_id,
            self.supervisor.adapters[replacement.run_id],
            prior_state=LifecycleState.WORKING,
        )
        published = await _wait_for_published(queue, "codex_auth_dead_revival")
        self.assertEqual(published["revived"], ["WIKI-AUTH-REPLACE"])
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
        # Round-7: run_id + provider ride along so the notice store can
        # reconcile a Claude-to-Codex replacement without needing a
        # ticket-scoped recovery event.
        self.assertEqual(first["run_id"], record.run_id)
        self.assertEqual(first["provider"], "claude")
        with self.assertRaises(TimeoutError):
            await _wait_for_published(queue, "claude_limit_hit", timeout=0.1)
        self.supervisor.unsubscribe(queue)

    async def test_claude_limit_alerts_per_run_not_per_ticket(self) -> None:
        # Round-8: throttling was previously keyed by ticket, so a
        # replacement run under the same ticket would silently drop its
        # own limit notice within the hour. Now keyed by run_id and
        # pruned in _detach_adapter.
        queue = self.supervisor.subscribe()
        first_record = await self.supervisor.start_run(
            agent_id="WIKI-CLAUDE-RETRY",
            provider=ProviderKind.CLAUDE,
            role="review",
            model="fixture-claude",
            effort=None,
            worktree=str(self.worktree),
            prompt="fixture",
        )
        first_adapter = self.supervisor.adapters[first_record.run_id]
        payload = {
            "type": "result",
            "subtype": "error",
            "is_error": True,
            "result": "Claude usage limit reached. Try again at 4pm.",
        }
        await self.supervisor._handle_provider_event(  # noqa: SLF001
            first_record.run_id,
            first_adapter,
            ProviderEvent(ProviderKind.CLAUDE, payload),
        )
        first = await _wait_for_published(queue, "claude_limit_hit")
        self.assertEqual(first["run_id"], first_record.run_id)

        # Terminate + archive the first run so the ticket becomes eligible
        # for a replacement start_run. _detach_adapter (called from
        # archive) must have pruned the per-run throttle timestamp.
        self.assertIn(first_record.run_id, self.supervisor.last_limit_alert_at)
        await self.supervisor.archive(first_record.run_id)
        self.assertNotIn(first_record.run_id, self.supervisor.last_limit_alert_at)

        second_record = await self.supervisor.start_run(
            agent_id="WIKI-CLAUDE-RETRY",
            provider=ProviderKind.CLAUDE,
            role="review",
            model="fixture-claude",
            effort=None,
            worktree=str(self.worktree),
            prompt="fixture",
        )
        self.assertNotEqual(second_record.run_id, first_record.run_id)
        second_adapter = self.supervisor.adapters[second_record.run_id]
        await self.supervisor._handle_provider_event(  # noqa: SLF001
            second_record.run_id,
            second_adapter,
            ProviderEvent(ProviderKind.CLAUDE, payload),
        )
        second = await _wait_for_published(queue, "claude_limit_hit")
        self.assertEqual(second["run_id"], second_record.run_id)
        self.supervisor.unsubscribe(queue)

    async def test_claude_success_with_limit_warning_only_clears_notice(self) -> None:
        queue = self.supervisor.subscribe()
        notice_store = AccountNoticeStore(path=self.root / "account-notices.json")
        record = await self.supervisor.start_run(
            agent_id="WIKI-CLAUDE-RECOVERY",
            provider=ProviderKind.CLAUDE,
            role="review",
            model="fixture-claude",
            effort=None,
            worktree=str(self.worktree),
            prompt="fixture",
        )
        adapter = self.supervisor.adapters[record.run_id]
        await self.supervisor._handle_provider_event(  # noqa: SLF001
            record.run_id,
            adapter,
            ProviderEvent(
                ProviderKind.CLAUDE,
                {
                    "type": "result",
                    "subtype": "error",
                    "is_error": True,
                    "result": "Claude usage limit reached. Try again at 4pm.",
                },
            ),
        )
        hit = await _wait_for_published(queue, "claude_limit_hit")
        notice_store.apply_event(hit)

        # A failed provider request is not recovery proof.
        await self.supervisor._handle_provider_event(  # noqa: SLF001
            record.run_id,
            adapter,
            ProviderEvent(
                ProviderKind.CLAUDE,
                {
                    "type": "result",
                    "subtype": "error",
                    "is_error": True,
                    "result": "Claude usage limit reached again.",
                },
            ),
        )
        with self.assertRaises(TimeoutError):
            await _wait_for_published(queue, "claude_limit_cleared", timeout=0.1)

        await self.supervisor._handle_provider_event(  # noqa: SLF001
            record.run_id,
            adapter,
            ProviderEvent(
                ProviderKind.CLAUDE,
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "Turn completed; approaching usage limit warning shown.",
                },
            ),
        )
        cleared = await _wait_for_published(queue, "claude_limit_cleared")
        notice_store.apply_event(cleared)
        self.assertEqual(cleared["ticket"], "WIKI-CLAUDE-RECOVERY")
        self.assertEqual(cleared["run_id"], record.run_id)
        self.assertEqual(notice_store.snapshot(), [])

        # A successful turn clears the per-run throttle so a later limit on
        # the same run produces a fresh actionable notice.
        self.assertNotIn(record.run_id, self.supervisor.last_limit_alert_at)
        await self.supervisor._handle_provider_event(  # noqa: SLF001
            record.run_id,
            adapter,
            ProviderEvent(
                ProviderKind.CLAUDE,
                {
                    "type": "result",
                    "subtype": "error",
                    "is_error": True,
                    "result": "Claude usage limit reached after recovery.",
                },
            ),
        )
        second_hit = await _wait_for_published(queue, "claude_limit_hit")
        notice_store.apply_event(second_hit)
        self.assertEqual(second_hit["run_id"], record.run_id)
        self.assertIn(record.run_id, self.supervisor.last_limit_alert_at)
        self.assertEqual(notice_store.snapshot()[0]["type"], "claude_limit_hit")
        self.supervisor.unsubscribe(queue)

    async def test_claude_limit_clear_survives_supervisor_restart(self) -> None:
        notice_store = AccountNoticeStore(path=self.root / "account-notices.json")
        queue = self.supervisor.subscribe()
        record = await self.supervisor.start_run(
            agent_id="WIKI-CLAUDE-RESTART",
            provider=ProviderKind.CLAUDE,
            role="review",
            model="fixture-claude",
            effort=None,
            worktree=str(self.worktree),
            prompt="fixture",
        )
        adapter = self.supervisor.adapters[record.run_id]
        await self.supervisor._handle_provider_event(  # noqa: SLF001
            record.run_id,
            adapter,
            ProviderEvent(
                ProviderKind.CLAUDE,
                {
                    "type": "result",
                    "subtype": "error",
                    "is_error": True,
                    "result": "Claude usage limit reached. Try again at 4pm.",
                },
            ),
        )
        hit = await _wait_for_published(queue, "claude_limit_hit")
        notice_store.apply_event(hit)
        self.supervisor.unsubscribe(queue)

        await self.supervisor.close()
        self.supervisor = Supervisor(
            self.store,
            FixtureAdapterFactory(FIXTURES, pid=os.getpid()),
        )
        queue = self.supervisor.subscribe()
        adapter = FixtureAdapterFactory(FIXTURES, pid=os.getpid())(record)
        self.supervisor._attach_adapter(record.run_id, adapter)  # noqa: SLF001
        await self.supervisor._handle_provider_event(  # noqa: SLF001
            record.run_id,
            adapter,
            ProviderEvent(
                ProviderKind.CLAUDE,
                {"type": "result", "subtype": "success", "is_error": False},
            ),
        )
        cleared = await _wait_for_published(queue, "claude_limit_cleared")
        notice_store.apply_event(cleared)
        self.assertEqual(cleared["run_id"], record.run_id)
        self.assertEqual(notice_store.snapshot(), [])
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

        def fail_registry_commit(
            _old_run_id: str,
            replacement: RunRecord,
            **_kwargs: object,
        ):
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
        # The replacement is published before its fresh provider session is
        # started, so a commit failure cannot expose stale provider identity.
        self.assertEqual(replacement.state, LifecycleState.STARTING)
        self.assertIsNone(replacement.provider_session_id)
        self.assertEqual(replacement.provider_generation, 0)
        self.assertIsNone(replacement.provider_pid)

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
            runtime_frozen=True,
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
                return_value={"status": "ok", "drained_run_ids": [], "runs": []},
            ) as request,
            mock.patch.object(client, "_spawn_detached") as spawn,
            mock.patch("backend.app.agent_runtime.client.os.kill") as kill,
        ):
            self.assertEqual(client.ensure_running(timeout=0.1), current)
        request.assert_called_once_with("supervisor/handover", {})
        kill.assert_called_once_with(424_242, signal.SIGTERM)
        spawn.assert_called_once_with()

    def test_client_migrates_idle_and_working_runs_across_runtime_swap(self) -> None:
        client = SupervisorClient(
            self.paths,
            timeout=0.1,
            runtime_fingerprint="current-runtime",
            runtime_frozen=True,
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
            if method == "supervisor/handover":
                return {
                    "drained_run_ids": ["idle-run", "working-run"],
                    "runs": [idle, working],
                }
            if method == "run/replace":
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
            [],
        )
        self.assertEqual(
            [values["run_id"] for method, values in calls if method == "run/interrupt"],
            [],
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
            runtime_frozen=True,
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

    def test_dev_client_uses_a_mismatched_supervisor_without_replacing_it(self) -> None:
        """WIKI-217: worktree/dev code must never swap-kill the app's supervisor."""

        client = SupervisorClient(
            self.paths,
            timeout=0.1,
            runtime_fingerprint="dev-source-hash",
            runtime_frozen=False,
        )
        health = {
            "status": "ok",
            "pid": 424_242,
            "runtime_fingerprint": "frozen-binary-stat",
        }
        with (
            mock.patch.object(client, "ping", return_value=health),
            mock.patch.object(client, "request") as request,
            mock.patch.object(client, "_spawn_detached") as spawn,
            mock.patch("backend.app.agent_runtime.client.os.kill") as kill,
        ):
            self.assertEqual(client.ensure_running(timeout=0.1), health)
        request.assert_not_called()
        spawn.assert_not_called()
        kill.assert_not_called()

    async def test_close_leaves_a_replacement_daemons_socket_in_place(self) -> None:
        """WIKI-217: a dying daemon must not unlink the socket a successor rebound."""

        self.paths.socket_path.unlink()
        replacement = UnixSupervisorServer(self.supervisor, self.paths.socket_path)
        await replacement.start()
        try:
            await self.server.close()
            self.assertTrue(self.paths.socket_path.exists())
        finally:
            await replacement.close()
        self.server = replacement


class DaemonShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_lock_and_pid_release_before_provider_drain(self) -> None:
        """WIKI-217: a replacement must be able to start while the old daemon drains."""

        with tempfile.TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            lock = agent_daemon._acquire_single_instance(paths)
            observed: dict[str, bool] = {}

            class DrainingSupervisor:
                async def close(self) -> None:
                    probe = paths.lock_path.open("a+b")
                    try:
                        fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        observed["lock_free"] = True
                        fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
                    except BlockingIOError:
                        observed["lock_free"] = False
                    finally:
                        probe.close()
                    observed["pid_gone"] = not paths.pid_path.exists()

            class ClosedServer:
                async def close(self) -> None:
                    pass

            await agent_daemon._shutdown(
                ClosedServer(),
                DrainingSupervisor(),
                [],
                lock,
                paths,
            )
            self.assertTrue(observed["lock_free"])
            self.assertTrue(observed["pid_gone"])


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

    def test_daemon_subprocess_restart_replays_spawn_steer_replace_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            worktree = root / "worktree"
            worktree.mkdir()
            repo_root = Path(__file__).resolve().parents[2]
            processes: list[subprocess.Popen[str]] = []

            def start_daemon() -> subprocess.Popen[str]:
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
                    cwd=repo_root,
                    env={
                        **os.environ,
                        "TMUX": "",
                        "WIKI_AGENT_ARCHIVE_DIR": str(paths.archive_dir),
                        "WIKI_AGENT_STATUS_DIR": str(paths.status_dir),
                    },
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                processes.append(process)
                client = SupervisorClient(paths, timeout=5)
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    try:
                        if client.ping().get("status") == "ok":
                            return process
                    except SupervisorUnavailable:
                        time.sleep(0.05)
                stderr = process.stderr.read() if process.stderr else ""
                self.fail(f"daemon did not restart: {stderr}")

            def stop_daemon(process: subprocess.Popen[str]) -> None:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=8)
                if process.stderr:
                    process.stderr.close()

            client = SupervisorClient(paths, timeout=5)
            process = start_daemon()
            try:
                start_params = {
                    "agent_id": "WIKI-219-SUBPROCESS",
                    "provider": "codex",
                    "role": "implement",
                    "model": "fixture-codex",
                    "effort": "high",
                    "worktree": str(worktree),
                    "prompt": "subprocess restart spawn",
                    "request_id": "subprocess-spawn",
                }
                started = client.request("run/start", start_params)
                start_run_id = started["run_id"]
                stop_daemon(process)
                process = start_daemon()
                replayed_start = client.request("run/start", start_params)
                self.assertEqual(replayed_start["run_id"], start_run_id)

                steer_params = {
                    "agent_id": "WIKI-219-SUBPROCESS",
                    "run_id": start_run_id,
                    "text": "subprocess restart steer",
                    "request_id": "subprocess-steer",
                }
                steered = client.request("run/send_now", steer_params)
                stop_daemon(process)
                process = start_daemon()
                replayed_steer = client.request("run/send_now", steer_params)
                self.assertEqual(replayed_steer, steered)

                replace_params = {
                    "agent_id": "WIKI-219-SUBPROCESS",
                    "run_id": start_run_id,
                    "prompt": "subprocess restart replace",
                    "request_id": "subprocess-replace",
                }
                replaced = client.request("run/replace", replace_params)
                replacement_run_id = replaced["run_id"]
                self.assertNotEqual(replacement_run_id, start_run_id)
                stop_daemon(process)
                process = start_daemon()
                replayed_replace = client.request("run/replace", replace_params)
                self.assertEqual(replayed_replace["run_id"], replacement_run_id)

                archive_params = {
                    "agent_id": "WIKI-219-SUBPROCESS",
                    "run_id": replacement_run_id,
                    "outcome": "subprocess-test",
                    "request_id": "subprocess-archive",
                }
                archived = client.request("run/archive", archive_params)
                stop_daemon(process)
                process = start_daemon()
                replayed_archive = client.request("run/archive", archive_params)
                self.assertEqual(replayed_archive["run_id"], archived["run_id"])
                self.assertEqual(
                    json.loads(paths.registry_path.read_text(encoding="utf-8")),
                    {},
                )

                log = RunStore(paths).command_log
                for method, request_id in (
                    ("run/start", "subprocess-spawn"),
                    ("run/send_now", "subprocess-steer"),
                    ("run/replace", "subprocess-replace"),
                    ("run/archive", "subprocess-archive"),
                ):
                    receipt = log.receipt(method, request_id)
                    self.assertIsNotNone(receipt)
                    assert receipt is not None
                    self.assertTrue(receipt.ok)
            finally:
                stop_daemon(process)
                for item in processes:
                    self.assertIsNotNone(item.poll())

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
                runtime_frozen=True,
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
