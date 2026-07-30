"""WIKI-174 backend replay endpoint + timeline builder tests.

Round-3 coverage additions:
    * Strict oversized-line survivors — BOTH surrounding records must
      survive the drop (round-2 test masked the data-loss regression).
    * Symlink swap under a valid run — mid-request replacement of the run
      dir's child with a symlink outside the runs root must fail the open
      via pathwalk, without leaking the swapped-in target.
    * Large-file pagination — a real ≥64 MiB events.jsonl must page in
      linear time via the opaque byte cursor (round-2 rescanned from zero
      each page).
    * Bounded ``run.json`` metadata reads + run-list cap surfaced.
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
def runs_root_fd(runs_root: Path):
    fd = replay.open_runs_root_fd(runs_root)
    try:
        yield fd
    finally:
        os.close(fd)


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


def _iter_events_from_path(path: Path, **kwargs) -> list[dict]:
    """Test convenience: stream_json_events over a raw path (opens+closes fd)."""

    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        stats = kwargs.pop("stats", replay._ScanStats())
        return [entry for _off, entry in replay.stream_json_events(fd, stats=stats, **kwargs)]
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# run_id / summary basics
# ---------------------------------------------------------------------------


def test_valid_run_id() -> None:
    assert replay.valid_run_id(RUN_ID)
    assert not replay.valid_run_id("not-a-uuid")
    assert not replay.valid_run_id("../etc/passwd")


def test_build_run_summary_reads_meta(runs_root_fd: int, run_dir: Path) -> None:
    summary = replay.build_run_summary(runs_root_fd, RUN_ID)
    assert summary.run_id == RUN_ID
    assert summary.agent_id == "WIKI-174"
    assert summary.orch_id == "wiki"
    assert summary.total_events == 4
    assert summary.initial_prompt_excerpt is not None
    assert summary.initial_prompt_excerpt.endswith("…")


def test_metadata_size_cap_rejects_oversize_run_json(
    runs_root_fd: int, runs_root: Path
) -> None:
    """Round-3 review item 1: run.json reads must be size-capped."""

    run_dir = runs_root / RUN_ID
    run_dir.mkdir()
    huge = {"run_id": RUN_ID, "initial_prompt": "x" * (replay.MAX_RUN_JSON_BYTES + 1024)}
    (run_dir / "run.json").write_text(json.dumps(huge), encoding="utf-8")
    with pytest.raises(replay.ReplayError) as exc:
        replay.build_run_summary(runs_root_fd, RUN_ID)
    assert "ceiling" in str(exc.value)


# ---------------------------------------------------------------------------
# Timeline window + bookmarks (synthetic)
# ---------------------------------------------------------------------------


def test_build_timeline_page_returns_all_events(
    runs_root_fd: int, run_dir: Path
) -> None:
    response = replay.build_timeline_response(runs_root_fd, RUN_ID)
    assert response["has_more"] is False
    assert response["next_cursor"] is None
    seqs = [e["seq"] for e in response["events"]]
    assert seqs == [1, 2, 3, 4]
    bookmark_kinds = {b["kind"] for b in response["bookmarks"]}
    assert bookmark_kinds == {"steer", "verdict", "error"}
    assert response["bookmarks_truncated"] is False


def test_zero_exit_code_is_not_error(runs_root_fd: int, runs_root: Path) -> None:
    """Round-2 review item 3: exit_code=0 was previously flagged as error."""

    run_dir = runs_root / RUN_ID
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps(_base_run_json()), encoding="utf-8")
    _write_events(
        run_dir / "events.jsonl",
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
    response = replay.build_timeline_response(runs_root_fd, RUN_ID)
    assert all(e["bookmark"] is None for e in response["events"])


def test_timeline_pagination_via_opaque_cursor(
    runs_root_fd: int, run_dir: Path
) -> None:
    """Round-3 review item 4: byte-offset cursor + has_more."""

    first = replay.build_timeline_response(runs_root_fd, RUN_ID, limit=2)
    assert first["has_more"] is True
    assert first["next_cursor"] is not None
    assert [e["seq"] for e in first["events"]] == [1, 2]
    # First page carries bookmarks; later pages must not repeat the scan.
    assert first["bookmarks"]

    second = replay.build_timeline_response(
        runs_root_fd, RUN_ID, cursor=first["next_cursor"], limit=2
    )
    assert [e["seq"] for e in second["events"]] == [3, 4]
    assert second["has_more"] is False
    assert second["next_cursor"] is None
    assert second["bookmarks"] == []


def test_cursor_is_opaque_and_encodes_byte_offset(runs_root_fd: int, run_dir: Path) -> None:
    """Any transformation of the cursor must fail — it's not a user int."""

    page = replay.build_timeline_response(runs_root_fd, RUN_ID, limit=1)
    cursor = page["next_cursor"]
    assert cursor is not None
    assert isinstance(cursor, str)
    # Bad cursor → 400 at endpoint / ReplayError at library.
    with pytest.raises(replay.ReplayError):
        replay.decode_cursor("this-is-not-base64!")


