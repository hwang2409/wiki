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
        self.assertEqual(len(tools), 4)

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

        multi_tool = tools[2]
        self.assertTrue(multi_tool["input"].startswith("```js\nconst results"))
        self.assertTrue(multi_tool["input"].endswith("\n```"))
        self.assertEqual(multi_tool["archetype"], "git")
        self.assertEqual(multi_tool["summary"], "git status")
        self.assertEqual(multi_tool["output"], "## git\n## wiki-main\n## rg\n1034: custom_tool_call\n")
        self.assertTrue(multi_tool["ok"])

        failed_tool = tools[3]
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

        malformed = tools[8]
        self.assertEqual(malformed["name"], "exec")
        self.assertIn("tools.exec_command(args)", malformed["input"])
        self.assertEqual(malformed["archetype"], "run")

    def test_harness_scanner_ignores_strings_comments_and_unsupported_args(self) -> None:
        cases = [
            'const text = "tools.exec_command({cmd: \\"hidden\\"})";',
            "// tools.exec_command({cmd: 'hidden'})",
            'const args = {cmd: "echo hi"}; tools.exec_command(args);',
            'tools.exec_command({cmd: foo_null});',
            'tools.exec_command({cmd: `echo hi`});',
            'tools.exec_command({cmd: "echo hi"',
        ]
        for source in cases:
            with self.subTest(source=source):
                self.assertIsNone(transcripts._codex_harness_tool("exec", source))

        comments = "tools.exec_command" + ("/* comment */" * 2000) + '({cmd: "echo hi"});'
        self.assertIsNotNone(transcripts._codex_harness_tool("exec", comments))


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
