from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

from fastapi import BackgroundTasks, HTTPException

from backend.app import main
from backend.app.agent_runtime.client import (
    SupervisorClient,
    SupervisorRemoteError,
    SupervisorUnavailable,
)
from backend.app.agent_runtime.fake import FixtureAdapterFactory
from backend.app.agent_runtime.protocol import UnixSupervisorServer
from backend.app.agent_runtime.store import RunStore, RuntimePaths
from backend.app.agent_runtime.supervisor import Supervisor


FIXTURES = Path(__file__).parent / "fixtures" / "agent_runtime"
RUN_ID = "00000000-0000-4000-8000-000000000042"
REPLACEMENT_RUN_ID = "00000000-0000-4000-8000-000000000043"


class FakeSupervisorClient:
    def __init__(self, registry_path: Path, raw_path: Path):
        self.registry_path = registry_path
        self.raw_path = raw_path
        self.calls: list[tuple[str, dict]] = []
        self.messages: list[dict[str, str]] = []
        self.normalized_events: list[dict[str, Any]] = []
        self.raw_events: list[dict[str, Any]] = []
        self.pending_requests: list[dict[str, Any]] = []
        self.fail_unavailable = False
        self.rotation_error: SupervisorRemoteError | None = None
        self.event = {
            "type": "session",
            "ticket": "WIKI-42",
            "surface": "session",
        }

    def ensure_running(self) -> dict:
        if self.fail_unavailable:
            raise SupervisorUnavailable("fixture unavailable")
        return {"status": "ok", "pid": 4242}

    def ping(self) -> dict:
        return self.ensure_running()

    def _registry(self) -> dict:
        try:
            return json.loads(self.registry_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def _write_current(self, run_id: str, params: dict) -> dict:
        registry = self._registry()
        agent_id = params["agent_id"]
        previous = registry.get(agent_id) or {}
        history = list(previous.get("history") or [])
        prior_current = previous.get("current")
        if isinstance(prior_current, dict):
            history.append({**prior_current, "outcome": "handoff"})
        current = {
            "ticket": agent_id,
            "run_id": run_id,
            "provider": params.get("provider", "codex"),
            "kind": "cdx" if params.get("provider", "codex") == "codex" else "cc",
            "role": params.get("role", "implement"),
            "model": params.get("model", "gpt-5.4"),
            "effort": params.get("effort"),
            "worktree": params.get("worktree"),
            "cwd": params.get("worktree"),
            "orch": params.get("orchestrator_id"),
            "state": "working",
            "provider_session_id": f"session-{run_id[-2:]}",
            "provider_pid": 4242,
            "transcript": None,
            "log": str(self.raw_path),
            "window": None,
            "spawned_at": "2026-07-09T12:00:00+00:00",
        }
        registry[agent_id] = {"history": history, "current": current}
        self.registry_path.write_text(json.dumps(registry), encoding="utf-8")
        return current

    def request(self, method: str, params: dict | None = None):
        if self.fail_unavailable:
            raise SupervisorUnavailable("fixture unavailable")
        values = dict(params or {})
        self.calls.append((method, values))
        registry = self._registry()
        if method == "fleet/rotate_codex":
            if self.rotation_error is not None:
                raise self.rotation_error
            revived = sorted(
                agent_id
                for agent_id, entry in registry.items()
                if not agent_id.startswith("_")
                and isinstance(entry, dict)
                and isinstance(entry.get("current"), dict)
                and entry["current"].get("run_id")
                and entry["current"].get("kind") == "cdx"
                and entry["current"].get("state")
                not in {"dead", "completed"}
            )
            return {
                "from": "alpha",
                "to": values.get("account") or "beta",
                "revived": revived,
                "failed": [],
                "failed_reasons": {},
            }
        if method == "run/list":
            rows = []
            for agent_id, entry in registry.items():
                if agent_id.startswith("_") or not isinstance(entry, dict):
                    continue
                row = dict(entry.get("current") or {})
                if row.get("run_id"):
                    row.update({"control_attached": True, "provider_alive": True})
                    rows.append(row)
            return {"runs": rows}
        if method == "run/start":
            row = self._write_current(RUN_ID, values)
            return {**row, "agent_id": values["agent_id"]}
        if method == "run/replace":
            agent_id = next(
                key
                for key, entry in registry.items()
                if isinstance(entry, dict)
                and (entry.get("current") or {}).get("run_id") == values["run_id"]
            )
            old = registry[agent_id]["current"]
            row = self._write_current(
                REPLACEMENT_RUN_ID,
                {
                    "agent_id": agent_id,
                    "provider": old["provider"],
                    "role": old["role"],
                    "model": old["model"],
                    "effort": old.get("effort"),
                    "worktree": old["worktree"],
                    "orchestrator_id": old.get("orch"),
                },
            )
            return {**row, "agent_id": agent_id}
        if method in {"run/interrupt", "run/resume", "run/stop", "run/archive"}:
            agent_id = values["agent_id"]
            current = registry[agent_id]["current"]
            current["state"] = {
                "run/interrupt": "interrupted",
                "run/resume": "working",
                "run/stop": "dead",
                "run/archive": "completed",
            }[method]
            self.registry_path.write_text(json.dumps(registry), encoding="utf-8")
            return {**current, "agent_id": agent_id}
        if method == "run/respond":
            agent_id = values["agent_id"]
            current = registry[agent_id]["current"]
            current["state"] = "working"
            self.pending_requests = [
                request
                for request in self.pending_requests
                if request.get("request_id") != values["request_id"]
            ]
            self.registry_path.write_text(json.dumps(registry), encoding="utf-8")
            return {**current, "agent_id": agent_id}
        if method == "run/send_now":
            return {"status": "sent"}
        if method == "run/send_on_idle":
            message = {
                "text": values["text"],
                "queued_at": "2026-07-09T12:00:00+00:00",
            }
            self.messages.append(message)
            return {
                "status": "queued",
                "position": len(self.messages),
                "messages": list(self.messages),
            }
        if method == "run/queue":
            return {"messages": list(self.messages)}
        if method == "run/queue/delete":
            self.messages.pop(values["index"])
            return {"messages": list(self.messages)}
        if method == "events/read":
            agent_id = values["agent_id"]
            current = registry[agent_id]["current"]
            limit = values.get("limit", 200)
            after_seq = values.get("after_seq", 0)
            normalized = [
                event
                for event in self.normalized_events
                if event.get("seq", 0) > after_seq
            ][-limit:]
            raw = [
                event for event in self.raw_events if event.get("seq", 0) > after_seq
            ][-limit:]
            return {
                "run_id": current["run_id"],
                "provider": current["provider"],
                "state": current["state"],
                "raw_count": len(self.raw_events),
                "normalized_count": len(self.normalized_events),
                "dispositions": {
                    "rendered": len(
                        [
                            event
                            for event in self.normalized_events
                            if event.get("disposition") == "rendered"
                        ]
                    ),
                    "summarized": 0,
                    "ignored": 0,
                    "unknown": 0,
                },
                "pending_requests": list(self.pending_requests),
                "events": normalized,
                "raw": raw if values.get("include_raw") else None,
            }
        raise AssertionError(f"unexpected supervisor method: {method}")

    async def subscribe_events(self):
        yield dict(self.event)
        await asyncio.Event().wait()


class HeadlessMainRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.registry = self.root / "agent-registry.json"
        self.status_dir = self.root / "status"
        self.archive_dir = self.root / "archive"
        self.tmp_dir = self.root / "tmp"
        self.queue_path = self.root / "legacy-queue.json"
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.status_dir.mkdir()
        self.raw = self.root / "raw.jsonl"
        self.raw.write_text(
            '{"seq":1,"payload":{"method":"turn/started"}}\n', encoding="utf-8"
        )
        self.client = FakeSupervisorClient(self.registry, self.raw)
        self.patchers = [
            mock.patch.object(main, "AGENT_REGISTRY_PATH", self.registry),
            mock.patch.object(main, "AGENT_STATUS_DIR", self.status_dir),
            mock.patch.object(main, "AGENT_ARCHIVE_DIR", self.archive_dir),
            mock.patch.object(main, "AGENT_TMP_DIR", self.tmp_dir),
            mock.patch.object(main, "MSG_QUEUE_PATH", self.queue_path),
            mock.patch.object(main, "SUPERVISOR_CLIENT", self.client),
            mock.patch.object(main.transcripts, "find_session", return_value=None),
        ]
        for patcher in self.patchers:
            patcher.start()

    async def asyncTearDown(self) -> None:
        main._event_subscribers.clear()  # noqa: SLF001 - isolate broker state
        main._session_paths.clear()  # noqa: SLF001 - isolate transcript cache
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    def _seed_headless(self) -> None:
        self.client._write_current(  # noqa: SLF001 - fixture setup
            RUN_ID,
            {
                "agent_id": "WIKI-42",
                "provider": "codex",
                "role": "implement",
                "model": "gpt-5.4",
                "effort": "high",
                "worktree": str(self.worktree),
                "orchestrator_id": None,
            },
        )

    async def test_composer_and_queue_shapes_route_only_to_supervisor(self) -> None:
        self._seed_headless()
        with mock.patch.object(
            main,
            "resolve_window",
            side_effect=AssertionError("headless route touched tmux"),
        ):
            sent = main.agent_message(
                "WIKI-42",
                main.MessageIn(text="steer now", mode="now"),
                BackgroundTasks(),
            )
            queued = main.agent_message(
                "WIKI-42",
                main.MessageIn(text="after idle", mode="on-idle"),
                BackgroundTasks(),
            )
            listed = main.agent_queue("WIKI-42")
            deleted = main.agent_queue_delete("WIKI-42", 0)

        self.assertEqual(sent, {"status": "sent"})
        self.assertEqual(
            queued,
            {
                "status": "queued",
                "position": 1,
                "messages": [
                    {
                        "text": "after idle",
                        "queued_at": "2026-07-09T12:00:00+00:00",
                    }
                ],
            },
        )
        self.assertEqual(listed, {"messages": queued["messages"]})
        self.assertEqual(deleted, {"messages": []})
        self.assertEqual(
            [method for method, _ in self.client.calls],
            [
                "run/send_now",
                "run/send_on_idle",
                "run/queue",
                "run/queue/delete",
            ],
        )

    async def test_headless_control_failure_never_falls_back_to_tmux(self) -> None:
        self._seed_headless()
        self.client.fail_unavailable = True
        with (
            mock.patch.object(
                main,
                "resolve_window",
                side_effect=AssertionError("headless route touched tmux"),
            ),
            self.assertRaises(HTTPException) as failed,
        ):
            main.agent_message(
                "WIKI-42",
                main.MessageIn(text="steer", mode="now"),
                BackgroundTasks(),
            )
        self.assertEqual(failed.exception.status_code, 503)

    async def test_lifecycle_controls_are_closed_supervisor_routes(self) -> None:
        self._seed_headless()
        interrupted = main.interrupt_agent("WIKI-42")
        resumed = main.resume_agent("WIKI-42")
        stopped = main.stop_agent("WIKI-42")
        archived = main.archive_agent("WIKI-42")
        self.assertEqual(interrupted["state"], "interrupted")
        self.assertEqual(resumed["state"], "working")
        self.assertEqual(stopped["state"], "dead")
        self.assertEqual(archived["state"], "completed")
        self.assertEqual(
            [method for method, _ in self.client.calls],
            ["run/interrupt", "run/resume", "run/stop", "run/archive"],
        )

        registry = json.loads(self.registry.read_text(encoding="utf-8"))
        registry["WIKI-LEGACY"] = {
            "history": [],
            "current": {"window": "@9999", "kind": "cc"},
        }
        self.registry.write_text(json.dumps(registry), encoding="utf-8")
        with self.assertRaises(HTTPException) as legacy:
            main.stop_agent("WIKI-LEGACY")
        self.assertEqual(legacy.exception.status_code, 409)
        self.assertIn("must be migrated", str(legacy.exception.detail))

    async def test_mixed_fleet_keeps_legacy_control_isolated(self) -> None:
        self._seed_headless()
        registry = json.loads(self.registry.read_text(encoding="utf-8"))
        registry["WIKI-LEGACY"] = {
            "history": [],
            "current": {
                "window": "@9999",
                "kind": "cc",
                "role": "review",
                "model": "sonnet",
                "worktree": str(self.worktree),
            },
        }
        self.registry.write_text(json.dumps(registry), encoding="utf-8")
        calls_before = list(self.client.calls)
        background = BackgroundTasks()

        with (
            mock.patch.object(main, "tmux_live_windows", return_value={"@9999"}),
            mock.patch.object(main, "resolve_window", return_value="@9999"),
        ):
            payload = main.agents()
            sent = main.agent_message(
                "WIKI-LEGACY",
                main.MessageIn(text="legacy steer", mode="now"),
                background,
            )

        workers = cast(list[dict[str, Any]], payload["workers"])
        by_ticket = {worker["ticket"]: worker for worker in workers}
        self.assertTrue(by_ticket["WIKI-42"]["control_attached"])
        self.assertTrue(by_ticket["WIKI-LEGACY"]["window_alive"])
        self.assertEqual(sent, {"status": "sent"})
        self.assertEqual(len(background.tasks), 1)
        self.assertEqual(
            [call for call in self.client.calls if call not in calls_before],
            [("run/list", {})],
        )

    async def test_agents_and_log_use_supervisor_liveness_without_tmux(self) -> None:
        self._seed_headless()
        with mock.patch.object(
            main,
            "tmux_live_windows",
            side_effect=AssertionError("headless listing touched tmux"),
        ):
            payload = main.agents()
            log = main.agent_log("WIKI-42", lines=10)

        workers = cast(list[dict[str, Any]], payload["workers"])
        supervisor = cast(dict[str, Any], payload["supervisor"])
        worker = workers[0]
        self.assertEqual(worker["run_id"], RUN_ID)
        self.assertEqual(worker["runtime_state"], "working")
        self.assertTrue(worker["window_alive"])
        self.assertTrue(worker["control_attached"])
        self.assertEqual(supervisor["status"], "ready")
        self.assertEqual(log["path"], str(self.raw))
        self.assertIn("turn/started", log["tail"])

    async def test_session_prefers_supervisor_transcript_and_runtime_state(
        self,
    ) -> None:
        self._seed_headless()
        transcript = self.root / "rollout-supervisor-session.jsonl"
        transcript.write_bytes((FIXTURES / "codex_rollout_success.jsonl").read_bytes())
        registry = json.loads(self.registry.read_text(encoding="utf-8"))
        registry["WIKI-42"]["current"]["transcript"] = str(transcript)
        self.registry.write_text(json.dumps(registry), encoding="utf-8")

        with mock.patch.object(
            main,
            "resolve_window",
            side_effect=AssertionError("headless session touched tmux"),
        ):
            payload = main.agent_session("WIKI-42")

        self.assertEqual(payload["path"], str(transcript))
        self.assertEqual(payload["format"], "codex")
        self.assertTrue(payload["working"])
        self.assertEqual(payload["queue"], [])

    async def test_session_extends_wiki41_inspector_with_provider_stream(self) -> None:
        self._seed_headless()
        normalized_event = {
            "seq": 1,
            "raw_seq": 1,
            "normalized_at": "2026-07-09T12:00:00+00:00",
            "disposition": "rendered",
            "kind": "approval",
            "payload": {"method": "item/tool/requestUserInput"},
            "lifecycle_state": "waiting-approval",
        }
        self.client.normalized_events = [normalized_event]
        self.client.raw_events = [
            {
                "seq": 1,
                "payload": {"method": "item/tool/requestUserInput"},
            }
        ]
        self.client.pending_requests = [
            {
                "request_id": 0,
                "request_kind": "item/tool/requestUserInput",
                "received_at": "2026-07-09T12:00:00+00:00",
                "raw_seq": 1,
                "payload": normalized_event["payload"],
            }
        ]

        payload = main.agent_session("WIKI-42")
        inspector = cast(dict[str, Any], payload["provider_inspector"])
        self.assertEqual(payload["format"], "provider-events")
        self.assertEqual(inspector["raw_count"], 1)
        self.assertEqual(inspector["normalized_count"], 1)
        self.assertEqual(inspector["dispositions"]["rendered"], 1)
        self.assertEqual(inspector["events"][0]["kind"], "approval")
        self.assertEqual(inspector["pending_requests"][0]["request_id"], 0)

        raw = main.agent_provider_events(
            "WIKI-42",
            after_seq=0,
            limit=200,
            include_raw=True,
        )
        self.assertEqual(cast(list[dict[str, Any]], raw["raw"])[0]["seq"], 1)

        responded = main.respond_to_agent(
            "WIKI-42",
            main.AgentRespondIn(
                request_id=0,
                response={"answers": {"scope": {"answers": ["Full"]}}},
            ),
        )
        self.assertEqual(responded["state"], "working")
        self.assertEqual(self.client.pending_requests, [])

    async def test_spawn_and_replace_are_supervisor_owned(self) -> None:
        with mock.patch.object(
            main,
            "tmux_live_windows",
            side_effect=AssertionError("headless spawn touched tmux"),
        ):
            spawned = main.spawn_agent(
                main.SpawnWorkerIn(
                    ticket="WIKI-42",
                    kind="cdx",
                    role="implement",
                    model="gpt-5.4",
                    effort="high",
                    workdir=str(self.worktree),
                    orch=None,
                    prompt="Implement WIKI-42",
                )
            )
        self.assertEqual(spawned["window"], None)
        self.assertEqual(spawned["run_id"], RUN_ID)
        self.assertEqual(spawned["log"], str(self.raw))
        self.assertFalse((self.status_dir / "WIKI-42.json").exists())

        with mock.patch.object(
            main.agent_replace,
            "replace_agent",
            side_effect=AssertionError("headless replace used legacy protocol"),
        ):
            replaced = main.replace_agent("WIKI-42")
        self.assertEqual(replaced["run_id"], REPLACEMENT_RUN_ID)
        self.assertEqual(replaced["window"], None)
        replace_call = next(
            params for method, params in self.client.calls if method == "run/replace"
        )
        self.assertIn(str(self.status_dir / "WIKI-42.json"), replace_call["prompt"])

    async def test_orchestrator_spawn_grouping_and_controls_are_supervisor_owned(
        self,
    ) -> None:
        with mock.patch.object(
            main,
            "tmux_live_windows",
            side_effect=AssertionError("headless orchestrator spawn touched tmux"),
        ):
            spawned = main.spawn_orchestrator(
                main.SpawnOrchestratorIn(
                    id="wiki_dev",
                    workdir=str(self.worktree),
                    model="opus",
                    goal="Coordinate the isolated fixture fleet.",
                )
            )
            payload = main.agents()

        self.assertIsNone(spawned["window"])
        self.assertEqual(spawned["run_id"], RUN_ID)
        self.assertEqual(spawned["log"], str(self.raw))
        start = next(params for method, params in self.client.calls if method == "run/start")
        self.assertEqual(start["provider"], "claude")
        self.assertEqual(start["role"], "orchestrator")
        self.assertFalse(start["migrate_legacy"])
        self.assertIn("do not use tmux", start["prompt"])

        workers = cast(list[dict[str, Any]], payload["workers"])
        orchestrators = cast(list[dict[str, Any]], payload["orchestrators"])
        self.assertEqual(workers, [])
        self.assertEqual(orchestrators[0]["id"], "wiki_dev")
        self.assertEqual(orchestrators[0]["run_id"], RUN_ID)
        self.assertTrue(orchestrators[0]["control_attached"])

        sent = main.agent_message(
            "wiki_dev",
            main.MessageIn(text="steer orchestrator", mode="now"),
            BackgroundTasks(),
        )
        with mock.patch.object(
            main.agent_replace,
            "replace_agent",
            side_effect=AssertionError("headless orchestrator used legacy replace"),
        ):
            replaced = main.replace_agent("wiki_dev")
        self.assertEqual(sent, {"status": "sent"})
        self.assertEqual(replaced["type"], "orchestrator")
        self.assertEqual(replaced["run_id"], REPLACEMENT_RUN_ID)

    async def test_supervisor_event_bridge_preserves_sse_dictionary(self) -> None:
        subscriber = main._subscribe_agent_events()  # noqa: SLF001 - contract test
        task = asyncio.create_task(main.supervisor_event_bridge())
        try:
            event = await asyncio.wait_for(subscriber.get(), timeout=2)
            self.assertEqual(event, self.client.event)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_account_rotation_is_one_supervisor_owned_rpc(self) -> None:
        self._seed_headless()
        registry = json.loads(self.registry.read_text(encoding="utf-8"))
        registry["WIKI-CLAUDE"] = {
            "history": [],
            "current": {
                "ticket": "WIKI-CLAUDE",
                "run_id": "00000000-0000-4000-8000-000000000044",
                "provider": "claude",
                "kind": "cc",
                "role": "review",
                "model": "sonnet",
                "worktree": str(self.worktree),
                "state": "idle",
            },
        }
        self.registry.write_text(json.dumps(registry), encoding="utf-8")
        tmux_called = AssertionError("account route touched tmux")

        with (
            mock.patch.object(accounts := main.accounts, "rotate", side_effect=tmux_called),
            mock.patch.object(accounts, "rotate_credentials", side_effect=tmux_called),
            mock.patch.object(accounts, "tmux_live_windows", side_effect=tmux_called),
        ):
            result = await main.rotate_account(main.AccountRotateIn(account="beta"))

        self.assertEqual(
            result,
            {
                "from": "alpha",
                "to": "beta",
                "revived": ["WIKI-42"],
                "failed": [],
                "failed_reasons": {},
            },
        )
        method, params = self.client.calls[-1]
        self.assertEqual(method, "fleet/rotate_codex")
        self.assertEqual(params["account"], "beta")
        self.assertRegex(
            params["operation_id"],
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        )

    async def test_account_rotation_preserves_supervisor_conflict_status(self) -> None:
        self._seed_headless()
        self.client.rotation_error = SupervisorRemoteError(
            "Codex fleet is not safe to rotate: approval pending",
            error_type="StoreConflict",
        )
        with self.assertRaises(HTTPException) as blocked:
            await main.rotate_account(main.AccountRotateIn(account="beta"))
        self.assertEqual(blocked.exception.status_code, 409)
        self.assertIn("approval pending", str(blocked.exception.detail))


class BackendSupervisorEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.paths = RuntimePaths(
            runtime_dir=self.root / "runtime",
            socket_path=self.root / "runtime" / "supervisor.sock",
            registry_path=self.root / "agent-registry.json",
            archive_dir=self.root / "archive",
            status_dir=self.root / "status",
        )
        self.store = RunStore(self.paths)
        self.supervisor = Supervisor(
            self.store,
            FixtureAdapterFactory(FIXTURES, pid=os.getpid()),
        )
        self.server = UnixSupervisorServer(self.supervisor, self.paths.socket_path)
        await self.server.start()
        self.client = SupervisorClient(self.paths, timeout=2)
        self.patchers = [
            mock.patch.object(main, "AGENT_REGISTRY_PATH", self.paths.registry_path),
            mock.patch.object(main, "AGENT_STATUS_DIR", self.root / "status"),
            mock.patch.object(main, "AGENT_ARCHIVE_DIR", self.root / "archive"),
            mock.patch.object(main, "AGENT_TMP_DIR", self.root / "tmp"),
            mock.patch.object(main, "MSG_QUEUE_PATH", self.root / "legacy-queue.json"),
            mock.patch.object(main, "SUPERVISOR_CLIENT", self.client),
        ]
        for patcher in self.patchers:
            patcher.start()

    async def asyncTearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        await self.server.close()
        await self.supervisor.close()
        self.tmp.cleanup()

    async def test_spawn_and_composer_contract_round_trip_over_unix_socket(
        self,
    ) -> None:
        with mock.patch.object(
            main,
            "resolve_window",
            side_effect=AssertionError("headless route touched tmux"),
        ):
            spawned = await asyncio.to_thread(
                main.spawn_agent,
                main.SpawnWorkerIn(
                    ticket="WIKI-42",
                    kind="cdx",
                    role="implement",
                    model="gpt-5.4",
                    effort="high",
                    workdir=str(self.worktree),
                    orch=None,
                    prompt="Work on WIKI-42",
                ),
            )
            sent = await asyncio.to_thread(
                main.agent_message,
                "WIKI-42",
                main.MessageIn(text="steer now", mode="now"),
                BackgroundTasks(),
            )
            queued = await asyncio.to_thread(
                main.agent_message,
                "WIKI-42",
                main.MessageIn(text="after idle", mode="on-idle"),
                BackgroundTasks(),
            )
            agents = await asyncio.to_thread(main.agents)

        self.assertIsInstance(spawned["run_id"], str)
        self.assertIsNone(spawned["window"])
        self.assertEqual(sent, {"status": "sent"})
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["position"], 1)
        messages = cast(list[dict[str, Any]], queued["messages"])
        self.assertEqual(messages[0]["text"], "after idle")
        workers = cast(list[dict[str, Any]], agents["workers"])
        self.assertTrue(workers[0]["control_attached"])
        supervisor = cast(dict[str, Any], agents["supervisor"])
        self.assertEqual(supervisor["status"], "ready")


if __name__ == "__main__":
    unittest.main()