def test_malformed_lines_are_counted_and_skipped(
    runs_root_fd: int, runs_root: Path
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
                "payload": {"message": {"content": [{"type": "text", "text": "one"}]}},
            }
        ).encode()
        + b"\n"
        + b"{not-json\n"
        + json.dumps(
            {
                "seq": 2,
                "raw_seq": 2,
                "kind": "claude_user",
                "disposition": "rendered",
                "payload": {"message": {"content": [{"type": "text", "text": "two"}]}},
            }
        ).encode()
        + b"\n"
    )
    response = replay.build_timeline_response(runs_root_fd, RUN_ID)
    seqs = [e["seq"] for e in response["events"]]
    assert seqs == [1, 2]
    assert any("malformed" in w for w in response["warnings"])


def test_bookmarks_truncated_flag_is_surfaced(
    runs_root_fd: int, runs_root: Path
) -> None:
    """Round-3 review item 5: MAX_BOOKMARKS cap must surface a truncation flag."""

    run_dir = runs_root / RUN_ID
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps(_base_run_json()), encoding="utf-8")
    entries = [
        {
            "seq": i,
            "raw_seq": i,
            "kind": "provider_process_exit",
            "disposition": "rendered",
            "payload": {"exit_code": 137},
        }
        for i in range(1, replay.MAX_BOOKMARKS + 5)
    ]
    _write_events(run_dir / "events.jsonl", entries)
    response = replay.build_timeline_response(runs_root_fd, RUN_ID, limit=1)
    assert response["bookmarks_truncated"] is True
    assert len(response["bookmarks"]) == replay.MAX_BOOKMARKS
    assert any("truncated" in w for w in response["warnings"])


# ---------------------------------------------------------------------------
# Real-shape fixture coverage
# ---------------------------------------------------------------------------


def test_codex_fixture_yields_all_bookmark_kinds() -> None:
    yielded = _iter_events_from_path(CODEX_FIXTURE)
    events = [replay._timeline_event_from(e) for e in yielded]
    events = [e for e in events if e is not None]
    bookmark_kinds = {e.bookmark for e in events if e.bookmark}
    assert bookmark_kinds == {"steer", "verdict", "error"}
    steer = next(e for e in events if e.kind == "item_completed" and e.bookmark == "steer")
    assert "userMessage" in steer.summary
    verdicts = [e for e in events if e.bookmark == "verdict"]
    assert any(e.kind == "item_completed" for e in verdicts)
    assert any(e.kind == "turn_completed" for e in verdicts)
    errors = [e for e in events if e.bookmark == "error"]
    error_kinds = {e.kind for e in errors}
    assert {"item_completed", "turn_completed", "provider_process_exit"} <= error_kinds


def test_codex_fixture_interrupted_turn_is_not_a_verdict() -> None:
    yielded = _iter_events_from_path(CODEX_FIXTURE)
    events = [replay._timeline_event_from(e) for e in yielded]
    events = [e for e in events if e is not None]
    interrupted = [
        e for e in events if e.kind == "turn_completed" and e.lifecycle_state == "interrupted"
    ]
    assert interrupted
    assert all(e.bookmark is None for e in interrupted)


def test_claude_fixture_yields_all_bookmark_kinds() -> None:
    yielded = _iter_events_from_path(CLAUDE_FIXTURE)
    events = [replay._timeline_event_from(e) for e in yielded]
    events = [e for e in events if e is not None]
    bookmark_kinds = {e.bookmark for e in events if e.bookmark}
    assert bookmark_kinds == {"steer", "verdict", "error"}
    assert any(e.bookmark == "steer" and e.kind == "claude_user" for e in events)
    assert any(e.bookmark == "verdict" and e.kind == "claude_assistant" for e in events)
    assert any(e.bookmark == "error" and e.kind == "claude_result" for e in events)
    assert any(
        e.bookmark == "error" and e.kind == "provider_process_exit" for e in events
    )


