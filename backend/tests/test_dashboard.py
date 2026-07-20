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
            "WIKI-SIM1": {"current": {"kind": "cc"}},
        }

        rows = dashboard.live_worker_rows(registry, {})

        self.assertEqual(
            {row["ticket"] for row in rows},
            {"WIKI-IMPLEMENT", "WIKI-LEGACY"},
        )

    def test_missing_role_fallback_is_anchored_and_covers_one_shot_suffixes(self) -> None:
        included = [
            "WIKI-REVIEWING",
            "WIKI-SIM",
            "WIKI-123-ORDINARY",
            "WIKI-TEST",
        ]
        excluded = [
            "WIKI-REVIEW",
            "WIKI-REVIEW1",
            "WIKI-SIM1",
            "WIKI-EVAL2",
            "WIKI-AUDIT3",
            "WIKI-CANARY4",
            "WIKI-THERMO5",
            "WIKI-DEMO6",
            "WIKI-TEST7",
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
        payload = main.dashboard_tickets()
        by_ticket = {row["ticket"]: row for row in payload["tickets"]}
        self.assertEqual(set(by_ticket), {"WIKI-50", "GAU-1"})
        self.assertEqual(by_ticket["WIKI-50"]["status"], "implementing")
        self.assertEqual(by_ticket["WIKI-50"]["description"], "editing")
        self.assertTrue(by_ticket["WIKI-50"]["live"])
        self.assertEqual(by_ticket["GAU-1"]["status"], "merged (local)")
        self.assertFalse(by_ticket["GAU-1"]["live"])
        self.assertEqual(payload["tickets"][0]["ticket"], "WIKI-50")

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
        older_ts = base - timedelta(days=30)
        older = self.archive / "PHO-OLD" / older_ts.strftime("%Y%m%d-%H%M%S")
        older.mkdir(parents=True)
        (older / "meta.json").write_text(
            json.dumps({"worker": {"kind": "cc", "role": "implement"}, "outcome": "merged"}),
            encoding="utf-8",
        )
        payload = main.dashboard_tickets()
        tickets = {row["ticket"] for row in payload["tickets"]}
        self.assertIn("PHO-CROWD", tickets)
        self.assertIn(
            "PHO-OLD",
            tickets,
            "older ticket must survive dedup-before-limit — otherwise pre-dedup 500-cap regressed",
        )


if __name__ == "__main__":
    unittest.main()
