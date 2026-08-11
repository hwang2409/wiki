from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from backend.app import dashboard, main
from backend.app.agent_runtime.archive_protocol import commit_archive


class InlineExecutor:
    def submit(self, fn, *args):
        fn(*args)


def _row(**overrides) -> dict:
    row = {
        "ticket": "PHO-1",
        "live": False,
        "role": "implement",
        "kind": "cc",
        "state": None,
        "step": None,
        "blocker": None,
        "pr": None,
        "outcome": None,
        "updated_at": "2026-07-17T10:00:00+00:00",
    }
    row.update(overrides)
    return row


def _enrich(**overrides) -> dict:
    data = {
        "title": "Fix the thing",
        "state": "OPEN",
        "updated_at": "2026-07-17T12:00:00Z",
        "merged_at": None,
        "checks": "pass",
        "failing_check": None,
        "thread_total": 0,
        "thread_unresolved": 0,
        "deployed": None,
    }
    data.update(overrides)
    return data


class DeriveStatusTests(unittest.TestCase):
    def test_merged_and_deployed_is_prod(self) -> None:
        self.assertEqual(
            dashboard.derive_status(_row(), _enrich(state="MERGED", deployed=True)),
            ("prod", None),
        )

    def test_merged_not_deployed_is_merged(self) -> None:
        for deployed in (False, None):
            self.assertEqual(
                dashboard.derive_status(_row(), _enrich(state="MERGED", deployed=deployed)),
                ("merged", None),
            )

    def test_closed_pr(self) -> None:
        self.assertEqual(
            dashboard.derive_status(_row(), _enrich(state="CLOSED")),
            ("closed", None),
        )

    def test_open_failing_beats_comments(self) -> None:
        enrich = _enrich(checks="fail", failing_check="pytest", thread_total=3)
        self.assertEqual(dashboard.derive_status(_row(), enrich), ("failing", "pytest"))

    def test_open_with_review_threads_is_has_comments(self) -> None:
        enrich = _enrich(thread_total=4, thread_unresolved=2)
        self.assertEqual(
            dashboard.derive_status(_row(), enrich),
            ("has-comments", "2/4 threads unresolved"),
        )

    def test_open_pending_checks(self) -> None:
        self.assertEqual(
            dashboard.derive_status(_row(), _enrich(checks="pending")),
            ("checks-pending", None),
        )

    def test_open_clean_is_passing(self) -> None:
        self.assertEqual(dashboard.derive_status(_row(), _enrich()), ("passing", None))
        self.assertEqual(
            dashboard.derive_status(_row(), _enrich(checks=None)),
            ("passing", None),
        )

    def test_outcome_merged_with_pr_but_no_enrichment(self) -> None:
        row = _row(outcome="merged", pr="https://github.com/hwang2409/wiki/pull/1")
        self.assertEqual(dashboard.derive_status(row, None), ("merged", None))

    def test_outcome_merged_without_pr_is_merged_local(self) -> None:
        row = _row(outcome="merged")
        self.assertEqual(dashboard.derive_status(row, None), ("merged (local)", None))

    def test_outcome_abandoned(self) -> None:
        self.assertEqual(
            dashboard.derive_status(_row(outcome="abandoned"), None),
            ("abandoned", None),
        )

    def test_pr_without_enrichment_is_pr_open(self) -> None:
        row = _row(pr="https://github.com/phoebe-health/phoebe/pull/9", step="waiting on CI")
        self.assertEqual(dashboard.derive_status(row, None), ("pr-open", "waiting on CI"))

    def test_live_worker_states(self) -> None:
        self.assertEqual(
            dashboard.derive_status(_row(live=True, state="working", step="edits"), None),
            ("implementing", "edits"),
        )
        self.assertEqual(
            dashboard.derive_status(_row(live=True, state="blocked", blocker="quota"), None),
            ("blocked", "quota"),
        )
        self.assertEqual(
            dashboard.derive_status(_row(live=True, state="merge-ready", step="done"), None),
            ("merge-ready", "done"),
        )

    def test_live_blocked_with_unenriched_pr_stays_blocked(self) -> None:
        row = _row(
            live=True,
            state="blocked",
            blocker="quota",
            pr="https://github.com/other/repo/pull/2",
        )
        self.assertEqual(dashboard.derive_status(row, None), ("blocked", "quota"))

    def test_archived_without_outcome_falls_back_to_state(self) -> None:
        self.assertEqual(
            dashboard.derive_status(_row(state="merge-ready", step="s"), None),
            ("merge-ready", "s"),
        )
        self.assertEqual(dashboard.derive_status(_row(), None), ("unknown", None))


