"""Retry-safe action identity helpers for autopilot."""

from __future__ import annotations

import hashlib
import json

from .autopilot_parser import Verdict


def steer_action_id(ticket: str, reviewer: str, verdict: Verdict) -> str:
    material = json.dumps(
        {
            "ticket": ticket,
            "reviewer": reviewer,
            "sha": verdict.source_sha,
            "findings": [finding.to_dict() for finding in verdict.findings],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "autopilot-steer-" + hashlib.sha256(material.encode()).hexdigest()[:32]


__all__ = ["steer_action_id"]
