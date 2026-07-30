"""Read-only branch collision analysis for the pre-spawn view."""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


MAX_ACTIVE_BRANCHES = 100
MAX_CHANGED_FILES = 5_000
MAX_CACHE_ENTRIES = 256
GIT_TIMEOUT_SECONDS = 2.0
ANALYSIS_TIMEOUT_SECONDS = 5.0
OPEN_PR_REFRESH_SECONDS = 30.0
OPEN_PR_MAX_AGE_SECONDS = 90.0


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


@dataclass(frozen=True)
class OpenPRBranch:
    name: str
    head_sha: str | None = None
    ticket: str | None = None


@dataclass(frozen=True)
class OpenPRSnapshotState:
    branches: tuple[OpenPRBranch, ...]
    complete: bool
    refreshed_at: float | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        # Keep older injected states with a positional error usable.
        if isinstance(self.refreshed_at, str) and self.error is None:
            object.__setattr__(self, "error", self.refreshed_at)
            object.__setattr__(self, "refreshed_at", None)


@dataclass(frozen=True)
class DiscoveryResult:
    branches: tuple[ActiveBranch, ...]
    failed_branches: tuple[dict[str, str], ...]
    open_pr_snapshot_complete: bool


class ChangedFiles(tuple[str, ...]):
    """Immutable changed files with explicit truncation metadata."""

    truncated: bool
    dropped_count: int

    def __new__(
        cls,
        files: Iterable[str] = (),
        *,
        truncated: bool = False,
        dropped_count: int = 0,
    ) -> "ChangedFiles":
        value = super().__new__(cls, files)
        value.truncated = truncated
        value.dropped_count = dropped_count
        return value


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


def parse_open_pr_snapshot(payload: object) -> tuple[OpenPRBranch, ...]:
    """Parse provider output without trusting it as a git ref."""

    rows = payload
    if isinstance(payload, dict):
        rows = payload.get("branches") or payload.get("pullRequests") or []
    if not isinstance(rows, list | tuple):
        raise ValueError("open PR snapshot must be a list")

    branches: dict[str, OpenPRBranch] = {}
    for row in rows:
        if isinstance(row, str):
            raw_name = row
            head_sha = None
            ticket = None
        elif isinstance(row, dict):
            raw_name = row.get("headRefName") or row.get("branch") or row.get("name")
            head_sha = row.get("headRefOid") or row.get("head_sha")
            ticket = row.get("ticket")
        else:
            raise ValueError("open PR snapshot contains a malformed row")
        if not _valid_snapshot_branch(raw_name):
            raise ValueError("open PR snapshot contains an invalid branch row")
        name = _logical_branch_name(str(raw_name).strip())
        normalized_sha = str(head_sha).strip() if head_sha else None
        normalized_ticket = str(ticket).strip() if ticket else None
        branches.setdefault(name, OpenPRBranch(name, normalized_sha, normalized_ticket))
    return tuple(sorted(branches.values(), key=lambda branch: branch.name))


def _validate_open_pr_payload(payload: object) -> None:
    rows = payload
    reported_total: int | None = None
    if isinstance(payload, dict):
        if "branches" not in payload and "pullRequests" not in payload:
            raise ValueError("open PR snapshot has no branch rows")
        rows = payload.get("branches") if "branches" in payload else payload.get("pullRequests")
        for key in ("totalCount", "total_count", "total"):
            value = payload.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                reported_total = value
                break
    if not isinstance(rows, list | tuple):
        raise ValueError("open PR snapshot rows must be a list")
    if reported_total is not None and reported_total > MAX_ACTIVE_BRANCHES:
        raise RuntimeError(
            f"open PR snapshot truncated; reported more than {MAX_ACTIVE_BRANCHES} branches"
        )
    if isinstance(rows, list | tuple) and len(rows) > MAX_ACTIVE_BRANCHES:
        raise RuntimeError(
            f"open PR snapshot truncated; fetched more than {MAX_ACTIVE_BRANCHES} branches"
        )
    if isinstance(rows, list | tuple) and reported_total is not None and len(rows) < reported_total:
        raise RuntimeError("open PR snapshot truncated; fetched fewer than the reported branch total")


