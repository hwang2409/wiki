from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from .provider import AdapterStatus
from .types import (
    EventDisposition,
    LifecycleState,
    RunRecord,
    TERMINAL_STATES,
    utc_now,
    validate_transition,
)


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


@dataclass(frozen=True)
class RuntimePaths:
    runtime_dir: Path
    socket_path: Path
    registry_path: Path

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
        # absolute() preserves the final path component instead of following a
        # pre-existing symlink. Writers can then reject symlinks explicitly.
        return cls(
            runtime_dir=runtime_dir.absolute(),
            socket_path=socket_path.absolute(),
            registry_path=registry_path.absolute(),
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
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


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
        _fsync_directory(path.parent)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp.unlink(missing_ok=True)
        raise


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
        _ensure_private_dir(paths.runtime_dir)
        _ensure_private_dir(paths.runs_dir)
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

    def provider_log_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "provider.log"

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

    def _registry_current(self, record: RunRecord) -> dict[str, Any]:
        return {
            "ticket": record.agent_id,
            "run_id": record.run_id,
            "provider": record.provider.value,
            "kind": record.provider.legacy_kind,
            "role": record.role,
            "model": record.model,
            "effort": record.effort,
            "worktree": record.worktree,
            "cwd": record.worktree,
            "orch": record.orchestrator_id,
            "state": record.state.value,
            "state_reason": record.state_reason,
            "recovery_from_state": (
                record.recovery_from_state.value if record.recovery_from_state else None
            ),
            "automatic_resume_suppressed": record.automatic_resume_suppressed,
            "automatic_resume_guarded_at": record.automatic_resume_guarded_at,
            "session_id": record.provider_session_id,
            "provider_session_id": record.provider_session_id,
            "provider_pid": record.provider_pid,
            "provider_generation": record.provider_generation,
            "active_turn_id": record.active_turn_id,
            "transcript": record.transcript_path,
            "log": str(self.raw_events_path(record.run_id)),
            # Transitional compatibility only. Headless liveness never reads it.
            "window": None,
            "spawned_at": record.created_at,
            "updated_at": record.updated_at,
        }

    def _write_record(self, record: RunRecord) -> None:
        record.updated_at = utc_now()
        _atomic_write_json(self.run_path(record.run_id), record.to_dict())

    def _create_run_files(self, record: RunRecord) -> None:
        directory = self.run_dir(record.run_id)
        if directory.exists():
            raise StoreConflict(f"run already exists: {record.run_id}")
        directory.mkdir(mode=0o700, parents=False)
        directory.chmod(0o700)
        for path in (
            self.raw_events_path(record.run_id),
            self.normalized_events_path(record.run_id),
        ):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        self._write_record(record)

    def _reconcile_existing_runs(self) -> None:
        """Repair event counters after a crash between JSONL fsync and run.json."""

        for path in sorted(self.paths.runs_dir.glob("*/run.json")):
            try:
                value = _read_json(path)
                if not isinstance(value, dict):
                    continue
                record = RunRecord.from_dict(value)
                raw_path = self.raw_events_path(record.run_id)
                normalized_path = self.normalized_events_path(record.run_id)
                _repair_jsonl_tail(raw_path)
                _repair_jsonl_tail(normalized_path)
                raw_events = self._read_json_lines(raw_path)
                normalized_events = self._read_json_lines(normalized_path)
                counts = {item.value: 0 for item in EventDisposition}
                lifecycle_checkpoint = record.last_lifecycle_event_seq
                for event in normalized_events:
                    disposition = event.get("disposition")
                    if disposition in counts:
                        counts[disposition] += 1
                    seq = int(event.get("seq", 0))
                    if seq <= lifecycle_checkpoint:
                        continue
                    lifecycle_value = event.get("lifecycle_state")
                    if isinstance(lifecycle_value, str):
                        try:
                            target = LifecycleState(lifecycle_value)
                            validate_transition(record.state, target)
                        except ValueError:
                            pass
                        else:
                            record.state = target
                            record.state_reason = None
                            if target is not LifecycleState.BLOCKED:
                                record.recovery_from_state = None
                    lifecycle_checkpoint = seq
                raw_count = max(
                    (int(event.get("seq", 0)) for event in raw_events),
                    default=0,
                )
                normalized_count = max(
                    (int(event.get("seq", 0)) for event in normalized_events),
                    default=0,
                )
                if (
                    record.raw_event_count != raw_count
                    or record.normalized_event_count != normalized_count
                    or record.disposition_counts != counts
                    or record.last_lifecycle_event_seq != lifecycle_checkpoint
                ):
                    record.raw_event_count = raw_count
                    record.normalized_event_count = normalized_count
                    record.disposition_counts = counts
                    record.last_lifecycle_event_seq = lifecycle_checkpoint
                    self._write_record(record)
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

        records_by_agent: dict[str, list[RunRecord]] = {}
        for record in self.list_runs():
            records_by_agent.setdefault(record.agent_id, []).append(record)
        if not records_by_agent:
            return

        registry = self._read_registry()
        changed = False
        for agent_id, records in records_by_agent.items():
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

    def create(self, record: RunRecord, *, migrate_legacy: bool = False) -> RunRecord:
        with self._lock:
            registry = self._read_registry()
            entry = registry.get(record.agent_id)
            current = entry.get("current") if isinstance(entry, dict) else None
            legacy_orchestrators = registry.get("_orchestrators")
            legacy_orchestrator = (
                legacy_orchestrators.get(record.agent_id)
                if isinstance(legacy_orchestrators, dict)
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
            self._create_run_files(record)
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
                assert isinstance(legacy_orchestrators, dict)
                legacy_orchestrators.pop(record.agent_id)
                if not legacy_orchestrators:
                    registry.pop("_orchestrators", None)
            registry[record.agent_id] = {
                "history": history,
                "current": self._registry_current(record),
            }
            self._write_registry(registry)
            return record

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
            if target is not LifecycleState.BLOCKED:
                record.recovery_from_state = None
            if adapter_status is not None:
                if adapter_status.session_id is not None:
                    record.provider_session_id = adapter_status.session_id
                record.provider_pid = adapter_status.pid
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
            return record

    def mark_recovery_blocked(self, run_id: str, *, reason: str) -> RunRecord:
        """Block an unowned live PID while preserving automatic-resume intent."""

        with self._lock:
            record = self.get(run_id)
            recovery_state = record.recovery_from_state or record.state
            if recovery_state not in {LifecycleState.WORKING, LifecycleState.IDLE}:
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
            if recovery_state not in {LifecycleState.WORKING, LifecycleState.IDLE}:
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
                "normalized_at": utc_now(),
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
                    if lifecycle_state is not LifecycleState.BLOCKED:
                        record.recovery_from_state = None
            record.last_lifecycle_event_seq = int(envelope["seq"])
            self._write_record(record)
            return envelope

    def queue_message(self, run_id: str, text: str) -> RunRecord:
        with self._lock:
            record = self.get(run_id)
            record.queued_messages.append({"text": text, "queued_at": utc_now()})
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
        self, old_run_id: str, new_record: RunRecord
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
            return old, new_record

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
