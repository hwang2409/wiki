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
    LifecycleState.WAITING_APPROVAL: RecoveryAction.BLOCK,
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
    backend_base_url: str | None = None
    desired_model: str | None = None
    state: LifecycleState = LifecycleState.STARTING
    effort: str | None = None
    orchestrator_id: str | None = None
    provider_session_id: str | None = None
    provider_pid: int | None = None
    provider_generation: int = 0
    active_turn_id: str | None = None
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
    outcome: str | None = None
    raw_event_count: int = 0
    normalized_event_count: int = 0
    last_lifecycle_event_seq: int = 0
    disposition_counts: dict[str, int] = field(
        default_factory=lambda: {item.value: 0 for item in EventDisposition}
    )
    pending_requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_user_messages: list[dict[str, str]] = field(default_factory=list)
    composer_messages: list[dict[str, Any]] = field(default_factory=list)
    queued_messages: list[dict[str, str]] = field(default_factory=list)
    message_dedupe_keys: list[str] = field(default_factory=list)
    schema_version: int = 1

    @classmethod
    def new(
        cls,
        *,
        agent_id: str,
        provider: ProviderKind,
        role: str,
        model: str,
        worktree: str,
        prompt: str,
        effort: str | None = None,
        orchestrator_id: str | None = None,
        replaces_run_id: str | None = None,
        backend_base_url: str | None = None,
    ) -> RunRecord:
        return cls(
            run_id=str(uuid4()),
            agent_id=agent_id,
            provider=provider,
            role=role,
            model=model,
            worktree=worktree,
            backend_base_url=backend_base_url,
            initial_prompt=prompt,
            effort=effort,
            orchestrator_id=orchestrator_id,
            replaces_run_id=replaces_run_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "provider": self.provider.value,
            "role": self.role,
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
            "provider_generation": self.provider_generation,
            "active_turn_id": self.active_turn_id,
            "transcript_path": self.transcript_path,
            "initial_prompt": self.initial_prompt,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "replaces_run_id": self.replaces_run_id,
            "replaced_by_run_id": self.replaced_by_run_id,
            "outcome": self.outcome,
            "raw_event_count": self.raw_event_count,
            "normalized_event_count": self.normalized_event_count,
            "last_lifecycle_event_seq": self.last_lifecycle_event_seq,
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
            provider_generation=int(value.get("provider_generation", 0)),
            active_turn_id=value.get("active_turn_id"),
            transcript_path=value.get("transcript_path"),
            initial_prompt=value.get("initial_prompt"),
            created_at=str(value.get("created_at") or utc_now()),
            updated_at=str(value.get("updated_at") or utc_now()),
            replaces_run_id=value.get("replaces_run_id"),
            replaced_by_run_id=value.get("replaced_by_run_id"),
            outcome=value.get("outcome"),
            raw_event_count=int(value.get("raw_event_count", 0)),
            normalized_event_count=int(value.get("normalized_event_count", 0)),
            last_lifecycle_event_seq=int(value.get("last_lifecycle_event_seq", 0)),
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
                str(item)
                for item in value.get("message_dedupe_keys") or []
                if isinstance(item, str) and item
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
    working/idle run, that block preserves `recovery_from_state`; the daemon
    rechecks the PID and resumes the exact session once it exits.

    The supervisor guards an automatic resume until its replacement control
    stream remains attached for the configured stability window. A failed or
    immediately dying resume is not retried every polling tick; its original
    working/idle intent remains available for an explicit operator resume.
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
                recovery_state in {LifecycleState.WORKING, LifecycleState.IDLE}
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
            else "pending approval cannot be reconstructed safely"
        ),
        RecoveryAction.SKIP: f"state {recovery_state.value} is not auto-resumable",
    }
    return RecoveryDecision(action, reasons[action])