def _default_open_pr_provider(repo_root: Path | None = None) -> object:
    """Read open PR heads outside the request path.

    The snapshot is refreshed by a background thread. A request only reads
    the last successful snapshot and never invokes GitHub or ``gh``.
    """

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
    """Injected, background-refreshed source of open PR branch names."""

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
        self._state = OpenPRSnapshotState((), False, None, "open PR snapshot has not refreshed")
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def read(self) -> OpenPRSnapshotState:
        with self._lock:
            return self._state

    def set_repo_root(self, repo_root: Path) -> None:
        self.repo_root = repo_root.resolve()

    def refresh(self) -> OpenPRSnapshotState:
        try:
            payload = (
                self._custom_provider()
                if self._custom_provider is not None
                else _default_open_pr_provider(self.repo_root)
            )
            _validate_open_pr_payload(payload)
            branches = parse_open_pr_snapshot(payload)
            if len(branches) > MAX_ACTIVE_BRANCHES:
                raise RuntimeError(
                    f"open PR snapshot truncated; fetched more than {MAX_ACTIVE_BRANCHES} branches"
                )
            state = OpenPRSnapshotState(branches, True, time.time())
        except Exception as exc:  # provider failure must not break the request path
            with self._lock:
                previous = self._state
            state = OpenPRSnapshotState(
                previous.branches,
                False,
                previous.refreshed_at,
                str(exc)[:200],
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


class DiffCache:
    """Bounded cache with same-key single-flight computation."""

    def __init__(self, max_entries: int = MAX_CACHE_ENTRIES) -> None:
        self.max_entries = max(1, max_entries)
        self._values: dict[tuple[str, str], tuple[str, ...]] = {}
        self._inflight: dict[tuple[str, str], Future[tuple[str, ...]]] = {}
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
            future = self._inflight.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._inflight[key] = future

        if not owner:
            return future.result()

        try:
            value = compute()
        except BaseException as exc:
            with self._lock:
                self._inflight.pop(key, None)
                future.set_exception(exc)
            raise

        with self._lock:
            cached = self._values.get(key)
            if cached is None:
                while len(self._values) >= self.max_entries:
                    self._values.pop(next(iter(self._values)))
                self._values[key] = value
                cached = value
            self._inflight.pop(key, None)
            future.set_result(cached)
        return cached


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
        if not line:
            continue
        parts = line.split("\x00")
        if len(parts) != 3:
            raise GitAnalysisError("git refs output is malformed")
        short, ref, sha = parts
        if not short or not ref or not sha or short.endswith("/HEAD"):
            if short.endswith("/HEAD"):
                continue
            raise GitAnalysisError("git refs output contains an invalid ref")
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
            raise GitAnalysisError("git refs output contains an invalid object id")
        refs.setdefault(short, BranchRef(name=short, ref=ref, head_sha=sha))
    return refs


def _resolve_main_ref(refs: dict[str, BranchRef]) -> BranchRef | None:
    for name in ("main", "origin/main"):
        if name in refs:
            return refs[name]
    return next((refs[name] for name in sorted(refs) if name.endswith("/main")), None)


def _worktree_branches(repo_root: Path, *, timeout: float) -> dict[str, str]:
    output = _run_git(repo_root, ["worktree", "list", "--porcelain", "--"], timeout=timeout)
    result: dict[str, str] = {}
    current_path: str | None = None
    for line in output.splitlines() + [""]:
        if line.startswith("worktree "):
            current_path = str(Path(line[9:]).resolve())
        elif line.startswith("branch refs/") and current_path:
            result[current_path] = _logical_branch_name(line[len("branch ") :])
        elif not line.strip():
            current_path = None
    return result


def _git_common_dir(worktree: Path, *, timeout: float) -> Path:
    del timeout
    git_path = worktree / ".git"
    try:
        if git_path.is_dir():
            return git_path.resolve()
        if git_path.is_file():
            marker = git_path.read_text(encoding="utf-8").strip()
            if not marker.startswith("gitdir:"):
                raise GitAnalysisError("git common directory marker is invalid")
            gitdir = Path(marker[len("gitdir:") :].strip())
            if not gitdir.is_absolute():
                gitdir = git_path.parent / gitdir
            return gitdir.resolve().parent.parent
    except (OSError, RuntimeError, UnicodeError) as exc:
        raise GitAnalysisError(str(exc)) from exc
    raise GitAnalysisError("git common directory is unavailable")


def _path_is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _registry_rows(registry: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    for ticket, entry in registry.items():
        if not isinstance(ticket, str) or ticket.startswith("_") or not isinstance(entry, dict):
            continue
        current = entry.get("current")
        if isinstance(current, dict):
            yield ticket, current


def _registry_shape_failures(registry: object) -> list[dict[str, str]]:
    if not isinstance(registry, dict):
        return [_failed("agent registry", "registry must be an object")]
    failures: list[dict[str, str]] = []
    for ticket, entry in registry.items():
        if isinstance(ticket, str) and ticket.startswith("_"):
            if ticket == "_orchestrators" and entry is not None and not isinstance(entry, dict):
                failures.append(_failed(ticket, "registry entry must be an object"))
            continue
        if not isinstance(ticket, str) or not isinstance(entry, dict):
            failures.append(_failed(str(ticket), "registry entry is malformed"))
            continue
        current = entry.get("current")
        if current is not None and not isinstance(current, dict):
            failures.append(_failed(ticket, "registry current entry is malformed"))
    return failures


def _branch_hint(current: dict[str, Any]) -> str | None:
    for key in ("branch", "branch_name", "pr_branch", "head_ref_name"):
        value = current.get(key)
        if isinstance(value, str) and value.strip():
            return _logical_branch_name(value.strip())
    return None


def _ticket_for_branch(branch: str, registry: dict[str, Any]) -> str | None:
    lowered = _logical_branch_name(branch).lower()
    for ticket, current in _registry_rows(registry):
        hint = _branch_hint(current)
        if hint and hint.lower() == lowered:
            return ticket
        if ticket.lower() == lowered or ticket.lower().replace("-", "/") == lowered:
            return ticket
    return None


def _ref_candidates(refs: dict[str, BranchRef], branch: str) -> list[BranchRef]:
    logical = _logical_branch_name(branch)
    preferred = [logical, f"origin/{logical}"]
    ordered: list[BranchRef] = []
    for name in preferred:
        ref = refs.get(name)
        if ref is not None:
            ordered.append(ref)
    ordered.extend(ref for name, ref in refs.items() if _logical_branch_name(name) == logical and ref not in ordered)
    return ordered


def _select_ref(
    refs: dict[str, BranchRef],
    branch: str,
    expected_sha: str | None = None,
) -> tuple[BranchRef | None, str | None]:
    candidates = [ref for ref in _ref_candidates(refs, branch) if _logical_branch_name(ref.name) != "main"]
    if not candidates:
        return None, "branch ref is not present in fetched refs"
    if expected_sha:
        matching = [ref for ref in candidates if ref.head_sha == expected_sha]
        if not matching:
            return None, "fetched branch head does not match the open PR head"
        return matching[0], None
    return candidates[0], None


def _failed(branch: str, reason: str) -> dict[str, str]:
    return {"branch": branch, "reason": reason[:200]}


def _add_selected(selected: dict[str, list[ActiveBranch]], branch: ActiveBranch) -> None:
    candidates = selected.setdefault(branch.name, [])
    if branch not in candidates:
        candidates.append(branch)


def _is_ancestor(repo_root: Path, ancestor: str, descendant: str, *, timeout: float) -> bool:
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", ancestor) or not re.fullmatch(
        r"[0-9a-fA-F]{40,64}", descendant
    ):
        raise GitAnalysisError("branch head is not a valid Git object id")
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "merge-base",
                "--is-ancestor",
                ancestor,
                descendant,
            ],
            capture_output=True,
            text=True,
            timeout=max(0.05, timeout),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitAnalysisError(str(exc)) from exc
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise GitAnalysisError((result.stderr or result.stdout or "cannot compare branch heads").strip()[:200])


