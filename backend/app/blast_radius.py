"""Attested branch collision analysis for the pre-spawn view."""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from .blast_radius_cache import DIFF_CACHE, DiffCache
from .blast_radius_discovery import (
    attest_registry,
    branch_hint as _branch_hint,
    discover_active_branch_result,
    discover_active_branches,
    failed as _failed,
    registry_rows as _registry_rows,
    registry_shape_failures as _registry_shape_failures,
    select_ref as _select_ref,
    ticket_for_branch as _ticket_for_branch,
)
from .blast_radius_git import GitAnalysisError, _run_git, parse_refs_output as _parse_refs_output, resolve_main_ref as _resolve_main_ref
from .blast_radius_provider import OPEN_PR_SNAPSHOT, OpenPRSnapshot, _default_open_pr_provider
from .blast_radius_types import (
    ANALYSIS_TIMEOUT_SECONDS,
    ActiveBranch,
    AttestationLedger,
    Attested,
    BranchFiles,
    BranchRef,
    ChangedFiles,
    GIT_TIMEOUT_SECONDS,
    MAX_ACTIVE_BRANCHES,
    MAX_CHANGED_FILES,
    OPEN_PR_MAX_AGE_SECONDS,
    OpenPRBranch,
    OpenPRSnapshotState,
    SourceAttestation,
)


def _refs(repo_root: Path, *, timeout: float) -> Attested[dict[str, BranchRef]]:
    """Keep ref reads patchable while parsing stays in the Git module."""

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
    refs = _parse_refs_output(output)
    return Attested(refs, "git-refs", True, True, True)


def changed_files(
    repo_root: Path,
    branch: BranchRef | ActiveBranch,
    *,
    main_ref: BranchRef | None = None,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> ChangedFiles:
    if main_ref is None:
        refs = _refs(repo_root, timeout=timeout)
        main_ref = _resolve_main_ref(refs.value or {})
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
        attestation=SourceAttestation(
            f"git-diff:{branch.name}",
            not dropped_count,
            True,
            True,
            f"dropped {dropped_count} files" if dropped_count else None,
        ),
    )


def collision_pairs(branches: Iterable[BranchFiles]) -> list[dict[str, Any]]:
    rows = sorted(branches, key=lambda item: item.branch)
    collisions: list[dict[str, Any]] = []
    for index, left in enumerate(rows):
        left_files = set(left.files)
        for right in rows[index + 1 :]:
            overlap = sorted(left_files.intersection(set(right.files)))
            if overlap:
                collisions.append(
                    {
                        "left": left.branch,
                        "right": right.branch,
                        "overlap": overlap,
                        "overlap_count": len(overlap),
                    }
                )
    return sorted(collisions, key=lambda item: (-int(item["overlap_count"]), item["left"], item["right"]))


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
    return bool(
        normalized
        and (
            branch.name.lower() == normalized
            or branch.ref.lower() == candidate.strip().lower()
            or (branch.ticket and branch.ticket.lower() == normalized)
        )
    )


def _candidate_ref(candidate: str, refs: dict[str, BranchRef], active: Sequence[ActiveBranch], registry: dict[str, Any]) -> BranchRef | None:
    found = next((branch for branch in active if _candidate_matches(candidate, branch)), None)
    if found is not None:
        return BranchRef(found.name, found.ref, found.head)
    normalized = candidate.strip().lower()
    if not normalized or normalized == "all":
        return None
    ref, _reason = _select_ref(refs, normalized)
    if ref is not None:
        return ref
    for ticket, current in _registry_rows(registry):
        if ticket.lower() == normalized:
            hint = _branch_hint(current)
            if hint:
                ref, _reason = _select_ref(refs, hint)
                return ref
    return None


def _invalid(source: str, reason: str, *, shape_valid: bool = True, fresh: bool = False) -> Attested[Any]:
    return Attested(None, source, False, shape_valid, fresh, reason)


