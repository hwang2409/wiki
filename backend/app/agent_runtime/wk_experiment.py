"""Allowlisted reviewer matching and plan-usage telemetry for wk experiments."""

from __future__ import annotations

import hashlib
import json
import os
import random
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


class WkExperimentError(ValueError):
    """The reviewer experiment admission or metric contract failed."""


@dataclass(frozen=True)
class WkMatchedReview:
    match_id: str
    reviewer_id: str
    pr_sha: str
    prompt_sha256: str
    arms: tuple[str, str]

    def arm_id(self, kind: str) -> str:
        if kind not in self.arms:
            raise WkExperimentError(f"kind is not in matched review: {kind!r}")
        return f"{self.match_id}:{kind}"


class WkReviewerExperiment:
    """Admit only allowlisted review workers and match both experiment arms."""

    def __init__(
        self,
        reviewer_allowlist: Iterable[str],
        *,
        seed: str = "wiki-wk-reviewer-v0",
        allowed_kinds: Iterable[str] = ("cdx", "wk-claude"),
    ) -> None:
        self.reviewer_allowlist = frozenset(reviewer_allowlist)
        self.seed = seed
        self.allowed_kinds = frozenset(allowed_kinds)

    def admit(
        self,
        reviewer_id: str,
        *,
        role: str,
        pr_sha: str,
        prompt: str,
        kind: str,
    ) -> WkMatchedReview:
        if reviewer_id not in self.reviewer_allowlist:
            raise WkExperimentError("reviewer is not allowlisted")
        if role != "review":
            raise WkExperimentError("wk experiment admits reviewer roles only")
        if kind not in self.allowed_kinds:
            raise WkExperimentError("reviewer kind is not allowlisted")
        if not pr_sha or not prompt:
            raise WkExperimentError("matched reviews require a PR SHA and prompt")
        match_id = hashlib.sha256(f"{pr_sha}\0{prompt}".encode()).hexdigest()[:24]
        arms = ["cdx", "wk-claude"]
        random.Random(f"{self.seed}:{match_id}").shuffle(arms)
        return WkMatchedReview(
            match_id=match_id,
            reviewer_id=reviewer_id,
            pr_sha=pr_sha,
            prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
            arms=(arms[0], arms[1]),
        )


@dataclass(frozen=True)
class WkPlanUsageUnits:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    rate_limit_window_percent: float = 0.0

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 0
            for value in (self.input_tokens, self.output_tokens, self.cached_input_tokens)
        ):
            raise WkExperimentError("token usage must be non-negative integers")
        if (
            isinstance(self.rate_limit_window_percent, bool)
            or not isinstance(self.rate_limit_window_percent, (int, float))
            or not 0 <= self.rate_limit_window_percent <= 100
        ):
            raise WkExperimentError("rate-limit window percent must be between 0 and 100")

    def to_dict(self) -> dict[str, object]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "rate_limit_window_percent": self.rate_limit_window_percent,
        }


def validate_wk_metric(value: Mapping[str, object]) -> dict[str, object]:
    """Validate the export schema and reject dollar-denominated metrics."""

    forbidden = {"dollars", "usd", "cost", "price"}
    if forbidden.intersection(value):
        raise WkExperimentError("wk metrics use plan usage units, not currency")
    units = value.get("plan_usage_units")
    if not isinstance(units, Mapping):
        raise WkExperimentError("metric must contain plan_usage_units")
    required = {"input_tokens", "output_tokens", "cached_input_tokens", "rate_limit_window_percent"}
    if set(units) != required:
        raise WkExperimentError("plan_usage_units schema is incomplete")
    parsed = WkPlanUsageUnits(
        input_tokens=units["input_tokens"],  # type: ignore[arg-type]
        output_tokens=units["output_tokens"],  # type: ignore[arg-type]
        cached_input_tokens=units["cached_input_tokens"],  # type: ignore[arg-type]
        rate_limit_window_percent=units["rate_limit_window_percent"],  # type: ignore[arg-type]
    )
    return {**dict(value), "plan_usage_units": parsed.to_dict()}


class WkMetricExporter:
    """Collect matched-arm metrics and write a validated JSON export atomically."""

    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def record(
        self,
        matched: WkMatchedReview,
        *,
        kind: str,
        usage: WkPlanUsageUnits,
        tool_calls: int = 0,
        retries: int = 0,
        wall_time_ms: int = 0,
        process_time_ms: int = 0,
    ) -> dict[str, object]:
        record = validate_wk_metric(
            {
                "schema": "wiki.wk.metric.v0",
                "match_id": matched.match_id,
                "arm_id": matched.arm_id(kind),
                "kind": kind,
                "pr_sha": matched.pr_sha,
                "plan_usage_units": usage.to_dict(),
                "tool_calls": tool_calls,
                "retries": retries,
                "wall_time_ms": wall_time_ms,
                "process_time_ms": process_time_ms,
            }
        )
        self.records.append(record)
        return record

    def export(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [validate_wk_metric(record) for record in self.records]
        fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        tmp = Path(raw_tmp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


__all__ = [
    "WkExperimentError",
    "WkMatchedReview",
    "WkMetricExporter",
    "WkPlanUsageUnits",
    "WkReviewerExperiment",
    "validate_wk_metric",
]
