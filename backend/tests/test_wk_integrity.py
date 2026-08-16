from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from backend.app.agent_runtime.wk_common import WkLedgerError, WkToolBridge, WkToolLedger
from backend.app.agent_runtime.wk_core import (
    WkDisposition,
    WkEventSequencer,
    WkEventPhase,
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
from backend.app.agent_runtime.wk_tools import WkGateTool, WkStatusTool, register_default_wk_tools
from backend.app.agent_runtime.wk_claude import WkClaudeLane
from backend.app.agent_runtime import wk_feature
from backend.app.agent_runtime.store import RunStore, RuntimePaths
from backend.app.agent_runtime.types import EventDisposition, ProviderKind, RunRecord


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
                arguments={"role": "review", "pr": "230"},
                mutation=WkMutationClass.PROCESS,
            )
        )

    result = asyncio.run(run())
    assert result.success is True
    assert result.exit_code == 0
    assert result.mutation_receipt is not None
    assert result.mutation_receipt["commands"] == [[str(script), "gate", "230", "--json"]]
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


def _record_gate(
    ledger: WkToolLedger,
    call_id: str,
    *,
    ready: bool,
    exit_code: int,
    head_sha: str,
    pr: str = "230",
    tree_clean: bool = True,
) -> None:
    request = WkToolRequest(
        call_id=call_id,
        name="wk.gate",
        arguments={"role": "review", "pr": "230"},
        mutation=WkMutationClass.PROCESS,
    )
    ledger.record_started(request)
    ledger.record_result(
        request,
        WkToolResult(
            success=exit_code == 0,
            exit_code=exit_code,
            mutation=WkMutationClass.PROCESS,
            mutation_receipt={
                "pid": 123,
                "stdout_sha256": "a" * 64,
                "stderr_sha256": "b" * 64,
                "pr": pr,
                "tree_clean": tree_clean,
                "verdict": {"ready": ready, "head_sha": head_sha},
            },
        ),
    )


def test_latest_gate_and_current_head_bind_merge_ready() -> None:
    ledger = WkToolLedger(_Translator())
    _record_gate(ledger, "gate-pass", ready=True, exit_code=0, head_sha="head")
    assert ledger.check_merge_ready(current_head_sha="head") == (
        True,
        "latest gate receipt proves merge-ready",
    )


def test_failed_gate_and_completed_mutation_invalidate_prior_pass() -> None:
    ledger = WkToolLedger(_Translator())
    _record_gate(ledger, "gate-pass", ready=True, exit_code=0, head_sha="head")
    _record_gate(ledger, "gate-fail", ready=False, exit_code=1, head_sha="head")
    assert ledger.check_merge_ready(current_head_sha="head")[0] is False

    ledger = WkToolLedger(_Translator())
    _record_gate(ledger, "gate-pass", ready=True, exit_code=0, head_sha="head")
    mutation = WkToolRequest(
        call_id="write-1",
        name="wk.write",
        arguments={"path": "file", "content": "changed"},
        mutation=WkMutationClass.FILE,
    )
    ledger.record_started(mutation)
    ledger.record_result(
        mutation,
        WkToolResult(
            success=True,
            exit_code=0,
            mutation=WkMutationClass.FILE,
            mutation_receipt={"path": "file", "after": "a" * 64},
        ),
    )
    assert ledger.check_merge_ready(current_head_sha="head")[0] is False
    _record_gate(ledger, "gate-refresh", ready=True, exit_code=0, head_sha="head")
    assert ledger.check_merge_ready(current_head_sha="head")[0] is True


def test_previous_head_gate_receipt_is_rejected() -> None:
    ledger = WkToolLedger(_Translator())
    _record_gate(ledger, "gate-old", ready=True, exit_code=0, head_sha="old-head")
    assert ledger.check_merge_ready(current_head_sha="new-head")[0] is False


