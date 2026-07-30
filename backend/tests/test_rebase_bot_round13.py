"""Round-13 verdict follow-ups for WIKI-175 PR #137.

Each test is written before its fix; the fixes address:

* F16 — reload replaces the in-memory collections wholesale so a key
        deleted on disk cannot resurrect through a stale writer.
* F17 — _state_lock() fails closed if flock cannot be acquired, matching
        _worktree_lock() behaviour.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend.app.agent_runtime import (
    rebase_bot,
    rebase_durable,
)
from backend.app.agent_runtime.rebase_parsing import RebaseError


def _fresh_state() -> None:
    rebase_durable._DURABLE_STATE_LOADED = False
    rebase_durable._DURABLE_JOBS.clear()
    rebase_durable._OUTBOX.clear()
    rebase_durable._DELIVERED_EVENTS.clear()


def _make_job(
    worktree: Path, *, job_id: str, pr: int, sha: str
) -> rebase_durable._RebaseJob:
    return rebase_durable._RebaseJob(
        job_id=job_id,
        worktree=worktree,
        prompt="p",
        done=threading.Event(),
        pr_number=pr,
        expected_sha=sha,
        ticket="WIKI-175",
        worker_id=f"WIKI-175-{job_id}",
        orchestrator="wiki",
        durable=True,
    )


def _result(sha: str) -> dict[str, object]:
    return {
        "status": "escalated",
        "head_sha": sha,
        "resolved_files": [],
        "escalated_hunks": [f"semantic-{sha}"],
    }


class F16ReplaceNotMerge(unittest.TestCase):
    def test_reload_wipes_entries_removed_by_another_process(self) -> None:
        # Reviewer's probe: process A publishes an outbox entry E; process
        # B (simulated by editing state.json directly) delivers and drops
        # E from the outbox, adding its delivery id to the ``delivered``
        # set.  Process A still holds E in its stale in-memory _OUTBOX.
        # When A then persists an UNRELATED job, the reload under the
        # lock must REPLACE its in-memory copy with the disk snapshot —
        # not merge — otherwise E resurrects and a stale sender re-fires
        # it after the max-attempts bound.
        with tempfile.TemporaryDirectory() as raw:
            fake_main = SimpleNamespace(AGENT_RUNTIME_DIR=raw)
            with mock.patch.object(rebase_bot, "_main", return_value=fake_main):
                _fresh_state()
                worktree = Path(raw)

                # Process A: publish job X — creates its outbox entry.
                job_x = _make_job(worktree, job_id="X", pr=101, sha="xxxx")
                rebase_durable._persist_completion(job_x, _result("xxxx"))
                self.assertIn("X:result", rebase_durable._OUTBOX)

                # Process B (via disk edit): drop the outbox entry and
                # mark the id as delivered.  Process A does NOT see this.
                state_path = Path(raw) / "rebase-bot" / "state.json"
                snapshot = json.loads(state_path.read_text(encoding="utf-8"))
                dropped_id = snapshot["outbox"].pop("X:result")["delivery_id"]
                snapshot["delivered"] = sorted(
                    set(snapshot.get("delivered", []) + [dropped_id])
                )
                state_path.write_text(
                    json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
                )
                # A's in-memory _OUTBOX still holds the stale entry.
                self.assertIn("X:result", rebase_durable._OUTBOX)

                # Process A persists an unrelated job Y.  Under the fix
                # the reload REPLACES in-memory state with disk state, so
                # the stale outbox entry gets wiped; then A adds Y's own
                # outbox entry and only Y ends up in the outbox.
                job_y = _make_job(worktree, job_id="Y", pr=202, sha="yyyy")
                rebase_durable._persist_completion(job_y, _result("yyyy"))

                # Re-read from disk and verify.
                _fresh_state()
                rebase_durable._load_durable_state()
        self.assertNotIn(
            "X:result",
            rebase_durable._OUTBOX,
            "dropped outbox entry resurrected via merge-not-replace",
        )
        self.assertIn("Y:result", rebase_durable._OUTBOX)
        self.assertIn(dropped_id, rebase_durable._DELIVERED_EVENTS)

    def test_bound_holds_after_stale_process_persists(self) -> None:
        # Full end-to-end reproduction of the reviewer's "sixth send"
        # scenario.  Bound the sender to _OUTBOX_MAX_ATTEMPTS and confirm
        # no extra call slips through after a stale writer.
        with tempfile.TemporaryDirectory() as raw:
            fake_main = SimpleNamespace(AGENT_RUNTIME_DIR=raw)
            with mock.patch.object(rebase_bot, "_main", return_value=fake_main):
                _fresh_state()
                worktree = Path(raw)

                sends: list[str] = []

                def failing(_t: str, _m: str, delivery_id: str) -> None:
                    sends.append(delivery_id)
                    raise RuntimeError("orchestrator flap")

                clock = [time.time()]

                def advancing_clock() -> float:
                    clock[0] += 3600.0
                    return clock[0]

                job_e = _make_job(worktree, job_id="E", pr=137, sha="eeee")
                rebase_durable._persist_completion(job_e, _result("eeee"))

                # Exhaust the retry bound.
                for _ in range(rebase_durable._OUTBOX_MAX_ATTEMPTS + 3):
                    rebase_bot._flush_outbox(failing, _now=advancing_clock)
                bound_hits = len(sends)
                self.assertEqual(
                    bound_hits,
                    rebase_durable._OUTBOX_MAX_ATTEMPTS,
                )
                self.assertNotIn("E:result", rebase_durable._OUTBOX)

                # Simulate a stale writer that never observed the drop
                # persisting an unrelated job.  In-memory _OUTBOX is
                # empty here so the stale-restoration bug shows up
                # instead by keeping a stale JOB copy — inject the stale
                # outbox entry back into the in-process dict to mimic
                # the reviewer's cross-process stale-holder.
                rebase_durable._OUTBOX["E:result"] = {
                    "id": "E:result",
                    "delivery_id": "137:eeee:escalated:eeee",
                    "target": "wiki",
                    "worker_id": "WIKI-175-E",
                    "result": _result("eeee"),
                    "attempts": 0,
                    "last_error": None,
                    "next_attempt_at": 0.0,
                }
                job_f = _make_job(worktree, job_id="F", pr=222, sha="ffff")
                rebase_durable._persist_completion(job_f, _result("ffff"))

                # After the stale writer's persist, another flush must
                # NOT re-send the exhausted-bound entry — E's delivery
                # id should never appear again, though F's own attempts
                # are expected until F also hits the bound.
                sends.clear()
                for _ in range(3):
                    rebase_bot._flush_outbox(failing, _now=advancing_clock)
        exhausted_id = "137:eeee:escalated:eeee"
        self.assertNotIn(
            exhausted_id,
            sends,
            f"exhausted-bound entry resurrected: {sends}",
        )


class F17StateLockFailsClosed(unittest.TestCase):
    def test_state_lock_raises_when_flock_fails(self) -> None:
        # When fcntl.flock refuses (EWOULDBLOCK, IO error, etc.), the
        # lock context must fail closed so we never publish state
        # without cross-process protection — the round-11 quiet-degrade
        # path is what let the reviewer reproduce a lost update in the
        # first place.
        with tempfile.TemporaryDirectory() as raw:
            fake_main = SimpleNamespace(AGENT_RUNTIME_DIR=raw)
            with mock.patch.object(rebase_bot, "_main", return_value=fake_main):
                _fresh_state()
                # Prime the state directory so the lock path exists.
                rebase_durable._persist_completion(
                    _make_job(Path(raw), job_id="pre", pr=1, sha="pre1"),
                    _result("pre1"),
                )
                import fcntl as _fcntl

                original_flock = _fcntl.flock

                def refuse(*_args, **_kwargs):
                    raise OSError("simulated flock refusal")

                with (
                    mock.patch.object(_fcntl, "flock", side_effect=refuse),
                    self.assertRaises(RebaseError),
                ):
                    rebase_durable._persist_completion(
                        _make_job(Path(raw), job_id="denied", pr=2, sha="deny"),
                        _result("deny"),
                    )
                # Sanity: original flock reference intact after the patch
                # is released.
                self.assertIs(_fcntl.flock, original_flock)


if __name__ == "__main__":
    unittest.main()
