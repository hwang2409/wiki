"""Bounded Git reads and ref/worktree parsing for blast-radius analysis."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .blast_radius_provider import _logical_branch_name, _valid_snapshot_branch
from .blast_radius_types import BranchRef, GitAnalysisError


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


def refs(repo_root: Path, *, timeout: float) -> dict[str, BranchRef]:
    return parse_refs_output(
        _run_git(
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
    )


def parse_refs_output(output: str) -> dict[str, BranchRef]:
    result: dict[str, BranchRef] = {}
    for line in output.splitlines():
        if not line:
            continue
        parts = line.split("\x00")
        if len(parts) != 3:
            raise GitAnalysisError("git refs output is malformed")
        short, ref, sha = parts
        if short.endswith("/HEAD"):
            continue
        if not short or not ref or not sha:
            raise GitAnalysisError("git refs output contains an invalid ref")
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
            raise GitAnalysisError("git refs output contains an invalid object id")
        result.setdefault(short, BranchRef(short, ref, sha))
    return result


def resolve_main_ref(refs_by_name: dict[str, BranchRef]) -> BranchRef | None:
    for name in ("main", "origin/main"):
        if name in refs_by_name:
            return refs_by_name[name]
    return next((refs_by_name[name] for name in sorted(refs_by_name) if name.endswith("/main")), None)


def worktree_branches(repo_root: Path, *, timeout: float) -> dict[str, str]:
    output = _run_git(repo_root, ["worktree", "list", "--porcelain", "--"], timeout=timeout)
    result: dict[str, str] = {}
    current_path: str | None = None
    current_head: str | None = None
    current_branch: str | None = None
    current_detached = False

    def finish_block() -> None:
        nonlocal current_path, current_head, current_branch, current_detached
        if current_path is None:
            return
        if current_head is None or not re.fullmatch(r"[0-9a-fA-F]{40,64}", current_head):
            raise GitAnalysisError("git worktree output is malformed")
        if current_branch is not None and current_detached:
            raise GitAnalysisError("git worktree output has conflicting branch state")
        if current_branch is not None and not _valid_snapshot_branch(current_branch):
            raise GitAnalysisError("git worktree output has an invalid branch")
        if current_branch is not None:
            result[current_path] = current_branch
        current_path = None
        current_head = None
        current_branch = None
        current_detached = False

    for line in output.splitlines() + [""]:
        if line.startswith("worktree "):
            finish_block()
            if not line[9:].strip():
                raise GitAnalysisError("git worktree output has an empty path")
            current_path = str(Path(line[9:]).resolve())
        elif line.startswith("HEAD ") and current_path:
            current_head = line[5:].strip()
        elif line.startswith("branch refs/") and current_path:
            current_branch = _logical_branch_name(line[len("branch ") :])
        elif line == "detached" and current_path:
            current_detached = True
        elif line in {"bare", "locked"} or line.startswith("locked ") or line.startswith("prunable "):
            continue
        elif line.strip():
            raise GitAnalysisError("git worktree output is malformed")
        elif not line.strip():
            finish_block()
    return result


def git_common_dir(worktree: Path) -> Path:
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


def path_is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def is_ancestor(repo_root: Path, ancestor: str, descendant: str, *, timeout: float) -> bool:
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", ancestor) or not re.fullmatch(
        r"[0-9a-fA-F]{40,64}", descendant
    ):
        raise GitAnalysisError("branch head is not a valid Git object id")
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", ancestor, descendant],
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
