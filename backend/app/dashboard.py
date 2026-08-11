"""Ticket/PR dashboard aggregation.

Read-only: merges live registry workers, archived runs, and cached GitHub PR
lookups into one row per BASE ticket (reviewer / sim / demo siblings and any
number of PRs collapse under their parent). gh calls happen on background
threads with a TTL + per-repository floor so refreshes never dogpile a single
repo and never block the page.
"""

from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote

from .github_pr import (
    _github_repo_from_url,
    _normalize_pr_url,
    _normalize_status_checks,
    _run_gh,
    _run_gh_json,
)

REPO_ALLOWLIST: tuple[str, ...] = tuple(
    part.strip()
    for part in (
        os.environ.get("WIKI_DASHBOARD_REPO_ALLOWLIST") or "phoebe-health/phoebe"
    ).split(",")
    if part.strip()
)
DEPLOY_ENVIRONMENT_BY_REPO: dict[str, str] = {
    "phoebe-health/phoebe": "phoebe-core / production",
}
CACHE_TTL_SECONDS = 300
REPO_MIN_INTERVAL_SECONDS = 60.0
GH_MAX_WORKERS = 3
ONE_SHOT_TICKET_TOKEN = re.compile(
    r"^(?:REVIEW|SIM|EVAL|AUDIT|CANARY|THERMO|DEMO|TEST|VERIFY)(?:[0-9]+[A-Z]*)?$",
    re.IGNORECASE,
)
BASE_TICKET_RE = re.compile(r"^([A-Z][A-Z0-9]*-\d+)")
REVIEW_ROUND_RE = re.compile(r"(?:^|-)REVIEW(\d+)", re.IGNORECASE)
REVIEW_ROUND_STEP_RE = re.compile(r"round\s+(\d+)", re.IGNORECASE)
STALE_STATUS_SECONDS = 30 * 60
REVIEW_GAP_SECONDS = 5 * 60

REVIEW_THREAD_COUNT_QUERY = """
query ReviewThreadCounts($url: URI!) {
  resource(url: $url) {
    ... on PullRequest {
      reviewThreads(first: 100) {
        totalCount
        nodes {
          isResolved
        }
      }
    }
  }
}
""".strip()


