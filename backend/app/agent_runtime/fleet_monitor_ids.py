"""Stable command identities for fleet-monitor notifications."""

from __future__ import annotations

import hashlib


def fleet_monitor_request_id(run_id: str, dedupe_key: str | None) -> str:
    """Scope a durable command request to one orchestrator run.

    Alarm keys survive daemon restarts. The run component prevents a stable
    alarm key from colliding with a replacement orchestrator's payload.
    """

    payload = f"{run_id or ''}:{dedupe_key or ''}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"fleet-monitor:{digest}"


def fleet_monitor_message_dedupe_key(
    run_id: str, dedupe_key: str | None
) -> str | None:
    """Scope transport dedupe to one orchestrator run.

    Run replacement copies message claims. A run-scoped digest keeps a new
    orchestrator from inheriting an old run's fleet-monitor claim.
    """

    if dedupe_key is None:
        return None
    payload = f"{run_id or ''}:{dedupe_key}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"fleet-monitor:{digest}"
