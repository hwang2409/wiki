"""Rebuildable SQLite knowledge index for vault notes and Wiki fleet runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping


LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 1
DEFAULT_CHUNK_CHARS = 4_000
MAX_SEARCH_LIMIT = 100
WIKILINK_RE = re.compile(r"\[\[([^\]|\n]+)(?:\|[^\]\n]*)?\]\]")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
FTS_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


class KnowledgeError(RuntimeError):
    """Base error for explicit CLI/MCP knowledge failures."""


class KnowledgeUnavailable(KnowledgeError):
    """The configured SQLite database cannot be opened or queried."""


class KnowledgeQueryError(KnowledgeError):
    """The caller supplied an invalid knowledge query."""


@dataclass(frozen=True)
class NoteChunk:
    heading: str | None
    text: str
    pos: int


@dataclass
class IngestStats:
    notes_scanned: int = 0
    notes_indexed: int = 0
    notes_metadata_updated: int = 0
    notes_deleted: int = 0
    chunks_indexed: int = 0
    links_indexed: int = 0
    runs_indexed: int = 0
    events_indexed: int = 0
    malformed_event_lines: int = 0
    elapsed_seconds: float = 0.0

    def merge(self, other: IngestStats) -> None:
        for field in (
            "notes_scanned",
            "notes_indexed",
            "notes_metadata_updated",
            "notes_deleted",
            "chunks_indexed",
            "links_indexed",
            "runs_indexed",
            "events_indexed",
            "malformed_event_lines",
        ):
            setattr(self, field, getattr(self, field) + getattr(other, field))

    def to_dict(self) -> dict[str, int | float]:
        return asdict(self)


@dataclass(frozen=True)
class KnowledgePaths:
    db_path: Path
    vault_dir: Path
    archive_dir: Path
    runtime_dir: Path

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        runtime_dir: Path | str | None = None,
        archive_dir: Path | str | None = None,
        vault_dir: Path | str | None = None,
    ) -> KnowledgePaths:
        values = os.environ if env is None else env
        home = Path(values.get("HOME") or Path.home()).expanduser()
        runtime = Path(
            runtime_dir
            or values.get("WIKI_AGENT_RUNTIME_DIR")
            or home / ".wiki" / "agent-runtime"
        ).expanduser()
        database = knowledge_db_path(runtime, env=values)
        vault = Path(
            vault_dir
            or values.get("WIKI_VAULT_DIR")
            or Path(__file__).resolve().parents[2] / "vault"
        ).expanduser()
        archive = Path(
            archive_dir
            or values.get("WIKI_AGENT_ARCHIVE_DIR")
            or home / "me" / "fun" / "agent-archive"
        ).expanduser()
        return cls(
            db_path=database.absolute(),
            vault_dir=vault.absolute(),
            archive_dir=archive.absolute(),
            runtime_dir=runtime.absolute(),
        )


def knowledge_db_path(
    runtime_dir: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the DB beside the runtime unless explicitly overridden.

    The production runtime is ``~/.wiki/agent-runtime``, yielding the specified
    ``~/.wiki/knowledge.db``. Tests that already isolate their runtime
    automatically get an isolated sibling database.
    """

    values = os.environ if env is None else env
    configured = values.get("WIKI_KNOWLEDGE_DB_PATH")
    if configured:
        return Path(configured).expanduser().absolute()
    runtime = Path(
        runtime_dir
        or values.get("WIKI_AGENT_RUNTIME_DIR")
        or Path(values.get("HOME") or Path.home()) / ".wiki" / "agent-runtime"
    ).expanduser()
    return (runtime.absolute().parent / "knowledge.db").absolute()


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.absolute())
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def _fence_marker(line: str) -> str | None:
    match = FENCE_RE.match(line)
    return match.group(1) if match else None


def _frontmatter_body(content: str) -> tuple[dict[str, str], str]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, content
    fields: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return fields, "\n".join(lines[index + 1 :])
        if ":" in line and not line.startswith((" ", "\t")):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return {}, content


