"""One-shot merge-ready reviewer orchestration.

This module deliberately keeps the individual agent-operation contracts in
``main``.  ``next_review`` only chooses the next reviewer identity and wires
the existing gate, worktree, spawn, and archive operations together.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Mapping
import json
from pathlib import Path
from typing import Any
from uuid import uuid4


_REVIEWER_ID = re.compile(r"^(?P<ticket>[A-Z0-9-]+)-REVIEW(?P<round>[1-9][0-9]*)$")
_TERMINAL_STATES = {
    "abandoned",
    "closed",
    "completed",
    "dead",
    "failed",
    "merge-ready",
    "stopped",
    "terminal",
}
_REQUEST_LOCK = threading.RLock()
_REQUEST_RESULTS: dict[str, dict[str, Any]] = {}
_REQUEST_STAGES: dict[str, dict[str, Any]] = {}
_REQUEST_STATE_LOADED = False


def _main() -> Any:
    # main imports the FastAPI application and cannot import this module at
    # module load time without creating a cycle.
    from .. import main

    return main


def _request_state_path() -> Path:
    return Path(_main().AGENT_RUNTIME_DIR) / "next-review-operations.json"


def _load_request_state() -> None:
    global _REQUEST_STATE_LOADED
    if _REQUEST_STATE_LOADED:
        return
    _REQUEST_STATE_LOADED = True
    try:
        payload = json.loads(_request_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(payload, Mapping):
        return
    completed = payload.get("completed")
    staged = payload.get("staged")
    if isinstance(completed, Mapping):
        _REQUEST_RESULTS.update(
            {key: dict(value) for key, value in completed.items() if isinstance(key, str) and isinstance(value, Mapping)}
        )
    if isinstance(staged, Mapping):
        _REQUEST_STAGES.update(
            {key: dict(value) for key, value in staged.items() if isinstance(key, str) and isinstance(value, Mapping)}
        )


def _persist_request_state() -> None:
    path = _request_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {"completed": _REQUEST_RESULTS, "staged": _REQUEST_STAGES},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def _resolve_root(orch: str) -> Path:
    return _main()._resolve_orchestrator_root(orch)  # noqa: SLF001


def _worktree(
    *,
    repo_root: Path,
    ticket: str,
    round_number: int,
    expected_sha: str,
) -> Path:
    """Create or validate the review worktree at the requested commit."""

    slug = ticket.lower()
    worktree = (repo_root / ".codex" / "worktrees" / f"{slug}-review{round_number}").resolve()
    return _main().provision_pinned_worktree(repo_root, worktree, expected_sha)


def _archived_reviewers(ticket: str, archived: list[Mapping[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in archived:
        value = row.get("ticket")
        if not isinstance(value, str):
            continue
        match = _REVIEWER_ID.fullmatch(value.upper())
        if match and match.group("ticket") == ticket.upper():
            result[value.upper()] = int(match.group("round"))
    return result


def _next_round(
    ticket: str,
    archived: list[Mapping[str, Any]],
    registry: Mapping[str, Any],
) -> int:
    reviewers = _archived_reviewers(ticket, archived)
    for value in registry:
        match = _REVIEWER_ID.fullmatch(str(value).upper())
        if match and match.group("ticket") == ticket.upper():
            reviewers[str(value).upper()] = int(match.group("round"))
    return max(reviewers.values(), default=0) + 1


def _previous_terminal_reviewer(
    ticket: str,
    round_number: int,
    registry: Mapping[str, Any],
) -> str | None:
    candidates: list[tuple[int, str, Mapping[str, Any]]] = []
    for value, entry in registry.items():
        match = _REVIEWER_ID.fullmatch(str(value).upper())
        if not match or match.group("ticket") != ticket.upper() or not isinstance(entry, Mapping):
            continue
        current = entry.get("current")
        if not isinstance(current, Mapping):
            continue
        state = str(current.get("state") or current.get("runtime_state") or "").lower()
        if state in _TERMINAL_STATES:
            candidates.append((int(match.group("round")), str(value).upper(), current))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    previous_round, reviewer_id, _ = candidates[0]
    return reviewer_id if previous_round < round_number else None


def _default_prompt(
    *,
    ticket: str,
    reviewer_id: str,
    pr_number: int,
    expected_sha: str,
    round_number: int,
    previous_reviewer: str | None,
) -> str:
    prior = previous_reviewer or "none (first review round)"
    return f"""review PR #{pr_number} for {ticket}.

