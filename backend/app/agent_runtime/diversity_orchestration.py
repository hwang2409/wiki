"""Durable collection of parallel reviewer reports."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .reviewer_diversity import record_diverse_verdicts
from .ticket import parse_reviewer_id, reviewer_id as canonical_reviewer_id


def journal_path(runtime_dir: Path, ticket: str, round_number: int) -> Path:
    return runtime_dir / f"diversity-{ticket.upper()}-round{round_number}.json"


def write_journal(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(dict(payload), sort_keys=True, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def _canonical_journal_reviewer(value: Any) -> Any:
    identity = parse_reviewer_id(str(value))
    if identity is None or identity.lens is None:
        return value
    return canonical_reviewer_id(identity.ticket, identity.round, identity.lens)


def create_journal(
    runtime_dir: Path,
    *,
    ticket: str,
    round_number: int,
    expected_sha: str,
    expected_lenses: Sequence[str],
    reviewers: Mapping[str, str],
    orch: str | None = None,
    created_at: str | None = None,
) -> Path:
    path = journal_path(runtime_dir, ticket, round_number)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"diversity journal is unreadable: {path}") from exc
        expected_reviewers = {
            lens: _canonical_journal_reviewer(reviewer)
            for lens, reviewer in reviewers.items()
        }
        existing_reviewers = existing.get("reviewers") if isinstance(existing, Mapping) else None
        normalized_existing_reviewers = (
            {
                lens: _canonical_journal_reviewer(reviewer)
                for lens, reviewer in existing_reviewers.items()
            }
            if isinstance(existing_reviewers, Mapping)
            else existing_reviewers
        )
        expected = {
            "ticket": ticket.upper(),
            "round": round_number,
            "expected_sha": expected_sha.lower(),
            "expected_lenses": sorted(expected_lenses),
            "reviewers": dict(sorted(expected_reviewers.items())),
        }
        if not isinstance(existing, Mapping) or any(
            (normalized_existing_reviewers if key == "reviewers" else existing.get(key)) != value
            for key, value in expected.items()
        ):
            raise RuntimeError(f"diversity journal intent changed: {path}")
        return path
    canonical_reviewers = {
        lens: _canonical_journal_reviewer(reviewer)
        for lens, reviewer in reviewers.items()
    }
    write_journal(
        path,
        {
            "ticket": ticket.upper(),
            "round": round_number,
            "expected_sha": expected_sha.lower(),
            "expected_lenses": sorted(expected_lenses),
            "reviewers": dict(sorted(canonical_reviewers.items())),
            "orch": orch,
            "verdicts": {},
            "created_at": created_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "synthesis_state": "pending",
        },
    )
    return path


def _verdict_mapping(verdict: Any) -> dict[str, Any]:
    if isinstance(verdict, Mapping):
        return dict(verdict)
    findings = []
    raw_findings = getattr(verdict, "findings", ())
    if isinstance(raw_findings, Sequence) and not isinstance(raw_findings, (str, bytes)):
        for finding in raw_findings:
            if hasattr(finding, "to_dict"):
                findings.append(finding.to_dict())
            elif isinstance(finding, Mapping):
                findings.append(dict(finding))
            else:
                findings.append(finding)
    else:
        findings = raw_findings
    return {
        "worker": getattr(verdict, "worker", None),
        "state": getattr(verdict, "state", None),
        "source_sha": getattr(verdict, "source_sha", None),
        "findings": findings,
    }


def collect_diversity_verdict(
    *,
    runtime_dir: Path,
    ticket: str,
    reviewer: str,
    verdict: Any,
    expected_sha: str | None = None,
    record_verdict: Callable[..., Any] | None = None,
    status_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Store one report and synthesize only after the exact set is present."""

    identity = parse_reviewer_id(reviewer)
    if identity is None or identity.lens is None or identity.lens == "synthesis":
        return None
    reviewer = canonical_reviewer_id(identity.ticket, identity.round, identity.lens)
    path = journal_path(runtime_dir, ticket, identity.round)
    if not path.is_file():
        raise RuntimeError(f"diversity journal is missing: {path}")
    try:
        journal = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"diversity journal is unreadable: {path}") from exc
    if not isinstance(journal, dict):
        raise RuntimeError(f"diversity journal is invalid: {path}")
    expected_lenses = journal.get("expected_lenses")
    reviewers = journal.get("reviewers")
    if not isinstance(expected_lenses, list) or not isinstance(reviewers, Mapping):
        raise RuntimeError(f"diversity journal lacks its expected lens set: {path}")
    journal_reviewer = reviewers.get(identity.lens)
    journal_reviewer = _canonical_journal_reviewer(journal_reviewer)
    if identity.lens not in expected_lenses or journal_reviewer != reviewer:
        raise RuntimeError(f"reviewer is not in the diversity journal: {reviewer}")
    journal_sha = str(journal.get("expected_sha") or "").lower()
    caller_sha_mismatch = expected_sha is not None and str(expected_sha).lower() != journal_sha
    expected_sha = journal_sha
    payload = _verdict_mapping(verdict)
    source_sha = str(payload.get("source_sha") or payload.get("sha") or "").lower()
    sha_mismatch = source_sha != str(expected_sha).lower()
    malformed_findings = not isinstance(payload.get("findings"), list) or any(
        not isinstance(item, Mapping) for item in payload.get("findings", [])
    )
    if malformed_findings or str(payload.get("state") or "").upper() not in {
        "MERGE-READY", "NOT-MERGE-READY", "NO-GO", "INSUFFICIENT-CONTEXT"
    }:
        payload["state"] = "NOT-MERGE-READY"
        payload["findings"] = payload.get("findings") if isinstance(payload.get("findings"), list) else []
    if sha_mismatch or caller_sha_mismatch:
        payload["state"] = "NOT-MERGE-READY"
    verdicts = journal.get("verdicts")
    if not isinstance(verdicts, dict):
        raise RuntimeError(f"diversity journal has malformed reports: {path}")
    previous = verdicts.get(identity.lens)
    if previous is not None:
        previous_bytes = json.dumps(previous, sort_keys=True, separators=(",", ":"))
        payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if previous_bytes != payload_bytes:
            raise RuntimeError(f"conflicting duplicate diversity verdict for {reviewer}")
        if journal.get("synthesis_state") == "complete":
            return journal.get("synthesis") if isinstance(journal.get("synthesis"), dict) else None
        return {"status": "pending", "received": sorted(verdicts), "expected": sorted(expected_lenses)}
    verdicts[identity.lens] = payload
    journal["verdicts"] = dict(sorted(verdicts.items()))
    write_journal(path, journal)
    if tuple(sorted(verdicts)) != tuple(sorted(expected_lenses)):
        return {"status": "pending", "received": sorted(verdicts), "expected": sorted(expected_lenses)}
    if journal.get("synthesis_state") == "complete":
        return journal.get("synthesis") if isinstance(journal.get("synthesis"), dict) else None
    journal["synthesis_state"] = "recording"
    write_journal(path, journal)
    if record_verdict is None:
        from .. import main, workgraph_service

        def record_verdict(**record: Any) -> None:
            delivered = workgraph_service.record_verdict(
                **record,
                wait_for_delivery=True,
            )
            if delivered is not None:
                delivered.result(timeout=10)
        orch = journal.get("orch")
        graph = None
        if not isinstance(orch, str) or not orch:
            graph = main.workgraph.load_workgraph(ticket, status_dir or main.AGENT_STATUS_DIR)
            orch = graph.get("orch") if isinstance(graph, Mapping) else None
        if not isinstance(orch, str):
            raise RuntimeError("diversity synthesis has no orchestrator")
    else:
        orch = str(journal.get("orch") or "henry")
    synthesis = record_diverse_verdicts(
        ticket=ticket,
        expected_sha=str(expected_sha),
        verdicts={key: verdicts[key] for key in sorted(verdicts)},
        expected_lenses=expected_lenses,
        orch=orch,
        record_verdict=record_verdict,
        request_id=f"diversity-{ticket.upper()}-round{identity.round}",
        status_dir=status_dir,
        round_number=identity.round,
        created_at=str(journal["created_at"]),
    )
    journal["synthesis"] = synthesis
    journal["synthesis_state"] = "complete"
    write_journal(path, journal)
    return synthesis


