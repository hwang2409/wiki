"""Durable collection of parallel reviewer reports."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .reviewer_diversity import record_diverse_verdicts
from .ticket import parse_reviewer_id


def journal_path(runtime_dir: Path, ticket: str, round_number: int) -> Path:
    return runtime_dir / f"diversity-{ticket.upper()}-round{round_number}.json"


def write_journal(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(dict(payload), sort_keys=True, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


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
        expected = {
            "ticket": ticket.upper(),
            "round": round_number,
            "expected_sha": expected_sha.lower(),
            "expected_lenses": sorted(expected_lenses),
            "reviewers": dict(sorted(reviewers.items())),
        }
        if not isinstance(existing, Mapping) or any(existing.get(key) != value for key, value in expected.items()):
            raise RuntimeError(f"diversity journal intent changed: {path}")
        return path
    write_journal(
        path,
        {
            "ticket": ticket.upper(),
            "round": round_number,
            "expected_sha": expected_sha.lower(),
            "expected_lenses": sorted(expected_lenses),
            "reviewers": dict(sorted(reviewers.items())),
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
    for finding in getattr(verdict, "findings", ()):
        findings.append(finding.to_dict() if hasattr(finding, "to_dict") else dict(finding))
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
    record_verdict: Callable[..., Any] | None = None,
    status_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Store one report and synthesize only after the exact set is present."""

    identity = parse_reviewer_id(reviewer)
    if identity is None or identity.lens is None or identity.lens == "synthesis":
        return None
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
    if identity.lens not in expected_lenses or reviewers.get(identity.lens) != reviewer:
        raise RuntimeError(f"reviewer is not in the diversity journal: {reviewer}")
    expected_sha = journal.get("expected_sha")
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
    if sha_mismatch:
        payload["state"] = "NOT-MERGE-READY"
    verdicts = journal.get("verdicts")
    if not isinstance(verdicts, dict):
        raise RuntimeError(f"diversity journal has malformed reports: {path}")
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


__all__ = ["collect_diversity_verdict", "create_journal", "journal_path", "write_journal"]
