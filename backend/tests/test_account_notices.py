from __future__ import annotations

from pathlib import Path

from backend.app.account_notices import AccountNoticeStore


def _store(tmp_path: Path) -> AccountNoticeStore:
    return AccountNoticeStore(path=tmp_path / "notices.json")


def _types(store: AccountNoticeStore) -> list[str]:
    return [entry["type"] for entry in store.snapshot()]


def _by_type(store: AccountNoticeStore, kind: str) -> dict:
    matches = [entry for entry in store.snapshot() if entry["type"] == kind]
    assert len(matches) == 1, f"expected one {kind} notice, got {len(matches)}"
    return matches[0]


def test_unresolved_notices_survive_reload(tmp_path: Path) -> None:
    path = tmp_path / "notices.json"
    first = AccountNoticeStore(path=path)
    assert first.apply_event(
        {"type": "codex_limit_no_eligible", "tickets": ["WIKI-1"], "reset_at": None, "ts": "t1"}
    )
    assert first.apply_event({"type": "codex_rotation_failed", "error": "boom", "ts": "t2"})

    reloaded = AccountNoticeStore(path=path)
    assert sorted(_types(reloaded)) == ["codex_limit_no_eligible", "codex_rotation_failed"]


def test_many_notices_are_all_retained(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tickets = [f"WIKI-{index}" for index in range(7)]
    for index, ticket in enumerate(tickets):
        store.apply_event({"type": "claude_limit_hit", "ticket": ticket, "window": "@1", "ts": f"t{index}"})
    store.apply_event({"type": "codex_limit_no_eligible", "tickets": tickets, "reset_at": None, "ts": "t9"})
    assert len(store.snapshot()) == 8


def test_rotation_success_resolves_limit_and_rotation_failure(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.apply_event({"type": "codex_limit_no_eligible", "tickets": ["WIKI-1"], "reset_at": None, "ts": "t1"})
    store.apply_event({"type": "codex_rotation_failed", "error": "boom", "ts": "t2"})
    store.apply_event(
        {"type": "codex_rotation", "from": "a", "to": "b", "revived": ["WIKI-1"], "failed": [], "ts": "t3"}
    )
    assert store.snapshot() == []


def test_exhaustion_tracks_tickets_and_clears_only_proven_revivals(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.apply_event({"type": "codex_auth_dead_exhausted", "tickets": ["WIKI-1"], "ts": "t1"})
    store.apply_event({"type": "codex_auth_dead_exhausted", "tickets": ["WIKI-2"], "ts": "t2"})
    # Both exhausted tickets are retained in one aggregated notice.
    assert _by_type(store, "codex_auth_dead_exhausted")["tickets"] == ["WIKI-1", "WIKI-2"]

    # A clean revival of WIKI-1 must not clear WIKI-2's exhaustion.
    store.apply_event({"type": "codex_auth_dead_revival", "revived": ["WIKI-1"], "failed": [], "ts": "t3"})
    assert _by_type(store, "codex_auth_dead_exhausted")["tickets"] == ["WIKI-2"]

    store.apply_event({"type": "codex_auth_dead_revival", "revived": ["WIKI-2"], "failed": [], "ts": "t4"})
    assert store.snapshot() == []


def test_revive_failures_merge_per_ticket_and_clear_only_revived(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.apply_event(
        {
            "type": "codex_rotation",
            "from": "a",
            "to": "b",
            "revived": [],
            "failed": ["WIKI-3"],
            "failed_reasons": {"WIKI-3": "window gone"},
            "ts": "t1",
        }
    )
    store.apply_event(
        {
            "type": "codex_rotation",
            "from": "b",
            "to": "c",
            "revived": [],
            "failed": ["WIKI-4"],
            "failed_reasons": {"WIKI-4": "spawn failed"},
            "ts": "t2",
        }
    )
    notice = _by_type(store, "codex_rotation")
    assert notice["failed"] == ["WIKI-3", "WIKI-4"]
    assert notice["failed_reasons"] == {"WIKI-3": "window gone", "WIKI-4": "spawn failed"}

    # WIKI-3 revives on the next rotation; WIKI-4 stays failed with its reason.
    store.apply_event(
        {"type": "codex_rotation", "from": "c", "to": "d", "revived": ["WIKI-3"], "failed": [], "ts": "t3"}
    )
    notice = _by_type(store, "codex_rotation")
    assert notice["failed"] == ["WIKI-4"]
    assert notice["failed_reasons"] == {"WIKI-4": "spawn failed"}

    store.apply_event(
        {"type": "codex_rotation", "from": "d", "to": "e", "revived": ["WIKI-4"], "failed": [], "ts": "t4"}
    )
    assert store.snapshot() == []


def test_auth_revival_failures_track_per_ticket(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.apply_event(
        {
            "type": "codex_auth_dead_revival",
            "revived": [],
            "failed": ["WIKI-5", "WIKI-6"],
            "failed_reasons": {"WIKI-5": "no window", "WIKI-6": "no prompt"},
            "ts": "t1",
        }
    )
    # One worker recovers; the other's unresolved failure must survive.
    store.apply_event(
        {"type": "codex_auth_dead_revival", "revived": ["WIKI-5"], "failed": [], "ts": "t2"}
    )
    notice = _by_type(store, "codex_auth_dead_revival")
    assert notice["failed"] == ["WIKI-6"]
    assert notice["failed_reasons"] == {"WIKI-6": "no prompt"}


def test_rotation_revival_also_clears_auth_revive_failures(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.apply_event(
        {"type": "codex_auth_dead_revival", "revived": [], "failed": ["WIKI-7"], "ts": "t1"}
    )
    store.apply_event(
        {"type": "codex_rotation", "from": "a", "to": "b", "revived": ["WIKI-7"], "failed": [], "ts": "t2"}
    )
    assert store.snapshot() == []


def test_verified_auth_clears_exhaustion(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.apply_event({"type": "codex_auth_dead_exhausted", "tickets": ["WIKI-3"], "ts": "t1"})
    store.apply_event(
        {"type": "codex_auth_verified", "success": True, "credential_source": "current", "ts": "t2"}
    )
    assert store.snapshot() == []


def test_generic_session_events_never_resolve_claude_limits(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.apply_event({"type": "claude_limit_hit", "ticket": "WIKI-4", "window": "@2", "ts": "t1"})
    # Queue edits, model changes, and other session invalidations prove
    # nothing about provider recovery.
    assert not store.apply_event({"type": "session", "ticket": "WIKI-4", "surface": "queue", "ts": "t2"})
    assert not store.apply_event({"type": "session", "ticket": "WIKI-4", "surface": None, "ts": "t3"})
    assert _types(store) == ["claude_limit_hit"]


def test_claude_limit_cleared_resolves_only_its_ticket(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.apply_event({"type": "claude_limit_hit", "ticket": "WIKI-4", "window": "@2", "ts": "t1"})
    store.apply_event({"type": "claude_limit_hit", "ticket": "WIKI-5", "window": "@3", "ts": "t2"})
    store.apply_event({"type": "claude_limit_cleared", "ticket": "WIKI-4", "window": "@2", "ts": "t3"})
    remaining = store.snapshot()
    assert [entry["ticket"] for entry in remaining] == ["WIKI-5"]


def test_malformed_and_irrelevant_events_are_ignored(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert not store.apply_event({})
    assert not store.apply_event({"type": 42})
    assert not store.apply_event({"type": "agents"})
    assert not store.apply_event({"type": "claude_limit_hit", "ticket": None, "ts": "t1"})
    assert store.snapshot() == []


def test_snapshot_orders_newest_first(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.apply_event({"type": "claude_limit_hit", "ticket": "WIKI-6", "window": "@1", "ts": "2026-07-31T01:00:00Z"})
    store.apply_event({"type": "codex_limit_no_eligible", "tickets": [], "reset_at": None, "ts": "2026-07-31T02:00:00Z"})
    assert _types(store) == ["codex_limit_no_eligible", "claude_limit_hit"]
