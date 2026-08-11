from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProviderKind(str, Enum):
    CODEX = "codex"
    CLAUDE = "claude"

    @property
    def legacy_kind(self) -> str:
        return "cdx" if self is ProviderKind.CODEX else "cc"


class LifecycleState(str, Enum):
    STARTING = "starting"
    WORKING = "working"
    WAITING_APPROVAL = "waiting-approval"
    IDLE = "idle"
    INTERRUPTED = "interrupted"
    DEAD = "dead"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class EventDisposition(str, Enum):
    RENDERED = "rendered"
    SUMMARIZED = "summarized"
    IGNORED = "ignored"
    UNKNOWN = "unknown"


class RecoveryAction(str, Enum):
    RESUME = "resume"
    RETAIN = "retain"
    SKIP = "skip"
    BLOCK = "block"


TERMINAL_STATES = frozenset({LifecycleState.DEAD, LifecycleState.COMPLETED})
MAX_MESSAGE_DEDUPE_KEYS = 256
MAX_PENDING_USER_MESSAGES = 32


def _dedupe_entry(item: Any) -> dict[str, str] | None:
    if isinstance(item, str) and item:
        return {"key": item}
    if isinstance(item, dict):
        key = item.get("key")
        if not isinstance(key, str) or not key:
            return None
        entry: dict[str, str] = {"key": key}
        owner = item.get("owner")
        if isinstance(owner, str) and owner:
            entry["owner"] = owner
        return entry
    return None


ALLOWED_STATE_TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.STARTING: frozenset(
        {
            LifecycleState.WORKING,
            LifecycleState.WAITING_APPROVAL,
            LifecycleState.IDLE,
            LifecycleState.INTERRUPTED,
            LifecycleState.DEAD,
            LifecycleState.COMPLETED,
            LifecycleState.BLOCKED,
        }
    ),
    LifecycleState.WORKING: frozenset(
        {
            LifecycleState.WAITING_APPROVAL,
            LifecycleState.IDLE,
            LifecycleState.INTERRUPTED,
            LifecycleState.DEAD,
            LifecycleState.COMPLETED,
            LifecycleState.BLOCKED,
        }
    ),
    LifecycleState.WAITING_APPROVAL: frozenset(
        {
            LifecycleState.WORKING,
            LifecycleState.IDLE,
            LifecycleState.INTERRUPTED,
            LifecycleState.DEAD,
            LifecycleState.COMPLETED,
            LifecycleState.BLOCKED,
        }
    ),
    LifecycleState.IDLE: frozenset(
        {
            LifecycleState.WORKING,
            LifecycleState.WAITING_APPROVAL,
            LifecycleState.INTERRUPTED,
            LifecycleState.DEAD,
            LifecycleState.COMPLETED,
            LifecycleState.BLOCKED,
        }
    ),
    LifecycleState.INTERRUPTED: frozenset(
        {
            LifecycleState.WORKING,
            LifecycleState.IDLE,
            LifecycleState.DEAD,
            LifecycleState.COMPLETED,
            LifecycleState.BLOCKED,
        }
    ),
    LifecycleState.DEAD: frozenset(),
    LifecycleState.COMPLETED: frozenset(),
    LifecycleState.BLOCKED: frozenset(
        {
            LifecycleState.STARTING,
            LifecycleState.WORKING,
            LifecycleState.IDLE,
            LifecycleState.DEAD,
            LifecycleState.COMPLETED,
        }
    ),
}


# This table applies only after the supervisor has established that the run is
# still current, was not replaced, and its recorded provider PID is gone.
RESTART_RECOVERY_TABLE: dict[LifecycleState, RecoveryAction] = {
    LifecycleState.STARTING: RecoveryAction.BLOCK,
    LifecycleState.WORKING: RecoveryAction.RESUME,
    # A supervisor handover closes the provider transport without changing
    # durable run state. The next supervisor can resume the exact session and
    # let the provider re-emit its pending approval request.
    LifecycleState.WAITING_APPROVAL: RecoveryAction.RESUME,
    LifecycleState.IDLE: RecoveryAction.RESUME,
    LifecycleState.INTERRUPTED: RecoveryAction.SKIP,
    LifecycleState.DEAD: RecoveryAction.SKIP,
    LifecycleState.COMPLETED: RecoveryAction.SKIP,
    LifecycleState.BLOCKED: RecoveryAction.SKIP,
}


