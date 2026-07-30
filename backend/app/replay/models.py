"""Shared dataclasses for the replay package."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TimelineEvent:
    seq: int
    raw_seq: int
    ts: str | None
    kind: str
    disposition: str
    lifecycle_state: str | None
    summary: str
    bookmark: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "raw_seq": self.raw_seq,
            "ts": self.ts,
            "kind": self.kind,
            "disposition": self.disposition,
            "lifecycle_state": self.lifecycle_state,
            "summary": self.summary,
            "bookmark": self.bookmark,
        }


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    agent_id: str | None
    orch_id: str | None
    role: str | None
    provider: str | None
    model: str | None
    outcome: str | None
    state: str | None
    created_at: str | None
    updated_at: str | None
    total_events: int
    initial_prompt_excerpt: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "orch_id": self.orch_id,
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "outcome": self.outcome,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "total_events": self.total_events,
            "initial_prompt_excerpt": self.initial_prompt_excerpt,
        }


@dataclass
class ScanStats:
    dropped_oversize: int = 0
    dropped_malformed: int = 0
    dropped_truncated_tail: bool = False
    scan_truncated: bool = False
    bookmarks_truncated: bool = False


@dataclass
class TicketRunsListing:
    runs: list[RunSummary]
    truncated: bool = False


@dataclass
class TimelinePage:
    events: list[TimelineEvent]
    end_offset: int
    end_skipping: bool
    has_more: bool