class RowBuildingTests(unittest.TestCase):
    def test_live_worker_rows_only_include_registered(self) -> None:
        spawned = datetime(2025, 7, 17, 8, 0, 0, tzinfo=timezone.utc)
        registry = {
            "WIKI-1": {
                "current": {
                    "role": "implement",
                    "kind": "cc",
                    "spawned_at": spawned.isoformat(),
                    "updated_at": "2025-07-17T09:00:00+00:00",
                }
            },
            "wiki-orch": {"current": {"role": "orchestrator", "kind": "cc"}},
            "_orchestrators": {"misc": {"window": "@1"}},
        }
        statuses = {
            "WIKI-1": {"state": "working", "step": "edit", "pr": None, "_mtime": 1752760000.0},
            "wiki-orch": {"state": "working"},
            "misc": {"state": "working"},
            "WIKI-9": {"state": "merge-ready", "step": "pr up", "_mtime": 1752760100.0},
        }
        rows = dashboard.live_worker_rows(registry, statuses)
        tickets = {row["ticket"] for row in rows}
        self.assertEqual(tickets, {"WIKI-1"})
        row = rows[0]
        self.assertEqual(row["state"], "working")
        self.assertEqual(
            row["updated_at"],
            datetime.fromtimestamp(1752760000.0, tz=timezone.utc).isoformat(),
        )

    def test_live_worker_rows_only_include_implementation_workers(self) -> None:
        registry = {
            "WIKI-IMPLEMENT": {"current": {"role": "implement", "kind": "cc"}},
            "WIKI-REVIEW1": {"current": {"role": "review", "kind": "cc"}},
            "WIKI-PLAN": {"current": {"role": "plan", "kind": "cc"}},
            "WIKI-ORCH": {"current": {"role": "orchestrator", "kind": "cc"}},
            "WIKI-LEGACY": {"current": {"kind": "cc"}},
            "WIKI-SIM1": {"current": {"role": "implement", "kind": "cc"}},
            "WIKI-DEMO": {"current": {"role": "implement", "kind": "cc"}},
            "TEST-1": {"current": {"role": "implement", "kind": "cc"}},
            "WIKI-85-DEMO-CC": {"current": {"role": "implement", "kind": "cc"}},
            "WIKI-85-VERIFY": {"current": {"role": "implement", "kind": "cc"}},
            "MITMWEB-B2-REVIEW20C": {"current": {"role": "implement", "kind": "cc"}},
            "MITMWEB-B2-REVIEW5B": {"current": {"role": "implement", "kind": "cc"}},
            "MITMWEB-F1-REVIEW4B": {"current": {"role": "implement", "kind": "cc"}},
            "REVIEW-10983": {"current": {"role": "implement", "kind": "cc"}},
        }

        rows = dashboard.live_worker_rows(registry, {})

        self.assertEqual(
            {row["ticket"] for row in rows},
            {"WIKI-IMPLEMENT", "WIKI-LEGACY"},
        )

    def test_missing_role_fallback_uses_anchored_one_shot_tokens(self) -> None:
        included = [
            "WIKI-ORDINARY",
            "WIKI-123-ORDINARY",
            "WIKI-REVIEWING",
            "WIKI-TESTING",
            "WIKI-SIMULATION",
            "WIKI-EVALUATION",
            "WIKI-AUDITOR",
            "WIKI-DEMOGRAPHIC",
        ]
        excluded = [
            "WIKI-REVIEW",
            "WIKI-REVIEW1",
            "WIKI-SIM",
            "WIKI-SIM1",
            "WIKI-EVAL",
            "WIKI-EVAL2",
            "WIKI-AUDIT",
            "WIKI-AUDIT3",
            "WIKI-CANARY",
            "WIKI-CANARY4",
            "WIKI-THERMO",
            "WIKI-THERMO5",
            "WIKI-DEMO",
            "WIKI-DEMO6",
            "WIKI-TEST",
            "WIKI-TEST7",
            "TEST-1",
            "DEMO-2",
            "WIKI-DEMO2-X",
            "WIKI-VERIFY",
            "WIKI-VERIFY-CC",
        ]
        registry = {
            ticket: {"current": {"kind": "cc"}}
            for ticket in included + excluded
        }

        rows = dashboard.live_worker_rows(registry, {})

        self.assertEqual({row["ticket"] for row in rows}, set(included))

    def test_orchestrator_is_still_excluded(self) -> None:
        rows = dashboard.live_worker_rows(
            {"WIKI-ORCH": {"current": {"role": "orchestrator", "kind": "cc"}}},
            {},
        )
        self.assertEqual(rows, [])

    def test_status_older_than_spawned_by_subsecond_is_ignored(self) -> None:
        # Even a 500ms-old status file must not leak into a fresh session.
        spawned = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
        registry = {
            "WIKI-3": {
                "current": {
                    "role": "implement",
                    "kind": "cc",
                    "state": "working",
                    "spawned_at": spawned.isoformat(),
                    "pr": None,
                }
            }
        }
        stale = {
            "state": "merge-ready",
            "step": "old",
            "pr": "https://github.com/hwang2409/wiki/pull/9",
            "_mtime": spawned.timestamp() - 0.5,
        }
        rows = dashboard.live_worker_rows(registry, {"WIKI-3": stale})
        row = rows[0]
        self.assertEqual(row["state"], "working")
        self.assertIsNone(row["pr"])
        self.assertIsNone(row["step"])

    def test_status_at_spawned_at_is_accepted(self) -> None:
        spawned = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
        registry = {
            "WIKI-4": {
                "current": {
                    "role": "implement",
                    "kind": "cc",
                    "spawned_at": spawned.isoformat(),
                    "pr": None,
                }
            }
        }
        fresh = {"state": "merge-ready", "step": "done", "_mtime": spawned.timestamp()}
        rows = dashboard.live_worker_rows(registry, {"WIKI-4": fresh})
        self.assertEqual(rows[0]["state"], "merge-ready")
        self.assertEqual(rows[0]["step"], "done")

    def test_stale_status_older_than_spawned_at_is_ignored(self) -> None:
        spawned = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
        registry = {
            "WIKI-2": {
                "current": {
                    "role": "implement",
                    "kind": "cc",
                    "state": "working",
                    "spawned_at": spawned.isoformat(),
                    "pr": None,
                }
            }
        }
        # Status file predates the current session's spawn — must be ignored.
        stale = {
            "state": "merge-ready",
            "step": "old",
            "pr": "https://github.com/hwang2409/wiki/pull/99",
            "_mtime": spawned.timestamp() - 3600,
        }
        rows = dashboard.live_worker_rows(registry, {"WIKI-2": stale})
        row = rows[0]
        self.assertEqual(row["state"], "working")
        self.assertIsNone(row["pr"])
        self.assertIsNone(row["step"])

    def test_archived_rows_keep_newest_session_per_ticket(self) -> None:
        archived = [
            {"ticket": "GAU-1", "archived_at": "2026-07-16T10:00:00+00:00", "outcome": "merged", "pr": None},
            {"ticket": "GAU-1", "archived_at": "2026-07-15T10:00:00+00:00", "outcome": "abandoned", "pr": None},
        ]
        rows = dashboard.archived_rows(archived)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "merged")

    def test_archived_rows_apply_worker_filter_and_keep_newest_session(self) -> None:
        archived = [
            {
                "ticket": "WIKI-REVIEW1",
                "archived_at": "2026-07-17T10:00:00+00:00",
                "role": "review",
            },
            {"ticket": "WIKI-SIM1", "archived_at": "2026-07-17T09:00:00+00:00"},
            {
                "ticket": "WIKI-IMPLEMENT",
                "archived_at": "2026-07-17T08:00:00+00:00",
                "role": "implement",
            },
            {"ticket": "WIKI-LEGACY", "archived_at": "2026-07-17T07:00:00+00:00"},
            {
                "ticket": "WIKI-ORCH",
                "archived_at": "2026-07-17T06:00:00+00:00",
                "role": "orchestrator",
            },
        ]

        rows = dashboard.archived_rows(archived)

        self.assertEqual(
            {row["ticket"] for row in rows},
            {"WIKI-IMPLEMENT", "WIKI-LEGACY"},
        )

    def test_one_shot_names_override_implement_role(self) -> None:
        for ticket in (
            "PHO-13944-SIM",
            "PHO-12880-DEMO",
            "WIKI-54-DEMO",
            "WIKI-85-DEMO-CC",
            "WIKI-85-VERIFY",
            "MITMWEB-B2-REVIEW20C",
            "MITMWEB-B2-REVIEW5B",
            "MITMWEB-F1-REVIEW4B",
            "REVIEW-10983",
            "TEST-1",
        ):
            self.assertFalse(dashboard._is_dashboard_worker(ticket, "implement"))
        for ticket in ("WIKI-134", "PHO-13944"):
            self.assertTrue(dashboard._is_dashboard_worker(ticket, "implement"))

    def test_merge_rows_prefers_live(self) -> None:
        live = [_row(ticket="T-1", live=True, state="working")]
        archived = [_row(ticket="T-1", outcome="merged"), _row(ticket="T-2", outcome="closed")]
        merged = dashboard.merge_rows(live, archived)
        by_ticket = {row["ticket"]: row for row in merged}
        self.assertEqual(len(merged), 2)
        self.assertTrue(by_ticket["T-1"]["live"])
        self.assertEqual(by_ticket["T-2"]["outcome"], "closed")

    def test_invalid_pr_urls_are_dropped(self) -> None:
        rows = dashboard.archived_rows(
            [{"ticket": "T-3", "archived_at": "2026-07-16T10:00:00+00:00", "pr": "not-a-url"}]
        )
        self.assertIsNone(rows[0]["pr"])


