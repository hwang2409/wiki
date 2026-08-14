"""Unified command-palette search.

The palette walks four kinds of records and returns a single ranked list.
It reuses upstream stores (never rebuilds them):

- sessions:  live agent registry + status files + archive index
- tickets:   vault/todo.md + vault/log/done.md ticket ID extraction
- artifacts: normalized events.jsonl entries with `kind: "artifact"`
- notes:     vault/**/*.md titles and first paragraph

Ranking is class-based (exact / prefix / word-boundary / substring /
subsequence). Recency and kind bias never cross a class; recency is a
bounded tie-breaker inside a class.

Vault walk enforces containment (rejects symlinks and paths that resolve
outside the vault) and bounds file count + per-file bytes. Results are
cached against the vault mtime signature so back-to-back searches don't
re-walk.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
from urllib.parse import quote
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .agent_runtime.archive_protocol import archive_is_committed

class PaletteCancelled(Exception):
    """Raised when a caller signalled cancellation mid-walk."""

TICKET_ID_PATTERN = re.compile(r"\b([A-Z]{2,10})-(\d{1,6})\b")

LINEAR_PROJECT_URLS: dict[str, str] = {
    "PHO": "https://linear.app/phoebework/issue/",
    "WIKI": "https://linear.app/phoebework/issue/",
    "MITMWEB": "https://linear.app/phoebework/issue/",
    "TIX": "https://linear.app/phoebework/issue/",
    "GAU": "https://linear.app/phoebework/issue/",
    "PUF": "https://linear.app/phoebework/issue/",
}

ARTIFACT_KINDS: tuple[str, ...] = (
    "code",
    "diff",
    "file-list",
    "image",
    "json",
    "mermaid",
    "plot",
    "svg",
    "table",
    "video",
    "audio",
    "visual-diff",
)

DEFAULT_LIMIT = 30
MAX_LIMIT = 100
DEFAULT_QUERY_MAX = 200

VAULT_MAX_FILES = 5000
VAULT_MAX_FILE_BYTES = 100_000

ARTIFACT_MAX_RUN_DIRS = 40
ARTIFACT_MAX_PER_DIR = 20

RECENCY_HALF_LIFE_DAYS = 30.0


@dataclass(frozen=True)
class PaletteItem:
    kind: str
    id: str
    title: str
    subtitle: str
    url: str
    updated_at: datetime | None
    haystack: str
    artifact_id: str | None = None
    ticket: str | None = None

    def to_payload(self, score: float) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "id": self.id,
            "title": self.title,
            "subtitle": self.subtitle,
            "url": self.url,
            "updated_at": self.updated_at.astimezone(timezone.utc).isoformat()
            if self.updated_at
            else None,
            "score": round(score, 4),
        }
        if self.artifact_id:
            payload["artifact_id"] = self.artifact_id
        if self.ticket:
            payload["ticket"] = self.ticket
        return payload


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _stat_mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _linear_url_for(prefix: str, number: str) -> str | None:
    base = LINEAR_PROJECT_URLS.get(prefix)
    if not base:
        return None
    return f"{base}{prefix}-{number}"


def _is_boundary(character: str) -> bool:
    return character in {"/", "\\", ".", "_", "-", " ", "\t"}


# Match classes — smaller is better. Recency and kind bias cannot bridge
# these gaps (each class is >= 4 units wide, adjustments stay < 2).
CLASS_EXACT = 0.0
CLASS_PREFIX = 8.0
CLASS_WORD_BOUNDARY = 16.0
CLASS_SUBSTRING = 24.0
CLASS_SUBSEQUENCE = 40.0


def _subsequence_score(haystack: str, needle: str) -> float | None:
    if not needle:
        return CLASS_EXACT
    text = haystack.lower()
    query = needle.lower()
    n = len(query)
    if n == 0:
        return CLASS_EXACT

    cursor = 0
    first = -1
    last = -1
    boundary_hits = 0
    gaps = 0

    for character in query:
        found = -1
        index = cursor
        while index < len(text):
            if text[index] == character:
                found = index
                if index == 0 or _is_boundary(text[index - 1]):
                    boundary_hits += 1
                    break
            index += 1
        if found < 0:
            return None
        if first < 0:
            first = found
        if last >= 0:
            gaps += found - last - 1
        last = found
        cursor = found + 1

    missing_boundary = n - boundary_hits
    # Stay strictly within the subsequence class band. Cap the penalty so
    # every subsequence match ranks below every substring match, and use
    # the sub-unit remainder as a within-class ordering hint.
    penalty = min(3.5, missing_boundary * 0.4 + gaps * 0.05)
    tail = first / max(len(text), 1) * 0.4
    return CLASS_SUBSEQUENCE + penalty + tail


def _basic_score(text: str, query: str) -> float | None:
    if not query:
        return CLASS_EXACT
    if text == query:
        return CLASS_EXACT
    if text.startswith(query):
        return CLASS_PREFIX + (1.0 - len(query) / max(len(text), 1)) * 3.0
    for index in range(1, len(text)):
        if _is_boundary(text[index - 1]) and text.startswith(query, index):
            return CLASS_WORD_BOUNDARY + index / max(len(text), 1) * 3.0
    if query in text:
        return CLASS_SUBSTRING + text.index(query) / max(len(text), 1) * 3.0
    return _subsequence_score(text, query)


def _match_score(haystack: str, query: str) -> float | None:
    text = haystack.lower()
    q = query.lower()
    direct = _basic_score(text, q)
    segment_best: float | None = None
    segment_start = 0
    for index in range(len(text) + 1):
        if index < len(text) and not _is_boundary(text[index]):
            continue
        if index > segment_start:
            segment_score = _basic_score(text[segment_start:index], q)
            if segment_score is not None and (
                segment_best is None or segment_score < segment_best
            ):
                segment_best = segment_score
        segment_start = index + 1
    candidates = [
        score for score in (direct, segment_best) if score is not None
    ]
    return min(candidates) if candidates else None


def _recency_boost(updated_at: datetime | None, now: datetime) -> float:
    if updated_at is None:
        return 0.0
    age_days = max(0.0, (now - updated_at).total_seconds() / 86400.0)
    return math.exp(-age_days / RECENCY_HALF_LIFE_DAYS)


_KIND_BIAS = {
    "session": -1.5,
    "ticket": -1.0,
    "artifact": -0.5,
    "note": 0.0,
}


def score_item(item: PaletteItem, query: str, now: datetime) -> float | None:
    match = _match_score(item.haystack, query)
    if match is None:
        return None
    return match + _KIND_BIAS.get(item.kind, 0.0)


def _sort_key(item_score: tuple[PaletteItem, float]) -> tuple[float, float]:
    item, score = item_score
    updated = item.updated_at or datetime.fromtimestamp(0, tz=timezone.utc)
    return (score, -updated.timestamp())


def _empty_ranking(items: Iterable[PaletteItem], limit: int) -> list[tuple[PaletteItem, float]]:
    ranked: list[tuple[PaletteItem, float]] = []
    for item in items:
        updated = item.updated_at or datetime.fromtimestamp(0, tz=timezone.utc)
        ranked.append((item, -updated.timestamp()))
    ranked.sort(key=lambda pair: pair[1])
    return ranked[:limit]


def rank(items: Iterable[PaletteItem], query: str, limit: int) -> list[dict[str, Any]]:
    query = (query or "").strip()
    now = datetime.now(tz=timezone.utc)
    if not query:
        top = _empty_ranking(items, limit)
        return [item.to_payload(-score) for item, score in top]

    scored: list[tuple[PaletteItem, float]] = []
    for item in items:
        score = score_item(item, query, now)
        if score is None:
            continue
        scored.append((item, score))
    scored.sort(key=_sort_key)
    return [item.to_payload(score) for item, score in scored[:limit]]


def collect_session_items(agents_payload: dict[str, Any]) -> list[PaletteItem]:
    items: list[PaletteItem] = []
    seen: set[str] = set()

    workers = agents_payload.get("workers")
    if isinstance(workers, list):
        for worker in workers:
            if not isinstance(worker, dict):
                continue
            ticket = worker.get("ticket")
            if not isinstance(ticket, str) or ticket in seen:
                continue
            seen.add(ticket)
            state = worker.get("state") or worker.get("runtime_state") or "unknown"
            role = worker.get("role") or "worker"
            kind = worker.get("kind")
            step = worker.get("step") or worker.get("blocker") or ""
            subtitle_parts = [
                f"{role}",
                str(kind) if kind else "",
                str(state),
            ]
            if step:
                subtitle_parts.append(str(step)[:80])
            subtitle = " · ".join(part for part in subtitle_parts if part)
            updated = _parse_iso(worker.get("spawned_at"))
            haystack = " ".join(
                filter(
                    None,
                    [
                        ticket,
                        str(role or ""),
                        str(kind or ""),
                        str(state or ""),
                        str(worker.get("model") or ""),
                        str(worker.get("orch") or ""),
                        str(step or ""),
                    ],
                )
            )
            items.append(
                PaletteItem(
                    kind="session",
                    id=ticket,
                    title=ticket,
                    subtitle=subtitle,
                    url=f"#/agent/{ticket}",
                    updated_at=updated,
                    haystack=haystack,
                    ticket=ticket,
                )
            )

    orchestrators = agents_payload.get("orchestrators")
    if isinstance(orchestrators, list):
        for orch in orchestrators:
            if not isinstance(orch, dict):
                continue
            orch_id = orch.get("id")
            if not isinstance(orch_id, str) or orch_id in seen:
                continue
            seen.add(orch_id)
            kind = orch.get("kind") or "orch"
            model = orch.get("model") or ""
            cwd = orch.get("cwd") or ""
            cwd_tail = cwd.rsplit("/", 1)[-1] if cwd else ""
            subtitle_parts = ["orchestrator", str(kind), str(model), cwd_tail]
            subtitle = " · ".join(part for part in subtitle_parts if part)
            items.append(
                PaletteItem(
                    kind="session",
                    id=orch_id,
                    title=orch_id,
                    subtitle=subtitle,
                    url=f"#/agent/{orch_id}",
                    updated_at=_parse_iso(orch.get("spawned_at")),
                    haystack=" ".join(
                        filter(
                            None,
                            [orch_id, str(kind), str(model), cwd_tail],
                        )
                    ),
                )
            )

    archived = agents_payload.get("archived")
    if isinstance(archived, list):
        for entry in archived:
            if not isinstance(entry, dict):
                continue
            ticket = entry.get("ticket")
            if not isinstance(ticket, str) or ticket in seen:
                continue
            seen.add(ticket)
            role = entry.get("role") or "archived"
            kind = entry.get("kind") or ""
            outcome = entry.get("outcome") or entry.get("state") or "archived"
            subtitle = " · ".join(
                part
                for part in [
                    "archived",
                    str(role),
                    str(kind) if kind else "",
                    str(outcome),
                ]
                if part
            )
            items.append(
                PaletteItem(
                    kind="session",
                    id=ticket,
                    title=ticket,
                    subtitle=subtitle,
                    url=f"#/agent/{ticket}",
                    updated_at=_parse_iso(entry.get("archived_at")),
                    haystack=" ".join(
                        filter(
                            None,
                            [ticket, str(role), str(kind), str(outcome)],
                        )
                    ),
                    ticket=ticket,
                )
            )
    return items


_TICKET_LINE_STATE = re.compile(r"^\s*-\s*(?:\[[ xX-]\]\s*)?", re.MULTILINE)


def _extract_ticket_lines(source: str) -> dict[str, tuple[str, str]]:
    """Return {ticket: (context_line, state)} for each ticket seen."""
    result: dict[str, tuple[str, str]] = {}
    section = "todo"
    for raw in source.splitlines():
        stripped = raw.strip()
        if stripped.startswith("## "):
            section = stripped[3:].strip()
            continue
        for match in TICKET_ID_PATTERN.finditer(stripped):
            key = f"{match.group(1)}-{match.group(2)}"
            if key in result:
                continue
            result[key] = (stripped[:200], section)
    return result


def collect_ticket_items(
    vault_dir: Path,
    session_ticket_ids: set[str],
) -> list[PaletteItem]:
    items: list[PaletteItem] = []
    seen: set[str] = set()

    todo_path = vault_dir / "todo.md"
    done_path = vault_dir / "log" / "done.md"
    todo_updated = _stat_mtime(todo_path)
    done_updated = _stat_mtime(done_path)

    todo_entries: dict[str, tuple[str, str]] = {}
    done_entries: dict[str, tuple[str, str]] = {}
    try:
        todo_entries = _extract_ticket_lines(todo_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        pass
    try:
        done_entries = _extract_ticket_lines(done_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        pass

    def ticket_url(ticket: str, has_session: bool) -> tuple[str, str]:
        prefix, number = ticket.split("-", 1)
        if has_session:
            return f"#/agent/{ticket}", "session live"
        linear = _linear_url_for(prefix, number)
        if linear:
            return linear, "linear"
        return f"#/agent/{ticket}", "no session"

    for ticket, (context, section) in todo_entries.items():
        if ticket in seen:
            continue
        seen.add(ticket)
        has_session = ticket in session_ticket_ids
        url, route = ticket_url(ticket, has_session)
        items.append(
            PaletteItem(
                kind="ticket",
                id=ticket,
                title=ticket,
                subtitle=f"todo · {section.lower()} · {route}",
                url=url,
                updated_at=todo_updated,
                haystack=f"{ticket} {context}",
                ticket=ticket,
            )
        )

    for ticket, (context, section) in done_entries.items():
        if ticket in seen:
            continue
        seen.add(ticket)
        has_session = ticket in session_ticket_ids
        url, route = ticket_url(ticket, has_session)
        items.append(
            PaletteItem(
                kind="ticket",
                id=ticket,
                title=ticket,
                subtitle=f"done · {section.lower()} · {route}",
                url=url,
                updated_at=done_updated,
                haystack=f"{ticket} {context}",
                ticket=ticket,
            )
        )

    return items


def _iter_events_jsonl(events_path: Path, max_bytes: int = 5_000_000) -> Iterable[dict[str, Any]]:
    try:
        with events_path.open("r", encoding="utf-8") as handle:
            budget = max_bytes
            for line in handle:
                budget -= len(line)
                if budget < 0:
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    yield event
    except OSError:
        return


def _artifact_payload_from_event(event: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Return (artifact_id, artifact_dict) when this event describes an artifact."""
    if event.get("kind") == "artifact":
        payload = event.get("payload")
        if isinstance(payload, dict) and payload.get("kind") == "artifact":
            artifact = payload.get("artifact")
            artifact_id = payload.get("id") or event.get("artifact_id")
            if isinstance(artifact, dict) and isinstance(artifact_id, str):
                return artifact_id, {**payload, "artifact": artifact}
        payload = event
        artifact = payload.get("artifact")
        artifact_id = payload.get("id")
        if isinstance(artifact, dict) and isinstance(artifact_id, str):
            return artifact_id, payload
    return None


