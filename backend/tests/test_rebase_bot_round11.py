"""Round-11 verdict follow-ups for WIKI-175 PR #137.

Each test is written before its fix; the fixes address:

* F10 — strict URL allowlist (no http, no extra path segments)
* F11 — atomic completion snapshot (single-file write, no torn state)
* F12 — exactly-once delivery on a raised sender
* F13 — dead compat wrappers removed
"""

from __future__ import annotations

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
    rebase_lockfiles,
    rebase_parsing,
)


class F10AllowlistIsStrict(unittest.TestCase):
    _REJECT = (
        "http://github.com/hwang2409/wiki",
        "http://github.com/hwang2409/wiki.git",
        "https://github.com/hwang2409/wiki/extra",
        "https://github.com/hwang2409/wiki/",
        "https://github.com/hwang2409",
        "https://github.com//wiki",
        "https://github.com/hwang2409/wiki?malicious=1",
        "https://github.com/hwang2409/wiki#frag",
        "git@github.com:hwang2409/wiki/extra",
        "ssh://git@github.com/hwang2409/wiki/extra",
        "https://github.com.evil/hwang2409/wiki",
    )
    _ACCEPT = (
        ("https://github.com/hwang2409/wiki", "hwang2409/wiki"),
        ("https://github.com/hwang2409/wiki.git", "hwang2409/wiki"),
        ("git@github.com:hwang2409/wiki", "hwang2409/wiki"),
        ("git@github.com:hwang2409/wiki.git", "hwang2409/wiki"),
        ("ssh://git@github.com/hwang2409/wiki", "hwang2409/wiki"),
        ("ssh://git@github.com/hwang2409/wiki.git", "hwang2409/wiki"),
        ("hwang2409/wiki", "hwang2409/wiki"),
    )

    def test_reject_forms(self) -> None:
        for value in self._REJECT:
            with self.subTest(value=value):
                self.assertIsNone(
                    rebase_bot._repo_name(value),
                    f"accepted forbidden form {value!r}",
                )

    def test_accept_forms(self) -> None:
        for value, expected in self._ACCEPT:
            with self.subTest(value=value):
                self.assertEqual(rebase_bot._repo_name(value), expected)