def _reconcile_selected_heads(
    repo_root: Path,
    candidates: dict[str, list[ActiveBranch]],
    *,
    deadline: float,
) -> tuple[dict[str, ActiveBranch], list[dict[str, str]]]:
    selected: dict[str, ActiveBranch] = {}
    failures: list[dict[str, str]] = []
    for name, branches in candidates.items():
        pr = next((branch for branch in branches if branch.source == "pr"), None)
        workers = [branch for branch in branches if branch.source == "worker"]
        if pr is None:
            unique_heads = {branch.head_sha for branch in workers}
            if len(unique_heads) > 1:
                failures.append(_failed(name, "worker branch heads do not agree"))
                continue
            if workers:
                selected[name] = workers[0]
            continue

        winner = pr
        undecidable = False
        for worker in workers:
            if worker.head_sha == winner.head_sha:
                continue
            try:
                worker_descendant = _is_ancestor(
                    repo_root,
                    winner.head_sha,
                    worker.head_sha,
                    timeout=min(GIT_TIMEOUT_SECONDS, max(0.05, deadline - time.monotonic())),
                )
                winner_descendant = _is_ancestor(
                    repo_root,
                    worker.head_sha,
                    winner.head_sha,
                    timeout=min(GIT_TIMEOUT_SECONDS, max(0.05, deadline - time.monotonic())),
                )
            except GitAnalysisError as exc:
                failures.append(_failed(name, f"cannot compare PR and worker branch heads: {exc}"))
                undecidable = True
                break
            if worker_descendant and not winner_descendant:
                winner = worker
            elif winner_descendant and not worker_descendant:
                continue
            else:
                failures.append(_failed(name, "PR and worker branch heads are mismatched or unrelated"))
                undecidable = True
                break
        if not undecidable:
            selected[name] = winner
    return selected, failures


