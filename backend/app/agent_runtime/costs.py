"""Incremental cost aggregation for headless agent runtime runs."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ticket import base_ticket


# USD per one million tokens. Cache rates use the standard provider tiers:
# OpenAI cache reads are 10% of input and writes are 125% of input. Anthropic
# cache reads are 10% of input and 5-minute writes are 125% of input.
# Sources: developers.openai.com/api/docs/models/compare and
# docs.anthropic.com/en/docs/about-claude/pricing.
PRICING_USD_PER_MILLION: dict[str, dict[str, float]] = {
    "gpt-5.6-sol": {"input": 5.0, "cache_read": 0.5, "cache_write": 6.25, "output": 30.0},
    "gpt-5.6-terra": {"input": 2.5, "cache_read": 0.25, "cache_write": 3.125, "output": 15.0},
    "gpt-5.6-luna": {"input": 1.0, "cache_read": 0.1, "cache_write": 1.25, "output": 6.0},
    "gpt-5.5": {"input": 2.0, "cache_read": 0.2, "cache_write": 2.5, "output": 8.0},
    "gpt-5.4": {"input": 2.5, "cache_read": 0.25, "cache_write": 3.125, "output": 15.0},
    "gpt-5.4-mini": {"input": 0.75, "cache_read": 0.075, "cache_write": 0.9375, "output": 4.5},
    "gpt-5.3-codex-spark": {"input": 1.5, "cache_read": 0.15, "cache_write": 1.875, "output": 6.0},
    "claude-fable-5": {"input": 10.0, "cache_read": 1.0, "cache_write": 12.5, "output": 50.0},
    "opus-4.7": {"input": 5.0, "cache_read": 0.5, "cache_write": 6.25, "output": 25.0},
    "opus": {"input": 5.0, "cache_read": 0.5, "cache_write": 6.25, "output": 25.0},
    "sonnet": {"input": 3.0, "cache_read": 0.3, "cache_write": 3.75, "output": 15.0},
    "sonnet-4.6": {"input": 3.0, "cache_read": 0.3, "cache_write": 3.75, "output": 15.0},
    "haiku": {"input": 0.8, "cache_read": 0.08, "cache_write": 1.0, "output": 4.0},
    "haiku-4.5": {"input": 0.8, "cache_read": 0.08, "cache_write": 1.0, "output": 4.0},
}

STATE_VERSION = 2
CHUNK_BYTES = 64 * 1024
MAX_EVENT_LINE_BYTES = 4 * 1024 * 1024
MAX_MESSAGE_DEDUPE_IDS = 4096
REFRESH_INTERVAL_SECONDS = float(os.environ.get("WIKI_COST_REFRESH_INTERVAL_SECONDS", "5"))
_REFRESH_LOCK = threading.Lock()


USAGE_FIELDS = ("input", "cache_read", "cache_write", "output")


def runtime_runs_dir() -> Path:
    runtime = Path(os.environ.get("WIKI_AGENT_RUNTIME_DIR") or Path.home() / ".wiki" / "agent-runtime")
    return Path(os.environ.get("WIKI_AGENT_RUNS_DIR") or runtime / "runs").expanduser()


def cost_state_path() -> Path:
    runtime = Path(os.environ.get("WIKI_AGENT_RUNTIME_DIR") or Path.home() / ".wiki" / "agent-runtime")
    return Path(os.environ.get("WIKI_COST_STATE_PATH") or runtime / "cost-aggregation.json").expanduser()


def _empty_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "updated_at": None, "runs": {}, "records": {}}


def _load_state() -> dict[str, Any]:
    try:
        value = json.loads(cost_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_state()
    if value.get("version") != STATE_VERSION or not isinstance(value.get("runs"), dict):
        return _empty_state()
    records = value.get("records")
    if not isinstance(records, dict):
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


def _pick(value: dict[str, Any], *names: str) -> int:
    for name in names:
        if name in value:
            return _number(value[name])
    return 0


def _usage_values(value: object, *, input_includes_cache: bool) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    cache_read = _pick(
        value,
        "cacheReadInputTokens",
        "cachedInputTokens",
        "cache_read_input_tokens",
        "cached_input_tokens",
    )
    cache_write = _pick(
        value,
        "cacheWriteInputTokens",
        "cache_write_input_tokens",
        "cache_creation_input_tokens",
        "cacheCreationInputTokens",
    )
    input_tokens = _pick(value, "inputTokens", "input_tokens")
    if input_includes_cache:
        input_tokens = max(0, input_tokens - cache_read - cache_write)
    result = {
        "input": input_tokens,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "output": _pick(value, "outputTokens", "output_tokens"),
    }
    return result if any(result.values()) else None


def _codex_usage(payload: dict[str, Any], run_state: dict[str, Any]) -> dict[str, int] | None:
    if payload.get("method") != "thread/tokenUsage/updated":
        return None
    params = payload.get("params")
    usage = params.get("tokenUsage") if isinstance(params, dict) else None
    total = usage.get("total") if isinstance(usage, dict) else None
    current = _usage_values(total, input_includes_cache=True)
    if current is None:
        return None
    previous = run_state.get("cumulative")
    run_state["cumulative"] = current
    if not isinstance(previous, dict):
        return current
    if any(current[key] < _number(previous.get(key)) for key in USAGE_FIELDS):
        return {key: 0 for key in USAGE_FIELDS}
    return {key: current[key] - _number(previous.get(key)) for key in USAGE_FIELDS}


def _claude_usage(payload: dict[str, Any], run_state: dict[str, Any]) -> dict[str, int] | None:
    if payload.get("type") != "assistant":
        return None
    message = payload.get("message")
    if not isinstance(message, dict):
        return None
    message_id = message.get("id")
    if isinstance(message_id, str) and message_id:
        seen = run_state.setdefault("seen_message_ids", {})
        if isinstance(seen, list):
            seen = {item: True for item in seen if isinstance(item, str)}
            run_state["seen_message_ids"] = seen
        if message_id in seen:
            return None
        seen[message_id] = True
        while len(seen) > MAX_MESSAGE_DEDUPE_IDS:
            seen.pop(next(iter(seen)))
    return _usage_values(message.get("usage"), input_includes_cache=False)


def _root_path() -> Path:
    return runtime_runs_dir().absolute().resolve(strict=False)


def _inside_root(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root)
    except ValueError:
        return False
    return True


def _safe_open(path: Path, root: Path, mode: int = os.O_RDONLY) -> int | None:
    if not _inside_root(path, root):
        return None
    try:
        return os.open(path, mode | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None


def _read_json(path: Path, root: Path) -> dict[str, Any]:
    fd = _safe_open(path, root)
    if fd is None:
        return {}
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _iter_new_json_lines(path: Path, root: Path, offset: int) -> tuple[Iterator[tuple[int, dict[str, Any]]], list[int]]:
    progress = [offset]

    def read() -> Iterator[tuple[int, dict[str, Any]]]:
        fd = _safe_open(path, root)
        if fd is None:
            return
        try:
            with os.fdopen(fd, "rb") as handle:
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
                        base += len(carry)
                        carry = b""
                        progress[0] = base
                progress[0] = base
        except OSError:
            return

    return read(), progress


def _record_key(day: str, metadata: dict[str, Any], model: str) -> str:
    worker = str(metadata.get("agent_id") or "unknown")
    ticket = base_ticket(worker)
    orchestrator = str(metadata.get("orchestrator_id") or "")
    return "\x1f".join((day, worker, ticket, orchestrator, model))


def _new_record(key: str, metadata: dict[str, Any], model: str, day: str) -> dict[str, Any]:
    worker = str(metadata.get("agent_id") or "unknown")
    return {
        "key": key,
        "day": day,
        "worker": worker,
        "ticket": base_ticket(worker),
        "orchestrator": str(metadata.get("orchestrator_id") or ""),
        "model": model,
        **{field: 0 for field in USAGE_FIELDS},
    }


def _add_contribution(
    state: dict[str, Any],
    run_state: dict[str, Any],
    key: str,
    metadata: dict[str, Any],
    model: str,
    day: str,
    usage: dict[str, int],
) -> None:
    records = state["records"]
    record = records.setdefault(key, _new_record(key, metadata, model, day))
    local = run_state.setdefault("records", {}).setdefault(key, {field: 0 for field in USAGE_FIELDS})
    for field in USAGE_FIELDS:
        amount = int(usage[field])
        record[field] += amount
        local[field] += amount


def _remove_run_contributions(state: dict[str, Any], run_state: dict[str, Any]) -> None:
    for key, local in (run_state.get("records") or {}).items():
        record = state["records"].get(key)
        if not isinstance(record, dict):
            continue
        for field in USAGE_FIELDS:
            record[field] -= _number(local.get(field))
        if not any(record[field] for field in USAGE_FIELDS):
            state["records"].pop(key, None)


def _scan_run(state: dict[str, Any], run_dir: Path, root: Path) -> None:
    run_id = run_dir.name
    raw_path = run_dir / "raw.jsonl"
    try:
        raw_stat = raw_path.stat(follow_symlinks=False)
    except OSError:
        old_run = state["runs"].pop(run_id, None)
        if isinstance(old_run, dict):
            _remove_run_contributions(state, old_run)
        return
    if not stat.S_ISREG(raw_stat.st_mode) or not _inside_root(raw_path, root):
        old_run = state["runs"].pop(run_id, None)
        if isinstance(old_run, dict):
            _remove_run_contributions(state, old_run)
        return
    metadata = _read_json(run_dir / "run.json", root)
    metadata.setdefault("agent_id", run_id)
    run_state = state["runs"].get(run_id)
    if not isinstance(run_state, dict):
        run_state = {"offset": 0, "cumulative": None, "seen_message_ids": {}, "records": {}}
        state["runs"][run_id] = run_state
    if raw_stat.st_size < _number(run_state.get("offset")) or raw_stat.st_ino != _number(run_state.get("inode")):
        _remove_run_contributions(state, run_state)
        run_state = {"offset": 0, "cumulative": None, "seen_message_ids": {}, "records": {}}
        state["runs"][run_id] = run_state
    iterator, progress = _iter_new_json_lines(raw_path, root, _number(run_state.get("offset")))
    provider = str(metadata.get("provider") or "")
    model = str(metadata.get("model") or "unknown")
    for _line_start, envelope in iterator:
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            continue
        usage = _codex_usage(payload, run_state) if provider in {"codex", "cdx"} else _claude_usage(payload, run_state)
        if usage is None:
            continue
        timestamp = envelope.get("received_at") or envelope.get("timestamp")
        event_dt = _parse_timestamp(timestamp)
        if event_dt is not None:
            iso = event_dt.isoformat().replace("+00:00", "Z")
            run_state.setdefault("first_event_at", iso)
            run_state["last_event_at"] = iso
            run_state["event_tokens"] = _number(run_state.get("event_tokens")) + sum(usage.values())
        day = _event_day(timestamp)
        if day is not None and any(usage.values()):
            _add_contribution(state, run_state, _record_key(day, metadata, model), metadata, model, day, usage)
    run_state.update(
        {
            "offset": progress[0],
            "size": raw_stat.st_size,
            "inode": raw_stat.st_ino,
            "prompt_chars": len(str(metadata.get("initial_prompt") or "")),
            "prompt_tokens_estimate": max(0, len(str(metadata.get("initial_prompt") or "")) // 4),
            "model": model,
            "worker": str(metadata.get("agent_id") or run_id),
            "ticket": base_ticket(str(metadata.get("agent_id") or run_id)),
            "orchestrator": str(metadata.get("orchestrator_id") or ""),
            "created_at": metadata.get("created_at"),
        }
    )


def refresh(state: dict[str, Any] | None = None) -> dict[str, Any]:
    if state is None:
        state = _load_state()
    root = _root_path()
    seen_runs: set[str] = set()
    try:
        entries = list(os.scandir(root))
    except OSError:
        entries = []
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            continue
        run_dir = Path(entry.path)
        if not _inside_root(run_dir, root):
            continue
        seen_runs.add(entry.name)
        _scan_run(state, run_dir, root)
    for run_id in list(state["runs"]):
        if run_id not in seen_runs:
            _remove_run_contributions(state, state["runs"][run_id])
            state["runs"].pop(run_id, None)
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


async def background_loop() -> None:
    while True:
        await refresh_in_background()
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)


def _state_stale(state: dict[str, Any]) -> bool:
    if not isinstance(state.get("updated_at"), str):
        return True
    parsed = _parse_timestamp(state["updated_at"])
    return parsed is None or (datetime.now(timezone.utc) - parsed).total_seconds() > REFRESH_INTERVAL_SECONDS * 2


def _money_and_pricing(model: str, usage: dict[str, Any]) -> tuple[float | None, str, int]:
    rates = PRICING_USD_PER_MILLION.get(model)
    tokens = sum(_number(usage.get(field)) for field in USAGE_FIELDS)
    if rates is None:
        return None, "unpriced", tokens
    cost = sum(_number(usage.get(field)) * rates[field] / 1_000_000 for field in USAGE_FIELDS)
    return cost, "priced", 0


def _rollup(items: list[dict[str, Any]], label_key: str, label: str | None = None) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        name = label if label is not None else str(item.get(label_key) or "(none)")
        row = grouped.setdefault(
            name,
            {"label": name, **{field: 0 for field in USAGE_FIELDS}, "cost_usd": 0.0, "unpriced_tokens": 0, "models": set(), "pricing_flags": set()},
        )
        for field in USAGE_FIELDS:
            row[field] += _number(item.get(field))
        row["models"].add(item.get("model") or "unknown")
        cost, pricing, unpriced = _money_and_pricing(str(item.get("model") or "unknown"), item)
        if cost is not None:
            row["cost_usd"] += cost
        row["unpriced_tokens"] += unpriced
        row["pricing_flags"].add(pricing)
    output = []
    for row in grouped.values():
        has_priced = "priced" in row["pricing_flags"]
        has_unpriced = row["unpriced_tokens"] > 0
        row["pricing"] = "mixed" if has_priced and has_unpriced else "unpriced" if has_unpriced else "priced"
        row["cost_usd"] = round(row["cost_usd"], 8) if has_priced else None
        row["cached"] = row["cache_read"] + row["cache_write"]
        row["total_tokens"] = sum(row[field] for field in USAGE_FIELDS)
        row["models"] = sorted(row["models"])
        row.pop("pricing_flags")
        output.append(row)
    output.sort(key=lambda item: (item["cost_usd"] is not None, item["cost_usd"] or 0, item["total_tokens"]), reverse=True)
    return output


def _range_matches_day(day: str, from_dt: datetime | None, to_dt: datetime | None) -> bool:
    parsed = _parse_timestamp(f"{day}T00:00:00Z")
    return parsed is not None and not (from_dt and parsed.date() < from_dt.date()) and not (to_dt and parsed.date() >= to_dt.date())


def _matching_runs(state: dict[str, Any], records: list[dict[str, Any]], ticket: str | None) -> list[dict[str, Any]]:
    keys = {str(record.get("key") or "") for record in records}
    wanted = base_ticket(ticket) if ticket else None
    return [
        run
        for run in state["runs"].values()
        if isinstance(run, dict)
        and keys.intersection((run.get("records") or {}).keys())
        and (wanted is None or base_ticket(str(run.get("worker"))) == wanted)
    ]


def _prompt_distribution(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bins = (("0-1k", 0, 1_000), ("1k-4k", 1_000, 4_000), ("4k-16k", 4_000, 16_000), ("16k+", 16_000, None))
    return [
        {"bucket": label, "runs": sum(1 for run in runs if _number(run.get("prompt_tokens_estimate")) >= lower and (upper is None or _number(run.get("prompt_tokens_estimate")) < upper))}
        for label, lower, upper in bins
    ]


def _query_state(state: dict[str, Any], from_ts: str | None = None, to_ts: str | None = None, ticket: str | None = None) -> dict[str, Any]:
    from_dt = _parse_timestamp(from_ts) if from_ts else None
    to_dt = _parse_timestamp(to_ts) if to_ts else None
    records = [
        record
        for record in state["records"].values()
        if (not ticket or base_ticket(str(record.get("ticket") or "")) == base_ticket(ticket))
        and _range_matches_day(str(record.get("day") or ""), from_dt, to_dt)
    ]
    totals = _rollup(records, "ticket", "all")
    all_total = totals[0] if totals else {"label": "all", **{field: 0 for field in USAGE_FIELDS}, "cached": 0, "total_tokens": 0, "cost_usd": 0.0, "unpriced_tokens": 0, "pricing": "priced", "models": []}
    runs = _matching_runs(state, records, ticket)
    if from_dt or to_dt:
        selected_days = [_parse_timestamp(f"{record['day']}T00:00:00Z") for record in records]
        selected_days = [value for value in selected_days if value]
        starts = selected_days
        ends = [value.replace(hour=23, minute=59, second=59) for value in selected_days]
    else:
        starts = [_parse_timestamp(run.get("first_event_at")) for run in runs]
        ends = [_parse_timestamp(run.get("last_event_at")) for run in runs]
    starts = [value for value in starts if value]
    ends = [value for value in ends if value]
    span = max(60, int((max(ends) - min(starts)).total_seconds()) if starts and ends else 60)
    velocity = {"tokens_per_minute": round(all_total["total_tokens"] / (span / 60), 2), "window_seconds": span, "tokens": all_total["total_tokens"]}
    return {
        "updated_at": state.get("updated_at"),
        "totals": all_total,
        "top": {"worker": _rollup(records, "worker"), "ticket": _rollup(records, "ticket"), "orchestrator": _rollup(records, "orchestrator"), "day": _rollup(records, "day")},
        "prompt_size_distribution": _prompt_distribution(runs),
        "velocity": velocity,
        "runs_scanned": len(runs),
        "refreshing": False,
    }


def query(from_ts: str | None = None, to_ts: str | None = None, ticket: str | None = None) -> dict[str, Any]:
    state = _load_state()
    response = _query_state(state, from_ts, to_ts, ticket)
    response["refreshing"] = _state_stale(state)
    return response