def run_diverse_review(
    *,
    runtime: Any,
    staged: dict[str, Any] | None,
    lenses: tuple[str, ...],
    ticket: str,
    pr_number: int,
    expected_sha: str,
    reviewer_kind: str,
    reviewer_model: str,
    reviewer_effort: str | None,
    prompt_template: str | None,
    request_id: str,
    orch: str,
    main: Any,
    gate: Callable[[int, str], Mapping[str, Any]] | None,
    resolve_root: Callable[[str], Path] | None,
    worktree: Callable[..., Path] | None,
    spawn: Callable[..., Mapping[str, Any]] | None,
    archive: Callable[[str], Mapping[str, Any]] | None,
    archived: Callable[[], list[Mapping[str, Any]]] | None,
    registry: Callable[[], Mapping[str, Any]] | None,
    status_reader: Callable[[str], Mapping[str, Any] | None] | None,
    implicit_request_id: bool = False,
    backend_base_url: str | None = None,
) -> dict[str, Any]:
    """Stage, provision, spawn, and archive one complete lens fan-out."""

    if staged is None:
        try:
            verdict = gate(pr_number, expected_sha) if gate is not None else main.composer_gate(
                main.ComposerGateIn(pr=str(pr_number), expect_sha=expected_sha)
            )
        except Exception as exc:
            return {"status": "gate_failed", "detail": str(exc)}
        if not isinstance(verdict, Mapping) or (
            verdict.get("verdict") != "pass" and verdict.get("ready") is not True
        ):
            detail = verdict.get("summary") if isinstance(verdict, Mapping) else "gate returned an invalid verdict"
            return {"status": "gate_failed", "detail": str(detail or "merge-ready gate failed")}
        archived_rows = list(archived()) if archived is not None else list(main.list_archived(limit=None))
        registry_data = dict((registry or main._read_agent_registry)())  # noqa: SLF001
        round_number = runtime._next_round(ticket, archived_rows, registry_data)
        prior = runtime._previous_terminal_reviewers(ticket, round_number, registry_data, status_reader)
        repo_root = (resolve_root or runtime._resolve_root)(orch)
        reviewers: dict[str, dict[str, Any]] = {}
        for lens in lenses:
            reviewer = canonical_reviewer_id(ticket, round_number, lens)
            reviewers[lens] = {
                "reviewer": reviewer,
                "worktree": str(
                    (repo_root / ".codex" / "worktrees" / f"{ticket.lower()}-review{round_number}-{lens}").resolve()
                ),
                "prompt": runtime._build_prompt(
                    prompt_template,
                    ticket=ticket.upper(),
                    reviewer_id=reviewer,
                    pr_number=pr_number,
                    expected_sha=expected_sha,
                    round_number=round_number,
                    previous_reviewer=prior[0] if prior else None,
                    lens=lens,
                ),
                "worktree_provisioned": False,
                "spawn_completed": False,
                "run_id": None,
            }
        staged = {
            "status": "staged",
            "diversity": True,
            "ticket": ticket.upper(),
            "round": round_number,
            "lenses": list(lenses),
            "reviewers": reviewers,
            "previous_reviewers": prior,
            "archives": {reviewer: False for reviewer in prior},
            "repo_root": str(repo_root),
            "expected_sha": expected_sha,
            "orch": orch,
            "request_id": request_id,
            "reviewer_kind": reviewer_kind,
            "reviewer_model": reviewer_model,
            "reviewer_effort": reviewer_effort,
            "synthesis_created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        runtime._REQUEST_STAGES[request_id] = staged
        runtime._persist_request_state()
        create_journal(
            main.AGENT_RUNTIME_DIR,
            ticket=staged["ticket"],
            round_number=int(staged["round"]),
            expected_sha=staged["expected_sha"],
            expected_lenses=staged["lenses"],
            reviewers={lens: details["reviewer"] for lens, details in staged["reviewers"].items()},
            orch=staged["orch"],
            created_at=staged["synthesis_created_at"],
        )

    for lens in staged["lenses"]:
        details = staged["reviewers"][lens]
        if details.get("worktree_provisioned"):
            continue
        runtime._persist_request_state()
        path = runtime._diverse_worktree(
            repo_root=Path(staged["repo_root"]),
            ticket=staged["ticket"],
            round_number=int(staged["round"]),
            lens=lens,
            expected_sha=staged["expected_sha"],
            worktree=worktree,
        )
        details["worktree"] = str(path)
        details["worktree_provisioned"] = True
        runtime._persist_request_state()

    pending = [lens for lens in staged["lenses"] if not staged["reviewers"][lens].get("spawn_completed")]

    def spawn_one(lens: str) -> tuple[str, Mapping[str, Any]]:
        details = staged["reviewers"][lens]
        spawn_args = main.SpawnWorkerIn(
            ticket=details["reviewer"],
            kind=staged["reviewer_kind"],
            role="review",
            model=staged["reviewer_model"],
            effort=staged["reviewer_effort"],
            workdir=details["worktree"],
            prompt=details["prompt"],
            orch=staged["orch"],
            request_id=runtime._child_spawn_request_id(  # noqa: SLF001
                staged["request_id"], details["reviewer"]
            ),
            implicit_request_id=implicit_request_id,
        )
        result = (
            spawn(spawn_args)
            if spawn is not None
            else main.spawn_agent(spawn_args, backend_base_url=backend_base_url)
        )
        return lens, result

    with ThreadPoolExecutor(max_workers=len(pending) or 1) as executor:
        futures = [executor.submit(spawn_one, lens) for lens in pending]
        errors: list[Exception] = []
        for future in as_completed(futures):
            try:
                lens, result = future.result()
            except Exception as exc:
                errors.append(exc)
                continue
            staged["reviewers"][lens]["run_id"] = result.get("run_id")
            staged["reviewers"][lens]["spawn_completed"] = True
            runtime._persist_request_state()
        if errors:
            raise errors[0]

    for reviewer in staged["previous_reviewers"]:
        if staged["archives"].get(reviewer):
            continue
        if runtime._is_archived(reviewer, archived=archived, main=main):
            staged["archives"][reviewer] = True
            runtime._persist_request_state()
            continue
        archive_result = archive(reviewer) if archive is not None else main.archive_agent(
            reviewer, main.AgentArchiveIn(outcome="closed")
        )
        staged["archives"][reviewer] = {"result": dict(archive_result)}
        runtime._persist_request_state()

    result = runtime._diverse_result(staged)
    runtime._REQUEST_STAGES.pop(request_id, None)
    runtime._REQUEST_RESULTS[request_id] = dict(result)
    runtime._persist_request_state()
    return result


__all__ = ["collect_diversity_verdict", "create_journal", "journal_path", "run_diverse_review", "write_journal"]
