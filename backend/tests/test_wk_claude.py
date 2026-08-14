from __future__ import annotations

import json
import os
import subprocess
import sys
import asyncio
from pathlib import Path

import pytest

from backend.app.agent_runtime.wk_claude import (
    WK_CLAUDE_TOOL_NAMES,
    WkClaudeEventTranslator,
    WkClaudePlanAuthError,
    WkClaudeToolBridge,
    WkLedgerError,
    WkToolLedger,
    plan_auth_environment,
)
from backend.app.agent_runtime.wk_core import (
    WkEventSequencer,
    WkLoop,
    WkMutationClass,
    WkRunMetadata,
    WkToolRegistry,
    WkToolRequest,
    WkToolResult,
)


FIXTURE = Path(__file__).parent / "fixtures" / "agent_runtime" / "claude_sdk_lane_events.jsonl"


def _fixture_events() -> list[dict[str, object]]:
    return [json.loads(line) for line in FIXTURE.read_text().splitlines()]


def _translator() -> WkClaudeEventTranslator:
    return WkClaudeEventTranslator(
        metadata=WkRunMetadata("wk-claude"),
        run_id="run-claude",
        agent_id="WIKI-289",
        sequencer=WkEventSequencer(event_id_factory=iter(f"event-{n}" for n in range(30)).__next__),
        timestamp=lambda: "2026-08-14T00:00:00+00:00",
    )


def test_flag_off_boot_does_not_import_claude_lane_or_sdk() -> None:
    env = dict(os.environ)
    env.pop("WIKI_ENABLE_WK", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import backend.app.main; print('backend.app.agent_runtime.wk_claude' in sys.modules); print('claude_agent_sdk' in sys.modules)",
        ],
        cwd=Path(__file__).parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip().splitlines()[-2:] == ["False", "False"]


def test_wk_claude_is_constructible_only_when_flag_is_on(tmp_path: Path) -> None:
    script = """
from pathlib import Path
from backend.app.agent_runtime.wk_claude import WkClaudeLane
from backend.app.agent_runtime.wk_core import WkLoop, WkRunMetadata, WkToolRegistry
lane = WkClaudeLane(metadata=WkRunMetadata('wk-claude'), run_id='r', agent_id='a', worktree=Path('.'), model='sonnet', registry=WkToolRegistry(), loop=WkLoop(status_path=Path('status.json')))
print(lane.metadata.kind)
"""
    env = dict(os.environ)
    env["WIKI_ENABLE_WK"] = "1"
    env["PYTHONPATH"] = str(Path(__file__).parents[2])
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "wk-claude"


def test_plan_auth_path_rejects_api_credentials() -> None:
    assert "ANTHROPIC_API_KEY" not in plan_auth_environment({"HOME": "/tmp"})
    with pytest.raises(WkClaudePlanAuthError, match="ANTHROPIC_API_KEY"):
        plan_auth_environment({"ANTHROPIC_API_KEY": "secret"})


def test_real_sdk_wire_fixture_translates_and_retains_raw_events() -> None:
    translator = _translator()
    events = [translator.translate(value) for value in _fixture_events()]

    assert len(translator.raw_events) == len(events) == 12
    assert translator.raw_events[1]["message"]["content"][1]["type"] == "tool_use"
    assert events[0].phase.value == "run"
    assert events[1].phase.value == "assistant"
    assert events[2].phase.value == "tool"
    assert events[5].phase.value == "status"
    assert events[8].phase.value == "compaction"
    assert events[9].phase.value == "compaction"
    assert events[10].phase.value == "turn"
    assert events[10].payload["subtype"] == "interrupted"
    assert events[11].payload["resume_from"] == "tool-bash-1"
    assert translator.session.entries[-1]["parent_id"] == translator.session.entries[-2]["id"]


def test_captured_claude_stream_fixture_preserves_sdk_tool_shapes() -> None:
    captured = Path(__file__).parent / "fixtures" / "agent_runtime" / "claude_stream_native_surfaces.jsonl"
    translator = _translator()
    events = [translator.translate(json.loads(line)) for line in captured.read_text().splitlines()]

    assert events
    assert any(event.phase.value == "tool" for event in events)
    assert any(
        raw.get("type") == "user"
        and any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in (raw.get("message") or {}).get("content", [])
        )
        for raw in translator.raw_events
    )


def test_tool_ledger_records_success_nonzero_and_timeout_results() -> None:
    translator = _translator()
    ledger = WkToolLedger(translator)
    cases = (
        ("read-1", "wk.read", WkToolResult(success=True, exit_code=0)),
        (
            "bash-1",
            "wk.bash",
            WkToolResult(success=False, exit_code=17, error_class="process_failed"),
        ),
        (
            "bash-timeout",
            "wk.bash",
            WkToolResult(success=False, exit_code=None, timed_out=True, error_class="timeout"),
        ),
    )
    for call_id, name, result in cases:
        request = WkToolRequest(
            call_id=call_id,
            name=name,
            arguments={"command": name},
            mutation=WkMutationClass.PROCESS if name == "wk.bash" else WkMutationClass.NONE,
        )
        ledger.record_started(request)
        ledger.record_result(request, result)
    assert [event.source_seq for event in ledger.events] == list(range(1, 7))
    ledger.reconcile()


def test_gate_success_requires_a_typed_result_and_real_exit_code() -> None:
    translator = _translator()
    ledger = WkToolLedger(translator)
    request = WkToolRequest(
        call_id="gate-1",
        name="wk.gate",
        arguments={"pr": "229"},
        mutation=WkMutationClass.PROCESS,
    )
    ledger.record_started(request)
    with pytest.raises(WkLedgerError, match="missing tool results"):
        ledger.reconcile()
    ledger.record_result(request, WkToolResult(success=True, exit_code=0))
    ledger.reconcile()


def test_dropped_tool_result_fails_sequence_and_ledger_completeness() -> None:
    translator = _translator()
    ledger = WkToolLedger(translator)
    request = WkToolRequest(call_id="read-1", name="wk.read", arguments={})
    ledger.record_started(request)
    result_event = ledger.record_result(request, WkToolResult(success=True, exit_code=0))
    with pytest.raises(WkLedgerError, match="missing tool results"):
        ledger.reconcile([event for event in ledger.events if event is not result_event])


def test_status_tool_routes_through_loop_owned_writer(tmp_path: Path) -> None:
    asyncio.run(_assert_status_tool_routes_through_loop_owned_writer(tmp_path))


async def _assert_status_tool_routes_through_loop_owned_writer(tmp_path: Path) -> None:
    translator = _translator()
    ledger = WkToolLedger(translator)
    loop = WkLoop(status_path=tmp_path / "status.json")
    bridge = WkClaudeToolBridge(registry=WkToolRegistry(), ledger=ledger, loop=loop)
    result = await bridge.invoke(
        "wk.status",
        {"state": "working", "step": "running", "pr": None, "blocker": None},
        call_id="status-1",
    )
    assert result.success is True
    assert json.loads((tmp_path / "status.json").read_text())["status_write_seq"] == 1
    assert ledger.events[0].payload["name"] == "wk.status"


def test_claude_tool_set_is_exactly_wiki_owned() -> None:
    assert WK_CLAUDE_TOOL_NAMES == (
        "wk.read",
        "wk.write",
        "wk.edit",
        "wk.bash",
        "wk.gate",
        "wk.status",
    )
