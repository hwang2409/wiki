"""Ticket/PR dashboard aggregation.

Read-only: merges live registry workers, archived runs, and cached GitHub PR
lookups into one row per ticket. gh calls happen on background threads with a
TTL cache — building the payload never blocks on the network.
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
GH_MAX_WORKERS = 3
ONE_SHOT_TICKET_TOKEN = re.compile(
    r"^(?:REVIEW|SIM|EVAL|AUDIT|CANARY|THERMO|DEMO|TEST|VERIFY)[0-9]*[A-Z]*$",
    re.IGNORECASE,
)

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
    """TTL cache over fetch_pr_summary; refreshes on background threads.

    Lookups always return whatever is cached (stale included) — a fetch
    failure keeps the previous payload and just re-arms the TTL.
    """

    def __init__(self, fetch: Callable[[str, str], dict[str, Any]] = fetch_pr_summary) -> None:
        self._fetch = fetch
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._inflight: set[str] = set()
        self._executor: ThreadPoolExecutor | None = None

    def lookup(self, pr_url: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._entries.get(pr_url)
            return entry["data"] if entry else None

    def request_refresh(self, pr_url: str, repo: str) -> None:
        with self._lock:
            entry = self._entries.get(pr_url)
            if entry:
                if time.time() - entry["checked_at"] < CACHE_TTL_SECONDS:
                    return
                data = entry["data"]
                if data and _is_terminal(data):
                    return
            if pr_url in self._inflight:
                return
            self._inflight.add(pr_url)
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=GH_MAX_WORKERS,
                    thread_name_prefix="dashboard-gh",
                )
            executor = self._executor
        executor.submit(self._refresh, pr_url, repo)

    def _refresh(self, pr_url: str, repo: str) -> None:
        data: dict[str, Any] | None = None
        try:
            data = self._fetch(pr_url, repo)
        except Exception:
            pass
        with self._lock:
            previous = self._entries.get(pr_url)
            self._entries[pr_url] = {
                "checked_at": time.time(),
                "data": data if data is not None else (previous or {}).get("data"),
            }
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

    Role is the primary signal, but one-shot ticket names override it because
    archived one-shot workers can be recorded with role=implement. Older
    archive records may not have a role; retain those unless their ticket uses
    a known one-shot worker name.
    """
    if any(ONE_SHOT_TICKET_TOKEN.fullmatch(token) for token in ticket.split("-")):
        return False
    if role == "implement":
        return True
    if role not in (None, ""):
        return False
    return True


def live_worker_rows(
    registry: dict[str, Any],
    statuses: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """One row per live registered non-orchestrator ticket.

    Status files are correlated with the registry entry's `spawned_at` — an
    older status file (from a previous session that outlived its worker)
    is ignored so a restarted ticket doesn't inherit the previous session's
    PR/state. Status files with no matching registry entry are never
    surfaced as live; they belong to the archive path.
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
                "live": True,
                "role": role,
                "kind": current.get("kind"),
                "state": status.get("state") or current.get("state"),
                "step": status.get("step"),
                "blocker": status.get("blocker"),
                "pr": _normalize_pr_url(status.get("pr") or current.get("pr")),
                "outcome": None,
                "updated_at": _status_mtime_iso(status)
                or current.get("updated_at")
                or current.get("spawned_at"),
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
    """Newest archive session per ticket (input sorted archived_at desc)."""
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
                "live": False,
                "role": role,
                "kind": entry.get("kind"),
                "state": entry.get("state"),
                "step": entry.get("step"),
                "blocker": None,
                "pr": _normalize_pr_url(entry.get("pr")),
                "outcome": entry.get("outcome"),
                "updated_at": entry.get("archived_at"),
            }
        )
    return rows


def merge_rows(
    live: list[dict[str, Any]],
    archived: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Live wins per ticket; a live row without a PR inherits nothing else."""
    by_ticket = {row["ticket"]: row for row in archived}
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
            if (enrich.get("thread_total") or 0) > 0:
                unresolved = enrich.get("thread_unresolved") or 0
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
        # PR known but not enriched (repo outside allowlist, or gh not yet
        # fetched): the PR's existence is still the strongest signal.
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


def build_payload(
    registry: dict[str, Any],
    statuses: dict[str, dict[str, Any]],
    archived: list[dict[str, Any]],
    *,
    cache: PrCache | None = None,
) -> dict[str, Any]:
    if cache is None:
        cache = PR_CACHE
    rows = merge_rows(live_worker_rows(registry, statuses), archived_rows(archived))
    tickets: list[dict[str, Any]] = []
    for row in rows:
        pr_url = row.get("pr")
        repo = _github_repo_from_url(pr_url) if pr_url else None
        allowlisted = repo in REPO_ALLOWLIST if repo else False
        enrich = None
        if pr_url and allowlisted and repo:
            enrich = cache.lookup(pr_url)
            cache.request_refresh(pr_url, repo)
        status, detail = derive_status(row, enrich)
        dates = [
            _parse_when(row.get("updated_at")),
            _parse_when((enrich or {}).get("updated_at")),
        ]
        latest = max((when for when in dates if when), default=None)
        description = (enrich or {}).get("title") or row.get("step") or ""
        tickets.append(
            {
                "ticket": row["ticket"],
                "description": description,
                "pr": pr_url,
                "repo": repo,
                "enriched": enrich is not None,
                "status": status,
                "detail": detail,
                "date": latest.astimezone(timezone.utc).isoformat() if latest else None,
                "live": bool(row.get("live")),
                "role": row.get("role"),
                "kind": row.get("kind"),
            }
        )
    tickets.sort(key=lambda item: item["date"] or "", reverse=True)
    return {"tickets": tickets, "repo_allowlist": list(REPO_ALLOWLIST)}
