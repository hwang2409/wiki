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
from .autopilot_actions import steer_action_id
from .autopilot_parser import (
    Finding,
    Verdict,
    build_steer_message,
    parse_verdict,
    verdict_from_graph,
)
from .autopilot_policy import merge_authorized, repository_from_pr_url


DEFAULT_ITERATION_CAP = 8
DEFAULT_PLATEAU_GUARD = 3
_PR = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/(\d+)")


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
    action_stages: dict[str, dict[str, Any]] = field(default_factory=dict)
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
            "action_stages": self.action_stages,
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
            "action_stages": {
                str(key): dict(stage)
                for key, stage in (value.get("action_stages") or {}).items()
                if isinstance(key, str) and isinstance(stage, Mapping)
            }
            if isinstance(value.get("action_stages"), Mapping)
            else {},
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
        enabled_state: AutopilotState
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
            enabled_state = current
        status = self.status_reader(ticket)
        if isinstance(status, Mapping) and status.get("state") == "merge-ready":
            graph = self.graph_loader(ticket)
            reviewer = self._current_reviewer(graph)
            agent_id = reviewer or ticket
            event = {
                "agent_id": agent_id,
                "run_id": status.get("run_id") or "autopilot-enable",
                "status_state": "merge-ready",
                "status_mtime": time.time_ns(),
                "sha": status.get("sha"),
                "pr": status.get("pr"),
                "verdict_path": status.get("verdict_path") or status.get("artifact_path"),
            }
            asyncio.run(self.on_transition(event))
            enabled_state = self.store.load(ticket)
        return enabled_state.to_dict()

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
        status = self.status_reader(ticket)
        sha = self._current_sha(ticket, status, event)
        verdict = self._latest_verdict(graph, reviewer=reviewer)
        if verdict is None:
            verdict = self._read_verdict_file(
                reviewer,
                event.get("verdict_path"),
                source_sha=sha,
            )
        if verdict is None:
            return False
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
            action_id = steer_action_id(ticket, reviewer, verdict)
            stage = state.action_stages.setdefault(
                action_id,
                {
                    "reviewer": reviewer,
                    "target": ticket,
                    "request_id": action_id,
                    "steer": "pending",
                    "archive": "pending",
                },
            )
            self.store.save(ticket, state)
            if stage.get("steer") != "done":
                await self._send_steer(
                    ticket, reviewer, verdict, message, request_id=action_id
                )
                stage["steer"] = "done"
                self._log(
                    state,
                    "steer-sent",
                    {
                        "reviewer": reviewer,
                        "target": ticket,
                        "source_sha": verdict.source_sha,
                        "preview": message[:500],
                        "finding_count": len(verdict.findings),
                        "request_id": action_id,
                    },
                )
                self.store.save(ticket, state)
            if stage.get("archive") != "done":
                await self._invoke(self.archive, reviewer)
                stage["archive"] = "done"
                self._log(
                    state,
                    "reviewer-archived",
                    {"reviewer": reviewer, "request_id": action_id},
                )
                self.store.save(ticket, state)
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
        if not verdict.clean:
            self._log(
                state,
                "merge-blocked-nonclean-verdict",
                {"verdict": verdict.to_dict()},
            )
            return True
        status = self.status_reader(ticket)
        pr = status.get("pr") if isinstance(status, Mapping) else None
        pr_url = pr if isinstance(pr, str) else None
        sha = self._current_sha(ticket, status, {})
        if pr_url is None or not isinstance(sha, str):
            return False
        repository = repository_from_pr_url(pr_url)
        if not merge_authorized(pr_url):
            self._log(
                state,
                "merge-blocked-repository-policy",
                {"pr": pr_url, "repository": repository},
            )
            return True
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
        self,
        ticket: str,
        source_worker: str,
        verdict: Verdict,
        message: str,
        *,
        request_id: str | None = None,
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
        return await self._invoke(
            self.steer,
            ticket,
            message,
            findings,
            request_id=request_id,
        )

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
    def _read_verdict_file(
        reviewer: str,
        artifact_path: Any = None,
        *,
        source_sha: str | None = None,
    ) -> Verdict | None:
        path = (
            Path(artifact_path)
            if isinstance(artifact_path, str) and artifact_path
            else Path("/tmp") / f"{reviewer}-verdict.json"
        )
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            return parse_verdict(raw, source_sha=source_sha)
        if not isinstance(value, Mapping):
            return None
        text = value.get("text") or value.get("content")
        if isinstance(text, str):
            return parse_verdict(text, source_sha=source_sha or value.get("source_sha"))
        return verdict_from_graph({**value, "source_sha": value.get("source_sha") or source_sha})

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
        ticket: str,
        message: str,
        findings: list[dict[str, Any]] | None = None,
        *,
        request_id: str | None = None,
    ) -> Any:
        from .. import main

        return main.agent_message(
            ticket,
            main.MessageIn(
                text=message,
                mode="now",
                source="autopilot",
                findings=findings,
                request_id=request_id,
                dedupe_key=request_id,
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
