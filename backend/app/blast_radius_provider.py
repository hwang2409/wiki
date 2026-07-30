"""Background open-PR provider and snapshot attestation."""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .blast_radius_types import (
    MAX_ACTIVE_BRANCHES,
    OPEN_PR_MAX_AGE_SECONDS,
    OPEN_PR_REFRESH_SECONDS,
    OpenPRBranch,
    OpenPRSnapshotState,
    SourceAttestation,
)


def _logical_branch_name(value: str) -> str:
    for prefix in ("refs/heads/", "refs/remotes/origin/", "origin/"):
        if value.startswith(prefix):
            return value[len(prefix) :]
    return value


def _valid_snapshot_branch(value: object) -> bool:
    if not isinstance(value, str):
        return False
    branch = _logical_branch_name(value.strip())
    return bool(
        branch
        and not branch.startswith("-")
        and "\x00" not in branch
        and ".." not in branch
        and "@{" not in branch
        and not any(char.isspace() or ord(char) < 32 for char in branch)
    )


def _valid_head(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-fA-F]{40,64}", value.strip()))


def parse_open_pr_snapshot(payload: object) -> tuple[OpenPRBranch, ...]:
    rows = payload
    if isinstance(payload, dict):
        if "branches" not in payload and "pullRequests" not in payload:
            raise ValueError("open PR snapshot has no branch rows")
        rows = payload.get("branches") if "branches" in payload else payload.get("pullRequests")
    if not isinstance(rows, list | tuple):
        raise ValueError("open PR snapshot must be a list")

    branches: dict[str, OpenPRBranch] = {}
    for index, row in enumerate(rows):
        if isinstance(row, str):
            raw_name = row
            head_sha = None
            ticket = None
        elif isinstance(row, dict):
            raw_name = row.get("headRefName")
            head_sha = row.get("headRefOid")
            ticket = row.get("ticket")
        else:
            raise ValueError(f"open PR snapshot row {index} is malformed")
        if not _valid_snapshot_branch(raw_name):
            raise ValueError(f"open PR snapshot row {index} has an invalid branch row")
        if not _valid_head(head_sha):
            raise ValueError(f"open PR snapshot row {index} has no valid head SHA")
        if ticket is not None and not isinstance(ticket, str):
            raise ValueError(f"open PR snapshot row {index} has an invalid ticket")
        name = _logical_branch_name(str(raw_name).strip())
        normalized_ticket = str(ticket).strip() if ticket else None
        if name in branches:
            raise ValueError(f"open PR snapshot row {index} duplicates branch {name}")
        branches.setdefault(
            name,
            OpenPRBranch(
                name,
                str(head_sha).strip(),
                normalized_ticket,
                SourceAttestation(f"open-pr-row:{index}", True, True, True),
            ),
        )
    return tuple(sorted(branches.values(), key=lambda branch: branch.name))


def _validate_open_pr_payload(payload: object) -> None:
    rows = payload
    reported_total: int | None = None
    if isinstance(payload, dict):
        if "branches" not in payload and "pullRequests" not in payload:
            raise ValueError("open PR snapshot has no branch rows")
        if "branches" in payload and "pullRequests" in payload:
            raise ValueError("open PR snapshot has ambiguous branch rows")
        rows = payload.get("branches") if "branches" in payload else payload.get("pullRequests")
        for key in ("totalCount", "total_count", "total"):
            if key not in payload:
                continue
            value = payload[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("open PR snapshot total is malformed")
            reported_total = value
            break
    if not isinstance(rows, list | tuple):
        raise ValueError("open PR snapshot rows must be a list")
    if reported_total is not None and reported_total > MAX_ACTIVE_BRANCHES:
        raise RuntimeError(f"open PR snapshot truncated; reported more than {MAX_ACTIVE_BRANCHES} branches")
    if len(rows) > MAX_ACTIVE_BRANCHES:
        raise RuntimeError(f"open PR snapshot truncated; fetched more than {MAX_ACTIVE_BRANCHES} branches")
    if reported_total is not None and len(rows) < reported_total:
        raise RuntimeError("open PR snapshot truncated; fetched fewer than the reported branch total")


def _default_open_pr_provider(repo_root: Path | None = None) -> object:
    bound_repo = (repo_root or Path(__file__).resolve().parents[2]).resolve()
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--limit",
                str(MAX_ACTIVE_BRANCHES + 1),
                "--json",
                "headRefName,headRefOid,number",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            cwd=str(bound_repo),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(str(exc)) from exc
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "gh pr list failed").strip()[:200])
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError("gh returned invalid open PR JSON") from exc
    _validate_open_pr_payload(payload)
    return payload


class OpenPRSnapshot:
    """Background snapshot. Requests only read an attested state."""

    def __init__(
        self,
        provider: Callable[[], object] | None = None,
        *,
        refresh_seconds: float = OPEN_PR_REFRESH_SECONDS,
    ) -> None:
        self.provider = provider or _default_open_pr_provider
        self.repo_root: Path | None = None
        self._custom_provider = provider
        self.refresh_seconds = max(1.0, refresh_seconds)
        self._state = OpenPRSnapshotState(
            (), False, None, "open PR snapshot has not refreshed",
            SourceAttestation("open-pr-snapshot", False, True, False, "not refreshed"),
        )
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def read(self) -> OpenPRSnapshotState:
        with self._lock:
            state = self._state
        if state.refreshed_at is not None and time.time() - state.refreshed_at > OPEN_PR_MAX_AGE_SECONDS:
            return OpenPRSnapshotState(
                state.branches,
                False,
                state.refreshed_at,
                state.error or "open PR snapshot is stale",
                SourceAttestation("open-pr-snapshot", False, True, False, "stale"),
            )
        return state

    def set_repo_root(self, repo_root: Path) -> None:
        self.repo_root = repo_root.resolve()

    def refresh(self) -> OpenPRSnapshotState:
        try:
            payload = self._custom_provider() if self._custom_provider is not None else _default_open_pr_provider(self.repo_root)
            _validate_open_pr_payload(payload)
            branches = parse_open_pr_snapshot(payload)
            state = OpenPRSnapshotState(
                branches,
                True,
                time.time(),
                None,
                SourceAttestation("open-pr-snapshot", True, True, True),
            )
        except Exception as exc:
            with self._lock:
                previous = self._state
            state = OpenPRSnapshotState(
                previous.branches,
                False,
                previous.refreshed_at,
                str(exc)[:200],
                SourceAttestation("open-pr-snapshot", False, False, False, str(exc)[:200]),
            )
        with self._lock:
            self._state = state
        return state

    def _run(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self.refresh_seconds)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="wiki-open-pr-snapshot", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1)
        self._thread = None


OPEN_PR_SNAPSHOT = OpenPRSnapshot()
