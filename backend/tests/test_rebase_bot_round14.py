"""Round-14 verdict follow-up for WIKI-175 PR #137.

* F18 — pruning is now one atomic locked operation (reload, remove
        expired, persist) so an expired job actually disappears from
        ``state.json`` instead of coming back on the next locked
        reload.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend.app.agent_runtime import rebase_bot, rebase_durable


def _fresh_state() -> None:
    rebase_durable._DURABLE_STATE_LOADED = False
    rebase_durable._DURABLE_JOBS.clear()
    rebase_durable._OUTBOX.clear()
    rebase_durable._DELIVERED_EVENTS.clear()


class F18AtomicPrune(unittest.TestCase):
    def test_expired_job_is_removed_from_disk_by_prune(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fake_main = SimpleNamespace(AGENT_RUNTIME_DIR=raw)
            state_path = Path(raw) / "rebase-bot" / "state.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            # Two records: one long-completed (expired) and one fresh.
            stale = {
                "job_id": "old",
                "pr_number": 1,
                "expected_sha": "aaaa",
                "ticket": "WIKI-175",
                "worker_id": "W-old",
                "worktree": str(raw),
                "orchestrator": "wiki",
                "prompt": "p",
                "verdict": {},
                "status": "completed",
                "result": {
                    "status": "escalated",
                    "head_sha": "aaaa",
                    "resolved_files": [],
                    "escalated_hunks": [],
                },
                "updated_at": time.time() - 3600,
                "completed_at": time.time() - 3600,
            }
            fresh = {
                **stale,
                "job_id": "new",
                "pr_number": 2,
                "expected_sha": "bbbb",
                "worker_id": "W-new",
                "updated_at": time.time(),
                "completed_at": time.time(),
            }
            state_path.write_text(
                json.dumps(
                    {
                        "jobs": {"1:aaaa": stale, "2:bbbb": fresh},
                        "outbox": {},
                        "delivered": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with mock.patch.object(rebase_bot, "_main", return_value=fake_main):
                _fresh_state()
                rebase_bot._prune_durable_jobs()
                # After prune, disk snapshot must have the expired key
                # gone AND the fresh key preserved — the prune write is
                # what makes the retention rule real.
                snapshot = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertNotIn("1:aaaa", snapshot["jobs"])
        self.assertIn("2:bbbb", snapshot["jobs"])

    def test_pruned_job_stays_pruned_across_another_process_reload(self) -> None:
        # Reviewer's constraint: an expired job removed by one prune must
        # not come back when another "process" reloads.  Simulate the
        # second process by clearing this one's cached state and reading
        # state.json fresh.
        with tempfile.TemporaryDirectory() as raw:
            fake_main = SimpleNamespace(AGENT_RUNTIME_DIR=raw)
            state_path = Path(raw) / "rebase-bot" / "state.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            expired = {
                "job_id": "expired",
                "pr_number": 42,
                "expected_sha": "eeee",
                "ticket": "WIKI-175",
                "worker_id": "W-42",
                "worktree": str(raw),
                "orchestrator": "wiki",
                "prompt": "p",
                "verdict": {},
                "status": "completed",
                "result": {
                    "status": "escalated",
                    "head_sha": "eeee",
                    "resolved_files": [],
                    "escalated_hunks": [],
                },
                "updated_at": time.time() - 7200,
                "completed_at": time.time() - 7200,
            }
            state_path.write_text(
                json.dumps(
                    {"jobs": {"42:eeee": expired}, "outbox": {}, "delivered": []},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with mock.patch.object(rebase_bot, "_main", return_value=fake_main):
                _fresh_state()
                rebase_bot._prune_durable_jobs()
                # Now simulate another process: drop every cached bit
                # and force a fresh read from disk under the state lock.
                _fresh_state()
                rebase_durable._load_durable_state()
        self.assertNotIn(
            "42:eeee",
            rebase_durable._DURABLE_JOBS,
            "expired job resurrected on the next locked reload",
        )

    def test_prune_uses_the_state_lock(self) -> None:
        # The prune write must go through _state_lock — same replace
        # protocol as _persist_completion — otherwise a concurrent
        # writer's snapshot can clobber the pruned state.
        with tempfile.TemporaryDirectory() as raw:
            fake_main = SimpleNamespace(AGENT_RUNTIME_DIR=raw)
            with mock.patch.object(rebase_bot, "_main", return_value=fake_main):
                _fresh_state()
                lock_calls = [0]
                original_lock = rebase_durable._state_lock

                @contextmanager
                def counting_lock():
                    lock_calls[0] += 1
                    with original_lock():
                        yield

                with mock.patch.object(
                    rebase_durable, "_state_lock", counting_lock
                ):
                    rebase_bot._prune_durable_jobs()
        self.assertGreaterEqual(lock_calls[0], 1)


if __name__ == "__main__":
    unittest.main()
