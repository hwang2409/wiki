"""Ticket-level composite-health monitoring for the fleet monitor."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .ticket import base_ticket


logger = logging.getLogger(__name__)


GRAPH_UNAVAILABLE_SPAWN_GRACE_SECONDS = 30.0


def _workgraph_module():
    """Load graph support lazily so backend-only daemon bundles still boot."""

    from .. import workgraph

    return workgraph


def default_clock() -> float:
    try:
        return _workgraph_module().CLOCK()
    except ModuleNotFoundError:
        return datetime.now(timezone.utc).timestamp()


def iteration_cap_for(graph: dict[str, Any]) -> int:
    """Resolve the iteration cap: explicit -> template -> module default.

    Shared with :mod:`loop_state` so ticket chrome and the monitor agree on
    the cap when computing danger tiers.
    """

    graph_module = _workgraph_module()
    direct = graph.get("iteration_cap")
    if isinstance(direct, int) and not isinstance(direct, bool) and direct > 0:
        return direct
    template_id = graph.get("template")
    if isinstance(template_id, str):
        try:
            from wiki_cli import workgraph_templates

            template = workgraph_templates.load_templates().get(template_id)
        except workgraph_templates.TemplateSelectionError:
            template = None
        if isinstance(template, dict):
            cap = template.get("iteration_cap")
            if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0:
                return cap
    return graph_module.DEFAULT_ITERATION_CAP


def load_validated_graph(
    ticket: str,
    *,
    status_dir: Any,
    snapshot_dir: Any | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Load one graph through the fleet monitor's hot/snapshot fallback.

    The fleet graph endpoint uses the same fail-closed loader as composite
    health.  ``source`` is ``live`` for a hot graph and ``snapshot`` for a
    durable archive; callers can use it to classify edges without inspecting
    the filesystem themselves.
    """

    try:
        graph_module = _workgraph_module()
    except ModuleNotFoundError:
        return None, None

    def valid(candidate: object) -> dict[str, Any] | None:
        if not isinstance(candidate, dict):
            return None
        try:
            violations = graph_module.graph_lint.validate_document(candidate, "workgraph")
        except Exception:
            logger.exception("fleet_graph: could not validate graph for %s", ticket)
            return None
        return candidate if not violations else None

    try:
        graph = graph_module.load_workgraph(ticket, status_dir)
    except graph_module.WorkgraphCorruptError:
        graph = None
    if graph is not None:
        graph = valid(graph)
        if graph is not None:
            return graph, "live"

    try:
        graph = graph_module.load_snapshot(ticket, snapshot_dir)
    except Exception:
        logger.exception("fleet_graph: could not load snapshot for %s", ticket)
        return None, None
    graph = valid(graph)
    return (graph, "snapshot") if graph is not None else (None, None)


@dataclass
class GraphHealthSnapshot:
    """Alarm state for one ticket-level composite-health scan."""

    last_blocking_no_reviewer_alarm_at: float | None = None
    blocking_no_reviewer_active: bool = False
    blocking_no_reviewer_episode_marker: str | None = None
    graph_unavailable_active: bool = False
    graph_unavailable_episode: int = 0
    last_graph_unavailable_alarm_at: float | None = None
    last_clock: float | None = None
    stall_active: bool = False
    stall_episode_marker: str | None = None
    stall_append_acknowledged: bool = False
    stall_notification_sent: bool = False
    iteration_active: bool = False
    iteration_episode_marker: str | None = None
    iteration_append_acknowledged: bool = False
    iteration_notification_sent: bool = False


EmitNotification = Callable[..., Awaitable[Any]]