def _make_artifact_item(
    artifact_id: str,
    payload: dict[str, Any],
    ticket: str | None,
    updated: datetime | None,
) -> PaletteItem | None:
    artifact = payload.get("artifact")
    if not isinstance(artifact, dict):
        return None
    kind = str(artifact.get("kind") or "").strip()
    if kind not in ARTIFACT_KINDS:
        return None
    title = str(payload.get("title") or artifact.get("filename") or "").strip()
    if not title:
        title = f"artifact {artifact_id[:8]}"
    caption = str(payload.get("caption") or "").strip()
    subtitle_parts = [kind]
    if ticket:
        subtitle_parts.append(ticket)
    if caption:
        subtitle_parts.append(caption[:60])
    subtitle = " · ".join(part for part in subtitle_parts if part)
    if ticket:
        # Durable deep-link: agent-session-surface reads panel state
        # from `panel`/`artifact`/`tab`/`focus` search params on mount +
        # popstate. Emitting them here means a refresh (or paste of this
        # URL into a new tab) reopens the same artifact tab focused.
        encoded_ticket = quote(ticket, safe="")
        encoded_artifact = quote(artifact_id, safe="")
        params = (
            f"panel={encoded_ticket}"
            f"&artifact={encoded_artifact}"
            f"&tab={encoded_artifact}"
            f"&focus={encoded_artifact}"
        )
        url = f"?{params}#/agent/{encoded_ticket}"
    else:
        url = "#/agents"
    haystack = " ".join(
        filter(
            None,
            [
                artifact_id,
                title,
                kind,
                ticket or "",
                caption,
                str(artifact.get("filename") or ""),
                str(artifact.get("language") or ""),
            ],
        )
    )
    return PaletteItem(
        kind="artifact",
        id=artifact_id,
        title=title,
        subtitle=subtitle,
        url=url,
        updated_at=updated,
        haystack=haystack,
        artifact_id=artifact_id,
        ticket=ticket,
    )


