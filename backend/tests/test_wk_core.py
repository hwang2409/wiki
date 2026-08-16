from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.app.agent_models import list_model_options
from backend.app.agent_runtime.types import ProviderKind, RunRecord
from backend.app.agent_runtime.wk_core import (
    WK_EVENT_SCHEMA,
    WkDisposition,
    WkEventEnvelope,
    WkEventPhase,
    WkEventSequencer,
    WkLoop,
    WkMutationClass,
    WkRunMetadata,
    WkToolRequest,
    WkToolResult,
    mutation_input_hash,
    redact_payload,
)
from backend.app.agent_runtime.wk_feature import wk_enabled


def _event_kwargs() -> dict[str, object]:
    return {
        "run_id": "run-1",
        "agent_id": "WIKI-289",
        "kind": "assistant.message",
        "phase": WkEventPhase.ASSISTANT,
        "provider": "codex",
        "lane": "wk-codex",
        "disposition": WkDisposition.RENDERED,
        "ts": "2026-08-14T00:00:00+00:00",
        "payload": {"text": "hello"},
    }


def test_wk_is_off_by_default_and_boot_does_not_import_core() -> None:
    assert wk_enabled() is False
    assert all(not option["kind"].startswith("wk-") for option in list_model_options())

    env = dict(os.environ)
    env.pop("WIKI_ENABLE_WK", None)
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import backend.app.main; print('backend.app.agent_runtime.wk_core' in sys.modules)"],
        cwd=Path(__file__).parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip().splitlines()[-1] == "False"


def test_wk_flag_exposes_only_distinct_kinds_when_enabled() -> None:
    env = dict(os.environ)
    env["WIKI_ENABLE_WK"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from backend.app.agent_models import list_model_options; print(sorted({m['kind'] for m in list_model_options() if m['kind'].startswith('wk-')}))",
        ],
        cwd=Path(__file__).parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "['wk-claude', 'wk-codex']"