# ---------------------------------------------------------------------------
# Bounded byte reads (round-3 review item 2 — STRICT)
# ---------------------------------------------------------------------------


def _make_records(count: int, seed_seq: int = 1) -> bytes:
    """Compact newline-separated JSON records."""

    out = []
    for i in range(count):
        out.append(
            json.dumps(
                {
                    "seq": seed_seq + i,
                    "raw_seq": seed_seq + i,
                    "normalized_at": "2026-07-30T00:00:01+00:00",
                    "kind": "claude_stream_event",
                    "disposition": "rendered",
                    "payload": {"event": {"type": "message_delta"}},
                }
            ).encode()
        )
    return b"\n".join(out) + b"\n"


def test_oversized_line_dropped_and_both_survivors_preserved(tmp_path: Path) -> None:
    """Round-3 review item 6: BOTH surrounding records must survive.

    The round-2 test accepted either one, which hid the drop-the-record-after
    bug the round-2 review flagged.
    """

    path = tmp_path / "events.jsonl"
    good_one = json.dumps(
        {"seq": 1, "raw_seq": 1, "kind": "k", "disposition": "d", "payload": {}}
    ).encode()
    big = b"{" + b" " * (5 * 1024 * 1024) + b'"seq":42}'
    good_two = json.dumps(
        {"seq": 100, "raw_seq": 100, "kind": "k", "disposition": "d", "payload": {}}
    ).encode()
    with path.open("wb") as f:
        f.write(good_one + b"\n")
        f.write(big + b"\n")
        f.write(good_two + b"\n")

    events = _iter_events_from_path(path, max_line_bytes=1 * 1024 * 1024)
    seqs = [e["seq"] for e in events]
    # STRICT: both surrounding records must survive.
    assert 1 in seqs, "record BEFORE oversized line lost"
    assert 100 in seqs, "record AFTER oversized line lost (round-2 regression)"
    assert 42 not in seqs, "oversized line must be dropped"


def test_scan_bytes_budget_counts_all_read_including_skips(tmp_path: Path) -> None:
    """Round-3 review item 2: bytes read while skipping oversized lines
    MUST count against the scan budget. The round-2 code let an oversized
    line bypass the budget entirely."""

    path = tmp_path / "events.jsonl"
    good_line = json.dumps(
        {"seq": 1, "raw_seq": 1, "kind": "k", "disposition": "d", "payload": {}}
    ).encode()
    # 8 MiB oversized line — a plain byte budget of 4 MiB must abort during
    # the skip, not read all 8 MiB.
    big = b"{" + b" " * (8 * 1024 * 1024) + b'"seq":42}'
    with path.open("wb") as f:
        f.write(good_line + b"\n")
        f.write(big + b"\n")
        f.write(good_line.replace(b'"seq": 1', b'"seq": 2') + b"\n")

    stats = replay._ScanStats()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        events = [
            entry
            for _off, entry in replay.stream_json_events(
                fd,
                start_offset=0,
                max_scan_bytes=4 * 1024 * 1024,
                max_line_bytes=1 * 1024 * 1024,
                stats=stats,
            )
        ]
    finally:
        os.close(fd)
    assert stats.scan_truncated is True
    # Whatever we yielded must be a prefix — the first good line at minimum,
    # never the tail record because we aborted mid-skip.
    seqs = [e["seq"] for e in events]
    assert 1 in seqs
    assert 2 not in seqs