def test_dirty_gate_receipt_cannot_prove_merge_ready() -> None:
    ledger = WkToolLedger(_Translator())
    _record_gate(ledger, "gate-dirty", ready=True, exit_code=0, head_sha="head", tree_clean=False)
    assert ledger.check_merge_ready(current_head_sha="head", expected_pr="230")[0] is False


def test_cross_pr_gate_receipt_cannot_prove_merge_ready() -> None:
    ledger = WkToolLedger(_Translator())
    _record_gate(ledger, "gate-other-pr", ready=True, exit_code=0, head_sha="head", pr="231")
    assert ledger.check_merge_ready(current_head_sha="head", expected_pr="230")[0] is False


def test_bash_status_path_write_is_blocked_and_logged(tmp_path: Path) -> None:
    loop = WkLoop(status_path=tmp_path / "status.json", worktree=tmp_path)
    translator = _Translator()
    ledger = WkToolLedger(translator)
    loop.bind_integrity(ledger)
    registry = register_default_wk_tools(WkToolRegistry(), root=tmp_path, loop=loop)
    bridge = WkToolBridge(registry=registry, ledger=ledger, loop=loop)

    result = asyncio.run(
        bridge.invoke(
            "wk.bash",
            {"command": f"printf hacked > {loop.status_path}"},
            call_id="status-bypass",
        )
    )
    status = json.loads(loop.status_path.read_text(encoding="utf-8"))
    assert result.success is False
    assert result.error_class == "integrity_violation"
    assert status["state"] == "blocked"
    assert any(event.kind == "wk.integrity_violation" for event in ledger.events)


def test_forged_status_projection_is_overwritten_and_not_authoritative(tmp_path: Path) -> None:
    loop = WkLoop(status_path=tmp_path / "status.json", worktree=tmp_path)
    translator = _Translator()
    ledger = WkToolLedger(translator)
    loop.bind_integrity(ledger)
    loop.write_status(state="working", pr=None, step="real", blocker=None)
    loop.status_path.write_text('{"state":"merge-ready","pr":"forged"}\n', encoding="utf-8")
    loop.write_status(state="working", pr=None, step="next projection", blocker=None)
    status = json.loads(loop.status_path.read_text(encoding="utf-8"))
    assert status["state"] == "working"
    assert status["pr"] is None
    assert any(event.kind == "wk.integrity_violation" for event in ledger.events)


def test_wk_status_replay_uses_causal_sequence_after_orphan_append(tmp_path: Path) -> None:
    paths = RuntimePaths(
        runtime_dir=tmp_path / "runtime",
        socket_path=tmp_path / "runtime" / "supervisor.sock",
        registry_path=tmp_path / "registry.json",
        archive_dir=tmp_path / "archive",
        status_dir=tmp_path / "status",
    )
    store = RunStore(paths)
    record = store.create(
        RunRecord.new(
            agent_id="WIKI-289",
            provider=ProviderKind.CLAUDE,
            role="implement",
            model="claude-plan",
            worktree=str(tmp_path),
            prompt="replay",
            execution_kind="wk-claude",
        )
    )
    translator = _Translator()
    ready = translator.sequencer.emit(
        run_id=record.run_id,
        agent_id=record.agent_id,
        kind="wk.status",
        phase=WkEventPhase.STATUS,
        provider="claude",
        lane="wk-claude",
        disposition=WkDisposition.RENDERED,
        ts=translator.timestamp(),
        payload={"state": "merge-ready", "pr": "https://github.com/hwang2409/wiki/pull/234", "step": "ready", "blocker": None},
    )
    revoked = translator.sequencer.emit(
        run_id=record.run_id,
        agent_id=record.agent_id,
        kind="wk.status_revoked",
        phase=WkEventPhase.STATUS,
        provider="claude",
        lane="wk-claude",
        disposition=WkDisposition.RENDERED,
        ts=translator.timestamp(),
        payload={"detail": "failed gate"},
    )
    for event in (ready, revoked):
        raw = store.append_raw(
            record.run_id,
            provider="claude",
            direction="inbound",
            payload=event.to_dict(),
        )
        store.append_normalized(
            record.run_id,
            raw_seq=int(raw["seq"]),
            disposition=EventDisposition.RENDERED,
            kind=event.kind,
            payload=event.to_dict(),
        )

    normalized_path = store.normalized_events_path(record.run_id)
    rows = [json.loads(line) for line in normalized_path.read_text().splitlines() if line]
    normalized_path.write_text(
        "".join(json.dumps(row) + "\n" for row in reversed(rows)),
        encoding="utf-8",
    )
    restarted = RunStore(paths)
    restored = restarted.rebuild_wk_status_projection(record.run_id)
    assert restored.wk_status_state == "blocked"
    assert restored.wk_status_source_seq == revoked.source_seq


