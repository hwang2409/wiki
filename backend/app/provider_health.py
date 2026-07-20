"""Bounded provider-auth probes and concurrency-safe health tracking."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from . import accounts


Status = Literal["ok", "unauthorized", "unknown"]
ReasonCode = Literal[
    "credentials_missing",
    "credentials_invalid",
    "verification_failed",
    "verification_unavailable",
    "auth_dead",
]

DEFAULT_PROBE_INTERVAL_SECONDS = 600.0
PROBE_TIMEOUT_SECONDS = 5.0
_AUTH_KEYWORDS = re.compile(r"unauthori[sz]ed|token|refresh|\blogin\b", re.IGNORECASE)
_REMEDIATION: dict[str, str] = {
    "cdx": "run codex login, then retry",
    "cc": "run claude login, then retry",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def claude_credentials_path() -> Path:
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


def _credential_fingerprint(path: Path) -> str | None:
    """Hash credential bytes without ever returning or logging the contents."""

    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _run_status_command(
    args: list[str], *, parse_json: bool = False, env: dict[str, str] | None = None
) -> bool | None:
    """Return auth success/failure, or None when verification was unavailable."""

    try:
        result = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if parse_json else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
            env=env,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not parse_json:
        return result.returncode == 0
    if result.returncode != 0 or not isinstance(result.stdout, str):
        return False
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return None
    return payload.get("loggedIn") is True if isinstance(payload, dict) else None


def _verify_operationally(kind: str) -> bool | None:
    """Run local status only as a bounded diagnostic; it never proves auth.

    The CLIs may refresh tokens or write config while inspecting status. Keep
    that behavior inside an isolated temporary home, and deliberately discard
    the result: local status cannot establish that the current credential can
    perform a remote authenticated request.
    """

    if kind not in {"cdx", "cc"}:
        return None
    with TemporaryDirectory(prefix="wiki-provider-probe-") as isolated:
        isolated_home = Path(isolated)
        (isolated_home / "tmp").mkdir()
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(isolated_home),
                "TMPDIR": str(isolated_home / "tmp"),
                "XDG_CONFIG_HOME": str(isolated_home / "xdg-config"),
                "XDG_CACHE_HOME": str(isolated_home / "xdg-cache"),
                "CODEX_HOME": str(isolated_home / ".codex"),
                "CLAUDE_CONFIG_DIR": str(isolated_home / ".claude"),
            }
        )
        if kind == "cdx":
            _run_status_command(["codex", "login", "status"], env=env)
        else:
            _run_status_command(
                ["claude", "auth", "status", "--json"], parse_json=True, env=env
            )
    return None


@dataclass(frozen=True)
class ProviderHealth:
    status: Status = "unknown"
    checked_at: str | None = None
    reason_code: ReasonCode | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "checked_at": self.checked_at,
            "reason_code": self.reason_code,
        }


def _verified_health(kind: str, *, checked_at: str) -> ProviderHealth:
    _verify_operationally(kind)
    return ProviderHealth(
        status="unknown",
        checked_at=checked_at,
        reason_code="verification_unavailable",
    )


def probe_codex(*, now: str | None = None) -> ProviderHealth:
    """Check auth-file shape, then require ``codex login status`` for ``ok``."""

    checked_at = now or _now_iso()
    path = accounts.codex_auth_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ProviderHealth("unknown", checked_at, "credentials_missing")
    except OSError:
        return ProviderHealth("unknown", checked_at, "verification_unavailable")
    try:
        data = json.loads(raw)
    except ValueError:
        return ProviderHealth("unauthorized", checked_at, "credentials_invalid")
    tokens = data.get("tokens") if isinstance(data, dict) else None
    refresh = tokens.get("refresh_token") if isinstance(tokens, dict) else None
    if not isinstance(refresh, str) or not refresh.strip():
        return ProviderHealth("unauthorized", checked_at, "credentials_invalid")
    return _verified_health("cdx", checked_at=checked_at)


def probe_claude(*, now: str | None = None) -> ProviderHealth:
    """Require ``claude auth status``; credential-file presence alone is unknown."""

    checked_at = now or _now_iso()
    creds = claude_credentials_path()
    try:
        raw = creds.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Claude may keep credentials in the macOS keychain, so the status CLI
        # is the only supported way to turn this into an operational result.
        return _verified_health("cc", checked_at=checked_at)
    except OSError:
        return ProviderHealth("unknown", checked_at, "verification_unavailable")
    try:
        data = json.loads(raw)
    except ValueError:
        return ProviderHealth("unauthorized", checked_at, "credentials_invalid")
    if not isinstance(data, dict) or not data:
        return ProviderHealth("unauthorized", checked_at, "credentials_invalid")
    return _verified_health("cc", checked_at=checked_at)


def state_reason_indicates_auth(reason: str | None) -> bool:
    """True when a blocked-state reason mentions auth/token/login keywords."""

    return bool(reason and _AUTH_KEYWORDS.search(reason))


ProbeFn = Callable[[], ProviderHealth]


class ProviderHealthTracker:
    """Per-provider health with sticky, generation-checked event downgrades."""

    KINDS: tuple[str, ...] = ("cdx", "cc")

    def __init__(
        self,
        *,
        probe_codex_fn: ProbeFn = probe_codex,
        probe_claude_fn: ProbeFn = probe_claude,
        credential_path_fn: Callable[[str], Path] | None = None,
    ) -> None:
        self._probe_fns: dict[str, ProbeFn] = {"cdx": probe_codex_fn, "cc": probe_claude_fn}
        self._credential_path_fn = credential_path_fn or self._default_credential_path
        self._state: dict[str, ProviderHealth] = {kind: ProviderHealth() for kind in self.KINDS}
        self._sticky: dict[str, bool] = {kind: False for kind in self.KINDS}
        self._sticky_fingerprint: dict[str, str | None] = {kind: None for kind in self.KINDS}
        self._generation: dict[str, int] = {kind: 0 for kind in self.KINDS}
        self._lock = threading.RLock()
        self._refresh_condition = threading.Condition(self._lock)
        self._refresh_inflight = False

    @staticmethod
    def _default_credential_path(kind: str) -> Path:
        if kind == "cdx":
            return accounts.codex_auth_path()
        if kind == "cc":
            return claude_credentials_path()
        raise ValueError(f"unknown provider kind: {kind}")

    def snapshot(self) -> dict[str, dict[str, object]]:
        with self._lock:
            return {kind: entry.to_dict() for kind, entry in self._state.items()}

    def get(self, kind: str) -> ProviderHealth:
        with self._lock:
            return self._state[kind]

    def refresh(self, *, kinds: Iterable[str] | None = None) -> dict[str, dict[str, object]]:
        """Run one probe batch; concurrent callers share the in-flight result."""

        with self._refresh_condition:
            if self._refresh_inflight:
                while self._refresh_inflight:
                    self._refresh_condition.wait()
                return self.snapshot()
            self._refresh_inflight = True
        try:
            for kind in kinds or self.KINDS:
                if kind not in self._state:
                    continue
                with self._lock:
                    generation = self._generation[kind]
                    sticky = self._sticky[kind]
                    sticky_fingerprint = self._sticky_fingerprint[kind]
                result = self._probe_fns[kind]()
                current_fingerprint = _credential_fingerprint(self._credential_path_fn(kind))
                with self._lock:
                    if self._generation[kind] != generation:
                        # A concurrent auth-dead event wins over this stale probe.
                        continue
                    if sticky and self._sticky[kind]:
                        explicit_success = result.status == "ok"
                        fingerprint_changed = current_fingerprint != sticky_fingerprint
                        # A changed credential fingerprint is only a recovery
                        # signal when paired with an explicit successful probe.
                        if not (explicit_success and (fingerprint_changed or result.reason_code is None)):
                            prior = self._state[kind]
                            self._state[kind] = replace(
                                prior,
                                checked_at=result.checked_at,
                            )
                            continue
                        self._sticky[kind] = False
                        self._sticky_fingerprint[kind] = None
                    self._state[kind] = result
            return self.snapshot()
        finally:
            with self._refresh_condition:
                self._refresh_inflight = False
                self._refresh_condition.notify_all()

    async def refresh_async(
        self, *, kinds: Iterable[str] | None = None
    ) -> dict[str, dict[str, object]]:
        """Run the blocking probe batch away from the asyncio event loop."""

        return await asyncio.to_thread(self.refresh, kinds=kinds)

    def mark_unauthorized(self, kind: str) -> None:
        """Mark an exhausted current-credential auth failure with a fixed code."""

        if kind not in self._state:
            return
        with self._lock:
            self._generation[kind] += 1
            self._sticky[kind] = True
            self._sticky_fingerprint[kind] = _credential_fingerprint(
                self._credential_path_fn(kind)
            )
            # checked_at is probe-owned; an event must not pretend a probe ran.
            self._state[kind] = replace(
                self._state[kind],
                status="unauthorized",
                reason_code="auth_dead",
            )

    def mark_reason(self, kind: str, reason: str | None) -> bool:
        """Compatibility helper for callers that only have a coarse auth reason."""

        if not state_reason_indicates_auth(reason):
            return False
        self.mark_unauthorized(kind)
        return True

    def spawn_hint(self, kind: str) -> str | None:
        """Return fixed remediation only; never include probe data."""

        with self._lock:
            entry = self._state.get(kind)
            if entry is None or entry.status != "unauthorized":
                return None
        label = "codex" if kind == "cdx" else "claude"
        return f"{label} auth is unavailable; {_REMEDIATION.get(kind, 'sign in again and retry')}"


async def probe_loop(
    tracker: ProviderHealthTracker,
    *,
    interval_seconds: float = DEFAULT_PROBE_INTERVAL_SECONDS,
    stop: asyncio.Event | None = None,
) -> None:
    """Background task: refresh probes every interval; cancel-safe."""

    while True:
        try:
            await tracker.refresh_async()
        except Exception:
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
