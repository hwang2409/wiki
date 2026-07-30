"""Read-only branch collision analysis for the pre-spawn view."""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


MAX_ACTIVE_BRANCHES = 100
MAX_CHANGED_FILES = 5_000
MAX_CACHE_ENTRIES = 256
GIT_TIMEOUT_SECONDS = 2.0
ANALYSIS_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class BranchRef:
    name: str
    ref: str
    head_sha: str


@dataclass(frozen=True)
class ActiveBranch:
    name: str
    ref: str
    head_sha: str
    source: str
    ticket: str | None = None


@dataclass(frozen=True)
class BranchFiles:
    branch: str
    head_sha: str
    files: tuple[str, ...]


class DiffCache:
    """Small process-local cache keyed by branch and its current head SHA."""

    def __init__(self, max_entries: int = MAX_CACHE_ENTRIES) -> None:
        self.max_entries = max(1, max_entries)
        self._values: dict[tuple[str, str], tuple[str, ...]] = {}
        self._lock = threading.Lock()

    def get_or_compute(
        self,
        branch: str,
        head_sha: str,
        compute: Callable[[], tuple[str, ...]],
    ) -> tuple[str, ...]:
        key = (branch, head_sha)
        with self._lock:
            cached = self._values.get(key)
            if cached is not None:
                return cached

        value = tuple(compute())
        with self._lock:
            cached = self._values.get(key)
            if cached is not None:
                return cached
            while len(self._values) >= self.max_entries:
                self._values.pop(next(iter(self._values)))
            self._values[key] = value
        return value


DIFF_CACHE = DiffCache()


class GitAnalysisError(RuntimeError):
    pass


def _run_git(repo_root: Path, args: list[str], *, timeout: float) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=max(0.05, timeout),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitAnalysisError(str(exc)) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "git failed").strip()
        raise GitAnalysisError(detail[:200])
    return result.stdout


def _refs(repo_root: Path, *, timeout: float) -> dict[str, BranchRef]:
    output = _run_git(
        repo_root,
        [
            "for-each-ref",
            "--format=%(refname:short)%00%(refname)%00%(objectname)",
            "refs/heads",
            "refs/remotes",
            "--",
        ],
        timeout=timeout,
    )
    refs: dict[str, BranchRef] = {}
    for line in output.splitlines():
        parts = line.split("\x00")
        if len(parts) != 3:
            continue
        short, ref, sha = parts
        if not short or not ref or not sha:
            continue
        if short == "origin/HEAD" or short.endswith("/HEAD"):
            continue
        refs.setdefault(short, BranchRef(name=short, ref=ref, head_sha=sha))
    return refs


def _worktree_branches(repo_root: Path, *, timeout: float) -> dict[str, str]:
    output = _run_git(repo_root, ["worktree", "list", "--porcelain", "--"], timeout=timeout)
    result: dict[str, str] = {}
    current_path: str | None = None
    for line in output.splitlines() + [""]:
        if line.startswith("worktree "):
            current_path = str(Path(line[9:]).resolve())
        elif line.startswith("branch refs/") and current_path:
            result[current_path] = line[len("branch refs/heads/") :]
        elif not line.strip():
            current_path = None
    return result


