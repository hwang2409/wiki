from __future__ import annotations

import json
import os
import subprocess
import sys
import asyncio
from dataclasses import replace
from pathlib import Path
from collections.abc import AsyncIterator, Mapping
from types import SimpleNamespace

import pytest

from backend.app.agent_runtime.wk_claude import (
    WK_CLAUDE_TOOL_NAMES,
    WkClaudeEventTranslator,
    WkClaudeError,
    WkClaudeLane,
    WkClaudePlanAuthError,
    WkClaudeToolBridge,
    WkLedgerError,
    WkToolLedger,
    WkBashTool,
    WkEditTool,
    WkGateTool,
    WkReadTool,
    WkWriteTool,
    build_claude_sdk_options,
    plan_auth_environment,
    register_default_wk_tools,
)
from backend.app.agent_runtime import wk_feature
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
    with pytest.raises(WkClaudePlanAuthError, match="CLAUDE_CODE_USE_BEDROCK"):
        plan_auth_environment({"CLAUDE_CODE_USE_BEDROCK": "1"})
    with pytest.raises(WkClaudePlanAuthError, match="CLAUDE_CODE_USE_VERTEX"):
        plan_auth_environment({"CLAUDE_CODE_USE_VERTEX": "1"})
    with pytest.raises(WkClaudePlanAuthError, match="AWS_PROFILE"):
        plan_auth_environment({"AWS_PROFILE": "default"})


def test_startup_verifier_requires_plan_auth_tools_and_no_hooks() -> None:
    translator = _translator()
    with pytest.raises(WkClaudePlanAuthError, match="not proven plan auth"):
        translator.translate(
            {
                "type": "system",
                "subtype": "init",
                "apiKeySource": "ANTHROPIC_API_KEY",
                "tools": list(translator_tool_names()),
            }
        )
    with pytest.raises(WkClaudePlanAuthError, match="non-Wiki tools"):
        translator.translate(
            {
                "type": "system",
                "subtype": "init",
                "apiKeySource": "none",
                "tools": ["Bash"],
            }
        )
    with pytest.raises(WkClaudePlanAuthError, match="missing Wiki tools"):
        translator.translate(
            {
                "type": "system",
                "subtype": "init",
                "apiKeySource": "claude.ai",
                "tools": list(translator_tool_names())[:-1],
            }
        )
    with pytest.raises(WkClaudePlanAuthError, match="active hooks"):
        translator.translate(
            {
                "type": "system",
                "subtype": "init",
                "apiKeySource": "claude.ai",
                "tools": list(translator_tool_names()),
                "hooks": ["PreToolUse"],
            }
        )
    with pytest.raises(WkClaudePlanAuthError, match="filesystem settings"):
        translator.translate(
            {
                "type": "system",
                "subtype": "init",
                "apiKeySource": "claude.ai",
                "tools": list(translator_tool_names()),
                "setting_sources": ["user"],
            }
        )


def translator_tool_names() -> tuple[str, ...]:
    from backend.app.agent_runtime.wk_claude import WK_CLAUDE_MCP_TOOL_NAMES

    return WK_CLAUDE_MCP_TOOL_NAMES


class _RecordedClient:
    def __init__(self, frames: list[dict[str, object]]):
        self.frames = frames
        self.queries: list[str] = []
        self.interrupted = False

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def interrupt(self) -> None:
        self.interrupted = True

    async def receive_messages(self) -> AsyncIterator[object]:
        for frame in self.frames:
            yield frame


def test_recorded_sdk_frames_cross_lane_transport_boundary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def run() -> tuple[list[dict[str, object]], _RecordedClient, WkToolLedger]:
        monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
        client = _RecordedClient(_fixture_events())
        lane = WkClaudeLane(
            metadata=WkRunMetadata("wk-claude"),
            run_id="run-transport",
            agent_id="WIKI-289",
            worktree=tmp_path,
            model="sonnet",
            loop=WkLoop(status_path=tmp_path / "status.json"),
            client_factory=lambda _options: client,
            options_factory=lambda **_kwargs: object(),
            steering_path=tmp_path / "steering.json",
        )
        await lane.start("start")
        await lane.close()
        return [item async for item in lane.events()], client, lane.ledger

    events, client, ledger = asyncio.run(run())
    assert len(events) == len(_fixture_events())
    assert all("raw" in item and "event" in item for item in events)
    assert not any(item["event"]["kind"] == "claude.provider_error" for item in events)
    ledger.reconcile_transport()
    assert client.queries == ["start"]