class PrCacheTests(unittest.TestCase):
    def _inline_cache(self, fetch) -> dashboard.PrCache:
        cache = dashboard.PrCache(fetch)
        cache._executor = InlineExecutor()
        return cache

    def test_fetch_failure_keeps_stale_data(self) -> None:
        calls = []

        def fetch(url, repo):
            calls.append(url)
            if len(calls) > 1:
                raise RuntimeError("gh down")
            return _enrich(title="v1")

        cache = self._inline_cache(fetch)
        cache.request_refresh("https://github.com/phoebe-health/phoebe/pull/1", "phoebe-health/phoebe")
        self.assertEqual(cache.lookup("https://github.com/phoebe-health/phoebe/pull/1")["title"], "v1")
        with mock.patch.object(dashboard.time, "time", return_value=cache._entries[
            "https://github.com/phoebe-health/phoebe/pull/1"
        ]["checked_at"] + dashboard.CACHE_TTL_SECONDS + 1):
            cache.request_refresh("https://github.com/phoebe-health/phoebe/pull/1", "phoebe-health/phoebe")
        self.assertEqual(len(calls), 2)
        self.assertEqual(cache.lookup("https://github.com/phoebe-health/phoebe/pull/1")["title"], "v1")

    def test_fresh_entries_are_not_refetched(self) -> None:
        calls = []
        cache = self._inline_cache(lambda url, repo: calls.append(url) or _enrich())
        cache.request_refresh("https://github.com/phoebe-health/phoebe/pull/2", "phoebe-health/phoebe")
        cache.request_refresh("https://github.com/phoebe-health/phoebe/pull/2", "phoebe-health/phoebe")
        self.assertEqual(len(calls), 1)

    def test_cold_lookup_returns_before_fetch_completes(self) -> None:
        release = threading.Event()
        fetch_started = threading.Event()

        def fetch(url, repo):
            fetch_started.set()
            release.wait(timeout=5)
            return _enrich(title="warm")

        cache = dashboard.PrCache(fetch)
        pr = "https://github.com/phoebe-health/phoebe/pull/5"
        archived = [{"ticket": "PHO-5", "archived_at": "2026-07-17T09:00:00+00:00", "pr": pr}]
        try:
            t0 = time.monotonic()
            payload = dashboard.build_payload({}, {}, archived, cache=cache)
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 0.5, "build_payload blocked on network fetch")
            self.assertTrue(fetch_started.wait(timeout=5), "background fetch never started")
            self.assertFalse(payload["tickets"][0]["enriched"])
            self.assertEqual(payload["tickets"][0]["status"], "pr-open")
        finally:
            release.set()
            # Drain the executor so the thread exits before test teardown.
            with cache._lock:
                executor = cache._executor
            if executor is not None:
                executor.shutdown(wait=True)

    def test_terminal_states_are_not_refetched(self) -> None:
        calls = []

        def fetch(url, repo):
            calls.append(url)
            return _enrich(state="MERGED", deployed=True)

        cache = self._inline_cache(fetch)
        url = "https://github.com/phoebe-health/phoebe/pull/3"
        cache.request_refresh(url, "phoebe-health/phoebe")
        with mock.patch.object(dashboard.time, "time", return_value=cache._entries[url]["checked_at"] + 10_000):
            cache.request_refresh(url, "phoebe-health/phoebe")
        self.assertEqual(len(calls), 1)

    def test_merged_untracked_repo_is_terminal(self) -> None:
        # No `deployed` key → repo isn't deployment-tracked → merged is terminal.
        data = {"state": "MERGED", "title": "x"}
        self.assertTrue(dashboard._is_terminal(data))

    def test_merged_tracked_unknown_keeps_refreshing(self) -> None:
        calls = []

        def fetch(url, repo):
            calls.append(url)
            return {"state": "MERGED", "deployed": None}

        cache = self._inline_cache(fetch)
        url = "https://github.com/phoebe-health/phoebe/pull/8"
        cache.request_refresh(url, "phoebe-health/phoebe")
        self.assertFalse(dashboard._is_terminal(cache.lookup(url)))
        with mock.patch.object(
            dashboard.time, "time", return_value=cache._entries[url]["checked_at"] + dashboard.CACHE_TTL_SECONDS + 1
        ):
            cache.request_refresh(url, "phoebe-health/phoebe")
        self.assertEqual(len(calls), 2)


