"""Focused tests for the WIKI-19 transcript resolver additions.

Only covers the pieces that changed: registry session_id preference and the
same-cwd chain rule after `codex resume`. Other transcripts.py behavior is
covered by manual QA on live rollouts.
"""

from __future__ import annotations

import json
import os
import time
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from backend.app import transcripts
from backend.app.agent_runtime.normalizer import normalize_provider_event
from backend.app.wiki_artifacts import TEXT_LIMIT, sentinel_text

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _artifact_protocol_event(kind: str, index: int) -> dict:
    artifact_id = f"00000000-0000-4000-8000-{index:012d}"
    payloads = {
        "mermaid": {"source": "graph TD; A-->B"},
        "svg": {"source": "<svg><rect width='1' height='1'/></svg>"},
        "image": {"ref": f"artifact://{artifact_id}", "mime": "image/png", "byte_size": 12},
        "table": {
            "columns": [{"key": "id", "label": "ID", "type": "number"}],
            "rows": [[1]],
        },
        "plot": {"spec_vega_lite": {"mark": "point"}},
        "code": {"language": "python", "source": "print(1)"},
    }
    return {
        "kind": "artifact",
        "id": artifact_id,
        "title": f"Fixture {kind}",
        "caption": "Fixture caption",
        "artifact": {"kind": kind, **payloads[kind]},
        "ts": "2026-07-13T12:00:00+00:00",
    }


class ArtifactTranscriptTests(unittest.TestCase):
    def setUp(self) -> None:
        transcripts._cache.clear()

    def test_only_wiki_render_artifact_tool_names_are_accepted(self) -> None:
        self.assertTrue(transcripts._is_artifact_tool("render_artifact"))
        self.assertTrue(
            transcripts._is_artifact_tool("mcp__wiki_artifacts__render_artifact")
        )
        self.assertFalse(transcripts._is_artifact_tool("other_server__render_artifact"))

    def test_codex_artifact_result_error_shapes_mark_completion_failed(self) -> None:
        input_payload = {
            "kind": "mermaid",
            "payload": {"source": "graph TD; A-->B"},
        }
        outputs = [
            {"error": "bad", "is_error": True},
            {"error": "bad", "isError": True},
            {"error": "bad", "failed": True},
            {"error": "bad", "exit_code": 1},
        ]
        for index, output in enumerate(outputs):
            with self.subTest(index=index), TemporaryDirectory() as tmp:
                call_id = f"artifact-error-{index}"
                rows = [
                    {
                        "type": "response_item",
                        "timestamp": "2026-08-07T12:00:00Z",
                        "payload": {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": "mcp__wiki_artifacts__render_artifact",
                            "arguments": json.dumps(input_payload),
                        },
                    },
                    {
                        "type": "response_item",
                        "timestamp": "2026-08-07T12:00:01Z",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": output,
                        },
                    },
                ]
                path = Path(tmp) / "codex-artifact-error.jsonl"
                path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
                parsed = transcripts.read_session_events("codex", path)

            self.assertEqual(len(parsed["events"]), 1)
            self.assertEqual(parsed["events"][0]["kind"], "tool")
            self.assertFalse(parsed["events"][0]["tool"]["ok"])

    def test_codex_and_claude_artifact_tools_emit_specialized_events(self) -> None:
        kinds = ["mermaid", "svg", "image", "table", "plot", "code"]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, kind in enumerate(kinds, start=1):
                protocol_event = _artifact_protocol_event(kind, index)
                call_id = f"artifact-{index}"
                tool_input = {
                    "kind": kind,
                    "title": protocol_event["title"],
                    "caption": protocol_event["caption"],
                    "payload": {"source": "fixture"},
                }
                if index % 2:
                    path = root / f"codex-{kind}.jsonl"
                    rows = [
                        {
                            "type": "response_item",
                            "timestamp": "2026-07-13T12:00:00Z",
                            "payload": {
                                "type": "function_call",
                                "call_id": call_id,
                                "name": "mcp__wiki_artifacts__render_artifact",
                                "arguments": json.dumps(tool_input),
                            },
                        },
                        {
                            "type": "response_item",
                            "timestamp": "2026-07-13T12:00:01Z",
                            "payload": {
                                "type": "function_call_output",
                                "call_id": call_id,
                                "output": sentinel_text(protocol_event),
                            },
                        },
                    ]
                    fmt = "codex"
                else:
                    path = root / f"claude-{kind}.jsonl"
                    rows = [
                        {
                            "type": "assistant",
                            "timestamp": "2026-07-13T12:00:00Z",
                            "message": {
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": call_id,
                                        "name": "mcp__wiki-artifacts__render_artifact",
                                        "input": tool_input,
                                    }
                                ]
                            },
                        },
                        {
                            "type": "user",
                            "timestamp": "2026-07-13T12:00:01Z",
                            "message": {
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": call_id,
                                        "content": sentinel_text(protocol_event),
                                    }
                                ]
                            },
                        },
                    ]
                    fmt = "claude"
                path.write_text(
                    "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
                )
                parsed = transcripts.read_session_events(fmt, path)
                self.assertEqual(len(parsed["events"]), 1, kind)
                event = parsed["events"][0]
                self.assertEqual(event["kind"], "artifact")
                self.assertEqual(event["artifact_id"], protocol_event["id"])
                self.assertEqual(event["artifact"], protocol_event["artifact"])
                self.assertEqual(event["title"], f"Fixture {kind}")

    def test_artifact_events_dedupe_by_artifact_id(self) -> None:
        protocol_event = _artifact_protocol_event("mermaid", 99)
        rows = []
        for index in range(2):
            call_id = f"duplicate-artifact-{index}"
            rows.extend(
                [
                    {
                        "type": "response_item",
                        "timestamp": f"2026-07-13T12:00:0{index}Z",
                        "payload": {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": "mcp__wiki_artifacts__render_artifact",
                            "arguments": json.dumps(
                                {
                                    "kind": "mermaid",
                                    "payload": {"source": "graph TD; A-->B"},
                                }
                            ),
                        },
                    },
                    {
                        "type": "response_item",
                        "timestamp": f"2026-07-13T12:00:1{index}Z",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": sentinel_text(protocol_event),
                        },
                    },
                ]
            )
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))

            parsed = transcripts.read_session_events("codex", path)

        self.assertEqual(
            [event for event in parsed["events"] if event["kind"] == "artifact"],
            [
                {
                    **{
                        "kind": "artifact",
                        "ts": protocol_event["ts"],
                        "text": protocol_event["title"],
                        "artifact_id": protocol_event["id"],
                        "title": protocol_event["title"],
                        "caption": protocol_event["caption"],
                        "artifact": protocol_event["artifact"],
                    },
                    "disposition": "rendered",
                    "id": 0,
                }
            ],
        )

    def test_completed_unparseable_artifact_is_not_rejected(self) -> None:
        call_id = "completed-without-sentinel"
        rows = [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": "mcp__wiki_artifacts__render_artifact",
                    "arguments": json.dumps(
                        {"kind": "mermaid", "payload": {"source": "graph TD; A-->B"}}
                    ),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": "artifact rendered",
                },
            },
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))

            parsed = transcripts.read_session_events("codex", path)

        event = parsed["events"][0]
        self.assertEqual(event["kind"], "tool")
        self.assertTrue(event["tool"]["ok"])
        self.assertEqual(
            event["tool"]["summary"],
            "render_artifact completed without a parseable artifact",
        )

    def test_codex_tool_retains_result_completion_time(self) -> None:
        rows = [
            {
                "type": "response_item",
                "timestamp": "2026-08-02T12:00:01Z",
                "payload": {
                    "type": "function_call",
                    "call_id": "call-with-late-result",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "npm test"}),
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-08-02T12:00:04Z",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-with-late-result",
                    "output": "tests passed\nexited with code 0",
                },
            },
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex-result-time.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            parsed = transcripts.read_session_events("codex", path)

        tool = parsed["events"][0]["tool"]
        self.assertEqual(tool["completed_at"], "2026-08-02T12:00:04Z")

    def test_codex_event_message_tool_retains_result_completion_time(self) -> None:
        rows = [
            {
                "type": "response_item",
                "timestamp": "2026-08-02T12:00:01Z",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "call-event-message-result",
                    "name": "mcp__fixture__read",
                    "input": "fixture input",
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-02T12:00:05Z",
                "payload": {
                    "type": "mcp_tool_call_end",
                    "call_id": "call-event-message-result",
                    "result": {
                        "Err": {
                            "content": [{"type": "text", "text": "fixture failed"}],
                        },
                    },
                },
            },
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex-event-message-result-time.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            parsed = transcripts.read_session_events("codex", path)

        tool = parsed["events"][0]["tool"]
        self.assertEqual(tool["completed_at"], "2026-08-02T12:00:05Z")

    def test_claude_tool_retains_result_completion_time(self) -> None:
        rows = [
            {
                "type": "assistant",
                "timestamp": "2026-08-02T12:00:01Z",
                "message": {
                    "content": [{
                        "type": "tool_use",
                        "id": "toolu-result-time",
                        "name": "Read",
                        "input": {"file_path": "frontend/src/session.tsx"},
                    }],
                },
            },
            {
                "type": "user",
                "timestamp": "2026-08-02T12:00:06Z",
                "message": {
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": "toolu-result-time",
                        "content": "fixture source",
                    }],
                },
            },
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "claude-result-time.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            parsed = transcripts.read_session_events("claude", path)

        tool = parsed["events"][0]["tool"]
        self.assertEqual(tool["completed_at"], "2026-08-02T12:00:06Z")

    def test_claude_structured_content_result_reconstructs_artifact_event(self) -> None:
        path = FIXTURES_DIR / "claude_artifact_structured_content.jsonl"

        parsed = transcripts.read_session_events("claude", path)

        self.assertEqual(len(parsed["events"]), 1)
        event = parsed["events"][0]
        self.assertEqual(event["kind"], "artifact")
        self.assertEqual(event["artifact_id"], "33b1c159-9d1e-4804-9b14-3d880ac2e3c7")
        self.assertEqual(
            event["artifact"],
            {"kind": "mermaid", "source": "graph TD; A-->B"},
        )
        self.assertEqual(event["title"], "Fixture diagram")
        self.assertEqual(event["caption"], "Structured result fallback")

    def test_codex_normalized_render_artifact_events_heal_archived_runs(self) -> None:
        fixture = json.loads(
            (
                FIXTURES_DIR
                / "agent_runtime"
                / "codex_render_artifact_completed.jsonl"
            ).read_text(encoding="utf-8")
        )
        payload = fixture["message"]
        envelope = {
            "seq": 200,
            "raw_seq": 200,
            "normalized_at": "2026-07-14T22:09:40.568821+00:00",
            "disposition": "rendered",
            "kind": "item_completed",
            "payload": payload,
        }
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(json.dumps(envelope) + "\n", encoding="utf-8")

            parsed = transcripts.read_session_events("codex-normalized", path)

        self.assertEqual(len(parsed["events"]), 1)
        event = parsed["events"][0]
        self.assertEqual(event["kind"], "artifact")
        self.assertEqual(event["artifact_id"], "6d0e7d00-2edf-4054-b0dc-fe17cd382c2a")
        self.assertEqual(event["title"], "Wiki.app architecture")
        self.assertEqual(
            event["caption"],
            "Native shell, frontend, FastAPI, headless supervisor, MCP, CLI, and local storage/data-control flows.",
        )
        self.assertEqual(
            event["artifact"],
            {"kind": "mermaid", "source": "flowchart TB\n  worker --> artifact"},
        )

        failed = deepcopy(envelope)
        failed["payload"] = deepcopy(payload)
        item = failed["payload"]["params"]["item"]
        item["status"] = "failed"
        item["error"] = "artifact server rejected the request"
        item["result"] = {"content": [{"type": "text", "text": "render rejected"}]}
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(json.dumps(failed) + "\n", encoding="utf-8")

            failed_parsed = transcripts.read_session_events("codex-normalized", path)

        self.assertEqual(len(failed_parsed["events"]), 1)
        failed_event = failed_parsed["events"][0]
        self.assertEqual(failed_event["kind"], "tool")
        self.assertFalse(failed_event["tool"]["ok"])
        self.assertEqual(failed_event["tool"]["summary"], "render_artifact rejected")

    def test_codex_normalized_artifact_and_diagnostic_events_do_not_duplicate(self) -> None:
        protocol_event = _artifact_protocol_event("mermaid", 100)
        item = {
            "arguments": {
                "kind": "mermaid",
                "payload": {"source": "graph TD; A-->B"},
            },
            "error": None,
            "id": "exec-duplicate",
            "result": {
                "content": [
                    {"type": "text", "text": sentinel_text(protocol_event)}
                ],
            },
            "server": "wiki_artifacts",
            "status": "completed",
            "tool": "render_artifact",
            "type": "mcpToolCall",
        }
        rows = [
            {
                "kind": "artifact",
                "disposition": "rendered",
                "payload": protocol_event,
                "normalized_at": "2026-07-14T22:09:40+00:00",
            },
            {
                "kind": "item_completed",
                "disposition": "rendered",
                "payload": {
                    "method": "item/completed",
                    "params": {"item": item},
                },
                "normalized_at": "2026-07-14T22:09:41+00:00",
            },
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))

            parsed = transcripts.read_session_events("codex-normalized", path)

        self.assertEqual(
            [event["artifact_id"] for event in parsed["events"] if event["kind"] == "artifact"],
            [protocol_event["id"]],
        )

    def test_codex_normalized_completed_unparseable_artifact_is_not_rejected(self) -> None:
        item = {
            "arguments": {
                "kind": "mermaid",
                "payload": {"source": "graph TD; A-->B"},
            },
            "error": None,
            "id": "exec-unparseable",
            "result": {"content": [{"type": "text", "text": "artifact rendered"}]},
            "server": "wiki_artifacts",
            "status": "completed",
            "tool": "render_artifact",
            "type": "mcpToolCall",
        }
        row = {
            "kind": "item_completed",
            "disposition": "rendered",
            "payload": {"method": "item/completed", "params": {"item": item}},
            "normalized_at": "2026-07-14T22:09:41+00:00",
        }
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(json.dumps(row) + "\n")

            parsed = transcripts.read_session_events("codex-normalized", path)

        event = parsed["events"][0]
        self.assertTrue(event["tool"]["ok"])
        self.assertEqual(
            event["tool"]["summary"],
            "render_artifact completed without a parseable artifact",
        )

    def test_round8_l2_artifact_failures_and_unparseable_twins_are_terminal_once(self) -> None:
        for item_id, item_updates in (
            (
                "artifact-failed-round8",
                {"status": "failed", "error": "render rejected", "result": {}},
            ),
            (
                "artifact-unparseable-round8",
                {
                    "status": "completed",
                    "result": {"content": [{"type": "text", "text": "not a sentinel"}]},
                },
            ),
        ):
            item = {
                "type": "mcpToolCall",
                "id": item_id,
                "server": "wiki_artifacts",
                "tool": "render_artifact",
                "arguments": {
                    "kind": "mermaid",
                    "payload": {"source": "graph TD; A-->B"},
                },
                **item_updates,
            }
            rows = [
                {
                    "kind": "item_completed",
                    "disposition": "rendered",
                    "payload": {"method": "item/completed", "params": {"item": item}},
                    "normalized_at": "2026-08-07T12:00:01Z",
                },
                {
                    "kind": "rawResponseItem_completed",
                    "disposition": "rendered",
                    "payload": {
                        "method": "rawResponseItem/completed",
                        "params": {"item": item},
                    },
                    "normalized_at": "2026-08-07T12:00:02Z",
                },
            ]
            with self.subTest(item_id=item_id), TemporaryDirectory() as tmp:
                path = Path(tmp) / "artifact-twins.jsonl"
                path.write_text("".join(json.dumps(row) + "\n" for row in rows))
                parsed = transcripts.read_session_events("codex-normalized", path)
                state = transcripts._cache[str(path)]

            tools = [event["tool"] for event in parsed["events"] if event["kind"] == "tool"]
            self.assertEqual(len(tools), 1)
            self.assertEqual(state["codex_item_lifecycle"][item_id]["state"], "terminal")

    def test_structured_image_result_reconstructs_artifact_reference(self) -> None:
        artifact_id = "33b1c159-9d1e-4804-9b14-3d880ac2e3c7"

        event = transcripts._artifact_from_structured_result(
            {
                "input": {
                    "kind": "image",
                    "payload": {"data_base64": "fixture-png", "mime": "image/png"},
                }
            },
            json.dumps({"artifact_id": artifact_id, "ok": True}),
        )

        self.assertIsNotNone(event)
        self.assertEqual(
            event["artifact"],
            {
                "kind": "image",
                "ref": f"artifact://{artifact_id}",
                "mime": "image/png",
            },
        )
        self.assertNotIn("data_base64", event["artifact"])

    def test_structured_binary_results_use_only_server_artifact_reference(self) -> None:
        artifact_id = "33b1c159-9d1e-4804-9b14-3d880ac2e3c7"
        cases = {
            "image": "image/png",
            "pdf": "application/pdf",
            "video": "video/mp4",
            "audio": "audio/mpeg",
        }
        for kind, mime in cases.items():
            with self.subTest(kind=kind):
                payload = {
                    "data_base64": "UNSCRUBBED-INPUT-BYTES",
                    "path": "/attacker/input.bin",
                    "mime": mime,
                    "poster_base64": "UNSCRUBBED-POSTER",
                    "transcript": "input transcript",
                }
                result = {"artifact_id": artifact_id, "ok": True}
                if kind == "video":
                    result["artifact"] = {
                        "kind": "video",
                        "ref": f"artifact://{artifact_id}",
                        "mime": mime,
                        "byte_size": 1234,
                        "width": 160,
                        "height": 120,
                        "duration_ms": 533,
                        "poster_base64": "data:image/png;base64,AA==",
                    }
                elif kind == "audio":
                    result["artifact"] = {
                        "kind": "audio",
                        "ref": f"artifact://{artifact_id}",
                        "mime": mime,
                        "byte_size": 1234,
                        "duration_ms": 500,
                        "peaks": [0, 128, 255],
                        "transcript": "validated transcript",
                    }
                event = transcripts._artifact_from_structured_result(
                    {"input": {"kind": kind, "payload": payload}},
                    json.dumps(result),
                )
                self.assertIsNotNone(event)
                if kind in {"video", "audio"}:
                    self.assertEqual(event["artifact"], result["artifact"])
                else:
                    self.assertEqual(
                        event["artifact"],
                        {
                            "kind": kind,
                            "ref": f"artifact://{artifact_id}",
                            "mime": mime,
                        },
                    )
                self.assertNotIn("UNSCRUBBED-INPUT-BYTES", json.dumps(event))
                self.assertNotIn("UNSCRUBBED-POSTER", json.dumps(event))

    def test_structured_binary_result_rejects_unallowed_mime(self) -> None:
        for kind in ("image", "pdf", "video", "audio"):
            with self.subTest(kind=kind):
                self.assertIsNone(
                    transcripts._artifact_from_structured_result(
                        {
                            "input": {
                                "kind": kind,
                                "payload": {
                                    "mime": "application/octet-stream",
                                    "data_base64": "input-bytes",
                                },
                            }
                        },
                        json.dumps(
                            {
                                "artifact_id": "33b1c159-9d1e-4804-9b14-3d880ac2e3c7",
                                "ok": True,
                            }
                        ),
                    )
                )

    def test_structured_artifact_fallback_rejects_errors_and_invalid_metadata(self) -> None:
        valid_input = {
            "kind": "mermaid",
            "payload": {"source": "graph TD; A-->B"},
        }
        valid_id = "33b1c159-9d1e-4804-9b14-3d880ac2e3c7"
        cases = [
            (valid_input, {"ok": True}, None),
            (valid_input, {"artifact_id": valid_id, "ok": False}, None),
            (valid_input, {"artifact_id": "not-a-uuid", "ok": True}, None),
            ({**valid_input, "kind": "unknown"}, {"artifact_id": valid_id, "ok": True}, None),
            (
                {**valid_input, "payload": {"source": "x" * (TEXT_LIMIT + 1)}},
                {"artifact_id": valid_id, "ok": True},
                None,
            ),
            ({**valid_input, "title": 42}, {"artifact_id": valid_id, "ok": True}, None),
            (valid_input, {"artifact_id": valid_id, "ok": True}, "is_error"),
            (valid_input, {"artifact_id": valid_id, "ok": True}, "isError"),
        ]
        for index, (tool_input, result, error_field) in enumerate(cases):
            with self.subTest(index=index), TemporaryDirectory() as tmp:
                call_id = f"artifact-{index}"
                path = Path(tmp) / "claude.jsonl"
                result_block = {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": json.dumps(result),
                }
                if error_field:
                    result_block[error_field] = True
                rows = [
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": call_id,
                                    "name": "mcp__wiki-artifacts__render_artifact",
                                    "input": tool_input,
                                }
                            ]
                        },
                    },
                    {
                        "type": "user",
                        "message": {
                            "content": [result_block]
                        },
                    },
                ]
                path.write_text(
                    "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
                )

                parsed = transcripts.read_session_events("claude", path)

                self.assertEqual(len(parsed["events"]), 1)
                event = parsed["events"][0]
                self.assertEqual(event["kind"], "tool")
                expected_ok = index in {0, 2, 3, 4, 5}
                self.assertEqual(event["tool"]["ok"], expected_ok)
                self.assertEqual(
                    event["tool"]["summary"],
                    (
                        "render_artifact completed without a parseable artifact"
                        if expected_ok
                        else "render_artifact rejected"
                    ),
                )


