"""Unified command-palette search.

The palette walks four kinds of records and returns a single ranked list.
It reuses upstream stores (never rebuilds them):

- sessions:  live agent registry + status files + archive index
- tickets:   vault/todo.md + vault/log/done.md ticket ID extraction
- artifacts: runtime runs/*/artifacts + archive */*/artifacts image files
- notes:     vault/**/*.md titles and first paragraph

Ranking = fuzzy subsequence score with boundary bonus, blended with a
recency boost that decays over ~30 days.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

TICKET_ID_PATTERN = re.compile(r"\b([A-Z]{2,10})-(\d{1,6})\b")
LINEAR_TICKET_PREFIX = "https://linear.app/phoebework/issue/"
ARTIFACT_MEDIA_EXTS = (".png", ".jpg", ".jpeg", ".webp")

DEFAULT_LIMIT = 30
MAX_LIMIT = 100
DEFAULT_QUERY_MAX = 200

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

    def to_payload(self, score: float) -> dict[str, Any]:
        return {
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
    if prefix in {"PHO"}:
        return f"{LINEAR_TICKET_PREFIX}{prefix}-{number}"
    return None


def _is_boundary(character: str) -> bool:
    return character in {"/", "\\", ".", "_", "-", " ", "\t"}


def _subsequence_score(haystack: str, needle: str) -> float | None:
    if not needle:
        return 0.0
    text = haystack.lower()
    query = needle.lower()
    n = len(query)
    if n == 0:
        return 0.0

    cursor = 0
    first = -1
    last = -1
    boundary_hits = 0
    contiguous_runs = 0
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
            if found == last + 1:
                contiguous_runs += 1
            else:
                gaps += found - last - 1
        last = found
        cursor = found + 1

    missing_boundary = n - boundary_hits
    return (
        40.0
        + missing_boundary * 12.0
        + gaps * 3.0
        - contiguous_runs * 2.0
        + first / max(len(text), 1)
    )


def _basic_score(text: str, query: str) -> float | None:
    """Prefix/word-boundary/substring beats subsequence."""
    if not query:
        return 0.0
    if text == query:
        return 0.0
    if text.startswith(query):
        return 4.0
    for index in range(1, len(text)):
        if _is_boundary(text[index - 1]) and text.startswith(query, index):
            return 8.0
    if query in text:
        return 12.0 + text.index(query) / max(len(text), 1)
    return _subsequence_score(text, query)


def _match_score(haystack: str, query: str) -> float | None:
    text = haystack.lower()
    q = query.lower()
    direct = _basic_score(text, q)
    # Per-segment scores hunt for the query landing inside a single token
    # (e.g. "vault/tools/x.md" — segments "vault", "tools", "x.md").
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


def _combined_score(match: float, boost: float, kind_bias: float) -> float:
    return match - boost * 8.0 + kind_bias


def score_item(item: PaletteItem, query: str, now: datetime) -> float | None:
    match = _match_score(item.haystack, query)
    if match is None:
        return None
    return _combined_score(
        match,
        _recency_boost(item.updated_at, now),
        _KIND_BIAS.get(item.kind, 0.0),
    )


def _sort_key(item_score: tuple[PaletteItem, float]) -> tuple[float, float]:
    item, score = item_score
    updated = item.updated_at or datetime.fromtimestamp(0, tz=timezone.utc)
    return (score, -updated.timestamp())


def _empty_ranking(items: Iterable[PaletteItem], limit: int) -> list[tuple[PaletteItem, float]]:
    ranked: list[tuple[PaletteItem, float]] = []
    for item in items:
        updated = item.updated_at or datetime.fromtimestamp(0, tz=timezone.utc)
        # Score = -updated timestamp so newer wins in ascending sort.
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

    for ticket, (context, section) in todo_entries.items():
        if ticket in seen:
            continue
        seen.add(ticket)
        prefix, number = ticket.split("-", 1)
        has_session = ticket in session_ticket_ids
        if has_session:
            url = f"#/agent/{ticket}"
            subtitle = "todo · session live"
        else:
            linear = _linear_url_for(prefix, number)
            url = linear if linear else f"#/agent/{ticket}"
            subtitle = f"todo · {section.lower()}"
        items.append(
            PaletteItem(
                kind="ticket",
                id=ticket,
                title=ticket,
                subtitle=subtitle,
                url=url,
                updated_at=todo_updated,
                haystack=f"{ticket} {context}",
            )
        )

    for ticket, (context, section) in done_entries.items():
        if ticket in seen:
            continue
        seen.add(ticket)
        prefix, number = ticket.split("-", 1)
        linear = _linear_url_for(prefix, number)
        url = linear if linear else f"#/agent/{ticket}"
        items.append(
            PaletteItem(
                kind="ticket",
                id=ticket,
                title=ticket,
                subtitle=f"done · {section.lower()}",
                url=url,
                updated_at=done_updated,
                haystack=f"{ticket} {context}",
            )
        )

    return items


def _iter_artifact_files(directory: Path, limit_per_dir: int = 20) -> list[Path]:
    if not directory.is_dir() or directory.is_symlink():
        return []
    candidates: list[Path] = []
    try:
        for entry in os.scandir(directory):
            name = entry.name.lower()
            if not name.endswith(ARTIFACT_MEDIA_EXTS):
                continue
            if entry.is_symlink() or not entry.is_file():
                continue
            candidates.append(Path(entry.path))
    except OSError:
        return []
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[:limit_per_dir]


def collect_artifact_items(
    runs_dir: Path,
    archive_dir: Path,
    *,
    ticket_by_run: dict[str, str] | None = None,
    max_dirs: int = 40,
) -> list[PaletteItem]:
    items: list[PaletteItem] = []
    dirs_seen = 0

    if runs_dir.is_dir():
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
            artifact_dir = Path(entry.path) / "artifacts"
            files = _iter_artifact_files(artifact_dir)
            if not files:
                continue
            dirs_seen += 1
            ticket = (ticket_by_run or {}).get(entry.name)
            for path in files:
                artifact_id = path.stem
                updated = _stat_mtime(path)
                title = f"artifact {artifact_id[:8]}"
                subtitle_parts = [path.suffix.lstrip("."), ticket or "run"]
                subtitle = " · ".join(part for part in subtitle_parts if part)
                url = (
                    f"#/agent/{ticket}"
                    if ticket
                    else "#/agents"
                )
                items.append(
                    PaletteItem(
                        kind="artifact",
                        id=artifact_id,
                        title=title,
                        subtitle=subtitle,
                        url=url,
                        updated_at=updated,
                        haystack=" ".join(
                            filter(
                                None,
                                [artifact_id, ticket or "", path.suffix.lstrip(".")],
                            )
                        ),
                    )
                )

    if archive_dir.is_dir():
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
            for session_entry in session_dirs[:3]:
                if not session_entry.is_dir() or session_entry.is_symlink():
                    continue
                artifact_dir = Path(session_entry.path) / "artifacts"
                files = _iter_artifact_files(artifact_dir)
                if not files:
                    continue
                dirs_seen += 1
                for path in files:
                    artifact_id = path.stem
                    updated = _stat_mtime(path)
                    title = f"artifact {artifact_id[:8]}"
                    subtitle = " · ".join(
                        part for part in [path.suffix.lstrip("."), ticket_entry.name] if part
                    )
                    items.append(
                        PaletteItem(
                            kind="artifact",
                            id=artifact_id,
                            title=title,
                            subtitle=subtitle,
                            url=f"#/agent/{ticket_entry.name}",
                            updated_at=updated,
                            haystack=" ".join(
                                [artifact_id, ticket_entry.name, path.suffix.lstrip(".")]
                            ),
                        )
                    )
                break  # one session dir per ticket keeps the list focused
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


def collect_note_items(vault_dir: Path) -> list[PaletteItem]:
    if not vault_dir.is_dir():
        return []
    items: list[PaletteItem] = []
    for path in vault_dir.rglob("*.md"):
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        relative = path.relative_to(vault_dir).as_posix()
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


def collect_all_items(
    *,
    agents_payload: dict[str, Any],
    vault_dir: Path,
    runs_dir: Path,
    archive_dir: Path,
) -> list[PaletteItem]:
    session_items = collect_session_items(agents_payload)
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
    return (
        session_items
        + collect_ticket_items(vault_dir, session_ids)
        + collect_artifact_items(runs_dir, archive_dir, ticket_by_run=ticket_by_run)
        + collect_note_items(vault_dir)
    )


def search(
    query: str,
    limit: int,
    *,
    agents_payload: dict[str, Any],
    vault_dir: Path,
    runs_dir: Path,
    archive_dir: Path,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, MAX_LIMIT))
    if len(query) > DEFAULT_QUERY_MAX:
        query = query[:DEFAULT_QUERY_MAX]
    items = collect_all_items(
        agents_payload=agents_payload,
        vault_dir=vault_dir,
        runs_dir=runs_dir,
        archive_dir=archive_dir,
    )
    return rank(items, query, limit)
