from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from backend.app import account_notices, main
from backend.app.agent_runtime.factory import RealAdapterFactory
from backend.app.agent_runtime.store import RunStore, RuntimePaths
from backend.app.agent_runtime.supervisor import Supervisor
from backend.app.agent_runtime.types import LifecycleState, ProviderKind, RunRecord
from backend.app.agent_runtime.wk_adapter import WkProviderAdapter
from backend.app.agent_runtime import wk_feature


FIXTURE = Path(__file__).parent / "fixtures" / "agent_runtime" / "wk_codex_app_server.py"


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