def discover_active_branch_result(
    repo_root: Path,
    registry: dict[str, Any],
    *,
    refs: dict[str, BranchRef],
    pr_snapshot: OpenPRSnapshotState,
    deadline: float,
) -> DiscoveryResult:
    failures: list[dict[str, str]] = []
    selected: dict[str, list[ActiveBranch]] = {}

    if pr_snapshot.complete:
        if len(pr_snapshot.branches) > MAX_ACTIVE_BRANCHES:
            failures.append(
                _failed(
                    "open PR branches",
                    f"open PR branch list truncated; dropped {len(pr_snapshot.branches) - MAX_ACTIVE_BRANCHES} branches",
                )
            )
        for pr_branch in pr_snapshot.branches[:MAX_ACTIVE_BRANCHES]:
            ref, reason = _select_ref(refs, pr_branch.name, pr_branch.head_sha)
            if ref is None:
                failures.append(_failed(pr_branch.name, reason or "branch ref unavailable"))
                continue
            _add_selected(
                selected,
                ActiveBranch(
                    name=_logical_branch_name(ref.name),
                    ref=ref.ref,
                    head_sha=ref.head_sha,
                    source="pr",
                    ticket=pr_branch.ticket or _ticket_for_branch(pr_branch.name, registry),
                ),
            )

    try:
        worktrees = _worktree_branches(
            repo_root,
            timeout=min(GIT_TIMEOUT_SECONDS, max(0.05, deadline - time.monotonic())),
        )
    except GitAnalysisError as exc:
        worktrees = {}
        failures.append(_failed("registered worker worktrees", str(exc)))

    try:
        primary_root = repo_root.resolve()
        primary_common_dir = _git_common_dir(
            repo_root,
            timeout=min(GIT_TIMEOUT_SECONDS, max(0.05, deadline - time.monotonic())),
        )
    except GitAnalysisError as exc:
        primary_root = repo_root
        primary_common_dir = None
        failures.append(_failed("git common directory", str(exc)))

    registered_paths: dict[str, tuple[str, str | None]] = {}
    for ticket, current in _registry_rows(registry):
        if current.get("role") == "orchestrator":
            continue
        eligible_for_hint = True
        raw_worktree = current.get("worktree") or current.get("cwd")
        if isinstance(raw_worktree, str) and raw_worktree.strip():
            try:
                path = Path(raw_worktree).expanduser().resolve()
            except (OSError, RuntimeError, TypeError, ValueError):
                if current.get("role") != "review":
                    failures.append(_failed(ticket, f"registered worker worktree is unreadable: {raw_worktree}"))
                eligible_for_hint = False
                continue
            local_path = _path_is_within(path, primary_root)
            if primary_common_dir is None:
                if local_path and current.get("role") != "review":
                    failures.append(_failed(ticket, f"registered worker worktree is unreadable: {path}"))
                eligible_for_hint = False
                continue
            try:
                worker_common_dir = _git_common_dir(
                    path,
                    timeout=min(GIT_TIMEOUT_SECONDS, max(0.05, deadline - time.monotonic())),
                )
            except GitAnalysisError:
                if local_path and current.get("role") != "review":
                    failures.append(_failed(ticket, f"registered worker worktree is unreadable: {path}"))
                eligible_for_hint = False
                continue
            if worker_common_dir != primary_common_dir:
                eligible_for_hint = False
                continue
            registered_paths[str(path)] = (ticket, current.get("role"))
            if current.get("role") == "review" and str(path) not in worktrees:
                eligible_for_hint = False
        elif raw_worktree is not None:
            if current.get("role") != "review":
                failures.append(_failed(ticket, f"registered worker worktree is unreadable: {raw_worktree}"))
            eligible_for_hint = False
        hint = _branch_hint(current)
        if hint and eligible_for_hint:
            ref, reason = _select_ref(refs, hint)
            if ref is None:
                failures.append(_failed(hint, reason or "registered branch ref unavailable"))
            else:
                _add_selected(
                    selected,
                    ActiveBranch(
                        name=_logical_branch_name(ref.name),
                        ref=ref.ref,
                        head_sha=ref.head_sha,
                        source="worker",
                        ticket=ticket,
                    ),
                )

    for path, (ticket, role) in registered_paths.items():
        branch_name = worktrees.get(path)
        if not branch_name:
            if role == "review":
                continue
            failures.append(_failed(ticket, "registered worker branch is not available"))
            continue
        ref, reason = _select_ref(refs, branch_name)
        if ref is None:
            failures.append(_failed(branch_name, reason or "registered branch ref unavailable"))
            continue
        _add_selected(
            selected,
            ActiveBranch(
                name=_logical_branch_name(ref.name),
                ref=ref.ref,
                head_sha=ref.head_sha,
                source="worker",
                ticket=ticket,
            ),
        )

    reconciled, head_failures = _reconcile_selected_heads(repo_root, selected, deadline=deadline)
    failures.extend(head_failures)
    workers = sorted(
        (branch for branch in reconciled.values() if branch.source == "worker"),
        key=lambda branch: branch.name,
    )
    other_branches = sorted(
        (branch for branch in reconciled.values() if branch.source != "worker"),
        key=lambda branch: branch.name,
    )
    all_branches = [*workers, *other_branches]
    dropped_count = max(0, len(all_branches) - MAX_ACTIVE_BRANCHES)
    if dropped_count:
        failures.append(
            _failed(
                "active branches",
                f"active branch list truncated; dropped {dropped_count} branches",
            )
        )
    return DiscoveryResult(
        tuple(all_branches[:MAX_ACTIVE_BRANCHES]),
        tuple(failures),
        pr_snapshot.complete,
    )


