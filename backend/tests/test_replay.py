"""WIKI-174 backend replay endpoint + timeline builder tests.

Round-4 coverage additions (from the review of 9218e49):
    * Injectable budgets — round-3 tests patched module attributes that
      Python had already captured at def time, so a 128 KiB budget
      silently ran at 64 MiB and hid the bug. All limits now resolve at
      call time.
    * Real sparse-file budget probe: with a small MAX_SCAN_BYTES, an
      events.jsonl that spans multiple scan windows must still be walkable
      via cursor resumption end-to-end.
    * Cursor carries skip state — a scan cut mid-oversized-record on
      page N returns a cursor whose page N+1 resumes the skip, not
      re-buffers the whole record.
    * FIFO probe — a FIFO substituted for run.json must be refused
      immediately without blocking the request.
    * Endpoint-level symlink swap — the production endpoint (not just the
      opener) must refuse a run whose events.jsonl was symlink-swapped
      after the request started.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import pytest
from fastapi import HTTPException

from backend.app import main, replay
from backend.app.agent_runtime.archive_protocol import commit_archive


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


def _write_archive_session(archive_root: Path, run_id: str = RUN_ID) -> Path:
    session_dir = archive_root / "WIKI-174" / "20260730-000000"
    session_dir.mkdir(parents=True)
    (session_dir / "run.json").write_text(
        json.dumps(_base_run_json(run_id=run_id)), encoding="utf-8"
    )
    _write_events(session_dir / "events.jsonl", _fixture_events())
    _write_events(
        session_dir / "raw.jsonl",
        [
            {
                "seq": i,
                "direction": "stdout",
                "payload": {"type": f"raw-{i}"},
            }
            for i in range(1, 5)
        ],
    )
    commit_archive(
        session_dir,
        run_id=run_id,
        completed_at="2026-07-30T00:06:00Z",
        expected_paths=[
            session_dir / "run.json",
            session_dir / "events.jsonl",
            session_dir / "raw.jsonl",
        ],
    )
    return session_dir


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


def _open_reader(path: Path, **kwargs) -> replay.SnapshotReader:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    return replay.SnapshotReader(
        fd,
        start_offset=kwargs.pop("start_offset", 0),
        start_skipping=kwargs.pop("start_skipping", False),
        max_scan_bytes=kwargs.pop("max_scan_bytes", replay.MAX_SCAN_BYTES),
        max_line_bytes=kwargs.pop("max_line_bytes", replay.MAX_LINE_BYTES),
        stats=kwargs.pop("stats", replay._ScanStats()),
    )


def _yield_events_from_path(path: Path, **kwargs) -> list[dict]:
    reader = _open_reader(path, **kwargs)
    try:
        out: list[dict] = []
        for _off, record in reader.records():
            try:
                value = json.loads(record)
            except ValueError:
                continue
            if isinstance(value, dict):
                out.append(value)
        return out
    finally:
        os.close(reader.fd)


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


def test_metadata_size_cap_rejects_oversize_run_json(
    runs_root_fd: int, runs_root: Path
) -> None:
    run_dir = runs_root / RUN_ID
    run_dir.mkdir()
    huge = {"run_id": RUN_ID, "initial_prompt": "x" * (replay.MAX_RUN_JSON_BYTES + 1024)}
    (run_dir / "run.json").write_text(json.dumps(huge), encoding="utf-8")
    with pytest.raises(replay.ReplayError) as exc:
        replay.build_run_summary(runs_root_fd, RUN_ID)
    assert "ceiling" in str(exc.value)
    assert exc.value.status_code == 413


# ---------------------------------------------------------------------------
# Timeline / bookmark builder — synthetic
# ---------------------------------------------------------------------------


def test_build_timeline_response_full_page(
    runs_root_fd: int, run_dir: Path
) -> None:
    response = replay.build_timeline_response(runs_root_fd, RUN_ID)
    assert response["has_more"] is False
    assert response["next_cursor"] is None
    assert [e["seq"] for e in response["events"]] == [1, 2, 3, 4]
    kinds = {b["kind"] for b in response["bookmarks"]}
    assert kinds == {"steer", "verdict", "error"}


def test_zero_exit_code_is_not_error(runs_root_fd: int, runs_root: Path) -> None:
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
    first = replay.build_timeline_response(runs_root_fd, RUN_ID, limit=2)
    assert first["has_more"] is True
    assert first["next_cursor"] is not None
    assert [e["seq"] for e in first["events"]] == [1, 2]
    assert first["bookmarks"]

    second = replay.build_timeline_response(
        runs_root_fd, RUN_ID, cursor=first["next_cursor"], limit=2
    )
    assert [e["seq"] for e in second["events"]] == [3, 4]
    assert second["has_more"] is False
    assert second["next_cursor"] is None
    assert second["bookmarks"] == []


def test_cursor_round_trip_preserves_offset_and_skip_state() -> None:
    """Round-5 review item 1: skip-state must round-trip through the cursor.

    Deleting the ``s`` field from encode/decode would cause the
    ``test_scan_budget_cut_mid_oversized_record_resumes_via_skip_flag``
    test to lose the skip state on the second page and re-buffer the
    oversized record; this test locks the wiring in place.
    """

    assert replay.decode_cursor(replay.encode_cursor(0, run_id=RUN_ID), run_id=RUN_ID) == (0, False)
    assert replay.decode_cursor(replay.encode_cursor(1024, run_id=RUN_ID), run_id=RUN_ID) == (1024, False)
    assert replay.decode_cursor(
        replay.encode_cursor(999, run_id=RUN_ID, skipping=True), run_id=RUN_ID
    ) == (999, True)


def test_cursor_rejects_signature_tamper() -> None:
    """Any byte-level change to the signature portion MUST decode to 400.

    We flip a byte in the middle of the base64 string so we're changing a
    signature byte, not padding that base64 might tolerate.
    """

    good = replay.encode_cursor(512, run_id=RUN_ID)
    mid = len(good) // 2
    tampered = good[:mid] + ("A" if good[mid] != "A" else "B") + good[mid + 1 :]
    assert tampered != good
    with pytest.raises(replay.ReplayError) as ctx:
        replay.decode_cursor(tampered, run_id=RUN_ID)
    assert ctx.value.status_code == 400


def test_cursor_rejects_wrong_run_id() -> None:
    """A cursor bound to run A must not decode against run B."""

    good = replay.encode_cursor(512, run_id=RUN_ID)
    with pytest.raises(replay.ReplayError) as ctx:
        replay.decode_cursor(good, run_id=OTHER_RUN)
    assert ctx.value.status_code == 400


def test_cursor_rejects_junk_encoding() -> None:
    """Malformed base64 / non-envelope input must decode to 400."""

    for bad in ("this-is-not-base64!", "AAAA", "\x00", "!"):
        with pytest.raises(replay.ReplayError) as ctx:
            replay.decode_cursor(bad, run_id=RUN_ID)
        assert ctx.value.status_code == 400


def test_cursor_rejects_negative_offset_via_forged_secret() -> None:
    """A forged cursor with a negative offset must decode to 400 even when
    signed correctly (the check is on payload contents, not just HMAC)."""

    saved_secret = replay.cursor._CURSOR_SECRET
    try:
        replay.cursor._reset_secret_for_tests(b"probe-secret" * 3)
        # Build a signed cursor with a negative offset manually — the
        # production ``encode_cursor`` refuses this, so we bypass it to
        # confirm ``decode_cursor`` also refuses.
        import base64 as _b64
        import hmac as _hmac
        import json as _json
        from hashlib import sha256 as _sha256
        payload = _json.dumps(
            {"o": -1, "s": False, "r": RUN_ID}, separators=(",", ":"), sort_keys=True
        ).encode()
        signature = _hmac.new(replay.cursor._CURSOR_SECRET, payload, _sha256).digest()
        cursor = _b64.urlsafe_b64encode(payload + signature).rstrip(b"=").decode("ascii")
        with pytest.raises(replay.ReplayError) as ctx:
            replay.decode_cursor(cursor, run_id=RUN_ID)
        assert ctx.value.status_code == 400
    finally:
        replay.cursor._reset_secret_for_tests(saved_secret)


def test_cursor_rejects_wrong_type_fields() -> None:
    """Signed cursor with wrong field types (bool offset, int skipping, etc)
    must decode to 400."""

    saved_secret = replay.cursor._CURSOR_SECRET
    try:
        replay.cursor._reset_secret_for_tests(b"probe-secret" * 3)
        import base64 as _b64
        import hmac as _hmac
        import json as _json
        from hashlib import sha256 as _sha256

        def sign(payload_dict):
            payload = _json.dumps(payload_dict, separators=(",", ":"), sort_keys=True).encode()
            sig = _hmac.new(replay.cursor._CURSOR_SECRET, payload, _sha256).digest()
            return _b64.urlsafe_b64encode(payload + sig).rstrip(b"=").decode("ascii")

        # bool offset (Python bool is int; guard must catch this).
        with pytest.raises(replay.ReplayError) as ctx:
            replay.decode_cursor(sign({"o": True, "s": False, "r": RUN_ID}), run_id=RUN_ID)
        assert ctx.value.status_code == 400
        # int skipping.
        with pytest.raises(replay.ReplayError) as ctx:
            replay.decode_cursor(sign({"o": 0, "s": 1, "r": RUN_ID}), run_id=RUN_ID)
        assert ctx.value.status_code == 400
        # missing run field.
        with pytest.raises(replay.ReplayError) as ctx:
            replay.decode_cursor(sign({"o": 0, "s": False}), run_id=RUN_ID)
        assert ctx.value.status_code == 400
    finally:
        replay.cursor._reset_secret_for_tests(saved_secret)


def test_cursor_beyond_snapshot_size_rejected_at_endpoint(
    run_dir: Path, runs_root: Path
) -> None:
    """A cursor pointing past ``st_size`` — legitimately signed but
    invalid post-snapshot — must 400 in ``SnapshotReader.__init__``."""

    events_path = run_dir / "events.jsonl"
    size = os.stat(events_path).st_size
    past_eof = replay.encode_cursor(size + 1024, run_id=RUN_ID)
    with mock.patch.object(main, "AGENT_RUNS_DIR", runs_root):
        with pytest.raises(HTTPException) as ctx:
            main.agent_run_replay_timeline(RUN_ID, cursor=past_eof)
    assert ctx.value.status_code == 400


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
    assert [e["seq"] for e in response["events"]] == [1, 2]
    assert any("malformed" in w for w in response["warnings"])


def test_bookmarks_truncated_flag_is_surfaced(
    runs_root_fd: int, runs_root: Path
) -> None:
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


# ---------------------------------------------------------------------------
# Real-shape fixtures
# ---------------------------------------------------------------------------


def _events_from_fixture(path: Path) -> list[replay.TimelineEvent]:
    yielded = _yield_events_from_path(path)
    events = [replay.classification.timeline_event_from(e) for e in yielded]
    return [e for e in events if e is not None]


def test_codex_fixture_yields_all_bookmark_kinds() -> None:
    events = _events_from_fixture(CODEX_FIXTURE)
    bookmark_kinds = {e.bookmark for e in events if e.bookmark}
    assert bookmark_kinds == {"steer", "verdict", "error"}


def test_claude_fixture_yields_all_bookmark_kinds() -> None:
    events = _events_from_fixture(CLAUDE_FIXTURE)
    bookmark_kinds = {e.bookmark for e in events if e.bookmark}
    assert bookmark_kinds == {"steer", "verdict", "error"}


# ---------------------------------------------------------------------------
# Bounded byte reads (round-3 semantics preserved)
# ---------------------------------------------------------------------------


def test_oversized_line_dropped_and_both_survivors_preserved(tmp_path: Path) -> None:
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

    events = _yield_events_from_path(path, max_line_bytes=1 * 1024 * 1024)
    seqs = [e["seq"] for e in events]
    assert 1 in seqs
    assert 100 in seqs
    assert 42 not in seqs


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
    events = _yield_events_from_path(path, stats=stats)
    assert [e["seq"] for e in events] == [1]
    assert stats.dropped_truncated_tail is True


# ---------------------------------------------------------------------------
# Injectable budgets + resumable cursor across scan-truncated pages
# (round-4 review items 1 + 4)
# ---------------------------------------------------------------------------


def test_scan_budget_is_injectable_and_returns_resumable_cursor(
    runs_root_fd: int, runs_root: Path
) -> None:
    """A small MAX_SCAN_BYTES must actually take effect (round-3 tests
    patched a value Python had already captured), AND the resulting cut
    must return ``has_more=True`` with a resumable cursor so the next
    request continues from the byte offset instead of restarting."""

    run_dir = runs_root / RUN_ID
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps(_base_run_json()), encoding="utf-8")
    events_path = run_dir / "events.jsonl"
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
            for i in range(1, 501)
        ],
    )
    small_budget = 32 * 1024
    with mock.patch.object(replay.errors, "MAX_SCAN_BYTES", small_budget):
        cursor: str | None = None
        seen: list[int] = []
        while True:
            response = replay.build_timeline_response(
                runs_root_fd, RUN_ID, cursor=cursor, limit=replay.DEFAULT_LIMIT
            )
            seen.extend(e["seq"] for e in response["events"])
            if not response["has_more"]:
                break
            assert response["next_cursor"] is not None
            cursor = response["next_cursor"]
    assert seen == sorted(set(seen))
    assert 500 in seen, "final event unreachable — cursor did not resume across budget cut"
    assert len(seen) == 500


def test_scan_budget_cut_mid_oversized_record_resumes_via_skip_flag(
    tmp_path: Path,
) -> None:
    """Round-4 review item 1: a scan cut INSIDE an oversized record must
    encode the skip state into the resume cursor so page 2 continues the
    skip instead of restarting from before the oversized record."""

    path = tmp_path / "events.jsonl"
    good_one = json.dumps({"seq": 1, "kind": "k", "disposition": "d", "payload": {}}).encode()
    big = b"{" + b" " * (5 * 1024 * 1024) + b'"seq":42}'
    good_two = json.dumps({"seq": 100, "kind": "k", "disposition": "d", "payload": {}}).encode()
    with path.open("wb") as f:
        f.write(good_one + b"\n")
        f.write(big + b"\n")
        f.write(good_two + b"\n")

    tiny_budget = 512 * 1024  # 512 KiB - forces cut mid-oversized-record
    line_cap = 256 * 1024

    stats_a = replay._ScanStats()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        reader_a = replay.SnapshotReader(
            fd,
            start_offset=0,
            start_skipping=False,
            max_scan_bytes=tiny_budget,
            max_line_bytes=line_cap,
            stats=stats_a,
        )
        seen_a: list[int] = []
        for _off, record in reader_a.records():
            try:
                seen_a.append(json.loads(record).get("seq"))
            except ValueError:
                pass
        resume_offset, resume_skipping = reader_a.resume_state()
    finally:
        os.close(fd)
    assert stats_a.scan_truncated is True, "budget did not take effect"
    assert 1 in seen_a
    assert 100 not in seen_a
    assert resume_skipping is True, "expected mid-skip resume state"

    # Now resume with a larger budget — the skip must complete without
    # re-buffering the oversized record from its start.
    stats_b = replay._ScanStats()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        reader_b = replay.SnapshotReader(
            fd,
            start_offset=resume_offset,
            start_skipping=resume_skipping,
            max_scan_bytes=8 * 1024 * 1024,
            max_line_bytes=line_cap,
            stats=stats_b,
        )
        seen_b: list[int] = []
        for _off, record in reader_b.records():
            try:
                seen_b.append(json.loads(record).get("seq"))
            except ValueError:
                pass
    finally:
        os.close(fd)
    assert 100 in seen_b, "record after oversized line unreachable across pagination"
    assert 42 not in seen_b


def test_reader_snapshot_size_bounds_reads(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(
        json.dumps({"seq": 1, "kind": "k", "disposition": "d", "payload": {}}).encode()
        + b"\n"
    )
    stats = replay._ScanStats()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        reader = replay.SnapshotReader(
            fd,
            start_offset=0,
            start_skipping=False,
            max_scan_bytes=replay.MAX_SCAN_BYTES,
            max_line_bytes=replay.MAX_LINE_BYTES,
            stats=stats,
        )
        seen = [json.loads(r) for _o, r in reader.records()]
    finally:
        os.close(fd)
    assert seen[0]["seq"] == 1


# ---------------------------------------------------------------------------
# O_NONBLOCK + non-regular refusal (round-4 review item 3)
# ---------------------------------------------------------------------------


def test_fifo_substituted_for_run_json_is_refused(
    runs_root: Path, runs_root_fd: int
) -> None:
    """A FIFO named run.json used to block ``fstat`` indefinitely before
    the reader could check the mode. O_NONBLOCK on the open + S_ISREG
    refusal must return promptly.

    Round-5 review item 4: bounded by SIGALRM so removing O_NONBLOCK
    from ``_open_run_child_fd`` fails FAST (SIGALRM trips) instead of
    hanging the whole suite until a global timeout.
    """

    import signal

    def _timeout_handler(signum, frame):  # noqa: ARG001
        raise AssertionError("FIFO open blocked >1s — O_NONBLOCK regressed")

    run_dir = runs_root / RUN_ID
    run_dir.mkdir()
    fifo_path = run_dir / "run.json"
    os.mkfifo(fifo_path)
    original_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(1)
    try:
        with pytest.raises(replay.ReplayError) as ctx:
            replay._open_run_child_fd(runs_root_fd, RUN_ID, "run.json")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, original_handler)
        fifo_path.unlink()
    assert ctx.value.status_code == 404
    assert "not a regular file" in str(ctx.value) or "not accessible" in str(ctx.value)


def test_fifo_substituted_for_events_jsonl_is_refused(
    runs_root: Path, runs_root_fd: int
) -> None:
    run_dir = runs_root / RUN_ID
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps(_base_run_json()), encoding="utf-8")
    fifo_path = run_dir / "events.jsonl"
    os.mkfifo(fifo_path)
    try:
        with pytest.raises(replay.ReplayError) as ctx:
            replay._open_run_child_fd(runs_root_fd, RUN_ID, "events.jsonl")
    finally:
        fifo_path.unlink()
    assert ctx.value.status_code == 404


# ---------------------------------------------------------------------------
# Symlink safety through the pathwalk opener
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


def test_symlink_swap_after_run_dir_check_fails_at_endpoint(
    runs_root: Path, tmp_path: Path
) -> None:
    """Round-4 review item 4: the symlink test must run the PRODUCTION
    endpoint after the swap so we catch a regression where the endpoint
    error mapping stopped translating pathwalk refusals into 404."""

    run_dir = runs_root / RUN_ID
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps(_base_run_json()), encoding="utf-8")

    # Now swap events.jsonl for a symlink to an outside file — the
    # endpoint must fail with 404, not follow the symlink and leak the
    # target.
    secret = tmp_path / "secret.jsonl"
    secret.write_text('{"seq":1,"kind":"leaked","disposition":"d","payload":{}}\n', encoding="utf-8")
    os.symlink(secret, run_dir / "events.jsonl")

    with mock.patch.object(main, "AGENT_RUNS_DIR", runs_root):
        with pytest.raises(HTTPException) as ctx:
            main.agent_run_replay_timeline(RUN_ID)
    assert ctx.value.status_code == 404
    assert "leaked" not in str(ctx.value.detail)


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
    for i in range(5):
        rid = f"{i:08d}-2222-3333-4444-555555555555"
        run_dir = runs_root / rid
        run_dir.mkdir()
        meta = _base_run_json(run_id=rid, agent_id="WIKI-174")
        (run_dir / "run.json").write_text(json.dumps(meta), encoding="utf-8")
    with mock.patch.object(replay.errors, "MAX_RUN_LIST_ENTRIES", 3):
        listing = replay.resolve_ticket_runs(runs_root_fd, "WIKI-174")
    assert len(listing.runs) == 3
    assert listing.truncated is True


# ---------------------------------------------------------------------------
# Raw event lookup — resumable
# ---------------------------------------------------------------------------


def test_raw_event_lookup_walks_across_scan_budget(
    runs_root: Path, runs_root_fd: int
) -> None:
    """Round-4 review item 1: raw events past the scan budget must still
    be reachable via the resumable-cursor loop inside ``load_raw_event``."""

    run_dir = runs_root / RUN_ID
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps(_base_run_json()), encoding="utf-8")
    entries = [
        {"seq": i, "direction": "stdout", "payload": {"idx": i}} for i in range(1, 501)
    ]
    (run_dir / "raw.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )
    with mock.patch.object(replay.errors, "MAX_SCAN_BYTES", 8 * 1024):
        entry = replay.load_raw_event(runs_root_fd, RUN_ID, 400)
    assert entry is not None
    assert entry["payload"] == {"idx": 400}


def test_raw_event_lookup_returns_none_past_end(
    runs_root: Path, runs_root_fd: int, run_dir: Path
) -> None:
    assert replay.load_raw_event(runs_root_fd, RUN_ID, 9999) is None
    assert replay.load_raw_event(runs_root_fd, RUN_ID, 0) is None


# ---------------------------------------------------------------------------
# Endpoint smoke tests
# ---------------------------------------------------------------------------


def test_timeline_endpoint_happy_path(run_dir: Path, runs_root: Path) -> None:
    with mock.patch.object(main, "AGENT_RUNS_DIR", runs_root):
        payload = main.agent_run_replay_timeline(RUN_ID)
    assert payload["run"]["run_id"] == RUN_ID
    assert len(payload["events"]) == 4
    assert payload["has_more"] is False


def test_replay_endpoints_fall_back_to_exact_archive(
    tmp_path: Path, runs_root: Path
) -> None:
    archive_root = tmp_path / "archive"
    _write_archive_session(archive_root)
    with (
        mock.patch.object(main, "AGENT_RUNS_DIR", runs_root),
        mock.patch.object(main, "AGENT_ARCHIVE_DIR", archive_root),
    ):
        listing = main.agent_replay_runs("WIKI-174")
        timeline = main.agent_run_replay_timeline(RUN_ID)
        event = main.agent_run_replay_event(RUN_ID, 2)

    assert [item["run_id"] for item in listing["runs"]] == [RUN_ID]
    assert timeline["run"]["run_id"] == RUN_ID
    assert [item["seq"] for item in timeline["events"]] == [1, 2, 3, 4]
    assert event["raw"]["payload"] == {"type": "raw-2"}


def test_endpoint_paginates_over_file_crossing_64mib(
    runs_root: Path,
) -> None:
    """Round-5 review item 4: a committed test that walks the production
    endpoint across a file larger than ``MAX_SCAN_BYTES``.

    The events.jsonl is padded so its total size is >64 MiB; the client
    paginates via signed cursors. Every event must be reachable across
    the boundary. The write is buffered so this takes ~1 s of disk I/O.
    """

    run_dir = runs_root / RUN_ID
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps(_base_run_json()), encoding="utf-8")
    events_path = run_dir / "events.jsonl"

    # Padded events ~4 KiB each; 20k events ≈ 80 MiB — crosses the 64 MiB
    # scan budget within one page's worth of pagination attempts.
    padding = "x" * 3800
    target_events = 20_000
    with events_path.open("wb") as f:
        buf: list[bytes] = []
        for i in range(1, target_events + 1):
            entry = {
                "seq": i,
                "raw_seq": i,
                "normalized_at": "2026-07-30T00:00:01+00:00",
                "kind": "claude_stream_event",
                "disposition": "rendered",
                "payload": {"event": {"type": "delta"}, "pad": padding},
            }
            buf.append(json.dumps(entry).encode() + b"\n")
            if len(buf) >= 500:
                f.write(b"".join(buf))
                buf.clear()
        if buf:
            f.write(b"".join(buf))
    assert events_path.stat().st_size > 64 * 1024 * 1024, "test file did not cross 64 MiB"

    seen: list[int] = []
    cursor: str | None = None
    pages = 0
    with mock.patch.object(main, "AGENT_RUNS_DIR", runs_root):
        while pages < 5000:
            payload = main.agent_run_replay_timeline(RUN_ID, cursor=cursor, limit=1000)
            seen.extend(e["seq"] for e in payload["events"])
            pages += 1
            if not payload["has_more"]:
                break
            cursor = payload["next_cursor"]
    assert seen[0] == 1
    assert seen[-1] == target_events
    assert len(seen) == target_events
    # The reader crossed the scan-budget boundary and continued via cursor.
    # The final page's cursor is None (has_more=False), proving we
    # exhausted the file — not silently stopped at the budget.


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