def _parse_when(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _summarize_checks(checks: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """(rollup, first failing check name); rollup None when no checks reported."""
    if not checks:
        return (None, None)
    failing = next((check for check in checks if check["state"] == "fail"), None)
    if failing:
        return ("fail", failing["name"])
    if any(check["state"] == "pending" for check in checks):
        return ("pending", None)
    return ("pass", None)


def _base_ticket(ticket: str) -> str:
    """Reduce a reviewer/sim/demo sibling to its parent ticket.

    ``WIKI-266-REVIEW3-correctness`` -> ``WIKI-266`` /
    ``PHO-13944-SIM2`` -> ``PHO-13944`` /
    ``CHIMY-42`` -> ``CHIMY-42``. Tickets that do not start with a
    ``PROJECT-NNN`` pair (``TEST-1``, ``REVIEW-10983``, orchestrator
    names, ...) return themselves.
    """
    match = BASE_TICKET_RE.match(ticket)
    return match.group(1) if match else ticket


def _has_project_prefix(ticket: str) -> bool:
    """True when the ticket follows the ``PROJECT-NNN`` (base) shape."""
    return BASE_TICKET_RE.match(ticket) is not None and ticket == _base_ticket(ticket)


def parse_review_round(ticket: str, step: Any) -> int | None:
    """Highest visible review round from a reviewer suffix or step text.

    Only unambiguous signals count: a ``-REVIEWn`` suffix on the ticket
    itself or an explicit ``round N`` phrase in the step. Bare numerals
    (``r3``, ``round-3``-hyphen-only, ...) are not accepted.
    """
    rounds: list[int] = []
    for match in REVIEW_ROUND_RE.finditer(ticket):
        rounds.append(int(match.group(1)))
    if isinstance(step, str) and step:
        for match in REVIEW_ROUND_STEP_RE.finditer(step):
            rounds.append(int(match.group(1)))
    return max(rounds) if rounds else None


def fetch_pr_summary(pr_url: str, repo: str) -> dict[str, Any]:
    """One gh round-trip bundle for a PR. Runs on a background thread.

    The `deployed` key is only present when the repo is deployment-tracked;
    absence signals "untracked, treat MERGED as terminal", None signals
    "tracked but not yet confirmed — keep refreshing".
    """
    view = _run_gh_json(
        [
            "pr",
            "view",
            pr_url,
            "--json",
            "title,state,updatedAt,mergedAt,mergeCommit,statusCheckRollup",
        ],
        timeout=30,
    )
    state = str(view.get("state") or "").upper()
    rollup, failing_check = _summarize_checks(
        _normalize_status_checks(view.get("statusCheckRollup"))
    )
    thread_total = 0
    thread_unresolved = 0
    if state == "OPEN":
        payload = _run_gh_json(
            ["api", "graphql", "-f", f"query={REVIEW_THREAD_COUNT_QUERY}", "-f", f"url={pr_url}"],
            timeout=30,
        )
        connection = (
            ((payload or {}).get("data") or {}).get("resource") or {}
        ).get("reviewThreads") or {}
        thread_total = int(connection.get("totalCount") or 0)
        thread_unresolved = sum(
            1
            for node in connection.get("nodes") or []
            if isinstance(node, dict) and not node.get("isResolved")
        )
    summary: dict[str, Any] = {
        "title": view.get("title"),
        "state": state,
        "updated_at": view.get("updatedAt"),
        "merged_at": view.get("mergedAt"),
        "checks": rollup,
        "failing_check": failing_check,
        "thread_total": thread_total,
        "thread_unresolved": thread_unresolved,
    }
    if state == "MERGED" and repo in DEPLOY_ENVIRONMENT_BY_REPO:
        merge_sha = (view.get("mergeCommit") or {}).get("oid")
        summary["deployed"] = (
            _merge_deployed(repo, merge_sha)
            if isinstance(merge_sha, str) and merge_sha
            else None
        )
    return summary


_deploy_sha_cache: dict[str, tuple[float, str | None]] = {}
_deploy_sha_inflight: dict[str, threading.Event] = {}
_deploy_sha_lock = threading.Lock()


def _prod_deploy_sha(repo: str) -> str | None:
    while True:
        with _deploy_sha_lock:
            cached = _deploy_sha_cache.get(repo)
            if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
                return cached[1]
            waiter = _deploy_sha_inflight.get(repo)
            if waiter is None:
                waiter = threading.Event()
                _deploy_sha_inflight[repo] = waiter
                owner = True
            else:
                owner = False
        if not owner:
            waiter.wait(timeout=60)
            continue
        try:
            sha = _fetch_prod_deploy_sha(repo)
        except BaseException:
            with _deploy_sha_lock:
                _deploy_sha_inflight.pop(repo, None)
            waiter.set()
            raise
        with _deploy_sha_lock:
            _deploy_sha_cache[repo] = (time.time(), sha)
            _deploy_sha_inflight.pop(repo, None)
        waiter.set()
        return sha


def _fetch_prod_deploy_sha(repo: str) -> str | None:
    environment = DEPLOY_ENVIRONMENT_BY_REPO[repo]
    deployments = _run_gh_json(
        ["api", f"repos/{repo}/deployments?environment={quote(environment, safe='')}&per_page=5"],
        timeout=30,
    )
    for deployment in deployments if isinstance(deployments, list) else []:
        if not isinstance(deployment, dict):
            continue
        statuses = _run_gh_json(
            ["api", f"repos/{repo}/deployments/{deployment.get('id')}/statuses?per_page=1"],
            timeout=30,
        )
        latest = statuses[0] if isinstance(statuses, list) and statuses else {}
        if isinstance(latest, dict) and latest.get("state") == "success":
            candidate = deployment.get("sha")
            if isinstance(candidate, str) and candidate:
                return candidate
    return None


def _merge_deployed(repo: str, merge_sha: str) -> bool | None:
    prod_sha = _prod_deploy_sha(repo)
    if not prod_sha:
        return None
    status = _run_gh(
        ["api", f"repos/{repo}/compare/{prod_sha}...{merge_sha}", "--jq", ".status"],
        timeout=30,
    ).strip()
    return status in {"behind", "identical"}


class PrCache:
    """TTL cache over fetch_pr_summary with per-repository pacing.

    Two guarantees the reviewer asked for (Q6):

    * ``request_refresh`` never enqueues a fetch for a repository within
      ``REPO_MIN_INTERVAL_SECONDS`` of the previous fetch for that same
      repo — a burst of task rows on the same repo pays one round-trip,
      not one per PR.
    * A fetch that raises records ``failure_at`` on the cache entry and
      leaves the previous payload untouched; ``lookup`` surfaces the
      failure alongside the last known-good timestamp so the page can
      render a stale marker instead of quietly showing pre-outage data.
    """

    def __init__(self, fetch: Callable[[str, str], dict[str, Any]] = fetch_pr_summary) -> None:
        self._fetch = fetch
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._inflight: set[str] = set()
        self._repo_last_call_at: dict[str, float] = {}
        self._executor: ThreadPoolExecutor | None = None

    def lookup(self, pr_url: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._entries.get(pr_url)
            return entry["data"] if entry else None

    def cache_entry(self, pr_url: str) -> dict[str, Any] | None:
        """Full entry so callers can read staleness/last-success timestamps."""
        with self._lock:
            entry = self._entries.get(pr_url)
            if not entry:
                return None
            return {
                "data": entry.get("data"),
                "checked_at": entry.get("checked_at"),
                "last_success_at": entry.get("last_success_at"),
                "failure_at": entry.get("failure_at"),
            }

    def request_refresh(self, pr_url: str, repo: str) -> None:
        with self._lock:
            entry = self._entries.get(pr_url)
            if entry:
                if time.time() - entry["checked_at"] < CACHE_TTL_SECONDS:
                    return
                data = entry.get("data")
                if data and _is_terminal(data):
                    return
            if pr_url in self._inflight:
                return
            last_repo_call = self._repo_last_call_at.get(repo)
            if (
                last_repo_call is not None
                and time.time() - last_repo_call < REPO_MIN_INTERVAL_SECONDS
            ):
                return
            self._inflight.add(pr_url)
            self._repo_last_call_at[repo] = time.time()
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=GH_MAX_WORKERS,
                    thread_name_prefix="dashboard-gh",
                )
            executor = self._executor
        executor.submit(self._refresh, pr_url, repo)

    def _refresh(self, pr_url: str, repo: str) -> None:
        data: dict[str, Any] | None = None
        failed = False
        try:
            data = self._fetch(pr_url, repo)
        except Exception:
            failed = True
        with self._lock:
            previous = self._entries.get(pr_url) or {}
            now_ts = time.time()
            entry: dict[str, Any] = {
                "checked_at": now_ts,
                "data": previous.get("data") if failed or data is None else data,
                "last_success_at": (
                    now_ts if not failed and data is not None else previous.get("last_success_at")
                ),
                "failure_at": now_ts if failed else None,
            }
            self._entries[pr_url] = entry
            self._inflight.discard(pr_url)


def _is_terminal(data: dict[str, Any]) -> bool:
    state = data.get("state")
    if state == "CLOSED":
        return True
    if state != "MERGED":
        return False
    # Absence of `deployed` = repo not deployment-tracked → merged is terminal.
    # None = tracked but unknown → keep refreshing.
    if "deployed" not in data:
        return True
    return data["deployed"] is True


PR_CACHE = PrCache()


def _is_dashboard_worker(ticket: str, role: Any) -> bool:
    """Return whether a registry/archive entry belongs on the ticket dashboard.

    Standalone one-shot tickets (``TEST-1``, ``DEMO-2``, ``REVIEW-10983``,
    ...) have no parent to fold into and stay hidden. Sibling one-shots
    (``WIKI-266-REVIEW3``) are kept — the aggregation groups them under
    their parent base ticket via :func:`_base_ticket`.
    """
    if role == "orchestrator":
        return False
    base = _base_ticket(ticket)
    if base == ticket:
        # No parent — this is either a normal ticket (PROJECT-NNN) or a
        # standalone one-shot. Only the standalone one-shots are dropped.
        if any(ONE_SHOT_TICKET_TOKEN.fullmatch(token) for token in ticket.split("-")):
            return False
    if role == "implement":
        return True
    if role in (None, ""):
        return True
    # Reviewers, planners, verify workers, ... — surface them so the
    # ticket pane can count them as siblings.
    return True


def _fold_ticket(ticket: str) -> str:
    """Alias for the primary aggregation key used by ticket rows."""
    return _base_ticket(ticket)


def live_worker_rows(
    registry: dict[str, Any],
    statuses: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """One row per live registered non-orchestrator worker.

    Reviewer siblings are included — the ticket aggregation folds them by
    base ticket. Status files older than ``spawned_at`` are ignored so a
    restarted session cannot inherit the previous session's PR/state.
    """
    rows: list[dict[str, Any]] = []
    for ticket, entry in sorted(registry.items()):
        if ticket.startswith("_") or not isinstance(entry, dict):
            continue
        current = entry.get("current")
        if not isinstance(current, dict):
            continue
        role = current.get("role")
        if role == "orchestrator":
            continue
        if not _is_dashboard_worker(ticket, role):
            continue
        status = _status_for_current(statuses.get(ticket), current)
        rows.append(
            {
                "ticket": ticket,
                "base_ticket": _base_ticket(ticket),
                "live": True,
                "role": role,
                "kind": current.get("kind"),
                "state": status.get("state") or current.get("state"),
                "runtime_state": current.get("state"),
                "step": status.get("step"),
                "blocker": status.get("blocker"),
                "pr": _normalize_pr_url(status.get("pr") or current.get("pr")),
                "outcome": None,
                "updated_at": _status_mtime_iso(status)
                or current.get("updated_at")
                or current.get("spawned_at"),
                "mtime": status.get("_mtime") if isinstance(status.get("_mtime"), (int, float)) else None,
            }
        )
    return rows


def _status_for_current(
    status: dict[str, Any] | None, current: dict[str, Any]
) -> dict[str, Any]:
    """Return `status` only if its mtime is at/after `current.spawned_at`."""
    if not isinstance(status, dict):
        return {}
    mtime = status.get("_mtime")
    spawned = _parse_when(current.get("spawned_at"))
    if isinstance(mtime, (int, float)) and spawned is not None:
        if mtime < spawned.timestamp():
            return {}
    return status


def _status_mtime_iso(status: dict[str, Any]) -> str | None:
    mtime = status.get("_mtime")
    if not isinstance(mtime, (int, float)):
        return None
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


def archived_rows(archived: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per sibling archive session (newest per ticket, sorted input).

    Reviewer/sim/demo siblings are kept — they still fold under their
    base ticket in :func:`merge_rows` so a ticket row can display its
    full PR history.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in archived:
        ticket = entry.get("ticket")
        if not isinstance(ticket, str) or ticket in seen:
            continue
        seen.add(ticket)
        role = entry.get("role")
        if not _is_dashboard_worker(ticket, role):
            continue
        rows.append(
            {
                "ticket": ticket,
                "base_ticket": _base_ticket(ticket),
                "live": False,
                "role": role,
                "kind": entry.get("kind"),
                "state": entry.get("state"),
                "runtime_state": None,
                "step": entry.get("step"),
                "blocker": None,
                "pr": _normalize_pr_url(entry.get("pr")),
                "outcome": entry.get("outcome"),
                "updated_at": entry.get("archived_at"),
                "mtime": None,
            }
        )
    return rows


def merge_rows(
    live: list[dict[str, Any]],
    archived: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Combine live + archived rows, preferring live per exact ticket.

    Deliberately does NOT collapse siblings — grouping by base ticket
    happens in :func:`build_payload` so we still know which sibling
    contributed each PR.
    """
    by_ticket: dict[str, dict[str, Any]] = {row["ticket"]: row for row in archived}
    for row in live:
        by_ticket[row["ticket"]] = row
    return list(by_ticket.values())


def derive_status(
    row: dict[str, Any],
    enrich: dict[str, Any] | None,
) -> tuple[str, str | None]:
    """(status, detail) for one row; enrich is the cached gh payload if any."""
    if enrich:
        state = enrich.get("state")
        if state == "MERGED":
            if enrich.get("deployed") is True:
                return ("prod", None)
            return ("merged", None)
        if state == "CLOSED":
            return ("closed", None)
        if state == "OPEN":
            if enrich.get("checks") == "fail":
                return ("failing", enrich.get("failing_check"))
            # Q8: only unresolved threads matter — resolved ones are not
            # "comments waiting on you", they're history.
            unresolved = enrich.get("thread_unresolved") or 0
            if unresolved > 0:
                total = enrich.get("thread_total") or 0
                return ("has-comments", f"{unresolved}/{total} threads unresolved")
            if enrich.get("checks") == "pending":
                return ("checks-pending", None)
            return ("passing", None)
    outcome = row.get("outcome")
    if outcome == "merged":
        return ("merged", None) if row.get("pr") else ("merged (local)", None)
    if outcome in {"closed", "abandoned"}:
        return (outcome, None)
    if row.get("pr"):
        state = row.get("state")
        if row.get("live") and state in {"blocked", "merge-ready"}:
            return (state, row.get("blocker") if state == "blocked" else row.get("step"))
        return ("pr-open", row.get("step"))
    if row.get("live"):
        state = row.get("state")
        if state == "blocked":
            return ("blocked", row.get("blocker"))
        if state == "merge-ready":
            return ("merge-ready", row.get("step"))
        return ("implementing", row.get("step"))
    if row.get("state"):
        return (str(row["state"]), row.get("step"))
    return ("unknown", None)


def _pr_enrichment(row: dict[str, Any], cache: Any) -> dict[str, Any] | None:
    """Bundle a PR's cache data + staleness metadata + repo for the payload.

    Tolerates cache stubs (tests) that only implement the classic
    ``lookup`` / ``request_refresh`` pair — falling back to a synthetic
    entry so unit tests do not have to know about the failure marker.
    """
    pr_url = row.get("pr")
    if not pr_url:
        return None
    repo = _github_repo_from_url(pr_url)
    if repo not in REPO_ALLOWLIST or not repo:
        return None
    entry_fn = getattr(cache, "cache_entry", None)
    entry = entry_fn(pr_url) if callable(entry_fn) else None
    cache.request_refresh(pr_url, repo)
    if entry is None:
        data = cache.lookup(pr_url)
        entry = {"data": data, "checked_at": None, "last_success_at": None, "failure_at": None}
    return {**entry, "repo": repo}


def _next_action_hint(
    row: dict[str, Any],
    enrich_data: dict[str, Any] | None,
    live_workers: list[dict[str, Any]],
    *,
    now_ts: float,
) -> str | None:
    """Conservative next-action hint. Return None when the signal is ambiguous.

    A wrong hint is worse than none, so every branch keys on a signal we
    already surface elsewhere (alarm derivations, enrich rollup, live
    worker state). Priority order runs from "someone is actively about
    to fix this" downwards.
    """
    live_impl = [w for w in live_workers if (w.get("role") or "implement") == "implement"]
    live_review = [w for w in live_workers if w.get("role") == "review"]
    live_states = {w.get("state") for w in live_impl}
    if any(w.get("blocker") for w in live_impl):
        return "blocked — see worker"
    if "blocked" in live_states:
        return "blocked — see worker"
    if any((w.get("runtime_state") == "waiting-approval") for w in live_impl + live_review):
        return "waiting approval"
    if live_review:
        for reviewer in live_review:
            step = reviewer.get("step") or ""
            if "MERGE-READY" in step or "NOT-MERGE-READY" in step:
                return "unrouted verdict — route it"
        return "review running"
    if enrich_data:
        if enrich_data.get("checks") == "fail":
            return "CI red"
        if (enrich_data.get("thread_unresolved") or 0) > 0:
            return "unresolved PR threads"
        if enrich_data.get("state") == "OPEN" and "merge-ready" in live_states:
            return "ready to merge"
    if "merge-ready" in live_states:
        # Q3 review-gap: >5m merge-ready implementer with no live reviewer.
        for impl in live_impl:
            if impl.get("state") != "merge-ready":
                continue
            mtime = impl.get("mtime")
            if isinstance(mtime, (int, float)) and now_ts - mtime > REVIEW_GAP_SECONDS:
                return "awaiting Henry merge word"
        return "awaiting Henry merge word"
    return None


def _fmt_iso(when: datetime | None) -> str | None:
    return when.astimezone(timezone.utc).isoformat() if when else None


def _pr_entry_payload(
    row: dict[str, Any],
    enrich: dict[str, Any] | None,
) -> dict[str, Any]:
    data = (enrich or {}).get("data") or None
    status, detail = derive_status(row, data)
    dates = [
        _parse_when(row.get("updated_at")),
        _parse_when((data or {}).get("updated_at")),
    ]
    latest = max((when for when in dates if when), default=None)
    return {
        "sibling_ticket": row["ticket"],
        "pr": row.get("pr"),
        "repo": (enrich or {}).get("repo"),
        "status": status,
        "detail": detail,
        "enriched": data is not None,
        "date": _fmt_iso(latest),
        "stale": bool(enrich and enrich.get("failure_at")),
        "last_success_at": (
            datetime.fromtimestamp(enrich["last_success_at"], tz=timezone.utc).isoformat()
            if enrich and isinstance(enrich.get("last_success_at"), (int, float))
            else None
        ),
    }


def build_payload(
    registry: dict[str, Any],
    statuses: dict[str, dict[str, Any]],
    archived: list[dict[str, Any]],
    *,
    cache: PrCache | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if cache is None:
        cache = PR_CACHE
    now_ts = (now or datetime.now(timezone.utc)).timestamp()
    live = live_worker_rows(registry, statuses)
    rows = merge_rows(live, archived_rows(archived))

    # Group siblings by base ticket. Sort each group's rows by date desc
    # so the newest sibling drives the primary payload (title, status).
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row.get("base_ticket") or row["ticket"], []).append(row)

    tickets: list[dict[str, Any]] = []
    for base_ticket, siblings in groups.items():
        enrichments = {row["ticket"]: _pr_enrichment(row, cache) for row in siblings}
        siblings.sort(
            key=lambda r: (
                (_pr_entry_payload(r, enrichments.get(r["ticket"])).get("date") or ""),
                1 if r.get("live") else 0,
            ),
            reverse=True,
        )
        pr_entries = [
            _pr_entry_payload(r, enrichments.get(r["ticket"]))
            for r in siblings
            if r.get("pr")
        ]
        # De-duplicate PR entries (a sibling live + archived for the same PR).
        seen_prs: set[str] = set()
        deduped_prs: list[dict[str, Any]] = []
        for entry in pr_entries:
            key = entry["pr"] or ""
            if key in seen_prs:
                continue
            seen_prs.add(key)
            deduped_prs.append(entry)
        primary_row = siblings[0]
        primary_enrich = enrichments.get(primary_row["ticket"])
        primary_data = (primary_enrich or {}).get("data")
        primary_status, primary_detail = derive_status(primary_row, primary_data)
        latest_date = max(
            (
                _parse_when(entry.get("date"))
                for entry in [_pr_entry_payload(primary_row, primary_enrich)] + deduped_prs
                if entry.get("date")
            ),
            default=_parse_when(primary_row.get("updated_at")),
        )
        live_workers = [row for row in siblings if row.get("live")]
        # For hint derivation we only care about live workers under this
        # base — that's the fleet-monitor semantic used for review-gap.
        review_rounds = [
            r for r in (parse_review_round(row["ticket"], row.get("step")) for row in siblings) if r
        ]
        review_round = max(review_rounds) if review_rounds else None
        next_action = _next_action_hint(primary_row, primary_data, live_workers, now_ts=now_ts)
        description = (primary_data or {}).get("title") or primary_row.get("step") or ""
        tickets.append(
            {
                "ticket": base_ticket,
                "description": description,
                "pr": primary_row.get("pr") if primary_row.get("pr") else (deduped_prs[0]["pr"] if deduped_prs else None),
                "repo": (primary_enrich or {}).get("repo"),
                "enriched": primary_data is not None,
                "status": primary_status,
                "detail": primary_detail,
                "date": _fmt_iso(latest_date),
                "live": bool(primary_row.get("live")),
                "role": primary_row.get("role"),
                "kind": primary_row.get("kind"),
                "prs": deduped_prs,
                "workers_live": [
                    {
                        "ticket": row["ticket"],
                        "role": row.get("role"),
                        "kind": row.get("kind"),
                        "state": row.get("state"),
                        "step": row.get("step"),
                    }
                    for row in live_workers
                ],
                "review_round": review_round,
                "next_action": next_action,
            }
        )
    tickets.sort(key=lambda item: item["date"] or "", reverse=True)
    return {"tickets": tickets, "repo_allowlist": list(REPO_ALLOWLIST)}


# --- Fleet + orchestrator aggregation (WIKI-276) --------------------------
#
# The task-first `build_payload` above collapses siblings into one row per
# base ticket. The fleet view is the opposite: it lists every worker the
# supervisor is running right now, grouped by orchestrator, with all four
# fleet-monitor alarm derivations translated for the browser.


def _worker_alarms(
    role: Any,
    state: Any,
    runtime_state: Any,
    mtime: float | None,
    blocker: Any,
    step: Any,
    *,
    has_live_reviewer_sibling: bool,
    is_merge_ready_gap: bool,
    now: float | None = None,
) -> list[str]:
    """Alarm chips derived from what the status file and registry expose.

    Mirrors the fleet-monitor semantics:

    * ``waiting-approval`` fires when the registered runtime state is
      ``waiting-approval`` and *overrides* the displayed state — even if
      the status file still reads ``working``.
    * ``stale`` fires on a working-ish worker whose status has not been
      touched in :data:`STALE_STATUS_SECONDS`.
    * ``unrouted-verdict`` fires on a live reviewer whose step text
      carries an unrouted ``MERGE-READY`` / ``NOT-MERGE-READY`` verdict.
    * ``review-gap`` fires on a live implementer that has been sitting on
      ``merge-ready`` for more than :data:`REVIEW_GAP_SECONDS` with no
      live reviewer sibling.
    """
    now_ts = time.time() if now is None else now
    alarms: list[str] = []
    if runtime_state == "waiting-approval":
        alarms.append("waiting-approval")
    if state == "blocked":
        alarms.append("blocked")
    elif state == "merge-ready":
        alarms.append("merge-ready")
    working_ish = state not in {"blocked", "merge-ready", "closed", "completed", "dead"}
    if (
        working_ish
        and isinstance(mtime, (int, float))
        and (now_ts - mtime) > STALE_STATUS_SECONDS
    ):
        alarms.append("stale")
    if role == "review":
        text = step or ""
        if isinstance(text, str) and (
            "MERGE-READY" in text or "NOT-MERGE-READY" in text
        ):
            alarms.append("unrouted-verdict")
    if (
        role in (None, "", "implement")
        and state == "merge-ready"
        and not has_live_reviewer_sibling
        and is_merge_ready_gap
    ):
        alarms.append("review-gap")
    if isinstance(blocker, str) and blocker.strip() and "blocked" not in alarms:
        alarms.append("attention")
    return alarms


def _display_state(state: Any, runtime_state: Any) -> str | None:
    """Effective displayed state — waiting-approval overrides working."""
    if runtime_state == "waiting-approval":
        return "waiting-approval"
    if state:
        return str(state)
    if runtime_state:
        return str(runtime_state)
    return None


def _state_bucket(display_state: str | None, alarms: list[str]) -> str:
    """Honest bucket for the rollup counts (Q5)."""
    if display_state == "blocked":
        return "blocked"
    if display_state == "merge-ready":
        return "merge_ready"
    if display_state == "idle":
        return "idle"
    if display_state in {"working", "waiting-approval"}:
        return "working"
    if "stale" in alarms:
        return "stalled_or_failed"
    # dead / completed / interrupted / starting / failed / unknown / None
    return "stalled_or_failed"


def fleet_workers(
    registry: dict[str, Any],
    statuses: dict[str, dict[str, Any]],
    *,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """One row per live registered non-orchestrator worker with alarms.

    Two-pass: the first pass reads registry+status into raw rows; the
    second computes alarms with sibling context (needed for
    ``review-gap`` which requires knowing whether a live reviewer exists
    under the same base ticket).
    """
    now_ts = time.time() if now is None else now
    raw: list[dict[str, Any]] = []
    for ticket, entry in sorted(registry.items()):
        if ticket.startswith("_") or not isinstance(entry, dict):
            continue
        current = entry.get("current")
        if not isinstance(current, dict):
            continue
        role = current.get("role")
        if role == "orchestrator":
            continue
        status = _status_for_current(statuses.get(ticket), current)
        mtime = status.get("_mtime")
        mtime_val = mtime if isinstance(mtime, (int, float)) else None
        raw_state = status.get("state") or current.get("state")
        runtime_state = current.get("state")
        step = status.get("step")
        blocker = status.get("blocker")
        pr = _normalize_pr_url(status.get("pr") or current.get("pr"))
        raw.append(
            {
                "ticket": ticket,
                "base_ticket": _base_ticket(ticket),
                "orch": current.get("orch"),
                "role": role,
                "kind": current.get("kind"),
                "model": current.get("model"),
                "raw_state": raw_state,
                "runtime_state": runtime_state,
                "step": step,
                "blocker": blocker,
                "pr": pr,
                "run_id": current.get("run_id"),
                "worktree": current.get("worktree"),
                "spawned_at": current.get("spawned_at"),
                "updated_at": _status_mtime_iso(status) or current.get("updated_at"),
                "status_age_s": (now_ts - mtime_val) if mtime_val else None,
                "mtime": mtime_val,
            }
        )
    reviewers_by_base: dict[str, list[dict[str, Any]]] = {}
    for row in raw:
        if row["role"] == "review":
            reviewers_by_base.setdefault(row["base_ticket"], []).append(row)
    rows: list[dict[str, Any]] = []
    for row in raw:
        display_state = _display_state(row["raw_state"], row["runtime_state"])
        base = row["base_ticket"]
        has_reviewer = any(
            r["ticket"] != row["ticket"] for r in reviewers_by_base.get(base, [])
        )
        merge_ready_gap = (
            isinstance(row["mtime"], (int, float))
            and (now_ts - row["mtime"]) > REVIEW_GAP_SECONDS
        )
        alarms = _worker_alarms(
            row["role"],
            display_state,
            row["runtime_state"],
            row["mtime"],
            row["blocker"],
            row["step"],
            has_live_reviewer_sibling=has_reviewer,
            is_merge_ready_gap=merge_ready_gap,
            now=now_ts,
        )
        rows.append(
            {
                "ticket": row["ticket"],
                "base_ticket": base,
                "orch": row["orch"],
                "role": row["role"],
                "kind": row["kind"],
                "model": row["model"],
                "state": display_state,
                "raw_state": row["raw_state"],
                "runtime_state": row["runtime_state"],
                "step": row["step"],
                "blocker": row["blocker"],
                "pr": row["pr"],
                "run_id": row["run_id"],
                "worktree": row["worktree"],
                "spawned_at": row["spawned_at"],
                "updated_at": row["updated_at"],
                "status_age_s": row["status_age_s"],
                "alarms": alarms,
                "bucket": _state_bucket(display_state, alarms),
                "review_round": parse_review_round(row["ticket"], row["step"]),
            }
        )
    return rows


def _orchestrator_ids(registry: dict[str, Any]) -> list[str]:
    """Every orchestrator we know about — declared or discovered.

    Q4: rollups must render zero-worker orchestrators too, so we start
    from the registry's declared orchestrators (both the
    ``_orchestrators`` book and any current record with
    ``role == 'orchestrator'``).
    """
    ids: set[str] = set()
    orch_book = registry.get("_orchestrators")
    if isinstance(orch_book, dict):
        for name in orch_book.keys():
            if isinstance(name, str):
                ids.add(name)
    for ticket, entry in registry.items():
        if not isinstance(entry, dict) or ticket.startswith("_"):
            continue
        current = entry.get("current")
        if isinstance(current, dict) and current.get("role") == "orchestrator":
            ids.add(ticket)
    return sorted(ids)


def orch_rollups(
    workers: list[dict[str, Any]],
    *,
    known_orchestrators: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Per-orchestrator counts glanceable in the header strip.

    Includes every orchestrator in ``known_orchestrators`` even if it
    has zero workers (Q4). Buckets follow :func:`_state_bucket` (Q5) so
    ``failed`` / ``dead`` / unknown never inflate ``working``.
    """
    buckets: dict[str, dict[str, Any]] = {}

    def _empty(orch: str) -> dict[str, Any]:
        return {
            "orch": orch,
            "total": 0,
            "working": 0,
            "idle": 0,
            "merge_ready": 0,
            "blocked": 0,
            "stalled_or_failed": 0,
            "waiting_approval": 0,
        }

    for orch in known_orchestrators or []:
        buckets[orch] = _empty(orch)
    for worker in workers:
        orch = worker.get("orch") or "(unassigned)"
        bucket = buckets.setdefault(orch, _empty(orch))
        bucket["total"] += 1
        target = worker.get("bucket") or _state_bucket(worker.get("state"), worker.get("alarms") or [])
        bucket[target] = bucket.get(target, 0) + 1
        if "waiting-approval" in (worker.get("alarms") or []):
            bucket["waiting_approval"] += 1
    return sorted(buckets.values(), key=lambda entry: entry["orch"])


def archived_today(
    archived: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Archive rows whose ``archived_at`` falls on the current UTC day."""
    reference = now if now is not None else datetime.now(timezone.utc)
    day_start = datetime.combine(
        reference.astimezone(timezone.utc).date(),
        datetime.min.time(),
        tzinfo=timezone.utc,
    )
    rows: list[dict[str, Any]] = []
    for entry in archived:
        parsed = _parse_when(entry.get("archived_at"))
        if parsed is None or parsed < day_start:
            continue
        rows.append(
            {
                "ticket": entry.get("ticket"),
                "role": entry.get("role"),
                "kind": entry.get("kind"),
                "orch": entry.get("orch"),
                "outcome": entry.get("outcome"),
                "state": entry.get("state"),
                "pr": _normalize_pr_url(entry.get("pr")),
                "step": entry.get("step"),
                "archived_at": entry.get("archived_at"),
            }
        )
    rows.sort(key=lambda item: item.get("archived_at") or "", reverse=True)
    return rows


def build_page_payload(
    registry: dict[str, Any],
    statuses: dict[str, dict[str, Any]],
    archived: list[dict[str, Any]],
    *,
    cache: PrCache | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Full JSON payload consumed by the browser dashboard page."""
    reference = now if now is not None else datetime.now(timezone.utc)
    now_ts = reference.timestamp()
    tickets_payload = build_payload(registry, statuses, archived, cache=cache, now=reference)
    workers = fleet_workers(registry, statuses, now=now_ts)
    return {
        "tickets": tickets_payload["tickets"],
        "repo_allowlist": tickets_payload["repo_allowlist"],
        "workers": workers,
        "orchestrators": orch_rollups(
            workers, known_orchestrators=_orchestrator_ids(registry)
        ),
        "archived_today": archived_today(archived, now=reference),
        "generated_at": reference.astimezone(timezone.utc).isoformat(),
    }