reviewer: {reviewer_id}
round: {round_number}
pinned sha: {expected_sha}
prior reviewer: {prior}

inspect the pinned worktree, identify actionable correctness, security, reliability,
and test issues, and report findings with file and line references. if the diff is
clean, report that explicitly. follow the repository review protocol and do not
modify the worktree.
"""


def _build_prompt(
    template: str | None,
    *,
    ticket: str,
    reviewer_id: str,
    pr_number: int,
    expected_sha: str,
    round_number: int,
    previous_reviewer: str | None,
) -> str:
    if not template:
        return _default_prompt(
            ticket=ticket,
            reviewer_id=reviewer_id,
            pr_number=pr_number,
            expected_sha=expected_sha,
            round_number=round_number,
            previous_reviewer=previous_reviewer,
        )
    values = {
        "ticket": ticket,
        "reviewer": reviewer_id,
        "pr_number": pr_number,
        "expected_sha": expected_sha,
        "round": round_number,
        "prior_reviewer": previous_reviewer or "none",
    }
    try:
        rendered = template.format_map(values)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"invalid prompt_template: {exc}") from exc
    context = (
        f"\n\nreview context: ticket={ticket}, pr=#{pr_number}, pinned_sha={expected_sha}, "
        f"round={round_number}, prior_reviewer={previous_reviewer or 'none'}"
    )
    return rendered + context


def next_review(
    ticket: str,
    pr_number: int,
    expected_sha: str,
    reviewer_kind: str = "cdx",
    reviewer_model: str = "gpt-5.6-sol",
    reviewer_effort: str | None = None,
    prompt_template: str | None = None,
    request_id: str | None = None,
    *,
    orch: str | None = None,
    caller_orch: str | None = None,
    gate: Callable[[int, str], Mapping[str, Any]] | None = None,
    resolve_root: Callable[[str], Path] | None = None,
    worktree: Callable[..., Path] | None = None,
    spawn: Callable[..., Mapping[str, Any]] | None = None,
    archive: Callable[[str], Mapping[str, Any]] | None = None,
    archived: Callable[[], list[Mapping[str, Any]]] | None = None,
    registry: Callable[[], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Gate and start the next pinned reviewer, replaying request ids."""

    orch = orch or caller_orch
    if not orch:
        raise ValueError("orch is required")
    if not re.fullmatch(r"[A-Z0-9-]+", ticket):
        raise ValueError("ticket must contain only uppercase letters, numbers, or dashes")
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", expected_sha):
        raise ValueError("expected_sha must be a git commit sha")
    if reviewer_kind not in {"cc", "cdx"}:
        raise ValueError("reviewer_kind must be cc or cdx")
    if reviewer_kind == "cdx":
        reviewer_effort = reviewer_effort or "high"
    elif reviewer_effort is not None:
        raise ValueError("Claude reviewers do not accept reasoning effort")
    if reviewer_kind == "cdx" and reviewer_effort not in {
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    }:
        raise ValueError("reviewer_effort is invalid")
    request_id = request_id or str(uuid4())
    with _REQUEST_LOCK:
        _load_request_state()
        previous_result = _REQUEST_RESULTS.get(request_id)
        if previous_result is not None:
            return dict(previous_result)

        staged = _REQUEST_STAGES.get(request_id)
        if staged is not None:
            main = _main()
            if (
                staged.get("previous_reviewer") is not None
                and not staged.get("archive_completed", False)
            ):
                if archive is not None:
                    archive_result = archive(staged["previous_reviewer"])
                else:
                    archive_result = main.archive_agent(
                        staged["previous_reviewer"], main.AgentArchiveIn(outcome="closed")
                    )
                staged["archive_completed"] = True
                staged["archive_result"] = dict(archive_result)
                _persist_request_state()
            result = _staged_result(staged, staged.get("archive_result"))
            _REQUEST_RESULTS[request_id] = dict(result)
            _REQUEST_STAGES.pop(request_id, None)
            _persist_request_state()
            return result

        main = _main()
        try:
            if gate is not None:
                verdict = gate(pr_number, expected_sha)
            else:
                verdict = main.composer_gate(
                    main.ComposerGateIn(pr=str(pr_number), expect_sha=expected_sha)
                )
        except Exception as exc:  # gate failures are a normal branch of this API
            return {"status": "gate_failed", "detail": str(exc)}
        if not isinstance(verdict, Mapping) or (
            verdict.get("verdict") != "pass" and verdict.get("ready") is not True
        ):
            detail = (
                verdict.get("summary")
                if isinstance(verdict, Mapping)
                else "gate returned an invalid verdict"
            ) or "merge-ready gate failed"
            return {"status": "gate_failed", "detail": str(detail)}

        if archived is None:
            archived_rows = list(main.list_archived(limit=None))
        else:
            archived_rows = list(archived())
        registry_data = dict((registry or main._read_agent_registry)())  # noqa: SLF001
        round_number = _next_round(ticket, archived_rows, registry_data)
        reviewer_id = f"{ticket.upper()}-REVIEW{round_number}"
        previous_reviewer = _previous_terminal_reviewer(
            ticket, round_number, registry_data
        )
        repo_root = (resolve_root or _resolve_root)(orch)
        worktree_path = (worktree or _worktree)(
            repo_root=repo_root,
            ticket=ticket.upper(),
            round_number=round_number,
            expected_sha=expected_sha,
        )
        prompt = _build_prompt(
            prompt_template,
            ticket=ticket.upper(),
            reviewer_id=reviewer_id,
            pr_number=pr_number,
            expected_sha=expected_sha,
            round_number=round_number,
            previous_reviewer=previous_reviewer,
        )
        spawn_args = main.SpawnWorkerIn(
            ticket=reviewer_id,
            kind=reviewer_kind,
            role="review",
            model=reviewer_model,
            effort=reviewer_effort,
            workdir=str(worktree_path),
            prompt=prompt,
            orch=orch,
            request_id=request_id,
        )
        spawn_result = spawn(spawn_args) if spawn is not None else main.spawn_agent(spawn_args)
        staged = {
            "status": "staged",
            "ticket": ticket.upper(),
            "reviewer": reviewer_id,
            "round": round_number,
            "run_id": spawn_result.get("run_id"),
            "worktree": str(worktree_path),
            "expected_sha": expected_sha,
            "orch": orch,
            "request_id": request_id,
            "previous_reviewer": previous_reviewer,
            "spawn_completed": True,
            "archive_completed": previous_reviewer is None,
        }
        _REQUEST_STAGES[request_id] = staged
        _persist_request_state()
        archived_result = None
        if previous_reviewer is not None:
            if archive is not None:
                archived_result = archive(previous_reviewer)
            else:
                archived_result = main.archive_agent(
                    previous_reviewer, main.AgentArchiveIn(outcome="closed")
                )
            staged["archive_completed"] = True
            staged["archive_result"] = dict(archived_result)
            _persist_request_state()

        result = _staged_result(staged, archived_result)
        _REQUEST_STAGES.pop(request_id, None)
        _REQUEST_RESULTS[request_id] = dict(result)
        _persist_request_state()
        return result


def _staged_result(
    staged: Mapping[str, Any], archive_result: Mapping[str, Any] | None
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "spawned",
        "ticket": staged["ticket"],
        "reviewer": staged["reviewer"],
        "round": staged["round"],
        "run_id": staged["run_id"],
        "worktree": staged["worktree"],
        "expected_sha": staged["expected_sha"],
        "orch": staged["orch"],
        "request_id": staged["request_id"],
    }
    if staged.get("previous_reviewer") is not None:
        result["archived_reviewer"] = staged["previous_reviewer"]
        result["archive"] = archive_result
    return result


__all__ = ["next_review"]