def _split_plain_text(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        cut = remaining.rfind("\n", 0, max_chars + 1)
        if cut < max_chars // 2:
            cut = remaining.rfind(" ", 0, max_chars + 1)
        if cut < max_chars // 2:
            cut = max_chars
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        pieces.append(remaining)
    return [piece for piece in pieces if piece]


def _section_units(lines: list[str], max_chars: int) -> list[str]:
    """Return paragraph/fence-safe units, splitting only plain oversized text."""

    units: list[tuple[str, bool]] = []
    plain: list[str] = []
    fenced: list[str] = []
    fence: str | None = None

    def flush_plain() -> None:
        if not plain:
            return
        value = "\n".join(plain).strip()
        plain.clear()
        if value:
            units.extend((piece, False) for piece in _split_plain_text(value, max_chars))

    for line in lines:
        marker = _fence_marker(line)
        if fence is not None:
            fenced.append(line)
            if marker and marker[0] == fence[0] and len(marker) >= len(fence):
                units.append(("\n".join(fenced).strip(), True))
                fenced = []
                fence = None
            continue
        if marker:
            flush_plain()
            fence = marker
            fenced = [line]
            continue
        if not line.strip():
            flush_plain()
            continue
        plain.append(line)
    flush_plain()
    if fenced:
        # An unterminated fence is still one indivisible source block.
        units.append(("\n".join(fenced).strip(), True))

    chunks: list[str] = []
    current = ""
    for unit, is_fence in units:
        if not unit:
            continue
        candidate = unit if not current else f"{current}\n\n{unit}"
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = unit
        elif is_fence and len(unit) > max_chars:
            if current:
                chunks.append(current)
            chunks.append(unit)
            current = ""
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def chunk_markdown(
    content: str,
    *,
    max_chars: int = DEFAULT_CHUNK_CHARS,
) -> list[NoteChunk]:
    """Chunk Markdown without crossing headings or splitting fenced blocks."""

    if max_chars < 32:
        raise ValueError("max_chars must be at least 32")
    _, body = _frontmatter_body(content)
    sections: list[tuple[str | None, list[str]]] = []
    heading: str | None = None
    lines: list[str] = []
    fence: str | None = None
    for line in body.splitlines():
        marker = _fence_marker(line)
        if fence is not None:
            lines.append(line)
            if marker and marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if marker:
            fence = marker
            lines.append(line)
            continue
        match = HEADING_RE.match(line)
        if match:
            if lines or heading is not None:
                sections.append((heading, lines))
            heading = match.group(2).strip()
            lines = []
            continue
        lines.append(line)
    if lines or heading is not None or not sections:
        sections.append((heading, lines))

    chunks: list[NoteChunk] = []
    for section_heading, section_lines in sections:
        values = _section_units(section_lines, max_chars)
        if not values and section_heading:
            values = [""]
        for value in values:
            chunks.append(NoteChunk(section_heading, value, len(chunks)))
    return chunks


def strip_code(content: str) -> str:
    """Remove fenced and inline code before extracting wikilinks."""

    output: list[str] = []
    fence: str | None = None
    for line in content.splitlines():
        marker = _fence_marker(line)
        if fence is not None:
            if marker and marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if marker:
            fence = marker
            continue
        output.append(re.sub(r"`[^`\n]*`", "", line))
    return "\n".join(output)


def _normalize_link_target(target: str) -> str:
    base = target.strip().partition("#")[0].strip().replace("\\", "/")
    if base.lower().endswith(".md"):
        base = base[:-3]
    return base.strip("/")


def resolve_wikilink_target(target: str, note_paths: Iterable[str]) -> str | None:
    """Resolve one Obsidian wikilink target; ambiguous basenames stay unresolved."""

    normalized = _normalize_link_target(target)
    if not normalized:
        return None
    paths = sorted({Path(path).as_posix() for path in note_paths})
    by_no_suffix = {
        Path(path).with_suffix("").as_posix().casefold(): path for path in paths
    }
    exact = by_no_suffix.get(normalized.casefold())
    if exact:
        return exact
    matches = [path for path in paths if Path(path).stem.casefold() == normalized.casefold()]
    return matches[0] if len(matches) == 1 else None


def extract_wikilinks(content: str) -> list[str]:
    return [match.group(1).strip() for match in WIKILINK_RE.finditer(strip_code(content))]


def _extract_title(content: str, rel_path: str) -> str:
    _, body = _frontmatter_body(content)
    for line in body.splitlines():
        match = HEADING_RE.match(line)
        if match and len(match.group(1)) == 1:
            return match.group(2).strip()
    return Path(rel_path).stem.replace("-", " ").replace("_", " ").title()


def _ticket_for_note(rel_path: str, content: str) -> str | None:
    match = TICKET_RE.search(f"{rel_path}\n{content}")
    return match.group(0) if match else None


def _json_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            yield stripped
    elif isinstance(value, list):
        for item in value:
            yield from _json_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in {"encrypted_content", "data_base64"}:
                continue
            yield from _json_strings(item)


def event_text(event: Mapping[str, Any]) -> str:
    """Flatten normalized event payload strings into searchable transcript text."""

    kind = str(event.get("kind") or "unknown")
    strings = [kind]
    seen = {kind}
    for value in _json_strings(event.get("payload") or {}):
        if value in seen:
            continue
        seen.add(value)
        strings.append(value)
    return "\n".join(strings)


def _corruption_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "database disk image is malformed",
            "file is not a database",
            "database corrupt",
            "malformed database schema",
        )
    )