class CodexNewRuntimeTranscriptTests(unittest.TestCase):
    def setUp(self) -> None:
        transcripts._cache.clear()

    def test_new_runtime_harness_calls_and_outputs_are_normalized(self) -> None:
        path = FIXTURES_DIR / "codex_new_runtime_rendering.jsonl"

        parsed = transcripts.read_session_events("codex", path)

        tools = [event["tool"] for event in parsed["events"] if event["kind"] == "tool"]
        self.assertEqual(len(tools), 6)

        read_tool = tools[0]
        self.assertEqual(read_tool["name"], "exec_command")
        self.assertEqual(read_tool["archetype"], "read")
        self.assertEqual(read_tool["summary"], "read transcripts.py:1-40")
        self.assertEqual(read_tool["output"], "1\tfrom __future__ import annotations\n2\t\n")
        self.assertTrue(read_tool["ok"])

        github_tool = tools[1]
        self.assertEqual(github_tool["archetype"], "github")
        self.assertEqual(github_tool["summary"], "gh pr checks 13606 --repo hwang2409/wiki")
        self.assertEqual(github_tool["output"], "all checks passed\n")

        # Homogeneous Promise.all children keep their own canonical identity
        # and input, so each actual call reaches the shared tool renderer.
        multi_outer = tools[2]
        multi_tools = tools[3:5]
        self.assertEqual([tool["name"] for tool in multi_tools], ["exec_command", "exec_command"])
        self.assertEqual([tool["input"] for tool in multi_tools], [
            'git status --short --branch',
            'rg -n "custom_tool_call" backend/app/transcripts.py',
        ])
        self.assertEqual(multi_outer["output"], "## git\n## wiki-main\n## rg\n1034: custom_tool_call\n")
        self.assertEqual([tool["output"] for tool in multi_tools], [None, None])

        # The wrapper exposes one aggregate result for this batch. Preserve it
        # on both children, but do not invent child status.
        multi_tool = multi_tools[0]
        self.assertEqual(multi_tool["name"], "exec_command")
        self.assertEqual(multi_tool["archetype"], "git")
        self.assertEqual(multi_tool["summary"], "git status")
        self.assertEqual([tool["ok"] for tool in multi_tools], [None, None])

        failed_tool = tools[5]
        self.assertEqual(failed_tool["archetype"], "validate")
        self.assertFalse(failed_tool["ok"])
        self.assertEqual(failed_tool["output"], "test command failed\n")

    def test_round2_runtime_harness_and_native_mcp_fixture(self) -> None:
        path = FIXTURES_DIR / "codex_round2_runtime_rendering.jsonl"

        parsed = transcripts.read_session_events("codex", path)
        tools = [event["tool"] for event in parsed["events"] if event["kind"] == "tool"]

        self.assertEqual(len(tools), 9)
        self.assertEqual(tools[0]["name"], "write_stdin")
        self.assertEqual(tools[0]["archetype"], "wait")
        self.assertEqual(tools[0]["summary"], "waiting on terminal")
        self.assertEqual(tools[0]["output"], "done\n")
        self.assertTrue(tools[0]["ok"])

        patch_tool = tools[1]
        self.assertEqual(patch_tool["name"], "apply_patch")
        self.assertEqual(patch_tool["archetype"], "edit")
        self.assertEqual(patch_tool["summary"], "edit example.py")
        self.assertEqual(
            patch_tool["edit"]["patch"],
            "*** Begin Patch\n*** Update File: backend/app/example.py\n@@\n-old\n+new\n*** End Patch",
        )
        self.assertEqual(patch_tool["output"], "patched\n")
        self.assertTrue(patch_tool["ok"])

        self.assertEqual(tools[2]["name"], "mcp__someserver__some_tool")
        self.assertEqual(tools[2]["archetype"], "tool")
        self.assertEqual(tools[2]["summary"], 'some_tool {"value": "fixture"}')
        self.assertEqual(tools[2]["output"], "mcp harness output\n")

        self.assertEqual(tools[3]["name"], "update_plan")
        self.assertEqual(tools[3]["archetype"], "plan")
        self.assertEqual(tools[3]["summary"], "updated plan")

        self.assertEqual(tools[4]["name"], "view_image")
        self.assertEqual(tools[4]["archetype"], "read")
        self.assertEqual(tools[4]["summary"], "view image preview.png")

        self.assertEqual(tools[5]["name"], "wait")
        self.assertEqual(tools[5]["archetype"], "wait")
        self.assertEqual(tools[5]["summary"], 'wait {"seconds": 2}')

        native_mcp = tools[6]
        self.assertEqual(native_mcp["name"], "mcp__filesystem__list_dir")
        self.assertEqual(native_mcp["archetype"], "tool")
        self.assertEqual(native_mcp["output"], "a.txt\n")
        self.assertTrue(native_mcp["ok"])

        self.assertEqual(tools[7]["output"], "plain text\n")
        self.assertTrue(tools[7]["ok"])

        # Object bindings are not safe to resolve. They use the bounded
        # semantic fallback instead of inventing a command.
        resolved = tools[8]
        self.assertEqual(resolved["name"], "exec")
        self.assertEqual(
            resolved["input"],
            "dynamic tool program",
        )
        self.assertEqual(resolved["archetype"], "run")
        self.assertEqual(resolved["summary"], "exec dynamic tool program")

    def test_wiki265_harness_shapes_never_leak_raw_javascript(self) -> None:
        """WIKI-265: real custom_tool_call shapes render as semantic tools.

        The four fixture rows cover the shapes the bug screenshot showed:
        one exec_command call, a `const patch = "..."; tools.apply_patch(patch)`
        wrapper, a homogeneous ``Promise.all`` of exec_commands, and a mixed
        ``Promise.all`` of MCP + exec. None may surface the harness JS.
        """
        path = FIXTURES_DIR / "codex_wiki265_harness_shapes.jsonl"

        parsed = transcripts.read_session_events("codex", path)
        tools = [event["tool"] for event in parsed["events"] if event["kind"] == "tool"]

        self.assertEqual(len(tools), 8)
        for tool in tools:
            self.assertNotIn("```js", tool["input"])
            self.assertNotIn("await tools.", tool["input"])
            self.assertNotIn("Promise.all", tool["input"])
            self.assertNotIn("const r =", tool["input"])
            self.assertNotIn("const patch =", tool["input"])

        single = tools[0]
        self.assertEqual(single["name"], "exec_command")
        self.assertEqual(single["archetype"], "read")
        self.assertEqual(single["summary"], "read phoebe-dev.json:1-120")

        patch = tools[1]
        self.assertEqual(patch["name"], "apply_patch")
        self.assertEqual(patch["archetype"], "edit")
        self.assertIn("*** Update File", patch["input"])
        # apply_patch preserves the structured edit payload for the diff view.
        self.assertIn("patch", patch["edit"])

        parallel_outer = tools[2]
        parallel = tools[3:5]
        self.assertEqual([tool["name"] for tool in parallel], ["exec_command", "exec_command"])
        self.assertEqual([tool["input"] for tool in parallel], [
            "/tmp/agent-status/pr_watch_summary.sh 13657",
            "gh pr view 13657 --json state,mergeable,mergeStateStatus",
        ])
        self.assertEqual([tool["archetype"] for tool in parallel], ["run", "github"])
        self.assertIsNotNone(parallel_outer["output"])

        mixed_outer = tools[5]
        mixed = tools[6:8]
        self.assertEqual(
            [tool["name"] for tool in mixed],
            ["mcp__wiki_artifacts__read_agent_pr", "exec_command"],
        )
        self.assertEqual([tool["archetype"] for tool in mixed], ["tool", "run"])
        self.assertEqual(
            [tool["output"] for tool in mixed],
            [None, None],
        )
        # This fixture has one aggregate result block, not one result per
        # child. Keep sibling output evidence without claiming both passed.
        self.assertEqual([tool["ok"] for tool in mixed], [None, None])
        self.assertEqual(mixed_outer["output"], "pr context\ngate result: green\n")

    def test_wiki267_live_order_prefers_reasoning_and_native_mcp_rows(self) -> None:
        path = FIXTURES_DIR / "codex_wiki267_live_order.jsonl"

        parsed = transcripts.read_session_events("codex-normalized", path)
        thinking = [event for event in parsed["events"] if event["kind"] == "thinking"]
        tools = [event["tool"] for event in parsed["events"] if event["kind"] == "tool"]

        self.assertEqual([event["text"] for event in thinking], ["**Exploring agent/events terminal module**"])
        self.assertTrue(thinking[0]["encrypted"])
        self.assertEqual(
            [tool["name"] for tool in tools],
            ["mcp__wiki_artifacts__read_agent", "mcp__wiki_artifacts__read_agent_events"],
        )
        self.assertEqual([tool["output"] for tool in tools], ["agent details", "event details"])
        self.assertTrue(all("const" not in tool["input"] and "await tools" not in tool["input"] for tool in tools))

    def test_codex_runtime_preamble_is_metadata_not_output(self) -> None:
        path = FIXTURES_DIR / "codex_preamble_runtime_rendering.jsonl"

        parsed = transcripts.read_session_events("codex", path)

        tools = [event["tool"] for event in parsed["events"] if event["kind"] == "tool"]
        self.assertEqual(
            tools[0]["output"],
            "90\tdef _clip(text: str, limit: int) -> str:\n"
            "91\t    if len(text) <= limit:\n",
        )
        self.assertTrue(tools[0]["ok"])
        self.assertEqual(tools[1]["output"], "1\t# wiki\n2\t\n")
        self.assertTrue(tools[1]["ok"])
        self.assertEqual(tools[2]["output"], "command failed\n")
        self.assertFalse(tools[2]["ok"])
        self.assertEqual(
            tools[3]["output"],
            "1\tfirst line\n2\tsecond line\n... [120 chars truncated]",
        )
        self.assertEqual(tools[4]["output"], "first line\nOutput:\nsecond line\n")
        self.assertTrue(tools[4]["ok"])
        self.assertEqual(
            tools[5]["output"],
            "Script completed\nWall time 1.0 seconds\nOutput:\nbody data\n",
        )
        self.assertTrue(tools[5]["ok"])
        self.assertEqual(tools[6]["output"], "terminated output\n")
        self.assertFalse(tools[6]["ok"])
        self.assertEqual(tools[7]["output"], "command exited with code 0\n")
        self.assertFalse(tools[7]["ok"])

    def test_codex_runtime_wall_time_fills_missing_completion_timestamp(self) -> None:
        rows = [
            {
                "type": "response_item",
                "timestamp": "2026-08-07T12:00:00Z",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "wall-time-only",
                    "name": "exec",
                    "input": 'tools.exec_command({cmd: "printf ok"})',
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "wall-time-only",
                    "output": "Script completed\nWall time 1.2 seconds\nOutput:\nok\n",
                },
            },
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex-wall-time.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            parsed = transcripts.read_session_events("codex", path)

        tool = parsed["events"][0]["tool"]
        self.assertEqual(tool["completed_at"], "2026-08-07T12:00:01.200000Z")

    def test_harness_scanner_ignores_strings_comments_and_unsupported_args(self) -> None:
        cases = [
            'const text = "tools.exec_command({cmd: \\"hidden\\"})";',
            "// tools.exec_command({cmd: 'hidden'})",
            'tools.exec_command({cmd: foo_null});',
            'tools.exec_command({cmd: `echo hi`});',
            'tools.exec_command({cmd: "echo hi"',
        ]
        for source in cases:
            with self.subTest(source=source):
                self.assertIsNone(transcripts._codex_harness_tool("exec", source))

        comments = "tools.exec_command" + ("/* comment */" * 2000) + '({cmd: "echo hi"});'
        self.assertIsNone(transcripts._codex_harness_tool("exec", comments))

        invalid_suffixes = [
            'const r = await tools.exec_command({cmd: "echo hi"}); text(r.output)));',
            'const r = await tools.exec_command({cmd: "echo hi"}); text(r.output = "fake");',
            'const r = await tools.exec_command({cmd: "echo hi"}); mutate(r);',
        ]
        for source in invalid_suffixes:
            with self.subTest(source=source):
                self.assertIsNone(transcripts._codex_harness_tool("exec", source))
                fallback = transcripts._codex_harness_fallback_input(source)
                self.assertEqual(
                    fallback,
                    "dynamic tool program",
                )

    def test_harness_scanner_rejects_object_variable_indirection(self) -> None:
        """Object bindings are unsafe without a complete mutation proof."""
        args_case = 'const args = {cmd: "echo hi"}; tools.exec_command(args);'
        self.assertIsNone(transcripts._codex_harness_tool("exec", args_case))

        patch_case = (
            'const patch = "*** Begin Patch\\n*** Add File: /a\\n+hi\\n*** End Patch";'
            "\ntext(await tools.apply_patch(patch));\n"
        )
        harness = transcripts._codex_harness_tool("exec", patch_case)
        self.assertIsNotNone(harness)
        self.assertEqual(harness["name"], "apply_patch")
        self.assertIn("*** Begin Patch", harness["input"])

    def test_harness_grammar_rejects_dead_control_flow(self) -> None:
        cases = [
            "if (false) tools.exec_command({cmd: 'dead'});",
            "false && tools.exec_command({cmd: 'dead'});",
            "function unused() { tools.exec_command({cmd: 'dead'}); }",
            "for (;;) { tools.exec_command({cmd: 'dead'}); }",
            "condition ? tools.exec_command({cmd: 'dead'}) : null;",
        ]
        for source in cases:
            with self.subTest(source=source):
                self.assertIsNone(transcripts._codex_harness_tool("exec", source))

    def test_harness_scanner_folds_homogeneous_promise_all(self) -> None:
        """Promise.all of exec_commands collapses to newline-joined bash."""
        source = (
            "const rs = await Promise.all([\n"
            '  tools.exec_command({cmd:"ls", workdir:"/x", yield_time_ms:100, max_output_tokens:100}),\n'
            '  tools.exec_command({cmd:"pwd", workdir:"/x", yield_time_ms:100, max_output_tokens:100}),\n'
            "]);\ntext(rs);\n"
        )
        harness = transcripts._codex_harness_tool("exec", source)
        self.assertIsNotNone(harness)
        self.assertEqual(harness["name"], "exec_command")
        self.assertEqual(harness["input"], "ls\npwd")
        self.assertEqual(harness["calls"], 2)
        self.assertEqual([child["input"] for child in harness["children"]], ["ls", "pwd"])

    def test_harness_scanner_mixed_batch_preserves_each_call(self) -> None:
        """Mixed Promise.all emits one semantic child per actual call."""
        source = (
            "const [pr, st] = await Promise.all([\n"
            '  tools.mcp__wiki_artifacts__read_agent_pr({id:"X"}),\n'
            '  tools.exec_command({cmd:"wiki gate 13659", workdir:"/x", yield_time_ms:100, max_output_tokens:100}),\n'
            "]);\ntext(pr); text(st);\n"
        )
        harness = transcripts._codex_harness_tool("exec", source)
        self.assertIsNotNone(harness)
        self.assertEqual(harness["calls"], 2)
        self.assertEqual(
            [child["name"] for child in harness["children"]],
            ["mcp__wiki_artifacts__read_agent_pr", "exec_command"],
        )
        self.assertEqual([child["input"] for child in harness["children"]], [
            '{"id": "X"}',
            "wiki gate 13659",
        ])

    def test_harness_scanner_rejects_reordered_batch_result_bindings(self) -> None:
        source = (
            "const [first, second] = await Promise.all(["
            'tools.exec_command({cmd:"first"}),'
            'tools.exec_command({cmd:"second"})]);'
            "text(second); text(first);"
        )
        self.assertIsNone(transcripts._codex_harness_tool("exec", source))

        aggregate_source = (
            "const rs = await Promise.all(["
            'tools.exec_command({cmd:"first"}),'
            'tools.exec_command({cmd:"second"})]);'
            "text([rs[1], rs[0]]);"
        )
        self.assertIsNone(transcripts._codex_harness_tool("exec", aggregate_source))

    def test_harness_fallback_hides_malformed_javascript_and_is_bounded(self) -> None:
        """Malformed wrappers keep a bounded semantic note, never raw JS."""
        raw = 'const r = await tools.exec_command({cmd: "' + ("x" * 20_000)
        self.assertIsNone(transcripts._codex_harness_tool("exec", raw))
        fallback = transcripts._codex_harness_fallback_input(raw)
        self.assertLessEqual(len(fallback), transcripts.MAX_TOOL_IO)
        self.assertNotIn("const r =", fallback)
        self.assertNotIn("await tools.", fallback)
        self.assertEqual(fallback, "dynamic tool program")

        oversized = "tools.exec_command({cmd: \"echo hi\"});" + (" " * transcripts.MAX_CODEX_HARNESS_SOURCE)
        self.assertIsNone(transcripts._codex_harness_tool("exec", oversized))

        cycle = 'const args = args; tools.exec_command(args);'
        self.assertIsNone(transcripts._codex_harness_tool("exec", cycle))

    def test_harness_batch_inputs_are_bounded(self) -> None:
        command = "echo " + ("x" * (transcripts.MAX_CODEX_BATCH_CHILD_INPUT * 4))
        source = (
            "const rs = await Promise.all(["
            f"tools.exec_command({{cmd:{json.dumps(command)}}}),"
            'tools.exec_command({cmd:"pwd"})]); text(rs);'
        )
        harness = transcripts._codex_harness_tool("exec", source)
        self.assertIsNotNone(harness)
        self.assertTrue(all(
            len(child["input"]) <= transcripts.MAX_TOOL_IO
            for child in harness["children"]
        ))

    def test_harness_batch_results_match_explicit_child_results(self) -> None:
        rows = [
            {
                "type": "response_item",
                "timestamp": "2026-08-07T12:00:00Z",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "batch-results",
                    "name": "exec",
                    "input": (
                        "const rs = await Promise.all(["
                        'tools.exec_command({cmd:"first"}),'
                        'tools.exec_command({cmd:"second"})]); text(rs);'
                    ),
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-08-07T12:00:01Z",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "batch-results",
                    "output": [
                        {"output": "first output\n", "exit_code": 0},
                        {"output": "second output\n", "exit_code": 1},
                    ],
                },
            },
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex-batch-results.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            tools = [
                event["tool"]
                for event in transcripts.read_session_events("codex", path)["events"]
                if event["kind"] == "tool"
            ]

        self.assertEqual([tool["input"] for tool in tools], ["first\nsecond", "first", "second"])
        self.assertEqual([tool["output"] for tool in tools], ["first output\n\nsecond output\n", None, None])
        self.assertEqual([tool["ok"] for tool in tools], [None, None, None])

    def test_harness_batch_results_normalize_runtime_input_text_envelope(self) -> None:
        rows = [
            {
                "type": "response_item",
                "timestamp": "2026-08-07T12:00:00Z",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "batch-envelope",
                    "name": "exec",
                    "input": (
                        "const rs = await Promise.all(["
                        'tools.exec_command({cmd:"first"}),'
                        'tools.exec_command({cmd:"second"})]); text(rs);'
                    ),
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-08-07T12:00:01Z",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "batch-envelope",
                    "output": [
                        {
                            "type": "input_text",
                            "text": "Script completed\nWall time 0.1 seconds\nOutput:\n",
                        },
                        {
                            "type": "input_text",
                            "text": (
                                '[{"output":"first output\\n","exit_code":0},'
                                '{"output":"second output\\n","exit_code":1}]'
                            ),
                        },
                    ],
                },
            },
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex-batch-envelope.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            tools = [
                event["tool"]
                for event in transcripts.read_session_events("codex", path)["events"]
                if event["kind"] == "tool"
            ]

        self.assertEqual([tool["output"] for tool in tools], ["first output\n\nsecond output\n", None, None])
        self.assertEqual([tool["ok"] for tool in tools], [None, None, None])

    def test_harness_batch_child_references_cannot_collide_with_provider_ids(self) -> None:
        rows = [
            {
                "type": "response_item",
                "timestamp": "2026-08-07T12:00:00Z",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "batch",
                    "name": "exec",
                    "input": (
                        "const rs = await Promise.all(["
                        'tools.exec_command({cmd:"first"}),'
                        'tools.exec_command({cmd:"second"})]); text(rs);'
                    ),
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-08-07T12:00:01Z",
                "payload": {
                    "type": "function_call",
                    "call_id": "batch:child:0",
                    "name": "wait",
                    "arguments": '{"seconds":1}',
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-08-07T12:00:02Z",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "batch",
                    "output": [
                        {"output": "first\n", "exit_code": 0},
                        {"output": "second\n", "exit_code": 0},
                    ],
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-08-07T12:00:03Z",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "batch:child:0",
                    "output": "waited\n",
                },
            },
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex-batch-id-collision.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            tools = [
                event["tool"]
                for event in transcripts.read_session_events("codex", path)["events"]
                if event["kind"] == "tool"
            ]

        self.assertEqual([tool["output"] for tool in tools], ["first\n\nsecond\n", None, None, "waited\n"])

    def test_harness_deep_output_is_bounded_and_replay_is_deterministic(self) -> None:
        nested = '{"value":' * (transcripts.MAX_CODEX_OUTPUT_DEPTH + 10)
        nested += '"deep"' + "}" * (transcripts.MAX_CODEX_OUTPUT_DEPTH + 10)
        rows = [
            {
                "type": "response_item",
                "timestamp": "2026-08-07T12:00:00Z",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "deep-batch",
                    "name": "exec",
                    "input": (
                        "const rs = await Promise.all(["
                        'tools.exec_command({cmd:"first"}),'
                        'tools.exec_command({cmd:"second"})]); text(rs);'
                    ),
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-08-07T12:00:01Z",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "deep-batch",
                    "output": [{"type": "input_text", "text": nested}],
                },
            },
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex-deep-output.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            cold = transcripts.read_session_events("codex", path)
            transcripts._cache.clear()
            replay = transcripts.read_session_events("codex", path)

        self.assertEqual(cold["events"], replay["events"])
        self.assertEqual(
            [event["tool"]["ok"] for event in cold["events"] if event["kind"] == "tool"],
            [None, None, None],
        )
        oversized = [
            {
                "type": "input_text",
                "text": "x" * (transcripts.MAX_CODEX_BATCH_RESULT_BYTES // 2 + 1),
            },
            {
                "type": "input_text",
                "text": "y" * (transcripts.MAX_CODEX_BATCH_RESULT_BYTES // 2 + 1),
            },
        ]
        self.assertIsNone(transcripts._codex_batch_output_values(oversized, 2))

    def test_harness_variable_resolution_rejects_unsafe_declarations(self) -> None:
        cases = [
            "// const args = {cmd: 'fake'};\ntools.exec_command(args);",
            "const args = {cmd: 'real'}; /* comment */ tools.exec_command(args);",
            "{ const args = {cmd: 'scoped'}; } tools.exec_command(args);",
            "const args = {cmd: 'first'}; args = {cmd: 'fake'}; tools.exec_command(args);",
            'const patch = "safe"; patch = "fake"; tools.apply_patch(patch);',
            'const patch = "safe"; patch.value = "fake"; tools.apply_patch(patch);',
            'const patch = "safe"; Object.assign(patch, {value: "fake"}); tools.apply_patch(patch);',
            'const patch = "safe"; const alias = patch; tools.apply_patch(alias);',
            'const args = "echo".replace("echo", "fake"); tools.exec_command(args);',
            'const args = "echo" + " fake"; tools.exec_command(args);',
        ]
        for source in cases:
            with self.subTest(source=source):
                self.assertIsNone(transcripts._codex_harness_tool("exec", source))

    def test_harness_argument_parser_is_bounded(self) -> None:
        variables = {
            f"a{index}": f"a{index + 1}"
            for index in range(transcripts.MAX_CODEX_JS_NODES + 10)
        }
        variables[f"a{transcripts.MAX_CODEX_JS_NODES + 10}"] = '"safe"'
        self.assertIsNone(transcripts._codex_js_arguments("a0", variables))

        nested = '{"a":' * (transcripts.MAX_CODEX_JS_DEPTH + 4)
        nested += '"x"' + "}" * (transcripts.MAX_CODEX_JS_DEPTH + 4)
        self.assertIsNone(transcripts._codex_js_arguments(nested))

    def test_harness_argument_parser_rejects_unsupported_javascript_escapes(self) -> None:
        for escape in ("a", "N{SNOWMAN}", "U0001F600", "q"):
            source = f'tools.exec_command({{cmd:"bad\\{escape}"}});'
            with self.subTest(escape=escape):
                self.assertIsNone(transcripts._codex_harness_tool("exec", source))

        source = r'''tools.exec_command({cmd:"line\nhex\x41unicode\u0042"});'''
        harness = transcripts._codex_harness_tool("exec", source)
        self.assertIsNotNone(harness)
        self.assertEqual(harness["input"], "line\nhexAunicodeB")


class CodexModernTranscriptParityTests(unittest.TestCase):
    def setUp(self) -> None:
        transcripts._cache.clear()

    @staticmethod
    def _row(method: str, params: dict, seq: int) -> dict:
        return {
            "seq": seq,
            "raw_seq": seq,
            "normalized_at": f"2026-08-07T12:00:{seq:02d}Z",
            "disposition": "rendered",
            "kind": method.replace("/", "_"),
            "payload": {"method": method, "params": params},
        }

    def test_modern_item_lifecycle_deltas_and_replay_share_one_tool(self) -> None:
        item = {
            "type": "commandExecution",
            "id": "cmd-1",
            "command": "printf hi",
            "cwd": "/workspace",
            "status": "inProgress",
        }
        completed = {
            **item,
            "status": "completed",
            "aggregatedOutput": "hi\n",
            "exitCode": 0,
            "durationMs": 12,
        }
        rows = [
            self._row("item/started", {"item": item}, 1),
            self._row("item/commandExecution/outputDelta", {"itemId": "cmd-1", "delta": "hi\n"}, 2),
            self._row("item/commandExecution/terminalInteraction", {"itemId": "cmd-1", "input": "y\n"}, 3),
            self._row("item/completed", {"item": completed}, 4),
            self._row("rawResponseItem/completed", {"item": completed}, 5),
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            parsed = transcripts.read_session_events("codex-normalized", path)

        tools = [event["tool"] for event in parsed["events"] if event["kind"] == "tool"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "exec_command")
        self.assertEqual(tools[0]["output"], "hi\n")
        self.assertTrue(tools[0]["ok"])
        self.assertEqual(tools[0]["duration_ms"], 12)
        self.assertEqual(tools[0]["terminal_input"], "y\n")
        self.assertEqual(tools[0]["call_id"], "cmd-1")

    def test_result_first_modern_item_is_linked_when_start_replays_later(self) -> None:
        completed = {
            "type": "mcpToolCall",
            "id": "mcp-1",
            "server": "filesystem",
            "tool": "read_file",
            "arguments": {"path": "README.md"},
            "result": {"content": [{"type": "text", "text": "hello"}]},
            "status": "completed",
        }
        rows = [
            self._row("item/completed", {"item": completed}, 1),
            self._row("item/started", {"item": {**completed, "status": "inProgress", "result": None}}, 2),
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            parsed = transcripts.read_session_events("codex-normalized", path)

        tools = [event["tool"] for event in parsed["events"] if event["kind"] == "tool"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "mcp__filesystem__read_file")
        self.assertEqual(tools[0]["output"], "hello")
        self.assertTrue(tools[0]["ok"])

    def test_modern_agent_message_delta_and_approval_are_visible_once(self) -> None:
        rows = [
            self._row(
                "item/started",
                {"item": {"type": "agentMessage", "id": "msg-1", "text": ""}},
                1,
            ),
            self._row("item/agentMessage/delta", {"itemId": "msg-1", "delta": "hello"}, 2),
            self._row("item/agentMessage/delta", {"itemId": "msg-1", "delta": " world"}, 3),
            self._row(
                "item/completed",
                {"item": {"type": "agentMessage", "id": "msg-1", "text": "hello world"}},
                4,
            ),
            self._row(
                "item/commandExecution/requestApproval",
                {"itemId": "cmd-2", "reason": "needs terminal access"},
                5,
            ),
            self._row(
                "serverRequest/resolved",
                {"requestId": "cmd-2", "response": {"approved": False}},
                6,
            ),
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            parsed = transcripts.read_session_events("codex-normalized", path)

        self.assertEqual(
            [(event["kind"], event["text"]) for event in parsed["events"]],
            [
                ("assistant", "hello world"),
                ("marker", "approval requested"),
                ("marker", "approval resolved"),
            ],
        )

    def test_round6_j1_modern_agent_completion_pairs_raw_assistant_twin_by_id(self) -> None:
        rows = [
            self._row(
                "item/completed",
                {
                    "item": {
                        "type": "agentMessage",
                        "id": "assistant-twin",
                        "text": "modern answer",
                    }
                },
                1,
            ),
            self._row(
                "rawResponseItem/completed",
                {
                    "item": {
                        "type": "message",
                        "id": "assistant-twin",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "modern answer"}],
                    }
                },
                2,
            ),
            self._row(
                "rawResponseItem/completed",
                {
                    "item": {
                        "type": "message",
                        "id": "raw-only",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "raw answer"}],
                    }
                },
                3,
            ),
            self._row(
                "rawResponseItem/completed",
                {
                    "item": {
                        "type": "message",
                        "id": "raw-first",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "raw first"}],
                    }
                },
                4,
            ),
            self._row(
                "item/completed",
                {
                    "item": {
                        "type": "agentMessage",
                        "id": "raw-first",
                        "text": "raw first",
                    }
                },
                5,
            ),
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw-message-twins.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            parsed = transcripts.read_session_events("codex-normalized", path)

        self.assertEqual(
            [(event["kind"], event["text"]) for event in parsed["events"]],
            [
                ("assistant", "modern answer"),
                ("assistant", "raw answer"),
                ("assistant", "raw first"),
            ],
        )

    def test_round6_j2_completed_artifact_ignores_replayed_start(self) -> None:
        artifact = _artifact_protocol_event("mermaid", 269)
        item = {
            "type": "mcpToolCall",
            "id": "artifact-terminal",
            "server": "wiki_artifacts",
            "tool": "render_artifact",
            "arguments": {"kind": "mermaid", "payload": {"source": "graph TD; A-->B"}},
            "status": "inProgress",
        }
        completed = {
            **item,
            "status": "completed",
            "result": {"content": [{"type": "text", "text": sentinel_text(artifact)}]},
        }
        rows = [
            self._row("item/started", {"item": item}, 1),
            self._row("item/completed", {"item": completed}, 2),
            self._row("item/started", {"item": item}, 3),
            self._row("turn/completed", {"turn": {"status": "completed"}}, 4),
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact-replay.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            parsed = transcripts.read_session_events("codex-normalized", path)
            state = transcripts._cache[str(path)]

        self.assertEqual([event["kind"] for event in parsed["events"]], ["artifact"])
        self.assertIn("artifact-terminal", state["codex_terminal_items"])
        self.assertNotIn("artifact-terminal", state["pending_artifacts"])

    def test_round9_m1_interrupted_artifact_suppresses_late_completion(self) -> None:
        artifact = _artifact_protocol_event("mermaid", 270)
        item = {
            "type": "mcpToolCall",
            "id": "artifact-partial-replay",
            "server": "wiki_artifacts",
            "tool": "render_artifact",
            "arguments": {"kind": "mermaid", "payload": {"source": "graph TD; A-->B"}},
            "status": "inProgress",
        }
        completed = {
            **item,
            "status": "completed",
            "result": {"content": [{"type": "text", "text": sentinel_text(artifact)}]},
        }
        rows = [
            self._row("turn/started", {"turn": {"id": "partial-artifact-turn"}}, 1),
            self._row("item/started", {"item": item}, 2),
            self._row("turn/completed", {"turn": {"status": "interrupted"}}, 3),
            self._row("item/completed", {"item": completed}, 4),
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "partial-artifact-replay.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            parsed = transcripts.read_session_events("codex-normalized", path)
            state = transcripts._cache[str(path)]

        outcomes = [
            event for event in parsed["events"] if event["kind"] in {"tool", "artifact"}
        ]
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["kind"], "tool")
        self.assertEqual(
            state["codex_item_lifecycle"]["artifact-partial-replay"]["state"],
            "partial",
        )

    def test_round6_j3_trim_evicts_old_maps_and_preserves_visible_lifecycle(self) -> None:
        rows = []
        for index in range(2200):
            item_id = f"filler-{index}"
            rows.extend(
                [
                    self._row(
                        "item/started",
                        {"item": {"type": "agentMessage", "id": item_id, "text": item_id}},
                        index * 2 + 1,
                    ),
                    self._row(
                        "item/completed",
                        {"item": {"type": "agentMessage", "id": item_id, "text": item_id}},
                        index * 2 + 2,
                    ),
                ]
            )
        rows.extend(
            [
                self._row(
                    "item/started",
                    {
                        "item": {
                            "type": "commandExecution",
                            "id": "visible-tool",
                            "command": "printf final",
                            "status": "inProgress",
                        }
                    },
                    3000,
                ),
                self._row(
                    "item/commandExecution/outputDelta",
                    {"itemId": "visible-tool", "delta": "old"},
                    3001,
                ),
                self._row(
                    "item/completed",
                    {
                        "item": {
                            "type": "commandExecution",
                            "id": "visible-tool",
                            "command": "printf final",
                            "status": "completed",
                            "aggregatedOutput": "final",
                            "exitCode": 0,
                        }
                    },
                    3002,
                ),
                self._row(
                    "item/commandExecution/outputDelta",
                    {"itemId": "visible-tool", "delta": " late"},
                    3003,
                ),
                self._row(
                    "item/started",
                    {
                        "item": {
                            "type": "agentMessage",
                            "id": "visible-open",
                            "text": "unfinished",
                        }
                    },
                    3004,
                ),
                self._row("turn/completed", {"turn": {"status": "interrupted"}}, 3005),
            ]
        )
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "lifecycle-trim.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            transcripts.read_session_events("codex-normalized", path)
            state = transcripts._cache[str(path)]

        for mapping_name in (
            "codex_modern_items",
            "codex_modern_messages",
            "codex_reasoning_events",
            "codex_turn_open_events",
            "codex_terminal_items",
            "codex_authoritative_items",
            "pending_modern_deltas",
            "artifact_ids",
        ):
            self.assertLessEqual(len(state[mapping_name]), transcripts.CODEX_EVENT_WINDOW)
        self.assertNotIn("filler-0", state["codex_modern_messages"])
        self.assertEqual(
            state["codex_modern_items"]["visible-tool"]["tool"]["output"], "final"
        )
        self.assertTrue(state["codex_modern_messages"]["visible-open"]["partial"])

    def test_round7_item_lifecycle_matrix_covers_kind_and_path(self) -> None:
        """Every item kind uses one registry across creation and completion paths."""

        def artifact_item(item_id: str, status: str = "inProgress") -> dict:
            item = {
                "type": "mcpToolCall",
                "id": item_id,
                "server": "wiki_artifacts",
                "tool": "render_artifact",
                "arguments": {
                    "kind": "mermaid",
                    "payload": {"source": "graph TD; A-->B"},
                },
                "status": status,
            }
            if status == "completed":
                item["result"] = {
                    "content": [{"type": "text", "text": sentinel_text(_artifact_protocol_event("mermaid", 270))}]
                }
            return item

        def modern(kind: str, item_id: str, complete: bool = True) -> list[dict]:
            if kind == "agentMessage":
                item = {"type": "agentMessage", "id": item_id, "text": "answer"}
                started = {**item, "text": ""}
            elif kind == "reasoning":
                item = {"type": "reasoning", "id": item_id, "summary": [{"text": "thought"}]}
                started = {**item, "summary": []}
            elif kind == "tool":
                item = {
                    "type": "commandExecution",
                    "id": item_id,
                    "command": "printf child",
                    "status": "completed",
                    "aggregatedOutput": "child",
                }
                started = {**item, "status": "inProgress", "aggregatedOutput": None}
            else:
                item = artifact_item(item_id, "completed")
                started = artifact_item(item_id)
            rows = [self._row("item/started", {"item": started}, 1)]
            if complete:
                rows.append(self._row("item/completed", {"item": item}, 2))
            return rows

        def legacy(kind: str, item_id: str, complete: bool = True) -> list[dict]:
            if kind == "agentMessage":
                return [
                    {
                        "type": "response_item",
                        "timestamp": "2026-08-07T12:00:01Z",
                        "payload": {
                            "type": "message",
                            "id": item_id,
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "answer"}],
                        },
                    }
                ]
            if kind == "reasoning":
                return [
                    {
                        "type": "response_item",
                        "timestamp": "2026-08-07T12:00:01Z",
                        "payload": {
                            "type": "reasoning",
                            "id": item_id,
                            "summary": [{"text": "thought"}],
                        },
                    }
                ]
            if kind == "tool":
                rows = [
                    {
                        "type": "response_item",
                        "timestamp": "2026-08-07T12:00:01Z",
                        "payload": {
                            "type": "function_call",
                            "call_id": item_id,
                            "name": "exec_command",
                            "arguments": json.dumps({"cmd": "printf child"}),
                        },
                    }
                ]
                if complete:
                    rows.append(
                        {
                            "type": "response_item",
                            "timestamp": "2026-08-07T12:00:02Z",
                            "payload": {
                                "type": "function_call_output",
                                "call_id": item_id,
                                "output": "child",
                            },
                        }
                    )
                return rows
            if kind == "artifact":
                rows = [
                    {
                        "type": "response_item",
                        "timestamp": "2026-08-07T12:00:01Z",
                        "payload": {
                            "type": "function_call",
                            "call_id": item_id,
                            "name": "render_artifact",
                            "arguments": {"kind": "mermaid", "payload": {"source": "graph TD; A-->B"}},
                        },
                    }
                ]
                if complete:
                    rows.append(
                        {
                            "type": "response_item",
                            "timestamp": "2026-08-07T12:00:02Z",
                            "payload": {
                                "type": "function_call_output",
                                "call_id": item_id,
                                "output": sentinel_text(_artifact_protocol_event("mermaid", 270)),
                            },
                        }
                    )
                return rows
            harness = (
                'const rs = await Promise.all(['
                'tools.mcp__wiki_artifacts__render_artifact({kind:"mermaid",payload:{source:"graph TD; A-->B"}}),'
                'tools.exec_command({cmd:"printf child"})]); text(rs);'
            )
            rows = [
                {
                    "type": "response_item",
                    "timestamp": "2026-08-07T12:00:01Z",
                    "payload": {
                        "type": "custom_tool_call",
                        "call_id": "batch-outer",
                        "name": "exec",
                        "input": harness,
                    },
                }
            ]
            if complete:
                rows.append(
                    {
                        "type": "response_item",
                        "timestamp": "2026-08-07T12:00:02Z",
                        "payload": {
                            "type": "custom_tool_call_output",
                            "call_id": "batch-outer",
                            "output": [
                                {"call_id": "batch-tool", "output": "child"},
                                {
                                    "call_id": "batch-artifact",
                                    "output": sentinel_text(_artifact_protocol_event("mermaid", 270)),
                                },
                            ],
                        },
                    }
                )
            return rows

        def raw_twin(kind: str, item_id: str) -> dict:
            if kind == "agentMessage":
                item = {
                    "type": "message",
                    "id": item_id,
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "answer"}],
                }
            elif kind == "reasoning":
                item = {
                    "type": "reasoning",
                    "id": item_id,
                    "summary": [{"text": "thought"}],
                }
            elif kind == "tool":
                item = {
                    "type": "commandExecution",
                    "id": item_id,
                    "command": "printf child",
                    "status": "completed",
                    "aggregatedOutput": "child",
                }
            else:
                item = artifact_item(item_id, "completed")
            return self._row("rawResponseItem/completed", {"item": item}, 3)

        def trim_prefix(path_kind: str) -> list[dict]:
            rows: list[dict] = []
            for index in range(transcripts.CODEX_EVENT_WINDOW + 1):
                filler_id = f"matrix-trim-{path_kind}-{index}"
                if path_kind == "modern":
                    rows.append(
                        self._row(
                            "item/completed",
                            {
                                "item": {
                                    "type": "agentMessage",
                                    "id": filler_id,
                                    "text": filler_id,
                                }
                            },
                            index + 1,
                        )
                    )
                else:
                    rows.append(
                        {
                            "type": "response_item",
                            "timestamp": "2026-08-07T12:00:01Z",
                            "payload": {
                                "type": "message",
                                "id": filler_id,
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": filler_id}],
                            },
                        }
                    )
            return rows

        def case_rows(
            kind: str,
            path_kind: str,
            item_id: str,
            include_raw_twin: bool,
            trim: bool,
        ) -> list[dict]:
            builder = modern if path_kind == "modern" else legacy
            rows = builder(kind, item_id)
            if include_raw_twin:
                rows.append(raw_twin(kind, item_id))
            return (trim_prefix(path_kind) if trim else []) + rows

        matrix = [
            ("agentMessage", "modern", True, True),
            ("reasoning", "modern", True, True),
            ("tool", "modern", True, True),
            ("artifact", "modern", True, True),
            ("agentMessage", "legacy", False, True),
            ("reasoning", "legacy", False, True),
            ("tool", "legacy", False, True),
            ("artifact", "legacy", False, True),
            ("batch child", "legacy", False, False),
        ]
        for kind, path_kind, include_raw_twin, trim in matrix:
            item_id = f"matrix-{kind.replace(' ', '-')}-{path_kind}"
            with self.subTest(kind=kind, path=path_kind), TemporaryDirectory() as tmp:
                path = Path(tmp) / "matrix.jsonl"
                rows = case_rows(kind, path_kind, item_id, include_raw_twin, trim)
                path.write_text("".join(json.dumps(row) + "\n" for row in rows))
                parsed = transcripts.read_session_events(
                    "codex-normalized" if path_kind == "modern" else "codex", path
                )
                state = transcripts._cache[str(path)]
            expected_ids = (
                {"batch-tool", "batch-artifact"}
                if kind == "batch child"
                else {item_id}
            )
            target_ids = (
                [
                    key
                    for key, record in state["codex_item_lifecycle"].items()
                    if key != "batch-outer"
                    and record.get("kind") in {"tool", "artifact"}
                ]
                if kind == "batch child"
                else [item_id]
            )
            self.assertEqual(set(target_ids), expected_ids)
            self.assertEqual(len(target_ids), len(expected_ids))
            for target_id in target_ids:
                self.assertEqual(state["codex_item_lifecycle"][target_id]["state"], "terminal")
            baseline_events = len(parsed["events"])
            with TemporaryDirectory() as tmp:
                path = Path(tmp) / "matrix-replay.jsonl"
                replay_case = case_rows(
                    kind, path_kind, item_id, include_raw_twin, trim
                )
                replay_rows = replay_case + replay_case
                path.write_text("".join(json.dumps(row) + "\n" for row in replay_rows))
                replayed = transcripts.read_session_events(
                    "codex-normalized" if path_kind == "modern" else "codex", path
                )
            self.assertEqual(len(replayed["events"]), baseline_events, (kind, path_kind))

        interrupt_matrix = [
            ("agentMessage", "modern"),
            ("reasoning", "modern"),
            ("tool", "modern"),
            ("artifact", "modern"),
            ("tool", "legacy"),
            ("artifact", "legacy"),
            ("batch child", "legacy"),
        ]
        for kind, path_kind in interrupt_matrix:
            item_id = f"partial-{kind.replace(' ', '-')}-{path_kind}"
            builder = modern if path_kind == "modern" else legacy
            rows = builder(kind, item_id, complete=False)
            if path_kind == "modern":
                turn_started = self._row("turn/started", {"turn": {"id": item_id}}, 0)
                turn_completed = self._row(
                    "turn/completed", {"turn": {"status": "interrupted"}}, 99
                )
            else:
                turn_started = {
                    "type": "event_msg",
                    "timestamp": "2026-08-07T12:00:00Z",
                    "payload": {"type": "turn_started", "turn": {"id": item_id}},
                }
                turn_completed = {
                    "type": "event_msg",
                    "timestamp": "2026-08-07T12:00:99Z",
                    "payload": {
                        "type": "turn_completed",
                        "turn": {"status": "interrupted"},
                    },
                }
            rows.insert(0, turn_started)
            rows.append(turn_completed)
            with self.subTest(kind=kind, path=f"{path_kind}-interrupt"), TemporaryDirectory() as tmp:
                path = Path(tmp) / "matrix-interrupt.jsonl"
                path.write_text("".join(json.dumps(row) + "\n" for row in rows))
                transcripts.read_session_events(
                    "codex-normalized" if path_kind == "modern" else "codex", path
                )
                state = transcripts._cache[str(path)]
            target_ids = (
                [
                    key
                    for key, record in state["codex_item_lifecycle"].items()
                    if key != "batch-outer"
                    and record.get("kind") in {"tool", "artifact"}
                ]
                if kind == "batch child"
                else [item_id]
            )
            for target_id in target_ids:
                self.assertEqual(state["codex_item_lifecycle"][target_id]["state"], "partial")

    def test_round7_k1_trim_keeps_buffered_unrendered_deltas(self) -> None:
        rows = [
            self._row(
                "item/commandExecution/outputDelta",
                {"itemId": "buffered-after-trim", "delta": "before-start"},
                1,
            )
        ]
        for index in range(2200):
            item_id = f"trim-{index}"
            rows.extend(
                [
                    self._row(
                        "item/started",
                        {"item": {"type": "agentMessage", "id": item_id, "text": item_id}},
                        index * 2 + 2,
                    ),
                    self._row(
                        "item/completed",
                        {"item": {"type": "agentMessage", "id": item_id, "text": item_id}},
                        index * 2 + 3,
                    ),
                ]
            )
        rows.append(
            self._row(
                "item/started",
                {"item": {"type": "commandExecution", "id": "buffered-after-trim", "command": "echo"}},
                5000,
            )
        )
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "buffered-trim.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            transcripts.read_session_events("codex-normalized", path)
            state = transcripts._cache[str(path)]
        self.assertEqual(
            state["codex_modern_items"]["buffered-after-trim"]["tool"]["output"],
            "before-start",
        )
        self.assertNotIn("buffered-after-trim", state["pending_modern_deltas"])

    def test_round7_k2_post_eviction_replay_does_not_reuse_trimmed_authority(self) -> None:
        target = self._row(
            "item/completed",
            {"item": {"type": "agentMessage", "id": "evicted-authority", "text": "final"}},
            1,
        )
        rows = [target]
        for index in range(transcripts.CODEX_EVENT_WINDOW - 1):
            filler_id = f"authority-filler-{index}"
            rows.append(
                self._row(
                    "item/completed",
                    {"item": {"type": "agentMessage", "id": filler_id, "text": filler_id}},
                    index + 2,
                )
            )
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "authority-eviction.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            transcripts.read_session_events("codex-normalized", path)

            path.write_text(
                path.read_text()
                + json.dumps(
                    self._row(
                        "item/agentMessage/delta",
                        {"itemId": "evicted-authority", "delta": " replay"},
                        3000,
                    )
                )
                + "\n"
            )
            transcripts.read_session_events("codex-normalized", path)
            state = transcripts._cache[str(path)]
            self.assertEqual(
                state["codex_modern_messages"]["evicted-authority"]["text"],
                "final",
            )
            self.assertNotIn("evicted-authority", state["pending_modern_deltas"])

            path.write_text(
                path.read_text()
                + json.dumps(
                    self._row(
                        "item/completed",
                        {"item": {"type": "agentMessage", "id": "last-filler", "text": "last"}},
                        3001,
                    )
                )
                + "\n"
            )
            transcripts.read_session_events("codex-normalized", path)
            self.assertNotIn("evicted-authority", state["codex_item_lifecycle"])

            path.write_text(
                path.read_text()
                + json.dumps(
                    self._row(
                        "item/agentMessage/delta",
                        {"itemId": "evicted-authority", "delta": " after-eviction"},
                        3002,
                    )
                )
                + "\n"
            )
            transcripts.read_session_events("codex-normalized", path)

        self.assertEqual(
            state["pending_modern_deltas"]["evicted-authority"][0]["text"],
            " after-eviction",
        )

    def test_round8_l3_reasoning_registry_tracks_every_split_event(self) -> None:
        rows = [
            self._row(
                "item/started",
                {"item": {"type": "reasoning", "id": "split-reasoning", "summary": []}},
                1,
            ),
            self._row(
                "item/reasoning/summaryTextDelta",
                {"itemId": "split-reasoning", "delta": "**first** **second**"},
                2,
            ),
            self._row(
                "turn/completed",
                {"turn": {"status": "interrupted"}},
                3,
            ),
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "reasoning-split.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            transcripts.read_session_events("codex-normalized", path)
            state = transcripts._cache[str(path)]

        record = state["codex_item_lifecycle"]["split-reasoning"]
        self.assertEqual(record["state"], "partial")
        self.assertEqual(len(record["events"]), 2)
        self.assertTrue(all(event.get("partial") for event in record["events"]))

    def test_round8_l4_never_rendered_registry_entries_evict_with_pending_maps(self) -> None:
        cases = {
            "delta": lambda index: self._row(
                "item/commandExecution/outputDelta",
                {"itemId": f"delta-only-{index}", "delta": "buffered"},
                index,
            ),
            "artifact": lambda index: self._row(
                "item/started",
                {
                    "item": {
                        "type": "mcpToolCall",
                        "id": f"artifact-start-only-{index}",
                        "server": "wiki_artifacts",
                        "tool": "render_artifact",
                        "arguments": {
                            "kind": "mermaid",
                            "payload": {"source": "graph TD; A-->B"},
                        },
                        "status": "inProgress",
                    }
                },
                index,
            ),
        }
        for kind, build in cases.items():
            rows = [build(index) for index in range(transcripts.CODEX_EVENT_WINDOW + 1)]
            with self.subTest(kind=kind), TemporaryDirectory() as tmp:
                path = Path(tmp) / f"{kind}-only.jsonl"
                path.write_text("".join(json.dumps(row) + "\n" for row in rows))
                transcripts.read_session_events("codex-normalized", path)
                state = transcripts._cache[str(path)]

            prefix = "delta-only" if kind == "delta" else "artifact-start-only"
            first_id = f"{prefix}-0"
            last_id = f"{prefix}-{transcripts.CODEX_EVENT_WINDOW}"
            pending_key = "pending_modern_deltas" if kind == "delta" else "pending_artifacts"
            self.assertNotIn(first_id, state["codex_item_lifecycle"])
            self.assertNotIn(first_id, state[pending_key])
            self.assertIn(last_id, state["codex_item_lifecycle"])
            self.assertIn(last_id, state[pending_key])
            self.assertLessEqual(len(state["codex_item_lifecycle"]), transcripts.CODEX_EVENT_WINDOW)

    def test_round9_m4_empty_reasoning_lifecycle_records_are_bounded(self) -> None:
        rows = [
            self._row(
                "item/completed",
                {"item": {"type": "reasoning", "id": f"empty-{index}", "summary": []}},
                index,
            )
            for index in range(transcripts.CODEX_EVENT_WINDOW + 1)
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty-reasoning.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            transcripts.read_session_events("codex-normalized", path)
            state = transcripts._cache[str(path)]

        self.assertLessEqual(
            len(state["codex_item_lifecycle"]), transcripts.CODEX_EVENT_WINDOW
        )
        self.assertNotIn("empty-0", state["codex_item_lifecycle"])
        self.assertEqual(
            state["codex_item_lifecycle"][f"empty-{transcripts.CODEX_EVENT_WINDOW}"]["state"],
            "terminal",
        )

    def test_round2_f1_wrapper_incremental_replay_matches_full_and_keeps_unmatched(self) -> None:
        wrapper = {
            "type": "response_item",
            "timestamp": "2026-08-07T12:00:00Z",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "wrapper-1",
                "name": "exec",
                "input": 'tools.mcp__fixture__lookup({value:"wanted"});',
            },
        }
        native = {
            "type": "response_item",
            "timestamp": "2026-08-07T12:00:01Z",
            "payload": {
                "type": "mcpToolCall",
                "id": "native-1",
                "server": "fixture",
                "tool": "lookup",
                "arguments": {"value": "wanted"},
                "status": "completed",
                "result": {"content": [{"type": "text", "text": "native"}]},
            },
        }
        unmatched = {
            **wrapper,
            "payload": {
                **wrapper["payload"],
                "call_id": "wrapper-2",
                "input": 'tools.mcp__fixture__lookup({value:"other"});',
            },
        }
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            incremental = root / "incremental.jsonl"
            incremental.write_text(json.dumps(wrapper) + "\n")
            transcripts._cache.clear()
            transcripts.read_session_events("codex", incremental)
            incremental.write_text(
                "\n".join(json.dumps(row) for row in (wrapper, native, unmatched)) + "\n"
            )
            incremental_result = transcripts.read_session_events("codex", incremental)
            full = root / "full.jsonl"
            full.write_text(
                "\n".join(json.dumps(row) for row in (wrapper, native, unmatched)) + "\n"
            )
            transcripts._cache.clear()
            full_result = transcripts.read_session_events("codex", full)

        self.assertEqual(incremental_result["events"], full_result["events"])
        tools = [event["tool"] for event in full_result["events"] if event["kind"] == "tool"]
        self.assertEqual([tool["name"] for tool in tools], [
            "mcp__fixture__lookup",
            "mcp__fixture__lookup",
        ])
        self.assertEqual([tool["input"] for tool in tools], [
            '{"value": "wanted"}',
            '{"value": "other"}',
        ])

    def test_round3_g1_wrapper_suppression_is_scoped_to_one_turn(self) -> None:
        def raw(seq: int, row_type: str, payload: dict) -> dict:
            return {
                "type": row_type,
                "timestamp": f"2026-08-07T12:00:{seq:02d}Z",
                "payload": payload,
            }

        wrapper_input = 'tools.mcp__fixture__lookup({value:"wanted"});'
        rows = [
            raw(1, "event_msg", {"type": "turn_started"}),
            raw(2, "response_item", {
                "type": "custom_tool_call",
                "call_id": "wrapper-1",
                "name": "exec",
                "input": wrapper_input,
            }),
            raw(3, "response_item", {
                "type": "mcpToolCall",
                "id": "native-1",
                "server": "fixture",
                "tool": "lookup",
                "arguments": {"value": "wanted"},
                "status": "completed",
                "result": {"content": [{"type": "text", "text": "native"}]},
            }),
            raw(4, "event_msg", {"type": "turn_completed", "turn": {"status": "completed"}}),
            raw(5, "event_msg", {"type": "turn_started"}),
            raw(6, "response_item", {
                "type": "custom_tool_call",
                "call_id": "wrapper-2",
                "name": "exec",
                "input": wrapper_input,
            }),
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "turn-scoped-wrapper.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            parsed = transcripts.read_session_events("codex", path)

        tools = [event["tool"] for event in parsed["events"] if event["kind"] == "tool"]
        self.assertEqual([tool["name"] for tool in tools], [
            "mcp__fixture__lookup",
            "mcp__fixture__lookup",
        ])
        self.assertEqual(tools[0]["output"], "native")
        self.assertIsNone(tools[1]["output"])
        self.assertIsNone(tools[1]["ok"])

    def test_round8_l1_wrapper_suppression_keeps_shared_native_lifecycle_open(self) -> None:
        wrapper = {
            "type": "response_item",
            "timestamp": "2026-08-07T12:00:00Z",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "shared-native-id",
                "name": "exec",
                "input": 'tools.mcp__fixture__lookup({value:"wanted"});',
            },
        }
        native = {
            "type": "response_item",
            "timestamp": "2026-08-07T12:00:01Z",
            "payload": {
                "type": "mcpToolCall",
                "id": "shared-native-id",
                "server": "fixture",
                "tool": "lookup",
                "arguments": {"value": "wanted"},
                "status": "completed",
                "result": {"content": [{"type": "text", "text": "native"}]},
            },
        }
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "shared-native.jsonl"
            path.write_text(json.dumps(wrapper) + "\n")
            transcripts.read_session_events("codex", path)
            path.write_text(json.dumps(wrapper) + "\n" + json.dumps(native) + "\n")
            parsed = transcripts.read_session_events("codex", path)
            state = transcripts._cache[str(path)]

        tools = [event["tool"] for event in parsed["events"] if event["kind"] == "tool"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["output"], "native")
        self.assertEqual(state["codex_item_lifecycle"]["shared-native-id"]["state"], "terminal")

    def test_round9_m2_incremental_batch_native_rows_clear_provisional_artifacts(self) -> None:
        source = (
            "const rs = await Promise.all(["
            "tools.mcp__wiki_artifacts__render_artifact({kind:\"mermaid\",payload:{source:\"graph TD; A-->B\"}}),"
            "tools.mcp__wiki_artifacts__render_artifact({kind:\"mermaid\",payload:{source:\"graph TD; A-->B\"}})"
            "]); text(rs);"
        )
        wrapper = {
            "type": "response_item",
            "timestamp": "2026-08-07T12:00:00Z",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "batch-artifact-wrapper",
                "name": "exec",
                "input": source,
            },
        }
        native_rows = [
            {
                "type": "response_item",
                "timestamp": f"2026-08-07T12:00:0{index + 1}Z",
                "payload": {
                    "type": "mcpToolCall",
                    "id": f"native-batch-artifact-{index}",
                    "server": "wiki_artifacts",
                    "tool": "render_artifact",
                    "arguments": {
                        "kind": "mermaid",
                        "payload": {"source": "graph TD; A-->B"},
                    },
                    "status": "completed",
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": sentinel_text(_artifact_protocol_event("mermaid", 271 + index)),
                            }
                        ]
                    },
                },
            }
            for index in range(2)
        ]
        turn_completed = {
            "type": "event_msg",
            "timestamp": "2026-08-07T12:00:03Z",
            "payload": {"type": "turn_completed", "turn": {"status": "completed"}},
        }
        with TemporaryDirectory() as tmp:
            cold_path = Path(tmp) / "cold-batch-artifact.jsonl"
            cold_path.write_text(json.dumps(wrapper) + "\n" + json.dumps(native_rows[0]) + "\n")
            cold_parsed = transcripts.read_session_events("codex", cold_path)
            cold_state = transcripts._cache[str(cold_path)]
            cold_child_ids = {
                tool.get("call_id")
                for event in cold_parsed["events"]
                if event["kind"] == "tool"
                for tool in [event["tool"]]
                if tool.get("call_id") in {
                    "__codex_batch_child__:batch-artifact-wrapper:0",
                    "__codex_batch_child__:batch-artifact-wrapper:1",
                }
            }
            self.assertEqual(
                cold_child_ids,
                {"__codex_batch_child__:batch-artifact-wrapper:1"},
            )
            self.assertNotIn(
                "__codex_batch_child__:batch-artifact-wrapper:0",
                cold_state["pending_artifacts"],
            )
            self.assertIn(
                "__codex_batch_child__:batch-artifact-wrapper:1",
                cold_state["pending_artifacts"],
            )

            path = Path(tmp) / "incremental-batch-artifact.jsonl"
            path.write_text(json.dumps(wrapper) + "\n")
            transcripts.read_session_events("codex", path)
            path.write_text(json.dumps(wrapper) + "\n" + json.dumps(native_rows[0]) + "\n")
            parsed = transcripts.read_session_events("codex", path)
            child_ids = {
                "__codex_batch_child__:batch-artifact-wrapper:0",
                "__codex_batch_child__:batch-artifact-wrapper:1",
            }
            visible_child_ids = {
                tool.get("call_id")
                for event in parsed["events"]
                if event["kind"] == "tool"
                for tool in [event["tool"]]
                if tool.get("call_id") in child_ids
            }
            self.assertEqual(
                visible_child_ids,
                {"__codex_batch_child__:batch-artifact-wrapper:1"},
            )
            self.assertNotIn(
                "__codex_batch_child__:batch-artifact-wrapper:0",
                transcripts._cache[str(path)]["pending_artifacts"],
            )
            self.assertIn(
                "__codex_batch_child__:batch-artifact-wrapper:1",
                transcripts._cache[str(path)]["pending_artifacts"],
            )
            path.write_text(
                "\n".join(json.dumps(row) for row in [wrapper, *native_rows, turn_completed])
                + "\n"
            )
            parsed = transcripts.read_session_events("codex", path)

        self.assertEqual(
            [event["kind"] for event in parsed["events"]],
            ["artifact", "artifact"],
        )
        self.assertNotIn(
            "render_artifact rejected",
            [event["tool"]["summary"] for event in parsed["events"] if event["kind"] == "tool"],
        )

    def test_round12_cold_batch_rows_share_native_occurrence_multiset(self) -> None:
        source = 'tools.mcp__fixture__lookup({value:"wanted"});'

        def wrapper(call_id: str) -> dict:
            return {
                "type": "response_item",
                "timestamp": f"2026-08-07T12:00:0{call_id[-1]}Z",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": call_id,
                    "name": "exec",
                    "input": source,
                },
            }

        def native(call_id: str, second: bool = False) -> dict:
            return {
                "type": "response_item",
                "timestamp": f"2026-08-07T12:00:0{3 if second else 2}Z",
                "payload": {
                    "type": "mcpToolCall",
                    "id": call_id,
                    "server": "fixture",
                    "tool": "lookup",
                    "arguments": {"value": "wanted"},
                    "status": "completed",
                    "result": {"content": [{"type": "text", "text": "native"}]},
                },
            }

        def visible_wrapper_count(parsed: dict) -> int:
            return sum(
                1
                for event in parsed["events"]
                if event["kind"] == "tool"
                and event["tool"]["name"] == "mcp__fixture__lookup"
                and event["tool"]["output"] is None
            )

        rows = [wrapper("wrapper-1"), wrapper("wrapper-2"), native("native-1")]
        with TemporaryDirectory() as tmp:
            one_native_path = Path(tmp) / "one-native.jsonl"
            one_native_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            one_native = transcripts.read_session_events("codex", one_native_path)

            two_native_path = Path(tmp) / "two-native.jsonl"
            two_native_rows = [*rows[:2], native("native-1"), native("native-2", second=True)]
            two_native_path.write_text(
                "\n".join(json.dumps(row) for row in two_native_rows) + "\n"
            )
            two_native = transcripts.read_session_events("codex", two_native_path)

        self.assertEqual(visible_wrapper_count(one_native), 1)
        self.assertEqual(visible_wrapper_count(two_native), 0)

    def test_round2_f2_statusless_completed_items_are_done(self) -> None:
        parsed = transcripts.read_session_events(
            "codex-normalized", FIXTURES_DIR / "codex_wiki266_round2_lifecycle.jsonl"
        )
        tools = [event["tool"] for event in parsed["events"] if event["kind"] == "tool"]
        by_call_id = {tool["call_id"]: tool for tool in tools}
        for call_id in ("search-1", "image-1"):
            self.assertIsNone(by_call_id[call_id]["output"])
            self.assertTrue(by_call_id[call_id]["ok"])
            self.assertEqual(by_call_id[call_id]["status"], "completed")

    def test_round2_f3_real_collab_type_and_dynamic_content_items_render(self) -> None:
        parsed = transcripts.read_session_events(
            "codex-normalized", FIXTURES_DIR / "codex_wiki266_round2_lifecycle.jsonl"
        )
        tools = [event["tool"] for event in parsed["events"] if event["kind"] == "tool"]
        by_call_id = {tool["call_id"]: tool for tool in tools}
        self.assertEqual(by_call_id["collab-1"]["name"], "collabAgentToolCall")
        self.assertNotIn("contentItems", by_call_id["dynamic-1"]["input"])
        self.assertEqual(by_call_id["dynamic-1"]["output"], "needle")

    def test_round3_g2_dynamic_content_items_are_output_and_success_is_authoritative(self) -> None:
        item = {
            "type": "dynamicToolCall",
            "id": "dynamic-failed",
            "tool": "lookup",
            "arguments": {"query": "needle"},
        }
        completed = {
            **item,
            "contentItems": [{"type": "text", "text": "not found"}],
            "success": False,
        }
        rows = [
            self._row("item/started", {"item": item}, 1),
            self._row("item/completed", {"item": completed}, 2),
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "dynamic-output.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            parsed = transcripts.read_session_events("codex-normalized", path)

        tool = next(event["tool"] for event in parsed["events"] if event["kind"] == "tool")
        self.assertEqual(tool["input"], "needle")
        self.assertEqual(tool["output"], "not found")
        self.assertFalse(tool["ok"])

    def test_round2_f4_failed_turn_closes_pending_tool_and_marks_turn(self) -> None:
        parsed = transcripts.read_session_events(
            "codex-normalized", FIXTURES_DIR / "codex_wiki266_round2_lifecycle.jsonl"
        )
        pending = next(
            event["tool"]
            for event in parsed["events"]
            if event["kind"] == "tool" and event["tool"]["call_id"] == "pending-1"
        )
        self.assertFalse(pending["ok"])
        self.assertEqual(pending["status"], "failed")
        self.assertTrue(pending["partial"])
        self.assertTrue(any(
            event["kind"] == "interrupt" and event["text"].startswith("turn failed")
            for event in parsed["events"]
        ))

    def test_round2_f5_delta_identity_keeps_repeated_content_and_marks_interrupt(self) -> None:
        rows = [
            self._row("item/started", {"item": {"type": "commandExecution", "id": "delta-1", "command": "x"}}, 1),
            self._row("item/commandExecution/outputDelta", {"itemId": "delta-1", "delta": "same"}, 2),
            self._row("item/commandExecution/outputDelta", {"itemId": "delta-1", "delta": "same"}, 3),
            self._row("item/commandExecution/outputDelta", {"itemId": "delta-1", "delta": "same"}, 3),
            self._row("turn/completed", {"turn": {"status": "interrupted"}}, 4),
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "delta.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            parsed = transcripts.read_session_events("codex-normalized", path)
        tool = next(event["tool"] for event in parsed["events"] if event["kind"] == "tool")
        self.assertEqual(tool["output"], "samesame")
        self.assertTrue(tool["partial"])

    def test_round3_g3_interrupted_assistant_and_reasoning_are_partial(self) -> None:
        rows = [
            self._row(
                "item/started",
                {"item": {"type": "agentMessage", "id": "assistant-partial", "text": "unfinished"}},
                1,
            ),
            self._row(
                "item/started",
                {
                    "item": {
                        "type": "reasoning",
                        "id": "reasoning-partial",
                        "summary": [{"text": "unfinished thought"}],
                        "encrypted_content": "opaque",
                    }
                },
                2,
            ),
            self._row("turn/completed", {"turn": {"status": "interrupted"}}, 3),
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "partial-messages.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            parsed = transcripts.read_session_events("codex-normalized", path)

        events = {
            event["text"]: event
            for event in parsed["events"]
            if event["kind"] in {"assistant", "thinking"}
        }
        self.assertTrue(events["unfinished"]["partial"])
        self.assertTrue(events["unfinished thought"]["partial"])

    def test_round3_g4_live_patch_carries_call_id_and_partial(self) -> None:
        started = self._row(
            "item/started",
            {"item": {"type": "commandExecution", "id": "live-command", "command": "echo hi"}},
            1,
        )
        completed = self._row(
            "turn/completed",
            {"turn": {"status": "interrupted"}},
            2,
        )
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "live-patch.jsonl"
            path.write_text(json.dumps(started) + "\n")
            initial = transcripts.read_session_delta("codex-normalized", path, 0)
            with path.open("a") as handle:
                handle.write(json.dumps(completed) + "\n")
            delta = transcripts.read_session_delta("codex-normalized", path, initial["cursor"])

        self.assertEqual(len(delta["patches"]), 1)
        patch = delta["patches"][0]
        self.assertEqual(patch["call_id"], "live-command")
        self.assertTrue(patch["partial"])

    def test_round2_f6_blank_agent_message_registers_before_delta(self) -> None:
        rows = [
            self._row("item/started", {"item": {"type": "agentMessage", "id": "live-1", "text": ""}}, 1),
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "live.jsonl"
            path.write_text(json.dumps(rows[0]) + "\n")
            first = transcripts.read_session_events("codex-normalized", path)
            path.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in rows + [
                        self._row("item/agentMessage/delta", {"itemId": "live-1", "delta": "live"}, 2)
                    ]
                )
                + "\n"
            )
            second = transcripts.read_session_events("codex-normalized", path)
        self.assertEqual([(event["kind"], event["text"]) for event in first["events"]], [("assistant", "")])
        self.assertEqual(second["events"][0]["text"], "live")

    def test_round2_f7_aggregate_batch_output_stays_on_outer_envelope(self) -> None:
        parsed = transcripts.read_session_events(
            "codex", FIXTURES_DIR / "codex_wiki266_round2_batch.jsonl"
        )
        tools = [event["tool"] for event in parsed["events"] if event["kind"] == "tool"]
        self.assertEqual(tools[0]["name"], "exec")
        self.assertEqual(tools[0]["output"], "one aggregate result")
        self.assertEqual([tool["output"] for tool in tools[1:]], [None, None])

    def test_round2_f8_explicit_child_call_ids_complete_artifact_and_tool(self) -> None:
        artifact = {
            "kind": "artifact",
            "id": "00000000-0000-4000-8000-000000000266",
            "title": "round 2",
            "caption": "fixture",
            "artifact": {"kind": "mermaid", "source": "graph TD; A-->B"},
            "ts": "2026-08-07T12:00:01Z",
        }
        source = (
            'const rs = await Promise.all(['
            'tools.mcp__wiki_artifacts__render_artifact({kind:"mermaid",payload:{source:"graph TD; A-->B"}}),'
            'tools.exec_command({cmd:"echo hi"})]); text(rs);'
        )
        rows = [
            {"type": "response_item", "timestamp": "2026-08-07T12:00:00Z", "payload": {"type": "custom_tool_call", "call_id": "outer-266", "name": "exec", "input": source}},
            {"type": "response_item", "timestamp": "2026-08-07T12:00:01Z", "payload": {"type": "custom_tool_call_output", "call_id": "outer-266", "output": [
                {"call_id": "artifact-266", "output": sentinel_text(artifact)},
                {"call_id": "command-266", "output": "hi\n", "exit_code": 0},
            ]}},
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "batch-artifact.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            parsed = transcripts.read_session_events("codex", path)
        tools = [event["tool"] for event in parsed["events"] if event["kind"] == "tool"]
        self.assertEqual(tools[1]["call_id"], "artifact-266")
        self.assertEqual(tools[2]["call_id"], "command-266")
        self.assertEqual(tools[2]["output"], "hi\n")
        self.assertEqual(
            [event["artifact_id"] for event in parsed["events"] if event["kind"] == "artifact"],
            [artifact["id"]],
        )

    def test_round4_h1_batch_artifact_child_emits_live_completion_patch(self) -> None:
        artifact = {
            "kind": "artifact",
            "id": "00000000-0000-4000-8000-000000000267",
            "title": "round 4",
            "caption": "fixture",
            "artifact": {"kind": "mermaid", "source": "graph TD; A-->B"},
            "ts": "2026-08-07T12:00:01Z",
        }
        source = (
            'const rs = await Promise.all(['
            'tools.mcp__wiki_artifacts__render_artifact({kind:"mermaid",payload:{source:"graph TD; A-->B"}}),'
            'tools.exec_command({cmd:"echo hi"})]); text(rs);'
        )
        started = {
            "type": "response_item",
            "timestamp": "2026-08-07T12:00:00Z",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "outer-267",
                "name": "exec",
                "input": source,
            },
        }
        completed = {
            "type": "response_item",
            "timestamp": "2026-08-07T12:00:01Z",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "outer-267",
                "output": [
                    {"call_id": "artifact-267", "output": sentinel_text(artifact)},
                    {"call_id": "command-267", "output": "hi\n", "exit_code": 0},
                ],
            },
        }
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "batch-artifact-live.jsonl"
            path.write_text(json.dumps(started) + "\n")
            initial = transcripts.read_session_delta("codex", path, 0)
            path.write_text(
                json.dumps(started) + "\n" + json.dumps(completed) + "\n"
            )
            delta = transcripts.read_session_delta("codex", path, initial["cursor"])

        artifact_patch = next(
            patch for patch in delta["patches"] if patch["call_id"] == "artifact-267"
        )
        self.assertEqual(artifact_patch["output"], sentinel_text(artifact))
        self.assertTrue(artifact_patch["ok"])
        self.assertEqual(
            [event["artifact_id"] for event in delta["events"] if event["kind"] == "artifact"],
            [artifact["id"]],
        )

    def test_round9_m3_batch_artifact_child_starts_pending(self) -> None:
        source = (
            "const rs = await Promise.all(["
            "tools.mcp__wiki_artifacts__render_artifact({kind:\"mermaid\",payload:{source:\"graph TD; A-->B\"}}),"
            "tools.exec_command({cmd:\"echo hi\"})"
            "]); text(rs);"
        )
        row = {
            "type": "response_item",
            "timestamp": "2026-08-07T12:00:00Z",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "batch-pending-artifact",
                "name": "exec",
                "input": source,
            },
        }
        turn_completed = {
            "type": "event_msg",
            "timestamp": "2026-08-07T12:00:02Z",
            "payload": {
                "type": "turn_completed",
                "turn": {"status": "completed"},
            },
        }
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "batch-pending-artifact.jsonl"
            path.write_text(json.dumps(row) + "\n")
            parsed = transcripts.read_session_events("codex", path)
            state = transcripts._cache[str(path)]

            pending_tool = next(
                event["tool"]
                for event in parsed["events"]
                if event["kind"] == "tool"
                and event["tool"]["name"] == "mcp__wiki_artifacts__render_artifact"
            )
            self.assertIsNone(pending_tool["ok"])
            self.assertIsNone(pending_tool["output"])
            self.assertEqual(pending_tool["status"], "inProgress")
            self.assertEqual(
                state["codex_item_lifecycle"][
                    "__codex_batch_child__:batch-pending-artifact:0"
                ]["state"],
                "open",
            )

            path.write_text(json.dumps(row) + "\n" + json.dumps(turn_completed) + "\n")
            parsed = transcripts.read_session_events("codex", path)
            state = transcripts._cache[str(path)]

        tool = next(
            event["tool"]
            for event in parsed["events"]
            if event["kind"] == "tool"
            and event["tool"]["name"] == "mcp__wiki_artifacts__render_artifact"
        )
        self.assertFalse(tool["ok"])
        self.assertIsNone(tool["output"])
        self.assertEqual(tool["status"], "failed")
        self.assertEqual(tool["summary"], "render_artifact pending")
        self.assertEqual(
            state["codex_item_lifecycle"]["__codex_batch_child__:batch-pending-artifact:0"]["state"],
            "terminal",
        )

    def test_round4_h2_interrupt_only_marks_open_current_items_after_trim(self) -> None:
        rows = [
            self._row("turn/started", {"turn": {"id": "prior-turn"}}, 1),
            self._row(
                "item/started",
                {"item": {"type": "agentMessage", "id": "prior", "text": "prior"}},
                2,
            ),
            self._row(
                "item/completed",
                {"item": {"type": "agentMessage", "id": "prior", "text": "prior"}},
                3,
            ),
            self._row("turn/completed", {"turn": {"status": "completed"}}, 4),
            self._row("turn/started", {"turn": {"id": "current-turn"}}, 5),
            self._row(
                "item/started",
                {
                    "item": {
                        "type": "agentMessage",
                        "id": "current-complete",
                        "text": "current complete",
                    }
                },
                6,
            ),
            self._row(
                "item/completed",
                {
                    "item": {
                        "type": "agentMessage",
                        "id": "current-complete",
                        "text": "current complete",
                    }
                },
                7,
            ),
        ]
        for index in range(2000):
            item_id = f"filler-{index}"
            rows.extend(
                [
                    self._row(
                        "item/started",
                        {"item": {"type": "agentMessage", "id": item_id, "text": item_id}},
                        9 + index * 2,
                    ),
                    self._row(
                        "item/completed",
                        {"item": {"type": "agentMessage", "id": item_id, "text": item_id}},
                        10 + index * 2,
                    ),
                ]
            )
        rows.append(
            self._row(
                "item/started",
                {
                    "item": {
                        "type": "agentMessage",
                        "id": "current-open",
                        "text": "current open",
                    }
                },
                5000,
            )
        )
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "interrupt-trimmed.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            transcripts.read_session_events("codex-normalized", path)
            path.write_text(
                path.read_text()
                + json.dumps(
                    self._row(
                        "turn/completed",
                        {"turn": {"status": "interrupted"}},
                        5001,
                    )
                )
                + "\n"
            )
            transcripts.read_session_events("codex-normalized", path)
            state = transcripts._cache[str(path)]

        self.assertNotIn("prior", state["codex_modern_messages"])
        self.assertNotIn("current-complete", state["codex_modern_messages"])
        self.assertIn("current-open", state["codex_modern_messages"])
        self.assertTrue(state["codex_modern_messages"]["current-open"]["partial"])

    def test_round4_h3_successful_native_artifact_has_no_rejected_row(self) -> None:
        artifact = _artifact_protocol_event("mermaid", 268)
        item = {
            "type": "mcpToolCall",
            "id": "native-artifact-268",
            "server": "wiki_artifacts",
            "tool": "render_artifact",
            "arguments": {
                "kind": "mermaid",
                "payload": {"source": "graph TD; A-->B"},
            },
            "status": "inProgress",
        }
        completed = {
            **item,
            "status": "completed",
            "result": {"content": [{"type": "text", "text": sentinel_text(artifact)}]},
        }
        rows = [
            self._row("turn/started", {"turn": {"id": "artifact-turn"}}, 1),
            self._row("item/started", {"item": item}, 2),
            self._row("item/completed", {"item": completed}, 3),
            self._row("turn/completed", {"turn": {"status": "completed"}}, 4),
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "native-artifact-success.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            parsed = transcripts.read_session_events("codex-normalized", path)

        self.assertEqual([event["kind"] for event in parsed["events"]], ["artifact"])
        self.assertEqual(
            [event for event in parsed["events"] if event["kind"] == "tool"], []
        )

    def test_round4_h4_authoritative_item_output_replaces_buffered_delta(self) -> None:
        rows = [
            self._row(
                "item/commandExecution/outputDelta",
                {"itemId": "authoritative-269", "delta": "stale "},
                1,
            ),
            self._row(
                "item/completed",
                {
                    "item": {
                        "type": "commandExecution",
                        "id": "authoritative-269",
                        "command": "echo final",
                        "status": "completed",
                        "aggregatedOutput": "final",
                        "exitCode": 0,
                    }
                },
                2,
            ),
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "authoritative-output.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            parsed = transcripts.read_session_events("codex-normalized", path)

        tools = [event["tool"] for event in parsed["events"] if event["kind"] == "tool"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["output"], "final")

    def test_round5_i1_interrupt_marks_every_delta_stream_kind_partial(self) -> None:
        cases = {
            "agentMessage": [
                self._row(
                    "item/started",
                    {"item": {"type": "agentMessage", "id": "agent-live", "text": ""}},
                    1,
                ),
                self._row(
                    "item/agentMessage/delta",
                    {"itemId": "agent-live", "delta": "unfinished answer"},
                    2,
                ),
            ],
            "reasoning": [
                self._row(
                    "item/started",
                    {"item": {"type": "reasoning", "id": "reasoning-live", "summary": []}},
                    1,
                ),
                self._row(
                    "item/reasoning/summaryTextDelta",
                    {"itemId": "reasoning-live", "delta": "unfinished thought"},
                    2,
                ),
            ],
            "tool output": [
                self._row(
                    "item/started",
                    {
                        "item": {
                            "type": "commandExecution",
                            "id": "tool-live",
                            "command": "echo live",
                        }
                    },
                    1,
                ),
                self._row(
                    "item/commandExecution/outputDelta",
                    {"itemId": "tool-live", "delta": "unfinished output"},
                    2,
                ),
            ],
        }
        for kind, rows in cases.items():
            with self.subTest(kind=kind), TemporaryDirectory() as tmp:
                rows.append(
                    self._row(
                        "turn/completed",
                        {"turn": {"status": "interrupted"}},
                        3,
                    )
                )
                path = Path(tmp) / "interrupt.jsonl"
                path.write_text("".join(json.dumps(row) + "\n" for row in rows))
                parsed = transcripts.read_session_events("codex-normalized", path)

            event = next(event for event in parsed["events"] if event["kind"] != "interrupt")
            if event["kind"] == "tool":
                self.assertTrue(event["tool"].get("partial"), kind)
            else:
                self.assertTrue(event.get("partial"), kind)

    def test_round5_i2_authoritative_completion_drops_every_late_delta_path(self) -> None:
        cases = {
            "agentMessage": [
                self._row(
                    "item/started",
                    {"item": {"type": "agentMessage", "id": "agent-authoritative", "text": ""}},
                    1,
                ),
                self._row(
                    "item/agentMessage/delta",
                    {"itemId": "agent-authoritative", "delta": "before "},
                    2,
                ),
                self._row(
                    "item/completed",
                    {"item": {"type": "agentMessage", "id": "agent-authoritative", "text": "final"}},
                    3,
                ),
                self._row(
                    "item/agentMessage/delta",
                    {"itemId": "agent-authoritative", "delta": "after"},
                    4,
                ),
            ],
            "reasoning": [
                self._row(
                    "item/started",
                    {"item": {"type": "reasoning", "id": "reasoning-authoritative", "summary": []}},
                    1,
                ),
                self._row(
                    "item/reasoning/summaryTextDelta",
                    {"itemId": "reasoning-authoritative", "delta": "before "},
                    2,
                ),
                self._row(
                    "item/completed",
                    {
                        "item": {
                            "type": "reasoning",
                            "id": "reasoning-authoritative",
                            "summary": [{"text": "final"}],
                        }
                    },
                    3,
                ),
                self._row(
                    "item/reasoning/summaryTextDelta",
                    {"itemId": "reasoning-authoritative", "delta": "after"},
                    4,
                ),
            ],
            "tool output": [
                self._row(
                    "item/started",
                    {
                        "item": {
                            "type": "commandExecution",
                            "id": "tool-authoritative",
                            "command": "echo final",
                        }
                    },
                    1,
                ),
                self._row(
                    "item/commandExecution/outputDelta",
                    {"itemId": "tool-authoritative", "delta": "before "},
                    2,
                ),
                self._row(
                    "item/completed",
                    {
                        "item": {
                            "type": "commandExecution",
                            "id": "tool-authoritative",
                            "command": "echo final",
                            "status": "completed",
                            "aggregatedOutput": "final",
                        }
                    },
                    3,
                ),
                self._row(
                    "item/commandExecution/outputDelta",
                    {"itemId": "tool-authoritative", "delta": "after"},
                    4,
                ),
            ],
        }
        for kind, rows in cases.items():
            with self.subTest(kind=kind), TemporaryDirectory() as tmp:
                rows.append(
                    self._row(
                        "turn/completed",
                        {"turn": {"status": "completed"}},
                        5,
                    )
                )
                path = Path(tmp) / "authoritative.jsonl"
                path.write_text("".join(json.dumps(row) + "\n" for row in rows))
                parsed = transcripts.read_session_events("codex-normalized", path)

            events = [event for event in parsed["events"] if event["kind"] != "interrupt"]
            self.assertEqual(len(events), 1, kind)
            event = events[0]
            value = event["tool"]["output"] if event["kind"] == "tool" else event["text"]
            self.assertEqual(value, "final", kind)
            self.assertNotIn("after", value, kind)

    def test_round3_g5_successful_turn_closes_pending_tools(self) -> None:
        rows = [
            self._row(
                "item/started",
                {"item": {"type": "commandExecution", "id": "successful-pending", "command": "echo hi"}},
                1,
            ),
            self._row("turn/completed", {"turn": {"status": "completed"}}, 2),
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "successful-turn.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            parsed = transcripts.read_session_events("codex-normalized", path)

        tool = next(event["tool"] for event in parsed["events"] if event["kind"] == "tool")
        self.assertTrue(tool["ok"])
        self.assertEqual(tool["status"], "completed")
        self.assertIsNotNone(tool["completed_at"])

    def test_legacy_result_first_is_buffered_until_call_identity_arrives(self) -> None:
        rows = [
            {
                "type": "response_item",
                "timestamp": "2026-08-07T12:00:00Z",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-first",
                    "output": "done\n",
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-08-07T12:00:01Z",
                "payload": {
                    "type": "function_call",
                    "call_id": "call-first",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "printf done"}),
                },
            },
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            parsed = transcripts.read_session_events("codex", path)

        tools = [event["tool"] for event in parsed["events"] if event["kind"] == "tool"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["output"], "done\n")
        self.assertTrue(tools[0]["ok"])


def _write_rollout(day_dir: Path, name: str, cwd: str, session_id: str,
                   kickoff_ticket: str | None = None, mtime: float | None = None) -> Path:
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-{name}.jsonl"
    lines = [json.dumps({"payload": {"cwd": cwd, "id": session_id}})]
    if kickoff_ticket:
        lines.append(json.dumps({
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": f"You are worker for ticket {kickoff_ticket}. Go.",
            },
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _write_claude_rollout(project_dir: Path, session_id: str, *,
                          cwd: str, kickoff_ticket: str | None = None,
                          mtime: float | None = None) -> Path:
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{session_id}.jsonl"
    lines = [json.dumps({"payload": {"cwd": cwd, "id": session_id}})]
    if kickoff_ticket:
        lines.append(json.dumps({
            "type": "user",
            "timestamp": "2026-07-09T01:00:00Z",
            "message": {"content": f"You are a worker for Linear ticket {kickoff_ticket}. Go."},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class NormalizedUserSourceTests(unittest.TestCase):
    """WIKI-161: normalized archived events must preserve `source` on user
    turns so archived synthetic messages keep rendering as marker rows."""

    def _parse(self, kind: str, envelope: dict) -> dict:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
            return transcripts.read_session_events(kind, path)

    def test_codex_normalized_user_event_carries_source(self) -> None:
        envelope = {
            "seq": 5,
            "raw_seq": 5,
            "normalized_at": "2026-07-22T20:00:00+00:00",
            "disposition": "rendered",
            "kind": "item_completed",
            "payload": {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "[fleet] merged"}],
                    }
                },
                "source": "fleet-monitor",
            },
        }
        parsed = self._parse("codex-normalized", envelope)
        user = [event for event in parsed["events"] if event["kind"] == "user"]
        self.assertEqual(len(user), 1)
        self.assertEqual(user[0]["source"], "fleet-monitor")

    def test_codex_normalized_user_event_without_source_stays_untagged(self) -> None:
        envelope = {
            "seq": 6,
            "raw_seq": 6,
            "normalized_at": "2026-07-22T20:00:05+00:00",
            "disposition": "rendered",
            "kind": "item_completed",
            "payload": {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "hi henry"}],
                    }
                },
            },
        }
        parsed = self._parse("codex-normalized", envelope)
        user = [event for event in parsed["events"] if event["kind"] == "user"]
        self.assertEqual(len(user), 1)
        self.assertNotIn("source", user[0])

    def test_codex_normalized_rejects_invalid_source_shape(self) -> None:
        envelope = {
            "seq": 7,
            "raw_seq": 7,
            "normalized_at": "2026-07-22T20:00:10+00:00",
            "disposition": "rendered",
            "kind": "item_completed",
            "payload": {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "attempt"}],
                    }
                },
                # Untrusted archive rows may carry hostile shapes; the parser
                # should silently drop anything the ingest validator would.
                "source": "has spaces",
            },
        }
        parsed = self._parse("codex-normalized", envelope)
        user = [event for event in parsed["events"] if event["kind"] == "user"]
        self.assertEqual(len(user), 1)
        self.assertNotIn("source", user[0])

    def test_claude_normalized_user_event_carries_source(self) -> None:
        envelope = {
            "seq": 8,
            "raw_seq": 8,
            "normalized_at": "2026-07-22T20:00:15+00:00",
            "disposition": "rendered",
            "kind": "claude_user",
            "payload": {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "[steer] tighten focus"}],
                },
                "source": "supervisor-steer",
            },
        }
        parsed = self._parse("claude-normalized", envelope)
        user = [event for event in parsed["events"] if event["kind"] == "user"]
        self.assertEqual(len(user), 1)
        self.assertEqual(user[0]["source"], "supervisor-steer")


