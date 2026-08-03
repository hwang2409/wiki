from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock
from uuid import uuid4

from backend.app import transcripts
from backend.app.agent_runtime import store as store_module
from backend.app.agent_runtime.fake import WireFixture
from backend.app.agent_runtime.normalizer import normalize_provider_event
from backend.app.agent_runtime.process import (
    ProviderProcessStatus,
    provider_process_group_members_sync,
    provider_process_status_sync,
)
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
    MAX_PENDING_USER_MESSAGES,
    ProviderKind,
    RecoveryAction,
    RunRecord,
    RESTART_RECOVERY_TABLE,
    restart_recovery_decision,
    validate_transition,
)
from backend.app.agent_runtime.unknown_kind_telemetry import UnknownKindTelemetry


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

    def test_provider_normalizer_covers_wiki41_native_surface_dispositions(
        self,
    ) -> None:
        claude_cases = [
            (
                {"type": "progress", "data": {"type": "planning"}},
                EventDisposition.RENDERED,
            ),
            (
                {"type": "permission-mode", "permissionMode": "bypassPermissions"},
                EventDisposition.RENDERED,
            ),
            (
                {
                    "type": "system",
                    "subtype": "api_error",
                    "error": {"formatted": "529"},
                },
                EventDisposition.RENDERED,
            ),
            (
                {
                    "type": "system",
                    "subtype": "thinking_tokens",
                    "estimated_tokens": 42,
                    "estimated_tokens_delta": 4,
                },
                EventDisposition.SUMMARIZED,
            ),
            ({"type": "system", "subtype": "init"}, EventDisposition.RENDERED),
            (
                {"type": "system", "subtype": "task_notification"},
                EventDisposition.RENDERED,
            ),
            ({"type": "system", "subtype": "task_updated"}, EventDisposition.RENDERED),
            ({"type": "system", "subtype": "api_retry"}, EventDisposition.RENDERED),
            (
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "status": "rejected",
                        "rateLimitType": "five_hour",
                        "isUsingOverage": False,
                        "overageStatus": "rejected",
                        "overageDisabledReason": "out_of_credits",
                        "resetsAt": 1784910600,
                    },
                },
                EventDisposition.RENDERED,
            ),
            (
                {"type": "attachment", "attachment": {"type": "task_reminder"}},
                EventDisposition.RENDERED,
            ),
            (
                {"type": "custom-title", "customTitle": "Fixture"},
                EventDisposition.SUMMARIZED,
            ),
            (
                {"type": "agent-name", "agentName": "worker"},
                EventDisposition.SUMMARIZED,
            ),
            (
                {"type": "file-history-snapshot", "snapshot": {}},
                EventDisposition.IGNORED,
            ),
            ({"type": "unknown-fixture"}, EventDisposition.UNKNOWN),
        ]
        for payload, disposition in claude_cases:
            with self.subTest(payload=payload):
                normalized = normalize_provider_event(ProviderKind.CLAUDE, payload)
                self.assertEqual(normalized.disposition, disposition)
                if payload.get("type") == "system":
                    self.assertEqual(normalized.kind, f"claude_{payload['subtype']}")
                elif payload.get("type") == "rate_limit_event":
                    self.assertEqual(normalized.kind, "claude_rate_limit_event")
                    self.assertEqual(normalized.payload["status"], "rejected")
                    self.assertEqual(normalized.payload["rateLimitType"], "five_hour")
                    self.assertFalse(normalized.payload["isUsingOverage"])
                    self.assertEqual(normalized.payload["overageStatus"], "rejected")
                    self.assertEqual(
                        normalized.payload["overageDisabledReason"], "out_of_credits"
                    )
                    self.assertEqual(normalized.payload["resetsAt"], 1784910600)

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

    def test_codex_stream_renderer_methods_have_explicit_dispositions(self) -> None:
        rendered_methods = (
            "turn/diff/updated",
            "item/commandExecution/terminalInteraction",
            "warning",
            "skills/changed",
            "turn/plan/updated",
        )
        summarized_methods = (
            "item/reasoning/summaryPartAdded",
            "hook/started",
            "hook/completed",
        )
        ignored_methods = ("rawResponse/completed",)
        for method in rendered_methods:
            with self.subTest(method=method):
                normalized = normalize_provider_event(
                    ProviderKind.CODEX,
                    {"method": method, "params": {}},
                )
                self.assertEqual(normalized.disposition, EventDisposition.RENDERED)
                self.assertEqual(normalized.kind, method.replace("/", "_"))
        for method in summarized_methods:
            with self.subTest(method=method):
                normalized = normalize_provider_event(
                    ProviderKind.CODEX,
                    {"method": method, "params": {}},
                )
                self.assertEqual(normalized.disposition, EventDisposition.SUMMARIZED)
                self.assertEqual(normalized.kind, method.replace("/", "_"))
        for method in ignored_methods:
            with self.subTest(method=method):
                normalized = normalize_provider_event(
                    ProviderKind.CODEX,
                    {"method": method, "params": {}},
                )
                self.assertEqual(normalized.disposition, EventDisposition.IGNORED)
                self.assertEqual(normalized.kind, method.replace("/", "_"))

    def test_codex_moderation_metadata_warns_only_for_non_safe_flags(self) -> None:
        safe = normalize_provider_event(
            ProviderKind.CODEX,
            {
                "method": "turn/moderationMetadata",
                "params": {
                    "metadata": {
                        "prompt": {
                            "omnimod": {
                                "outputs": [
                                    {
                                        "results": [
                                            {
                                                "category_flags": {
                                                    "sexual": False,
                                                    "violence": False,
                                                }
                                            }
                                        ]
                                    }
                                ]
                            }
                        }
                    }
                },
            },
        )
        warning = normalize_provider_event(
            ProviderKind.CODEX,
            {
                "method": "turn/moderationMetadata",
                "params": {
                    "metadata": {
                        "prompt": {
                            "omnimod": {
                                "outputs": [
                                    {
                                        "results": [
                                            {
                                                "category_flags": {
                                                    "sexual": False,
                                                    "violence": True,
                                                }
                                            }
                                        ]
                                    }
                                ]
                            }
                        }
                    }
                },
            },
        )
        self.assertEqual(safe.disposition, EventDisposition.IGNORED)
        self.assertEqual(safe.kind, "turn_moderationMetadata")
        self.assertEqual(warning.disposition, EventDisposition.RENDERED)
        self.assertEqual(warning.kind, "turn_moderationMetadata_warning")

    def test_codex_moderation_metadata_warns_for_blocked_payload_shapes(self) -> None:
        payloads = (
            {"metadata": {"prompt": {"omnimod": {"outputs": [{"is_blocked": True}]}}}},
            {
                "metadata": {
                    "prompt": {
                        "omnimod": {
                            "outputs": [{"results": [{"labels": ["violence"]}]}]
                        }
                    }
                }
            },
        )
        for params in payloads:
            with self.subTest(params=params):
                normalized = normalize_provider_event(
                    ProviderKind.CODEX,
                    {"method": "turn/moderationMetadata", "params": params},
                )
                self.assertEqual(normalized.disposition, EventDisposition.RENDERED)
                self.assertEqual(normalized.kind, "turn_moderationMetadata_warning")

    def test_codex_render_artifact_completion_normalizes_as_artifact(self) -> None:
        row = json.loads(
            (FIXTURES / "codex_render_artifact_completed.jsonl").read_text(
                encoding="utf-8"
            )
        )
        normalized = normalize_provider_event(ProviderKind.CODEX, row["message"])

        self.assertEqual(normalized.disposition, EventDisposition.RENDERED)
        self.assertEqual(normalized.kind, "artifact")
        self.assertEqual(
            normalized.payload["id"], "6d0e7d00-2edf-4054-b0dc-fe17cd382c2a"
        )
        self.assertEqual(normalized.payload["title"], "Wiki.app architecture")
        self.assertEqual(
            normalized.payload["caption"],
            "Native shell, frontend, FastAPI, headless supervisor, MCP, CLI, and local storage/data-control flows.",
        )
        self.assertEqual(
            normalized.payload["artifact"],
            {"kind": "mermaid", "source": "flowchart TB\n  worker --> artifact"},
        )

    def test_codex_failed_render_completion_preserves_write_time_event(self) -> None:
        row = json.loads(
            (FIXTURES / "codex_render_artifact_completed.jsonl").read_text(
                encoding="utf-8"
            )
        )
        failure_cases = (
            ("status", {"status": "failed"}),
            ("error", {"error": "artifact server rejected the request"}),
            ("result.isError", None),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(_paths(root))
            record = store.create(_record(root))
            persisted = []
            for label, failure_update in failure_cases:
                message = deepcopy(row["message"])
                item = message["params"]["item"]
                item["status"] = "completed"
                item["error"] = None
                item["result"]["isError"] = False
                if failure_update is None:
                    item["result"]["isError"] = True
                else:
                    item.update(failure_update)

                normalized = normalize_provider_event(ProviderKind.CODEX, message)
                self.assertEqual(
                    normalized.disposition, EventDisposition.RENDERED, label
                )
                self.assertEqual(normalized.kind, "item_completed", label)

                raw = store.append_raw(
                    record.run_id,
                    provider="codex",
                    direction="server",
                    payload=message,
                )
                persisted.append(
                    store.append_normalized(
                        record.run_id,
                        raw_seq=raw["seq"],
                        disposition=normalized.disposition,
                        kind=normalized.kind,
                        payload=normalized.payload,
                        lifecycle_state=normalized.lifecycle_state,
                    )
                )

            unparseable = deepcopy(row["message"])
            item = unparseable["params"]["item"]
            item["status"] = "completed"
            item["error"] = None
            item["result"]["isError"] = False
            item["result"]["content"][0]["text"] = "not a wiki artifact sentinel"
            normalized = normalize_provider_event(ProviderKind.CODEX, unparseable)
            raw = store.append_raw(
                record.run_id,
                provider="codex",
                direction="server",
                payload=unparseable,
            )
            persisted.append(
                store.append_normalized(
                    record.run_id,
                    raw_seq=raw["seq"],
                    disposition=normalized.disposition,
                    kind=normalized.kind,
                    payload=normalized.payload,
                    lifecycle_state=normalized.lifecycle_state,
                )
            )

            stored_lines = (
                store.normalized_events_path(record.run_id)
                .read_text(encoding="utf-8")
                .splitlines()
            )
            self.assertEqual([json.loads(line) for line in stored_lines], persisted)
            self.assertEqual(
                [event["payload"]["params"]["item"]["status"] for event in persisted],
                ["failed", "completed", "completed", "completed"],
            )
            self.assertEqual(
                persisted[1]["payload"]["params"]["item"]["error"],
                "artifact server rejected the request",
            )
            self.assertTrue(
                persisted[2]["payload"]["params"]["item"]["result"]["isError"]
            )

            transcripts._cache.clear()
            parsed = transcripts.read_session_events(
                "codex-normalized", store.normalized_events_path(record.run_id)
            )
            self.assertEqual(len(parsed["events"]), 4)
            for event in parsed["events"][:3]:
                self.assertEqual(event["kind"], "tool")
                self.assertFalse(event["tool"]["ok"])
                self.assertEqual(event["tool"]["summary"], "render_artifact rejected")
                self.assertIn("<<wiki-artifact:v1>>", event["tool"]["output"])

            completed_unparseable = parsed["events"][3]
            self.assertEqual(completed_unparseable["kind"], "tool")
            self.assertTrue(completed_unparseable["tool"]["ok"])
            self.assertEqual(
                completed_unparseable["tool"]["summary"],
                "render_artifact completed without a parseable artifact",
            )
            self.assertEqual(
                completed_unparseable["tool"]["output"], "not a wiki artifact sentinel"
            )


class LifecycleTests(unittest.TestCase):
    def test_restart_recovery_table_is_closed_and_resumable_sessions(
        self,
    ) -> None:
        expected = {
            LifecycleState.STARTING: RecoveryAction.BLOCK,
            LifecycleState.WORKING: RecoveryAction.RESUME,
            LifecycleState.WAITING_APPROVAL: RecoveryAction.RESUME,
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

    def test_current_turn_diff_survives_event_window_and_resets_on_new_turn(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            store = RunStore(paths)
            record = store.create(_record(root))

            def append(method: str, params: dict[str, Any], kind: str) -> None:
                payload = {"method": method, "params": params}
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
                    kind=kind,
                    payload=payload,
                )

            append("turn/started", {"turn": {"id": "turn-1"}}, "turn_started")
            append("turn/diff/updated", {"diff": "diff one"}, "turn_diff_updated")
            self.assertNotIn("diff one", store.run_path(record.run_id).read_text())
            self.assertEqual(
                json.loads(store.current_turn_diff_path(record.run_id).read_text()),
                {"diff": "diff one"},
            )
            for index in range(51):
                append("warning", {"message": f"event {index}"}, "warning")

            window = store.read_normalized_events(record.run_id, limit=50)
            self.assertFalse(
                any(event["kind"] == "turn_diff_updated" for event in window)
            )
            self.assertEqual(
                store.current_turn_diff(record.run_id),
                {"turn_id": "turn-1", "seq": 2, "diff": "diff one"},
            )

            append("turn/started", {"turn": {"id": "turn-2"}}, "turn_started")
            self.assertIsNone(store.current_turn_diff(record.run_id))
            append("turn/diff/updated", {"diff": "diff two"}, "turn_diff_updated")
            self.assertEqual(
                store.current_turn_diff(record.run_id),
                {"turn_id": "turn-2", "seq": 55, "diff": "diff two"},
            )
            restarted = RunStore(paths)
            self.assertEqual(
                restarted.current_turn_diff(record.run_id),
                {"turn_id": "turn-2", "seq": 55, "diff": "diff two"},
            )

    def test_current_turn_diff_sidecar_is_not_rewritten_for_unrelated_events(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(_paths(root))
            record = store.create(_record(root))

            def append(method: str, params: dict[str, Any], kind: str) -> None:
                payload = {"method": method, "params": params}
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
                    kind=kind,
                    payload=payload,
                )

            with mock.patch.object(
                store,
                "_write_current_turn_diff_snapshot",
                wraps=store._write_current_turn_diff_snapshot,
            ) as write_snapshot:
                append("turn/started", {"turn": {"id": "turn-1"}}, "turn_started")
                append("turn/diff/updated", {"diff": "large diff"}, "turn_diff_updated")
                for index in range(200):
                    append("warning", {"message": str(index)}, "warning")
            self.assertEqual(write_snapshot.call_count, 2)

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

    def test_pending_user_message_store_rejects_overbound_append(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(_paths(root))
            record = store.create(_record(root))
            for index in range(MAX_PENDING_USER_MESSAGES):
                store.track_pending_user_message(
                    record.run_id,
                    f"pending-{index}",
                    "same text",
                )

            with self.assertRaisesRegex(
                StoreConflict, "pending user message limit reached"
            ):
                store.track_pending_user_message(
                    record.run_id,
                    "pending-overflow",
                    "same text",
                )

            self.assertEqual(
                len(store.get(record.run_id).pending_user_messages),
                MAX_PENDING_USER_MESSAGES,
            )

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
                [
                    event["seq"]
                    for event in store.read_raw_events(record.run_id, limit=2)
                ],
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
                [
                    event["seq"]
                    for event in store.read_raw_events(record.run_id, limit=2)
                ],
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
            metadata = json.loads(
                store.run_path(record.run_id).read_text(encoding="utf-8")
            )
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

    def test_legacy_snapshot_migration_preserves_external_blocked_state(
        self,
    ) -> None:
        """WIKI-232 REVIEW12 H1: pre-``last_causal_raw_seq`` snapshots
        lack the checkpoint. Without seeding it from the legacy
        ``last_lifecycle_event_seq`` marker on load, the boot rebuild
        replays every historical lifecycle event and destroys an
        externally-driven BLOCKED state — the exact case is a run that
        went WORKING -> IDLE via turn events, was then transitioned to
        BLOCKED with a ``state_reason`` and ``recovery_from_state`` by
        the recovery watcher (or ``mark_automatic_resume_failed``), and
        is expected to stay BLOCKED after the daemon restarts. The
        legacy migration must derive the applied raw boundary and
        replay only rows past it."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            store = RunStore(paths)
            record = store.create(_record(root))
            # Seed the lifecycle history the review requires: prior
            # WORKING (turn/started) then IDLE (turn/completed) — both
            # legal for a run before the operator/watcher pins BLOCKED.
            started = store.append_raw(
                record.run_id,
                provider="codex",
                direction="provider",
                payload={
                    "method": "turn/started",
                    "params": {"turn": {"turnId": "legacy-1"}},
                },
            )
            store.append_normalized(
                record.run_id,
                raw_seq=started["seq"],
                disposition=EventDisposition.RENDERED,
                kind="turn_started",
                payload={"method": "turn/started"},
                lifecycle_state=LifecycleState.WORKING,
            )
            completed = store.append_raw(
                record.run_id,
                provider="codex",
                direction="provider",
                payload={
                    "method": "turn/completed",
                    "params": {
                        "turn": {"turnId": "legacy-1", "status": "completed"}
                    },
                },
            )
            store.append_normalized(
                record.run_id,
                raw_seq=completed["seq"],
                disposition=EventDisposition.RENDERED,
                kind="turn_completed",
                payload={"status": "completed"},
                lifecycle_state=LifecycleState.IDLE,
            )
            # External transition after the events — pins BLOCKED with
            # state_reason and recovery_from_state.
            store.transition(
                record.run_id,
                LifecycleState.BLOCKED,
                reason="provider identity is uncertain after restart",
            )
            # Set recovery_from_state directly to mirror what
            # ``mark_provider_pid_recovery_pending`` / ``mark_automatic_resume_failed``
            # persist alongside the BLOCKED transition.
            live = store.get(record.run_id)
            live.recovery_from_state = LifecycleState.IDLE
            store._write_record(live)  # noqa: SLF001 - external-transition fixture
            live = store.get(record.run_id)
            self.assertEqual(live.state, LifecycleState.BLOCKED)
            self.assertEqual(
                live.state_reason,
                "provider identity is uncertain after restart",
            )
            self.assertEqual(
                live.recovery_from_state, LifecycleState.IDLE
            )

            # Simulate the legacy on-disk shape: strip
            # ``last_causal_raw_seq`` so the migration path has to
            # derive it from ``last_lifecycle_event_seq``. Keep
            # ``last_lifecycle_event_seq`` as the legacy schema wrote
            # it (the normalized seq of the last applied lifecycle
            # event — here the turn/completed row).
            metadata = json.loads(
                store.run_path(record.run_id).read_text(encoding="utf-8")
            )
            self.assertGreater(metadata.get("last_lifecycle_event_seq", 0), 0)
            metadata.pop("last_causal_raw_seq", None)
            store.run_path(record.run_id).write_text(
                json.dumps(metadata),
                encoding="utf-8",
            )

            restarted = RunStore(paths)
            recovered = restarted.get(record.run_id)
            # (a) External BLOCKED survives the migration.
            self.assertEqual(
                recovered.state,
                LifecycleState.BLOCKED,
                "legacy migration must not replay pre-BLOCKED lifecycle "
                "history and overwrite the externally-pinned state",
            )
            # (b) state_reason survives.
            self.assertEqual(
                recovered.state_reason,
                "provider identity is uncertain after restart",
            )
            # (c) recovery_from_state survives.
            self.assertEqual(
                recovered.recovery_from_state, LifecycleState.IDLE
            )
            # (d) The migration seeded last_causal_raw_seq from the
            # legacy marker so future stale-order recoveries are still
            # guarded — anything at or below the last applied raw_seq
            # is treated as already-applied causal history.
            self.assertGreaterEqual(
                recovered.last_causal_raw_seq,
                int(completed["seq"]),
                "migration must seed last_causal_raw_seq from the "
                "legacy last_lifecycle_event_seq boundary",
            )

    def test_rebuild_applies_later_lifecycle_row_at_same_raw_sequence(
        self,
    ) -> None:
        """REVIEW13 H1: the durable checkpoint is a raw/normalized pair.

        One provider row can produce more than one normalized lifecycle row.
        Simulate a crash after the later IDLE JSONL append but before its
        run.json replace. Recovery must apply the later same-raw row while
        still treating lower raw sequences as stale.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            store = RunStore(paths)
            record = store.create(_record(root))
            raw = store.append_raw(
                record.run_id,
                provider="codex",
                direction="provider",
                payload={"method": "turn/lifecycle"},
            )
            store.append_normalized(
                record.run_id,
                raw_seq=raw["seq"],
                disposition=EventDisposition.RENDERED,
                kind="turn_started",
                payload={"method": "turn/started"},
                lifecycle_state=LifecycleState.WORKING,
            )
            before_idle = store.run_path(record.run_id).read_text(
                encoding="utf-8"
            )
            store.append_normalized(
                record.run_id,
                raw_seq=raw["seq"],
                disposition=EventDisposition.RENDERED,
                kind="turn_completed",
                payload={"status": "completed"},
                lifecycle_state=LifecycleState.IDLE,
            )
            self.assertEqual(store.get(record.run_id).state, LifecycleState.IDLE)

            # Restore the metadata checkpoint from before IDLE. The later
            # normalized JSONL row stays durable, matching the crash boundary.
            store.run_path(record.run_id).write_text(before_idle, encoding="utf-8")

            restarted = RunStore(paths)
            recovered = restarted.get(record.run_id)
            self.assertEqual(recovered.state, LifecycleState.IDLE)
            self.assertEqual(recovered.last_causal_raw_seq, int(raw["seq"]))
            self.assertEqual(recovered.last_lifecycle_event_seq, 2)

    def test_wiki_243_iter_json_lines_streams_records_without_full_load(
        self,
    ) -> None:
        """WIKI-243: streaming helper yields parsed dicts one at a time
        and swallows the same errors as ``_read_json_lines``."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(_paths(root))
            record = store.create(_record(root))
            path = store.raw_events_path(record.run_id)
            with path.open("w", encoding="utf-8") as handle:
                handle.write('{"seq":1,"payload":{}}\n')
                handle.write("\n")
                handle.write("not-json\n")
                handle.write('"scalar"\n')
                handle.write('{"seq":2,"payload":{}}\n')
            stream = store._iter_json_lines(path)  # noqa: SLF001
            first = next(stream)
            second = next(stream)
            self.assertEqual(first["seq"], 1)
            self.assertEqual(second["seq"], 2)
            self.assertRaises(StopIteration, next, stream)

    def test_wiki_243_reconcile_streams_recovery_logs_without_materializing_raw(
        self,
    ) -> None:
        """WIKI-243: startup recovery must not pull the raw JSONL into RAM.

        Previously ``_reconcile_existing_runs`` called ``_read_json_lines``
        on both raw and normalized paths — a full list-of-parsed-dicts
        allocation per run just to compute two ``max(seq)`` scalars (and
        one legacy boundary). On multi-hundred-run stores this spiked
        backend RSS. The refactor streams both. We patch both helpers to
        record which paths they touch and assert the raw log is never
        materialized via ``_read_json_lines`` during recovery. The
        normalized log is still read once by
        ``rebuild_projections_from_normalized`` — out of scope here.
        """

        rows = 1000
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            store = RunStore(paths)
            record = store.create(_record(root))
            raw_path = store.raw_events_path(record.run_id)
            norm_path = store.normalized_events_path(record.run_id)
            with (
                raw_path.open("w", encoding="utf-8") as raw_handle,
                norm_path.open("w", encoding="utf-8") as norm_handle,
            ):
                for seq in range(1, rows + 1):
                    raw_handle.write(
                        json.dumps(
                            {
                                "seq": seq,
                                "received_at": "2026-01-01T00:00:00Z",
                                "provider": "codex",
                                "direction": "provider",
                                "generation": 1,
                                "payload": {"method": "fixture"},
                            }
                        )
                        + "\n"
                    )
                    norm_handle.write(
                        json.dumps(
                            {
                                "seq": seq,
                                "raw_seq": seq,
                                "normalized_at": "2026-01-01T00:00:00Z",
                                "disposition": EventDisposition.IGNORED.value,
                                "kind": "fixture",
                                "payload": {},
                                "lifecycle_state": None,
                            }
                        )
                        + "\n"
                    )
            del store

            original_read = store_module.RunStore._read_json_lines
            original_iter = store_module.RunStore._iter_json_lines
            read_paths: list[Path] = []
            iter_paths: list[Path] = []

            def spy_read(
                self: store_module.RunStore, path: Path
            ) -> list[dict[str, Any]]:
                read_paths.append(path)
                return original_read(self, path)

            def spy_iter(self: store_module.RunStore, path: Path):
                iter_paths.append(path)
                yield from original_iter(self, path)

            with (
                mock.patch.object(
                    store_module.RunStore, "_read_json_lines", spy_read
                ),
                mock.patch.object(
                    store_module.RunStore, "_iter_json_lines", spy_iter
                ),
            ):
                restarted = RunStore(paths)

            self.assertNotIn(
                raw_path,
                read_paths,
                "reconcile must stream the raw log, not materialize it",
            )
            self.assertIn(
                raw_path,
                iter_paths,
                "reconcile must scan the raw log via the streaming helper",
            )
            self.assertIn(
                norm_path,
                iter_paths,
                "reconcile must scan the normalized log via the streaming helper",
            )
            recovered = restarted.get(record.run_id)
            self.assertEqual(recovered.raw_event_count, rows)
            self.assertEqual(recovered.normalized_event_count, rows)

    def test_wiki_243_reconcile_derives_legacy_last_causal_raw_seq_via_stream(
        self,
    ) -> None:
        """WIKI-243 + WIKI-232 REVIEW12 H1: the legacy
        ``last_causal_raw_seq`` derivation now runs inside the streaming
        pass over normalized events. The raw log must not be materialized
        via ``_read_json_lines`` on the recovery path."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            store = RunStore(paths)
            record = store.create(_record(root))
            started = store.append_raw(
                record.run_id,
                provider="codex",
                direction="provider",
                payload={"method": "turn/started"},
            )
            store.append_normalized(
                record.run_id,
                raw_seq=started["seq"],
                disposition=EventDisposition.RENDERED,
                kind="turn_started",
                payload={"method": "turn/started"},
                lifecycle_state=LifecycleState.WORKING,
            )
            completed = store.append_raw(
                record.run_id,
                provider="codex",
                direction="provider",
                payload={"method": "turn/completed"},
            )
            store.append_normalized(
                record.run_id,
                raw_seq=completed["seq"],
                disposition=EventDisposition.RENDERED,
                kind="turn_completed",
                payload={"status": "completed"},
                lifecycle_state=LifecycleState.IDLE,
            )
            run_path = store.run_path(record.run_id)
            metadata = json.loads(run_path.read_text(encoding="utf-8"))
            metadata.pop("last_causal_raw_seq", None)
            run_path.write_text(json.dumps(metadata), encoding="utf-8")

            raw_path = store.raw_events_path(record.run_id)
            original_read = store_module.RunStore._read_json_lines

            def blocking_read(
                self: store_module.RunStore, path: Path
            ) -> list[dict[str, Any]]:
                if path == raw_path:
                    raise AssertionError(
                        "reconcile must not materialize the raw log"
                    )
                return original_read(self, path)

            with mock.patch.object(
                store_module.RunStore, "_read_json_lines", blocking_read
            ):
                restarted = RunStore(paths)
            recovered = restarted.get(record.run_id)
            self.assertEqual(
                recovered.last_causal_raw_seq, int(completed["seq"])
            )

    def test_clean_reopen_does_not_rewrite_rebuilt_run_projection(
        self,
    ) -> None:
        """REVIEW18 M1: a clean projection rebuild is byte-for-byte idle."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            store = RunStore(paths)
            record = store.create(_record(root))
            raw = store.append_raw(
                record.run_id,
                provider="codex",
                direction="provider",
                payload={"method": "fixture/clean-reopen"},
            )
            store.append_normalized(
                record.run_id,
                raw_seq=int(raw["seq"]),
                disposition=EventDisposition.IGNORED,
                kind="fixture_clean_reopen",
                payload={},
            )
            run_path = store.run_path(record.run_id)
            before_bytes = run_path.read_bytes()
            before_mtime = run_path.stat().st_mtime_ns
            before_updated_at = store.get(record.run_id).updated_at

            time.sleep(0.01)
            restarted = RunStore(paths)

            self.assertEqual(run_path.read_bytes(), before_bytes)
            self.assertEqual(run_path.stat().st_mtime_ns, before_mtime)
            self.assertEqual(
                restarted.get(record.run_id).updated_at,
                before_updated_at,
            )

    def test_rebuild_orders_current_composer_echoes_by_raw_sequence(
        self,
    ) -> None:
        """REVIEW14 M1: middle-gap replay keeps identical echoes correlated."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(_paths(root))
            record = store.create(_record(root))
            inherited = {
                "pending_id": "inherited",
                "text": "older message",
                "sent_at": "2026-01-01T00:00:00Z",
                "echoed_at": "2026-01-01T00:00:01Z",
                "seq": 9,
            }
            record.composer_messages = [inherited]
            store._write_record(record)  # noqa: SLF001 - replacement history fixture

            raw_a = store.append_raw(
                record.run_id,
                provider="claude",
                direction="provider",
                payload={"type": "user", "label": "A"},
            )
            raw_b = store.append_raw(
                record.run_id,
                provider="claude",
                direction="provider",
                payload={"type": "user", "label": "B"},
            )
            # B normalizes first. A is the recovered middle-gap row. Both
            # carry identical text, so FIFO order is required for exact source
            # correlation.
            store.append_normalized(
                record.run_id,
                raw_seq=raw_b["seq"],
                disposition=EventDisposition.RENDERED,
                kind="claude_user",
                payload={
                    "pending_id": "pending-b",
                    "composer_text": "identical user message",
                    "composer_sent_at": "2026-01-01T00:00:03Z",
                },
            )
            store.append_normalized(
                record.run_id,
                raw_seq=raw_a["seq"],
                disposition=EventDisposition.RENDERED,
                kind="claude_user",
                payload={
                    "pending_id": "pending-a",
                    "composer_text": "identical user message",
                    "composer_sent_at": "2026-01-01T00:00:02Z",
                    "source": "fleet-monitor",
                },
            )
            self.assertEqual(
                [
                    message["pending_id"]
                    for message in store.get(record.run_id).composer_messages
                ],
                ["inherited", "pending-b"],
            )

            rebuilt = store.rebuild_projections_from_normalized(record.run_id)
            self.assertEqual(
                [message["pending_id"] for message in rebuilt.composer_messages],
                ["inherited", "pending-a", "pending-b"],
            )
            self.assertEqual(rebuilt.composer_messages[0], inherited)
            self.assertEqual(
                rebuilt.composer_messages[1].get("source"), "fleet-monitor"
            )
            self.assertNotIn("source", rebuilt.composer_messages[2])
            self.assertEqual(
                rebuilt.composer_messages[1]["text"],
                rebuilt.composer_messages[2]["text"],
            )

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
            self.assertFalse(current["control_attached"])

    def test_control_attachment_is_projected_and_resets_on_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            store = RunStore(paths)
            record = store.create(_record(root))

            store.set_control_attached(record.run_id, True)
            registry = json.loads(paths.registry_path.read_text(encoding="utf-8"))
            self.assertTrue(registry["WIKI-42"]["current"]["control_attached"])

            store.update_adapter_status(
                record.run_id,
                AdapterStatus(LifecycleState.WORKING, "session-1", 4242),
            )
            registry = json.loads(paths.registry_path.read_text(encoding="utf-8"))
            self.assertTrue(registry["WIKI-42"]["current"]["control_attached"])

            restarted = RunStore(paths)
            registry = json.loads(paths.registry_path.read_text(encoding="utf-8"))
            self.assertFalse(registry["WIKI-42"]["current"]["control_attached"])
            self.assertEqual(restarted.get(record.run_id).provider_pid, 4242)

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
            # The replaced legacy provider identity rides on the returned
            # record so the caller (main.py's spawn route) can clear the
            # right ticket-only notice regardless of the destination kind.
            self.assertEqual(record.replaced_legacy_provider, "codex")
            persisted = store.get(record.run_id)
            self.assertEqual(persisted.replaced_legacy_provider, "codex")

    def test_create_rejects_oversized_status_before_registry_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            original_registry = {
                "WIKI-42": {
                    "history": [],
                    "current": {"window": "@9999", "kind": "cdx"},
                }
            }
            paths.registry_path.parent.mkdir(parents=True)
            paths.registry_path.write_text(json.dumps(original_registry), encoding="utf-8")
            paths.status_dir.mkdir(parents=True)
            status_path = paths.status_dir / "WIKI-42.json"
            oversized = b"x" * (store_module.MAX_START_STATUS_BYTES + 1)
            status_path.write_bytes(oversized)
            store = RunStore(paths)

            with self.assertRaisesRegex(StoreError, "status file exceeds"):
                store.create(_record(root), migrate_legacy=True)

            self.assertEqual(json.loads(paths.registry_path.read_text()), original_registry)
            self.assertEqual(status_path.read_bytes(), oversized)

    def test_create_rejects_symlink_status_before_registry_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            original_registry = {
                "WIKI-42": {
                    "history": [],
                    "current": {"window": "@9999", "kind": "cdx"},
                }
            }
            paths.registry_path.parent.mkdir(parents=True)
            paths.registry_path.write_text(json.dumps(original_registry), encoding="utf-8")
            paths.status_dir.mkdir(parents=True)
            outside = root / "outside-status.json"
            outside.write_text('{"state":"working"}\n', encoding="utf-8")
            status_path = paths.status_dir / "WIKI-42.json"
            status_path.symlink_to(outside)
            store = RunStore(paths)

            with self.assertRaisesRegex(StoreError, "symlink status file"):
                store.create(_record(root), migrate_legacy=True)

            self.assertEqual(json.loads(paths.registry_path.read_text()), original_registry)
            self.assertTrue(status_path.is_symlink())
            self.assertEqual(outside.read_text(encoding="utf-8"), '{"state":"working"}\n')

    def test_create_rejects_malformed_legacy_orchestrator_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            paths.registry_path.parent.mkdir(parents=True)
            paths.registry_path.write_text(
                json.dumps({"_orchestrators": []}),
                encoding="utf-8",
            )

            store = RunStore(paths)
            with self.assertRaisesRegex(
                StoreError, "legacy orchestrator registry must be an object"
            ):
                store.create(_record(root))

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
            # Legacy `_orchestrators` migration reports the replaced provider
            # so cross-provider (cdx-to-cc, cc-to-cdx) orchestrator swaps can
            # drive the right notice cleanup.
            self.assertEqual(created.replaced_legacy_provider, "claude")
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
            self.assertIsNone(restarted.current_run_id("wiki-dev"))
            self.assertEqual(
                registry,
                {
                    "_orchestrators": {
                        "wiki-dev": {
                            "window": "@9999",
                            "cwd": str(root / "legacy-worktree"),
                            "model": "opus",
                        }
                    }
                },
            )
            self.assertFalse(paths.runs_dir.joinpath(record.run_id).exists())

    def test_create_failure_restores_status_registry_and_run_files(self) -> None:
        for failure_point in ("_create_run_files", "_write_registry"):
            with self.subTest(failure_point=failure_point), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                paths = _paths(root)
                paths.registry_path.parent.mkdir(parents=True, exist_ok=True)
                original_registry = {
                    "_orchestrators": {
                        "wiki-dev": {
                            "window": "@9999",
                            "cwd": str(root / "legacy-worktree"),
                            "model": "opus",
                        }
                    }
                }
                paths.registry_path.write_text(
                    json.dumps(original_registry), encoding="utf-8"
                )
                store = RunStore(paths)
                record = _record(root, "wiki-dev")
                record.provider = ProviderKind.CLAUDE
                record.role = "orchestrator"
                record.model = "opus"
                record.orchestrator_id = None
                status = store.status_path(record.agent_id)
                status.parent.mkdir(parents=True, exist_ok=True)
                status_content = '{"state":"working","step":"legacy"}\n'
                status.write_text(status_content, encoding="utf-8")

                if failure_point == "_create_run_files":
                    def fail_create(value: RunRecord) -> None:
                        value_dir = store.run_dir(value.run_id)
                        value_dir.mkdir(mode=0o700, parents=False)
                        raise OSError("simulated run-file failure")

                    failure = mock.patch.object(
                        store, failure_point, side_effect=fail_create
                    )
                else:
                    failure = mock.patch.object(
                        store,
                        failure_point,
                        side_effect=OSError("simulated registry failure"),
                    )
                with failure, self.assertRaisesRegex(OSError, "simulated"):
                    store.create(record, migrate_legacy=True)

                self.assertEqual(
                    json.loads(paths.registry_path.read_text(encoding="utf-8")),
                    original_registry,
                )
                self.assertEqual(status.read_text(encoding="utf-8"), status_content)
                self.assertFalse(store.run_dir(record.run_id).exists())
                self.assertEqual(store._start_registry_snapshots, {})  # noqa: SLF001

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
            store.status_path(old.agent_id).parent.mkdir(parents=True, exist_ok=True)
            store.status_path(old.agent_id).write_text(
                json.dumps(
                    {
                        "state": "merge-ready",
                        "pr": "old-replace-pr",
                        "step": "old provider",
                        "blocker": None,
                    }
                ),
                encoding="utf-8",
            )
            replacement = _record(root)
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
            self.assertEqual(repaired_replacement.state, LifecycleState.STARTING)
            self.assertIsNone(repaired_replacement.provider_session_id)
            self.assertIsNone(repaired_replacement.provider_pid)
            self.assertEqual(repaired_replacement.provider_generation, 0)
            self.assertFalse(store.status_path(old.agent_id).exists())
            replacement_decision = restart_recovery_decision(
                repaired_replacement,
                is_current=True,
                provider_pid_alive=False,
                provider_control_attached=False,
            )
            self.assertEqual(replacement_decision.action, RecoveryAction.BLOCK)
            old_decision = restart_recovery_decision(
                repaired_old,
                is_current=False,
                provider_pid_alive=False,
                provider_control_attached=False,
            )
            self.assertEqual(old_decision.action, RecoveryAction.SKIP)
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

    def test_quiesce_intent_can_capture_prior_working_state_from_blocked_run(
        self,
    ) -> None:
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
            status_path = paths.status_dir / "WIKI-42.json"
            status_path.parent.mkdir(parents=True, exist_ok=True)
            status_path.write_text('{"state":"merge-ready"}', encoding="utf-8")
            replacement = _record(root)
            replacement.replaces_run_id = old.run_id
            archived, current = store.replace(old.run_id, replacement)
            self.assertEqual(archived.replaced_by_run_id, current.run_id)
            self.assertEqual(archived.outcome, "handoff")
            self.assertEqual(store.current_run_id("WIKI-42"), current.run_id)
            self.assertFalse(status_path.exists())
            registry = json.loads(paths.registry_path.read_text(encoding="utf-8"))
            self.assertEqual(registry["WIKI-42"]["history"][0]["outcome"], "handoff")
            with self.assertRaisesRegex(StoreConflict, "no longer current"):
                store.replace(old.run_id, _record(root))

    def test_abort_replace_stops_recorded_provider_and_restores_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            store = RunStore(paths)
            old = store.create(_record(root))
            replacement = _record(root)
            store.replace(old.run_id, replacement)
            replacement.provider_pid = 4242
            replacement.provider_pid_started_at = 1.0
            replacement.provider_executable = "/bin/provider"
            replacement.provider_process_group_id = 4242
            replacement.provider_process_group_members = [
                {"pid": 4242, "created_at": 1.0, "executable": "/bin/provider"}
            ]
            store._write_record(replacement)  # noqa: SLF001 - crash fixture

            with (
                mock.patch.object(store_module.os, "kill"),
                mock.patch.object(
                    store_module,
                    "terminate_verified_provider_group",
                    return_value=True,
                ) as terminate,
            ):
                restored = store.abort_replace(
                    old.run_id,
                    replacement.run_id,
                    reason="replacement failed",
                    adapter_status=AdapterStatus(
                        LifecycleState.BLOCKED,
                        None,
                        None,
                        generation=old.provider_generation,
                    ),
                )

            terminate.assert_called_once()
            self.assertEqual(restored.run_id, old.run_id)
            self.assertEqual(store.current_run_id(old.agent_id), old.run_id)
            self.assertFalse(store.run_dir(replacement.run_id).exists())
            self.assertEqual(
                store.command_state_for(old.agent_id)[old.agent_id]["current"]["run_id"],
                old.run_id,
            )
            store.transition(old.run_id, LifecycleState.COMPLETED)

    def test_restart_discards_dead_uncommitted_pid_without_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            store = RunStore(paths)
            record = store.create(_record(root), transactional_start=True)
            record.provider_pid = 999_999_999
            record.provider_pid_started_at = None
            record.provider_executable = None
            record.provider_process_group_id = None
            store._write_record(record)  # noqa: SLF001 - crash fixture

            restarted = RunStore(paths)

            self.assertEqual(restarted.list_runs(), [])

    def test_restart_retains_live_unverifiable_uncommitted_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            store = RunStore(paths)
            record = store.create(_record(root), transactional_start=True)
            record.provider_pid = os.getpid()
            record.provider_pid_started_at = None
            record.provider_executable = None
            record.provider_process_group_id = None
            store._write_record(record)  # noqa: SLF001 - crash fixture

            restarted = RunStore(paths)

            self.assertEqual(restarted.get(record.run_id).run_id, record.run_id)

    def test_restart_kills_late_different_executable_group_member(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            run_id = str(uuid4())
            agent_id = "WIKI-GROUP-IDENTITY"
            child_code = (
                "import os, subprocess, sys\n"
                "print(f'leader:{os.getpid()}', flush=True)\n"
                "if sys.stdin.readline().strip() != 'spawn':\n"
                "    raise SystemExit('spawn command missing')\n"
                "child = subprocess.Popen(['/bin/sleep', '30'], env=os.environ.copy())\n"
                "print(f'child:{child.pid}', flush=True)\n"
                "import time; time.sleep(60)\n"
            )
            daemon_code = (
                "import json, os, subprocess, sys, time\n"
                "from pathlib import Path\n"
                "from backend.app.agent_runtime.store import RunStore, RuntimePaths\n"
                "from backend.app.agent_runtime.types import ProviderKind, RunRecord\n"
                "root = Path(sys.argv[1])\n"
                "paths = RuntimePaths(\n"
                "    runtime_dir=root / 'runtime',\n"
                "    socket_path=root / 'runtime' / 'supervisor.sock',\n"
                "    registry_path=root / 'registry' / 'agents.json',\n"
                "    archive_dir=root / 'archive',\n"
                "    status_dir=root / 'status',\n"
                ")\n"
                "store = RunStore(paths)\n"
                "record = RunRecord.new(\n"
                "    agent_id=sys.argv[2], provider=ProviderKind.CODEX,\n"
                "    role='implement', model='fixture-codex', worktree=str(root),\n"
                "    prompt='group identity crash fixture', run_id=sys.argv[3],\n"
                ")\n"
                "store.create(record, transactional_start=True)\n"
                "env = os.environ.copy()\n"
                "env['WIKI_RUN_ID'] = record.run_id\n"
                "env['WIKI_AGENT_ID'] = record.agent_id\n"
                "provider = subprocess.Popen(\n"
                "    [sys.executable, '-c', sys.argv[4]],\n"
                "    env=env, start_new_session=True, stdin=subprocess.PIPE,\n"
                "    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,\n"
                ")\n"
                "leader = provider.stdout.readline().strip()\n"
                "assert leader == f'leader:{provider.pid}', leader\n"
                "store.record_provider_process_created(record.run_id, provider.pid)\n"
                "provider.stdin.write('spawn\\n')\n"
                "provider.stdin.flush()\n"
                "child = provider.stdout.readline().strip()\n"
                "print(json.dumps({'run_id': record.run_id, 'leader': provider.pid, 'child': int(child.split(':', 1)[1]), 'pgid': os.getpgid(provider.pid)}), flush=True)\n"
                "time.sleep(60)\n"
            )
            env = os.environ.copy()
            repo_root = str(Path(__file__).resolve().parents[2])
            env["PYTHONPATH"] = repo_root
            daemon = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    daemon_code,
                    str(root),
                    agent_id,
                    run_id,
                    child_code,
                ],
                cwd=repo_root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            provider_info: dict[str, int | str] | None = None
            try:
                line = daemon.stdout.readline() if daemon.stdout is not None else ""
                if not line:
                    stderr = daemon.stderr.read() if daemon.stderr is not None else ""
                    self.fail(f"daemon exited before barrier: {stderr}")
                provider_info = json.loads(line)
                leader_pid = int(provider_info["leader"])
                child_pid = int(provider_info["child"])
                process_group_id = int(provider_info["pgid"])
                self.assertIsNotNone(provider_process_status_sync(leader_pid))
                self.assertIsNotNone(provider_process_status_sync(child_pid))

                os.kill(daemon.pid, signal.SIGKILL)
                daemon.wait(timeout=5)

                restarted = RunStore(paths)
                self.assertEqual(restarted.list_runs(), [])
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    members = provider_process_group_members_sync(process_group_id)
                    if not members:
                        break
                    time.sleep(0.05)
                self.assertEqual(provider_process_group_members_sync(process_group_id), [])
            finally:
                if daemon.poll() is None:
                    daemon.kill()
                    daemon.wait(timeout=5)
                if provider_info is not None:
                    process_group_id = int(provider_info["pgid"])
                    try:
                        os.killpg(process_group_id, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass

    def test_subprocess_kill_after_status_unlink_restores_exact_preimage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            agent_id = "WIKI-STATUS-PREIMAGE"
            run_id = str(uuid4())
            status_content = b'{"state":"working","step":"before start"}\n'
            registry_content = {
                agent_id: {
                    "history": [{"run_id": "old-run", "state": "completed"}],
                    "current": None,
                }
            }
            paths.status_dir.mkdir(parents=True)
            paths.status_dir.joinpath(f"{agent_id}.json").write_bytes(status_content)
            paths.registry_path.parent.mkdir(parents=True)
            paths.registry_path.write_text(
                json.dumps(registry_content), encoding="utf-8"
            )
            daemon_code = (
                "import os, sys, time\n"
                "from pathlib import Path\n"
                "from backend.app.agent_runtime.store import RunStore, RuntimePaths\n"
                "from backend.app.agent_runtime.types import ProviderKind, RunRecord\n"
                "root = Path(sys.argv[1])\n"
                "paths = RuntimePaths(\n"
                "    runtime_dir=root / 'runtime',\n"
                "    socket_path=root / 'runtime' / 'supervisor.sock',\n"
                "    registry_path=root / 'registry' / 'agents.json',\n"
                "    archive_dir=root / 'archive',\n"
                "    status_dir=root / 'status',\n"
                ")\n"
                "store = RunStore(paths)\n"
                "print('ready', flush=True)\n"
                "if sys.stdin.readline().strip() != 'create':\n"
                "    raise SystemExit('create command missing')\n"
                "record = RunRecord.new(\n"
                "    agent_id=sys.argv[2], provider=ProviderKind.CODEX,\n"
                "    role='implement', model='fixture-codex', worktree=str(root),\n"
                "    prompt='status preimage crash fixture', run_id=sys.argv[3],\n"
                ")\n"
                "store.create(record, transactional_start=True)\n"
                "time.sleep(60)\n"
            )
            env = os.environ.copy()
            repo_root = str(Path(__file__).resolve().parents[2])
            env["PYTHONPATH"] = repo_root
            daemon = subprocess.Popen(
                [sys.executable, "-c", daemon_code, str(root), agent_id, run_id],
                cwd=repo_root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
                text=True,
            )
            status_path = paths.status_dir / f"{agent_id}.json"
            run_path = paths.run_dir(run_id) / "run.json"
            try:
                line = daemon.stdout.readline() if daemon.stdout is not None else ""
                self.assertEqual(line.strip(), "ready")
                daemon.stdin.write("create\n")
                daemon.stdin.flush()
                deadline = time.monotonic() + 5
                while status_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.0001)
                self.assertFalse(status_path.exists())
                self.assertTrue(run_path.exists())
                marker = json.loads(run_path.read_text(encoding="utf-8"))
                self.assertIsNotNone(marker.get("start_transaction"))
                os.kill(daemon.pid, signal.SIGKILL)
                daemon.wait(timeout=5)

                restarted = RunStore(paths)
                self.assertEqual(restarted.list_runs(), [])
                self.assertEqual(status_path.read_bytes(), status_content)
                self.assertEqual(
                    json.loads(paths.registry_path.read_text(encoding="utf-8")),
                    registry_content,
                )
            finally:
                if daemon.poll() is None:
                    daemon.kill()
                    daemon.wait(timeout=5)

    def test_restart_discovers_unrecorded_provider_by_run_identity(self) -> None:
        for provider in (ProviderKind.CODEX, ProviderKind.CLAUDE):
            with self.subTest(provider=provider.value), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                paths = _paths(root)
                store = RunStore(paths)
                record = _record(root)
                record.provider = provider
                record.start_transaction = {"version": 1}
                store.create(record)
                store._write_record(record)  # noqa: SLF001 - crash fixture
                identity = ProviderProcessStatus(
                    pid=4242,
                    parent_pid=1,
                    created_at=1.0,
                    process_group_id=4242,
                    executable="/bin/provider",
                )

                with (
                    mock.patch.object(
                        store_module,
                        "provider_processes_for_run_sync",
                        return_value=[identity],
                    ),
                    mock.patch.object(
                        store_module,
                        "provider_process_group_members_sync",
                        return_value=[
                            {
                                "pid": 4242,
                                "created_at": 1.0,
                                "executable": "/bin/provider",
                            }
                        ],
                    ),
                    mock.patch.object(store_module.os, "kill"),
                    mock.patch.object(
                        store_module,
                        "terminate_verified_provider_group",
                        return_value=True,
                    ) as terminate,
                ):
                    restarted = RunStore(paths)

                self.assertEqual(restarted.list_runs(), [])
                terminate.assert_called_once()

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
                json.dumps(
                    {
                        "state": "merge-ready",
                        "step": "done",
                        "pr": "https://example/pr/42",
                    }
                ),
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
            self.assertTrue(
                (session_dir / "artifacts" / linked_artifact.name).is_symlink()
            )
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

    def test_archive_marker_wins_when_cleanup_fails_and_retry_removes_remnant(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            store = RunStore(paths)
            record = store.create(_record(root))
            for _ in range(60):
                store.append_raw(
                    record.run_id,
                    provider="codex",
                    direction="provider",
                    payload={"method": "item/novel", "params": {}},
                )
            store.transition(record.run_id, LifecycleState.COMPLETED)

            with (
                mock.patch.object(
                    store_module.shutil,
                    "rmtree",
                    side_effect=OSError("cleanup interrupted"),
                ),
                self.assertRaises(OSError),
            ):
                store.archive_current(record.run_id, outcome="merged")

            calls: list[str] = []
            telemetry = UnknownKindTelemetry(
                paths,
                threshold=100,
                todo_runner=calls.append,
                clock=lambda: datetime.now(timezone.utc).timestamp(),
            )
            first = telemetry.run_once()
            second = telemetry.run_once()

            self.assertEqual(first["unknown_counts"], {"item/novel": 60})
            self.assertEqual(second["unknown_counts"], {"item/novel": 60})
            self.assertEqual(calls, [])
            self.assertTrue(store.run_dir(record.run_id).exists())

            store.archive_current(record.run_id, outcome="merged")

            self.assertFalse(store.run_dir(record.run_id).exists())

    def test_archive_marker_replay_resumes_cleanup_after_each_late_step(self) -> None:
        for failure in ("rmtree", "implicit", "projection"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                paths = _paths(root)
                store = RunStore(paths)
                record = store.create(_record(root))
                store.transition(record.run_id, LifecycleState.COMPLETED)

                if failure == "rmtree":
                    original_rmtree = store_module.shutil.rmtree

                    def fail_rmtree(path: Path, *args: Any, **kwargs: Any) -> None:
                        if path == store.run_dir(record.run_id):
                            raise OSError("crash after archive marker")
                        original_rmtree(path, *args, **kwargs)

                    patch = mock.patch.object(store_module.shutil, "rmtree", fail_rmtree)
                elif failure == "implicit":
                    patch = mock.patch.object(
                        store.command_log,
                        "forget_implicit_for_run",
                        side_effect=OSError("crash after run removal"),
                    )
                else:
                    patch = mock.patch.object(
                        store.command_log,
                        "replace_projection",
                        side_effect=OSError("crash after registry cleanup"),
                    )

                with self.assertRaises(OSError), patch:
                    store.archive_current(record.run_id, outcome="merged")

                archived = store.finalize_archived_run(record.run_id)

                self.assertIsNotNone(archived)
                self.assertFalse(store.run_dir(record.run_id).exists())
                self.assertIsNone(store.current_run_id(record.agent_id))

    def test_archive_recovery_repairs_implicit_index_before_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            store = RunStore(paths)
            record = _record(root)
            record.start_request_id = "implicit-archive-retry"
            record.implicit_start_request = True
            store.create(record)
            store.transition(record.run_id, LifecycleState.COMPLETED)

            with (
                mock.patch.object(
                    store.command_log,
                    "archive_start_request",
                    side_effect=OSError("crash before archive index"),
                ),
                self.assertRaises(OSError),
            ):
                store.archive_current(record.run_id, outcome="merged")

            restarted = RunStore(paths)
            archived = restarted.finalize_archived_run(record.run_id)

            self.assertIsNotNone(archived)
            indexed = restarted.command_log.archived_start_request(
                record.start_request_id
            )
            self.assertIsNotNone(indexed)
            assert indexed is not None
            self.assertEqual(indexed["run_id"], record.run_id)

            fresh = _record(root)
            fresh.start_request_id = record.start_request_id
            fresh.implicit_start_request = True
            restarted.create(fresh)
            self.assertEqual(restarted.current_run_id(record.agent_id), fresh.run_id)
            with self.assertRaisesRegex(StoreConflict, "older archive marker"):
                restarted.finalize_archived_run(record.run_id)
            self.assertEqual(restarted.current_run_id(record.agent_id), fresh.run_id)

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
