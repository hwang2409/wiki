from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest

from backend.app import account_notices, github_pr, main
from backend.app.agent_runtime.autopilot import AutopilotController
from backend.app.agent_runtime.fleet_monitor import FleetMonitor
from backend.app.agent_runtime.factory import RealAdapterFactory
from backend.app.agent_runtime.provider import StartRequest
from backend.app.agent_runtime.store import RunStore, RuntimePaths
from backend.app.agent_runtime.supervisor import Supervisor
from backend.app.agent_runtime.types import LifecycleState, ProviderKind, RunRecord
from backend.app.agent_runtime.wk_adapter import WkProviderAdapter
from backend.app.agent_runtime.wk_common import WkSessionTree, WkToolLedger
from backend.app.agent_runtime import wk_feature


FIXTURE = Path(__file__).parent / "fixtures" / "agent_runtime" / "wk_codex_app_server.py"
CLAUDE_FIXTURE = Path(__file__).parent / "fixtures" / "agent_runtime" / "claude_sdk_stub_cli.py"


def _paths(root: Path) -> RuntimePaths:
    runtime = root / "runtime"
    return RuntimePaths(
        runtime_dir=runtime,
        socket_path=runtime / "supervisor.sock",
        registry_path=root / "registry.json",
        archive_dir=root / "archive",
        status_dir=root / "status",
    )


def _factory(root: Path, paths: RuntimePaths) -> RealAdapterFactory:
    codex_home = root / "codex"
    codex_home.mkdir(exist_ok=True)
    environment = {
        "HOME": str(root),
        "PATH": os.environ["PATH"],
        "CODEX_HOME": str(codex_home),
        "WIKI_AGENT_STATUS_DIR": str(paths.status_dir),
    }
    return RealAdapterFactory(
        codex_command=(sys.executable, "-u", str(FIXTURE)),
        env=environment,
        runtime_dir=paths.runtime_dir,
    )


def _claude_factory(
    root: Path, paths: RuntimePaths, environment: dict[str, str]
) -> RealAdapterFactory:
    environment.setdefault("HOME", str(root))
    environment.setdefault("PATH", os.environ["PATH"])
    environment.setdefault("CLAUDE_CONFIG_DIR", str(root / "claude-config"))
    environment.setdefault("WIKI_AGENT_STATUS_DIR", str(paths.status_dir))
    Path(environment["CLAUDE_CONFIG_DIR"]).mkdir(parents=True, exist_ok=True)
    return RealAdapterFactory(
        claude_command=(str(CLAUDE_FIXTURE.resolve()),),
        env=environment,
        runtime_dir=paths.runtime_dir,
    )


def test_wk_factory_dispatches_only_recorded_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    paths = _paths(tmp_path)
    record = RunRecord.new(
        agent_id="WIKI-289",
        provider=ProviderKind.CODEX,
        role="review",
        model="gpt-5.6-terra",
        worktree=str(tmp_path),
        prompt="review",
        execution_kind="wk-codex",
    )
    adapter = _factory(tmp_path, paths)(record)
    assert isinstance(adapter, WkProviderAdapter)
    assert adapter.provider is ProviderKind.CODEX


def test_wk_admission_is_blocked_when_flag_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", False)
    paths = _paths(tmp_path)
    supervisor = Supervisor(RunStore(paths), _factory(tmp_path, paths))

    async def run() -> None:
        with pytest.raises(ValueError, match="wk execution kind is disabled"):
            await supervisor.start_run(
                agent_id="WIKI-289",
                provider=ProviderKind.CODEX,
                role="review",
                model="gpt-5.6-terra",
                worktree=str(tmp_path),
                prompt="review",
                effort="high",
                execution_kind="wk-codex",
            )
        await supervisor.close()

    asyncio.run(run())


