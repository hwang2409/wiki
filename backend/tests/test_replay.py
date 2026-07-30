"""WIKI-174 backend replay endpoint + timeline builder tests.

Round-2 test additions cover the review's HIGH/MEDIUM findings:
    * bounded byte reads (oversized-line drop + torn-tail guard)
    * symlink-safe run dir resolution
    * bookmark classification against fixtures derived from a real Claude
      and a real Codex ``events.jsonl`` (with synthetic supplements for the
      failure-path shapes the sampled sessions didn't happen to emit)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import pytest
from fastapi import HTTPException

from backend.app import main, replay


RUN_ID = "11111111-2222-3333-4444-555555555555"
OTHER_RUN = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "replay"
CODEX_FIXTURE = FIXTURE_DIR / "codex_sample.jsonl"
CLAUDE_FIXTURE = FIXTURE_DIR / "claude_sample.jsonl"


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
            "payload": {"exit_code": 1},
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


# ---------------------------------------------------------------------------
# run_id / summary basics
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Timeline window + bookmarks (synthetic)
# ---------------------------------------------------------------------------


def test_build_timeline_window_returns_all_events(run_dir: Path) -> None:
    events, next_seq, stats = replay.build_timeline_window(run_dir / "events.jsonl")
    assert next_seq is None
    assert [e.seq for e in events] == [1, 2, 3, 4]
    assert stats.dropped_oversize == 0
    assert stats.dropped_truncated_tail is False
    steer = next(e for e in events if e.seq == 2)
    assert steer.bookmark == "steer"
    assert "please do the thing" in steer.summary
    verdict = next(e for e in events if e.seq == 3)
    assert verdict.bookmark == "verdict"
    assert "MERGE-READY" in verdict.summary
    exit_event = next(e for e in events if e.seq == 4)
    # Non-zero exit_code → error bookmark
    assert exit_event.bookmark == "error"


def test_zero_exit_code_is_not_error(tmp_path: Path) -> None:
    """WIKI-174 round-2 review item 3: exit_code=0 was previously flagged as error."""

    path = tmp_path / "events.jsonl"
    _write_events(
        path,
        [
            {
                "seq": 1,
                "raw_seq": 1,
                "kind": "provider_process_exit",
                "disposition": "rendered",
                "payload": {"type": "provider_process_exit", "exit_code": 0},
            },
            {
                "seq": 2,
                "raw_seq": 2,
                "kind": "provider_process_exit",
                "disposition": "rendered",
                "payload": {"method": "provider/processExited", "params": {"returncode": 0}},
            },
        ],
    )
    events, _, _ = replay.build_timeline_window(path)
    assert [e.bookmark for e in events] == [None, None]


def test_build_timeline_window_bounds_response(run_dir: Path) -> None:
    events, next_seq, _ = replay.build_timeline_window(
        run_dir / "events.jsonl", limit=2
    )
    assert len(events) == 2
    assert next_seq == 2
    tail, tail_next, _ = replay.build_timeline_window(
        run_dir / "events.jsonl", after_seq=2, limit=2
    )
    assert [e.seq for e in tail] == [3, 4]
    assert tail_next == 4
    after_end, after_end_next, _ = replay.build_timeline_window(
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
    events, _, stats = replay.build_timeline_window(path)
    assert [e.seq for e in events] == [1, 2]
    assert stats.dropped_malformed == 1


def test_build_bookmarks_caps_output(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    entries = [
        {
            "seq": i,
            "raw_seq": i,
            "kind": "provider_process_exit",
            "disposition": "rendered",
            "payload": {"exit_code": 137},
        }
        for i in range(1, 25)
    ]
    _write_events(path, entries)
    bookmarks, _ = replay.build_bookmarks(path, cap=10)
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
    events, _, _ = replay.build_timeline_window(path)
    assert events[0].bookmark is None


# ---------------------------------------------------------------------------
# Real-shape fixture coverage (round-2 review item 3)
# ---------------------------------------------------------------------------


def test_codex_fixture_yields_all_bookmark_kinds() -> None:
    """Regression: round-1 code returned zero bookmarks against canonical Codex shapes."""

    events, _, _ = replay.build_timeline_window(CODEX_FIXTURE, limit=replay.MAX_LIMIT)
    bookmark_kinds = {e.bookmark for e in events if e.bookmark}
    assert bookmark_kinds == {"steer", "verdict", "error"}
    # userMessage item → steer
    steer = next(e for e in events if e.kind == "item_completed" and e.bookmark == "steer")
    assert "userMessage" in steer.summary
    # agentMessage carrying MERGE-READY → verdict
    verdicts = [e for e in events if e.bookmark == "verdict"]
    assert any(e.kind == "item_completed" for e in verdicts)
    assert any(e.kind == "turn_completed" for e in verdicts)
    # non-zero commandExecution.exitCode, failed turn, non-zero provider exit → error
    errors = [e for e in events if e.bookmark == "error"]
    error_kinds = {e.kind for e in errors}
    assert {"item_completed", "turn_completed", "provider_process_exit"} <= error_kinds


def test_codex_fixture_interrupted_turn_is_not_a_verdict() -> None:
    """A turn that ended with status=interrupted is neither verdict nor error."""

    events, _, _ = replay.build_timeline_window(CODEX_FIXTURE, limit=replay.MAX_LIMIT)
    interrupted = [
        e
        for e in events
        if e.kind == "turn_completed" and e.lifecycle_state == "interrupted"
    ]
    assert interrupted
    assert all(e.bookmark is None for e in interrupted)


def test_claude_fixture_yields_all_bookmark_kinds() -> None:
    events, _, _ = replay.build_timeline_window(CLAUDE_FIXTURE, limit=replay.MAX_LIMIT)
    bookmark_kinds = {e.bookmark for e in events if e.bookmark}
    assert bookmark_kinds == {"steer", "verdict", "error"}
    steer_events = [e for e in events if e.bookmark == "steer"]
    assert any(e.kind == "claude_user" for e in steer_events)
    verdict_events = [e for e in events if e.bookmark == "verdict"]
    assert any(e.kind == "claude_assistant" for e in verdict_events)
    error_events = [e for e in events if e.bookmark == "error"]
    assert any(e.kind == "claude_result" for e in error_events)
    assert any(e.kind == "provider_process_exit" for e in error_events)


# ---------------------------------------------------------------------------
# Bounded byte reads (round-2 review item 1)
# ---------------------------------------------------------------------------


def test_oversized_line_dropped_without_buffering(tmp_path: Path) -> None:
    """A single 20 MiB line must not be buffered into memory.

    We enforce this by keeping ``MAX_LINE_BYTES`` low for this test and
    asserting that (a) the oversize record is not yielded and (b) records
    after the oversized line are still processed. The 20 MiB payload is
    written from disk so ``pytest -x`` doesn't peak on the driver too.
    """

    path = tmp_path / "events.jsonl"
    big_line = b"{" + b" " * (5 * 1024 * 1024) + b'"seq":42}\n'
    good_line = json.dumps(
        {
            "seq": 99,
            "raw_seq": 99,
            "normalized_at": "2026-07-30T00:00:01+00:00",
            "kind": "claude_user",
            "disposition": "rendered",
            "payload": {"message": {"content": [{"type": "text", "text": "hi"}]}},
        }
    ).encode() + b"\n"
    with path.open("wb") as f:
        f.write(good_line)
        f.write(big_line)
        f.write(good_line.replace(b'"seq": 99', b'"seq": 100'))

    stats = replay._ScanStats()
    yielded = list(
        replay._iter_json_events(
            path,
            max_line_bytes=1 * 1024 * 1024,
            stats=stats,
        )
    )
    seqs = [entry.get("seq") for entry in yielded]
    assert 42 not in seqs, "oversized line must be dropped, not yielded"
    assert seqs.count(99) + seqs.count(100) >= 1
    assert stats.dropped_oversize >= 1


def test_scan_bytes_budget_terminates(tmp_path: Path) -> None:
    """Hard byte budget stops long files even if all lines are well-formed."""

    path = tmp_path / "events.jsonl"
    line = json.dumps(
        {
            "seq": 1,
            "raw_seq": 1,
            "kind": "claude_stream_event",
            "disposition": "rendered",
            "payload": {"event": {"type": "message_delta"}},
        }
    )
    # Enough records to exceed the tiny scan budget below.
    with path.open("w", encoding="utf-8") as f:
        for i in range(2_000):
            f.write(line.replace('"seq": 1', f'"seq": {i + 1}') + "\n")
    stats = replay._ScanStats()
    limited = list(
        replay._iter_json_events(
            path,
            max_scan_bytes=4 * 1024,
            stats=stats,
        )
    )
    assert stats.scan_truncated is True
    assert len(limited) < 2_000


def test_torn_write_missing_trailing_newline_dropped(tmp_path: Path) -> None:
    """A live-append that flushed JSON without its trailing newline must be
    treated as incomplete — never yielded until the newline arrives."""

    path = tmp_path / "events.jsonl"
    complete = json.dumps(
        {
            "seq": 1,
            "raw_seq": 1,
            "kind": "claude_user",
            "disposition": "rendered",
            "payload": {"message": {"content": [{"type": "text", "text": "one"}]}},
        }
    )
    torn = json.dumps(
        {
            "seq": 2,
            "raw_seq": 2,
            "kind": "claude_user",
            "disposition": "rendered",
            "payload": {"message": {"content": [{"type": "text", "text": "two"}]}},
        }
    )
    # First record ends with \n (committed); second record is mid-write.
    path.write_bytes(complete.encode() + b"\n" + torn.encode())
    stats = replay._ScanStats()
    yielded = list(replay._iter_json_events(path, stats=stats))
    assert [e.get("seq") for e in yielded] == [1]
    assert stats.dropped_truncated_tail is True


# ---------------------------------------------------------------------------
# Symlink safety (round-2 review item 2)
# ---------------------------------------------------------------------------


def test_symlinked_run_dir_is_rejected(runs_root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "run.json").write_text(json.dumps(_base_run_json()), encoding="utf-8")
    link = runs_root / RUN_ID
    os.symlink(outside, link)
    with mock.patch.object(main, "AGENT_RUNS_DIR", runs_root):
        with pytest.raises(HTTPException) as ctx:
            main._replay_run_dir(RUN_ID)
    assert ctx.value.status_code == 404


def test_symlinked_events_file_is_rejected(runs_root: Path, tmp_path: Path) -> None:
    """Even a real run dir must reject a substituted-symlink child."""

    run_dir = runs_root / RUN_ID
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps(_base_run_json()), encoding="utf-8")
    # Attacker points events.jsonl at an arbitrary file outside the runs root.
    secret = tmp_path / "outside" / "secret.jsonl"
    secret.parent.mkdir(parents=True)
    secret.write_text("secret", encoding="utf-8")
    os.symlink(secret, run_dir / "events.jsonl")
    with mock.patch.object(main, "AGENT_RUNS_DIR", runs_root):
        with pytest.raises(HTTPException) as ctx:
            main._replay_run_dir(RUN_ID)
    assert ctx.value.status_code == 404


def test_iter_snapshot_refuses_symlinked_input(runs_root: Path, tmp_path: Path) -> None:
    """Even if main's guard is bypassed, the reader itself refuses to
    follow symlinks (defence in depth for the round-2 review probe)."""

    target = tmp_path / "outside.jsonl"
    target.write_text("secret\n", encoding="utf-8")
    link = runs_root / "linked.jsonl"
    os.symlink(target, link)
    with pytest.raises(OSError):
        # ``_open_nofollow`` should raise ELOOP on macOS/Linux.
        replay._open_nofollow(link)


def test_resolve_ticket_runs_ignores_symlinked_entries(
    runs_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "impostor"
    outside.mkdir()
    (outside / "run.json").write_text(
        json.dumps(_base_run_json(run_id=RUN_ID)), encoding="utf-8"
    )
    os.symlink(outside, runs_root / RUN_ID)
    with mock.patch.object(main, "AGENT_RUNS_DIR", runs_root):
        assert main._resolve_ticket_runs("WIKI-174") == []


# ---------------------------------------------------------------------------
# Endpoint smoke tests
# ---------------------------------------------------------------------------


def test_timeline_endpoint_happy_path(run_dir: Path, runs_root: Path) -> None:
    with mock.patch.object(main, "AGENT_RUNS_DIR", runs_root):
        payload = main.agent_run_replay_timeline(RUN_ID)
    assert payload["run"]["run_id"] == RUN_ID
    assert payload["run"]["agent_id"] == "WIKI-174"
    assert len(payload["events"]) == 4
    assert payload["next_after_seq"] is None
    bookmark_kinds = {b["kind"] for b in payload["bookmarks"]}
    assert bookmark_kinds >= {"steer", "verdict", "error"}
    assert payload["warnings"] == []


def test_timeline_endpoint_paginates(run_dir: Path, runs_root: Path) -> None:
    with mock.patch.object(main, "AGENT_RUNS_DIR", runs_root):
        first = main.agent_run_replay_timeline(RUN_ID, limit=2)
        assert first["next_after_seq"] == 2
        assert [e["seq"] for e in first["events"]] == [1, 2]
        assert first["bookmarks"]
        second = main.agent_run_replay_timeline(RUN_ID, after_seq=2, limit=2)
        assert [e["seq"] for e in second["events"]] == [3, 4]
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


def test_timeline_endpoint_surfaces_dropped_lines_as_warnings(
    runs_root: Path,
) -> None:
    run_dir = runs_root / RUN_ID
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps(_base_run_json()), encoding="utf-8")
    events_path = run_dir / "events.jsonl"
    events_path.write_bytes(
        json.dumps(
            {
                "seq": 1,
                "raw_seq": 1,
                "kind": "claude_user",
                "disposition": "rendered",
                "payload": {"message": {"content": [{"type": "text", "text": "ok"}]}},
            }
        ).encode()
        + b"\n"
        + b"{not-json\n"
    )
    with mock.patch.object(main, "AGENT_RUNS_DIR", runs_root):
        payload = main.agent_run_replay_timeline(RUN_ID)
    assert any("malformed" in w for w in payload["warnings"])
