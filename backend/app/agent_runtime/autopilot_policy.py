"""Merge authority and repository policy for autopilot."""

from __future__ import annotations

from urllib.parse import urlparse


AUTO_MERGE_REPOSITORIES = frozenset({"hwang2409/wiki"})


def repository_from_pr_url(pr_url: str) -> str | None:
    parsed = urlparse(pr_url)
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 4 or parts[2] != "pull" or not parts[3].isdigit():
        return None
    return f"{parts[0]}/{parts[1]}".lower()


def merge_authorized(pr_url: str) -> bool:
    return repository_from_pr_url(pr_url) in AUTO_MERGE_REPOSITORIES


__all__ = ["AUTO_MERGE_REPOSITORIES", "merge_authorized", "repository_from_pr_url"]
