"""Opt-in automation for the merge-ready review loop.

The controller is deliberately small at the integration boundary.  Agent
operations, gate evaluation, workgraph loading, and PR lookup are injected so
the parser and the safety decisions can be tested without a running
supervisor or GitHub credentials.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from .graph_health import load_validated_graph
from .loop_state import derive_loop_state
from .ticket import base_ticket


DEFAULT_ITERATION_CAP = 8
DEFAULT_PLATEAU_GUARD = 3
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
_PR = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/(\d+)")


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
    path = str(
        value.get("path")
        or value.get("file")
        or value.get("location")
        or "unknown"
    )
    problem = str(
        value.get("problem")
        or value.get("observed")
        or value.get("title")
        or "review finding"
    )
    fix = str(
        value.get("fix")
        or value.get("do_instead")
        or value.get("recommendation")
        or "address the finding"
    )
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
        source_worker=(
            str(finding_source_worker) if finding_source_worker else None
        ),
        mutation_contract=str(contract) if contract else None,
        source_sha=str(value.get("source_sha") or source_sha)
        if (value.get("source_sha") or source_sha)
        else None,
    )


def parse_verdict(
    text: str,
    *,
    source_sha: str | None = None,
    fallback: Callable[[str], Mapping[str, Any] | None] | None = None,
) -> Verdict | None:
    """Parse the structured reviewer format.

    ``None`` means the header was not present.  A header with no finding
    entries is still a valid verdict; that matters for clean reviews.
    """

    if not isinstance(text, str):
        return None
    headers = list(_HEADER.finditer(text))
    if not headers:
        if fallback is not None:
            value = fallback(text)
            if isinstance(value, Mapping):
                state = value.get("state")
                if isinstance(state, str):
                    parsed = verdict_from_graph(
                        {**value, "source_sha": value.get("source_sha") or source_sha}
                    )
                    return None if parsed is not None and parsed.clean else parsed
        return None
    states = {
        (
            "NOT-MERGE-READY"
            if header.group(1).upper()
            in {"NEEDS FIXES", "NEEDS-FIXES", "NEEDS WORK", "NEEDS-WORK"}
            else header.group(1).upper()
        )
        for header in headers
    }
    if len(states) != 1:
        return None
    header = headers[-1]
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
        inline_fix = re.search(
            r"\s+(?:fix|do instead|recommendation)\s*:\s*(?P<fix>.+)$",
            problem,
            re.IGNORECASE,
        )
        if inline_fix:
            problem = problem[: inline_fix.start()].rstrip(" .")
        findings.append(
            Finding(
                severity=match.group("severity").upper(),
                path=path,
                line=line,
                problem=problem,
                fix=(
                    fix_match.group("fix") if fix_match else "address the finding"
                ).strip(),
                mutation_contract=(
                    contract_match.group("contract") if contract_match else None
                ),
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
    if state == "MERGE-READY" and (
        (matches and not findings) or header.group(2) not in {None, "0"}
    ):
        return None
    return Verdict(
        state=state, findings=tuple(findings), source_sha=verdict_sha, raw=text
    )


def verdict_from_graph(payload: Mapping[str, Any]) -> Verdict | None:
    state = payload.get("state")
    if not isinstance(state, str) or state.upper() not in {
        "MERGE-READY",
        "NOT-MERGE-READY",
        "NO-GO",
    }:
        return None
    source_sha = (
        payload.get("sha")
        or payload.get("source_sha")
        or payload.get("head_sha")
        or payload.get("pinned_sha")
    )
    source_sha = source_sha if isinstance(source_sha, str) else None
    source_worker = payload.get("worker")
    source_worker = source_worker if isinstance(source_worker, str) else None
    values = payload.get("findings")
    if values is not None and not isinstance(values, list):
        return None
    if isinstance(values, list) and any(not isinstance(value, Mapping) for value in values):
        return None
    findings = tuple(
        _finding_from_mapping(value, source_sha, source_worker)
        for value in values or []
    )
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
            model=os.environ.get(
                "WIKI_AUTOPILOT_PARSER_MODEL", "claude-3-5-haiku-latest"
            ),
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


def build_steer_message(verdict: Verdict, *, target_worker: str | None = None) -> str:
    """Render one actionable, source-SHA-cited item per reviewer finding."""

    heading = "autopilot: reviewer findings to address"
    if target_worker:
        heading += f" for {target_worker}"
    if not verdict.findings:
        return f"{heading}. reviewer verdict: {verdict.state}; no structured findings were supplied."
    lines = [heading + "."]
    for index, finding in enumerate(verdict.findings, start=1):
        location = finding.path
        if finding.line is not None:
            location += f":{finding.line}"
        citation = finding.source_sha or verdict.source_sha or "unknown-sha"
        lines.extend(
            [
                f"{index}. [{finding.severity}] {location} (source sha: {citation})",
                f"   problem: {finding.problem}",
                f"   fix: {finding.fix}",
            ]
        )
        if finding.mutation_contract:
            lines.append(f"   mutation contract: {finding.mutation_contract}")
    lines.append("do not declare merge-ready until every item is fixed and verified.")
    return "\n".join(lines)


@dataclass
class AutopilotState:
    enabled: bool = False
    iteration_cap: int = DEFAULT_ITERATION_CAP
    plateau_guard: int = DEFAULT_PLATEAU_GUARD
    henry_ack_required_for_merge: bool = False
    last_action_at_ns: int = 0
    halted: str | None = None
    merge_ack_at_ns: int | None = None
    merge_ack_sha: str | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)
    last_event_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "iteration_cap": self.iteration_cap,
            "plateau_guard": self.plateau_guard,
            "henry_ack_required_for_merge": self.henry_ack_required_for_merge,
            "last_action_at_ns": self.last_action_at_ns,
            "halted": self.halted,
            "merge_ack_at_ns": self.merge_ack_at_ns,
            "merge_ack_sha": self.merge_ack_sha,
            "actions": self.actions[-100:],
            "last_event_key": self.last_event_key,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AutopilotState:
        def integer(raw: Any, default: int) -> int:
            try:
                return int(raw)
            except (TypeError, ValueError):
                return default

        kwargs = {
            "enabled": value.get("enabled") is True,
            "iteration_cap": integer(value.get("iteration_cap"), DEFAULT_ITERATION_CAP),
            "plateau_guard": integer(value.get("plateau_guard"), DEFAULT_PLATEAU_GUARD),
            "henry_ack_required_for_merge": value.get("henry_ack_required_for_merge")
            is True,
            "last_action_at_ns": integer(value.get("last_action_at_ns"), 0),
            "halted": value.get("halted")
            if isinstance(value.get("halted"), str)
            else None,
            "merge_ack_at_ns": integer(value.get("merge_ack_at_ns"), 0)
            if value.get("merge_ack_at_ns")
            else None,
            "merge_ack_sha": value.get("merge_ack_sha")
            if isinstance(value.get("merge_ack_sha"), str)
            else None,
            "actions": list(value.get("actions"))
            if isinstance(value.get("actions"), list)
            else [],
            "last_event_key": value.get("last_event_key")
            if isinstance(value.get("last_event_key"), str)
            else None,
        }
        kwargs["iteration_cap"] = max(1, kwargs["iteration_cap"])
        kwargs["plateau_guard"] = max(1, kwargs["plateau_guard"])
        return cls(**kwargs)


class AutopilotStore:
    def __init__(self, root: Path | None = None):
        self.root = (
            root
            or Path(
                os.environ.get("WIKI_AGENT_RUNTIME_DIR")
                or Path.home() / ".wiki" / "agent-runtime"
            )
            / "autopilot"
        )
        self._lock = threading.RLock()

    def path(self, ticket: str) -> Path:
        if not re.fullmatch(r"[A-Z0-9-]+", ticket):
            raise ValueError("invalid ticket")
        return self.root / f"{ticket}.json"

    def lock_path(self, ticket: str) -> Path:
        if not re.fullmatch(r"[A-Z0-9-]+", ticket):
            raise ValueError("invalid ticket")
        return self.root / f"{ticket}.lock"

    @contextmanager
    def lock(self, ticket: str):
        """Serialize ticket state mutations across controller processes."""

        with self._lock:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            handle = self.lock_path(ticket).open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def load(self, ticket: str) -> AutopilotState:
        try:
            value = json.loads(self.path(ticket).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return AutopilotState()
        return (
            AutopilotState.from_dict(value)
            if isinstance(value, Mapping)
            else AutopilotState()
        )

    def save(self, ticket: str, state: AutopilotState) -> None:
        with self._lock:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            target = self.path(ticket)
            temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            temporary.write_text(
                json.dumps(state.to_dict(), ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(target)

    def list(self) -> dict[str, AutopilotState]:
        if not self.root.is_dir():
            return {}
        result: dict[str, AutopilotState] = {}
        for path in self.root.glob("*.json"):
            ticket = path.stem
            try:
                result[ticket] = self.load(ticket)
            except ValueError:
                continue
        return result


def _pr_number(value: str | None) -> int | None:
    if not isinstance(value, str):
        return None
    match = _PR.search(value)
    if match:
        return int(match.group(1))
    parsed = urlparse(value)
    if parsed.path.isdigit():
        return int(parsed.path)
    return int(value) if value.isdigit() else None


def _sha_matches(verdict_sha: str | None, head_sha: str | None) -> bool:
    if not verdict_sha or not head_sha:
        return False
    verdict = verdict_sha.lower()
    head = head_sha.lower()
    return verdict == head or head.startswith(verdict) or verdict.startswith(head)


class AutopilotController:
    """Apply one action per fleet transition, with hard safety stops."""

    def __init__(
        self,
        *,
        store: AutopilotStore | None = None,
        status_reader: Callable[[str], Mapping[str, Any]] | None = None,
        registry_reader: Callable[[], Mapping[str, Any]] | None = None,
        graph_loader: Callable[[str], Mapping[str, Any] | None] | None = None,
        gate: Callable[[int, str], Mapping[str, Any]] | None = None,
        next_review: Callable[..., Mapping[str, Any]] | None = None,
        steer: Callable[[str, str], Any] | None = None,
        archive: Callable[[str], Any] | None = None,
        merge: Callable[[str], Any] | None = None,
        notify: Callable[[str, str], Any] | None = None,
    ):
        self.store = store or AutopilotStore()
        self.status_reader = status_reader or self._default_status
        self.registry_reader = registry_reader or self._default_registry
        self.graph_loader = graph_loader or self._default_graph
        self.gate = gate or self._default_gate
        self.next_review = next_review or self._default_next_review
        self.steer = steer or self._default_steer
        self._structured_steer = steer is None
        self.archive = archive or self._default_archive
        self.merge = merge or self._default_merge
        self.notify = notify or self._default_notify
        self._locks: dict[str, asyncio.Lock] = {}

    def enable(
        self, ticket: str, *, henry_ack_required_for_merge: bool = False
    ) -> dict[str, Any]:
        ticket = ticket.upper()
        with self.store.lock(ticket):
            current = self.store.load(ticket)
            if current.halted and current.enabled:
                # A hard halt cannot be restarted by a second enable call. The
                # operator must disable and re-enable, making the reset explicit.
                self.store.save(ticket, current)
                return current.to_dict()
            if current.halted:
                current.halted = None
            current.enabled = True
            current.henry_ack_required_for_merge = henry_ack_required_for_merge
            current.last_action_at_ns = time.time_ns()
            self.store.save(ticket, current)
            return current.to_dict()

    def disable(self, ticket: str) -> dict[str, Any]:
        ticket = ticket.upper()
        with self.store.lock(ticket):
            current = self.store.load(ticket)
            current.enabled = False
            current.merge_ack_at_ns = None
            current.merge_ack_sha = None
            self._log(current, "manual-disable", {"halted": "manual-disable"})
            current.last_action_at_ns = time.time_ns()
            self.store.save(ticket, current)
            return current.to_dict()

    def ack_merge(self, ticket: str) -> dict[str, Any]:
        ticket = ticket.upper()
        with self.store.lock(ticket):
            current = self.store.load(ticket)
            current.merge_ack_at_ns = time.time_ns()
            current.last_action_at_ns = current.merge_ack_at_ns
            status = self.status_reader(ticket)
            current.merge_ack_sha = self._current_sha(ticket, status, {})
            self.store.save(ticket, current)
        graph = self.graph_loader(ticket)
        reviewer = self._current_reviewer(graph)
        verdict = self._latest_verdict(graph, reviewer=reviewer) if reviewer else None
        if verdict is None:
            return self.status(ticket)

        async def retry_merge() -> None:
            with self.store.lock(ticket):
                state = self.store.load(ticket)
                if state.enabled and not state.halted:
                    await self._maybe_merge(ticket, state, verdict)
                self.store.save(ticket, state)

        asyncio.run(retry_merge())
        return self.status(ticket)

    def status(self, ticket: str | None = None) -> dict[str, Any]:
        values = self.store.list()
        if ticket is not None:
            return {
                "ticket": ticket.upper(),
                **values.get(ticket.upper(), AutopilotState()).to_dict(),
            }
        enabled = sum(1 for state in values.values() if state.enabled)
        halted = sum(1 for state in values.values() if state.halted)
        actions = sum(
            1
            for state in values.values()
            for action in state.actions
            if time.time_ns() - int(action.get("at_ns") or 0) <= 3_600_000_000_000
        )
        return {
            "tickets": {ticket: state.to_dict() for ticket, state in values.items()},
            "enabled": enabled,
            "halted": halted,
            "actions_last_hour": actions,
        }

    async def on_transition(self, event: Mapping[str, Any]) -> bool:
        agent_id = event.get("agent_id")
        status_state = event.get("status_state")
        if not isinstance(agent_id, str) or not isinstance(status_state, str):
            return True
        ticket = base_ticket(agent_id).upper()
        lock = self._locks.setdefault(ticket, asyncio.Lock())
        async with lock:
            return await self._handle_transition(ticket, agent_id, status_state, event)

    async def _handle_transition(
        self,
        ticket: str,
        agent_id: str,
        status_state: str,
        event: Mapping[str, Any],
    ) -> bool:
        with self.store.lock(ticket):
            state = self.store.load(ticket)
            if not state.enabled or state.halted:
                return True
            key = f"{agent_id}:{event.get('run_id')}:{status_state}:{event.get('status_mtime')}"
            if state.last_event_key == key:
                return True
            try:
                if status_state == "merge-ready" and re.search(
                    r"-REVIEW[1-9][0-9]*$", agent_id.upper()
                ):
                    success = await self._reviewer_ready(ticket, agent_id, event, state)
                elif status_state == "merge-ready":
                    success = await self._implementer_ready(ticket, event, state)
                else:
                    success = True
            except Exception as exc:
                self._log(state, "autopilot-action-failed", {"error": str(exc)})
                success = False
            if success:
                state.last_event_key = key
                self.store.save(ticket, state)
            else:
                self.store.save(ticket, state)
            return success

    async def _reviewer_ready(
        self,
        ticket: str,
        reviewer: str,
        event: Mapping[str, Any],
        state: AutopilotState,
    ) -> bool:
        graph = self.graph_loader(ticket)
        if graph is None:
            return False
        current_reviewer = self._current_reviewer(graph)
        if current_reviewer != reviewer:
            self._log(
                state,
                "reviewer-verdict-ignored-stale-reviewer",
                {"reviewer": reviewer, "current_reviewer": current_reviewer},
            )
            return True
        verdict = self._latest_verdict(graph, reviewer=reviewer)
        if verdict is None:
            return False
        status = self.status_reader(ticket)
        sha = self._current_sha(ticket, status, event)
        if not _sha_matches(verdict.source_sha, sha):
            self._log(
                state,
                "reviewer-verdict-blocked-sha-mismatch",
                {"reviewer": reviewer, "verdict_sha": verdict.source_sha, "head_sha": sha},
            )
            return True
        self._log(state, "parsed-verdict", {"reviewer": reviewer, **verdict.to_dict()})
        if not verdict.clean:
            message = build_steer_message(verdict, target_worker=ticket)
            await self._send_steer(ticket, reviewer, verdict, message)
            self._log(
                state,
                "steer-sent",
                {
                    "reviewer": reviewer,
                    "target": ticket,
                    "source_sha": verdict.source_sha,
                    "preview": message[:500],
                    "finding_count": len(verdict.findings),
                },
            )
            await self._invoke(self.archive, reviewer)
            self._log(state, "reviewer-archived", {"reviewer": reviewer})
            loop = derive_loop_state(dict(graph)) if graph else None
            if loop and loop.plateau_length >= state.plateau_guard:
                self._halt(ticket, state, "plateau")
            elif loop and loop.round >= loop.cap:
                self._halt(ticket, state, "iteration-cap")
            return True
        return await self._maybe_merge(ticket, state, verdict)

    async def _implementer_ready(
        self, ticket: str, event: Mapping[str, Any], state: AutopilotState
    ) -> bool:
        graph = self.graph_loader(ticket)
        if graph is None:
            return False
        loop = derive_loop_state(dict(graph)) if graph else None
        current_reviewer = self._current_reviewer(graph)
        verdict = (
            self._latest_verdict(graph, reviewer=current_reviewer)
            if graph and current_reviewer
            else None
        )
        status = self.status_reader(ticket)
        sha = self._current_sha(ticket, status, event)
        if verdict is not None and not _sha_matches(verdict.source_sha, sha):
            self._log(
                state,
                "implementer-verdict-blocked-sha-mismatch",
                {"reviewer": current_reviewer, "verdict_sha": verdict.source_sha, "head_sha": sha},
            )
            verdict = None
        if loop and loop.round >= loop.cap and (verdict is None or not verdict.clean):
            self._halt(ticket, state, "iteration-cap")
            return True
        if (
            loop
            and loop.plateau_length >= state.plateau_guard
            and (verdict is None or not verdict.clean)
        ):
            self._halt(ticket, state, "plateau")
            return True
        if verdict and verdict.clean:
            return await self._maybe_merge(ticket, state, verdict)
        pr = status.get("pr") if isinstance(status, Mapping) else None
        number = _pr_number(pr if isinstance(pr, str) else None)
        orch = self._orchestrator(ticket, graph)
        if number is None or sha is None or orch is None:
            self._log(
                state, "halted-missing-context", {"pr": pr, "sha": sha, "orch": orch}
            )
            return False
        result = await self._invoke(
            self.next_review,
            ticket=ticket,
            pr_number=number,
            expected_sha=sha,
            orch=orch,
        )
        self._log(
            state,
            "reviewer-spawned",
            {
                **(dict(result) if isinstance(result, Mapping) else {}),
                "head_sha": sha,
                "pr": pr,
            },
        )
        return isinstance(result, Mapping) and result.get("status") == "spawned"

    async def _maybe_merge(
        self, ticket: str, state: AutopilotState, verdict: Verdict
    ) -> bool:
        status = self.status_reader(ticket)
        pr = status.get("pr") if isinstance(status, Mapping) else None
        pr_url = pr if isinstance(pr, str) else None
        sha = self._current_sha(ticket, status, {})
        if pr_url is None or not isinstance(sha, str):
            return False
        if not _sha_matches(verdict.source_sha, sha):
            self._log(
                state,
                "merge-blocked-verdict-sha",
                {"verdict_sha": verdict.source_sha, "head_sha": sha},
            )
            return True
        if state.henry_ack_required_for_merge and (
            state.merge_ack_at_ns is None or state.merge_ack_sha != sha
        ):
            self._log(state, "merge-awaiting-henry-ack", {"sha": sha})
            return True
        gate = await self._invoke(self.gate, pr_url, sha)
        gate_pr = (
            gate.get("pr") or gate.get("url")
            if isinstance(gate, Mapping)
            else None
        )
        if gate_pr != pr_url:
            self._log(
                state,
                "merge-blocked-gate-pr-mismatch",
                {"expected_pr": pr_url, "gate_pr": gate_pr},
            )
            return True
        clean = (
            gate.get("verdict") == "pass" or gate.get("ready") is True
            if isinstance(gate, Mapping)
            else False
        )
        if not clean:
            self._log(
                state,
                "merge-blocked-gate",
                {"gate": dict(gate) if isinstance(gate, Mapping) else gate},
            )
            return False
        await self._invoke(self.merge, pr_url, sha)
        self._log(
            state,
            "merged",
            {"pr": pr_url, "source_sha": verdict.source_sha, "head_sha": sha},
        )
        return True

    @staticmethod
    async def _invoke(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(function, *args, **kwargs)

    async def _send_steer(
        self, ticket: str, source_worker: str, verdict: Verdict, message: str
    ) -> Any:
        if not self._structured_steer:
            return await self._invoke(self.steer, ticket, message)
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        findings = [
            finding.to_steer_dict(
                source_worker=source_worker,
                source_sha=verdict.source_sha,
                created_at=created_at,
            )
            for finding in verdict.findings
        ]
        return await self._invoke(self.steer, ticket, message, findings)

    def _current_reviewer(self, graph: Mapping[str, Any] | None) -> str | None:
        if not graph:
            return None
        nodes = {
            str(node.get("id")): node
            for node in graph.get("nodes", [])
            if isinstance(node, Mapping) and isinstance(node.get("id"), str)
        }
        candidates: list[tuple[float, int, str]] = []
        for index, edge in enumerate(graph.get("edges", [])):
            if not isinstance(edge, Mapping) or edge.get("kind") != "spawn":
                continue
            payload = edge.get("payload")
            target = nodes.get(str(edge.get("to")))
            is_review = isinstance(payload, Mapping) and payload.get("role") == "review"
            is_review = is_review or (
                isinstance(target, Mapping) and target.get("kind") == "review"
            )
            if not is_review:
                continue
            reviewer = (
                edge.get("to")
                or (payload.get("ticket") if isinstance(payload, Mapping) else None)
                or (target.get("worker_id") if isinstance(target, Mapping) else None)
            )
            if not isinstance(reviewer, str) or not re.search(
                r"-REVIEW[1-9][0-9]*$", reviewer.upper()
            ):
                continue
            created = edge.get("created_at")
            try:
                timestamp = datetime.fromisoformat(
                    str(created).replace("Z", "+00:00")
                ).timestamp()
            except (TypeError, ValueError, OverflowError):
                timestamp = float(index)
            candidates.append((timestamp, index, reviewer))
        return max(candidates)[2] if candidates else None

    def _latest_verdict(
        self,
        graph: Mapping[str, Any] | None,
        *,
        reviewer: str | None = None,
    ) -> Verdict | None:
        if not graph:
            return None
        for edge in reversed(graph.get("edges", [])):
            if (
                isinstance(edge, Mapping)
                and edge.get("kind") == "verdict"
                and isinstance(edge.get("payload"), Mapping)
                and (
                    reviewer is None
                    or edge.get("from") == reviewer
                    or edge["payload"].get("worker") == reviewer
                    or edge["payload"].get("reviewer") == reviewer
                )
            ):
                return verdict_from_graph(edge["payload"])
        return None

    def _current_sha(
        self,
        ticket: str,
        status: Mapping[str, Any] | None,
        event: Mapping[str, Any],
    ) -> str | None:
        candidates = [
            status.get("sha") if isinstance(status, Mapping) else None,
            event.get("sha"),
        ]
        registry = self.registry_reader()
        entry = registry.get(ticket) if isinstance(registry, Mapping) else None
        current = entry.get("current") if isinstance(entry, Mapping) else None
        worktree = current.get("worktree") if isinstance(current, Mapping) else None
        if isinstance(worktree, str):
            try:
                result = subprocess.run(
                    ["git", "-C", worktree, "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                result = None
            if result is not None and result.returncode == 0:
                candidates.append(result.stdout.strip())
        return next(
            (
                value
                for value in candidates
                if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{7,64}", value)
            ),
            None,
        )

    @staticmethod
    def _read_verdict_file(reviewer: str) -> Verdict | None:
        path = Path("/tmp") / f"{reviewer}-verdict.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return verdict_from_graph(value) if isinstance(value, Mapping) else None

    def _orchestrator(self, ticket: str, graph: Mapping[str, Any] | None) -> str | None:
        if graph and isinstance(graph.get("orch"), str):
            return str(graph["orch"])
        registry = self.registry_reader()
        entry = registry.get(ticket) if isinstance(registry, Mapping) else None
        current = entry.get("current") if isinstance(entry, Mapping) else None
        value = current.get("orch") if isinstance(current, Mapping) else None
        if isinstance(value, str) and value:
            return value
        return None

    def _log(
        self, state: AutopilotState, action: str, details: Mapping[str, Any]
    ) -> None:
        now = time.time_ns()
        state.last_action_at_ns = now
        state.actions.append(
            {"action": action, "at_ns": now, **dict(details), "source": "autopilot"}
        )

    def _halt(self, ticket: str, state: AutopilotState, reason: str) -> None:
        state.halted = reason
        self._log(state, f"halted-at-{reason}", {"ticket": ticket, "halted": reason})
        orch = self._orchestrator(ticket, None)
        if orch:
            try:
                self.notify(
                    orch,
                    f"autopilot halted {ticket}: {reason}; explicit operator action required",
                )
            except Exception as exc:
                self._log(state, "halt-notify-failed", {"error": str(exc)})

    @staticmethod
    def _default_status(ticket: str) -> Mapping[str, Any]:
        from .. import main

        return main.read_agent_status(ticket) or {}

    @staticmethod
    def _default_registry() -> Mapping[str, Any]:
        from .. import main

        return main._read_agent_registry()  # noqa: SLF001

    @staticmethod
    def _default_graph(ticket: str) -> Mapping[str, Any] | None:
        from .. import main

        graph, _source = load_validated_graph(ticket, status_dir=main.AGENT_STATUS_DIR)
        return graph

    @staticmethod
    def _default_gate(pr_url: str, sha: str) -> Mapping[str, Any]:
        from .. import main

        return {
            "pr": pr_url,
            **main.composer_gate(main.ComposerGateIn(pr=pr_url, expect_sha=sha)),
        }

    @staticmethod
    def _default_next_review(**kwargs: Any) -> Mapping[str, Any]:
        from .next_review import next_review

        return next_review(**kwargs)

    @staticmethod
    def _default_steer(
        ticket: str, message: str, findings: list[dict[str, Any]] | None = None
    ) -> Any:
        from .. import main

        return main.agent_message(
            ticket,
            main.MessageIn(
                text=message,
                mode="now",
                source="autopilot",
                findings=findings,
            ),
            main.BackgroundTasks(),
        )

    @staticmethod
    def _default_archive(reviewer: str) -> Any:
        from .. import main

        return main.archive_agent(reviewer, main.AgentArchiveIn(outcome="closed"))

    @staticmethod
    def _default_merge(pr_url: str, sha: str) -> Any:
        from .. import github_pr

        return github_pr.merge_pr(pr_url, sha)

    @staticmethod
    def _default_notify(orch: str, message: str) -> Any:
        from .. import main

        return main.agent_message(
            orch,
            main.MessageIn(text=message, mode="now", source="autopilot"),
            main.BackgroundTasks(),
        )


__all__ = [
    "AutopilotController",
    "AutopilotState",
    "AutopilotStore",
    "Finding",
    "Verdict",
    "build_steer_message",
    "parse_verdict",
    "verdict_from_graph",
]
