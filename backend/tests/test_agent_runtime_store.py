from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.app.agent_runtime.fake import WireFixture
from backend.app.agent_runtime.provider import AdapterStatus
from backend.app.agent_runtime.store import (
    RunStore,
    RuntimePaths,
    StoreConflict,
    StoreError,
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

    def test_env_paths_keep_every_live_surface_redirectable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                "WIKI_AGENT_RUNTIME_DIR": str(root / "runtime"),
                "WIKI_SUPERVISOR_SOCKET_PATH": str(root / "control.sock"),
                "WIKI_AGENT_REGISTRY_PATH": str(root / "registry.json"),
            }
            paths = RuntimePaths.from_env(env)
            self.assertEqual(paths.runtime_dir, (root / "runtime").absolute())
            self.assertEqual(paths.socket_path, (root / "control.sock").absolute())
            self.assertEqual(paths.registry_path, (root / "registry.json").absolute())


if __name__ == "__main__":
    unittest.main()
