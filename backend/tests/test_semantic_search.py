"""Deterministic semantic index tests with no network access."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend.app import knowledge


class FakeEmbeddingProvider:
    model = "fixture-v1"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
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
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM note_embeddings").fetchone()[0],
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
        self.db.write_bytes(b"torn sqlite write")

        partial = self.index.search_semantic("database")
        self.assertTrue(partial["rebuilding"])
        self.assertEqual(partial["results"], [])
        self.index.refresh_all()
        self.assertEqual(
            self.index.search_semantic("database")["results"][0]["path"],
            "db.md",
        )


if __name__ == "__main__":
    unittest.main()
