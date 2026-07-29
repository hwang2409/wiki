"""Opt-in automation for the merge-ready review loop.

The controller is deliberately small at the integration boundary.  Agent
operations, gate evaluation, workgraph loading, and PR lookup are injected so
the parser and the safety decisions can be tested without a running
supervisor or GitHub credentials.
"""

from __future__ import annotations

import asyncio
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
    r"(?im)^\s*(MERGE-READY|NOT-MERGE-READY|NO-GO)\s*(?::\s*(?:(\d+)\s+findings?|[^\n]*))?\s*$"
)
_FINDING = re.compile(
    r"(?im)^\s*(?:\d+[.)]|[-*])\s*(?:\*\*)?\[?"
    r"(?P<severity>BLOCKING|HIGH|MEDIUM|LOW)\]?\*?\*?\s*"
    r"(?:(?::|[-—])\s*)?(?P<location>`[^`\n]+`|[^:\n]+?)(?::(?P<line>\d+))?\s*[-—:]\s*"
    r"(?P<problem>[^\n]+)"
)
_FIELD = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:fix|do instead|recommendation)\s*:\s*(?P<fix>[^\n]+)"
)
_CONTRACT = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:mutation contract|contract)\s*:\s*(?P<contract>[^\n]+)"
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
        if self.mutation_contract:
            result["mutation_contract"] = self.mutation_contract
        if self.source_sha:
            result["source_sha"] = self.source_sha
        return result


@dataclass(frozen=True)
class Verdict:
    state: str
    findings: tuple[Finding, ...] = ()
    source_sha: str | None = None
    raw: str = ""

    @property
    def clean(self) -> bool:
        return self.state == "MERGE-READY"

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "findings": [finding.to_dict() for finding in self.findings],
            "source_sha": self.source_sha,
        }