class RegistryIdBeatsDiscoveryTests(unittest.TestCase):
    def test_registry_id_confines_result_to_anchor_cwd(self) -> None:
        """Discovery mode has a stale-worker foot-gun: a NEWER rollout in a
        DIFFERENT cwd (a sibling ticket, wrong resume, etc.) that also matches
        the kickoff regex will win. Registry-id mode must anchor to the pinned
        rollout's cwd — the sibling in another cwd MUST NOT be returned."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            now = datetime.now(tz=timezone.utc)
            day_dir = root / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.day:02d}"
            worktree = Path(tmp) / "wiki-15"
            other_cwd = Path(tmp) / "wiki-15-copy"
            # NEWER kickoff-matched rollout in a DIFFERENT cwd (would win under
            # discovery-only rules).
            _write_rollout(day_dir, "decoy", cwd=str(other_cwd),
                           session_id="sess-decoy",
                           kickoff_ticket="WIKI-15", mtime=time.time())
            # Anchor pinned by registry, older, worktree cwd.
            _write_rollout(day_dir, "pinned", cwd=str(worktree),
                           session_id="sess-pinned",
                           kickoff_ticket="WIKI-15", mtime=time.time() - 3600)
            with mock.patch.object(transcripts, "CODEX_SESSIONS_DIR", root):
                without_id = transcripts.find_codex_session(
                    "WIKI-15", now.isoformat(), session_id=None
                )
                with_id = transcripts.find_codex_session(
                    "WIKI-15", now.isoformat(), session_id="sess-pinned"
                )
            self.assertIsNotNone(without_id)
            self.assertIsNotNone(with_id)
            # Discovery picks the decoy — that's the whole point of the id path.
            self.assertEqual(without_id.name, "rollout-decoy.jsonl")
            # Registry-id path stays confined to anchor cwd.
            self.assertEqual(with_id.name, "rollout-pinned.jsonl")

    def test_registry_id_returns_anchor_when_no_newer_chain(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            now = datetime.now(tz=timezone.utc)
            day_dir = root / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.day:02d}"
            worktree = Path(tmp) / "wiki-15"
            # Only one rollout in cwd — the pinned one.
            _write_rollout(day_dir, "pinned", cwd=str(worktree),
                           session_id="sess-pinned",
                           kickoff_ticket="WIKI-15", mtime=time.time())
            # An unrelated rollout in a different cwd should NOT be chained in.
            _write_rollout(day_dir, "other", cwd=str(Path(tmp) / "other-wt"),
                           session_id="sess-other",
                           kickoff_ticket="WIKI-99", mtime=time.time() + 10)
            with mock.patch.object(transcripts, "CODEX_SESSIONS_DIR", root):
                found = transcripts.find_codex_session(
                    "WIKI-15", now.isoformat(), session_id="sess-pinned"
                )
            self.assertIsNotNone(found)
            self.assertEqual(found.name, "rollout-pinned.jsonl")


class ClaudeSessionIdResolverTests(unittest.TestCase):
    def test_session_id_primary_path_wins_without_slug_match(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / ".claude" / "projects"
            session_id = "599b561a-7d40-4492-ab82-35a3ae91f733"
            now = datetime.now(tz=timezone.utc)
            project_dir = root / "-Users-henry-me-fun-phoebe--claude-worktrees-pr10475-eval-rerun"
            expected = _write_claude_rollout(
                project_dir,
                session_id,
                cwd="/Users/henry/me/fun/phoebe/claude-worktrees/pr10475-eval-rerun",
                mtime=time.time(),
            )
            with mock.patch.object(transcripts, "CLAUDE_PROJECTS_DIR", root):
                found = transcripts.find_session(
                    "cc", "PR-10475", now.isoformat(), session_id=session_id
                )
            self.assertIsNotNone(found)
            self.assertEqual(found, ("claude", expected))

    def test_slug_glob_fallback_remains_when_session_id_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / ".claude" / "projects"
            now = datetime.now(tz=timezone.utc)
            project_dir = root / "-Users-henry-me-fun-phoebe--claude-worktrees-pr10475-eval-rerun"
            expected = _write_claude_rollout(
                project_dir,
                "fallback-session",
                cwd="/Users/henry/me/fun/phoebe/claude-worktrees/pr10475-eval-rerun",
                kickoff_ticket="PR-10475",
                mtime=time.time(),
            )
            with mock.patch.object(transcripts, "CLAUDE_PROJECTS_DIR", root):
                found = transcripts.find_session("cc", "PR-10475", now.isoformat())
            self.assertIsNotNone(found)
            self.assertEqual(found, ("claude", expected))

    def test_session_id_beats_newer_slug_match(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / ".claude" / "projects"
            now = datetime.now(tz=timezone.utc)
            pinned_dir = root / "eval-rerun"
            slug_dir = root / "-Users-henry-me-fun-phoebe--claude-worktrees-pr10475"
            pinned = _write_claude_rollout(
                pinned_dir,
                "599b561a-7d40-4492-ab82-35a3ae91f733",
                cwd="/Users/henry/me/fun/phoebe/claude-worktrees/pr10475-eval-rerun",
                mtime=time.time() - 3600,
            )
            slug = _write_claude_rollout(
                slug_dir,
                "slug-session",
                cwd="/Users/henry/me/fun/phoebe/claude-worktrees/pr10475",
                kickoff_ticket="PR-10475",
                mtime=time.time(),
            )
            with mock.patch.object(transcripts, "CLAUDE_PROJECTS_DIR", root):
                pinned_found = transcripts.find_session(
                    "cc", "PR-10475", now.isoformat(), session_id="599b561a-7d40-4492-ab82-35a3ae91f733"
                )
                slug_found = transcripts.find_session("cc", "PR-10475", now.isoformat())
            self.assertEqual(pinned_found, ("claude", pinned))
            self.assertEqual(slug_found, ("claude", slug))


class SameCwdChainRuleTests(unittest.TestCase):
    """`codex resume <id>` REUSES the anchor rollout file (verified live
    2026-07-08 via open file handles), so the exact-id file IS the live
    session. The same-cwd chain caused cross-ticket bleed for sessions
    sharing a cwd and now applies ONLY when the anchor file vanished."""

    def test_exact_anchor_wins_over_newer_same_cwd_sibling(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            now = datetime.now(tz=timezone.utc)
            day_dir = root / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.day:02d}"
            shared_cwd = Path(tmp) / "repo-root"
            older = time.time() - 3600
            newer = time.time()
            _write_rollout(day_dir, "anchor", cwd=str(shared_cwd),
                           session_id="sess-anchor",
                           kickoff_ticket="WIKI-15", mtime=older)
            # A DIFFERENT ticket's newer session in the same cwd must not win.
            _write_rollout(day_dir, "other-ticket", cwd=str(shared_cwd),
                           session_id="sess-other", mtime=newer)
            with mock.patch.object(transcripts, "CODEX_SESSIONS_DIR", root):
                found = transcripts.find_codex_session(
                    "WIKI-15", now.isoformat(), session_id="sess-anchor"
                )
            self.assertIsNotNone(found)
            self.assertEqual(found.name, "rollout-anchor.jsonl")


class TranscriptSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        transcripts._cache.clear()

    def test_claude_native_surfaces_fixture(self) -> None:
        path = FIXTURES_DIR / "claude_native_surfaces.jsonl"
        result = transcripts.read_session_events("claude", path)

        self.assertEqual(
            result["dispositions"],
            {"rendered": 10, "summarized": 2, "ignored": 3, "unknown": 0},
        )
        self.assertEqual(
            result["session_meta"],
            {"custom_title": "Fixture transcript", "agent_name": "wiki worker"},
        )

        questions = [event for event in result["events"] if event["kind"] == "question"]
        self.assertEqual(len(questions), 2)
        self.assertEqual(questions[0]["question"]["tool_use_id"], "toolu_question")
        self.assertFalse(questions[0]["question"]["multi_select"])
        self.assertEqual(questions[0]["question"]["answered_option"], 1)
        self.assertEqual(questions[0]["question"]["answered_options"], [1])
        self.assertIsNone(questions[0]["question"]["custom_reply"])
        self.assertIsNone(questions[1]["question"]["answered_option"])
        self.assertEqual(questions[1]["question"]["answered_options"], [])
        self.assertEqual(questions[1]["question"]["custom_reply"], "Go with the fresh branch")

        markers = {(event.get("marker"), event["text"]) for event in result["events"] if event["kind"] == "marker"}
        self.assertIn(("permission-mode", "permissions · bypass permissions"), markers)
        self.assertIn(("progress", "Sampling fixtures and wiring renderers."), markers)
        self.assertTrue(any(event.get("marker") == "tool_reference" for event in result["events"]))
        self.assertTrue(any(event["kind"] == "image" and event["text"].startswith("/api/transcript-images/") for event in result["events"]))
        self.assertEqual(result["tasks"][0]["status"], "in_progress")

    def test_claude_provider_stream_renderers_preserve_structured_payloads(self) -> None:
        rows = [
            {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 12, "estimated_tokens_delta": 3},
            {"type": "system", "subtype": "init", "model": "opus-4.7", "cwd": "/Users/henry/me/fun/wiki"},
            {"type": "system", "subtype": "task_notification", "task_id": "task-1", "status": "running", "summary": "build", "output_file": "/tmp/task.out"},
            {"type": "system", "subtype": "task_updated", "task_id": "task-1", "patch": {"status": "completed", "summary": "build done"}},
            {"type": "system", "subtype": "api_retry", "attempt": 2, "max_retries": 3, "error_status": "529", "retry_delay_ms": 500},
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": "rejected",
                    "rateLimitType": "five_hour",
                    "isUsingOverage": False,
                    "overageStatus": "rejected",
                    "overageDisabledReason": "out_of_credits",
                    "resetsAt": 2_000_000_000,
                },
            },
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "claude-provider-renderers.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            result = transcripts.read_session_events("claude", path)

        self.assertEqual(result["dispositions"], {"rendered": 5, "summarized": 1, "ignored": 0, "unknown": 0})
        kinds = [event["kind"] for event in result["events"]]
        self.assertEqual(kinds, ["claude_init", "claude_task", "claude_api_retry", "claude_rate_limit"])
        task = result["events"][1]["claude_task"]
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["summary"], "build done")
        self.assertEqual(result["session_meta"]["thinking_tokens"]["total"], 12)
        self.assertEqual(result["session_meta"]["rate_limit"]["status"], "rejected")
        self.assertFalse(result["session_meta"]["rate_limit"]["isUsingOverage"])
        self.assertEqual(result["session_meta"]["rate_limit"]["overageStatus"], "rejected")
        self.assertEqual(result["session_meta"]["rate_limit"]["overageDisabledReason"], "out_of_credits")

    def test_codex_native_surfaces_fixture(self) -> None:
        path = FIXTURES_DIR / "codex_native_surfaces.jsonl"
        result = transcripts.read_session_events("codex", path)

        self.assertEqual(
            result["dispositions"],
            {"rendered": 6, "summarized": 1, "ignored": 3, "unknown": 1},
        )
        thinking = next(event for event in result["events"] if event["kind"] == "thinking")
        self.assertTrue(thinking["encrypted"])
        self.assertEqual(result["tokens"], 3210)
        self.assertTrue(any(event.get("marker") == "task_started" for event in result["events"]))
        self.assertTrue(any(event.get("marker") == "subagent" for event in result["events"]))
        self.assertTrue(any(event.get("marker") == "task_complete" for event in result["events"]))
        tool = next(event for event in result["events"] if event["kind"] == "tool")
        self.assertEqual(tool["tool"]["output"], "done\nexited with code 0")
        self.assertTrue(tool["tool"]["ok"])

    def test_codex_reasoning_keeps_detailed_multiline_summary(self) -> None:
        rows = [
            {
                "type": "response_item",
                "timestamp": "2026-08-07T12:00:00Z",
                "payload": {
                    "type": "reasoning",
                    "summary": [
                        {
                            "text": (
                                "**Planning gate loop verification and CI checks**\n"
                                "Inspect every supervisor-owned role.\n"
                                "Keep the shared renderer unchanged."
                            )
                        }
                    ],
                    "encrypted_content": "opaque-codex-reasoning",
                },
            }
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex-detailed-reasoning.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            result = transcripts.read_session_events("codex", path)

        thinking = result["events"][0]
        self.assertEqual(thinking["kind"], "thinking")
        self.assertTrue(thinking["encrypted"])
        self.assertEqual(
            thinking["text"],
            "**Planning gate loop verification and CI checks**\n"
            "Inspect every supervisor-owned role.\n"
            "Keep the shared renderer unchanged.",
        )

    def test_codex_reasoning_fragment_fixture_splits_only_complete_sequences(self) -> None:
        path = FIXTURES_DIR / "codex_reasoning_fragments.jsonl"
        result = transcripts.read_session_events("codex", path)
        thinking = [event for event in result["events"] if event["kind"] == "thinking"]

        self.assertEqual(
            [event["text"] for event in thinking],
            [
                "**thought one**",
                "**thought two**",
                "**thought three**",
                "**thought four**",
                "**thought five** and **thought six**",
                "**thought **nested**",
                "**thought seven",
            ],
        )
        self.assertEqual(
            [event["ts"] for event in thinking],
            [
                "2026-08-07T15:00:00Z",
                "2026-08-07T15:00:00Z",
                "2026-08-07T15:00:01Z",
                "2026-08-07T15:00:01Z",
                "2026-08-07T15:00:02Z",
                "2026-08-07T15:00:03Z",
                "2026-08-07T15:00:04Z",
            ],
        )
        self.assertTrue(all(event["encrypted"] for event in thinking))

    def test_equivalent_claude_and_codex_tools_share_canonical_fields(self) -> None:
        claude_rows = [
            {
                "type": "assistant",
                "timestamp": "2026-08-07T12:00:00Z",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "claude-read",
                            "name": "Read",
                            "input": {"file_path": "src/main.py"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "timestamp": "2026-08-07T12:00:01Z",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "claude-read",
                            "content": "line one",
                        }
                    ]
                },
            },
        ]
        codex_rows = [
            {
                "type": "response_item",
                "timestamp": "2026-08-07T12:00:00Z",
                "payload": {
                    "type": "function_call",
                    "call_id": "codex-read",
                    "name": "Read",
                    "arguments": json.dumps({"file_path": "src/main.py"}),
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-08-07T12:00:01Z",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "codex-read",
                    "output": "line one",
                },
            },
        ]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_path = root / "claude.jsonl"
            codex_path = root / "codex.jsonl"
            claude_path.write_text("\n".join(json.dumps(row) for row in claude_rows) + "\n")
            codex_path.write_text("\n".join(json.dumps(row) for row in codex_rows) + "\n")
            claude_tool = next(
                event["tool"]
                for event in transcripts.read_session_events("claude", claude_path)["events"]
                if event["kind"] == "tool"
            )
            codex_tool = next(
                event["tool"]
                for event in transcripts.read_session_events("codex", codex_path)["events"]
                if event["kind"] == "tool"
            )

        for field in ("name", "input", "output", "ok", "archetype", "summary"):
            with self.subTest(field=field):
                self.assertEqual(codex_tool[field], claude_tool[field])

    def test_multi_select_question_preserves_all_answers_and_custom_reply(self) -> None:
        path = FIXTURES_DIR / "agent_runtime" / "claude_stream_native_surfaces.jsonl"
        result = transcripts.read_session_events("claude", path)

        questions = [
            event["question"]
            for event in result["events"]
            if event.get("question", {}).get("tool_use_id") == "toolu_ask"
        ]
        self.assertEqual(len(questions), 2)
        self.assertFalse(questions[0]["multi_select"])
        self.assertEqual(questions[0]["answered_option"], 0)
        self.assertTrue(questions[1]["multi_select"])
        self.assertIsNone(questions[1]["answered_option"])
        self.assertEqual(questions[1]["answered_options"], [0, 1])
        self.assertEqual(questions[1]["custom_reply"], "Include typed Other text")

    def test_structured_question_answers_use_tool_use_result_map(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "structured-answers.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "assistant",
                                "timestamp": "2026-07-10T17:31:04.506Z",
                                "message": {
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "tool_use",
                                            "id": "toolu_question",
                                            "name": "AskUserQuestion",
                                            "input": {
                                                "questions": [
                                                    {
                                                        "question": "Which path should I take?",
                                                        "header": "Path choice",
                                                        "multiSelect": False,
                                                        "options": [
                                                            {"label": "Path A"},
                                                            {"label": "Path B"},
                                                            {"label": "Path C"},
                                                        ],
                                                    }
                                                ]
                                            },
                                        }
                                    ],
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "user",
                                "timestamp": "2026-07-10T17:31:16.659Z",
                                "message": {
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "tool_result",
                                            "tool_use_id": "toolu_question",
                                            "content": "Your questions have been answered: . You can now continue with these answers in mind.",
                                        }
                                    ],
                                },
                                "toolUseResult": {
                                    "questions": [
                                        {
                                            "question": "Which path should I take?",
                                            "header": "Path choice",
                                            "multiSelect": False,
                                            "options": [
                                                {"label": "Path A"},
                                                {"label": "Path B"},
                                                {"label": "Path C"},
                                            ],
                                        }
                                    ],
                                    "answers": {
                                        "Which path should I take?": "Path B",
                                    },
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            result = transcripts.read_session_events("claude", path)

        question = next(event for event in result["events"] if event["kind"] == "question")
        self.assertFalse(question["question"]["multi_select"])
        self.assertEqual(question["question"]["answered_option"], 1)
        self.assertEqual(question["question"]["answered_options"], [1])
        self.assertIsNone(question["question"]["custom_reply"])


class TranscriptEditPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        transcripts._cache.clear()
        transcripts._cache_locks.clear()

    def test_raw_claude_edit_survives_normalization_for_renderer(self) -> None:
        raw_rows = [
            json.loads(line)
            for line in (FIXTURES_DIR / "claude_edit_raw.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        envelopes = []
        for raw in raw_rows:
            normalized = normalize_provider_event("claude", raw)
            envelopes.append({
                "kind": normalized.kind,
                "disposition": normalized.disposition.value,
                "normalized_at": raw["timestamp"],
                "payload": normalized.payload,
            })
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(
                "\n".join(json.dumps(envelope) for envelope in envelopes) + "\n",
                encoding="utf-8",
            )
            result = transcripts.read_session_events("claude-normalized", path)

        tool = result["events"][0]["tool"]
        self.assertEqual(
            tool["edit"],
            {
                "file_path": "/Users/henry/me/fun/wiki/.claude/worktrees/wiki-153/frontend/src/ansi.tsx",
                "old_string": "function classNamesFor(style: AnsiStyle): string {",
                "new_string": "export function classNamesFor(style: AnsiStyle): string {",
                "replace_all": False,
            },
        )
        self.assertIn("updated successfully", tool["output"])

    def test_edit_payload_strings_are_bounded(self) -> None:
        long_text = "x" * (transcripts.MAX_EDIT_PAYLOAD + 500)
        payload = {
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use",
                "id": "toolu-long-edit",
                "name": "Edit",
                "input": {
                    "file_path": "hot.md",
                    "old_string": long_text,
                    "new_string": long_text,
                },
            }]},
        }
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "claude.jsonl"
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            result = transcripts.read_session_events("claude", path)

        edit = result["events"][0]["tool"]["edit"]
        self.assertLessEqual(len(edit["old_string"]), transcripts.MAX_EDIT_PAYLOAD + 64)
        self.assertEqual(len(edit["old_string"]), transcripts.MAX_EDIT_PAYLOAD)
        self.assertNotIn("truncated", edit["old_string"])
        self.assertTrue(edit["old_string_truncated"])
        self.assertTrue(edit["new_string_truncated"])

    def test_cache_version_rebuilds_old_normalized_events(self) -> None:
        payload = {
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use",
                "id": "toolu-cache-edit",
                "name": "Edit",
                "input": {
                    "file_path": "hot.md",
                    "old_string": "old",
                    "new_string": "new",
                },
            }]},
        }
        envelope = {
            "kind": "claude_assistant",
            "disposition": "rendered",
            "normalized_at": "2026-08-06T00:00:00Z",
            "payload": payload,
        }
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
            transcripts.read_session_events("claude-normalized", path)
            transcripts._cache[str(path)]["cache_version"] = transcripts.TRANSCRIPT_CACHE_VERSION - 1
            rebuilt = transcripts.read_session_events("claude-normalized", path)

        self.assertEqual(rebuilt["events"][0]["tool"]["edit"]["new_string"], "new")


if __name__ == "__main__":
    unittest.main()