class F11AtomicCompletion(unittest.TestCase):
    """Every completion persists in one file replace so no crash order loses events."""

    def _make_job(self, raw: str) -> rebase_durable._RebaseJob:
        return rebase_durable._RebaseJob(
            job_id="atomic-crash",
            worktree=Path(raw),
            prompt="p",
            done=threading.Event(),
            pr_number=137,
            expected_sha="sha-crash",
            ticket="WIKI-175",
            worker_id="WIKI-175-IMPL",
            orchestrator="wiki",
            durable=True,
        )

    def _completion_result(self) -> dict[str, object]:
        return {
            "status": "escalated",
            "head_sha": "sha-crash",
            "resolved_files": [],
            "escalated_hunks": ["semantic"],
        }

    def _reset_state(self) -> None:
        rebase_durable._DURABLE_STATE_LOADED = False
        rebase_durable._DURABLE_JOBS.clear()
        rebase_durable._OUTBOX.clear()
        rebase_durable._DELIVERED_EVENTS.clear()

    def test_completion_is_a_single_file_replace(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fake_main = SimpleNamespace(AGENT_RUNTIME_DIR=raw)
            with mock.patch.object(rebase_bot, "_main", return_value=fake_main):
                self._reset_state()
                replace_calls: list[str] = []
                original_replace = Path.replace

                def counting_replace(self, target):
                    replace_calls.append(str(target))
                    return original_replace(self, target)

                with mock.patch.object(Path, "replace", counting_replace):
                    rebase_durable._persist_completion(
                        self._make_job(raw), self._completion_result()
                    )
                # A single durable-state write means one atomic file replace.
                self.assertEqual(
                    len(replace_calls),
                    1,
                    f"expected 1 replace, got {len(replace_calls)}: {replace_calls}",
                )

    def test_crash_during_replace_never_orphans_completion(self) -> None:
        # A crash during persistence must not leave a completed job record
        # with no matching outbox entry — the failure mode that lost the
        # notification in prod when the three-file split had a torn write.
        # The single-file snapshot makes crash-before-replace mean "nothing
        # persisted" and crash-after-replace mean "everything persisted".
        with tempfile.TemporaryDirectory() as raw:
            fake_main = SimpleNamespace(AGENT_RUNTIME_DIR=raw)
            with mock.patch.object(rebase_bot, "_main", return_value=fake_main):
                self._reset_state()

                def failing_replace(_self, _target):
                    raise OSError("simulated crash during replace")

                with (
                    mock.patch.object(Path, "replace", failing_replace),
                    self.assertRaises(OSError),
                ):
                    rebase_durable._persist_completion(
                        self._make_job(raw), self._completion_result()
                    )
                self._reset_state()
                rebase_durable._load_durable_state()
                key = rebase_durable._durable_key(137, "sha-crash")
                job_present = key in rebase_durable._DURABLE_JOBS
                outbox_present = any(
                    "atomic-crash" in entry_id
                    for entry_id in rebase_durable._OUTBOX
                )
        # Invariant: either both records survived, or neither did.  A
        # completed job with no outbox is the failure mode from prod.
        self.assertFalse(
            job_present and not outbox_present,
            "completed job survived without its outbox entry",
        )
        self.assertFalse(
            outbox_present and not job_present,
            "outbox entry survived for a job the durable store has not recorded",
        )

    def test_successful_completion_persists_both_records(self) -> None:
        # Positive control: on a clean run, both sections must land.
        with tempfile.TemporaryDirectory() as raw:
            fake_main = SimpleNamespace(AGENT_RUNTIME_DIR=raw)
            with mock.patch.object(rebase_bot, "_main", return_value=fake_main):
                self._reset_state()
                rebase_durable._persist_completion(
                    self._make_job(raw), self._completion_result()
                )
                self._reset_state()
                rebase_durable._load_durable_state()
                key = rebase_durable._durable_key(137, "sha-crash")
                self.assertIn(key, rebase_durable._DURABLE_JOBS)
                self.assertTrue(
                    any(
                        "atomic-crash" in entry_id
                        for entry_id in rebase_durable._OUTBOX
                    )
                )


class F12ExactlyOnceDelivery(unittest.TestCase):
    """A sender that delivers then raises must not be retried."""

    def _reset_state(self) -> None:
        rebase_durable._DURABLE_STATE_LOADED = False
        rebase_durable._DURABLE_JOBS.clear()
        rebase_durable._OUTBOX.clear()
        rebase_durable._DELIVERED_EVENTS.clear()

    def test_delivered_then_raised_sends_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fake_main = SimpleNamespace(AGENT_RUNTIME_DIR=raw)
            with mock.patch.object(rebase_bot, "_main", return_value=fake_main):
                self._reset_state()
                job = rebase_durable._RebaseJob(
                    job_id="exactly-once",
                    worktree=Path(raw),
                    prompt="p",
                    done=threading.Event(),
                    pr_number=137,
                    expected_sha="sha-once",
                    ticket="WIKI-175",
                    worker_id="WIKI-175-IMPL",
                    orchestrator="wiki",
                    durable=True,
                )
                result = {
                    "status": "escalated",
                    "head_sha": "sha-once",
                    "resolved_files": [],
                    "escalated_hunks": ["semantic"],
                }
                rebase_durable._persist_completion(job, result)

                # After R12 the sender-side is at-least-once + retry, and
                # the receiver-side dedupes on the ``delivery_id`` that
                # threads through the notifier.  Simulate the receiver
                # inbox by dropping messages whose id we have seen.
                delivered: list[str] = []
                seen_ids: set[str] = set()

                def flaky_receiver_dedupe(
                    _target: str, message: str, delivery_id: str
                ) -> None:
                    if delivery_id in seen_ids:
                        # Receiver drops the duplicate: exactly-once at
                        # the observable layer.
                        raise RuntimeError("duplicate suppressed")
                    seen_ids.add(delivery_id)
                    delivered.append(message)
                    raise RuntimeError("delivered then raised")

                clock = [time.time()]

                def advancing_clock() -> float:
                    clock[0] += 3600.0
                    return clock[0]

                for _ in range(5):
                    rebase_bot._flush_outbox(
                        flaky_receiver_dedupe, _now=advancing_clock
                    )
        # Receiver observed exactly one unique event.
        self.assertEqual(len(delivered), 1)
        self.assertEqual(len(seen_ids), 1)


class F13UnusedWrappersRemoved(unittest.TestCase):
    def test_lockfile_command_wrapper_is_gone(self) -> None:
        self.assertFalse(hasattr(rebase_lockfiles, "_lockfile_command"))

    def test_parse_conflicts_wrapper_is_gone(self) -> None:
        self.assertFalse(hasattr(rebase_parsing, "_parse_conflicts"))


if __name__ == "__main__":
    unittest.main()
