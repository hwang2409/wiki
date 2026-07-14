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
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from backend.app import transcripts
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
                self.assertFalse(event["tool"]["ok"])
                self.assertEqual(event["tool"]["summary"], "render_artifact rejected")


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


if __name__ == "__main__":
    unittest.main()
