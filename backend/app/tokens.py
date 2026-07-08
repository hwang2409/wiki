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

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

CACHE_VERSION = 1
MAX_MSG_IDS_PER_FILE = 5000  # streaming dedupe window; assistant rows / session

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
        # path -> {offset, mtime, size, cli, model, cum (dict|None), sess_id, msg_ids (list, LRU)}
        "files": {},
        # list of {ts (iso hour), cli, model, input, cached, output, reasoning}
        "buckets": [],
    }


def _load_state() -> dict:
    try:
        raw = json.loads(token_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_state()
    if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
        return _empty_state()
    raw.setdefault("files", {})
    raw.setdefault("buckets", [])
    return raw


def _save_state(state: dict) -> None:
    try:
        token_cache_path().parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    tmp = token_cache_path().with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(token_cache_path())
    except OSError:
        return


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
    index: dict[tuple[str, str, str], int] | None = None,
) -> None:
    ts_hour = _floor_hour(ts)
    if ts_hour is None:
        return
    model = model or "unknown"
    if not any(delta.get(k) for k in ("input", "cached", "output", "reasoning")):
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
            }
        )
        index[key] = len(state["buckets"]) - 1
    else:
        b = state["buckets"][idx]
        b["input"] += int(delta.get("input", 0))
        b["cached"] += int(delta.get("cached", 0))
        b["output"] += int(delta.get("output", 0))
        b["reasoning"] += int(delta.get("reasoning", 0))


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

    cum = {
        "input": int(total.get("input_tokens") or 0),
        "cached": int(total.get("cached_input_tokens") or 0),
        "output": int(total.get("output_tokens") or 0),
        "reasoning": int(total.get("reasoning_output_tokens") or 0),
    }
    prev = file_state.get("cum")
    if prev is None:
        delta = cum
    else:
        # A resumed session's counters restart from 0. If ANY counter went
        # down, treat as a re-anchor: emit zero delta this event, adopt the
        # new baseline. Individual counters can move independently, so clamp
        # per-field at 0 rather than dropping the whole event.
        drops = any(cum[k] < prev.get(k, 0) for k in cum)
        if drops:
            delta = {k: 0 for k in cum}
        else:
            delta = {k: cum[k] - prev.get(k, 0) for k in cum}
    file_state["cum"] = cum

    _add_delta(
        state,
        ts,
        "codex",
        file_state.get("model"),
        delta,
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
        ids = file_state.setdefault("msg_ids", [])
        if msg_id in ids:
            return
        ids.append(msg_id)
        # LRU cap — drop oldest half when we hit the ceiling.
        if len(ids) > MAX_MSG_IDS_PER_FILE:
            del ids[: MAX_MSG_IDS_PER_FILE // 2]

    model = message.get("model") if isinstance(message.get("model"), str) else None
    delta = {
        "input": int(usage.get("input_tokens") or 0),
        "cached": int(usage.get("cache_read_input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
        "reasoning": 0,
    }
    _add_delta(state, row.get("timestamp"), "claude", model, delta, index=index)


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
            "msg_ids": [],
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
    text = chunk.decode("utf-8", errors="replace")
    # Trailing partial line stays in the file until it's fully written; back
    # the offset off so we re-read it next scan.
    if not text.endswith("\n"):
        last_nl = text.rfind("\n")
        if last_nl == -1:
            # No newline at all — nothing complete to parse this pass.
            return
        consumed = last_nl + 1
        text = text[:consumed]
    else:
        consumed = len(chunk)
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
    _save_state(state)
    return state


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


def query(
    from_ts: str | None = None,
    to_ts: str | None = None,
    bucket: str = BUCKET_HOUR,
    cli: str | None = None,
    model: str | None = None,
    state: dict | None = None,
) -> dict:
    """Filter + roll up the persistent buckets. Buckets in the response are
    KEYED by ts and carry a per-series {"<cli>/<model>": {...}} map so a
    single response can drive stacked charts without a second request."""
    if state is None:
        state = refresh()
    frm = _parse_iso(from_ts)
    to = _parse_iso(to_ts)
    if bucket not in (BUCKET_HOUR, BUCKET_DAY):
        bucket = BUCKET_HOUR

    cli_filter = cli if cli in ("codex", "claude") else None
    model_filter = model or None

    kept: list[dict] = []
    models_seen: set[str] = set()
    cli_seen: set[str] = set()
    totals = {"input": 0, "cached": 0, "output": 0, "reasoning": 0}
    grouped: dict[str, dict[str, dict[str, int]]] = {}

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
            series_key, {"input": 0, "cached": 0, "output": 0, "reasoning": 0}
        )
        for k in ("input", "cached", "output", "reasoning"):
            series[k] += int(b.get(k, 0))
            totals[k] += int(b.get(k, 0))

    buckets_out = [
        {"ts": ts, "series": series}
        for ts, series in sorted(grouped.items())
    ]
    return {
        "buckets": buckets_out,
        "totals": totals,
        "models": sorted(models_seen),
        "clis": sorted(cli_seen),
        "sessions_scanned": len(state["files"]),
        "bucket": bucket,
    }


