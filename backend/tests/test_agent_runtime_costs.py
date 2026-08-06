from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from backend.app.agent_runtime import costs


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "agent_runtime"


def _envelope(received_at: str, payload: dict, provider: str = "codex") -> dict:
    return {"seq": 1, "received_at": received_at, "provider": provider, "direction": "server", "payload": payload}


def _usage(input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> dict:
    return {
        "method": "thread/tokenUsage/updated",
        "params": {
            "tokenUsage": {
                "total": {
                    "inputTokens": input_tokens,
                    "cacheReadInputTokens": cache_read,
                    "cacheWriteInputTokens": cache_write,
                    "outputTokens": output_tokens,
                    "reasoningOutputTokens": 0,
                }
            }
        },
    }


class CostAggregatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.runs = root / "runs"
        self.state = root / "cost.json"
        self.runs.mkdir()
        self.env = mock.patch.dict(
            os.environ,
            {
                "WIKI_AGENT_RUNS_DIR": str(self.runs),
                "WIKI_COST_STATE_PATH": str(self.state),
                "WIKI_COST_REFRESH_MAX_AGE_SECONDS": "0",
            },
        )
        self.env.start()

    def tearDown(self) -> None:
        costs.invalidate_background_state()
        self.env.stop()
        self.tmp.cleanup()

    def _run(self, run_id: str, *, model: str = "gpt-5.4", agent_id: str = "WIKI-178", orch: str = "wiki", provider: str = "codex") -> Path:
        run = self.runs / run_id
        run.mkdir()
        (run / "run.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "agent_id": agent_id,
                    "provider": provider,
                    "model": model,
                    "orchestrator_id": orch,
                    "initial_prompt": "x" * 8000,
                    "created_at": "2026-07-30T12:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        return run / "raw.jsonl"

    def test_incremental_cumulative_usage_buckets_by_event_day(self) -> None:
        raw = self._run("run-1")
        raw.write_text(
            "\n".join(
                json.dumps(_envelope(ts, _usage(inp, out, cache_read, cache_write)))
                for ts, inp, out, cache_read, cache_write in (
                    ("2026-07-29T23:59:59Z", 1000, 100, 200, 50),
                    ("2026-07-30T00:00:01Z", 1500, 150, 300, 75),
                )
            )
            + "\n",
            encoding="utf-8",
        )
        first = costs.refresh()
        self.assertEqual(len(first["records"]), 2)
        totals = costs.query()
        self.assertEqual(totals["totals"]["input"], 1125)
        self.assertEqual(totals["totals"]["output"], 150)
        self.assertEqual(totals["totals"]["cache_read"], 300)
        self.assertEqual(totals["totals"]["cache_write"], 75)
        self.assertEqual(totals["totals"]["pricing"], "priced")
        self.assertAlmostEqual(totals["totals"]["cost_usd"], 0.005371875, places=8)
        self.assertEqual(totals["velocity"]["window_seconds"], 60)

        with raw.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_envelope("2026-07-30T00:01:01Z", _usage(1600, 175, 350, 100))) + "\n")
        second = costs.refresh(costs._load_state())
        self.assertEqual(sum(record["input"] for record in second["records"].values()), 1150)
        self.assertEqual(sum(record["output"] for record in second["records"].values()), 175)
        self.assertEqual(second["runs"]["run-1"]["offset"], raw.stat().st_size)

    def test_unknown_model_is_unpriced_and_keeps_tokens(self) -> None:
        raw = self._run("run-unknown", model="future-model")
        raw.write_text(json.dumps(_envelope("2026-07-30T10:00:00Z", _usage(400, 50))) + "\n", encoding="utf-8")
        result = costs.refresh()
        row = costs._query_state(result)["totals"]
        self.assertEqual(row["pricing"], "unpriced")
        self.assertIsNone(row["cost_usd"])
        self.assertEqual(row["unpriced_tokens"], 450)

    def test_redacted_provider_fixtures_use_real_event_shapes(self) -> None:
        codex_raw = self._run("fixture-codex", model="gpt-5.6-sol", provider="codex")
        shutil.copyfile(FIXTURE_ROOT / "costs-codex.jsonl", codex_raw)
        claude_raw = self._run("fixture-claude", model="claude-opus-4-7", provider="claude")
        shutil.copyfile(FIXTURE_ROOT / "costs-claude.jsonl", claude_raw)

        result = costs.refresh()
        codex_rows = [record for record in result["records"].values() if record["model"] == "gpt-5.6-sol"]
        claude_rows = [record for record in result["records"].values() if record["model"] == "claude-opus-4-7"]
        self.assertEqual(sum(record["input"] for record in codex_rows), 1550)
        self.assertEqual(sum(record["cache_write_5m"] for record in codex_rows), 50)
        self.assertEqual(sum(record["cache_read"] for record in codex_rows), 200)
        self.assertEqual(sum(record["output"] for record in codex_rows), 100)
        self.assertEqual(claude_rows[0]["cache_write_5m"], 10)
        self.assertEqual(claude_rows[0]["cache_write_1h"], 30)

    def test_large_fixture_keeps_reads_bounded_and_advances_cursor(self) -> None:
        raw = self._run("run-large")
        event = json.dumps(_envelope("2026-07-30T10:00:00Z", _usage(1, 1)))
        raw.write_text((event + "\n") * 2_000, encoding="utf-8")

        started = time.perf_counter()
        result = costs.refresh()
        elapsed = time.perf_counter() - started
        run_state = result["runs"]["run-large"]
        self.assertEqual(run_state["offset"], raw.stat().st_size)
        self.assertGreater(run_state["read_chunks"], 1)
        self.assertLess(elapsed, 2.0)
        self.assertEqual(sum(record["output"] for record in result["records"].values()), 1)

    def test_claude_cache_read_and_creation_are_priced(self) -> None:
        raw = self._run("run-claude", model="opus-4.7", provider="claude")
        payload = {
            "type": "assistant",
            "message": {
                "id": "message-1",
                "model": "claude-opus-4-7",
                "usage": {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 20,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 10,
                        "ephemeral_1h_input_tokens": 20,
                    },
                    "output_tokens": 10,
                },
            },
        }
        raw.write_text(json.dumps(_envelope("2026-07-30T10:00:00Z", payload, "claude")) + "\n", encoding="utf-8")
        result = costs._query_state(costs.refresh())
        row = result["totals"]
        self.assertEqual((row["input"], row["cache_read"], row["cache_write"], row["output"]), (100, 20, 30, 10))
        self.assertAlmostEqual(row["cost_usd"], 0.0010225, places=8)

        state = {"seen_message_ids": {f"old-{index}": True for index in range(costs.MAX_MESSAGE_DEDUPE_IDS)}}
        costs._claude_usage(payload, state)
        self.assertLessEqual(len(state["seen_message_ids"]), costs.MAX_MESSAGE_DEDUPE_IDS)

    def test_all_catalog_model_rates_are_distinct_and_explicit(self) -> None:
        self.assertEqual(costs.PRICING_USD_PER_MILLION["gpt-5.5"]["input"], 5.0)
        self.assertEqual(costs.PRICING_USD_PER_MILLION["gpt-5.5"]["output"], 30.0)
        self.assertEqual(costs.PRICING_USD_PER_MILLION["claude-haiku-4-5"]["input"], 1.0)
        self.assertEqual(costs.PRICING_USD_PER_MILLION["claude-haiku-4-5"]["output"], 5.0)
        self.assertEqual(costs.PRICING_USD_PER_MILLION["claude-opus-4-7"]["cache_write_1h"], 10.0)
        self.assertEqual(costs.PRICING_USD_PER_MILLION["claude-fable-5"]["output"], 50.0)
        self.assertNotIn("gpt-5.3-codex-spark", costs.PRICING_USD_PER_MILLION)

    def test_reset_replaces_per_run_contributions_and_prunes_deleted_runs(self) -> None:
        raw = self._run("run-reset")
        raw.write_text(json.dumps(_envelope("2026-07-30T10:00:00Z", _usage(220, 22))) + "\n", encoding="utf-8")
        costs.refresh()
        raw.write_text(json.dumps(_envelope("2026-07-30T10:00:00Z", _usage(330, 33))) + "\n", encoding="utf-8")
        refreshed = costs.refresh()
        self.assertEqual(refreshed["records"][next(iter(refreshed["records"]))]["input"], 330)
        with raw.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_envelope("2026-07-30T10:01:00Z", _usage(440, 44))) + "\n")
        appended = costs.refresh()
        self.assertEqual(sum(record["input"] for record in appended["records"].values()), 440)
        raw.unlink()
        shutil.rmtree(raw.parent)
        pruned = costs.refresh()
        self.assertEqual(pruned["runs"], {})
        self.assertEqual(pruned["records"], {})

    def test_missing_raw_file_removes_run_contributions(self) -> None:
        raw = self._run("run-missing-raw")
        raw.write_text(json.dumps(_envelope("2026-07-30T10:00:00Z", _usage(220, 22))) + "\n", encoding="utf-8")
        costs.refresh()
        raw.unlink()

        refreshed = costs.refresh()
        self.assertEqual(refreshed["runs"], {})
        self.assertEqual(refreshed["records"], {})

    def test_ticket_rollup_uses_base_ticket_and_includes_role_siblings(self) -> None:
        primary = self._run("run-primary", agent_id="WIKI-178")
        review = self._run("run-review", agent_id="WIKI-178-REVIEW1")
        primary.write_text(json.dumps(_envelope("2026-07-30T10:00:00Z", _usage(100, 10))) + "\n", encoding="utf-8")
        review.write_text(json.dumps(_envelope("2026-07-30T10:01:00Z", _usage(200, 20))) + "\n", encoding="utf-8")
        result = costs._query_state(costs.refresh(), ticket="WIKI-178")
        self.assertEqual(result["totals"]["input"], 300)
        self.assertEqual([row["label"] for row in result["top"]["ticket"]], ["WIKI-178"])
        self.assertEqual({row["label"] for row in result["top"]["worker"]}, {"WIKI-178", "WIKI-178-REVIEW1"})
        self.assertEqual(sum(item["runs"] for item in result["prompt_size_distribution"]), 2)

    def test_symlinked_runs_and_raw_files_are_not_followed(self) -> None:
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        external_run = outside / "outside-run"
        external_run.mkdir()
        (external_run / "run.json").write_text(
            json.dumps({"agent_id": "OUTSIDE-1", "provider": "codex", "model": "gpt-5.4"}),
            encoding="utf-8",
        )
        external_raw = external_run / "raw.jsonl"
        external_raw.write_text(json.dumps(_envelope("2026-07-30T10:00:00Z", _usage(999, 99))) + "\n", encoding="utf-8")
        visible_link = self.runs / "linked-run"
        visible_link.symlink_to(external_run, target_is_directory=True)
        local_raw = self._run("linked-raw")
        local_raw.symlink_to(external_raw)
        result = costs.refresh()
        self.assertEqual(result["records"], {})

    def test_non_regular_metadata_does_not_block_refresh(self) -> None:
        raw = self._run("run-fifo")
        raw.write_text(json.dumps(_envelope("2026-07-30T10:00:00Z", _usage(999, 99))) + "\n", encoding="utf-8")
        metadata = raw.parent / "run.json"
        metadata.unlink()
        os.mkfifo(metadata)

        result = costs.refresh()
        self.assertEqual(result["records"], {})
        self.assertEqual(result["runs"], {})

    def test_partial_line_is_retried_without_rescanning_complete_lines(self) -> None:
        raw = self._run("run-partial")
        complete = json.dumps(_envelope("2026-07-30T10:00:00Z", _usage(100, 10))) + "\n"
        raw.write_text(complete + '{"seq":2,"received_at":"2026-07-30T10:01:00Z"', encoding="utf-8")
        first = costs.refresh()
        self.assertEqual(first["runs"]["run-partial"]["offset"], len(complete.encode()))
        self.assertEqual(next(iter(first["records"].values()))["input"], 100)
        with raw.open("a", encoding="utf-8") as handle:
            handle.write(',"provider":"codex","direction":"server","payload":' + json.dumps(_usage(200, 20)) + "}\n")
        second = costs.refresh(costs._load_state())
        record = next(iter(second["records"].values()))
        self.assertEqual(record["input"], 200)
        self.assertEqual(record["output"], 20)

    def test_refresh_does_not_leak_file_descriptors(self) -> None:
        raw = self._run("run-fd")
        raw.write_text(json.dumps(_envelope("2026-07-30T10:00:00Z", _usage(10, 2))) + "\n", encoding="utf-8")
        costs.refresh()
        baseline = len(os.listdir("/dev/fd"))
        for _ in range(5):
            costs.refresh(costs._load_state())
        self.assertEqual(len(os.listdir("/dev/fd")), baseline)

    def test_unchanged_runs_skip_full_directory_sweep(self) -> None:
        raw = self._run("run-cache")
        raw.write_text(json.dumps(_envelope("2026-07-30T10:00:00Z", _usage(10, 2))) + "\n", encoding="utf-8")
        costs.refresh()

        with (
            mock.patch.object(costs.os, "scandir", wraps=costs.os.scandir) as scandir,
            mock.patch.object(costs, "_scan_run", wraps=costs._scan_run) as scan_run,
            mock.patch.object(costs, "_save_state", wraps=costs._save_state) as save_state,
            mock.patch.object(costs, "_save_heartbeat", wraps=costs._save_heartbeat) as save_heartbeat,
        ):
            refreshed = costs.refresh(costs._load_state())

        self.assertEqual(scan_run.call_count, 0)
        self.assertEqual(scandir.call_count, 0)
        self.assertEqual(save_state.call_count, 0)
        self.assertEqual(save_heartbeat.call_count, 1)
        self.assertEqual(refreshed["runs"]["run-cache"]["offset"], raw.stat().st_size)

    def test_background_refresh_loads_state_once_across_ticks(self) -> None:
        costs.invalidate_background_state()
        with mock.patch.object(costs, "_load_state", wraps=costs._load_state) as load_state:
            self.assertTrue(asyncio.run(costs.refresh_in_background()))
            self.assertTrue(asyncio.run(costs.refresh_in_background()))

        self.assertEqual(load_state.call_count, 1)

    def test_failed_state_write_does_not_advance_heartbeat(self) -> None:
        raw = self._run("run-write-failure")
        raw.write_text(json.dumps(_envelope("2026-07-30T10:00:00Z", _usage(10, 2))) + "\n", encoding="utf-8")
        costs.refresh()
        heartbeat_before = costs.cost_heartbeat_path().read_text(encoding="utf-8")

        with mock.patch.object(costs, "_save_state", return_value=False) as save_state, mock.patch.object(
            costs, "_save_heartbeat", wraps=costs._save_heartbeat
        ) as save_heartbeat:
            raw.write_text(
                json.dumps(_envelope("2026-07-30T10:01:00Z", _usage(20, 4))) + "\n",
                encoding="utf-8",
            )
            costs.refresh(costs._load_state())

        self.assertTrue(save_state.called)
        self.assertFalse(save_heartbeat.called)
        self.assertEqual(costs.cost_heartbeat_path().read_text(encoding="utf-8"), heartbeat_before)

    def test_atomic_run_publication_appears_on_the_next_refresh(self) -> None:
        costs.refresh()

        staging = self.runs / ".run-pending"
        staging.mkdir()
        (staging / "run.json").write_text(
            json.dumps(
                {
                    "run_id": "run-pending",
                    "agent_id": "WIKI-178",
                    "provider": "codex",
                    "model": "gpt-5.4",
                    "created_at": "2026-07-30T12:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        raw = staging / "raw.jsonl"
        raw.write_text(json.dumps(_envelope("2026-07-30T10:00:00Z", _usage(10, 2))) + "\n", encoding="utf-8")
        costs.refresh(costs._load_state())
        self.assertNotIn("run-pending", costs._load_state()["runs"])

        os.replace(staging, self.runs / "run-pending")
        raw = self.runs / "run-pending" / "raw.jsonl"
        refreshed = costs.refresh(costs._load_state())

        self.assertEqual(refreshed["runs"]["run-pending"]["offset"], raw.stat().st_size)
        self.assertEqual(sum(record["input"] for record in refreshed["records"].values()), 10)
        self.assertNotIn("pending_runs", refreshed)

    def test_state_is_atomic_and_does_not_touch_live_paths(self) -> None:
        raw = self._run("run-atomic")
        raw.write_text(json.dumps(_envelope("2026-07-30T10:00:00Z", _usage(10, 2))) + "\n", encoding="utf-8")
        costs.refresh()
        self.assertTrue(self.state.is_file())
        self.assertFalse((self.runs / "run-atomic" / "cost-aggregation.json").exists())
        loaded = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(loaded["version"], costs.STATE_VERSION)


if __name__ == "__main__":
    unittest.main()