class KnowledgeIndex:
    def __init__(self, paths: KnowledgePaths):
        self.paths = paths
        self._lock = _path_lock(paths.db_path)

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        **overrides: Path | str | None,
    ) -> KnowledgeIndex:
        return cls(KnowledgePaths.from_env(env, **overrides))

    @property
    def rebuild_marker(self) -> Path:
        return Path(f"{self.paths.db_path}.rebuilding")

    @property
    def refresh_marker(self) -> Path:
        return Path(f"{self.paths.db_path}.refresh")

    @property
    def rebuilding(self) -> bool:
        return self.rebuild_marker.exists()

    def _mark_rebuilding(self) -> None:
        try:
            self.paths.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.rebuild_marker.touch(exist_ok=True)
        except OSError as exc:
            raise KnowledgeUnavailable(
                f"cannot mark rebuild for {self.paths.db_path}: {exc}"
            ) from exc

    def _connect_raw(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            self.paths.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            connection = sqlite3.connect(self.paths.db_path, timeout=0.75)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=750")
            connection.execute("PRAGMA synchronous=NORMAL")
            try:
                self.paths.db_path.chmod(0o600)
            except OSError:
                pass
            return connection
        except sqlite3.DatabaseError as exc:
            if connection is not None:
                connection.close()
            if _corruption_error(exc):
                raise
            raise KnowledgeUnavailable(
                f"cannot open {self.paths.db_path}: {exc}"
            ) from exc
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            raise KnowledgeUnavailable(
                f"cannot open {self.paths.db_path}: {exc}"
            ) from exc

    def _remove_database(self) -> None:
        for path in (
            self.paths.db_path,
            Path(f"{self.paths.db_path}-wal"),
            Path(f"{self.paths.db_path}-shm"),
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise KnowledgeUnavailable(f"cannot replace corrupt {path}: {exc}") from exc

    def _recover_corruption(self, exc: BaseException) -> None:
        LOGGER.warning(
            "knowledge database corrupt at %s; dropping for rebuild: %s",
            self.paths.db_path,
            exc,
        )
        with self._lock:
            self._mark_rebuilding()
            self._remove_database()
            replacement = self._connect_raw()
            try:
                self._reset_schema(replacement)
            finally:
                replacement.close()

    def _reset_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            DROP TRIGGER IF EXISTS chunks_ai;
            DROP TRIGGER IF EXISTS chunks_ad;
            DROP TRIGGER IF EXISTS chunks_au;
            DROP TABLE IF EXISTS chunks_fts;
            DROP TABLE IF EXISTS links;
            DROP TABLE IF EXISTS chunks;
            DROP TABLE IF EXISTS events;
            DROP TABLE IF EXISTS runs;
            DROP TABLE IF EXISTS notes;
            DROP TABLE IF EXISTS meta;

            CREATE TABLE meta (
                schema_version INTEGER NOT NULL,
                built_at TEXT
            );
            CREATE TABLE notes (
                path TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                type TEXT,
                tags TEXT,
                created TEXT,
                updated TEXT,
                mtime INTEGER NOT NULL,
                content_hash TEXT NOT NULL
            );
            CREATE TABLE chunks (
                id INTEGER PRIMARY KEY,
                source_kind TEXT NOT NULL CHECK(source_kind IN ('note', 'event')),
                source_id TEXT NOT NULL,
                ticket TEXT,
                title TEXT NOT NULL DEFAULT '',
                heading TEXT,
                text TEXT NOT NULL,
                pos INTEGER NOT NULL,
                UNIQUE(source_kind, source_id, pos)
            );
            CREATE VIRTUAL TABLE chunks_fts USING fts5(
                text,
                title,
                heading,
                content='chunks',
                content_rowid='id',
                tokenize='porter unicode61'
            );
            CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
                INSERT INTO chunks_fts(rowid, text, title, heading)
                VALUES (new.id, new.text, new.title, new.heading);
            END;
            CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, text, title, heading)
                VALUES ('delete', old.id, old.text, old.title, old.heading);
            END;
            CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, text, title, heading)
                VALUES ('delete', old.id, old.text, old.title, old.heading);
                INSERT INTO chunks_fts(rowid, text, title, heading)
                VALUES (new.id, new.text, new.title, new.heading);
            END;
            CREATE TABLE links (
                src_note TEXT NOT NULL REFERENCES notes(path) ON DELETE CASCADE,
                dst_name TEXT NOT NULL,
                resolved_path TEXT REFERENCES notes(path) ON DELETE SET NULL
            );
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                ticket TEXT,
                provider TEXT,
                model TEXT,
                role TEXT,
                spawned_at TEXT,
                ended_at TEXT,
                outcome TEXT
            );
            CREATE TABLE events (
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                seq INTEGER NOT NULL,
                type TEXT NOT NULL,
                ts TEXT,
                text_excerpt TEXT NOT NULL,
                PRIMARY KEY(run_id, seq)
            );
            CREATE INDEX chunks_source_idx ON chunks(source_kind, source_id, pos);
            CREATE INDEX chunks_ticket_idx ON chunks(ticket);
            CREATE INDEX links_dst_idx ON links(resolved_path);
            CREATE INDEX events_type_ts_idx ON events(type, ts);
            INSERT INTO meta(schema_version, built_at) VALUES (1, NULL);
            """
        )
        connection.execute(
            "UPDATE meta SET schema_version = ?", (SCHEMA_VERSION,)
        )
        connection.commit()

    def _prepare(self) -> tuple[sqlite3.Connection, bool]:
        """Return a usable connection and whether a full rebuild is required."""

        with self._lock:
            connection: sqlite3.Connection | None = None
            try:
                connection = self._connect_raw()
                try:
                    row = connection.execute(
                        "SELECT schema_version FROM meta LIMIT 1"
                    ).fetchone()
                except sqlite3.OperationalError as exc:
                    if "no such table" not in str(exc).lower():
                        raise
                    row = None
                if row is not None and int(row[0]) == SCHEMA_VERSION:
                    return connection, False
                LOGGER.warning(
                    "knowledge schema missing/mismatched at %s; rebuilding",
                    self.paths.db_path,
                )
                self._mark_rebuilding()
                self._reset_schema(connection)
                return connection, True
            except sqlite3.DatabaseError as exc:
                if connection is not None:
                    connection.close()
                if not _corruption_error(exc):
                    raise KnowledgeUnavailable(
                        f"cannot inspect {self.paths.db_path}: {exc}"
                    ) from exc
                self._recover_corruption(exc)
                replacement = self._connect_raw()
                return replacement, True
            except KnowledgeError:
                if connection is not None:
                    connection.close()
                raise
            except (OSError, sqlite3.Error) as exc:
                if connection is not None:
                    connection.close()
                raise KnowledgeUnavailable(
                    f"cannot prepare {self.paths.db_path}: {exc}"
                ) from exc

    def _finish_build(
        self,
        connection: sqlite3.Connection,
        *,
        clear_rebuild_marker: bool,
    ) -> None:
        built_at = datetime.now(timezone.utc).isoformat()
        connection.execute("UPDATE meta SET built_at = ?", (built_at,))
        connection.commit()
        if clear_rebuild_marker:
            self.rebuild_marker.unlink(missing_ok=True)

    def request_refresh(self) -> bool:
        """Enqueue a cheap cross-process refresh request; never raise to writers."""

        try:
            self.paths.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.refresh_marker.touch(exist_ok=True)
        except OSError:
            LOGGER.exception("could not enqueue knowledge refresh")
            return False
        return True

    def _refresh_request_mtime(self) -> int | None:
        try:
            return self.refresh_marker.stat().st_mtime_ns
        except OSError:
            return None

    def _clear_refresh_request(self, observed_mtime: int | None) -> None:
        if observed_mtime is None:
            return
        try:
            if self.refresh_marker.stat().st_mtime_ns == observed_mtime:
                self.refresh_marker.unlink(missing_ok=True)
        except OSError:
            pass

    def _note_files(self) -> list[tuple[str, Path]]:
        if not self.paths.vault_dir.is_dir():
            return []
        files: list[tuple[str, Path]] = []
        vault = self.paths.vault_dir.resolve()
        for candidate in self.paths.vault_dir.rglob("*.md"):
            try:
                candidate_rel = candidate.relative_to(self.paths.vault_dir)
            except ValueError:
                continue
            if not candidate.is_file() or any(
                part.startswith(".") for part in candidate_rel.parts
            ):
                continue
            try:
                resolved = candidate.resolve()
                rel = resolved.relative_to(vault).as_posix()
            except (OSError, ValueError):
                continue
            files.append((rel, resolved))
        return sorted(files)

    def scan_vault(self) -> IngestStats:
        started = time.perf_counter()
        connection, _ = self._prepare()
        stats = IngestStats()
        try:
            existing = {
                str(row["path"]): row
                for row in connection.execute(
                    "SELECT path, mtime, content_hash FROM notes"
                )
            }
            contents: dict[str, str] = {}
            details: dict[str, tuple[int, str]] = {}
            for rel, path in self._note_files():
                try:
                    data = path.read_bytes()
                    content = data.decode("utf-8")
                    mtime = path.stat().st_mtime_ns
                except (OSError, UnicodeDecodeError) as exc:
                    LOGGER.warning("skipping unreadable vault note %s: %s", path, exc)
                    continue
                digest = hashlib.sha256(data).hexdigest()
                contents[rel] = content
                details[rel] = (mtime, digest)
                stats.notes_scanned += 1

            with connection:
                removed = sorted(set(existing) - set(contents))
                for rel in removed:
                    connection.execute(
                        "DELETE FROM chunks WHERE source_kind = 'note' AND source_id = ?",
                        (rel,),
                    )
                    connection.execute("DELETE FROM notes WHERE path = ?", (rel,))
                    stats.notes_deleted += 1

                for rel in sorted(contents):
                    content = contents[rel]
                    mtime, digest = details[rel]
                    previous = existing.get(rel)
                    if previous is not None and str(previous["content_hash"]) == digest:
                        if int(previous["mtime"]) != mtime:
                            connection.execute(
                                "UPDATE notes SET mtime = ? WHERE path = ?", (mtime, rel)
                            )
                            stats.notes_metadata_updated += 1
                        continue

                    fields, _ = _frontmatter_body(content)
                    title = _extract_title(content, rel)
                    connection.execute(
                        """
                        INSERT INTO notes(path, title, type, tags, created, updated, mtime, content_hash)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(path) DO UPDATE SET
                            title=excluded.title,
                            type=excluded.type,
                            tags=excluded.tags,
                            created=excluded.created,
                            updated=excluded.updated,
                            mtime=excluded.mtime,
                            content_hash=excluded.content_hash
                        """,
                        (
                            rel,
                            title,
                            fields.get("type"),
                            fields.get("tags"),
                            fields.get("created"),
                            fields.get("updated"),
                            mtime,
                            digest,
                        ),
                    )
                    connection.execute(
                        "DELETE FROM chunks WHERE source_kind = 'note' AND source_id = ?",
                        (rel,),
                    )
                    ticket = _ticket_for_note(rel, content)
                    chunks = chunk_markdown(content)
                    if not chunks:
                        chunks = [NoteChunk(None, "", 0)]
                    for chunk in chunks:
                        connection.execute(
                            """
                            INSERT INTO chunks(
                                source_kind, source_id, ticket, title, heading, text, pos
                            ) VALUES ('note', ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                rel,
                                ticket,
                                title,
                                chunk.heading,
                                chunk.text,
                                chunk.pos,
                            ),
                        )
                    stats.notes_indexed += 1
                    stats.chunks_indexed += len(chunks)

                # Link resolution depends on the complete filename set. Rebuilding
                # this small table also resolves links from unchanged notes when a
                # target is created, renamed, or deleted.
                connection.execute("DELETE FROM links")
                note_paths = sorted(contents)
                for rel in note_paths:
                    for target in extract_wikilinks(contents[rel]):
                        connection.execute(
                            "INSERT INTO links(src_note, dst_name, resolved_path) VALUES (?, ?, ?)",
                            (
                                rel,
                                target,
                                rel
                                if not _normalize_link_target(target)
                                else resolve_wikilink_target(target, note_paths),
                            ),
                        )
                        stats.links_indexed += 1
            return stats
        except (OSError, sqlite3.Error) as exc:
            if isinstance(exc, sqlite3.Error) and _corruption_error(exc):
                connection.close()
                self._recover_corruption(exc)
            raise KnowledgeUnavailable(f"vault indexing failed: {exc}") from exc
        finally:
            connection.close()
            stats.elapsed_seconds = time.perf_counter() - started

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _last_event_seq(path: Path) -> int:
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                end = handle.tell()
                offset = max(0, end - 256 * 1024)
                handle.seek(offset)
                data = handle.read()
        except OSError:
            return 0
        lines = data.splitlines()
        if offset and lines:
            lines = lines[1:]
        for raw in reversed(lines):
            try:
                value = json.loads(raw)
                seq = int(value.get("seq", 0)) if isinstance(value, dict) else 0
            except (ValueError, TypeError):
                continue
            if seq > 0:
                return seq
        return 0

    @staticmethod
    def _run_metadata(run_dir: Path) -> dict[str, Any]:
        run = KnowledgeIndex._read_json(run_dir / "run.json")
        meta = KnowledgeIndex._read_json(run_dir / "meta.json")
        worker = meta.get("worker") if isinstance(meta.get("worker"), dict) else {}
        ticket = (
            run.get("agent_id")
            or worker.get("ticket")
            or worker.get("agent_id")
            or (run_dir.parent.name if run_dir.parent.name else None)
        )
        run_id = run.get("run_id") or worker.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            digest = hashlib.sha256(str(run_dir.absolute()).encode()).hexdigest()[:24]
            run_id = f"archive-{digest}"
        provider = run.get("provider") or worker.get("provider")
        if provider is None:
            provider = {"cdx": "codex", "cc": "claude"}.get(worker.get("kind"))
        return {
            "run_id": run_id,
            "ticket": str(ticket) if ticket else None,
            "provider": str(provider) if provider else None,
            "model": run.get("model") or worker.get("model"),
            "role": run.get("role") or worker.get("role"),
            "spawned_at": run.get("created_at") or worker.get("spawned_at"),
            "ended_at": meta.get("ended_at") or worker.get("ended_at"),
            "outcome": meta.get("outcome") or run.get("outcome") or worker.get("outcome"),
        }

    def index_run_directory(self, run_dir: Path) -> IngestStats:
        """Delta-index one live or archived run directory."""

        started = time.perf_counter()
        events_path = run_dir / "events.jsonl"
        metadata = self._run_metadata(run_dir)
        run_id = str(metadata["run_id"])
        connection, _ = self._prepare()
        stats = IngestStats(runs_indexed=1)
        try:
            current_row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            after_seq = int(current_row["seq"] if current_row else 0)
            last_seq = self._last_event_seq(events_path)
            with connection:
                connection.execute(
                    """
                    INSERT INTO runs(run_id, ticket, provider, model, role, spawned_at, ended_at, outcome)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        ticket=excluded.ticket,
                        provider=excluded.provider,
                        model=excluded.model,
                        role=excluded.role,
                        spawned_at=excluded.spawned_at,
                        ended_at=COALESCE(excluded.ended_at, runs.ended_at),
                        outcome=COALESCE(excluded.outcome, runs.outcome)
                    """,
                    (
                        run_id,
                        metadata["ticket"],
                        metadata["provider"],
                        metadata["model"],
                        metadata["role"],
                        metadata["spawned_at"],
                        metadata["ended_at"],
                        metadata["outcome"],
                    ),
                )
                if not events_path.is_file() or (last_seq > 0 and last_seq <= after_seq):
                    return stats
                try:
                    lines = events_path.read_text(encoding="utf-8").splitlines()
                except FileNotFoundError:
                    lines = []
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise ValueError("event is not an object")
                        seq = int(event.get("seq", 0))
                        if seq < 1:
                            raise ValueError("event sequence is missing")
                    except (TypeError, ValueError):
                        stats.malformed_event_lines += 1
                        continue
                    if seq <= after_seq:
                        continue
                    payload = event.get("payload")
                    if not isinstance(payload, dict):
                        payload = {}
                    kind = str(
                        event.get("kind")
                        or payload.get("type")
                        or payload.get("method")
                        or "unknown"
                    )
                    text = event_text(event)
                    excerpt = re.sub(r"\s+", " ", text).strip()[:500]
                    ts = event.get("normalized_at") or event.get("ts")
                    if not ts:
                        ts = payload.get("timestamp") or payload.get("ts")
                    connection.execute(
                        """
                        INSERT INTO events(run_id, seq, type, ts, text_excerpt)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(run_id, seq) DO UPDATE SET
                            type=excluded.type,
                            ts=excluded.ts,
                            text_excerpt=excluded.text_excerpt
                        """,
                        (run_id, seq, kind, ts, excerpt),
                    )
                    connection.execute(
                        "DELETE FROM chunks WHERE source_kind = 'event' AND source_id = ? AND pos = ?",
                        (run_id, seq),
                    )
                    connection.execute(
                        """
                        INSERT INTO chunks(
                            source_kind, source_id, ticket, title, heading, text, pos
                        ) VALUES ('event', ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            metadata["ticket"],
                            metadata["ticket"] or run_id,
                            kind,
                            text,
                            seq,
                        ),
                    )
                    stats.events_indexed += 1
                    stats.chunks_indexed += 1
            if stats.malformed_event_lines:
                LOGGER.warning(
                    "skipped %d malformed event lines in %s",
                    stats.malformed_event_lines,
                    events_path,
                )
            return stats
        except (OSError, sqlite3.Error) as exc:
            if isinstance(exc, sqlite3.Error) and _corruption_error(exc):
                connection.close()
                self._recover_corruption(exc)
            raise KnowledgeUnavailable(f"run indexing failed for {run_dir}: {exc}") from exc
        finally:
            connection.close()
            stats.elapsed_seconds = time.perf_counter() - started

    def scan_archives(self) -> IngestStats:
        stats = IngestStats()
        started = time.perf_counter()
        if self.paths.archive_dir.is_dir():
            for events_path in sorted(self.paths.archive_dir.rglob("events.jsonl")):
                if events_path.is_file():
                    stats.merge(self.index_run_directory(events_path.parent))
        stats.elapsed_seconds = time.perf_counter() - started
        return stats

    def index_live_runs(self) -> IngestStats:
        stats = IngestStats()
        started = time.perf_counter()
        runs_dir = self.paths.runtime_dir / "runs"
        if runs_dir.is_dir():
            for events_path in sorted(runs_dir.glob("*/events.jsonl")):
                if events_path.is_file():
                    stats.merge(self.index_run_directory(events_path.parent))
        stats.elapsed_seconds = time.perf_counter() - started
        return stats

    def rebuild(self) -> IngestStats:
        """Drop derived data and deterministically restore it from source files."""

        started = time.perf_counter()
        refresh_mtime = self._refresh_request_mtime()
        with self._lock:
            self._mark_rebuilding()
            connection = self._connect_raw()
            try:
                self._reset_schema(connection)
            finally:
                connection.close()
        stats = IngestStats()
        try:
            stats.merge(self.scan_vault())
            stats.merge(self.scan_archives())
            connection, _ = self._prepare()
            try:
                self._finish_build(connection, clear_rebuild_marker=True)
            finally:
                connection.close()
            self._clear_refresh_request(refresh_mtime)
        finally:
            stats.elapsed_seconds = time.perf_counter() - started
        return stats

    def refresh_all(self) -> IngestStats:
        """Run a startup/requested delta refresh, rebuilding on schema reset."""

        refresh_mtime = self._refresh_request_mtime()
        connection, needs_rebuild = self._prepare()
        connection.close()
        if needs_rebuild or self.rebuilding:
            return self.rebuild()
        started = time.perf_counter()
        stats = IngestStats()
        stats.merge(self.scan_vault())
        stats.merge(self.scan_archives())
        connection, _ = self._prepare()
        try:
            self._finish_build(connection, clear_rebuild_marker=False)
        finally:
            connection.close()
        self._clear_refresh_request(refresh_mtime)
        stats.elapsed_seconds = time.perf_counter() - started
        return stats

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = FTS_TOKEN_RE.findall(query)
        if not tokens:
            raise KnowledgeQueryError("query must contain a searchable word")
        return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)

    @staticmethod
    def _since_value(value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise KnowledgeQueryError("since must be a YYYY-MM-DD date") from exc

    def search(
        self,
        query: str,
        *,
        ticket: str | None = None,
        kind: str | None = None,
        event_type: str | None = None,
        since: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        if kind not in {None, "note", "run"}:
            raise KnowledgeQueryError("kind must be note or run")
        if not 1 <= limit <= MAX_SEARCH_LIMIT:
            raise KnowledgeQueryError(f"limit must be between 1 and {MAX_SEARCH_LIMIT}")
        fts_query = self._fts_query(query)
        since_value = self._since_value(since)
        connection, _ = self._prepare()
        connection.close()
        # Live runs are append-only and indexed on first query, delta by seq.
        self.index_live_runs()
        connection, _ = self._prepare()
        try:
            clauses = ["chunks_fts MATCH ?"]
            params: list[Any] = [fts_query]
            if ticket:
                clauses.append("UPPER(COALESCE(c.ticket, '')) = ?")
                params.append(ticket.upper())
            if kind == "note":
                clauses.append("c.source_kind = 'note'")
            elif kind == "run":
                clauses.append("c.source_kind = 'event'")
            if event_type:
                clauses.append("c.source_kind = 'event' AND e.type = ?")
                params.append(event_type)
            if since_value:
                clauses.append(
                    """
                    CASE WHEN c.source_kind = 'note'
                         THEN COALESCE(n.updated, n.created, '')
                         ELSE COALESCE(e.ts, r.ended_at, r.spawned_at, '')
                    END >= ?
                    """
                )
                params.append(since_value)
            params.append(limit)
            rows = connection.execute(
                f"""
                SELECT
                    c.source_kind,
                    c.source_id,
                    c.ticket,
                    c.heading,
                    n.path AS note_path,
                    e.run_id AS run_id,
                    e.seq AS event_seq,
                    e.type AS event_type,
                    snippet(chunks_fts, 0, '[', ']', ' … ', 18) AS snippet,
                    bm25(chunks_fts, 1.0, 2.0, 1.5) AS rank
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                LEFT JOIN notes n
                    ON c.source_kind = 'note' AND n.path = c.source_id
                LEFT JOIN events e
                    ON c.source_kind = 'event'
                    AND e.run_id = c.source_id
                    AND e.seq = c.pos
                LEFT JOIN runs r ON r.run_id = e.run_id
                WHERE {' AND '.join(clauses)}
                ORDER BY rank, c.id
                LIMIT ?
                """,
                params,
            ).fetchall()
            results: list[dict[str, Any]] = []
            for row in rows:
                if row["source_kind"] == "note":
                    citation = str(row["note_path"] or row["source_id"])
                    result = {
                        "kind": "note",
                        "citation": citation,
                        "path": citation,
                        "ticket": row["ticket"],
                        "heading": row["heading"],
                        "snippet": row["snippet"] or "",
                        "score": round(-float(row["rank"]), 8),
                    }
                else:
                    run_id = str(row["run_id"] or row["source_id"])
                    seq = int(row["event_seq"] or 0)
                    result = {
                        "kind": "run",
                        "citation": f"{run_id}:{seq}",
                        "run_id": run_id,
                        "seq": seq,
                        "ticket": row["ticket"],
                        "type": row["event_type"],
                        "snippet": row["snippet"] or "",
                        "score": round(-float(row["rank"]), 8),
                    }
                results.append(result)
            return {
                "query": query,
                "results": results,
                "rebuilding": self.rebuilding,
            }
        except sqlite3.Error as exc:
            if _corruption_error(exc):
                connection.close()
                self._recover_corruption(exc)
                return {"query": query, "results": [], "rebuilding": True}
            raise KnowledgeUnavailable(f"knowledge query failed: {exc}") from exc
        finally:
            connection.close()

    def _note_paths(self, connection: sqlite3.Connection) -> list[str]:
        return [str(row[0]) for row in connection.execute("SELECT path FROM notes ORDER BY path")]

    def backlinks(self, note: str) -> dict[str, Any]:
        connection, _ = self._prepare()
        try:
            resolved = resolve_wikilink_target(note, self._note_paths(connection))
            if resolved is None:
                if self.rebuilding:
                    return {
                        "note": note,
                        "backlinks": [],
                        "rebuilding": True,
                    }
                raise KnowledgeQueryError(f"note not found or ambiguous: {note}")
            rows = connection.execute(
                "SELECT DISTINCT src_note FROM links WHERE resolved_path = ? ORDER BY src_note",
                (resolved,),
            )
            return {
                "note": resolved,
                "backlinks": [str(row[0]) for row in rows],
                "rebuilding": self.rebuilding,
            }
        except sqlite3.Error as exc:
            if _corruption_error(exc):
                connection.close()
                self._recover_corruption(exc)
                return {"note": note, "backlinks": [], "rebuilding": True}
            raise KnowledgeUnavailable(f"backlinks query failed: {exc}") from exc
        finally:
            connection.close()

    def orphans(self) -> dict[str, Any]:
        connection, _ = self._prepare()
        try:
            rows = connection.execute(
                """
                SELECT n.path
                FROM notes n
                WHERE NOT EXISTS (
                    SELECT 1 FROM links l WHERE l.resolved_path = n.path
                )
                ORDER BY n.path
                """
            )
            return {
                "orphans": [str(row[0]) for row in rows],
                "rebuilding": self.rebuilding,
            }
        except sqlite3.Error as exc:
            if _corruption_error(exc):
                connection.close()
                self._recover_corruption(exc)
                return {"orphans": [], "rebuilding": True}
            raise KnowledgeUnavailable(f"orphans query failed: {exc}") from exc
        finally:
            connection.close()

    def unresolved(self) -> dict[str, Any]:
        connection, _ = self._prepare()
        try:
            rows = connection.execute(
                """
                SELECT src_note, dst_name
                FROM links
                WHERE resolved_path IS NULL
                ORDER BY src_note, dst_name
                """
            )
            return {
                "unresolved": [
                    {"src_note": str(row[0]), "dst_name": str(row[1])} for row in rows
                ],
                "rebuilding": self.rebuilding,
            }
        except sqlite3.Error as exc:
            if _corruption_error(exc):
                connection.close()
                self._recover_corruption(exc)
                return {"unresolved": [], "rebuilding": True}
            raise KnowledgeUnavailable(f"unresolved query failed: {exc}") from exc
        finally:
            connection.close()

    def row_counts(self) -> dict[str, int]:
        connection, _ = self._prepare()
        try:
            return {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ("notes", "chunks", "chunks_fts", "links", "runs", "events")
            }
        finally:
            connection.close()


def enqueue_refresh(
    env: Mapping[str, str] | None = None,
    *,
    runtime_dir: Path | str | None = None,
) -> bool:
    return KnowledgeIndex.from_env(env, runtime_dir=runtime_dir).request_refresh()


async def background_index_loop(
    paths: KnowledgePaths,
    *,
    poll_seconds: float = 0.5,
) -> None:
    """Own backend ingest work while keeping startup and vault writes non-blocking."""

    index = KnowledgeIndex(paths)
    first = True
    while True:
        delay = poll_seconds
        if first or index.refresh_marker.exists() or index.rebuild_marker.exists():
            first = False
            try:
                stats = await asyncio.to_thread(index.refresh_all)
                LOGGER.info("knowledge index refreshed: %s", stats.to_dict())
            except asyncio.CancelledError:
                raise
            except Exception:
                # A broken cache cannot take down note writes or the fleet.
                LOGGER.exception("knowledge background refresh failed")
                first = True
                delay = max(5.0, poll_seconds)
        await asyncio.sleep(delay)