def test_real_sdk_subprocess_transport_receives_sanitized_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("claude_agent_sdk")
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    stub = Path(__file__).parent / "fixtures" / "agent_runtime" / "claude_sdk_stub_cli.py"
    safe_environment = {
        "HOME": os.environ["HOME"],
        "PATH": os.environ["PATH"],
        "USER": os.environ.get("USER", "worker"),
        "TERM": os.environ.get("TERM", "xterm"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "config"),
    }
    loop = WkLoop(status_path=tmp_path / "status.json")
    translator = _translator()
    ledger = WkToolLedger(translator)
    registry = register_default_wk_tools(WkToolRegistry(), root=tmp_path, loop=loop)
    bridge = WkClaudeToolBridge(registry=registry, ledger=ledger, loop=loop)

    approval_calls: list[tuple[str, Mapping[str, object]]] = []

    async def approve(tool_name: str, arguments: Mapping[str, object]) -> bool:
        approval_calls.append((tool_name, arguments))
        return True

    options = build_claude_sdk_options(
        prompt="",
        model="sonnet",
        worktree=tmp_path,
        bridge=bridge,
        approval=approve,
        environment=safe_environment,
    )
    options = replace(options, cli_path=str(stub))
    lane = WkClaudeLane(
        metadata=WkRunMetadata("wk-claude"),
        run_id="run-real-subprocess",
        agent_id="WIKI-289",
        worktree=tmp_path,
        model="sonnet",
        loop=loop,
        options_factory=lambda **_kwargs: options,
        steering_path=tmp_path / "steering.json",
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-child")
    monkeypatch.setenv("FOUNDRY_API_KEY", "must-not-reach-child")

    async def run() -> list[Mapping[str, object]]:
        await lane.start("read")
        events: list[Mapping[str, object]] = []
        async for item in lane.events():
            events.append(item)
            if item["raw"]["type"] == "result":
                break
        await lane.close()
        lane.ledger.reconcile_transport()
        return events

    events = asyncio.run(run())
    captured = json.loads((tmp_path / "config" / "env-capture.json").read_text())
    assert "ANTHROPIC_API_KEY" not in captured
    assert "FOUNDRY_API_KEY" not in captured
    assert captured["HOME"] == safe_environment["HOME"]
    assert approval_calls == [("mcp__wiki__read", {"path": "README.md"})]
    assert any(item["raw"]["type"] == "system" for item in events)
    assert any(item["raw"]["type"] == "user" for item in events)
    assert not any(item["event"]["kind"] == "claude.provider_error" for item in events)


def test_auth_stream_error_is_an_envelope_event(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def run() -> list[dict[str, object]]:
        monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
        client = _RecordedClient(
            [{
                "type": "system",
                "subtype": "init",
                "apiKeySource": "unknown",
                "tools": list(translator_tool_names()),
            }]
        )
        lane = WkClaudeLane(
            metadata=WkRunMetadata("wk-claude"),
            run_id="run-auth-error",
            agent_id="WIKI-289",
            worktree=tmp_path,
            model="sonnet",
            loop=WkLoop(status_path=tmp_path / "status.json"),
            client_factory=lambda _options: client,
            options_factory=lambda **_kwargs: object(),
            steering_path=tmp_path / "steering.json",
        )
        with pytest.raises(WkClaudeError, match="not proven plan auth"):
            await lane.start("start")
        await lane.close()
        return [item async for item in lane.events()]

    events = asyncio.run(run())
    assert events[-1]["event"]["kind"] == "claude.provider_error"


def test_midstream_auth_error_is_a_status_envelope() -> None:
    translator = _translator()
    translator.translate(
        {
            "type": "system",
            "subtype": "init",
            "apiKeySource": "none",
            "tools": list(translator_tool_names()),
        }
    )
    event = translator.translate(
        {"type": "assistant", "error": "authentication_failed", "message": {}}
    )
    assert event.kind == "claude.provider_error"
    assert event.phase.value == "status"


def test_settings_source_drift_blocks_the_next_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def run() -> list[dict[str, object]]:
        monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text('{"hooks":{}}', encoding="utf-8")
        client = _RecordedClient(_fixture_events())
        lane = WkClaudeLane(
            metadata=WkRunMetadata("wk-claude"),
            run_id="run-settings-drift",
            agent_id="WIKI-289",
            worktree=tmp_path,
            model="sonnet",
            loop=WkLoop(status_path=tmp_path / "status.json"),
            client_factory=lambda _options: client,
            options_factory=lambda **_kwargs: object(),
            steering_path=tmp_path / "steering.json",
        )
        await lane.start("start")
        settings.write_text('{"hooks":{"PreToolUse":[]}}', encoding="utf-8")
        with pytest.raises(WkClaudeError, match="settings source changed"):
            await lane.send_now("next")
        await lane.close()
        return [item async for item in lane.events()]

    events = asyncio.run(run())
    assert any(item["event"]["kind"] == "claude.provider_error" for item in events)


def test_auth_account_drift_blocks_the_next_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    stub = Path(__file__).parent / "fixtures" / "agent_runtime" / "claude_sdk_stub_cli.py"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    environment = {
        "HOME": str(tmp_path),
        "PATH": os.environ["PATH"],
        "USER": "worker",
        "TERM": "xterm",
        "CLAUDE_CONFIG_DIR": str(config_dir),
    }
    options = SimpleNamespace(cli_path=str(stub), env=environment)

    async def run() -> list[dict[str, object]]:
        lane = WkClaudeLane(
            metadata=WkRunMetadata("wk-claude"),
            run_id="run-auth-drift",
            agent_id="WIKI-289",
            worktree=tmp_path,
            model="sonnet",
            loop=WkLoop(status_path=tmp_path / "status.json"),
            client_factory=lambda _options: _RecordedClient(_fixture_events()),
            options_factory=lambda **_kwargs: options,
            steering_path=tmp_path / "steering.json",
        )
        await lane.start("start")
        (config_dir / "auth-status.json").write_text(
            json.dumps({"accountId": "account-b"}), encoding="utf-8"
        )
        with pytest.raises(WkClaudeError, match="auth identity changed"):
            await lane.send_now("next")
        await lane.close()
        return [item async for item in lane.events()]

    events = asyncio.run(run())
    assert any(item["event"]["kind"] == "claude.provider_error" for item in events)


def test_real_sdk_wire_fixture_translates_and_retains_raw_events() -> None:
    translator = _translator()
    events = [translator.translate(value) for value in _fixture_events()]

    assert len(translator.raw_events) == len(events) == 11
    assert translator.raw_events[1]["message"]["content"][1]["type"] == "tool_use"
    assert events[0].phase.value == "run"
    assert events[1].phase.value == "assistant"
    assert events[2].phase.value == "tool"
    assert events[5].phase.value == "status"
    assert events[8].phase.value == "compaction"
    assert events[9].phase.value == "turn"
    assert events[9].payload["subtype"] == "interrupted"
    assert events[10].payload["resume_from"] == "tool-bash-1"
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


def test_claude_message_content_tool_result_completes_transport_ledger() -> None:
    translator = _translator()
    ledger = WkToolLedger(translator)
    ledger.record_transport_frame(
        {"message": {"content": [{"type": "tool_use", "id": "tool-content-1"}]}}
    )
    with pytest.raises(WkLedgerError, match="missing transport tool results"):
        ledger.reconcile_transport()
    ledger.record_transport_frame(
        {"message": {"content": [{"type": "tool_result", "tool_use_id": "tool-content-1"}]}}
    )
    ledger.reconcile_transport()


def test_real_file_tools_and_registration(tmp_path: Path) -> None:
    loop = WkLoop(status_path=tmp_path / "status.json")
    registry = register_default_wk_tools(WkToolRegistry(), root=tmp_path, loop=loop)
    assert registry.names == set(WK_CLAUDE_TOOL_NAMES)


def test_real_file_tool_round_trip(tmp_path: Path) -> None:
    async def run() -> tuple[WkToolResult, WkToolResult, WkToolResult]:
        write = WkWriteTool(root=tmp_path)
        edit = WkEditTool(root=tmp_path)
        read = WkReadTool(root=tmp_path)
        written = await write.execute(
            WkToolRequest(call_id="write", name="wk.write", arguments={"path": "a.txt", "content": "old"})
        )
        edited = await edit.execute(
            WkToolRequest(
                call_id="edit",
                name="wk.edit",
                arguments={"path": "a.txt", "old": "old", "new": "new"},
            )
        )
        read_result = await read.execute(
            WkToolRequest(call_id="read", name="wk.read", arguments={"path": "a.txt"})
        )
        return written, edited, read_result

    written, edited, read_result = asyncio.run(run())
    assert written.success and edited.success and read_result.stdout == "new"


def test_real_bash_tool_preserves_nonzero_exit_and_timeout(tmp_path: Path) -> None:
    async def run() -> tuple[WkToolResult, WkToolResult]:
        tool = WkBashTool(root=tmp_path)
        failed = await tool.execute(
            WkToolRequest(call_id="failed", name="wk.bash", arguments={"command": "exit 17"})
        )
        timed_out = await tool.execute(
            WkToolRequest(
                call_id="timeout",
                name="wk.bash",
                arguments={"command": "sleep 1", "timeout_ms": 10},
            )
        )
        return failed, timed_out

    failed, timed_out = asyncio.run(run())
    assert failed.success is False and failed.exit_code == 17
    assert timed_out.success is False and timed_out.timed_out is True


def test_real_gate_tool_runs_process_boundary_stub(tmp_path: Path) -> None:
    script = tmp_path / "wiki-gate"
    script.write_text("#!/bin/sh\nprintf '%s\\n' '{\"ready\":true}'\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)

    async def run() -> WkToolResult:
        tool = WkGateTool(root=tmp_path, wiki_command=(str(script),))
        return await tool.execute(
            WkToolRequest(call_id="gate", name="wk.gate", arguments={"pr": "230"})
        )

    result = asyncio.run(run())
    assert result.success is True
    assert result.exit_code == 0
    assert json.loads(result.stdout)["ready"] is True


def test_gate_success_requires_a_real_process_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    script = tmp_path / "wiki-gate"
    marker = tmp_path / "invoked"
    script.write_text(
        f"#!/bin/sh\ntouch {marker}\nprintf '%s\\n' '{{\"ready\":true}}'\nexit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    loop = WkLoop(status_path=tmp_path / "status.json")
    lane = WkClaudeLane(
        metadata=WkRunMetadata("wk-claude"),
        run_id="run-gate-production",
        agent_id="WIKI-289",
        worktree=tmp_path,
        model="sonnet",
        loop=loop,
        options_factory=lambda **_kwargs: object(),
        client_factory=lambda _options: _RecordedClient([]),
        wiki_command=(str(script),),
    )

    async def run() -> WkToolResult:
        return await lane.bridge.invoke("wk.gate", {"pr": "230"}, call_id="gate-1")

    result = asyncio.run(run())
    assert marker.exists()
    assert json.loads(result.stdout)["ready"] is True
    assert result.mutation_receipt is not None
    assert result.mutation_receipt["verdict"] == {"ready": True}
    lane.ledger.reconcile()


def test_gate_success_cannot_disagree_with_exit_code() -> None:
    translator = _translator()
    ledger = WkToolLedger(translator)
    request = WkToolRequest(
        call_id="gate-mismatch",
        name="wk.gate",
        arguments={"pr": "230"},
        mutation=WkMutationClass.PROCESS,
    )
    ledger.record_started(request)
    ledger.record_result(request, WkToolResult(success=True, exit_code=17))
    with pytest.raises(WkLedgerError, match="disagrees with exit code"):
        ledger.reconcile()


@pytest.mark.parametrize(
    ("exit_code", "ready", "fails"),
    ((0, True, False), (0, False, False), (17, True, True), (17, False, False)),
)
def test_gate_reconciliation_covers_exit_and_ready_quadrants(
    exit_code: int, ready: bool, fails: bool
) -> None:
    translator = _translator()
    ledger = WkToolLedger(translator)
    request = WkToolRequest(
        call_id=f"gate-{exit_code}-{ready}",
        name="wk.gate",
        arguments={"pr": "230"},
        mutation=WkMutationClass.PROCESS,
    )
    ledger.record_started(request)
    ledger.record_result(
        request,
        WkToolResult(
            success=exit_code == 0,
            exit_code=exit_code,
            mutation=WkMutationClass.PROCESS,
            duration_ms=1,
            mutation_receipt={
                "pid": 123,
                "stdout_sha256": "a" * 64,
                "stderr_sha256": "b" * 64,
                "verdict": {"ready": ready},
            },
        ),
    )
    if fails:
        with pytest.raises(WkLedgerError, match="ready verdict disagrees"):
            ledger.reconcile()
    else:
        ledger.reconcile()


def test_gate_reconciliation_rejects_empty_verdict() -> None:
    translator = _translator()
    ledger = WkToolLedger(translator)
    request = WkToolRequest(
        call_id="gate-empty",
        name="wk.gate",
        arguments={"pr": "230"},
        mutation=WkMutationClass.PROCESS,
    )
    ledger.record_started(request)
    ledger.record_result(
        request,
        WkToolResult(
            success=True,
            exit_code=0,
            mutation=WkMutationClass.PROCESS,
            mutation_receipt={
                "pid": 123,
                "stdout_sha256": "a" * 64,
                "stderr_sha256": "b" * 64,
                "verdict": {},
            },
        ),
    )
    with pytest.raises(WkLedgerError, match="boolean verdict"):
        ledger.reconcile()


def test_gate_reconciliation_rejects_missing_process_receipt() -> None:
    translator = _translator()
    ledger = WkToolLedger(translator)
    request = WkToolRequest(
        call_id="gate-receipt",
        name="wk.gate",
        arguments={"pr": "230"},
        mutation=WkMutationClass.PROCESS,
    )
    ledger.record_started(request)
    ledger.record_result(
        request,
        WkToolResult(
            success=True,
            exit_code=0,
            mutation=WkMutationClass.PROCESS,
            mutation_receipt={"pid": 123, "verdict": {"ready": True}},
        ),
    )
    with pytest.raises(WkLedgerError, match="process receipt"):
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
    registry = register_default_wk_tools(WkToolRegistry(), root=tmp_path, loop=loop)
    bridge = WkClaudeToolBridge(registry=registry, ledger=ledger, loop=loop)
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