class ProdDeploySingleFlightTests(unittest.TestCase):
    def setUp(self) -> None:
        dashboard._deploy_sha_cache.clear()
        dashboard._deploy_sha_inflight.clear()

    def test_inflight_owner_failure_wakes_waiter_and_retries(self) -> None:
        """Concurrent waiter must wake, retry, and get the successful SHA.

        Removing the failure-path `waiter.set()` would leave the waiter
        blocked on the inflight Event (60s wait) and the join(5) below
        would find the waiter thread still alive.
        """
        release_owner = threading.Event()
        owner_started = threading.Event()
        waiter_entered = threading.Event()
        call_count = 0
        call_lock = threading.Lock()

        def flaky_fetch(repo):
            nonlocal call_count
            with call_lock:
                call_count += 1
                n = call_count
            if n == 1:
                owner_started.set()
                self.assertTrue(release_owner.wait(timeout=5))
                raise RuntimeError("simulated network failure")
            return "def456"

        owner_err: dict[str, Exception] = {}
        waiter_result: dict[str, str | None] = {}

        def run_owner():
            try:
                dashboard._prod_deploy_sha("phoebe-health/phoebe")
            except Exception as exc:
                owner_err["err"] = exc

        def run_waiter():
            self.assertTrue(owner_started.wait(timeout=5))
            waiter_entered.set()
            waiter_result["sha"] = dashboard._prod_deploy_sha("phoebe-health/phoebe")

        original_fetch = dashboard._fetch_prod_deploy_sha
        dashboard._fetch_prod_deploy_sha = flaky_fetch
        try:
            owner_t = threading.Thread(target=run_owner)
            waiter_t = threading.Thread(target=run_waiter)
            owner_t.start()
            waiter_t.start()
            self.assertTrue(waiter_entered.wait(timeout=5))
            # Give the waiter a beat to actually enter `waiter.wait(timeout=60)`.
            time.sleep(0.1)
            release_owner.set()
            owner_t.join(timeout=5)
            waiter_t.join(timeout=5)
        finally:
            dashboard._fetch_prod_deploy_sha = original_fetch

        self.assertFalse(owner_t.is_alive(), "owner did not complete")
        self.assertFalse(
            waiter_t.is_alive(),
            "waiter still blocked — owner's failure path did not wake it",
        )
        self.assertIsInstance(owner_err.get("err"), RuntimeError)
        self.assertEqual(waiter_result.get("sha"), "def456")
        self.assertEqual(call_count, 2, "one owner call + one waiter retry — no more, no fewer")
        with dashboard._deploy_sha_lock:
            self.assertNotIn("phoebe-health/phoebe", dashboard._deploy_sha_inflight)
            # Cache holds the successful retry result.
            self.assertEqual(dashboard._deploy_sha_cache["phoebe-health/phoebe"][1], "def456")

    def test_owner_failure_releases_inflight_and_next_call_retries(self) -> None:
        release = threading.Event()
        outcomes: list[str] = []
        outcomes_lock = threading.Lock()
        call_count = 0
        call_lock = threading.Lock()

        def fetch(repo):
            nonlocal call_count
            with call_lock:
                call_count += 1
                current = call_count
            if current == 1:
                # Wait for the second caller to arrive on the inflight event…
                release.wait(timeout=5)
                raise RuntimeError("gh explosion")
            return "def456"

        def call(label):
            with mock.patch.object(dashboard, "_fetch_prod_deploy_sha", fetch):
                try:
                    sha = dashboard._prod_deploy_sha("phoebe-health/phoebe")
                    with outcomes_lock:
                        outcomes.append(f"{label}:{sha}")
                except RuntimeError as exc:
                    with outcomes_lock:
                        outcomes.append(f"{label}:err:{exc}")

        owner = threading.Thread(target=call, args=("owner",))
        owner.start()
        # Wait until owner is inside the fetch (call_count == 1) then release
        # so it raises. The retry after join must fetch anew (no stale cache,
        # no stuck inflight entry).
        for _ in range(200):
            with call_lock:
                if call_count >= 1:
                    break
            time.sleep(0.01)
        release.set()
        owner.join(timeout=5)
        # Inflight must be cleared even though the owner raised.
        with dashboard._deploy_sha_lock:
            self.assertNotIn("phoebe-health/phoebe", dashboard._deploy_sha_inflight)
            self.assertNotIn("phoebe-health/phoebe", dashboard._deploy_sha_cache)
        call("retry")
        self.assertIn("retry:def456", outcomes)
        self.assertEqual(call_count, 2)

    def test_concurrent_misses_share_one_fetch(self) -> None:
        release = threading.Event()
        call_count = 0
        call_lock = threading.Lock()

        def fetch(repo):
            nonlocal call_count
            with call_lock:
                call_count += 1
            release.wait(timeout=5)
            return "abc123"

        results: list[str | None] = []
        results_lock = threading.Lock()

        def worker():
            sha = dashboard._prod_deploy_sha("phoebe-health/phoebe")
            with results_lock:
                results.append(sha)

        original_fetch = dashboard._fetch_prod_deploy_sha
        dashboard._fetch_prod_deploy_sha = fetch
        try:
            threads = [threading.Thread(target=worker) for _ in range(4)]
            for thread in threads:
                thread.start()
            # Give threads a moment to enter and pick the same inflight event.
            time.sleep(0.05)
            release.set()
            for thread in threads:
                thread.join(timeout=5)
        finally:
            dashboard._fetch_prod_deploy_sha = original_fetch
        self.assertEqual(call_count, 1)
        self.assertEqual(results, ["abc123"] * 4)