def _artifact_ts(payload: dict[str, Any], fallback: datetime | None) -> datetime | None:
    ts = _parse_iso(payload.get("ts")) or _parse_iso(payload.get("normalized_at"))
    return ts or fallback


@dataclass(frozen=True)
class _ArtifactDirCacheEntry:
    mtime_ns: int
    items: tuple[PaletteItem, ...]


# events.jsonl parsing dominates palette latency (multi-MB JSON per dir);
# keyed on the file's mtime so unchanged dirs never re-parse.
_artifact_dir_cache: dict[str, _ArtifactDirCacheEntry] = {}
_artifact_dir_cache_lock = threading.Lock()


def _collect_from_run_dir(
    run_dir: Path,
    ticket: str | None,
) -> list[PaletteItem]:
    events_path = run_dir / "events.jsonl"
    if not events_path.is_file() or events_path.is_symlink():
        return []
    try:
        mtime_ns = int(events_path.stat().st_mtime_ns)
    except OSError:
        return []
    cache_key = f"{events_path}|{ticket or ''}"
    with _artifact_dir_cache_lock:
        cached = _artifact_dir_cache.get(cache_key)
        if cached is not None and cached.mtime_ns == mtime_ns:
            return list(cached.items)
    dir_mtime = _stat_mtime(events_path)
    items: list[PaletteItem] = []
    seen: set[str] = set()
    for event in _iter_events_jsonl(events_path):
        extracted = _artifact_payload_from_event(event)
        if not extracted:
            continue
        artifact_id, payload = extracted
        if artifact_id in seen:
            continue
        seen.add(artifact_id)
        item = _make_artifact_item(
            artifact_id,
            payload,
            ticket,
            _artifact_ts(payload, dir_mtime),
        )
        if item is not None:
            items.append(item)
        if len(items) >= ARTIFACT_MAX_PER_DIR:
            break
    with _artifact_dir_cache_lock:
        _artifact_dir_cache[cache_key] = _ArtifactDirCacheEntry(
            mtime_ns=mtime_ns, items=tuple(items)
        )
    return items