def discover_active_branches(
    repo_root: Path,
    registry: dict[str, Any],
    *,
    refs: dict[str, BranchRef] | None = None,
    deadline: float | None = None,
    pr_snapshot: OpenPRSnapshotState | None = None,
) -> list[ActiveBranch]:
    """Compatibility wrapper returning only validated active branches."""

    deadline = deadline or time.monotonic() + ANALYSIS_TIMEOUT_SECONDS
    refs = refs or _refs(repo_root, timeout=GIT_TIMEOUT_SECONDS)
    snapshot = pr_snapshot or OPEN_PR_SNAPSHOT.read()
    return list(
        discover_active_branch_result(
            repo_root,
            registry,
            refs=refs,
            pr_snapshot=snapshot,
            deadline=deadline,
        ).branches
    )


def changed_files(
    repo_root: Path,
    branch: BranchRef | ActiveBranch,
    *,
    main_ref: BranchRef | None = None,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> ChangedFiles:
    """Return the immutable changed-file set for ``main...branch``."""

    if main_ref is None:
        main_ref = _resolve_main_ref(_refs(repo_root, timeout=timeout))
    if main_ref is None:
        raise GitAnalysisError("main ref is not present in fetched refs")
    output = _run_git(
        repo_root,
        ["diff", "--name-only", "--no-renames", f"{main_ref.ref}...{branch.ref}", "--"],
        timeout=timeout,
    )
    files = tuple(dict.fromkeys(line for line in output.splitlines() if line))
    dropped_count = max(0, len(files) - MAX_CHANGED_FILES)
    return ChangedFiles(
        files[:MAX_CHANGED_FILES],
        truncated=bool(dropped_count),
        dropped_count=dropped_count,
    )


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
    hot_files = [
        path for path, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:10]
    ]
    count = len(collisions)
    level = "none" if count == 0 else "low" if count == 1 else "medium" if count < 4 else "high"
    return {"count": count, "level": level, "hot_files": hot_files}


