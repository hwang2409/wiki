"""Token-usage aggregator over local CLI session files.

Sources (read-only):
  Codex   — ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
            event_msg/token_count carries CUMULATIVE total_token_usage; deltas
            come from diffing consecutive events per session. Cumulative can
            reset (resume/rotate) — a negative diff means "new baseline", emit
            zero delta and re-anchor.
            turn_context rows switch the active model mid-session; the delta
            following a turn_context is attributed to that model.
  Claude  — ~/.claude/projects/**/*.jsonl (including subagents/agent-*.jsonl)
            assistant rows carry per-message message.usage {input, output,
            cache_read_input_tokens, cache_creation_input_tokens} plus a
            message.id used to dedupe streaming/retry duplicates.

Hourly buckets keyed by (cli, model). Aggregation is incremental: per-file
byte offset + persisted state cached at WIKI_TOKEN_CACHE_PATH so a rescan is
tail-append only. All parsing is defensive — CLI formats are unversioned.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

# Single-flight guard: a full first scan takes minutes of disk-wait, and every
# concurrent /api/tokens request that also scanned would stack another
# IO-bound thread until the threadpool starved ALL sync endpoints (live
# incident 2026-07-08). Losers of the race serve the last persisted snapshot.
_REFRESH_LOCK = threading.Lock()

CACHE_VERSION = 4  # v3 zero-filled the cumulative-token path and unioned
                    # `provided` file-wide, so a mid-session model switch lost
                    # tokens (input/output nulled to 0 alongside a disappeared
                    # reasoning field) and plain non-reasoning models still
                    # advertised reasoning as available. v4 keeps cumulative
                    # fields optional end-to-end and scopes reasoning
                    # availability per-model at event time.
METRIC_KEYS: tuple[str, ...] = ("input", "cached", "output", "reasoning")
SYNC_REFRESH_MAX_AGE_SECONDS = int(os.environ.get("WIKI_TOKEN_SYNC_MAX_AGE_SECONDS", "15"))

BUCKET_HOUR = "hour"
BUCKET_DAY = "day"


def codex_sessions_dir() -> Path:
    return Path(
        os.environ.get("WIKI_CODEX_SESSIONS_DIR", str(Path.home() / ".codex" / "sessions"))
    )


def claude_projects_dir() -> Path:
    return Path(
        os.environ.get("WIKI_CLAUDE_PROJECTS_DIR", str(Path.home() / ".claude" / "projects"))
    )


def token_cache_path() -> Path:
    return Path(
        os.environ.get("WIKI_TOKEN_CACHE_PATH", str(Path.home() / ".wiki" / "token-cache.json"))
    )


# --------------------------------------------------------------------- state


def _empty_state() -> dict:
    return {
        "version": CACHE_VERSION,
        "updated_at": None,
        # path -> {offset, mtime, size, cli, model, cum (dict|None), sess_id}
        "files": {},
        # list of {ts (iso hour), cli, model, input, cached, output, reasoning}
        "buckets": [],
        # Global msg-id dedupe across all claude project/subagent files —
        # 277/21,521 real ids appear in >=2 files (resume/fork replays);
        # per-file dedupe double-counts ~1.3%. Persisted as a list; loaded
        # into a set for O(1) membership.
        "seen_msg_ids": [],
    }


def _load_state() -> dict:
    try:
        raw = json.loads(token_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_state()
    if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
        return _empty_state()
    raw.setdefault("updated_at", None)
    raw.setdefault("files", {})
    raw.setdefault("buckets", [])
    raw.setdefault("seen_msg_ids", [])
    # Rehydrate the on-disk id list into an in-memory set for O(1) lookup;
    # _save_state serialises it back to a sorted list on the way out.
    raw["_seen_msg_ids_set"] = set(raw["seen_msg_ids"])
    return raw


def _save_state(state: dict) -> None:
    try:
        token_cache_path().parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    tmp = token_cache_path().with_suffix(".tmp")
    # Freeze the in-memory set back to a stable on-disk list. Keep the set
    # off-disk (private "_"-prefixed key) so JSON doesn't choke on it.
    seen = state.get("_seen_msg_ids_set")
    if isinstance(seen, set):
        state["seen_msg_ids"] = sorted(seen)
    serialisable = {k: v for k, v in state.items() if not k.startswith("_")}
    try:
        tmp.write_text(json.dumps(serialisable), encoding="utf-8")
        tmp.replace(token_cache_path())
    except OSError:
        return


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------- helpers


def _floor_hour(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        # jsonl timestamps end with 'Z'; datetime doesn't parse 'Z' pre-3.11 the
        # same way — normalise first.
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    dt = dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")


def _bucket_index(buckets: list[dict]) -> dict[tuple[str, str, str], int]:
    return {(b["ts"], b["cli"], b["model"]): i for i, b in enumerate(buckets)}


def _add_delta(
    state: dict,
    ts: str | None,
    cli: str,
    model: str | None,
    delta: dict[str, int],
    *,
    provided: Iterable[str],
    index: dict[tuple[str, str, str], int] | None = None,
) -> None:
    ts_hour = _floor_hour(ts)
    if ts_hour is None:
        return
    model = model or "unknown"
    provided_set = {k for k in provided if k in METRIC_KEYS}
    if not any(delta.get(k) for k in METRIC_KEYS):
        return
    key = (ts_hour, cli, model)
    if index is None:
        index = _bucket_index(state["buckets"])
    idx = index.get(key)
    if idx is None:
        state["buckets"].append(
            {
                "ts": ts_hour,
                "cli": cli,
                "model": model,
                "input": int(delta.get("input", 0)),
                "cached": int(delta.get("cached", 0)),
                "output": int(delta.get("output", 0)),
                "reasoning": int(delta.get("reasoning", 0)),
                "provided": sorted(provided_set),
            }
        )
        index[key] = len(state["buckets"]) - 1
    else:
        b = state["buckets"][idx]
        b["input"] += int(delta.get("input", 0))
        b["cached"] += int(delta.get("cached", 0))
        b["output"] += int(delta.get("output", 0))
        b["reasoning"] += int(delta.get("reasoning", 0))
        existing = set(b.get("provided") or [])
        b["provided"] = sorted(existing | provided_set)


# --------------------------------------------------------------------- codex


def _codex_apply(state: dict, file_state: dict, row: dict, index: dict) -> None:
    ts = row.get("timestamp")
    rtype = row.get("type")
    payload = row.get("payload") or {}
    if not isinstance(payload, dict):
        return

    if rtype == "session_meta":
        model = payload.get("model")
        if isinstance(model, str) and model:
            file_state["model"] = model
        sid = payload.get("id")
        if isinstance(sid, str):
            file_state["sess_id"] = sid
        return

    if rtype == "turn_context":
        model = payload.get("model")
        if isinstance(model, str) and model:
            file_state["model"] = model
        return

    if rtype != "event_msg":
        return
    if payload.get("type") != "token_count":
        return

    info = payload.get("info") or {}
    total = info.get("total_token_usage")
    if not isinstance(total, dict):
        return

    # Only pick up numeric cumulative fields — never fabricate 0 for a
    # missing / null entry. WIKI-157 round-3: on a mid-session model switch,
    # codex frequently drops the reasoning field entirely; the old
    # `int(... or 0)` path both fake-reported reasoning: 0 AND tripped the
    # group drop-clamp, zeroing the delta across every co-reported metric
    # and losing real input / output activity.
    cum_event: dict[str, int] = {}
    for src, dst in (
        ("input_tokens", "input"),
        ("cached_input_tokens", "cached"),
        ("output_tokens", "output"),
        ("reasoning_output_tokens", "reasoning"),
    ):
        raw = total.get(src)
        if isinstance(raw, (int, float)):
            cum_event[dst] = int(raw)
    if not cum_event:
        return

    prev: dict[str, int] = file_state.get("cum") or {}
    delta: dict[str, int] = {}
    for k, v in cum_event.items():
        p = prev.get(k)
        if p is None:
            # First appearance of this metric on this file — cumulative IS
            # the delta.
            delta[k] = v
        elif v < p:
            # Per-metric re-anchor (resume/rotate). Zero this metric only —
            # co-reported metrics that continued to advance still contribute.
            delta[k] = 0
        else:
            delta[k] = v - p

    # Availability semantics per model (round-3 review): a plain
    # non-reasoning model that ships reasoning_output_tokens: 0 is *not*
    # actually reasoning — don't advertise the metric. Only mark reasoning
    # available once the model has actually accrued reasoning tokens.
    provided = {"input", "cached", "output"} & set(cum_event)
    if cum_event.get("reasoning", 0) > 0:
        provided.add("reasoning")

    # Persist the new cumulative — absent fields retain their prior last
    # value so a later event that re-includes them still diffs correctly.
    merged = dict(prev)
    merged.update(cum_event)
    file_state["cum"] = merged

    _add_delta(
        state,
        ts,
        "codex",
        file_state.get("model"),
        delta,
        provided=provided,
        index=index,
    )


# --------------------------------------------------------------------- claude


def _claude_apply(state: dict, file_state: dict, row: dict, index: dict) -> None:
    if row.get("type") != "assistant":
        return
    message = row.get("message") or {}
    if not isinstance(message, dict):
        return
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return
    msg_id = message.get("id")
    if isinstance(msg_id, str):
        seen = state.setdefault("_seen_msg_ids_set", set())
        if msg_id in seen:
            return  # global dedupe: resume/fork replays copy the same id
        seen.add(msg_id)

    model = message.get("model") if isinstance(message.get("model"), str) else None
    delta = {
        "input": int(usage.get("input_tokens") or 0),
        "cached": int(usage.get("cache_read_input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
        "reasoning": 0,
    }
    # Anthropic's usage payload never carries a reasoning-token field, so
    # the metric is genuinely unavailable for claude sessions.
    _add_delta(
        state,
        row.get("timestamp"),
        "claude",
        model,
        delta,
        provided={"input", "cached", "output"},
        index=index,
    )


# --------------------------------------------------------------------- scan


def _scan_file(state: dict, path: Path, cli: str, index: dict) -> None:
    try:
        stat = path.stat()
    except OSError:
        return
    key = str(path)
    file_state = state["files"].get(key)
    reset = False
    if file_state is None:
        reset = True
    else:
        # File shrank (rotated/truncated) — reparse from scratch, remove the
        # buckets we contributed. We can't perfectly reverse the effect, so
        # for now we simply drop the file's cache entry and rebuild forward;
        # the old buckets stay (a small overcount worst-case for a rotated
        # session, which is uncommon in practice — CLI rollout files are
        # append-only unless the user manually rewrites them).
        if stat.st_size < file_state.get("offset", 0):
            reset = True
    if reset:
        file_state = {
            "offset": 0,
            "cli": cli,
            "model": None,
            "cum": None,
            "sess_id": None,
            "mtime": stat.st_mtime,
            "size": stat.st_size,
        }
        state["files"][key] = file_state
    if stat.st_size == file_state.get("offset", 0):
        file_state["mtime"] = stat.st_mtime
        file_state["size"] = stat.st_size
        return
    apply = _codex_apply if cli == "codex" else _claude_apply
    try:
        with path.open("rb") as f:
            f.seek(file_state["offset"])
            chunk = f.read()
    except OSError:
        return
    # Trailing partial line stays in the file until it's fully written; back
    # the offset off so we re-read it next scan. Do the newline split on the
    # RAW BYTES — decoded-character indices don't map to file byte offsets
    # once multi-byte UTF-8 shows up (a mismatch here silently desyncs the
    # per-file offset and re-parses complete token_count rows on every scan).
    if not chunk.endswith(b"\n"):
        last_nl = chunk.rfind(b"\n")
        if last_nl == -1:
            return  # No newline at all — nothing complete to parse this pass.
        consumed = last_nl + 1
        chunk = chunk[:consumed]
    else:
        consumed = len(chunk)
    text = chunk.decode("utf-8", errors="replace")
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        try:
            apply(state, file_state, row, index)
        except (KeyError, TypeError, AttributeError, ValueError):
            continue
    file_state["offset"] += consumed
    file_state["mtime"] = stat.st_mtime
    file_state["size"] = stat.st_size


def _iter_codex_files() -> Iterable[Path]:
    root = codex_sessions_dir()
    if not root.is_dir():
        return
    # Glob is cheap enough; there are typically hundreds of files, not millions.
    for path in root.rglob("rollout-*.jsonl"):
        if path.is_file():
            yield path


def _iter_claude_files() -> Iterable[Path]:
    root = claude_projects_dir()
    if not root.is_dir():
        return
    for path in root.rglob("*.jsonl"):
        if path.is_file():
            yield path


def refresh(state: dict | None = None) -> dict:
    """Tail-scan every session file into the persistent aggregate."""
    if state is None:
        state = _load_state()
    index = _bucket_index(state["buckets"])
    for path in _iter_codex_files():
        _scan_file(state, path, "codex", index)
    for path in _iter_claude_files():
        _scan_file(state, path, "claude", index)
    state["updated_at"] = _now_iso()
    _save_state(state)
    return state


def _refresh_in_thread(state: dict | None = None) -> None:
    try:
        refresh(state)
    finally:
        _REFRESH_LOCK.release()


def try_start_refresh(state: dict | None = None) -> bool:
    if not _REFRESH_LOCK.acquire(blocking=False):
        return False
    threading.Thread(
        target=_refresh_in_thread,
        args=(state,),
        name="wiki-token-refresh",
        daemon=True,
    ).start()
    return True


async def refresh_in_background() -> bool:
    if not _REFRESH_LOCK.acquire(blocking=False):
        return False
    try:
        await asyncio.to_thread(refresh)
    finally:
        _REFRESH_LOCK.release()
    return True


# --------------------------------------------------------------------- query


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc)


def _bucket_dt(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _floor_day(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _state_has_snapshot(state: dict) -> bool:
    return bool(state.get("updated_at"))


def _state_is_stale(state: dict) -> bool:
    if not _state_has_snapshot(state):
        return True
    updated = _parse_iso(state.get("updated_at"))
    if updated is None:
        return True
    age = (datetime.now(tz=timezone.utc) - updated).total_seconds()
    return age > SYNC_REFRESH_MAX_AGE_SECONDS


def _query_from_state(
    state: dict,
    from_ts: str | None = None,
    to_ts: str | None = None,
    bucket: str = BUCKET_HOUR,
    cli: str | None = None,
    model: str | None = None,
) -> dict:
    """Filter + roll up the persistent buckets. Buckets in the response are
    KEYED by ts and carry a per-series {"<cli>/<model>": {...}} map so a
    single response can drive stacked charts without a second request."""
    frm = _parse_iso(from_ts)
    to = _parse_iso(to_ts)
    if bucket not in (BUCKET_HOUR, BUCKET_DAY):
        bucket = BUCKET_HOUR

    cli_filter = cli if cli in ("codex", "claude") else None
    model_filter = model or None

    kept: list[dict] = []
    models_seen: set[str] = set()
    cli_seen: set[str] = set()
    totals_sum = {k: 0 for k in METRIC_KEYS}
    totals_provided: set[str] = set()
    grouped: dict[str, dict[str, dict[str, int]]] = {}
    provided_per_series: dict[tuple[str, str], set[str]] = {}

    for b in state["buckets"]:
        dt = _bucket_dt(b["ts"])
        if dt is None:
            continue
        if frm and dt < frm:
            continue
        if to and dt >= to:
            continue
        cli_seen.add(b["cli"])
        models_seen.add(b["model"])
        if cli_filter and b["cli"] != cli_filter:
            continue
        if model_filter and b["model"] != model_filter:
            continue
        kept.append(b)

    for b in kept:
        dt = _bucket_dt(b["ts"])
        assert dt is not None
        if bucket == BUCKET_DAY:
            dt = _floor_day(dt)
        ts_key = dt.isoformat().replace("+00:00", "Z")
        series_key = f"{b['cli']}/{b['model']}"
        series = grouped.setdefault(ts_key, {}).setdefault(
            series_key, {k: 0 for k in METRIC_KEYS}
        )
        # Legacy (pre-v3) buckets without a `provided` list are assumed to
        # cover the full metric set — preserves old-shape output on reads
        # that pre-date the schema bump.
        raw_provided = b.get("provided")
        bucket_provided = (
            set(raw_provided) if isinstance(raw_provided, list) else set(METRIC_KEYS)
        )
        provided_per_series.setdefault((ts_key, series_key), set()).update(bucket_provided)
        totals_provided.update(bucket_provided)
        for k in METRIC_KEYS:
            series[k] += int(b.get(k, 0))
            totals_sum[k] += int(b.get(k, 0))

    buckets_out = []
    for ts, series_map in sorted(grouped.items()):
        series_out = {}
        for series_key, sums in series_map.items():
            provided = provided_per_series.get((ts, series_key), set(METRIC_KEYS))
            # Only emit metrics some source actually reported for this series
            # — the frontend's "unavailable" convention relies on the key
            # being absent (not present-with-zero).
            series_out[series_key] = {k: sums[k] for k in METRIC_KEYS if k in provided}
        buckets_out.append({"ts": ts, "series": series_out})
    totals_out = {k: totals_sum[k] for k in METRIC_KEYS if k in totals_provided}
    return {
        "buckets": buckets_out,
        "totals": totals_out,
        "models": sorted(models_seen),
        "clis": sorted(cli_seen),
        "sessions_scanned": len(state["files"]),
        "bucket": bucket,
    }


def query(
    from_ts: str | None = None,
    to_ts: str | None = None,
    bucket: str = BUCKET_HOUR,
    cli: str | None = None,
    model: str | None = None,
    state: dict | None = None,
) -> dict:
    if state is None:
        if _REFRESH_LOCK.acquire(blocking=False):
            try:
                state = refresh()
            finally:
                _REFRESH_LOCK.release()
        else:
            state = _load_state()
    return _query_from_state(
        state,
        from_ts=from_ts,
        to_ts=to_ts,
        bucket=bucket,
        cli=cli,
        model=model,
    )


def query_nonblocking(
    from_ts: str | None = None,
    to_ts: str | None = None,
    bucket: str = BUCKET_HOUR,
    cli: str | None = None,
    model: str | None = None,
) -> dict:
    state = _load_state()

    if _REFRESH_LOCK.locked():
        response = _query_from_state(
            state,
            from_ts=from_ts,
            to_ts=to_ts,
            bucket=bucket,
            cli=cli,
            model=model,
        )
        response["refreshing"] = True
        return response

    if _state_is_stale(state):
        try_start_refresh(state)
        response = _query_from_state(
            state,
            from_ts=from_ts,
            to_ts=to_ts,
            bucket=bucket,
            cli=cli,
            model=model,
        )
        response["refreshing"] = True
        return response

    if _REFRESH_LOCK.acquire(blocking=False):
        try:
            state = refresh(state)
        finally:
            _REFRESH_LOCK.release()
        response = _query_from_state(
            state,
            from_ts=from_ts,
            to_ts=to_ts,
            bucket=bucket,
            cli=cli,
            model=model,
        )
        response["refreshing"] = False
        return response

    response = _query_from_state(
        _load_state(),
        from_ts=from_ts,
        to_ts=to_ts,
        bucket=bucket,
        cli=cli,
        model=model,
    )
    response["refreshing"] = True
    return response