def test_merge_ready_without_a_bound_ledger_fails_closed(tmp_path: Path) -> None:
    loop = WkLoop(status_path=tmp_path / "status.json")
    registry = WkToolRegistry((WkStatusTool(loop=loop),))
    translator = _Translator()
    ledger = WkToolLedger(translator)
    bridge = WkToolBridge(registry=registry, ledger=ledger, loop=loop)
    result = asyncio.run(
        bridge.invoke(
            "wk.status",
            {"state": "merge-ready", "step": "forged", "pr": "https://example.test/pull/230"},
            call_id="unbound-status",
        )
    )
    assert result.success is False
    assert result.error_detail == "wk ledger is required for merge-ready"
    assert json.loads(loop.status_path.read_text(encoding="utf-8"))["state"] == "blocked"


def test_loop_binds_gate_proof_to_real_git_head(tmp_path: Path) -> None:
    subprocess.run(("git", "init", "-q", str(tmp_path)), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "config", "user.email", "test@example.com"), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "config", "user.name", "Wiki Test"), check=True)
    (tmp_path / "README").write_text("head\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(tmp_path), "add", "README"), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "commit", "-q", "-m", "head"), check=True)
    head = subprocess.check_output(("git", "-C", str(tmp_path), "rev-parse", "HEAD"), text=True).strip()
    loop = WkLoop(status_path=tmp_path / "status.json", worktree=tmp_path)
    translator = _Translator()
    ledger = WkToolLedger(translator)
    loop.bind_integrity(ledger)
    _record_gate(ledger, "gate-head", ready=True, exit_code=0, head_sha=head)
    registry = WkToolRegistry((WkStatusTool(loop=loop),))
    bridge = WkToolBridge(registry=registry, ledger=ledger, loop=loop)
    result = asyncio.run(
        bridge.invoke(
            "wk.status",
            {"state": "merge-ready", "step": "gate passed", "pr": "230"},
            call_id="head-status",
        )
    )
    assert result.success is True


def test_ready_status_is_revoked_by_a_later_mutation(tmp_path: Path) -> None:
    subprocess.run(("git", "init", "-q", str(tmp_path)), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "config", "user.email", "test@example.com"), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "config", "user.name", "Wiki Test"), check=True)
    (tmp_path / "README").write_text("head\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(tmp_path), "add", "README"), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "commit", "-q", "-m", "head"), check=True)
    head = subprocess.check_output(("git", "-C", str(tmp_path), "rev-parse", "HEAD"), text=True).strip()
    loop = WkLoop(status_path=tmp_path / "status.json", worktree=tmp_path)
    translator = _Translator()
    ledger = WkToolLedger(translator)
    loop.bind_integrity(ledger)
    _record_gate(ledger, "gate-pass", ready=True, exit_code=0, head_sha=head)
    loop.note_authoritative_status(
        state="merge-ready",
        pr="230",
        step="ready",
        blocker=None,
    )
    loop.project_status(state="merge-ready", pr="230", step="ready", blocker=None)
    registry = register_default_wk_tools(WkToolRegistry(), root=tmp_path, loop=loop)
    bridge = WkToolBridge(registry=registry, ledger=ledger, loop=loop)
    result = asyncio.run(
        bridge.invoke(
            "wk.bash",
            {"command": "exit 17"},
            call_id="mutation-after-ready",
        )
    )
    status = json.loads(loop.status_path.read_text(encoding="utf-8"))
    assert result.success is False
    assert status["state"] == "blocked"
    assert any(event.kind == "wk.status_revoked" for event in ledger.events)


def test_ready_status_is_revoked_by_a_failed_gate(tmp_path: Path) -> None:
    subprocess.run(("git", "init", "-q", str(tmp_path)), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "config", "user.email", "test@example.com"), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "config", "user.name", "Wiki Test"), check=True)
    (tmp_path / "README").write_text("head\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(tmp_path), "add", "README"), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "commit", "-q", "-m", "head"), check=True)
    head = subprocess.check_output(("git", "-C", str(tmp_path), "rev-parse", "HEAD"), text=True).strip()
    loop = WkLoop(status_path=tmp_path / "status.json", worktree=tmp_path)
    ledger = WkToolLedger(_Translator())
    loop.bind_integrity(ledger)
    _record_gate(ledger, "gate-pass", ready=True, exit_code=0, head_sha=head)
    loop.note_authoritative_status(state="merge-ready", pr="230", step="ready", blocker=None)
    loop.project_status(state="merge-ready", pr="230", step="ready", blocker=None)
    registry = register_default_wk_tools(
        WkToolRegistry(), root=tmp_path, loop=loop, wiki_command=("false",)
    )
    bridge = WkToolBridge(registry=registry, ledger=ledger, loop=loop)
    result = asyncio.run(
        bridge.invoke("wk.gate", {"role": "review", "pr": "230"}, call_id="gate-fail")
    )
    status = json.loads(loop.status_path.read_text(encoding="utf-8"))
    assert result.success is False
    assert status["state"] == "blocked"
    assert any(event.kind == "wk.status_revoked" for event in ledger.events)


def test_real_claude_loop_rejects_model_forgery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("claude_agent_sdk")
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    stub = Path(__file__).parent / "fixtures" / "agent_runtime" / "claude_sdk_stub_cli.py"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "integrity-forge").touch()
    gate = tmp_path / "gate"
    gate.write_text("#!/bin/sh\nprintf 'not a gate receipt\\n'\nexit 1\n", encoding="utf-8")
    gate.chmod(0o755)
    loop = WkLoop(status_path=config_dir / "status.json", worktree=tmp_path)
    lane = WkClaudeLane(
        metadata=WkRunMetadata("wk-claude"),
        run_id="run-real-integrity-forgery",
        agent_id="WIKI-289",
        worktree=tmp_path,
        model="sonnet",
        loop=loop,
        cli_path=stub,
        environment={
            "HOME": os.environ["HOME"],
            "PATH": os.environ["PATH"],
            "USER": os.environ.get("USER", "worker"),
            "TERM": os.environ.get("TERM", "xterm"),
            "CLAUDE_CONFIG_DIR": str(config_dir),
        },
        wiki_command=(str(gate),),
        steering_path=tmp_path / "steering.json",
    )

    async def run() -> list[Mapping[str, object]]:
        await lane.start("attempt the forgery")
        events: list[Mapping[str, object]] = []
        async for item in lane.events():
            events.append(item)
            raw = item.get("raw")
            if isinstance(raw, Mapping) and raw.get("type") == "result":
                break
        await lane.close()
        return events

    events = asyncio.run(run())
    status = json.loads((config_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "blocked"
    assert any(
        isinstance(item.get("raw"), Mapping)
        and item["raw"].get("type") == "wk_ledger"
        for item in events
    )
    assert any(item.get("event", {}).get("kind") == "wk.integrity_violation" for item in events)
    assert any(item.get("event", {}).get("kind") == "tool.failed" for item in events)
    assert not any(
        item.get("event", {}).get("kind") == "tool.completed"
        and item.get("event", {}).get("payload", {}).get("name") == "wk.gate"
        for item in events
    )


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