def _candidate_matches(candidate: str, branch: ActiveBranch) -> bool:
    normalized = _logical_branch_name(candidate.strip()).lower()
    return bool(
        normalized
        and (
            branch.name.lower() == normalized
            or branch.ref.lower() == candidate.strip().lower()
            or (branch.ticket and branch.ticket.lower() == normalized)
        )
    )


def _candidate_ref(
    candidate: str,
    refs: dict[str, BranchRef],
    active: Sequence[ActiveBranch],
    registry: dict[str, Any],
) -> BranchRef | None:
    found = next((branch for branch in active if _candidate_matches(candidate, branch)), None)
    if found is not None:
        return BranchRef(found.name, found.ref, found.head_sha)
    normalized = _logical_branch_name(candidate.strip()).lower()
    if not normalized or normalized == "all":
        return None
    ref, _reason = _select_ref(refs, normalized)
    if ref is not None:
        return ref
    for ticket, current in _registry_rows(registry):
        if ticket.lower() != normalized:
            continue
        hint = _branch_hint(current)
        if hint:
            ref, _reason = _select_ref(refs, hint)
            return ref
    return None


def _response(
    *,
    candidate: str,
    candidate_found: bool | None,
    branches: list[dict[str, Any]],
    collisions: list[dict[str, Any]],
    failed_branches: list[dict[str, str]],
    refreshed_at: float | None = None,
    snapshot_max_age_seconds: float = OPEN_PR_MAX_AGE_SECONDS,
    error: str | None = None,
) -> dict[str, Any]:
    deduped_failures: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for failure in failed_branches:
        key = (failure.get("branch", ""), failure.get("reason", ""))
        if key in seen:
            continue
        seen.add(key)
        deduped_failures.append(failure)
    complete = not deduped_failures and error is None
    payload: dict[str, Any] = {
        "candidate": candidate,
        "candidate_found": candidate_found,
        "complete": complete,
        "failed_branches": deduped_failures,
        "branches": branches,
        "collisions": collisions,
        "risk": _risk_summary(collisions) if complete else None,
        "refreshed_at": refreshed_at,
        "snapshot_max_age_seconds": snapshot_max_age_seconds,
    }
    if error:
        payload["error"] = error
    return payload


