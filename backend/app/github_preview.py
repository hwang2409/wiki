from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


PREVIEW_CACHE_TTL_SECONDS = 300
PREVIEW_CACHE_MAX_SIZE = 128
GITHUB_PREVIEW_URL_PATTERN = re.compile(
    r"^https://(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)/"
    r"(?:(?P<kind>pull|issues)/(?P<number>\d+)|commit/(?P<sha>[0-9a-fA-F]{7,40}))/?$"
)

_preview_cache: OrderedDict[str, tuple[float, str, dict[str, Any]]] = OrderedDict()


class GitHubPreviewFetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class ParsedGitHubPreviewUrl:
    normalized_url: str
    owner: str
    repo: str
    kind: str
    identifier: str


def parse_github_preview_url(url: str) -> ParsedGitHubPreviewUrl:
    match = GITHUB_PREVIEW_URL_PATTERN.fullmatch(url.strip())
    if not match:
        raise ValueError("Invalid GitHub preview URL")
    owner = match.group("owner")
    repo = match.group("repo")
    kind = match.group("kind") or "commit"
    identifier = match.group("number") or match.group("sha")
    if not identifier:
        raise ValueError("Invalid GitHub preview URL")
    normalized_url = (
        f"https://github.com/{owner}/{repo}/{kind}/{identifier}"
        if kind != "commit"
        else f"https://github.com/{owner}/{repo}/commit/{identifier}"
    )
    return ParsedGitHubPreviewUrl(
        normalized_url=normalized_url,
        owner=owner,
        repo=repo,
        kind="pr" if kind == "pull" else "issue" if kind == "issues" else "commit",
        identifier=identifier,
    )


def _run_gh_json(args: list[str], *, timeout: int = 20) -> Any:
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitHubPreviewFetchError(str(exc)) from exc
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or "unknown gh error"
        raise GitHubPreviewFetchError(stderr[:300])
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise GitHubPreviewFetchError("gh returned invalid JSON") from exc


def _check_summary(items: Any) -> dict[str, int]:
    summary = {"pass": 0, "fail": 0, "pending": 0}
    if not isinstance(items, list):
        return summary
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_state = str(
            item.get("conclusion")
            or item.get("status")
            or item.get("state")
            or item.get("statusState")
            or "UNKNOWN"
        ).upper()
        if raw_state in {"SUCCESS", "SUCCESSFUL", "NEUTRAL", "SKIPPED"}:
            summary["pass"] += 1
        elif raw_state in {
            "FAILURE",
            "FAILED",
            "TIMED_OUT",
            "CANCELLED",
            "STARTUP_FAILURE",
            "ACTION_REQUIRED",
            "ERROR",
        }:
            summary["fail"] += 1
        else:
            summary["pending"] += 1
    return summary


def _pr_preview(parsed: ParsedGitHubPreviewUrl) -> dict[str, Any]:
    view = _run_gh_json(
        [
            "pr",
            "view",
            parsed.normalized_url,
            "--json",
            "title,state,mergeStateStatus,statusCheckRollup,updatedAt,changedFiles",
        ]
    )
    if not isinstance(view, dict):
        raise GitHubPreviewFetchError("gh returned invalid PR payload")
    return {
        "ok": True,
        "kind": "pr",
        "title": view.get("title") or parsed.normalized_url,
        "state": view.get("state"),
        "extra": {
            "mergeStateStatus": view.get("mergeStateStatus"),
            "checks": _check_summary(view.get("statusCheckRollup")),
            "changedFiles": view.get("changedFiles"),
            "updatedAt": view.get("updatedAt"),
        },
    }


def _issue_preview(parsed: ParsedGitHubPreviewUrl) -> dict[str, Any]:
    view = _run_gh_json(
        [
            "issue",
            "view",
            parsed.normalized_url,
            "--json",
            "title,state,updatedAt",
        ]
    )
    if not isinstance(view, dict):
        raise GitHubPreviewFetchError("gh returned invalid issue payload")
    return {
        "ok": True,
        "kind": "issue",
        "title": view.get("title") or parsed.normalized_url,
        "state": view.get("state"),
        "extra": {
            "updatedAt": view.get("updatedAt"),
        },
    }


def _commit_preview(parsed: ParsedGitHubPreviewUrl) -> dict[str, Any]:
    payload = _run_gh_json(["api", f"repos/{parsed.owner}/{parsed.repo}/commits/{parsed.identifier}"])
    if not isinstance(payload, dict):
        raise GitHubPreviewFetchError("gh returned invalid commit payload")
    commit = payload.get("commit") or {}
    author = payload.get("author") or {}
    commit_author = commit.get("author") or {}
    message = str(commit.get("message") or "").splitlines()[0].strip() or parsed.identifier
    sha = str(payload.get("sha") or parsed.identifier)
    return {
        "ok": True,
        "kind": "commit",
        "title": message,
        "state": None,
        "extra": {
            "sha": sha,
            "author": author.get("login") or commit_author.get("name"),
            "date": commit_author.get("date"),
        },
    }


def _build_preview(parsed: ParsedGitHubPreviewUrl) -> dict[str, Any]:
    if parsed.kind == "pr":
        return _pr_preview(parsed)
    if parsed.kind == "issue":
        return _issue_preview(parsed)
    return _commit_preview(parsed)


def _etag_for_payload(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f'"{hashlib.sha1(encoded).hexdigest()}"'


def _store_cached_preview(url: str, expires_at: float, etag: str, payload: dict[str, Any]) -> None:
    _preview_cache[url] = (expires_at, etag, payload)
    _preview_cache.move_to_end(url)
    while len(_preview_cache) > PREVIEW_CACHE_MAX_SIZE:
        _preview_cache.popitem(last=False)


def get_github_preview(url: str) -> tuple[dict[str, Any], str]:
    parsed = parse_github_preview_url(url)
    cached = _preview_cache.get(parsed.normalized_url)
    now = time.time()
    if cached and cached[0] > now:
        _preview_cache.move_to_end(parsed.normalized_url)
        return (cached[2], cached[1])
    payload = _build_preview(parsed)
    etag = _etag_for_payload(payload)
    _store_cached_preview(parsed.normalized_url, now + PREVIEW_CACHE_TTL_SECONDS, etag, payload)
    return (payload, etag)


def etag_matches(header_value: str | None, etag: str) -> bool:
    if not header_value:
        return False
    candidates = [value.strip() for value in header_value.split(",")]
    return etag in candidates or "*" in candidates
