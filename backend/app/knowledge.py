"""Rebuildable SQLite knowledge index for vault notes and Wiki fleet runs."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .knowledge_content import (
    NoteChunk,
    chunk_markdown,
    extract_title,
    extract_wikilinks,
    frontmatter_body,
    normalize_link_target,
    resolve_wikilink_target,
    ticket_for_note,
)
from .knowledge_runs import (
    is_supervisor_archive,
    last_event_seq,
    load_run_metadata,
    read_run_events,
)
from .knowledge_schema import (
    SCHEMA_VERSION,
    is_corruption_error as _corruption_error,
    reset_schema,
)
from .semantic_index import SemanticIndex, SemanticNote


LOGGER = logging.getLogger(__name__)
MAX_SEARCH_LIMIT = 100
SEMANTIC_SCORE_FLOOR = 0.15
FTS_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()
_REFRESH_GATES: dict[str, threading.Lock] = {}
_REFRESH_GATES_GUARD = threading.Lock()
_DEFAULT_PROVIDER = object()


class KnowledgeError(RuntimeError):
    """Base error for explicit CLI/MCP knowledge failures."""


class KnowledgeUnavailable(KnowledgeError):
    """The configured SQLite database cannot be opened or queried."""


class KnowledgeQueryError(KnowledgeError):
    """The caller supplied an invalid knowledge query."""


class KnowledgeSourceError(KnowledgeError):
    """One source file could not be ingested while the database remains usable."""


@dataclass
class IngestStats:
    notes_scanned: int = 0
    notes_indexed: int = 0
    notes_metadata_updated: int = 0
    notes_deleted: int = 0
    chunks_indexed: int = 0
    links_indexed: int = 0
    runs_indexed: int = 0
    runs_skipped: int = 0
    events_indexed: int = 0
    malformed_event_lines: int = 0
    event_chunks_excerpted: int = 0
    base64_blob_lines_skipped: int = 0
    ansi_heavy_lines_skipped: int = 0
    legacy_runs_skipped: int = 0
    embeddings_indexed: int = 0
    embeddings_skipped: int = 0
    semantic_unavailable: int = 0
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
            "runs_skipped",
            "events_indexed",
            "malformed_event_lines",
            "event_chunks_excerpted",
            "base64_blob_lines_skipped",
            "ansi_heavy_lines_skipped",
            "legacy_runs_skipped",
            "embeddings_indexed",
            "embeddings_skipped",
            "semantic_unavailable",
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
    include_legacy_archives: bool = False

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
            include_legacy_archives=values.get("WIKI_KNOWLEDGE_INCLUDE_LEGACY", "").lower()
            in {"1", "true", "yes", "on"},
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


def _refresh_gate(path: Path) -> threading.Lock:
    key = str(path.absolute())
    with _REFRESH_GATES_GUARD:
        return _REFRESH_GATES.setdefault(key, threading.Lock())


class KnowledgeIndex:
    def __init__(
        self,
        paths: KnowledgePaths,
        embedding_provider: EmbeddingProvider | None | object = _DEFAULT_PROVIDER,
        *,
        provider_env: Mapping[str, str] | None = None,
    ):
        self.paths = paths
        self._lock = _path_lock(paths.db_path)
        if embedding_provider is _DEFAULT_PROVIDER:
            self.semantic_index = SemanticIndex(paths.db_path, env=provider_env)
        else:
            self.semantic_index = SemanticIndex(
                paths.db_path,
                embedding_provider=embedding_provider,
            )

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        **overrides: Path | str | None,
    ) -> KnowledgeIndex:
        values = os.environ if env is None else env
        return cls(
            KnowledgePaths.from_env(values, **overrides),
            provider_env=values,
        )

    def semantic_status(self) -> dict[str, Any]:
        return self.semantic_index.status()

    def activate_semantic(self) -> bool:
        activated = self.semantic_index.activate()
        if activated:
            self.request_refresh()
        return activated

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
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            if str(journal_mode).lower() != "wal":
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

    def _quarantine_database(self) -> Path:
        quarantine = Path(
            f"{self.paths.db_path}.corrupt-{time.time_ns()}-{os.getpid()}"
        )
        for source, destination in (
            (self.paths.db_path, quarantine),
            (Path(f"{self.paths.db_path}-wal"), Path(f"{quarantine}-wal")),
            (Path(f"{self.paths.db_path}-shm"), Path(f"{quarantine}-shm")),
        ):
            try:
                source.replace(destination)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise KnowledgeUnavailable(
                    f"cannot quarantine corrupt {source}: {exc}"
                ) from exc
        return quarantine

    def _recover_corruption(self, exc: BaseException) -> None:
        LOGGER.warning(
            "knowledge database corrupt at %s; quarantining for rebuild: %s",
            self.paths.db_path,
            exc,
        )
        with self._lock:
            self._mark_rebuilding()
            quarantine = self._quarantine_database()
            LOGGER.warning("quarantined corrupt knowledge database at %s", quarantine)
            replacement = self._connect_raw()
            try:
                reset_schema(replacement)
            finally:
                replacement.close()

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
                reset_schema(connection)
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

                    fields, _ = frontmatter_body(content)
                    title = extract_title(content, rel)
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
                    ticket = ticket_for_note(rel, content)
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
                                if not normalize_link_target(target)
                                else resolve_wikilink_target(target, note_paths),
                            ),
                        )
                        stats.links_indexed += 1
            semantic_stats = self.semantic_index.refresh(
                [
                    SemanticNote(
                        path=rel,
                            content_hash=details[rel][1],
                            title=extract_title(contents[rel], rel),
                            text=contents[rel][:120_000],
                            snippet=contents[rel][:500],
                        ticket=ticket_for_note(rel, contents[rel]),
                    )
                    for rel in sorted(contents)
                ]
            )
            stats.embeddings_indexed += semantic_stats.embeddings_indexed
            stats.embeddings_skipped += semantic_stats.embeddings_skipped
            stats.semantic_unavailable += semantic_stats.semantic_unavailable
            return stats
        except (OSError, sqlite3.Error) as exc:
            if isinstance(exc, sqlite3.Error) and _corruption_error(exc):
                connection.close()
                self._recover_corruption(exc)
            raise KnowledgeUnavailable(f"vault indexing failed: {exc}") from exc
        finally:
            connection.close()
            stats.elapsed_seconds = time.perf_counter() - started

    def index_run_directory(self, run_dir: Path) -> IngestStats:
        """Delta-index one live or archived run directory."""

        started = time.perf_counter()
        events_path = run_dir / "events.jsonl"
        metadata = load_run_metadata(run_dir)
        run_id = metadata.run_id
        connection, _ = self._prepare()
        stats = IngestStats(runs_indexed=1)
        try:
            current_row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            after_seq = int(current_row["seq"] if current_row else 0)
            last_seq = last_event_seq(events_path)
            batch = (
                read_run_events(events_path, after_seq=after_seq)
                if events_path.is_file() and not (last_seq > 0 and last_seq <= after_seq)
                else None
            )
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
                        metadata.ticket,
                        metadata.provider,
                        metadata.model,
                        metadata.role,
                        metadata.spawned_at,
                        metadata.ended_at,
                        metadata.outcome,
                    ),
                )
                for event in batch.events if batch else ():
                    connection.execute(
                        """
                        INSERT INTO events(run_id, seq, type, ts, text_excerpt)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(run_id, seq) DO UPDATE SET
                            type=excluded.type,
                            ts=excluded.ts,
                            text_excerpt=excluded.text_excerpt
                        """,
                        (
                            run_id,
                            event.seq,
                            event.event_type,
                            event.ts,
                            event.excerpt,
                        ),
                    )
                    connection.execute(
                        "DELETE FROM chunks WHERE source_kind = 'event' AND source_id = ? AND pos = ?",
                        (run_id, event.seq),
                    )
                    connection.execute(
                        """
                        INSERT INTO chunks(
                            source_kind, source_id, ticket, title, heading, text, pos
                        ) VALUES ('event', ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            metadata.ticket,
                            metadata.ticket or run_id,
                            event.event_type,
                            event.text,
                            event.seq,
                        ),
                    )
                    stats.chunks_indexed += 1
                    stats.events_indexed += 1
            stats.malformed_event_lines = batch.malformed_lines if batch else 0
            stats.event_chunks_excerpted = batch.event_chunks_excerpted if batch else 0
            stats.base64_blob_lines_skipped = batch.base64_blob_lines_skipped if batch else 0
            stats.ansi_heavy_lines_skipped = batch.ansi_heavy_lines_skipped if batch else 0
            if stats.malformed_event_lines:
                LOGGER.warning(
                    "skipped %d malformed event lines in %s",
                    stats.malformed_event_lines,
                    events_path,
                )
            return stats
        except sqlite3.Error as exc:
            if _corruption_error(exc):
                connection.close()
                self._recover_corruption(exc)
            raise KnowledgeUnavailable(f"run indexing failed for {run_dir}: {exc}") from exc
        except (OSError, UnicodeError) as exc:
            raise KnowledgeSourceError(
                f"run source unreadable for {run_dir}: {exc}"
            ) from exc
        finally:
            connection.close()
            stats.elapsed_seconds = time.perf_counter() - started

    def scan_archives(self, *, include_legacy_archives: bool | None = None) -> IngestStats:
        stats = IngestStats()
        started = time.perf_counter()
        include_legacy = (
            self.paths.include_legacy_archives
            if include_legacy_archives is None
            else include_legacy_archives
        )
        if self.paths.archive_dir.is_dir():
            for events_path in sorted(self.paths.archive_dir.rglob("events.jsonl")):
                if events_path.is_file():
                    if not include_legacy and not is_supervisor_archive(events_path.parent):
                        stats.legacy_runs_skipped += 1
                        continue
                    try:
                        stats.merge(self.index_run_directory(events_path.parent))
                    except KnowledgeSourceError as exc:
                        stats.runs_skipped += 1
                        LOGGER.warning("skipping unreadable archived run: %s", exc)
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

    def rebuild(
        self,
        *,
        include_legacy_archives: bool | None = None,
        explicit: bool = True,
    ) -> IngestStats:
        """Drop derived data and deterministically restore it from source files."""

        started = time.perf_counter()
        refresh_mtime = self._refresh_request_mtime()
        with self._lock:
            self._mark_rebuilding()
            connection = self._connect_raw()
            try:
                reset_schema(connection)
            finally:
                connection.close()
        if explicit:
            self.semantic_index.reset_for_explicit_rebuild()
        stats = IngestStats()
        try:
            stats.merge(self.scan_vault())
            stats.merge(self.scan_archives(include_legacy_archives=include_legacy_archives))
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

        with _refresh_gate(self.paths.db_path):
            return self._refresh_all()

    def _refresh_all(self) -> IngestStats:
        """Run one serialized refresh pass."""

        refresh_mtime = self._refresh_request_mtime()
        connection, needs_rebuild = self._prepare()
        connection.close()
        if needs_rebuild or self.rebuilding:
            return self.rebuild(explicit=False)
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
        connection, needs_rebuild = self._prepare()
        stale = needs_rebuild or self.rebuilding
        # Live runs are append-only and indexed on first query, delta by seq.
        try:
            self.index_live_runs()
        except (KnowledgeError, OSError) as exc:
            stale = True
            LOGGER.warning("live run indexing failed; returning stale results: %s", exc)
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
                "stale": stale,
            }
        except sqlite3.Error as exc:
            if _corruption_error(exc):
                connection.close()
                self._recover_corruption(exc)
                return {
                    "query": query,
                    "results": [],
                    "rebuilding": True,
                    "stale": True,
                }
            raise KnowledgeUnavailable(f"knowledge query failed: {exc}") from exc
        finally:
            connection.close()

    def search_semantic(
        self,
        query: str,
        *,
        ticket: str | None = None,
        limit: int = 20,
        score_floor: float = SEMANTIC_SCORE_FLOOR,
    ) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise KnowledgeQueryError("query must contain a searchable word")
        if not 1 <= limit <= MAX_SEARCH_LIMIT:
            raise KnowledgeQueryError(f"limit must be between 1 and {MAX_SEARCH_LIMIT}")
        if ticket is not None and not ticket.strip():
            raise KnowledgeQueryError("ticket must not be empty")
        try:
            payload = self.semantic_index.search(
                query,
                ticket=ticket,
                limit=limit,
                score_floor=score_floor,
            )
        except Exception as exc:
            LOGGER.warning("semantic query unavailable; using lexical fallback: %s", exc)
            payload = {
                "results": [],
                "semantic": {
                    "available": False,
                    "active": False,
                    "indexing": False,
                    "model": None,
                    "reason": f"semantic search unavailable: {exc}",
                },
                "rebuilding": True,
                "stale": True,
            }
        if payload["semantic"]["available"]:
            return {"query": query, **payload}
        try:
            fallback = self.search(query, kind="note", limit=limit)
            fallback_results = fallback["results"]
        except KnowledgeError:
            fallback_results = []
        return {
            "query": query,
            "results": fallback_results,
            "lexical_results": fallback_results,
            "semantic_results": payload["results"],
            "fallback": "lexical",
            "semantic": payload["semantic"],
            "rebuilding": payload.get("rebuilding", False),
            "stale": payload.get("stale", False),
        }

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
                for table in (
                    "notes",
                    "chunks",
                    "chunks_fts",
                    "links",
                    "runs",
                    "events",
                )
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