def analyze(
    repo_root: Path,
    registry: dict[str, Any],
    candidate: str = "all",
    *,
    cache: DiffCache = DIFF_CACHE,
    pr_snapshot: OpenPRSnapshot | OpenPRSnapshotState | None = None,
    registry_error: str | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + ANALYSIS_TIMEOUT_SECONDS
    snapshot_state = (
        pr_snapshot.read()
        if isinstance(pr_snapshot, OpenPRSnapshot)
        else pr_snapshot or OPEN_PR_SNAPSHOT.read()
    )
    snapshot_age = (
        None
        if snapshot_state.refreshed_at is None
        else max(0.0, time.time() - snapshot_state.refreshed_at)
    )
    snapshot_stale = snapshot_age is None or snapshot_age > OPEN_PR_MAX_AGE_SECONDS
    if snapshot_stale and snapshot_state.complete:
        snapshot_state = OpenPRSnapshotState(
            snapshot_state.branches,
            False,
            snapshot_state.refreshed_at,
            snapshot_state.error or "open PR snapshot is stale",
        )
    registry_failures = _registry_shape_failures(registry)
    if registry_error:
        registry_failures.insert(0, _failed("agent registry", registry_error))
    if not isinstance(registry, dict):
        registry = {}
    try:
        refs = _refs(repo_root, timeout=GIT_TIMEOUT_SECONDS)
    except GitAnalysisError as exc:
        return _response(
            candidate=candidate,
            candidate_found=None if candidate.strip().lower() in {"", "all"} else False,
            branches=[],
            collisions=[],
            failed_branches=[*registry_failures, _failed("git refs", str(exc))],
            refreshed_at=snapshot_state.refreshed_at,
            error="branch refs are unavailable",
        )

    main_ref = _resolve_main_ref(refs)
    if main_ref is None:
        return _response(
            candidate=candidate,
            candidate_found=None if candidate.strip().lower() in {"", "all"} else False,
            branches=[],
            collisions=[],
            failed_branches=[*registry_failures, _failed("main", "main ref is not present in fetched refs")],
            refreshed_at=snapshot_state.refreshed_at,
            error="main ref is unavailable",
        )

    discovery = discover_active_branch_result(
        repo_root,
        registry,
        refs=refs,
        pr_snapshot=snapshot_state,
        deadline=deadline,
    )
    failures = [*registry_failures, *discovery.failed_branches]
    if not snapshot_state.complete:
        reason = snapshot_state.error or "snapshot is incomplete"
        if snapshot_stale and "stale" not in reason:
            reason = f"{reason}; snapshot is stale"
        failures.append(_failed("open PR snapshot", reason))

    candidate_found: bool | None = None
    candidate_branch: ActiveBranch | None = None
    if candidate.strip().lower() not in {"", "all"}:
        candidate_ref = _candidate_ref(candidate, refs, discovery.branches, registry)
        if candidate_ref is None:
            candidate_found = False
            failures.append(_failed(candidate, "candidate branch was not found in fetched refs"))
        else:
            candidate_found = True
            candidate_branch = ActiveBranch(
                name=_logical_branch_name(candidate_ref.name),
                ref=candidate_ref.ref,
                head_sha=candidate_ref.head_sha,
                source="candidate",
                ticket=_ticket_for_branch(candidate_ref.name, registry),
            )

    selected: list[ActiveBranch] = list(discovery.branches)
    if candidate_branch is not None:
        selected = [candidate_branch, *[branch for branch in selected if branch.name != candidate_branch.name]]

    rows: list[BranchFiles] = []
    branch_payload: list[dict[str, Any]] = []
    for index, branch in enumerate(selected):
        if time.monotonic() >= deadline:
            failures.extend(_failed(remaining.name, "analysis deadline exceeded") for remaining in selected[index:])
            break
        try:
            files = cache.get_or_compute(
                branch.name,
                branch.head_sha,
                lambda branch=branch: changed_files(
                    repo_root,
                    branch,
                    main_ref=main_ref,
                    timeout=min(GIT_TIMEOUT_SECONDS, max(0.05, deadline - time.monotonic())),
                ),
            )
        except BaseException as exc:
            failures.append(_failed(branch.name, str(exc)))
            continue
        if getattr(files, "truncated", False):
            failures.append(
                _failed(
                    branch.name,
                    f"changed file list truncated; dropped {getattr(files, 'dropped_count', 0)} files",
                )
            )
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
    return _response(
        candidate=candidate,
        candidate_found=candidate_found,
        branches=branch_payload,
        collisions=collisions,
        failed_branches=failures,
        refreshed_at=snapshot_state.refreshed_at,
    )
