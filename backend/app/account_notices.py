"""Durable store of unresolved provider-health notices.

Account events (usage limits, rotation failures, dead auth, failed worker
revivals) arrive over SSE only while the app is open. This store keeps the
unresolved ones on disk so the action guidance survives an app reload, and
clears a notice only when a matching recovery event proves resolution.

Worker-scoped failures (dead-auth exhaustion, failed revivals) are tracked
per ticket inside one aggregated notice per failure kind: new events merge
their tickets in, and a recovery event removes only the tickets it proves
revived. Fleet-wide conditions (usage-limit deadlock, rotation failure)
stay single-keyed.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path


def _default_path() -> Path:
    override = os.environ.get("WIKI_ACCOUNT_NOTICES_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".wiki" / "account-notices.json"


def _ticket_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, str) and entry]


def _reason_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        ticket: reason
        for ticket, reason in value.items()
        if isinstance(ticket, str) and isinstance(reason, str)
    }


_EXHAUSTED_KEY = "codex:auth-exhausted"
_REVIVE_FAILED_KEYS = {
    "codex_rotation": "codex:rotation-revive-failed",
    "codex_auth_dead_revival": "codex:auth-revive-failed",
}


class AccountNoticeStore:
    """Keyed unresolved notices with recovery-event resolution."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _default_path()
        self._lock = threading.Lock()
        self._notices: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            key: value
            for key, value in data.items()
            if isinstance(key, str) and isinstance(value, dict)
        }

    def _persist(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self._notices, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            # Persistence is best-effort; in-memory state stays authoritative.
            pass

    def apply_event(self, event: dict) -> bool:
        """Set or resolve notices from one SSE event. True when state changed."""

        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return False
        with self._lock:
            changed = self._apply_locked(event)
            if changed:
                self._persist()
            return changed

    def _apply_locked(self, event: dict) -> bool:
        kind = event["type"]
        changed = False

        def set_notice(key: str, payload: dict) -> None:
            nonlocal changed
            self._notices[key] = payload
            changed = True

        def clear(*keys: str) -> None:
            nonlocal changed
            for key in keys:
                if self._notices.pop(key, None) is not None:
                    changed = True

        def merge_exhausted(tickets: list[str]) -> None:
            existing = self._notices.get(_EXHAUSTED_KEY)
            merged = sorted(
                set(_ticket_list(existing.get("tickets")) if existing else [])
                | set(tickets)
            )
            payload = dict(event)
            payload["tickets"] = merged
            set_notice(_EXHAUSTED_KEY, payload)

        def remove_revived(revived: list[str]) -> None:
            """A revived worker is proven running: drop it from every
            worker-scoped failure notice; keep tickets not yet proven."""

            if not revived:
                return
            revived_set = set(revived)
            existing = self._notices.get(_EXHAUSTED_KEY)
            if existing is not None:
                remaining = [
                    ticket
                    for ticket in _ticket_list(existing.get("tickets"))
                    if ticket not in revived_set
                ]
                if remaining != _ticket_list(existing.get("tickets")):
                    if remaining:
                        updated = dict(existing)
                        updated["tickets"] = remaining
                        set_notice(_EXHAUSTED_KEY, updated)
                    else:
                        clear(_EXHAUSTED_KEY)
            for key in _REVIVE_FAILED_KEYS.values():
                notice = self._notices.get(key)
                if notice is None:
                    continue
                remaining = [
                    ticket
                    for ticket in _ticket_list(notice.get("failed"))
                    if ticket not in revived_set
                ]
                if remaining == _ticket_list(notice.get("failed")):
                    continue
                if remaining:
                    updated = dict(notice)
                    updated["failed"] = remaining
                    updated["failed_reasons"] = {
                        ticket: reason
                        for ticket, reason in _reason_map(notice.get("failed_reasons")).items()
                        if ticket in remaining
                    }
                    set_notice(key, updated)
                else:
                    clear(key)

        def merge_revive_failures(key: str) -> None:
            failed = _ticket_list(event.get("failed"))
            if not failed:
                return
            existing = self._notices.get(key)
            prior_failed = _ticket_list(existing.get("failed")) if existing else []
            prior_reasons = _reason_map(existing.get("failed_reasons")) if existing else {}
            merged_failed = sorted(set(prior_failed) | set(failed))
            merged_reasons = {**prior_reasons, **_reason_map(event.get("failed_reasons"))}
            payload = dict(event)
            payload["failed"] = merged_failed
            payload["failed_reasons"] = {
                ticket: reason
                for ticket, reason in merged_reasons.items()
                if ticket in set(merged_failed)
            }
            set_notice(key, payload)

        if kind == "codex_limit_no_eligible":
            set_notice("codex:limit", dict(event))
        elif kind == "codex_rotation_failed":
            set_notice("codex:rotation-failed", dict(event))
        elif kind == "codex_auth_dead_exhausted":
            merge_exhausted(_ticket_list(event.get("tickets")))
        elif kind in ("codex_rotation", "codex_auth_dead_revival"):
            if kind == "codex_rotation":
                # A completed rotation moved onto an eligible account: the
                # limit deadlock and any prior rotation failure are resolved.
                clear("codex:limit", "codex:rotation-failed")
            remove_revived(_ticket_list(event.get("revived")))
            merge_revive_failures(_REVIVE_FAILED_KEYS[kind])
        elif kind == "codex_auth_verified":
            if event.get("success") is True and event.get("credential_source") == "current":
                clear(_EXHAUSTED_KEY)
        elif kind == "claude_limit_hit":
            ticket = event.get("ticket")
            if isinstance(ticket, str) and ticket:
                set_notice(f"claude:limit:{ticket}", dict(event))
        elif kind == "claude_limit_cleared":
            # The only Claude recovery proof: the watchdog observed the limit
            # banner gone from a previously limited worker. Generic session
            # events (queue edits, model changes) must never resolve limits.
            ticket = event.get("ticket")
            if isinstance(ticket, str) and ticket:
                clear(f"claude:limit:{ticket}")

        return changed

    def snapshot(self) -> list[dict]:
        """Unresolved notices, newest first."""

        with self._lock:
            entries = list(self._notices.values())
        return sorted(
            (dict(entry) for entry in entries),
            key=lambda entry: str(entry.get("ts") or ""),
            reverse=True,
        )
