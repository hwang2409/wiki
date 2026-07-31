"""Codex account rotation + fleet-revival watchdog.

Every ~poll_seconds, scan registry cdx workers' panes for the "You've hit your
usage limit" signature. When ≥1 matches, rotate to the next eligible account
(round-robin over ~/.codex-accounts) and revive the killed workers via
`codex resume`. Claude workers get a limit alert but no auto-rotation.

Every filesystem path is env-overridable so tests never touch real credentials.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Iterator


# ---------------------------------------------------------------------------
# Env-overridable paths.
# ---------------------------------------------------------------------------

def _home() -> Path:
    return Path(os.environ.get("WIKI_ACCOUNT_HOME_OVERRIDE") or Path.home())


def codex_auth_path() -> Path:
    override = os.environ.get("WIKI_CODEX_AUTH_PATH")
    if override:
        return Path(override).expanduser()
    return _home() / ".codex" / "auth.json"


def codex_accounts_dir() -> Path:
    override = os.environ.get("WIKI_CODEX_ACCOUNTS_DIR")
    if override:
        return Path(override).expanduser()
    return _home() / ".codex-accounts"


def codex_sessions_dir() -> Path:
    override = os.environ.get("WIKI_CODEX_SESSIONS_DIR")
    if override:
        return Path(override).expanduser()
    return _home() / ".codex" / "sessions"


def rotation_log_path() -> Path:
    override = os.environ.get("WIKI_ROTATION_LOG_PATH")
    if override:
        return Path(override).expanduser()
    return codex_accounts_dir() / "rotation.log"


def registry_path() -> Path:
    override = os.environ.get("WIKI_AGENT_REGISTRY_PATH")
    if override:
        return Path(override).expanduser()
    return Path("/tmp/agent-registry.json")


def wiki_cli_path() -> Path:
    override = os.environ.get("WIKI_CLI_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / "me" / "fun" / "wiki" / "wiki"


def watchdog_enabled() -> bool:
    return (os.environ.get("WIKI_ACCOUNT_WATCHDOG") or "on").lower() != "off"


def poll_seconds() -> float:
    return float(os.environ.get("WIKI_ROTATION_POLL_SECONDS") or 60)


def debounce_seconds() -> float:
    return float(os.environ.get("WIKI_ROTATION_DEBOUNCE_SECONDS") or 600)


def revival_wait_seconds() -> float:
    return float(os.environ.get("WIKI_REVIVAL_WAIT_SECONDS") or 30)


# ---------------------------------------------------------------------------
# Detection.
# ---------------------------------------------------------------------------

# The real string looks like:
#   ■ You've hit your usage limit. Visit https://chatgpt.com/... or try again at Jul 9th, 2026 8:36 PM.
# Curly apostrophe or ASCII, "You've/You have", varies. Lowercase everything and match.
LIMIT_HIT_PATTERN = re.compile(
    r"you(?:'|’|\s+ha)ve\s+hit\s+your\s+usage\s+limit",
    re.IGNORECASE,
)
# codex also shows a benign "usage limit resets available. Run /usage" — must NOT trigger.
BENIGN_USAGE_PATTERN = re.compile(
    r"usage\s+limit\s+resets\s+available",
    re.IGNORECASE,
)
# Claude Code prints this when its per-plan limit is hit.
CLAUDE_LIMIT_PATTERN = re.compile(
    r"claude\s+usage\s+limit\s+reached|approaching\s+usage\s+limit",
    re.IGNORECASE,
)
# Codex prints this when its cached refresh token is stale (workstation slept,
# auth swapped externally, another install signed in). The pane is dead —
# process keeps running but every generation fails with a token-refresh error.
# Match the full "your access token could not be refreshed" phrase only —
# a bare "please sign in again" tail is too broad (product copy, docs, tests,
# and any pane displaying sign-in-related text would false-positive and
# trigger a kill+resume every poll cycle).
AUTH_DEAD_PATTERN = re.compile(
    r"your\s+access\s+token\s+could\s+not\s+be\s+refreshed",
    re.IGNORECASE,
)
# Codex's cwd-mismatch chooser when a rollout is resumed from a directory
# different from the one it was recorded in.
CWD_DIALOG_PATTERN = re.compile(
    r"use\s+current\s+directory",
    re.IGNORECASE,
)
# Codex renders completed TUI errors with a solid-square prefix. Requiring it
# avoids treating source code, quoted fixtures, or transcript prose as a hit.
CODEX_LIMIT_BANNER_PATTERN = re.compile(r"^\s*■\s+")
KICKOFF_TICKET_PATTERN = re.compile(r"(?:Linear )?ticket ([A-Z]+-\d+)\b")
# "try again at Jul 9th, 2026 8:36 PM" — optional ordinal, optional comma.
RESET_TIME_PATTERN = re.compile(
    r"try\s+again\s+at\s+"
    r"(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+"
    r"(?P<year>\d{4})\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*"
    r"(?P<meridiem>AM|PM)",
    re.IGNORECASE,
)
BARE_RESET_TIME_PATTERN = re.compile(
    r"try\s+again\s+at\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*"
    r"(?P<meridiem>AM|PM)\b",
    re.IGNORECASE,
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
TERMINAL_OUTCOMES = {"merged", "closed", "plan-ready", "abandoned"}
UNKNOWN_RESET_HOURS = 24


def detect_codex_limit(pane: str) -> bool:
    """True when the pane shows the usage-limit signature (excludes benign form)."""
    if not pane:
        return False
    for line in pane.splitlines():
        if not CODEX_LIMIT_BANNER_PATTERN.match(line):
            continue
        if BENIGN_USAGE_PATTERN.search(line) and not LIMIT_HIT_PATTERN.search(line):
            continue
        if LIMIT_HIT_PATTERN.search(line):
            return True
    return False


def detect_claude_limit(pane: str) -> bool:
    return bool(pane and CLAUDE_LIMIT_PATTERN.search(pane))


def detect_codex_auth_dead(pane: str) -> bool:
    """True when codex's stale-refresh-token error is on-screen. Auth-dead is
    NOT a limit hit — the process just needs to be revived onto the current
    (already-fresh) auth.json; no account swap."""
    return bool(pane and AUTH_DEAD_PATTERN.search(pane))


def detect_cwd_dialog(pane: str) -> bool:
    return bool(pane and CWD_DIALOG_PATTERN.search(pane))


def _iter_payload_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for nested in value.values():
            yield from _iter_payload_strings(nested)
        return
    if isinstance(value, list | tuple | set):
        for nested in value:
            yield from _iter_payload_strings(nested)


def _payload_matches(pattern: re.Pattern[str], payload: object) -> bool:
    return any(pattern.search(text) for text in _iter_payload_strings(payload))


def detect_claude_limit_payload(payload: object) -> bool:
    """Event-level Claude limit detector that ignores echoed user content."""

    if not isinstance(payload, dict):
        return False
    event_type = payload.get("type")
    if event_type not in {
        "result",
        "system",
        "provider_stderr",
        "provider_protocol_error",
    }:
        return False
    return _payload_matches(CLAUDE_LIMIT_PATTERN, payload)


def detect_codex_auth_dead_payload(payload: object) -> bool:
    """Event-level Codex auth-dead detector that ignores echoed prompts."""

    if not isinstance(payload, dict):
        return False
    method = payload.get("method")
    if method not in {
        "error",
        "turn/completed",
        "provider/stderr",
        "provider/protocolError",
    }:
        return False
    return _payload_matches(AUTH_DEAD_PATTERN, payload)


def codex_rate_limit_reached_type(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("method") != "account/rateLimits/updated":
        return None
    params = payload.get("params")
    if not isinstance(params, dict):
        return None
    rate_limits = params.get("rateLimits")
    if not isinstance(rate_limits, dict):
        return None
    value = rate_limits.get("rateLimitReachedType")
    return value if isinstance(value, str) and value else None


def rate_limit_reset_from_snapshot(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    params = payload.get("params")
    if not isinstance(params, dict):
        return None
    rate_limits = params.get("rateLimits")
    if not isinstance(rate_limits, dict):
        return None

    resets_at: list[float] = []
    for value in rate_limits.values():
        if not isinstance(value, dict):
            continue
        raw = value.get("resetsAt")
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            continue
        if raw <= 0:
            continue
        resets_at.append(float(raw))
    if not resets_at:
        return None
    return datetime.fromtimestamp(min(resets_at), tz=timezone.utc).isoformat()


def _local_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now().astimezone()
    if now.tzinfo is None:
        return now.astimezone()
    return now


def _local_datetime(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
    *,
    now: datetime | None = None,
) -> datetime:
    dt = datetime(year, month, day, hour, minute)
    if now is None:
        return dt.astimezone()
    local_now = _local_now(now)
    return dt.replace(tzinfo=local_now.tzinfo)


def _meridiem_hour(hour_text: str, meridiem: str) -> int:
    hour = int(hour_text) % 12
    if meridiem.upper() == "PM":
        hour += 12
    return hour


def parse_reset_time(pane: str, *, now: datetime | None = None) -> str | None:
    """Return a tz-aware ISO-8601 timestamp for the reset window, or None.

    Codex prints reset times in the operator's local timezone. Full date
    strings carry the date; bare times are interpreted as today unless that
    local instant has already passed, in which case they roll to tomorrow.
    """
    if not pane:
        return None
    local_now = _local_now(now)
    tz = local_now.tzinfo
    match = RESET_TIME_PATTERN.search(pane)
    if match:
        try:
            month = _MONTHS[match.group("month").lower()[:3]]
            day = int(match.group("day"))
            year = int(match.group("year"))
            hour = _meridiem_hour(match.group("hour"), match.group("meridiem"))
            minute = int(match.group("minute"))
            return _local_datetime(year, month, day, hour, minute, now=now).isoformat()
        except (KeyError, ValueError):
            return None

    match = BARE_RESET_TIME_PATTERN.search(pane)
    if not match:
        return None
    try:
        hour = _meridiem_hour(match.group("hour"), match.group("meridiem"))
        minute = int(match.group("minute"))
        dt = datetime(
            local_now.year,
            local_now.month,
            local_now.day,
            hour,
            minute,
            tzinfo=tz,
        )
    except ValueError:
        return None
    if dt < local_now:
        dt += timedelta(days=1)
    return dt.isoformat()


def _fallback_reset_time(now: datetime | None = None) -> str:
    """Conservative reset pin when the pane omitted an unparseable reset time."""
    return (_local_now(now) + timedelta(hours=UNKNOWN_RESET_HOURS)).isoformat()


def fallback_reset_time(now: datetime | None = None) -> str:
    return _fallback_reset_time(now)


# ---------------------------------------------------------------------------
# State file.
# ---------------------------------------------------------------------------

@dataclass
class AccountState:
    active: str | None = None
    last_rotated_at: str | None = None
    accounts: dict[str, dict[str, object]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "active": self.active,
            "last_rotated_at": self.last_rotated_at,
            "accounts": self.accounts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object] | None) -> "AccountState":
        data = data or {}
        active_value = data.get("active")
        active = active_value if isinstance(active_value, str) else None
        last_rotated_value = data.get("last_rotated_at")
        last_rotated_at = (
            last_rotated_value if isinstance(last_rotated_value, str) else None
        )
        raw_accounts = data.get("accounts")
        accounts: dict[str, dict[str, object]] = {}
        if isinstance(raw_accounts, dict):
            for key, value in raw_accounts.items():
                if not isinstance(key, str):
                    continue
                if not isinstance(value, dict):
                    accounts[key] = {}
                    continue
                normalized: dict[str, object] = {}
                for entry_key, entry_value in value.items():
                    if isinstance(entry_key, str):
                        normalized[entry_key] = entry_value
                accounts[key] = normalized
        return cls(
            active=active,
            last_rotated_at=last_rotated_at,
            accounts=accounts,
        )


def _state_path() -> Path:
    return codex_accounts_dir() / "state.json"


def read_state() -> AccountState:
    path = _state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    return AccountState.from_dict(data)


def write_state(state: AccountState) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def list_available_accounts() -> list[str]:
    """Every subdirectory of ~/.codex-accounts that carries an auth.json."""
    accounts_dir = codex_accounts_dir()
    if not accounts_dir.is_dir():
        return []
    names: list[str] = []
    for entry in sorted(accounts_dir.iterdir()):
        if entry.is_dir() and (entry / "auth.json").is_file():
            names.append(entry.name)
    return names


def ensure_state_initialized(state: AccountState) -> AccountState:
    """Add rows for every account on disk; adopt the first as active if none set."""
    names = list_available_accounts()
    changed = False
    for name in names:
        if name not in state.accounts:
            state.accounts[name] = {"limit_reset_at": None}
            changed = True
    if state.active is None and names:
        state.active = names[0]
        changed = True
    if changed:
        write_state(state)
    return state


def _iso_in_past_or_null(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return True
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return True
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    return dt <= now


def pick_next_account(state: AccountState) -> str | None:
    """Round-robin among accounts whose reset time is null or past."""
    names = list_available_accounts()
    if not names:
        return None
    eligible = [
        name for name in names
        if _iso_in_past_or_null((state.accounts.get(name) or {}).get("limit_reset_at"))
    ]
    if not eligible:
        return None
    if state.active is None or state.active not in names:
        return eligible[0]
    # Start scanning at the position after `active`, wrap.
    start = names.index(state.active)
    ordered = names[start + 1:] + names[: start + 1]
    for candidate in ordered:
        if candidate in eligible and candidate != state.active:
            return candidate
    # Only one eligible and it's the active — no swap makes sense.
    if len(eligible) == 1 and eligible[0] == state.active:
        return None
    return eligible[0]


# ---------------------------------------------------------------------------
# Codex rollout resolution (richest-lineage discovery for revival).
# ---------------------------------------------------------------------------

def _rollout_meta(path: Path) -> dict | None:
    """Parse the session_meta header (first row) of a rollout, defensively."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            first = fh.readline()
    except OSError:
        return None
    if not first:
        return None
    try:
        record = json.loads(first)
    except ValueError:
        return None
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else record
    return payload if isinstance(payload, dict) else None