def collect_artifact_items(
    runs_dir: Path,
    archive_dir: Path,
    *,
    ticket_by_run: dict[str, str] | None = None,
    max_dirs: int = ARTIFACT_MAX_RUN_DIRS,
) -> list[PaletteItem]:
    items: list[PaletteItem] = []
    dirs_seen = 0

    if runs_dir.is_dir() and not runs_dir.is_symlink():
        try:
            run_entries = list(os.scandir(runs_dir))
        except OSError:
            run_entries = []
        run_entries.sort(key=lambda entry: entry.stat().st_mtime, reverse=True)
        for entry in run_entries:
            if dirs_seen >= max_dirs:
                break
            if not entry.is_dir() or entry.is_symlink():
                continue
            run_dir = Path(entry.path)
            ticket = (ticket_by_run or {}).get(entry.name)
            found = _collect_from_run_dir(run_dir, ticket)
            if found:
                dirs_seen += 1
                items.extend(found)

    if archive_dir.is_dir() and not archive_dir.is_symlink():
        try:
            ticket_dirs = list(os.scandir(archive_dir))
        except OSError:
            ticket_dirs = []
        ticket_dirs.sort(key=lambda entry: entry.stat().st_mtime, reverse=True)
        for ticket_entry in ticket_dirs:
            if dirs_seen >= max_dirs:
                break
            if not ticket_entry.is_dir() or ticket_entry.is_symlink():
                continue
            if ticket_entry.name.startswith("_"):
                continue
            try:
                session_dirs = list(os.scandir(ticket_entry.path))
            except OSError:
                continue
            session_dirs.sort(key=lambda entry: entry.name, reverse=True)
            committed_session_dirs = [
                entry
                for entry in session_dirs
                if (
                    entry.is_dir()
                    and not entry.is_symlink()
                    and archive_is_committed(Path(entry.path))
                )
            ]
            for session_entry in committed_session_dirs[:3]:
                session_dir = Path(session_entry.path)
                found = _collect_from_run_dir(session_dir, ticket_entry.name)
                if found:
                    dirs_seen += 1
                    items.extend(found)
                    break

    return items


