from __future__ import annotations

import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from fastapi import HTTPException

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

    def _cached_events(self, root: Path, count: int) -> tuple[Path, dict]:
        path = root / "large.jsonl"
        path.touch()
        state = transcripts._new_parse_state("codex")
        state["events"] = [
            {
                "id": index,
                "kind": "assistant",
                "ts": None,
                "text": f"event-{index}",
                "disposition": "rendered",
            }
            for index in range(count)
        ]
        state["cursor"] = count
        state["next_event_id"] = count
        transcripts._cache[str(path)] = state
        return path, state

    def test_full_reset_returns_tail_window_with_older_flag(self) -> None:
        with TemporaryDirectory() as tmp, mock.patch.object(transcripts, "TAIL_WINDOW_EVENTS", 3):
            path, state = self._cached_events(Path(tmp), 5)

            result = transcripts.read_session_delta("codex", path, 0)

            state["events"][2]["tool"] = {"output": None, "ok": None}
            isolated = transcripts.read_session_delta("codex", path, 0)
            state["events"][2]["text"] = "mutated after read"
            state["events"][2]["tool"]["output"] = "late output"

            self.assertEqual(result["base"], 2)
            self.assertEqual(result["tail_from"], 2)
            self.assertEqual([event["id"] for event in result["events"]], [2, 3, 4])
            self.assertTrue(result["has_older"])
            self.assertIsNot(result["events"][0], state["events"][2])
            self.assertEqual(isolated["events"][0]["text"], "event-2")
            self.assertIsNone(isolated["events"][0]["tool"]["output"])

    def test_full_reset_returns_all_events_without_older_flag(self) -> None:
        with TemporaryDirectory() as tmp, mock.patch.object(transcripts, "TAIL_WINDOW_EVENTS", 5):
            path, _state = self._cached_events(Path(tmp), 3)

            result = transcripts.read_session_delta("codex", path, 0)

            self.assertEqual(result["base"], 0)
            self.assertEqual([event["id"] for event in result["events"]], [0, 1, 2])
            self.assertFalse(result["has_older"])

    def test_read_older_session_returns_slice_and_retention_boundary(self) -> None:
        with TemporaryDirectory() as tmp:
            path, _state = self._cached_events(Path(tmp), 6)

            page = transcripts.read_older_session("codex", path, before=5, count=2)
            first_page = transcripts.read_older_session("codex", path, before=2, count=2)

            self.assertEqual(page["base"], 3)
            self.assertEqual([event["id"] for event in page["events"]], [3, 4])
            self.assertTrue(page["has_older"])
            self.assertEqual(first_page["base"], 0)
            self.assertEqual([event["id"] for event in first_page["events"]], [0, 1])
            self.assertFalse(first_page["has_older"])

    def test_claude_annotation_does_not_mutate_cached_events(self) -> None:
        event = {
            "id": 1,
            "kind": "tool",
            "tool": {"name": "Agent", "prompt_head": "delegate this"},
        }
        with mock.patch.object(
            transcripts,
            "list_subagents",
            return_value=[{"id": "abc12345", "prompt_head": "delegate this"}],
        ):
            annotated = transcripts.annotate_agent_events(Path("session.jsonl"), [event])

        self.assertNotIn("agent_id", event["tool"])
        self.assertEqual(annotated[0]["tool"]["agent_id"], "abc12345")
        self.assertIsNot(annotated[0], event)

    def test_claude_annotation_matches_duplicate_prompt_heads_in_order(self) -> None:
        # WIKI-244 review: retried/repeated prompts share a head. The Nth
        # parent must map to the Nth child (ordered by start time); a parent
        # beyond the child count must stay unannotated instead of reusing a
        # sibling's child transcript.
        events = [
            {
                "id": index,
                "kind": "tool",
                "ts": f"2026-08-05T12:0{index}:00Z",
                "tool": {"name": "Task", "prompt_head": "explore the code"},
            }
            for index in range(3)
        ]
        children = [
            {"id": "bbb22222", "prompt_head": "explore the code", "started_at": "2026-08-05T12:01:10Z"},
            {"id": "aaa11111", "prompt_head": "explore the code", "started_at": "2026-08-05T12:00:10Z"},
        ]
        with mock.patch.object(transcripts, "list_subagents", return_value=children):
            annotated = transcripts.annotate_agent_events(Path("session-dup.jsonl"), events, assignments={})

        self.assertEqual(annotated[0]["tool"]["agent_id"], "aaa11111")
        self.assertEqual(annotated[1]["tool"]["agent_id"], "bbb22222")
        self.assertNotIn("agent_id", annotated[2]["tool"])

    def test_claude_annotation_survives_duplicate_parents_split_across_deltas(self) -> None:
        # WIKI-244 review round 2 (H1): a later delta window that carries only
        # the second duplicate parent must not restart the 1:1 counter and
        # re-map that parent onto the first parent's child.
        store = {}
        children = [
            {"id": "aaa11111", "prompt_head": "explore the code", "started_at": "2026-08-05T12:00:10Z"},
            {"id": "bbb22222", "prompt_head": "explore the code", "started_at": "2026-08-05T12:05:10Z"},
        ]
        first_parent = {
            "id": 4,
            "kind": "tool",
            "ts": "2026-08-05T12:00:00Z",
            "tool": {"name": "Task", "prompt_head": "explore the code"},
        }
        second_parent = {
            "id": 9,
            "kind": "tool",
            "ts": "2026-08-05T12:05:00Z",
            "tool": {"name": "Task", "prompt_head": "explore the code"},
        }
        with mock.patch.object(transcripts, "list_subagents", return_value=children):
            first = transcripts.annotate_agent_events(Path("session-split.jsonl"), [first_parent], assignments=store)
            # Second delta carries ONLY the later duplicate parent.
            second = transcripts.annotate_agent_events(Path("session-split.jsonl"), [second_parent], assignments=store)
            # Re-annotating the first parent (older-page refetch) keeps its child.
            refetched = transcripts.annotate_agent_events(Path("session-split.jsonl"), [first_parent], assignments=store)

        self.assertEqual(first[0]["tool"]["agent_id"], "aaa11111")
        self.assertEqual(second[0]["tool"]["agent_id"], "bbb22222")
        self.assertEqual(refetched[0]["tool"]["agent_id"], "aaa11111")

    def test_claude_annotation_split_deltas_arriving_out_of_order(self) -> None:
        # The timestamp anchor makes the mapping independent of arrival order:
        # even when the LATER parent is annotated first, it claims the child
        # that started after its own timestamp, leaving the earlier child for
        # the earlier parent.
        store = {}
        children = [
            {"id": "aaa11111", "prompt_head": "explore the code", "started_at": "2026-08-05T12:00:10Z"},
            {"id": "bbb22222", "prompt_head": "explore the code", "started_at": "2026-08-05T12:05:10Z"},
        ]
        early_parent = {
            "id": 4,
            "kind": "tool",
            "ts": "2026-08-05T12:00:00Z",
            "tool": {"name": "Task", "prompt_head": "explore the code"},
        }
        late_parent = {
            "id": 9,
            "kind": "tool",
            "ts": "2026-08-05T12:05:00Z",
            "tool": {"name": "Task", "prompt_head": "explore the code"},
        }
        with mock.patch.object(transcripts, "list_subagents", return_value=children):
            late = transcripts.annotate_agent_events(Path("session-order.jsonl"), [late_parent], assignments=store)
            early = transcripts.annotate_agent_events(Path("session-order.jsonl"), [early_parent], assignments=store)

        self.assertEqual(late[0]["tool"]["agent_id"], "bbb22222")
        self.assertEqual(early[0]["tool"]["agent_id"], "aaa11111")

    def test_claude_annotation_drops_stale_assignment_on_prompt_change(self) -> None:
        # WIKI-244 review round 4 (M2): a cached parent-id assignment must be
        # re-validated against the current child list. If the parent at that
        # id now carries a different prompt (id reuse after a rewrite), the
        # stale child link is dropped and re-resolved by head.
        store = {5: "old11111"}
        children = [
            {"id": "old11111", "prompt_head": "old work", "started_at": "2026-08-05T12:00:10Z"},
            {"id": "new22222", "prompt_head": "different work", "started_at": "2026-08-05T12:05:10Z"},
        ]
        parent = {
            "id": 5,
            "kind": "tool",
            "ts": "2026-08-05T12:05:00Z",
            "tool": {"name": "Task", "prompt_head": "different work"},
        }
        with mock.patch.object(transcripts, "list_subagents", return_value=children):
            annotated = transcripts.annotate_agent_events(Path("session-stale.jsonl"), [parent], assignments=store)

        self.assertEqual(annotated[0]["tool"]["agent_id"], "new22222")
        self.assertEqual(store, {5: "new22222"})

    def test_claude_annotation_store_prunes_below_retained_base(self) -> None:
        # Assignments for parents that fell below the retained event base can
        # never be referenced again; they must not accumulate for the backend
        # lifetime.
        path = Path("session-prune.jsonl")
        state = transcripts._new_parse_state("claude")
        state["base"] = 10
        state["agent_child_assignments"] = {3: "aaa11111", 12: "bbb22222"}
        transcripts._cache[str(path)] = state

        store = transcripts._agent_assignment_store(path)

        self.assertEqual(store, {12: "bbb22222"})
        self.assertIs(store, state["agent_child_assignments"])

    def test_claude_annotation_resets_with_parse_state_on_transcript_shrink(self) -> None:
        # WIKI-244 review round 4 (M2): a shrunk/rewritten transcript rebuilds
        # the parse state; the assignment map must die with it so a reused
        # path+event-id with a NEW prompt maps to the new child, never the old
        # one.
        long_prompt = "map the entire legacy billing pipeline and list every consumer of the invoice generator"
        short_prompt = "check disk usage"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "session.jsonl"
            subdir = root / "session" / "subagents"
            subdir.mkdir(parents=True)
            _write_rows(
                subdir / "agent-aaaa1111.jsonl",
                [{"type": "user", "timestamp": "2026-08-05T12:00:10Z", "message": {"content": long_prompt}}],
                mode="w",
            )
            _write_rows(
                path,
                [
                    {
                        "type": "assistant",
                        "timestamp": "2026-08-05T12:00:00Z",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "tool_use", "id": "t1", "name": "Task", "input": {"prompt": long_prompt}},
                            ],
                        },
                    },
                ],
                mode="w",
            )
            first = transcripts.read_session_delta("claude", path, 0, tail_window=False)
            annotated = transcripts.annotate_agent_events(path, first["events"])
            tool_events = [event for event in annotated if event.get("kind") == "tool"]
            self.assertEqual(tool_events[0]["tool"]["agent_id"], "aaaa1111")
            state = transcripts._cache[str(path)]
            self.assertTrue(state["agent_child_assignments"], "assignment must live in the parse state")

            # Rewrite the transcript SMALLER with a different prompt at the
            # same event id, and add the matching new child transcript.
            _write_rows(
                subdir / "agent-bbbb2222.jsonl",
                [{"type": "user", "timestamp": "2026-08-05T12:10:10Z", "message": {"content": short_prompt}}],
                mode="w",
            )
            _write_rows(
                path,
                [
                    {
                        "type": "assistant",
                        "timestamp": "2026-08-05T12:10:00Z",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "tool_use", "id": "t2", "name": "Task", "input": {"prompt": short_prompt}},
                            ],
                        },
                    },
                ],
                mode="w",
            )
            second = transcripts.read_session_delta("claude", path, 0, tail_window=False)
            reannotated = transcripts.annotate_agent_events(path, second["events"])
            new_tools = [event for event in reannotated if event.get("kind") == "tool"]
            self.assertEqual(new_tools[0]["tool"]["agent_id"], "bbbb2222")
            new_state = transcripts._cache[str(path)]
            self.assertIsNot(new_state, state, "shrink must rebuild the parse state")
            self.assertNotIn("aaaa1111", new_state["agent_child_assignments"].values())

    def test_models_endpoint_includes_new_codex_and_claude_options(self) -> None:
        payload = main.list_models()
        models = {model["id"]: model for model in payload["models"]}

        self.assertNotIn("gpt-5.6", models)
        self.assertEqual(
            ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
            [payload["models"][index]["id"] for index in range(3)],
        )
        self.assertEqual(models["gpt-5.6-sol"]["kind"], "cdx")
        self.assertIn("opus-4.7", models)
        self.assertEqual(models["opus-4.7"]["kind"], "cc")
        self.assertIn("claude-fable-5", models)
        self.assertEqual(models["claude-fable-5"]["kind"], "cc")

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
                [
                    {
                        "id": initial["events"][0]["id"],
                        "output": "done\nexited with code 0",
                        "ok": True,
                        "completed_at": "2026-07-09T01:00:02Z",
                    }
                ],
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

    def test_agent_session_older_endpoint_returns_requested_page(self) -> None:
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
                            "WIKI-94": {
                                "window": "@9999",
                                "spawned_at": "2026-07-13T00:00:00Z",
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
                        "timestamp": f"2026-07-13T00:00:0{index}Z",
                        "payload": {"type": "user_message", "message": f"event-{index}"},
                    }
                    for index in range(5)
                ],
                mode="w",
            )

            with (
                mock.patch.object(main, "AGENT_REGISTRY_PATH", registry),
                mock.patch.object(main, "AGENT_STATUS_DIR", status_dir),
                mock.patch.object(main, "MSG_QUEUE_PATH", queue),
                mock.patch.object(main, "resolve_window", return_value=None),
                mock.patch.object(transcripts, "TAIL_WINDOW_EVENTS", 2),
                mock.patch.dict(main._session_paths, {}, clear=True),
            ):
                initial = main.agent_session("WIKI-94", cursor=0)
                older = main.agent_session_older("WIKI-94", before=initial["base"], count=2)

            self.assertEqual(initial["base"], 3)
            self.assertTrue(initial["has_older"])
            self.assertEqual(older["base"], 1)
            self.assertEqual([event["text"] for event in older["events"]], ["event-1", "event-2"])
            self.assertTrue(older["has_older"])

    def test_archived_session_prefers_provider_native_transcript(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "rollout.jsonl"
            registry = root / "agent-registry.json"
            queue = root / "queue.json"
            archive_root = root / "archive"
            session_dir = archive_root / "WIKI-99" / "20260713-010203"
            session_dir.mkdir(parents=True)
            registry.write_text("{}", encoding="utf-8")
            queue.write_text("{}", encoding="utf-8")
            _write_rows(
                transcript,
                [
                    {
                        "type": "event_msg",
                        "timestamp": "2026-07-13T00:00:00Z",
                        "payload": {
                            "type": "agent_message",
                            "message": "provider-native",
                        },
                    }
                ],
                mode="w",
            )
            _write_rows(
                session_dir / "events.jsonl",
                [
                    {
                        "seq": 1,
                        "raw_seq": 1,
                        "normalized_at": "2026-07-13T00:00:00Z",
                        "disposition": "rendered",
                        "kind": "item_completed",
                        "payload": {
                            "method": "item/completed",
                            "params": {
                                "item": {
                                    "type": "agentMessage",
                                    "text": "archived-normalized",
                                }
                            },
                        },
                    }
                ],
                mode="w",
            )
            (session_dir / "run.json").write_text(
                json.dumps({"provider": "codex", "model": "gpt-5.6-sol"}),
                encoding="utf-8",
            )

            with (
                mock.patch.object(main, "AGENT_REGISTRY_PATH", registry),
                mock.patch.object(main, "AGENT_ARCHIVE_DIR", archive_root),
                mock.patch.object(main, "MSG_QUEUE_PATH", queue),
                mock.patch.object(main, "resolve_window", return_value=None),
                mock.patch.object(
                    transcripts,
                    "find_session",
                    return_value=("codex", transcript),
                ),
                mock.patch.dict(main._session_paths, {}, clear=True),
            ):
                body = main.agent_session("WIKI-99", cursor=0)

            self.assertEqual(body["format"], "codex")
            self.assertEqual(body["path"], str(transcript))
            self.assertEqual([event["text"] for event in body["events"]], ["provider-native"])

    def test_archived_normalized_session_is_structured_and_pages_older_events(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "agent-registry.json"
            archive_root = root / "archive"
            session_dir = archive_root / "WIKI-99" / "20260713-010203"
            session_dir.mkdir(parents=True)
            registry.write_text("{}", encoding="utf-8")
            rows = []
            for index in range(5):
                item_type = "userMessage" if index % 2 == 0 else "agentMessage"
                item = {"type": item_type}
                if item_type == "userMessage":
                    item["content"] = [{"type": "text", "text": f"event-{index}"}]
                else:
                    item["text"] = f"event-{index}"
                rows.append(
                    {
                        "seq": index + 1,
                        "raw_seq": index + 1,
                        "normalized_at": f"2026-07-13T00:00:0{index}Z",
                        "disposition": "rendered",
                        "kind": "item_completed",
                        "payload": {
                            "method": "item/completed",
                            "params": {"item": item},
                        },
                    }
                )
            _write_rows(session_dir / "events.jsonl", rows, mode="w")
            (session_dir / "run.json").write_text(
                json.dumps(
                    {
                        "provider": "codex",
                        "model": "gpt-5.6-sol",
                        "desired_model": None,
                    }
                ),
                encoding="utf-8",
            )
            (session_dir / "meta.json").write_text(
                json.dumps({"worker": {"kind": "cdx"}}),
                encoding="utf-8",
            )

            with (
                mock.patch.object(main, "AGENT_REGISTRY_PATH", registry),
                mock.patch.object(main, "AGENT_ARCHIVE_DIR", archive_root),
                mock.patch.object(main.transcripts, "TAIL_WINDOW_EVENTS", 2),
                mock.patch.object(transcripts, "find_session", return_value=None),
                mock.patch.dict(main._session_paths, {}, clear=True),
            ):
                initial = main.agent_session("WIKI-99", cursor=0)
                older = main.agent_session_older(
                    "WIKI-99",
                    before=initial["base"],
                    count=2,
                )

            self.assertEqual(initial["format"], "provider-events")
            self.assertEqual(initial["base"], 3)
            self.assertEqual(initial["tail_from"], 3)
            self.assertEqual(initial["cursor"], 5)
            self.assertTrue(initial["has_older"])
            self.assertFalse(initial["working"])
            self.assertEqual(initial["queue"], [])
            self.assertEqual(initial["model"], "gpt-5.6-sol")
            self.assertEqual(initial["kind"], "cdx")
            self.assertEqual(initial["provider"], "codex")
            self.assertEqual(
                [(event["kind"], event["text"]) for event in initial["events"]],
                [("assistant", "event-3"), ("user", "event-4")],
            )
            self.assertEqual(older["format"], "provider-events")
            self.assertEqual(older["base"], 1)
            self.assertEqual(
                [(event["kind"], event["text"]) for event in older["events"]],
                [("assistant", "event-1"), ("user", "event-2")],
            )
            self.assertTrue(older["has_older"])

    def test_archived_session_without_transcript_events_or_log_is_404(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "agent-registry.json"
            archive_root = root / "archive"
            (archive_root / "WIKI-99" / "20260713-010203").mkdir(parents=True)
            registry.write_text("{}", encoding="utf-8")

            with (
                mock.patch.object(main, "AGENT_REGISTRY_PATH", registry),
                mock.patch.object(main, "AGENT_ARCHIVE_DIR", archive_root),
                mock.patch.object(transcripts, "find_session", return_value=None),
                mock.patch.dict(main._session_paths, {}, clear=True),
                self.assertRaises(HTTPException) as missing,
            ):
                main.agent_session("WIKI-99", cursor=0)

            self.assertEqual(missing.exception.status_code, 404)

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
                with mock.patch.object(transcripts, "TAIL_WINDOW_EVENTS", 1):
                    body = main.subagent_session("WIKI-44", "abc123def456", cursor=0)
            self.assertEqual(
                len(body["events"]),
                2,
                "subagent sessions must stay complete because they have no older-page UI",
            )
            self.assertFalse(body["has_older"])

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
                                "model": "gpt-5.6-sol",
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
            self.assertEqual(body["model"], "gpt-5.6-sol")
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
                            "model": "opus-4.7",
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
            self.assertEqual(body["model"], "opus-4.7")
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
