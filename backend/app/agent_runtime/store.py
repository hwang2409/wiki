from __future__ import annotations

import base64
import errno
import json
import logging
import os
import shutil
import stat
import tempfile
import threading
from collections.abc import Callable, Iterator
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from .. import knowledge
from .archive_protocol import archive_is_committed, commit_archive
from .command_log import CommandLog
from .process import (
    provider_process_group_members_sync,
    provider_processes_for_run_sync,
    provider_process_status_sync,
    terminate_verified_provider_group,
)
from .provider import AdapterStatus
from .types import (
    EventDisposition,
    LifecycleState,
    ProviderKind,
    RunRecord,
    MAX_MESSAGE_DEDUPE_KEYS,
    MAX_PENDING_USER_MESSAGES,
    TERMINAL_STATES,
    utc_now,
    validate_transition,
)


MAX_START_STATUS_BYTES = 64 * 1024
DEFAULT_RUN_RETENTION_DAYS = 30
logger = logging.getLogger(__name__)


def _run_retention_cutoff() -> datetime:
    raw_days = os.environ.get("WIKI_AGENT_RUN_RETENTION_DAYS")
    try:
        days = float(raw_days) if raw_days is not None else DEFAULT_RUN_RETENTION_DAYS
    except ValueError:
        days = DEFAULT_RUN_RETENTION_DAYS
    days = max(0.0, days)
    return datetime.now(timezone.utc) - timedelta(days=days)


def _run_is_older_than(value: str, cutoff: datetime) -> bool:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc) < cutoff


class StoreError(RuntimeError):
    pass


class StoreConflict(StoreError):
    pass


class RunNotFound(StoreError):
    pass


def _validated_run_id(run_id: str) -> str:
    """Accept only canonical UUIDs before using a run id as a path component."""

    try:
        parsed = UUID(run_id)
    except (ValueError, AttributeError) as exc:
        raise StoreError(f"invalid run id: {run_id!r}") from exc
    if str(parsed) != run_id:
        raise StoreError(f"run id is not a canonical UUID: {run_id!r}")
    return run_id


def _validated_operation_id(operation_id: str) -> str:
    """Accept only canonical UUIDs for stale-operation comparisons."""

    try:
        parsed = UUID(operation_id)
    except (ValueError, AttributeError) as exc:
        raise StoreError(f"invalid quiesce operation id: {operation_id!r}") from exc
    if str(parsed) != operation_id:
        raise StoreError(
            f"quiesce operation id is not a canonical UUID: {operation_id!r}"
        )
    return operation_id


def _provider_request_key(request_id: str | int) -> str:
    if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
        raise StoreError("provider request id must be a string or integer")
    prefix = "int" if isinstance(request_id, int) else "str"
    return f"{prefix}:{request_id}"


def _provider_request_id(kind: str, payload: dict[str, Any]) -> str | int | None:
    if kind == "approval":
        value = payload.get("id", payload.get("request_id"))
    elif kind == "approval_resolved":
        params = payload.get("params") or {}
        value = params.get("requestId") if isinstance(params, dict) else None
    elif kind == "approval_cancelled":
        value = payload.get("request_id")
    elif kind == "approval_response":
        response = payload.get("response") or {}
        value = (
            response.get("request_id")
            if isinstance(response, dict) and payload.get("type") == "control_response"
            else payload.get("id")
        )
    else:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return value


def _codex_turn_id(payload: dict[str, Any]) -> str | None:
    params = payload.get("params")
    if not isinstance(params, dict):
        return None
    direct = params.get("turnId")
    if isinstance(direct, str) and direct:
        return direct
    turn = params.get("turn")
    if isinstance(turn, dict):
        value = turn.get("id")
        if isinstance(value, str) and value:
            return value
    return None


def _current_turn_diff_update(
    record: RunRecord,
    envelope: dict[str, Any],
) -> tuple[bool, str | None]:
    if record.provider is not ProviderKind.CODEX:
        return False, None
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return False, None
    method = payload.get("method")
    seq = int(envelope.get("seq", 0))
    if method == "turn/started":
        record.current_turn_diff_turn_id = _codex_turn_id(payload)
        record.current_turn_diff_started_seq = seq
        record.current_turn_diff_seq = 0
        return True, None
    if method != "turn/diff/updated" or record.current_turn_diff_started_seq <= 0:
        return False, None
    params = payload.get("params")
    diff = params.get("diff") if isinstance(params, dict) else None
    if isinstance(diff, str):
        record.current_turn_diff_seq = seq
        return True, diff
    return False, None


def _apply_pending_request_event(
    record: RunRecord,
    *,
    kind: str,
    payload: dict[str, Any],
    raw_seq: int,
    normalized_at: str,
) -> None:
    request_id = _provider_request_id(kind, payload)
    if (
        kind == "approval"
        and request_id is not None
        and record.state not in TERMINAL_STATES
    ):
        request = payload.get("request") or {}
        request_kind = payload.get("method")
        if not isinstance(request_kind, str) and isinstance(request, dict):
            request_kind = request.get("subtype")
        record.pending_requests[_provider_request_key(request_id)] = {
            "request_id": request_id,
            "request_kind": str(request_kind or "approval"),
            "received_at": normalized_at,
            "raw_seq": raw_seq,
            "payload": dict(payload),
        }
    elif kind in {
        "approval_resolved",
        "approval_cancelled",
        "approval_response",
    } and request_id is not None:
        record.pending_requests.pop(_provider_request_key(request_id), None)


_UNREAD_SKIP_KIND_SUFFIXES = ("_client_message", "_stderr")

# Codex protocol rows that fire around a turn (before/after any actual worker
# output) but do not represent a new view for Henry. Names match the
# ``method.replace("/", "_")`` conversion used by _normalize_codex.
_CODEX_UNREAD_SKIP_KINDS = frozenset(
    {
        "thread_started",
        "thread_archived",
        "thread_closed",
        "thread_status_changed",
        "thread_tokenUsage_updated",
        "thread_settings_updated",
        "thread_goal_cleared",
        "turn_started",
        "turn_completed",
        "turn_aborted",
        "context_compacted",
        "serverRequest_resolved",
        "account_rateLimits_updated",
        "account_chatgptAuthTokens_refresh",
    }
)

_UNREAD_SKIP_KINDS = frozenset(
    _CODEX_UNREAD_SKIP_KINDS
    | {
        # Inbound Claude user echo — same rule as Codex user items below.
        "claude_user",
        # (``codex_user`` is unreachable in practice — Codex user turns are
        #  ``item_started`` / ``item_completed`` with ``item.type ==
        #  "userMessage"``; those are filtered by payload inspection below.)
        "codex_user",
        # Approval-side and provider-lifecycle rows.
        "approval",
        "approval_response",
        "approval_cancelled",
        "approval_resolved",
        "provider_process_exit",
        "provider_protocol_error",
        "rpc_response",
        "unknown",
        "normalization_error",
    }
)


def _codex_item_type(payload: dict[str, Any]) -> str | None:
    params = payload.get("params")
    if not isinstance(params, dict):
        return None
    item = params.get("item")
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    return item_type if isinstance(item_type, str) else None


def _claude_payload_type(payload: dict[str, Any]) -> str | None:
    value = payload.get("type")
    return value if isinstance(value, str) else None


def _is_unread_worthy(kind: str, payload: dict[str, Any] | None, disposition: str) -> bool:
    """Whether a normalized event should advance ``unread_event_seq``.

    Unread is "new worker-authored output Henry has not seen yet". Anything
    else — outbound client_message rows, provider-lifecycle boundaries,
    approval/response bookkeeping, both directions of user echoes (Henry
    typed and synthetic supervisor wakes), token metrics, and any payload
    carrying an explicit ``source`` tag — must leave the counter alone so a
    fleet-monitor turn cannot light the dot before the worker responds.
    """

    if disposition == EventDisposition.IGNORED.value:
        return False
    if kind in _UNREAD_SKIP_KINDS:
        return False
    if any(kind.endswith(suffix) for suffix in _UNREAD_SKIP_KIND_SUFFIXES):
        return False
    if not isinstance(payload, dict):
        # Kind alone survived every skip above; still not a rendered surface.
        return False
    source = payload.get("source")
    if isinstance(source, str) and source:
        return False
    # Codex normalizes real user turns as ``item_started`` / ``item_completed``
    # envelopes whose inner ``item.type == "userMessage"``. Those look agent-
    # originated from the kind alone and would otherwise sneak past the
    # (deliberately unreachable) ``codex_user`` allowlist entry above.
    if kind in {"item_started", "item_completed"}:
        item_type = _codex_item_type(payload)
        if item_type == "userMessage":
            return False
        # Only ``item_completed`` finalizes a visible surface. The paired
        # ``item_started`` is a lifecycle boundary — dropping it prevents
        # each streamed assistant turn from double-counting.
        if kind == "item_started":
            return False
    # Claude payloads with ``type == "user"`` are user inbound echoes even
    # when the outer normalized ``kind`` failed to be ``claude_user`` (e.g.
    # source-tagged wakes surface here with the fleet ``source`` filter
    # above, but defence-in-depth catches any missed labelling).
    if _claude_payload_type(payload) == "user":
        return False
    return True


def _apply_composer_message_event(
    record: RunRecord,
    *,
    payload: dict[str, Any],
    seq: int,
    normalized_at: str,
) -> None:
    pending_id = payload.get("pending_id")
    text = payload.get("composer_text")
    sent_at = payload.get("composer_sent_at")
    if not all(isinstance(value, str) for value in (pending_id, text, sent_at)):
        return
    source_raw = payload.get("source")
    source = source_raw if isinstance(source_raw, str) and source_raw else None
    record.pending_user_messages = [
        message
        for message in record.pending_user_messages
        if message.get("pending_id") != pending_id
    ]
    if any(
        message.get("pending_id") == pending_id
        for message in record.composer_messages
    ):
        return
    entry: dict[str, Any] = {
        "pending_id": pending_id,
        "text": text,
        "sent_at": sent_at,
        "echoed_at": normalized_at,
        "seq": seq,
    }
    if source is not None:
        entry["source"] = source
    record.composer_messages.append(entry)


def _resolved_parent(path: Path) -> Path:
    """Resolve the parent chain, keep the final component unresolved."""
    absolute = path.absolute()
    return absolute.parent.resolve() / absolute.name


@dataclass(frozen=True)
class RuntimePaths:
    runtime_dir: Path
    socket_path: Path
    registry_path: Path
    archive_dir: Path = (
        Path(
            os.environ.get("WIKI_AGENT_ARCHIVE_DIR")
            or Path.home() / "me" / "fun" / "agent-archive"
        )
        .expanduser()
        .absolute()
    )
    status_dir: Path = (
        Path(os.environ.get("WIKI_AGENT_STATUS_DIR") or "/tmp/agent-status")
        .expanduser()
        .absolute()
    )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> RuntimePaths:
        values = os.environ if env is None else env
        runtime_dir = Path(
            values.get("WIKI_AGENT_RUNTIME_DIR")
            or Path.home() / ".wiki" / "agent-runtime"
        ).expanduser()
        socket_path = Path(
            values.get("WIKI_SUPERVISOR_SOCKET_PATH") or runtime_dir / "supervisor.sock"
        ).expanduser()
        registry_path = Path(
            values.get("WIKI_AGENT_REGISTRY_PATH") or "/tmp/agent-registry.json"
        ).expanduser()
        archive_dir = Path(
            values.get("WIKI_AGENT_ARCHIVE_DIR")
            or Path.home() / "me" / "fun" / "agent-archive"
        ).expanduser()
        status_dir = Path(
            values.get("WIKI_AGENT_STATUS_DIR") or "/tmp/agent-status"
        ).expanduser()
        # Resolve the PARENT chain but keep the final component unresolved:
        # macOS's /tmp is itself a symlink (-> /private/tmp), which the
        # symlink-dir guards would otherwise refuse, while the final component
        # must stay unresolved so writers still reject symlinked files/dirs
        # planted at the exact target path.
        return cls(
            runtime_dir=_resolved_parent(runtime_dir),
            socket_path=_resolved_parent(socket_path),
            registry_path=_resolved_parent(registry_path),
            archive_dir=_resolved_parent(archive_dir),
            status_dir=_resolved_parent(status_dir),
        )

    @property
    def runs_dir(self) -> Path:
        return self.runtime_dir / "runs"

    @property
    def lock_path(self) -> Path:
        return self.runtime_dir / "supervisor.lock"

    @property
    def pid_path(self) -> Path:
        return self.runtime_dir / "supervisor.pid"

    @property
    def log_path(self) -> Path:
        return self.runtime_dir / "supervisor.log"

    @property
    def codex_rotation_journal_path(self) -> Path:
        return self.runtime_dir / "codex-rotation-journal.json"

    @property
    def command_log_path(self) -> Path:
        return self.runtime_dir / "command-log.sqlite3"

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / _validated_run_id(run_id)


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise StoreError(f"refusing symlink directory: {path}")
    path.chmod(0o700)


def _ensure_parent_dir(path: Path) -> None:
    """Create a missing parent privately without chmod'ing shared dirs like /tmp."""

    if not path.exists():
        path.mkdir(mode=0o700, parents=True)
    if path.is_symlink():
        raise StoreError(f"refusing symlink directory: {path}")


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _archive_tree_expected_paths(source: Path, destination: Path) -> list[Path]:
    """Return every archive file or symlink expected from an artifact tree."""

    expected: list[Path] = []
    with os.scandir(source) as entries:
        for entry in entries:
            source_entry = Path(entry.path)
            destination_entry = destination / entry.name
            if entry.is_dir(follow_symlinks=False):
                expected.extend(
                    _archive_tree_expected_paths(source_entry, destination_entry)
                )
            elif entry.is_symlink() or entry.is_file(follow_symlinks=False):
                expected.append(destination_entry)
            else:
                raise StoreError(f"refusing special artifact: {source_entry}")
    return expected


