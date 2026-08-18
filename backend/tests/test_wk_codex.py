from __future__ import annotations

import asyncio
import json
import os
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend.app.agent_runtime import wk_feature
from backend.app.agent_runtime.factory import RealAdapterFactory
from backend.app.agent_runtime.provider import StartRequest
from backend.app.agent_runtime.wk_codex import (
    WK_CODEX_DYNAMIC_TOOLS,
    WkCodexDisabled,
    WkCodexError,
    WkCodexLane,
    WkCodexPlanAuthError,
)
from backend.app.agent_runtime.types import ProviderKind, RunRecord
from backend.app.agent_runtime.wk_core import WkLoop, WkRunMetadata
from backend.app.agent_runtime.wk_common import WkLedgerError


FIXTURE = Path(__file__).parent / "fixtures" / "agent_runtime" / "wk_codex_app_server.py"


def _environment(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    codex_home.mkdir(exist_ok=True)
    return {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        "CODEX_HOME": str(codex_home),
        "LANG": "C.UTF-8",
    }


def _lane(
    tmp_path: Path,
    *,
    environment: dict[str, str] | None = None,
    wiki_command: tuple[str, ...] = ("wiki",),
    role: str = "review",
) -> WkCodexLane:
    env = environment or _environment(tmp_path)
    return WkCodexLane(
        metadata=WkRunMetadata("wk-codex"),
        run_id="run-codex",
        agent_id="WIKI-289",
        worktree=tmp_path,
        model="gpt-5-codex",
        loop=WkLoop(status_path=tmp_path / "status.json"),
        role=role,
        command=(sys.executable, str(FIXTURE)),
        auth_command=(sys.executable, str(FIXTURE)),
        environment=env,
        wiki_command=wiki_command,
    )


def test_flag_off_boot_does_not_import_codex_lane_or_sdk() -> None:
    env = dict(os.environ)
    env.pop("WIKI_ENABLE_WK", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import backend.app.main; print('backend.app.agent_runtime.wk_codex' in sys.modules)",
        ],
        cwd=Path(__file__).parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip().splitlines()[-1] == "False"


def test_live_codex_app_server_accepts_recorded_handshake_when_binary_exists() -> None:
    binary = shutil.which("codex")
    if binary is None:
        pytest.skip("codex binary is not installed")
    from backend.app.agent_runtime.wk_codex import codex_plan_auth_environment

    try:
        environment = codex_plan_auth_environment(os.environ)
    except WkCodexPlanAuthError as exc:
        pytest.skip(str(exc))
    process = subprocess.Popen(
        [binary, "app-server", "--stdio"],
        cwd=Path.cwd(),
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        frames = [
            {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "wiki-live-smoke", "title": "Wiki live smoke", "version": "0.1.0"}, "capabilities": {"experimentalApi": True}}},
            {"id": 2, "method": "thread/start", "params": {"cwd": str(Path.cwd()), "model": "gpt-5.6-terra", "approvalPolicy": "never", "sandboxPolicy": {"type": "dangerFullAccess"}, "dynamicTools": [dict(WK_CODEX_DYNAMIC_TOOLS[0])], "developerInstructions": "handshake only", "experimentalRawEvents": True}},
        ]
        for frame in frames:
            process.stdin.write(json.dumps(frame) + "\n")
            process.stdin.flush()
        responses: list[dict[str, object]] = []
        deadline = time.monotonic() + 20
        while len(responses) < len(frames):
            ready, _, _ = select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))
            assert ready, "Codex App Server did not answer the recorded handshake"
            response = json.loads(process.stdout.readline())
            if response.get("id") in {frame["id"] for frame in frames}:
                responses.append(response)
        assert responses[0].get("id") == 1 and "result" in responses[0]
        assert responses[1].get("id") == 2 and "result" in responses[1]
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_codex_lane_is_constructible_only_when_flag_is_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", False)
    with pytest.raises(WkCodexDisabled):
        _lane(tmp_path)
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    assert _lane(tmp_path).metadata.kind == "wk-codex"


