"""Shared agent-id to workgraph-ticket normalization."""

from __future__ import annotations

import re
from dataclasses import dataclass


_ROLE_SUFFIX = re.compile(
    r"-(?:REVIEW\d*(?:-[A-Z0-9][A-Z0-9-]*)?|(?:PLAN|SIM|AUDIT|CANARY|THERMO|EVAL)\d*)\s*$",
    re.IGNORECASE,
)
_REVIEWER_ID = re.compile(
    r"^(?P<ticket>[A-Z0-9-]+)-REVIEW(?P<round>[1-9][0-9]*)(?:-(?P<lens>[A-Z0-9][A-Z0-9-]*))?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReviewerIdentity:
    ticket: str
    round: int
    lens: str | None = None


def parse_reviewer_id(agent_id: str) -> ReviewerIdentity | None:
    """Parse every reviewer id with one case-insensitive contract."""

    match = _REVIEWER_ID.fullmatch(str(agent_id).strip())
    if match is None:
        return None
    return ReviewerIdentity(
        ticket=match.group("ticket").upper(),
        round=int(match.group("round")),
        lens=match.group("lens").lower() if match.group("lens") else None,
    )


def reviewer_id(ticket: str, round_number: int, lens: str | None = None) -> str:
    """Build a canonical, round-scoped reviewer id."""

    suffix = f"-{lens.lower()}" if lens else ""
    return f"{ticket.upper()}-REVIEW{round_number}{suffix}"


def reviewer_id_candidates(agent_id: str) -> tuple[str, ...]:
    """Return exact, canonical, and legacy-uppercase reviewer keys."""

    raw = str(agent_id).strip()
    parsed = parse_reviewer_id(raw)
    if parsed is None:
        values = (raw, raw.upper())
    else:
        canonical = reviewer_id(parsed.ticket, parsed.round, parsed.lens)
        values = (raw, canonical, raw.upper())
    return tuple(dict.fromkeys(value for value in values if value))


def base_ticket(agent_id: str) -> str:
    """Return the workgraph ticket shared by a worker and its role siblings."""

    parsed = parse_reviewer_id(agent_id)
    if parsed is not None:
        return parsed.ticket
    return _ROLE_SUFFIX.sub("", agent_id)
