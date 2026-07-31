from __future__ import annotations

from pathlib import Path

from backend.app.account_notices import AccountNoticeStore


def _store(tmp_path: Path) -> AccountNoticeStore:
    return AccountNoticeStore(path=tmp_path / "notices.json")


def _types(store: AccountNoticeStore) -> list[str]:
    return [entry["type"] for entry in store.snapshot()]


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


def test_rotation_with_failures_sets_revive_notice_until_clean_rotation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.apply_event(
        {
            "type": "codex_rotation",
            "from": "a",
            "to": "b",
            "revived": [],
            "failed": ["WIKI-2"],
            "failed_reasons": {"WIKI-2": "window gone"},
            "ts": "t1",
        }
    )
    assert _types(store) == ["codex_rotation"]
    store.apply_event(
        {"type": "codex_rotation", "from": "b", "to": "c", "revived": ["WIKI-2"], "failed": [], "ts": "t2"}
    )
    assert store.snapshot() == []


def test_auth_exhausted_resolved_by_clean_revival_or_verified_auth(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.apply_event({"type": "codex_auth_dead_exhausted", "tickets": ["WIKI-3"], "ts": "t1"})
    store.apply_event(
        {"type": "codex_auth_dead_revival", "revived": [], "failed": ["WIKI-3"], "ts": "t2"}
    )
    assert sorted(_types(store)) == ["codex_auth_dead_exhausted", "codex_auth_dead_revival"]

    store.apply_event({"type": "codex_auth_dead_revival", "revived": ["WIKI-3"], "failed": [], "ts": "t3"})
    assert store.snapshot() == []

    store.apply_event({"type": "codex_auth_dead_exhausted", "tickets": ["WIKI-3"], "ts": "t4"})
    store.apply_event(
        {"type": "codex_auth_verified", "success": True, "credential_source": "current", "ts": "t5"}
    )
    assert store.snapshot() == []


def test_claude_limit_resolved_by_session_activity_for_same_ticket(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.apply_event({"type": "claude_limit_hit", "ticket": "WIKI-4", "window": "@2", "ts": "t1"})
    store.apply_event({"type": "session", "ticket": "WIKI-5", "ts": "t2"})
    assert _types(store) == ["claude_limit_hit"]
    store.apply_event({"type": "session", "ticket": "WIKI-4", "ts": "t3"})
    assert store.snapshot() == []


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