def _atomic_write_json(path: Path, value: Any) -> None:
    _ensure_parent_dir(path.parent)
    if path.is_symlink():
        raise StoreError(f"refusing symlink file: {path}")
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        path.chmod(0o600)
        _fsync_file(path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp.unlink(missing_ok=True)
        raise


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    _ensure_parent_dir(path.parent)
    if path.is_symlink():
        raise StoreError(f"refusing symlink file: {path}")
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        path.chmod(0o600)
        _fsync_file(path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp.unlink(missing_ok=True)
        raise


def _read_start_status(path: Path) -> tuple[bool, bytes | None]:
    try:
        initial = os.lstat(path)
    except FileNotFoundError:
        return False, None
    except OSError as exc:
        raise StoreError(f"could not inspect status file: {path}") from exc
    if stat.S_ISLNK(initial.st_mode):
        raise StoreError(f"refusing symlink status file: {path}")
    if not stat.S_ISREG(initial.st_mode):
        raise StoreError(f"refusing non-regular status file: {path}")
    if initial.st_size > MAX_START_STATUS_BYTES:
        raise StoreError(f"status file exceeds {MAX_START_STATUS_BYTES} bytes: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return False, None
    except OSError as exc:
        raise StoreError(f"could not open status file: {path}") from exc
    try:
        opened = os.fstat(fd)
        if stat.S_ISLNK(opened.st_mode) or not stat.S_ISREG(opened.st_mode):
            raise StoreError(f"refusing non-regular status file: {path}")
        if opened.st_size > MAX_START_STATUS_BYTES:
            raise StoreError(f"status file exceeds {MAX_START_STATUS_BYTES} bytes: {path}")
        content = bytearray()
        while len(content) <= MAX_START_STATUS_BYTES:
            chunk = os.read(fd, MAX_START_STATUS_BYTES + 1 - len(content))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > MAX_START_STATUS_BYTES:
            raise StoreError(f"status file exceeds {MAX_START_STATUS_BYTES} bytes: {path}")
        return True, bytes(content)
    finally:
        os.close(fd)


def _append_json_line(path: Path, value: Any) -> None:
    _ensure_parent_dir(path.parent)
    if path.is_symlink():
        raise StoreError(f"refusing symlink file: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, separators=(",", ":"), sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        # fdopen closes the descriptor; this branch covers failures before it.
        try:
            os.close(fd)
        except OSError:
            pass


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RunNotFound(str(path)) from exc
    except (OSError, ValueError) as exc:
        raise StoreError(f"could not read {path}: {exc}") from exc


def _repair_jsonl_tail(path: Path) -> None:
    """Drop a crash-truncated final JSONL fragment before future appends."""

    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return
    if not data or data.endswith(b"\n"):
        return
    complete_end = data.rfind(b"\n") + 1
    fd = os.open(path, os.O_WRONLY)
    try:
        os.ftruncate(fd, complete_end)
        os.fsync(fd)
    finally:
        os.close(fd)


class RunStore:
    """Single-writer durable state owned by the headless supervisor."""

    def __init__(self, paths: RuntimePaths):
        self.paths = paths
        self._lock = threading.RLock()
        # Adapter ownership is process-local. A restarted supervisor must
        # project every retained PID as detached until it reattaches control.
        self._control_attached_run_ids: set[str] = set()
        self._start_registry_snapshots: dict[str, dict[str, Any]] = {}
        self._terminal_run_prune_guard: Callable[[RunRecord], bool] | None = None
        self._archive_events_exporter: Callable[[str, Path, Path], bool] | None = None
        _ensure_private_dir(paths.runtime_dir)
        _ensure_private_dir(paths.runs_dir)
        staging_dir = paths.runs_dir / ".staging"
        _ensure_private_dir(staging_dir)
        for temp_dir in staging_dir.iterdir():
            if temp_dir.is_dir() and not temp_dir.is_symlink():
                shutil.rmtree(temp_dir, ignore_errors=True)
        for temp_dir in paths.runs_dir.glob(".run-*"):
            if temp_dir.is_dir() and not temp_dir.is_symlink():
                shutil.rmtree(temp_dir, ignore_errors=True)
        self.command_log = CommandLog(paths.command_log_path)
        self._abort_uncommitted_starts()
        # Repair the small registry projection before pruning. This lets a
        # terminal current run move to the durable archive safely after a
        # crash between the run and registry writes.
        self._reconcile_registry_from_runs()
        self._reconcile_existing_runs()
        self._reconcile_registry_from_runs()

    def run_dir(self, run_id: str) -> Path:
        return self.paths.run_dir(run_id)

    def run_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "run.json"

    def raw_events_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "raw.jsonl"

    def normalized_events_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "events.jsonl"

    def set_terminal_run_prune_guard(
        self, guard: Callable[[RunRecord], bool] | None
    ) -> None:
        self._terminal_run_prune_guard = guard

    def set_archive_events_exporter(
        self,
        exporter: Callable[[str, Path, Path], bool] | None,
    ) -> None:
        """Set the SQLite archive exporter used before archive commit."""

        self._archive_events_exporter = exporter

    def _export_archive_events(
        self,
        run_id: str,
        source: Path,
        destination: Path,
    ) -> bool:
        exporter = self._archive_events_exporter
        if exporter is None:
            try:
                from .event_store import SQLiteEventStore, runtime_event_db_path

                exporter = SQLiteEventStore(
                    runtime_event_db_path(self.paths.runtime_dir),
                    migrate=False,
                ).export_events_jsonl
            except Exception:
                return False
        try:
            return bool(
                exporter(
                    run_id,
                    destination,
                    legacy_source=source,
                )
            )
        except Exception:
            logger.exception("SQLite archive export failed for %s", run_id)
            destination.unlink(missing_ok=True)
            return False

    def prune_terminal_runs(self) -> dict[str, int]:
        with self._lock:
            return self._prune_terminal_runs()

    def current_turn_diff_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "current-turn-diff.json"

    def provider_log_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "provider.log"

    def archive_ticket_dir(self, agent_id: str) -> Path:
        return self.paths.archive_dir / agent_id

    def _find_archived_run_entry(
        self, run_id: str
    ) -> tuple[RunRecord, Path] | None:
        """Return one archive record and its session directory."""

        for session_dir in self.paths.archive_dir.glob("*/*"):
            try:
                if not archive_is_committed(session_dir):
                    continue
                value = _read_json(session_dir / "run.json")
                if isinstance(value, dict) and value.get("run_id") == run_id:
                    return RunRecord.from_dict(value), session_dir
            except (OSError, StoreError, TypeError, ValueError):
                continue
        return None

    def find_archived_run(self, run_id: str) -> RunRecord | None:
        """Find one completed archive for a replayed archive effect."""

        with self._lock:
            entry = self._find_archived_run_entry(run_id)
            return entry[0] if entry is not None else None

    def find_latest_archived_agent(self, agent_id: str) -> RunRecord | None:
        """Return the newest archived run for an idempotent archive retry."""

        with self._lock:
            candidates = sorted(
                self.archive_ticket_dir(agent_id).glob("*/archive-complete.json"),
                key=lambda path: path.parent.name,
                reverse=True,
            )
            for marker_path in candidates:
                try:
                    value = _read_json(marker_path.parent / "run.json")
                    if isinstance(value, dict):
                        return RunRecord.from_dict(value)
                except (OSError, StoreError, TypeError, ValueError):
                    continue
            return None

    def finalize_archived_run(self, run_id: str) -> RunRecord | None:
        """Resume archive cleanup and return only after live state is gone."""

        with self._lock:
            archived_entry = self._find_archived_run_entry(run_id)
            if archived_entry is None:
                return None
            archived, session_dir = archived_entry
            if not archive_is_committed(session_dir):
                raise StoreConflict("archive is no longer committed")

            live_path = self.run_path(run_id)
            if live_path.is_file():
                try:
                    live = RunRecord.from_dict(_read_json(live_path))
                except (OSError, StoreError, TypeError, ValueError) as exc:
                    raise StoreConflict(
                        "archive marker has an unreadable live run"
                    ) from exc
                if live.created_at != archived.created_at:
                    raise StoreConflict(
                        "older archive marker cannot remove a newer live run"
                    )
            registry = self._read_registry()
            entry = registry.get(archived.agent_id)
            current = entry.get("current") if isinstance(entry, dict) else None
            if isinstance(current, dict) and current.get("run_id") not in {
                None,
                run_id,
            }:
                raise StoreConflict("older archive marker cannot remove a live current run")

            if archived.implicit_start_request and archived.start_request_id:
                # Repair this index before deleting the implicit receipt. A
                # restart can otherwise reuse the old deterministic run id.
                self.command_log.archive_start_request(
                    archived.start_request_id,
                    archived.run_id,
                    str(session_dir),
                )

            run_dir = self.run_dir(run_id)
            if run_dir.exists():
                shutil.rmtree(run_dir)
            if run_dir.exists():
                raise StoreConflict("archived run directory remains after cleanup")
            self.command_log.forget_implicit_for_run(run_id)

            if isinstance(current, dict) and current.get("run_id") == run_id:
                registry.pop(archived.agent_id, None)
                self._write_registry(registry)
                self.command_log.replace_projection(archived.agent_id, {})
            elif not isinstance(current, dict):
                self.command_log.replace_projection(archived.agent_id, {})

            entry = self._read_registry().get(archived.agent_id)
            current = entry.get("current") if isinstance(entry, dict) else None
            if run_dir.exists() or (
                isinstance(current, dict) and current.get("run_id") == run_id
            ):
                raise StoreConflict("archived run remains live after cleanup")
            return archived

    def discover_provider_process(self, run_id: str) -> RunRecord:
        """Persist a provider found by its exact inherited run identity."""

        with self._lock:
            record = self.get(run_id)
            if record.provider_pid is not None:
                return record
            candidates = provider_processes_for_run_sync(
                record.run_id,
                record.agent_id,
            )
            if not candidates:
                return record
            candidate_pids = {item.pid for item in candidates}
            roots = [
                item for item in candidates if item.parent_pid not in candidate_pids
            ]
            identity = min(roots or candidates, key=lambda item: item.pid)
            record.provider_pid = identity.pid
            record.provider_pid_started_at = identity.created_at
            record.provider_executable = identity.executable
            record.provider_process_group_id = identity.process_group_id
            record.provider_process_group_members = (
                provider_process_group_members_sync(identity.process_group_id)
            )
            self._write_record(record)
            registry = self._read_registry()
            current = (registry.get(record.agent_id) or {}).get("current") or {}
            if current.get("run_id") == record.run_id:
                registry[record.agent_id]["current"] = self._registry_current(record)
                self._write_registry(registry)
                self.command_log.replace_projection(
                    record.agent_id,
                    {record.agent_id: registry[record.agent_id]},
                )
            return record

    def find_archived_start_request(self, request_id: str) -> RunRecord | None:
        """Find an implicit start that was already archived and is reusable."""

        with self._lock:
            indexed = self.command_log.archived_start_request(request_id)
            if indexed is None:
                return None
            try:
                value = _read_json(Path(indexed["session_path"]) / "run.json")
            except (OSError, StoreError, TypeError, ValueError):
                return None
            if not isinstance(value, dict):
                return None
            record = RunRecord.from_dict(value)
            return record if record.run_id == indexed["run_id"] else None

    def status_path(self, agent_id: str) -> Path:
        return self.paths.status_dir / f"{agent_id}.json"

    def read_codex_rotation_journal(self) -> dict[str, Any] | None:
        """Read the secret-free account-rotation checkpoint, if present."""

        with self._lock:
            try:
                value = _read_json(self.paths.codex_rotation_journal_path)
            except RunNotFound:
                return None
            if value == {}:
                return None
            if not isinstance(value, dict):
                raise StoreError("Codex rotation journal must contain an object")
            return dict(value)

    def write_codex_rotation_journal(self, value: dict[str, Any]) -> None:
        """Atomically persist caller-sanitized rotation coordination metadata."""

        with self._lock:
            _atomic_write_json(self.paths.codex_rotation_journal_path, dict(value))

    def clear_codex_rotation_journal(self) -> None:
        with self._lock:
            _atomic_write_json(self.paths.codex_rotation_journal_path, {})

    def _read_registry(self) -> dict[str, Any]:
        try:
            value = json.loads(self.paths.registry_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise StoreError(
                f"could not read registry {self.paths.registry_path}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise StoreError("agent registry must contain an object")
        return value

    def _write_registry(self, registry: dict[str, Any]) -> None:
        _atomic_write_json(self.paths.registry_path, registry)

    def command_state(self) -> dict[str, Any]:
        """Return the registry projection used by the command decider."""

        with self._lock:
            return deepcopy(self._read_registry())

    def command_state_for(self, agent_id: str) -> dict[str, Any]:
        """Return one agent projection for a keyed command transaction."""

        with self._lock:
            projected = self.command_log.projection_for(agent_id)
            if projected:
                return deepcopy(projected)
            entry = self._read_registry().get(agent_id)
            if entry is None:
                return {agent_id: None}
            seeded = {agent_id: deepcopy(entry)}
            self.command_log.seed_projection(agent_id, seeded)
            return seeded

    def authoritative_command_state_for(self, agent_id: str) -> dict[str, Any]:
        """Read one agent from the registry without using the command projection."""

        with self._lock:
            entry = self._read_registry().get(agent_id)
            return {agent_id: deepcopy(entry)} if entry is not None else {agent_id: None}

    def find_start_request(self, request_id: str) -> RunRecord | None:
        """Find a durable successful start after cache eviction or restart."""

        if not isinstance(request_id, str) or not request_id:
            return None
        with self._lock:
            indexed = self.command_log.start_request(request_id)
            if indexed is None:
                return None
            try:
                record = self.get(str(indexed["run_id"]))
            except RunNotFound:
                return None
            if (
                record.agent_id == indexed["agent_id"]
                and self.is_current(record)
                and record.start_transaction is None
            ):
                return record
        return None

    def _restore_start_snapshot(
        self,
        record: RunRecord,
        snapshot: dict[str, Any] | None,
    ) -> None:
        """Restore the pre-start registry and status file from durable data."""

        registry = self._read_registry()
        current = registry.get(record.agent_id)
        current_run_id = (
            current.get("current", {}).get("run_id")
            if isinstance(current, dict)
            and isinstance(current.get("current"), dict)
            else None
        )
        if snapshot is None:
            if current_run_id == record.run_id:
                registry.pop(record.agent_id, None)
            self.status_path(record.agent_id).unlink(missing_ok=True)
            self._write_registry(registry)
            return

        if bool(snapshot.get("agent_present")):
            previous = snapshot.get("agent_entry")
            if isinstance(previous, dict):
                registry[record.agent_id] = deepcopy(previous)
        elif current_run_id == record.run_id:
            registry.pop(record.agent_id, None)

        legacy = registry.get("_orchestrators")
        if bool(snapshot.get("legacy_present")):
            previous_legacy = snapshot.get("legacy_entry")
            if not isinstance(legacy, dict):
                legacy = {}
                registry["_orchestrators"] = legacy
            if isinstance(previous_legacy, dict):
                legacy[record.agent_id] = deepcopy(previous_legacy)
        elif isinstance(legacy, dict) and current_run_id == record.run_id:
            legacy.pop(record.agent_id, None)
            if not legacy:
                registry.pop("_orchestrators", None)

        status_path = self.status_path(record.agent_id)
        if bool(snapshot.get("status_present")):
            encoded = snapshot.get("status_content")
            if isinstance(encoded, str):
                _atomic_write_bytes(status_path, base64.b64decode(encoded))
        else:
            status_path.unlink(missing_ok=True)
        if not snapshot.get("registry_file_present", True) and not registry:
            self.paths.registry_path.unlink(missing_ok=True)
        else:
            self._write_registry(registry)
        self.command_log.replace_projection(
            record.agent_id,
            {record.agent_id: registry.get(record.agent_id)}
            if record.agent_id in registry
            else {},
        )

    def _abort_uncommitted_starts(self) -> None:
        """Abort fresh starts that were published before provider commit."""

        self.abort_uncommitted_starts()

    def abort_uncommitted_starts(self) -> list[str]:
        """Abort safe uncommitted starts and return the removed run ids.

        Skips runs whose adapter this supervisor owns — such runs are between
        create() and commit_start() in an in-flight local launch. Aborting one
        of those would race the launching coroutine, terminating the verified
        provider group and deleting the run directory before commit_start().
        A restarted supervisor's set is empty, so the constructor pass still
        cleans up starts that crashed before commit.
        """

        aborted: list[str] = []
        with self._lock:
            for path in sorted(self.paths.runs_dir.glob("*/run.json")):
                try:
                    value = _read_json(path)
                    if not isinstance(value, dict):
                        continue
                    record = RunRecord.from_dict(value)
                    if not record.start_transaction:
                        continue
                    if self._try_abort_uncommitted_start_locked(record):
                        aborted.append(record.run_id)
                except (OSError, StoreError, TypeError, ValueError):
                    # Leave damaged metadata for the normal inspector path.
                    continue
        return aborted

    def abort_uncommitted_start(self, run_id: str) -> bool:
        """Abort one uncommitted start; retry recovery when the PID exits."""

        with self._lock:
            try:
                record = self.get(run_id)
            except RunNotFound:
                return False
            if not record.start_transaction:
                return False
            try:
                return self._try_abort_uncommitted_start_locked(record)
            except (OSError, StoreError, TypeError, ValueError):
                return False

    def _try_abort_uncommitted_start_locked(self, record: RunRecord) -> bool:
        if record.run_id in self._control_attached_run_ids:
            return False
        if record.provider_pid is None:
            record = self.discover_provider_process(record.run_id)
        if not self._terminate_recorded_provider_pid(record):
            return False
        self._restore_start_snapshot(record, record.start_transaction)
        if record.start_request_id:
            self.command_log.remove_start_request(record.start_request_id)
        self._start_registry_snapshots.pop(record.run_id, None)
        shutil.rmtree(self.run_dir(record.run_id), ignore_errors=True)
        return True

    @staticmethod
    def _terminate_recorded_provider_pid(
        record: RunRecord,
        *,
        allow_dead_without_identity: bool = True,
    ) -> bool:
        """Verify and stop a provider before deleting its uncommitted run."""

        if record.provider_pid is None or record.provider_pid <= 1:
            return True
        pid_dead = False
        try:
            os.kill(record.provider_pid, 0)
        except ProcessLookupError:
            # A dead child needs no identity. This also handles a crash before
            # process inspection persisted the executable and start time.
            pid_dead = True
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                pid_dead = True
            # Permission errors and all other failures are live/uncertain.
        if pid_dead and allow_dead_without_identity:
            return True
        if (
            record.provider_pid_started_at is None
            or not record.provider_executable
            or record.provider_process_group_id is None
        ):
            # A numeric PID without an identity is unsafe after a restart.
            return False
        return terminate_verified_provider_group(
            pid=record.provider_pid,
            created_at=record.provider_pid_started_at,
            executable=record.provider_executable,
            process_group_id=record.provider_process_group_id,
            group_members=record.provider_process_group_members,
            run_id=record.run_id,
            agent_id=record.agent_id,
        )

    def legacy_codex_agent_ids(self) -> list[str]:
        """Return tmux-era Codex currents, failing closed on corrupt entries."""

        with self._lock:
            registry = self._read_registry()
            legacy: list[str] = []
            for agent_id, entry in registry.items():
                if agent_id.startswith("_"):
                    continue
                if not isinstance(entry, dict):
                    raise StoreError(f"registry entry for {agent_id} must be an object")
                current = entry.get("current")
                if current is None:
                    continue
                if not isinstance(current, dict):
                    raise StoreError(
                        f"registry current entry for {agent_id} must be an object"
                    )
                kind = current.get("kind")
                run_id = current.get("run_id")
                if kind is not None and not isinstance(kind, str):
                    raise StoreError(f"registry kind for {agent_id} must be a string")
                if run_id is not None and not isinstance(run_id, str):
                    raise StoreError(f"registry run id for {agent_id} must be a string")
                if kind == "cdx" and not run_id:
                    legacy.append(agent_id)
            return sorted(legacy)

    def _registry_current(self, record: RunRecord) -> dict[str, Any]:
        value: dict[str, Any] = {
            "ticket": record.agent_id,
            "run_id": record.run_id,
            "provider": record.provider.value,
            "kind": record.kind,
            "role": record.role,
            "auto_archive": record.auto_archive,
            "model": record.model,
            "desired_model": record.desired_model,
            "effort": record.effort,
            "worktree": record.worktree,
            "cwd": record.worktree,
            "backend_base_url": record.backend_base_url,
            "orch": record.orchestrator_id,
            "state": record.state.value,
            "state_reason": record.state_reason,
            "recovery_from_state": (
                record.recovery_from_state.value if record.recovery_from_state else None
            ),
            "quiesce_operation_id": record.quiesce_operation_id,
            "quiesce_resume_state": (
                record.quiesce_resume_state.value
                if record.quiesce_resume_state
                else None
            ),
            "automatic_resume_suppressed": record.automatic_resume_suppressed,
            "automatic_resume_guarded_at": record.automatic_resume_guarded_at,
            "session_id": record.provider_session_id,
            "provider_session_id": record.provider_session_id,
            "provider_pid": record.provider_pid,
            "provider_pid_started_at": record.provider_pid_started_at,
            "provider_executable": record.provider_executable,
            "provider_process_group_id": record.provider_process_group_id,
            "provider_process_group_members": list(record.provider_process_group_members),
            "control_attached": record.run_id in self._control_attached_run_ids,
            "provider_generation": record.provider_generation,
            "active_turn_id": record.active_turn_id,
            "transcript": record.transcript_path,
            "log": str(self.raw_events_path(record.run_id)),
            # Transitional compatibility only. Headless liveness never reads it.
            "window": None,
            "spawned_at": record.created_at,
            "last_viewed_at": record.last_viewed_at,
            "last_viewed_seq": record.last_viewed_seq,
            "auto_archive_terminal_at": record.auto_archive_terminal_at,
            "auto_archive_verdict_seq": record.auto_archive_verdict_seq,
            "updated_at": record.updated_at,
            "start_request_id": record.start_request_id,
            # Projected so /api/agents can clear ticket-only legacy Codex
            # notices without needing a spawn replay (round 27 completion of
            # the round-26 migration marker). The value survives supervisor
            # restarts because it's persisted on the RunRecord and re-written
            # into the registry on every current-entry refresh.
            "replaced_legacy_provider": record.replaced_legacy_provider,
        }
        if record.execution_kind is not None and record.wk_lane is not None:
            value["lane"] = record.wk_lane
        return value

    def _write_record(self, record: RunRecord) -> None:
        record.updated_at = utc_now()
        _atomic_write_json(self.run_path(record.run_id), record.to_dict())

    def _write_current_turn_diff_snapshot(self, run_id: str, diff: str | None) -> None:
        _atomic_write_json(self.current_turn_diff_path(run_id), {"diff": diff})

    def _read_current_turn_diff_snapshot(self, run_id: str) -> str | None:
        try:
            value = _read_json(self.current_turn_diff_path(run_id))
        except RunNotFound:
            return None
        if not isinstance(value, dict):
            return None
        diff = value.get("diff")
        return diff if isinstance(diff, str) else None

    def _create_run_files(self, record: RunRecord) -> None:
        directory = self.run_dir(record.run_id)
        if directory.exists():
            raise StoreConflict(f"run already exists: {record.run_id}")
        staging_dir = self.paths.runs_dir / ".staging"
        staging_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(prefix=".run-", dir=staging_dir))
        try:
            temp_dir.chmod(0o700)
            for name in ("raw.jsonl", "events.jsonl"):
                fd = os.open(temp_dir / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
            record.updated_at = utc_now()
            _atomic_write_json(temp_dir / "run.json", record.to_dict())
            # Publish only after every run file is complete and durable.
            os.replace(temp_dir, directory)
            _fsync_directory(self.paths.runs_dir)
        except BaseException:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

    def _archive_terminal_run(self, record: RunRecord) -> None:
        """Move one non-current terminal run out of the boot hot path."""

        registry = self._read_registry()
        entry = registry.get(record.agent_id)
        current = entry.get("current") if isinstance(entry, dict) else None
        if isinstance(current, dict) and current.get("run_id") == record.run_id:
            self.archive_current(record.run_id, outcome=record.outcome)
            return
        archived_entry = self._find_archived_run_entry(record.run_id)
        if (
            archived_entry is not None
            and archived_entry[0].created_at == record.created_at
            and archive_is_committed(archived_entry[1])
        ):
            # A prior boot published a complete archive before cleanup. Finish
            # only the redundant hot-store removal and keep the archive intact.
            shutil.rmtree(self.run_dir(record.run_id))
            return
        session_dir = self._next_archive_session_dir(record.agent_id)
        ended_at = record.updated_at
        history = [
            dict(item)
            for item in (entry.get("history") if isinstance(entry, dict) else []) or []
            if isinstance(item, dict) and item.get("run_id") != record.run_id
        ]
        archive_worker = self._registry_current(record)
        archive_worker["ended_at"] = ended_at
        if record.outcome is not None:
            archive_worker["outcome"] = record.outcome
        expected_paths = [session_dir / "run.json", session_dir / "meta.json"]
        _atomic_write_json(session_dir / "run.json", record.to_dict())
        _atomic_write_json(
            session_dir / "meta.json",
            {
                "outcome": record.outcome,
                "ended_at": ended_at,
                "worker": archive_worker,
                "history": history,
                "source": "headless-supervisor",
            },
        )
        for source, destination in (
            (
                self.raw_events_path(record.run_id),
                session_dir / f"{record.provider.legacy_kind}-{record.agent_id}.log",
            ),
            (self.raw_events_path(record.run_id), session_dir / "raw.jsonl"),
            (
                self.normalized_events_path(record.run_id),
                session_dir / "events.jsonl",
            ),
            (
                self.current_turn_diff_path(record.run_id),
                session_dir / "current-turn-diff.json",
            ),
            (self.provider_log_path(record.run_id), session_dir / "provider.log"),
        ):
            if destination.name == "events.jsonl" and self._export_archive_events(
                record.run_id,
                source,
                destination,
            ):
                expected_paths.append(destination)
                continue
            if source.is_file():
                expected_paths.append(destination)
            self._copy_archive_file(source, destination)
        if record.initial_prompt:
            prompt_path = session_dir / f"{record.provider.legacy_kind}-{record.agent_id}-prompt.md"
            expected_paths.append(prompt_path)
            _atomic_write_bytes(prompt_path, record.initial_prompt.encode("utf-8"))
        artifact_dir = self.run_dir(record.run_id) / "artifacts"
        if artifact_dir.is_symlink():
            raise StoreError(f"refusing symlink artifact directory: {artifact_dir}")
        if artifact_dir.is_dir():
            expected_paths.extend(
                _archive_tree_expected_paths(artifact_dir, session_dir / "artifacts")
            )
            self._copy_archive_tree(artifact_dir, session_dir / "artifacts")
        commit_archive(
            session_dir,
            run_id=record.run_id,
            completed_at=ended_at,
            expected_paths=expected_paths,
        )
        if not archive_is_committed(session_dir):
            raise StoreError("archive commit failed verification")
        knowledge.enqueue_refresh(runtime_dir=self.paths.runtime_dir)
        if isinstance(entry, dict):
            row = dict(archive_worker)
            row.update(
                {
                    "outcome": record.outcome,
                    "ended_at": ended_at,
                    "replaced_by_run_id": record.replaced_by_run_id,
                }
            )
            history.append(row)
            registry[record.agent_id] = {
                "history": history,
                "current": current,
            }
            self._write_registry(registry)
            self.command_log.replace_projection(
                record.agent_id,
                {record.agent_id: registry[record.agent_id]},
            )
        shutil.rmtree(self.run_dir(record.run_id))
        if self.run_dir(record.run_id).exists():
            raise StoreConflict("pruned terminal run remains in the hot store")

    def _prune_terminal_runs(self) -> dict[str, int]:
        """Archive terminal runs older than the configured hot-store window."""

        cutoff = _run_retention_cutoff()
        pruned = 0
        failed_prunes = 0
        for snapshot in self.list_runs():
            if snapshot.state not in TERMINAL_STATES or not _run_is_older_than(
                snapshot.updated_at, cutoff
            ):
                continue
            try:
                record = self.get(snapshot.run_id)
                if record.state not in TERMINAL_STATES:
                    continue
                if (
                    self._terminal_run_prune_guard is not None
                    and not self._terminal_run_prune_guard(record)
                ):
                    continue
                if any(
                    effect.get("run_id") == record.run_id
                    for effect in self.command_log.sending_steer_effects()
                ):
                    continue
                self._archive_terminal_run(record)
                pruned += 1
            except (OSError, StoreError, TypeError, ValueError):
                # Keep a terminal run intact if archive preparation fails.
                failed_prunes += 1
                logger.exception(
                    "terminal run prune failed: run_id=%s", snapshot.run_id
                )
        return {"pruned": pruned, "failed_prunes": failed_prunes}

    def _reconcile_existing_runs(self) -> None:
        """Repair event counters after a crash between JSONL fsync and run.json.

        Delegates the order-sensitive projection walk to
        ``rebuild_projections_from_normalized`` so a boot after
        orphan-raw recovery sees projections rebuilt in raw provider
        order rather than JSONL append order (WIKI-232 REVIEW11 H1).
        """

        for path in sorted(self.paths.runs_dir.glob("*/run.json")):
            try:
                value = _read_json(path)
                if not isinstance(value, dict):
                    continue
                record = RunRecord.from_dict(value)
                if (
                    record.state in TERMINAL_STATES
                    and _run_is_older_than(record.updated_at, _run_retention_cutoff())
                ):
                    continue
                if record.start_request_id and record.start_transaction is None:
                    self.command_log.register_start_request(
                        record.start_request_id,
                        record.agent_id,
                        record.run_id,
                        implicit=record.implicit_start_request,
                        committed=True,
                    )
                raw_path = self.raw_events_path(record.run_id)
                normalized_path = self.normalized_events_path(record.run_id)
                _repair_jsonl_tail(raw_path)
                _repair_jsonl_tail(normalized_path)
                # WIKI-243: stream both logs instead of materializing full
                # dict lists. Startup used to pull every run's raw +
                # normalized JSONL into RAM just to compute max(seq)
                # (and, for legacy runs, one legacy_boundary), which
                # peaked backend RSS on repos with hundreds of runs.
                raw_count = 0
                for event in self._iter_json_lines(raw_path):
                    seq = int(event.get("seq", 0))
                    if seq > raw_count:
                        raw_count = seq
                # WIKI-232 REVIEW12 H1: legacy snapshots (schema_version < 2)
                # were written before ``last_causal_raw_seq`` existed, so
                # ``from_dict`` defaults it to 0. Left unseeded, the
                # rebuild below would treat every historical lifecycle
                # row as "not yet applied" and replay it — a run that
                # was persisted as BLOCKED (via ``store.transition`` or
                # ``mark_automatic_resume_failed``) after a prior
                # ``turn/completed`` -> IDLE and ``turn/started`` ->
                # WORKING would get its BLOCKED, ``state_reason``, and
                # ``recovery_from_state`` wiped when the walk re-applied
                # the pre-BLOCKED lifecycle history. Derive the boundary
                # from the legacy ``last_lifecycle_event_seq`` marker
                # (still persisted in the old schema) by mapping it to
                # the max raw_seq of any normalized event at or below
                # that normalized checkpoint. Anything past the boundary
                # is genuinely new and still applies.
                need_legacy_boundary = (
                    "last_causal_raw_seq" not in value
                    and record.last_lifecycle_event_seq > 0
                    and record.last_causal_raw_seq == 0
                )
                normalized_count = 0
                legacy_boundary = 0
                for event in self._iter_json_lines(normalized_path):
                    seq = int(event.get("seq", 0))
                    if seq > normalized_count:
                        normalized_count = seq
                    if (
                        need_legacy_boundary
                        and seq <= record.last_lifecycle_event_seq
                    ):
                        raw_seq = int(event.get("raw_seq", 0))
                        if raw_seq > legacy_boundary:
                            legacy_boundary = raw_seq
                record_dirty = False
                if (
                    record.raw_event_count != raw_count
                    or record.normalized_event_count != normalized_count
                ):
                    record.raw_event_count = raw_count
                    record.normalized_event_count = normalized_count
                    record_dirty = True
                if need_legacy_boundary and legacy_boundary > 0:
                    record.last_causal_raw_seq = legacy_boundary
                    record_dirty = True
                if record_dirty:
                    self._write_record(record)
                # Rebuild every event-derived projection from scratch in
                # raw_seq order so a post-recovery boot cannot resurrect
                # superseded lifecycle / pending_request / current_turn_diff
                # state from the JSONL append order (WIKI-232 REVIEW11 H1).
                self.rebuild_projections_from_normalized(record.run_id)
            except (OSError, StoreError, TypeError, ValueError):
                # A corrupt run remains on disk for the inspector; one bad run
                # must not prevent the daemon from recovering healthy siblings.
                continue

    def _reconcile_registry_from_runs(self) -> None:
        """Rebuild runtime-owned registry entries after cross-file crashes.

        Per-run metadata is the supervisor authority. The compatibility
        registry is a projection for the legacy UI during migration, so a
        crash between their separate atomic writes is repaired here. Entries
        and history rows not owned by this runtime are preserved verbatim.
        """

        registry = self._read_registry()
        changed = False
        for agent_id, entry in list(registry.items()):
            if agent_id.startswith("_") or not isinstance(entry, dict):
                continue
            current = entry.get("current")
            if not isinstance(current, dict):
                continue
            run_id = current.get("run_id")
            if not isinstance(run_id, str):
                continue
            if self.run_path(run_id).is_file():
                continue
            # Archive finalization deletes the runtime run dir first, then
            # drops the registry row. If the daemon stops between those steps,
            # the missing run file is the durable signal that the current row
            # must not survive restart.
            registry.pop(agent_id, None)
            changed = True

        records_by_agent: dict[str, list[RunRecord]] = {}
        for record in self.list_runs():
            records_by_agent.setdefault(record.agent_id, []).append(record)
        archived_run_ids: set[str] = set()
        for session_dir in self.paths.archive_dir.glob("*/*"):
            try:
                if not archive_is_committed(session_dir):
                    continue
                value = _read_json(session_dir / "run.json")
            except (OSError, StoreError, TypeError, ValueError):
                continue
            if (
                isinstance(value, dict)
                and isinstance(value.get("run_id"), str)
            ):
                archived_run_ids.add(value["run_id"])
        if not records_by_agent:
            if changed:
                self._write_registry(registry)
            return
        for agent_id, records in records_by_agent.items():
            live_records = [
                record
                for record in records
                if record.run_id not in archived_run_ids
                and record.replaced_by_run_id not in archived_run_ids
            ]
            if not live_records:
                if agent_id in registry:
                    registry.pop(agent_id, None)
                    changed = True
                continue
            records = live_records
            records.sort(key=lambda item: (item.created_at, item.run_id))
            by_id = {record.run_id: record for record in records}
            child_by_parent = {
                record.replaces_run_id: record
                for record in records
                if record.replaces_run_id in by_id
            }
            heads = [
                record for record in records if record.run_id not in child_by_parent
            ]
            raw_entry = registry.get(agent_id)
            entry = raw_entry if isinstance(raw_entry, dict) else {}
            raw_current = entry.get("current")
            existing_current = raw_current if isinstance(raw_current, dict) else {}
            existing_current_id = existing_current.get("run_id")
            current = next(
                (record for record in heads if record.run_id == existing_current_id),
                None,
            )
            if current is None and len(heads) == 1:
                current = heads[0]
            if current is None:
                # Multiple unlinked heads are corruption, not a safe place to
                # guess. Preserve the entry so the inspector can surface it.
                continue

            for old_id, child in child_by_parent.items():
                old = by_id[old_id]
                if old.replaced_by_run_id == child.run_id and old.outcome == "handoff":
                    continue
                old.replaced_by_run_id = child.run_id
                old.outcome = "handoff"
                old.state_reason = "replaced"
                if old.state not in TERMINAL_STATES:
                    old.state = LifecycleState.COMPLETED
                self._write_record(old)

            runtime_ids = set(by_id)
            raw_history = entry.get("history")
            existing_history = raw_history if isinstance(raw_history, list) else []
            history_by_id = {
                item.get("run_id"): item
                for item in existing_history
                if isinstance(item, dict) and item.get("run_id") in runtime_ids
            }
            history = [
                item
                for item in existing_history
                if not isinstance(item, dict) or item.get("run_id") not in runtime_ids
            ]
            for record in records:
                if record.run_id == current.run_id:
                    continue
                child = child_by_parent.get(record.run_id)
                row = dict(
                    history_by_id.get(record.run_id) or self._registry_current(record)
                )
                row.update(
                    {
                        "outcome": record.outcome or ("handoff" if child else None),
                        "ended_at": row.get("ended_at") or record.updated_at,
                        "replaced_by_run_id": (
                            child.run_id if child else record.replaced_by_run_id
                        ),
                    }
                )
                history.append(row)

            legacy_orchestrators = registry.get("_orchestrators")
            legacy_orchestrator = (
                legacy_orchestrators.get(agent_id)
                if isinstance(legacy_orchestrators, dict)
                else None
            )
            if isinstance(legacy_orchestrator, dict) and not any(
                isinstance(item, dict)
                and item.get("migration") == "headless-supervisor"
                and item.get("role") == "orchestrator"
                for item in history
            ):
                history.append(
                    {
                        **legacy_orchestrator,
                        "ticket": agent_id,
                        "kind": legacy_orchestrator.get("kind") or "cc",
                        "role": legacy_orchestrator.get("role") or "orchestrator",
                        "worktree": legacy_orchestrator.get("worktree")
                        or legacy_orchestrator.get("cwd"),
                        "outcome": legacy_orchestrator.get("outcome") or "handoff",
                        "ended_at": legacy_orchestrator.get("ended_at") or current.created_at,
                        "migration": "headless-supervisor",
                    }
                )
            projected = {"history": history, "current": self._registry_current(current)}
            if registry.get(agent_id) != projected:
                registry[agent_id] = projected
                changed = True
            if isinstance(legacy_orchestrators, dict) and agent_id in legacy_orchestrators:
                # A run file may have reached disk immediately before a crash
                # stopped the legacy orchestrator projection from being removed.
                # Durable run metadata wins on restart; never expose two live
                # identities for the same agent id.
                legacy_orchestrators.pop(agent_id)
                if not legacy_orchestrators:
                    registry.pop("_orchestrators", None)
                changed = True
        if changed:
            self._write_registry(registry)

    def _next_archive_session_dir(self, agent_id: str) -> Path:
        archive_dir = self.paths.archive_dir
        archive_dir_existed = archive_dir.exists()
        _ensure_parent_dir(archive_dir)
        if not archive_dir_existed:
            _fsync_directory(archive_dir.parent)
        ticket_dir = self.archive_ticket_dir(agent_id)
        ticket_dir_existed = ticket_dir.exists()
        _ensure_parent_dir(ticket_dir)
        if not ticket_dir_existed:
            _fsync_directory(ticket_dir.parent)
        stamp = datetime.now().astimezone().replace(microsecond=0)
        while True:
            session_dir = ticket_dir / stamp.strftime("%Y%m%d-%H%M%S")
            if not session_dir.exists():
                session_dir.mkdir(mode=0o700, parents=True)
                session_dir.chmod(0o700)
                _fsync_directory(session_dir.parent)
                return session_dir
            stamp += timedelta(seconds=1)

    def _copy_archive_file(self, source: Path, destination: Path) -> None:
        if not source.is_file():
            return
        _ensure_parent_dir(destination.parent)
        shutil.copy2(source, destination)
        destination.chmod(0o600)
        _fsync_file(destination)
        _fsync_directory(destination.parent)

    def _copy_archive_tree(self, source: Path, destination: Path) -> None:
        """Copy an artifact tree while making every entry durable."""

        _ensure_parent_dir(destination.parent)
        if destination.exists() or destination.is_symlink():
            raise StoreConflict(f"archive destination already exists: {destination}")
        destination.mkdir(mode=0o700)
        destination.chmod(0o700)
        _fsync_directory(destination.parent)
        try:
            with os.scandir(source) as entries:
                for entry in entries:
                    source_entry = Path(entry.path)
                    destination_entry = destination / entry.name
                    if entry.is_symlink():
                        destination_entry.symlink_to(
                            os.readlink(source_entry),
                            target_is_directory=entry.is_dir(follow_symlinks=True),
                        )
                        _fsync_directory(destination)
                    elif entry.is_dir(follow_symlinks=False):
                        self._copy_archive_tree(source_entry, destination_entry)
                    elif entry.is_file(follow_symlinks=False):
                        self._copy_archive_file(source_entry, destination_entry)
                    else:
                        raise StoreError(f"refusing special artifact: {source_entry}")
            _fsync_directory(destination)
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise

    def archive_current(
        self,
        run_id: str,
        *,
        outcome: str | None = None,
    ) -> tuple[RunRecord, Path]:
        with self._lock:
            record = self.get(run_id)
            archived_entry = self._find_archived_run_entry(run_id)
            if (
                archived_entry is not None
                and archived_entry[0].created_at != record.created_at
            ):
                raise StoreConflict(
                    "older archive marker cannot remove a newer live run"
                )
            if archived_entry is not None:
                archived = self.finalize_archived_run(run_id)
                if archived is None:
                    raise StoreConflict("committed archive could not be finalized")
                return archived, archived_entry[1]
            registry = self._read_registry()
            entry = registry.get(record.agent_id)
            current = entry.get("current") if isinstance(entry, dict) else None
            if not isinstance(current, dict) or current.get("run_id") != run_id:
                raise StoreConflict("archive target is no longer current")
            if record.state not in TERMINAL_STATES:
                raise StoreConflict("archive target must be terminal before finalization")

            ended_at = utc_now()
            if outcome is not None:
                record.outcome = outcome
            record.updated_at = ended_at
            entry_dict = entry if isinstance(entry, dict) else {}
            history = [
                dict(item)
                for item in (entry_dict.get("history") or [])
                if isinstance(item, dict)
            ]
            session_dir = self._next_archive_session_dir(record.agent_id)
            log_name = f"{record.provider.legacy_kind}-{record.agent_id}.log"
            prompt_name = f"{record.provider.legacy_kind}-{record.agent_id}-prompt.md"
            archive_worker = {**current, "ended_at": ended_at}
            if record.outcome is not None:
                archive_worker["outcome"] = record.outcome
            expected_paths = [session_dir / "run.json", session_dir / "meta.json"]
            _atomic_write_json(session_dir / "run.json", record.to_dict())
            _atomic_write_json(
                session_dir / "meta.json",
                {
                    "outcome": record.outcome,
                    "ended_at": ended_at,
                    "worker": archive_worker,
                    "history": history,
                    "source": "headless-supervisor",
                },
            )
            for source, destination in (
                (self.raw_events_path(run_id), session_dir / log_name),
                (self.raw_events_path(run_id), session_dir / "raw.jsonl"),
                (self.normalized_events_path(run_id), session_dir / "events.jsonl"),
                (
                    self.current_turn_diff_path(run_id),
                    session_dir / "current-turn-diff.json",
                ),
            ):
                if destination.name == "events.jsonl" and self._export_archive_events(
                    run_id,
                    source,
                    destination,
                ):
                    expected_paths.append(destination)
                    continue
                if source.is_file():
                    expected_paths.append(destination)
                self._copy_archive_file(source, destination)
            # The normalized transcript is durable before the background index
            # hook is enqueued. A cache failure can never block archive
            # finalization or provider cleanup.
            knowledge.enqueue_refresh(runtime_dir=self.paths.runtime_dir)
            provider_log = self.provider_log_path(run_id)
            if provider_log.is_file():
                expected_paths.append(session_dir / "provider.log")
            self._copy_archive_file(provider_log, session_dir / "provider.log")
            status_path_to_remove: Path | None = None
            if record.initial_prompt:
                prompt_path = session_dir / prompt_name
                expected_paths.append(prompt_path)
                _atomic_write_bytes(prompt_path, record.initial_prompt.encode("utf-8"))
            status_path = self.status_path(record.agent_id)
            if status_path.is_file():
                status_path_to_remove = status_path
                try:
                    status = json.loads(status_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    status = None
                if isinstance(status, dict):
                    final_status_path = session_dir / "final-status.json"
                    expected_paths.append(final_status_path)
                    _atomic_write_json(final_status_path, status)

            artifact_dir = self.run_dir(run_id) / "artifacts"
            if artifact_dir.is_symlink():
                raise StoreError(f"refusing symlink artifact directory: {artifact_dir}")
            if artifact_dir.is_dir():
                expected_paths.extend(
                    _archive_tree_expected_paths(artifact_dir, session_dir / "artifacts")
                )
                self._copy_archive_tree(artifact_dir, session_dir / "artifacts")

            # Publish the archive only after every file is complete. Telemetry
            # scans sessions with this marker and never observes a copy in
            # progress.
            commit_archive(
                session_dir,
                run_id=run_id,
                completed_at=ended_at,
                expected_paths=expected_paths,
            )
            if not archive_is_committed(session_dir):
                raise StoreError("archive commit failed verification")

            if record.start_request_id and record.implicit_start_request:
                self.command_log.archive_start_request(
                    record.start_request_id,
                    run_id,
                    str(session_dir),
                )
            # The marker is the durable commit point. Remove every live
            # source only after it is durable.
            if status_path_to_remove is not None:
                status_path_to_remove.unlink(missing_ok=True)
            shutil.rmtree(self.run_dir(run_id))
            self.command_log.forget_implicit_for_run(run_id)
            registry.pop(record.agent_id, None)
            self._write_registry(registry)
            self.command_log.replace_projection(record.agent_id, {})
            return record, session_dir

    def create(
        self,
        record: RunRecord,
        *,
        migrate_legacy: bool = False,
        transactional_start: bool = False,
    ) -> RunRecord:
        with self._lock:
            registry = self._read_registry()
            entry = registry.get(record.agent_id)
            current = entry.get("current") if isinstance(entry, dict) else None
            legacy_orchestrators = registry.get("_orchestrators")
            if legacy_orchestrators is not None and not isinstance(
                legacy_orchestrators, dict
            ):
                raise StoreError("legacy orchestrator registry must be an object")
            legacy_orchestrator = (
                legacy_orchestrators.get(record.agent_id)
                if legacy_orchestrators is not None
                else None
            )
            if isinstance(current, dict) and current.get("run_id"):
                raise StoreConflict(
                    f"agent already has a current run: {record.agent_id}"
                )
            if isinstance(current, dict) and isinstance(legacy_orchestrator, dict):
                raise StoreConflict(
                    f"agent id has ambiguous legacy registrations: {record.agent_id}"
                )
            if isinstance(current, dict) and not migrate_legacy:
                raise StoreConflict(
                    f"agent has a legacy current run requiring explicit migration: {record.agent_id}"
                )
            if isinstance(legacy_orchestrator, dict) and not migrate_legacy:
                raise StoreConflict(
                    "agent has a legacy orchestrator registration requiring "
                    f"explicit migration: {record.agent_id}"
                )
            # A status file belongs to the run that creates it. Capture the
            # old status and registry before publishing any start side effect.
            # The supervisor calls create() while holding the per-agent lock.
            status_path = self.status_path(record.agent_id)
            _ensure_parent_dir(status_path.parent)
            status_present, status_content = _read_start_status(status_path)
            registry_before = deepcopy(registry)
            registry_was_present = self.paths.registry_path.exists()
            legacy_orchestrators = registry.get("_orchestrators")
            legacy_entry = (
                legacy_orchestrators.get(record.agent_id)
                if isinstance(legacy_orchestrators, dict)
                else None
            )
            # WIKI-219 owns durable snapshots and journal-before-side-effect
            # recovery across supervisor exits.
            start_snapshot = {
                "version": 1,
                "agent_present": record.agent_id in registry,
                "agent_entry": deepcopy(registry.get(record.agent_id)),
                "legacy_present": isinstance(legacy_orchestrators, dict)
                and record.agent_id in legacy_orchestrators,
                "legacy_entry": deepcopy(legacy_entry),
                "status_present": status_present,
                "status_content": (
                    base64.b64encode(status_content).decode("ascii")
                    if status_content is not None
                    else None
                ),
                "registry_file_present": registry_was_present,
            }
            if transactional_start:
                record.start_transaction = start_snapshot
            self._start_registry_snapshots[record.run_id] = {
                "agent_present": record.agent_id in registry,
                "agent_entry": deepcopy(registry.get(record.agent_id)),
                "legacy_present": isinstance(legacy_orchestrators, dict)
                and record.agent_id in legacy_orchestrators,
                "legacy_entry": deepcopy(legacy_entry),
                "status_present": status_present,
                "status_content": status_content,
            }
            # The replaced-legacy provider identity travels back on the record so
            # main.py's ticket-only Codex-notice cleanup can key off the legacy
            # kind (worker "current" or "_orchestrators" entry), not the new
            # provider. This is the whole point of the fix: a cdx-to-cc migration
            # replaces a Codex worker and must still clear its notice ticket.
            replaced_kind: str | None = None
            if isinstance(current, dict) and current:
                raw = current.get("kind")
                if isinstance(raw, str):
                    replaced_kind = raw
            if replaced_kind is None and isinstance(legacy_orchestrator, dict):
                raw = legacy_orchestrator.get("kind")
                if isinstance(raw, str):
                    replaced_kind = raw
                else:
                    # Match the history-archive default at ~line 1229: a legacy
                    # `_orchestrators` row without an explicit kind is treated
                    # as Claude, so a missing kind means the replaced identity
                    # is Claude (not "unknown"). Keeps the notice cleanup
                    # decision aligned with how the row is recorded.
                    replaced_kind = "cc"
            if replaced_kind == "cdx":
                record.replaced_legacy_provider = "codex"
            elif replaced_kind == "cc":
                record.replaced_legacy_provider = "claude"
            run_dir_was_absent = not self.run_dir(record.run_id).exists()
            try:
                # The run record contains the preimage and transaction marker.
                # It must reach disk before the previous status can disappear.
                self._create_run_files(record)
                status_path.unlink(missing_ok=True)
                if record.start_request_id:
                    self.command_log.register_start_request(
                        record.start_request_id,
                        record.agent_id,
                        record.run_id,
                        implicit=record.implicit_start_request,
                        committed=not transactional_start,
                    )
                history = (
                    list((entry or {}).get("history") or [])
                    if isinstance(entry, dict)
                    else []
                )
                if isinstance(current, dict) and current:
                    # Mixed-fleet migration: the backend refuses a still-live
                    # legacy window before calling create(). A stale tmux-era
                    # current row is archived once, then the supervisor becomes
                    # the sole registry writer for this agent.
                    history.append(
                        {
                            **current,
                            "outcome": current.get("outcome") or "handoff",
                            "ended_at": current.get("ended_at") or utc_now(),
                            "migration": "headless-supervisor",
                        }
                    )
                if isinstance(legacy_orchestrator, dict):
                    history.append(
                        {
                            **legacy_orchestrator,
                            "ticket": record.agent_id,
                            "kind": legacy_orchestrator.get("kind") or "cc",
                            "role": legacy_orchestrator.get("role") or "orchestrator",
                            "worktree": legacy_orchestrator.get("worktree")
                            or legacy_orchestrator.get("cwd"),
                            "outcome": legacy_orchestrator.get("outcome") or "handoff",
                            "ended_at": legacy_orchestrator.get("ended_at") or utc_now(),
                            "migration": "headless-supervisor",
                        }
                    )
                    legacy_orchestrators.pop(record.agent_id)
                    if not legacy_orchestrators:
                        registry.pop("_orchestrators", None)
                registry[record.agent_id] = {
                    "history": history,
                    "current": self._registry_current(record),
                }
                self._write_registry(registry)
            except BaseException:
                if record.start_request_id:
                    self.command_log.remove_start_request(record.start_request_id)
                self._start_registry_snapshots.pop(record.run_id, None)
                if run_dir_was_absent:
                    shutil.rmtree(self.run_dir(record.run_id), ignore_errors=True)
                if status_present:
                    _atomic_write_bytes(status_path, status_content)
                else:
                    status_path.unlink(missing_ok=True)
                if registry_was_present:
                    _atomic_write_json(self.paths.registry_path, registry_before)
                else:
                    self.paths.registry_path.unlink(missing_ok=True)
                raise
            return record

    def commit_start(self, run_id: str) -> None:
        """Commit a start and remove its durable pre-start transaction marker."""

        with self._lock:
            self._start_registry_snapshots.pop(run_id, None)
            record = self.get(run_id)
            if record.start_transaction is not None:
                record.start_transaction = None
                self._write_record(record)
            if record.start_request_id:
                self.command_log.commit_start_request(record.start_request_id)

    def abort_start(self, run_id: str, *, reason: str) -> None:
        """Remove a failed start and restore the registry before that start."""

        del reason  # The failed run is rolled back instead of persisted.
        with self._lock:
            record = self.get(run_id)
            snapshot = self._start_registry_snapshots.pop(run_id, None)
            durable_snapshot = record.start_transaction
            if durable_snapshot is None and snapshot is not None:
                durable_snapshot = {
                    "agent_present": snapshot["agent_present"],
                    "agent_entry": snapshot["agent_entry"],
                    "legacy_present": snapshot["legacy_present"],
                    "legacy_entry": snapshot["legacy_entry"],
                    "status_present": snapshot["status_present"],
                    "status_content": (
                        base64.b64encode(snapshot["status_content"]).decode("ascii")
                        if snapshot["status_content"] is not None
                        else None
                    ),
                }
            self._restore_start_snapshot(record, durable_snapshot)
            self._control_attached_run_ids.discard(run_id)
            if record.start_request_id:
                self.command_log.remove_start_request(record.start_request_id)
            shutil.rmtree(self.run_dir(run_id))

    def get(self, run_id: str) -> RunRecord:
        with self._lock:
            value = _read_json(self.run_path(run_id))
            if not isinstance(value, dict):
                raise StoreError(f"run metadata is not an object: {run_id}")
            return RunRecord.from_dict(value)

    def list_runs(self) -> list[RunRecord]:
        with self._lock:
            records: list[RunRecord] = []
            for path in sorted(self.paths.runs_dir.glob("*/run.json")):
                try:
                    value = _read_json(path)
                    if isinstance(value, dict):
                        records.append(RunRecord.from_dict(value))
                except StoreError:
                    continue
            return records

    def current_run_id(self, agent_id: str) -> str | None:
        with self._lock:
            current = (self._read_registry().get(agent_id) or {}).get("current") or {}
            run_id = current.get("run_id")
            return run_id if isinstance(run_id, str) else None

    def is_current(self, record: RunRecord) -> bool:
        return self.current_run_id(record.agent_id) == record.run_id

    def set_control_attached(self, run_id: str, attached: bool) -> RunRecord:
        """Project this supervisor's adapter ownership without persisting it in a run."""

        with self._lock:
            record = self.get(run_id)
            if attached:
                self._control_attached_run_ids.add(run_id)
            else:
                self._control_attached_run_ids.discard(run_id)
            registry = self._read_registry()
            current = (registry.get(record.agent_id) or {}).get("current") or {}
            if current.get("run_id") == run_id:
                registry[record.agent_id]["current"] = self._registry_current(record)
                self._write_registry(registry)
            return record

    def transition(
        self,
        run_id: str,
        target: LifecycleState,
        *,
        reason: str | None = None,
        adapter_status: AdapterStatus | None = None,
        guard_automatic_resume: bool = False,
    ) -> RunRecord:
        with self._lock:
            record = self.get(run_id)
            if (
                record.state is target
                and record.state_reason == reason
                and adapter_status is None
            ):
                return record
            validate_transition(record.state, target)
            record.state = target
            record.state_reason = reason
            if (
                target is not LifecycleState.BLOCKED
                and record.quiesce_operation_id is None
            ):
                record.recovery_from_state = None
            if target in TERMINAL_STATES:
                record.pending_requests.clear()
            if adapter_status is not None:
                if adapter_status.session_id is not None:
                    record.provider_session_id = adapter_status.session_id
                record.provider_pid = adapter_status.pid
                if adapter_status.pid is None:
                    record.provider_pid_started_at = None
                    record.provider_executable = None
                    record.provider_process_group_id = None
                    record.provider_process_group_members = []
                else:
                    identity = provider_process_status_sync(adapter_status.pid)
                    if identity is None or identity.executable is None:
                        record.provider_pid_started_at = None
                        record.provider_executable = None
                        record.provider_process_group_id = None
                        record.provider_process_group_members = []
                    else:
                        record.provider_pid_started_at = identity.created_at
                        record.provider_executable = identity.executable
                        record.provider_process_group_id = identity.process_group_id
                        record.provider_process_group_members = (
                            provider_process_group_members_sync(identity.process_group_id)
                        )
                record.provider_generation = adapter_status.generation
                record.active_turn_id = adapter_status.active_turn_id
                if adapter_status.transcript_path is not None:
                    record.transcript_path = adapter_status.transcript_path
                if adapter_status.detail and reason is None:
                    record.state_reason = adapter_status.detail
            if guard_automatic_resume:
                record.automatic_resume_suppressed = True
                record.automatic_resume_guarded_at = utc_now()
            self._write_record(record)
            registry = self._read_registry()
            current = (registry.get(record.agent_id) or {}).get("current") or {}
            if current.get("run_id") == record.run_id:
                registry[record.agent_id]["current"] = self._registry_current(record)
                self._write_registry(registry)
                self.command_log.replace_projection(
                    record.agent_id,
                    {record.agent_id: registry[record.agent_id]},
                )
            return record

    def mark_recovery_blocked(self, run_id: str, *, reason: str) -> RunRecord:
        """Block an unowned live PID while preserving automatic-resume intent."""

        with self._lock:
            record = self.get(run_id)
            recovery_state = record.recovery_from_state or record.state
            if recovery_state not in {
                LifecycleState.WORKING,
                LifecycleState.WAITING_APPROVAL,
                LifecycleState.IDLE,
            }:
                raise StoreConflict(
                    f"state {recovery_state.value} is not eligible for recovery polling"
                )
            if (
                record.state is LifecycleState.BLOCKED
                and record.recovery_from_state is recovery_state
                and record.state_reason == reason
            ):
                return record
            validate_transition(record.state, LifecycleState.BLOCKED)
            record.state = LifecycleState.BLOCKED
            record.state_reason = reason
            record.recovery_from_state = recovery_state
            record.automatic_resume_suppressed = False
            record.automatic_resume_guarded_at = None
            self._write_record(record)
            registry = self._read_registry()
            current = (registry.get(record.agent_id) or {}).get("current") or {}
            if current.get("run_id") == record.run_id:
                registry[record.agent_id]["current"] = self._registry_current(record)
                self._write_registry(registry)
                self.command_log.replace_projection(
                    record.agent_id,
                    {record.agent_id: registry[record.agent_id]},
                )
            return record

    def mark_automatic_resume_failed(
        self,
        run_id: str,
        *,
        reason: str,
        recovery_state: LifecycleState,
    ) -> RunRecord:
        """Suppress crash-loop retries while preserving explicit resume intent."""

        with self._lock:
            record = self.get(run_id)
            if recovery_state not in {
                LifecycleState.WORKING,
                LifecycleState.WAITING_APPROVAL,
                LifecycleState.IDLE,
            }:
                raise StoreConflict(
                    f"state {recovery_state.value} has no resumable recovery intent"
                )
            validate_transition(record.state, LifecycleState.BLOCKED)
            record.state = LifecycleState.BLOCKED
            record.state_reason = reason
            record.recovery_from_state = recovery_state
            record.automatic_resume_suppressed = True
            record.automatic_resume_guarded_at = None
            self._write_record(record)
            registry = self._read_registry()
            current = (registry.get(record.agent_id) or {}).get("current") or {}
            if current.get("run_id") == record.run_id:
                registry[record.agent_id]["current"] = self._registry_current(record)
                self._write_registry(registry)
            return record

    def clear_automatic_resume_suppression(self, run_id: str) -> RunRecord:
        with self._lock:
            record = self.get(run_id)
            if record.quiesce_operation_id is not None:
                raise StoreConflict(
                    "quiesce marker must be cleared after controlled resume"
                )
            if not record.automatic_resume_suppressed:
                return record
            record.automatic_resume_suppressed = False
            record.automatic_resume_guarded_at = None
            self._write_record(record)
            registry = self._read_registry()
            current = (registry.get(record.agent_id) or {}).get("current") or {}
            if current.get("run_id") == record.run_id:
                registry[record.agent_id]["current"] = self._registry_current(record)
                self._write_registry(registry)
            return record

    def update_adapter_status(
        self,
        run_id: str,
        status: AdapterStatus,
        *,
        guard_automatic_resume: bool = False,
    ) -> RunRecord:
        return self.transition(
            run_id,
            status.state,
            reason=status.detail,
            adapter_status=status,
            guard_automatic_resume=guard_automatic_resume,
        )

    def mark_viewed(self, run_id: str, requested_seq: int | None = None) -> RunRecord:
        """Persist the monotonic event cursor owned by the supervisor."""

        with self._lock:
            record = self.get(run_id)
            current_seq = record.normalized_event_count
            accepted_seq = (
                current_seq
                if requested_seq is None
                else min(requested_seq, current_seq)
            )
            if accepted_seq < 0:
                raise ValueError("seq must be non-negative")
            if (
                record.last_viewed_seq is not None
                and record.last_viewed_seq >= accepted_seq
            ):
                return record
            record.last_viewed_seq = accepted_seq
            record.last_viewed_at = utc_now()
            self._write_record(record)
            registry = self._read_registry()
            current = (registry.get(record.agent_id) or {}).get("current") or {}
            if current.get("run_id") == record.run_id:
                registry[record.agent_id]["current"] = self._registry_current(record)
                self._write_registry(registry)
            return record

    def set_auto_archive_candidate(self, run_id: str, verdict_seq: int) -> RunRecord:
        """Record the first terminal verdict boundary for restart-safe sweeps."""

        with self._lock:
            record = self.get(run_id)
            if record.auto_archive_terminal_at is not None:
                return record
            record.auto_archive_terminal_at = utc_now()
            record.auto_archive_verdict_seq = max(0, verdict_seq)
            self._write_record(record)
            registry = self._read_registry()
            current = (registry.get(record.agent_id) or {}).get("current") or {}
            if current.get("run_id") == record.run_id:
                registry[record.agent_id]["current"] = self._registry_current(record)
                self._write_registry(registry)
            return record

    def record_provider_process_created(self, run_id: str, pid: int) -> RunRecord:
        """Persist process identity before provider startup can do more work."""

        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
            raise StoreConflict("provider process id is invalid")
        with self._lock:
            record = self.get(run_id)
            identity = provider_process_status_sync(pid)
            record.provider_pid = pid
            if identity is None or identity.executable is None:
                record.provider_pid_started_at = None
                record.provider_executable = None
                record.provider_process_group_id = None
                record.provider_process_group_members = []
            else:
                record.provider_pid_started_at = identity.created_at
                record.provider_executable = identity.executable
                record.provider_process_group_id = identity.process_group_id
                record.provider_process_group_members = (
                    provider_process_group_members_sync(identity.process_group_id)
                )
            self._write_record(record)
            registry = self._read_registry()
            current = (registry.get(record.agent_id) or {}).get("current") or {}
            if current.get("run_id") == record.run_id:
                registry[record.agent_id]["current"] = self._registry_current(record)
                self._write_registry(registry)
                self.command_log.replace_projection(
                    record.agent_id,
                    {record.agent_id: registry[record.agent_id]},
                )
            return record

    def finalize_handover_detach(
        self,
        run_id: str,
        *,
        state: LifecycleState,
        session_id: str,
        generation: int,
        transcript_path: str | None,
        pending_requests: dict[str, dict[str, Any]],
    ) -> RunRecord:
        """Commit the pre-stop recovery intent after provider events drain."""

        if state not in {
            LifecycleState.WORKING,
            LifecycleState.WAITING_APPROVAL,
            LifecycleState.IDLE,
        }:
            raise StoreConflict(f"handover state {state.value} is not resumable")
        if not session_id:
            raise StoreConflict("handover session id is missing")
        with self._lock:
            record = self.get(run_id)
            if record.replaced_by_run_id or not self.is_current(record):
                raise StoreConflict("handover target is no longer current")
            record.state = state
            record.state_reason = None
            record.recovery_from_state = None
            record.provider_session_id = session_id
            record.provider_pid = None
            record.provider_generation = generation
            record.active_turn_id = None
            record.transcript_path = transcript_path
            record.pending_requests = {
                key: dict(request) for key, request in pending_requests.items()
            }
            self._write_record(record)
            registry = self._read_registry()
            current = (registry.get(record.agent_id) or {}).get("current") or {}
            if current.get("run_id") == record.run_id:
                registry[record.agent_id]["current"] = self._registry_current(record)
                self._write_registry(registry)
            return record

    def set_desired_model(self, run_id: str, model: str | None) -> RunRecord:
        with self._lock:
            record = self.get(run_id)
            record.desired_model = model
            self._write_record(record)
            registry = self._read_registry()
            current = (registry.get(record.agent_id) or {}).get("current") or {}
            if current.get("run_id") == record.run_id:
                registry[record.agent_id]["current"] = self._registry_current(record)
                self._write_registry(registry)
            return record

    def append_raw(
        self,
        run_id: str,
        *,
        provider: str,
        direction: str,
        payload: dict[str, Any],
        generation: int = 1,
        received_at: str | None = None,
    ) -> dict[str, Any]:
        """Durably append raw provider input before normalization is attempted."""

        with self._lock:
            record = self.get(run_id)
            envelope = {
                "seq": record.raw_event_count + 1,
                "received_at": received_at or utc_now(),
                "provider": provider,
                "direction": direction,
                "generation": generation,
                "payload": payload,
            }
            _append_json_line(self.raw_events_path(run_id), envelope)
            # The fsync above is the ordering boundary: only now may callers
            # normalize the event or expose it to subscribers.
            record.raw_event_count = int(envelope["seq"])
            self._write_record(record)
            return envelope

    def append_normalized(
        self,
        run_id: str,
        *,
        raw_seq: int,
        disposition: EventDisposition,
        kind: str,
        payload: dict[str, Any],
        lifecycle_state: LifecycleState | None = None,
        normalized_at: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            record = self.get(run_id)
            if raw_seq < 1 or raw_seq > record.raw_event_count:
                raise StoreError(
                    f"normalized event references missing raw sequence {raw_seq}"
                )
            envelope = {
                "seq": record.normalized_event_count + 1,
                "raw_seq": raw_seq,
                "normalized_at": utc_now() if normalized_at is None else normalized_at,
                "disposition": disposition.value,
                "kind": kind,
                "payload": payload,
                "lifecycle_state": lifecycle_state.value if lifecycle_state else None,
            }
            _append_json_line(self.normalized_events_path(run_id), envelope)
            record.normalized_event_count = int(envelope["seq"])
            record.disposition_counts[disposition.value] = (
                record.disposition_counts.get(disposition.value, 0) + 1
            )
            # A stale-order recovery (``_normalize_orphan_raw_events``
            # replaying a raw row whose raw_seq sits strictly below the
            # max already applied) must not mutate any order-sensitive
            # projection. Applying it would let a raw_seq=1 orphan
            # approval re-add a pending_request that raw_seq=2
            # serverRequest/resolved already cleared, or flip an idle
            # run back to working after turn/completed already landed.
            # The suppression covers every later causal event, not just
            # lifecycle (WIKI-232 REVIEW11 H1). Same-raw_seq additional
            # normalized rows (one raw event can fan out into several
            # normalized surfaces — see unread-sequence tests) are
            # applied in normalized order so live and rebuild agree
            # (WIKI-232 REVIEW12 M2). The durable JSONL row and
            # disposition tally still land unconditionally so
            # observability and replay are complete.
            if raw_seq >= record.last_causal_raw_seq:
                if raw_seq > record.last_causal_raw_seq:
                    record.last_causal_raw_seq = raw_seq
                # Unread advances only on genuinely worker-authored surface
                # events (see _is_unread_worthy). Synthetic supervisor wakes,
                # outbound client_message rows, provider-lifecycle
                # boundaries, and user inbound echoes all leave the counter
                # alone.
                if _is_unread_worthy(kind, payload, disposition.value):
                    if int(envelope["seq"]) > record.unread_event_seq:
                        record.unread_event_seq = int(envelope["seq"])
                changed, snapshot = _current_turn_diff_update(record, envelope)
                if changed:
                    self._write_current_turn_diff_snapshot(run_id, snapshot)
                _apply_pending_request_event(
                    record,
                    kind=kind,
                    payload=payload,
                    raw_seq=raw_seq,
                    normalized_at=str(envelope["normalized_at"]),
                )
                _apply_composer_message_event(
                    record,
                    payload=payload,
                    seq=int(envelope["seq"]),
                    normalized_at=str(envelope["normalized_at"]),
                )
                if lifecycle_state is not None:
                    try:
                        validate_transition(record.state, lifecycle_state)
                    except ValueError:
                        # Late provider events cannot resurrect terminal/replaced
                        # runs, but the event remains durably accounted for.
                        pass
                    else:
                        record.state = lifecycle_state
                        record.state_reason = None
                        if (
                            lifecycle_state is not LifecycleState.BLOCKED
                            and record.quiesce_operation_id is None
                        ):
                            record.recovery_from_state = None
                if record.state in TERMINAL_STATES:
                    record.pending_requests.clear()
                record.last_lifecycle_event_seq = int(envelope["seq"])
            self._write_record(record)
            return envelope

    def current_turn_diff(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self.get(run_id)
            diff = self._read_current_turn_diff_snapshot(run_id)
            if record.provider is not ProviderKind.CODEX or diff is None:
                return None
            return {
                "turn_id": record.current_turn_diff_turn_id,
                "seq": record.current_turn_diff_seq,
                "diff": diff,
            }

    def clear_pending_request(
        self,
        run_id: str,
        request_id: str | int,
    ) -> RunRecord:
        with self._lock:
            record = self.get(run_id)
            record.pending_requests.pop(_provider_request_key(request_id), None)
            self._write_record(record)
            return record

    def clear_pending_request_key(self, run_id: str, key: str) -> RunRecord:
        """Clear one durable request by its canonical storage key."""
        with self._lock:
            record = self.get(run_id)
            record.pending_requests.pop(key, None)
            self._write_record(record)
            return record

    def clear_pending_request_by_tool_use_id(
        self,
        run_id: str,
        tool_use_id: str,
    ) -> RunRecord:
        with self._lock:
            record = self.get(run_id)
            to_remove = [
                key
                for key, request in record.pending_requests.items()
                for payload in [request.get("payload") if isinstance(request, dict) else None]
                for nested_request in [payload.get("request") if isinstance(payload, dict) else None]
                if isinstance(nested_request, dict)
                and nested_request.get("tool_use_id") == tool_use_id
            ]
            if not to_remove:
                return record
            for key in to_remove:
                record.pending_requests.pop(key, None)
            self._write_record(record)
            return record

    def clear_pending_requests(self, run_id: str) -> RunRecord:
        with self._lock:
            record = self.get(run_id)
            if not record.pending_requests:
                return record
            record.pending_requests.clear()
            self._write_record(record)
            return record

    def restore_pending_requests(
        self,
        run_id: str,
        pending_requests: dict[str, dict[str, Any]],
    ) -> RunRecord:
        """Restore approval requests after a failed replacement transport."""

        with self._lock:
            record = self.get(run_id)
            record.pending_requests = {
                str(key): dict(request) for key, request in pending_requests.items()
            }
            self._write_record(record)
            return record

    def mark_quiesce_intent(
        self,
        run_id: str,
        operation_id: str,
        expected_session_id: str,
        *,
        resume_state: LifecycleState | None = None,
    ) -> RunRecord:
        """Capture the exact current session and resumable state before stopping it."""

        _validated_operation_id(operation_id)
        if not expected_session_id:
            raise StoreError("expected provider session id must not be empty")
        with self._lock:
            record = self.get(run_id)
            if not self.is_current(record):
                raise StoreConflict("quiesce target is no longer current")
            if record.replaced_by_run_id:
                raise StoreConflict("quiesce target was replaced")
            if record.provider_session_id != expected_session_id:
                raise StoreConflict("provider session changed during quiesce preflight")
            if record.quiesce_operation_id is not None:
                if record.quiesce_operation_id == operation_id:
                    return record
                raise StoreConflict("run belongs to another quiesce operation")
            captured_state = resume_state or record.state
            if captured_state not in {LifecycleState.WORKING, LifecycleState.IDLE}:
                raise StoreConflict(
                    f"state {captured_state.value} cannot be quiesced for exact-session resume"
                )
            if resume_state is None and record.state not in {
                LifecycleState.WORKING,
                LifecycleState.IDLE,
            }:
                raise StoreConflict(
                    f"state {record.state.value} cannot be quiesced for exact-session resume"
                )
            record.quiesce_operation_id = operation_id
            record.quiesce_resume_state = captured_state
            self._write_record(record)
            registry = self._read_registry()
            current = (registry.get(record.agent_id) or {}).get("current") or {}
            if current.get("run_id") == record.run_id:
                registry[record.agent_id]["current"] = self._registry_current(record)
                self._write_registry(registry)
            return record

    def finish_provider_detached(
        self,
        run_id: str,
        operation_id: str,
        *,
        reason: str,
    ) -> RunRecord:
        """Converge a matching quiesce after transport shutdown and late events."""

        _validated_operation_id(operation_id)
        with self._lock:
            record = self.get(run_id)
            if record.quiesce_operation_id != operation_id:
                raise StoreConflict("quiesce operation is missing or stale")
            resume_state = record.quiesce_resume_state
            if resume_state not in {LifecycleState.WORKING, LifecycleState.IDLE}:
                raise StoreError("quiesce operation has no resumable captured state")
            record.provider_pid = None
            record.active_turn_id = None
            record.pending_requests.clear()
            record.automatic_resume_suppressed = True
            record.automatic_resume_guarded_at = None
            record.recovery_from_state = resume_state
            record.state = LifecycleState.BLOCKED
            record.state_reason = reason
            self._write_record(record)
            registry = self._read_registry()
            current = (registry.get(record.agent_id) or {}).get("current") or {}
            if current.get("run_id") == record.run_id:
                registry[record.agent_id]["current"] = self._registry_current(record)
                self._write_registry(registry)
            return record

    def clear_quiesce_marker(
        self,
        run_id: str,
        operation_id: str,
    ) -> RunRecord:
        """Clear the matching marker only after its provider is attached again."""

        _validated_operation_id(operation_id)
        with self._lock:
            record = self.get(run_id)
            if record.quiesce_operation_id != operation_id:
                raise StoreConflict("quiesce operation is missing or stale")
            if (
                record.state not in {LifecycleState.WORKING, LifecycleState.IDLE}
                or record.provider_pid is None
            ):
                raise StoreConflict("provider has not completed controlled resume")
            record.quiesce_operation_id = None
            record.quiesce_resume_state = None
            record.recovery_from_state = None
            record.automatic_resume_suppressed = False
            record.automatic_resume_guarded_at = None
            self._write_record(record)
            registry = self._read_registry()
            current = (registry.get(record.agent_id) or {}).get("current") or {}
            if current.get("run_id") == record.run_id:
                registry[record.agent_id]["current"] = self._registry_current(record)
                self._write_registry(registry)
            return record

    def abandon_quiesce_marker(
        self,
        run_id: str,
        operation_id: str,
    ) -> RunRecord:
        """Forget a marker only when the captured run can never be revived."""

        _validated_operation_id(operation_id)
        with self._lock:
            record = self.get(run_id)
            if record.quiesce_operation_id != operation_id:
                raise StoreConflict("quiesce operation is missing or stale")
            if (
                self.is_current(record)
                and not record.replaced_by_run_id
                and record.state not in TERMINAL_STATES
            ):
                raise StoreConflict("current resumable run cannot abandon quiesce")
            record.quiesce_operation_id = None
            record.quiesce_resume_state = None
            record.recovery_from_state = None
            record.automatic_resume_suppressed = False
            record.automatic_resume_guarded_at = None
            self._write_record(record)
            registry = self._read_registry()
            current = (registry.get(record.agent_id) or {}).get("current") or {}
            if current.get("run_id") == record.run_id:
                registry[record.agent_id]["current"] = self._registry_current(record)
                self._write_registry(registry)
            return record

    def track_pending_user_message(
        self,
        run_id: str,
        pending_id: str,
        text: str,
        source: str | None = None,
    ) -> RunRecord:
        with self._lock:
            record = self.get(run_id)
            existing = next(
                (
                    message
                    for message in record.pending_user_messages
                    if message.get("pending_id") == pending_id
                ),
                None,
            )
            if existing is not None:
                if existing.get("text") != text:
                    raise StoreConflict("pending message id was reused with different text")
                return record
            if len(record.pending_user_messages) >= MAX_PENDING_USER_MESSAGES:
                raise StoreConflict("pending user message limit reached")
            entry: dict[str, Any] = {
                "pending_id": pending_id,
                "text": text,
                "sent_at": utc_now(),
            }
            if isinstance(source, str) and source:
                entry["source"] = source
            record.pending_user_messages.append(entry)
            self._write_record(record)
            return record

    def match_pending_user_message(
        self,
        run_id: str,
        echoed_text: str,
    ) -> dict[str, str] | None:
        normalized_echo = echoed_text.strip()
        if not normalized_echo:
            return None
        with self._lock:
            record = self.get(run_id)
            # This list is append-ordered. Always scan from the front so one
            # provider echo acknowledges the oldest identical send (FIFO),
            # including the hook-wrapped fallback below.
            exact = next(
                (
                    message
                    for message in record.pending_user_messages
                    if message.get("text", "").strip() == normalized_echo
                ),
                None,
            )
            if exact is not None:
                return dict(exact)
            for message in record.pending_user_messages:
                text = message.get("text", "").strip()
                suffix = normalized_echo[len(text) :].lstrip() if text else ""
                prefix = normalized_echo[: -len(text)].rstrip() if text else ""
                if text and (
                    (normalized_echo.startswith(text) and suffix.startswith("<"))
                    or (normalized_echo.endswith(text) and prefix.endswith(">"))
                ):
                    return dict(message)
            return None

    def steer_delivery_observed(self, run_id: str, pending_id: str) -> bool:
        """Check durable composer state before retrying an uncertain delivery."""

        with self._lock:
            record = self.get(run_id)
            if any(
                message.get("pending_id") == pending_id
                for message in record.composer_messages
            ):
                return True
            try:
                events = self.read_normalized_events(run_id)
            except RunNotFound:
                return False
            return any(
                isinstance(event.get("payload"), dict)
                and event["payload"].get("pending_id") == pending_id
                for event in events
            )

    def discard_pending_user_message(
        self,
        run_id: str,
        pending_id: str,
    ) -> RunRecord:
        with self._lock:
            record = self.get(run_id)
            record.pending_user_messages = [
                message
                for message in record.pending_user_messages
                if message.get("pending_id") != pending_id
            ]
            self._write_record(record)
            return record

    def clear_pending_user_messages(self, run_id: str) -> RunRecord:
        """Retire matchers after their provider transport is fully stopped."""

        with self._lock:
            record = self.get(run_id)
            if not record.pending_user_messages:
                return record
            record.pending_user_messages = []
            self._write_record(record)
            return record

    def retire_overbound_pending_user_messages(self, run_id: str) -> bool:
        """Clear legacy overflow only at a caller-proven transport boundary."""

        with self._lock:
            record = self.get(run_id)
            if len(record.pending_user_messages) <= MAX_PENDING_USER_MESSAGES:
                return False
            record.pending_user_messages = []
            self._write_record(record)
            return True

    def composer_messages_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """Read one canonical composer journal across a replacement chain."""

        with self._lock:
            chain: list[RunRecord] = []
            seen_run_ids: set[str] = set()
            record = self.get(run_id)
            while record.run_id not in seen_run_ids:
                chain.append(record)
                seen_run_ids.add(record.run_id)
                if record.replaces_run_id is None:
                    break
                try:
                    record = self.get(record.replaces_run_id)
                except RunNotFound:
                    break

            messages: list[dict[str, Any]] = []
            seen_pending_ids: set[str] = set()
            for source_record in reversed(chain):
                for message in source_record.composer_messages:
                    pending_id = message.get("pending_id")
                    if isinstance(pending_id, str):
                        if pending_id in seen_pending_ids:
                            continue
                        seen_pending_ids.add(pending_id)
                    messages.append(dict(message))
            return messages

    def queue_message(
        self,
        run_id: str,
        text: str,
        pending_id: str | None = None,
        source: str | None = None,
    ) -> RunRecord:
        with self._lock:
            record = self.get(run_id)
            message: dict[str, Any] = {"text": text, "queued_at": utc_now()}
            if pending_id is not None:
                message["pending_id"] = pending_id
            if isinstance(source, str) and source:
                message["source"] = source
            record.queued_messages.append(message)
            self._write_record(record)
            return record

    def claim_message_dedupe_key(
        self,
        run_id: str,
        dedupe_key: str,
        *,
        owner: str | None = None,
    ) -> tuple[RunRecord, bool]:
        with self._lock:
            record = self.get(run_id)
            for entry in record.message_dedupe_keys:
                if entry.get("key") != dedupe_key:
                    continue
                # An owner-scoped re-claim by the same effect_id succeeds so a
                # crash between the dedupe write and the provider delivery can
                # replay through the same effect (WIKI-232). Un-owned claims
                # (or a different owner) still reject as before so unrelated
                # retries stay deduplicated.
                existing_owner = entry.get("owner")
                if owner is not None and existing_owner == owner:
                    return record, True
                return record, False
            entry: dict[str, str] = {"key": dedupe_key}
            if owner is not None:
                entry["owner"] = owner
            record.message_dedupe_keys.append(entry)
            if len(record.message_dedupe_keys) > MAX_MESSAGE_DEDUPE_KEYS:
                del record.message_dedupe_keys[:-MAX_MESSAGE_DEDUPE_KEYS]
            self._write_record(record)
            return record, True

    def release_message_dedupe_key(
        self,
        run_id: str,
        dedupe_key: str,
    ) -> RunRecord:
        with self._lock:
            record = self.get(run_id)
            record.message_dedupe_keys = [
                entry
                for entry in record.message_dedupe_keys
                if entry.get("key") != dedupe_key
            ]
            self._write_record(record)
            return record

    def replace_queued_messages(
        self,
        run_id: str,
        messages: list[dict[str, str]],
    ) -> RunRecord:
        with self._lock:
            record = self.get(run_id)
            record.queued_messages = [dict(message) for message in messages]
            self._write_record(record)
            return record

    def pop_queued_message(self, run_id: str) -> dict[str, str] | None:
        with self._lock:
            record = self.get(run_id)
            if not record.queued_messages:
                return None
            message = record.queued_messages.pop(0)
            self._write_record(record)
            return message

    def remove_queued_message_by_pending_id(
        self,
        run_id: str,
        pending_id: str,
    ) -> RunRecord:
        with self._lock:
            record = self.get(run_id)
            record.queued_messages = [
                message
                for message in record.queued_messages
                if message.get("pending_id") != pending_id
            ]
            self._write_record(record)
            return record

    def peek_queued_message(self, run_id: str) -> dict[str, str] | None:
        with self._lock:
            record = self.get(run_id)
            if not record.queued_messages:
                return None
            return dict(record.queued_messages[0])

    def queued_messages(self, run_id: str) -> list[dict[str, str]]:
        with self._lock:
            return [dict(message) for message in self.get(run_id).queued_messages]

    def delete_queued_message(self, run_id: str, index: int) -> RunRecord:
        with self._lock:
            record = self.get(run_id)
            if not 0 <= index < len(record.queued_messages):
                raise RunNotFound("no such queued message")
            record.queued_messages.pop(index)
            self._write_record(record)
            return record

    def replace(
        self,
        old_run_id: str,
        new_record: RunRecord,
        *,
        reset_status: bool = True,
    ) -> tuple[RunRecord, RunRecord]:
        with self._lock:
            old = self.get(old_run_id)
            if new_record.agent_id != old.agent_id:
                raise StoreConflict("replacement agent id must match")
            if new_record.run_id == old.run_id:
                raise StoreConflict("replacement must use a new run id")
            registry = self._read_registry()
            entry = registry.get(old.agent_id) or {}
            current = entry.get("current") or {}
            if current.get("run_id") != old.run_id:
                raise StoreConflict("replacement target is no longer current")
            if reset_status:
                # Direct store callers have no supervisor lock boundary to do
                # this first. Supervisor replacement passes False only after
                # quiescing the old provider and resetting under that lock.
                self.status_path(old.agent_id).unlink(missing_ok=True)

            # Replacement quiesces and drains the old transport before this
            # run becomes current. Its unresolved text matchers cannot receive
            # a later echo. Do not copy them into the new transport, where an
            # equal-text alarm could otherwise stay blocked or consume the
            # old source identity (REVIEW19/20 H1).
            new_record.pending_user_messages = []
            new_record.composer_messages = [
                dict(message) for message in old.composer_messages
            ]
            new_record.message_dedupe_keys = list(old.message_dedupe_keys)
            old.replaced_by_run_id = new_record.run_id
            old.outcome = "handoff"
            old.state_reason = "replaced"
            if old.state not in {LifecycleState.DEAD, LifecycleState.COMPLETED}:
                old.state = LifecycleState.COMPLETED
            new_record.replaces_run_id = old.run_id

            self._create_run_files(new_record)
            self._write_record(old)
            history = list(entry.get("history") or [])
            history.append(
                {
                    **current,
                    "outcome": "handoff",
                    "ended_at": old.updated_at,
                    "replaced_by_run_id": new_record.run_id,
                }
            )
            registry[old.agent_id] = {
                "history": history,
                "current": self._registry_current(new_record),
            }
            self._write_registry(registry)
            self.command_log.replace_projection(
                old.agent_id,
                {old.agent_id: registry[old.agent_id]},
            )
            return old, new_record

    def abort_replace(
        self,
        old_run_id: str,
        replacement_run_id: str,
        *,
        reason: str,
        adapter_status: AdapterStatus,
    ) -> RunRecord:
        """Roll back a published replacement whose new provider failed."""

        with self._lock:
            old = self.get(old_run_id)
            replacement = self.get(replacement_run_id)
            if replacement.agent_id != old.agent_id:
                raise StoreConflict("replacement agent id must match")
            registry = self._read_registry()
            entry = registry.get(old.agent_id) or {}
            current = entry.get("current") or {}
            if current.get("run_id") != replacement_run_id:
                raise StoreConflict("replacement target is no longer current")
            replacement = self.discover_provider_process(replacement_run_id)
            if not self._terminate_recorded_provider_pid(
                replacement,
                allow_dead_without_identity=False,
            ):
                raise StoreConflict(
                    "replacement provider identity is uncertain; rollback retained"
                )

            self.status_path(old.agent_id).unlink(missing_ok=True)
            old.replaced_by_run_id = None
            old.outcome = None
            old.state = LifecycleState.BLOCKED
            old.state_reason = reason
            old.provider_session_id = adapter_status.session_id
            old.provider_pid = adapter_status.pid
            old.provider_generation = adapter_status.generation
            old.active_turn_id = adapter_status.active_turn_id
            if adapter_status.transcript_path is not None:
                old.transcript_path = adapter_status.transcript_path
            self._write_record(old)

            history = [
                item
                for item in (entry.get("history") or [])
                if not (
                    isinstance(item, dict)
                    and item.get("run_id") == old.run_id
                    and item.get("replaced_by_run_id") == replacement_run_id
                )
            ]
            registry[old.agent_id] = {
                "history": history,
                "current": self._registry_current(old),
            }
            shutil.rmtree(self.run_dir(replacement_run_id))
            self._write_registry(registry)
            self.command_log.replace_projection(
                old.agent_id,
                {old.agent_id: registry[old.agent_id]},
            )
            return old

    def read_raw_events(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        path = self.raw_events_path(run_id)
        return self._event_page(
            self._read_json_lines_tail(path, limit)
            if after_seq == 0 and limit is not None
            else self._read_json_lines(path),
            after_seq=after_seq,
            limit=limit,
        )

    def rebuild_projections_from_normalized(self, run_id: str) -> RunRecord:
        """Replay the normalized event log in raw provider order to
        repair projections after a crash or an orphan-raw recovery.

        ``pending_requests``, ``current_turn_diff``,
        ``disposition_counts``, and ``unread_event_seq`` are rebuilt
        from scratch by walking events sorted by ``(raw_seq, seq)``, so a crash
        between the JSONL append and ``run.json`` replace cannot leave
        stale approval indices behind. Lifecycle transitions are only
        re-applied for events whose raw/normalized position exceeds
        ``(record.last_causal_raw_seq, record.last_lifecycle_event_seq)``.
        The two-part guard preserves external
        transitions (``store.transition(BLOCKED)``,
        ``mark_automatic_resume_failed`` etc.) while still catching new
        lifecycle events that landed in JSONL but had not persisted a
        matching ``record.state`` before the crash.

        ``pending_user_messages`` is left alone because it interleaves
        with external ``track_pending_user_message`` calls that predate
        any matching echo event; the walk's remove semantics inside
        ``_apply_composer_message_event`` are already idempotent. Composer
        rows inherited from a replaced run are also preserved. Rows owned by
        this run are removed and replayed in raw order.
        """

        with self._lock:
            record = self.get(run_id)
            record_before_rebuild = record.to_dict()
            current_diff_before_rebuild = self._read_current_turn_diff_snapshot(
                run_id
            )
            normalized_events = sorted(
                self._read_json_lines(self.normalized_events_path(run_id)),
                key=lambda event: (
                    int(event.get("raw_seq", 0)),
                    int(event.get("seq", 0)),
                ),
            )
            replayed_composer_ids = {
                str(payload["pending_id"])
                for event in normalized_events
                for payload in [event.get("payload")]
                if isinstance(payload, dict)
                and isinstance(payload.get("pending_id"), str)
                and isinstance(payload.get("composer_text"), str)
                and isinstance(payload.get("composer_sent_at"), str)
            }
            record.composer_messages = [
                message
                for message in record.composer_messages
                if message.get("pending_id") not in replayed_composer_ids
            ]
            record.pending_requests = {}
            record.current_turn_diff_turn_id = None
            record.current_turn_diff_started_seq = 0
            record.current_turn_diff_seq = 0
            counts = {item.value: 0 for item in EventDisposition}
            rebuilt_unread_seq = 0
            current_diff: str | None = None
            current_diff_dirty = False
            lifecycle_gate = (
                record.last_causal_raw_seq,
                record.last_lifecycle_event_seq,
            )
            max_raw_seq = record.last_causal_raw_seq
            max_causal_seq = record.last_lifecycle_event_seq

            for event in normalized_events:
                disposition = event.get("disposition")
                if disposition in counts:
                    counts[disposition] += 1
                payload = event.get("payload")
                kind_value = str(event.get("kind") or "unknown")
                seq = int(event.get("seq", 0))
                raw_seq_int = int(event.get("raw_seq", 0))
                causal_position = (raw_seq_int, seq)
                normalized_at = str(event.get("normalized_at") or utc_now())
                if raw_seq_int > max_raw_seq:
                    max_raw_seq = raw_seq_int
                    max_causal_seq = seq
                elif raw_seq_int == max_raw_seq and seq > max_causal_seq:
                    max_causal_seq = seq
                if isinstance(payload, dict):
                    _apply_pending_request_event(
                        record,
                        kind=kind_value,
                        payload=payload,
                        raw_seq=raw_seq_int,
                        normalized_at=normalized_at,
                    )
                    _apply_composer_message_event(
                        record,
                        payload=payload,
                        seq=seq,
                        normalized_at=normalized_at,
                    )
                changed, snapshot = _current_turn_diff_update(record, event)
                if changed:
                    current_diff = snapshot
                    current_diff_dirty = True
                if _is_unread_worthy(
                    kind_value,
                    payload if isinstance(payload, dict) else None,
                    str(disposition or ""),
                ):
                    if seq > rebuilt_unread_seq:
                        rebuilt_unread_seq = seq
                lifecycle_value = event.get("lifecycle_state")
                if (
                    isinstance(lifecycle_value, str)
                    and causal_position > lifecycle_gate
                ):
                    try:
                        target = LifecycleState(lifecycle_value)
                        validate_transition(record.state, target)
                    except ValueError:
                        pass
                    else:
                        record.state = target
                        record.state_reason = None
                        if (
                            target is not LifecycleState.BLOCKED
                            and record.quiesce_operation_id is None
                        ):
                            record.recovery_from_state = None
                        if target in TERMINAL_STATES:
                            record.pending_requests.clear()
                    record.last_lifecycle_event_seq = seq
                    lifecycle_gate = causal_position

            record.disposition_counts = counts
            record.unread_event_seq = rebuilt_unread_seq
            record.last_causal_raw_seq = max_raw_seq
            record.last_lifecycle_event_seq = max_causal_seq
            if record.to_dict() != record_before_rebuild:
                self._write_record(record)
            if (
                current_diff_dirty
                and current_diff != current_diff_before_rebuild
            ):
                self._write_current_turn_diff_snapshot(run_id, current_diff)
            return record

    def read_normalized_events(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        path = self.normalized_events_path(run_id)
        return self._event_page(
            self._read_json_lines_tail(path, limit)
            if after_seq == 0 and limit is not None
            else self._read_json_lines(path),
            after_seq=after_seq,
            limit=limit,
        )

    @staticmethod
    def _event_page(
        events: list[dict[str, Any]],
        *,
        after_seq: int,
        limit: int | None,
    ) -> list[dict[str, Any]]:
        filtered = [event for event in events if int(event.get("seq", 0)) > after_seq]
        if limit is None:
            return filtered
        if after_seq == 0:
            return filtered[-limit:]
        return filtered[:limit]

    def _read_json_lines(self, path: Path) -> list[dict[str, Any]]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError as exc:
            raise RunNotFound(str(path)) from exc
        events: list[dict[str, Any]] = []
        for line in lines:
            if not line:
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                events.append(value)
        return events

    def _iter_json_lines(self, path: Path) -> Iterator[dict[str, Any]]:
        """Stream one parsed record per JSONL line without materializing the file.

        Recovery paths that only need a scalar per event (max seq, a set of
        raw_seq values) use this instead of ``_read_json_lines`` so a
        multi-hundred-run boot does not spike RSS holding every parsed
        dict from every JSONL log at once (WIKI-243). Errors are swallowed
        to match ``_read_json_lines``.
        """

        try:
            handle = path.open("r", encoding="utf-8")
        except FileNotFoundError as exc:
            raise RunNotFound(str(path)) from exc
        try:
            for line in handle:
                stripped = line.rstrip("\n")
                if not stripped:
                    continue
                try:
                    value = json.loads(stripped)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    yield value
        finally:
            handle.close()

    def iter_raw_events(self, run_id: str) -> Iterator[dict[str, Any]]:
        """Stream raw-event records for ``run_id`` without materializing the log."""

        return self._iter_json_lines(self.raw_events_path(run_id))

    def iter_normalized_events(self, run_id: str) -> Iterator[dict[str, Any]]:
        """Stream normalized-event records for ``run_id`` without materializing the log."""

        return self._iter_json_lines(self.normalized_events_path(run_id))

    def _read_json_lines_tail(
        self,
        path: Path,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Read complete JSONL records from the tail without scanning large logs."""

        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                remaining = handle.tell()
                chunks: list[bytes] = []
                newline_count = 0
                while remaining > 0 and newline_count <= limit:
                    size = min(64 * 1024, remaining)
                    remaining -= size
                    handle.seek(remaining)
                    chunk = handle.read(size)
                    chunks.append(chunk)
                    newline_count += chunk.count(b"\n")
        except FileNotFoundError as exc:
            raise RunNotFound(str(path)) from exc
        raw_lines = b"".join(reversed(chunks)).splitlines()
        if remaining > 0 and raw_lines:
            raw_lines.pop(0)  # the first chunk began in the middle of a record
        events: list[dict[str, Any]] = []
        for raw_line in raw_lines[-limit:]:
            if not raw_line:
                continue
            try:
                value = json.loads(raw_line)
            except ValueError:
                continue
            if isinstance(value, dict):
                events.append(value)
        return events

    def file_modes(self, run_id: str) -> dict[str, int]:
        paths = {
            "run": self.run_path(run_id),
            "raw": self.raw_events_path(run_id),
            "events": self.normalized_events_path(run_id),
        }
        return {name: stat.S_IMODE(path.stat().st_mode) for name, path in paths.items()}
