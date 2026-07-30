"""Incremental cost aggregation for headless agent runtime runs.

The raw event logs can grow to multiple gigabytes. This module reads them in
bounded byte chunks and persists cursors with the aggregate in one atomic
snapshot. A failed write leaves both the old cursor and old aggregate intact.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Rates are USD per one million tokens. Keep this table here so pricing changes
# have one reviewable source of truth. Unknown models stay explicitly unpriced.
PRICING_USD_PER_MILLION: dict[str, dict[str, float]] = {
    "gpt-5.6-sol": {"input": 3.0, "output": 12.0},
    "gpt-5.6-terra": {"input": 3.0, "output": 12.0},
    "gpt-5.6-luna": {"input": 3.0, "output": 12.0},
    "gpt-5.5": {"input": 2.5, "output": 10.0},
    "gpt-5.4": {"input": 2.5, "output": 10.0},
    "gpt-5.4-mini": {"input": 0.3, "output": 1.2},
    "gpt-5.3-codex-spark": {"input": 0.3, "output": 1.2},
    "claude-fable-5": {"input": 5.0, "output": 25.0},
    "opus-4.7": {"input": 15.0, "output": 75.0},
    "opus": {"input": 15.0, "output": 75.0},
    "sonnet": {"input": 3.0, "output": 15.0},
    "sonnet-4.6": {"input": 3.0, "output": 15.0},
    "haiku": {"input": 0.8, "output": 4.0},
    "haiku-4.5": {"input": 0.8, "output": 4.0},
}

STATE_VERSION = 1
CHUNK_BYTES = 64 * 1024
MAX_EVENT_LINE_BYTES = 4 * 1024 * 1024
REFRESH_MAX_AGE_SECONDS = int(os.environ.get("WIKI_COST_REFRESH_MAX_AGE_SECONDS", "15"))
_REFRESH_LOCK = threading.Lock()


def runtime_runs_dir() -> Path:
    return Path(
        os.environ.get("WIKI_AGENT_RUNS_DIR")
        or Path(os.environ.get("WIKI_AGENT_RUNTIME_DIR") or Path.home() / ".wiki" / "agent-runtime")
        / "runs"
    ).expanduser()


def cost_state_path() -> Path:
    return Path(
        os.environ.get("WIKI_COST_STATE_PATH")
        or Path(os.environ.get("WIKI_AGENT_RUNTIME_DIR") or Path.home() / ".wiki" / "agent-runtime")
        / "cost-aggregation.json"
    ).expanduser()


def _empty_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "updated_at": None, "runs": {}, "records": []}


def _load_state() -> dict[str, Any]:
    try:
        value = json.loads(cost_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_state()
    if not isinstance(value, dict) or value.get("version") != STATE_VERSION:
        return _empty_state()
    if not isinstance(value.get("runs"), dict) or not isinstance(value.get("records"), list):
        return _empty_state()
    value.setdefault("updated_at", None)
    return value


def _save_state(state: dict[str, Any]) -> None:
    path = cost_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        tmp = Path(raw_tmp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            try:
                dir_fd = os.open(path.parent, os.O_RDONLY)
            except OSError:
                return
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            tmp.unlink(missing_ok=True)
    except OSError:
        return


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _event_day(value: object) -> str | None:
    parsed = _parse_timestamp(value)
    return parsed.date().isoformat() if parsed else None


def _number(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _usage_values(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None

    def pick(*names: str) -> int:
        for name in names:
            if name in value:
                return _number(value[name])
        return 0

    result = {
        "input": pick("inputTokens", "input_tokens"),
        "cached": pick("cachedInputTokens", "cached_input_tokens", "cache_read_input_tokens"),
        "output": pick("outputTokens", "output_tokens"),
        "reasoning": pick("reasoningOutputTokens", "reasoning_output_tokens"),
    }
    return result if any(result.values()) else None


def _codex_usage(payload: dict[str, Any], run_state: dict[str, Any]) -> dict[str, int] | None:
    if payload.get("method") != "thread/tokenUsage/updated":
        return None
    params = payload.get("params")
    usage = params.get("tokenUsage") if isinstance(params, dict) else None
    total = usage.get("total") if isinstance(usage, dict) else None
    current = _usage_values(total)
    if current is None:
        return None
    previous = run_state.get("cumulative")
    run_state["cumulative"] = current
    if not isinstance(previous, dict):
        return current
    if any(current[key] < _number(previous.get(key)) for key in current):
        return {key: 0 for key in current}
    return {key: current[key] - _number(previous.get(key)) for key in current}


def _claude_usage(payload: dict[str, Any], run_state: dict[str, Any]) -> dict[str, int] | None:
    if payload.get("type") != "assistant":
        return None
    message = payload.get("message")
    if not isinstance(message, dict):
        return None
    message_id = message.get("id")
    if isinstance(message_id, str) and message_id:
        seen = run_state.setdefault("seen_message_ids", [])
        if message_id in seen:
            return None
        seen.append(message_id)
    return _usage_values(message.get("usage"))


def _iter_new_json_lines(path: Path, offset: int) -> tuple[Iterator[tuple[int, dict[str, Any]]], list[int]]:
    """Return a bounded iterator and its final complete-line byte offset."""

    progress = [offset]

    def read() -> Iterator[tuple[int, dict[str, Any]]]:
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                carry = b""
                base = offset
                while chunk := handle.read(CHUNK_BYTES):
                    data = carry + chunk
                    start = 0
                    while True:
                        end = data.find(b"\n", start)
                        if end < 0:
                            break
                        line = data[start:end]
                        line_start = base + start
                        progress[0] = base + end + 1
                        start = end + 1
                        if not line or len(line) > MAX_EVENT_LINE_BYTES:
                            continue
                        try:
                            row = json.loads(line)
                        except (ValueError, UnicodeDecodeError):
                            continue
                        if isinstance(row, dict):
                            yield line_start, row
                    carry = data[start:]
                    base += start
                    if len(carry) > MAX_EVENT_LINE_BYTES:
                        carry = b""
                        base += len(data[start:])
                        progress[0] = base
        except OSError:
            return
        # A partial line remains unconsumed. The next refresh starts at base.
        progress[0] = base

    iterator = read()
    return iterator, progress


def _read_run_metadata(run_dir: Path, run_id: str) -> dict[str, Any]:
    try:
        value = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {"agent_id": run_id}


def _record_key(day: str, metadata: dict[str, Any], model: str) -> tuple[str, str, str, str, str]:
    worker = str(metadata.get("agent_id") or "unknown")
    ticket = worker
    orchestrator = str(metadata.get("orchestrator_id") or "")
    return day, worker, ticket, orchestrator, model


def _add_record(state: dict[str, Any], day: str, metadata: dict[str, Any], model: str, usage: dict[str, int]) -> None:
    key = _record_key(day, metadata, model)
    record = next(
        (item for item in state["records"] if tuple(item.get(name) for name in ("day", "worker", "ticket", "orchestrator", "model")) == key),
        None,
    )
    if record is None:
        record = {
            "day": key[0],
            "worker": key[1],
            "ticket": key[2],
            "orchestrator": key[3],
            "model": key[4],
            "input": 0,
            "cached": 0,
            "output": 0,
            "reasoning": 0,
        }
        state["records"].append(record)
    for name, value in usage.items():
        record[name] += int(value)


def _scan_run(state: dict[str, Any], run_dir: Path) -> None:
    run_id = run_dir.name
    raw_path = run_dir / "raw.jsonl"
    try:
        stat = raw_path.stat()
    except OSError:
        return
    metadata = _read_run_metadata(run_dir, run_id)
    run_state = state["runs"].get(run_id)
    if not isinstance(run_state, dict) or stat.st_size < _number(run_state.get("offset")):
        run_state = {"offset": 0, "cumulative": None, "seen_message_ids": []}
        state["runs"][run_id] = run_state
    iterator, progress = _iter_new_json_lines(raw_path, _number(run_state.get("offset")))
    provider = str(metadata.get("provider") or "")
    model = str(metadata.get("model") or "unknown")
    for _line_start, envelope in iterator:
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            continue
        usage = _codex_usage(payload, run_state) if provider == "codex" else _claude_usage(payload, run_state)
        if usage is None:
            continue
        event_timestamp = envelope.get("received_at") or envelope.get("timestamp")
        event_dt = _parse_timestamp(event_timestamp)
        if event_dt is not None:
            event_iso = event_dt.isoformat().replace("+00:00", "Z")
            if not run_state.get("first_event_at"):
                run_state["first_event_at"] = event_iso
            run_state["last_event_at"] = event_iso
            run_state["event_tokens"] = _number(run_state.get("event_tokens")) + usage["input"] + usage["output"]
        day = _event_day(event_timestamp)
        if day is not None and any(usage.values()):
            _add_record(state, day, metadata, model, usage)
    run_state["offset"] = progress[0]
    run_state["size"] = stat.st_size
    run_state["prompt_chars"] = len(str(metadata.get("initial_prompt") or ""))
    run_state["prompt_tokens_estimate"] = max(0, run_state["prompt_chars"] // 4)
    run_state["model"] = model
    run_state["worker"] = str(metadata.get("agent_id") or run_id)
    run_state["ticket"] = str(metadata.get("agent_id") or run_id)
    run_state["orchestrator"] = str(metadata.get("orchestrator_id") or "")
    run_state["created_at"] = metadata.get("created_at")


def refresh(state: dict[str, Any] | None = None) -> dict[str, Any]:
    if state is None:
        state = _load_state()
    runs_dir = runtime_runs_dir()
    try:
        run_dirs = sorted(path for path in runs_dir.iterdir() if path.is_dir())
    except OSError:
        run_dirs = []
    for run_dir in run_dirs:
        _scan_run(state, run_dir)
    state["updated_at"] = _now_iso()
    _save_state(state)
    return state


async def refresh_in_background() -> bool:
    if not _REFRESH_LOCK.acquire(blocking=False):
        return False
    try:
        await asyncio.to_thread(refresh)
    finally:
        _REFRESH_LOCK.release()
    return True


def _state_stale(state: dict[str, Any]) -> bool:
    if not isinstance(state.get("updated_at"), str):
        return True
    parsed = _parse_timestamp(state["updated_at"])
    return parsed is None or (datetime.now(timezone.utc) - parsed).total_seconds() > REFRESH_MAX_AGE_SECONDS


def _money_and_pricing(model: str, usage: dict[str, int]) -> tuple[float | None, str, int]:
    rates = PRICING_USD_PER_MILLION.get(model)
    total_tokens = usage["input"] + usage["output"]
    if rates is None:
        return None, "unpriced", total_tokens
    cost = usage["input"] * rates["input"] / 1_000_000 + usage["output"] * rates["output"] / 1_000_000
    return cost, "priced", 0


def _rollup(items: list[dict[str, Any]], label_key: str, label: str | None = None) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        name = label if label is not None else str(item.get(label_key) or "(none)")
        row = grouped.setdefault(name, {"label": name, "input": 0, "cached": 0, "output": 0, "reasoning": 0, "cost_usd": 0.0, "unpriced_tokens": 0, "models": set()})
        for field in ("input", "cached", "output", "reasoning"):
            row[field] += int(item.get(field, 0))
        row["models"].add(item.get("model") or "unknown")
        cost, pricing, unpriced = _money_and_pricing(str(item.get("model") or "unknown"), item)
        if cost is not None:
            row["cost_usd"] += cost
        row["unpriced_tokens"] += unpriced
        row.setdefault("priced_models", set()).add(pricing == "priced")
    result = []
    for row in grouped.values():
        has_priced = bool(row.pop("priced_models") & {True})
        has_unpriced = row["unpriced_tokens"] > 0
        row["pricing"] = "mixed" if has_priced and has_unpriced else "unpriced" if has_unpriced else "priced"
        row["cost_usd"] = round(row["cost_usd"], 8) if has_priced else None
        row["total_tokens"] = row["input"] + row["output"]
        row["models"] = sorted(row["models"])
        result.append(row)
    result.sort(key=lambda item: (item["cost_usd"] is not None, item["cost_usd"] or 0, item["total_tokens"]), reverse=True)
    return result


def _prompt_distribution(state: dict[str, Any]) -> list[dict[str, Any]]:
    bins = (("0-1k", 0, 1_000), ("1k-4k", 1_000, 4_000), ("4k-16k", 4_000, 16_000), ("16k+", 16_000, None))
    output = []
    for label, lower, upper in bins:
        count = 0
        for run in state["runs"].values():
            tokens = _number(run.get("prompt_tokens_estimate"))
            if tokens >= lower and (upper is None or tokens < upper):
                count += 1
        output.append({"bucket": label, "runs": count})
    return output


def _query_state(state: dict[str, Any], from_ts: str | None = None, to_ts: str | None = None, ticket: str | None = None) -> dict[str, Any]:
    from_dt = _parse_timestamp(from_ts) if from_ts else None
    to_dt = _parse_timestamp(to_ts) if to_ts else None
    records = []
    for record in state["records"]:
        if ticket and record.get("ticket") != ticket:
            continue
        day = _parse_timestamp(f"{record.get('day')}T00:00:00Z")
        if day is None or (from_dt and day.date() < from_dt.date()) or (to_dt and day.date() >= to_dt.date()):
            continue
        records.append(record)
    totals = _rollup(records, "ticket", "all")
    all_total = totals[0] if totals else {"label": "all", "input": 0, "cached": 0, "output": 0, "reasoning": 0, "total_tokens": 0, "cost_usd": 0.0, "unpriced_tokens": 0, "pricing": "priced", "models": []}
    event_starts = [parsed for run in state["runs"].values() if (parsed := _parse_timestamp(run.get("first_event_at")))]
    event_ends = [parsed for run in state["runs"].values() if (parsed := _parse_timestamp(run.get("last_event_at")))]
    observed_seconds = max(
        60,
        int((max(event_ends) - min(event_starts)).total_seconds()) if event_starts and event_ends else 60,
    )
    velocity = {"tokens_per_minute": round(all_total["total_tokens"] / (observed_seconds / 60), 2), "window_seconds": observed_seconds, "tokens": all_total["total_tokens"]}
    return {
        "updated_at": state.get("updated_at"),
        "totals": all_total,
        "top": {"worker": _rollup(records, "worker"), "ticket": _rollup(records, "ticket"), "orchestrator": _rollup(records, "orchestrator"), "day": _rollup(records, "day")},
        "prompt_size_distribution": _prompt_distribution(state),
        "velocity": velocity,
        "runs_scanned": len(state["runs"]),
        "refreshing": False,
    }


def query(from_ts: str | None = None, to_ts: str | None = None, ticket: str | None = None) -> dict[str, Any]:
    state = _load_state()
    if _REFRESH_LOCK.acquire(blocking=False):
        try:
            state = refresh(state)
        finally:
            _REFRESH_LOCK.release()
    else:
        state = _load_state()
    response = _query_state(state, from_ts, to_ts, ticket)
    response["refreshing"] = _state_stale(state)
    return response
