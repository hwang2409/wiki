from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

from backend.app.agent_runtime.fake import WireFixture
from backend.app.agent_runtime.normalizer import normalize_provider_event
from backend.app.agent_runtime.provider import AdapterStatus
from backend.app.agent_runtime.store import (
    RunStore,
    RuntimePaths,
    StoreConflict,
    StoreError,
    _atomic_write_json,
)
from backend.app.agent_runtime.types import (
    EventDisposition,
    LifecycleState,
    ProviderKind,
    RecoveryAction,
    RunRecord,
    RESTART_RECOVERY_TABLE,
    restart_recovery_decision,
    validate_transition,
)


FIXTURES = Path(__file__).parent / "fixtures" / "agent_runtime"


def _paths(root: Path) -> RuntimePaths:
    return RuntimePaths(
        runtime_dir=root / "runtime",
        socket_path=root / "runtime" / "supervisor.sock",
        registry_path=root / "registry" / "agents.json",
        archive_dir=root / "archive",
        status_dir=root / "status",
    )


def _record(root: Path, agent_id: str = "WIKI-42") -> RunRecord:
    worktree = root / f"worktree-{agent_id.lower()}"
    worktree.mkdir(parents=True, exist_ok=True)
    return RunRecord.new(
        agent_id=agent_id,
        provider=ProviderKind.CODEX,
        role="implement",
        model="fixture-codex",
        effort="high",
        worktree=str(worktree),
        prompt=f"Work on ticket {agent_id}",
        orchestrator_id="wiki-dev",
    )