@pytest.mark.parametrize("execution_kind", ("wk-claude", "wk-codex"))
def test_wk_adapter_fresh_constructs_and_starts_without_durable_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    execution_kind: str,
) -> None:
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    paths = _paths(tmp_path)
    if execution_kind == "wk-claude":
        environment = {"CLAUDE_CONFIG_DIR": str(tmp_path / "claude-config")}
        factory = _claude_factory(tmp_path, paths, environment)
        command = factory.claude_command
        provider = ProviderKind.CLAUDE
    else:
        factory = _factory(tmp_path, paths)
        command = factory.codex_command
        provider = ProviderKind.CODEX
    record = RunRecord.new(
        agent_id=f"WIKI-289-{execution_kind}",
        provider=provider,
        role="review",
        model="gpt-5.6-terra",
        worktree=str(tmp_path),
        prompt="start",
        execution_kind=execution_kind,
    )

    async def run() -> None:
        adapter = WkProviderAdapter(
            record,
            status_path=paths.status_dir / f"{record.agent_id}.json",
            command=command,
            env=factory.env,
            raw_events_path=paths.runtime_dir / "runs" / record.run_id / "raw.jsonl",
        )
        assert isinstance(adapter._lane.translator.session, WkSessionTree)  # noqa: SLF001
        assert isinstance(adapter._lane.ledger, WkToolLedger)  # noqa: SLF001
        status = await adapter.start(
            StartRequest(
                prompt="start",
                model=record.model,
                effort=None,
                worktree=record.worktree,
                run_id=record.run_id,
                agent_id=record.agent_id,
            )
        )
        assert status.session_id
        await asyncio.sleep(0.3)
        await adapter.close()

    asyncio.run(run())


