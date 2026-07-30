"""Deterministic semantic index tests with no network access."""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import runpy
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing, redirect_stdout, suppress
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from backend.app import knowledge, main
from backend.app import semantic_index as semantic_index_module


class FakeEmbeddingProvider:
    model = "fixture-v1"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return self._vectors(texts)

    @staticmethod
    def _vectors(texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            lowered = text.lower()
            vectors.append(
                [
                    1.0 if "database" in lowered or "sqlite" in lowered else 0.0,
                    1.0 if "garden" in lowered or "plants" in lowered else 0.0,
                ]
            )
        return vectors


class BlockingEmbeddingProvider(FakeEmbeddingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.first_started = threading.Event()
        self.second_started = threading.Event()
        self.release = threading.Event()

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if len(self.calls) == 1:
            self.first_started.set()
        else:
            self.second_started.set()
        if not self.release.wait(timeout=2):
            raise RuntimeError("fixture provider was not released")
        return self._vectors(texts)


class RejectingEmbeddingProvider(FakeEmbeddingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.reject = True

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.reject and any("reject-this-note" in text for text in texts):
            self.calls.append(list(texts))
            raise RuntimeError("fixture rejected one note")
        return super().embed(texts)


def write_note(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {path.stem}\n\n{body}\n", encoding="utf-8")


class SemanticIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.vault = root / "vault"
        self.archive = root / "archive"
        self.runtime = root / "runtime"
        self.db = root / "knowledge.db"
        for path in (self.vault, self.archive, self.runtime):
            path.mkdir()
        self.provider = FakeEmbeddingProvider()
        self.index = knowledge.KnowledgeIndex(
            knowledge.KnowledgePaths(
                db_path=self.db,
                vault_dir=self.vault,
                archive_dir=self.archive,
                runtime_dir=self.runtime,
            ),
            self.provider,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_hash_skip_and_changed_note_reembedding(self) -> None:
        write_note(self.vault / "db.md", "sqlite database notes")
        write_note(self.vault / "plants.md", "garden plants notes")

        first = self.index.rebuild()
        self.assertEqual(first.embeddings_indexed, 2)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.index.scan_vault().embeddings_indexed, 0)
        self.assertEqual(len(self.provider.calls), 1)

        write_note(self.vault / "db.md", "sqlite database changed")
        changed = self.index.scan_vault()
        self.assertEqual(changed.embeddings_indexed, 1)
        self.assertEqual(len(self.provider.calls), 2)

    def test_deleted_notes_are_pruned_from_embedding_store(self) -> None:
        write_note(self.vault / "db.md", "sqlite database notes")
        self.index.rebuild()
        (self.vault / "db.md").unlink()

        stats = self.index.scan_vault()
        self.assertEqual(stats.notes_deleted, 1)
        with closing(sqlite3.connect(f"{self.db}.semantic")) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0],
                0,
            )

    def test_cosine_ranking_is_deterministic_and_has_snippet_context(self) -> None:
        write_note(self.vault / "garden.md", "garden plants and soil")
        write_note(self.vault / "db.md", "sqlite database and indexes")
        self.index.rebuild()

        result = self.index.search_semantic("database query", limit=10)
        self.assertTrue(result["semantic"]["available"])
        self.assertEqual(result["results"][0]["path"], "db.md")
        self.assertIn("sqlite database", result["results"][0]["snippet"])
        self.assertEqual(result["results"][0]["score"], 1.0)

    def test_no_provider_degrades_with_explicit_status(self) -> None:
        write_note(self.vault / "db.md", "sqlite database notes")
        index = knowledge.KnowledgeIndex(
            knowledge.KnowledgePaths(
                db_path=self.db,
                vault_dir=self.vault,
                archive_dir=self.archive,
                runtime_dir=self.runtime,
            ),
            None,
        )
        index.rebuild()
        result = index.search_semantic("database")
        self.assertFalse(result["semantic"]["available"])
        self.assertIn("no embedding API key", result["semantic"]["reason"])
        self.assertEqual(result["fallback"], "lexical")
        self.assertEqual(result["results"][0]["path"], "db.md")

    def test_corrupt_embedding_database_is_rebuildable(self) -> None:
        write_note(self.vault / "db.md", "sqlite database notes")
        self.index.rebuild()
        semantic_db = Path(f"{self.db}.semantic")
        semantic_db.write_bytes(b"torn sqlite write")
        Path(f"{semantic_db}-wal").unlink(missing_ok=True)
        Path(f"{semantic_db}-shm").unlink(missing_ok=True)

        partial = self.index.search_semantic("database")
        self.assertTrue(partial["rebuilding"])
        self.assertEqual(partial["fallback"], "lexical")
        self.assertEqual(partial["results"][0]["path"], "db.md")
        self.index.refresh_all()
        self.assertEqual(
            self.index.search_semantic("database")["results"][0]["path"],
            "db.md",
        )

    def test_startup_does_not_call_injected_provider_until_explicit_activation(self) -> None:
        write_note(self.vault / "db.md", "sqlite database notes")
        provider = FakeEmbeddingProvider()
        index = knowledge.KnowledgeIndex(
            knowledge.KnowledgePaths(
                db_path=self.db,
                vault_dir=self.vault,
                archive_dir=self.archive,
                runtime_dir=self.runtime,
            ),
            provider,
        )
        index.refresh_all()
        self.assertEqual(provider.calls, [])
        self.assertTrue(index.semantic_index.reset_for_explicit_rebuild())
        index.refresh_all()
        self.assertEqual(len(provider.calls), 1)

    def test_explicit_rebuild_reports_indexing_and_uses_lexical_until_built(self) -> None:
        write_note(self.vault / "db.md", "sqlite database notes")
        self.assertTrue(self.index.semantic_index.reset_for_explicit_rebuild())
        status = self.index.semantic_status()
        self.assertTrue(status["active"])
        self.assertTrue(status["indexing"])
        self.assertFalse(status["available"])

        result = self.index.search_semantic("database")
        self.assertEqual(result["fallback"], "lexical")
        self.assertEqual(result["results"], [])
        self.assertEqual(self.provider.calls, [])

        self.index.refresh_all()
        status = self.index.semantic_status()
        self.assertTrue(status["available"])
        self.assertFalse(status["indexing"])

    def test_generic_openai_key_is_not_an_embedding_consent_signal(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "generic-fixture-key"},
            clear=True,
        ):
            index = knowledge.KnowledgeIndex.from_env(
                vault_dir=self.vault,
                archive_dir=self.archive,
                runtime_dir=self.runtime,
                env={
                    "OPENAI_API_KEY": "generic-fixture-key",
                    "WIKI_KNOWLEDGE_DB_PATH": str(self.db),
                },
            )
        self.assertIsNone(index.semantic_index.embedding_provider)
        self.assertFalse(index.semantic_status()["available"])

    def test_rejected_note_does_not_discard_successful_notes_and_retries(self) -> None:
        for number in range(33):
            body = (
                "sqlite database notes reject-this-note"
                if number == 10
                else "sqlite database notes"
            )
            write_note(self.vault / f"note-{number:02}.md", body)
        provider = RejectingEmbeddingProvider()
        index = knowledge.KnowledgeIndex(
            knowledge.KnowledgePaths(self.db, self.vault, self.archive, self.runtime),
            provider,
        )
        first = index.rebuild()
        self.assertEqual(first.embeddings_indexed, 32)
        self.assertEqual(first.embeddings_skipped, 1)
        with closing(sqlite3.connect(f"{self.db}.semantic")) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0],
                32,
            )
        provider.reject = False
        second = index.scan_vault()
        self.assertEqual(second.embeddings_indexed, 1)
        self.assertEqual(second.embeddings_skipped, 0)

    def test_refresh_is_single_flight_for_overlapping_callers(self) -> None:
        write_note(self.vault / "db.md", "sqlite database notes")
        provider = BlockingEmbeddingProvider()
        first = knowledge.KnowledgeIndex(
            knowledge.KnowledgePaths(self.db, self.vault, self.archive, self.runtime),
            provider,
        )
        self.assertTrue(first.semantic_index.reset_for_explicit_rebuild())
        second = knowledge.KnowledgeIndex(
            knowledge.KnowledgePaths(self.db, self.vault, self.archive, self.runtime),
            provider,
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(first.refresh_all)
            self.assertTrue(provider.first_started.wait(timeout=2))
            second_future = executor.submit(second.refresh_all)
            self.assertFalse(provider.second_started.wait(timeout=0.2))
            provider.release.set()
            results = [first_future.result(timeout=2), second_future.result(timeout=2)]
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(sum(result.embeddings_indexed for result in results), 1)

    def test_query_keeps_one_vector_per_note_and_returns_only_top_n(self) -> None:
        for number in range(500):
            write_note(
                self.vault / f"note-{number:03}.md",
                f"sqlite database notes row {number}",
            )
        self.index.rebuild()
        with closing(sqlite3.connect(f"{self.db}.semantic")) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0],
                500,
            )
        real_connect = sqlite3.connect

        class NoFetchAllCursor(sqlite3.Cursor):
            def fetchall(self):
                raise AssertionError("semantic query fetched all rows")

        class NoFetchAllConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                return super().cursor(factory=NoFetchAllCursor).execute(sql, parameters)

        def guarded_connect(*args, **kwargs):
            kwargs["factory"] = NoFetchAllConnection
            return real_connect(*args, **kwargs)

        with mock.patch.object(
            semantic_index_module.sqlite3,
            "connect",
            side_effect=guarded_connect,
        ):
            result = self.index.search_semantic("database", limit=5)
        self.assertTrue(result["semantic"]["available"])
        self.assertEqual(len(result["results"]), 5)
        self.assertEqual(
            [row["path"] for row in result["results"]],
            [f"note-{number:03}.md" for number in range(5)],
        )

    def test_semantic_endpoint_falls_back_without_scanning_when_key_is_missing(self) -> None:
        write_note(self.vault / "endpoint.md", "endpointneedle lexical context")
        env = {
            "WIKI_KNOWLEDGE_DB_PATH": str(self.db),
            "WIKI_VAULT_DIR": str(self.vault),
            "WIKI_AGENT_RUNTIME_DIR": str(self.runtime),
            "WIKI_AGENT_ARCHIVE_DIR": str(self.archive),
        }
        with mock.patch.dict(os.environ, env, clear=False):
            knowledge.KnowledgeIndex.from_env().rebuild()
            with mock.patch.object(knowledge.KnowledgeIndex, "refresh_all") as refresh:
                result = asyncio.run(
                    main.knowledge_search(q="endpointneedle", mode="semantic")
                )
        refresh.assert_not_called()
        self.assertFalse(result["semantic"]["available"])
        self.assertEqual(result["fallback"], "lexical")
        self.assertEqual(result["results"][0]["path"], "endpoint.md")

    def test_cli_semantic_search_does_not_index_an_unindexed_vault(self) -> None:
        write_note(self.vault / "db.md", "sqlite database notes")
        provider = FakeEmbeddingProvider()
        index = knowledge.KnowledgeIndex(
            knowledge.KnowledgePaths(self.db, self.vault, self.archive, self.runtime),
            provider,
        )
        cli = runpy.run_path(
            str(Path(__file__).resolve().parents[2] / "wiki"),
            run_name="wiki_test",
        )
        cli["cmd_search"].__globals__["_knowledge_index"] = lambda: index
        args = argparse.Namespace(
            mode="semantic",
            query="database",
            ticket=None,
            limit=20,
            json=True,
            kind="note",
            event_type=None,
            since=None,
        )
        with redirect_stdout(io.StringIO()):
            cli["cmd_search"](args)
        self.assertEqual(provider.calls, [])

    def test_all_search_entry_points_are_read_only_even_after_background_drain(self) -> None:
        write_note(self.vault / "db.md", "sqlite database private note")
        provider = FakeEmbeddingProvider()
        index = knowledge.KnowledgeIndex(
            knowledge.KnowledgePaths(self.db, self.vault, self.archive, self.runtime),
            provider,
        )

        cli = runpy.run_path(
            str(Path(__file__).resolve().parents[2] / "wiki"),
            run_name="wiki_read_only_test",
        )
        cli["cmd_search"].__globals__["_knowledge_index"] = lambda: index
        cli_args = argparse.Namespace(
            mode="semantic",
            query="database",
            ticket=None,
            limit=20,
            json=True,
            kind="note",
            event_type=None,
            since=None,
        )
        with redirect_stdout(io.StringIO()):
            cli["cmd_search"](cli_args)

        class Request:
            async def is_disconnected(self) -> bool:
                return False

        async def exercise_http_entry_points() -> None:
            with (
                mock.patch.object(knowledge.KnowledgeIndex, "from_env", return_value=index),
                mock.patch.object(main, "agents", return_value=[]),
                mock.patch.object(main.palette, "search", return_value=[]),
            ):
                await main.palette_search(Request(), q="database", mode="semantic")
                await main.knowledge_search(q="database", mode="semantic")

        asyncio.run(exercise_http_entry_points())
        self.assertFalse(index.refresh_marker.exists())
        self.assertEqual(provider.calls, [])

        async def drain_background_once() -> None:
            with mock.patch.object(knowledge, "KnowledgeIndex", return_value=index):
                task = asyncio.create_task(
                    knowledge.background_index_loop(index.paths, poll_seconds=0.01)
                )
                await asyncio.sleep(0.05)
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        asyncio.run(drain_background_once())
        self.assertEqual(provider.calls, [])

    def test_knowledge_index_annotations_resolve(self) -> None:
        from typing import get_type_hints

        get_type_hints(knowledge.KnowledgeIndex.__init__)


if __name__ == "__main__":
    unittest.main()
