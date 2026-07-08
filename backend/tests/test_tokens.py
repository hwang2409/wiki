"""Tests for backend.app.tokens — session-file scanner + bucket aggregator.

Every path here is a temp dir. No real session files, no real cache."""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


class _EnvOverride:
    def __init__(self) -> None:
        self._tmp: TemporaryDirectory[str] | None = None
        self._patch = None

    def __enter__(self) -> dict[str, Path]:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        paths = {
            "root": root,
            "codex": root / "codex-sessions",
            "claude": root / "claude-projects",
            "cache": root / "token-cache.json",
        }
        paths["codex"].mkdir(parents=True, exist_ok=True)
        paths["claude"].mkdir(parents=True, exist_ok=True)
        env = {
            "WIKI_CODEX_SESSIONS_DIR": str(paths["codex"]),
            "WIKI_CLAUDE_PROJECTS_DIR": str(paths["claude"]),
            "WIKI_TOKEN_CACHE_PATH": str(paths["cache"]),
        }
        self._patch = mock.patch.dict(os.environ, env)
        self._patch.start()
        return paths

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._patch is not None:
            self._patch.stop()
        if self._tmp is not None:
            self._tmp.cleanup()


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _codex_token_row(ts: str, cum: dict[str, int]) -> dict:
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": cum["input"],
                    "cached_input_tokens": cum.get("cached", 0),
                    "output_tokens": cum.get("output", 0),
                    "reasoning_output_tokens": cum.get("reasoning", 0),
                    "total_tokens": (
                        cum["input"]
                        + cum.get("cached", 0)
                        + cum.get("output", 0)
                        + cum.get("reasoning", 0)
                    ),
                },
            },
        },
    }


def _codex_turn_context(ts: str, model: str) -> dict:
    return {"timestamp": ts, "type": "turn_context", "payload": {"model": model}}


def _codex_session_meta(ts: str, model: str = "gpt-5.4") -> dict:
    return {
        "timestamp": ts,
        "type": "session_meta",
        "payload": {"id": "abc", "model": model, "cwd": "/tmp/x"},
    }


def _claude_assistant_row(ts: str, msg_id: str, model: str, usage: dict) -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {"id": msg_id, "model": model, "usage": usage},
    }


class CodexDeltaTests(unittest.TestCase):
    def test_cumulative_to_per_event_deltas(self) -> None:
        with _EnvOverride() as paths:
            from backend.app import tokens

            day = paths["codex"] / "2026/07/08"
            rows = [
                _codex_session_meta("2026-07-08T18:00:00Z", "gpt-5.4"),
                _codex_token_row(
                    "2026-07-08T18:00:10Z",
                    {"input": 100, "cached": 50, "output": 20, "reasoning": 5},
                ),
                _codex_token_row(
                    "2026-07-08T18:15:00Z",
                    {"input": 300, "cached": 150, "output": 60, "reasoning": 10},
                ),
            ]
            _write_jsonl(day / "rollout-abc.jsonl", rows)
            state = tokens.refresh()

            # One hour bucket ts, one series (codex/gpt-5.4).
            self.assertEqual(len(state["buckets"]), 1)
            b = state["buckets"][0]
            self.assertEqual(b["cli"], "codex")
            self.assertEqual(b["model"], "gpt-5.4")
            # First event delta = cumulative (100/50/20/5), second event
            # delta = diff (200/100/40/5). Summed = 300/150/60/10.
            self.assertEqual(b["input"], 300)
            self.assertEqual(b["cached"], 150)
            self.assertEqual(b["output"], 60)
            self.assertEqual(b["reasoning"], 10)

    def test_resume_reset_clamps_negative_delta(self) -> None:
        with _EnvOverride() as paths:
            from backend.app import tokens

            day = paths["codex"] / "2026/07/08"
            rows = [
                _codex_session_meta("2026-07-08T18:00:00Z"),
                _codex_token_row("2026-07-08T18:00:10Z", {"input": 500, "output": 100}),
                # Session resumed → counters restart. Should not go negative.
                _codex_token_row("2026-07-08T18:10:00Z", {"input": 10, "output": 2}),
                # New baseline established, next event grows.
                _codex_token_row("2026-07-08T18:20:00Z", {"input": 30, "output": 5}),
            ]
            _write_jsonl(day / "rollout-x.jsonl", rows)
            state = tokens.refresh()
            b = next(x for x in state["buckets"])
            # 500 (first) + 0 (reset, clamped) + 20 (30 - 10) = 520.
            self.assertEqual(b["input"], 520)  # 500 + 0 + (30-10)
            self.assertEqual(b["output"], 103)  # 100 + 0 + (5-2)

    def test_model_attribution_follows_turn_context(self) -> None:
        with _EnvOverride() as paths:
            from backend.app import tokens

            day = paths["codex"] / "2026/07/08"
            rows = [
                _codex_session_meta("2026-07-08T18:00:00Z", "gpt-5.4"),
                _codex_token_row("2026-07-08T18:00:10Z", {"input": 100}),
                _codex_turn_context("2026-07-08T18:01:00Z", "gpt-5.5"),
                _codex_token_row("2026-07-08T18:01:10Z", {"input": 300}),
            ]
            _write_jsonl(day / "rollout-y.jsonl", rows)
            state = tokens.refresh()
            models = {b["model"]: b for b in state["buckets"]}
            self.assertIn("gpt-5.4", models)
            self.assertIn("gpt-5.5", models)
            self.assertEqual(models["gpt-5.4"]["input"], 100)
            self.assertEqual(models["gpt-5.5"]["input"], 200)  # 300 - 100


