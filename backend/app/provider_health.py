"""Provider auth health probe + tracker.

Watches whether the local codex / claude CLIs still have working credentials.
Two signals:

- **Periodic probe** — reads credential files (non-destructive) to spot
  obvious "not logged in" states before a spawn fails.
- **Event-driven mark** — auth-dead events from the supervisor (or any caller)
  immediately flip a provider to `unauthorized`. This mark is sticky: it stays
  until the credential file's mtime advances (i.e. the user ran `codex login`
  or `claude login`), so a passing probe cannot silently clear a real failure.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from . import accounts


Status = Literal["ok", "unauthorized", "unknown"]

DEFAULT_PROBE_INTERVAL_SECONDS = 600.0  # 10 minutes
_AUTH_KEYWORDS = re.compile(r"unauthori[sz]ed|token|refresh|\blogin\b", re.IGNORECASE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def claude_credentials_path() -> Path:
    """Where Claude Code stores its credential file on Linux/non-keychain hosts."""
    override = os.environ.get("WIKI_CLAUDE_CREDENTIALS_PATH")
    if override:
        return Path(override).expanduser()
    home = Path(os.environ.get("WIKI_ACCOUNT_HOME_OVERRIDE") or Path.home())
    return home / ".claude" / ".credentials.json"


def claude_home_path() -> Path:
    override = os.environ.get("WIKI_CLAUDE_HOME_PATH")
    if override:
        return Path(override).expanduser()
    home = Path(os.environ.get("WIKI_ACCOUNT_HOME_OVERRIDE") or Path.home())
    return home / ".claude"


def _file_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


@dataclass(frozen=True)
class ProviderHealth:
    status: Status = "unknown"
    checked_at: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "checked_at": self.checked_at,
            "detail": self.detail,
        }


def probe_codex(*, now: str | None = None) -> ProviderHealth:
    """File-only probe. Existence + non-empty refresh token = ok; missing = unknown."""
    path = accounts.codex_auth_path()
    checked_at = now or _now_iso()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ProviderHealth(
            status="unknown",
            checked_at=checked_at,
            detail=f"{path} not found; run `codex login`",
        )
    except OSError as exc:
        return ProviderHealth(
            status="unknown",
            checked_at=checked_at,
            detail=f"cannot read {path}: {exc}",
        )
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return ProviderHealth(
            status="unauthorized",
            checked_at=checked_at,
            detail=f"auth.json is not valid JSON: {exc}",
        )
    tokens = data.get("tokens") if isinstance(data, dict) else None
    refresh = tokens.get("refresh_token") if isinstance(tokens, dict) else None
    if not isinstance(refresh, str) or not refresh.strip():
        return ProviderHealth(
            status="unauthorized",
            checked_at=checked_at,
            detail="auth.json missing refresh_token; run `codex login`",
        )
    return ProviderHealth(status="ok", checked_at=checked_at, detail=None)


def probe_claude(*, now: str | None = None) -> ProviderHealth:
    """File-only probe. macOS Keychain users show as unknown (no file to read)."""
    checked_at = now or _now_iso()
    creds = claude_credentials_path()
    try:
        raw = creds.read_text(encoding="utf-8")
    except FileNotFoundError:
        # macOS stores creds in Keychain — no cheap file read available.
        # Fall through to unknown so the badge stays subtle.
        if claude_home_path().is_dir():
            return ProviderHealth(
                status="unknown",
                checked_at=checked_at,
                detail=f"{creds} not found (macOS Keychain?)",
            )
        return ProviderHealth(
            status="unknown",
            checked_at=checked_at,
            detail=f"{claude_home_path()} not found; run `claude login`",
        )
    except OSError as exc:
        return ProviderHealth(
            status="unknown",
            checked_at=checked_at,
            detail=f"cannot read {creds}: {exc}",
        )
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return ProviderHealth(
            status="unauthorized",
            checked_at=checked_at,
            detail=f"credentials file is not valid JSON: {exc}",
        )
    if not isinstance(data, dict) or not data:
        return ProviderHealth(
            status="unauthorized",
            checked_at=checked_at,
            detail="credentials file is empty; run `claude login`",
        )
    return ProviderHealth(status="ok", checked_at=checked_at, detail=None)


def state_reason_indicates_auth(reason: str | None) -> bool:
    """True when a blocked-state reason mentions auth/token/login keywords."""
    return bool(reason and _AUTH_KEYWORDS.search(reason))


ProbeFn = Callable[[], ProviderHealth]


class ProviderHealthTracker:
    """Per-provider in-memory health with sticky event-driven downgrades."""

    KINDS: tuple[str, ...] = ("cdx", "cc")

    def __init__(
        self,
        *,
        probe_codex_fn: ProbeFn = probe_codex,
        probe_claude_fn: ProbeFn = probe_claude,
        credential_path_fn: Callable[[str], Path] | None = None,
    ) -> None:
        self._probe_fns: dict[str, ProbeFn] = {
            "cdx": probe_codex_fn,
            "cc": probe_claude_fn,
        }
        self._credential_path_fn = credential_path_fn or self._default_credential_path
        self._state: dict[str, ProviderHealth] = {kind: ProviderHealth() for kind in self.KINDS}
        # sticky_mtime[kind] = credential mtime at moment of unauthorized mark;
        # None means no sticky mark active.
        self._sticky_mtime: dict[str, float | None] = {kind: None for kind in self.KINDS}

    @staticmethod
    def _default_credential_path(kind: str) -> Path:
        if kind == "cdx":
            return accounts.codex_auth_path()
        if kind == "cc":
            return claude_credentials_path()
        raise ValueError(f"unknown provider kind: {kind}")

    def snapshot(self) -> dict[str, dict[str, object]]:
        return {kind: entry.to_dict() for kind, entry in self._state.items()}

    def get(self, kind: str) -> ProviderHealth:
        return self._state[kind]

    def refresh(self, *, kinds: Iterable[str] | None = None) -> dict[str, dict[str, object]]:
        """Re-probe each kind. A sticky unauthorized mark blocks the fresh
        probe from clearing state until the credential file mtime advances."""

        for kind in kinds or self.KINDS:
            if kind not in self._state:
                continue
            sticky = self._sticky_mtime.get(kind)
            if sticky is not None:
                current_mtime = _file_mtime(self._credential_path_fn(kind))
                if current_mtime is not None and current_mtime > sticky:
                    # User re-authenticated — clear sticky and re-probe fresh.
                    self._sticky_mtime[kind] = None
                else:
                    # Preserve the sticky unauthorized state; bump checked_at.
                    prior = self._state[kind]
                    self._state[kind] = replace(prior, checked_at=_now_iso())
                    continue
            self._state[kind] = self._probe_fns[kind]()
        return self.snapshot()

    def mark_unauthorized(self, kind: str, *, detail: str | None = None) -> None:
        """Event-driven downgrade. Sticks until credential file mtime advances."""

        if kind not in self._state:
            return
        mtime = _file_mtime(self._credential_path_fn(kind))
        self._sticky_mtime[kind] = mtime if mtime is not None else time.time()
        self._state[kind] = ProviderHealth(
            status="unauthorized",
            checked_at=_now_iso(),
            detail=detail or "auth-dead event",
        )

    def mark_reason(self, kind: str, reason: str | None) -> bool:
        """If reason mentions auth keywords, mark unauthorized. Returns True on mark."""

        if not state_reason_indicates_auth(reason):
            return False
        self.mark_unauthorized(kind, detail=(reason or "").strip() or "auth-related blocker")
        return True

    def spawn_hint(self, kind: str) -> str | None:
        """Human-readable warning to attach to spawn responses when unhealthy."""

        entry = self._state.get(kind)
        if entry is None or entry.status != "unauthorized":
            return None
        cli = "codex login" if kind == "cdx" else "claude login"
        detail = f" ({entry.detail})" if entry.detail else ""
        return (
            f"provider auth marked unhealthy for {kind}{detail}. "
            f"if the spawn fails, re-run `{cli}` and retry."
        )


async def probe_loop(
    tracker: ProviderHealthTracker,
    *,
    interval_seconds: float = DEFAULT_PROBE_INTERVAL_SECONDS,
    stop: asyncio.Event | None = None,
) -> None:
    """Background task: refresh probes every `interval_seconds`. Cancel-safe."""

    while True:
        try:
            tracker.refresh()
        except Exception:
            # Probes must not tear down the loop; try again next tick.
            pass
        if stop is not None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
                if stop.is_set():
                    return
            except TimeoutError:
                continue
        else:
            await asyncio.sleep(interval_seconds)