def _rollout_kickoff_ticket(path: Path) -> str | None:
    """Ticket named in the first user_message ("... worker for Linear ticket X-N")."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for _ in range(200):
                line = fh.readline()
                if not line:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                payload = row.get("payload") or {}
                if row.get("type") == "event_msg" and payload.get("type") == "user_message":
                    match = KICKOFF_TICKET_PATTERN.search(payload.get("message") or "")
                    return match.group(1) if match else None
    except OSError:
        return None
    return None


def _candidate_rollouts(spawned_at: str | None) -> list[Path]:
    """Scan spawn day ± neighbors and today's day directory for rollouts."""
    root = codex_sessions_dir()
    if not root.is_dir():
        return []
    try:
        spawn = datetime.fromisoformat(spawned_at) if spawned_at else None
    except ValueError:
        spawn = None
    base = spawn or datetime.now(tz=timezone.utc)
    day_keys: list[str] = []
    seen: set[str] = set()
    for delta in (0, 1, -1):
        d = base + timedelta(days=delta)
        key = f"{d.year:04d}/{d.month:02d}/{d.day:02d}"
        if key not in seen:
            seen.add(key)
            day_keys.append(key)
    now = datetime.now(tz=timezone.utc)
    today_key = f"{now.year:04d}/{now.month:02d}/{now.day:02d}"
    if today_key not in seen:
        seen.add(today_key)
        day_keys.append(today_key)
    candidates: list[Path] = []
    for key in day_keys:
        day_dir = root / key
        if day_dir.is_dir():
            candidates.extend(day_dir.glob("rollout-*.jsonl"))
    return candidates