def test_wk_state_mismatch_is_reported_as_integrity_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    paths = _paths(tmp_path)
    store = RunStore(paths)
    record = store.create(
        RunRecord.new(
            agent_id="WIKI-289",
            provider=ProviderKind.CODEX,
            role="review",
            model="gpt-5.6-terra",
            worktree=str(tmp_path),
            prompt="review",
            execution_kind="wk-codex",
        )
    )
    record.state = LifecycleState.WORKING
    record.core_phase = "turn"
    store._write_record(record)  # noqa: SLF001 - durable route fixture
    registry = json.loads(paths.registry_path.read_text(encoding="utf-8"))
    registry["WIKI-289"]["current"].update(
        {"state": "working", "runtime_state": "working", "provider_state": "idle", "core_phase": "turn"}
    )
    paths.registry_path.write_text(json.dumps(registry) + "\n", encoding="utf-8")
    paths.status_dir.joinpath("WIKI-289.json").write_text(
        '{"state":"working","pr":null,"step":"claimed","blocker":null}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "AGENT_REGISTRY_PATH", paths.registry_path)
    monkeypatch.setattr(main, "AGENT_STATUS_DIR", paths.status_dir)
    monkeypatch.setattr(main, "AGENT_ARCHIVE_DIR", paths.archive_dir)
    monkeypatch.setattr(main, "AGENT_RUNTIME_DIR", paths.runtime_dir)
    monkeypatch.setattr(main, "AGENT_VIEWED_PATH", tmp_path / "viewed.json")
    monkeypatch.setattr(
        main,
        "ACCOUNT_NOTICES",
        account_notices.AccountNoticeStore(tmp_path / "notices.json"),
    )
    result = main.agents()
    worker = next(item for item in result["workers"] if item["ticket"] == "WIKI-289")
    assert worker["state"] == "blocked"
    assert "runtime" in str(worker["blocker"])
    assert worker["core_phase"] == "turn"


def test_wk_dual_stack_routes_start_real_run_and_show_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    paths = _paths(tmp_path)
    store = RunStore(paths)
    supervisor = Supervisor(store, _factory(tmp_path, paths), pid_alive=lambda _pid: False)
    monkeypatch.setattr(main, "AGENT_REGISTRY_PATH", paths.registry_path)
    monkeypatch.setattr(main, "AGENT_STATUS_DIR", paths.status_dir)
    monkeypatch.setattr(main, "AGENT_RUNTIME_DIR", paths.runtime_dir)
    monkeypatch.setattr(main, "AGENT_RUNS_DIR", paths.runs_dir)
    monkeypatch.setattr(main, "AGENT_ARCHIVE_DIR", paths.archive_dir)
    monkeypatch.setattr(main, "AGENT_VIEWED_PATH", tmp_path / "viewed.json")
    main._session_paths.clear()

    async def run() -> None:
        record = await supervisor.start_run(
            agent_id="WIKI-289",
            provider=ProviderKind.CODEX,
            role="review",
            model="gpt-5.6-terra",
            worktree=str(Path.cwd()),
            prompt="read README",
            effort="high",
            execution_kind="wk-codex",
        )
        await asyncio.sleep(0.5)
        session = main.agent_session("WIKI-289", cursor=0, client_path=None)
        dashboard = main.dashboard_tickets()
        assert session["path"].startswith("sqlite://")
        assert session["events"]
        assert "WIKI-289" in json.dumps(dashboard, sort_keys=True)

        forged = {
            "state": "merge-ready",
            "pr": "https://github.com/hwang2409/wiki/pull/999",
            "step": "forged file state",
            "blocker": None,
        }
        status_path = paths.status_dir / "WIKI-289.json"
        status_path.write_text(json.dumps(forged) + "\n", encoding="utf-8")
        worker = next(
            item for item in main.agents()["workers"] if item["ticket"] == "WIKI-289"
        )
        assert worker["state"] != "merge-ready"
        assert worker["pr"] != forged["pr"]

        monkeypatch.setattr(github_pr, "AGENT_REGISTRY_PATH", paths.registry_path)
        monkeypatch.setattr(github_pr, "AGENT_STATUS_DIR", paths.status_dir)
        effective = AutopilotController._default_status("WIKI-289")
        assert effective.get("state") != "merge-ready"
        assert effective.get("pr") != forged["pr"]
        assert github_pr.resolve_pr("WIKI-289") != (
            forged["pr"],
            "hwang2409/wiki",
        )

        live = store.get(record.run_id)
        live.orchestrator_id = "WIKI-289-ORCH"
        store._write_record(live)  # noqa: SLF001 - real fleet evaluation fixture
        async def send_fleet_message(*_args, **_kwargs):
            return None

        fleet = FleetMonitor(store, send_fleet_message)
        await fleet.tick()
        view = next(item for item in fleet._collect_views() if item.record.run_id == record.run_id)  # noqa: SLF001
        assert view.status_state != "merge-ready"
        assert view.pr != forged["pr"]

        await supervisor.send_now(record.run_id, "read README again")
        await asyncio.sleep(0.3)
        projected = json.loads(status_path.read_text(encoding="utf-8"))
        assert projected != forged
        assert projected["step"] != forged["step"]

        archived = await supervisor.archive(record.run_id, outcome="review")
        assert archived.state is LifecycleState.COMPLETED
        archived_payload = main.agents()["archived"]
        assert any(item.get("run_id") == record.run_id for item in archived_payload)
        assert any(paths.archive_dir.rglob("archive-complete.json"))
        await supervisor.close()

    asyncio.run(run())


def test_wk_replace_rekeys_identity_and_honors_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    paths = _paths(tmp_path)
    store = RunStore(paths)
    supervisor = Supervisor(store, _factory(tmp_path, paths), pid_alive=lambda _pid: False)

    async def run() -> None:
        record = await supervisor.start_run(
            agent_id="WIKI-289",
            provider=ProviderKind.CODEX,
            role="review",
            model="gpt-5.6-terra",
            worktree=str(Path.cwd()),
            prompt="read README",
            effort="high",
            execution_kind="wk-codex",
        )
        await asyncio.sleep(0.3)
        old_adapter = supervisor.adapters[record.run_id]
        replacement = await supervisor.replace(
            record.run_id,
            prompt="read pyproject",
            model="gpt-5.6-next",
            execution_kind="wk-codex",
        )
        assert replacement.run_id != record.run_id
        assert replacement.model == "gpt-5.6-next"
        assert supervisor.adapters[replacement.run_id] is not old_adapter
        await supervisor.close()

    asyncio.run(run())


def test_real_wk_run_uses_supervisor_ingest_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    paths = _paths(tmp_path)
    store = RunStore(paths)
    supervisor = Supervisor(store, _factory(tmp_path, paths), pid_alive=lambda _pid: False)

    async def run() -> None:
        record = await supervisor.start_run(
            agent_id="WIKI-289",
            provider=ProviderKind.CODEX,
            role="review",
            model="gpt-5.6-terra",
            worktree=str(Path.cwd()),
            prompt="read README",
            effort="high",
            execution_kind="wk-codex",
        )
        await asyncio.sleep(0.5)
        current = store.get(record.run_id)
        raw_rows = store.read_raw_events(record.run_id)
        normalized_rows = store.read_normalized_events(record.run_id)
        assert current.kind == "wk-codex"
        assert isinstance(current.provider_session_id, str)
        assert isinstance(current.provider_pid, int) and current.provider_pid > 1
        assert current.raw_event_count == current.normalized_event_count
        assert raw_rows and normalized_rows
        assert all("_wk_event" in row["payload"] for row in raw_rows)
        assert all(row["payload"].get("schema") == "wiki.wk.event.v0" for row in normalized_rows)
        sqlite_rows = supervisor.event_store.read_normalized_events(record.run_id)
        assert sqlite_rows
        assert all(row.get("schema") == "wiki.wk.event.v0" for row in sqlite_rows)
        assert all("_wk_event" not in row for row in sqlite_rows)
        sqlite_events = supervisor.event_store.read_events(record.run_id)
        assert sqlite_events
        assert all(event.get("schema") == "wiki.wk.event.v0" for event in sqlite_events)
        assert any("integrity" in event for event in sqlite_events)
        before_source_seq = [int(event["source_seq"]) for event in sqlite_events]
        assert paths.status_dir.joinpath("WIKI-289.json").exists()
        registry = json.loads(paths.registry_path.read_text(encoding="utf-8"))
        assert registry["WIKI-289"]["current"]["kind"] == "wk-codex"
        assert registry["WIKI-289"]["current"]["core_phase"] == "status"
        await supervisor.close()

        restarted = Supervisor(RunStore(paths), _factory(tmp_path, paths), pid_alive=lambda _pid: False)
        results = await restarted.recover_on_start()
        assert any(item["run_id"] == record.run_id for item in results)
        await asyncio.sleep(0.2)
        resumed_adapter = restarted.adapters[record.run_id]
        assert resumed_adapter._lane.translator.session.entries  # noqa: SLF001
        assert resumed_adapter._lane.ledger.operations  # noqa: SLF001
        resumed_events = restarted.event_store.read_events(record.run_id)
        resumed_source_seq = [int(event["source_seq"]) for event in resumed_events]
        assert len(resumed_source_seq) == len(set(resumed_source_seq))
        assert resumed_source_seq[: len(before_source_seq)] == before_source_seq
        assert max(resumed_source_seq) > max(before_source_seq)
        transport = (tmp_path / "codex" / "transport.log").read_text(encoding="utf-8")
        assert "thread/resume" in transport
        await restarted.close()

    asyncio.run(run())


def test_real_wk_claude_run_replays_and_resolves_open_tool_on_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("claude_agent_sdk")
    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    paths = _paths(tmp_path)
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    pause_request = config_dir / "pause-before-result"
    pause_ready = config_dir / "pause-ready"
    resume_result = config_dir / "resume-result"
    pause_request.touch()
    environment = {"CLAUDE_CONFIG_DIR": str(config_dir)}
    supervisor = Supervisor(
        RunStore(paths),
        _claude_factory(tmp_path, paths, environment),
        pid_alive=lambda _pid: False,
    )

    async def wait_for(predicate: object) -> None:
        for _ in range(300):
            if callable(predicate) and predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("timed out waiting for Claude fixture state")

    async def run() -> None:
        record = await supervisor.start_run(
            agent_id="WIKI-289-CLAUDE",
            provider=ProviderKind.CLAUDE,
            role="review",
            model="claude-sonnet-4-6",
            worktree=str(tmp_path),
            prompt="read README",
            effort=None,
            execution_kind="wk-claude",
        )
        await wait_for(pause_ready.exists)
        current = supervisor.store.get(record.run_id)
        assert isinstance(current.provider_pid, int) and current.provider_pid > 1
        os.kill(current.provider_pid, signal.SIGKILL)
        pause_request.unlink()
        await asyncio.sleep(0.3)
        before_events = supervisor.event_store.read_events(record.run_id)
        assert before_events
        assert any(
            row.get("payload", {}).get("type") == "assistant"
            for row in supervisor.store.read_raw_events(record.run_id)
        )
        await supervisor.close()

        resume_result.touch()
        restarted = Supervisor(
            RunStore(paths),
            _claude_factory(tmp_path, paths, environment),
            pid_alive=lambda _pid: False,
        )
        recovery = await restarted.recover_on_start()
        assert any(item["run_id"] == record.run_id and item["action"] == "resume" for item in recovery)
        await wait_for(
            lambda: any(
                row.get("payload", {}).get("result") == "fixture resumed"
                for row in restarted.store.read_raw_events(record.run_id)
            )
        )
        resumed_adapter = restarted.adapters[record.run_id]
        assert isinstance(resumed_adapter._lane.translator.session, WkSessionTree)  # noqa: SLF001
        assert resumed_adapter._lane.translator.session.entries  # noqa: SLF001
        restored_ids = [
            str(entry["source_event_id"])
            for entry in resumed_adapter._lane.translator.session.entries  # noqa: SLF001
            if entry.get("source_event_id")
        ]
        assert restored_ids[: len(before_events)] == [
            str(event["source_event_id"]) for event in before_events
        ]
        assert not resumed_adapter._lane.ledger._transport_pending  # noqa: SLF001
        resumed_events = restarted.event_store.read_events(record.run_id)
        resumed_sequences = [int(event["source_seq"]) for event in resumed_events]
        before_sequences = [int(event["source_seq"]) for event in before_events]
        assert resumed_sequences[: len(before_sequences)] == before_sequences
        assert len(resumed_sequences) == len(set(resumed_sequences))
        assert max(resumed_sequences) > max(before_sequences)
        await restarted.close()

    asyncio.run(run())
