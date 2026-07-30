"""WIKI-174 backend replay endpoint + timeline builder tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest
from fastapi import HTTPException

from backend.app import main, replay


RUN_ID = "11111111-2222-3333-4444-555555555555"
OTHER_RUN = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _write_events(events_path: Path, entries: list[dict]) -> None:
    events_path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n",
        encoding="utf-8",
    )


def _base_run_json(run_id: str = RUN_ID, agent_id: str = "WIKI-174") -> dict:
    return {
        "run_id": run_id,
        "agent_id": agent_id,
        "orchestrator_id": "wiki",
        "role": "implement",
        "provider": "claude",
        "model": "claude-opus-4-7",
        "created_at": "2026-07-30T00:00:00+00:00",
        "updated_at": "2026-07-30T00:05:00+00:00",
        "outcome": "handoff",
        "state": "dead",
        "normalized_event_count": 4,
        "raw_event_count": 4,
        "initial_prompt": "Do the thing " * 40,
    }


def _fixture_events() -> list[dict]:
    return [
        {
            "seq": 1,
            "raw_seq": 1,
            "normalized_at": "2026-07-30T00:00:01+00:00",
            "kind": "claude_client_message",
            "disposition": "ignored",
            "lifecycle_state": None,
            "payload": {"type": "control_request", "request": {"subtype": "initialize"}},
        },
        {
            "seq": 2,
            "raw_seq": 2,
            "normalized_at": "2026-07-30T00:00:02+00:00",
            "kind": "claude_user",
            "disposition": "rendered",
            "lifecycle_state": None,
            "payload": {
                "message": {
                    "content": [
                        {"type": "text", "text": "please do the thing now"},
                    ],
                },
            },
        },
        {
            "seq": 3,
            "raw_seq": 3,
            "normalized_at": "2026-07-30T00:00:03+00:00",
            "kind": "claude_assistant",
            "disposition": "rendered",
            "lifecycle_state": "working",
            "payload": {
                "message": {
                    "content": [
                        {"type": "text", "text": "MERGE-READY: https://github.com/x/y/pull/1"},
                    ],
                },
            },
        },
        {
            "seq": 4,
            "raw_seq": 4,
            "normalized_at": "2026-07-30T00:00:04+00:00",
            "kind": "provider_process_exit",
            "disposition": "rendered",
            "lifecycle_state": "completed",
            "payload": {"exit_code": 0},
        },
    ]


@pytest.fixture()
def runs_root(tmp_path: Path) -> Path:
    root = tmp_path / "runs"
    root.mkdir()
    return root


@pytest.fixture()
def run_dir(runs_root: Path) -> Path:
    run_dir = runs_root / RUN_ID
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps(_base_run_json()), encoding="utf-8")
    _write_events(run_dir / "events.jsonl", _fixture_events())
    (run_dir / "raw.jsonl").write_text(
        "\n".join(
            json.dumps({"seq": i + 1, "direction": "stdout", "payload": {"type": f"raw-{i}"}})
            for i in range(4)
        )
        + "\n",
        encoding="utf-8",
    )
    return run_dir


def test_valid_run_id() -> None:
    assert replay.valid_run_id(RUN_ID)
    assert not replay.valid_run_id("not-a-uuid")
    assert not replay.valid_run_id("../etc/passwd")


def test_build_run_summary_reads_meta(run_dir: Path) -> None:
    summary = replay.build_run_summary(run_dir)
    assert summary.run_id == RUN_ID
    assert summary.agent_id == "WIKI-174"
    assert summary.orch_id == "wiki"
    assert summary.total_events == 4
    assert summary.initial_prompt_excerpt is not None
    assert summary.initial_prompt_excerpt.endswith("…")


def test_build_timeline_window_returns_all_events(run_dir: Path) -> None:
    events, next_seq = replay.build_timeline_window(run_dir / "events.jsonl")
    assert next_seq is None
    assert [e.seq for e in events] == [1, 2, 3, 4]
    steer = next(e for e in events if e.seq == 2)
    assert steer.bookmark == "steer"
    assert "please do the thing" in steer.summary
    verdict = next(e for e in events if e.seq == 3)
    assert verdict.bookmark == "verdict"
    assert "MERGE-READY" in verdict.summary
    exit_event = next(e for e in events if e.seq == 4)
    assert exit_event.bookmark == "error"


def test_build_timeline_window_bounds_response(run_dir: Path) -> None:
    events, next_seq = replay.build_timeline_window(
        run_dir / "events.jsonl", limit=2
    )
    assert len(events) == 2
    assert next_seq == 2
    tail, tail_next = replay.build_timeline_window(
        run_dir / "events.jsonl", after_seq=2, limit=2
    )
    assert [e.seq for e in tail] == [3, 4]
    # Window filled exactly at file end: caller cannot tell without a
    # follow-up read, so ``next_after_seq`` stays non-null and the follow-up
    # call returns an empty page.
    assert tail_next == 4
    after_end, after_end_next = replay.build_timeline_window(
        run_dir / "events.jsonl", after_seq=4, limit=2
    )
    assert after_end == []
    assert after_end_next is None


def test_build_timeline_window_skips_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"seq": 1, "raw_seq": 1, "kind": "k", "disposition": "d", "payload": {}}),
                "{not-json",
                json.dumps({"seq": 2, "raw_seq": 2, "kind": "k", "disposition": "d", "payload": {}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    events, _ = replay.build_timeline_window(path)
    assert [e.seq for e in events] == [1, 2]


def test_build_bookmarks_caps_output(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    entries = [
        {
            "seq": i,
            "raw_seq": i,
            "kind": "provider_process_exit",
            "disposition": "rendered",
            "payload": {"exit_code": 0},
        }
        for i in range(1, 25)
    ]
    _write_events(path, entries)
    bookmarks = replay.build_bookmarks(path, cap=10)
    assert len(bookmarks) == 10
    assert bookmarks[0]["kind"] == "error"


def test_load_raw_event_bailout(run_dir: Path) -> None:
    assert replay.load_raw_event(run_dir, 2) == {
        "seq": 2,
        "direction": "stdout",
        "payload": {"type": "raw-1"},
    }
    assert replay.load_raw_event(run_dir, 99) is None
    assert replay.load_raw_event(run_dir, 0) is None


def test_missing_run_json_raises(tmp_path: Path) -> None:
    with pytest.raises(replay.ReplayError):
        replay.load_run_metadata(tmp_path)


def test_tool_result_only_message_is_not_a_steer(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_events(
        path,
        [
            {
                "seq": 1,
                "raw_seq": 1,
                "kind": "claude_user",
                "disposition": "rendered",
                "payload": {
                    "message": {
                        "content": [
                            {"type": "tool_result", "content": [{"type": "text", "text": "ok"}]},
                        ],
                    },
                },
            }
        ],
    )
    events, _ = replay.build_timeline_window(path)
    assert events[0].bookmark is None


def test_timeline_endpoint_happy_path(run_dir: Path, runs_root: Path) -> None:
    with mock.patch.object(main, "AGENT_RUNS_DIR", runs_root):
        payload = main.agent_run_replay_timeline(RUN_ID)
    assert payload["run"]["run_id"] == RUN_ID
    assert payload["run"]["agent_id"] == "WIKI-174"
    assert len(payload["events"]) == 4
    assert payload["next_after_seq"] is None
    bookmark_kinds = {b["kind"] for b in payload["bookmarks"]}
    assert bookmark_kinds >= {"steer", "verdict", "error"}


def test_timeline_endpoint_paginates(run_dir: Path, runs_root: Path) -> None:
    with mock.patch.object(main, "AGENT_RUNS_DIR", runs_root):
        first = main.agent_run_replay_timeline(RUN_ID, limit=2)
        assert first["next_after_seq"] == 2
        assert [e["seq"] for e in first["events"]] == [1, 2]
        assert first["bookmarks"]
        second = main.agent_run_replay_timeline(RUN_ID, after_seq=2, limit=2)
        assert [e["seq"] for e in second["events"]] == [3, 4]
        # Filled window at file end → non-null cursor; follow-up returns empty.
        assert second["next_after_seq"] == 4
        assert second["bookmarks"] == []
        third = main.agent_run_replay_timeline(RUN_ID, after_seq=4, limit=2)
        assert third["events"] == []
        assert third["next_after_seq"] is None


def test_timeline_endpoint_rejects_bad_run_id(runs_root: Path) -> None:
    with mock.patch.object(main, "AGENT_RUNS_DIR", runs_root):
        with pytest.raises(HTTPException) as bad:
            main.agent_run_replay_timeline("not-a-uuid")
        assert bad.value.status_code == 400
        with pytest.raises(HTTPException) as missing:
            main.agent_run_replay_timeline(OTHER_RUN)
        assert missing.value.status_code == 404


def test_replay_runs_lists_matching_agent_id(runs_root: Path) -> None:
    newer = runs_root / RUN_ID
    newer.mkdir()
    meta = _base_run_json()
    meta["updated_at"] = "2026-07-30T00:10:00+00:00"
    (newer / "run.json").write_text(json.dumps(meta), encoding="utf-8")
    older = runs_root / OTHER_RUN
    older.mkdir()
    older_meta = _base_run_json(run_id=OTHER_RUN)
    older_meta["updated_at"] = "2026-07-30T00:01:00+00:00"
    (older / "run.json").write_text(json.dumps(older_meta), encoding="utf-8")
    unrelated_id = "99999999-9999-9999-9999-999999999999"
    unrelated = runs_root / unrelated_id
    unrelated.mkdir()
    unrelated_meta = _base_run_json(run_id=unrelated_id, agent_id="WIKI-999")
    (unrelated / "run.json").write_text(json.dumps(unrelated_meta), encoding="utf-8")

    with mock.patch.object(main, "AGENT_RUNS_DIR", runs_root):
        payload = main.agent_replay_runs("WIKI-174")
    assert payload["ticket"] == "WIKI-174"
    assert [r["run_id"] for r in payload["runs"]] == [RUN_ID, OTHER_RUN]


def test_replay_runs_rejects_bad_ticket(runs_root: Path) -> None:
    with mock.patch.object(main, "AGENT_RUNS_DIR", runs_root):
        with pytest.raises(HTTPException) as ctx:
            main.agent_replay_runs("../etc/passwd")
        assert ctx.value.status_code == 400


def test_raw_event_endpoint(run_dir: Path, runs_root: Path) -> None:
    with mock.patch.object(main, "AGENT_RUNS_DIR", runs_root):
        body = main.agent_run_replay_event(RUN_ID, 2)
        assert body["run_id"] == RUN_ID
        assert body["raw"]["payload"] == {"type": "raw-1"}
        with pytest.raises(HTTPException) as missing:
            main.agent_run_replay_event(RUN_ID, 999)
        assert missing.value.status_code == 404
        with pytest.raises(HTTPException) as bad:
            main.agent_run_replay_event(RUN_ID, 0)
        assert bad.value.status_code == 400
