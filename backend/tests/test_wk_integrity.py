from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend.app.agent_runtime.wk_common import WkLedgerError, WkToolBridge, WkToolLedger
from backend.app.agent_runtime.wk_core import (
    WkEventSequencer,
    WkLoop,
    WkMutationClass,
    WkRunMetadata,
    WkToolRegistry,
    WkToolRequest,
    WkToolResult,
)
from backend.app.agent_runtime.wk_experiment import (
    WkExperimentError,
    WkMetricExporter,
    WkPlanUsageUnits,
    WkReviewerExperiment,
    validate_wk_metric,
)
from backend.app.agent_runtime.wk_tools import WkGateTool, WkStatusTool


class _Translator:
    metadata = WkRunMetadata("wk-claude")
    run_id = "run-integrity"
    agent_id = "WIKI-289"
    provider = "claude"
    lane = "wk-claude"
    timestamp = staticmethod(lambda: "2026-08-15T00:00:00+00:00")

    def __init__(self) -> None:
        self.sequencer = WkEventSequencer(event_id_factory=iter((f"event-{n}" for n in range(20))).__next__)
        self.session = type("Session", (), {"record_event": lambda _self, _event: "entry"})()


def test_gate_runner_records_role_commands_and_real_receipt(tmp_path: Path) -> None:
    script = tmp_path / "wiki-gate"
    script.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '{\"ready\":true,\"head_sha\":\"abc123\"}'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    async def run() -> WkToolResult:
        return await WkGateTool(root=tmp_path, wiki_command=(str(script),)).execute(
            WkToolRequest(
                call_id="gate-1",
                name="wk.gate",
                arguments={"role": "review", "pr": "230", "expected_sha": "abc"},
                mutation=WkMutationClass.PROCESS,
            )
        )

    result = asyncio.run(run())
    assert result.success is True
    assert result.exit_code == 0
    assert result.mutation_receipt is not None
    assert result.mutation_receipt["commands"] == [[str(script), "gate", "230", "--json", "--expect-sha", "abc"]]
    assert result.mutation_receipt["verdict"] == {"ready": True, "head_sha": "abc123"}


def test_mutation_receipt_reconciliation_rejects_claim_without_receipt() -> None:
    translator = _Translator()
    ledger = WkToolLedger(translator)
    request = WkToolRequest(
        call_id="mut-1",
        name="wk.bash",
        arguments={"command": "touch file"},
        mutation=WkMutationClass.PROCESS,
    )
    ledger.record_started(request)
    ledger.record_result(request, WkToolResult(success=True, exit_code=0, mutation=WkMutationClass.PROCESS))
    with pytest.raises(WkLedgerError, match="no receipt"):
        ledger.reconcile()


def test_model_cannot_forge_gate_or_merge_ready_status(tmp_path: Path) -> None:
    loop = WkLoop(status_path=tmp_path / "status.json")
    translator = _Translator()
    ledger = WkToolLedger(translator)
    loop.bind_integrity(ledger)
    registry = WkToolRegistry((WkStatusTool(loop=loop),))
    bridge = WkToolBridge(registry=registry, ledger=ledger, loop=loop)

    async def run() -> WkToolResult:
        return await bridge.invoke(
            "wk.status",
            {"state": "merge-ready", "step": "forged", "pr": "https://example.test/pull/230"},
            call_id="forged-status",
        )

    result = asyncio.run(run())
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert result.success is False
    assert status["state"] == "blocked"
    assert not any(event.kind == "tool.completed" and event.payload.get("name") == "wk.gate" for event in ledger.events)


def test_reviewer_admission_matches_sha_and_prompt() -> None:
    experiment = WkReviewerExperiment({"reviewer-a"}, seed="test")
    first = experiment.admit(
        "reviewer-a", role="review", pr_sha="abc123", prompt="review this", kind="wk-claude"
    )
    second = experiment.admit(
        "reviewer-a", role="review", pr_sha="abc123", prompt="review this", kind="cdx"
    )
    assert first.match_id == second.match_id
    assert first.prompt_sha256 == second.prompt_sha256
    assert first.arms == second.arms
    with pytest.raises(WkExperimentError, match="not allowlisted"):
        experiment.admit("reviewer-b", role="review", pr_sha="abc123", prompt="review this", kind="wk-claude")


def test_metric_export_uses_plan_units_and_rejects_currency(tmp_path: Path) -> None:
    experiment = WkReviewerExperiment({"reviewer-a"})
    matched = experiment.admit(
        "reviewer-a", role="review", pr_sha="abc123", prompt="review this", kind="wk-claude"
    )
    exporter = WkMetricExporter()
    exporter.record(
        matched,
        kind="wk-claude",
        usage=WkPlanUsageUnits(input_tokens=10, output_tokens=20, rate_limit_window_percent=12.5),
    )
    path = tmp_path / "metrics.json"
    exporter.export(path)
    exported = json.loads(path.read_text(encoding="utf-8"))
    assert exported[0]["plan_usage_units"]["input_tokens"] == 10
    with pytest.raises(WkExperimentError, match="not currency"):
        validate_wk_metric({"dollars": 1, "plan_usage_units": {}})
