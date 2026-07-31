"""Durable store of unresolved provider-health notices.

Account events (usage limits, rotation failures, dead auth, failed worker
revivals) arrive over SSE only while the app is open. This store keeps the
unresolved ones on disk so the action guidance survives an app reload, and
clears a notice only when a matching recovery event proves resolution.
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


def _failed_count(event: dict) -> int:
    failed = event.get("failed")
    return len(failed) if isinstance(failed, list) else 0


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

        def set_notice(key: str) -> None:
            nonlocal changed
            self._notices[key] = dict(event)
            changed = True

        def clear(*keys: str) -> None:
            nonlocal changed
            for key in keys:
                if self._notices.pop(key, None) is not None:
                    changed = True

        if kind == "codex_limit_no_eligible":
            set_notice("codex:limit")
        elif kind == "codex_rotation_failed":
            set_notice("codex:rotation-failed")
        elif kind == "codex_auth_dead_exhausted":
            set_notice("codex:auth-exhausted")
        elif kind == "codex_rotation":
            # A completed rotation moved onto an eligible account: the limit
            # deadlock and any prior rotation failure are resolved.
            clear("codex:limit", "codex:rotation-failed")
            if _failed_count(event) > 0:
                set_notice("codex:revive-failed")
            else:
                clear("codex:revive-failed")
        elif kind == "codex_auth_dead_revival":
            if _failed_count(event) > 0:
                set_notice("codex:revive-failed")
            else:
                clear("codex:auth-exhausted", "codex:revive-failed")
        elif kind == "codex_auth_verified":
            if event.get("success") is True and event.get("credential_source") == "current":
                clear("codex:auth-exhausted")
        elif kind == "claude_limit_hit":
            ticket = event.get("ticket")
            if isinstance(ticket, str) and ticket:
                set_notice(f"claude:limit:{ticket}")
        elif kind == "session":
            # New session activity from a limit-hit worker proves it resumed.
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