class GraphHealthMonitor:
    """Load workgraphs and deliver durable composite-health alarms."""

    def __init__(
        self,
        *,
        store: Any,
        emit: EmitNotification,
        graph_health_realarm: float,
        review_route_suppression: float,
        send_timeout: float,
    ) -> None:
        self.store = store
        self.emit = emit
        self.graph_health_realarm = graph_health_realarm
        self.review_route_suppression = review_route_suppression
        self.send_timeout = send_timeout
        self.snapshots: dict[str, GraphHealthSnapshot] = {}
        self._degraded_tickets: set[str] = set()

    async def process(self, views: list[Any], now: float) -> list[Any]:
        """Run the detector once for each normalized ticket represented by views."""

        by_ticket: dict[str, Any] = {}
        views_by_ticket: dict[str, list[Any]] = {}
        for view in views:
            ticket = base_ticket(view.record.agent_id)
            views_by_ticket.setdefault(ticket, []).append(view)
            prior = by_ticket.get(ticket)
            if prior is None or view.record.agent_id == ticket:
                by_ticket[ticket] = view

        results: list[Any] = []
        for ticket, view in by_ticket.items():
            ticket_views = views_by_ticket[ticket]
            graph = self.load_graph(ticket)
            if graph is None:
                results.extend(
                    await self._maybe_graph_unavailable(
                        ticket, view, ticket_views, now
                    )
                )
                continue
            state = self.snapshots.setdefault(ticket, GraphHealthSnapshot())
            if ticket in self._degraded_tickets:
                results.extend(
                    await self._maybe_graph_unavailable(
                        ticket, view, ticket_views, now
                    )
                )
            else:
                state.graph_unavailable_active = False
                state.last_graph_unavailable_alarm_at = None
            try:
                result = await self._maybe_graph_health(
                    ticket, view, ticket_views, graph, now
                )
            except Exception:
                logger.exception("fleet_monitor: graph health scan failed for %s", ticket)
                continue
            results.extend(result)
        return results

    def load_graph(self, ticket: str) -> dict[str, Any] | None:
        """Load a validated hot graph, falling back to a validated snapshot."""

        self._degraded_tickets.discard(ticket)
        graph, _source = load_validated_graph(
            ticket,
            status_dir=self.store.paths.status_dir,
        )
        if graph is None or _source != "live":
            self._degraded_tickets.add(ticket)
        return graph

    _iteration_cap = staticmethod(iteration_cap_for)

    @staticmethod
    def _edge_marker(edge: dict[str, Any], index: int) -> str:
        request_id = edge.get("request_id")
        identity = (
            request_id
            if isinstance(request_id, str) and request_id
            else edge.get("created_at")
        )
        return f"{index}:{edge.get('kind')}:{identity}"

    @classmethod
    def _latest_verdict_marker(cls, graph: dict[str, Any]) -> str | None:
        for index in range(len(graph.get("edges", [])) - 1, -1, -1):
            edge = graph["edges"][index]
            if isinstance(edge, dict) and edge.get("kind") == "verdict":
                return cls._edge_marker(edge, index)
        return None

    @staticmethod
    def _latest_verdict_route_time(graph: dict[str, Any], ticket: str) -> float | None:
        """Return the route timestamp only when it follows the latest verdict."""

        edges = graph.get("edges", [])
        latest_verdict_index = None
        for index in range(len(edges) - 1, -1, -1):
            edge = edges[index]
            if isinstance(edge, dict) and edge.get("kind") == "verdict":
                latest_verdict_index = index
                break
        if latest_verdict_index is None:
            return None
        for edge in edges[latest_verdict_index + 1 :]:
            if not isinstance(edge, dict) or edge.get("kind") != "steer":
                continue
            payload = edge.get("payload")
            target = payload.get("target_worker") if isinstance(payload, dict) else None
            if target != ticket and edge.get("to") != ticket:
                continue
            timestamp = _workgraph_module()._parse_ts(edge.get("created_at"))  # noqa: SLF001
            if timestamp is not None:
                return timestamp
        return None

    @staticmethod
    def _episode_digest(marker: str) -> str:
        return hashlib.sha256(marker.encode("utf-8")).hexdigest()[:20]

    @classmethod
    def _stall_episode_marker(cls, graph: dict[str, Any]) -> str:
        graph_module = _workgraph_module()
        candidates: list[tuple[float, str, int, str]] = []
        for node in graph_module._live_worker_nodes(graph):  # noqa: SLF001
            node_id = node.get("id") if isinstance(node, dict) else None
            if not isinstance(node_id, str):
                continue
            latest: tuple[float, int, str] | None = None
            for index, edge in enumerate(graph.get("edges", [])):
                if not isinstance(edge, dict) or node_id not in (
                    edge.get("from"),
                    edge.get("to"),
                ):
                    continue
                timestamp = graph_module._parse_ts(edge.get("created_at"))  # noqa: SLF001
                if timestamp is None:
                    continue
                marker = GraphHealthMonitor._edge_marker(edge, index)
                if latest is None or timestamp > latest[0] or (
                    timestamp == latest[0] and index > latest[1]
                ):
                    latest = (timestamp, index, marker)
            if latest is None:
                candidates.append((float("-inf"), node_id, -1, f"{node_id}:none"))
            else:
                candidates.append((latest[0], node_id, latest[1], latest[2]))
        if not candidates:
            return "no-live-worker"
        _timestamp, node_id, index, marker = min(candidates)
        return f"stall:{node_id}:{index}:{marker}"

    @classmethod
    def _episode_marker(
        cls, graph: dict[str, Any], condition: str, iteration_cap: int
    ) -> str:
        if condition == "stall":
            marker = cls._stall_episode_marker(graph)
        elif condition == "iteration-cap":
            verdicts = [
                (index, edge)
                for index, edge in enumerate(graph.get("edges", []))
                if isinstance(edge, dict) and edge.get("kind") == "verdict"
            ]
            onset = verdicts[iteration_cap] if len(verdicts) > iteration_cap else None
            marker = (
                cls._edge_marker(onset[1], onset[0])
                if onset is not None
                else "iteration-cap"
            )
        else:
            marker = cls._latest_verdict_marker(graph) or "blocking-no-reviewer"
        return cls._episode_digest(f"{condition}:{marker}")

    @staticmethod
    def _recent(timestamp: float | None, now: float, threshold: float) -> bool:
        if timestamp is None:
            return False
        age = now - timestamp
        return 0 <= age < threshold

    @classmethod
    def _newest_spawn_age(cls, views: list[Any], now: float) -> float | None:
        graph_module = _workgraph_module()
        newest: float | None = None
        for view in views:
            timestamp = graph_module._parse_ts(  # noqa: SLF001
                getattr(view.record, "created_at", None)
            )
            if timestamp is not None and (newest is None or timestamp > newest):
                newest = timestamp
        return now - newest if newest is not None else None

    @staticmethod
    def _run_event_mtimes(view: Any, store: Any) -> list[float]:
        timestamps: list[float] = []
        for method_name in ("raw_events_path", "normalized_events_path"):
            path_factory = getattr(store, method_name, None)
            if not callable(path_factory):
                continue
            try:
                timestamps.append(path_factory(view.record.run_id).stat().st_mtime)
            except (FileNotFoundError, OSError):
                continue
        return timestamps

    @classmethod
    def _stall_activity_is_fresh(
        cls,
        graph: dict[str, Any],
        views: list[Any],
        store: Any,
        now: float,
        threshold: float,
    ) -> bool:
        graph_module = _workgraph_module()
        stalled_nodes: list[str] = []
        for node in graph_module._live_worker_nodes(graph):  # noqa: SLF001
            node_id = node.get("id") if isinstance(node, dict) else None
            if not isinstance(node_id, str):
                continue
            latest: float | None = None
            for edge in graph.get("edges", []):
                if not isinstance(edge, dict) or node_id not in (
                    edge.get("from"),
                    edge.get("to"),
                ):
                    continue
                timestamp = graph_module._parse_ts(edge.get("created_at"))  # noqa: SLF001
                if timestamp is not None and (latest is None or timestamp > latest):
                    latest = timestamp
            if latest is not None and now - latest > threshold:
                stalled_nodes.append(node_id)
        if not stalled_nodes:
            return False
        views_by_worker: dict[str, list[Any]] = {}
        for view in views:
            views_by_worker.setdefault(view.record.agent_id, []).append(view)
        for node_id in stalled_nodes:
            matching_views = views_by_worker.get(node_id)
            # A graph node without a current run is not observable. Treat it
            # as stale instead of borrowing activity from a sibling worker.
            if not matching_views:
                return False
            for view in matching_views:
                activity = [view.status_mtime]
                activity.extend(cls._run_event_mtimes(view, store))
                if any(
                    cls._recent(timestamp, now, threshold) for timestamp in activity
                ):
                    break
            else:
                return False
        return True

    async def _maybe_graph_unavailable(
        self, ticket: str, view: Any, views: list[Any], now: float
    ) -> list[Any]:
        state = self.snapshots.setdefault(ticket, GraphHealthSnapshot())
        if state.last_clock is not None and now < state.last_clock:
            state.last_graph_unavailable_alarm_at = None
            state.graph_unavailable_episode += 1
        state.last_clock = now
        if not state.graph_unavailable_active:
            state.graph_unavailable_active = True
            state.graph_unavailable_episode += 1
            state.last_graph_unavailable_alarm_at = None
        spawn_age = self._newest_spawn_age(views, now)
        if spawn_age is not None and 0 <= spawn_age < GRAPH_UNAVAILABLE_SPAWN_GRACE_SECONDS:
            state.last_graph_unavailable_alarm_at = None
            return []
        last = state.last_graph_unavailable_alarm_at
        if last is not None and now - last < self.graph_health_realarm:
            return []
        window = int(now // max(self.graph_health_realarm, 1.0))
        notif = await self.emit(
            view,
            event_type="graph-unavailable",
            message=(
                f"[fleet] {ticket}: workgraph unavailable or invalid. "
                "Restore the graph before relying on composite health."
            ),
            dedupe_key=(
                f"fleet:{ticket}:graph-unavailable:{state.graph_unavailable_episode}:{window}"
            ),
        )
        if notif is not None:
            state.last_graph_unavailable_alarm_at = now
            return [notif]
        return []

    async def _maybe_graph_health(
        self,
        ticket: str,
        view: Any,
        views: list[Any],
        graph: dict[str, Any],
        now: float,
    ) -> list[Any]:
        graph_module = _workgraph_module()
        state = self.snapshots.setdefault(ticket, GraphHealthSnapshot())
        if state.last_clock is not None and now < state.last_clock:
            state.last_blocking_no_reviewer_alarm_at = None
            state.blocking_no_reviewer_active = False
            state.stall_active = False
            state.stall_episode_marker = None
            state.stall_append_acknowledged = False
            state.stall_notification_sent = False
            state.iteration_active = False
            state.iteration_episode_marker = None
            state.iteration_append_acknowledged = False
            state.iteration_notification_sent = False
        state.last_clock = now
        cap = self._iteration_cap(graph)
        current = graph_module.current_health(graph, iteration_cap=cap, now_ts=now)
        health = current["health"]
        alarms = {alarm["check"]: alarm for alarm in current["alarms"]}
        results: list[Any] = []

        no_reviewer = "blocking_no_reviewer" in alarms
        if not no_reviewer:
            state.last_blocking_no_reviewer_alarm_at = None
            state.blocking_no_reviewer_active = False
            state.blocking_no_reviewer_episode_marker = None
        else:
            marker = self._episode_marker(graph, "blocking-no-reviewer", cap)
            if not state.blocking_no_reviewer_active or (
                state.blocking_no_reviewer_episode_marker != marker
            ):
                state.blocking_no_reviewer_active = True
                state.blocking_no_reviewer_episode_marker = marker
            routed_at = self._latest_verdict_route_time(graph, ticket)
            route_age = now - routed_at if routed_at is not None else None
            suppressed = route_age is not None and 0 <= route_age < self.review_route_suppression
            last = state.last_blocking_no_reviewer_alarm_at
            if not suppressed and (
                last is None or now - last >= self.graph_health_realarm
            ):
                blocking = health.get("blocking", 0)
                window = int(now // max(self.graph_health_realarm, 1.0))
                notif = await self.emit(
                    view,
                    event_type="graph-health",
                    message=(
                        f"[fleet] {ticket}: graph health has {blocking} BLOCKING "
                        "finding(s) and no live reviewer. Spawn a reviewer or "
                        "route the review."
                    ),
                    dedupe_key=(
                        f"fleet:{ticket}:graph-health:blocking-no-reviewer:"
                        f"{marker}:{window}"
                    ),
                )
                if notif is not None:
                    state.last_blocking_no_reviewer_alarm_at = now
                    results.append(notif)

        if "node_stall" not in alarms:
            state.stall_active = False
            state.stall_episode_marker = None
            state.stall_append_acknowledged = False
            state.stall_notification_sent = False
        else:
            marker = self._episode_marker(graph, "stall", cap)
            if not state.stall_active or state.stall_episode_marker != marker:
                state.stall_active = True
                state.stall_episode_marker = marker
                state.stall_append_acknowledged = False
                state.stall_notification_sent = False
            stall = health.get("slowest_node_stall_seconds", 0)
            stall_activity_fresh = self._stall_activity_is_fresh(
                graph,
                views,
                self.store,
                now,
                graph_module.STALL_ALARM_SECONDS,
            )
            if not stall_activity_fresh and not state.stall_append_acknowledged:
                state.stall_append_acknowledged = await self._append_escalation(
                    ticket,
                    view,
                    reason=f"node stall exceeded {graph_module.STALL_ALARM_SECONDS}s ({stall}s)",
                    prior_findings=self._open_findings(graph),
                    condition="stall",
                    episode=marker,
                    graph=graph,
                    now=now,
                )
            if (
                not stall_activity_fresh
                and state.stall_append_acknowledged
                and not state.stall_notification_sent
            ):
                notif = await self.emit(
                    view,
                    event_type="graph-health-stall",
                    message=(
                        f"[fleet] {ticket}: graph node stalled {stall}s "
                        f"(> {graph_module.STALL_ALARM_SECONDS}s); escalation sent to Henry."
                    ),
                    dedupe_key=f"fleet:{ticket}:graph-health:stall:{marker}",
                )
                if notif is not None:
                    state.stall_notification_sent = True
                    results.append(notif)

        if "iteration_cap" not in alarms:
            state.iteration_active = False
            state.iteration_episode_marker = None
            state.iteration_append_acknowledged = False
            state.iteration_notification_sent = False
        else:
            marker = self._episode_marker(graph, "iteration-cap", cap)
            if not state.iteration_active or state.iteration_episode_marker != marker:
                state.iteration_active = True
                state.iteration_episode_marker = marker
                state.iteration_append_acknowledged = False
                state.iteration_notification_sent = False
            iterations = health.get("iteration_count", 0)
            if not state.iteration_append_acknowledged:
                state.iteration_append_acknowledged = await self._append_escalation(
                    ticket,
                    view,
                    reason=f"iteration count exceeded cap {cap} ({iterations})",
                    prior_findings=self._open_findings(graph),
                    condition="iteration-cap",
                    episode=marker,
                    graph=graph,
                    now=now,
                )
            if state.iteration_append_acknowledged and not state.iteration_notification_sent:
                notif = await self.emit(
                    view,
                    event_type="graph-health-iteration-cap",
                    message=(
                        f"[fleet] {ticket}: iteration count {iterations} exceeds "
                        f"cap {cap}; escalation sent to Henry."
                    ),
                    dedupe_key=f"fleet:{ticket}:graph-health:iteration-cap:{marker}",
                )
                if notif is not None:
                    state.iteration_notification_sent = True
                    results.append(notif)
        return results

    @staticmethod
    def _open_findings(graph: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            finding
            for finding in _workgraph_module().collect_findings(graph).values()
            if not finding.get("resolved_by")
        ]

    async def _append_escalation(
        self,
        ticket: str,
        view: Any,
        *,
        reason: str,
        prior_findings: list[dict[str, Any]],
        condition: str,
        episode: str,
        graph: dict[str, Any],
        now: float,
    ) -> bool:
        orch = view.record.orchestrator_id
        from .. import workgraph_service

        request_id = f"fleet:{ticket}:graph-health:{condition}:{episode}"
        if any(
            isinstance(edge, dict)
            and edge.get("kind") == "escalation"
            and edge.get("request_id") == request_id
            for edge in graph.get("edges", [])
        ):
            return True
        ack = workgraph_service.record_escalation(
            ticket=ticket,
            orch=orch,
            reason=reason,
            prior_findings=prior_findings,
            target="henry",
            request_id=request_id,
            status_dir=self.store.paths.status_dir,
            now_ts=now,
            wait_for_delivery=True,
        )
        if ack is None:
            logger.error("fleet_monitor: escalation append was not accepted for %s", ticket)
            return False
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(ack)), timeout=self.send_timeout
            )
        except Exception as exc:
            logger.warning(
                "fleet_monitor: escalation append failed for %s (%s): %s",
                ticket,
                condition,
                exc,
            )
            return False
        return True