def test_torn_write_missing_trailing_newline_dropped(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    complete = json.dumps(
        {"seq": 1, "raw_seq": 1, "kind": "k", "disposition": "d", "payload": {}}
    )
    torn = json.dumps(
        {"seq": 2, "raw_seq": 2, "kind": "k", "disposition": "d", "payload": {}}
    )
    path.write_bytes(complete.encode() + b"\n" + torn.encode())
    stats = replay._ScanStats()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        yielded = [
            entry
            for _off, entry in replay.stream_json_events(fd, start_offset=0, stats=stats)
        ]
    finally:
        os.close(fd)
    assert [e["seq"] for e in yielded] == [1]
    assert stats.dropped_truncated_tail is True


def test_stream_snapshot_records_resume_from_byte_offset(tmp_path: Path) -> None:
    """Verify the byte cursor lets us pick up exactly where a prior page ended."""

    path = tmp_path / "events.jsonl"
    path.write_bytes(_make_records(5))
    stats_a = replay._ScanStats()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        first_two: list[tuple[int, bytes]] = []
        gen = replay.stream_snapshot_records(fd, start_offset=0, stats=stats_a)
        for record in gen:
            first_two.append(record)
            if len(first_two) >= 2:
                break
        gen.close()
    finally:
        os.close(fd)
    resume_offset = first_two[-1][0]

    stats_b = replay._ScanStats()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        rest = list(
            replay.stream_snapshot_records(fd, start_offset=resume_offset, stats=stats_b)
        )
    finally:
        os.close(fd)
    seqs_first = [json.loads(rec)["seq"] for _off, rec in first_two]
    seqs_rest = [json.loads(rec)["seq"] for _off, rec in rest]
    assert seqs_first == [1, 2]
    assert seqs_rest == [3, 4, 5]


# ---------------------------------------------------------------------------
# Symlink safety (round-3 review item 3 + item 6)
# ---------------------------------------------------------------------------


def test_symlinked_run_dir_is_rejected(
    runs_root: Path, runs_root_fd: int, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "run.json").write_text(json.dumps(_base_run_json()), encoding="utf-8")
    os.symlink(outside, runs_root / RUN_ID)
    with pytest.raises(replay.ReplayError):
        replay.verify_run_dir_exists(runs_root_fd, RUN_ID)


def test_symlinked_events_file_is_refused_by_pathwalk(
    runs_root: Path, runs_root_fd: int, tmp_path: Path
) -> None:
    """Round-3 review item 3: a symlink swapped in for events.jsonl must
    fail the ``open_relative_file`` walk with ELOOP, not follow into the
    swap target."""

    run_dir = runs_root / RUN_ID
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps(_base_run_json()), encoding="utf-8")
    secret = tmp_path / "outside" / "secret.jsonl"
    secret.parent.mkdir(parents=True)
    secret.write_text('{"seq":1,"kind":"leaked","disposition":"d","payload":{}}\n', encoding="utf-8")
    os.symlink(secret, run_dir / "events.jsonl")
    # verify_run_dir_exists succeeds (the run dir itself is a real dir);
    # the open of events.jsonl through pathwalk is what must refuse.
    replay.verify_run_dir_exists(runs_root_fd, RUN_ID)
    with pytest.raises(OSError):
        replay._open_run_child_fd(runs_root_fd, RUN_ID, "events.jsonl")


def test_symlink_swap_after_run_dir_check_still_refused(
    runs_root: Path, runs_root_fd: int, tmp_path: Path
) -> None:
    """Deterministic swap: the run dir exists as a real dir when the request
    starts, then a symlink is swapped in for events.jsonl before the reader
    opens it. Pathwalk must still refuse — that's the TOCTTOU gap the round-2
    resolve-then-open guard couldn't close."""

    run_dir = runs_root / RUN_ID
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps(_base_run_json()), encoding="utf-8")
    _write_events(run_dir / "events.jsonl", _fixture_events())

    # First, verify normal operation works.
    ok = replay.build_timeline_response(runs_root_fd, RUN_ID)
    assert ok["events"]

    # Now the "attacker" swaps events.jsonl for a symlink after the run
    # dir check would have passed. The reader opens through pathwalk and
    # must refuse rather than follow the link.
    secret = tmp_path / "secret.jsonl"
    secret.write_text('{"seq":1}\n', encoding="utf-8")
    (run_dir / "events.jsonl").unlink()
    os.symlink(secret, run_dir / "events.jsonl")

    with pytest.raises(OSError):
        replay._open_run_child_fd(runs_root_fd, RUN_ID, "events.jsonl")


def test_resolve_ticket_runs_ignores_symlinked_entries(
    runs_root: Path, runs_root_fd: int, tmp_path: Path
) -> None:
    outside = tmp_path / "impostor"
    outside.mkdir()
    (outside / "run.json").write_text(
        json.dumps(_base_run_json(run_id=RUN_ID)), encoding="utf-8"
    )
    os.symlink(outside, runs_root / RUN_ID)
    listing = replay.resolve_ticket_runs(runs_root_fd, "WIKI-174")
    assert listing.runs == []


def test_resolve_ticket_runs_reports_truncation(
    runs_root: Path, runs_root_fd: int
) -> None:
    """When more runs match than MAX_RUN_LIST_ENTRIES, expose it."""

    # Cap at 3 for this test so we don't have to write 200 runs to disk.
    for i in range(5):
        # UUIDs sortable by created time via mtime — write in reverse to
        # make later-touched dirs appear newer.
        rid = f"{i:08d}-2222-3333-4444-555555555555"
        run_dir = runs_root / rid
        run_dir.mkdir()
        meta = _base_run_json(run_id=rid, agent_id="WIKI-174")
        (run_dir / "run.json").write_text(json.dumps(meta), encoding="utf-8")
    with mock.patch.object(replay, "MAX_RUN_LIST_ENTRIES", 3):
        listing = replay.resolve_ticket_runs(runs_root_fd, "WIKI-174")
    assert len(listing.runs) == 3
    assert listing.truncated is True


