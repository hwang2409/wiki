"""Weekly telemetry for provider event kinds the normalizer does not know."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .normalizer import NormalizedProviderEvent, normalize_provider_event
from .store import RuntimePaths
from .types import EventDisposition, ProviderKind


logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_UNKNOWN_KIND_THRESHOLD = 100
MAX_SCHEDULE_DELAY_SECONDS = DEFAULT_INTERVAL_SECONDS
SCAN_CHUNK_BYTES = 64 * 1024
MAX_EVENT_LINE_BYTES = 1024 * 1024
CHECKPOINT_EVENT_COUNT = 256
MAX_CURSOR_ENTRIES = 4096
STATE_VERSION = 2
ARCHIVE_COMPLETION_MARKER = "archive-complete.json"


@dataclass(frozen=True)
class _ScanSource:
    key: str
    raw_path: Path
    terminal: bool


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _week_start(timestamp: float) -> str:
    current = datetime.fromtimestamp(timestamp, tz=timezone.utc).date()
    return current.fromordinal(current.toordinal() - current.weekday()).isoformat()


def _default_state(timestamp: float) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "week_start": _week_start(timestamp),
        "last_run_at": None,
        "cursors": {},
        "unknown_counts": {},
        "covered_kinds": [],
        "pending_kinds": {},
        "filed_kinds": [],
        "completed_runs": [],
    }


def _todo_runner(text: str) -> None:
    """File one todo item through the repository's CLI."""

    repo_dir = Path(
        os.environ.get("WIKI_REPO_DIR") or Path(__file__).resolve().parents[3]
    )
    cli = repo_dir / "wiki"
    subprocess.run(
        [str(cli), "todo", "add", text, "--section", "Todo", "--if-missing"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


class UnknownKindTelemetry:
    """Incrementally scan raw provider streams and file novel unknown kinds."""

    def __init__(
        self,
        paths: RuntimePaths,
        *,
        threshold: int = DEFAULT_UNKNOWN_KIND_THRESHOLD,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        todo_runner: Callable[[str], None] = _todo_runner,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if threshold < 0:
            raise ValueError("threshold must not be negative")
        if interval <= 0:
            raise ValueError("interval must be positive")
        self.paths = paths
        self.threshold = threshold
        self.interval = interval
        self.todo_runner = todo_runner
        self.clock = clock
        self.state_path = paths.runtime_dir / "unknown-kind-telemetry.json"
        self._lock = threading.Lock()

    def _load_state(self, timestamp: float) -> dict[str, Any]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return _default_state(timestamp)
        if not isinstance(state, dict) or state.get("version") not in {
            1,
            STATE_VERSION,
        }:
            return _default_state(timestamp)
        result = _default_state(timestamp)
        result.update(state)
        if state.get("version") == 1:
            old_week = str(state.get("week_start") or _week_start(timestamp))
            old_counts = state.get("unknown_counts")
            result["unknown_counts"] = {
                old_week: old_counts if isinstance(old_counts, dict) else {}
            }
            result["version"] = STATE_VERSION
        for key in ("cursors", "unknown_counts"):
            if not isinstance(result[key], dict):
                result[key] = {}
        result["cursors"] = {
            str(run_name): cursor
            for run_name, cursor in result["cursors"].items()
            if isinstance(run_name, str) and isinstance(cursor, dict)
        }
        result["unknown_counts"] = {
            str(week): counts
            for week, counts in result["unknown_counts"].items()
            if isinstance(counts, dict)
        }
        if not isinstance(result["pending_kinds"], dict):
            result["pending_kinds"] = {}
        for key in ("covered_kinds", "filed_kinds"):
            if not isinstance(result[key], list):
                result[key] = []
        result["covered_kinds"] = [
            kind for kind in result["covered_kinds"] if isinstance(kind, str)
        ]
        result["filed_kinds"] = [
            kind for kind in result["filed_kinds"] if isinstance(kind, str)
        ]
        if not isinstance(result["completed_runs"], list):
            result["completed_runs"] = []
        result["completed_runs"] = [
            run_name
            for run_name in result["completed_runs"]
            if isinstance(run_name, str)
        ]
        return result

    def _save_state(self, state: dict[str, Any]) -> None:
        _atomic_write_json(self.state_path, state)

    def _start_week(self, state: dict[str, Any], timestamp: float) -> None:
        current_week = _week_start(timestamp)
        stored_week = state.get("week_start")
        if not isinstance(stored_week, str) or current_week > stored_week:
            state["week_start"] = current_week

    @staticmethod
    def _cursor_for(
        state: dict[str, Any],
        run_name: str,
        path: Path,
        timestamp: float,
        *,
        terminal: bool,
    ) -> dict[str, Any]:
        cursors = state["cursors"]
        cursor = cursors.get(run_name)
        if not isinstance(cursor, dict):
            cursor = {}
            cursors[run_name] = cursor
        try:
            stat = path.stat()
        except OSError:
            return cursor
        try:
            cursor_offset = int(cursor.get("offset", 0))
        except (TypeError, ValueError):
            cursor_offset = 0
        source_type = "archive" if terminal else "live"
        source_changed_to_archive = (
            cursor.get("source_type") == "live" and source_type == "archive"
        )
        if (
            cursor.get("device") != stat.st_dev
            or cursor.get("inode") != stat.st_ino
            or cursor_offset > stat.st_size
        ) and (not source_changed_to_archive or cursor_offset > stat.st_size):
            cursor.clear()
            cursor.update({"offset": 0, "device": stat.st_dev, "inode": stat.st_ino})
        elif source_changed_to_archive:
            cursor["device"] = stat.st_dev
            cursor["inode"] = stat.st_ino
        cursor["source_type"] = source_type
        cursor["path"] = str(path)
        cursor["last_seen_at"] = timestamp
        cursor["missing_sweeps"] = 0
        return cursor

    def _prune_cursors(self, state: dict[str, Any]) -> None:
        cursors = state["cursors"]
        for run_name in list(cursors):
            cursor = cursors[run_name]
            cursor_path = cursor.get("path") if isinstance(cursor, dict) else None
            if not isinstance(cursor_path, str) or not Path(cursor_path).is_file():
                if isinstance(cursor, dict):
                    missing_sweeps = int(cursor.get("missing_sweeps", 0))
                    if missing_sweeps < 1:
                        cursor["missing_sweeps"] = missing_sweeps + 1
                        continue
                cursors.pop(run_name, None)
        # A cursor is never evicted while its file can be scanned again.
        # Completed archive cursors are safe to remove because their source is
        # skipped permanently by ``_iter_sources``.
        completed = set(state["completed_runs"])
        while len(cursors) > MAX_CURSOR_ENTRIES:
            terminal_names = [name for name in cursors if name in completed]
            if not terminal_names:
                break
            oldest_name = min(
                terminal_names,
                key=lambda name: float(cursors[name].get("last_seen_at", 0)),
            )
            cursors.pop(oldest_name, None)

    def _iter_sources(self, state: dict[str, Any]):
        completed = set(state["completed_runs"])
        runs_dir = self.paths.runs_dir
        if runs_dir.is_dir():
            for run_dir in runs_dir.iterdir():
                raw_path = run_dir / "raw.jsonl"
                if run_dir.is_dir() and raw_path.is_file():
                    yield _ScanSource(run_dir.name, raw_path, False)

        archive_dir = self.paths.archive_dir
        if not archive_dir.is_dir():
            return
        for raw_path in archive_dir.rglob("raw.jsonl"):
            if not (raw_path.parent / ARCHIVE_COMPLETION_MARKER).is_file():
                continue
            run_name = None
            metadata_path = raw_path.parent / "run.json"
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                candidate = metadata.get("run_id")
                if isinstance(candidate, str) and candidate:
                    run_name = candidate
            except (OSError, TypeError, ValueError):
                pass
            if run_name is None:
                run_name = f"archive:{raw_path.relative_to(archive_dir)}"
            if run_name not in completed:
                yield _ScanSource(run_name, raw_path, True)

    @staticmethod
    def _event_week(envelope: dict[str, Any], fallback_week: str) -> str:
        received_at = envelope.get("received_at")
        if not isinstance(received_at, str):
            return fallback_week
        try:
            parsed = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
        except ValueError:
            return fallback_week
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return _week_start(parsed.astimezone(timezone.utc).timestamp())

    def _process_line(
        self,
        state: dict[str, Any],
        line: bytes,
        *,
        fallback_week: str,
    ) -> None:
        try:
            envelope = json.loads(line.decode("utf-8"))
            provider = ProviderKind(str(envelope["provider"]))
            payload = envelope["payload"]
            direction = str(envelope.get("direction", "provider"))
            if not isinstance(payload, dict):
                raise ValueError("payload is not an object")
        except (UnicodeDecodeError, KeyError, TypeError, ValueError):
            return
        try:
            normalized = normalize_provider_event(
                provider, payload, direction=direction
            )
        except Exception as exc:
            normalized = NormalizedProviderEvent(
                EventDisposition.UNKNOWN,
                "normalization_error",
                {"error": str(exc), "raw_payload": payload},
            )
        kind = normalized.kind
        if normalized.disposition is EventDisposition.UNKNOWN:
            week = self._event_week(envelope, fallback_week)
            week_counts = state["unknown_counts"].setdefault(week, {})
            week_counts[kind] = int(week_counts.get(kind, 0)) + 1
        else:
            covered = set(state["covered_kinds"])
            covered.add(kind)
            state["covered_kinds"] = sorted(covered)

    def _scan_run(
        self,
        state: dict[str, Any],
        source: _ScanSource,
        *,
        fallback_week: str,
        timestamp: float,
    ) -> int:
        if not source.raw_path.is_file():
            return 0
        cursor = self._cursor_for(
            state,
            source.key,
            source.raw_path,
            timestamp,
            terminal=source.terminal,
        )
        try:
            offset = max(0, int(cursor.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0
        scanned = 0
        stream_offset = offset
        line_buffer = bytearray()
        discarding = bool(cursor.get("discarding_oversized_line", False))
        checkpoint_events = 0
        discard_checkpoint_offset = stream_offset
        try:
            with source.raw_path.open("rb") as handle:
                handle.seek(offset)
                while chunk := handle.read(SCAN_CHUNK_BYTES):
                    chunk_start = 0
                    while chunk_start < len(chunk):
                        newline = chunk.find(b"\n", chunk_start)
                        if newline == -1:
                            segment = chunk[chunk_start:]
                            stream_offset += len(segment)
                            if not discarding:
                                if (
                                    len(line_buffer) + len(segment)
                                    > MAX_EVENT_LINE_BYTES
                                ):
                                    line_buffer.clear()
                                    discarding = True
                                    cursor["offset"] = stream_offset
                                    cursor["discarding_oversized_line"] = True
                                    self._save_state(state)
                                    discard_checkpoint_offset = stream_offset
                                else:
                                    line_buffer.extend(segment)
                            elif (
                                stream_offset - discard_checkpoint_offset
                                >= SCAN_CHUNK_BYTES * 16
                            ):
                                cursor["offset"] = stream_offset
                                self._save_state(state)
                                discard_checkpoint_offset = stream_offset
                            break

                        segment = chunk[chunk_start : newline + 1]
                        stream_offset += len(segment)
                        if discarding:
                            line_buffer.clear()
                            discarding = False
                            cursor["offset"] = stream_offset
                            cursor["discarding_oversized_line"] = False
                            self._save_state(state)
                        elif len(line_buffer) + len(segment) > MAX_EVENT_LINE_BYTES:
                            line_buffer.clear()
                            discarding = False
                            cursor["offset"] = stream_offset
                            cursor["discarding_oversized_line"] = False
                            self._save_state(state)
                        else:
                            line_buffer.extend(segment)
                            cursor["offset"] = stream_offset
                            cursor["discarding_oversized_line"] = False
                            self._process_line(
                                state,
                                bytes(line_buffer),
                                fallback_week=fallback_week,
                            )
                            line_buffer.clear()
                            scanned += 1
                            checkpoint_events += 1
                            if checkpoint_events >= CHECKPOINT_EVENT_COUNT:
                                self._save_state(state)
                                checkpoint_events = 0
                        chunk_start = newline + 1
            # Persist the final batch. Normal incomplete lines retain their
            # last complete-line offset; discard mode advances as consumed.
            if discarding:
                cursor["offset"] = stream_offset
                cursor["discarding_oversized_line"] = True
            self._save_state(state)
        except OSError:
            logger.exception(
                "unknown-kind telemetry could not scan %s", source.raw_path
            )
        return scanned

    def _file_todos(self, state: dict[str, Any]) -> list[str]:
        covered = set(state["covered_kinds"])
        filed = set(state["filed_kinds"])
        pending = state["pending_kinds"]
        filed_now: list[str] = []

        for kind in list(pending):
            if kind in covered or kind in filed:
                pending.pop(kind, None)
                continue
            item = pending[kind]
            text = item.get("text") if isinstance(item, dict) else None
            if not isinstance(text, str):
                pending.pop(kind, None)
                continue
            try:
                self.todo_runner(text)
            except Exception:
                logger.exception("unknown-kind telemetry todo add failed for %s", kind)
            else:
                pending.pop(kind, None)
                filed.add(kind)
                state["filed_kinds"] = sorted(filed)
                self._save_state(state)
                filed_now.append(kind)

        for week, week_counts in sorted(state["unknown_counts"].items()):
            for kind, count in sorted(week_counts.items()):
                if (
                    int(count) <= self.threshold
                    or kind in covered
                    or kind in filed
                    or kind in pending
                ):
                    continue
                text = (
                    f'unknown provider event kind "{kind}" exceeded '
                    f"{self.threshold} events in the week of {week}"
                )
                pending[kind] = {"text": text, "week": week}
                self._save_state(state)
                try:
                    self.todo_runner(text)
                except Exception:
                    logger.exception(
                        "unknown-kind telemetry todo add failed for %s", kind
                    )
                else:
                    pending.pop(kind, None)
                    filed.add(kind)
                    state["filed_kinds"] = sorted(filed)
                    self._save_state(state)
                    filed_now.append(kind)
        return filed_now

    def run_once(self, *, timestamp: float | None = None) -> dict[str, Any]:
        """Run one bounded sweep and return a JSON-safe summary."""

        with self._lock:
            current = self.clock() if timestamp is None else timestamp
            state = self._load_state(current)
            self._start_week(state, current)
            self._save_state(state)
            scanned_runs = 0
            scanned_events = 0
            for source in self._iter_sources(state):
                scanned_runs += 1
                scanned_events += self._scan_run(
                    state,
                    source,
                    fallback_week=state["week_start"],
                    timestamp=current,
                )
                if source.terminal:
                    cursor = state["cursors"].get(source.key)
                    try:
                        complete = (
                            isinstance(cursor, dict)
                            and not cursor.get("discarding_oversized_line", False)
                            and int(cursor.get("offset", 0))
                            >= source.raw_path.stat().st_size
                        )
                    except (OSError, TypeError, ValueError):
                        complete = False
                    if complete and source.key not in state["completed_runs"]:
                        state["completed_runs"].append(source.key)
            self._prune_cursors(state)
            filed_now = self._file_todos(state)
            state["last_run_at"] = current
            self._save_state(state)
            current_counts = state["unknown_counts"].get(state["week_start"], {})
            return {
                "week_start": state["week_start"],
                "scanned_runs": scanned_runs,
                "scanned_events": scanned_events,
                "unknown_counts": dict(current_counts),
                "unknown_counts_by_week": {
                    week: dict(counts)
                    for week, counts in state["unknown_counts"].items()
                },
                "filed_kinds": filed_now,
                "last_run_at": current,
            }

    def seconds_until_due(self, *, timestamp: float | None = None) -> float:
        current = self.clock() if timestamp is None else timestamp
        state = self._load_state(current)
        last_run = state.get("last_run_at")
        if not isinstance(last_run, (int, float)):
            return 0
        delay = max(0.0, self.interval - (current - float(last_run)))
        return min(MAX_SCHEDULE_DELAY_SECONDS, delay)

    async def periodic_loop(self, stop) -> None:
        """Run weekly while the backend lifespan is active."""

        import asyncio

        while not stop.is_set():
            delay = self.seconds_until_due()
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                try:
                    await asyncio.to_thread(self.run_once)
                except Exception:
                    logger.exception("unknown-kind telemetry sweep failed")
