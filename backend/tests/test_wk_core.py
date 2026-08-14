from __future__ import annotations

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
        wk_lane="codex",
    )
    value = wk.to_dict()
    assert value["kind"] == "wk-codex"
    assert value["lane"] == "codex"
    assert RunRecord.from_dict(value).kind == "wk-codex"
    assert WkRunMetadata.from_kind("wk-codex").lane == "codex"


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
        },
        max_length=8,
    )
    assert value == {
        "api_key": "[REDACTED]",
        "nested": {
            "authorization": "[REDACTED]",
            "text": "xxxxxxxx[TRUNCATED]",
        },
    }


def test_status_writer_is_loop_owned_atomic_and_sequenced(tmp_path: Path) -> None:
    status_path = tmp_path / "agent.json"
    loop = WkLoop(status_path=status_path)
    assert loop.write_status(
        state="working", pr=None, step="running", blocker=None
    ) == 1
    assert loop.write_status(
        state="blocked", pr=None, step="stopped", blocker="tool failed"
    ) == 2
    assert json.loads(status_path.read_text()) == {
        "blocker": "tool failed",
        "pr": None,
        "state": "blocked",
        "status_write_seq": 2,
        "step": "stopped",
    }
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
