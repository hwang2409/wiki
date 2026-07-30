"""Opt-in, incremental semantic index for vault notes."""

from __future__ import annotations

import heapq
import logging
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .semantic_search import (
    EmbeddingProvider,
    EmbeddingUnavailable,
    configured_provider,
    cosine_normalized,
    normalise_vector,
    pack_vector,
    provider_model,
    unpack_vector,
)


LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 1
MAX_EMBED_BATCH_SIZE = 32
MAX_EMBED_TEXT_CHARS = 120_000
SEMANTIC_SCORE_FLOOR = 0.15
MAX_SEARCH_LIMIT = 100
_DEFAULT_PROVIDER = object()


@dataclass(frozen=True)
class SemanticNote:
    path: str
    content_hash: str
    title: str
    text: str
    snippet: str
    ticket: str | None


@dataclass
class SemanticRefreshStats:
    embeddings_indexed: int = 0
    embeddings_skipped: int = 0
    semantic_unavailable: int = 0


@dataclass(frozen=True)
class _ReversePath:
    value: str

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, _ReversePath):
            return NotImplemented
        return self.value > other.value


class SemanticIndex:
    """Own semantic storage and lifecycle separately from lexical knowledge."""

    def __init__(
        self,
        knowledge_db_path: Path | str,
        embedding_provider: EmbeddingProvider | None | object = _DEFAULT_PROVIDER,
        *,
        env: Mapping[str, str] | None = None,
        lexical_fallback: Callable[[str, str | None, int], list[dict[str, Any]]] | None = None,
        query_error: Callable[[str], Exception] | None = None,
    ) -> None:
        self.db_path = Path(f"{knowledge_db_path}.semantic").absolute()
        self.embedding_provider = (
            configured_provider(env)
            if embedding_provider is _DEFAULT_PROVIDER
            else embedding_provider
        )
        self.lexical_fallback = lexical_fallback
        self.query_error = query_error
        self._semantic_error: str | None = None
        self._recovered_corruption = False

    @property
    def rebuild_marker(self) -> Path:
        return Path(f"{self.db_path}.rebuilding")

    @property
    def active_marker(self) -> Path:
        return Path(f"{self.db_path}.enabled")

    def _connect(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            self.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            connection = sqlite3.connect(self.db_path, timeout=0.75)
            connection.row_factory = sqlite3.Row
            if str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
                connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=750")
            connection.execute("PRAGMA synchronous=NORMAL")
            try:
                self.db_path.chmod(0o600)
            except OSError:
                pass
            return connection
        except sqlite3.DatabaseError:
            if connection is not None:
                connection.close()
            raise
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            raise RuntimeError(f"cannot open semantic index {self.db_path}: {exc}") from exc

    def _reset(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            DROP TABLE IF EXISTS embeddings;
            DROP TABLE IF EXISTS meta;
            CREATE TABLE meta (
                schema_version INTEGER NOT NULL,
                built_at TEXT,
                enabled INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE embeddings (
                path TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                model TEXT NOT NULL,
                dimension INTEGER NOT NULL CHECK(dimension > 0),
                vector BLOB NOT NULL,
                title TEXT NOT NULL,
                snippet TEXT NOT NULL,
                ticket TEXT,
                embedded_at TEXT NOT NULL
            );
            CREATE INDEX embeddings_hash_idx ON embeddings(content_hash);
            """
        )
        connection.execute(
            "INSERT INTO meta(schema_version, built_at, enabled) VALUES (?, NULL, 0)",
            (SCHEMA_VERSION,),
        )
        connection.commit()

    def _prepare(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT schema_version FROM meta LIMIT 1"
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    raise
                row = None
            if row is None or int(row[0]) != SCHEMA_VERSION:
                was_enabled = self.active_marker.exists()
                self.rebuild_marker.touch(exist_ok=True)
                self._reset(connection)
                if was_enabled:
                    connection.execute("UPDATE meta SET enabled = 1")
                    connection.commit()
                self.rebuild_marker.unlink(missing_ok=True)
            return connection
        except sqlite3.DatabaseError as exc:
            if connection is not None:
                connection.close()
            if not any(
                marker in str(exc).lower()
                for marker in (
                    "database disk image is malformed",
                    "file is not a database",
                    "database corrupt",
                    "malformed database schema",
                )
            ):
                raise RuntimeError(f"cannot prepare semantic index: {exc}") from exc
            quarantine = Path(f"{self.db_path}.corrupt-{time.time_ns()}-{os.getpid()}")
            for source, destination in (
                (self.db_path, quarantine),
                (Path(f"{self.db_path}-wal"), Path(f"{quarantine}-wal")),
                (Path(f"{self.db_path}-shm"), Path(f"{quarantine}-shm")),
            ):
                try:
                    source.replace(destination)
                except FileNotFoundError:
                    pass
            replacement = self._connect()
            self._reset(replacement)
            if self.active_marker.exists():
                replacement.execute("UPDATE meta SET enabled = 1")
                replacement.commit()
            self._recovered_corruption = True
            return replacement

    def _enabled(self, connection: sqlite3.Connection) -> bool:
        row = connection.execute("SELECT enabled FROM meta LIMIT 1").fetchone()
        return bool(row and int(row[0]))

    def status(self) -> dict[str, Any]:
        if self.embedding_provider is None:
            return {
                "available": False,
                "active": False,
                "indexing": False,
                "model": None,
                "reason": "semantic search unavailable: no embedding API key configured; set WIKI_EMBEDDINGS_API_KEY to enable it",
            }
        if self._semantic_error:
            return {
                "available": False,
                "active": self.active_marker.exists(),
                "indexing": False,
                "model": provider_model(self.embedding_provider),
                "reason": f"semantic search unavailable: {self._semantic_error}",
            }
        connection = self._prepare()
        try:
            row = connection.execute(
                "SELECT enabled, built_at FROM meta LIMIT 1"
            ).fetchone()
            active = self.active_marker.exists() or bool(row and int(row["enabled"]))
            built = bool(row and row["built_at"])
        finally:
            connection.close()
        if active and not built:
            return {
                "available": False,
                "active": True,
                "indexing": True,
                "model": provider_model(self.embedding_provider),
                "reason": "semantic index is indexing; using lexical search until the first refresh completes",
            }
        return {
            "available": active,
            "active": active,
            "indexing": False,
            "model": provider_model(self.embedding_provider),
            "reason": None if active else "semantic search is disabled until enabled by an explicit action",
        }

    def reset_for_explicit_rebuild(self) -> bool:
        if self.embedding_provider is None:
            return False
        connection = self._prepare()
        try:
            connection.execute("DELETE FROM embeddings")
            connection.execute("UPDATE meta SET built_at = NULL, enabled = 1")
            connection.commit()
            self.active_marker.touch(exist_ok=True)
        finally:
            connection.close()
        return True

    def refresh(self, notes: Sequence[SemanticNote]) -> SemanticRefreshStats:
        """Sweep notes and commit each successful provider batch separately."""

        stats = SemanticRefreshStats()
        notes = tuple(notes)
        connection = self._prepare()
        try:
            if not self._enabled(connection) or self.embedding_provider is None:
                return stats
            existing = {
                str(row["path"]): row
                for row in connection.execute(
                    "SELECT path, content_hash, model FROM embeddings"
                )
            }
            current_paths = {note.path for note in notes}
            for stale_path in sorted(set(existing) - current_paths):
                connection.execute("DELETE FROM embeddings WHERE path = ?", (stale_path,))
            connection.commit()

            model = provider_model(self.embedding_provider)
            pending = [
                note
                for note in notes
                if (
                    existing.get(note.path) is None
                    or str(existing[note.path]["content_hash"]) != note.content_hash
                    or str(existing[note.path]["model"]) != model
                )
            ]
            for note in pending:
                connection.execute("DELETE FROM embeddings WHERE path = ?", (note.path,))
            connection.commit()

            def store(note: SemanticNote, vector: Sequence[float], model: str) -> bool:
                try:
                    packed = pack_vector(vector)
                except Exception as exc:
                    stats.embeddings_skipped += 1
                    LOGGER.warning("semantic embedding rejected for %s: %s", note.path, exc)
                    return False
                connection.execute(
                    """
                    INSERT INTO embeddings(
                        path, content_hash, model, dimension, vector,
                        title, snippet, ticket, embedded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        content_hash=excluded.content_hash,
                        model=excluded.model,
                        dimension=excluded.dimension,
                        vector=excluded.vector,
                        title=excluded.title,
                        snippet=excluded.snippet,
                        ticket=excluded.ticket,
                        embedded_at=excluded.embedded_at
                    """,
                    (
                        note.path,
                        note.content_hash,
                        model,
                        len(packed) // 4,
                        packed,
                        note.title,
                        note.snippet,
                        note.ticket,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                return True

            for start in range(0, len(pending), MAX_EMBED_BATCH_SIZE):
                batch = pending[start : start + MAX_EMBED_BATCH_SIZE]
                texts = [f"{note.title}\n{note.text}" for note in batch]
                try:
                    vectors = self.embedding_provider.embed(
                        [text[:MAX_EMBED_TEXT_CHARS] for text in texts]
                    )
                    if len(vectors) != len(batch):
                        raise EmbeddingUnavailable(
                            "embedding provider returned the wrong number of vectors"
                        )
                except Exception as exc:
                    # Retry one note at a time. A provider rejection must not
                    # discard valid notes from the same batch.
                    LOGGER.warning("semantic embedding batch rejected: %s", exc)
                    accepted = 0
                    for note in batch:
                        try:
                            single = self.embedding_provider.embed(
                                [f"{note.title}\n{note.text}"[:MAX_EMBED_TEXT_CHARS]]
                            )
                            if len(single) != 1:
                                raise EmbeddingUnavailable(
                                    "embedding provider returned the wrong number of vectors"
                                )
                            accepted += int(store(note, single[0], model))
                        except Exception as single_exc:
                            stats.embeddings_skipped += 1
                            LOGGER.warning(
                                "semantic embedding rejected for %s: %s",
                                note.path,
                                single_exc,
                            )
                    connection.commit()
                    stats.embeddings_indexed += accepted
                    continue

                accepted = sum(
                    int(store(note, vector, model))
                    for note, vector in zip(batch, vectors, strict=True)
                )
                connection.commit()
                stats.embeddings_indexed += accepted
            connection.execute(
                "UPDATE meta SET built_at = ? WHERE enabled = 1",
                (datetime.now(timezone.utc).isoformat(),),
            )
            connection.commit()
            if stats.embeddings_skipped:
                stats.semantic_unavailable = 1
            self._semantic_error = None
            self._recovered_corruption = False
            return stats
        finally:
            connection.close()

    def search(
        self,
        query: str,
        *,
        ticket: str | None = None,
        limit: int = 20,
        score_floor: float = SEMANTIC_SCORE_FLOOR,
    ) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise ValueError("query must contain a searchable word")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        status = self.status()
        connection = self._prepare()
        try:
            recovered = self._recovered_corruption
            if not status["available"]:
                return {
                    "results": [],
                    "semantic": status,
                    "rebuilding": recovered,
                    "stale": recovered,
                }
            query_vectors = self.embedding_provider.embed([query])
            if len(query_vectors) != 1:
                raise EmbeddingUnavailable(
                    "embedding provider returned the wrong number of vectors"
                )
            query_vector = normalise_vector(query_vectors[0])
            heap: list[tuple[float, _ReversePath, str]] = []
            for row in connection.execute(
                "SELECT path, ticket, dimension, vector FROM embeddings"
            ):
                if ticket and str(row["ticket"] or "").upper() != ticket.upper():
                    continue
                try:
                    vector = unpack_vector(bytes(row["vector"]), int(row["dimension"]))
                    score = max(-1.0, min(1.0, cosine_normalized(query_vector, vector)))
                except (EmbeddingUnavailable, TypeError, ValueError):
                    continue
                if score < score_floor:
                    continue
                path = str(row["path"])
                item = (score, _ReversePath(path), path)
                if len(heap) < limit:
                    heapq.heappush(heap, item)
                elif item > heap[0]:
                    heapq.heapreplace(heap, item)
            ranked = sorted(heap, key=lambda item: (-item[0], item[2]))
            if not ranked:
                return {
                    "results": [],
                    "semantic": self.status(),
                    "rebuilding": recovered,
                    "stale": recovered,
                }
            paths = [path for _score, _reverse_path, path in ranked]
            placeholders = ",".join("?" for _ in paths)
            metadata = {
                str(row["path"]): row
                for row in connection.execute(
                    f"SELECT path, title, snippet FROM embeddings WHERE path IN ({placeholders})",
                    paths,
                )
            }
            return {
                "results": [
                    {
                        "kind": "note",
                        "citation": path,
                        "path": path,
                        "title": str(metadata[path]["title"]),
                        "heading": None,
                        "snippet": str(metadata[path]["snippet"])[:500],
                        "score": round(score, 8),
                    }
                    for score, _reverse_path, path in ranked
                    if path in metadata
                ],
                "semantic": self.status(),
                "rebuilding": recovered,
                "stale": recovered,
            }
        except Exception as exc:
            if isinstance(exc, EmbeddingUnavailable):
                self._semantic_error = str(exc)
                return {"results": [], "semantic": self.status(), "stale": True}
            raise
        finally:
            connection.close()

    def search_with_fallback(
        self,
        query: str,
        *,
        ticket: str | None = None,
        limit: int = 20,
        score_floor: float = SEMANTIC_SCORE_FLOOR,
    ) -> dict[str, Any]:
        query = query.strip()
        if not query:
            error = "query must contain a searchable word"
            raise self.query_error(error) if self.query_error else ValueError(error)
        if not 1 <= limit <= MAX_SEARCH_LIMIT:
            error = f"limit must be between 1 and {MAX_SEARCH_LIMIT}"
            raise self.query_error(error) if self.query_error else ValueError(error)
        if ticket is not None and not ticket.strip():
            error = "ticket must not be empty"
            raise self.query_error(error) if self.query_error else ValueError(error)
        try:
            payload = self.search(
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
            fallback_results = (
                self.lexical_fallback(query, ticket, limit)
                if self.lexical_fallback is not None
                else []
            )
        except Exception:
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