def _response(
    *,
    candidate: str,
    candidate_found: bool | None,
    branches: list[dict[str, Any]],
    collisions: list[dict[str, Any]],
    failed_branches: list[dict[str, str]],
    inputs: Iterable[Attested[Any]],
    expected_input_count: int,
    refreshed_at: float | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    deduped_failures: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for failure in failed_branches:
        key = (failure.get("branch", ""), failure.get("reason", ""))
        if key not in seen:
            seen.add(key)
            deduped_failures.append(failure)
    ledger = AttestationLedger(tuple(inputs), expected_input_count)
    payload: dict[str, Any] = {
        "candidate": candidate,
        "candidate_found": candidate_found,
        "complete": ledger.complete,
        "failed_branches": deduped_failures,
        "branches": branches,
        "collisions": collisions,
        "risk": _risk_summary(collisions) if ledger.complete else None,
        "refreshed_at": refreshed_at,
        "snapshot_max_age_seconds": OPEN_PR_MAX_AGE_SECONDS,
        "attestation": ledger.as_dict(),
    }
    if error:
        payload["error"] = error
    return payload


def analyze(
    repo_root: Path,
    registry: object,
    candidate: str = "all",
    *,
    cache: DiffCache = DIFF_CACHE,
    pr_snapshot: OpenPRSnapshot | OpenPRSnapshotState | None = None,
    registry_error: str | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + ANALYSIS_TIMEOUT_SECONDS
    snapshot_state = pr_snapshot.read() if isinstance(pr_snapshot, OpenPRSnapshot) else pr_snapshot or OPEN_PR_SNAPSHOT.read()
    snapshot = snapshot_state.snapshot
    snapshot_age = None if snapshot_state.refreshed_at is None else max(0.0, time.time() - snapshot_state.refreshed_at)
    snapshot_fresh = snapshot_age is not None and snapshot_age <= OPEN_PR_MAX_AGE_SECONDS
    snapshot_input = Attested(
        snapshot.value,
        snapshot.source,
        snapshot.ok,
        snapshot.shape_valid,
        snapshot.fresh and snapshot_fresh,
        snapshot.reason or ("stale" if not snapshot_fresh else None),
    )
    registry_input = registry if isinstance(registry, Attested) else attest_registry(registry)
    if registry_error:
        registry_input = _invalid("registry-input", registry_error, shape_valid=True)
    registry_value = registry_input.value if registry_input.valid and isinstance(registry_input.value, dict) else {}
    inputs: list[Attested[Any]] = [snapshot_input, registry_input]
    failures: list[dict[str, str]] = []
    if registry_error:
        failures.append(_failed("agent registry", registry_error))
    expected_input_count = len(inputs)
    inputs.extend(branch.head for branch in snapshot_state.branches)
    expected_input_count += len(snapshot_state.branches)

    try:
        refs_input = _refs(repo_root, timeout=GIT_TIMEOUT_SECONDS)
        refs = refs_input.value or {}
        inputs.append(refs_input)
        expected_input_count += 1
    except GitAnalysisError as exc:
        reason = str(exc)
        inputs.append(_invalid("git-refs", reason, shape_valid=False))
        expected_input_count += 1
        return _response(
            candidate=candidate,
            candidate_found=None if candidate.strip().lower() in {"", "all"} else False,
            branches=[],
            collisions=[],
            failed_branches=[_failed("git refs", reason)],
            inputs=inputs,
            expected_input_count=expected_input_count,
            error="branch refs are unavailable",
        )

    main_ref = _resolve_main_ref(refs)
    if main_ref is None:
        reason = "main ref is not present in fetched refs"
        inputs.append(_invalid("main-ref", reason))
        expected_input_count += 1
        return _response(
            candidate=candidate,
            candidate_found=None if candidate.strip().lower() in {"", "all"} else False,
            branches=[],
            collisions=[],
            failed_branches=[_failed("main", reason)],
            inputs=inputs,
            expected_input_count=expected_input_count,
            error="main ref is unavailable",
        )
    inputs.append(Attested(main_ref, "main-ref", True, True, True))
    inputs.append(main_ref.head)
    expected_input_count += 2

    discovery = discover_active_branch_result(
        repo_root,
        registry_value,
        refs=refs,
        pr_snapshot=snapshot_state,
        deadline=deadline,
        max_active_branches=MAX_ACTIVE_BRANCHES,
    )
    failures.extend([*(_registry_shape_failures(registry_value)), *discovery.failed_branches])
    inputs.extend(discovery.inputs)
    expected_input_count += discovery.expected_input_count
    inputs.append(discovery.branches)
    expected_input_count += 1
    if not snapshot_input.valid:
        failures.append(_failed("open PR snapshot", snapshot_state.error or snapshot_input.reason or "snapshot is incomplete"))

    candidate_found: bool | None = None
    candidate_branch: ActiveBranch | None = None
    if candidate.strip().lower() not in {"", "all"}:
        candidate_ref = _candidate_ref(candidate, refs, discovery.branches.value or (), registry_value)
        if candidate_ref is None:
            candidate_found = False
            reason = "candidate branch was not found in fetched refs"
            failures.append(_failed(candidate, reason))
            inputs.append(_invalid("candidate", reason))
            expected_input_count += 1
        else:
            candidate_found = True
            candidate_branch = ActiveBranch(candidate_ref.name, candidate_ref.ref, candidate_ref.head, "candidate", _ticket_for_branch(candidate_ref.name, registry_value))
            inputs.append(Attested(candidate_branch, "candidate", True, True, True))
            inputs.append(candidate_branch.head)
            expected_input_count += 2

    selected = list(discovery.branches.value or ())
    if candidate_branch is not None:
        selected = [candidate_branch, *[branch for branch in selected if branch.name != candidate_branch.name]]
    candidate_head = candidate_branch.head_sha if candidate_branch else ""
    rows: list[BranchFiles] = []
    branch_payload: list[dict[str, Any]] = []
    for index, branch in enumerate(selected):
        if time.monotonic() >= deadline:
            remaining = selected[index:]
            for missing in remaining:
                inputs.extend((_invalid(f"cache-entry:{missing.name}", "analysis deadline exceeded"), _invalid(f"git-diff:{missing.name}", "analysis deadline exceeded")))
            expected_input_count += 2 * len(remaining)
            failures.extend(_failed(remaining_branch.name, "analysis deadline exceeded") for remaining_branch in remaining)
            break
        expected_input_count += 2
        try:
            raw_cached = cache.get_or_compute(
                branch.name,
                branch.head_sha,
                lambda branch=branch: changed_files(repo_root, branch, main_ref=main_ref, timeout=min(GIT_TIMEOUT_SECONDS, max(0.05, deadline - time.monotonic()))),
                candidate_head_sha=candidate_head,
                main_head_sha=main_ref.head_sha,
            )
            expected_cache_source = f"cache-entry:{branch.name}"
            if isinstance(raw_cached, Attested) and isinstance(raw_cached.value, ChangedFiles):
                cache_input = Attested(raw_cached.value, expected_cache_source, raw_cached.ok, raw_cached.shape_valid, raw_cached.fresh, raw_cached.reason)
                diff_input = raw_cached.value
            elif isinstance(raw_cached, ChangedFiles):
                cache_input = _invalid(expected_cache_source, "cache entry attestation was dropped")
                diff_input = raw_cached
            else:
                cache_input = _invalid(expected_cache_source, "cache entry is not attested")
                diff_input = _invalid(f"git-diff:{branch.name}", "diff value is not attested")
            inputs.extend((cache_input, diff_input))
            if not cache_input.valid or not diff_input.valid:
                failures.append(_failed(branch.name, cache_input.reason or diff_input.reason or "diff or cache entry is unattested"))
                continue
            rows.append(BranchFiles(branch.name, branch.head_sha, diff_input.value or ()))
            branch_payload.append({"branch": branch.name, "ticket": branch.ticket, "source": branch.source, "head_sha": branch.head_sha, "files": list(diff_input.value or ()), "file_count": len(diff_input.value or ())})
        except BaseException as exc:
            reason = str(exc)
            inputs.extend((_invalid(f"cache-entry:{branch.name}", reason), _invalid(f"git-diff:{branch.name}", reason, shape_valid=False)))
            failures.append(_failed(branch.name, reason))

    collisions = collision_pairs(rows)
    if candidate_branch:
        collisions = [collision for collision in collisions if candidate_branch.name in {collision["left"], collision["right"]}]
    return _response(
        candidate=candidate,
        candidate_found=candidate_found,
        branches=branch_payload,
        collisions=collisions,
        failed_branches=failures,
        inputs=inputs,
        expected_input_count=expected_input_count,
        refreshed_at=snapshot_state.refreshed_at,
    )