def collect_artifact_items_from_index(
    event_store: Any,
    *,
    ticket_by_run: dict[str, str] | None = None,
) -> list[PaletteItem] | None:
    """Build palette artifacts from SQLite without walking event JSONL files.

    ``None`` means the index is not available yet.  Callers then retain the
    legacy scan for installations that have not materialized any runs.
    """

    try:
        indexed_events = event_store.read_artifact_events()
    except Exception:
        return None
    items: list[PaletteItem] = []
    seen: set[str] = set()
    for run_id, event in indexed_events:
        extracted = _artifact_payload_from_event(event)
        if extracted is None:
            continue
        artifact_id, payload = extracted
        if artifact_id in seen:
            continue
        seen.add(artifact_id)
        item = _make_artifact_item(
            artifact_id,
            payload,
            (ticket_by_run or {}).get(run_id),
            _artifact_ts(payload, None),
        )
        if item is not None:
            items.append(item)
    return items


def _title_from_note(note_path: Path, content: str) -> tuple[str, str]:
    """Return (title, first_paragraph)."""
    title = ""
    lines = content.splitlines()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            break
    if not title:
        title = note_path.stem.replace("-", " ").replace("_", " ").strip() or "Untitled"

    first_para: list[str] = []
    in_frontmatter = False
    seen_content = False
    for line in lines:
        stripped = line.strip()
        if stripped == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter:
            continue
        if not stripped:
            if first_para:
                break
            continue
        if stripped.startswith("#"):
            if seen_content:
                break
            continue
        seen_content = True
        first_para.append(stripped)
    first = " ".join(first_para)
    if len(first) > 160:
        first = first[:157].rstrip() + "..."
    return title, first