def _finding_from_mapping(value: Mapping[str, Any], source_sha: str | None) -> Finding:
    line = value.get("line")
    if isinstance(line, str) and line.isdigit():
        line = int(line)
    if not isinstance(line, int) or isinstance(line, bool):
        line = None
    path = str(value.get("path") or value.get("file") or "unknown")
    problem = str(
        value.get("problem")
        or value.get("observed")
        or value.get("title")
        or "review finding"
    )
    fix = str(value.get("fix") or value.get("do_instead") or "address the finding")
    contract = value.get("mutation_contract") or value.get("contract")
    return Finding(
        severity=str(value.get("severity") or "MEDIUM").upper(),
        path=path,
        line=line,
        problem=problem,
        fix=fix,
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
    header = _HEADER.search(text)
    if header is None:
        if fallback is not None:
            value = fallback(text)
            if isinstance(value, Mapping):
                state = value.get("state")
                if isinstance(state, str):
                    return verdict_from_graph(value)
        return None
    state = header.group(1).upper()
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
        path = match.group("location").strip().strip("`")
        line = int(match.group("line")) if match.group("line") else None
        if line is None:
            location_match = re.fullmatch(r"(.+):(\d+)", path)
            if location_match:
                path = location_match.group(1).strip()
                line = int(location_match.group(2))
        findings.append(
            Finding(
                severity=match.group("severity").upper(),
                path=path,
                line=line,
                problem=match.group("problem").strip(),
                fix=(
                    fix_match.group("fix") if fix_match else "address the finding"
                ).strip(),
                mutation_contract=(
                    contract_match.group("contract") if contract_match else None
                ),
                source_sha=verdict_sha,
            )
        )
    if not findings and header.group(2) not in {None, "0"} and fallback is not None:
        value = fallback(text)
        if isinstance(value, Mapping):
            parsed = verdict_from_graph(value)
            if parsed is not None:
                return parsed
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
    source_sha = payload.get("sha")
    source_sha = source_sha if isinstance(source_sha, str) else None
    values = payload.get("findings")
    findings = (
        tuple(
            _finding_from_mapping(value, source_sha)
            for value in values
            if isinstance(value, Mapping)
        )
        if isinstance(values, list)
        else ()
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
    except (ImportError, OSError, ValueError, AttributeError):
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
            temporary = target.with_name(f".{target.name}.tmp")
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
        self.archive = archive or self._default_archive
        self.merge = merge or self._default_merge
        self.notify = notify or self._default_notify
        self._locks: dict[str, asyncio.Lock] = {}

    def enable(
        self, ticket: str, *, henry_ack_required_for_merge: bool = False
    ) -> dict[str, Any]:
        ticket = ticket.upper()
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
        current = self.store.load(ticket)
        current.enabled = False
        current.merge_ack_at_ns = None
        current.merge_ack_sha = None
        current.last_action_at_ns = time.time_ns()
        self.store.save(ticket, current)
        return current.to_dict()

    def ack_merge(self, ticket: str) -> dict[str, Any]:
        ticket = ticket.upper()
        current = self.store.load(ticket)
        current.merge_ack_at_ns = time.time_ns()
        current.last_action_at_ns = current.merge_ack_at_ns
        status = self.status_reader(ticket)
        current.merge_ack_sha = self._current_sha(ticket, status, {})
        self.store.save(ticket, current)
        return current.to_dict()

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

    async def on_transition(self, event: Mapping[str, Any]) -> None:
        agent_id = event.get("agent_id")
        status_state = event.get("status_state")
        if not isinstance(agent_id, str) or not isinstance(status_state, str):
            return
        ticket = base_ticket(agent_id).upper()
        lock = self._locks.setdefault(ticket, asyncio.Lock())
        async with lock:
            await self._handle_transition(ticket, agent_id, status_state, event)

    async def _handle_transition(
        self,
        ticket: str,
        agent_id: str,
        status_state: str,
        event: Mapping[str, Any],
    ) -> None:
        state = self.store.load(ticket)
        if not state.enabled or state.halted:
            return
        key = f"{agent_id}:{event.get('run_id')}:{status_state}:{event.get('status_mtime')}"
        if state.last_event_key == key:
            return
        state.last_event_key = key
        try:
            if status_state == "merge-ready" and re.search(
                r"-REVIEW[1-9][0-9]*$", agent_id.upper()
            ):
                await self._reviewer_ready(ticket, agent_id, event, state)
            elif status_state == "merge-ready":
                await self._implementer_ready(ticket, event, state)
        finally:
            self.store.save(ticket, state)

    async def _reviewer_ready(
        self,
        ticket: str,
        reviewer: str,
        event: Mapping[str, Any],
        state: AutopilotState,
    ) -> None:
        graph = self.graph_loader(ticket)
        verdict = self._latest_verdict(graph) if graph else None
        if verdict is None:
            verdict = self._read_verdict_file(reviewer)
        if verdict is None:
            verdict = parse_verdict(
                str(event.get("step") or ""), fallback=_anthropic_fallback
            )
        if verdict is None:
            return
        self._log(state, "parsed-verdict", {"reviewer": reviewer, **verdict.to_dict()})
        if not verdict.clean:
            message = build_steer_message(verdict, target_worker=ticket)
            await self._invoke(self.steer, ticket, message)
            self._log(
                state,
                "steer-sent",
                {
                    "reviewer": reviewer,
                    "target": ticket,
                    "source_sha": verdict.source_sha,
                },
            )
            await self._invoke(self.archive, reviewer)
            self._log(state, "reviewer-archived", {"reviewer": reviewer})
            loop = derive_loop_state(dict(graph)) if graph else None
            if loop and loop.plateau_length >= state.plateau_guard:
                self._halt(ticket, state, "plateau")
            elif loop and loop.round >= loop.cap:
                self._halt(ticket, state, "iteration-cap")
            return
        await self._maybe_merge(ticket, state, verdict)

    async def _implementer_ready(
        self, ticket: str, event: Mapping[str, Any], state: AutopilotState
    ) -> None:
        graph = self.graph_loader(ticket)
        loop = derive_loop_state(dict(graph)) if graph else None
        verdict = self._latest_verdict(graph) if graph else None
        if loop and loop.round >= loop.cap and (verdict is None or not verdict.clean):
            self._halt(ticket, state, "iteration-cap")
            return
        if (
            loop
            and loop.plateau_length >= state.plateau_guard
            and (verdict is None or not verdict.clean)
        ):
            self._halt(ticket, state, "plateau")
            return
        if verdict and verdict.clean:
            await self._maybe_merge(ticket, state, verdict)
            return
        status = self.status_reader(ticket)
        pr = status.get("pr") if isinstance(status, Mapping) else None
        number = _pr_number(pr if isinstance(pr, str) else None)
        sha = self._current_sha(ticket, status, event)
        orch = self._orchestrator(ticket, graph)
        if number is None or sha is None or orch is None:
            self._log(
                state, "halted-missing-context", {"pr": pr, "sha": sha, "orch": orch}
            )
            return
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
            dict(result) if isinstance(result, Mapping) else {},
        )

    async def _maybe_merge(
        self, ticket: str, state: AutopilotState, verdict: Verdict
    ) -> None:
        status = self.status_reader(ticket)
        pr = status.get("pr") if isinstance(status, Mapping) else None
        number = _pr_number(pr if isinstance(pr, str) else None)
        sha = self._current_sha(ticket, status, {})
        if number is None or not isinstance(sha, str):
            return
        if state.henry_ack_required_for_merge and (
            state.merge_ack_at_ns is None or state.merge_ack_sha != sha
        ):
            self._log(state, "merge-awaiting-henry-ack", {"sha": sha})
            return
        gate = await self._invoke(self.gate, number, sha)
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
            return
        await self._invoke(self.merge, ticket)
        self._log(state, "merged", {"pr": pr, "source_sha": verdict.source_sha})

    @staticmethod
    async def _invoke(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(function, *args, **kwargs)

    def _latest_verdict(self, graph: Mapping[str, Any] | None) -> Verdict | None:
        if not graph:
            return None
        for edge in reversed(graph.get("edges", [])):
            if (
                isinstance(edge, Mapping)
                and edge.get("kind") == "verdict"
                and isinstance(edge.get("payload"), Mapping)
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
            {"action": action, "at_ns": now, "source": "autopilot", **dict(details)}
        )

    def _halt(self, ticket: str, state: AutopilotState, reason: str) -> None:
        state.halted = reason
        self._log(state, f"halted-at-{reason}", {"ticket": ticket})
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
    def _default_gate(number: int, sha: str) -> Mapping[str, Any]:
        from .. import main

        return main.composer_gate(main.ComposerGateIn(pr=str(number), expect_sha=sha))

    @staticmethod
    def _default_next_review(**kwargs: Any) -> Mapping[str, Any]:
        from .next_review import next_review

        return next_review(**kwargs)

    @staticmethod
    def _default_steer(ticket: str, message: str) -> Any:
        from .. import main

        return main.agent_message(
            ticket,
            main.MessageIn(text=message, mode="now", source="autopilot"),
            main.BackgroundTasks(),
        )

    @staticmethod
    def _default_archive(reviewer: str) -> Any:
        from .. import main

        return main.archive_agent(reviewer, main.AgentArchiveIn(outcome="closed"))

    @staticmethod
    def _default_merge(ticket: str) -> Any:
        from .. import github_pr

        return github_pr.merge_pr(ticket)

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
