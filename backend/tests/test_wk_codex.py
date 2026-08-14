from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.app.agent_runtime import wk_feature
from backend.app.agent_runtime.wk_codex import (
    WkCodexDisabled,
    WkCodexError,
    WkCodexLane,
    WkCodexPlanAuthError,
)
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
) -> WkCodexLane:
    env = environment or _environment(tmp_path)
    return WkCodexLane(
        metadata=WkRunMetadata("wk-codex"),
        run_id="run-codex",
        agent_id="WIKI-289",
        worktree=tmp_path,
        model="gpt-5-codex",
        loop=WkLoop(status_path=tmp_path / "status.json"),
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


def test_codex_lane_is_constructible_only_when_flag_is_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", False)
    with pytest.raises(WkCodexDisabled):
        _lane(tmp_path)
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    assert _lane(tmp_path).metadata.kind == "wk-codex"


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
