from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.app.agent_runtime import costs


def _envelope(received_at: str, payload: dict) -> dict:
    return {"seq": 1, "received_at": received_at, "provider": "codex", "direction": "server", "payload": payload}


def _usage(input_tokens: int, output_tokens: int, cached: int = 0) -> dict:
    return {
        "method": "thread/tokenUsage/updated",
        "params": {
            "tokenUsage": {
                "total": {
                    "inputTokens": input_tokens,
                    "cachedInputTokens": cached,
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
        self.env.stop()
        self.tmp.cleanup()

    def _run(self, run_id: str, *, model: str = "gpt-5.4", agent_id: str = "WIKI-178", orch: str = "wiki") -> Path:
        run = self.runs / run_id
        run.mkdir()
        (run / "run.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "agent_id": agent_id,
                    "provider": "codex",
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
                json.dumps(_envelope(ts, _usage(inp, out, cached)))
                for ts, inp, out, cached in (
                    ("2026-07-29T23:59:59Z", 1000, 100, 200),
                    ("2026-07-30T00:00:01Z", 1500, 150, 300),
                )
            )
            + "\n",
            encoding="utf-8",
        )
        first = costs.refresh()
        self.assertEqual(len(first["records"]), 2)
        totals = costs.query()
        self.assertEqual(totals["totals"]["input"], 1500)
        self.assertEqual(totals["totals"]["output"], 150)
        self.assertEqual(totals["totals"]["cached"], 300)
        self.assertEqual(totals["totals"]["pricing"], "priced")
        self.assertAlmostEqual(totals["totals"]["cost_usd"], 0.00525, places=8)
        self.assertEqual(totals["velocity"]["window_seconds"], 60)

        with raw.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_envelope("2026-07-30T00:01:01Z", _usage(1600, 175, 350))) + "\n")
        second = costs.refresh(costs._load_state())
        self.assertEqual(sum(record["input"] for record in second["records"]), 1600)
        self.assertEqual(sum(record["output"] for record in second["records"]), 175)
        self.assertEqual(second["runs"]["run-1"]["offset"], raw.stat().st_size)

    def test_unknown_model_is_unpriced_and_keeps_tokens(self) -> None:
        raw = self._run("run-unknown", model="future-model")
        raw.write_text(json.dumps(_envelope("2026-07-30T10:00:00Z", _usage(400, 50))) + "\n", encoding="utf-8")
        result = costs.refresh()
        row = costs._query_state(result)["totals"]
        self.assertEqual(row["pricing"], "unpriced")
        self.assertIsNone(row["cost_usd"])
        self.assertEqual(row["unpriced_tokens"], 450)

    def test_partial_line_is_retried_without_rescanning_complete_lines(self) -> None:
        raw = self._run("run-partial")
        complete = json.dumps(_envelope("2026-07-30T10:00:00Z", _usage(100, 10))) + "\n"
        raw.write_text(complete + '{"seq":2,"received_at":"2026-07-30T10:01:00Z"', encoding="utf-8")
        first = costs.refresh()
        self.assertEqual(first["runs"]["run-partial"]["offset"], len(complete.encode()))
        self.assertEqual(first["records"][0]["input"], 100)
        with raw.open("a", encoding="utf-8") as handle:
            handle.write(',"provider":"codex","direction":"server","payload":' + json.dumps(_usage(200, 20)) + "}\n")
        second = costs.refresh(costs._load_state())
        self.assertEqual(second["records"][0]["input"], 200)
        self.assertEqual(second["records"][0]["output"], 20)

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