def test_codex_inner_record_preserves_lane_role(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    lane = _lane(tmp_path, role="review")
    assert lane._adapter.env["WIKI_AGENT_ROLE"] == "review"


def test_codex_lane_passes_effort_to_the_adapter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    lane = WkCodexLane(
        metadata=WkRunMetadata("wk-codex"),
        run_id="run-codex-effort",
        agent_id="WIKI-289",
        worktree=tmp_path,
        model="gpt-5.6-sol",
        loop=WkLoop(status_path=tmp_path / "status.json"),
        effort="high",
        command=(sys.executable, str(FIXTURE)),
        environment=_environment(tmp_path),
    )
    assert lane.effort == "high"
    assert lane._adapter.effort == "high"


def test_plan_auth_environment_rejects_api_credentials() -> None:
    from backend.app.agent_runtime.wk_codex import codex_plan_auth_environment

    with pytest.raises(WkCodexPlanAuthError, match="OPENAI_API_KEY"):
        codex_plan_auth_environment({"HOME": "/tmp", "OPENAI_API_KEY": "secret"})


def test_dynamic_tool_ownership_blocks_before_the_first_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    env = _environment(tmp_path)
    lane = _lane(tmp_path, environment=env)
    lane._adapter._thread_start_options["dynamicTools"] = []

    async def run() -> None:
        with pytest.raises(WkCodexError, match="dynamicTools"):
            await lane.start("start")

    asyncio.run(run())
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "blocked"
    methods = (Path(env["CODEX_HOME"]) / "transport.log").read_text(encoding="utf-8").splitlines()
    assert "thread/start" in methods
    assert "turn/start" not in methods


def test_policy_validation_finishes_before_any_turn_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    lane = _lane(tmp_path)

    def reject_policy() -> None:
        raise RuntimeError("policy validation failed")

    lane._verify_policy = reject_policy  # type: ignore[method-assign]

    async def run() -> None:
        with pytest.raises(WkCodexError, match="policy validation failed"):
            await lane.start("start")

    asyncio.run(run())
    methods = (Path(lane.environment["CODEX_HOME"]) / "transport.log").read_text(encoding="utf-8").splitlines()
    assert "turn/start" not in methods


def test_native_policy_mismatch_blocks_before_the_first_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    env = _environment(tmp_path)
    (Path(env["CODEX_HOME"]) / "unsafe-sandbox").touch()
    lane = _lane(tmp_path, environment=env)

    async def run() -> None:
        with pytest.raises(WkCodexError, match="read-only native tool confinement"):
            await lane.start("start")

    asyncio.run(run())
    methods = (Path(env["CODEX_HOME"]) / "transport.log").read_text(encoding="utf-8").splitlines()
    assert "turn/start" not in methods


def test_real_codex_transport_translates_and_reconciles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    lane = _lane(tmp_path)

    async def run() -> list[dict[str, object]]:
        await lane.start("start")
        rows: list[dict[str, object]] = []
        async for row in lane.events():
            rows.append(dict(row))
            event = row["event"]
            if isinstance(event, dict) and event.get("kind") == "codex.turn_completed":
                break
        await lane.close()
        return rows

    rows = asyncio.run(run())
    methods = [
        row["raw"].get("method")
        for row in rows
        if isinstance(row.get("raw"), dict)
    ]
    assert "item/started" in methods
    assert "item/completed" in methods
    assert "turn/completed" in methods
    assert [row["event"]["source_seq"] for row in rows] == list(range(1, len(rows) + 1))
    assert all(row["event"]["lane"] == "wk-codex" for row in rows)
    assert lane.translator.raw_events
    assert json.loads(
        (Path(lane.environment["CODEX_HOME"]) / "thread-start-sandbox.json").read_text(
            encoding="utf-8"
        )
    ) == {"type": "readOnly", "networkAccess": False}


def test_real_factory_preserves_the_shared_codex_thread_start_payload(
    tmp_path: Path,
) -> None:
    fake = Path(__file__).parent / "fixtures" / "agent_runtime" / "fake_codex_app_server.py"
    env = _environment(tmp_path)
    env.update(
        {
            "FAKE_PROTOCOL_LOG": str(tmp_path / "protocol.jsonl"),
            "FAKE_CODEX_TRANSCRIPT_DIR": str(tmp_path / "sessions"),
            "WIKI_AGENT_RUNTIME_DIR": str(tmp_path / "runtime"),
        }
    )
    record = RunRecord.new(
        agent_id="WIKI-289-default",
        provider=ProviderKind.CODEX,
        role="implement",
        model="gpt-5.6-terra",
        worktree=str(tmp_path),
        prompt="payload test",
    )
    adapter = RealAdapterFactory(
        codex_command=(sys.executable, "-u", str(fake)),
        env=env,
    )(record)

    async def run() -> None:
        try:
            await adapter.start(
                StartRequest(
                    prompt="payload test",
                    model=record.model,
                    effort=None,
                    worktree=str(tmp_path),
                    run_id=record.run_id,
                    agent_id=record.agent_id,
                )
            )
        finally:
            await adapter.close()

    asyncio.run(run())
    rows = [
        json.loads(line)
        for line in (tmp_path / "protocol.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    thread_start = next(row for row in rows if row.get("method") == "thread/start")
    params = thread_start["params"]
    assert params["sandbox"] == "danger-full-access"
    assert "sandboxPolicy" not in params


def test_server_request_matching_crosses_the_real_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    env = _environment(tmp_path)
    (Path(env["CODEX_HOME"]) / "approval").touch()
    lane = _lane(tmp_path, environment=env)

    async def run() -> list[dict[str, object]]:
        start_task = asyncio.create_task(lane.start("start"))
        rows: list[dict[str, object]] = []
        while True:
            row = dict(await anext(lane.events()))
            rows.append(row)
            raw = row.get("raw")
            if isinstance(raw, dict) and raw.get("method") == "item/commandExecution/requestApproval":
                await lane.respond(71, {"decision": "approve"})
                break
        await start_task
        while True:
            row = dict(await anext(lane.events()))
            rows.append(row)
            event = row.get("event")
            if isinstance(event, dict) and event.get("kind") == "codex.turn_completed":
                break
        await lane.close()
        return rows

    rows = asyncio.run(run())
    assert any(
        isinstance(row.get("raw"), dict) and row["raw"].get("method") == "serverRequest/resolved"
        for row in rows
    )


def test_resume_reuses_the_app_server_session_and_policy_proof(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    env = _environment(tmp_path)
    first = _lane(tmp_path, environment=env)
    second = _lane(tmp_path, environment=env)

    async def run() -> None:
        await first.start("start")
        async for row in first.events():
            event = row.get("event")
            if isinstance(event, dict) and event.get("kind") == "codex.turn_completed":
                break
        await first.close()
        await second.resume("wk-thread-1")
        await second.close()

    asyncio.run(run())


def test_auth_drift_blocks_the_next_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    env = _environment(tmp_path)
    auth_status = Path(env["CODEX_HOME"]) / "auth-status.txt"
    lane = _lane(tmp_path, environment=env)

    async def run() -> None:
        await lane.start("start")
        auth_status.write_text("account-b organization-a", encoding="utf-8")
        with pytest.raises(WkCodexError, match="auth identity changed"):
            await lane.send_now("next")
        await lane.close()

    asyncio.run(run())
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "blocked"


def test_codex_trust_write_is_allowed_but_foreign_settings_drift_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    lane = _lane(tmp_path)

    async def run() -> None:
        await lane.start("start")
        async for row in lane.events():
            event = row.get("event")
            if isinstance(event, dict) and event.get("kind") == "codex.turn_completed":
                break
        config = Path(lane.environment["CODEX_HOME"]) / "config.toml"
        with config.open("a", encoding="utf-8") as handle:
            handle.write("\n[foreign]\nvalue = \"changed\"\n")
        with pytest.raises(WkCodexError, match="settings source changed"):
            await lane.send_now("next")
        await lane.close()

    asyncio.run(run())
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "blocked"


def test_codex_gate_uses_real_process_and_reconciles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    gate = tmp_path / "wiki-gate"
    gate.write_text(
        "#!/bin/sh\nprintf '%s\\n' invoked > gate-invoked\nprintf '%s\\n' '{\"ready\":true}'\nexit 0\n",
        encoding="utf-8",
    )
    gate.chmod(0o755)
    lane = _lane(
        tmp_path,
        environment=_environment(tmp_path),
        wiki_command=(str(gate),),
    )
    (Path(lane.environment["CODEX_HOME"]) / "gate-call").touch()

    async def run() -> None:
        await lane.start("start")
        async for row in lane.events():
            event = row.get("event")
            if isinstance(event, dict) and event.get("kind") == "codex.turn_completed":
                break
        await lane.close()

    asyncio.run(run())
    assert (tmp_path / "gate-invoked").read_text(encoding="utf-8").strip() == "invoked"
    gate_events = [event for event in lane.ledger.events if event.kind.startswith("tool.")]
    assert {event.kind for event in gate_events} == {"tool.started", "tool.completed"}


def test_dropped_tool_result_fails_transport_reconciliation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    env = _environment(tmp_path)
    (Path(env["CODEX_HOME"]) / "drop-tool-result").touch()
    lane = _lane(tmp_path, environment=env)

    async def run() -> None:
        await lane.start("start")
        async for row in lane.events():
            raw = row.get("raw")
            if isinstance(raw, dict) and raw.get("method") == "item/started":
                break
        with pytest.raises(WkLedgerError, match="missing transport tool results"):
            await lane.close()

    asyncio.run(run())
