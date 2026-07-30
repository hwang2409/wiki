"""Worker registry and branch discovery with source attestations."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterable

from .blast_radius_git import (
    GitAnalysisError,
    git_common_dir,
    is_ancestor,
    path_is_within,
    refs as git_refs,
    worktree_branches,
)
from .blast_radius_provider import _logical_branch_name
from .blast_radius_types import (
    ActiveBranch,
    ANALYSIS_TIMEOUT_SECONDS,
    BranchRef,
    DiscoveryResult,
    GIT_TIMEOUT_SECONDS,
    MAX_ACTIVE_BRANCHES,
    OpenPRSnapshotState,
    SourceAttestation,
)


def failed(branch: str, reason: str) -> dict[str, str]:
    return {"branch": branch, "reason": reason[:200]}


def registry_rows(registry: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    for ticket, entry in registry.items():
        if not isinstance(ticket, str) or ticket.startswith("_") or not isinstance(entry, dict):
            continue
        current = entry.get("current")
        if isinstance(current, dict):
            yield ticket, current


def registry_attestations(registry: object) -> list[SourceAttestation]:
    if not isinstance(registry, dict):
        return [SourceAttestation("registry", False, False, True, "registry must be an object")]
    attestations: list[SourceAttestation] = []
    for ticket, entry in registry.items():
        source = f"registry-row:{ticket}"
        if isinstance(ticket, str) and ticket.startswith("_"):
            valid = ticket != "_orchestrators" or entry is None or isinstance(entry, dict)
            attestations.append(SourceAttestation(source, valid, valid, True, None if valid else "invalid registry object"))
            continue
        if not isinstance(ticket, str) or not isinstance(entry, dict):
            attestations.append(SourceAttestation(source, False, False, True, "registry row is malformed"))
            continue
        current = entry.get("current")
        if current is None:
            attestations.append(SourceAttestation(source, False, True, True, "registry row has no current entry"))
            continue
        if not isinstance(current, dict):
            attestations.append(SourceAttestation(source, False, False, True, "current entry is malformed"))
            continue
        has_locator = any(
            isinstance(current.get(key), str) and current[key].strip()
            for key in ("branch", "branch_name", "pr_branch", "head_ref_name", "worktree", "cwd")
        )
        attestations.append(
            SourceAttestation(
                source,
                has_locator,
                True,
                True,
                None if has_locator else "registry row has no branch or worktree locator",
            )
        )
    return attestations


def registry_shape_failures(registry: object) -> list[dict[str, str]]:
    return [
        failed(attestation.source.removeprefix("registry-row:"), attestation.reason or "registry row is malformed")
        for attestation in registry_attestations(registry)
        if not attestation.valid
    ]


def branch_hint(current: dict[str, Any]) -> str | None:
    for key in ("branch", "branch_name", "pr_branch", "head_ref_name"):
        value = current.get(key)
        if isinstance(value, str) and value.strip():
            return _logical_branch_name(value.strip())
    return None


def ticket_for_branch(branch: str, registry: dict[str, Any]) -> str | None:
    lowered = _logical_branch_name(branch).lower()
    for ticket, current in registry_rows(registry):
        hint = branch_hint(current)
        if hint and hint.lower() == lowered:
            return ticket
        if ticket.lower() == lowered or ticket.lower().replace("-", "/") == lowered:
            return ticket
    return None


def ref_candidates(refs: dict[str, BranchRef], branch: str) -> list[BranchRef]:
    logical = _logical_branch_name(branch)
    ordered: list[BranchRef] = []
    for name in (logical, f"origin/{logical}"):
        if name in refs:
            ordered.append(refs[name])
    ordered.extend(ref for name, ref in refs.items() if _logical_branch_name(name) == logical and ref not in ordered)
    return ordered


def select_ref(refs: dict[str, BranchRef], branch: str, expected_sha: str | None = None) -> tuple[BranchRef | None, str | None]:
    candidates = [ref for ref in ref_candidates(refs, branch) if _logical_branch_name(ref.name) != "main"]
    if not candidates:
        return None, "branch ref is not present in fetched refs"
    if expected_sha:
        matching = [ref for ref in candidates if ref.head_sha == expected_sha]
        if not matching:
            return None, "fetched branch head does not match the open PR head"
        return matching[0], None
    return candidates[0], None


def add_selected(selected: dict[str, list[ActiveBranch]], branch: ActiveBranch) -> None:
    selected.setdefault(branch.name, [])
    if branch not in selected[branch.name]:
        selected[branch.name].append(branch)


def reconcile_heads(repo_root: Path, candidates: dict[str, list[ActiveBranch]], *, deadline: float) -> tuple[dict[str, ActiveBranch], list[dict[str, str]], list[SourceAttestation]]:
    selected: dict[str, ActiveBranch] = {}
    failures: list[dict[str, str]] = []
    attestations: list[SourceAttestation] = []
    for name, branches in candidates.items():
        pr = next((branch for branch in branches if branch.source == "pr"), None)
        workers = [branch for branch in branches if branch.source == "worker"]
        if pr is None:
            if len({branch.head_sha for branch in workers}) > 1:
                reason = "worker branch heads do not agree"
                failures.append(failed(name, reason))
                attestations.append(SourceAttestation(f"heads:{name}", False, True, True, reason))
            elif workers:
                selected[name] = workers[0]
                attestations.append(SourceAttestation(f"heads:{name}", True, True, True))
            continue
        winner = pr
        for worker in workers:
            if worker.head_sha == winner.head_sha:
                continue
            try:
                worker_descendant = is_ancestor(repo_root, winner.head_sha, worker.head_sha, timeout=min(GIT_TIMEOUT_SECONDS, max(0.05, deadline - time.monotonic())))
                winner_descendant = is_ancestor(repo_root, worker.head_sha, winner.head_sha, timeout=min(GIT_TIMEOUT_SECONDS, max(0.05, deadline - time.monotonic())))
            except GitAnalysisError as exc:
                reason = f"cannot compare PR and worker branch heads: {exc}"
                failures.append(failed(name, reason))
                attestations.append(SourceAttestation(f"heads:{name}", False, True, False, reason))
                winner = None
                break
            if worker_descendant and not winner_descendant:
                winner = worker
            elif winner_descendant and not worker_descendant:
                continue
            else:
                reason = "PR and worker branch heads are mismatched or unrelated"
                failures.append(failed(name, reason))
                attestations.append(SourceAttestation(f"heads:{name}", False, True, True, reason))
                winner = None
                break
        if winner is not None:
            selected[name] = winner
            attestations.append(SourceAttestation(f"heads:{name}", True, True, True))
    return selected, failures, attestations


def discover_active_branch_result(
    repo_root: Path,
    registry: dict[str, Any],
    *,
    refs: dict[str, BranchRef],
    pr_snapshot: OpenPRSnapshotState,
    deadline: float,
    max_active_branches: int = MAX_ACTIVE_BRANCHES,
) -> DiscoveryResult:
    failures = registry_shape_failures(registry)
    attestations = registry_attestations(registry)
    candidates: dict[str, list[ActiveBranch]] = {}
    if pr_snapshot.complete:
        for branch in pr_snapshot.branches:
            attestations.append(branch.attestation)
        for pr_branch in pr_snapshot.branches[:max_active_branches]:
            ref, reason = select_ref(refs, pr_branch.name, pr_branch.head_sha)
            if ref is None:
                failures.append(failed(pr_branch.name, reason or "branch ref unavailable"))
                attestations.append(SourceAttestation(f"pr-ref:{pr_branch.name}", False, True, True, reason))
                continue
            add_selected(candidates, ActiveBranch(_logical_branch_name(ref.name), ref.ref, ref.head_sha, "pr", pr_branch.ticket or ticket_for_branch(pr_branch.name, registry)))
        if len(pr_snapshot.branches) > max_active_branches:
            count = len(pr_snapshot.branches) - max_active_branches
            reason = f"open PR branch list truncated; dropped {count} branches"
            failures.append(failed("open PR branches", reason))
            attestations.append(SourceAttestation("pr-branch-limit", False, True, True, reason))

    try:
        worktrees = worktree_branches(repo_root, timeout=min(GIT_TIMEOUT_SECONDS, max(0.05, deadline - time.monotonic())))
        attestations.append(SourceAttestation("worktree-list", True, True, True))
    except GitAnalysisError as exc:
        worktrees = {}
        failures.append(failed("registered worker worktrees", str(exc)))
        attestations.append(SourceAttestation("worktree-list", False, False, False, str(exc)))

    try:
        primary_root = repo_root.resolve()
        primary_common = git_common_dir(repo_root)
        attestations.append(SourceAttestation("git-common-dir", True, True, True))
    except GitAnalysisError as exc:
        primary_root = repo_root
        primary_common = None
        failures.append(failed("git common directory", str(exc)))
        attestations.append(SourceAttestation("git-common-dir", False, False, False, str(exc)))

    registered_paths: dict[str, tuple[str, str | None]] = {}
    for ticket, current in registry_rows(registry):
        if current.get("role") == "orchestrator":
            continue
        eligible_for_hint = True
        raw_worktree = current.get("worktree") or current.get("cwd")
        if isinstance(raw_worktree, str) and raw_worktree.strip():
            path = Path(raw_worktree).expanduser().resolve()
            local_path = path_is_within(path, primary_root)
            if primary_common is None:
                eligible_for_hint = False
                continue
            try:
                worker_common = git_common_dir(path)
            except GitAnalysisError:
                if local_path and current.get("role") != "review":
                    reason = f"registered worker worktree is unreadable: {path}"
                    failures.append(failed(ticket, reason))
                    attestations.append(SourceAttestation(f"worktree:{ticket}", False, False, True, reason))
                eligible_for_hint = False
                continue
            if worker_common != primary_common:
                eligible_for_hint = False
                continue
            registered_paths[str(path)] = (ticket, current.get("role"))
            if current.get("role") == "review" and str(path) not in worktrees:
                eligible_for_hint = False
        elif raw_worktree is not None:
            eligible_for_hint = False
        if eligible_for_hint:
            hint = branch_hint(current)
            if hint:
                ref, reason = select_ref(refs, hint)
                if ref is None:
                    failures.append(failed(hint, reason or "registered branch ref unavailable"))
                    attestations.append(SourceAttestation(f"worker-ref:{ticket}", False, True, True, reason))
                else:
                    add_selected(candidates, ActiveBranch(_logical_branch_name(ref.name), ref.ref, ref.head_sha, "worker", ticket))

    for path, (ticket, role) in registered_paths.items():
        branch_name = worktrees.get(path)
        if not branch_name:
            if role == "review":
                continue
            reason = "registered worker branch is not available"
            failures.append(failed(ticket, reason))
            attestations.append(SourceAttestation(f"worktree:{ticket}", False, True, True, reason))
            continue
        ref, reason = select_ref(refs, branch_name)
        if ref is None:
            failures.append(failed(branch_name, reason or "registered branch ref unavailable"))
            attestations.append(SourceAttestation(f"worker-ref:{ticket}", False, True, True, reason))
            continue
        add_selected(candidates, ActiveBranch(_logical_branch_name(ref.name), ref.ref, ref.head_sha, "worker", ticket))

    reconciled, head_failures, head_attestations = reconcile_heads(repo_root, candidates, deadline=deadline)
    failures.extend(head_failures)
    attestations.extend(head_attestations)
    all_branches = sorted(reconciled.values(), key=lambda branch: (branch.source != "worker", branch.name))
    if len(all_branches) > max_active_branches:
        count = len(all_branches) - max_active_branches
        reason = f"active branch list truncated; dropped {count} branches"
        failures.append(failed("active branches", reason))
        attestations.append(SourceAttestation("active-branch-limit", False, True, True, reason))
    return DiscoveryResult(
        tuple(all_branches[:max_active_branches]),
        tuple(failures),
        pr_snapshot.complete,
        tuple(attestations),
    )


def discover_active_branches(repo_root: Path, registry: dict[str, Any], *, refs: dict[str, BranchRef] | None = None, deadline: float | None = None, pr_snapshot: OpenPRSnapshotState | None = None) -> list[ActiveBranch]:
    return list(discover_active_branch_result(repo_root, registry, refs=refs or git_refs(repo_root, timeout=GIT_TIMEOUT_SECONDS), pr_snapshot=pr_snapshot or OpenPRSnapshotState((), False), deadline=deadline or time.monotonic() + ANALYSIS_TIMEOUT_SECONDS).branches)