def test_wk_off_surface_snapshot_matches_origin_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compare every client and runtime exposure against origin/main bytes.

    The digest was captured from origin/main at 57b3fdd. It covers model
    options, registry entries, runtime cards, /api/agents, spawn validation,
    and both Pydantic schemas.
    """

    from backend.app import account_notices, main
    from backend.app.agent_runtime.runtime_card import runtime_card
    from backend.app.agent_runtime.store import RunStore, RuntimePaths

    fixed = "2026-08-14T00:00:00+00:00"
    root = tmp_path / "surface"
    runtime_dir = root / "runtime"
    paths = RuntimePaths(
        runtime_dir=runtime_dir,
        socket_path=runtime_dir / "supervisor.sock",
        registry_path=runtime_dir / "registry.json",
        archive_dir=root / "archive",
        status_dir=root / "status",
    )
    record = RunRecord.new(
        agent_id="WIKI-289",
        provider=ProviderKind.CODEX,
        role="review",
        model="gpt-5.4",
        worktree="/tmp/wiki-289-snapshot/worktree",
        prompt="review",
        run_id="00000000-0000-4000-8000-000000000289",
    )
    record.created_at = fixed
    record.updated_at = fixed
    registry = RunStore(paths)._registry_current(record)
    registry["log"] = "<runtime>/runs/00000000-0000-4000-8000-000000000289/raw.jsonl"

    monkeypatch.setattr(main, "AGENT_REGISTRY_PATH", root / "registry.json")
    main.AGENT_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    main.AGENT_REGISTRY_PATH.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "AGENT_STATUS_DIR", root / "status")
    main.AGENT_STATUS_DIR.mkdir(parents=True)
    monkeypatch.setattr(main, "AGENT_ARCHIVE_DIR", root / "archive")
    main.AGENT_ARCHIVE_DIR.mkdir(parents=True)
    monkeypatch.setattr(main, "AGENT_VIEWED_PATH", root / "agent-viewed.json")
    main.AGENT_VIEWED_PATH.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        main,
        "ACCOUNT_NOTICES",
        account_notices.AccountNoticeStore(root / "notices.json"),
    )

    try:
        main.spawn_agent(
            main.SpawnWorkerIn(
                ticket="WIKI-289",
                kind="wk-codex",
                role="review",
                model="gpt-5.4",
                effort="high",
                workdir=str(root),
                prompt="review",
            )
        )
    except main.HTTPException as exc:
        spawn_error = {"status_code": exc.status_code, "detail": exc.detail}
    else:
        spawn_error = None

    snapshot = {
        "models": list_model_options(),
        "registry": registry,
        "runtime_card": runtime_card(
            record, status_path=Path("/tmp/wiki-289-snapshot/status/WIKI-289.json")
        ),
        "api_agents": main.agents(),
        "spawn_error": spawn_error,
        "worker_schema": main.SpawnWorkerIn.model_json_schema(),
        "orchestrator_schema": main.SpawnOrchestratorIn.model_json_schema(),
    }
    assert main._normalize_kind("wk-codex") is None  # noqa: SLF001
    assert main._provider_for_kind("wk-codex") is None  # noqa: SLF001
    raw = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(raw).hexdigest() == (
        "3f1dccb59b5789709fb988a2c0d8cefb190dab5e77bb84f3af19f82c879cf998"
    )
    assert b"wk-" not in raw


def test_cc_cdx_run_shape_is_unchanged_and_wk_metadata_is_additive() -> None:
    legacy = RunRecord.new(
        agent_id="WIKI-289",
        provider=ProviderKind.CODEX,
        role="review",
        model="gpt-5.6-sol",
        worktree="/tmp/worktree",
        prompt="review",
    )
    legacy_value = legacy.to_dict()
    assert "execution_kind" not in legacy_value
    assert "kind" not in legacy_value
    assert "lane" not in legacy_value

    wk = RunRecord.new(
        agent_id="WIKI-289",
        provider=ProviderKind.CODEX,
        role="review",
        model="gpt-5.6-sol",
        worktree="/tmp/worktree",
        prompt="review",
        execution_kind="wk-codex",
    )
    value = wk.to_dict()
    assert value["kind"] == "wk-codex"
    assert value["lane"] == "codex"
    assert RunRecord.from_dict(value).kind == "wk-codex"
    assert WkRunMetadata.from_kind("wk-codex").lane == "codex"
    with pytest.raises(ValueError, match="unsupported wk kind"):
        WkRunMetadata("wk-other")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="invalid execution kind/provider pair"):
        RunRecord.new(
            agent_id="WIKI-289",
            provider=ProviderKind.CLAUDE,
            role="review",
            model="sonnet",
            worktree="/tmp/worktree",
            prompt="review",
            execution_kind="wk-codex",
        )
    with pytest.raises(TypeError):
        RunRecord.new(
            agent_id="WIKI-289",
            provider=ProviderKind.CODEX,
            role="review",
            model="gpt-5.6-sol",
            worktree="/tmp/worktree",
            prompt="review",
            execution_kind="wk-codex",
            wk_lane="claude",  # type: ignore[call-arg]
        )


def test_sequence_assignment_is_deterministic_and_owned_by_sink() -> None:
    def build() -> list[dict[str, object]]:
        sequencer = WkEventSequencer(event_id_factory=iter(("event-1", "event-2")).__next__)
        return [
            sequencer.emit(**_event_kwargs()).to_dict(),
            sequencer.emit(**_event_kwargs()).to_dict(),
        ]

    first = build()
    second = build()
    assert [item["source_seq"] for item in first] == [1, 2]
    assert first == second
    assert first[0]["schema"] == WK_EVENT_SCHEMA
    assert WkEventEnvelope.from_dict(first[0]).to_dict() == first[0]


def test_redaction_removes_secrets_and_bounds_output() -> None:
    value = redact_payload(
        {
            "api_key": "secret-value",
            "nested": {"authorization": "Bearer abc123", "text": "x" * 20},
            "usage": {"input_tokens": 12, "output_tokens": 34},
        },
        max_length=8,
    )
    assert value == {
        "api_key": "[REDACTED]",
        "nested": {
            "authorization": "[REDACTED]",
            "text": "xxxxxxxx[TRUNCATED]",
        },
        "usage": {"input_tokens": 12, "output_tokens": 34},
    }


def test_integrity_hash_commits_to_secret_input_but_events_redact_display() -> None:
    first = WkToolRequest(
        call_id="tool-1",
        name="wk.bash",
        arguments={"authorization": "Bearer first-secret"},
    )
    second = WkToolRequest(
        call_id="tool-1",
        name="wk.bash",
        arguments={"authorization": "Bearer second-secret"},
    )
    assert mutation_input_hash(first) != mutation_input_hash(second)
    event = WkEventEnvelope(
        schema=WK_EVENT_SCHEMA,
        source_event_id="event-1",
        source_seq=1,
        run_id="run-1",
        agent_id="WIKI-289",
        kind="tool.started",
        phase=WkEventPhase.TOOL,
        provider="codex",
        lane="wk-codex",
        disposition=WkDisposition.RENDERED,
        ts="2026-08-14T00:00:00+00:00",
        parent_source_event_id=None,
        payload={"arguments": first.arguments},
    )
    assert "first-secret" not in json.dumps(event.to_dict())
    assert "Bearer" not in json.dumps(event.to_dict())


def test_status_writer_is_loop_owned_atomic_and_sequenced(tmp_path: Path) -> None:
    status_path = tmp_path / "agent.json"
    loop = WkLoop(status_path=status_path)
    assert loop.write_status(
        state="working", pr=None, step="running", blocker=None
    ) == 1
    assert loop.write_status(
        state="blocked", pr=None, step="stopped", blocker="tool failed"
    ) == 2
    value = json.loads(status_path.read_text())
    assert {
        key: value[key]
        for key in ("blocker", "pr", "state", "status_write_seq", "step")
    } == {
        "blocker": "tool failed",
        "pr": None,
        "state": "blocked",
        "status_write_seq": 2,
        "step": "stopped",
    }
    assert isinstance(value["status_nonce"], str)
    assert isinstance(value["status_checksum"], str)
    assert list(tmp_path.glob(".*.agent.json.*")) == []

    with pytest.raises(PermissionError):
        loop._status_writer.write(  # type: ignore[attr-defined]
            state="working",
            pr=None,
            step="forged",
            blocker=None,
            authority=object(),  # type: ignore[arg-type]
        )


def test_typed_tool_result_round_trip_preserves_real_exit_code() -> None:
    request = WkToolRequest(
        call_id="tool-1",
        name="wk.bash",
        arguments={"command": "false"},
        mutation=WkMutationClass.PROCESS,
    )
    result = WkToolResult(
        success=False,
        exit_code=17,
        stderr="failed",
        error_class="process_failed",
        mutation=WkMutationClass.PROCESS,
        mutation_receipt={"before": "a", "after": "b"},
    )
    restored = WkToolResult.from_dict(result.to_dict())
    assert restored == result
    assert restored.exit_code == 17
    assert mutation_input_hash(request) == (
        "58f2e9baf3cd2a4c0cb9a84ec74a722251da95532a33c6337f69e4d51a85113c"
    )