@dataclass(frozen=True)
class RecoveryDecision:
    action: RecoveryAction
    reason: str
    retryable: bool = False


@dataclass
class RunRecord:
    run_id: str
    agent_id: str
    provider: ProviderKind
    role: str
    model: str
    worktree: str
    auto_archive: bool = False
    backend_base_url: str | None = None
    desired_model: str | None = None
    state: LifecycleState = LifecycleState.STARTING
    effort: str | None = None
    orchestrator_id: str | None = None
    provider_session_id: str | None = None
    provider_pid: int | None = None
    provider_pid_started_at: float | None = None
    provider_executable: str | None = None
    provider_process_group_id: int | None = None
    provider_process_group_members: list[dict[str, Any]] = field(default_factory=list)
    provider_generation: int = 0
    active_turn_id: str | None = None
    current_turn_diff_turn_id: str | None = None
    current_turn_diff_started_seq: int = 0
    current_turn_diff_seq: int = 0
    transcript_path: str | None = None
    initial_prompt: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    state_reason: str | None = None
    recovery_from_state: LifecycleState | None = None
    quiesce_operation_id: str | None = None
    quiesce_resume_state: LifecycleState | None = None
    automatic_resume_suppressed: bool = False
    automatic_resume_guarded_at: str | None = None
    replaces_run_id: str | None = None
    replaced_by_run_id: str | None = None
    replaced_legacy_provider: str | None = None
    outcome: str | None = None
    last_viewed_at: str | None = None
    last_viewed_seq: int | None = None
    auto_archive_terminal_at: str | None = None
    auto_archive_verdict_seq: int | None = None
    start_request_id: str | None = None
    implicit_start_request: bool = False
    # Set before a fresh start is published. It contains the exact registry
    # and status preimage needed to undo a start after a daemon restart.
    start_transaction: dict[str, Any] | None = None
    raw_event_count: int = 0
    normalized_event_count: int = 0
    # Every normalized event bumps ``normalized_event_count`` — including the
    # synthetic user echoes injected by fleet monitor / supervisor steers.
    # The unread-dot surface must not light on those, so we track a parallel
    # counter that advances only on genuinely agent-originated events.
    unread_event_seq: int = 0
    # Despite its legacy name, this is the normalized-sequence component of
    # the causal checkpoint. Pair it with ``last_causal_raw_seq`` so recovery
    # can apply later normalized lifecycle rows that share one raw sequence.
    last_lifecycle_event_seq: int = 0
    # Max ``raw_seq`` of a normalized event whose ORDER-SENSITIVE
    # projections (lifecycle_state, pending_requests, current_turn_diff,
    # composer_messages, unread_event_seq) have been applied to the
    # record. Any later ``append_normalized`` — including
    # ``_normalize_orphan_raw_events`` replaying a stale-order recovery
    # — writes the durable normalized row for observability but only
    # mutates projections when its ``(raw_seq, normalized seq)`` position is
    # at or beyond the causal checkpoint. Equal raw values let one raw event
    # fan out into normalized rows in stable order. This stops a raw_seq=1
    # orphan approval
    # from re-adding a
    # pending_request that raw_seq=2 serverRequest/resolved already
    # cleared, and a raw_seq=1 orphan turn/started from flipping IDLE
    # back to WORKING after raw_seq=2 turn/completed already landed
    # (WIKI-232 REVIEW11 H1). Suppression covers every later causal
    # event, not only lifecycle events.
    last_causal_raw_seq: int = 0
    disposition_counts: dict[str, int] = field(
        default_factory=lambda: {item.value: 0 for item in EventDisposition}
    )
    pending_requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_user_messages: list[dict[str, str]] = field(default_factory=list)
    composer_messages: list[dict[str, Any]] = field(default_factory=list)
    queued_messages: list[dict[str, str]] = field(default_factory=list)
    # Entries: {"key": str, "owner": str | None}. Older on-disk snapshots
    # stored bare strings; from_dict() promotes them to owner-less entries so
    # a legacy claim remains unretryable, while post-WIKI-232 claims can bind
    # a stable owner (steer effect_id) and safely replay after a crash.
    message_dedupe_keys: list[dict[str, str]] = field(default_factory=list)
    schema_version: int = 1

    @classmethod
    def new(
        cls,
        *,
        agent_id: str,
        provider: ProviderKind,
        role: str,
        auto_archive: bool = False,
        model: str,
        worktree: str,
        prompt: str,
        effort: str | None = None,
        orchestrator_id: str | None = None,
        replaces_run_id: str | None = None,
        backend_base_url: str | None = None,
        run_id: str | None = None,
        start_request_id: str | None = None,
        implicit_start_request: bool = False,
    ) -> RunRecord:
        return cls(
            run_id=run_id or str(uuid4()),
            agent_id=agent_id,
            provider=provider,
            role=role,
            auto_archive=auto_archive,
            model=model,
            worktree=worktree,
            backend_base_url=backend_base_url,
            initial_prompt=prompt,
            effort=effort,
            orchestrator_id=orchestrator_id,
            replaces_run_id=replaces_run_id,
            start_request_id=start_request_id,
            implicit_start_request=implicit_start_request,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "provider": self.provider.value,
            "role": self.role,
            "auto_archive": self.auto_archive,
            "model": self.model,
            "desired_model": self.desired_model,
            "effort": self.effort,
            "worktree": self.worktree,
            "backend_base_url": self.backend_base_url,
            "orchestrator_id": self.orchestrator_id,
            "state": self.state.value,
            "state_reason": self.state_reason,
            "recovery_from_state": (
                self.recovery_from_state.value if self.recovery_from_state else None
            ),
            "quiesce_operation_id": self.quiesce_operation_id,
            "quiesce_resume_state": (
                self.quiesce_resume_state.value if self.quiesce_resume_state else None
            ),
            "automatic_resume_suppressed": self.automatic_resume_suppressed,
            "automatic_resume_guarded_at": self.automatic_resume_guarded_at,
            "provider_session_id": self.provider_session_id,
            "provider_pid": self.provider_pid,
            "provider_pid_started_at": self.provider_pid_started_at,
            "provider_executable": self.provider_executable,
            "provider_process_group_id": self.provider_process_group_id,
            "provider_process_group_members": list(self.provider_process_group_members),
            "provider_generation": self.provider_generation,
            "active_turn_id": self.active_turn_id,
            "current_turn_diff_turn_id": self.current_turn_diff_turn_id,
            "current_turn_diff_started_seq": self.current_turn_diff_started_seq,
            "current_turn_diff_seq": self.current_turn_diff_seq,
            "transcript_path": self.transcript_path,
            "initial_prompt": self.initial_prompt,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "replaces_run_id": self.replaces_run_id,
            "replaced_by_run_id": self.replaced_by_run_id,
            "replaced_legacy_provider": self.replaced_legacy_provider,
            "outcome": self.outcome,
            "last_viewed_at": self.last_viewed_at,
            "last_viewed_seq": self.last_viewed_seq,
            "auto_archive_terminal_at": self.auto_archive_terminal_at,
            "auto_archive_verdict_seq": self.auto_archive_verdict_seq,
            "start_request_id": self.start_request_id,
            "implicit_start_request": self.implicit_start_request,
            "start_transaction": self.start_transaction,
            "raw_event_count": self.raw_event_count,
            "normalized_event_count": self.normalized_event_count,
            "unread_event_seq": self.unread_event_seq,
            "last_lifecycle_event_seq": self.last_lifecycle_event_seq,
            "last_causal_raw_seq": self.last_causal_raw_seq,
            "disposition_counts": dict(self.disposition_counts),
            "pending_requests": {
                key: dict(request) for key, request in self.pending_requests.items()
            },
            "pending_user_messages": list(self.pending_user_messages),
            "composer_messages": list(self.composer_messages),
            "queued_messages": list(self.queued_messages),
            "message_dedupe_keys": list(self.message_dedupe_keys),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RunRecord:
        pending_requests = value.get("pending_requests")
        if not isinstance(pending_requests, dict):
            pending_requests = {}
        return cls(
            schema_version=int(value.get("schema_version", 1)),
            run_id=str(value["run_id"]),
            agent_id=str(value["agent_id"]),
            provider=ProviderKind(value["provider"]),
            role=str(value["role"]),
            auto_archive=bool(value.get("auto_archive", False)),
            model=str(value["model"]),
            desired_model=value.get("desired_model"),
            effort=value.get("effort"),
            worktree=str(value["worktree"]),
            backend_base_url=value.get("backend_base_url"),
            orchestrator_id=value.get("orchestrator_id"),
            state=LifecycleState(value.get("state", LifecycleState.STARTING.value)),
            state_reason=value.get("state_reason"),
            recovery_from_state=(
                LifecycleState(value["recovery_from_state"])
                if value.get("recovery_from_state")
                else None
            ),
            quiesce_operation_id=value.get("quiesce_operation_id"),
            quiesce_resume_state=(
                LifecycleState(value["quiesce_resume_state"])
                if value.get("quiesce_resume_state")
                else None
            ),
            automatic_resume_suppressed=bool(
                value.get("automatic_resume_suppressed", False)
            ),
            automatic_resume_guarded_at=value.get("automatic_resume_guarded_at"),
            provider_session_id=value.get("provider_session_id"),
            provider_pid=value.get("provider_pid"),
            provider_pid_started_at=value.get("provider_pid_started_at"),
            provider_executable=value.get("provider_executable"),
            provider_process_group_id=value.get("provider_process_group_id"),
            provider_process_group_members=[
                dict(item)
                for item in value.get("provider_process_group_members", [])
                if isinstance(item, dict)
            ],
            provider_generation=int(value.get("provider_generation", 0)),
            active_turn_id=value.get("active_turn_id"),
            current_turn_diff_turn_id=value.get("current_turn_diff_turn_id"),
            current_turn_diff_started_seq=int(value.get("current_turn_diff_started_seq", 0)),
            current_turn_diff_seq=int(value.get("current_turn_diff_seq", 0)),
            transcript_path=value.get("transcript_path"),
            initial_prompt=value.get("initial_prompt"),
            created_at=str(value.get("created_at") or utc_now()),
            updated_at=str(value.get("updated_at") or utc_now()),
            replaces_run_id=value.get("replaces_run_id"),
            replaced_by_run_id=value.get("replaced_by_run_id"),
            replaced_legacy_provider=value.get("replaced_legacy_provider"),
            outcome=value.get("outcome"),
            last_viewed_at=value.get("last_viewed_at"),
            last_viewed_seq=(
                int(value["last_viewed_seq"])
                if isinstance(value.get("last_viewed_seq"), int)
                else None
            ),
            auto_archive_terminal_at=value.get("auto_archive_terminal_at"),
            auto_archive_verdict_seq=(
                int(value["auto_archive_verdict_seq"])
                if isinstance(value.get("auto_archive_verdict_seq"), int)
                else None
            ),
            start_request_id=value.get("start_request_id"),
            implicit_start_request=bool(value.get("implicit_start_request", False)),
            start_transaction=(
                dict(value["start_transaction"])
                if isinstance(value.get("start_transaction"), dict)
                else None
            ),
            raw_event_count=int(value.get("raw_event_count", 0)),
            normalized_event_count=int(value.get("normalized_event_count", 0)),
            unread_event_seq=int(
                value.get(
                    "unread_event_seq",
                    value.get("normalized_event_count", 0),
                )
            ),
            last_lifecycle_event_seq=int(value.get("last_lifecycle_event_seq", 0)),
            last_causal_raw_seq=int(value.get("last_causal_raw_seq", 0)),
            disposition_counts={
                item.value: int(
                    (value.get("disposition_counts") or {}).get(item.value, 0)
                )
                for item in EventDisposition
            },
            pending_requests={
                str(key): dict(request)
                for key, request in pending_requests.items()
                if isinstance(request, dict)
            },
            pending_user_messages=[
                dict(item) for item in value.get("pending_user_messages") or []
            ],
            composer_messages=[
                dict(item) for item in value.get("composer_messages") or []
            ],
            queued_messages=[dict(item) for item in value.get("queued_messages") or []],
            message_dedupe_keys=[
                entry
                for entry in (
                    _dedupe_entry(item)
                    for item in value.get("message_dedupe_keys") or []
                )
                if entry is not None
            ],
        )


def validate_transition(current: LifecycleState, target: LifecycleState) -> None:
    if current is target:
        return
    if target not in ALLOWED_STATE_TRANSITIONS[current]:
        raise ValueError(
            f"invalid lifecycle transition: {current.value} -> {target.value}"
        )


def restart_recovery_decision(
    record: RunRecord,
    *,
    is_current: bool,
    provider_pid_alive: bool,
    provider_control_attached: bool = False,
) -> RecoveryDecision:
    """Decide whether a run is eligible for automatic supervisor recovery.

    The state table for a *current, non-replaced run whose provider PID is
    gone* is deliberately small and closed:

    | last state       | restart action |
    |------------------|----------------|
    | starting         | block          |
    | working          | resume         |
    | waiting-approval | block          |
    | idle             | resume         |
    | interrupted      | skip           |
    | dead             | skip           |
    | completed        | skip           |
    | blocked          | skip           |

    Codex resumes by exact thread id and reuses its rollout file. Claude also
    resumes by explicit session id. Replaced, completed, and dead runs never
    revive. A still-live PID is retained only when this supervisor still owns
    its adapter control channel. A PID alone is not liveness: after supervisor
    restart it may be an orphan or reused PID, so the run is blocked rather
    than duplicated until an adapter can verify process identity. For a
    working, waiting-approval, or idle run, that block preserves
    `recovery_from_state`; the daemon rechecks the PID and resumes the exact
    session once it exits.

    The supervisor guards an automatic resume until its replacement control
    stream remains attached for the configured stability window. A failed or
    immediately dying resume is not retried every polling tick; its original
    working, waiting-approval, or idle intent remains available for an
    explicit operator resume.
    """

    if not is_current:
        return RecoveryDecision(
            RecoveryAction.SKIP, "run is not the registry current run"
        )
    if record.replaced_by_run_id:
        return RecoveryDecision(RecoveryAction.SKIP, "run was replaced")
    if record.state in TERMINAL_STATES:
        return RecoveryDecision(RecoveryAction.SKIP, f"run is {record.state.value}")
    recovery_state = record.recovery_from_state or record.state
    if provider_pid_alive and provider_control_attached:
        return RecoveryDecision(RecoveryAction.RETAIN, "provider PID is still alive")
    if provider_pid_alive:
        return RecoveryDecision(
            RecoveryAction.BLOCK,
            "provider PID is live but its control channel is not attached",
            retryable=(
                recovery_state
                in {
                    LifecycleState.WORKING,
                    LifecycleState.WAITING_APPROVAL,
                    LifecycleState.IDLE,
                }
                and bool(record.provider_session_id)
            ),
        )

    action = RESTART_RECOVERY_TABLE[recovery_state]
    if action is RecoveryAction.RESUME and not record.provider_session_id:
        return RecoveryDecision(
            RecoveryAction.BLOCK, "eligible state has no provider session id"
        )
    reasons = {
        RecoveryAction.RESUME: "current working/idle run has a dead provider PID",
        RecoveryAction.BLOCK: (
            "provider start did not finish"
            if recovery_state is LifecycleState.STARTING
            else "pending approval session will be resumed"
        ),
        RecoveryAction.SKIP: f"state {recovery_state.value} is not auto-resumable",
    }
    return RecoveryDecision(action, reasons[action])
