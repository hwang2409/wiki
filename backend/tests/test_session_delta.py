from __future__ import annotations

import json
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


if __name__ == "__main__":
    unittest.main()