class ClaudeUsageTests(unittest.TestCase):
    def test_extracts_per_message_usage(self) -> None:
        with _EnvOverride() as paths:
            from backend.app import tokens

            project = paths["claude"] / "-Users-h-proj"
            rows = [
                _claude_assistant_row(
                    "2026-07-08T18:00:00Z",
                    "msg_1",
                    "claude-opus-4-7",
                    {
                        "input_tokens": 6,
                        "output_tokens": 445,
                        "cache_read_input_tokens": 22913,
                        "cache_creation_input_tokens": 55658,
                    },
                ),
            ]
            _write_jsonl(project / "session.jsonl", rows)
            state = tokens.refresh()
            self.assertEqual(len(state["buckets"]), 1)
            b = state["buckets"][0]
            self.assertEqual(b["cli"], "claude")
            self.assertEqual(b["model"], "claude-opus-4-7")
            self.assertEqual(b["input"], 6)
            self.assertEqual(b["output"], 445)
            # cached = read + creation combined.
            self.assertEqual(b["cached"], 22913 + 55658)

    def test_dedupes_repeated_message_ids(self) -> None:
        with _EnvOverride() as paths:
            from backend.app import tokens

            project = paths["claude"] / "proj"
            usage = {"input_tokens": 10, "output_tokens": 20}
            rows = [
                _claude_assistant_row("2026-07-08T18:00:00Z", "msg_x", "sonnet", usage),
                _claude_assistant_row("2026-07-08T18:00:00Z", "msg_x", "sonnet", usage),
                _claude_assistant_row("2026-07-08T18:00:00Z", "msg_x", "sonnet", usage),
            ]
            _write_jsonl(project / "s.jsonl", rows)
            state = tokens.refresh()
            b = state["buckets"][0]
            # Deduped down to one — should not be tripled.
            self.assertEqual(b["input"], 10)
            self.assertEqual(b["output"], 20)


