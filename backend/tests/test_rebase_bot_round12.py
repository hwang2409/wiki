"""Round-12 verdict follow-ups for WIKI-175 PR #137.

Each test is written before its fix; the fixes address:

* F14 — cross-process state lock + unique temp filenames so concurrent
        completions cannot overwrite each other's snapshot.
* F15 — plumb the delivery id through ``NotificationSender`` into
        ``MessageIn.dedupe_key`` and retry (bounded) with the same key
        so a duplicate delivery is dropped at the receiver.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend.app import main
from backend.app.agent_runtime import rebase_bot, rebase_durable


def _fresh_state() -> None:
    rebase_durable._DURABLE_STATE_LOADED = False
    rebase_durable._DURABLE_JOBS.clear()
    rebase_durable._OUTBOX.clear()
    rebase_durable._DELIVERED_EVENTS.clear()


class F14ConcurrentCompletions(unittest.TestCase):
    def _make_job(self, worktree: Path, *, job_id: str, pr: int, sha: str) -> rebase_durable._RebaseJob:
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

    def _result(self, sha: str) -> dict[str, object]:
        return {
            "status": "escalated",
            "head_sha": sha,
            "resolved_files": [],
            "escalated_hunks": [f"semantic-{sha}"],
        }

    def test_persist_completion_reloads_from_disk_before_writing(self) -> None:
        # Reviewer's interleaving: two workers each read the same state,
        # A pauses after serializing, B publishes, A resumes and writes
        # stale data — B's job vanishes.  We simulate the "another process
        # wrote in the meantime" step deterministically by editing
        # ``state.json`` in place between two ``_persist_completion``
        # calls from the same process.  The fix takes a cross-process
        # ``flock`` and re-reads ``state.json`` under it, so the second
        # completion must preserve the injected sibling job.
        with tempfile.TemporaryDirectory() as raw:
            fake_main = SimpleNamespace(AGENT_RUNTIME_DIR=raw)
            with mock.patch.object(rebase_bot, "_main", return_value=fake_main):
                _fresh_state()
                worktree = Path(raw)
                # Job A lands normally.
                rebase_durable._persist_completion(
                    self._make_job(worktree, job_id="A", pr=101, sha="aaaa"),
                    self._result("aaaa"),
                )
                # Simulate another process writing job B directly to
                # state.json — the in-memory dicts in THIS process do NOT
                # see B until we reload under the lock.
                state_path = Path(raw) / "rebase-bot" / "state.json"
                snapshot = json.loads(state_path.read_text(encoding="utf-8"))
                snapshot.setdefault("jobs", {})["202:bbbb"] = {
                    "job_id": "B",
                    "pr_number": 202,
                    "expected_sha": "bbbb",
                    "ticket": "WIKI-175",
                    "worker_id": "WIKI-175-B",
                    "worktree": str(worktree),
                    "orchestrator": "wiki",
                    "prompt": "p",
                    "verdict": {},
                    "status": "completed",
                    "result": self._result("bbbb"),
                    "updated_at": time.time(),
                    "completed_at": time.time(),
                }
                snapshot.setdefault("outbox", {})["B:result"] = {
                    "id": "B:result",
                    "delivery_id": "202:bbbb:escalated:bbbb",
                    "target": "wiki",
                    "worker_id": "WIKI-175-B",
                    "result": self._result("bbbb"),
                    "attempts": 0,
                    "last_error": None,
                    "next_attempt_at": 0.0,
                }
                state_path.write_text(
                    json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
                )
                # Job C completes next — under the fix it reloads under
                # the lock and preserves job B; without the fix it writes
                # stale in-memory state (only jobs A and C) and loses B.
                rebase_durable._persist_completion(
                    self._make_job(worktree, job_id="C", pr=303, sha="cccc"),
                    self._result("cccc"),
                )
                _fresh_state()
                rebase_durable._load_durable_state()
        self.assertIn(
            rebase_durable._durable_key(101, "aaaa"),
            rebase_durable._DURABLE_JOBS,
        )
        self.assertIn(
            "202:bbbb",
            rebase_durable._DURABLE_JOBS,
            "external-process job B was clobbered by a stale in-memory write",
        )
        self.assertIn(
            rebase_durable._durable_key(303, "cccc"),
            rebase_durable._DURABLE_JOBS,
        )
        self.assertIn("B:result", rebase_durable._OUTBOX)

    def test_temp_filenames_are_unique_per_write(self) -> None:
        # Two writers cannot share the same state.json.tmp path — a shared
        # temp name is what let the pause-resume interleaving corrupt data
        # in the first place.
        seen: list[str] = []
        original_write_text = Path.write_text

        def capture_write_text(self, *args, **kwargs):
            if self.suffix == ".tmp":
                seen.append(self.name)
            return original_write_text(self, *args, **kwargs)

        with tempfile.TemporaryDirectory() as raw:
            fake_main = SimpleNamespace(AGENT_RUNTIME_DIR=raw)
            with mock.patch.object(rebase_bot, "_main", return_value=fake_main):
                _fresh_state()
                with mock.patch.object(Path, "write_text", capture_write_text):
                    for i in range(3):
                        rebase_durable._persist_completion(
                            self._make_job(Path(raw), job_id=f"J{i}", pr=i, sha=f"s{i}"),
                            self._result(f"s{i}"),
                        )
        # Every temp file has a distinct name across writes.
        self.assertEqual(
            len(seen), len(set(seen)),
            f"temp filenames repeated across writes: {seen}",
        )


class F15DeliveryIdPlumbThrough(unittest.TestCase):
    def test_flush_outbox_passes_delivery_id_to_notifier(self) -> None:
        captured: list[tuple[str, str, str]] = []

        def capturing_send(target: str, message: str, delivery_id: str) -> None:
            captured.append((target, message, delivery_id))

        with tempfile.TemporaryDirectory() as raw:
            fake_main = SimpleNamespace(AGENT_RUNTIME_DIR=raw)
            with mock.patch.object(rebase_bot, "_main", return_value=fake_main):
                _fresh_state()
                job = rebase_durable._RebaseJob(
                    job_id="deliver-through",
                    worktree=Path(raw),
                    prompt="p",
                    done=threading.Event(),
                    pr_number=137,
                    expected_sha="sha-key",
                    ticket="WIKI-175",
                    worker_id="WIKI-175-IMPL",
                    orchestrator="wiki",
                    durable=True,
                )
                result = {
                    "status": "escalated",
                    "head_sha": "head-final",
                    "resolved_files": [],
                    "escalated_hunks": ["semantic"],
                }
                rebase_durable._persist_completion(job, result)
                rebase_bot._flush_outbox(capturing_send)
        self.assertEqual(len(captured), 1)
        _target, _msg, delivery_id = captured[0]
        self.assertEqual(delivery_id, "137:sha-key:escalated:head-final")

    def test_rebase_bot_notifier_sets_message_dedupe_key(self) -> None:
        # The MessageIn passed to agent_message must carry the delivery id
        # so the receiver can dedupe duplicates from bounded retries.
        with (
            mock.patch.object(main, "agent_message") as send,
            mock.patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("PYTEST_CURRENT_TEST", None)
            main.install_rebase_recording_notifier(None)
            try:
                main._rebase_bot_notification_sender(
                    "wiki", "hi", "137:sha-key:escalated:head-final"
                )
            finally:
                # Restore the pytest env marker so later tests keep their
                # safety net.
                os.environ["PYTEST_CURRENT_TEST"] = "restored"
        self.assertEqual(send.call_count, 1)
        call_args = send.call_args
        ticket = call_args.args[0] if call_args.args else call_args.kwargs.get("ticket")
        body = (
            call_args.args[1]
            if len(call_args.args) > 1
            else call_args.kwargs.get("body")
        )
        self.assertEqual(ticket, "wiki")
        self.assertEqual(body.dedupe_key, "137:sha-key:escalated:head-final")
        self.assertEqual(body.text, "hi")
        self.assertEqual(body.source, "rebase-bot")

    def test_bounded_retries_reuse_the_same_delivery_id(self) -> None:
        # A raising sender must be retried up to the bound WITH the same
        # delivery id, so downstream dedupe drops the duplicates.
        seen_ids: list[str] = []

        def flaky(_target: str, _msg: str, delivery_id: str) -> None:
            seen_ids.append(delivery_id)
            raise RuntimeError("network flap")

        with tempfile.TemporaryDirectory() as raw:
            fake_main = SimpleNamespace(AGENT_RUNTIME_DIR=raw)
            with mock.patch.object(rebase_bot, "_main", return_value=fake_main):
                _fresh_state()
                job = rebase_durable._RebaseJob(
                    job_id="retry-with-key",
                    worktree=Path(raw),
                    prompt="p",
                    done=threading.Event(),
                    pr_number=137,
                    expected_sha="sha-retry",
                    ticket="WIKI-175",
                    worker_id="WIKI-175-IMPL",
                    orchestrator="wiki",
                    durable=True,
                )
                result = {
                    "status": "escalated",
                    "head_sha": "head-retry",
                    "resolved_files": [],
                    "escalated_hunks": ["fail"],
                }
                rebase_durable._persist_completion(job, result)

                clock = [time.time()]

                def advancing_clock() -> float:
                    clock[0] += 3600.0
                    return clock[0]

                for _ in range(rebase_durable._OUTBOX_MAX_ATTEMPTS + 3):
                    rebase_bot._flush_outbox(flaky, _now=advancing_clock)
        # Sender was retried more than once — the bound governs how many.
        self.assertGreater(len(seen_ids), 1)
        self.assertLessEqual(len(seen_ids), rebase_durable._OUTBOX_MAX_ATTEMPTS)
        self.assertTrue(all(sid == seen_ids[0] for sid in seen_ids))
        self.assertEqual(seen_ids[0], "137:sha-retry:escalated:head-retry")


if __name__ == "__main__":
    unittest.main()
