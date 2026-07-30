"""Pure reviewer-lens selection and deterministic verdict synthesis."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_DIVERSITY_LENSES = ("correctness", "security", "perf", "test-strength")
LENS_PROMPTS = {
    "correctness": "focus on functional correctness, invariants, and edge cases",
    "security": "focus on trust boundaries, input validation, privilege, and abuse paths",
    "perf": "focus on latency, concurrency, resource use, and failure under load",
    "test-strength": "focus on missing tests, weak assertions, and untested failure paths",
}
_SEVERITY_RANK = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "BLOCKING": 4}
_STOP_WORDS = frozenset("a an and are as at be by for from in is it of on or that the this to with".split())


def normalize_diversity(value: int | Sequence[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, bool) or isinstance(value, (str, bytes)):
        raise ValueError("diversity must be a lens list or a count")
    if isinstance(value, int):
        if not 1 <= value <= len(DEFAULT_DIVERSITY_LENSES):
            raise ValueError("diversity count must be between 1 and 4")
        return DEFAULT_DIVERSITY_LENSES[:value]
    lenses = tuple(str(item).strip().lower() for item in value)
    if not 1 <= len(lenses) <= len(DEFAULT_DIVERSITY_LENSES):
        raise ValueError("diversity must contain between 1 and 4 lenses")
    if len(set(lenses)) != len(lenses):
        raise ValueError("diversity lenses must be unique")
    unknown = sorted(set(lenses) - set(LENS_PROMPTS))
    if unknown:
        raise ValueError(f"unknown diversity lens: {unknown[0]}")
    return lenses


def _tokens(finding: Mapping[str, Any]) -> set[str]:
    text = " ".join(str(finding.get(key) or "") for key in ("problem", "observed", "title", "why_wrong", "description"))
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) >= 3 and token not in _STOP_WORDS}


def _line_range(finding: Mapping[str, Any]) -> tuple[int, int] | None:
    raw_range = finding.get("line_range")
    line = finding.get("line")
    end = finding.get("line_end") or finding.get("end_line")
    if isinstance(raw_range, Sequence) and not isinstance(raw_range, (str, bytes)) and len(raw_range) == 2:
        line, end = raw_range
    try:
        start, finish = int(line), int(end) if end is not None else int(line)
    except (TypeError, ValueError):
        return None
    return (start, finish) if start >= 1 and finish >= start else None


def _collides(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_path = str(left.get("file") or left.get("path") or left.get("location") or "unknown")
    right_path = str(right.get("file") or right.get("path") or right.get("location") or "unknown")
    if left_path != right_path or not (_tokens(left) & _tokens(right)):
        return False
    left_lines, right_lines = _line_range(left), _line_range(right)
    if left_lines is None or right_lines is None:
        return True
    return left_lines[0] <= right_lines[1] and right_lines[0] <= left_lines[1]


def _finding_id(finding: Mapping[str, Any], sha: str) -> str:
    material = json.dumps(
        {"file": finding.get("file") or finding.get("path") or "unknown", "line": _line_range(finding), "description": sorted(_tokens(finding)), "sha": sha},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "F-" + hashlib.sha256(material.encode()).hexdigest()[:6]


def _finding_sort_key(finding: Mapping[str, Any]) -> str:
    return json.dumps(dict(finding), sort_keys=True, separators=(",", ":"), default=str)


def _schema_finding(raw: Mapping[str, Any], *, lens: str, reviewer: str, sha: str, created_at: str) -> dict[str, Any]:
    severity = str(raw.get("severity") or "MEDIUM").upper()
    if severity not in _SEVERITY_RANK:
        severity = "MEDIUM"
    path = str(raw.get("file") or raw.get("path") or raw.get("location") or "unknown")
    problem = str(raw.get("problem") or raw.get("observed") or raw.get("title") or "review finding")
    line = _line_range(raw)
    raw_id = str(raw.get("id") or "")
    result: dict[str, Any] = {
        "id": raw_id if re.fullmatch(r"F-[a-z0-9]{6}", raw_id) else _finding_id(raw, sha),
        "severity": severity,
        "title": str(raw.get("title") or problem)[:140],
        "file": path,
        "observed": str(raw.get("observed") or problem),
        "why_wrong": str(raw.get("why_wrong") or problem),
        "do_instead": str(raw.get("do_instead") or raw.get("fix") or "address the finding"),
        "source_worker": str(raw.get("source_worker") or reviewer),
        "source_lenses": [lens],
        "source_kind": "review",
        "source_sha": sha,
        "created_at": created_at,
    }
    if line is not None:
        result["line"] = line[0]
        if line[1] != line[0]:
            result["line_end"] = line[1]
    if raw.get("constraint"):
        result["constraint"] = str(raw["constraint"])
    return result


def synthesize_diverse_verdicts(
    ticket: str,
    expected_sha: str,
    verdicts: Mapping[str, Mapping[str, Any]],
    *,
    expected_lenses: Sequence[str] | None = None,
    round_number: int = 1,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Aggregate an exact, SHA-bound lens set with stable bytes."""

    expected_sha = expected_sha.lower()
    created_at = created_at or "1970-01-01T00:00:00+00:00"
    required = tuple(sorted(normalize_diversity(expected_lenses))) if expected_lenses is not None else tuple(sorted(normalize_diversity(tuple(verdicts))))
    supplied = tuple(sorted(str(lens).lower() for lens in verdicts))
    clean = supplied == required and bool(required)
    merged: list[dict[str, Any]] = []
    for lens in required:
        value = verdicts.get(lens)
        if not isinstance(value, Mapping):
            clean = False
            continue
        source_sha = str(value.get("source_sha") or value.get("sha") or "").lower()
        if source_sha != expected_sha:
            clean = False
        findings = value.get("findings")
        if not isinstance(findings, list):
            clean = False
            continue
        if str(value.get("state") or "").upper() != "MERGE-READY" or findings:
            clean = False
        reviewer = str(value.get("worker") or f"{ticket.upper()}-REVIEW{round_number}-{lens}")
        for raw in sorted(findings, key=lambda item: _finding_sort_key(item) if isinstance(item, Mapping) else repr(item)):
            if not isinstance(raw, Mapping):
                clean = False
                continue
            candidate = _schema_finding(raw, lens=lens, reviewer=reviewer, sha=expected_sha, created_at=created_at)
            collision = next((item for item in merged if _collides(item, candidate)), None)
            if collision is None:
                merged.append(candidate)
            else:
                if _SEVERITY_RANK[candidate["severity"]] > _SEVERITY_RANK[collision["severity"]]:
                    collision["severity"] = candidate["severity"]
                collision["source_lenses"] = sorted(set(collision.get("source_lenses") or []) | {lens})
                collision["linked_findings"] = sorted(set(collision.get("linked_findings") or []) | {candidate["id"]})
    for finding in merged:
        finding["id"] = _finding_id(finding, expected_sha)
    return {
        "worker": f"{ticket.upper()}-REVIEW{round_number}-synthesis",
        "sha": expected_sha,
        "state": "MERGE-READY" if clean else "NOT-MERGE-READY",
        "findings": sorted(merged, key=lambda item: (item.get("file", ""), item.get("line", 0), item["id"])),
        "summary": f"deterministic synthesis of {len(required)} reviewer lenses",
        "created_at": created_at,
    }


