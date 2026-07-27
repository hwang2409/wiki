"""Shared agent-id to workgraph-ticket normalization."""

from __future__ import annotations

import re


_ROLE_SUFFIX = re.compile(
    r"-(?:REVIEW|PLAN|SIM|AUDIT|CANARY|THERMO|EVAL)\d*$", re.IGNORECASE
)


def base_ticket(agent_id: str) -> str:
    """Return the workgraph ticket shared by a worker and its role siblings."""

    return _ROLE_SUFFIX.sub("", agent_id)