def find_session_id_for_worker(
    ticket: str,
    worktree: str,
    spawned_at: str | None,
) -> str | None:
    """Richest-lineage rollout id for this worker, or None.

    Matches by kickoff-ticket regex in first user_message OR by
    session_meta.cwd basename == worktree slug (works for post-resume rollouts
    whose kickoff was replayed as a response_item and is invisible to the
    kickoff scan). Rank: newest mtime, then largest size.
    """
    if not worktree or not ticket:
        return None
    slug = None
    try:
        slug = Path(worktree).name.lower() or None
    except OSError:
        slug = None
    best: tuple[float, int, str] | None = None
    for path in _candidate_rollouts(spawned_at):
        meta = _rollout_meta(path)
        if not meta:
            continue
        session_id = meta.get("id") if isinstance(meta.get("id"), str) else None
        if not session_id:
            continue
        cwd = meta.get("cwd") if isinstance(meta.get("cwd"), str) else None
        cwd_slug = Path(cwd).name.lower() if cwd else None
        cwd_match = bool(cwd_slug and slug and cwd_slug == slug)
        kickoff_match = _rollout_kickoff_ticket(path) == ticket
        if not (cwd_match or kickoff_match):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        rank = (stat.st_mtime, stat.st_size, session_id)
        if best is None or rank > best:
            best = rank
    return best[2] if best else None


# ---------------------------------------------------------------------------
# Registry scanning + tmux plumbing (thin wrappers so tests can monkeypatch).
# ---------------------------------------------------------------------------

@dataclass
class WorkerEntry:
    ticket: str
    window: str
    worktree: str
    log: str
    kind: str
    role: str | None
    orch: str | None
    spawned_at: str | None = None
    session_id: str | None = None  # codex rollout session id (registry-tracked)