class StubCache:
    def __init__(self, data: dict[str, dict]) -> None:
        self.data = data
        self.refreshed: list[tuple[str, str]] = []

    def lookup(self, pr_url: str):
        return self.data.get(pr_url)

    def request_refresh(self, pr_url: str, repo: str) -> None:
        self.refreshed.append((pr_url, repo))


class BuildPayloadTests(unittest.TestCase):
    def test_allowlist_controls_enrichment(self) -> None:
        phoebe_pr = "https://github.com/phoebe-health/phoebe/pull/11"
        wiki_pr = "https://github.com/hwang2409/wiki/pull/22"
        cache = StubCache({phoebe_pr: _enrich(state="MERGED", deployed=True, title="Phoebe fix")})
        archived = [
            {"ticket": "PHO-11", "archived_at": "2026-07-16T10:00:00+00:00", "outcome": "merged", "pr": phoebe_pr},
            {"ticket": "WIKI-22", "archived_at": "2026-07-16T11:00:00+00:00", "outcome": "merged", "pr": wiki_pr},
        ]
        payload = dashboard.build_payload({}, {}, archived, cache=cache)
        by_ticket = {row["ticket"]: row for row in payload["tickets"]}
        self.assertEqual(by_ticket["PHO-11"]["status"], "prod")
        self.assertEqual(by_ticket["PHO-11"]["description"], "Phoebe fix")
        self.assertTrue(by_ticket["PHO-11"]["enriched"])
        self.assertEqual(by_ticket["WIKI-22"]["status"], "merged")
        self.assertFalse(by_ticket["WIKI-22"]["enriched"])
        self.assertEqual(cache.refreshed, [(phoebe_pr, "phoebe-health/phoebe")])

    def test_date_is_max_of_row_and_pr_updates(self) -> None:
        pr = "https://github.com/phoebe-health/phoebe/pull/12"
        cache = StubCache({pr: _enrich(updated_at="2026-07-17T12:00:00Z")})
        archived = [
            {"ticket": "PHO-12", "archived_at": "2026-07-16T10:00:00+00:00", "outcome": None, "pr": pr},
        ]
        payload = dashboard.build_payload({}, {}, archived, cache=cache)
        self.assertEqual(payload["tickets"][0]["date"], "2026-07-17T12:00:00+00:00")

    def test_rows_sorted_by_date_desc(self) -> None:
        archived = [
            {"ticket": "A-1", "archived_at": "2026-07-14T10:00:00+00:00"},
            {"ticket": "A-2", "archived_at": "2026-07-16T10:00:00+00:00"},
            {"ticket": "A-3", "archived_at": "2026-07-15T10:00:00+00:00"},
        ]
        payload = dashboard.build_payload({}, {}, archived, cache=StubCache({}))
        self.assertEqual([row["ticket"] for row in payload["tickets"]], ["A-2", "A-3", "A-1"])


class DashboardEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.registry = root / "registry.json"
        self.status_dir = root / "status"
        self.archive = root / "archive"
        self.status_dir.mkdir()
        self.archive.mkdir()
        self.patches = (
            mock.patch.object(main, "AGENT_REGISTRY_PATH", self.registry),
            mock.patch.object(main, "AGENT_STATUS_DIR", self.status_dir),
            mock.patch.object(main, "AGENT_ARCHIVE_DIR", self.archive),
            mock.patch.object(dashboard, "PR_CACHE", StubCache({})),
        )
        for patcher in self.patches:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()
        self.tmp.cleanup()

    @staticmethod
    def _commit_archive(session: Path, run_id: str) -> None:
        (session / "run.json").write_text(
            json.dumps({"run_id": run_id, "provider": "claude"}), encoding="utf-8"
        )
        (session / "raw.jsonl").write_text("raw\n", encoding="utf-8")
        (session / "events.jsonl").write_text("events\n", encoding="utf-8")
        commit_archive(session, run_id=run_id, completed_at="2026-08-01T00:00:00Z")

    def test_endpoint_merges_registry_status_and_archive(self) -> None:
        self.registry.write_text(
            json.dumps(
                {
                    "WIKI-50": {
                        "current": {
                            "role": "implement",
                            "kind": "cc",
                            "updated_at": "2026-07-17T09:00:00+00:00",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        (self.status_dir / "WIKI-50.json").write_text(
            json.dumps({"state": "working", "pr": None, "step": "editing", "blocker": None}),
            encoding="utf-8",
        )
        session = self.archive / "GAU-1" / "20260716-101500"
        session.mkdir(parents=True)
        (session / "final-status.json").write_text(
            json.dumps({"state": "merge-ready", "pr": None, "step": "done"}),
            encoding="utf-8",
        )
        (session / "meta.json").write_text(
            json.dumps({"outcome": "merged", "worker": {"kind": "cc", "role": "implement"}}),
            encoding="utf-8",
        )
        self._commit_archive(session, "gau-1")
        payload = main.dashboard_tickets()
        by_ticket = {row["ticket"]: row for row in payload["tickets"]}
        self.assertEqual(set(by_ticket), {"WIKI-50", "GAU-1"})
        self.assertEqual(by_ticket["WIKI-50"]["status"], "implementing")
        self.assertEqual(by_ticket["WIKI-50"]["description"], "editing")
        self.assertTrue(by_ticket["WIKI-50"]["live"])
        self.assertEqual(by_ticket["GAU-1"]["status"], "merged (local)")
        self.assertFalse(by_ticket["GAU-1"]["live"])
        self.assertEqual(payload["tickets"][0]["ticket"], "WIKI-50")

    def test_endpoint_excludes_real_live_and_archived_one_shot_workers(self) -> None:
        self.registry.write_text(
            json.dumps(
                {
                    "WIKI-135": {"current": {"role": "implement", "kind": "cc"}},
                    "PHO-13944-SIM2": {
                        "current": {"role": "implement", "kind": "cdx"}
                    },
                    "WIKI-85-DEMO-CC": {
                        "current": {"role": "implement", "kind": "cc"}
                    },
                    "WIKI-85-VERIFY": {
                        "current": {"role": "implement", "kind": "cc"}
                    },
                    "MITMWEB-B2-REVIEW20C": {
                        "current": {"role": "implement", "kind": "cc"}
                    },
                    "MITMWEB-B2-REVIEW5B": {
                        "current": {"role": "implement", "kind": "cc"}
                    },
                    "MITMWEB-F1-REVIEW4B": {
                        "current": {"role": "implement", "kind": "cc"}
                    },
                    "REVIEW-10983": {
                        "current": {"role": "implement", "kind": "cc"}
                    },
                }
            ),
            encoding="utf-8",
        )
        for ticket in (
            "PHO-13944-SIM",
            "PHO-12880-DEMO",
            "WIKI-54-DEMO",
            "WIKI-85-DEMO-CC",
            "WIKI-85-VERIFY",
            "MITMWEB-B2-REVIEW20C",
            "MITMWEB-B2-REVIEW5B",
            "MITMWEB-F1-REVIEW4B",
            "REVIEW-10983",
            "TEST-1",
        ):
            session = self.archive / ticket / "20260719-120000"
            session.mkdir(parents=True)
            (session / "meta.json").write_text(
                json.dumps({"worker": {"kind": "cc", "role": "implement"}}),
                encoding="utf-8",
            )
            self._commit_archive(session, f"{ticket}-run")

        payload = main.dashboard_tickets()

        self.assertEqual([row["ticket"] for row in payload["tickets"]], ["WIKI-135"])
        self.assertEqual(len(payload["tickets"]), 1)

    def test_endpoint_survives_missing_inputs(self) -> None:
        payload = main.dashboard_tickets()
        self.assertEqual(payload["tickets"], [])

    def test_archive_dedup_before_ticket_limit_survives_500_session_burst(self) -> None:
        """500 newer sessions for one ticket must not push a smaller older ticket out.

        Mutation to catch: restoring `list_archived(limit=500)` (i.e.
        dropping `latest_per_ticket=True` and applying the cap first).
        With this fixture, that mutation returns 500 rows all for
        PHO-CROWD; PHO-OLD falls off the tail and disappears from the
        dashboard.
        """
        base = datetime(2026, 1, 1, 0, 0, 0)
        crowded = self.archive / "PHO-CROWD"
        crowded.mkdir()
        # 500 sessions spread over ~500 hours so all timestamps are unique
        # AND every one is strictly newer than PHO-OLD below.
        for idx in range(500):
            ts = base + timedelta(hours=idx)
            session = crowded / ts.strftime("%Y%m%d-%H%M%S")
            session.mkdir()
            (session / "meta.json").write_text(
                json.dumps({"worker": {"kind": "cc", "role": "implement"}}),
                encoding="utf-8",
            )
            self._commit_archive(session, f"crowd-{idx}")
        older_ts = base - timedelta(days=30)
        older = self.archive / "PHO-OLD" / older_ts.strftime("%Y%m%d-%H%M%S")
        older.mkdir(parents=True)
        (older / "meta.json").write_text(
            json.dumps({"worker": {"kind": "cc", "role": "implement"}, "outcome": "merged"}),
            encoding="utf-8",
        )
        self._commit_archive(older, "pho-old")
        payload = main.dashboard_tickets()
        tickets = {row["ticket"] for row in payload["tickets"]}
        self.assertIn("PHO-CROWD", tickets)
        self.assertIn(
            "PHO-OLD",
            tickets,
            "older ticket must survive dedup-before-limit — otherwise pre-dedup 500-cap regressed",
        )


class FleetWorkersTests(unittest.TestCase):
    """WIKI-276: the fleet pane shows every non-orchestrator worker."""

    def _registry(self) -> dict:
        return {
            "WIKI-100": {
                "current": {
                    "role": "implement", "kind": "cc", "orch": "wiki-dev",
                    "model": "opus-4.7",
                    "spawned_at": "2026-08-01T00:00:00+00:00",
                }
            },
            "WIKI-100-REVIEW1": {
                "current": {
                    "role": "review", "kind": "cdx", "orch": "wiki-dev",
                    "model": "gpt-5.6-sol",
                    "spawned_at": "2026-08-01T00:10:00+00:00",
                }
            },
            "CHIMY-42": {
                "current": {
                    "role": "implement", "kind": "cdx", "orch": "tooling-dev",
                    "model": "gpt-5.6-luna",
                    "spawned_at": "2026-08-01T01:00:00+00:00",
                }
            },
            "wiki-dev": {"current": {"role": "orchestrator", "kind": "cc"}},
            "_orchestrators": {"wiki-dev": {"window": "@1"}},
        }

    def test_includes_reviewers_and_excludes_orchestrator(self) -> None:
        registry = self._registry()
        workers = dashboard.fleet_workers(registry, {})
        tickets = {w["ticket"] for w in workers}
        self.assertEqual(tickets, {"WIKI-100", "WIKI-100-REVIEW1", "CHIMY-42"})

    def test_status_older_than_spawn_is_ignored(self) -> None:
        spawned = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)
        registry = {
            "WIKI-9": {
                "current": {
                    "role": "implement", "kind": "cc", "orch": "wiki-dev",
                    "spawned_at": spawned.isoformat(),
                }
            }
        }
        stale = {"state": "merge-ready", "step": "old", "_mtime": spawned.timestamp() - 5}
        rows = dashboard.fleet_workers(registry, {"WIKI-9": stale})
        self.assertIsNone(rows[0]["state"])
        self.assertIsNone(rows[0]["step"])

    def test_worker_alarms_stale_only_when_working(self) -> None:
        now = 1_000_000.0
        # merge-ready: never stale (waiting on Henry)
        self.assertEqual(
            dashboard._worker_alarms("merge-ready", now - 3600, None, now=now),
            ["merge-ready"],
        )
        # working + old status: stale
        self.assertEqual(
            dashboard._worker_alarms("working", now - 3600, None, now=now),
            ["stale"],
        )
        # blocked: blocked, not stale, even if old
        self.assertEqual(
            dashboard._worker_alarms("blocked", now - 3600, "quota", now=now),
            ["blocked"],
        )
        # blocker with no matching state -> attention
        self.assertEqual(
            dashboard._worker_alarms("working", now - 60, "waiting on Henry", now=now),
            ["attention"],
        )
        # working + fresh: no alarms
        self.assertEqual(dashboard._worker_alarms("working", now - 60, None, now=now), [])

    def test_mutation_stale_threshold_gates(self) -> None:
        # Guards against regressing the 30-minute rule. If someone changes
        # the constant without updating tests, this pair pins the boundary.
        now = 1_000_000.0
        just_under = dashboard.STALE_STATUS_SECONDS - 1
        just_over = dashboard.STALE_STATUS_SECONDS + 1
        self.assertNotIn("stale", dashboard._worker_alarms("working", now - just_under, None, now=now))
        self.assertIn("stale", dashboard._worker_alarms("working", now - just_over, None, now=now))

    def test_worker_row_carries_alarms_and_age(self) -> None:
        now = 1_000_000.0
        registry = {
            "WIKI-100": {
                "current": {
                    "role": "implement", "kind": "cc", "orch": "wiki-dev",
                    "model": "opus-4.7",
                    "spawned_at": "2026-08-01T00:00:00+00:00",
                }
            }
        }
        statuses = {
            "WIKI-100": {"state": "working", "step": "editing", "pr": None, "_mtime": now - 3600}
        }
        with mock.patch.object(dashboard, "_status_for_current", side_effect=lambda s, c: s or {}):
            rows = dashboard.fleet_workers(registry, statuses, now=now)
        row = rows[0]
        self.assertEqual(row["orch"], "wiki-dev")
        self.assertEqual(row["kind"], "cc")
        self.assertEqual(row["model"], "opus-4.7")
        self.assertIn("stale", row["alarms"])
        self.assertAlmostEqual(row["status_age_s"], 3600, delta=1)


class OrchRollupsTests(unittest.TestCase):
    """WIKI-276: per-orch counts drive the header rollup strip."""

    def test_counts_across_orchestrators(self) -> None:
        workers = [
            {"orch": "wiki-dev", "state": "working", "alarms": []},
            {"orch": "wiki-dev", "state": "merge-ready", "alarms": ["merge-ready"]},
            {"orch": "wiki-dev", "state": "blocked", "alarms": ["blocked"]},
            {"orch": "tooling-dev", "state": "working", "alarms": ["stale"]},
            {"orch": "tooling-dev", "state": "working", "alarms": []},
            {"orch": None, "state": "working", "alarms": []},
        ]
        rollups = dashboard.orch_rollups(workers)
        by_orch = {r["orch"]: r for r in rollups}
        self.assertEqual(by_orch["wiki-dev"]["working"], 1)
        self.assertEqual(by_orch["wiki-dev"]["merge_ready"], 1)
        self.assertEqual(by_orch["wiki-dev"]["blocked"], 1)
        self.assertEqual(by_orch["wiki-dev"]["stalled"], 0)
        self.assertEqual(by_orch["tooling-dev"]["working"], 2)
        self.assertEqual(by_orch["tooling-dev"]["stalled"], 1)
        self.assertEqual(by_orch["(unassigned)"]["working"], 1)

    def test_empty_registry_yields_no_rollups(self) -> None:
        self.assertEqual(dashboard.orch_rollups([]), [])


class ArchivedTodayTests(unittest.TestCase):
    def test_filters_to_current_utc_day(self) -> None:
        anchor = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)
        archived = [
            {"ticket": "WIKI-A", "archived_at": "2026-08-11T02:00:00+00:00", "outcome": "merged"},
            {"ticket": "WIKI-B", "archived_at": "2026-08-11T23:00:00+00:00", "outcome": "closed"},
            {"ticket": "WIKI-C", "archived_at": "2026-08-10T23:59:59+00:00", "outcome": "merged"},
            {"ticket": "WIKI-D", "archived_at": None},
        ]
        rows = dashboard.archived_today(archived, now=anchor)
        tickets = [r["ticket"] for r in rows]
        self.assertEqual(tickets, ["WIKI-B", "WIKI-A"])
        # Newer first: sorting is descending by archived_at.
        self.assertEqual(rows[0]["outcome"], "closed")

    def test_empty_input(self) -> None:
        self.assertEqual(dashboard.archived_today([]), [])


class BuildPagePayloadTests(unittest.TestCase):
    def test_payload_shape(self) -> None:
        registry = {
            "WIKI-1": {
                "current": {
                    "role": "implement", "kind": "cc", "orch": "wiki-dev",
                    "spawned_at": "2026-08-01T00:00:00+00:00",
                }
            }
        }
        anchor = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)
        archived = [
            {
                "ticket": "WIKI-OLD",
                "archived_at": anchor.isoformat(),
                "outcome": "merged",
                "role": "implement",
                "kind": "cc",
                "orch": "wiki-dev",
                "state": "merge-ready",
                "step": "shipped",
                "pr": "https://github.com/hwang2409/wiki/pull/1",
            }
        ]
        payload = dashboard.build_page_payload(registry, {}, archived, now=anchor)
        self.assertIn("tickets", payload)
        self.assertIn("workers", payload)
        self.assertIn("orchestrators", payload)
        self.assertIn("archived_today", payload)
        self.assertEqual(payload["generated_at"], anchor.isoformat())
        self.assertEqual({w["ticket"] for w in payload["workers"]}, {"WIKI-1"})
        self.assertEqual([o["orch"] for o in payload["orchestrators"]], ["wiki-dev"])
        self.assertEqual([a["ticket"] for a in payload["archived_today"]], ["WIKI-OLD"])


class DashboardPageRouterTests(unittest.TestCase):
    """WIKI-276: HTML page + JSON data endpoint mount cleanly."""

    def test_html_and_data_endpoints(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from backend.app import dashboard_page

        app = FastAPI()
        anchor = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)
        dashboard_page.register_payload_builder(
            lambda: dashboard.build_page_payload({}, {}, [], now=anchor)
        )
        app.include_router(dashboard_page.router)
        with TestClient(app) as client:
            page = client.get("/dashboard")
            self.assertEqual(page.status_code, 200)
            self.assertIn("Fleet Dashboard", page.text)
            self.assertEqual(page.headers["content-type"].split(";")[0], "text/html")

            data = client.get("/dashboard/data")
            self.assertEqual(data.status_code, 200)
            body = data.json()
            self.assertEqual(body["generated_at"], anchor.isoformat())
            self.assertEqual(body["workers"], [])
            self.assertEqual(body["tickets"], [])
            self.assertEqual(data.headers["cache-control"], "no-store")

    def test_data_endpoint_returns_empty_payload_without_builder(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from backend.app import dashboard_page

        # Force unregistered state: fresh registration with a builder that
        # errors would still return via the closure, so we explicitly
        # clear it here to prove the safe default.
        dashboard_page._payload_builder = None
        app = FastAPI()
        app.include_router(dashboard_page.router)
        with TestClient(app) as client:
            body = client.get("/dashboard/data").json()
            self.assertEqual(body["workers"], [])
            self.assertEqual(body["orchestrators"], [])


if __name__ == "__main__":
    unittest.main()