def record_diverse_verdicts(
    *, ticket: str, expected_sha: str, verdicts: Mapping[str, Mapping[str, Any]], expected_lenses: Sequence[str] | None = None,
    orch: str, record_verdict: Callable[..., Any], request_id: str, status_dir: Path | None = None, round_number: int = 1,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Write lens edges and one synthesis edge in deterministic order."""

    sha = expected_sha.lower()
    required = tuple(sorted(normalize_diversity(expected_lenses or tuple(verdicts))))
    created_at = created_at or "1970-01-01T00:00:00+00:00"
    for lens in required:
        raw = verdicts.get(lens)
        payload = dict(raw) if isinstance(raw, Mapping) else {}
        worker = str(payload.get("worker") or f"{ticket.upper()}-REVIEW{round_number}-{lens}")
        findings = payload.get("findings") if isinstance(payload.get("findings"), list) else []
        payload = {
            "worker": worker,
            "sha": sha,
            "state": str(payload.get("state") or "NOT-MERGE-READY").upper(),
            "findings": [_schema_finding(item, lens=lens, reviewer=worker, sha=sha, created_at=created_at) for item in findings if isinstance(item, Mapping)],
            "created_at": created_at,
        }
        record_verdict(ticket=ticket, reviewer=worker, orch=orch, payload=payload, request_id=f"{request_id}:verdict:{lens}", status_dir=status_dir)
    synthesis = synthesize_diverse_verdicts(ticket, sha, verdicts, expected_lenses=required, round_number=round_number, created_at=created_at)
    record_verdict(ticket=ticket, reviewer=synthesis["worker"], orch=orch, payload=synthesis, request_id=f"{request_id}:synthesis", status_dir=status_dir)
    return synthesis


__all__ = ["DEFAULT_DIVERSITY_LENSES", "LENS_PROMPTS", "normalize_diversity", "record_diverse_verdicts", "synthesize_diverse_verdicts"]