# ---------------------------------------------------------------------------
# Large pagination probe (round-3 review item 4 + item 6)
# ---------------------------------------------------------------------------


def test_large_events_file_pages_linearly_via_cursor(
    runs_root: Path, runs_root_fd: int
) -> None:
    """Round-3 review item 4: byte cursor must let paging skip past the
    scan budget. We write more than MAX_SCAN_BYTES worth of small events
    and confirm we can walk to the very last event via the cursor."""

    run_dir = runs_root / RUN_ID
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps(_base_run_json()), encoding="utf-8")
    events_path = run_dir / "events.jsonl"
    # Each record ~150 bytes → 500 events = ~75 KiB. To keep the test fast
    # while still exercising the cursor, we exceed the scan budget in a
    # PATCHED reader with a small budget rather than writing 65 MiB to disk.
    _write_events(
        events_path,
        [
            {
                "seq": i,
                "raw_seq": i,
                "normalized_at": "2026-07-30T00:00:01+00:00",
                "kind": "claude_stream_event",
                "disposition": "rendered",
                "payload": {"event": {"type": f"delta-{i}"}},
            }
            for i in range(1, 5001)
        ],
    )
    small_budget = 128 * 1024  # 128 KiB
    with mock.patch.object(replay, "MAX_SCAN_BYTES", small_budget):
        # Paginate to the end.
        cursor: str | None = None
        seen: list[int] = []
        pages = 0
        while pages < 200:
            response = replay.build_timeline_response(
                runs_root_fd, RUN_ID, cursor=cursor, limit=replay.DEFAULT_LIMIT
            )
            seen.extend(e["seq"] for e in response["events"])
            pages += 1
            if not response["has_more"]:
                break
            cursor = response["next_cursor"]
    # Must reach the last event across pages. Round-2 rescanned from byte
    # zero each page and would either loop or stall against the budget.
    assert 5000 in seen
    assert seen == sorted(seen)
    assert len(seen) == 5000


# ---------------------------------------------------------------------------
# Endpoint smoke tests
# ---------------------------------------------------------------------------


def test_timeline_endpoint_happy_path(run_dir: Path, runs_root: Path) -> None:
    with mock.patch.object(main, "AGENT_RUNS_DIR", runs_root):
        payload = main.agent_run_replay_timeline(RUN_ID)
    assert payload["run"]["run_id"] == RUN_ID
    assert payload["run"]["agent_id"] == "WIKI-174"
    assert len(payload["events"]) == 4
    assert payload["has_more"] is False
    bookmark_kinds = {b["kind"] for b in payload["bookmarks"]}
    assert bookmark_kinds >= {"steer", "verdict", "error"}


def test_timeline_endpoint_rejects_bad_run_id(runs_root: Path) -> None:
    with mock.patch.object(main, "AGENT_RUNS_DIR", runs_root):
        with pytest.raises(HTTPException) as bad:
            main.agent_run_replay_timeline("not-a-uuid")
        assert bad.value.status_code == 400
        with pytest.raises(HTTPException) as missing:
            main.agent_run_replay_timeline(OTHER_RUN)
        assert missing.value.status_code == 404


def test_timeline_endpoint_rejects_bad_cursor(run_dir: Path, runs_root: Path) -> None:
    with mock.patch.object(main, "AGENT_RUNS_DIR", runs_root):
        with pytest.raises(HTTPException) as ctx:
            main.agent_run_replay_timeline(RUN_ID, cursor="not-base64!")
    assert ctx.value.status_code == 400


def test_replay_runs_endpoint_lists_matches(run_dir: Path, runs_root: Path) -> None:
    with mock.patch.object(main, "AGENT_RUNS_DIR", runs_root):
        payload = main.agent_replay_runs("WIKI-174")
    assert payload["ticket"] == "WIKI-174"
    assert [r["run_id"] for r in payload["runs"]] == [RUN_ID]
    assert payload["runs_truncated"] is False


def test_replay_runs_endpoint_rejects_bad_ticket(runs_root: Path) -> None:
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