def read_registry() -> dict:
    try:
        data = json.loads(registry_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _safe_revival_cwd(
    current: dict,
    *,
    require_existing: bool = False,
) -> tuple[str | None, str | None]:
    raw = current.get("worktree")
    source = "worktree"
    if not isinstance(raw, str) or not raw.strip():
        raw = current.get("cwd")
        source = "cwd"
    if not isinstance(raw, str) or not raw.strip():
        return None, "registry entry has no worktree/cwd for revival"

    expanded = os.path.expandvars(os.path.expanduser(raw.strip()))
    if not os.path.isabs(expanded):
        return None, f"registry {source} is not absolute: {raw!r}"
    expanded = os.path.abspath(expanded)
    home = os.path.abspath(os.path.expanduser("~"))
    if expanded == home:
        return None, "registry revival cwd resolves to $HOME; refusing zombie spawn"
    if require_existing and not os.path.isdir(expanded):
        return None, f"registry revival cwd does not exist: {expanded}"
    return expanded, None


def _entry_has_terminal_outcome(entry: dict) -> bool:
    current = entry.get("current")
    if isinstance(current, dict) and current.get("outcome") in TERMINAL_OUTCOMES:
        return True
    history = entry.get("history")
    if not isinstance(history, list):
        return False
    return any(
        isinstance(row, dict) and row.get("outcome") in TERMINAL_OUTCOMES
        for row in history
    )


def _worker_from_registry_entry(
    ticket: str,
    entry: object,
    kind: str,
    *,
    require_existing_cwd: bool = False,
) -> tuple[WorkerEntry | None, str | None]:
    if not isinstance(entry, dict):
        return None, "ticket is no longer in the registry"
    if _entry_has_terminal_outcome(entry):
        return None, "ticket has terminal registry outcome; skipping revival"
    current_obj = entry.get("current")
    if not isinstance(current_obj, dict):
        return None, "ticket has no current registry worker"
    current: dict[str, object] = {}
    for key, value in current_obj.items():
        if isinstance(key, str):
            current[key] = value
    if current.get("kind") != kind:
        return None, "registry worker kind changed; skipping stale revival"
    window = current.get("window")
    log = current.get("log")
    if not isinstance(window, str) or not window:
        return None, "registry entry has no window for revival"
    if not isinstance(log, str) or not log:
        return None, "registry entry has no log path for revival"
    cwd, reason = _safe_revival_cwd(current, require_existing=require_existing_cwd)
    if not cwd:
        return None, reason
    role_value = current.get("role")
    orch_value = current.get("orch")
    spawned_at_value = current.get("spawned_at")
    session_id_value = current.get("session_id")
    return WorkerEntry(
        ticket=ticket,
        window=window,
        worktree=cwd,
        log=log,
        kind=kind,
        role=role_value if isinstance(role_value, str) else None,
        orch=orch_value if isinstance(orch_value, str) else None,
        spawned_at=spawned_at_value if isinstance(spawned_at_value, str) else None,
        session_id=session_id_value if isinstance(session_id_value, str) else None,
    ), None


def iter_workers(kind: str) -> list[WorkerEntry]:
    workers: list[WorkerEntry] = []
    registry = read_registry()
    for ticket, entry in registry.items():
        if ticket.startswith("_") or not isinstance(entry, dict):
            continue
        worker, _ = _worker_from_registry_entry(ticket, entry, kind)
        if worker:
            workers.append(worker)
    return workers


def current_worker_for_revival(
    snapshot: WorkerEntry,
) -> tuple[WorkerEntry | None, str | None]:
    """Re-read registry immediately before reviving a killed worker.

    If the ticket was deregistered, archived with a terminal outcome, or
    re-registered/updated to a different window while rotation was in flight,
    skip instead of spawning a zombie from the stale pre-kill snapshot.
    """
    registry = read_registry()
    worker, reason = _worker_from_registry_entry(
        snapshot.ticket,
        registry.get(snapshot.ticket),
        snapshot.kind,
        require_existing_cwd=True,
    )
    if not worker:
        return None, reason
    if worker.window != snapshot.window:
        return None, (
            f"registry window changed from {snapshot.window} to {worker.window}; "
            "skipping stale revival"
        )
    return worker, None


def tmux_capture(window: str, lines: int = 60) -> str:
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-J", "-S", f"-{lines}", "-t", window],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def tmux_live_windows() -> set[str]:
    try:
        result = subprocess.run(
            ["tmux", "list-windows", "-a", "-F", "#{window_id}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def tmux_window_session(window: str) -> str | None:
    """The tmux session name owning `window`, or None if the window is gone.

    Load-bearing at revival: workers must respawn into the SAME session their
    dying window lived in — otherwise a rotation from the wiki orchestrator
    session pulls every phoebe worker's revival window into the wiki session.
    """
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", window, "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    return name or None


def tmux_kill_window(window: str) -> None:
    subprocess.run(["tmux", "kill-window", "-t", window], timeout=5, check=False)


def tmux_new_window(
    name: str,
    cwd: str,
    command: str,
    target_session: str | None = None,
) -> str | None:
    """Spawn a detached tmux window. When `target_session` is provided, spawn
    into that session (uses `-t <session>:`); otherwise attaches to the current
    tmux session (whichever session tmux picks — historically a source of
    revival windows piling into the wrong session).
    """
    args = ["tmux", "new-window", "-dP", "-F", "#{window_id}", "-n", name, "-c", cwd]
    if target_session:
        args.extend(["-t", f"{target_session}:"])
    args.append(command)
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    window = result.stdout.strip()
    return window if re.fullmatch(r"@\d+", window) else None


def tmux_pipe_pane(window: str, log_path: str) -> None:
    subprocess.run(
        ["tmux", "pipe-pane", "-t", window, "-o", f"cat >> {shlex.quote(log_path)}"],
        timeout=5,
        check=False,
    )


def tmux_send_literal_and_enter(window: str, text: str) -> None:
    """Same paste-detect protocol as main.deliver_message."""
    subprocess.run(["tmux", "send-keys", "-t", window, "-l", text], timeout=5, check=False)
    time.sleep(0.5)
    subprocess.run(["tmux", "send-keys", "-t", window, "Enter"], timeout=5, check=False)
    time.sleep(2)
    pane = tmux_capture(window, lines=30)
    if text[:60] in pane.replace("\n", " "):
        subprocess.run(["tmux", "send-keys", "-t", window, "Enter"], timeout=5, check=False)


def wait_for_cwd_dialog_and_answer(
    window: str,
    timeout_seconds: float = 8.0,
) -> bool:
    """Watch for codex's "1. Use original / 2. Use current directory" chooser.

    Answer "2" (current dir = worktree — keeps the new rollout mappable by the
    cwd resolver). No-op if the dialog never shows (e.g. resume launched from
    the original cwd already).
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        pane = tmux_capture(window, lines=40)
        if detect_cwd_dialog(pane):
            subprocess.run(
                ["tmux", "send-keys", "-t", window, "2", "Enter"],
                timeout=5,
                check=False,
            )
            time.sleep(0.5)
            return True
        time.sleep(0.4)
    return False


def wait_for_codex_ready(window: str, timeout_seconds: float) -> bool:
    """Ready = composer prompt drawn AND no `esc to interrupt` spinner.

    `esc to interrupt` is the BUSY signature — the composer is not accepting
    input while it's showing. Wait for it to disappear before we send-keys.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        pane = tmux_capture(window, lines=40)
        if pane and "esc to " not in pane and ("▌" in pane or ">_" in pane or "> " in pane):
            return True
        time.sleep(0.5)
    return False


def wiki_agent_update(
    ticket: str,
    window: str,
    log: str,
    session_id: str | None = None,
) -> tuple[bool, str | None]:
    args = [
        str(wiki_cli_path()),
        "agent",
        "update",
        ticket,
        "--window",
        window,
        "--log",
        log,
    ]
    if session_id:
        args.extend(["--session", session_id])
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return False, detail or f"exit {result.returncode}"
    return True, None


def codex_login_status() -> bool:
    try:
        result = subprocess.run(
            ["codex", "login", "status"], capture_output=True, text=True, timeout=8
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Rotation.
# ---------------------------------------------------------------------------

def _next_log_path(existing: str) -> str:
    """Append `-rN.log` (or bump N) so the pipe-pane log doesn't overwrite the pre-swap tail."""
    path = Path(existing)
    stem = path.stem
    m = re.match(r"^(.*)-r(\d+)$", stem)
    if m:
        base = m.group(1)
        next_index = int(m.group(2)) + 1
    else:
        base = stem
        next_index = 1
    return str(path.with_name(f"{base}-r{next_index}.log"))


def snapshot_active_auth(account_name: str) -> None:
    """Copy the current auth.json back into the outgoing account's dir so refreshed
    tokens survive the swap. No-op if the source file is missing (fresh install)."""
    src = codex_auth_path()
    if not src.is_file():
        return
    dst_dir = codex_accounts_dir() / account_name
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / "auth.json"
    shutil.copy2(src, dst)
    try:
        os.chmod(dst, 0o600)
    except OSError:
        pass


def install_incoming_auth(account_name: str) -> bool:
    src = codex_accounts_dir() / account_name / "auth.json"
    if not src.is_file():
        return False
    dst = codex_auth_path()
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    try:
        os.chmod(dst, 0o600)
    except OSError:
        pass
    return True


def revival_message(ticket: str) -> str:
    """Re-poke sent to a revived worker. Names the ticket + status file so a
    fresh codex resume (which sometimes replays little context) reliably picks
    up its identity and current step."""
    return (
        f"You are worker for ticket {ticket}. Your session was interrupted "
        f"(usage-limit account rotation or stale auth). Re-read "
        f"/tmp/agent-status/{ticket}.json and resume from your current step."
    )


# Kept for tests / import compatibility. Watchdog itself uses revival_message().
REVIVAL_MESSAGE = (
    "continue — interrupted by usage-limit account rotation; "
    "re-read your status file and resume from your current step"
)


@dataclass
class RotationResult:
    outgoing: str | None
    incoming: str
    revived: list[str]  # ticket ids
    failed: list[str]  # ticket ids that couldn't be revived
    reset_at: str | None  # reset time recorded for outgoing account
    failed_reasons: dict[str, str] = field(default_factory=dict)
    # Ticket -> run_id for the failing run at the moment the rotation
    # decided the worker could not be revived. The notice store uses this
    # to reconcile against the live registry: a replaced ticket has a new
    # run_id and its notice drops on the next refresh. Empty for legacy
    # tmux workers that have no run_id.
    failed_run_ids: dict[str, str] = field(default_factory=dict)


@dataclass
class RevivalResult:
    revived: list[str]
    failed: list[str]
    failed_reasons: dict[str, str] = field(default_factory=dict)


def rotate(
    *,
    state: AccountState,
    force_target: str | None = None,
    outgoing_reset_at: str | None = None,
) -> RotationResult:
    """Kill every live cdx worker → swap auth → revive each into new tmux windows.

    Load-bearing order: kill EVERY worker before touching auth.json (a live codex
    process could rewrite the file mid-swap via its refresh loop). Codex 0.143+
    has an account_id guard that aborts the refresh instead of clobbering, but
    kill-first stays as the safety rail.
    """
    target = force_target or pick_next_account(state)
    if target is None:
        raise NoEligibleAccountError("no eligible account for rotation")
    if target == state.active:
        raise NoEligibleAccountError("target account is already active")

    outgoing = state.active
    workers = iter_workers("cdx")
    live = tmux_live_windows()
    to_revive = [w for w in workers if w.window in live]

    # Snapshot each worker's tmux session BEFORE killing — revival must land
    # in the same session it left. Captured up-front because the display-message
    # lookup fails on a killed window.
    session_by_window: dict[str, str | None] = {
        w.window: tmux_window_session(w.window) for w in to_revive
    }

    for worker in to_revive:
        tmux_kill_window(worker.window)

    if outgoing:
        snapshot_active_auth(outgoing)
        if outgoing_reset_at is not None:
            state.accounts.setdefault(outgoing, {})["limit_reset_at"] = outgoing_reset_at

    if not install_incoming_auth(target):
        raise RotationError(f"incoming auth.json missing for account {target!r}")

    # Never revive workers onto an unverified auth.json. If `codex login status`
    # fails, we've already killed the workers — restore the outgoing snapshot
    # so the next rotation attempt starts from a known-good state.
    if not codex_login_status():
        if outgoing:
            install_incoming_auth(outgoing)
        raise RotationError(f"codex login status failed after swap to {target!r}")

    state.active = target
    state.last_rotated_at = datetime.now(timezone.utc).isoformat()
    state.accounts.setdefault(target, {})["limit_reset_at"] = None
    write_state(state)

    revived: list[str] = []
    failed: list[str] = []
    reasons: dict[str, str] = {}
    for snapshot in to_revive:
        worker, reason = current_worker_for_revival(snapshot)
        if not worker:
            failed.append(snapshot.ticket)
            if reason:
                reasons[snapshot.ticket] = reason
            continue
        new_window, reason = _revive_worker(worker, session_by_window.get(snapshot.window))
        if new_window:
            revived.append(worker.ticket)
        else:
            failed.append(worker.ticket)
            if reason:
                reasons[worker.ticket] = reason

    _append_rotation_log(outgoing, target, revived, failed)
    return RotationResult(
        outgoing=outgoing,
        incoming=target,
        revived=revived,
        failed=failed,
        reset_at=outgoing_reset_at,
        failed_reasons=reasons,
    )


@dataclass(frozen=True)
class _FileSnapshot:
    exists: bool
    contents: bytes = b""
    mode: int | None = None


def _capture_file(path: Path) -> _FileSnapshot:
    try:
        contents = path.read_bytes()
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        return _FileSnapshot(exists=False)
    return _FileSnapshot(exists=True, contents=contents, mode=mode)


def _restore_file(path: Path, snapshot: _FileSnapshot) -> None:
    if not snapshot.exists:
        path.unlink(missing_ok=True)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f".{path.name}.wiki-rollback-{os.getpid()}-{time.time_ns()}"
    )
    try:
        tmp.write_bytes(snapshot.contents)
        if snapshot.mode is not None:
            os.chmod(tmp, snapshot.mode)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _replace_state(target: AccountState, source: AccountState) -> None:
    accounts = copy.deepcopy(source.accounts)
    target.active = source.active
    target.last_rotated_at = source.last_rotated_at
    target.accounts = accounts


def rotate_credentials(
    *,
    state: AccountState,
    force_target: str | None = None,
    outgoing_reset_at: str | None = None,
    snapshot_outgoing: bool = True,
) -> RotationResult:
    """Atomically swap auth after the supervisor has quiesced Codex runs.

    The caller may snapshot the outgoing account before quiescing the fleet and
    pass ``snapshot_outgoing=False``. On every failure, the active auth file,
    persisted account state, and caller-owned ``AccountState`` are restored to
    their exact pre-call values. This credential-only path never inspects or
    controls tmux.
    """

    target = force_target or pick_next_account(state)
    if target is None:
        raise NoEligibleAccountError("no eligible account for rotation")
    if target == state.active:
        raise NoEligibleAccountError("target account is already active")
    outgoing = state.active

    auth_before = _capture_file(codex_auth_path())
    state_file_before = _capture_file(_state_path())
    state_before = copy.deepcopy(state)
    candidate = copy.deepcopy(state)

    try:
        if outgoing and snapshot_outgoing:
            snapshot_active_auth(outgoing)
        if outgoing and outgoing_reset_at is not None:
            candidate.accounts.setdefault(outgoing, {})[
                "limit_reset_at"
            ] = outgoing_reset_at
        if not install_incoming_auth(target):
            raise RotationError(f"incoming auth.json missing for account {target!r}")
        if not codex_login_status():
            raise RotationError(f"codex login status failed after swap to {target!r}")
        candidate.active = target
        candidate.last_rotated_at = datetime.now(timezone.utc).isoformat()
        candidate.accounts.setdefault(target, {})["limit_reset_at"] = None
        write_state(candidate)
        _replace_state(state, candidate)
    except Exception as error:
        rollback_errors: list[str] = []
        for label, path, snapshot in (
            ("active auth", codex_auth_path(), auth_before),
            ("account state", _state_path(), state_file_before),
        ):
            try:
                _restore_file(path, snapshot)
            except OSError as rollback_error:
                rollback_errors.append(f"{label}: {rollback_error}")
        _replace_state(state, state_before)
        if rollback_errors:
            detail = "; ".join(rollback_errors)
            raise RotationError(
                f"credential rotation failed and rollback was incomplete ({detail})"
            ) from error
        raise

    return RotationResult(
        outgoing=outgoing,
        incoming=target,
        revived=[],
        failed=[],
        reset_at=outgoing_reset_at,
    )


def _resolve_revival_session_id(worker: WorkerEntry) -> str | None:
    """Registry-tracked id beats discovery. Otherwise scan rollouts."""
    if worker.session_id:
        return worker.session_id
    return find_session_id_for_worker(worker.ticket, worker.worktree, worker.spawned_at)


def _revive_worker(
    worker: WorkerEntry,
    target_session: str | None,
) -> tuple[str | None, str | None]:
    """Kick a fresh window off `codex resume <session_id>` for one worker.

    Explicit-id-only: if no lineage is resolvable, we do NOT spawn a fresh
    session (which would replay no history and orphan the transcript). Return
    (None, reason) so the caller can surface an SSE alert.
    """
    if not target_session:
        return None, "original tmux session unavailable for revival"
    session_id = _resolve_revival_session_id(worker)
    if not session_id:
        return None, "no session lineage for revival — manual attention needed"
    command = f"codex resume {shlex.quote(session_id)}"
    new_window = tmux_new_window(
        f"{worker.kind}:{worker.ticket}",
        worker.worktree,
        command,
        target_session=target_session,
    )
    if not new_window:
        return None, "tmux new-window failed"
    new_log = _next_log_path(worker.log)
    tmux_pipe_pane(new_window, new_log)
    # Codex's cwd-mismatch chooser shows up when the rollout's recorded cwd
    # differs from launch cwd. Answer "2" (use current dir = worktree) so the
    # NEW rollout stays cwd-mappable by the resolver.
    wait_for_cwd_dialog_and_answer(new_window)
    wait_for_codex_ready(new_window, revival_wait_seconds())
    # Post-resume: rollout id may change (resume writes a fresh rollout with a
    # new id). Re-scan and record whichever id is now newest for this worker.
    post_resume_id = find_session_id_for_worker(
        worker.ticket, worker.worktree, worker.spawned_at
    )
    if not post_resume_id:
        tmux_kill_window(new_window)
        return None, "post-resume session id unresolved; registry not updated"
    updated, update_error = wiki_agent_update(
        worker.ticket,
        new_window,
        new_log,
        post_resume_id,
    )
    if not updated:
        tmux_kill_window(new_window)
        return None, f"wiki agent update failed: {update_error or 'unknown error'}"
    tmux_send_literal_and_enter(new_window, revival_message(worker.ticket))
    return new_window, None


def revive_auth_dead(workers: list[WorkerEntry]) -> RevivalResult:
    """Kill + resume workers that hit the stale-refresh-token pane signature.

    Auth-dead != limit-dead: the current auth.json is already fresh; the codex
    process just needs to reload it. No account swap; no debounce interaction.
    """
    live = tmux_live_windows()
    to_revive = [w for w in workers if w.window in live]
    session_by_window: dict[str, str | None] = {
        w.window: tmux_window_session(w.window) for w in to_revive
    }
    for worker in to_revive:
        tmux_kill_window(worker.window)
    revived: list[str] = []
    failed: list[str] = []
    reasons: dict[str, str] = {}
    for snapshot in to_revive:
        worker, reason = current_worker_for_revival(snapshot)
        if not worker:
            failed.append(snapshot.ticket)
            if reason:
                reasons[snapshot.ticket] = reason
            continue
        new_window, reason = _revive_worker(worker, session_by_window.get(snapshot.window))
        if new_window:
            revived.append(worker.ticket)
        else:
            failed.append(worker.ticket)
            if reason:
                reasons[worker.ticket] = reason
    return RevivalResult(revived=revived, failed=failed, failed_reasons=reasons)


def _append_rotation_log(outgoing: str | None, incoming: str, revived: list[str], failed: list[str]) -> None:
    """One-line append per rotation. Never logs credential contents."""
    path = rotation_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "from": outgoing,
            "to": incoming,
            "revived": revived,
            "failed": failed,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        pass


def record_rotation_log(result: RotationResult) -> None:
    _append_rotation_log(
        result.outgoing,
        result.incoming,
        result.revived,
        result.failed,
    )


class RotationError(Exception):
    pass


class NoEligibleAccountError(RotationError):
    pass


class RotationDebouncedError(RotationError):
    pass


# One rotation at a time across manual endpoint + watchdog. asyncio.Lock is
# safe here — every rotate() call runs under a single event loop (endpoint is
# async; watchdog awaits inside the loop). Sync callers guard via
# run_coroutine_threadsafe if that ever changes.
_rotation_lock: asyncio.Lock | None = None


def _get_rotation_lock() -> asyncio.Lock:
    global _rotation_lock
    if _rotation_lock is None:
        _rotation_lock = asyncio.Lock()
    return _rotation_lock


def seconds_since_last_rotation(state: AccountState) -> float:
    """Distance from now to the persisted last_rotated_at, inf if never rotated."""
    if not state.last_rotated_at:
        return float("inf")
    try:
        last = datetime.fromisoformat(state.last_rotated_at)
    except ValueError:
        return float("inf")
    now = datetime.now(last.tzinfo) if last.tzinfo else datetime.now()
    return (now - last).total_seconds()


async def rotate_locked(
    *,
    state: AccountState,
    force_target: str | None = None,
    outgoing_reset_at: str | None = None,
    respect_debounce: bool = True,
) -> "RotationResult":
    """Serialize rotations and re-read persisted state under the lock.

    Debounce check runs BOTH against the persisted state (survives restart /
    cross-request) and against `respect_debounce` = False for a forced call
    from the endpoint (which does its own 409 handling upstream).
    """
    async with _get_rotation_lock():
        # Re-read under the lock — the in-memory state we were handed may be
        # stale (parallel rotate wrote the file after we snapshotted).
        fresh = await asyncio.to_thread(read_state)
        fresh = await asyncio.to_thread(ensure_state_initialized, fresh)
        if respect_debounce:
            elapsed = seconds_since_last_rotation(fresh)
            if elapsed < debounce_seconds():
                raise RotationDebouncedError(
                    f"rotation debounced ({elapsed:.0f}s since last)"
                )
        # Merge caller-provided overrides onto the fresh view.
        if state.accounts:
            for name, row in state.accounts.items():
                if isinstance(row, dict):
                    reset = row.get("limit_reset_at")
                    if reset is not None:
                        fresh.accounts.setdefault(name, {})["limit_reset_at"] = reset
        return await asyncio.to_thread(
            rotate,
            state=fresh,
            force_target=force_target,
            outgoing_reset_at=outgoing_reset_at,
        )


async def rotate_credentials_locked(
    *,
    state: AccountState,
    force_target: str | None = None,
    outgoing_reset_at: str | None = None,
    respect_debounce: bool = True,
    snapshot_outgoing: bool = True,
) -> RotationResult:
    """Serialize a supervisor-coordinated credential-only rotation."""

    async with _get_rotation_lock():
        fresh = await asyncio.to_thread(read_state)
        fresh = await asyncio.to_thread(ensure_state_initialized, fresh)
        if respect_debounce:
            elapsed = seconds_since_last_rotation(fresh)
            if elapsed < debounce_seconds():
                raise RotationDebouncedError(
                    f"rotation debounced ({elapsed:.0f}s since last)"
                )
        if state.accounts:
            for name, row in state.accounts.items():
                if isinstance(row, dict):
                    reset = row.get("limit_reset_at")
                    if reset is not None:
                        fresh.accounts.setdefault(name, {})["limit_reset_at"] = reset
        return await asyncio.to_thread(
            rotate_credentials,
            state=fresh,
            force_target=force_target,
            outgoing_reset_at=outgoing_reset_at,
            snapshot_outgoing=snapshot_outgoing,
        )


# ---------------------------------------------------------------------------
# Watchdog loop.
# ---------------------------------------------------------------------------

EventEmitter = Callable[[dict], Awaitable[None]]


@dataclass
class WatchdogInternalState:
    last_rotation_attempt: float = 0.0
    last_alert_at: dict[str, float] = field(default_factory=dict)
    last_no_eligible_alert: float = 0.0
    # Per-ticket auth-dead revival timestamps (monotonic). Bounded loop:
    # after AUTH_DEAD_MAX_ATTEMPTS attempts inside AUTH_DEAD_WINDOW_SECONDS,
    # or if the last attempt was under AUTH_DEAD_COOLDOWN_SECONDS ago, skip
    # the revive and emit a manual-attention alert — otherwise a genuinely
    # broken token loops kill+resume every poll cycle forever.
    auth_dead_attempts: dict[str, list[float]] = field(default_factory=dict)
    auth_dead_alert_at: dict[str, float] = field(default_factory=dict)
    # Tickets whose pane currently shows the Claude usage-limit banner. When
    # the banner leaves the pane on a later poll, the watchdog emits
    # claude_limit_cleared — the recovery proof that resolves the notice.
    claude_limited: set[str] = field(default_factory=set)


AUTH_DEAD_MAX_ATTEMPTS = 3
AUTH_DEAD_WINDOW_SECONDS = 3600.0
AUTH_DEAD_COOLDOWN_SECONDS = 300.0
AUTH_DEAD_ALERT_INTERVAL_SECONDS = 3600.0


def _seconds_since(ts: float) -> float:
    return time.monotonic() - ts if ts else float("inf")


async def _check_once(
    watch: WatchdogInternalState,
    emit: EventEmitter,
) -> None:
    live = await asyncio.to_thread(tmux_live_windows)
    codex_workers = await asyncio.to_thread(iter_workers, "cdx")
    claude_workers = await asyncio.to_thread(iter_workers, "cc")

    codex_hits: list[tuple[WorkerEntry, str]] = []
    auth_dead: list[WorkerEntry] = []
    for worker in codex_workers:
        if worker.window not in live:
            continue
        pane = await asyncio.to_thread(tmux_capture, worker.window, 80)
        if detect_codex_limit(pane):
            codex_hits.append((worker, pane))
        elif detect_codex_auth_dead(pane):
            auth_dead.append(worker)

    # Auth-dead workers get killed + resumed on the CURRENT auth.json — no
    # account swap, so no debounce interaction with the rotation loop below.
    # Bounded per-ticket: cooldown + windowed max-attempts. Genuinely-dead
    # tokens keep re-showing the signature after revive; without the cap the
    # watchdog would kill+resume the same worker every poll forever.
    if auth_dead:
        now_mono = time.monotonic()
        eligible: list[WorkerEntry] = []
        exhausted: list[str] = []
        for worker in auth_dead:
            history = watch.auth_dead_attempts.setdefault(worker.ticket, [])
            history[:] = [t for t in history if now_mono - t < AUTH_DEAD_WINDOW_SECONDS]
            if history and now_mono - history[-1] < AUTH_DEAD_COOLDOWN_SECONDS:
                exhausted.append(worker.ticket)
                continue
            if len(history) >= AUTH_DEAD_MAX_ATTEMPTS:
                exhausted.append(worker.ticket)
                continue
            history.append(now_mono)
            eligible.append(worker)
        if exhausted:
            need_alert = [
                ticket for ticket in exhausted
                if now_mono - watch.auth_dead_alert_at.get(ticket, 0.0)
                >= AUTH_DEAD_ALERT_INTERVAL_SECONDS
            ]
            if need_alert:
                for ticket in need_alert:
                    watch.auth_dead_alert_at[ticket] = now_mono
                await emit({
                    "type": "codex_auth_dead_exhausted",
                    "provider": "codex",
                    "failure": "auth",
                    "credential_source": "current",
                    "exhausted": True,
                    "tickets": need_alert,
                    "ts": datetime.now(timezone.utc).isoformat(),
                })
        if eligible:
            result = await asyncio.to_thread(revive_auth_dead, eligible)
            await emit({
                "type": "codex_auth_dead_revival",
                "provider": "codex",
                "failure": "auth",
                "credential_source": "current",
                "revived": result.revived,
                "failed": result.failed,
                "failed_reasons": result.failed_reasons,
                "ts": datetime.now(timezone.utc).isoformat(),
            })

    for worker in claude_workers:
        if worker.window not in live:
            continue
        pane = await asyncio.to_thread(tmux_capture, worker.window, 80)
        if detect_claude_limit(pane):
            watch.claude_limited.add(worker.ticket)
            # Alert once per hour per ticket.
            if _seconds_since(watch.last_alert_at.get(worker.ticket, 0.0)) < 3600:
                continue
            watch.last_alert_at[worker.ticket] = time.monotonic()
            await emit({
                "type": "claude_limit_hit",
                "ticket": worker.ticket,
                "window": worker.window,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
        elif worker.ticket in watch.claude_limited:
            # The limit banner left the pane of a previously limited worker:
            # the only observable proof that this worker can make progress.
            watch.claude_limited.discard(worker.ticket)
            watch.last_alert_at.pop(worker.ticket, None)
            await emit({
                "type": "claude_limit_cleared",
                "ticket": worker.ticket,
                "window": worker.window,
                "ts": datetime.now(timezone.utc).isoformat(),
            })

    if not codex_hits:
        return

    if _seconds_since(watch.last_rotation_attempt) < debounce_seconds():
        return

    state = await asyncio.to_thread(read_state)
    state = await asyncio.to_thread(ensure_state_initialized, state)
    # Persisted debounce survives backend restart AND cross-request races.
    if seconds_since_last_rotation(state) < debounce_seconds():
        return

    watch.last_rotation_attempt = time.monotonic()

    # Aggregate the outgoing account's reset window from any hit that carried one.
    outgoing_reset: str | None = None
    for _, pane in codex_hits:
        parsed = parse_reset_time(pane)
        if parsed:
            outgoing_reset = parsed
            break
    if outgoing_reset is None:
        outgoing_reset = _fallback_reset_time()

    if state.active:
        state.accounts.setdefault(state.active, {})["limit_reset_at"] = outgoing_reset

    try:
        result = await rotate_locked(
            state=state,
            outgoing_reset_at=outgoing_reset,
        )
    except RotationDebouncedError:
        return
    except NoEligibleAccountError:
        if _seconds_since(watch.last_no_eligible_alert) >= 3600:
            watch.last_no_eligible_alert = time.monotonic()
            reset_hint = outgoing_reset or _earliest_pending_reset(state)
            await emit({
                "type": "codex_limit_no_eligible",
                "tickets": [w.ticket for w, _ in codex_hits],
                "reset_at": reset_hint,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
        return
    except RotationError as exc:
        await emit({
            "type": "codex_rotation_failed",
            "error": str(exc),
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        return

    await emit({
        "type": "codex_rotation",
        "from": result.outgoing,
        "to": result.incoming,
        "revived": result.revived,
        "failed": result.failed,
        "failed_reasons": result.failed_reasons,
        # Legacy tmux workers have no run_id, so this map is empty for
        # this path. The headless supervisor populates it; notices with
        # no stored run_id fall back to ticket-only reconciliation
        # (archive clears, replace does not) — legacy replace flows
        # publish codex_worker_replaced to cover that case.
        "failed_run_ids": result.failed_run_ids,
        "ts": datetime.now(timezone.utc).isoformat(),
    })


def _earliest_pending_reset(state: AccountState) -> str | None:
    times: list[str] = []
    for row in state.accounts.values():
        value = row.get("limit_reset_at") if isinstance(row, dict) else None
        if isinstance(value, str):
            times.append(value)
    if not times:
        return None
    try:
        return min(times, key=lambda t: datetime.fromisoformat(t))
    except ValueError:
        return times[0]


async def watchdog_loop(emit: EventEmitter) -> None:
    """Never dies — every iteration guards against transient tmux/registry errors."""
    watch = WatchdogInternalState()
    while True:
        try:
            if watchdog_enabled():
                await _check_once(watch, emit)
        except Exception:  # noqa: BLE001 — same policy as message_dispatcher
            pass
        await asyncio.sleep(poll_seconds())


# ---------------------------------------------------------------------------
# Public snapshot for the /api/accounts endpoint.
# ---------------------------------------------------------------------------

def snapshot() -> dict[str, object]:
    """No credential contents — safe to expose over HTTP."""
    state = read_state()
    state = ensure_state_initialized(state)
    accounts: list[dict[str, object]] = []
    for name in list_available_accounts():
        row = state.accounts.get(name) or {}
        accounts.append({
            "name": name,
            "limit_reset_at": row.get("limit_reset_at"),
            "eligible": _iso_in_past_or_null(row.get("limit_reset_at")),
        })
    return {
        "active": state.active,
        "last_rotated_at": state.last_rotated_at,
        "debounce_seconds": debounce_seconds(),
        "watchdog_enabled": watchdog_enabled(),
        "accounts": accounts,
    }