@dataclass(frozen=True)
class _NoteCacheEntry:
    # Per-file signature (relative posix path -> st_mtime_ns). Editing a note
    # updates only the file's mtime, not the vault directory's mtime, so a
    # directory-level signature would stay stuck. Cheap-statting every .md on
    # each request costs O(files) which is dwarfed by anything else the walk
    # does.
    signature: frozenset[tuple[str, int]]
    items: tuple[PaletteItem, ...] = ()


@dataclass
class _NoteCache:
    entries: dict[str, _NoteCacheEntry] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


_note_cache = _NoteCache()


def _check_cancelled(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel is not None and should_cancel():
        raise PaletteCancelled()


def _iter_vault_md(
    vault_dir: Path,
    should_cancel: Callable[[], bool] | None = None,
) -> Iterable[tuple[Path, str]]:
    """Yield (path, relative_posix) for every safe .md under the vault.

    Rejects symlinks and paths that resolve outside the vault, and caps total
    files at VAULT_MAX_FILES.
    """
    resolved_vault = vault_dir.resolve(strict=False)
    try:
        candidates = vault_dir.rglob("*.md")
    except OSError:
        return
    count = 0
    for path in candidates:
        if count >= VAULT_MAX_FILES:
            return
        if count % 200 == 0:
            _check_cancelled(should_cancel)
        try:
            if path.is_symlink() or not path.is_file():
                continue
        except OSError:
            continue
        try:
            resolved = path.resolve(strict=False)
            resolved.relative_to(resolved_vault)
        except (OSError, ValueError):
            continue
        try:
            relative = path.relative_to(vault_dir).as_posix()
        except ValueError:
            continue
        yield path, relative
        count += 1


def _vault_signature(
    vault_dir: Path,
    should_cancel: Callable[[], bool] | None = None,
) -> frozenset[tuple[str, int]]:
    entries: list[tuple[str, int]] = []
    for path, relative in _iter_vault_md(vault_dir, should_cancel):
        try:
            mtime_ns = int(path.stat().st_mtime_ns)
        except OSError:
            continue
        entries.append((relative, mtime_ns))
    return frozenset(entries)


def _walk_vault(
    vault_dir: Path,
    should_cancel: Callable[[], bool] | None = None,
) -> list[PaletteItem]:
    items: list[PaletteItem] = []
    for path, relative in _iter_vault_md(vault_dir, should_cancel):
        _check_cancelled(should_cancel)
        try:
            with path.open("rb") as handle:
                raw = handle.read(VAULT_MAX_FILE_BYTES)
        except OSError:
            continue
        try:
            content = raw.decode("utf-8", errors="ignore")
        except UnicodeDecodeError:
            continue
        title, first_para = _title_from_note(path, content)
        subtitle = first_para or relative
        updated = _stat_mtime(path)
        items.append(
            PaletteItem(
                kind="note",
                id=relative,
                title=title,
                subtitle=subtitle,
                url=f"#/note/{relative}",
                updated_at=updated,
                haystack=f"{title} {relative} {first_para}",
            )
        )
    return items


def collect_note_items(
    vault_dir: Path,
    should_cancel: Callable[[], bool] | None = None,
) -> list[PaletteItem]:
    if not vault_dir.is_dir() or vault_dir.is_symlink():
        return []
    key = str(vault_dir.resolve(strict=False))
    signature = _vault_signature(vault_dir, should_cancel)
    with _note_cache.lock:
        cached = _note_cache.entries.get(key)
        if cached is not None and cached.signature == signature:
            return list(cached.items)
    # Build fresh outside the lock so concurrent readers can keep hitting the
    # old cache. Publish atomically once complete — never expose a
    # half-populated view.
    fresh_items = _walk_vault(vault_dir, should_cancel)
    fresh_entry = _NoteCacheEntry(signature=signature, items=tuple(fresh_items))
    with _note_cache.lock:
        _note_cache.entries[key] = fresh_entry
    return list(fresh_entry.items)


def collect_all_items(
    *,
    agents_payload: dict[str, Any],
    vault_dir: Path,
    runs_dir: Path,
    archive_dir: Path,
    artifact_items: list[PaletteItem] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list[PaletteItem]:
    session_items = collect_session_items(agents_payload)
    _check_cancelled(should_cancel)
    session_ids = {item.id for item in session_items}
    ticket_by_run: dict[str, str] = {}
    workers = agents_payload.get("workers")
    if isinstance(workers, list):
        for worker in workers:
            if not isinstance(worker, dict):
                continue
            run_id = worker.get("run_id")
            ticket = worker.get("ticket")
            if isinstance(run_id, str) and isinstance(ticket, str):
                ticket_by_run[run_id] = ticket
    ticket_items = collect_ticket_items(vault_dir, session_ids)
    _check_cancelled(should_cancel)
    if artifact_items is None:
        artifact_items = collect_artifact_items(
            runs_dir, archive_dir, ticket_by_run=ticket_by_run
        )
    _check_cancelled(should_cancel)
    note_items = collect_note_items(vault_dir, should_cancel)
    return session_items + ticket_items + artifact_items + note_items


def search(
    query: str,
    limit: int,
    *,
    agents_payload: dict[str, Any],
    vault_dir: Path,
    runs_dir: Path,
    archive_dir: Path,
    artifact_items: list[PaletteItem] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, MAX_LIMIT))
    if len(query) > DEFAULT_QUERY_MAX:
        query = query[:DEFAULT_QUERY_MAX]
    items = collect_all_items(
        agents_payload=agents_payload,
        vault_dir=vault_dir,
        runs_dir=runs_dir,
        archive_dir=archive_dir,
        artifact_items=artifact_items,
        should_cancel=should_cancel,
    )
    _check_cancelled(should_cancel)
    return rank(items, query, limit)
