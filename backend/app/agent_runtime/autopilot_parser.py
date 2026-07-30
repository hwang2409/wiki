"""Parse and render reviewer verdict artifacts for autopilot."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping


_HEADER = re.compile(
    r"(?im)^[ \t]*(?:verdict[ \t]*:[ \t]*)?"
    r"(MERGE-READY|NOT-MERGE-READY|NO-GO|NEEDS[- ](?:FIXES|WORK))"
    r"[ \t]*(?:[:;—-][ \t]*(?:(\d+)[ \t]+findings?|[^\n]*))?[ \t]*$"
)
_FINDING = re.compile(
    r"(?im)^\s*(?:\d+[.)]|[-*])\s*(?:\*\*)?\[?"
    r"(?P<severity>BLOCKING|CRITICAL|HIGH|MAJOR|MEDIUM|MINOR|LOW)\]?\*?\*?\s*"
    r"(?::|[-—])?\s*(?P<rest>[^\n]+)"
)
_FIELD = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:fix|do instead|recommendation)\s*:\s*(?P<fix>[^\n]+)"
)
_CONTRACT = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:mutation contract|contract)\s*:\s*(?P<contract>[^\n]+)"
)
_LOCATION = re.compile(
    r"^\s*(?P<location>`[^`\n]+`|\*\*[^*\n]+\*\*|[^\s—-]+?)"
    r"(?::(?P<line>\d+))?\s*(?:[-—:]\s+)(?P<problem>.+?)\s*$"
)
_SHA = re.compile(
    r"(?i)\b(?:source[_ -]?sha|pinned[_ -]?sha|sha)\s*[:=]\s*([0-9a-f]{7,64})\b"
)


@dataclass(frozen=True)
class Finding:
    severity: str
    path: str
    line: int | None
    problem: str
    fix: str
    finding_id: str | None = None
    title: str | None = None
    observed: str | None = None
    why_wrong: str | None = None
    constraint: str | None = None
    source_worker: str | None = None
    mutation_contract: str | None = None
    source_sha: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "severity": self.severity,
            "path": self.path,
            "line": self.line,
            "problem": self.problem,
            "fix": self.fix,
        }
        if self.finding_id:
            result["id"] = self.finding_id
        if self.title:
            result["title"] = self.title
        if self.observed:
            result["observed"] = self.observed
        if self.why_wrong:
            result["why_wrong"] = self.why_wrong
        if self.constraint:
            result["constraint"] = self.constraint
        if self.source_worker:
            result["source_worker"] = self.source_worker
        if self.mutation_contract:
            result["mutation_contract"] = self.mutation_contract
        if self.source_sha:
            result["source_sha"] = self.source_sha
        return result

    def to_steer_dict(
        self,
        *,
        source_worker: str,
        source_sha: str | None,
        created_at: str,
    ) -> dict[str, Any]:
        canonical_sha = self.source_sha or source_sha or "0000000"
        identity = self.finding_id or (
            "F-"
            + hashlib.sha256(
                f"{canonical_sha}\0{self.path}\0{self.line}\0{self.problem}\0{self.fix}".encode()
            ).hexdigest()[:6]
        )
        return {
            "id": identity,
            "severity": self.severity,
            "title": (self.title or self.problem)[:140],
            "file": self.path,
            **({"line": self.line} if self.line is not None else {}),
            "observed": self.observed or self.problem,
            "why_wrong": self.why_wrong or self.problem,
            "do_instead": self.fix,
            **(
                {"constraint": self.constraint or self.mutation_contract}
                if self.constraint or self.mutation_contract
                else {}
            ),
            "source_worker": self.source_worker or source_worker,
            "source_kind": "review",
            "source_sha": canonical_sha,
            "created_at": created_at,
        }


@dataclass(frozen=True)
class Verdict:
    state: str
    findings: tuple[Finding, ...] = ()
    source_sha: str | None = None
    raw: str = ""

    @property
    def clean(self) -> bool:
        return self.state == "MERGE-READY" and not self.findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "findings": [finding.to_dict() for finding in self.findings],
            "source_sha": self.source_sha,
        }


def _finding_from_mapping(
    value: Mapping[str, Any], source_sha: str | None, source_worker: str | None = None
) -> Finding:
    line = value.get("line")
    if isinstance(line, str) and line.isdigit():
        line = int(line)
    if not isinstance(line, int) or isinstance(line, bool):
        line = None
    path = str(value.get("path") or value.get("file") or value.get("location") or "unknown")
    problem = str(value.get("problem") or value.get("observed") or value.get("title") or "review finding")
    fix = str(value.get("fix") or value.get("do_instead") or value.get("recommendation") or "address the finding")
    observed = str(value.get("observed") or problem)
    why_wrong = str(value.get("why_wrong") or problem)
    title = str(value.get("title") or problem)
    finding_id = value.get("id")
    finding_source_worker = value.get("source_worker") or value.get("worker") or source_worker
    contract = value.get("mutation_contract") or value.get("contract")
    return Finding(
        severity=str(value.get("severity") or "MEDIUM").upper(),
        path=path,
        line=line,
        problem=problem,
        fix=fix,
        finding_id=str(finding_id) if finding_id else None,
        title=title,
        observed=observed,
        why_wrong=why_wrong,
        constraint=str(value.get("constraint")) if value.get("constraint") else None,
        source_worker=str(finding_source_worker) if finding_source_worker else None,
        mutation_contract=str(contract) if contract else None,
        source_sha=str(value.get("source_sha") or source_sha)
        if (value.get("source_sha") or source_sha)
        else None,
    )


def verdict_from_graph(payload: Mapping[str, Any]) -> Verdict | None:
    state = payload.get("state")
    if not isinstance(state, str) or state.upper() not in {"MERGE-READY", "NOT-MERGE-READY", "NO-GO"}:
        return None
    source_sha = payload.get("sha") or payload.get("source_sha") or payload.get("head_sha") or payload.get("pinned_sha")
    source_sha = source_sha if isinstance(source_sha, str) else None
    source_worker = payload.get("worker")
    source_worker = source_worker if isinstance(source_worker, str) else None
    values = payload.get("findings")
    if values is not None and not isinstance(values, list):
        return None
    if isinstance(values, list) and any(not isinstance(value, Mapping) for value in values):
        return None
    findings = tuple(_finding_from_mapping(value, source_sha, source_worker) for value in values or [])
    return Verdict(state=state.upper(), findings=findings, source_sha=source_sha)


def _anthropic_fallback(text: str) -> Mapping[str, Any] | None:
    """Best-effort parser for a genuinely non-standard reviewer response."""

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=os.environ.get("WIKI_AUTOPILOT_PARSER_MODEL", "claude-3-5-haiku-latest"),
            max_tokens=1200,
            system=(
                "Extract a code review verdict. Return JSON only with state "
                "(MERGE-READY or NOT-MERGE-READY), sha, and findings. Each finding "
                "must have severity, path, line, problem, fix, and optional mutation_contract."
            ),
            messages=[{"role": "user", "content": text}],
        )
        content = response.content[0].text if response.content else ""
        value = json.loads(content)
    except Exception:
        return None
    return value if isinstance(value, Mapping) else None


def parse_verdict(
    text: str,
    *,
    source_sha: str | None = None,
    fallback: Callable[[str], Mapping[str, Any] | None] | None = None,
) -> Verdict | None:
    """Parse one structured reviewer verdict. Fallback output cannot authorize merge."""

    if not isinstance(text, str):
        return None
    headers = list(_HEADER.finditer(text))
    if not headers:
        if fallback is not None:
            value = fallback(text)
            if isinstance(value, Mapping) and isinstance(value.get("state"), str):
                parsed = verdict_from_graph({**value, "source_sha": value.get("source_sha") or source_sha})
                return None if parsed is not None and parsed.clean else parsed
        return None
    states = {
        "NOT-MERGE-READY"
        if header.group(1).upper() in {"NEEDS FIXES", "NEEDS-FIXES", "NEEDS WORK", "NEEDS-WORK"}
        else header.group(1).upper()
        for header in headers
    }
    if len(headers) != 1 or len(states) != 1:
        return None
    header = headers[0]
    state = header.group(1).upper()
    if state in {"NEEDS FIXES", "NEEDS-FIXES", "NEEDS WORK", "NEEDS-WORK"}:
        state = "NOT-MERGE-READY"
    sha_match = _SHA.search(text)
    verdict_sha = source_sha or (sha_match.group(1) if sha_match else None)
    findings: list[Finding] = []
    body = text[header.end() :]
    matches = list(_FINDING.finditer(body))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        entry = body[match.start() : end]
        fix_match = _FIELD.search(entry)
        contract_match = _CONTRACT.search(entry)
        rest = match.group("rest").strip()
        location_match = _LOCATION.match(rest)
        if location_match is None:
            continue
        path = location_match.group("location").strip().strip("`*")
        line = int(location_match.group("line")) if location_match.group("line") else None
        if line is None:
            embedded_line = re.fullmatch(r"(.+):(\d+)", path)
            if embedded_line:
                path = embedded_line.group(1).strip()
                line = int(embedded_line.group(2))
        problem = location_match.group("problem").strip()
        inline_fix = re.search(r"\s+(?:fix|do instead|recommendation)\s*:\s*(?P<fix>.+)$", problem, re.IGNORECASE)
        if inline_fix:
            problem = problem[: inline_fix.start()].rstrip(" .")
        findings.append(
            Finding(
                severity=match.group("severity").upper(),
                path=path,
                line=line,
                problem=problem,
                fix=(fix_match.group("fix") if fix_match else "address the finding").strip(),
                mutation_contract=contract_match.group("contract") if contract_match else None,
                source_sha=verdict_sha,
            )
        )
        if inline_fix and not fix_match:
            finding = findings[-1]
            findings[-1] = Finding(
                severity=finding.severity,
                path=finding.path,
                line=finding.line,
                problem=finding.problem,
                fix=inline_fix.group("fix").strip(),
                mutation_contract=finding.mutation_contract,
                source_sha=finding.source_sha,
            )
    if state == "MERGE-READY" and ((matches and not findings) or header.group(2) not in {None, "0"}):
        return None
    return Verdict(state=state, findings=tuple(findings), source_sha=verdict_sha, raw=text)


def build_steer_message(verdict: Verdict, *, target_worker: str | None = None) -> str:
    """Render every canonical field needed to reproduce a reviewer finding."""

    heading = "autopilot: reviewer findings to address"
    if target_worker:
        heading += f" for {target_worker}"
    if not verdict.findings:
        return f"{heading}. reviewer verdict: {verdict.state}; no structured findings were supplied."
    lines = [heading + "."]
    for index, finding in enumerate(verdict.findings, start=1):
        location = finding.path + (f":{finding.line}" if finding.line is not None else "")
        citation = finding.source_sha or verdict.source_sha or "unknown-sha"
        lines.extend([
            f"{index}. [{finding.severity}] {location} (source sha: {citation})",
            f"   problem: {finding.problem}",
            f"   fix: {finding.fix}",
        ])
        if finding.finding_id:
            lines.append(f"   finding id: {finding.finding_id}")
        if finding.observed:
            lines.append(f"   observed: {finding.observed}")
        if finding.why_wrong:
            lines.append(f"   why wrong: {finding.why_wrong}")
        if finding.constraint:
            lines.append(f"   constraint: {finding.constraint}")
        if finding.mutation_contract:
            lines.append(f"   mutation contract: {finding.mutation_contract}")
        if finding.source_worker:
            lines.append(f"   source worker: {finding.source_worker}")
    lines.append("do not declare merge-ready until every item is fixed and verified.")
    return "\n".join(lines)


__all__ = ["Finding", "Verdict", "build_steer_message", "parse_verdict", "verdict_from_graph"]