def _registry_rows(registry: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    for ticket, entry in registry.items():
        if not isinstance(ticket, str) or ticket.startswith("_") or not isinstance(entry, dict):
            continue
        current = entry.get("current")
        if isinstance(current, dict):
            yield ticket, current


def _branch_hint(current: dict[str, Any]) -> str | None:
    for key in ("branch", "branch_name", "pr_branch", "head_ref_name"):
        value = current.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _ticket_for_branch(branch: str, registry: dict[str, Any]) -> str | None:
    lowered = branch.lower()
    for ticket, current in _registry_rows(registry):
        hint = _branch_hint(current)
        if hint and hint.lower() == lowered:
            return ticket
        if ticket.lower() == lowered or ticket.lower().replace("-", "/") == lowered:
            return ticket
    return None


def discover_active_branches(
    repo_root: Path,
    registry: dict[str, Any],
    *,
    refs: dict[str, BranchRef] | None = None,
    deadline: float | None = None,
) -> list[ActiveBranch]:
    """Find branch refs without inspecting any worker worktree.

    Remote-tracking refs are the only local record of fetched PR heads. They
    are included as PR candidates. Registered worker worktrees add their exact
    branch, even when the branch is local-only.
    """

    deadline = deadline or time.monotonic() + ANALYSIS_TIMEOUT_SECONDS

    def remaining() -> float:
        return max(0.05, min(GIT_TIMEOUT_SECONDS, deadline - time.monotonic()))

    try:
        refs = refs if refs is not None else _refs(repo_root, timeout=remaining())
        worktrees = _worktree_branches(repo_root, timeout=remaining())
    except GitAnalysisError:
        return []

    selected: dict[str, ActiveBranch] = {}
    main = refs.get("main")
    for name, ref in refs.items():
        if name == "main" or name.endswith("/main") or (main and ref.ref == main.ref):
            continue
        if name.startswith("origin/"):
            selected[name] = ActiveBranch(
                name=name,
                ref=ref.ref,
                head_sha=ref.head_sha,
                source="pr",
                ticket=_ticket_for_branch(name, registry),
            )

    registered_paths: dict[str, str] = {}
    for ticket, current in _registry_rows(registry):
        if current.get("role") == "orchestrator":
            continue
        raw_worktree = current.get("worktree") or current.get("cwd")
        if isinstance(raw_worktree, str) and raw_worktree.strip():
            try:
                registered_paths[str(Path(raw_worktree).expanduser().resolve())] = ticket
            except (OSError, RuntimeError, TypeError, ValueError):
                pass
        hint = _branch_hint(current)
        if hint:
            candidate = refs.get(hint) or refs.get(hint.removeprefix("refs/heads/"))
            if candidate and candidate.name not in {"main"}:
                selected[candidate.name] = ActiveBranch(
                    name=candidate.name,
                    ref=candidate.ref,
                    head_sha=candidate.head_sha,
                    source="worker",
                    ticket=ticket,
                )

    for path, ticket in registered_paths.items():
        branch_name = worktrees.get(path)
        if not branch_name:
            continue
        candidate = refs.get(branch_name) or refs.get(branch_name.removeprefix("refs/heads/"))
        if candidate is None or candidate.name == "main" or candidate.name.endswith("/main"):
            continue
        selected[candidate.name] = ActiveBranch(
            name=candidate.name,
            ref=candidate.ref,
            head_sha=candidate.head_sha,
            source="worker",
            ticket=ticket,
        )

    workers = sorted(
        (branch for branch in selected.values() if branch.source == "worker"),
        key=lambda branch: branch.name,
    )
    other_branches = sorted(
        (branch for branch in selected.values() if branch.source != "worker"),
        key=lambda branch: branch.name,
    )
    return [*workers, *other_branches][:MAX_ACTIVE_BRANCHES]


def changed_files(
    repo_root: Path,
    branch: BranchRef | ActiveBranch,
    *,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> tuple[str, ...]:
    """Return the immutable changed-file set for ``main...branch``."""

    output = _run_git(
        repo_root,
        ["diff", "--name-only", "--no-renames", f"main...{branch.ref}", "--"],
        timeout=timeout,
    )
    files = tuple(dict.fromkeys(line for line in output.splitlines() if line))
    return files[:MAX_CHANGED_FILES]


def collision_pairs(branches: Iterable[BranchFiles]) -> list[dict[str, Any]]:
    """Compute sorted pair intersections without mutating caller-owned sets."""

    rows = sorted(branches, key=lambda item: item.branch)
    collisions: list[dict[str, Any]] = []
    for index, left in enumerate(rows):
        left_files = set(left.files)
        for right in rows[index + 1 :]:
            overlap = sorted(left_files.intersection(set(right.files)))
            if not overlap:
                continue
            collisions.append(
                {
                    "left": left.branch,
                    "right": right.branch,
                    "overlap": overlap,
                    "overlap_count": len(overlap),
                }
            )
    return sorted(
        collisions,
        key=lambda item: (-int(item["overlap_count"]), item["left"], item["right"]),
    )


def _risk_summary(collisions: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for collision in collisions:
        for path in collision["overlap"]:
            counts[path] = counts.get(path, 0) + 1
    hot_files = [path for path, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:10]]
    count = len(collisions)
    level = "none" if count == 0 else "low" if count == 1 else "medium" if count < 4 else "high"
    return {"count": count, "level": level, "hot_files": hot_files}


def _candidate_matches(candidate: str, branch: ActiveBranch) -> bool:
    normalized = candidate.strip().lower()
    if not normalized or normalized == "all":
        return False
    if branch.name.lower() == normalized or branch.ref.lower() == normalized:
        return True
    if branch.ticket and branch.ticket.lower() == normalized:
        return True
    return False


def analyze(
    repo_root: Path,
    registry: dict[str, Any],
    candidate: str = "all",
    *,
    cache: DiffCache = DIFF_CACHE,
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + ANALYSIS_TIMEOUT_SECONDS
    try:
        refs = _refs(repo_root, timeout=GIT_TIMEOUT_SECONDS)
        active = discover_active_branches(repo_root, registry, refs=refs, deadline=deadline)
    except GitAnalysisError:
        return {
            "candidate": candidate,
            "branches": [],
            "collisions": [],
            "risk": _risk_summary([]),
            "error": "branch refs are unavailable",
        }

    candidate_branch: ActiveBranch | None = None
    if candidate.strip().lower() not in {"", "all"}:
        candidate_branch = next(
            (branch for branch in active if _candidate_matches(candidate, branch)),
            None,
        )
        if candidate_branch is None:
            normalized = candidate.strip().lower()
            candidate_ref = next(
                (
                    ref
                    for ref in refs.values()
                    if ref.name.lower() == normalized
                    or ref.ref.lower() == normalized
                    or ref.name.lower() == normalized.removeprefix("refs/heads/")
                ),
                None,
            )
            if candidate_ref is None:
                for ticket_name, current in _registry_rows(registry):
                    hint = _branch_hint(current)
                    if ticket_name.lower() == normalized or (hint and hint.lower() == normalized):
                        candidate_ref = refs.get(hint or "")
                        if candidate_ref is not None:
                            break
            if candidate_ref is not None and candidate_ref.name != "main" and not candidate_ref.name.endswith("/main"):
                candidate_branch = ActiveBranch(
                    name=candidate_ref.name,
                    ref=candidate_ref.ref,
                    head_sha=candidate_ref.head_sha,
                    source="candidate",
                    ticket=_ticket_for_branch(candidate_ref.name, registry),
                )
        if candidate_branch is None:
            return {
                "candidate": candidate,
                "candidate_found": False,
                "branches": [],
                "collisions": [],
                "risk": _risk_summary([]),
                "error": "candidate branch was not found in fetched refs",
            }

    rows: list[BranchFiles] = []
    branch_payload: list[dict[str, Any]] = []
    selected = [candidate_branch] if candidate_branch else active
    if candidate_branch:
        selected = [candidate_branch, *[branch for branch in active if branch.name != candidate_branch.name]]

    for branch in selected:
        if time.monotonic() >= deadline:
            break
        try:
            files = cache.get_or_compute(
                branch.name,
                branch.head_sha,
                lambda branch=branch: changed_files(
                    repo_root,
                    branch,
                    timeout=min(GIT_TIMEOUT_SECONDS, max(0.05, deadline - time.monotonic())),
                ),
            )
        except GitAnalysisError:
            continue
        rows.append(BranchFiles(branch=branch.name, head_sha=branch.head_sha, files=files))
        branch_payload.append(
            {
                "branch": branch.name,
                "ticket": branch.ticket,
                "source": branch.source,
                "head_sha": branch.head_sha,
                "files": list(files),
                "file_count": len(files),
            }
        )

    collisions = collision_pairs(rows)
    if candidate_branch:
        collisions = [
            collision
            for collision in collisions
            if candidate_branch.name in {collision["left"], collision["right"]}
        ]
    return {
        "candidate": candidate,
        "candidate_found": candidate_branch is not None if candidate.strip().lower() not in {"", "all"} else None,
        "branches": branch_payload,
        "collisions": collisions,
        "risk": _risk_summary(collisions),
    }
