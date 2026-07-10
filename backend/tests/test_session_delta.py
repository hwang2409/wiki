from __future__ import annotations

import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from backend.app import main, transcripts


def _write_rows(path: Path, rows: list[dict], mode: str = "a") -> None:
    with path.open(mode, encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _legacy_delta(fmt: str, path: Path, after: int) -> dict:
    result = transcripts.read_session_events(fmt, path)
    base = result["base"]
    events = result["events"]
    total = base + len(events)
    start = max(base, min(max(after, 0), result["dirty_from"], total))
    return {"from": start, "events": events[start - base :], "total": total}


class SessionDeltaTests(unittest.TestCase):
    def setUp(self) -> None:
        transcripts._cache.clear()

    def test_models_endpoint_includes_new_codex_and_claude_options(self) -> None:
        payload = main.list_models()
        models = {model["id"]: model for model in payload["models"]}

        self.assertIn("gpt-5.6", models)
        self.assertEqual(models["gpt-5.6"]["kind"], "cdx")
        self.assertIn("opus-4.8", models)
        self.assertEqual(models["opus-4.8"]["kind"], "cc")

    def test_landing_poll_patch_fixes_legacy_miss(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            _write_rows(
                path,
                [
                    {
                        "type": "response_item",
                        "timestamp": "2026-07-09T01:00:00Z",
                        "payload": {
                            "type": "function_call",
                            "call_id": "call-1",
                            "name": "exec_command",
                            "arguments": "echo hi",
                        },
                    }
                ],
                mode="w",
            )

            initial = transcripts.read_session_delta("codex", path, 0)
            self.assertEqual(len(initial["events"]), 1)
            self.assertIsNone(initial["events"][0]["tool"]["output"])

            _write_rows(
                path,
                [
                    {
                        "type": "response_item",
                        "timestamp": "2026-07-09T01:00:02Z",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-1",
                            "output": "done\nexited with code 0",
                        },
                    }
                ],
            )

            legacy = _legacy_delta("codex", path, after=1)
            self.assertEqual(legacy["from"], legacy["total"])
            self.assertEqual(legacy["events"], [])

            fixed = transcripts.read_session_delta("codex", path, initial["cursor"])
            self.assertEqual(fixed["events"], [])
            self.assertEqual(
                fixed["patches"],
                [{"id": initial["events"][0]["id"], "output": "done\nexited with code 0", "ok": True}],
            )

    def test_output_patch_is_emitted_exactly_once(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            _write_rows(
                path,
                [
                    {
                        "type": "response_item",
                        "timestamp": "2026-07-09T01:00:00Z",
                        "payload": {
                            "type": "function_call",
                            "call_id": "call-1",
                            "name": "exec_command",
                            "arguments": "echo hi",
                        },
                    }
                ],
                mode="w",
            )
            initial = transcripts.read_session_delta("codex", path, 0)
            _write_rows(
                path,
                [
                    {
                        "type": "response_item",
                        "timestamp": "2026-07-09T01:00:02Z",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-1",
                            "output": "done\nexited with code 0",
                        },
                    }
                ],
            )

            first = transcripts.read_session_delta("codex", path, initial["cursor"])
            second = transcripts.read_session_delta("codex", path, first["cursor"])

            self.assertEqual(len(first["patches"]), 1)
            self.assertEqual(second["patches"], [])
            self.assertEqual(second["events"], [])

    def test_cursor_is_monotonic_until_a_rewind(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_text("", encoding="utf-8")

            empty = transcripts.read_session_delta("codex", path, 0)
            _write_rows(
                path,
                [
                    {
                        "type": "event_msg",
                        "timestamp": "2026-07-09T01:00:00Z",
                        "payload": {"type": "user_message", "message": "one"},
                    }
                ],
            )
            first = transcripts.read_session_delta("codex", path, empty["cursor"])
            steady = transcripts.read_session_delta("codex", path, first["cursor"])
            _write_rows(
                path,
                [
                    {
                        "type": "event_msg",
                        "timestamp": "2026-07-09T01:00:01Z",
                        "payload": {"type": "user_message", "message": "two"},
                    }
                ],
            )
            second = transcripts.read_session_delta("codex", path, steady["cursor"])

            self.assertEqual([empty["cursor"], first["cursor"], steady["cursor"], second["cursor"]], [0, 1, 1, 2])

    def test_tail_replaced_replays_rewritten_tail(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "claude.jsonl"
            _write_rows(
                path,
                [
                    {
                        "type": "user",
                        "timestamp": "2026-07-09T01:00:00Z",
                        "parentUuid": "parent-1",
                        "message": {"content": "draft 1"},
                    }
                ],
                mode="w",
            )
            first = transcripts.read_session_delta("claude", path, 0)
            first_event = first["events"][0]

            _write_rows(
                path,
                [
                    {
                        "type": "user",
                        "timestamp": "2026-07-09T01:00:01Z",
                        "parentUuid": "parent-1",
                        "message": {"content": "draft 2"},
                    }
                ],
            )
            second = transcripts.read_session_delta("claude", path, first["cursor"])

            self.assertEqual(second["tail_from"], 0)
            self.assertEqual(len(second["events"]), 1)
            self.assertEqual(second["events"][0]["text"], "draft 2")
            self.assertEqual(second["events"][0]["id"], first_event["id"])

    def test_shrink_rewinds_to_a_safe_full_reset(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            _write_rows(
                path,
                [
                    {
                        "type": "event_msg",
                        "timestamp": "2026-07-09T01:00:00Z",
                        "payload": {"type": "user_message", "message": "one"},
                    },
                    {
                        "type": "event_msg",
                        "timestamp": "2026-07-09T01:00:01Z",
                        "payload": {"type": "user_message", "message": "two"},
                    },
                ],
                mode="w",
            )
            first = transcripts.read_session_delta("codex", path, 0)

            _write_rows(
                path,
                [
                    {
                        "type": "event_msg",
                        "timestamp": "2026-07-09T01:00:02Z",
                        "payload": {"type": "user_message", "message": "reset"},
                    }
                ],
                mode="w",
            )
            reset = transcripts.read_session_delta("codex", path, first["cursor"])

            self.assertEqual(reset["tail_from"], 0)
            self.assertEqual([event["text"] for event in reset["events"]], ["reset"])
            self.assertEqual(reset["cursor"], 1)

    def test_agent_session_endpoint_returns_v2_contract(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "rollout.jsonl"
            registry = root / "agent-registry.json"
            queue = root / "queue.json"
            status_dir = root / "status"
            status_dir.mkdir()
            registry.write_text("{}", encoding="utf-8")
            queue.write_text(json.dumps({"WIKI-32": [{"text": "queued", "queued_at": "2026-07-09T01:00:00Z"}]}), encoding="utf-8")
            _write_rows(
                transcript,
                [
                    {
                        "type": "event_msg",
                        "timestamp": "2026-07-09T01:00:00Z",
                        "payload": {"type": "user_message", "message": "hello"},
                    }
                ],
                mode="w",
            )

            with (
                mock.patch.object(main, "AGENT_REGISTRY_PATH", registry),
                mock.patch.object(main, "AGENT_STATUS_DIR", status_dir),
                mock.patch.object(main, "MSG_QUEUE_PATH", queue),
                mock.patch.object(main, "resolve_window", return_value=None),
                mock.patch.dict(main._session_paths, {"WIKI-32": ("codex", transcript)}, clear=True),
            ):
                body = main.agent_session("WIKI-32", cursor=0)

            self.assertEqual(body["version"], 2)
            self.assertEqual(body["tail_from"], 0)
            self.assertEqual(body["cursor"], 1)
            self.assertEqual(body["queue"][0]["text"], "queued")
            self.assertEqual(body["events"][0]["text"], "hello")
            self.assertEqual(body["session_meta"], {})
            self.assertEqual(body["dispositions"], {"rendered": 1, "summarized": 0, "ignored": 0, "unknown": 0})
            self.assertEqual(body["model"], None)
            self.assertEqual(body["kind"], "cdx")
            self.assertEqual(body["provider"], "codex")

    def test_subagent_session_renders_sidechain_events(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            main_transcript = root / "session.jsonl"
            subagents_dir = root / "session" / "subagents"
            subagents_dir.mkdir(parents=True)
            agent_path = subagents_dir / "agent-abc123def456.jsonl"
            _write_rows(main_transcript, [{"type": "user", "message": {"content": "seed"}}], mode="w")
            _write_rows(
                agent_path,
                [
                    {
                        "type": "user",
                        "isSidechain": True,
                        "message": {"content": "subagent prompt"},
                    },
                    {
                        "type": "assistant",
                        "isSidechain": True,
                        "message": {
                            "content": [{"type": "text", "text": "subagent reply"}]
                        },
                    },
                ],
                mode="w",
            )
            registry = root / "agent-registry.json"
            registry.write_text("{}", encoding="utf-8")
            with (
                mock.patch.object(main, "AGENT_REGISTRY_PATH", registry),
                mock.patch.dict(
                    main._session_paths,
                    {"WIKI-44": ("claude", main_transcript)},
                    clear=True,
                ),
            ):
                body = main.subagent_session("WIKI-44", "abc123def456", cursor=0)
            self.assertGreater(
                len(body["events"]),
                0,
                "sidechain rows must render (regression: fmt='claude' filters them out)",
            )

    def test_path_mismatch_forces_full_reset(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.jsonl"
            second = root / "second.jsonl"
            registry = root / "agent-registry.json"
            queue = root / "queue.json"
            status_dir = root / "status"
            status_dir.mkdir()
            registry.write_text("{}", encoding="utf-8")
            queue.write_text("{}", encoding="utf-8")
            _write_rows(
                first,
                [
                    {
                        "type": "event_msg",
                        "timestamp": f"2026-07-09T01:00:0{index}Z",
                        "payload": {"type": "user_message", "message": f"old-{index}"},
                    }
                    for index in range(3)
                ],
                mode="w",
            )
            _write_rows(
                second,
                [
                    {
                        "type": "event_msg",
                        "timestamp": f"2026-07-09T01:01:0{index}Z",
                        "payload": {"type": "user_message", "message": f"new-{index}"},
                    }
                    for index in range(8)
                ],
                mode="w",
            )

            with (
                mock.patch.object(main, "AGENT_REGISTRY_PATH", registry),
                mock.patch.object(main, "AGENT_STATUS_DIR", status_dir),
                mock.patch.object(main, "MSG_QUEUE_PATH", queue),
                mock.patch.object(main, "resolve_window", return_value=None),
                mock.patch.dict(main._session_paths, {"WIKI-32": ("codex", second)}, clear=True),
            ):
                body = main.agent_session("WIKI-32", cursor=3, client_path=str(first))

            self.assertEqual(body["path"], str(second))
            self.assertEqual(body["tail_from"], 0)
            self.assertEqual(body["cursor"], 8)
            self.assertEqual(len(body["events"]), 8)
            self.assertEqual(body["events"][0]["text"], "new-0")

    def test_concurrent_readers_do_not_double_apply_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_text("", encoding="utf-8")
            transcripts.read_session_delta("codex", path, 0)
            _write_rows(
                path,
                [
                    {
                        "type": "event_msg",
                        "timestamp": "2026-07-09T01:00:00Z",
                        "payload": {"type": "user_message", "message": "one"},
                    }
                ],
                mode="w",
            )

            original_open = Path.open
            first_read_started = threading.Event()
            slowed = {"done": False}

            class SlowHandle:
                def __init__(self, inner):
                    self._inner = inner

                def read(self, *args, **kwargs):
                    if not slowed["done"]:
                        slowed["done"] = True
                        first_read_started.set()
                        time.sleep(0.1)
                    return self._inner.read(*args, **kwargs)

                def __getattr__(self, name):
                    return getattr(self._inner, name)

                def __enter__(self):
                    self._inner.__enter__()
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return self._inner.__exit__(exc_type, exc, tb)

            def patched_open(self, *args, **kwargs):
                handle = original_open(self, *args, **kwargs)
                if self == path:
                    return SlowHandle(handle)
                return handle

            results: list[dict] = []
            errors: list[Exception] = []

            def worker() -> None:
                try:
                    results.append(transcripts.read_session_delta("codex", path, 0))
                except Exception as exc:  # pragma: no cover - test should stay green
                    errors.append(exc)

            with mock.patch.object(Path, "open", patched_open):
                first = threading.Thread(target=worker)
                second = threading.Thread(target=worker)
                first.start()
                self.assertTrue(first_read_started.wait(timeout=1))
                second.start()
                first.join(timeout=1)
                second.join(timeout=1)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertFalse(errors)
            self.assertEqual(len(results), 2)
            self.assertEqual(len(transcripts._cache[str(path)]["events"]), 1)
            self.assertEqual(transcripts._cache[str(path)]["cursor"], 1)
            self.assertTrue(all(len(result["events"]) == 1 for result in results))
            self.assertTrue(all(result["events"][0]["text"] == "one" for result in results))

    def test_orchestrator_direct_codex_transcript_is_detected(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "rollout.jsonl"
            registry = root / "agent-registry.json"
            queue = root / "queue.json"
            status_dir = root / "status"
            status_dir.mkdir()
            registry.write_text(
                json.dumps(
                    {
                        "_orchestrators": {
                            "WIKI-32": {
                                "window": "@9999",
                                "model": "gpt-5.6",
                                "spawned_at": "2026-07-09T01:00:00Z",
                                "transcript": str(transcript),
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            queue.write_text("{}", encoding="utf-8")
            _write_rows(
                transcript,
                [
                    {
                        "type": "event_msg",
                        "timestamp": "2026-07-09T01:00:00Z",
                        "payload": {"type": "user_message", "message": "hello"},
                    },
                    {
                        "type": "event_msg",
                        "timestamp": "2026-07-09T01:00:01Z",
                        "payload": {"type": "agent_message", "message": "world"},
                    },
                ],
                mode="w",
            )

            with (
                mock.patch.object(main, "AGENT_REGISTRY_PATH", registry),
                mock.patch.object(main, "AGENT_STATUS_DIR", status_dir),
                mock.patch.object(main, "MSG_QUEUE_PATH", queue),
                mock.patch.object(main, "resolve_window", return_value=None),
                mock.patch.dict(main._session_paths, {}, clear=True),
            ):
                body = main.agent_session("WIKI-32", cursor=0)

            self.assertEqual(body["format"], "codex")
            self.assertEqual(body["cursor"], 2)
            self.assertEqual([event["text"] for event in body["events"]], ["hello", "world"])
            self.assertEqual(body["model"], "gpt-5.6")
            self.assertEqual(body["kind"], "cdx")
            self.assertEqual(body["provider"], "codex")

    def test_archived_pane_log_exposes_model_from_meta(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_dir = root / "archive"
            session_dir = archive_dir / "WIKI-55" / "20260710-010203"
            session_dir.mkdir(parents=True)
            (session_dir / "worker.log").write_text("worker output\n", encoding="utf-8")
            (session_dir / "meta.json").write_text(
                json.dumps(
                    {
                        "worker": {
                            "kind": "cc",
                            "provider": "claude",
                            "model": "opus-4.8",
                        }
                    }
                ),
                encoding="utf-8",
            )
            registry = root / "agent-registry.json"
            queue = root / "queue.json"
            status_dir = root / "status"
            status_dir.mkdir()
            registry.write_text("{}", encoding="utf-8")
            queue.write_text("{}", encoding="utf-8")

            with (
                mock.patch.object(main, "AGENT_REGISTRY_PATH", registry),
                mock.patch.object(main, "AGENT_ARCHIVE_DIR", archive_dir),
                mock.patch.object(main, "AGENT_STATUS_DIR", status_dir),
                mock.patch.object(main, "MSG_QUEUE_PATH", queue),
                mock.patch.object(main, "resolve_window", return_value=None),
                mock.patch.object(main.transcripts, "find_session", return_value=None),
                mock.patch.dict(main._session_paths, {}, clear=True),
            ):
                body = main.agent_session("WIKI-55", cursor=0)

            self.assertEqual(body["format"], "pane-log")
            self.assertEqual(body["model"], "opus-4.8")
            self.assertEqual(body["kind"], "cc")
            self.assertEqual(body["provider"], "claude")

    def test_session_path_invalidation_drops_only_changed_tickets(self) -> None:
        with mock.patch.dict(
            main._session_paths,
            {
                "WIKI-46": ("claude", Path("/tmp/old.jsonl")),
                "WIKI-99": ("codex", Path("/tmp/keep.jsonl")),
            },
            clear=True,
        ):
            main._invalidate_session_paths({"WIKI-46"})
            self.assertNotIn("WIKI-46", main._session_paths)
            self.assertIn("WIKI-99", main._session_paths)

    def test_registry_handoff_re_resolves_after_cache_invalidation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_path = root / "old.jsonl"
            new_path = root / "new.jsonl"
            registry = root / "agent-registry.json"
            queue = root / "queue.json"
            status_dir = root / "status"
            status_dir.mkdir()
            queue.write_text("{}", encoding="utf-8")
            _write_rows(
                old_path,
                [
                    {
                        "type": "user",
                        "timestamp": "2026-07-09T01:00:00Z",
                        "message": {"content": "old transcript"},
                    }
                ],
                mode="w",
            )
            _write_rows(
                new_path,
                [
                    {
                        "type": "event_msg",
                        "timestamp": "2026-07-09T01:00:01Z",
                        "payload": {"type": "user_message", "message": "new transcript"},
                    }
                ],
                mode="w",
            )
            previous = {
                "WIKI-46": {
                    "current": {
                        "kind": "cc",
                        "spawned_at": "2026-07-09T01:00:00Z",
                        "session_id": "old-session",
                    }
                }
            }
            current = {
                "WIKI-46": {
                    "current": {
                        "kind": "cdx",
                        "spawned_at": "2026-07-09T01:00:00Z",
                        "session_id": "new-session",
                    }
                }
            }
            registry.write_text(json.dumps(previous), encoding="utf-8")

            def fake_find_session(kind: str | None, ticket: str, spawned_at: str | None, session_id: str | None = None, worktree: str | None = None):
                if kind == "cc":
                    return ("claude", old_path)
                if kind == "cdx":
                    return ("codex", new_path)
                return None

            with (
                mock.patch.object(main, "AGENT_REGISTRY_PATH", registry),
                mock.patch.object(main, "AGENT_STATUS_DIR", status_dir),
                mock.patch.object(main, "MSG_QUEUE_PATH", queue),
                mock.patch.object(main, "resolve_window", return_value=None),
                mock.patch.object(main.transcripts, "find_session", side_effect=fake_find_session) as find_session,
                mock.patch.dict(main._session_paths, {"WIKI-46": ("claude", old_path)}, clear=True),
            ):
                first = main.agent_session("WIKI-46", cursor=0)
                registry.write_text(json.dumps(current), encoding="utf-8")
                main._invalidate_session_paths(main._changed_registry_tickets(previous, current))
                second = main.agent_session("WIKI-46", cursor=0)

            self.assertEqual(first["path"], str(old_path))
            self.assertEqual(second["path"], str(new_path))
            self.assertEqual([event["text"] for event in first["events"]], ["old transcript"])
            self.assertEqual([event["text"] for event in second["events"]], ["new transcript"])
            self.assertEqual(find_session.call_args_list[-1].args[0], "cdx")


if __name__ == "__main__":
    unittest.main()
