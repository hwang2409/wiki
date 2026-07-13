from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
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
RAW_PENDING_ASK_FIXTURE = Path(__file__).parent / "fixtures" / "headless_pending_ask_raw.jsonl"
RAW_RESOLVED_ASK_FIXTURE = Path(__file__).parent / "fixtures" / "headless_resolved_ask_raw.jsonl"
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
            "desired_model": params.get("desired_model"),
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
                    "provider": values.get("provider", old["provider"]),
                    "role": old["role"],
                    "model": values.get("model", old["model"]),
                    "effort": values.get("effort", old.get("effort")),
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
        if method == "run/queue_model_change":
            agent_id = next(
                key
                for key, entry in registry.items()
                if isinstance(entry, dict)
                and (entry.get("current") or {}).get("run_id") == values["run_id"]
            )
            registry[agent_id]["current"]["desired_model"] = values["model"]
            self.registry_path.write_text(json.dumps(registry), encoding="utf-8")
            return {"status": "queued", "desired_model": values["model"]}
        if method == "run/cancel_model_change":
            agent_id = next(
                key
                for key, entry in registry.items()
                if isinstance(entry, dict)
                and (entry.get("current") or {}).get("run_id") == values["run_id"]
            )
            registry[agent_id]["current"]["desired_model"] = None
            self.registry_path.write_text(json.dumps(registry), encoding="utf-8")
            return {"status": "canceled", "desired_model": None}
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
        main._session_question_overlays.clear()  # noqa: SLF001 - isolate overlay cache
        main.transcripts._cache.clear()  # noqa: SLF001 - isolate transcript cache
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    def _seed_headless(
        self,
        *,
        provider: str = "codex",
        transcript: Path | None = None,
    ) -> None:
        self.client._write_current(  # noqa: SLF001 - fixture setup
            RUN_ID,
            {
                "agent_id": "WIKI-42",
                "provider": provider,
                "role": "implement",
                "model": "sonnet" if provider == "claude" else "gpt-5.4",
                "effort": None if provider == "claude" else "high",
                "worktree": str(self.worktree),
                "orchestrator_id": None,
            },
        )
        if transcript is not None:
            registry = json.loads(self.registry.read_text(encoding="utf-8"))
            registry["WIKI-42"]["current"]["transcript"] = str(transcript)
            self.registry.write_text(json.dumps(registry), encoding="utf-8")

    def _write_claude_transcript(
        self,
        path: Path,
        *,
        answered: bool = False,
    ) -> None:
        rows: list[dict[str, Any]] = [
            {
                "type": "assistant",
                "timestamp": "2026-07-10T16:00:00Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Waiting on the user."}],
                },
            }
        ]
        if answered:
            rows = [
                {
                    "type": "assistant",
                    "timestamp": "2026-07-10T16:00:01Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_pending_fixture",
                                "name": "AskUserQuestion",
                                "input": {
                                    "questions": [
                                        {
                                            "question": "Which path should I take?",
                                            "header": "Path",
                                            "options": [
                                                {"label": "Ship it"},
                                                {"label": "Wait"},
                                            ],
                                        }
                                    ]
                                },
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "timestamp": "2026-07-10T16:00:02Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_pending_fixture",
                                "content": 'Your questions have been answered: "Which path should I take?"="Ship it". You can now continue with these answers in mind.',
                                "is_error": False,
                            }
                        ],
                    },
                },
            ]
        path.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
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

    async def test_set_model_routes_to_supervisor_and_cancel_clears_pending(self) -> None:
        self._seed_headless()

        queued = main.set_agent_model("WIKI-42", main.SetModelIn(model="gpt-5.5"))
        session = main.agent_session("WIKI-42")
        canceled = main.cancel_agent_model("WIKI-42")

        self.assertEqual(queued, {"status": "queued", "desired_model": "gpt-5.5"})
        self.assertEqual(session["desired_model"], "gpt-5.5")
        self.assertEqual(canceled, {"status": "canceled", "desired_model": None})
        self.assertEqual(
            [method for method, _ in self.client.calls],
            [
                "run/queue_model_change",
                "events/read",
                "run/queue",
                "run/cancel_model_change",
            ],
        )

    async def test_set_model_rejects_invalid_cross_kind_and_noop_models(self) -> None:
        self._seed_headless(provider="claude")

        with self.assertRaises(HTTPException) as invalid:
            main.set_agent_model("WIKI-42", main.SetModelIn(model="gpt-5.5"))
        with self.assertRaises(HTTPException) as noop:
            main.set_agent_model("WIKI-42", main.SetModelIn(model="sonnet"))

        self.assertEqual(invalid.exception.status_code, 400)
        self.assertIn("not allowed for Claude", str(invalid.exception.detail))
        self.assertEqual(noop.exception.status_code, 400)
        self.assertIn("matches current", str(noop.exception.detail))
        self.assertEqual(self.client.calls, [])

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

    async def test_mixed_fleet_surfaces_legacy_liveness_but_refuses_control(self) -> None:
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

        with mock.patch.object(main, "tmux_live_windows", return_value={"@9999"}):
            payload = main.agents()
            with self.assertRaises(HTTPException) as blocked:
                main.agent_message(
                    "WIKI-LEGACY",
                    main.MessageIn(text="legacy steer", mode="now"),
                    background,
                )

        workers = cast(list[dict[str, Any]], payload["workers"])
        by_ticket = {worker["ticket"]: worker for worker in workers}
        self.assertTrue(by_ticket["WIKI-42"]["control_attached"])
        self.assertTrue(by_ticket["WIKI-LEGACY"]["window_alive"])
        self.assertEqual(blocked.exception.status_code, 409)
        self.assertIn("must be migrated", str(blocked.exception.detail))
        self.assertEqual(len(background.tasks), 0)
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
        self.assertEqual(payload["model"], "gpt-5.4")
        self.assertEqual(payload["kind"], "cdx")
        self.assertEqual(payload["provider"], "codex")

    async def test_session_overlays_pending_headless_question_from_raw_log(self) -> None:
        transcript = self.root / "claude-session.jsonl"
        self._write_claude_transcript(transcript)
        self.raw.write_bytes(RAW_PENDING_ASK_FIXTURE.read_bytes())
        self._seed_headless(provider="claude", transcript=transcript)

        payload = main.agent_session("WIKI-42")
        questions = [
            event for event in cast(list[dict[str, Any]], payload["events"])
            if event.get("kind") == "question"
        ]
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["question"]["tool_use_id"], "toolu_pending_fixture")
        self.assertIsNone(questions[0]["question"]["answered_option"])
        self.assertGreater(
            int(payload["cursor"]),
            int(main.transcripts.read_session_events("claude", transcript)["cursor"]),
        )

        unchanged = main.agent_session(
            "WIKI-42",
            cursor=cast(int, payload["cursor"]),
            client_path=cast(str, payload["path"]),
        )
        self.assertEqual(unchanged["events"], [])

    async def test_session_drops_overlay_after_raw_tool_result_arrives(self) -> None:
        transcript = self.root / "claude-session.jsonl"
        self._write_claude_transcript(transcript)
        self.raw.write_bytes(RAW_RESOLVED_ASK_FIXTURE.read_bytes())
        self._seed_headless(provider="claude", transcript=transcript)

        payload = main.agent_session("WIKI-42")
        questions = [
            event for event in cast(list[dict[str, Any]], payload["events"])
            if event.get("kind") == "question"
        ]
        self.assertEqual(questions, [])

    async def test_session_replaces_overlay_with_answered_transcript_without_duplicate(
        self,
    ) -> None:
        transcript = self.root / "claude-session.jsonl"
        self._write_claude_transcript(transcript)
        self.raw.write_bytes(RAW_PENDING_ASK_FIXTURE.read_bytes())
        self._seed_headless(provider="claude", transcript=transcript)

        first = main.agent_session("WIKI-42")
        first_questions = [
            event for event in cast(list[dict[str, Any]], first["events"])
            if event.get("kind") == "question"
        ]
        self.assertEqual(len(first_questions), 1)
        self.assertIsNone(first_questions[0]["question"]["answered_option"])

        self._write_claude_transcript(transcript, answered=True)
        self.raw.write_bytes(RAW_RESOLVED_ASK_FIXTURE.read_bytes())
        main.transcripts._cache.clear()  # noqa: SLF001 - simulate transcript refresh

        second = main.agent_session(
            "WIKI-42",
            cursor=cast(int, first["cursor"]),
            client_path=cast(str, first["path"]),
        )
        second_questions = [
            event for event in cast(list[dict[str, Any]], second["events"])
            if event.get("kind") == "question"
        ]
        self.assertEqual(len(second_questions), 1)
        self.assertEqual(second_questions[0]["question"]["tool_use_id"], "toolu_pending_fixture")
        self.assertEqual(second_questions[0]["question"]["answered_option"], 0)
        self.assertEqual(second["tail_from"], second["base"])
        self.assertGreater(cast(int, second["cursor"]), cast(int, first["cursor"]))

    async def test_session_skips_overlay_for_non_headless_claude_transcript(self) -> None:
        transcript = self.root / "claude-session.jsonl"
        self._write_claude_transcript(transcript)
        self.raw.write_bytes(RAW_PENDING_ASK_FIXTURE.read_bytes())

        payload = main._session_delta_payload("claude", transcript, cursor=0)
        questions = [
            event for event in cast(list[dict[str, Any]], payload["events"])
            if event.get("kind") == "question"
        ]
        self.assertEqual(questions, [])

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
        self.assertEqual(payload["model"], "gpt-5.4")
        self.assertEqual(payload["kind"], "cdx")
        self.assertEqual(payload["provider"], "codex")
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

        mixed_answers = {
            "answers": {
                "Which features?": ["Search", "Vim mode"],
                "Which rollout?": "Ship now",
            }
        }
        main.respond_to_agent(
            "WIKI-42",
            main.AgentRespondIn(request_id="toolu-mixed", response=mixed_answers),
        )
        self.assertEqual(self.client.calls[-1][0], "run/respond")
        self.assertEqual(self.client.calls[-1][1]["response"], mixed_answers)

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

        replaced = main.replace_agent("WIKI-42")
        self.assertEqual(replaced["run_id"], REPLACEMENT_RUN_ID)
        self.assertEqual(replaced["window"], None)
        replace_call = next(
            params for method, params in self.client.calls if method == "run/replace"
        )
        self.assertIn(str(self.status_dir / "WIKI-42.json"), replace_call["prompt"])
        self.assertEqual(replace_call["provider"], "codex")
        self.assertEqual(replace_call["model"], "gpt-5.4")
        self.assertEqual(replace_call["effort"], "high")

    async def test_replace_accepts_model_kind_and_effort_overrides(self) -> None:
        self._seed_headless(provider="claude")

        replaced = main.replace_agent(
            "WIKI-42",
            main.SpawnReplaceIn(kind="cdx", model="gpt-5.4"),
        )

        self.assertEqual(replaced["model"], "gpt-5.4")
        replace_call = self.client.calls[-1][1]
        self.assertEqual(replace_call["provider"], "codex")
        self.assertEqual(replace_call["model"], "gpt-5.4")
        self.assertEqual(replace_call["effort"], "high")

    async def test_replace_kind_only_uses_role_default_model(self) -> None:
        self.client._write_current(  # noqa: SLF001 - fixture setup
            RUN_ID,
            {
                "agent_id": "wiki_dev",
                "provider": "claude",
                "role": "orchestrator",
                "model": "opus",
                "effort": None,
                "worktree": str(self.worktree),
                "orchestrator_id": None,
            },
        )

        main.replace_agent("wiki_dev", main.SpawnReplaceIn(kind="cdx"))

        replace_call = self.client.calls[-1][1]
        self.assertEqual(replace_call["provider"], "codex")
        self.assertEqual(replace_call["model"], "gpt-5.6-sol")
        self.assertEqual(replace_call["effort"], "high")

    async def test_replace_cc_model_override_stays_on_claude(self) -> None:
        self._seed_headless(provider="claude")

        main.replace_agent(
            "WIKI-42",
            main.SpawnReplaceIn(model="sonnet-4.6"),
        )

        replace_call = self.client.calls[-1][1]
        self.assertEqual(replace_call["provider"], "claude")
        self.assertEqual(replace_call["model"], "sonnet-4.6")
        self.assertIsNone(replace_call["effort"])

    async def test_replace_rejects_invalid_model_and_kind_before_supervisor(self) -> None:
        self._seed_headless(provider="claude")

        with self.assertRaises(HTTPException) as bad_model:
            main.replace_agent(
                "WIKI-42",
                main.SpawnReplaceIn(model="gpt-5.4"),
            )
        with self.assertRaises(HTTPException) as bad_kind:
            main.replace_agent(
                "WIKI-42",
                main.SpawnReplaceIn(kind="other"),
            )

        self.assertEqual(bad_model.exception.status_code, 400)
        self.assertEqual(bad_kind.exception.status_code, 400)
        self.assertEqual(self.client.calls, [])

    async def test_spawn_rejects_unknown_model_with_clear_400_before_supervisor(self) -> None:
        with self.assertRaises(HTTPException) as blocked:
            main.spawn_agent(
                {
                    "ticket": "WIKI-42",
                    "kind": "cc",
                    "role": "implement",
                    "model": "opus-4.8",
                    "effort": None,
                    "workdir": str(self.worktree),
                    "orch": None,
                    "prompt": "Implement WIKI-42",
                }
            )

        self.assertEqual(blocked.exception.status_code, 400)
        self.assertEqual(self.client.calls, [])
        self.assertIn("opus-4.8", str(blocked.exception.detail))
        self.assertIn("Allowed values: claude-fable-5, opus-4.7, opus, sonnet, sonnet-4.6, haiku, haiku-4.5", str(blocked.exception.detail))

    async def test_spawn_accepts_gpt_56_sol_variant(self) -> None:
        spawned = main.spawn_agent(
            main.SpawnWorkerIn(
                ticket="WIKI-42",
                kind="cdx",
                role="implement",
                model="gpt-5.6-sol",
                effort="high",
                workdir=str(self.worktree),
                orch=None,
                prompt="Implement WIKI-42",
            )
        )

        self.assertEqual(spawned["run_id"], RUN_ID)
        start = next(params for method, params in self.client.calls if method == "run/start")
        self.assertEqual(start["model"], "gpt-5.6-sol")

    async def test_spawn_rejects_legacy_bare_gpt_56_before_supervisor(self) -> None:
        with self.assertRaises(HTTPException) as blocked:
            main.spawn_agent(
                main.SpawnWorkerIn(
                    ticket="WIKI-42",
                    kind="cdx",
                    role="implement",
                    model="gpt-5.6",
                    effort="high",
                    workdir=str(self.worktree),
                    orch=None,
                    prompt="Implement WIKI-42",
                )
            )

        self.assertEqual(blocked.exception.status_code, 400)
        self.assertEqual(self.client.calls, [])
        self.assertIn("gpt-5.6", str(blocked.exception.detail))
        self.assertIn(
            "Allowed values: gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-5.5, gpt-5.4, gpt-5.4-mini, gpt-5.3-codex-spark",
            str(blocked.exception.detail),
        )

    async def test_orchestrator_spawn_rejects_unknown_model_before_supervisor(self) -> None:
        with self.assertRaises(HTTPException) as blocked:
            main.spawn_orchestrator(
                {
                    "id": "wiki_dev",
                    "workdir": str(self.worktree),
                    "model": "opus-4.8",
                    "goal": "Coordinate the isolated fixture fleet.",
                }
            )

        self.assertEqual(blocked.exception.status_code, 400)
        self.assertEqual(self.client.calls, [])
        self.assertIn("opus-4.8", str(blocked.exception.detail))
        self.assertIn("Allowed values: claude-fable-5, opus-4.7, opus, sonnet, sonnet-4.6, haiku, haiku-4.5", str(blocked.exception.detail))

    async def test_legacy_replace_is_rejected(self) -> None:
        registry = {}
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

        with self.assertRaises(HTTPException) as blocked:
            main.replace_agent("WIKI-LEGACY")

        self.assertEqual(blocked.exception.status_code, 409)
        self.assertIn("must be migrated", str(blocked.exception.detail))

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
        replaced = main.replace_agent("wiki_dev")
        self.assertEqual(sent, {"status": "sent"})
        self.assertEqual(replaced["type"], "orchestrator")
        self.assertEqual(replaced["run_id"], REPLACEMENT_RUN_ID)

    async def test_orchestrator_spawn_accepts_cdx_kind_with_valid_model_and_effort(self) -> None:
        spawned = main.spawn_orchestrator(
            main.SpawnOrchestratorIn(
                id="wiki_cdx",
                workdir=str(self.worktree),
                kind="cdx",
                model="gpt-5.4",
                effort="high",
                goal="Orchestrate the cdx fleet.",
            )
        )
        self.assertIsNone(spawned["window"])
        self.assertEqual(spawned["run_id"], RUN_ID)
        start = next(params for method, params in self.client.calls if method == "run/start")
        self.assertEqual(start["provider"], "codex")
        self.assertEqual(start["role"], "orchestrator")
        self.assertEqual(start["model"], "gpt-5.4")
        self.assertEqual(start["effort"], "high")

    async def test_orchestrator_spawn_rejects_cdx_with_invalid_model_before_supervisor(self) -> None:
        with self.assertRaises(HTTPException) as blocked:
            main.spawn_orchestrator(
                {
                    "id": "wiki_cdx",
                    "workdir": str(self.worktree),
                    "kind": "cdx",
                    "model": "gpt-99",
                    "effort": "high",
                    "goal": "Coordinate.",
                }
            )
        self.assertEqual(blocked.exception.status_code, 400)
        self.assertEqual(self.client.calls, [])
        self.assertIn("gpt-99", str(blocked.exception.detail))

    async def test_orchestrator_spawn_rejects_invalid_kind(self) -> None:
        with self.assertRaises(HTTPException) as blocked:
            main.spawn_orchestrator(
                {
                    "id": "wiki_bad",
                    "workdir": str(self.worktree),
                    "kind": "bad",
                    "model": "opus",
                    "effort": None,
                    "goal": "",
                }
            )
        self.assertEqual(blocked.exception.status_code, 400)
        self.assertEqual(self.client.calls, [])

    async def test_orchestrator_spawn_cc_still_works_without_kind(self) -> None:
        spawned = main.spawn_orchestrator(
            {
                "id": "wiki_cc_compat",
                "workdir": str(self.worktree),
                "model": "opus",
                "goal": "Coordinate the cc fleet.",
            }
        )
        self.assertIsNone(spawned["window"])
        self.assertEqual(spawned["run_id"], RUN_ID)
        start = next(params for method, params in self.client.calls if method == "run/start")
        self.assertEqual(start["provider"], "claude")
        self.assertEqual(start["effort"], None)

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


class DetachedHeadlessAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo_root = Path(__file__).resolve().parents[2]
        self.python = Path(sys.executable)
        self.worktree = self.root / "worktree"
        self.bin_dir = self.root / "bin"
        self.status_dir = self.root / "status"
        self.archive_dir = self.root / "archive"
        self.tmp_dir = self.root / "tmp"
        self.sessions_dir = self.root / "sessions"
        self.vault_dir = self.root / "vault"
        self.account_home = self.root / "account-home"
        self.claude_config_dir = self.account_home / "claude"
        self.paths = RuntimePaths(
            runtime_dir=self.root / "runtime",
            socket_path=self.root / "runtime" / "supervisor.sock",
            registry_path=self.root / "agent-registry.json",
            archive_dir=self.archive_dir,
            status_dir=self.status_dir,
        )
        for directory in (
            self.worktree,
            self.bin_dir,
            self.status_dir,
            self.archive_dir,
            self.tmp_dir,
            self.sessions_dir,
            self.vault_dir,
            self.claude_config_dir,
            self.root / "queue-parent",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.queue_path = self.root / "queue-parent" / "wiki-msg-queue.json"
        self.queue_path.write_text("{}\n", encoding="utf-8")
        self.backend_driver = self.root / "backend-driver.py"
        self.codex_log = self.root / "codex-protocol.log"
        self.claude_log = self.root / "claude-protocol.log"
        self._write_cli_wrapper(
            "codex",
            self.codex_log,
            FIXTURES / "fake_codex_app_server.py",
            extra_exports={
                "FAKE_CODEX_TRANSCRIPT_DIR": str(self.sessions_dir),
            },
        )
        self._write_cli_wrapper(
            "claude",
            self.claude_log,
            FIXTURES / "fake_claude_stream.py",
        )
        self.env = {
            **os.environ,
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "PYTHONPATH": f"{self.repo_root}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
            "PYTHONUNBUFFERED": "1",
            "TMUX": "",
            "TMUX_PANE": "",
            "WIKI_AGENT_REGISTRY_PATH": str(self.paths.registry_path),
            "WIKI_AGENT_STATUS_DIR": str(self.status_dir),
            "WIKI_AGENT_ARCHIVE_DIR": str(self.archive_dir),
            "WIKI_AGENT_TMP_DIR": str(self.tmp_dir),
            "WIKI_MSG_QUEUE_PATH": str(self.queue_path),
            "WIKI_AGENT_RUNTIME_DIR": str(self.paths.runtime_dir),
            "WIKI_SUPERVISOR_SOCKET_PATH": str(self.paths.socket_path),
            "WIKI_SUPERVISOR_AUTOSTART": "off",
            "WIKI_VAULT_DIR": str(self.vault_dir),
            "WIKI_CODEX_SESSIONS_DIR": str(self.sessions_dir),
            "WIKI_CLAUDE_PROJECTS_DIR": str(self.claude_config_dir / "projects"),
            "WIKI_ACCOUNT_HOME_OVERRIDE": str(self.account_home),
            "WIKI_CODEX_AUTH_PATH": str(self.account_home / "codex" / "auth.json"),
            "WIKI_CODEX_ACCOUNTS_DIR": str(self.account_home / "codex-accounts"),
            "WIKI_ROTATION_LOG_PATH": str(self.account_home / "rotation.log"),
            "WIKI_ACCOUNT_WATCHDOG": "off",
            "CODEX_HOME": str(self.account_home / "codex"),
            "CLAUDE_CONFIG_DIR": str(self.claude_config_dir),
            "WIKI_TEST_REPO_ROOT": str(self.repo_root),
        }
        self._write_backend_driver()
        self.daemon: subprocess.Popen[str] | None = None
        self.client = SupervisorClient(self.paths, timeout=1.0)

    def tearDown(self) -> None:
        self._stop_daemon()
        self.tmp.cleanup()

    def _write_cli_wrapper(
        self,
        name: str,
        log_path: Path,
        script_path: Path,
        *,
        extra_exports: dict[str, str] | None = None,
    ) -> None:
        extra = extra_exports or {}
        exports = "\n".join(
            f'export {key}="{value}"' for key, value in {"FAKE_PROTOCOL_LOG": str(log_path), **extra}.items()
        )
        wrapper = self.bin_dir / name
        wrapper.write_text(
            "#!/bin/sh\n"
            f'{exports}\n'
            f'exec "{self.python}" "{script_path}" "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

    def _write_backend_driver(self) -> None:
        self.backend_driver.write_text(
            """
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ["WIKI_TEST_REPO_ROOT"])
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import BackgroundTasks, HTTPException
from backend.app import main


def _ticket_from(path: str, suffix: str) -> str:
    prefix = "/api/agents/"
    if not path.startswith(prefix) or not path.endswith(suffix):
        raise RuntimeError(f"unsupported path: {path}")
    return path[len(prefix) : -len(suffix)]


for raw in sys.stdin:
    if not raw:
        break
    request = json.loads(raw)
    method = request["method"]
    path = request["path"]
    body = request.get("body") or {}
    try:
        if method == "GET" and path == "/health":
            result = main.health()
        elif method == "GET" and path == "/api/agents":
            result = main.agents()
        elif method == "POST" and path == "/api/agents/spawn":
            result = main.spawn_agent(main.SpawnWorkerIn(**body))
        elif method == "GET" and path.endswith("/events?include_raw=true"):
            result = main.agent_provider_events(
                _ticket_from(path, "/events?include_raw=true"),
                after_seq=0,
                limit=200,
                include_raw=True,
            )
        elif method == "POST" and path.endswith("/replace"):
            result = main.replace_agent(
                _ticket_from(path, "/replace"),
                main.SpawnReplaceIn(**body),
            )
        elif method == "POST" and path.endswith("/resume"):
            result = main.resume_agent(_ticket_from(path, "/resume"))
        elif method == "POST" and path.endswith("/message"):
            result = main.agent_message(
                _ticket_from(path, "/message"),
                main.MessageIn(**body),
                BackgroundTasks(),
            )
        else:
            raise RuntimeError(f"unsupported request: {method} {path}")
        sys.stdout.write(json.dumps({"result": result}, separators=(",", ":")) + "\\n")
        sys.stdout.flush()
    except HTTPException as exc:
        sys.stdout.write(
            json.dumps(
                {
                    "error": {
                        "type": "HTTPException",
                        "status": exc.status_code,
                        "detail": exc.detail,
                    }
                },
                separators=(",", ":"),
            )
            + "\\n"
        )
        sys.stdout.flush()
    except Exception as exc:
        sys.stdout.write(
            json.dumps(
                {
                    "error": {
                        "type": type(exc).__name__,
                        "detail": str(exc),
                    }
                },
                separators=(",", ":"),
            )
            + "\\n"
        )
        sys.stdout.flush()
""".strip()
            + "\n",
            encoding="utf-8",
        )
        self.backend_driver.chmod(0o755)

    def _wait_for(
        self,
        loader,
        predicate,
        *,
        timeout: float = 15.0,
        interval: float = 0.05,
    ):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                last = loader()
            except Exception as exc:  # noqa: BLE001 - polling helper
                last = exc
            else:
                if predicate(last):
                    return last
            time.sleep(interval)
        self.fail(f"timed out waiting for condition; last value: {last!r}")

    def _start_daemon(self) -> None:
        if self.daemon is not None and self.daemon.poll() is None:
            return
        self.daemon = subprocess.Popen(
            [
                str(self.python),
                "-m",
                "backend.app.agent_runtime.daemon",
                "--runtime-dir",
                str(self.paths.runtime_dir),
                "--socket",
                str(self.paths.socket_path),
                "--registry",
                str(self.paths.registry_path),
            ],
            cwd=self.repo_root,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self._wait_for(
                self.client.ping,
                lambda payload: payload.get("status") == "ok",
            )
        except AssertionError as exc:
            stderr = self.daemon.stderr.read() if self.daemon and self.daemon.stderr else ""
            raise AssertionError(f"{exc}\ndaemon stderr:\n{stderr}") from exc
        if self.daemon.poll() is not None:
            stderr = self.daemon.stderr.read() if self.daemon.stderr else ""
            self.fail(f"daemon exited during startup: {stderr}")

    def _stop_daemon(self) -> None:
        process = self.daemon
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process.stderr:
            process.stderr.close()
        self.daemon = None

    def _start_backend(self) -> None:
        try:
            self._wait_for(
                lambda: self._read_json("GET", "/health"),
                lambda payload: payload.get("status") == "ok",
            )
        except AssertionError as exc:
            raise AssertionError(f"{exc}\nbackend request bootstrap failed") from exc

    def _stop_backend(self) -> None:
        """Each request uses a fresh backend process; no persistent child to stop."""

        return None

    def _read_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = json.dumps(
            {"method": method, "path": path, "body": body},
            separators=(",", ":"),
        )
        process = subprocess.run(
            [str(self.python), str(self.backend_driver)],
            cwd=self.repo_root,
            env=self.env,
            input=f"{request}\n",
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if process.returncode != 0:
            raise AssertionError(
                f"backend request process failed ({process.returncode}): {process.stderr or process.stdout}"
            )
        lines = [line for line in process.stdout.splitlines() if line]
        if not lines:
            raise AssertionError(f"backend request produced no output: {process.stderr}")
        response = cast(dict[str, Any], json.loads(lines[-1]))
        error = response.get("error")
        if isinstance(error, dict):
            raise AssertionError(f"{method} {path} failed: {error}")
        return cast(dict[str, Any], response["result"])

    def _registry(self) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            json.loads(self.paths.registry_path.read_text(encoding="utf-8")),
        )

    def _current(self, agent_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._registry()[agent_id]["current"])

    def _agents_payload(self) -> dict[str, Any]:
        return self._read_json("GET", "/api/agents")

    def _worker_row(self, payload: dict[str, Any], ticket: str) -> dict[str, Any]:
        workers = cast(list[dict[str, Any]], payload["workers"])
        return next(row for row in workers if row["ticket"] == ticket)

    def _events(self, ticket: str) -> dict[str, Any]:
        return self._read_json("GET", f"/api/agents/{ticket}/events?include_raw=true")

    def _assert_tmux_stripped(self, log_path: Path) -> None:
        rows = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        env_rows = [row for row in rows if "tmux" in row]
        self.assertTrue(env_rows)
        for row in env_rows:
            self.assertIn(row.get("tmux"), (None, ""))
            self.assertIn(row.get("tmux_pane"), (None, ""))

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def _write_evidence(self, payload: dict[str, Any]) -> None:
        out_dir = os.environ.get("WIKI_HEADLESS_EVIDENCE_DIR")
        if not out_dir:
            return
        target = Path(out_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / "summary.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        shutil.copy2(self.codex_log, target / "codex-protocol.log")
        shutil.copy2(self.claude_log, target / "claude-protocol.log")
        shutil.copy2(self.paths.registry_path, target / "agent-registry.json")

    def test_real_daemon_backend_restart_and_mixed_fleet_cycle_without_tmux(
        self,
    ) -> None:
        self._start_daemon()
        self._start_backend()

        codex = self._read_json(
            "POST",
            "/api/agents/spawn",
            {
                "ticket": "WIKI-CODEX",
                "kind": "cdx",
                "role": "implement",
                "model": "gpt-5.4",
                "effort": "high",
                "workdir": str(self.worktree),
                "orch": None,
                "prompt": "Implement the Codex side of WIKI-42.",
            },
        )
        claude = self._read_json(
            "POST",
            "/api/agents/spawn",
            {
                "ticket": "WIKI-CLAUDE",
                "kind": "cc",
                "role": "review",
                "model": "sonnet",
                "effort": None,
                "workdir": str(self.worktree),
                "orch": None,
                "prompt": "Review the Claude side of WIKI-42.",
            },
        )
        self.assertIsNone(codex["window"])
        self.assertIsNone(claude["window"])

        initial_agents = self._wait_for(
            self._agents_payload,
            lambda payload: {
                row["ticket"] for row in cast(list[dict[str, Any]], payload["workers"])
            }
            >= {"WIKI-CODEX", "WIKI-CLAUDE"}
            and cast(dict[str, Any], payload["supervisor"]).get("status") == "ready",
        )
        codex_row = self._worker_row(initial_agents, "WIKI-CODEX")
        claude_row = self._worker_row(initial_agents, "WIKI-CLAUDE")
        self.assertTrue(codex_row["control_attached"])
        self.assertTrue(claude_row["control_attached"])
        self.assertIsNone(codex_row["window"])
        self.assertIsNone(claude_row["window"])

        codex_events_before = self._wait_for(
            lambda: self._events("WIKI-CODEX"),
            lambda payload: cast(int, payload["raw_count"]) > 0,
        )
        claude_events_before = self._wait_for(
            lambda: self._events("WIKI-CLAUDE"),
            lambda payload: cast(int, payload["raw_count"]) > 0,
        )
        codex_session_before = cast(str, self._current("WIKI-CODEX")["provider_session_id"])
        claude_session_before = cast(
            str, self._current("WIKI-CLAUDE")["provider_session_id"]
        )
        self._assert_tmux_stripped(self.codex_log)
        self._assert_tmux_stripped(self.claude_log)

        self._stop_backend()
        self.assertEqual(
            self.client.send_message("WIKI-CODEX", "keep going while backend is down", "now"),
            {"status": "sent"},
        )
        self.assertEqual(
            self.client.send_message("WIKI-CLAUDE", "keep going while backend is down", "now"),
            {"status": "sent"},
        )

        self._start_backend()
        codex_events_after_restart = self._wait_for(
            lambda: self._events("WIKI-CODEX"),
            lambda payload: cast(int, payload["raw_count"])
            > cast(int, codex_events_before["raw_count"]),
        )
        claude_events_after_restart = self._wait_for(
            lambda: self._events("WIKI-CLAUDE"),
            lambda payload: cast(int, payload["raw_count"])
            > cast(int, claude_events_before["raw_count"]),
        )
        self.assertEqual(
            self._current("WIKI-CODEX")["provider_session_id"], codex_session_before
        )
        self.assertEqual(
            self._current("WIKI-CLAUDE")["provider_session_id"], claude_session_before
        )

        replaced = self._read_json("POST", "/api/agents/WIKI-CODEX/replace")
        replaced_run_id = cast(str, replaced["run_id"])
        self.assertNotEqual(replaced_run_id, codex["run_id"])
        replaced_current = self._wait_for(
            lambda: self._current("WIKI-CODEX"),
            lambda current: current["run_id"] == replaced_run_id,
        )

        claude_before_kill = self._current("WIKI-CLAUDE")
        claude_pid_before = cast(int, claude_before_kill["provider_pid"])
        os.kill(claude_pid_before, signal.SIGTERM)
        detached = self._wait_for(
            self._agents_payload,
            lambda payload: not self._worker_row(payload, "WIKI-CLAUDE")["control_attached"],
        )
        self.assertIsNotNone(self._worker_row(detached, "WIKI-CLAUDE")["provider_pid"])

        try:
            resumed = self._read_json("POST", "/api/agents/WIKI-CLAUDE/resume")
        except AssertionError as exc:
            if "run already has an attached provider adapter" not in str(exc):
                raise
            # The daemon's recovery loop may win the race and reattach before
            # the explicit API resume lands; either path is a valid revival.
            resumed = self._current("WIKI-CLAUDE")
        self.assertEqual(resumed["provider_session_id"], claude_session_before)
        revived = self._wait_for(
            lambda: self._current("WIKI-CLAUDE"),
            lambda current: current["provider_session_id"] == claude_session_before
            and current["provider_pid"] not in (None, claude_pid_before),
        )
        final_agents = self._agents_payload()
        self.assertTrue(self._worker_row(final_agents, "WIKI-CLAUDE")["control_attached"])
        self.assertTrue(self._worker_row(final_agents, "WIKI-CODEX")["control_attached"])
        self._assert_tmux_stripped(self.codex_log)
        self._assert_tmux_stripped(self.claude_log)
        self._write_evidence(
            {
                "backend_restart": {
                    "codex_raw_before": codex_events_before["raw_count"],
                    "codex_raw_after": codex_events_after_restart["raw_count"],
                    "claude_raw_before": claude_events_before["raw_count"],
                    "claude_raw_after": claude_events_after_restart["raw_count"],
                },
                "replace": {
                    "old_run_id": codex["run_id"],
                    "new_run_id": replaced_run_id,
                    "session_id": replaced_current["provider_session_id"],
                },
                "revive": {
                    "session_id": claude_session_before,
                    "old_pid": claude_pid_before,
                    "new_pid": revived["provider_pid"],
                },
            }
        )

    def test_cross_provider_orchestrator_replace_terminates_old_provider_pids(
        self,
    ) -> None:
        self._start_daemon()
        self._start_backend()
        started = cast(
            dict[str, Any],
            self.client.request(
                "run/start",
                {
                    "agent_id": "wiki",
                    "provider": "claude",
                    "role": "orchestrator",
                    "model": "opus",
                    "effort": None,
                    "worktree": str(self.worktree),
                    "prompt": "Coordinate the isolated WIKI-81 fixture fleet.",
                    "orchestrator_id": None,
                },
            ),
        )
        claude_pid = cast(int, started["provider_pid"])
        self.assertTrue(self._pid_alive(claude_pid))

        codex = self._read_json(
            "POST",
            "/api/agents/wiki/replace",
            {"kind": "cdx", "model": "gpt-5.4", "effort": "high"},
        )
        codex_current = self._wait_for(
            lambda: self._current("wiki"),
            lambda current: current["run_id"] == codex["run_id"]
            and current["provider"] == "codex",
        )
        codex_pid = cast(int, codex_current["provider_pid"])
        self.assertNotEqual(codex_pid, claude_pid)
        self.assertTrue(self._pid_alive(codex_pid))
        self._wait_for(lambda: self._pid_alive(claude_pid), lambda alive: not alive)

        claude = self._read_json(
            "POST",
            "/api/agents/wiki/replace",
            {"kind": "cc", "model": "opus"},
        )
        claude_current = self._wait_for(
            lambda: self._current("wiki"),
            lambda current: current["run_id"] == claude["run_id"]
            and current["provider"] == "claude",
        )
        replacement_claude_pid = cast(int, claude_current["provider_pid"])
        self.assertNotIn(replacement_claude_pid, {claude_pid, codex_pid})
        self.assertTrue(self._pid_alive(replacement_claude_pid))
        self._wait_for(lambda: self._pid_alive(codex_pid), lambda alive: not alive)
        self.assertEqual(claude_current["kind"], "cc")
        self.assertEqual(claude_current["model"], "opus")
        self.assertIsNone(claude_current["effort"])


if __name__ == "__main__":
    unittest.main()
