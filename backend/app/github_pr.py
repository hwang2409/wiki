from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException


AGENT_REGISTRY_PATH = Path("/tmp/agent-registry.json")
AGENT_STATUS_DIR = Path("/tmp/agent-status")
PR_CACHE_TTL_SECONDS = 20
PR_URL_HOSTS = {"github.com", "www.github.com"}
REVIEW_THREADS_QUERY = """
query ReviewThreads($url: URI!, $endCursor: String) {
  resource(url: $url) {
    ... on PullRequest {
      reviewThreads(first: 100, after: $endCursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          isResolved
          path
          comments(last: 1) {
            nodes {
              body
              updatedAt
              url
              author {
                login
              }
            }
          }
        }
      }
    }
  }
}
""".strip()

_pr_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _normalize_pr_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme != "https" or parsed.netloc.lower() not in PR_URL_HOSTS:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 4 or parts[2] != "pull" or not parts[3].isdigit():
        return None
    return f"https://github.com/{parts[0]}/{parts[1]}/pull/{parts[3]}"


def resolve_pr_url(ticket: str) -> str | None:
    registry = _read_json(AGENT_REGISTRY_PATH) or {}
    current = (registry.get(ticket) or {}).get("current") or {}
    status = _read_json(AGENT_STATUS_DIR / f"{ticket}.json") or {}
    candidates = [
        status.get("pr"),
        current.get("pr"),
        (registry.get(ticket) or {}).get("pr"),
    ]
    for candidate in candidates:
        url = _normalize_pr_url(candidate)
        if url:
            return url
    return None


def _run_gh(args: list[str], *, timeout: int = 30) -> str:
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=502, detail=f"gh failed: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or "unknown gh error"
        raise HTTPException(status_code=502, detail=f"gh failed: {stderr[:300]}")
    return result.stdout


def _run_gh_json(args: list[str], *, timeout: int = 30) -> Any:
    try:
        return json.loads(_run_gh(args, timeout=timeout))
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="gh returned invalid JSON") from exc


def _normalize_check_state(item: dict[str, Any]) -> tuple[str, str]:
    raw = (
        item.get("conclusion")
        or item.get("status")
        or item.get("state")
        or item.get("statusState")
        or "UNKNOWN"
    )
    value = str(raw).upper()
    if value in {"SUCCESS", "SUCCESSFUL", "NEUTRAL", "SKIPPED"}:
        return ("pass", value)
    if value in {"FAILURE", "FAILED", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE", "ACTION_REQUIRED", "ERROR"}:
        return ("fail", value)
    if value in {"EXPECTED", "PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}:
        return ("pending", value)
    return ("pending", value)


def _normalize_status_checks(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    checks: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        state, raw = _normalize_check_state(item)
        name = item.get("name") or item.get("context") or item.get("workflowName") or "check"
        checks.append(
            {
                "name": str(name),
                "state": state,
                "rawState": raw,
                "workflow": item.get("workflowName"),
                "detailsUrl": item.get("detailsUrl") or item.get("targetUrl"),
            }
        )
    order = {"fail": 0, "pending": 1, "pass": 2}
    return sorted(checks, key=lambda check: (order.get(check["state"], 3), check["name"].lower()))


def _fetch_unresolved_threads(pr_url: str) -> list[dict[str, Any]]:
    threads: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        args = ["api", "graphql", "-f", f"query={REVIEW_THREADS_QUERY}", "-F", f"url={pr_url}"]
        if cursor:
            args.extend(["-F", f"endCursor={cursor}"])
        payload = _run_gh_json(args, timeout=30)
        resource = ((payload or {}).get("data") or {}).get("resource") or {}
        connection = resource.get("reviewThreads") or {}
        for node in connection.get("nodes") or []:
            if not isinstance(node, dict) or node.get("isResolved"):
                continue
            comments = (((node.get("comments") or {}).get("nodes")) or [])
            latest = comments[-1] if comments else {}
            threads.append(
                {
                    "path": node.get("path") or "unknown",
                    "latestComment": (latest or {}).get("body") or "",
                    "author": ((latest or {}).get("author") or {}).get("login"),
                    "updatedAt": (latest or {}).get("updatedAt"),
                    "url": (latest or {}).get("url"),
                }
            )
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            break
    return threads


def _fetch_pr_payload(pr_url: str) -> dict[str, Any]:
    view = _run_gh_json(
        [
            "pr",
            "view",
            pr_url,
            "--json",
            ",".join(
                [
                    "title",
                    "state",
                    "mergeable",
                    "mergeStateStatus",
                    "additions",
                    "deletions",
                    "changedFiles",
                    "statusCheckRollup",
                    "reviewDecision",
                    "url",
                    "headRefName",
                ]
            ),
        ],
        timeout=30,
    )
    diff = _run_gh(["pr", "diff", pr_url], timeout=60)
    threads = _fetch_unresolved_threads(pr_url)
    return {
        "url": view.get("url") or pr_url,
        "title": view.get("title") or pr_url,
        "state": view.get("state"),
        "mergeable": view.get("mergeable"),
        "mergeStateStatus": view.get("mergeStateStatus"),
        "reviewDecision": view.get("reviewDecision"),
        "headRefName": view.get("headRefName"),
        "additions": view.get("additions") or 0,
        "deletions": view.get("deletions") or 0,
        "changedFiles": view.get("changedFiles") or 0,
        "statusChecks": _normalize_status_checks(view.get("statusCheckRollup")),
        "unresolvedThreads": threads,
        "diff": diff,
    }


def get_pr_payload(ticket: str) -> dict[str, Any]:
    now = time.time()
    cached = _pr_cache.get(ticket)
    if cached and now - cached[0] < PR_CACHE_TTL_SECONDS:
        payload = cached[1]
        if payload.get("missing"):
            raise HTTPException(status_code=404, detail="No PR found for this agent")
        return payload

    pr_url = resolve_pr_url(ticket)
    if not pr_url:
        _pr_cache[ticket] = (now, {"missing": True})
        raise HTTPException(status_code=404, detail="No PR found for this agent")

    payload = _fetch_pr_payload(pr_url)
    _pr_cache[ticket] = (now, payload)
    return payload


def approve_pr(ticket: str) -> dict[str, str]:
    pr_url = resolve_pr_url(ticket)
    if not pr_url:
        raise HTTPException(status_code=404, detail="No PR found for this agent")
    _run_gh(["pr", "review", pr_url, "--approve"], timeout=30)
    _pr_cache.pop(ticket, None)
    return {"status": "approved"}