class ProtocolFixtureTests(unittest.TestCase):
    def test_codex_wire_fixtures_cover_success_failure_resume_steer_interrupt(
        self,
    ) -> None:
        success = WireFixture(FIXTURES / "codex_app_server_success.jsonl")
        failure = WireFixture(FIXTURES / "codex_app_server_failure.jsonl")
        control = WireFixture(FIXTURES / "codex_app_server_control.jsonl")
        approval = WireFixture(FIXTURES / "codex_app_server_approval.jsonl")

        thread_start = success.response_result("thread/start")
        assert thread_start is not None
        self.assertEqual(thread_start["thread"]["status"], {"type": "idle"})
        self.assertTrue(
            any(
                message.get("method") == "turn/completed"
                and message["params"]["turn"]["status"] == "completed"
                for message in success.server_messages("turn/start")
            )
        )
        request = next(
            message
            for message in approval.server_messages("turn/start")
            if message.get("method") == "item/tool/requestUserInput"
        )
        self.assertEqual(request["id"], 0)
        self.assertEqual(request["params"]["questions"][0]["id"], "wiki_surface")
        self.assertEqual(request["params"]["autoResolutionMs"], None)
        approval_rows = [
            json.loads(line)
            for line in (FIXTURES / "codex_app_server_approval.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        response = next(
            row["message"]
            for row in approval_rows
            if row["direction"] == "client"
            and row["message"].get("id") == request["id"]
            and "result" in row["message"]
        )
        self.assertEqual(
            response["result"],
            {"answers": {"wiki_surface": {"answers": ["Agents page"]}}},
        )
        self.assertTrue(
            any(
                message.get("method") == "error"
                for message in failure.server_messages("turn/start")
            )
        )
        self.assertIsNotNone(control.response_result("thread/resume"))
        steer = control.response_result("turn/steer")
        assert steer is not None
        self.assertIn("turnId", steer)
        self.assertEqual(control.response_result("turn/interrupt"), {})
        self.assertTrue(
            any(
                message.get("method") == "turn/completed"
                and message["params"]["turn"]["status"] == "interrupted"
                for message in control.server_messages("turn/interrupt")
            )
        )

    def test_captured_fixtures_do_not_contain_live_paths_or_credentials(self) -> None:
        forbidden = ("/Users/henry", "Henrys-MacBook", "access_token", "refresh_token")
        for path in FIXTURES.glob("*.jsonl"):
            text = path.read_text(encoding="utf-8")
            for marker in forbidden:
                self.assertNotIn(marker, text, f"{marker!r} leaked in {path.name}")

    def test_provider_normalizer_covers_wiki41_native_surface_dispositions(self) -> None:
        claude_cases = [
            ({"type": "progress", "data": {"type": "planning"}}, EventDisposition.RENDERED),
            (
                {"type": "permission-mode", "permissionMode": "bypassPermissions"},
                EventDisposition.RENDERED,
            ),
            (
                {"type": "system", "subtype": "api_error", "error": {"formatted": "529"}},
                EventDisposition.RENDERED,
            ),
            (
                {"type": "attachment", "attachment": {"type": "task_reminder"}},
                EventDisposition.RENDERED,
            ),
            ({"type": "custom-title", "customTitle": "Fixture"}, EventDisposition.SUMMARIZED),
            ({"type": "agent-name", "agentName": "worker"}, EventDisposition.SUMMARIZED),
            ({"type": "file-history-snapshot", "snapshot": {}}, EventDisposition.IGNORED),
            ({"type": "unknown-fixture"}, EventDisposition.UNKNOWN),
        ]
        for payload, disposition in claude_cases:
            with self.subTest(payload=payload):
                normalized = normalize_provider_event(ProviderKind.CLAUDE, payload)
                self.assertEqual(normalized.disposition, disposition)

        auth = normalize_provider_event(
            ProviderKind.CODEX,
            {"method": "account/chatgptAuthTokens/refresh", "params": {}},
        )
        approval = normalize_provider_event(
            ProviderKind.CODEX,
            {"method": "item/tool/requestUserInput", "params": {}},
        )
        self.assertEqual(auth.disposition, EventDisposition.RENDERED)
        self.assertEqual(auth.lifecycle_state, LifecycleState.BLOCKED)
        self.assertEqual(approval.kind, "approval")
        self.assertEqual(approval.lifecycle_state, LifecycleState.WAITING_APPROVAL)
        response = normalize_provider_event(
            ProviderKind.CODEX,
            {"id": 0, "result": {"answers": {}}},
            direction="client",
        )
        self.assertEqual(response.kind, "approval_response")
        self.assertEqual(response.disposition, EventDisposition.IGNORED)


class LifecycleTests(unittest.TestCase):
    def test_restart_recovery_table_is_closed_and_only_working_idle_resume(
        self,
    ) -> None:
        expected = {
            LifecycleState.STARTING: RecoveryAction.BLOCK,
            LifecycleState.WORKING: RecoveryAction.RESUME,
            LifecycleState.WAITING_APPROVAL: RecoveryAction.BLOCK,
            LifecycleState.IDLE: RecoveryAction.RESUME,
            LifecycleState.INTERRUPTED: RecoveryAction.SKIP,
            LifecycleState.DEAD: RecoveryAction.SKIP,
            LifecycleState.COMPLETED: RecoveryAction.SKIP,
            LifecycleState.BLOCKED: RecoveryAction.SKIP,
        }
        self.assertEqual(RESTART_RECOVERY_TABLE, expected)

        with tempfile.TemporaryDirectory() as tmp:
            for state, action in expected.items():
                record = _record(Path(tmp), f"WIKI-{state.value}")
                record.state = state
                record.provider_session_id = "session-fixture"
                decision = restart_recovery_decision(
                    record,
                    is_current=True,
                    provider_pid_alive=False,
                )
                self.assertEqual(decision.action, action, state.value)

    def test_replaced_terminal_live_pid_and_missing_session_never_duplicate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = _record(Path(tmp))
            record.state = LifecycleState.WORKING
            record.provider_session_id = "session-fixture"

            record.replaced_by_run_id = "replacement"
            self.assertEqual(
                restart_recovery_decision(
                    record,
                    is_current=True,
                    provider_pid_alive=False,
                ).action,
                RecoveryAction.SKIP,
            )

            record.replaced_by_run_id = None
            self.assertEqual(
                restart_recovery_decision(
                    record,
                    is_current=True,
                    provider_pid_alive=True,
                    provider_control_attached=True,
                ).action,
                RecoveryAction.RETAIN,
            )
            self.assertEqual(
                restart_recovery_decision(
                    record,
                    is_current=True,
                    provider_pid_alive=True,
                ).action,
                RecoveryAction.BLOCK,
            )

            record.provider_session_id = None
            self.assertEqual(
                restart_recovery_decision(
                    record,
                    is_current=True,
                    provider_pid_alive=False,
                ).action,
                RecoveryAction.BLOCK,
            )

    def test_terminal_state_cannot_be_resurrected(self) -> None:
        with self.assertRaisesRegex(ValueError, "dead -> working"):
            validate_transition(LifecycleState.DEAD, LifecycleState.WORKING)
        with self.assertRaisesRegex(ValueError, "completed -> idle"):
            validate_transition(LifecycleState.COMPLETED, LifecycleState.IDLE)


class RunStoreTests(unittest.TestCase):
    def test_run_id_cannot_escape_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(_paths(Path(tmp)))
            with self.assertRaisesRegex(StoreError, "invalid run id"):
                store.get("../../outside")

    def test_raw_event_is_durable_before_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(_paths(root))
            record = store.create(_record(root))

            raw = store.append_raw(
                record.run_id,
                provider="codex",
                direction="server",
                payload={"method": "turn/started", "params": {}},
            )
            reloaded = store.get(record.run_id)
            self.assertEqual(reloaded.raw_event_count, 1)
            self.assertEqual(reloaded.normalized_event_count, 0)
            self.assertEqual(store.read_raw_events(record.run_id)[0], raw)
            self.assertEqual(store.read_normalized_events(record.run_id), [])

            normalized = store.append_normalized(
                record.run_id,
                raw_seq=1,
                disposition=EventDisposition.RENDERED,
                kind="turn_started",
                payload={"method": "turn/started"},
            )
            self.assertEqual(normalized["raw_seq"], 1)
            reloaded = store.get(record.run_id)
            self.assertEqual(reloaded.disposition_counts["rendered"], 1)

    def test_pending_user_message_matching_is_durable_and_fifo_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(_paths(root))
            record = store.create(_record(root))
            first = str(uuid4())
            second = str(uuid4())
            store.track_pending_user_message(record.run_id, first, "same text")
            store.track_pending_user_message(record.run_id, second, "same text")

            matched = store.match_pending_user_message(
                record.run_id,
                "same text\n<system-reminder>hook context</system-reminder>",
            )
            self.assertIsNotNone(matched)
            assert matched is not None
            self.assertEqual(matched["pending_id"], first)
            stale_record = store.get(record.run_id).to_dict()
            raw = store.append_raw(
                record.run_id,
                provider="claude",
                direction="inbound",
                payload={"type": "user"},
            )
            store.append_normalized(
                record.run_id,
                raw_seq=raw["seq"],
                disposition=EventDisposition.RENDERED,
                kind="claude_user",
                payload={
                    "pending_id": first,
                    "composer_text": "same text",
                    "composer_sent_at": matched["sent_at"],
                },
            )
            store.run_path(record.run_id).write_text(
                json.dumps(stale_record),
                encoding="utf-8",
            )

            reloaded = RunStore(_paths(root))
            self.assertEqual(
                reloaded.match_pending_user_message(record.run_id, "same text")[
                    "pending_id"
                ],
                second,
            )
            self.assertEqual(reloaded.get(record.run_id).composer_messages[0]["seq"], 1)

    def test_event_inspector_pages_from_cursor_or_bounded_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(_paths(root))
            record = store.create(_record(root))
            for index in range(1, 4):
                raw = store.append_raw(
                    record.run_id,
                    provider="codex",
                    direction="server",
                    payload={"method": f"fixture/{index}"},
                )
                store.append_normalized(
                    record.run_id,
                    raw_seq=raw["seq"],
                    disposition=EventDisposition.RENDERED,
                    kind=f"fixture_{index}",
                    payload=raw["payload"],
                )

            self.assertEqual(
                [event["seq"] for event in store.read_raw_events(record.run_id, limit=2)],
                [2, 3],
            )
            self.assertEqual(
                [
                    event["seq"]
                    for event in store.read_normalized_events(
                        record.run_id,
                        after_seq=1,
                        limit=1,
                    )
                ],
                [2],
            )
            large_rows = [
                {"seq": index, "payload": {"text": "x" * 2048}}
                for index in range(1, 81)
            ]
            store.raw_events_path(record.run_id).write_text(
                "\n".join(json.dumps(row) for row in large_rows) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                [event["seq"] for event in store.read_raw_events(record.run_id, limit=2)],
                [79, 80],
            )

    def test_pending_provider_requests_are_durable_and_id_type_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(_paths(root))
            record = store.create(_record(root))
            numeric_payload = {
                "id": 0,
                "method": "item/tool/requestUserInput",
                "params": {"questions": [{"id": "scope"}]},
            }
            string_payload = {
                "id": "0",
                "method": "item/commandExecution/requestApproval",
                "params": {},
            }
            for payload in (numeric_payload, string_payload):
                raw = store.append_raw(
                    record.run_id,
                    provider="codex",
                    direction="server",
                    payload=payload,
                )
                store.append_normalized(
                    record.run_id,
                    raw_seq=raw["seq"],
                    disposition=EventDisposition.RENDERED,
                    kind="approval",
                    payload=payload,
                    lifecycle_state=LifecycleState.WAITING_APPROVAL,
                )

            pending = store.get(record.run_id).pending_requests
            self.assertEqual(set(pending), {"int:0", "str:0"})
            self.assertIsInstance(pending["int:0"]["request_id"], int)
            store.clear_pending_request(record.run_id, 0)
            self.assertEqual(set(store.get(record.run_id).pending_requests), {"str:0"})
            store.get(record.run_id).pending_requests["str:ask"] = {
                "request_id": "ask",
                "request_kind": "can_use_tool",
                "received_at": "2026-07-10T12:00:00+00:00",
                "raw_seq": 3,
                "payload": {
                    "request": {
                        "subtype": "can_use_tool",
                        "tool_use_id": "toolu_fixture",
                    }
                },
            }
            store._write_record(store.get(record.run_id))  # noqa: SLF001 - persist fixture mutation
            store.clear_pending_request_by_tool_use_id(
                record.run_id,
                "toolu_fixture",
            )
            self.assertEqual(set(store.get(record.run_id).pending_requests), {"str:0"})

            resolved_payload = {
                "method": "serverRequest/resolved",
                "params": {"requestId": "0"},
            }
            raw = store.append_raw(
                record.run_id,
                provider="codex",
                direction="server",
                payload=resolved_payload,
            )
            store.append_normalized(
                record.run_id,
                raw_seq=raw["seq"],
                disposition=EventDisposition.RENDERED,
                kind="approval_resolved",
                payload=resolved_payload,
            )
            self.assertEqual(store.get(record.run_id).pending_requests, {})

    def test_restart_rebuilds_pending_request_index_from_event_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            store = RunStore(paths)
            record = store.create(_record(root))
            approval = {
                "id": 0,
                "method": "item/tool/requestUserInput",
                "params": {"questions": []},
            }
            raw = store.append_raw(
                record.run_id,
                provider="codex",
                direction="server",
                payload=approval,
            )
            store.append_normalized(
                record.run_id,
                raw_seq=raw["seq"],
                disposition=EventDisposition.RENDERED,
                kind="approval",
                payload=approval,
                lifecycle_state=LifecycleState.WAITING_APPROVAL,
            )
            metadata = json.loads(store.run_path(record.run_id).read_text(encoding="utf-8"))
            metadata["pending_requests"] = {}
            store.run_path(record.run_id).write_text(
                json.dumps(metadata),
                encoding="utf-8",
            )

            restarted = RunStore(paths)
            self.assertIn("int:0", restarted.get(record.run_id).pending_requests)
            response = {"id": 0, "result": {"answers": {}}}
            raw = restarted.append_raw(
                record.run_id,
                provider="codex",
                direction="client",
                payload=response,
            )
            restarted.append_normalized(
                record.run_id,
                raw_seq=raw["seq"],
                disposition=EventDisposition.IGNORED,
                kind="approval_response",
                payload=response,
            )
            self.assertEqual(restarted.get(record.run_id).pending_requests, {})

    def test_store_files_are_private_and_registry_keeps_legacy_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            store = RunStore(paths)
            record = store.create(_record(root))
            self.assertEqual(
                store.file_modes(record.run_id),
                {"run": 0o600, "raw": 0o600, "events": 0o600},
            )
            self.assertEqual(store.run_dir(record.run_id).stat().st_mode & 0o777, 0o700)
            registry = json.loads(paths.registry_path.read_text(encoding="utf-8"))
            current = registry["WIKI-42"]["current"]
            self.assertEqual(current["run_id"], record.run_id)
            self.assertEqual(current["kind"], "cdx")
            self.assertIsNone(current["window"])
            self.assertEqual(current["log"], str(store.raw_events_path(record.run_id)))

    def test_create_archives_stale_legacy_current_during_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            paths.registry_path.parent.mkdir(parents=True)
            paths.registry_path.write_text(
                json.dumps(
                    {
                        "WIKI-42": {
                            "history": [],
                            "current": {
                                "window": "@9999",
                                "kind": "cdx",
                                "role": "implement",
                                "model": "legacy",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            store = RunStore(paths)
            with self.assertRaisesRegex(StoreConflict, "explicit migration"):
                store.create(_record(root))
            record = store.create(_record(root), migrate_legacy=True)
            registry = json.loads(paths.registry_path.read_text(encoding="utf-8"))

            self.assertEqual(registry["WIKI-42"]["current"]["run_id"], record.run_id)
            legacy = registry["WIKI-42"]["history"][0]
            self.assertEqual(legacy["window"], "@9999")
            self.assertEqual(legacy["outcome"], "handoff")
            self.assertEqual(legacy["migration"], "headless-supervisor")

    def test_create_archives_stale_legacy_orchestrator_only_when_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            paths.registry_path.parent.mkdir(parents=True)
            paths.registry_path.write_text(
                json.dumps(
                    {
                        "_orchestrators": {
                            "wiki_dev": {
                                "window": "@9999",
                                "cwd": str(root / "legacy-worktree"),
                                "model": "opus",
                                "spawned_at": "2026-07-08T12:00:00+00:00",
                            },
                            "keep-me": {"window": "@9998", "model": "sonnet"},
                        }
                    }
                ),
                encoding="utf-8",
            )

            store = RunStore(paths)
            record = _record(root, "wiki_dev")
            record.provider = ProviderKind.CLAUDE
            record.role = "orchestrator"
            record.model = "opus"
            record.orchestrator_id = None
            with self.assertRaisesRegex(StoreConflict, "explicit migration"):
                store.create(record)
            created = store.create(record, migrate_legacy=True)
            registry = json.loads(paths.registry_path.read_text(encoding="utf-8"))

            self.assertEqual(registry["wiki_dev"]["current"]["run_id"], created.run_id)
            legacy = registry["wiki_dev"]["history"][0]
            self.assertEqual(legacy["window"], "@9999")
            self.assertEqual(legacy["role"], "orchestrator")
            self.assertEqual(legacy["kind"], "cc")
            self.assertEqual(legacy["migration"], "headless-supervisor")
            self.assertNotIn("wiki_dev", registry["_orchestrators"])
            self.assertIn("keep-me", registry["_orchestrators"])

    def test_restart_finishes_legacy_orchestrator_migration_after_registry_crash(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            paths.registry_path.parent.mkdir(parents=True)
            paths.registry_path.write_text(
                json.dumps(
                    {
                        "_orchestrators": {
                            "wiki-dev": {
                                "window": "@9999",
                                "cwd": str(root / "legacy-worktree"),
                                "model": "opus",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            store = RunStore(paths)
            record = _record(root, "wiki-dev")
            record.provider = ProviderKind.CLAUDE
            record.role = "orchestrator"
            record.orchestrator_id = None
            with mock.patch.object(
                store,
                "_write_registry",
                side_effect=OSError("simulated registry crash"),
            ):
                with self.assertRaisesRegex(OSError, "simulated registry crash"):
                    store.create(record, migrate_legacy=True)

            restarted = RunStore(paths)
            registry = json.loads(paths.registry_path.read_text(encoding="utf-8"))
            self.assertEqual(restarted.current_run_id("wiki-dev"), record.run_id)
            self.assertNotIn("_orchestrators", registry)
            self.assertEqual(registry["wiki-dev"]["history"][0]["window"], "@9999")
            self.assertEqual(
                registry["wiki-dev"]["history"][0]["migration"],
                "headless-supervisor",
            )

    def test_restart_repairs_crash_stale_counts_and_truncated_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            store = RunStore(paths)
            record = store.create(_record(root))
            raw = store.raw_events_path(record.run_id)
            raw.write_text(
                '{"seq":1,"provider":"codex","direction":"server","payload":{}}\n'
                '{"seq":2,"provider":"codex"',
                encoding="utf-8",
            )
            self.assertEqual(store.get(record.run_id).raw_event_count, 0)

            restarted = RunStore(paths)
            repaired = restarted.get(record.run_id)
            self.assertEqual(repaired.raw_event_count, 1)
            self.assertTrue(raw.read_bytes().endswith(b"\n"))
            appended = restarted.append_raw(
                record.run_id,
                provider="codex",
                direction="server",
                payload={"method": "turn/started"},
            )
            self.assertEqual(appended["seq"], 2)

    def test_restart_replays_lifecycle_event_missing_from_run_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            store = RunStore(paths)
            record = store.create(_record(root))
            record = store.transition(record.run_id, LifecycleState.WORKING)
            store.normalized_events_path(record.run_id).write_text(
                json.dumps(
                    {
                        "seq": 1,
                        "raw_seq": 1,
                        "disposition": "rendered",
                        "kind": "turn_completed",
                        "payload": {},
                        "lifecycle_state": "idle",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            restarted = RunStore(paths)
            repaired = restarted.get(record.run_id)
            self.assertEqual(repaired.state, LifecycleState.IDLE)
            self.assertEqual(repaired.last_lifecycle_event_seq, 1)
            registry = json.loads(paths.registry_path.read_text(encoding="utf-8"))
            self.assertEqual(registry["WIKI-42"]["current"]["state"], "idle")

    def test_restart_repairs_replacement_crash_before_registry_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            store = RunStore(paths)
            old = store.create(_record(root))
            replacement = _record(root)
            replacement.state = LifecycleState.WORKING
            replacement.provider_session_id = "replacement-session"
            replacement.provider_pid = 4242
            replacement.provider_generation = 2
            replacement.transcript_path = "/isolated/replacement-rollout.jsonl"
            with mock.patch.object(
                store,
                "_write_registry",
                side_effect=OSError("simulated crash before registry rename"),
            ):
                with self.assertRaisesRegex(OSError, "simulated crash"):
                    store.replace(old.run_id, replacement)

            restarted = RunStore(paths)
            repaired_old = restarted.get(old.run_id)
            repaired_replacement = restarted.get(replacement.run_id)
            self.assertEqual(restarted.current_run_id("WIKI-42"), replacement.run_id)
            self.assertEqual(repaired_old.replaced_by_run_id, replacement.run_id)
            self.assertEqual(repaired_old.state, LifecycleState.COMPLETED)
            self.assertEqual(repaired_replacement.state, LifecycleState.WORKING)
            self.assertEqual(
                repaired_replacement.provider_session_id,
                "replacement-session",
            )
            self.assertEqual(repaired_replacement.provider_generation, 2)
            registry = json.loads(paths.registry_path.read_text(encoding="utf-8"))
            self.assertEqual(registry["WIKI-42"]["history"][0]["run_id"], old.run_id)

    def test_adapter_status_updates_registry_identity_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            store = RunStore(paths)
            record = store.create(_record(root))
            updated = store.update_adapter_status(
                record.run_id,
                AdapterStatus(
                    LifecycleState.WORKING,
                    "session-1",
                    4242,
                    generation=2,
                    active_turn_id="turn-1",
                    transcript_path="/isolated/rollout.jsonl",
                ),
            )
            self.assertEqual(updated.provider_session_id, "session-1")
            current = json.loads(paths.registry_path.read_text(encoding="utf-8"))[
                "WIKI-42"
            ]["current"]
            self.assertEqual(current["provider_pid"], 4242)
            self.assertEqual(current["provider_generation"], 2)
            self.assertEqual(current["active_turn_id"], "turn-1")

    def test_quiesce_intent_is_exact_current_session_and_durable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            store = RunStore(paths)
            record = store.create(_record(root))
            record = store.update_adapter_status(
                record.run_id,
                AdapterStatus(
                    LifecycleState.WORKING,
                    "session-1",
                    4242,
                    active_turn_id="turn-1",
                ),
            )
            operation_id = str(uuid4())

            with self.assertRaisesRegex(StoreConflict, "session changed"):
                store.mark_quiesce_intent(
                    record.run_id,
                    operation_id,
                    "stale-session",
                )
            marked = store.mark_quiesce_intent(
                record.run_id,
                operation_id,
                "session-1",
            )
            self.assertEqual(marked.quiesce_operation_id, operation_id)
            self.assertEqual(marked.quiesce_resume_state, LifecycleState.WORKING)
            self.assertEqual(
                store.mark_quiesce_intent(
                    record.run_id,
                    operation_id,
                    "session-1",
                ).quiesce_operation_id,
                operation_id,
            )
            with self.assertRaisesRegex(StoreConflict, "another quiesce"):
                store.mark_quiesce_intent(
                    record.run_id,
                    str(uuid4()),
                    "session-1",
                )

            restarted = RunStore(paths)
            persisted = restarted.get(record.run_id)
            self.assertEqual(persisted.quiesce_operation_id, operation_id)
            self.assertEqual(
                persisted.quiesce_resume_state,
                LifecycleState.WORKING,
            )
            current = json.loads(paths.registry_path.read_text(encoding="utf-8"))[
                "WIKI-42"
            ]["current"]
            self.assertEqual(current["quiesce_operation_id"], operation_id)
            self.assertEqual(current["quiesce_resume_state"], "working")

    def test_quiesce_intent_rejects_nonresumable_and_noncurrent_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(_paths(root))
            record = store.create(_record(root))
            record = store.update_adapter_status(
                record.run_id,
                AdapterStatus(
                    LifecycleState.WAITING_APPROVAL,
                    "session-1",
                    4242,
                ),
            )
            with self.assertRaisesRegex(StoreConflict, "waiting-approval"):
                store.mark_quiesce_intent(
                    record.run_id,
                    str(uuid4()),
                    "session-1",
                )

            store.transition(record.run_id, LifecycleState.IDLE)
            replacement = _record(root)
            _, replacement = store.replace(record.run_id, replacement)
            with self.assertRaisesRegex(StoreConflict, "no longer current"):
                store.mark_quiesce_intent(
                    record.run_id,
                    str(uuid4()),
                    "session-1",
                )
            self.assertEqual(store.current_run_id("WIKI-42"), replacement.run_id)

    def test_quiesce_intent_can_capture_prior_working_state_from_blocked_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(_paths(root))
            record = store.create(_record(root))
            record = store.update_adapter_status(
                record.run_id,
                AdapterStatus(
                    LifecycleState.WORKING,
                    "session-1",
                    4242,
                ),
            )
            store.transition(
                record.run_id,
                LifecycleState.BLOCKED,
                reason="fixture auth-dead",
            )

            marked = store.mark_quiesce_intent(
                record.run_id,
                str(uuid4()),
                "session-1",
                resume_state=LifecycleState.WORKING,
            )
            self.assertEqual(marked.quiesce_resume_state, LifecycleState.WORKING)

    def test_quiesce_detach_converges_after_late_lifecycle_and_clears_on_resume(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            store = RunStore(paths)
            record = store.create(_record(root))
            record = store.update_adapter_status(
                record.run_id,
                AdapterStatus(
                    LifecycleState.WORKING,
                    "session-1",
                    4242,
                    active_turn_id="turn-1",
                ),
            )
            operation_id = str(uuid4())
            store.mark_quiesce_intent(
                record.run_id,
                operation_id,
                "session-1",
            )

            approval = {"id": 7, "method": "item/commandExecution/requestApproval"}
            raw = store.append_raw(
                record.run_id,
                provider="codex",
                direction="server",
                payload=approval,
            )
            store.append_normalized(
                record.run_id,
                raw_seq=raw["seq"],
                disposition=EventDisposition.RENDERED,
                kind="approval",
                payload=approval,
                lifecycle_state=LifecycleState.WAITING_APPROVAL,
            )
            with self.assertRaisesRegex(StoreConflict, "missing or stale"):
                store.finish_provider_detached(
                    record.run_id,
                    str(uuid4()),
                    reason="quiesced for account rotation",
                )

            detached = store.finish_provider_detached(
                record.run_id,
                operation_id,
                reason="quiesced for account rotation",
            )
            self.assertEqual(detached.state, LifecycleState.BLOCKED)
            self.assertEqual(detached.recovery_from_state, LifecycleState.WORKING)
            self.assertIsNone(detached.provider_pid)
            self.assertIsNone(detached.active_turn_id)
            self.assertEqual(detached.pending_requests, {})
            self.assertTrue(detached.automatic_resume_suppressed)
            self.assertEqual(detached.quiesce_operation_id, operation_id)

            with self.assertRaisesRegex(StoreConflict, "not completed"):
                store.clear_quiesce_marker(record.run_id, operation_id)
            resumed = store.update_adapter_status(
                record.run_id,
                AdapterStatus(LifecycleState.IDLE, "session-1", 5252),
            )
            self.assertEqual(resumed.recovery_from_state, LifecycleState.WORKING)
            self.assertTrue(resumed.automatic_resume_suppressed)
            with self.assertRaisesRegex(StoreConflict, "quiesce marker"):
                store.clear_automatic_resume_suppression(record.run_id)
            with self.assertRaisesRegex(StoreConflict, "missing or stale"):
                store.clear_quiesce_marker(record.run_id, str(uuid4()))

            cleared = store.clear_quiesce_marker(record.run_id, operation_id)
            self.assertIsNone(cleared.quiesce_operation_id)
            self.assertIsNone(cleared.quiesce_resume_state)
            self.assertIsNone(cleared.recovery_from_state)
            self.assertFalse(cleared.automatic_resume_suppressed)
            current = json.loads(paths.registry_path.read_text(encoding="utf-8"))[
                "WIKI-42"
            ]["current"]
            self.assertIsNone(current["quiesce_operation_id"])
            self.assertIsNone(current["quiesce_resume_state"])

    def test_terminal_run_can_abandon_but_current_resumable_cannot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(_paths(root))
            record = store.create(_record(root))
            record = store.update_adapter_status(
                record.run_id,
                AdapterStatus(LifecycleState.IDLE, "session-1", 4242),
            )
            operation_id = str(uuid4())
            store.mark_quiesce_intent(record.run_id, operation_id, "session-1")
            with self.assertRaisesRegex(StoreConflict, "cannot abandon"):
                store.abandon_quiesce_marker(record.run_id, operation_id)

            store.transition(record.run_id, LifecycleState.COMPLETED)
            abandoned = store.abandon_quiesce_marker(record.run_id, operation_id)
            self.assertEqual(abandoned.state, LifecycleState.COMPLETED)
            self.assertIsNone(abandoned.quiesce_operation_id)
            self.assertIsNone(abandoned.quiesce_resume_state)

    def test_rotation_journal_is_atomic_private_and_restart_durable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            store = RunStore(paths)
            self.assertIsNone(store.read_codex_rotation_journal())
            operation_id = str(uuid4())
            journal = {
                "operation_id": operation_id,
                "phase": "providers-detached",
                "runs": [{"run_id": str(uuid4()), "agent_id": "WIKI-42"}],
            }
            store.write_codex_rotation_journal(journal)
            self.assertEqual(
                paths.codex_rotation_journal_path.stat().st_mode & 0o777,
                0o600,
            )
            self.assertEqual(
                RunStore(paths).read_codex_rotation_journal(),
                journal,
            )
            store.clear_codex_rotation_journal()
            self.assertIsNone(RunStore(paths).read_codex_rotation_journal())

            paths.codex_rotation_journal_path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(StoreError, "must contain an object"):
                store.read_codex_rotation_journal()

    def test_legacy_codex_detection_is_strict_and_ignores_headless_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            paths.registry_path.parent.mkdir(parents=True)
            paths.registry_path.write_text(
                json.dumps(
                    {
                        "WIKI-LEGACY": {"current": {"kind": "cdx", "window": "@9999"}},
                        "WIKI-HEADLESS": {
                            "current": {"kind": "cdx", "run_id": str(uuid4())}
                        },
                        "WIKI-CLAUDE": {"current": {"kind": "cc"}},
                        "_orchestrators": {"wiki-dev": {"kind": "cdx"}},
                    }
                ),
                encoding="utf-8",
            )
            store = RunStore(paths)
            self.assertEqual(store.legacy_codex_agent_ids(), ["WIKI-LEGACY"])

            paths.registry_path.write_text(
                json.dumps({"WIKI-BROKEN": {"current": []}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(StoreError, "current entry"):
                store.legacy_codex_agent_ids()

    def test_replace_archives_handoff_and_prevents_stale_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            store = RunStore(paths)
            old = store.create(_record(root))
            replacement = _record(root)
            replacement.replaces_run_id = old.run_id
            archived, current = store.replace(old.run_id, replacement)
            self.assertEqual(archived.replaced_by_run_id, current.run_id)
            self.assertEqual(archived.outcome, "handoff")
            self.assertEqual(store.current_run_id("WIKI-42"), current.run_id)
            registry = json.loads(paths.registry_path.read_text(encoding="utf-8"))
            self.assertEqual(registry["WIKI-42"]["history"][0]["outcome"], "handoff")
            with self.assertRaisesRegex(StoreConflict, "no longer current"):
                store.replace(old.run_id, _record(root))

    def test_archive_current_writes_snapshot_and_removes_runtime_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            store = RunStore(paths)
            record = store.create(_record(root))
            record = store.transition(record.run_id, LifecycleState.COMPLETED)
            paths.status_dir.mkdir(parents=True, exist_ok=True)
            status_path = paths.status_dir / "WIKI-42.json"
            status_path.write_text(
                json.dumps({"state": "merge-ready", "step": "done", "pr": "https://example/pr/42"}),
                encoding="utf-8",
            )
            artifact_dir = store.run_dir(record.run_id) / "artifacts"
            artifact_dir.mkdir(mode=0o700)
            artifact_path = artifact_dir / "00000000-0000-4000-8000-000000000085.png"
            artifact_path.write_bytes(b"artifact-png")
            artifact_path.chmod(0o600)
            outside = root / "outside.jpg"
            outside.write_bytes(b"must-not-be-copied")
            linked_artifact = artifact_dir / "00000000-0000-4000-8000-000000000086.jpg"
            linked_artifact.symlink_to(outside)

            archived, session_dir = store.archive_current(
                record.run_id,
                outcome="merged",
            )

            self.assertEqual(archived.outcome, "merged")
            self.assertFalse(store.run_dir(record.run_id).exists())
            self.assertEqual(store.current_run_id("WIKI-42"), None)
            self.assertFalse(status_path.exists())
            self.assertTrue((session_dir / "run.json").is_file())
            self.assertTrue((session_dir / "raw.jsonl").is_file())
            self.assertTrue((session_dir / "events.jsonl").is_file())
            self.assertTrue((session_dir / "cdx-WIKI-42.log").is_file())
            archived_artifact = session_dir / "artifacts" / artifact_path.name
            self.assertEqual(archived_artifact.read_bytes(), b"artifact-png")
            self.assertEqual(archived_artifact.stat().st_mode & 0o777, 0o600)
            self.assertTrue((session_dir / "artifacts" / linked_artifact.name).is_symlink())
            self.assertEqual(
                (session_dir / "cdx-WIKI-42-prompt.md").read_text(encoding="utf-8"),
                "Work on ticket WIKI-42",
            )
            meta = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["outcome"], "merged")
            self.assertEqual(meta["worker"]["run_id"], record.run_id)
            final_status = json.loads(
                (session_dir / "final-status.json").read_text(encoding="utf-8")
            )
            self.assertEqual(final_status["state"], "merge-ready")

    def test_reconcile_prunes_headless_registry_rows_missing_run_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            paths.registry_path.parent.mkdir(parents=True, exist_ok=True)
            missing_run_id = str(uuid4())
            paths.registry_path.write_text(
                json.dumps(
                    {
                        "WIKI-42": {
                            "history": [],
                            "current": {
                                "run_id": missing_run_id,
                                "kind": "cdx",
                                "role": "implement",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            store = RunStore(paths)

            self.assertEqual(store.current_run_id("WIKI-42"), None)
            self.assertEqual(
                json.loads(paths.registry_path.read_text(encoding="utf-8")),
                {},
            )

    def test_env_paths_keep_every_live_surface_redirectable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                "WIKI_AGENT_RUNTIME_DIR": str(root / "runtime"),
                "WIKI_SUPERVISOR_SOCKET_PATH": str(root / "control.sock"),
                "WIKI_AGENT_REGISTRY_PATH": str(root / "registry.json"),
                "WIKI_AGENT_ARCHIVE_DIR": str(root / "archive"),
                "WIKI_AGENT_STATUS_DIR": str(root / "status"),
            }
            paths = RuntimePaths.from_env(env)
            resolved = root.resolve()
            self.assertEqual(paths.runtime_dir, resolved / "runtime")
            self.assertEqual(paths.socket_path, resolved / "control.sock")
            self.assertEqual(paths.registry_path, resolved / "registry.json")
            self.assertEqual(paths.archive_dir, resolved / "archive")
            self.assertEqual(paths.status_dir, resolved / "status")

    def test_registry_under_symlinked_parent_dir_writes_fine(self) -> None:
        """macOS /tmp is a symlink to /private/tmp; parents must resolve so the
        symlink-dir guard only rejects the final component."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            real.mkdir()
            link = root / "link"
            link.symlink_to(real, target_is_directory=True)
            env = {
                "WIKI_AGENT_RUNTIME_DIR": str(root / "runtime"),
                "WIKI_SUPERVISOR_SOCKET_PATH": str(root / "control.sock"),
                "WIKI_AGENT_REGISTRY_PATH": str(link / "registry.json"),
                "WIKI_AGENT_ARCHIVE_DIR": str(root / "archive"),
                "WIKI_AGENT_STATUS_DIR": str(link / "status"),
            }
            paths = RuntimePaths.from_env(env)
            self.assertEqual(paths.registry_path.parent, real.resolve())
            _atomic_write_json(paths.registry_path, {"ok": True})
            self.assertTrue((real / "registry.json").is_file())

    def test_symlinked_final_component_still_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            planted = root / "registry.json"
            planted.symlink_to(target)
            env = {
                "WIKI_AGENT_RUNTIME_DIR": str(root / "runtime"),
                "WIKI_AGENT_REGISTRY_PATH": str(planted),
            }
            paths = RuntimePaths.from_env(env)
            with self.assertRaises(StoreError):
                _atomic_write_json(paths.registry_path, {"ok": True})


if __name__ == "__main__":
    unittest.main()