class BucketAggregationTests(unittest.TestCase):
    def test_hourly_and_daily_rollups(self) -> None:
        with _EnvOverride() as paths:
            from backend.app import tokens

            day = paths["codex"] / "2026/07/08"
            rows = [
                _codex_session_meta("2026-07-08T18:00:00Z", "gpt-5.4"),
                _codex_token_row("2026-07-08T18:00:10Z", {"input": 100}),
                _codex_token_row("2026-07-08T19:00:00Z", {"input": 300}),
                _codex_token_row("2026-07-08T19:30:00Z", {"input": 500}),
            ]
            _write_jsonl(day / "rollout-a.jsonl", rows)
            tokens.refresh()

            hourly = tokens.query(bucket="hour")
            self.assertEqual(len(hourly["buckets"]), 2)
            first_hour = hourly["buckets"][0]
            self.assertEqual(first_hour["series"]["codex/gpt-5.4"]["input"], 100)
            second_hour = hourly["buckets"][1]
            self.assertEqual(second_hour["series"]["codex/gpt-5.4"]["input"], 400)  # 200 + 200

            daily = tokens.query(bucket="day")
            self.assertEqual(len(daily["buckets"]), 1)
            self.assertEqual(daily["buckets"][0]["series"]["codex/gpt-5.4"]["input"], 500)

    def test_filters_by_cli_and_model(self) -> None:
        with _EnvOverride() as paths:
            from backend.app import tokens

            _write_jsonl(
                paths["codex"] / "2026/07/08/rollout-c.jsonl",
                [
                    _codex_session_meta("2026-07-08T18:00:00Z", "gpt-5.4"),
                    _codex_token_row("2026-07-08T18:00:10Z", {"input": 100}),
                ],
            )
            _write_jsonl(
                paths["claude"] / "p/s.jsonl",
                [
                    _claude_assistant_row(
                        "2026-07-08T18:00:00Z", "m1", "sonnet", {"input_tokens": 50, "output_tokens": 5}
                    )
                ],
            )
            tokens.refresh()

            only_codex = tokens.query(cli="codex")
            self.assertEqual(only_codex["totals"]["input"], 100)
            only_claude = tokens.query(cli="claude")
            self.assertEqual(only_claude["totals"]["input"], 50)
            all_ = tokens.query()
            self.assertEqual(all_["totals"]["input"], 150)
            self.assertIn("codex", all_["clis"])
            self.assertIn("claude", all_["clis"])

    def test_time_range_filter(self) -> None:
        with _EnvOverride() as paths:
            from backend.app import tokens

            day = paths["codex"] / "2026/07/08"
            rows = [
                _codex_session_meta("2026-07-08T10:00:00Z", "gpt-5.4"),
                _codex_token_row("2026-07-08T10:00:10Z", {"input": 100}),
                _codex_token_row("2026-07-08T20:00:00Z", {"input": 500}),
            ]
            _write_jsonl(day / "rollout-t.jsonl", rows)
            tokens.refresh()

            windowed = tokens.query(from_ts="2026-07-08T15:00:00Z")
            self.assertEqual(windowed["totals"]["input"], 400)
            capped = tokens.query(to_ts="2026-07-08T15:00:00Z")
            self.assertEqual(capped["totals"]["input"], 100)


class IncrementalScanTests(unittest.TestCase):
    def test_appended_lines_only_reparsed_on_rescan(self) -> None:
        with _EnvOverride() as paths:
            from backend.app import tokens

            day = paths["codex"] / "2026/07/08"
            rollout = day / "rollout-i.jsonl"
            _write_jsonl(
                rollout,
                [
                    _codex_session_meta("2026-07-08T18:00:00Z", "gpt-5.4"),
                    _codex_token_row("2026-07-08T18:00:10Z", {"input": 100}),
                ],
            )
            tokens.refresh()
            state = tokens._load_state()  # noqa: SLF001 - test peek
            first_offset = state["files"][str(rollout)]["offset"]
            self.assertGreater(first_offset, 0)

            _append_jsonl(
                rollout,
                [_codex_token_row("2026-07-08T18:30:00Z", {"input": 300})],
            )
            tokens.refresh()
            state = tokens._load_state()  # noqa: SLF001
            # Offset advanced but the file wasn't reparsed from the top —
            # totals reflect BOTH lines.
            self.assertGreater(state["files"][str(rollout)]["offset"], first_offset)
            summary = tokens.query()
            self.assertEqual(summary["totals"]["input"], 300)

    def test_shrunk_file_is_reparsed_from_start(self) -> None:
        with _EnvOverride() as paths:
            from backend.app import tokens

            rollout = paths["codex"] / "2026/07/08/rollout-s.jsonl"
            _write_jsonl(
                rollout,
                [
                    _codex_session_meta("2026-07-08T18:00:00Z", "gpt-5.4"),
                    _codex_token_row("2026-07-08T18:00:10Z", {"input": 100}),
                    _codex_token_row("2026-07-08T18:30:00Z", {"input": 500}),
                ],
            )
            tokens.refresh()

            # Rewrite the file smaller (simulating rotation) — the scanner
            # must NOT skip these lines.
            _write_jsonl(
                rollout,
                [
                    _codex_session_meta("2026-07-08T19:00:00Z", "gpt-5.4"),
                    _codex_token_row("2026-07-08T19:00:10Z", {"input": 42}),
                ],
            )
            tokens.refresh()
            # No hard invariant on the pre-shrink total (buckets aren't
            # reversible), but the fresh 42 MUST show up in the 19:00 hour.
            hourly = tokens.query(bucket="hour")
            hour_ts = "2026-07-08T19:00:00Z"
            hour_bucket = next((b for b in hourly["buckets"] if b["ts"] == hour_ts), None)
            self.assertIsNotNone(hour_bucket)
            self.assertEqual(hour_bucket["series"]["codex/gpt-5.4"]["input"], 42)


if __name__ == "__main__":
    unittest.main()
