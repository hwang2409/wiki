"""Weekly telemetry for provider event kinds the normalizer does not know."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .normalizer import NormalizedProviderEvent, normalize_provider_event
from .store import RuntimePaths
from .types import EventDisposition, ProviderKind


logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_UNKNOWN_KIND_THRESHOLD = 100
STATE_VERSION = 1


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
        "filed_kinds": [],
    }


def _todo_runner(text: str) -> None:
    """File one todo item through the repository's CLI."""

    cli = Path(__file__).resolve().parents[3] / "wiki"
    subprocess.run(
        [sys.executable, str(cli), "todo", "add", text, "--section", "Todo"],
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
        if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
            return _default_state(timestamp)
        result = _default_state(timestamp)
        result.update(state)
        for key in ("cursors", "unknown_counts"):
            if not isinstance(result[key], dict):
                result[key] = {}
        for key in ("covered_kinds", "filed_kinds"):
            if not isinstance(result[key], list):
                result[key] = []
        return result

    def _save_state(self, state: dict[str, Any]) -> None:
        _atomic_write_json(self.state_path, state)

    def _start_week(self, state: dict[str, Any], timestamp: float) -> None:
        current_week = _week_start(timestamp)
        if state.get("week_start") == current_week:
            return
        state["week_start"] = current_week
        state["unknown_counts"] = {}

    @staticmethod
    def _cursor_for(state: dict[str, Any], run_name: str, path: Path) -> dict[str, Any]:
        cursors = state["cursors"]
        cursor = cursors.get(run_name)
        if not isinstance(cursor, dict):
            cursor = {}
            cursors[run_name] = cursor
        try:
            stat = path.stat()
        except OSError:
            return cursor
        if (
            cursor.get("device") != stat.st_dev
            or cursor.get("inode") != stat.st_ino
            or int(cursor.get("offset", 0)) > stat.st_size
        ):
            cursor.clear()
            cursor.update({"offset": 0, "device": stat.st_dev, "inode": stat.st_ino})
        return cursor

    def _scan_run(self, state: dict[str, Any], run_dir: Path) -> int:
        path = run_dir / "raw.jsonl"
        if not path.is_file():
            return 0
        cursor = self._cursor_for(state, run_dir.name, path)
        offset = int(cursor.get("offset", 0))
        scanned = 0
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                while True:
                    line = handle.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        break
                    cursor["offset"] = handle.tell()
                    scanned += 1
                    try:
                        envelope = json.loads(line.decode("utf-8"))
                        provider = ProviderKind(str(envelope["provider"]))
                        payload = envelope["payload"]
                        direction = str(envelope.get("direction", "provider"))
                        if not isinstance(payload, dict):
                            raise ValueError("payload is not an object")
                    except (UnicodeDecodeError, KeyError, TypeError, ValueError):
                        self._save_state(state)
                        continue
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
                        counts = state["unknown_counts"]
                        counts[kind] = int(counts.get(kind, 0)) + 1
                    else:
                        covered = set(state["covered_kinds"])
                        covered.add(kind)
                        state["covered_kinds"] = sorted(covered)
                    # Persist every complete line. A crash can therefore lose
                    # at most the incomplete final line, never a multi-GB scan.
                    self._save_state(state)
        except OSError:
            logger.exception("unknown-kind telemetry could not scan %s", path)
        return scanned

    def _file_todos(self, state: dict[str, Any]) -> list[str]:
        covered = set(state["covered_kinds"])
        filed = set(state["filed_kinds"])
        filed_now: list[str] = []
        for kind, count in sorted(state["unknown_counts"].items()):
            if int(count) <= self.threshold or kind in covered or kind in filed:
                continue
            text = (
                f'unknown provider event kind "{kind}" exceeded '
                f"{self.threshold} events in the week of {state['week_start']}"
            )
            # Record before the subprocess. This gives the external, non-
            # transactional CLI an at-most-once durable invocation contract.
            filed.add(kind)
            state["filed_kinds"] = sorted(filed)
            self._save_state(state)
            try:
                self.todo_runner(text)
            except Exception:
                logger.exception("unknown-kind telemetry todo add failed for %s", kind)
            else:
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
            runs_dir = self.paths.runs_dir
            if runs_dir.is_dir():
                for run_dir in sorted(runs_dir.iterdir(), key=lambda item: item.name):
                    if not run_dir.is_dir():
                        continue
                    scanned_runs += 1
                    scanned_events += self._scan_run(state, run_dir)
            filed_now = self._file_todos(state)
            state["last_run_at"] = current
            self._save_state(state)
            return {
                "week_start": state["week_start"],
                "scanned_runs": scanned_runs,
                "scanned_events": scanned_events,
                "unknown_counts": dict(state["unknown_counts"]),
                "filed_kinds": filed_now,
                "last_run_at": current,
            }

    def seconds_until_due(self, *, timestamp: float | None = None) -> float:
        current = self.clock() if timestamp is None else timestamp
        state = self._load_state(current)
        last_run = state.get("last_run_at")
        if not isinstance(last_run, (int, float)):
            return 0
        return max(0.0, self.interval - (current - float(last_run)))

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
